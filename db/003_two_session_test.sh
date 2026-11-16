#!/usr/bin/env bash
# 003_two_session_test.sh
# LOCAL ONLY. Never run this against Supabase: it needs a Postgres it can create
# and destroy. It starts a THROWAWAY cluster on a unix socket (no TCP port is
# opened), applies db/001, db/002 and db/003, runs the two-session scenarios below
# under READ COMMITTED, and deletes the cluster. 003 was applied to Supabase on 2026-11-15 (test script 55/55,
# row counts unchanged); this script itself is only for a local Postgres.
#
# What it proves (two psql sessions, A and B, coordinated through named pipes, with
# pg_stat_activity used to see that a statement is really waiting on a lock):
#   X1  A status update and a terminal event insert for the same command cannot
#       leave a contradiction. B holds an uncommitted executing -> failed; A inserts
#       execution_completed. A must WAIT for B's row lock, then be refused because the
#       command is now failed. Run with 002's trigger disabled (to show the row lock
#       of 003 on its own) and enabled (002 refuses at once, without waiting).
#   X2  The other direction: A holds an uncommitted execution_completed insert; B's
#       executing -> completed waits and then SUCCEEDS (it sees the event), and B's
#       executing -> failed waits and is REFUSED (no execution_failed event).
#   X3  Two execution_failed inserts for the same command: the second waits, then is
#       refused (002's advisory lock and 003 together); exactly one event remains.
#   X4  A claim-style update (claimed_by) waits for an uncommitted terminal insert and
#       then goes through; no deadlock, a lock_timeout guards the wait.
#   X6  The deadlock FOR UPDATE would cause: two transactions each insert a
#       non-terminal event (plan_generated) and then a terminal one for the same
#       command, concurrently. With 003's FOR NO KEY UPDATE neither deadlocks: the first
#       commits, the second is refused by 002 ("already has a terminal execution event").
#   X5  Mutation of this test: 003's event trigger WITHOUT its row lock. X1 must then
#       produce the contradiction (status failed AND an execution_completed event):
#       the stale status read lets the insert through. If it does not, this test cannot
#       detect the race and the script fails.
#   X7  Control for X6: the event trigger with FOR UPDATE (the earlier version). The
#       same steps as X6 must now end in "deadlock detected". If they do not, X6 proves
#       nothing and the script fails.
#
# Usage: db/003_two_session_test.sh      (PGBIN=/path/to/postgres/bin to override)
# Exit code 0 = every check passed.

set -u
PGBIN="${PGBIN:-/Library/PostgreSQL/18/bin}"
PATH="$PGBIN:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
# A short directory: a unix socket path is limited to about 100 characters.
W="$(mktemp -d "${TMPDIR:-/tmp}/pg003s.XXXXXX")" || exit 2

FAILS=0
cleanup() {
  exec 3>&- 4>&- 2>/dev/null
  pg_ctl -D "$W/data" -m immediate stop >/dev/null 2>&1
  rm -rf "$W"
}
trap cleanup EXIT

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
check() { # check "name" command...
  local name="$1"; shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}

initdb -D "$W/data" -A trust -U postgres >"$W/initdb.log" 2>&1 || { echo "initdb failed"; exit 2; }
pg_ctl -D "$W/data" -o "-c listen_addresses='' -c unix_socket_directories=$W -c fsync=off" \
  -l "$W/pg.log" -w start >/dev/null 2>&1 || { echo "cluster did not start"; exit 2; }

adm() { psql -h "$W" -U postgres -d t -X -q -At -v ON_ERROR_STOP=1 "$@"; }
psql -h "$W" -U postgres -X -q -c "create database t" || exit 2
adm -c "create role anon nologin; create role authenticated nologin; create publication supabase_realtime;" 2>/dev/null
for f in 001_init.sql 002_terminal_guards.sql 003_status_event_consistency.sql; do
  adm -f "$HERE/$f" 2>&1 | grep -v -E "WARNING|HINT" ; done
[ "$(adm -c "show default_transaction_isolation")" = "read committed" ] || { echo "not READ COMMITTED"; exit 2; }
[ "$(adm -c "select count(*) from pg_trigger where not tgisinternal and tgname in ('audit_events_one_terminal_execution','audit_events_terminal_requires_state','commands_terminal_status_needs_event')")" = "3" ] \
  || { echo "migrations 002/003 are not applied"; exit 2; }

