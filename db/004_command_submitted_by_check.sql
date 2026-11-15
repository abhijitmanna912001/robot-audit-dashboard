-- 004_command_submitted_by_check.sql
-- READ-ONLY (SELECT only: no insert, update, delete or DDL). Run it BEFORE 004 and again AFTER.
-- Result sets: 1 columns, 2 row counts, 3 triggers on the two tables, 4 rule violations.

-- 1. Before 004: no rows. After 004: two rows (both nullable, uuid / text).
select column_name, data_type, is_nullable
  from information_schema.columns
 where table_schema = 'public' and table_name = 'commands'
   and column_name in ('submitted_by', 'submitted_by_id')
 order by column_name;

-- 2. Totals. Before 004 the two submitted_* columns do not exist, so run this one only AFTER.
--    (If it errors with "column does not exist", 004 has not been applied yet.)
select count(*)                                  as commands_total,
       count(submitted_by)                       as with_submitted_by,
       count(submitted_by_id)                    as with_submitted_by_id,
       count(*) filter (where submitted_by is null and submitted_by_id is not null)
                                                 as id_without_name
  from public.commands;

-- 3. The 002 and 003 triggers must all still be listed (4 of them), plus
--    commands_submitter_immutable after 004.
select event_object_table as table_name, trigger_name, action_timing, event_manipulation
  from information_schema.triggers
 where trigger_schema = 'public'
   and event_object_table in ('commands', 'audit_events')
 order by event_object_table, trigger_name;

-- 4. AFTER 004: no rows expected (the CHECK enforces it, shown here for completeness).
select id, submitted_by
  from public.commands
 where submitted_by is not null and length(btrim(submitted_by)) not between 1 and 40;
