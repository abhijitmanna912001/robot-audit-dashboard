"""The bounded movement sequence (simulation_executor.py), with a fake twin only.

Offline: no Cyberwave, no simulation session, no network, no database. Every
"movement" below is a method call on ``tests.fake_twin.FakeTwin``, which only
changes numbers in memory. The network guard is active.
"""

from __future__ import annotations

import dataclasses
import inspect
import multiprocessing
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import simulation_executor
from simulation_executor import (
    DEFAULT_TIMEOUTS,
    RehearsalFailure,
    RehearsalSuccess,
    StopResult,
    Timeouts,
    run_rehearsal,
)
from simulation_gates import LiveSimulationGates, load_shared_limits
from simulation_child import MAX_MOVES
from simulation_supervisor import SessionRefused, SessionTimeout, SessionUnusable, TwinCallFailed
from tests.fake_twin import ALL_CONFIRMED, FAST, HANG, START, FakeTwin
from tests.fake_twin_child import open_process_session
from tests.inprocess_session import InProcessSession
from tests.test_simulation_gates import (
    ALL_TRUE,
    REAL_CYBERWAVE_GATES,
    action,
    snapshot,
    use_canonical_gates,
)

SUCCESS_CALLS = [
    "connect",
    "runtime_target",
    "twin_target",
    "read_telemetry",  # 1 start
    "move_joint",  # 1 target
    "read_telemetry",  # 2 target wait
    "read_telemetry",  # 3 target verify
    "move_joint",  # 2 return
    "read_telemetry",  # 4 return wait
    "read_telemetry",  # 5 restored verify
    "disconnect",
]
DEFAULT_PLAN: Any = object()  # sentinel: "use the permitted first-rehearsal plan"


def assert_no_child_processes(test: unittest.TestCase) -> None:
    """No supervised twin process may outlive a test (orphans, unfinished work)."""
    left = [p.name for p in multiprocessing.active_children()]
    test.assertEqual(left, [], "a twin child process was left running")


def open_session(test: unittest.TestCase, twin: FakeTwin, **spec_options: Any) -> Any:
    """A STARTED session for ``twin``: supervised (real child process) when the fake
    uses HANG, which only process supervision can end; otherwise in this process."""
    if twin.uses_hang() or spec_options:
        return open_process_session(test, twin, **spec_options)
    return InProcessSession(twin)


class ExecutorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(assert_no_child_processes, self)  # runs last
        # TEST ONLY: the fake twin's ALL_CONFIRMED becomes the canonical object,
        # so the identity check can pass. Undone after each test.
        use_canonical_gates(self, ALL_CONFIRMED)
        self.twin = FakeTwin()
        self.addCleanup(self.twin.release.set)  # let any hung fake call finish

    def use_gates(self, gates: LiveSimulationGates) -> None:
        """Make ``gates`` canonical AND give it to the twin, so its fields are what is tested."""
        use_canonical_gates(self, gates)
        self.twin.confirmations = gates

    def run_it(self, snap: Any = DEFAULT_PLAN, timeouts: Timeouts = FAST, **spec_options: Any) -> Any:
        # The session (and any child process) is started BEFORE the timed call,
        # like the processor starts it before the rehearsal clock.
        self.session = open_session(self, self.twin, **spec_options)
        started = time.monotonic()
        try:
            return run_rehearsal(snapshot() if snap is DEFAULT_PLAN else snap, self.session, timeouts)
        finally:
            self.elapsed = time.monotonic() - started
            self.session.close()

    def failure(self, kind: str, **kwargs: Any) -> RehearsalFailure:
        outcome = self.run_it(**kwargs)
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        self.assertEqual(outcome.kind, kind, outcome.detail)
        self.assertLessEqual(len(outcome.detail), 200)
        return outcome

    def assert_nothing_sent(self, outcome: RehearsalFailure) -> None:
        self.assertEqual(self.twin.moves(), [])
        self.assertNotIn("stop", self.twin.names())
        self.assertEqual(outcome.stop, "not_needed")
        self.assertFalse(outcome.target_dispatched)
        self.assertFalse(outcome.return_dispatched)


class SuccessTests(ExecutorCase):
    def test_exact_call_sequence(self) -> None:
        outcome = self.run_it()
        self.assertIsInstance(outcome, RehearsalSuccess)
        self.assertEqual(self.twin.names(), SUCCESS_CALLS)

    def test_exactly_one_target_movement_and_one_restoration_movement(self) -> None:
        self.run_it()
        self.assertEqual(self.twin.moves(), [("_1", 0.25), ("_1", START["_1"])])
        self.assertNotIn("stop", self.twin.names())

    def test_the_three_readings_are_what_was_measured(self) -> None:
        outcome = self.run_it()
        self.assertEqual(dict(outcome.start.joints), START)
        self.assertEqual(dict(outcome.target.joints), {**START, "_1": 0.25})
        self.assertEqual(dict(outcome.restored.joints), START)
        self.assertLessEqual(outcome.start.read_at, outcome.target.read_at)
        self.assertLessEqual(outcome.target.read_at, outcome.restored.read_at)

    def test_readings_within_tolerance_are_accepted(self) -> None:
        self.twin.move_offset = 0.004  # target reads 0.254, restored reads 0.104
        self.assertIsInstance(self.run_it(), RehearsalSuccess)

    def test_the_return_goes_to_the_captured_start_not_to_zero(self) -> None:
        self.twin = FakeTwin({**START, "_1": -0.4})
        self.run_it()
        self.assertEqual(self.twin.moves(), [("_1", 0.25), ("_1", -0.4)])


class RestrictionAndSnapshotTests(ExecutorCase):
    def test_only_the_first_rehearsal_plan_is_executed(self) -> None:
        for name, snap in {
            "another joint": snapshot(action(target="_2")),
            "another target": snapshot(action(value=0.5)),
            "two actions": snapshot(action(), action(actionId="a2", target="_3")),
            "another type": snapshot(action(type="grip")),
        }.items():
            with self.subTest(case=name):
                self.twin = FakeTwin()
                outcome = self.failure("rehearsal_restriction", snap=snap)
                self.assertEqual(self.twin.calls, [])  # not even a connection
                self.assert_nothing_sent(outcome)

    def test_command_text_or_raw_actions_are_refused(self) -> None:
        for value in ("set joint _1 to 0.25 rad", [action()], None):
            with self.subTest(value=type(value).__name__):
                self.twin = FakeTwin()
                self.failure("approval_invalid", snap=value)
                self.assertEqual(self.twin.calls, [])

    def test_the_snapshot_hash_is_rechecked(self) -> None:
        forged = dataclasses.replace(
            snapshot(), canonical_actions=snapshot(action(value=1.5)).canonical_actions
        )
        self.failure("approval_invalid", snap=forged)
        self.assertEqual(self.twin.calls, [])

    def test_the_hash_is_checked_again_immediately_before_the_movement(self) -> None:
        with mock.patch.object(
            simulation_executor, "require_first_rehearsal_plan",
            wraps=simulation_executor.require_first_rehearsal_plan,
        ) as check:
            self.run_it()
        self.assertEqual(check.call_count, 2)

    def test_the_executor_takes_a_snapshot_and_no_command_text(self) -> None:
        self.assertEqual(list(inspect.signature(run_rehearsal).parameters), ["snapshot", "session", "timeouts"])


class GateTests(ExecutorCase):
    def test_no_movement_when_any_gate_is_unconfirmed(self) -> None:
        names = [f.name for f in dataclasses.fields(LiveSimulationGates)]
        for missing in names:
            with self.subTest(missing=missing):
                self.twin = FakeTwin()
                self.use_gates(LiveSimulationGates(**{n: n != missing for n in names}))
                outcome = self.failure("gate_unconfirmed")
                self.assertEqual(self.twin.calls, [])
                self.assert_nothing_sent(outcome)

    def test_no_movement_when_the_cancellation_method_is_unconfirmed(self) -> None:
        self.use_gates(dataclasses.replace(ALL_CONFIRMED, stop_method_confirmed=False))
        outcome = self.failure("gate_unconfirmed")
        self.assertIn("stop/cancel", outcome.detail)
        self.assertEqual(self.twin.calls, [])

    def test_no_movement_with_the_real_cyberwave_gates(self) -> None:
        CYBERWAVE_GATES = REAL_CYBERWAVE_GATES  # the unpatched, all-False object

        self.use_gates(CYBERWAVE_GATES)
        self.failure("gate_unconfirmed")
        self.assertEqual(self.twin.calls, [])

    def test_missing_gates_block(self) -> None:
        for value in (None, True, "confirmed"):
            with self.subTest(value=value):
                self.twin = FakeTwin()
                self.twin.confirmations = value
                self.failure("gate_unconfirmed")
                self.assertEqual(self.twin.calls, [])