# ---- two sessions A (fd 3) and B (fd 4), each a psql reading from a named pipe ------
open_session() { # name fd
  mkfifo "$W/$1.in"; : >"$W/$1.out"
  psql -h "$W" -U postgres -d t -X -q -At -v ON_ERROR_STOP=0 <"$W/$1.in" >>"$W/$1.out" 2>&1 &
  eval "exec $2>\"$W/$1.in\""
}
open_session A 3
open_session B 4
fdof() { case "$1" in A) echo 3;; B) echo 4;; esac; }
send() { local fd; fd=$(fdof "$1"); printf '%s\n' "$2" >&"$fd"; }  # send A "sql;"
reset_out() { : >"$W/A.out"; : >"$W/B.out"; }
await() { # await A text : wait (up to 15 s) until text appears in the session output
  local i
  for i in $(seq 1 150); do grep -q -- "$2" "$W/$1.out" && return 0; sleep 0.1; done
  return 1
}
mark() { send "$1" "select 'MARK_$2';"; await "$1" "MARK_$2"; }   # returns once the session is idle again
blocked_on() { # blocked_on pattern : a backend is waiting on a lock in a statement containing pattern
  local i n
  for i in $(seq 1 100); do
    n=$(adm -c "select count(*) from pg_stat_activity where datname='t' and wait_event_type='Lock' and pid <> pg_backend_pid() and query ilike '%$1%'")
    [ "${n:-0}" -ge 1 ] && return 0
    sleep 0.1
  done
  return 1
}
not_blocked_for_a_moment() { sleep 1; [ "$(adm -c "select count(*) from pg_stat_activity where datname='t' and wait_event_type='Lock' and pid <> pg_backend_pid()")" = "0" ]; }
out_has() { grep -q -- "$2" "$W/$1.out"; }
out_lacks() { ! grep -q -- "$2" "$W/$1.out"; }

newcmd() { adm -c "insert into public.commands (text, status) values ('two-session test', '$1') returning id"; }
event() { adm -c "insert into public.audit_events (command_id, event_type, message) values ('$1', '$2', 'setup')"; }
status_of() { adm -c "select status from public.commands where id = '$1'"; }
count_events() { adm -c "select count(*) from public.audit_events where command_id = '$1' and event_type = '$2'"; }

# ---- X1 with 002's trigger DISABLED: 003's row lock on its own ------------------------
echo "== X1 (002 trigger disabled): event insert vs executing -> failed"
adm -c "alter table public.audit_events disable trigger audit_events_one_terminal_execution"
reset_out
C=$(newcmd executing); event "$C" execution_failed
send B "begin; update public.commands set status = 'failed' where id = '$C';"; mark B x1b1
send A "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_completed', 'A');"
check "X1 the execution_completed insert WAITS for the status update's row lock" blocked_on "execution_completed"
send B "commit;"; mark B x1b2
mark A x1a
check "X1 it is then refused: the command is failed, not executing" out_has A "not executing; execution_completed is not allowed"
check "X1 no execution_completed event exists" test "$(count_events "$C" execution_completed)" = "0"
check "X1 the status is failed" test "$(status_of "$C")" = "failed"
adm -c "alter table public.audit_events enable trigger audit_events_one_terminal_execution"

# ---- X1 with 002 enabled: 002 refuses at once -----------------------------------------
echo "== X1 (002 enabled): the same case"
reset_out
C=$(newcmd executing); event "$C" execution_failed
send B "begin; update public.commands set status = 'failed' where id = '$C';"; mark B x1c1
send A "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_completed', 'A');"
mark A x1c2
check "X1/002 refused immediately by 002, without waiting" out_has A "already has a terminal execution event"
send B "commit;"; mark B x1c3
check "X1/002 no contradiction" test "$(count_events "$C" execution_completed)" = "0"

# ---- X2: the other direction ----------------------------------------------------------
echo "== X2: uncommitted execution_completed vs status updates"
reset_out
C=$(newcmd executing)
send A "begin; insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_completed', 'A');"; mark A x2a1
send B "update public.commands set status = 'completed' where id = '$C';"
check "X2 executing -> completed WAITS for the uncommitted event insert" blocked_on "update public.commands set status"
send A "commit;"; mark A x2a2
mark B x2b
check "X2 and then succeeds (it sees the committed event)" test "$(status_of "$C")" = "completed"
check "X2 without an error" out_lacks B "ERROR"

reset_out
C=$(newcmd executing)
send A "begin; insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_completed', 'A');"; mark A x2c1
send B "update public.commands set status = 'failed' where id = '$C';"
check "X2 executing -> failed WAITS as well" blocked_on "update public.commands set status"
send A "commit;"; mark A x2c2
mark B x2c3
check "X2 and is then refused: there is no execution_failed event" out_has B "has an execution_completed event; it cannot become failed"
check "X2 the status is still executing" test "$(status_of "$C")" = "executing"

# ---- X3: two terminal inserts ---------------------------------------------------------
echo "== X3: two execution_failed inserts for one command"
reset_out
C=$(newcmd executing)
send A "begin; insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'A');"; mark A x3a1
send B "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'second-writer');"
check "X3 the second insert WAITS" blocked_on "second-writer"
send A "commit;"; mark A x3a2
mark B x3b
check "X3 and is refused" out_has B "already has a terminal execution event"
check "X3 exactly one execution_failed event" test "$(count_events "$C" execution_failed)" = "1"

