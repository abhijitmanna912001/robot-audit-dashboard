-- 002_terminal_guards_test.sql
-- MANUAL TEST for db/002_terminal_guards.sql. Run it in the Supabase SQL editor
-- AFTER 002 has been applied (before that, the "rejected" cases show FAIL, which
-- is also a useful proof that the test can fail).
--
-- It creates throwaway commands and events INSIDE a transaction and ends with
-- ROLLBACK, so nothing is kept. Rows that are rolled back are never published to
-- Realtime. Every statement that must be refused runs in a savepoint-like
-- sub-block, so one refusal does not abort the rest.
--
-- Read the result: every row of the last result set should say PASS.
-- If the editor shows only the result of the LAST statement (the ROLLBACK), use
-- the FALLBACK at the bottom instead: it reports through an error message, which
-- also aborts the transaction, so nothing is kept either.
--
-- Concurrency (advisory lock) cannot be shown in one session. Optional proof with
-- two SQL editor tabs, using any real command id C that has no terminal event:
--   Tab 1:  begin;  insert into public.audit_events (command_id, event_type, message)
--                   values ('C', 'execution_failed', 'tab 1');       -- do not commit yet
--   Tab 2:  begin;  insert into public.audit_events (command_id, event_type, message)
--                   values ('C', 'execution_failed', 'tab 2');       -- waits on the lock
--   Tab 1:  commit;      -- tab 2 now fails with "already has a terminal execution event"
--   Tab 2:  rollback;
--   Then delete nothing: audit_events is append-only, so use a throwaway command
--   created for this and accept that its event stays (do NOT do this on a real command).

begin;

create temp table guard_results (
  n        int generated always as identity,
  test     text not null,
  expected text not null check (expected in ('accepted', 'rejected')),
  actual   text
) on commit drop;

-- Runs one statement; reports 'accepted', or 'rejected' plus the reason.
create function pg_temp.attempt(stmt text) returns text
language plpgsql
as $$
begin
  execute stmt;
  return 'accepted';
exception when others then
  return 'rejected: ' || sqlerrm;
end $$;

-- Throwaway commands (fixed ids, rolled back at the end).
insert into public.commands (id, text, status) values
  ('00000000-0000-4000-8000-0000000000a1', 'guard test A', 'executing'),
  ('00000000-0000-4000-8000-0000000000b1', 'guard test B', 'executing'),
  ('00000000-0000-4000-8000-0000000000c1', 'guard test C', 'completed'),
  ('00000000-0000-4000-8000-0000000000d1', 'guard test D', 'failed'),
  ('00000000-0000-4000-8000-0000000000e1', 'guard test E', 'rejected'),
  ('00000000-0000-4000-8000-0000000000f1', 'guard test F', 'executing'),
  ('00000000-0000-4000-8000-000000000091', 'guard test G', 'approved');

-- ---- Trigger (a): one terminal execution event per command ---------------------

insert into guard_results (test, expected, actual) values
  ('a1 execution_started on a fresh command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000a1', 'execution_started', 't') $q$)),
  ('a2 first terminal event (execution_failed)', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000a1', 'execution_failed', 't') $q$)),
  ('a3 second execution_failed for the same command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000a1', 'execution_failed', 't') $q$)),
  ('a4 execution_completed after execution_failed', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000a1', 'execution_completed', 't') $q$)),
  ('a5 execution_started after a terminal event', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000a1', 'execution_started', 't') $q$)),
  ('a6 a non-execution event after a terminal event', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000a1', 'plan_generated', 't') $q$)),
  ('a7 first execution_completed on another command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000b1', 'execution_completed', 't') $q$)),
  ('a8 second execution_completed for that command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000b1', 'execution_completed', 't') $q$)),
  ('a9 a terminal event for a command with only a non-terminal history', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-0000000000f1', 'execution_failed', 't') $q$));

-- ---- Trigger (b): a terminal status never changes -------------------------------

insert into guard_results (test, expected, actual) values
  ('b1 completed -> executing', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'executing'
     where id = '00000000-0000-4000-8000-0000000000c1' $q$)),
  ('b2 completed -> failed', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-0000000000c1' $q$)),
  ('b3 failed -> completed', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-0000000000d1' $q$)),
  ('b4 failed -> received', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'received'
     where id = '00000000-0000-4000-8000-0000000000d1' $q$)),
  ('b5 rejected -> approved', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'approved'
     where id = '00000000-0000-4000-8000-0000000000e1' $q$)),
  ('b6 failed -> failed with another column changed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed', claimed_by = 'x'
     where id = '00000000-0000-4000-8000-0000000000d1' $q$)),
  ('b7 a terminal command, a column other than status', 'accepted',
   pg_temp.attempt($q$ update public.commands set claimed_by = 'y'
     where id = '00000000-0000-4000-8000-0000000000c1' $q$)),
  ('b8 executing -> failed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-0000000000f1' $q$)),
  ('b9 failed -> completed after b8', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-0000000000f1' $q$)),
  ('b10 approved -> executing', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'executing'
     where id = '00000000-0000-4000-8000-000000000091' $q$));

select n, test, expected, actual,
       case when actual like expected || '%' then 'PASS' else 'FAIL' end as result
from guard_results
order by n;

rollback;

-- ---------------------------------------------------------------------------
-- FALLBACK (only if the editor shows nothing useful for the script above).
-- Copy everything from "begin;" to just before the final "select", then end with
-- this block INSTEAD of the select and the rollback. It raises an error whose
-- message is the report; the error aborts the transaction, so nothing is kept.
--
-- do $$
-- declare report text;
-- begin
--   select string_agg(
--            format('%s | expected %s | %s | %s', test, expected, actual,
--                   case when actual like expected || '%' then 'PASS' else 'FAIL' end),
--            E'\n' order by n)
--     into report from guard_results;
--   raise exception E'002 guard test report (transaction aborted on purpose):\n%', report;
-- end $$;
-- ---------------------------------------------------------------------------