class CanonicalGateTests(ExecutorCase):
    """The twin cannot supply replacement confirmations (executor path)."""

    def assert_refused_before_anything(self, fragment: str) -> None:
        outcome = self.failure("gate_unconfirmed")
        self.assertIn(fragment, outcome.detail)
        self.assertEqual(self.twin.calls, [])  # not even a connection
        self.assert_nothing_sent(outcome)

    def test_a_twin_with_its_own_all_true_gates_is_refused_with_the_real_gates(self) -> None:
        use_canonical_gates(self, REAL_CYBERWAVE_GATES)  # production state: all False
        self.twin.confirmations = LiveSimulationGates(**ALL_TRUE)
        self.assert_refused_before_anything("not the canonical")

    def test_an_equal_all_true_copy_is_refused_even_when_canonical_is_all_true(self) -> None:
        copy = LiveSimulationGates(**ALL_TRUE)
        self.assertEqual(copy, ALL_CONFIRMED)  # equal to the (patched) canonical object
        self.twin.confirmations = copy
        self.assert_refused_before_anything("not the canonical")

    def test_a_different_all_false_instance_is_refused(self) -> None:
        use_canonical_gates(self, REAL_CYBERWAVE_GATES)
        self.twin.confirmations = LiveSimulationGates()
        self.assert_refused_before_anything("not the canonical")

    def test_missing_confirmations_are_refused(self) -> None:
        del self.twin.confirmations
        self.assertFalse(hasattr(self.twin, "confirmations"))
        self.assert_refused_before_anything("carries no live-simulation gates")

    def test_an_incorrect_confirmations_type_is_refused(self) -> None:
        class Subclass(LiveSimulationGates):
            pass

        for value in (dict(ALL_TRUE), types.SimpleNamespace(**ALL_TRUE), Subclass(**ALL_TRUE)):
            with self.subTest(value=type(value).__name__):
                self.twin = FakeTwin()
                self.twin.confirmations = value
                self.assert_refused_before_anything("not a LiveSimulationGates object")

    def test_the_canonical_object_itself_is_accepted(self) -> None:
        import simulation_gates

        self.assertIs(self.twin.confirmations, simulation_gates.CYBERWAVE_GATES)
        self.assertIsInstance(self.run_it(), RehearsalSuccess)
        self.assertEqual(self.twin.names(), SUCCESS_CALLS)

    def test_swapping_in_own_gates_after_the_movement_cannot_confirm_the_stop(self) -> None:
        # After a dispatch the stop is still tried once (best effort), but a twin
        # that replaced its confirmations can never get a "confirmed" stop, even
        # though its stop method returns StopResult(confirmed=True).
        def swap(name: str) -> None:
            if name == "move_joint":
                self.twin.confirmations = LiveSimulationGates(**ALL_TRUE)

        self.twin.recorder = swap
        self.twin.ignore_moves = {1}
        self.assertEqual(self.twin.stop_result, StopResult(confirmed=True))
        outcome = self.failure("target_timeout")
        self.assertEqual(outcome.stop, "unconfirmed")
        self.assertEqual(self.twin.names().count("stop"), 1)  # tried exactly once
        self.assertEqual(self.twin.moves(), [("_1", 0.25)])  # no return movement either


class SimulationOnlyTests(ExecutorCase):
    def test_no_movement_when_the_sdk_runtime_is_not_simulation(self) -> None:
        for value in ("live", "hardware", "edge", "sim", "Simulation", "", None, True, ["simulation"]):
            with self.subTest(runtime=value):
                self.twin = FakeTwin()
                self.twin.reported_runtime_target = value
                outcome = self.failure("not_simulation")
                self.assert_nothing_sent(outcome)
                self.assertNotIn("read_telemetry", self.twin.names())

    def test_no_movement_when_the_twin_reports_live_hardware(self) -> None:
        for value in ("live", "hardware", "physical", None, 1):
            with self.subTest(twin=value):
                self.twin = FakeTwin()
                self.twin.reported_twin_target = value
                outcome = self.failure("not_simulation")
                self.assert_nothing_sent(outcome)
                self.assertNotIn("read_telemetry", self.twin.names())

    def test_no_movement_when_the_mode_cannot_be_read(self) -> None:
        for method in ("runtime_target", "twin_target"):
            with self.subTest(method=method):
                self.twin = FakeTwin()
                self.twin.fail[method] = RuntimeError("no such attribute")
                self.assert_nothing_sent(self.failure("not_simulation"))

    def test_both_checks_run_before_the_first_read_and_the_first_movement(self) -> None:
        self.run_it()
        names = self.twin.names()
        self.assertLess(names.index("runtime_target"), names.index("read_telemetry"))
        self.assertLess(names.index("twin_target"), names.index("read_telemetry"))
        self.assertLess(names.index("read_telemetry"), names.index("move_joint"))

    def test_the_twin_interface_has_no_target_selector(self) -> None:
        protocol = simulation_executor.SimulationTwin
        self.assertEqual(list(inspect.signature(protocol.move_joint).parameters), ["self", "joint", "value_rad"])
        self.assertEqual(list(inspect.signature(protocol.connect).parameters), ["self", "timeout_s"])
        for name in ("affect", "set_mode", "use_live", "target", "mode"):
            self.assertFalse(hasattr(protocol, name), name)
            self.assertFalse(hasattr(FakeTwin, name), name)

    def test_connection_failure_sends_nothing(self) -> None:
        self.twin.fail["connect"] = ConnectionError("refused")
        outcome = self.failure("connection_failed")
        self.assert_nothing_sent(outcome)
        self.assertEqual(self.twin.names(), ["connect", "disconnect"])


class StartTelemetryTests(ExecutorCase):
    def test_no_movement_when_telemetry_provenance_is_unverified(self) -> None:
        cases: dict[str, Any] = {
            "no source": lambda t: setattr(t, "source_type", None),
            "hardware source": lambda t: setattr(t, "source_type", "edge"),
            "live teleop": lambda t: setattr(t, "source_type", "tele"),
            "simulation teleop command": lambda t: setattr(t, "source_type", "sim_tele"),
            "editor command": lambda t: setattr(t, "source_type", "edit"),
            "unknown source": lambda t: setattr(t, "source_type", "simulation"),
            "source of the wrong type": lambda t: setattr(t, "source_type", True),
            "no update": lambda t: setattr(t, "update_received", False),
            "stale": lambda t: setattr(t, "age_s", 30.0),
            "no timestamp": lambda t: setattr(t, "omit_timestamp", True),
            "string timestamp": lambda t: setattr(t, "bad_timestamp", "2026-11-14T08:30:01Z"),
            "millisecond timestamp": lambda t: setattr(t, "bad_timestamp", time.time() * 1000),
            "negative timestamp": lambda t: setattr(t, "bad_timestamp", -1.0),
            "five joints": lambda t: t.positions.pop("_6"),
            "nan": lambda t: t.positions.__setitem__("_2", float("nan")),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name):
                self.twin = FakeTwin()
                arrange(self.twin)
                outcome = self.failure("telemetry_invalid")
                self.assert_nothing_sent(outcome)
                self.assertIsNone(outcome.last_telemetry)

    def test_sdk_zero_fill_is_rejected_as_a_placeholder(self) -> None:
        self.twin = FakeTwin({name: 0.0 for name in START})
        self.twin.update_received = False
        outcome = self.failure("telemetry_invalid")
        self.assertIn("placeholder_suspected", outcome.detail)
        self.assert_nothing_sent(outcome)

    def test_a_failed_start_read_sends_nothing(self) -> None:
        self.twin.fail["read_telemetry"] = TimeoutError("no telemetry")
        self.assert_nothing_sent(self.failure("telemetry_unavailable"))


class LimitsTests(ExecutorCase):
    def test_shared_limits_are_revalidated_after_the_start_read_and_before_dispatch(self) -> None:
        order: list[str] = []
        self.twin.recorder = order.append

        def load() -> Any:
            order.append("load_shared_limits")
            return load_shared_limits()

        with mock.patch.object(simulation_executor, "load_shared_limits", side_effect=load):
            self.run_it()
        self.assertEqual(order.count("load_shared_limits"), 1)
        self.assertLess(order.index("read_telemetry"), order.index("load_shared_limits"))
        self.assertLess(order.index("load_shared_limits"), order.index("move_joint"))

    def test_no_movement_when_the_target_is_outside_the_shared_limits(self) -> None:
        narrow = {**load_shared_limits(), "_1": (-0.2, 0.2)}
        with mock.patch.object(simulation_executor, "load_shared_limits", return_value=narrow):
            outcome = self.failure("limits_revalidation_failed")
        self.assert_nothing_sent(outcome)
        self.assertEqual(dict(outcome.last_telemetry.joints), START)  # the start reading

    def test_no_movement_when_the_return_position_is_outside_the_shared_limits(self) -> None:
        self.twin = FakeTwin({**START, "_1": 1.95})
        self.assert_nothing_sent(self.failure("limits_revalidation_failed"))

    def test_no_movement_when_the_limits_file_cannot_be_read(self) -> None:
        from simulation_gates import GateError

        with mock.patch.object(
            simulation_executor, "load_shared_limits",
            side_effect=GateError("limits_revalidation_failed", "the shared joint limits could not be read"),
        ):
            self.assert_nothing_sent(self.failure("limits_revalidation_failed"))


