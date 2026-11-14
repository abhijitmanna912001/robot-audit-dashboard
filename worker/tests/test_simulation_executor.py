"""The bounded movement sequence (simulation_executor.py), with a fake twin only.

Offline: no Cyberwave, no simulation session, no network, no database. Every
"movement" below is a method call on ``tests.fake_twin.FakeTwin``, which only
changes numbers in memory. The network guard is active.
"""

from __future__ import annotations

import dataclasses
import inspect
import time
import unittest
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
from tests.fake_twin import ALL_CONFIRMED, FAST, HANG, START, FakeTwin
from tests.test_simulation_gates import action, snapshot

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


class ExecutorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.twin = FakeTwin()
        self.addCleanup(self.twin.release.set)  # let any hung fake call finish

    def run_it(self, snap: Any = DEFAULT_PLAN, timeouts: Timeouts = FAST) -> Any:
        started = time.monotonic()
        outcome = run_rehearsal(snapshot() if snap is DEFAULT_PLAN else snap, self.twin, timeouts)
        self.elapsed = time.monotonic() - started
        return outcome

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
        self.assertEqual(list(inspect.signature(run_rehearsal).parameters), ["snapshot", "twin", "timeouts"])


class GateTests(ExecutorCase):
    def test_no_movement_when_any_gate_is_unconfirmed(self) -> None:
        names = [f.name for f in dataclasses.fields(LiveSimulationGates)]
        for missing in names:
            with self.subTest(missing=missing):
                self.twin = FakeTwin()
                self.twin.confirmations = LiveSimulationGates(**{n: n != missing for n in names})
                outcome = self.failure("gate_unconfirmed")
                self.assertEqual(self.twin.calls, [])
                self.assert_nothing_sent(outcome)

    def test_no_movement_when_the_cancellation_method_is_unconfirmed(self) -> None:
        self.twin.confirmations = dataclasses.replace(ALL_CONFIRMED, stop_method_confirmed=False)
        outcome = self.failure("gate_unconfirmed")
        self.assertIn("stop/cancel", outcome.detail)
        self.assertEqual(self.twin.calls, [])

    def test_no_movement_with_the_real_cyberwave_gates(self) -> None:
        from simulation_gates import CYBERWAVE_GATES

        self.twin.confirmations = CYBERWAVE_GATES
        self.failure("gate_unconfirmed")
        self.assertEqual(self.twin.calls, [])

    def test_missing_gates_block(self) -> None:
        for value in (None, True, "confirmed"):
            with self.subTest(value=value):
                self.twin = FakeTwin()
                self.twin.confirmations = value
                self.failure("gate_unconfirmed")
                self.assertEqual(self.twin.calls, [])


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
            "no source": lambda t: setattr(t, "source", None),
            "live source": lambda t: setattr(t, "source", "live"),
            "commanded": lambda t: setattr(t, "kind", "commanded"),
            "placeholder": lambda t: setattr(t, "kind", "placeholder"),
            "kind unknown": lambda t: setattr(t, "kind", None),
            "no update": lambda t: setattr(t, "update_received", False),
            "stale": lambda t: setattr(t, "age_s", 30.0),
            "no timestamp": lambda t: setattr(t, "omit_timestamp", True),
            "naive timestamp": lambda t: setattr(t, "naive_timestamp", True),
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
        # The joint did not move; the twin echoes the commanded value instead.
        self.twin.ignore_moves = {1}
        self.twin.read_overrides[2] = lambda positions: self.twin.reading(
            {**positions, "_1": 0.25}, kind="commanded"
        )
        outcome = self.failure("telemetry_invalid")
        self.assertIn("commanded_not_measured", outcome.detail)
        self.assert_stopped_once(outcome, moves=1)

    def test_telemetry_losing_its_provenance_after_dispatch_is_a_failure(self) -> None:
        self.twin.read_overrides[2] = lambda positions: self.twin.reading(source=None)
        outcome = self.failure("telemetry_invalid")
        self.assert_stopped_once(outcome, moves=1)

    def test_a_reading_from_before_the_movement_is_not_evidence(self) -> None:
        # An old-but-valid reading already showing 0.25 must not end the wait.
        from datetime import datetime, timedelta, timezone

        def before_dispatch(positions: dict[str, float]) -> Any:
            return self.twin.reading(
                {**positions, "_1": 0.25},
                observed_at=datetime.now(timezone.utc) - timedelta(seconds=1.0),
            )

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
        self.twin.read_overrides[2] = lambda positions: self.twin.reading(source=None)
        bad = self.failure("dispatch_failed")
        self.assertIsNone(bad.last_telemetry)  # the earlier start reading is not reused

        self.twin = FakeTwin()
        self.twin.fail_move_number[1] = RuntimeError("x")
        self.twin.fail_read_number[2] = RuntimeError("read failed")
        self.assertIsNone(self.failure("dispatch_failed").last_telemetry)

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
        # Movement phases get 0.15 s in total; recovery keeps its own reserve.
        tight = dataclasses.replace(FAST, total_s=FAST.recovery_reserve_s + 0.15)
        self.twin.ignore_moves = {1}
        outcome = self.failure("overall_timeout", timeouts=tight)
        self.assertLess(self.elapsed, tight.total_s)
        self.assertTrue(outcome.target_dispatched)
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

    def test_a_successful_run_does_not_wait_for_the_limits(self) -> None:
        self.run_it(timeouts=DEFAULT_TIMEOUTS)
        self.assertLess(self.elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
