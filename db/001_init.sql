-- 001_init.sql
-- Initial schema for the robot audit dashboard. Source: docs/schema-draft.md.
-- DRAFT: review before applying. Not re-runnable on purpose: plain CREATE
-- statements fail loudly if objects already exist, instead of silently skipping.
-- Everything runs in one transaction, so a failure leaves nothing half-applied.

begin;

-- ---------------------------------------------------------------------------
-- 1. Tables
-- Why: `commands` holds the current state of each command (updated in place);
-- `audit_events` is the append-only history. text + CHECK is used instead of
-- enums because the status and event lists are still moving, and changing a
-- CHECK is a one-line swap. payload is jsonb because the action and verdict
-- schemas are not agreed yet.
-- ---------------------------------------------------------------------------

create table public.commands (
  id          uuid primary key default gen_random_uuid(),
  text        text not null check (length(btrim(text)) > 0),
  status      text not null default 'received'
              check (status in ('received','approved','rejected',
                                'executing','completed','failed')),
  plan_id     uuid,                       -- null until the worker generates a plan
  claimed_by  text,                       -- worker instance id (doc section 3)
  claimed_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- on delete restrict: a command that has audit events can never be deleted,
-- which is intended for an audit trail.
create table public.audit_events (
  id          uuid primary key default gen_random_uuid(),
  command_id  uuid not null references public.commands(id) on delete restrict,
  event_type  text not null
              check (event_type in ('command_received','plan_generated',
                                    'validator_result','critic_result',
                                    'execution_started','execution_completed',
                                    'execution_failed')),
  message     text not null,
  payload     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 2. Indexes
-- Why: the dashboard reads a command's timeline (events by command, oldest
-- first) and lists commands by status, newest first.
-- ---------------------------------------------------------------------------

create index audit_events_command_created_idx
  on public.audit_events (command_id, created_at);

create index commands_status_created_idx
  on public.commands (status, created_at);

-- ---------------------------------------------------------------------------
-- 3. updated_at trigger on commands
-- Why: updated_at stays correct no matter which writer changes the row.
-- search_path is pinned (deviation from the doc) so the function cannot be
-- hijacked by objects in another schema; it only needs now(), which is built in.
-- ---------------------------------------------------------------------------

create function public.set_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end $$;

create trigger commands_set_updated_at
  before update on public.commands
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. Append-only trigger on audit_events
-- Why: enforces the audit log is never edited or deleted, even by the
-- service-role key (which bypasses RLS). Row-level, so it does NOT block
-- TRUNCATE; anon/authenticated lose TRUNCATE via the revokes in part 5.
-- ---------------------------------------------------------------------------

create function public.audit_events_block_mutation() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'audit_events is append-only';
end $$;

create trigger audit_events_no_update_delete
  before update or delete on public.audit_events
  for each row execute function public.audit_events_block_mutation();

-- ---------------------------------------------------------------------------
-- 5. Row Level Security
-- Why: the browser uses the anon key and must be read-only. With RLS on and
-- only SELECT policies, INSERT/UPDATE/DELETE are denied for anon and
-- authenticated. The service-role key bypasses RLS, so it needs no policies.
-- Note: because anon can SELECT, treat both tables as public. Do not store
-- secrets or personal data in `text` or `payload`.
-- ---------------------------------------------------------------------------

alter table public.commands     enable row level security;
alter table public.audit_events enable row level security;

create policy "anon can read commands"
  on public.commands for select to anon, authenticated using (true);

create policy "anon can read audit events"
  on public.audit_events for select to anon, authenticated using (true);

-- No insert, update or delete policies are created on purpose.

-- Defence in depth: also remove the table privileges, in case a write policy
-- is added by mistake later.
revoke insert, update, delete, truncate on public.commands     from anon, authenticated;
revoke insert, update, delete, truncate on public.audit_events from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 6. Realtime
-- Why: the dashboard subscribes to changes on both tables. Realtime applies
-- the SELECT policies above for the subscribing role.
-- ---------------------------------------------------------------------------

alter publication supabase_realtime add table public.commands;
alter publication supabase_realtime add table public.audit_events;

commit;