class MovementFailureTests(ExecutorCase):
    def assert_stopped_once(self, outcome: RehearsalFailure, moves: int) -> None:
        self.assertEqual(self.twin.names().count("stop"), 1)
        self.assertEqual(len(self.twin.moves()), moves)  # nothing re-sent
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(outcome.stop, "confirmed")
        # No movement after the stop.
        after_stop = self.twin.names()[self.twin.names().index("stop") :]
        self.assertNotIn("move_joint", after_stop)

    def test_target_movement_call_fails(self) -> None:
        self.twin.fail_move_number[1] = RuntimeError("publish failed")
        outcome = self.failure("dispatch_failed")
        self.assert_stopped_once(outcome, moves=1)
        self.assertFalse(outcome.return_dispatched)

    def test_target_timeout_is_handled(self) -> None:
        self.twin.ignore_moves = {1}  # accepted, but the joint never moves
        outcome = self.failure("target_timeout")
        self.assert_stopped_once(outcome, moves=1)
        self.assertFalse(outcome.return_dispatched)
        self.assertEqual(dict(outcome.last_telemetry.joints), START)

    def test_a_target_outside_the_tolerance_is_not_reached(self) -> None:
        self.twin.move_offset = 0.05  # reads 0.30
        outcome = self.failure("target_timeout")
        self.assert_stopped_once(outcome, moves=1)

    def test_other_joints_moving_at_the_target_is_a_failure(self) -> None:
        self.twin.drift_on_move = {1: {"_3": 0.05}}
        outcome = self.failure("unexpected_joint_motion")
        self.assertIn("_3", outcome.detail)
        self.assert_stopped_once(outcome, moves=1)  # no return movement was sent
        self.assertFalse(outcome.return_dispatched)

    def test_the_target_must_hold_on_the_verification_reading(self) -> None:
        self.twin.read_overrides[3] = lambda positions: self.twin.reading({**positions, "_1": 0.1})
        outcome = self.failure("target_not_verified")
        self.assert_stopped_once(outcome, moves=1)

    def test_a_commanded_target_is_not_accepted_as_proof_of_the_target(self) -> None:
        # The joint did not move; a command message carrying the target arrives
        # instead (source_type "sim_tele" is a command, not simulator state).
        self.twin.ignore_moves = {1}
        self.twin.read_overrides[2] = lambda positions: self.twin.reading(
            {**positions, "_1": 0.25}, source_type="sim_tele"
        )
        outcome = self.failure("telemetry_invalid")
        self.assertIn("commanded_not_measured", outcome.detail)
        self.assert_stopped_once(outcome, moves=1)

    def test_telemetry_losing_its_provenance_after_dispatch_is_a_failure(self) -> None:
        self.twin.read_overrides[2] = lambda positions: self.twin.reading(source_type=None)
        outcome = self.failure("telemetry_invalid")
        self.assert_stopped_once(outcome, moves=1)

    def test_a_reading_from_before_the_movement_is_not_evidence(self) -> None:
        # An old-but-valid reading already showing 0.25 must not end the wait.
        def before_dispatch(positions: dict[str, float]) -> Any:
            return self.twin.reading({**positions, "_1": 0.25}, timestamp=time.time() - 1.0)

        self.twin.ignore_moves = {1}
        self.twin.read_overrides[2] = before_dispatch
        outcome = self.failure("target_timeout")
        self.assert_stopped_once(outcome, moves=1)

    def test_return_movement_call_fails(self) -> None:
        self.twin.fail_move_number[2] = RuntimeError("publish failed")
        outcome = self.failure("dispatch_failed")
        self.assert_stopped_once(outcome, moves=2)
        self.assertTrue(outcome.return_dispatched)
        self.assertIn("return", outcome.detail)

    def test_return_timeout_is_handled(self) -> None:
        self.twin.ignore_moves = {2}
        outcome = self.failure("return_timeout")
        self.assert_stopped_once(outcome, moves=2)  # no second return movement
        self.assertEqual(dict(outcome.last_telemetry.joints), {**START, "_1": 0.25})

    def test_restoration_must_hold_on_the_verification_reading(self) -> None:
        self.twin.read_overrides[5] = lambda positions: self.twin.reading({**positions, "_5": 0.6})
        outcome = self.failure("restore_not_verified")
        self.assertIn("_5", outcome.detail)
        self.assert_stopped_once(outcome, moves=2)

    def test_a_joint_drifting_during_the_return_is_never_restored(self) -> None:
        self.twin.drift_on_move = {2: {"_4": 0.05}}
        outcome = self.failure("return_timeout")
        self.assert_stopped_once(outcome, moves=2)

    def test_no_automatic_retry_of_anything(self) -> None:
        self.twin.fail_move_number[1] = RuntimeError("publish failed")
        self.failure("dispatch_failed")
        names = self.twin.names()
        self.assertEqual(names.count("connect"), 1)
        self.assertEqual(names.count("move_joint"), 1)
        self.assertEqual(names.count("stop"), 1)
        self.assertEqual(names[-1], "disconnect")

    def test_error_details_never_carry_exception_text(self) -> None:
        secret = "https://api.example.invalid/v1?token=sk-LEAK-123 Authorization: Bearer LEAK"
        for arrange in (
            lambda t: t.fail.__setitem__("connect", RuntimeError(secret)),
            lambda t: t.fail.__setitem__("read_telemetry", RuntimeError(secret)),
            lambda t: t.fail_move_number.__setitem__(1, RuntimeError(secret)),
            lambda t: t.fail_move_number.__setitem__(2, RuntimeError(secret)),
        ):
            self.twin = FakeTwin()
            arrange(self.twin)
            outcome = self.run_it()
            self.assertIsInstance(outcome, RehearsalFailure)
            for fragment in ("LEAK", "https", "Bearer", "token"):
                self.assertNotIn(fragment, outcome.detail)
            self.assertIn("RuntimeError", outcome.detail)


class StopTests(ExecutorCase):
    def stop_after_failed_target(self, stop_result: Any = None, fail: Any = None) -> RehearsalFailure:
        self.twin = FakeTwin()
        self.addCleanup(self.twin.release.set)
        self.twin.ignore_moves = {1}
        if fail is not None:
            self.twin.fail["stop"] = fail
        else:
            self.twin.stop_result = stop_result
        return self.failure("target_timeout")

    def test_successful_stop_confirmation_is_recorded(self) -> None:
        outcome = self.stop_after_failed_target(StopResult(confirmed=True))
        self.assertEqual(outcome.stop, "confirmed")

    def test_failed_or_ambiguous_stop_is_unconfirmed(self) -> None:
        for name, result in {
            "explicit false": StopResult(confirmed=False),
            "default": StopResult(),
            "none": None,
            "true": True,
            "ok string": "ok",
            "dict": {"confirmed": True},
            "truthy confirmed": StopResult(confirmed="yes"),  # type: ignore[arg-type]
            "http 200": 200,
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.stop_after_failed_target(result).stop, "unconfirmed")
                self.assertEqual(self.twin.names().count("stop"), 1)  # not retried

    def test_a_stop_that_raises_or_hangs_is_unconfirmed(self) -> None:
        for fail in (RuntimeError("connection lost"), HANG):
            with self.subTest(fail=str(fail)):
                outcome = self.stop_after_failed_target(fail=fail)
                self.assertEqual(outcome.stop, "unconfirmed")
                self.assertEqual(self.twin.names().count("stop"), 1)
                self.assertEqual(len(self.twin.moves()), 1)

    def test_stop_is_never_called_when_nothing_was_sent(self) -> None:
        self.twin.reported_twin_target = "live"
        outcome = self.failure("not_simulation")
        self.assertEqual(outcome.stop, "not_needed")
        self.assertNotIn("stop", self.twin.names())

    def test_last_telemetry_only_from_a_reading_that_validates(self) -> None:
        self.twin.ignore_moves = {1}
        good = self.failure("target_timeout")
        self.assertIsNotNone(good.last_telemetry)

        self.twin = FakeTwin()
        self.twin.fail_move_number[1] = RuntimeError("x")
        self.twin.read_overrides[2] = lambda positions: self.twin.reading(source_type=None)
        bad = self.failure("dispatch_failed")
        self.assertIsNone(bad.last_telemetry)  # the earlier start reading is not reused

        self.twin = FakeTwin()
        self.twin.fail_move_number[1] = RuntimeError("x")
        self.twin.fail_read_number[2] = RuntimeError("read failed")
        self.assertIsNone(self.failure("dispatch_failed").last_telemetry)

    def test_a_gate_failure_before_dispatch_does_not_call_stop(self) -> None:
        for name, value in {
            "own all-true gates": LiveSimulationGates(**ALL_TRUE),
            "an unconfirmed gate": dataclasses.replace(ALL_CONFIRMED, stop_method_confirmed=False),
            "no gates": None,
        }.items():
            with self.subTest(case=name):
                self.twin = FakeTwin()
                if name == "an unconfirmed gate":
                    self.use_gates(value)
                else:
                    self.twin.confirmations = value
                outcome = self.failure("gate_unconfirmed")
                self.assertEqual(outcome.stop, "not_needed")
                self.assertEqual(self.twin.calls, [])  # no connect, no move, no stop

    def test_a_post_dispatch_gate_failure_attempts_stop_exactly_once(self) -> None:
        for name, value in {
            "own all-true gates": LiveSimulationGates(**ALL_TRUE),
            "an equal copy": dataclasses.replace(ALL_CONFIRMED),
            "no gates": None,
        }.items():
            with self.subTest(case=name):
                self.twin = FakeTwin()
                self.addCleanup(self.twin.release.set)

                def swap(call: str, twin: FakeTwin = self.twin, value: Any = value) -> None:
                    if call == "move_joint":
                        twin.confirmations = value

                self.twin.recorder = swap
                self.twin.ignore_moves = {1}
                outcome = self.failure("target_timeout")
                self.assertEqual(self.twin.names().count("stop"), 1)
                self.assertEqual(outcome.stop, "unconfirmed")
                self.assertEqual(len(self.twin.moves()), 1)

    def test_gates_invalidated_during_the_stop_call_are_unconfirmed(self) -> None:
        def swap(call: str) -> None:
            if call == "stop":
                self.twin.confirmations = LiveSimulationGates(**ALL_TRUE)

        self.twin.recorder = swap
        self.twin.ignore_moves = {1}
        outcome = self.failure("target_timeout")
        self.assertEqual(outcome.stop, "unconfirmed")
        self.assertEqual(self.twin.names().count("stop"), 1)

    def test_an_expired_stop_budget_does_not_call_stop(self) -> None:
        original = simulation_executor._Rehearsal._attempt_stop

        def with_expired_deadline(rehearsal: Any) -> str:
            rehearsal.deadline = time.monotonic() - 1.0  # the total deadline has passed
            return original(rehearsal)

        self.twin.ignore_moves = {1}
        with mock.patch.object(simulation_executor._Rehearsal, "_attempt_stop", with_expired_deadline):
            outcome = self.failure("target_timeout")
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(outcome.stop, "unconfirmed")
        self.assertNotIn("stop", self.twin.names())

    def test_a_returned_value_is_not_a_confirmation(self) -> None:
        class LookalikeResult(StopResult):
            pass

        for name, result in {
            "subclass claiming confirmed": LookalikeResult(confirmed=True),
            "returned without raising": None,
            "confirmed int": StopResult(confirmed=1),  # type: ignore[arg-type]
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.stop_after_failed_target(result).stop, "unconfirmed")
                self.assertEqual(self.twin.names().count("stop"), 1)

    def test_the_stop_is_never_retried(self) -> None:
        for name, behaviour in {
            "confirmed": StopResult(confirmed=True),
            "unconfirmed": StopResult(confirmed=False),
            "raises": RuntimeError("connection lost"),
        }.items():
            with self.subTest(case=name):
                twin = FakeTwin()
                if isinstance(behaviour, BaseException):
                    twin.fail["stop"] = behaviour
                else:
                    twin.stop_result = behaviour
                rehearsal = simulation_executor._Rehearsal(snapshot(), InProcessSession(twin), FAST)
                rehearsal.target_dispatched = True
                first = rehearsal._attempt_stop()
                second = rehearsal._attempt_stop()  # a second request makes no call
                self.assertEqual(twin.names().count("stop"), 1)
                self.assertEqual(second, "unconfirmed")
                self.assertEqual(first, "confirmed" if name == "confirmed" else "unconfirmed")

    def test_an_interruption_after_dispatch_tries_the_stop_once_and_propagates(self) -> None:
        self.twin.fail_move_number[1] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_it()
        self.assertEqual(self.twin.names().count("stop"), 1)
        self.assertEqual(self.twin.names()[-1], "disconnect")
        self.assertEqual(len(self.twin.moves()), 1)


