"""Reconciliation of 'executing' commands (reconcile_executing.py). Offline.

Every test uses the in-memory FakeClient from test_audit_store and a private
temporary lock file. Nothing here can reach a database, Cyberwave, a twin or a
robot. The network guard is active. There is no clock in the decision: whether a
worker runs is settled by the worker lock, which the tests hold or release.
"""

from __future__ import annotations

import ast
import copy
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import reconcile_executing as rc
import worker_lock
from audit_store import StateConflictError, WriteError
from reconcile_executing import (
    ACTION_CLAIMED_RECEIVED,
    ACTION_FINISH_STATUS,
    ACTION_NEEDS_PERSON,
    ACTION_NOTHING_TO_DO,
    ACTION_RECORD_FAILURE,
    RECONCILED_DETAIL,
    RECONCILED_KIND,
    RECONCILED_MESSAGE,
    ReconcileRefused,
    assess,
    list_commands,
    reconcile_command,
)
from simulation_payloads import (
    MANUAL_VERIFICATION_NOTICE,
    MAX_TEXT_LENGTH,
    build_execution_completed,
    build_execution_failed,
    validate_execution_failed,
)
from tests import net_guard
from tests.test_audit_store import FakeClient, cmd
from worker_lock import LockHeld, WorkerLock

WORKER_DIR = Path(__file__).resolve().parents[1]
COMMAND_ID = "6f9619ff-8b86-4d11-b42d-00c04fc964ff"
OTHER_ID = "1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b"
SIM_STARTED = {"dryRun": False, "mode": "simulation"}
DECISION = ["command_received", "plan_generated", "validator_result", "critic_result"]
CLAIMED_AT = "2026-11-14T10:00:00+00:00"
POSIX = worker_lock.fcntl is not None


def event(event_type: str, n: int, payload: Any = None, command_id: str = COMMAND_ID) -> dict[str, Any]:
    return {
        "id": f"event-{n}",
        "command_id": command_id,
        "event_type": event_type,
        "message": f"{event_type} message",
        "payload": {} if payload is None else payload,
        "created_at": f"2026-11-14T10:00:{n:02d}+00:00",
    }


def timeline(*extra: tuple[str, Any], started: Any = SIM_STARTED) -> list[dict[str, Any]]:
    """The decision events, execution_started (unless started is None), then ``extra``."""
    rows = [event(t, i) for i, t in enumerate(DECISION)]
    if started is not None:
        rows.append(event("execution_started", len(rows), started))
    for event_type, payload in extra:
        rows.append(event(event_type, len(rows), payload))
    return rows


def failed_payload(kind: str = "interrupted", stop: str = "confirmed") -> dict[str, Any]:
    return build_execution_failed(kind, "the run was interrupted (SystemExit)", stop)


def worker_lost_payload() -> dict[str, Any]:
    """Exactly what this script writes."""
    return build_execution_failed(RECONCILED_KIND, RECONCILED_DETAIL, "unconfirmed")


def completed_payload() -> dict[str, Any]:
    joints = {name: 0.0 for name in ("_1", "_2", "_3", "_4", "_5", "_6")}
    snap = {"readAt": "2026-11-14T10:00:30Z", "joints": joints}
    return build_execution_completed("rehearsal completed", snap, snap, snap)


def command(status: str = "executing", **row: Any) -> dict[str, Any]:
    result = cmd(COMMAND_ID, "2026-11-14T09:59:00+00:00", status=status,
                 claimed_by="rehearsal-worker", claimed_at=CLAIMED_AT)
    result.update(row)
    return result


def stranded_client(status: str = "executing", events: list[dict[str, Any]] | None = None,
                    **row: Any) -> FakeClient:
    return FakeClient(commands=[command(status, **row)],
                      audit_events=timeline() if events is None else events)


def same_tables(client: FakeClient) -> FakeClient:
    """A fresh client (no injected failures) over the same stored rows: a later run."""
    return FakeClient(commands=client.tables["commands"], audit_events=client.tables["audit_events"])


