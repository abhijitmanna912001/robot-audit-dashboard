"""Manual reconciliation of 'executing' commands (reconcile_executing.py). Offline.

Every test uses the in-memory FakeClient from test_audit_store. Nothing here can
reach a database, Cyberwave, a twin or a robot. The network guard is active.

The production lifecycle table has phases without a finite upper bound, so no
command can qualify with it (ProductionBoundTests). Tests of the write paths
replace the table with a finite TEST-ONLY one and fix the clock.
"""

from __future__ import annotations

import ast
import copy
import io
import math
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import reconcile_executing as rc
from audit_store import StateConflictError, WriteError
from reconcile_executing import (
    ACTION_FINISH_STATUS,
    ACTION_NEEDS_PERSON,
    ACTION_NOT_ELIGIBLE,
    ACTION_NOTHING_TO_DO,
    ACTION_RECORD_FAILURE,
    RECONCILED_DETAIL,
    RECONCILED_KIND,
    RECONCILED_MESSAGE,
    SOURCE_CLAIMED_AT,
    SOURCE_EXECUTION_STARTED,
    Phase,
    ReconcileError,
    ReconcileRefused,
    assess,
    evaluate_eligibility,
    reconcile_command,
    runtime_bound,
)
from simulation_payloads import (
    MANUAL_VERIFICATION_NOTICE,
    MAX_TEXT_LENGTH,
    build_execution_failed,
    validate_execution_failed,
)
from tests import net_guard
from tests.test_audit_store import FakeClient, cmd

WORKER_DIR = Path(__file__).resolve().parents[1]
_PRODUCTION_PHASES = rc.LIFECYCLE_PHASES  # captured before any test patches it
COMMAND_ID = "6f9619ff-8b86-4d11-b42d-00c04fc964ff"
OTHER_ID = "1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b"
SIM_STARTED = {"dryRun": False, "mode": "simulation"}
DECISION = ["command_received", "plan_generated", "validator_result", "critic_result"]

CLAIMED_AT = "2026-11-14T10:00:00+00:00"
STARTED_INDEX = len(DECISION)  # execution_started is event number 4
STARTED_AT = f"2026-11-14T10:00:{STARTED_INDEX:02d}+00:00"
UTC = timezone.utc

# TEST ONLY: a finite lifecycle. From execution_started: 60 s; from the claim: 90 s.
FINITE_PHASES = (
    Phase("test decision phase", 30.0, "test", False),
    Phase("test execution phase", 60.0, "test", True),
)
MARGIN = rc.STALE_SAFETY_MARGIN_S
STARTED_THRESHOLD = 60.0 + MARGIN
CLAIMED_THRESHOLD = 90.0 + MARGIN
LONG_AFTER = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)  # two hours later


def at(iso: str, seconds: float) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC) + timedelta(seconds=seconds)


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


def _snapshot() -> dict[str, Any]:
    joints = {name: 0.0 for name in ("_1", "_2", "_3", "_4", "_5", "_6")}
    return {"readAt": "2026-11-14T10:00:30Z", "joints": joints}


def command(status: str = "executing", **row: Any) -> dict[str, Any]:
    result = cmd(COMMAND_ID, "2026-11-14T09:59:00+00:00", status=status,
                 claimed_by="rehearsal-worker", claimed_at=CLAIMED_AT)
    result.update(row)
    return result


def stale_client(status: str = "executing", events: list[dict[str, Any]] | None = None,
                 **row: Any) -> FakeClient:
    return FakeClient(commands=[command(status, **row)],
                      audit_events=timeline() if events is None else events)


def same_tables(client: FakeClient) -> FakeClient:
    """A fresh client (no injected failures) over the same stored rows: a later run."""
    return FakeClient(commands=client.tables["commands"], audit_events=client.tables["audit_events"])