class TimeoutTests(ExecutorCase):
    def test_default_limits_are_the_documented_values(self) -> None:
        self.assertEqual(
            dataclasses.asdict(DEFAULT_TIMEOUTS),
            {
                "connect_s": 10.0, "mode_check_s": 3.0, "telemetry_read_s": 3.0,
                "dispatch_s": 5.0, "target_move_s": 10.0, "target_verify_s": 3.0,
                "return_move_s": 10.0, "restore_verify_s": 3.0, "stop_s": 5.0,
                "disconnect_s": 3.0, "total_s": 75.0, "poll_interval_s": 0.2,
            },
        )
        self.assertEqual(DEFAULT_TIMEOUTS.recovery_reserve_s, 11.0)
        phases = 10 + 3 + 3 + 3 + 5 + 10 + 3 + 5 + 10 + 3
        self.assertLessEqual(phases + DEFAULT_TIMEOUTS.recovery_reserve_s, DEFAULT_TIMEOUTS.total_s)

    def test_limits_can_be_tightened_but_never_loosened_or_disabled(self) -> None:
        Timeouts(target_move_s=1.0)
        for field in dataclasses.fields(Timeouts):
            for bad in (field.default + 0.5, 0, -1.0, float("inf"), float("nan"), None, True, "5"):
                with self.subTest(field=field.name, value=bad):
                    with self.assertRaises(ValueError):
                        Timeouts(**{field.name: bad})
        with self.assertRaises(ValueError):
            Timeouts(total_s=5.0)  # shorter than the recovery reserve
        with self.assertRaises(TypeError):
            run_rehearsal(snapshot(), self.twin, timeouts={"total_s": 9999})  # type: ignore[arg-type]

    def test_no_unbounded_waits(self) -> None:
        """Every adapter call that never returns is cut off; the run still ends."""
        for method, kind, sent in (
            ("connect", "connection_failed", False),
            ("runtime_target", "not_simulation", False),
            ("twin_target", "not_simulation", False),
            ("read_telemetry", "telemetry_unavailable", False),
            ("move_joint", "dispatch_failed", True),
        ):
            with self.subTest(method=method):
                self.twin = FakeTwin()
                self.addCleanup(self.twin.release.set)
                self.twin.fail[method] = HANG
                outcome = self.failure(kind)
                self.assertIn("time limit", outcome.detail)
                self.assertLess(self.elapsed, FAST.total_s)
                self.assertEqual(outcome.target_dispatched, sent)
                if sent:
                    # The hung movement call may still have been sent: stop is tried.
                    self.assertEqual(self.twin.names().count("stop"), 1)
                    self.assertEqual(len(self.twin.moves()), 1)
                else:
                    self.assert_nothing_sent(outcome)
                self.twin.release.set()

    def test_a_hung_disconnect_does_not_block(self) -> None:
        self.twin.fail["disconnect"] = HANG
        outcome = self.run_it()
        self.assertIsInstance(outcome, RehearsalSuccess)
        self.assertLess(self.elapsed, FAST.total_s)

    def test_overall_timeout_is_handled_and_recovery_still_fits(self) -> None:
        # DETERMINISTIC (fake clock, fake pause, fake session: no real time, no scheduling).
        # Work window 0.5 s, recovery keeps its own reserve. A call is issued only if its
        # whole limit (0.4 s) fits before the work deadline, so the start read and the
        # dispatch go out (the window is 0.5 s and the fake calls take 0.5 ms each); the
        # run then ends when the polling reads no longer fit. Real-clock version below.
        tight = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + 0.5)
        self.twin.ignore_moves = {1}
        clock = _FakeClock()
        session = _ClockedSession(self.twin, clock, lambda method, now, budget: 0.0005)
        with mock.patch.object(simulation_executor, "time", clock):
            outcome = run_rehearsal(snapshot(), session, tight)
        session.close()
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        self.assertEqual(outcome.kind, "overall_timeout", outcome.detail)
        self.assertLessEqual(len(outcome.detail), 200)
        self.assertLess(clock.now - _FakeClock.START, tight.total_s)  # elapsed, in fake time
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(self.twin.names().count("stop"), 1)
        self.assertEqual(outcome.stop, "confirmed")
        self.assertIsNotNone(outcome.last_telemetry)
        self.assertEqual(len(self.twin.moves()), 1)
        # the dispatch was issued with its whole limit, and the child was never terminated
        dispatch_budgets = [b for m, _at, b in session.calls if m == "dispatch_move"]
        self.assertEqual(len(dispatch_budgets), 1)
        self.assertAlmostEqual(dispatch_budgets[0], tight.dispatch_s, places=9)
        self.assertFalse(session.terminated)

    def test_overall_timeout_after_dispatch_with_a_real_child_and_the_real_clock(self) -> None:
        """INTEGRATION CHECK, NOT DETERMINISTIC (real child process, real clock).

        The window (1.0 s) leaves about 0.6 s of real time for connect, two mode checks, the
        start read and the dispatch (each 0.4 s limit, milliseconds in practice). If this
        machine stalls longer than that, the dispatch is legitimately never issued and
        nothing is proven, so the test SKIPS instead of passing or failing falsely. The
        deterministic proof is the test above."""
        # target_move_s is raised so the work deadline, not the 0.25 s target phase, ends the run
        tight = dataclasses.replace(FAST, target_move_s=5.0, total_s=FAST.recovery_reserve_s + 1.0)
        self.twin.ignore_moves = {1}
        outcome = self.run_it(timeouts=tight, fresh_session_stop="unsupported")
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        self.assertEqual(outcome.kind, "overall_timeout", outcome.detail)
        if not outcome.target_dispatched:
            self.skipTest("the machine stalled: the dispatch was never reached; nothing proven")
        self.assertLess(self.elapsed, tight.total_s)
        self.assertEqual(self.twin.names().count("stop"), 1)
        self.assertEqual(outcome.stop, "confirmed")
        self.assertIsNotNone(outcome.last_telemetry)
        self.assertEqual(len(self.twin.moves()), 1)

    def test_overall_timeout_before_anything_was_sent(self) -> None:
        tight = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + 0.1)
        self.twin.fail["connect"] = HANG
        outcome = self.failure("overall_timeout", timeouts=tight)
        self.assert_nothing_sent(outcome)
        self.assertLess(self.elapsed, tight.total_s)

    def test_a_work_window_of_0_15_s_ends_before_anything_is_sent(self) -> None:
        # The window the test above used to run with. The first call (connect, limit 0.4 s)
        # does not fit in 0.15 s, so it is never issued: the session stays healthy, no
        # movement, no stop, overall_timeout.
        tight = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + 0.15)
        self.twin.ignore_moves = {1}
        outcome = self.failure("overall_timeout", timeouts=tight)
        self.assert_nothing_sent(outcome)
        self.assertEqual(self.twin.moves(), [])
        self.assertNotIn("connect", self.twin.names())
        self.assertLess(self.elapsed, tight.total_s)

    def test_a_successful_run_does_not_wait_for_the_limits(self) -> None:
        self.run_it(timeouts=DEFAULT_TIMEOUTS)
        self.assertLess(self.elapsed, 2.0)