class ReconcileTestCase(unittest.TestCase):
    """Holds the worker lock in a private temporary file, as main() would."""

    def setUp(self) -> None:
        self.blocked_before = len(net_guard.BLOCKED_ATTEMPTS)
        self.addCleanup(self.assert_network_untouched)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock_path = Path(tmp.name) / "worker.lock"
        self.lock = WorkerLock(self.lock_path)
        if POSIX:
            self.lock.acquire()
        else:  # pragma: no cover - the lock tests are POSIX only
            self.skipTest("needs fcntl (macOS or Linux)")
        self.addCleanup(self.lock.unlock)

    def assert_network_untouched(self) -> None:
        self.assertEqual(len(net_guard.BLOCKED_ATTEMPTS), self.blocked_before)

    def dry(self, client: FakeClient) -> rc.ReconcileResult:
        return reconcile_command(COMMAND_ID, lock=self.lock, client=client)

    def apply(self, client: FakeClient) -> rc.ReconcileResult:
        return reconcile_command(
            COMMAND_ID, lock=self.lock, apply=True, worker_stopped_confirmed=True, client=client
        )

    def writes(self, client: FakeClient) -> list[str]:
        return [f"{c.table}:{c.op}" for c in client.calls if c.op in ("insert", "update")]

    def stored_events(self, client: FakeClient, event_type: str) -> list[dict[str, Any]]:
        return [e for e in client.tables["audit_events"] if e["event_type"] == event_type]

    def status(self, client: FakeClient) -> str:
        return client.tables["commands"][0]["status"]


# --------------------------------------------------------------------------
# The lock is the proof: no lock, no reads, no writes
# --------------------------------------------------------------------------


