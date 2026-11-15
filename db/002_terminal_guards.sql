-- 002_terminal_guards.sql
-- STATUS: applied to Supabase on 2026-11-15; test script passed 19/19.
-- 001_init.sql is applied and must not change: this is the new
-- migration. Run db/002_terminal_guards_check.sql FIRST (read-only): if existing
-- rows already break these rules, the new triggers do not look back at them, but
-- you should know about it.
-- Not re-runnable on purpose, like 001: plain CREATE statements fail loudly if the
-- objects already exist. Everything runs in one transaction.
-- Prove it with db/002_terminal_guards_test.sql (BEGIN ... ROLLBACK, leaves nothing).

begin;

-- ---------------------------------------------------------------------------
-- 1. One terminal execution event per command
-- Why: a command's run ends exactly once. Two writers (a worker and a
-- reconciliation run, or two reconciliation runs) must not both record an end,
-- and nothing may be recorded about an execution after it has ended.
-- On every insert of execution_started / execution_completed / execution_failed
-- the trigger takes a transaction-scoped advisory lock keyed on the command, so
-- two such inserts for the SAME command run one after the other, then rejects the
-- insert if execution_completed or execution_failed already exists for it.
-- Without the lock, two concurrent transactions could both see "no terminal event
-- yet" and both insert one. The lock is released at commit or rollback.
-- This relies on READ COMMITTED (the PostgreSQL and Supabase default): the check
-- runs after the lock is obtained and so sees the other transaction's committed
-- event. Under REPEATABLE READ or SERIALIZABLE the snapshot could be too old.
-- Other event types (command_received, plan_generated, validator_result,
-- critic_result) are not affected. search_path is pinned like the other trigger
-- functions; pg_catalog functions (hashtextextended, pg_advisory_xact_lock) are
-- always found.
-- ---------------------------------------------------------------------------

create function public.audit_events_block_second_terminal() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  perform pg_advisory_xact_lock(
    hashtextextended('audit_events:' || new.command_id::text, 0)
  );

  if exists (
    select 1
    from public.audit_events e
    where e.command_id = new.command_id
      and e.event_type in ('execution_completed', 'execution_failed')
  ) then
    raise exception
      'command % already has a terminal execution event; % is not allowed',
      new.command_id, new.event_type;
  end if;

  return new;
end $$;

create trigger audit_events_one_terminal_execution
  before insert on public.audit_events
  for each row
  when (new.event_type in ('execution_started',
                           'execution_completed',
                           'execution_failed'))
  execute function public.audit_events_block_second_terminal();

-- ---------------------------------------------------------------------------
-- 2. A terminal status never changes
-- Why: completed, failed and rejected are final. Moving a command out of one of
-- them (for example failed -> executing) would hide what happened and could make
-- a worker run it again. Updates that keep the same status (claimed_by,
-- updated_at, plan_id) are still allowed; only a status CHANGE is rejected.
-- ---------------------------------------------------------------------------

create function public.commands_block_terminal_status_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception
    'command % is % and its status cannot change (to %)',
    old.id, old.status, new.status;
end $$;

create trigger commands_terminal_status_is_final
  before update on public.commands
  for each row
  when (old.status in ('completed', 'failed', 'rejected')
        and new.status is distinct from old.status)
  execute function public.commands_block_terminal_status_change();

commit;