class CustomStop(BaseException):
    """An unexpected BaseException that is neither Ctrl-C nor SystemExit."""


def raise_in_parent_on(name: str, exc: BaseException) -> Any:
    """A recorder that raises ``exc`` IN THE PARENT when the child reports ``name``."""

    def recorder(call: str) -> None:
        if call == name:
            raise exc

    return recorder


class SupervisedInterruptionTests(ExecutorCase):
    """Interruptions with a REAL supervised child (the twin runs in another process)."""

    def interrupted(self, exc: BaseException, at: str, **spec_options: Any) -> RehearsalFailure:
        self.twin.recorder = raise_in_parent_on(at, exc)
        with self.assertRaises(type(exc)) as ctx:
            self.run_it(fresh_session_stop="best_effort", **spec_options)
        self.assertIs(ctx.exception, exc)  # the ORIGINAL exception continues
        outcome = ctx.exception.rehearsal_outcome
        self.assertIsInstance(outcome, RehearsalFailure)
        self.assertLessEqual(len(outcome.detail), 200)
        return outcome

    def test_ctrl_c_before_dispatch(self) -> None:
        outcome = self.interrupted(KeyboardInterrupt(), at="connect")
        self.assertEqual((outcome.kind, outcome.stop), ("interrupted", "not_needed"))
        self.assertFalse(outcome.target_dispatched)
        self.assertFalse(outcome.manual_verification_required)
        self.assertEqual(self.twin.moves(), [])
        self.assertNotIn("stop", self.twin.names())

    def test_ctrl_c_after_the_dispatch_intent_was_acknowledged(self) -> None:
        outcome = self.interrupted(KeyboardInterrupt(), at="move_joint")
        self.assertEqual(outcome.kind, "interrupted")
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(outcome.stop, "unconfirmed")  # only a fresh session was left
        self.assertTrue(outcome.manual_verification_required)
        self.assertEqual(self.twin.names().count("stop"), 1)  # at most once
        self.assertEqual(len(self.twin.moves()), 1)  # never retried

    def test_system_exit_after_dispatch(self) -> None:
        exit_request = SystemExit(7)
        outcome = self.interrupted(exit_request, at="move_joint")
        self.assertEqual(outcome.kind, "interrupted")
        self.assertEqual(exit_request.code, 7)
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(len(self.twin.moves()), 1)

    def test_a_custom_base_exception_after_dispatch_is_an_internal_error(self) -> None:
        outcome = self.interrupted(CustomStop(), at="move_joint")
        self.assertEqual(outcome.kind, "internal_error")
        self.assertIn("CustomStop", outcome.detail)
        self.assertTrue(outcome.manual_verification_required)

    def test_a_hung_move_is_never_retried_and_a_fresh_stop_is_never_confirmed(self) -> None:
        self.twin.fail_move_number[1] = HANG
        self.twin.stop_result = StopResult(confirmed=True)  # what the fresh session's fake says
        outcome = self.failure("dispatch_failed", fresh_session_stop="best_effort")
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(outcome.stop, "unconfirmed")
        self.assertTrue(outcome.manual_verification_required)
        self.assertEqual(len(self.twin.moves()), 1)
        self.assertEqual(self.twin.names().count("stop"), 1)

    def test_without_a_fresh_session_stop_nothing_is_sent_after_termination(self) -> None:
        self.twin.fail_move_number[1] = HANG
        outcome = self.failure("dispatch_failed", fresh_session_stop="unsupported")
        self.assertEqual(outcome.stop, "unconfirmed")
        self.assertTrue(outcome.manual_verification_required)
        self.assertNotIn("stop", self.twin.names())
        self.assertEqual(len(self.twin.moves()), 1)

    def test_a_stop_through_the_live_session_can_be_confirmed(self) -> None:
        self.twin.ignore_moves = {1}  # target never reached; the child stays healthy
        outcome = self.failure("target_timeout", fresh_session_stop="unsupported")
        self.assertEqual(outcome.stop, "confirmed")
        self.assertFalse(outcome.manual_verification_required)
        self.assertEqual(self.twin.names().count("stop"), 1)


class _SliverClock:
    """Stands in for the ``time`` module INSIDE simulation_executor only.

    The final, shortened poll pause of a wait phase (the one that ends at the phase
    deadline) is cut so it returns SLIVER_S before that deadline, as an early wake-up
    does. Every other pause and every clock read is real. No load is needed.
    """

    SLIVER_S = 0.00003  # far shorter than any pipe round trip to a child process

    def __init__(self, poll_interval_s: float) -> None:
        self.poll_interval_s = poll_interval_s
        self.cut_pauses = 0
        self.seen_remaining_s: list[float] = []  # what the executor saw right after each cut pause
        self._last_now = 0.0
        self._expect: float | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def monotonic(self) -> float:
        self._last_now = time.monotonic()
        if self._expect is not None:  # the executor's first clock read after a cut pause
            self.seen_remaining_s.append(self._expect - self._last_now)
            self._expect = None
        return self._last_now

    def sleep(self, seconds: float) -> None:
        if seconds >= self.poll_interval_s * 0.999:
            time.sleep(seconds)
            return
        # The executor computed ``seconds`` as phase_deadline - (its last clock read).
        target = self._last_now + seconds  # exactly the phase deadline
        time.sleep(max(0.0, seconds - 0.010))
        while time.monotonic() < target - self.SLIVER_S:
            pass
        self._expect = target
        self.cut_pauses += 1


