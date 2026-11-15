-- 003_status_event_consistency.sql
-- STATUS: NOT APPLIED. Prepared and tested on a throwaway local Postgres only.
-- 001 and 002 are applied and must not change: this is a new migration.
-- Run db/003_status_event_consistency_check.sql FIRST (read-only): the new triggers
-- only act on new writes and never look back, but you should know about existing rows
-- that already break these rules. Prove it with db/003_status_event_consistency_test.sql
-- (BEGIN ... ROLLBACK) and, on a local Postgres, db/003_two_session_test.sh.
-- Not re-runnable on purpose, like 001 and 002: plain CREATE statements fail loudly if
-- the objects already exist. Everything runs in one transaction.
--
-- Goal: a command's status and its terminal execution events never contradict each
-- other, and the two checks cannot race, because both lock the same commands row.
--
--   * An execution_completed event is accepted only while the command is 'executing'.
--   * An execution_failed event is accepted only while the command is 'approved' or
--     'executing'. 'approved' is allowed because the worker records a run that failed
--     BEFORE it reached 'executing' (the started insert failed, a pre-check refused,
--     approved -> executing did not apply) with execution_failed first, then
--     approved -> failed. Restricting it to 'executing' would break those paths.
--   * executing -> completed needs an execution_completed event; any other status
--     change to 'completed' is refused.
--   * executing -> failed and approved -> failed need an execution_failed event.
--     received -> failed (a planner failure, recorded by plan_generated) is unchanged.
--   * A status change to 'failed' is refused if an execution_completed event exists.
--
-- Locking. Event insert: 002's advisory lock (audit_events_one_terminal_execution
-- fires first, triggers run in name order), then SELECT ... FOR NO KEY UPDATE on the
-- commands row here. Status update: the UPDATE itself holds that lock mode (it does
-- not change a key column), and the trigger takes SELECT ... FOR NO KEY UPDATE on the
-- same row explicitly, so the lock is visible in the code. FOR NO KEY UPDATE conflicts
-- with itself, so the event insert and the status update for one command exclude each
-- other. The status path takes no advisory lock, so there is no lock cycle. A writer
-- that waits for the row lock re-reads the latest committed row afterwards.
-- Why not FOR UPDATE: the foreign key check of every audit_events insert holds
-- FOR KEY SHARE on the row until commit, and FOR UPDATE conflicts with it. Two
-- transactions that each insert a non-terminal event and then a terminal event for the
-- same command then deadlock (each holds KEY SHARE and wants FOR UPDATE, or waits on
-- 002's advisory lock). FOR NO KEY UPDATE does not conflict with KEY SHARE, so that
-- case now works (see db/003_two_session_test.sh, X6). It also lets inserts of other
-- events for the same command run while a status update is in flight, which they
-- could not with FOR UPDATE.
--
-- READ COMMITTED (the PostgreSQL and Supabase default) is assumed, as in 002: each
-- statement inside a trigger takes a fresh snapshot, so the existence checks see
-- events committed by the writer that was waited for. Under REPEATABLE READ or
-- SERIALIZABLE the snapshot could be too old. The functions are SECURITY INVOKER, so
-- the writer needs UPDATE on commands for the FOR NO KEY UPDATE (the service role has it).
-- search_path is pinned like the other trigger functions.

begin;

-- ---------------------------------------------------------------------------
-- 1. A terminal execution event only in the right status
-- ---------------------------------------------------------------------------

create function public.audit_events_terminal_requires_state() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_status text;
begin
  select c.status
    into v_status
    from public.commands c
   where c.id = new.command_id
     for no key update;

  if not found then
    raise exception 'command % does not exist; % is not allowed',
      new.command_id, new.event_type;
  end if;

  if new.event_type = 'execution_completed' and v_status <> 'executing' then
    raise exception
      'command % is %, not executing; execution_completed is not allowed',
      new.command_id, v_status;
  end if;

  if new.event_type = 'execution_failed'
     and v_status not in ('approved', 'executing') then
    raise exception
      'command % is %, not approved or executing; execution_failed is not allowed',
      new.command_id, v_status;
  end if;

  return new;
end $$;

create trigger audit_events_terminal_requires_state
  before insert on public.audit_events
  for each row
  when (new.event_type in ('execution_completed', 'execution_failed'))
  execute function public.audit_events_terminal_requires_state();

-- ---------------------------------------------------------------------------
-- 2. A status that ends a run only with the matching event
-- ---------------------------------------------------------------------------

create function public.commands_terminal_status_needs_event() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  -- The UPDATE already holds this row lock; taking it explicitly shows that this
  -- trigger and audit_events_terminal_requires_state serialise on the same row.
  perform 1 from public.commands c where c.id = old.id for no key update;

  if new.status = 'completed' then
    if old.status <> 'executing' then
      raise exception
        'command % is %; only an executing command can become completed',
        old.id, old.status;
    end if;
    if not exists (
      select 1 from public.audit_events e
       where e.command_id = old.id and e.event_type = 'execution_completed'
    ) then
      raise exception
        'command % has no execution_completed event; it cannot become completed',
        old.id;
    end if;
  elsif new.status = 'failed' then
    if exists (
      select 1 from public.audit_events e
       where e.command_id = old.id and e.event_type = 'execution_completed'
    ) then
      raise exception
        'command % has an execution_completed event; it cannot become failed',
        old.id;
    end if;
    if old.status in ('approved', 'executing') and not exists (
      select 1 from public.audit_events e
       where e.command_id = old.id and e.event_type = 'execution_failed'
    ) then
      raise exception
        'command % has no execution_failed event; % -> failed is not allowed',
        old.id, old.status;
    end if;
  end if;

  return new;
end $$;

create trigger commands_terminal_status_needs_event
  before update on public.commands
  for each row
  when (old.status is distinct from new.status
        and new.status in ('completed', 'failed'))
  execute function public.commands_terminal_status_needs_event();

commit;
