-- 003_status_event_consistency_test.sql
-- MANUAL TEST for db/003_status_event_consistency.sql. 003 was applied to Supabase on
-- 2026-11-15 (this test script passed 55/55, row counts unchanged).
-- Run it in the Supabase SQL editor AFTER 003 has been applied (before that, the
-- "rejected" cases show FAIL, which is also a useful proof that the test can fail).
--
-- It creates throwaway commands and events INSIDE a transaction and ends with
-- ROLLBACK, so nothing is kept. Rows that are rolled back are never published to
-- Realtime. Every statement that must be refused runs in a sub-block, so one
-- refusal does not abort the rest.
--
-- Read the result: every row of the last result set should say PASS.
-- If the editor shows only the result of the LAST statement (the ROLLBACK), use the
-- FALLBACK at the bottom instead: it reports through an error message, which also
-- aborts the transaction, so nothing is kept either.
--
-- Not covered here (one session cannot show it): the lock behaviour. Use
-- db/003_two_session_test.sh on a LOCAL Postgres for that.
--
-- 002's own test (db/002_terminal_guards_test.sql) still passes 19/19 on a local
-- Postgres after 003 is applied (its terminal events go to 'executing' commands), so
-- 003 does not change what 002 guarantees. Run both. Before 003 is applied, the
-- "rejected" rows of THIS script show FAIL (17 of the 55 rows on a 001 + 002 database),
-- which proves the script can fail.

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

-- Throwaway commands (fixed ids, rolled back at the end). Inserting a command with
-- any status is not restricted by 002 or 003.
--   ...3001 rec_a   received          ...3012 exe_f   executing
--   ...3002 rec_b   received          ...3013 exe_g   executing
--   ...3003 app_a   approved          ...3014 exe_h   executing
--   ...3004 app_b   approved          ...3015-3021    w1..w7, worker orders (D)
--   ...3005 app_c   approved          ...3022 done    completed (with its event)
--   ...3006 rej_a   rejected          ...3023 fail    failed (with its event)
--   ...3007-3011 exe_a..exe_e executing
insert into public.commands (id, text, status) values
  ('00000000-0000-4000-8000-000000003001', 'guard test rec_a', 'received'),
  ('00000000-0000-4000-8000-000000003002', 'guard test rec_b', 'received'),
  ('00000000-0000-4000-8000-000000003003', 'guard test app_a', 'approved'),
  ('00000000-0000-4000-8000-000000003004', 'guard test app_b', 'approved'),
  ('00000000-0000-4000-8000-000000003005', 'guard test app_c', 'approved'),
  ('00000000-0000-4000-8000-000000003006', 'guard test rej_a', 'rejected'),
  ('00000000-0000-4000-8000-000000003007', 'guard test exe_a', 'executing'),
  ('00000000-0000-4000-8000-000000003008', 'guard test exe_b', 'executing'),
  ('00000000-0000-4000-8000-000000003009', 'guard test exe_c', 'executing'),
  ('00000000-0000-4000-8000-000000003010', 'guard test exe_d', 'executing'),
  ('00000000-0000-4000-8000-000000003011', 'guard test exe_e', 'executing'),
  ('00000000-0000-4000-8000-000000003012', 'guard test exe_f', 'executing'),
  ('00000000-0000-4000-8000-000000003013', 'guard test exe_g', 'executing'),
  ('00000000-0000-4000-8000-000000003014', 'guard test exe_h', 'executing'),
  ('00000000-0000-4000-8000-000000003015', 'guard test w1', 'approved'),
  ('00000000-0000-4000-8000-000000003016', 'guard test w2', 'approved'),
  ('00000000-0000-4000-8000-000000003017', 'guard test w3', 'executing'),
  ('00000000-0000-4000-8000-000000003018', 'guard test w4', 'executing'),
  ('00000000-0000-4000-8000-000000003019', 'guard test w5', 'executing'),
  ('00000000-0000-4000-8000-000000003020', 'guard test w6', 'received'),
  ('00000000-0000-4000-8000-000000003021', 'guard test w7', 'approved'),
  ('00000000-0000-4000-8000-000000003022', 'guard test done', 'executing'),
  ('00000000-0000-4000-8000-000000003023', 'guard test fail', 'executing');

