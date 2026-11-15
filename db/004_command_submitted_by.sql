-- 004_command_submitted_by.sql
-- STATUS: written, NOT applied to Supabase. Apply it BEFORE deploying the code that writes these
-- columns (POST /api/commands inserts submitted_by and submitted_by_id; until this runs, that
-- insert fails with a generic 500 and nothing is saved). Reading is unaffected: the dashboard
-- uses select *.
-- Order: db/004_command_submitted_by_check.sql (read-only), then this file, then
-- db/004_command_submitted_by_test.sql (BEGIN ... ROLLBACK).
-- 001, 002 and 003 are applied and must not change: this is a new migration. Not re-runnable on
-- purpose (plain ALTER/CREATE fail loudly if the objects already exist), one transaction.
--
-- Who submitted a command. NEVER an email: anon can SELECT commands and audit_events (001), so
-- both columns are publicly readable.
--   submitted_by     operator display name, 1 to 40 characters once trimmed
--   submitted_by_id  Supabase Auth user id (uuid); also what the rate limit counts by
-- Both are nullable, so every older command stays valid. There is deliberately no foreign key to
-- auth.users: deleting an Auth user must never touch or block the audit record.
--
-- No conflict with 002 or 003. Those triggers are:
--   audit_events_one_terminal_execution   BEFORE INSERT on audit_events (terminal events only)
--   commands_terminal_status_is_final     BEFORE UPDATE on commands (old status terminal)
--   audit_events_terminal_requires_state  BEFORE INSERT on audit_events (terminal events only)
--   commands_terminal_status_needs_event  BEFORE UPDATE on commands (status becomes completed/failed)
-- None fires on INSERT into commands, and the worker's UPDATEs never change the two new columns.
-- The immutability trigger below fires only when one of those columns changes.

begin;

alter table public.commands
  add column submitted_by text
    check (submitted_by is null or length(btrim(submitted_by)) between 1 and 40),
  add column submitted_by_id uuid;

-- Serves the rate-limit counts (rows by submitter in the last minute and day).
create index commands_submitter_created_idx
  on public.commands (submitted_by_id, created_at)
  where submitted_by_id is not null;

-- The record of who submitted cannot be rewritten after the insert.
create function public.commands_submitter_immutable() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'command %: submitted_by and submitted_by_id cannot be changed', old.id;
end $$;

create trigger commands_submitter_immutable
  before update on public.commands
  for each row
  when (old.submitted_by is distinct from new.submitted_by
        or old.submitted_by_id is distinct from new.submitted_by_id)
  execute function public.commands_submitter_immutable();

commit;