class _RecordingSession:
    """Delegates to a real session and records the budget each telemetry read was given."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.read_budgets_s: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def call(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        if method == "read_telemetry":
            self.read_budgets_s.append(deadline - time.monotonic())
        return self._session.call(method, args, deadline)


class RealChildSliverIntegrationTest(ExecutorCase):
    """INTEGRATION CHECK, NOT DETERMINISTIC: the same property with a REAL supervised child.

    The sliver is placed by a real-time spin, so a pre-empted test thread can miss it; the
    test then SKIPS (it never passes falsely). The deterministic proof of the invariant is
    ``DeterministicDeadlineTests`` (fake clock, fake pause, fake session: no real time).
    This one only shows that the real supervisor and a real pipe agree with the fake.
    """

    MIN_USEFUL_READ_S = 0.001  # no read may be issued with less than 1 ms

    def run_with_sliver(self, **timeout_options: Any) -> tuple[Any, _RecordingSession, _SliverClock, bool]:
        session = open_process_session(self, self.twin, fresh_session_stop="unsupported")
        recording = _RecordingSession(session)
        # poll_interval_s 0.2 (its maximum) against a 0.25 s phase: the first pause is a full
        # one and the SECOND is always the shortened one that ends at the phase deadline.
        timeouts = dataclasses.replace(FAST, **{"poll_interval_s": 0.2, **timeout_options})
        clock = _SliverClock(timeouts.poll_interval_s)
        with mock.patch.object(simulation_executor, "time", clock):
            outcome = run_rehearsal(snapshot(), recording, timeouts)
        still_healthy = session.healthy  # read BEFORE close: was the child killed during the run?
        session.close()
        return outcome, recording, clock, still_healthy

    def check(self, kind: str, result: tuple[Any, _RecordingSession, _SliverClock, bool]) -> None:
        outcome, recording, clock, still_healthy = result
        self.assertGreaterEqual(clock.cut_pauses, 1, "the sliver was not set up; the test proves nothing")
        if not (clock.seen_remaining_s and 0 < clock.seen_remaining_s[0] < self.MIN_USEFUL_READ_S):
            # Only the FIRST look after the cut pause is the sliver this test sets up (a later
            # one may be past the deadline, after a full-limit read). If that first look was
            # already past the deadline, the thread was pre-empted: no sliver was presented,
            # and nothing is proven either way.
            self.skipTest(f"the sliver was not presented (executor saw {clock.seen_remaining_s})")
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        # Each property is checked on its own, so a failure shows every one that broke.
        with self.subTest("no read is issued with a sliver of the phase's time"):
            self.assertGreaterEqual(min(recording.read_budgets_s), self.MIN_USEFUL_READ_S)
        with self.subTest("the healthy twin child is not terminated"):
            self.assertTrue(still_healthy)
        with self.subTest("the error kind is the phase timeout"):
            self.assertEqual(outcome.kind, kind, outcome.detail)
        # The same outcome as for an ordinary phase timeout (see SupervisedInterruptionTests).
        with self.subTest("the stop is sent through the live session and confirmed"):
            self.assertEqual(outcome.stop, "confirmed")
            self.assertFalse(outcome.manual_verification_required)
            self.assertEqual(self.twin.names().count("stop"), 1)

    def test_a_target_phase_ending_in_a_sliver_with_a_real_child(self) -> None:
        self.twin.ignore_moves = {1}
        self.check("target_timeout", self.run_with_sliver())


class _FakeClock:
    """A complete fake of ``time`` for simulation_executor: monotonic() and sleep() only.

    ``sleep`` never sleeps: it moves the clock. ``early_wake_s``: every pause returns that
    much EARLY (an early wake-up), so the pause that ends a phase leaves a sliver of it.
    ``land_at``: a pause that would cross that instant stops exactly on it instead.
    Nothing here depends on real time or scheduling.
    """

    START = 1024.0  # a power of two: every figure below is exact in binary floating point

    def __init__(self, early_wake_s: float = 0.0, land_at: float | None = None) -> None:
        self.now = self.START
        self.early_wake_s = early_wake_s
        self.land_at = land_at

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        target = self.now + max(0.0, seconds - self.early_wake_s)
        if self.land_at is not None and self.now < self.land_at <= target:
            target = self.land_at
        self.now = max(self.now, target)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _ClockedSession(InProcessSession):
    """The fake twin session with the SUPERVISOR's timing rules, on a fake clock.

    Every call takes ``latency(method, now, budget)`` of fake time. A call whose budget is
    shorter than that cannot finish: like ``ProcessTwinSession._receive`` it runs to its
    deadline, TERMINATES the child (``terminated``) and raises ``SessionTimeout``. Each
    call is recorded as (method, issued_at, budget).
    """

    def __init__(self, twin: Any, clock: _FakeClock, latency: Any) -> None:
        super().__init__(twin)
        self.clock = clock
        self.latency = latency
        self.terminated = False
        self.calls: list[tuple[str, float, float]] = []
        self.after: Any = None  # called with the method name after each call completes

    @property
    def healthy(self) -> bool:
        return not self.closed and not self.terminated

    def call_with_gates(self, method: str, args: tuple[Any, ...], deadline: float) -> tuple[Any, bool, bool]:
        budget = deadline - self.clock.now
        self.calls.append((method, self.clock.now, budget))
        if self.terminated:
            raise SessionUnusable("the twin session is not usable")
        took = self.latency(method, self.clock.now, budget)
        if budget < took:
            self.clock.now = max(self.clock.now, deadline)
            self.terminated = True
            raise SessionTimeout("the twin request did not finish in time")
        self.clock.now += took
        result = super().call_with_gates(method, args, deadline)
        if self.after is not None:
            self.after(method)
        return result

    def dispatch_move(self, joint: str, value_rad: float, deadline: float, on_acknowledged: Any) -> None:
        """The two-phase handshake of ``ProcessTwinSession.dispatch_move`` on the fake clock.

        PREPARE (one round trip) -> acknowledgement recorded -> GO: the child CALLS
        ``move_joint`` and replies (one more round trip). BOTH round trips share the ONE
        ``deadline``; a missed reply terminates the child (a movement may already have been
        sent by then: ``move_joint`` has run).
        """
        budget = deadline - self.clock.now
        self.calls.append(("dispatch_move", self.clock.now, budget))
        if self.terminated:
            raise SessionUnusable("the twin session is not usable")
        trip = self.latency("dispatch_move", self.clock.now, budget)
        if self.clock.now + trip > deadline:  # no INTENT in time: nothing was sent
            self.clock.now = max(self.clock.now, deadline)
            self.terminated = True
            raise SessionTimeout("the twin request did not finish in time")
        self.clock.now += trip
        if self.moves >= MAX_MOVES:
            raise SessionRefused("movement_limit_reached")
        on_acknowledged()
        self.moves += 1
        try:
            self.twin.move_joint(joint, value_rad)  # GO: the child moves, then replies
        except Exception as exc:
            raise TwinCallFailed(type(exc).__name__) from None
        if self.clock.now + trip > deadline:  # the reply "moved" misses the deadline
            self.clock.now = max(self.clock.now, deadline)
            self.terminated = True
            raise SessionTimeout("the twin request did not finish in time")
        self.clock.now += trip
        if self.after is not None:
            self.after("dispatch_move")

    def reads(self) -> list[tuple[float, float]]:
        """(issued_at, budget) of every telemetry read BEFORE the stop (the rehearsal's own)."""
        out = []
        for method, issued_at, budget in self.calls:
            if method == "stop":
                break
            if method == "read_telemetry":
                out.append((issued_at, budget))
        return out

    def stop_budgets(self) -> list[float]:
        return [budget for method, _at, budget in self.calls if method == "stop"]


class DeterministicDeadlineTests(ExecutorCase):
    """DETERMINISTIC: no real sleeping, no real clock, no scheduling, no child process.

    A fake clock and a fake pause drive the executor to the final-poll boundary on EVERY
    run (nothing here can skip). Invariant: a telemetry read is issued only with its FULL
    read limit, and only if that limit fits before D_work; a phase or work deadline that is
    a sliver away ends the run as an ordinary timeout, with the child alive and the stop
    sent through the live session with its full reserve.
    """

    SLIVER_S = 0.00003
    CALL_S = 0.0005  # fake time every call takes

    def drive(self, timeouts: Timeouts, *, clock: _FakeClock, latency: Any = None) -> tuple[Any, _ClockedSession]:
        latency = latency or (lambda method, now, budget: self.CALL_S)
        session = _ClockedSession(self.twin, clock, latency)
        with mock.patch.object(simulation_executor, "time", clock):
            outcome = run_rehearsal(snapshot(), session, timeouts)
        session.close()
        return outcome, session

    def check(self, kind: str, timeouts: Timeouts, outcome: Any, session: _ClockedSession, clock: _FakeClock) -> None:
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        work_deadline = _FakeClock.START + timeouts.total_s - timeouts.recovery_reserve_s
        reads = session.reads()
        self.assertTrue(reads, "no telemetry read was issued at all")
        with self.subTest("every telemetry read was issued with its full read limit"):
            for _at, budget in reads:
                self.assertAlmostEqual(budget, timeouts.telemetry_read_s, places=9)
        with self.subTest("no read ends after the work deadline (the reserve is never consumed)"):
            for issued_at, budget in reads:
                self.assertLessEqual(issued_at + budget, work_deadline + 1e-9)
        with self.subTest("the child was not terminated"):
            self.assertFalse(session.terminated)
        with self.subTest("the error kind"):
            self.assertEqual(outcome.kind, kind, outcome.detail)
        with self.subTest("the stop is the same as for an ordinary timeout"):
            self.assertEqual(outcome.stop, "confirmed")
            self.assertFalse(outcome.manual_verification_required)
            self.assertEqual(self.twin.names().count("stop"), 1)
            self.assertEqual(len(session.stop_budgets()), 1)  # through the live session
            self.assertAlmostEqual(session.stop_budgets()[0], timeouts.stop_s, places=9)  # full reserve

    def sliver_clock(self) -> _FakeClock:
        return _FakeClock(early_wake_s=self.SLIVER_S)

    def test_a_target_phase_ending_in_a_sliver(self) -> None:
        self.twin.ignore_moves = {1}
        timeouts = dataclasses.replace(FAST, poll_interval_s=0.2)
        clock = self.sliver_clock()
        outcome, session = self.drive(timeouts, clock=clock)
        self.check("target_timeout", timeouts, outcome, session, clock)

    def test_a_return_phase_ending_in_a_sliver(self) -> None:
        self.twin.ignore_moves = {2}
        timeouts = dataclasses.replace(FAST, poll_interval_s=0.2)
        clock = self.sliver_clock()
        outcome, session = self.drive(timeouts, clock=clock)
        self.check("return_timeout", timeouts, outcome, session, clock)

    def test_the_full_read_limit_does_not_fit_before_the_work_deadline(self) -> None:
        # (a) The PHASE is long (1 s), so the WORK deadline ends the wait: the last pause
        # ends a sliver before it. The read is not issued; overall_timeout; the child is
        # alive; the stop goes through the live session and is confirmed.
        self.twin.ignore_moves = {1}
        reserve = FAST.stop_s + 0.05 + FAST.disconnect_s
        timeouts = dataclasses.replace(
            # every call's WHOLE limit must fit before D_work to be started: small limits
            FAST, connect_s=0.05, mode_check_s=0.05, telemetry_read_s=0.05, dispatch_s=0.05,
            target_move_s=1.0, total_s=reserve + 0.30, poll_interval_s=0.2,
        )
        clock = self.sliver_clock()
        outcome, session = self.drive(timeouts, clock=clock)
        self.check("overall_timeout", timeouts, outcome, session, clock)
        work_deadline = _FakeClock.START + timeouts.total_s - timeouts.recovery_reserve_s
        with self.subTest("the read that did not fit was never issued"):
            last_issued_at, _budget = session.reads()[-1]
            self.assertLess(work_deadline - last_issued_at, 0.30)  # reads happened...
            self.assertGreaterEqual(work_deadline - last_issued_at, timeouts.telemetry_read_s - 1e-9)
            self.assertGreater(clock.now, last_issued_at)  # ...and time moved on to the sliver

    def test_a_read_with_exactly_enough_room_completes_and_the_stop_keeps_its_full_reserve(self) -> None:
        # (b) Dyadic figures, so the equality below is exact. The last pause lands EXACTLY
        # read_limit before D_work: the read is issued with its full limit and takes all of
        # it (finishing exactly at D_work); the stop must still get its whole stop_s.
        self.twin.ignore_moves = {1}
        timeouts = dataclasses.replace(
            FAST, connect_s=0.25, mode_check_s=0.25, telemetry_read_s=0.0625, dispatch_s=0.25,
            target_move_s=1.0, target_verify_s=0.25, return_move_s=0.25, restore_verify_s=0.25,
            stop_s=0.25, disconnect_s=0.25, total_s=0.5625 + 0.5, poll_interval_s=0.125,
        )
        work_deadline = _FakeClock.START + timeouts.total_s - timeouts.recovery_reserve_s
        boundary = work_deadline - timeouts.telemetry_read_s
        clock = _FakeClock(land_at=boundary)

        def latency(method: str, now: float, budget: float) -> float:
            return budget if (method == "read_telemetry" and now == boundary) else 0.001953125

        outcome, session = self.drive(timeouts, clock=clock, latency=latency)
        self.check("overall_timeout", timeouts, outcome, session, clock)
        with self.subTest("the boundary read WAS issued, with exactly enough room, and completed"):
            self.assertIn((boundary, timeouts.telemetry_read_s), session.reads())
        with self.subTest("it ended exactly at the work deadline and the stop still had stop_s"):
            self.assertEqual(session.stop_budgets(), [timeouts.stop_s])


class DeterministicCallInvariantTests(ExecutorCase):
    """DETERMINISTIC (fake clock, fake session; never skips): every call capped by the work
    deadline is issued only with its WHOLE limit, and the recovery read likewise.

    The work window is squeezed so that exactly one call finds only a sliver of it left. That
    call must not be started: nothing is sent, the run ends as overall_timeout and the child is
    alive. The dispatch is the one that matters: no acknowledgement may be recorded as "possibly
    sent", and no movement may reach the twin.
    """

    CALL_S = 0.0005
    SLIVER_S = 0.00003
    ROUND_TRIPS = ("connect", "runtime_target", "twin_target", "read_telemetry", "dispatch_move")

    def limits(self, timeouts: Timeouts) -> dict[str, float]:
        return {
            "connect": timeouts.connect_s, "runtime_target": timeouts.mode_check_s,
            "twin_target": timeouts.mode_check_s, "read_telemetry": timeouts.telemetry_read_s,
            "dispatch_move": timeouts.dispatch_s,
        }

    def squeezed(self, victim: str, leave_s: float) -> tuple[Timeouts, _FakeClock, Any]:
        """Timeouts and a latency so that, when ``victim`` is about to be issued, ``leave_s`` of
        the work window is left. The call BEFORE it is made to end exactly then (it was issued
        with its full limit and fits, so it is started)."""
        index = self.ROUND_TRIPS.index(victim)
        if index == 0:  # no earlier call: the window itself is the sliver
            timeouts = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + leave_s)
            return timeouts, _FakeClock(), lambda method, now, budget: self.CALL_S
        previous = self.ROUND_TRIPS[index - 1]
        base = dataclasses.replace(FAST)
        # the previous call must find its whole limit: window = earlier calls + its limit (+ 1 us)
        window = (index - 1) * self.CALL_S + self.limits(base)[previous] + 1e-6
        timeouts = dataclasses.replace(base, total_s=base.recovery_reserve_s + window)
        work_deadline = _FakeClock.START + window

        def latency(method: str, now: float, budget: float) -> float:
            if method == previous:
                took = (work_deadline - leave_s) - now
                assert 0 < took <= budget, "the test set-up does not fit"
                return took
            return self.CALL_S

        return timeouts, _FakeClock(), latency

    def drive(self, timeouts: Timeouts, clock: _FakeClock, latency: Any,
              after: Any = None) -> tuple[Any, _ClockedSession]:
        session = _ClockedSession(self.twin, clock, latency)
        session.after = after
        with mock.patch.object(simulation_executor, "time", clock):
            outcome = run_rehearsal(snapshot(), session, timeouts)
        session.close()
        return outcome, session

    def check_not_started(self, victim: str, timeouts: Timeouts, outcome: Any, session: _ClockedSession) -> None:
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        limits = self.limits(timeouts)
        with self.subTest(f"{victim}: it was never issued"):
            self.assertNotIn(victim, [method for method, _at, _b in session.calls])
        with self.subTest(f"{victim}: every call that WAS issued had its whole limit"):
            for method, _at, budget in session.calls:
                if method in limits:  # (the cleanup disconnect has its own budget)
                    self.assertAlmostEqual(budget, limits[method], places=9, msg=method)
        with self.subTest(f"{victim}: overall_timeout, the child healthy"):
            self.assertEqual(outcome.kind, "overall_timeout", outcome.detail)
            self.assertFalse(session.terminated)
        with self.subTest(f"{victim}: nothing was sent and nothing recorded as possibly sent"):
            self.assertEqual(self.twin.moves(), [])
            self.assertNotIn("move_joint", self.twin.names())
            self.assertFalse(outcome.target_dispatched)
            self.assertFalse(outcome.return_dispatched)
            self.assertEqual(outcome.stop, "not_needed")
            self.assertNotIn("stop", self.twin.names())

    def test_connect_a_sliver_from_the_work_deadline_is_not_started(self) -> None:
        timeouts, clock, latency = self.squeezed("connect", self.SLIVER_S)
        outcome, session = self.drive(timeouts, clock, latency)
        self.check_not_started("connect", timeouts, outcome, session)
        # only the cleanup disconnect reached the session: no connect, no check, no read, no move
        self.assertEqual([method for method, _at, _b in session.calls], ["disconnect"])

    def test_the_mode_checks_and_the_start_read_a_sliver_from_the_work_deadline_are_not_started(self) -> None:
        for victim in ("runtime_target", "twin_target", "read_telemetry"):
            with self.subTest(victim=victim):
                self.twin = FakeTwin()
                self.addCleanup(self.twin.release.set)
                timeouts, clock, latency = self.squeezed(victim, self.SLIVER_S)
                outcome, session = self.drive(timeouts, clock, latency)
                self.check_not_started(victim, timeouts, outcome, session)

    def test_the_dispatch_a_sliver_from_the_work_deadline_sends_nothing(self) -> None:
        # Less than one round trip is left: the PREPARE could not even be answered.
        for leave_s in (self.SLIVER_S, 1.5 * self.CALL_S, 3.5 * self.CALL_S):
            with self.subTest(left_for_the_dispatch_s=leave_s):
                self.twin = FakeTwin()
                self.addCleanup(self.twin.release.set)
                self.twin.ignore_moves = {1}
                timeouts, clock, latency = self.squeezed("dispatch_move", leave_s)
                outcome, session = self.drive(timeouts, clock, latency)
                self.check_not_started("dispatch_move", timeouts, outcome, session)

    def test_the_recovery_read_is_not_started_when_its_whole_limit_does_not_fit(self) -> None:
        # The process stalls (fake clock jump) right after the movement was sent, so that
        # recovery starts a sliver before the end of the recovery-read window. No stop can be
        # attempted (its deadline has passed) and the read must not be started either.
        self.twin.ignore_moves = {1}
        timeouts = FAST
        window_end = _FakeClock.START + timeouts.total_s + 1.0 - timeouts.disconnect_s  # D_total + TERMINATE - disc
        clock = _FakeClock()

        def stall(method: str) -> None:
            if method == "dispatch_move":
                clock.now = window_end - self.SLIVER_S

        outcome, session = self.drive(timeouts, clock, lambda method, now, budget: self.CALL_S, after=stall)
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        stalled_at = window_end - self.SLIVER_S
        with self.subTest("the movement was sent; no stop could be attempted"):
            self.assertTrue(outcome.target_dispatched)
            self.assertEqual(outcome.kind, "overall_timeout")
            self.assertEqual(outcome.stop, "unconfirmed")
            self.assertTrue(outcome.manual_verification_required)
            self.assertNotIn("stop", self.twin.names())
        with self.subTest("no read was issued after the stall"):
            late_reads = [m for m, at, _b in session.calls if m == "read_telemetry" and at >= stalled_at]
            self.assertEqual(late_reads, [])
            self.assertIsNone(outcome.last_telemetry)
        with self.subTest("the child was not terminated and the disconnect still ran with its whole limit"):
            self.assertFalse(session.terminated)
            disconnects = [b for m, _at, b in session.calls if m == "disconnect"]
            self.assertEqual(len(disconnects), 1)
            self.assertAlmostEqual(disconnects[0], timeouts.disconnect_s, places=9)


