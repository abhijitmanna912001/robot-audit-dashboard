-- 004_command_submitted_by_test.sql
-- MANUAL TEST for db/004_command_submitted_by.sql. Run it in the Supabase SQL editor AFTER 004
-- is applied (before that, every submitted_* case shows FAIL, which proves the test can fail).
-- It creates throwaway rows INSIDE a transaction and ends with ROLLBACK, so nothing is kept
-- (rolled-back rows are never published to Realtime). Each statement that must be refused runs
-- in a sub-block. Read the result: every row of the last result set should say PASS.
-- If the editor shows only the result of the LAST statement (the ROLLBACK), use the FALLBACK at
-- the bottom (reports through an error message, which also aborts the transaction).
-- 002 and 003 are exercised too, to show 004 did not change what they guarantee.

begin;

create temp table results (
  n        int generated always as identity,
  test     text not null,
  expected text not null check (expected in ('accepted', 'rejected')),
  actual   text
) on commit drop;

create function pg_temp.attempt(stmt text) returns text
language plpgsql
as $$
begin
  execute stmt;
  return 'accepted';
exception when others then
  return 'rejected: ' || sqlerrm;
end $$;

-- Throwaway ids (rolled back).
--   ...4001 named (submitted_by + id)   ...4003 old (both null)   ...4004 rec (received, for 002/003)
insert into public.commands (id, text, submitted_by, submitted_by_id)
values ('00000000-0000-4000-8000-000000004001', 'named', 'Abhijit', '00000000-0000-4000-8000-0000000000a1');
insert into public.commands (id, text) values ('00000000-0000-4000-8000-000000004003', 'old');
insert into public.commands (id, text) values ('00000000-0000-4000-8000-000000004004', 'rec');

insert into results (test, expected, actual) values
 ('insert with both columns', 'accepted', pg_temp.attempt($q$
    insert into public.commands (text, submitted_by, submitted_by_id)
    values ('x', 'Garvit', '00000000-0000-4000-8000-0000000000a2') $q$)),
 ('insert with neither column (older shape)', 'accepted', pg_temp.attempt($q$
    insert into public.commands (text) values ('x') $q$)),
 ('empty submitted_by', 'rejected', pg_temp.attempt($q$
    insert into public.commands (text, submitted_by) values ('x', '') $q$)),
 ('blank submitted_by', 'rejected', pg_temp.attempt($q$
    insert into public.commands (text, submitted_by) values ('x', '   ') $q$)),
 ('41-character submitted_by', 'rejected', pg_temp.attempt($q$
    insert into public.commands (text, submitted_by) values ('x', repeat('n', 41)) $q$)),
 ('40-character submitted_by', 'accepted', pg_temp.attempt($q$
    insert into public.commands (text, submitted_by) values ('x', repeat('n', 40)) $q$)),
 ('submitted_by_id must be a uuid', 'rejected', pg_temp.attempt($q$
    insert into public.commands (text, submitted_by_id) values ('x', 'not-a-uuid') $q$)),
 ('change submitted_by', 'rejected', pg_temp.attempt($q$
    update public.commands set submitted_by = 'Other'
     where id = '00000000-0000-4000-8000-000000004001' $q$)),
 ('change submitted_by_id', 'rejected', pg_temp.attempt($q$
    update public.commands set submitted_by_id = '00000000-0000-4000-8000-0000000000a9'
     where id = '00000000-0000-4000-8000-000000004001' $q$)),
 ('clear submitted_by', 'rejected', pg_temp.attempt($q$
    update public.commands set submitted_by = null
     where id = '00000000-0000-4000-8000-000000004001' $q$)),
 ('fill submitted_by on an older command', 'rejected', pg_temp.attempt($q$
    update public.commands set submitted_by = 'Late'
     where id = '00000000-0000-4000-8000-000000004003' $q$)),
 ('update that leaves them unchanged (status received -> approved)', 'accepted', pg_temp.attempt($q$
    update public.commands set status = 'approved'
     where id = '00000000-0000-4000-8000-000000004001' $q$)),
 ('002/003: received -> completed still refused', 'rejected', pg_temp.attempt($q$
    update public.commands set status = 'completed'
     where id = '00000000-0000-4000-8000-000000004004' $q$)),
 ('003: execution_completed on a received command still refused', 'rejected', pg_temp.attempt($q$
    insert into public.audit_events (command_id, event_type, message)
    values ('00000000-0000-4000-8000-000000004004', 'execution_completed', 'm') $q$)),
 ('command_received event with the new payload fields', 'accepted', pg_temp.attempt($q$
    insert into public.audit_events (command_id, event_type, message, payload)
    values ('00000000-0000-4000-8000-000000004001', 'command_received', 'm',
            '{"source":"dashboard","submitted_by":"Abhijit","submitted_by_id":"00000000-0000-4000-8000-0000000000a1"}') $q$));

select n, test, expected, actual,
       case when (expected = 'accepted' and actual = 'accepted')
              or (expected = 'rejected' and actual like 'rejected:%')
            then 'PASS' else 'FAIL' end as result
  from results order by n;

rollback;

-- FALLBACK (use if the editor shows only the last result): run the block above WITHOUT the final
-- select and rollback, then run this single statement. It raises an error that lists every FAIL
-- (or says ALL PASS), and the error aborts the transaction, so nothing is kept either.
--
-- do $$
-- declare v_fail text;
-- begin
--   select string_agg(n || ' ' || test, '; ') into v_fail from results
--    where not ((expected = 'accepted' and actual = 'accepted')
--            or (expected = 'rejected' and actual like 'rejected:%'));
--   raise exception '%', coalesce('FAIL: ' || v_fail, 'ALL PASS');
-- end $$;