class ReconcileTestCase(unittest.TestCase):
    """Finite TEST-ONLY lifecycle and a fixed clock, two hours after the run."""

    finite = True

    def setUp(self) -> None:
        self.blocked_before = len(net_guard.BLOCKED_ATTEMPTS)
        self.addCleanup(self.assert_network_untouched)
        if self.finite:
            self.use_phases(FINITE_PHASES)
        self.set_now(LONG_AFTER)

    def use_phases(self, phases: Any) -> None:
        patcher = mock.patch.object(rc, "LIFECYCLE_PHASES", phases)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_now(self, moment: datetime) -> None:
        patcher = mock.patch.object(rc, "_utc_now", return_value=moment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_network_untouched(self) -> None:
        self.assertEqual(len(net_guard.BLOCKED_ATTEMPTS), self.blocked_before)

    def apply(self, client: FakeClient) -> rc.ReconcileResult:
        return reconcile_command(COMMAND_ID, apply=True, worker_stopped_confirmed=True, client=client)

    def writes(self, client: FakeClient) -> list[str]:
        return [f"{c.table}:{c.op}" for c in client.calls if c.op in ("insert", "update")]

    def stored_events(self, client: FakeClient, event_type: str) -> list[dict[str, Any]]:
        return [e for e in client.tables["audit_events"] if e["event_type"] == event_type]

    def status(self, client: FakeClient) -> str:
        return client.tables["commands"][0]["status"]


# --------------------------------------------------------------------------
# The production bound: no finite maximum today, so nothing qualifies
# --------------------------------------------------------------------------


class ProductionBoundTests(ReconcileTestCase):
    finite = False

    def test_there_is_no_finite_maximum_from_either_timestamp(self) -> None:
        for source in (SOURCE_EXECUTION_STARTED, SOURCE_CLAIMED_AT):
            with self.subTest(source=source):
                bound = runtime_bound(source)
                self.assertIsNone(bound.maximum_s)
                self.assertTrue(bound.unbounded)
                self.assertIn("terminal event insert", bound.unbounded)
        self.assertEqual(runtime_bound(SOURCE_EXECUTION_STARTED).bounded_s, 98.0)
        self.assertEqual(runtime_bound(SOURCE_CLAIMED_AT).bounded_s, 108.0)
        self.assertIn("planner", runtime_bound(SOURCE_CLAIMED_AT).unbounded)
        self.assertNotIn("planner", runtime_bound(SOURCE_EXECUTION_STARTED).unbounded)

    def test_even_a_very_old_command_is_not_eligible_and_nothing_is_written(self) -> None:
        self.set_now(datetime(2030, 1, 1, tzinfo=UTC))
        client = stale_client()
        result = self.apply(client)
        self.assertEqual(result.assessment.action, ACTION_NOT_ELIGIBLE)
        self.assertFalse(result.applied)
        self.assertIn("no finite maximum", result.assessment.reason)
        self.assertEqual(self.writes(client), [])
        self.assertEqual(self.status(client), "executing")
        eligibility = result.assessment.eligibility
        assert eligibility is not None
        self.assertIsNone(eligibility.threshold_s)
        self.assertEqual(eligibility.source, SOURCE_EXECUTION_STARTED)

    def test_the_bounded_phases_match_their_constants(self) -> None:
        import critic_client
        import postgrest.constants
        import simulation_executor
        import simulation_supervisor as sup

        bounds = {p.name: p.bound_s for p in rc.LIFECYCLE_PHASES}
        self.assertEqual(bounds["critic request"], critic_client.TOTAL_DEADLINE_SECONDS)
        self.assertEqual(
            bounds["twin session start"],
            sup.CHILD_START_LIMIT_S + sup.TERMINATE_LIMIT_S + sup.CLEANUP_LIMIT_S,
        )
        self.assertEqual(
            bounds["rehearsal, stop and child terminations"],
            simulation_executor.DEFAULT_TIMEOUTS.total_s + 3 * sup.TERMINATE_LIMIT_S,
        )
        self.assertEqual(bounds["twin session close"], sup.CLEANUP_LIMIT_S)
        # The Supabase steps rely on the PostgREST per-operation default only.
        self.assertEqual(postgrest.constants.DEFAULT_POSTGREST_CLIENT_TIMEOUT, 120)
        supabase_steps = [p for p in rc.LIFECYCLE_PHASES if "PostgREST" in p.basis]
        self.assertTrue(all(p.bound_s is None for p in supabase_steps))
        self.assertGreaterEqual(len(supabase_steps), 10)

    def test_the_margin_is_an_explicit_named_value(self) -> None:
        self.assertEqual(rc.STALE_SAFETY_MARGIN_S, 300.0)
        source = (WORKER_DIR / "reconcile_executing.py").read_text(encoding="utf-8")
        self.assertIn("STALE_SAFETY_MARGIN_S = 300.0", source)
        self.assertIn("PROPOSED", source)

    def test_the_dry_run_prints_every_number_of_the_rule(self) -> None:
        client = stale_client()
        out = io.StringIO()
        with mock.patch.object(rc, "_get_client", return_value=client), redirect_stdout(out):
            code = rc.main(["--command-id", COMMAND_ID])
        text = out.getvalue()
        self.assertEqual(code, 5)
        for fragment in (
            f"timestamp source {SOURCE_EXECUTION_STARTED}", f"timestamp {STARTED_AT}", "age ",
            "Maximum worker runtime from that timestamp: NOT FINITE", "bounded phases 98.0 s",
            "PostgREST", "STALE_SAFETY_MARGIN_S: 300.0 s", "Effective threshold: none",
            "Qualifies: NO", "Age is never proof", "possibly moved",
        ):
            self.assertIn(fragment, text)
        self.assertNotIn("To write it", text)
        self.assertEqual(self.writes(client), [])


# --------------------------------------------------------------------------
# Rule 3: threshold boundaries and timestamps (finite TEST-ONLY table)
# --------------------------------------------------------------------------


class ThresholdTests(ReconcileTestCase):
    def eligibility(self, now: datetime, **row: Any) -> rc.Eligibility:
        started = timeline()[STARTED_INDEX]
        overrides = row.pop("started", None)
        if overrides is not None:
            started = {**started, **overrides}
        return evaluate_eligibility(command(**row), started, now)

    def test_execution_started_is_preferred_and_the_threshold_is_strict(self) -> None:
        cases = {
            -0.001: False,
            0.0: False,  # exactly at the threshold: not OLDER than it
            0.001: True,
        }
        for offset, qualifies in cases.items():
            with self.subTest(offset=offset):
                e = self.eligibility(at(STARTED_AT, STARTED_THRESHOLD + offset))
                self.assertEqual(e.source, SOURCE_EXECUTION_STARTED)
                self.assertEqual(e.maximum_s, 60.0)
                self.assertEqual(e.margin_s, MARGIN)
                self.assertEqual(e.threshold_s, STARTED_THRESHOLD)
                self.assertEqual(e.qualifies, qualifies)

    def test_claimed_at_is_used_with_the_whole_lifecycle_when_started_has_no_valid_time(self) -> None:
        for bad in (None, "", "garbage", "2026-11-14T10:00:04", 1731578404, "2026-13-40T99:00:00+00:00"):
            with self.subTest(started_created_at=bad):
                below = self.eligibility(at(CLAIMED_AT, CLAIMED_THRESHOLD), started={"created_at": bad})
                above = self.eligibility(at(CLAIMED_AT, CLAIMED_THRESHOLD + 0.001),
                                         started={"created_at": bad})
                self.assertEqual((below.source, above.source), (SOURCE_CLAIMED_AT, SOURCE_CLAIMED_AT))
                self.assertEqual(above.maximum_s, 90.0)
                self.assertEqual(above.threshold_s, CLAIMED_THRESHOLD)
                self.assertFalse(below.qualifies)
                self.assertTrue(above.qualifies)
                self.assertIn("whole lifecycle from the claim", above.reason)

    def test_missing_or_invalid_timestamps_never_qualify(self) -> None:
        for bad in (None, "", "  ", "garbage", "2026-11-14T10:00:00", 0, [], "2026-11-14"):
            with self.subTest(claimed_at=bad):
                e = self.eligibility(datetime(2030, 1, 1, tzinfo=UTC), claimed_at=bad,
                                     started={"created_at": None})
                self.assertFalse(e.qualifies)
                self.assertIsNone(e.source)
                self.assertIsNone(e.threshold_s)
                self.assertIn("valid timestamp", e.reason)

    def test_a_timestamp_in_the_future_never_qualifies(self) -> None:
        e = self.eligibility(at(STARTED_AT, -1.0))
        self.assertFalse(e.qualifies)
        self.assertLess(e.age_s or 0.0, 0)
        self.assertIn("later than the current time", e.reason)

    def test_a_timestamp_with_another_offset_is_compared_in_utc(self) -> None:
        local = "2026-11-14T15:30:04+05:30"  # the same instant as STARTED_AT
        e = self.eligibility(at(STARTED_AT, STARTED_THRESHOLD + 1), started={"created_at": local})
        self.assertTrue(e.qualifies)
        self.assertAlmostEqual(e.age_s or 0.0, STARTED_THRESHOLD + 1)

    def test_any_unbounded_phase_means_nothing_qualifies(self) -> None:
        self.use_phases(FINITE_PHASES + (Phase("test unbounded step", None, "test", True),))
        e = self.eligibility(datetime(2030, 1, 1, tzinfo=UTC))
        self.assertFalse(e.qualifies)
        self.assertIsNone(e.maximum_s)
        self.assertEqual(e.unbounded, ("test unbounded step",))
        # An unbounded DECISION-phase step matters only for the claimed_at fallback.
        self.use_phases(FINITE_PHASES + (Phase("test unbounded claim step", None, "test", False),))
        self.assertTrue(self.eligibility(datetime(2030, 1, 1, tzinfo=UTC)).qualifies)
        self.assertFalse(
            self.eligibility(datetime(2030, 1, 1, tzinfo=UTC), started={"created_at": None}).qualifies
        )

    def test_an_invalid_margin_never_qualifies(self) -> None:
        for margin in (-1.0, math.nan, math.inf, 300):
            with self.subTest(margin=margin), mock.patch.object(rc, "STALE_SAFETY_MARGIN_S", margin):
                self.assertFalse(self.eligibility(datetime(2030, 1, 1, tzinfo=UTC)).qualifies)

    def test_a_phase_bound_must_be_positive_and_finite(self) -> None:
        for bad in (0.0, -1.0, math.nan, math.inf, 5):
            with self.subTest(bound=bad), self.assertRaises(ValueError):
                Phase("x", bad, "test", True)  # type: ignore[arg-type]

    def test_the_clock_must_be_timezone_aware(self) -> None:
        with self.assertRaises(ValueError):
            self.eligibility(datetime(2030, 1, 1))

    def test_a_young_command_is_not_eligible_and_nothing_is_written(self) -> None:
        self.set_now(at(STARTED_AT, STARTED_THRESHOLD))
        client = stale_client()
        result = self.apply(client)
        self.assertEqual(result.assessment.action, ACTION_NOT_ELIGIBLE)
        self.assertEqual(self.writes(client), [])

    def test_a_command_without_any_valid_timestamp_is_not_eligible_and_nothing_is_written(self) -> None:
        events = timeline()
        events[STARTED_INDEX]["created_at"] = "not a time"
        client = stale_client(events=events, claimed_at=None)
        result = self.apply(client)
        self.assertEqual(result.assessment.action, ACTION_NOT_ELIGIBLE)
        self.assertEqual(self.writes(client), [])


# --------------------------------------------------------------------------
# A candidate: dry run by default, --apply with the attestation
# --------------------------------------------------------------------------


class CandidateTests(ReconcileTestCase):
    def test_the_default_is_a_dry_run_that_writes_nothing(self) -> None:
        client = stale_client()
        result = reconcile_command(COMMAND_ID, client=client)
        self.assertEqual(result.assessment.action, ACTION_RECORD_FAILURE)
        self.assertTrue(result.assessment.eligibility and result.assessment.eligibility.qualifies)
        self.assertFalse(result.applied)
        self.assertEqual(self.writes(client), [])
        self.assertEqual({c.op for c in client.calls}, {"select"})
        self.assertEqual(self.status(client), "executing")

    def test_apply_without_the_attestation_is_refused_before_anything_is_read(self) -> None:
        client = stale_client()
        for confirmed in (False, None, "yes", 1):
            with self.subTest(confirmed=confirmed):
                with self.assertRaises(ReconcileRefused):
                    reconcile_command(
                        COMMAND_ID, apply=True, worker_stopped_confirmed=confirmed,  # type: ignore[arg-type]
                        client=client,
                    )
        self.assertEqual(client.calls, [])

    def test_the_attestation_without_apply_writes_nothing(self) -> None:
        client = stale_client()
        result = reconcile_command(COMMAND_ID, worker_stopped_confirmed=True, client=client)
        self.assertFalse(result.applied)
        self.assertEqual(self.writes(client), [])

    def test_apply_records_one_failure_with_an_unconfirmed_stop_then_the_status(self) -> None:
        client = stale_client()
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
        self.assertEqual(payload["error"]["kind"], RECONCILED_KIND)
        self.assertTrue(payload["error"]["detail"].startswith(MANUAL_VERIFICATION_NOTICE))
        self.assertNotIn("lastTelemetry", payload)  # no reading exists, none is invented
        self.assertEqual(written["message"], RECONCILED_MESSAGE)
        # The message is the dashboard timeline title.
        self.assertEqual(
            written["message"],
            "Simulation failed: worker lost. Command possibly moved. Manual verification required.",
        )
        for warning in ("possibly moved", "Manual verification required"):
            self.assertIn(warning, written["message"])
        self.assertEqual(set(written), {"id", "command_id", "event_type", "message", "payload", "created_at"})
        self.assertEqual(set(payload), {"dryRun", "mode", "error", "stop"})
        self.assertEqual(set(payload["error"]), {"kind", "detail"})

        update = client.calls_of("update")[0]
        self.assertEqual(update.values, {"status": "failed"})
        self.assertIn(("eq", "status", "executing"), update.filters)  # conditional
        self.assertIn(("eq", "id", COMMAND_ID), update.filters)
        self.assertEqual(self.status(client), "failed")

    def test_the_recorded_detail_fits_and_keeps_the_notice(self) -> None:
        self.assertLessEqual(len(RECONCILED_DETAIL), MAX_TEXT_LENGTH)
        self.assertIn("possibly moved", RECONCILED_DETAIL)

    def test_only_the_named_command_is_touched(self) -> None:
        client = stale_client()
        other = cmd(OTHER_ID, "2026-11-14T09:00:00+00:00", status="executing", claimed_at=CLAIMED_AT)
        client.tables["commands"].append(other)
        client.tables["audit_events"] += [
            event("execution_started", 50, SIM_STARTED, command_id=OTHER_ID)
        ]
        self.apply(client)
        self.assertEqual(client.tables["commands"][1]["status"], "executing")
        inserts, updates = client.calls_of("insert"), client.calls_of("update")
        self.assertEqual((len(inserts), len(updates)), (1, 1))
        self.assertEqual(inserts[0].values["command_id"], COMMAND_ID)
        self.assertIn(("eq", "id", COMMAND_ID), updates[0].filters)
        self.assertEqual(
            [e for e in client.tables["audit_events"]
             if e["command_id"] == OTHER_ID and e["event_type"] == "execution_failed"],
            [],
        )

    def test_an_invalid_command_id_is_refused_without_a_call(self) -> None:
        client = stale_client()
        for bad in ("", "not-a-uuid", "1; drop table commands", None, 42):
            with self.subTest(bad=bad):
                with self.assertRaises(ReconcileRefused):
                    reconcile_command(bad, client=client)  # type: ignore[arg-type]
        self.assertEqual(client.calls, [])


# --------------------------------------------------------------------------
# Terminal events, terminal statuses and other states (all old enough)
# --------------------------------------------------------------------------


class TerminalAndOtherStateTests(ReconcileTestCase):
    def test_completed_failed_and_rejected_commands_are_left_alone(self) -> None:
        cases = {
            "completed": timeline(("execution_completed", {"dryRun": False})),
            "failed": timeline(("execution_failed", failed_payload())),
            "rejected": timeline(started=None),
        }
        for status, events in cases.items():
            with self.subTest(status=status):
                client = stale_client(status=status, events=events)
                result = self.apply(client)
                self.assertEqual(result.assessment.action, ACTION_NOTHING_TO_DO)
                self.assertIsNone(result.assessment.eligibility)
                self.assertFalse(result.applied)
                self.assertEqual(self.writes(client), [])
                self.assertEqual(self.status(client), status)

    def test_a_terminal_event_is_never_a_candidate_however_old(self) -> None:
        self.set_now(datetime(2030, 1, 1, tzinfo=UTC))
        cases = {
            "execution_failed": timeline(("execution_failed", failed_payload(stop="unconfirmed"))),
            "execution_completed": timeline(("execution_completed", {"dryRun": False})),
            "both": timeline(("execution_failed", failed_payload()),
                             ("execution_completed", {"dryRun": False})),
            "two execution_failed": timeline(("execution_failed", failed_payload()),
                                             ("execution_failed", failed_payload())),
        }
        for name, events in cases.items():
            with self.subTest(case=name):
                client = stale_client(events=events)
                before = copy.deepcopy(client.tables)
                result = self.apply(client)
                self.assertEqual(result.assessment.action, ACTION_NEEDS_PERSON)
                self.assertIsNone(result.assessment.eligibility)
                self.assertEqual(self.writes(client), [])
                self.assertEqual(client.tables, before)

    def test_an_execution_completed_while_executing_is_never_turned_into_a_status(self) -> None:
        client = stale_client(events=timeline(("execution_completed", {"dryRun": False})))
        result = self.apply(client)
        self.assertIn("never records a success", result.assessment.reason)
        self.assertEqual(self.status(client), "executing")

    def test_anything_unusual_needs_a_person_and_writes_nothing(self) -> None:
        cases = {
            "no execution_started": stale_client(events=timeline(started=None)),
            "two execution_started": stale_client(
                events=timeline(("execution_started", SIM_STARTED))
            ),
            "a dry-run command": stale_client(events=timeline(started={"dryRun": True})),
            "an unknown started payload": stale_client(
                events=timeline(started={"dryRun": False, "mode": "live"})
            ),
            "approved, not executing": stale_client(status="approved"),
            "received, not executing": stale_client(status="received", events=timeline(started=None)),
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

    def test_a_read_failure_writes_nothing(self) -> None:
        for table in ("commands", "audit_events"):
            with self.subTest(table=table):
                client = stale_client()
                client.fail(table, "select", RuntimeError("down"))
                with self.assertRaises(ReconcileError):
                    self.apply(client)
                self.assertEqual(self.writes(client), [])

    def test_a_malformed_read_response_writes_nothing(self) -> None:
        client = stale_client()
        client.respond_with("audit_events", "select", "not a list")
        with self.assertRaises(ReconcileError):
            self.apply(client)
        self.assertEqual(self.writes(client), [])


# --------------------------------------------------------------------------
# Repeated runs and partial write failures (idempotency)
# --------------------------------------------------------------------------


class IdempotencyTests(ReconcileTestCase):
    def test_a_repeated_run_writes_nothing_more(self) -> None:
        client = stale_client()
        self.apply(client)
        later = same_tables(client)
        result = self.apply(later)
        self.assertEqual(result.assessment.action, ACTION_NOTHING_TO_DO)
        self.assertEqual(self.writes(later), [])
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_event_written_but_status_update_failed_then_a_rerun_finishes_only_the_status(self) -> None:
        for failure in (WriteError("lost response"), RuntimeError("boom")):
            with self.subTest(failure=type(failure).__name__):
                client = stale_client()
                client.fail("commands", "update", failure)
                first = self.apply(client)
                self.assertEqual(first.events_written, ("execution_failed",))
                self.assertIn("never write it again", first.error or "")
                self.assertIsNone(first.status_after)
                self.assertEqual(self.writes(client), ["audit_events:insert", "commands:update"])
                self.assertEqual(self.status(client), "executing")

                later = same_tables(client)
                dry = reconcile_command(COMMAND_ID, client=later)
                self.assertEqual(dry.assessment.action, ACTION_FINISH_STATUS)
                self.assertEqual(self.writes(later), [])

                second = self.apply(later)
                self.assertIsNone(second.error)
                self.assertEqual(second.events_written, ())
                self.assertEqual(second.status_after, "failed")
                self.assertEqual(self.writes(later), ["commands:update"])  # status only
                update = later.calls_of("update")[0]
                self.assertEqual(update.values, {"status": "failed"})
                self.assertIn(("eq", "status", "executing"), update.filters)
                self.assertIn(("eq", "id", COMMAND_ID), update.filters)
                self.assertEqual(self.status(later), "failed")
                self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

                # Repeated runs after that: nothing to do, nothing written.
                for _ in range(2):
                    again = same_tables(later)
                    result = self.apply(again)
                    self.assertEqual(result.assessment.action, ACTION_NOTHING_TO_DO)
                    self.assertEqual(self.writes(again), [])
                    self.assertEqual(len(self.stored_events(again, "execution_failed")), 1)

    def test_a_status_only_run_that_fails_again_can_be_repeated_without_any_event(self) -> None:
        client = stale_client(events=timeline(("execution_failed", worker_lost_payload())))
        client.fail("commands", "update", WriteError("lost again"))
        first = self.apply(client)
        self.assertEqual(first.assessment.action, ACTION_FINISH_STATUS)
        self.assertIn("writes no event", first.error or "")
        self.assertEqual(self.writes(client), ["commands:update"])
        later = same_tables(client)
        second = self.apply(later)
        self.assertEqual(second.status_after, "failed")
        self.assertEqual(self.writes(later), ["commands:update"])
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_the_status_only_path_needs_the_stale_rule_and_both_flags(self) -> None:
        events = timeline(("execution_failed", worker_lost_payload()))
        # Not eligible yet: nothing.
        self.set_now(at(STARTED_AT, STARTED_THRESHOLD))
        young = stale_client(events=events)
        result = self.apply(young)
        self.assertEqual(result.assessment.action, ACTION_NOT_ELIGIBLE)
        self.assertIn("worker_lost execution_failed is recorded", result.assessment.reason)
        self.assertEqual(self.writes(young), [])
        # Eligible, but without --apply or without the attestation: nothing.
        self.set_now(at(STARTED_AT, STARTED_THRESHOLD + 1))
        client = stale_client(events=events)
        self.assertFalse(reconcile_command(COMMAND_ID, client=client).applied)
        self.assertFalse(
            reconcile_command(COMMAND_ID, worker_stopped_confirmed=True, client=client).applied
        )
        with self.assertRaises(ReconcileRefused):
            reconcile_command(COMMAND_ID, apply=True, client=client)
        self.assertEqual(self.writes(client), [])

    def test_the_status_only_path_is_never_open_with_the_production_bounds(self) -> None:
        self.use_phases(_PRODUCTION_PHASES)
        self.set_now(datetime(2030, 1, 1, tzinfo=UTC))
        client = stale_client(events=timeline(("execution_failed", worker_lost_payload())))
        result = self.apply(client)
        self.assertEqual(result.assessment.action, ACTION_NOT_ELIGIBLE)
        self.assertEqual(self.writes(client), [])

    def test_only_this_scripts_own_unconfirmed_worker_lost_record_opens_the_status_only_path(self) -> None:
        no_mode = worker_lost_payload()
        del no_mode["mode"]
        cases = {
            "the worker's own interrupted failure": failed_payload(stop="unconfirmed"),
            "worker_lost with stop confirmed": build_execution_failed(
                RECONCILED_KIND, RECONCILED_DETAIL, "confirmed"),
            "worker_lost with stop not_needed": build_execution_failed(
                RECONCILED_KIND, RECONCILED_DETAIL, "not_needed"),
            "worker_lost with a reading": build_execution_failed(
                RECONCILED_KIND, RECONCILED_DETAIL, "unconfirmed", _snapshot()),
            "worker_lost without mode (malformed)": no_mode,
            "a dry-run shaped failure": {"dryRun": True, "error": "worker lost"},
            "not an object": "worker_lost",
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                client = stale_client(events=timeline(("execution_failed", payload)))
                result = self.apply(client)
                self.assertEqual(result.assessment.action, ACTION_NEEDS_PERSON)
                self.assertIn("by hand", result.assessment.reason)
                self.assertEqual(self.writes(client), [])
                self.assertEqual(self.status(client), "executing")

    def test_a_duplicated_or_conflicting_worker_lost_record_needs_a_person(self) -> None:
        cases = {
            "two worker_lost": timeline(("execution_failed", worker_lost_payload()),
                                        ("execution_failed", worker_lost_payload())),
            "worker_lost and another failure": timeline(
                ("execution_failed", worker_lost_payload()), ("execution_failed", failed_payload())),
            "worker_lost and a completion": timeline(
                ("execution_failed", worker_lost_payload()), ("execution_completed", {"dryRun": False})),
        }
        for name, events in cases.items():
            with self.subTest(case=name):
                client = stale_client(events=events)
                result = self.apply(client)
                self.assertEqual(result.assessment.action, ACTION_NEEDS_PERSON)
                self.assertEqual(self.writes(client), [])

    def test_a_failed_insert_changes_no_status_and_a_rerun_records_once(self) -> None:
        client = stale_client()
        client.fail("audit_events", "insert", WriteError("timeout"))
        first = self.apply(client)
        self.assertIn("MAY have been written", first.error or "")
        self.assertEqual(first.events_written, ())
        self.assertEqual(self.writes(client), ["audit_events:insert"])  # no status update
        self.assertEqual(self.status(client), "executing")

        later = same_tables(client)  # the insert really did not land in this case
        second = self.apply(later)
        self.assertEqual(second.events_written, ("execution_failed",))
        self.assertEqual(self.status(later), "failed")
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_an_insert_that_landed_despite_the_error_is_not_written_twice(self) -> None:
        client = stale_client()

        def land_then_fail(c: FakeClient) -> None:
            c.tables["audit_events"].append(
                event("execution_failed", 90, build_execution_failed(
                    RECONCILED_KIND, RECONCILED_DETAIL, "unconfirmed"))
            )

        client.before_next("insert", land_then_fail)
        client.fail("audit_events", "insert", WriteError("response lost"))
        self.apply(client)
        later = same_tables(client)
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)
        result = self.apply(later)
        self.assertEqual(result.assessment.action, ACTION_FINISH_STATUS)
        self.assertEqual(self.writes(later), ["commands:update"])  # status only, no insert
        self.assertEqual(self.status(later), "failed")
        self.assertEqual(len(self.stored_events(later, "execution_failed")), 1)

    def test_a_status_changed_after_the_read_is_not_overwritten(self) -> None:
        client = stale_client()
        client.before_next("update", lambda c: c.tables["commands"][0].update(status="completed"))
        result = self.apply(client)
        self.assertIn("matched no row", result.error or "")
        self.assertEqual(self.status(client), "completed")  # untouched
        self.assertEqual(len(client.calls_of("update")), 1)  # attempted once, not retried

    def test_nothing_is_ever_retried_within_a_run(self) -> None:
        for table, op in (("audit_events", "insert"), ("commands", "update")):
            with self.subTest(op=op):
                client = stale_client()
                client.fail(table, op, WriteError("x"))
                self.apply(client)
                self.assertEqual(len(client.calls_of(op)), 1)

    def test_a_state_conflict_from_the_store_is_reported_not_retried(self) -> None:
        client = stale_client()
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
        out = io.StringIO()
        with mock.patch.object(rc, "_get_client", return_value=client) as factory, redirect_stdout(out):
            code = rc.main(argv)
        self.factory = factory
        return code, out.getvalue()

    def test_a_target_is_required(self) -> None:
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            rc.main([])

    def test_the_dry_run_of_a_candidate_prints_the_rule_and_how_to_write(self) -> None:
        client = stale_client()
        code, text = self.run_main(["--command-id", COMMAND_ID], client)
        self.assertEqual(code, 0)
        for fragment in (
            "DRY RUN", "Nothing was written", "possibly moved",
            f"timestamp source {SOURCE_EXECUTION_STARTED}", "Maximum worker runtime from that "
            "timestamp: 60.0 s", f"Effective threshold: {STARTED_THRESHOLD:.1f} s", "Qualifies: YES",
            "Age is never proof", "--confirm-worker-stopped",
        ):
            self.assertIn(fragment, text)
        self.assertEqual(self.writes(client), [])

    def test_apply_without_the_attestation_reads_and_writes_nothing(self) -> None:
        client = stale_client()
        code, text = self.run_main(["--command-id", COMMAND_ID, "--apply"], client)
        self.assertEqual(code, 2)
        self.assertIn("needs --confirm-worker-stopped", text)
        self.factory.assert_not_called()
        self.assertEqual(client.calls, [])

    def test_the_attestation_without_apply_is_a_dry_run(self) -> None:
        client = stale_client()
        code, text = self.run_main(["--command-id", COMMAND_ID, "--confirm-worker-stopped"], client)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", text)
        self.assertEqual(self.writes(client), [])

    def test_apply_with_the_attestation_writes_and_says_to_verify(self) -> None:
        client = stale_client()
        code, text = self.run_main(
            ["--command-id", COMMAND_ID, "--apply", "--confirm-worker-stopped"], client
        )
        self.assertEqual(code, 0)
        self.assertIn("APPLIED", text)
        self.assertIn(MANUAL_VERIFICATION_NOTICE, text)
        self.assertEqual(self.status(client), "failed")

    def test_a_not_eligible_command_exits_5_even_with_both_flags(self) -> None:
        self.set_now(at(STARTED_AT, 10.0))
        client = stale_client()
        code, text = self.run_main(
            ["--command-id", COMMAND_ID, "--apply", "--confirm-worker-stopped"], client
        )
        self.assertEqual(code, 5)
        self.assertIn("Qualifies: NO", text)
        self.assertEqual(self.writes(client), [])

    def test_a_failed_write_exits_nonzero(self) -> None:
        client = stale_client()
        client.fail("commands", "update", WriteError("lost"))
        code, text = self.run_main(
            ["--command-id", COMMAND_ID, "--apply", "--confirm-worker-stopped"], client
        )
        self.assertEqual(code, 1)
        self.assertIn("WRITE FAILED", text)

    def test_needs_person_exits_4(self) -> None:
        client = stale_client(events=timeline(("execution_completed", {"dryRun": False})))
        code, text = self.run_main(["--command-id", COMMAND_ID], client)
        self.assertEqual(code, 4)
        self.assertIn("Stale rule: not evaluated", text)

    def test_list_is_read_only_informational_and_refuses_apply(self) -> None:
        client = stale_client()
        code, text = self.run_main(["--list"], client)
        self.assertEqual(code, 0)
        self.assertIn(COMMAND_ID, text)
        self.assertIn(ACTION_RECORD_FAILURE, text)
        self.assertIn("At most 50 rows", text)
        self.assertIn("does not authorize reconciliation", text)
        self.assertEqual(self.writes(client), [])
        code, _ = self.run_main(["--list", "--apply", "--confirm-worker-stopped"], client)
        self.assertEqual(code, 2)
        self.assertEqual(self.writes(client), [])

    def test_there_is_no_sweep_retry_threshold_or_movement_option(self) -> None:
        tree = ast.parse((WORKER_DIR / "reconcile_executing.py").read_text(encoding="utf-8"))
        options = {
            arg.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant)
        }
        self.assertEqual(options, {"--command-id", "--list", "--apply", "--confirm-worker-stopped"})


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
