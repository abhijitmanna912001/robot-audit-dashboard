-- 002_terminal_guards_check.sql
-- READ-ONLY. Run it (before applying 002) in the Supabase SQL editor to see whether
-- existing rows already break the rules the two new triggers enforce. It only
-- SELECTs: no insert, update, delete or DDL.
--
-- Read the result: no rows = nothing found. Any row is something to look at BEFORE
-- applying 002. The triggers only act on new writes; they do not change or reject
-- existing rows, so a violation found here stays as it is (audit_events is
-- append-only) and is only something to know and, if needed, to explain.
--
-- check_name:
--   more_than_one_terminal_event   trigger (a): a command with two or more
--                                  execution_completed / execution_failed events.
--   execution_event_after_terminal trigger (a): an execution_* event recorded after
--                                  the command's first terminal event. LIMITS: it orders
--                                  by created_at, so events with identical timestamps
--                                  can escape it (an event that is not strictly later
--                                  than the first terminal event is not reported).
--                                  There is no reliable ordering column: audit_events.id
--                                  is a random uuid. Since migration 003 the triggers
--                                  prevent this situation for new rows, so the check
--                                  only matters for rows written before 002 and 003
--                                  were applied (it found none).
--   status_contradicts_event       a terminal status that disagrees with the
--                                  terminal event (completed + execution_failed,
--                                  failed + execution_completed), or an
--                                  execution_* event on a rejected command.
--   INFO executing_with_terminal   NOT a violation: status still 'executing' but a
--                                  terminal event exists (what the reconcile
--                                  script's status-only path finishes).
--   INFO executing_without_terminal NOT a violation: 'executing' with no terminal
--                                  event: possibly moved; see worker/README.md.
--
-- Trigger (b) (a terminal status never changes) is about future UPDATEs, so
-- existing rows cannot violate it; there is no row to find for it.

with terminal as (
  select command_id,
         count(*)        as terminal_count,
         min(created_at) as first_terminal_at,
         bool_or(event_type = 'execution_completed') as has_completed,
         bool_or(event_type = 'execution_failed')    as has_failed
  from public.audit_events
  where event_type in ('execution_completed', 'execution_failed')
  group by command_id
)
select 'more_than_one_terminal_event' as check_name,
       t.command_id,
       t.terminal_count::text as detail
from terminal t
where t.terminal_count > 1

union all

select 'execution_event_after_terminal',
       e.command_id,
       e.event_type || ' at ' || e.created_at::text
from public.audit_events e
join terminal t on t.command_id = e.command_id
where e.event_type in ('execution_started', 'execution_completed', 'execution_failed')
  and e.created_at > t.first_terminal_at

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
   or c.status = 'rejected'

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