class LockRequiredTests(ReconcileTestCase):
    def test_a_lock_that_is_not_held_refuses_before_any_call(self) -> None:
        client = stranded_client()
        self.lock.unlock()
        for call in (
            lambda: reconcile_command(COMMAND_ID, lock=self.lock, client=client),
            lambda: reconcile_command(COMMAND_ID, lock=self.lock, apply=True,
                                      worker_stopped_confirmed=True, client=client),
            lambda: list_commands(lock=self.lock, client=client),
            lambda: list_commands(lock=None, client=client),  # type: ignore[arg-type]
            lambda: reconcile_command(COMMAND_ID, lock="held", client=client),  # type: ignore[arg-type]
        ):
            with self.assertRaises(ReconcileRefused):
                call()
        self.assertEqual(client.calls, [])

    def test_main_refuses_when_another_process_holds_the_lock(self) -> None:
        client = stranded_client()
        for argv in ([], ["--command-id", COMMAND_ID],
                     ["--command-id", COMMAND_ID, "--apply", "--confirm-worker-stopped"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with mock.patch.object(rc, "_get_client", return_value=client) as factory, \
                        redirect_stdout(out):
                    code = rc.main([*argv, "--lock-file", str(self.lock_path)])
                self.assertEqual(code, worker_lock.EXIT_LOCK_HELD)
                self.assertIn("holds the lock", out.getvalue())
                self.assertIn("Nothing was read or written", out.getvalue())
                factory.assert_not_called()  # not even a database client
                self.assertEqual(client.calls, [])

    def test_main_holds_the_lock_for_the_whole_run_and_releases_it_after(self) -> None:
        self.lock.unlock()
        client = stranded_client()
        seen: list[str] = []
        real = rc.list_commands

        def spy(**kwargs: Any) -> Any:
            try:
                WorkerLock(self.lock_path).acquire()
                seen.append("free")
            except LockHeld:
                seen.append("held")
            return real(**kwargs)

        with mock.patch.object(rc, "_get_client", return_value=client), \
                mock.patch.object(rc, "list_commands", side_effect=spy), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(rc.main(["--lock-file", str(self.lock_path)]), 0)
        self.assertEqual(seen, ["held"])
        with WorkerLock(self.lock_path):  # released afterwards
            pass

    def test_main_without_fcntl_refuses_and_touches_nothing(self) -> None:
        self.lock.unlock()
        client = stranded_client()
        out = io.StringIO()
        with mock.patch.object(worker_lock, "fcntl", None), \
                mock.patch.object(rc, "_get_client", return_value=client) as factory, \
                redirect_stdout(out):
            code = rc.main(["--lock-file", str(self.lock_path)])
        self.assertEqual(code, worker_lock.EXIT_LOCK_UNAVAILABLE)
        self.assertIn("no fcntl", out.getvalue())
        factory.assert_not_called()
        self.assertEqual(client.calls, [])


# --------------------------------------------------------------------------
# Candidate selection (pure, no clock)
# --------------------------------------------------------------------------


class CandidateSelectionTests(ReconcileTestCase):
    def action_of(self, status: str, events: list[dict[str, Any]], **row: Any) -> str:
        return assess(COMMAND_ID, command(status, **row), events).action

    def test_executing_without_a_terminal_event_is_a_candidate_however_young(self) -> None:
        # No age is involved: a command claimed a second ago is a candidate too,
        # because the lock (held by the caller) says no worker runs.
        found = assess(COMMAND_ID, command(claimed_at="2999-01-01T00:00:00+00:00"), timeline())
        self.assertEqual(found.action, ACTION_RECORD_FAILURE)
        self.assertEqual(found.target_status, "failed")

    def test_a_candidate_needs_no_timestamp_at_all(self) -> None:
        found = assess(COMMAND_ID, command(claimed_at=None), timeline())
        self.assertEqual(found.action, ACTION_RECORD_FAILURE)

    def test_a_terminal_event_makes_it_status_only_never_an_event_candidate(self) -> None:
        cases = {
            "execution_failed (worker_lost)": (("execution_failed", worker_lost_payload()), "failed"),
            "execution_failed (interrupted)": (("execution_failed", failed_payload()), "failed"),
            "execution_completed": (("execution_completed", completed_payload()), "completed"),
        }
        for name, (extra, target) in cases.items():
            with self.subTest(case=name):
                found = assess(COMMAND_ID, command(), timeline(extra))
                self.assertEqual(found.action, ACTION_FINISH_STATUS)
                self.assertEqual(found.target_status, target)

    def test_terminal_statuses_need_nothing_however_the_timeline_looks(self) -> None:
        for status in ("completed", "failed", "rejected"):
            for events in (timeline(), timeline(("execution_failed", failed_payload()))):
                with self.subTest(status=status):
                    self.assertEqual(self.action_of(status, events), ACTION_NOTHING_TO_DO)

    def test_anything_unusual_needs_a_person(self) -> None:
        cases = {
            "no execution_started": ("executing", timeline(started=None)),
            "two execution_started": ("executing", timeline(("execution_started", SIM_STARTED))),
            "a dry-run command": ("executing", timeline(started={"dryRun": True})),
            "an unknown started payload": ("executing", timeline(started={"dryRun": False, "mode": "live"})),
            "approved": ("approved", timeline()),
            "two terminal events": (
                "executing",
                timeline(("execution_failed", failed_payload()),
                         ("execution_completed", completed_payload())),
            ),
            "two execution_failed": (
                "executing",
                timeline(("execution_failed", failed_payload()), ("execution_failed", failed_payload())),
            ),
            "a malformed terminal payload": ("executing", timeline(("execution_failed", {"dryRun": False}))),
        }
        for name, (status, events) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.action_of(status, events), ACTION_NEEDS_PERSON)
        self.assertEqual(assess(COMMAND_ID, None, []).action, ACTION_NEEDS_PERSON)

    def test_a_received_command_claimed_by_a_worker_is_listed_and_never_a_candidate(self) -> None:
        found = assess(COMMAND_ID, command("received"), timeline(started=None)[:1])
        self.assertEqual(found.action, ACTION_CLAIMED_RECEIVED)
        self.assertIsNone(found.target_status)
        self.assertIn("never changes", found.reason)
        unclaimed = assess(COMMAND_ID, command("received", claimed_by=None, claimed_at=None), [])
        self.assertEqual(unclaimed.action, ACTION_NOTHING_TO_DO)

    def test_the_module_has_no_age_runtime_deadline_or_margin(self) -> None:
        for name in ("Phase", "LIFECYCLE_PHASES", "RuntimeBound", "runtime_bound", "Eligibility",
                     "evaluate_eligibility", "STALE_SAFETY_MARGIN_S", "SOURCE_CLAIMED_AT",
                     "SOURCE_EXECUTION_STARTED", "ACTION_NOT_ELIGIBLE", "_utc_now"):
            self.assertFalse(hasattr(rc, name), name)
        source = (WORKER_DIR / "reconcile_executing.py").read_text(encoding="utf-8")
        for word in ("datetime", "timedelta", "time.time", "monotonic"):
            self.assertNotIn(word, source)


class ListTests(ReconcileTestCase):
    def test_list_shows_executing_and_claimed_received_and_writes_nothing(self) -> None:
        client = stranded_client()
        client.tables["commands"] += [
            cmd(OTHER_ID, "2026-11-14T09:00:00+00:00", status="received",
                claimed_by="rehearsal-worker", claimed_at=CLAIMED_AT),
            cmd("3c9e1f00-0000-4000-8000-000000000001", "2026-11-14T09:01:00+00:00"),  # unclaimed
            cmd("3c9e1f00-0000-4000-8000-000000000002", "2026-11-14T09:02:00+00:00", status="completed"),
        ]
        before = copy.deepcopy(client.tables)
        found, truncated = list_commands(lock=self.lock, client=client)
        self.assertFalse(truncated)
        self.assertEqual({(a.command_id, a.action) for a in found},
                         {(COMMAND_ID, ACTION_RECORD_FAILURE), (OTHER_ID, ACTION_CLAIMED_RECEIVED)})
        self.assertEqual(self.writes(client), [])
        self.assertEqual(client.tables, before)

    def test_a_cut_list_says_so(self) -> None:
        client = FakeClient(commands=[
            cmd(f"3c9e1f00-0000-4000-8000-{i:012d}", f"2026-11-14T09:{i // 60:02d}:{i % 60:02d}+00:00",
                status="completed" if False else "executing")
            for i in range(rc.LIST_LIMIT + 1)
        ])
        found, truncated = list_commands(lock=self.lock, client=client)
        self.assertTrue(truncated)
        self.assertEqual(len(found), rc.LIST_LIMIT)

    def test_a_read_failure_writes_nothing(self) -> None:
        client = stranded_client()
        client.fail("commands", "select", RuntimeError("down"))
        with self.assertRaises(rc.ReconcileError):
            list_commands(lock=self.lock, client=client)
        self.assertEqual(self.writes(client), [])


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


class ApplyTests(ReconcileTestCase):
    def test_the_default_is_a_dry_run_that_writes_nothing(self) -> None:
        client = stranded_client()
        result = self.dry(client)
        self.assertEqual(result.assessment.action, ACTION_RECORD_FAILURE)
        self.assertFalse(result.applied)
        self.assertEqual(self.writes(client), [])

    def test_apply_without_the_attestation_is_refused_before_anything_is_read(self) -> None:
        client = stranded_client()
        with self.assertRaises(ReconcileRefused):
            reconcile_command(COMMAND_ID, lock=self.lock, apply=True, client=client)
        self.assertEqual(client.calls, [])

    def test_the_attestation_without_apply_writes_nothing(self) -> None:
        client = stranded_client()
        result = reconcile_command(
            COMMAND_ID, lock=self.lock, worker_stopped_confirmed=True, client=client
        )
        self.assertFalse(result.applied)
        self.assertEqual(self.writes(client), [])

    def test_apply_records_one_worker_lost_failure_with_an_unconfirmed_stop_then_the_status(self) -> None:
        client = stranded_client()
        result = self.apply(client)
        self.assertIsNone(result.error)
        self.assertEqual(result.events_written, ("execution_failed",))
        self.assertEqual(result.status_after, "failed")
        self.assertEqual(self.writes(client), ["audit_events:insert", "commands:update"])

        [written] = self.stored_events(client, "execution_failed")
        payload = written["payload"]
        validate_execution_failed(payload)
        self.assertEqual(payload["dryRun"], False)
        self.assertEqual(payload["mode"], "simulation")
        self.assertEqual(payload["stop"], "unconfirmed")
        self.assertEqual(payload["error"]["kind"], "worker_lost")
        self.assertTrue(payload["error"]["detail"].startswith(MANUAL_VERIFICATION_NOTICE))
        self.assertNotIn("lastTelemetry", payload)  # no reading exists, none is invented
        self.assertEqual(written["message"], RECONCILED_MESSAGE)
        for warning in ("possibly moved", "Manual verification required"):
            self.assertIn(warning, written["message"])
        self.assertEqual(set(payload), {"dryRun", "mode", "error", "stop"})
        self.assertEqual(set(payload["error"]), {"kind", "detail"})
        self.assertLessEqual(len(RECONCILED_DETAIL), MAX_TEXT_LENGTH)

        update = client.calls_of("update")[0]
        self.assertEqual(update.values, {"status": "failed"})
        self.assertIn(("eq", "status", "executing"), update.filters)  # conditional
        self.assertIn(("eq", "id", COMMAND_ID), update.filters)
        self.assertEqual(self.status(client), "failed")

    def test_only_the_named_command_is_touched(self) -> None:
        client = stranded_client()
        client.tables["commands"].append(
            cmd(OTHER_ID, "2026-11-14T09:00:00+00:00", status="executing", claimed_at=CLAIMED_AT)
        )
        client.tables["audit_events"].append(event("execution_started", 50, SIM_STARTED, command_id=OTHER_ID))
        self.apply(client)
        self.assertEqual(client.tables["commands"][1]["status"], "executing")
        [insert], [update] = client.calls_of("insert"), client.calls_of("update")
        self.assertEqual(insert.values["command_id"], COMMAND_ID)
        self.assertIn(("eq", "id", COMMAND_ID), update.filters)

    def test_an_invalid_command_id_is_refused_without_a_call(self) -> None:
        client = stranded_client()
        for bad in ("", "not-a-uuid", "1; drop table commands", None, 42):
            with self.subTest(bad=bad):
                with self.assertRaises(ReconcileRefused):
                    reconcile_command(bad, lock=self.lock, client=client)  # type: ignore[arg-type]
        self.assertEqual(client.calls, [])

    def test_a_received_command_is_never_changed_even_with_both_flags(self) -> None:
        client = stranded_client("received", events=timeline(started=None)[:2])
        before = copy.deepcopy(client.tables)
        result = self.apply(client)
        self.assertEqual(result.assessment.action, ACTION_CLAIMED_RECEIVED)
        self.assertFalse(result.applied)
        self.assertEqual(self.writes(client), [])
        self.assertEqual(client.tables, before)

    def test_other_states_need_a_person_and_nothing_is_written(self) -> None:
        cases = {
            "no execution_started": stranded_client(events=timeline(started=None)),
            "a dry-run command": stranded_client(events=timeline(started={"dryRun": True})),
            "approved": stranded_client(status="approved"),
            "two terminal events": stranded_client(events=timeline(
                ("execution_failed", failed_payload()), ("execution_completed", completed_payload()))),
            "no such command": FakeClient(),
        }
        for name, client in cases.items():
            with self.subTest(case=name):
                before = copy.deepcopy(client.tables)
                result = self.apply(client)
                self.assertEqual(result.assessment.action, ACTION_NEEDS_PERSON)
                self.assertFalse(result.applied)
                self.assertEqual(self.writes(client), [])
                self.assertEqual(client.tables, before)

    def test_terminal_statuses_are_left_alone(self) -> None:
        for status in ("completed", "failed", "rejected"):
            with self.subTest(status=status):
                client = stranded_client(status=status)
                result = self.apply(client)
                self.assertEqual(result.assessment.action, ACTION_NOTHING_TO_DO)
                self.assertEqual(self.writes(client), [])

    def test_a_read_failure_writes_nothing(self) -> None:
        for table in ("commands", "audit_events"):
            with self.subTest(table=table):
                client = stranded_client()
                client.fail(table, "select", RuntimeError("down"))
                with self.assertRaises(rc.ReconcileError):
                    self.apply(client)
                self.assertEqual(self.writes(client), [])

    def test_a_terminal_event_that_lands_between_the_two_reads_stops_the_write(self) -> None:
        client = stranded_client()

        def land(c: FakeClient) -> None:
            c.tables["audit_events"].append(event("execution_failed", 90, failed_payload()))

        # The hook runs just before the SECOND events read (the re-read before writing).
        selects = {"n": 0}
        real = client._execute

        def counting(call: Any) -> Any:
            if call.table == "audit_events" and call.op == "select":
                selects["n"] += 1
                if selects["n"] == 2:
                    land(client)
            return real(call)

        client._execute = counting  # type: ignore[method-assign]
        result = self.apply(client)
        self.assertFalse(result.applied)
        self.assertIn("changed between two reads", result.error or "")
        self.assertEqual(self.writes(client), [])
        self.assertEqual(len(self.stored_events(client, "execution_failed")), 1)  # only the one that landed


class StatusOnlyTests(ReconcileTestCase):
    def test_a_recorded_failure_with_a_stuck_status_makes_only_the_status_change(self) -> None:
        for payload in (worker_lost_payload(), failed_payload(), failed_payload("overall_timeout", "unconfirmed")):
            with self.subTest(kind=payload["error"]["kind"]):
                client = stranded_client(events=timeline(("execution_failed", payload)))
                result = self.apply(client)
                self.assertIsNone(result.error)
                self.assertEqual(result.events_written, ())
                self.assertEqual(result.status_after, "failed")
                self.assertEqual(self.writes(client), ["commands:update"])  # no insert
                update = client.calls_of("update")[0]
                self.assertEqual(update.values, {"status": "failed"})
                self.assertIn(("eq", "status", "executing"), update.filters)
                self.assertEqual(len(self.stored_events(client, "execution_failed")), 1)

    def test_a_recorded_success_with_a_stuck_status_completes_it_without_an_event(self) -> None:
        client = stranded_client(events=timeline(("execution_completed", completed_payload())))
        result = self.apply(client)
        self.assertIsNone(result.error)
        self.assertEqual(result.status_after, "completed")
        self.assertEqual(self.writes(client), ["commands:update"])
        self.assertEqual(client.calls_of("update")[0].values, {"status": "completed"})
        self.assertEqual(self.stored_events(client, "execution_failed"), [])

    def test_the_status_only_path_needs_both_flags(self) -> None:
        client = stranded_client(events=timeline(("execution_failed", worker_lost_payload())))
        self.assertEqual(self.dry(client).assessment.action, ACTION_FINISH_STATUS)
        self.assertEqual(self.writes(client), [])
        with self.assertRaises(ReconcileRefused):
            reconcile_command(COMMAND_ID, lock=self.lock, apply=True, client=client)
        self.assertEqual(self.writes(client), [])


class IdempotencyTests(ReconcileTestCase):
    def test_a_repeated_run_writes_nothing_more(self) -> None:
        client = stranded_client()
        self.apply(client)
        for _ in range(2):
            later = same_tables(client)
            result = self.apply(later)
            self.assertEqual(result.assessment.action, ACTION_NOTHING_TO_DO)
            self.assertEqual(self.writes(later), [])
            self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_event_written_but_status_update_failed_then_a_rerun_finishes_only_the_status(self) -> None:
        for failure in (WriteError("lost response"), RuntimeError("boom")):
            with self.subTest(failure=type(failure).__name__):
                client = stranded_client()
                client.fail("commands", "update", failure)
                first = self.apply(client)
                self.assertEqual(first.events_written, ("execution_failed",))
                self.assertIn("never write one again", first.error or "")
                self.assertIsNone(first.status_after)
                self.assertEqual(self.status(client), "executing")

                later = same_tables(client)
                self.assertEqual(self.dry(later).assessment.action, ACTION_FINISH_STATUS)
                second = self.apply(later)
                self.assertIsNone(second.error)
                self.assertEqual(second.events_written, ())
                self.assertEqual(self.writes(later), ["commands:update"])  # status only
                self.assertEqual(self.status(later), "failed")
                self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_a_status_only_run_that_fails_again_can_be_repeated_without_any_event(self) -> None:
        client = stranded_client(events=timeline(("execution_failed", worker_lost_payload())))
        client.fail("commands", "update", WriteError("lost again"))
        first = self.apply(client)
        self.assertIn("writes no event", first.error or "")
        later = same_tables(client)
        second = self.apply(later)
        self.assertEqual(second.status_after, "failed")
        self.assertEqual(self.writes(later), ["commands:update"])
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_a_failed_insert_changes_no_status_and_a_rerun_records_once(self) -> None:
        client = stranded_client()
        client.fail("audit_events", "insert", WriteError("timeout"))
        first = self.apply(client)
        self.assertIn("MAY have been written", first.error or "")
        self.assertEqual(self.writes(client), ["audit_events:insert"])  # no status update
        later = same_tables(client)  # the insert really did not land in this case
        second = self.apply(later)
        self.assertEqual(second.events_written, ("execution_failed",))
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_an_insert_that_landed_despite_the_error_is_not_written_twice(self) -> None:
        client = stranded_client()
        client.before_next("insert", lambda c: c.tables["audit_events"].append(
            event("execution_failed", 90, worker_lost_payload())))
        client.fail("audit_events", "insert", WriteError("response lost"))
        self.apply(client)
        later = same_tables(client)
        result = self.apply(later)
        self.assertEqual(result.assessment.action, ACTION_FINISH_STATUS)
        self.assertEqual(self.writes(later), ["commands:update"])  # status only, no insert
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_a_status_changed_after_the_read_is_not_overwritten(self) -> None:
        client = stranded_client()
        client.before_next("update", lambda c: c.tables["commands"][0].update(status="completed"))
        result = self.apply(client)
        self.assertIn("matched no row", result.error or "")
        self.assertEqual(self.status(client), "completed")  # untouched
        self.assertEqual(len(client.calls_of("update")), 1)

    def test_nothing_is_ever_retried_within_a_run(self) -> None:
        for table, op in (("audit_events", "insert"), ("commands", "update")):
            with self.subTest(op=op):
                client = stranded_client()
                client.fail(table, op, WriteError("x"))
                self.apply(client)
                self.assertEqual(len(client.calls_of(op)), 1)

    def test_a_state_conflict_from_the_store_is_reported_not_retried(self) -> None:
        client = stranded_client()
        with mock.patch.object(rc.audit_store, "set_command_status",
                               side_effect=StateConflictError("no row")) as update:
            result = self.apply(client)
        update.assert_called_once()
        self.assertIn("status changed", result.error or "")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


class CommandLineTests(ReconcileTestCase):
    def run_main(self, argv: list[str], client: Any) -> tuple[int, str]:
        self.lock.unlock()  # main takes the lock itself
        out = io.StringIO()
        with mock.patch.object(rc, "_get_client", return_value=client), redirect_stdout(out):
            code = rc.main([*argv, "--lock-file", str(self.lock_path)])
        return code, out.getvalue()

    def test_the_default_lists_and_writes_nothing(self) -> None:
        client = stranded_client()
        code, text = self.run_main([], client)
        self.assertEqual(code, 0)
        self.assertIn(COMMAND_ID, text)
        self.assertIn("Read only", text)
        self.assertEqual(self.writes(client), [])

    def test_the_dry_run_of_a_candidate_says_how_to_write(self) -> None:
        client = stranded_client()
        code, text = self.run_main(["--command-id", COMMAND_ID], client)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", text)
        self.assertIn("--apply --confirm-worker-stopped", text)
        self.assertIn("possibly moved", text)
        self.assertEqual(self.writes(client), [])

    def test_apply_without_the_attestation_reads_and_writes_nothing(self) -> None:
        client = stranded_client()
        code, text = self.run_main(["--command-id", COMMAND_ID, "--apply"], client)
        self.assertEqual(code, 2)
        self.assertIn("--confirm-worker-stopped", text)
        self.assertEqual(client.calls, [])

    def test_apply_needs_one_named_command(self) -> None:
        client = stranded_client()
        code, text = self.run_main(["--apply", "--confirm-worker-stopped"], client)
        self.assertEqual(code, 2)
        self.assertIn("--command-id", text)
        self.assertEqual(client.calls, [])

    def test_the_attestation_without_apply_is_a_dry_run(self) -> None:
        client = stranded_client()
        code, text = self.run_main(["--command-id", COMMAND_ID, "--confirm-worker-stopped"], client)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", text)
        self.assertEqual(self.writes(client), [])

    def test_apply_with_both_flags_writes_and_says_to_verify(self) -> None:
        client = stranded_client()
        code, text = self.run_main(
            ["--command-id", COMMAND_ID, "--apply", "--confirm-worker-stopped"], client)
        self.assertEqual(code, 0)
        self.assertIn("APPLIED", text)
        self.assertIn(MANUAL_VERIFICATION_NOTICE, text)
        self.assertEqual(self.status(client), "failed")

    def test_needs_person_and_claimed_received_exit_4(self) -> None:
        code, _ = self.run_main(["--command-id", COMMAND_ID],
                                stranded_client(events=timeline(started={"dryRun": True})))
        self.assertEqual(code, 4)
        code, _ = self.run_main(["--command-id", COMMAND_ID],
                                stranded_client("received", events=timeline(started=None)[:2]))
        self.assertEqual(code, 4)

    def test_a_failed_write_exits_nonzero(self) -> None:
        client = stranded_client()
        client.fail("audit_events", "insert", WriteError("x"))
        code, text = self.run_main(
            ["--command-id", COMMAND_ID, "--apply", "--confirm-worker-stopped"], client)
        self.assertEqual(code, 1)
        self.assertIn("WRITE FAILED", text)

    def test_the_options_are_only_the_agreed_ones(self) -> None:
        tree = ast.parse((WORKER_DIR / "reconcile_executing.py").read_text(encoding="utf-8"))
        options = {
            arg.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant)
        }
        # --lock-file comes from worker_lock.add_lock_argument.
        self.assertEqual(options, {"--command-id", "--apply", "--confirm-worker-stopped"})


# --------------------------------------------------------------------------
# Isolation: no twin, no movement, no critic
# --------------------------------------------------------------------------


class IsolationTests(unittest.TestCase):
    def test_no_movement_or_twin_call_exists_in_the_module(self) -> None:
        tree = ast.parse((WORKER_DIR / "reconcile_executing.py").read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        forbidden = {
            "move_joint", "dispatch_move", "stop", "standby_stop", "connect", "start",
            "run_rehearsal", "affect", "set_joints", "review_plan", "claim_next_command",
        }
        self.assertEqual(called & forbidden, set())

    def test_importing_it_loads_no_twin_executor_critic_or_cyberwave_module(self) -> None:
        code = (
            "import sys, reconcile_executing; "
            "print(sorted(m for m in sys.modules if 'cyberwave' in m or m in ("
            "'simulation_executor', 'simulation_supervisor', 'simulation_child', "
            "'simulation_processor', 'critic_client', 'command_processor', 'httpx')))"
        )
        out = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=WORKER_DIR, capture_output=True,
            text=True, timeout=60, check=True,
        )
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