class _SkewClock:
    """``time`` for simulation_executor: the REAL monotonic clock plus a skew the test can move.

    Used with a REAL supervised child, whose own deadlines run on the real clock: until the
    test jumps the skew, executor and supervisor agree on the time."""

    def __init__(self) -> None:
        self.skew = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.skew

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _JumpAfterStartRead:
    """Wraps a REAL ``ProcessTwinSession``: after the start telemetry read it moves the clock so that
    exactly ``leave_s`` of the work window is left, and records every message that is written
    to the child's pipe (``_send``: PREPARE, GO, call, ...)."""

    def __init__(self, session: Any, clock: _SkewClock, rehearsals: list[Any], leave_s: float) -> None:
        self._session = session
        self._clock = clock
        self._rehearsals = rehearsals
        self._leave_s = leave_s
        self.sent: list[str] = []  # message kinds written to the child, in order
        self.jumped = False
        original = session._send

        def recording_send(child: Any, message: tuple[Any, ...]) -> None:
            self.sent.append(message[0] if message[0] != "call" else f"call:{message[2]}")
            original(child, message)

        session._send = recording_send

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def _maybe_jump(self, method: str) -> None:
        if method == "read_telemetry" and not self.jumped:
            self.jumped = True
            work_deadline = self._rehearsals[0].work_deadline
            self._clock.skew += (work_deadline - self._leave_s) - self._clock.monotonic()

    def call(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        value = self._session.call(method, args, deadline)
        self._maybe_jump(method)
        return value

    def call_with_gates(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        value = self._session.call_with_gates(method, args, deadline)
        self._maybe_jump(method)
        return value


class DispatchBudgetWithRealChildTests(ExecutorCase):
    """The PREPARE/ack/GO sequence against a REAL supervised child process (fake twin inside).

    The budget check runs before ``session.dispatch_move``, whose first act is to write PREPARE
    to the child's pipe. So a refused dispatch must leave NO ``prepare`` (and no ``go``) message
    on that pipe. A control run proves the recorder sees both when the dispatch is allowed."""

    WINDOW_S = 2.0  # work window; generous so the earlier calls always fit on a loaded machine

    def run_with_leave(self, leave_s: float) -> tuple[Any, _JumpAfterStartRead]:
        self.twin = FakeTwin()
        self.addCleanup(self.twin.release.set)
        self.twin.ignore_moves = {1}
        timeouts = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + self.WINDOW_S)
        clock = _SkewClock()
        rehearsals: list[Any] = []

        class Spy(simulation_executor._Rehearsal):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                rehearsals.append(self)

        real = open_process_session(self, self.twin)
        session = _JumpAfterStartRead(real, clock, rehearsals, leave_s)
        with mock.patch.object(simulation_executor, "time", clock), \
                mock.patch.object(simulation_executor, "_Rehearsal", Spy):
            outcome = run_rehearsal(snapshot(), session, timeouts)
        real.close()
        self.assertTrue(session.jumped, "the start read never completed: the test set-up is wrong")
        return outcome, session

    def test_a_refused_dispatch_writes_no_prepare_to_the_child(self) -> None:
        # Less than the dispatch limit (0.4 s) is left; 30, 750 and 1750 microseconds.
        for leave_s in (0.00003, 0.00075, 0.00175):
            with self.subTest(left_us=round(leave_s * 1e6)):
                outcome, session = self.run_with_leave(leave_s)
                self.assertIsInstance(outcome, RehearsalFailure, outcome)
                self.assertEqual(outcome.kind, "overall_timeout", outcome.detail)
                self.assertIn("call:read_telemetry", session.sent)  # the recorder works
                self.assertNotIn("prepare", session.sent)
                self.assertNotIn("go", session.sent)
                self.assertEqual(self.twin.moves(), [])
                self.assertNotIn("move_joint", self.twin.names())
                self.assertFalse(outcome.target_dispatched)
                self.assertEqual(outcome.stop, "not_needed")
                self.assertNotIn("stop", self.twin.names())

    def test_control_an_allowed_dispatch_writes_prepare_then_go(self) -> None:
        outcome, session = self.run_with_leave(1.0)  # well over dispatch_s: the dispatch is issued
        self.assertIn("prepare", session.sent)
        self.assertEqual(session.sent[session.sent.index("prepare") + 1], "go")
        self.assertTrue(outcome.target_dispatched)
        self.assertEqual(len(self.twin.moves()), 1)


class ReserveDerivationTests(unittest.TestCase):
    """The recovery reserve is stop_s + telemetry_read_s + disconnect_s, never a fixed number."""

    def test_the_reserve_is_the_sum_of_the_three_settings(self) -> None:
        for stop_s, read_s, disc_s in ((5.0, 3.0, 3.0), (0.3, 0.4, 0.3), (1.25, 0.5, 2.0), (4.0, 1.0, 0.5)):
            with self.subTest(stop_s=stop_s, telemetry_read_s=read_s, disconnect_s=disc_s):
                timeouts = Timeouts(stop_s=stop_s, telemetry_read_s=read_s, disconnect_s=disc_s)
                self.assertEqual(timeouts.recovery_reserve_s, stop_s + read_s + disc_s)

    def test_each_setting_moves_the_reserve_and_the_work_deadline_by_exactly_its_own_change(self) -> None:
        base = Timeouts(stop_s=4.0, telemetry_read_s=2.0, disconnect_s=2.0, total_s=40.0)
        for name in ("stop_s", "telemetry_read_s", "disconnect_s"):
            with self.subTest(changed=name):
                smaller = dataclasses.replace(base, **{name: getattr(base, name) - 1.0})
                self.assertEqual(base.recovery_reserve_s - smaller.recovery_reserve_s, 1.0)
                # D_work = D_total - reserve: a 1 s smaller reserve gives a 1 s later D_work
                with mock.patch.object(simulation_executor, "time", _FakeClock()):
                    a = simulation_executor._Rehearsal(snapshot(), object(), base)
                    b = simulation_executor._Rehearsal(snapshot(), object(), smaller)
                self.assertEqual(a.deadline, b.deadline)
                self.assertEqual(a.work_deadline, a.deadline - base.recovery_reserve_s)
                self.assertEqual(b.work_deadline - a.work_deadline, 1.0)

    def test_the_production_work_deadline_is_total_minus_eleven_seconds(self) -> None:
        with mock.patch.object(simulation_executor, "time", _FakeClock()):
            rehearsal = simulation_executor._Rehearsal(snapshot(), object(), DEFAULT_TIMEOUTS)
        self.assertEqual(rehearsal.deadline - _FakeClock.START, 75.0)
        self.assertEqual(rehearsal.work_deadline - _FakeClock.START, 75.0 - (5.0 + 3.0 + 3.0))


class StubSession:
    """A session whose every call raises a chosen supervisor error (no process)."""

    fresh_session_stop = "unsupported"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.healthy = True
        self.calls: list[str] = []

    def gate_blockers(self) -> tuple[str, ...]:
        return ()

    def arm(self, until: float) -> None:
        pass

    def call(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        self.calls.append(method)
        self.healthy = False  # as after a terminated child
        raise self.error

    def call_with_gates(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        return self.call(method, args, deadline)

    def dispatch_move(self, *args: Any) -> None:
        raise AssertionError("no movement expected")

    def standby_stop(self, deadline: float) -> Any:
        raise AssertionError("no stop expected")

    def close(self, limit_s: float = 0.0) -> None:
        pass


class RefusedDispatchWordingTests(ExecutorCase):
    """N4: the failure text must not say "before it was sent" once GO was in play."""

    def refused(self, *, after_ack: bool) -> RehearsalFailure:
        from simulation_supervisor import SessionRefused

        class RefusingSession(InProcessSession):
            def dispatch_move(self, joint: str, value_rad: float, deadline: float, on_acknowledged: Any) -> None:
                if after_ack:
                    on_acknowledged()  # the parent recorded "may have been sent", then GO was refused
                raise SessionRefused("no_pending_intent")

        outcome = run_rehearsal(snapshot(), RefusingSession(self.twin), FAST)
        self.assertIsInstance(outcome, RehearsalFailure, outcome)
        self.assertEqual(outcome.kind, "dispatch_failed", outcome.detail)
        self.assertEqual(self.twin.moves(), [])  # this stub never reaches move_joint
        return outcome

    def test_a_refusal_before_the_acknowledgement_says_nothing_was_sent(self) -> None:
        outcome = self.refused(after_ack=False)
        self.assertIn("before any movement was sent", outcome.detail)
        self.assertFalse(outcome.target_dispatched)

    def test_a_refusal_after_the_acknowledgement_does_not_claim_it_was_not_sent(self) -> None:
        outcome = self.refused(after_ack=True)
        self.assertNotIn("before it was sent", outcome.detail)
        self.assertNotIn("before any movement was sent", outcome.detail)
        self.assertIn("treated as sent", outcome.detail)
        self.assertTrue(outcome.target_dispatched)  # still recorded as possibly sent


class DeadlineAndClassificationTests(ExecutorCase):
    def test_the_disconnect_is_not_started_after_its_deadline(self) -> None:
        rehearsal = simulation_executor._Rehearsal(snapshot(), InProcessSession(self.twin), FAST)
        rehearsal.connect_attempted = True
        rehearsal.deadline = time.monotonic() - simulation_executor.TERMINATE_LIMIT_S - 0.01
        rehearsal._disconnect()
        self.assertNotIn("disconnect", self.twin.names())

    def test_a_disconnect_inside_its_deadline_still_runs(self) -> None:
        rehearsal = simulation_executor._Rehearsal(snapshot(), InProcessSession(self.twin), FAST)
        rehearsal.connect_attempted = True
        rehearsal._disconnect()
        self.assertEqual(self.twin.names(), ["disconnect"])

    def test_a_termination_failure_never_replaces_the_failure_kind(self) -> None:
        from simulation_supervisor import SessionCrashed, SessionTimeout

        tight = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + 0.1)
        for name, error, timeouts, kind in (
            ("timeout capped by the work deadline", SessionTimeout("x"), tight, "overall_timeout"),
            ("timeout of the call's own limit", SessionTimeout("x"), FAST, "connection_failed"),
            ("crash", SessionCrashed("x"), FAST, "connection_failed"),
        ):
            with self.subTest(case=name):
                # The supervisor recorded "could not be confirmed stopped" as
                # diagnostic information; the failure kind is still the primary one.
                outcome = run_rehearsal(snapshot(), StubSession(error), timeouts)
                self.assertIsInstance(outcome, RehearsalFailure)
                self.assertEqual(outcome.kind, kind, outcome.detail)
                self.assertEqual(outcome.stop, "not_needed")


class ThreadIsolationTests(unittest.TestCase):
    def test_the_executor_and_supervisor_start_no_thread(self) -> None:
        for module in ("simulation_executor", "simulation_supervisor"):
            source = (Path(__file__).resolve().parents[1] / f"{module}.py").read_text("utf-8")
            with self.subTest(module=module):
                self.assertNotIn("threading", source)
                self.assertNotIn("Thread(", source)


if __name__ == "__main__":
    unittest.main()