# ---- X4: a claim-style update just waits ----------------------------------------------
echo "== X4: a claim update while a terminal insert is uncommitted"
reset_out
C=$(newcmd executing)
send A "begin; insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'A');"; mark A x4a1
send B "set lock_timeout = '10s'; update public.commands set claimed_by = 'worker-x' where id = '$C';"
check "X4 the claim update waits for A, nothing else" blocked_on "claimed_by"
send A "commit;"; mark A x4a2
mark B x4b
check "X4 it then goes through, no timeout and no deadlock" out_lacks B "ERROR"
check "X4 claimed_by was set" test "$(adm -c "select claimed_by from public.commands where id = '$C'")" = "worker-x"

# ---- X6: non-terminal then terminal insert in one transaction, run twice -------------
two_transactions() { # two_transactions C : both sessions do plan_generated, then execution_failed
  send A "begin; insert into public.audit_events (command_id, event_type, message) values ('$1', 'plan_generated', 'A');"; mark A "$2a1"
  send B "begin; insert into public.audit_events (command_id, event_type, message) values ('$1', 'plan_generated', 'B');"; mark B "$2b1"
}
echo "== X6: a non-terminal then a terminal insert in one transaction, twice concurrently"
reset_out
C=$(newcmd executing)
two_transactions "$C" x6
send A "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'first-writer');"
mark A x6a2
check "X6 the first terminal insert is not blocked by the other transaction's key share" out_lacks A "ERROR"
send B "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'second-writer');"
check "X6 the second waits for the first (002's advisory lock), as it should" blocked_on "second-writer"
send A "commit;"; mark A x6a3
mark B x6b2
check "X6 the second is then refused by 002, not by a deadlock" out_has B "already has a terminal execution event"
check "X6 no deadlock was reported" out_lacks A "deadlock detected"
check "X6 no deadlock was reported (second session)" out_lacks B "deadlock detected"
send B "rollback;"; mark B x6b3
check "X6 exactly one execution_failed event" test "$(count_events "$C" execution_failed)" = "1"
check "X6 and the plan_generated of the transaction that committed is there" test "$(count_events "$C" plan_generated)" = "1"

# ---- X5: mutation of this test --------------------------------------------------------
echo "== X5: 003's event trigger WITHOUT its row lock (the test must detect the race)"
adm -c "alter table public.audit_events disable trigger audit_events_one_terminal_execution"
adm <<'SQL'
create or replace function public.audit_events_terminal_requires_state() returns trigger
language plpgsql set search_path = '' as $$
declare v_status text;
begin
  select c.status into v_status from public.commands c where c.id = new.command_id;  -- MUTATION: no row lock
  if new.event_type = 'execution_completed' and v_status <> 'executing' then
    raise exception 'command % is %, not executing', new.command_id, v_status;
  end if;
  return new;
end $$;
SQL
reset_out
C=$(newcmd executing); event "$C" execution_failed
send B "begin; update public.commands set status = 'failed' where id = '$C';"; mark B x5b1
send A "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_completed', 'A');"
mark A x5a   # the mutated trigger read the status BEFORE B committed ('executing') and let it pass
send B "commit;"; mark B x5b2
check "X5 mutated: the insert is not held back" out_lacks A "ERROR"
check "X5 mutated: the contradiction appears (failed + execution_completed), so X1 can detect it" \
  test "$(status_of "$C")|$(count_events "$C" execution_completed)" = "failed|1"

# ---- X7: control for X6 - the same steps with FOR UPDATE must deadlock ---------------
echo "== X7: control: the event trigger with FOR UPDATE (the earlier version) must deadlock in X6's steps"
adm -c "alter table public.audit_events enable trigger audit_events_one_terminal_execution"
adm <<'SQL'
create or replace function public.audit_events_terminal_requires_state() returns trigger
language plpgsql set search_path = '' as $$
declare v_status text;
begin
  select c.status into v_status from public.commands c where c.id = new.command_id for update;
  if new.event_type = 'execution_completed' and v_status <> 'executing' then
    raise exception 'command % is %, not executing', new.command_id, v_status;
  end if;
  return new;
end $$;
SQL
reset_out
C=$(newcmd executing)
two_transactions "$C" x7
send A "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'first-writer');"
check "X7 the first terminal insert WAITS behind the other transaction's key share" blocked_on "first-writer"
send B "insert into public.audit_events (command_id, event_type, message) values ('$C', 'execution_failed', 'second-writer');"
dead() { await A "deadlock detected" || await B "deadlock detected"; }
check "X7 the database reports a deadlock (so X6 can detect one)" dead
send A "rollback;"; send B "rollback;"; mark A x7a9; mark B x7b9

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; fi
exit "$FAILS"