-- Histories that need a legitimate order: the event while 'executing', then the status.
-- (Plain statements, not attempts: if setup is refused the whole script stops.)
insert into public.audit_events (command_id, event_type, message) values
  ('00000000-0000-4000-8000-000000003022', 'execution_completed', 'setup'),
  ('00000000-0000-4000-8000-000000003023', 'execution_failed', 'setup'),
  ('00000000-0000-4000-8000-000000003007', 'execution_completed', 'setup'),
  ('00000000-0000-4000-8000-000000003009', 'execution_failed', 'setup');
update public.commands set status = 'completed' where id = '00000000-0000-4000-8000-000000003022';
update public.commands set status = 'failed'    where id = '00000000-0000-4000-8000-000000003023';

-- ---- A. A terminal execution event only in the right status -----------------------

insert into guard_results (test, expected, actual) values
  ('A1 execution_completed on a received command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003001', 'execution_completed', 't') $q$)),
  ('A2 execution_failed on a received command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003002', 'execution_failed', 't') $q$)),
  ('A3 execution_completed on an approved command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003003', 'execution_completed', 't') $q$)),
  ('A4 execution_failed on an approved command (a run that failed before executing)', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003004', 'execution_failed', 't') $q$)),
  ('A5 execution_completed on an executing command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003008', 'execution_completed', 't') $q$)),
  ('A6 execution_failed on an executing command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003010', 'execution_failed', 't') $q$)),
  ('A7 execution_completed on a rejected command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003006', 'execution_completed', 't') $q$)),
  ('A8 execution_failed on a rejected command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003006', 'execution_failed', 't') $q$)),
  ('A9 execution_completed on a completed command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003022', 'execution_completed', 't') $q$)),
  ('A10 execution_failed on a completed command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003022', 'execution_failed', 't') $q$)),
  ('A11 execution_failed on a failed command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003023', 'execution_failed', 't') $q$)),
  ('A12 execution_completed on a failed command', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003023', 'execution_completed', 't') $q$)),
  ('A13 a terminal event for a command that does not exist', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000009999', 'execution_failed', 't') $q$));

-- ---- A'. Events that are not terminal are not restricted by 003 -------------------

insert into guard_results (test, expected, actual) values
  ('A14 execution_started on a received command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003001', 'execution_started', 't') $q$)),
  ('A15 execution_started on an approved command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003005', 'execution_started', 't') $q$)),
  ('A16 execution_started on an executing command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003012', 'execution_started', 't') $q$)),
  ('A17 plan_generated on a completed command', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003022', 'plan_generated', 't') $q$)),
  ('A18 execution_started after a terminal event (002 still rejects)', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003022', 'execution_started', 't') $q$));

-- ---- B. A status that ends a run needs the matching event --------------------------

insert into guard_results (test, expected, actual) values
  ('B1 executing -> completed with an execution_completed event', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003007' $q$)),
  ('B2 executing -> completed with no terminal event', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003011' $q$)),
  ('B3 executing -> completed with only an execution_failed event', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003009' $q$)),
  ('B4 executing -> failed with an execution_failed event', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003009' $q$)),
  ('B5 executing -> failed with no terminal event', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003013' $q$)),
  ('B6 executing -> failed with only an execution_completed event', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003008' $q$)),
  ('B7 approved -> failed with an execution_failed event', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003004' $q$)),
  ('B8 approved -> failed with no event', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003005' $q$)),
  ('B9 approved -> completed', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003003' $q$)),
  ('B10 received -> completed', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003001' $q$)),
  ('B11 received -> failed (planner failure, no execution event)', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003002' $q$));

-- ---- C. Other changes are untouched, and 002 still holds ---------------------------

insert into guard_results (test, expected, actual) values
  ('C1 received -> approved', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'approved'
     where id = '00000000-0000-4000-8000-000000003001' $q$)),
  ('C2 approved -> executing', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'executing'
     where id = '00000000-0000-4000-8000-000000003005' $q$)),
  ('C3 an executing command, a column other than status', 'accepted',
   pg_temp.attempt($q$ update public.commands set claimed_by = 'x'
     where id = '00000000-0000-4000-8000-000000003014' $q$)),
  ('C4 an approved command, columns other than status', 'accepted',
   pg_temp.attempt($q$ update public.commands set claimed_by = 'x', plan_id = gen_random_uuid()
     where id = '00000000-0000-4000-8000-000000003003' $q$)),
  ('C5 completed -> failed (002 rejects)', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003022' $q$)),
  ('C6 failed -> completed (002 rejects)', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003023' $q$)),
  ('C7 completed -> executing (002 rejects)', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'executing'
     where id = '00000000-0000-4000-8000-000000003022' $q$)),
  ('C8 a second terminal event on an executing command (002 rejects)', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003010', 'execution_completed', 't') $q$));

-- ---- D. The orders the worker and the reconcile script really use ------------------
-- One INSERT per step, so the steps run in the order written.

-- D1 success (dry run / simulation): started, approved -> executing, completed, executing -> completed
insert into guard_results (test, expected, actual) values
  ('D1.1 execution_started while approved', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003015', 'execution_started', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D1.2 approved -> executing', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'executing'
     where id = '00000000-0000-4000-8000-000000003015' $q$));
insert into guard_results (test, expected, actual) values
  ('D1.3 execution_completed while executing', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003015', 'execution_completed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D1.4 executing -> completed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003015' $q$));

-- D2 a run that failed before executing: execution_failed while approved, then approved -> failed
insert into guard_results (test, expected, actual) values
  ('D2.1 execution_failed while approved', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003016', 'execution_failed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D2.2 approved -> failed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003016' $q$));

-- D3 a run that failed while executing
insert into guard_results (test, expected, actual) values
  ('D3.1 execution_failed while executing', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003017', 'execution_failed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D3.2 executing -> failed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003017' $q$));

-- D4 reconcile, no terminal event: execution_failed, then executing -> failed
insert into guard_results (test, expected, actual) values
  ('D4.1 reconcile writes execution_failed while executing', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003018', 'execution_failed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D4.2 reconcile: executing -> failed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003018' $q$));

-- D5 reconcile, the terminal event is already there: status only
insert into guard_results (test, expected, actual) values
  ('D5.1 execution_completed recorded earlier by the worker', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003019', 'execution_completed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D5.2 reconcile: executing -> completed, no event written', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003019' $q$));

-- D6 planner failure: plan_generated, then received -> failed
insert into guard_results (test, expected, actual) values
  ('D6.1 plan_generated (planner failure)', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003020', 'plan_generated', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D6.2 received -> failed', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003020' $q$));

-- D7 the status update after execution_failed was lost; the command is still approved
insert into guard_results (test, expected, actual) values
  ('D7.1 execution_failed while approved', 'accepted',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003021', 'execution_failed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D7.2 an execution_completed is refused for it', 'rejected',
   pg_temp.attempt($q$ insert into public.audit_events (command_id, event_type, message)
     values ('00000000-0000-4000-8000-000000003021', 'execution_completed', 't') $q$));
insert into guard_results (test, expected, actual) values
  ('D7.3 approved -> completed is refused', 'rejected',
   pg_temp.attempt($q$ update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000003021' $q$));
insert into guard_results (test, expected, actual) values
  ('D7.4 approved -> failed finishes it', 'accepted',
   pg_temp.attempt($q$ update public.commands set status = 'failed'
     where id = '00000000-0000-4000-8000-000000003021' $q$));

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
--   raise exception E'003 guard test report (transaction aborted on purpose):\n%', report;
-- end $$;
-- ---------------------------------------------------------------------------
