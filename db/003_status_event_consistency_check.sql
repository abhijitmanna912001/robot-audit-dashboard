-- 003_status_event_consistency_check.sql
-- READ-ONLY. 003 was applied to Supabase on 2026-11-15 (this check showed no violations,
-- test script 55/55, row counts unchanged). It was run (before applying 003) in the
-- Supabase SQL editor to see whether existing rows already break the rules the two
-- new triggers enforce. It only SELECTs: no insert, update, delete or DDL.
--
-- Read the result: no rows = nothing found. Any row is something to look at BEFORE
-- applying 003. The triggers only act on new writes; they do not change or reject
-- existing rows, so a violation found here stays as it is (audit_events is
-- append-only) and is only something to know and, if needed, to explain.
--
-- check_name:
--   completed_without_completed_event   status 'completed' but no execution_completed
--                                       event (rule: executing -> completed needs it).
--   failed_with_execution_but_no_failed_event
--                                       status 'failed', the run had started
--                                       (execution_started exists) but there is no
--                                       execution_failed event (rule: executing/approved
--                                       -> failed needs it). A planner failure
--                                       (received -> failed) has no execution events
--                                       and is NOT reported.
--   status_contradicts_event            completed + execution_failed, or failed +
--                                       execution_completed.
--   terminal_event_on_never_executed    an execution_completed or execution_failed event
--                                       on a command that is 'received' or 'rejected'
--                                       (rule: terminal events only in approved /
--                                       executing).
--   execution_completed_not_executing   an execution_completed event on a command that
--                                       is 'approved' (it can only be written while
--                                       'executing').
--   more_than_one_terminal_event        also a 002 rule; repeated here so one run shows
--                                       everything.
--   INFO approved_with_execution_failed NOT a violation: a run failed before executing,
--                                       execution_failed is written and the status is
--                                       still 'approved' (the approved -> failed update
--                                       did not happen). 003 lets it be finished.
--   INFO executing_with_terminal        NOT a violation: status still 'executing' but a
--                                       terminal event exists (what the reconcile
--                                       script's status-only path finishes).
--   INFO executing_without_terminal     NOT a violation: 'executing' with no terminal
--                                       event: possibly moved; see worker/README.md.
--
-- What data cannot show: whether a terminal event was written while the command was
-- 'executing'. A completed command legitimately has its event and the status
-- 'completed', so that rule is checked by the test scripts, not by existing rows.

with terminal as (
  select command_id,
         count(*) as terminal_count,
         bool_or(event_type = 'execution_completed') as has_completed,
         bool_or(event_type = 'execution_failed')    as has_failed
  from public.audit_events
  where event_type in ('execution_completed', 'execution_failed')
  group by command_id
),
started as (
  select distinct command_id
  from public.audit_events
  where event_type = 'execution_started'
)
select 'completed_without_completed_event' as check_name,
       c.id as command_id,
       'status completed, terminal events: ' || coalesce(t.terminal_count, 0)::text as detail
from public.commands c
left join terminal t on t.command_id = c.id
where c.status = 'completed'
  and coalesce(t.has_completed, false) = false

union all

select 'failed_with_execution_but_no_failed_event',
       c.id,
       'status failed, execution_started recorded, no execution_failed'
from public.commands c
join started s on s.command_id = c.id
left join terminal t on t.command_id = c.id
where c.status = 'failed'
  and coalesce(t.has_failed, false) = false

union all

select 'status_contradicts_event',
       c.id,
       'status ' || c.status
         || case when t.has_completed then ', has execution_completed' else '' end
         || case when t.has_failed then ', has execution_failed' else '' end
from public.commands c
join terminal t on t.command_id = c.id
where (c.status = 'completed' and t.has_failed)
   or (c.status = 'failed' and t.has_completed)

union all

select 'terminal_event_on_never_executed',
       c.id,
       'status ' || c.status || ', terminal events: ' || t.terminal_count::text
from public.commands c
join terminal t on t.command_id = c.id
where c.status in ('received', 'rejected')

union all

select 'execution_completed_not_executing',
       c.id,
       'status ' || c.status || ' with execution_completed'
from public.commands c
join terminal t on t.command_id = c.id
where c.status = 'approved'
  and t.has_completed

union all

select 'more_than_one_terminal_event',
       t.command_id,
       t.terminal_count::text
from terminal t
where t.terminal_count > 1

union all

select 'INFO approved_with_execution_failed',
       c.id,
       'status approved, execution_failed recorded; approved -> failed not done'
from public.commands c
join terminal t on t.command_id = c.id
where c.status = 'approved'
  and t.has_failed

union all

select 'INFO executing_with_terminal',
       c.id,
       'status executing, terminal events: ' || t.terminal_count::text
from public.commands c
join terminal t on t.command_id = c.id
where c.status = 'executing'

union all

select 'INFO executing_without_terminal',
       c.id,
       'status executing since ' || c.updated_at::text
from public.commands c
where c.status = 'executing'
  and not exists (select 1 from terminal t where t.command_id = c.id)

order by 1, 2;
