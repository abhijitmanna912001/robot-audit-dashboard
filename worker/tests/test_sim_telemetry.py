"""Telemetry validation (sim_telemetry.py) and the read-only script. Offline.

No Cyberwave connection is made: readings come from literals and from a fake
robot. The network guard is active.
"""

from __future__ import annotations

import ast
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import read_joint_telemetry
import sim_telemetry
from cyberwave_client import JointReading, RobotConnectionError
from sim_telemetry import (
    JointSnapshot,
    RawTelemetry,
    TelemetryRejected,
    joints_outside_tolerance,
    validate_telemetry,
)
from simulation_payloads import validate_snapshot

WORKER_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 14, 8, 30, 1, 500000, tzinfo=timezone.utc)
POSITIONS = {"_1": 0.1, "_2": -0.2, "_3": 0.3, "_4": 0.0, "_5": 0.5, "_6": 0.05}
ZEROS = {name: 0.0 for name in POSITIONS}


def raw(**overrides: Any) -> RawTelemetry:
    values: dict[str, Any] = {
        "positions": dict(POSITIONS),
        "source": "simulation",
        "kind": "measured",
        "observed_at": NOW - timedelta(milliseconds=200),
        "update_received": True,
    }
    values.update(overrides)
    return RawTelemetry(**values)


class TelemetryCase(unittest.TestCase):
    def reason(self, reading: Any, **kwargs: Any) -> str:
        with self.assertRaises(TelemetryRejected) as ctx:
            validate_telemetry(reading, now=NOW, **kwargs)
        return ctx.exception.reason


class ValidReadingTests(TelemetryCase):
    def test_a_fully_proven_reading_is_accepted(self) -> None:
        snapshot = validate_telemetry(raw(), now=NOW)
        self.assertIsInstance(snapshot, JointSnapshot)
        self.assertEqual(dict(snapshot.joints), POSITIONS)
        self.assertEqual(snapshot.read_at, NOW - timedelta(milliseconds=200))
        payload = snapshot.to_payload()
        self.assertEqual(payload["readAt"], "2026-11-14T08:30:01Z")
        validate_snapshot(payload)

    def test_the_snapshot_cannot_be_changed(self) -> None:
        snapshot = validate_telemetry(raw(), now=NOW)
        with self.assertRaises(TypeError):
            snapshot.joints["_1"] = 9.9  # type: ignore[index]
        with self.assertRaises(AttributeError):
            snapshot.read_at = NOW  # type: ignore[misc]

    def test_a_non_utc_aware_time_is_converted_to_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        snapshot = validate_telemetry(raw(observed_at=NOW.astimezone(ist)), now=NOW)
        self.assertEqual(snapshot.read_at.utcoffset(), timedelta(0))
        self.assertEqual(snapshot.to_payload()["readAt"], "2026-11-14T08:30:01Z")


class JointSetTests(TelemetryCase):
    def test_exactly_six_joints_are_required(self) -> None:
        five = {k: v for k, v in POSITIONS.items() if k != "_6"}
        cases = {
            "five joints": five,
            "seven joints": {**POSITIONS, "_7": 0.0},
            "renamed joint": {**five, "gripper": 0.0},
            "empty": {},
        }
        for name, positions in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(raw(positions=positions)), "joint_set_mismatch")
        for name, positions in {"none": None, "list": [0.1] * 6, "string": "0.1"}.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(raw(positions=positions)), "positions_missing")

    def test_values_must_be_finite_numbers(self) -> None:
        for name, value, expected in (
            ("nan", float("nan"), "non_finite_value"),
            ("infinity", float("inf"), "non_finite_value"),
            ("negative infinity", float("-inf"), "non_finite_value"),
            ("huge int", 10**400, "non_finite_value"),
            ("boolean", True, "non_numeric_value"),
            ("numeric string", "0.1", "non_numeric_value"),
            ("none", None, "non_numeric_value"),
        ):
            with self.subTest(case=name):
                self.assertEqual(self.reason(raw(positions={**POSITIONS, "_3": value})), expected)

    def test_something_that_is_not_a_reading_is_refused(self) -> None:
        for value in (None, dict(POSITIONS), "ok", 200, True):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(self.reason(value), "not_telemetry")


class ProvenanceTests(TelemetryCase):
    def test_a_bare_sdk_style_reading_is_never_accepted(self) -> None:
        # What get_joints() gives today: positions only.
        self.assertEqual(self.reason(RawTelemetry(positions=dict(POSITIONS))), "source_unverified")

    def test_real_simulation_source_is_required(self) -> None:
        self.assertEqual(self.reason(raw(source=None)), "source_unverified")
        for source in ("live", "edge", "sim", "Simulation", "simulation ", "", True, 1):
            with self.subTest(source=source):
                self.assertEqual(self.reason(raw(source=source)), "not_simulation_source")

    def test_commanded_targets_are_not_telemetry(self) -> None:
        self.assertEqual(self.reason(raw(kind="commanded")), "commanded_not_measured")

    def test_placeholders_and_unknown_kinds_are_refused(self) -> None:
        self.assertEqual(self.reason(raw(kind="placeholder")), "placeholder")
        for kind in (None, "default", "edited", "Measured", "", True):
            with self.subTest(kind=kind):
                self.assertEqual(self.reason(raw(kind=kind)), "kind_unverified")

    def test_a_reading_without_a_received_update_is_refused(self) -> None:
        for value in (None, False, "yes", 1):
            with self.subTest(value=value):
                self.assertEqual(self.reason(raw(update_received=value)), "no_update_received")


class PlaceholderTests(TelemetryCase):
    def test_zero_filled_reading_is_not_automatically_valid(self) -> None:
        for name, reading in {
            "sdk zero fill": RawTelemetry(positions=dict(ZEROS)),
            "no update": raw(positions=dict(ZEROS), update_received=False),
            "no source": raw(positions=dict(ZEROS), source=None),
            "not measured": raw(positions=dict(ZEROS), kind=None),
            "commanded zeros": raw(positions=dict(ZEROS), kind="commanded"),
            "integer zeros": raw(positions={k: 0 for k in ZEROS}, update_received=None),
            "negative zeros": raw(positions={k: -0.0 for k in ZEROS}, source="live"),
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(reading), "placeholder_suspected")

    def test_zeros_are_believed_only_with_full_proof(self) -> None:
        snapshot = validate_telemetry(raw(positions=dict(ZEROS)), now=NOW)
        self.assertEqual(dict(snapshot.joints), ZEROS)

    def test_a_successful_read_is_not_proof(self) -> None:
        # The call "worked" and returned six finite numbers. Still refused.
        reading = RawTelemetry(positions=dict(POSITIONS), update_received=True)
        self.assertEqual(self.reason(reading), "source_unverified")


class TimestampTests(TelemetryCase):
    def test_missing_timestamp_is_refused(self) -> None:
        self.assertEqual(self.reason(raw(observed_at=None)), "timestamp_missing")

    def test_invalid_timestamps_are_refused(self) -> None:
        for name, value in {
            "naive datetime": NOW.replace(tzinfo=None),
            "string": "2026-11-14T08:30:01Z",
            "epoch seconds": 1763109001.0,
            "boolean": True,
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(raw(observed_at=value)), "timestamp_invalid")

    def test_stale_readings_are_refused(self) -> None:
        self.assertEqual(self.reason(raw(observed_at=NOW - timedelta(seconds=2.5))), "stale")
        self.assertEqual(self.reason(raw(observed_at=NOW - timedelta(hours=1))), "stale")
        validate_telemetry(raw(observed_at=NOW - timedelta(seconds=1.9)), now=NOW)

    def test_future_readings_are_refused(self) -> None:
        self.assertEqual(self.reason(raw(observed_at=NOW + timedelta(seconds=5))), "timestamp_in_future")
        validate_telemetry(raw(observed_at=NOW + timedelta(milliseconds=100)), now=NOW)

    def test_a_reading_from_before_the_movement_is_refused(self) -> None:
        sent_at = NOW - timedelta(milliseconds=100)
        self.assertEqual(self.reason(raw(), not_before=sent_at), "stale_before_dispatch")
        validate_telemetry(raw(observed_at=NOW), now=NOW, not_before=sent_at)

    def test_the_age_limit_can_only_be_tightened(self) -> None:
        self.assertEqual(self.reason(raw(), max_age_s=0.1), "stale")
        for bad in (2.5, 0, -1, float("inf"), float("nan"), True, "2"):
            with self.subTest(max_age_s=bad):
                with self.assertRaises(ValueError):
                    validate_telemetry(raw(), now=NOW, max_age_s=bad)
        self.assertEqual(sim_telemetry.MAX_TELEMETRY_AGE_S, 2.0)

    def test_now_must_be_timezone_aware(self) -> None:
        with self.assertRaises(ValueError):
            validate_telemetry(raw(), now=NOW.replace(tzinfo=None))


class ToleranceTests(unittest.TestCase):
    def snapshot(self, **joints: float) -> JointSnapshot:
        return validate_telemetry(raw(positions={**POSITIONS, **joints}), now=NOW)

    def test_target_joint_within_tolerance(self) -> None:
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.255), {"_1": 0.25}), ())
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.245), {"_1": 0.25}), ())
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.27), {"_1": 0.25}), ("_1",))
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.1), {"_1": 0.25}), ("_1",))

    def test_other_five_joints_remain_within_tolerance_at_target(self) -> None:
        others = {k: v for k, v in POSITIONS.items() if k != "_1"}
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.25, _3=0.305), others), ())
        self.assertEqual(
            joints_outside_tolerance(self.snapshot(_1=0.25, _3=0.33, _6=0.0), others), ("_3", "_6")
        )

    def test_all_six_joints_restored_within_tolerance(self) -> None:
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.104), POSITIONS), ())
        self.assertEqual(joints_outside_tolerance(self.snapshot(_1=0.25), POSITIONS), ("_1",))
        self.assertEqual(joints_outside_tolerance(self.snapshot(_5=0.52), POSITIONS), ("_5",))

    def test_the_tolerance_is_not_a_parameter(self) -> None:
        import inspect

        self.assertEqual(
            list(inspect.signature(joints_outside_tolerance).parameters), ["snapshot", "expected"]
        )


# --------------------------------------------------------------------------
# The read-only script, with a fake robot
# --------------------------------------------------------------------------


class FakeReadOnlyRobot:
    """Only the read-only surface. No movement method exists on it."""

    def __init__(self, reading: Any = None, error: BaseException | None = None) -> None:
        self.reading = reading
        self.error = error
        self.calls: list[str] = []
        self.runtime_mode = "simulation"

    def connect(self) -> None:
        self.calls.append("connect")
        if self.error is not None:
            raise self.error

    def load_existing_twin(self) -> None:
        self.calls.append("load_existing_twin")

    def read_joint_positions(self, timeout_s: float = 3.0) -> Any:
        self.calls.append("read_joint_positions")
        return self.reading

    def disconnect(self) -> list[str]:
        self.calls.append("disconnect")
        return []


class ReadOnlyScriptTests(unittest.TestCase):
    def test_zero_fill_from_the_sdk_is_reported_as_unverified(self) -> None:
        robot = FakeReadOnlyRobot(JointReading(positions=dict(ZEROS), telemetry_received=False))
        report = read_joint_telemetry.read_once(robot)
        self.assertIs(report["verified"], False)
        self.assertEqual(report["rejected"], "placeholder_suspected")
        self.assertIs(report["movementSent"], False)
        self.assertNotIn("telemetry", report)
        self.assertEqual(
            robot.calls, ["connect", "load_existing_twin", "read_joint_positions", "disconnect"]
        )

    def test_a_received_update_is_still_unverified_without_provenance(self) -> None:
        robot = FakeReadOnlyRobot(JointReading(positions=dict(POSITIONS), telemetry_received=True))
        report = read_joint_telemetry.read_once(robot)
        self.assertIs(report["verified"], False)
        self.assertEqual(report["rejected"], "source_unverified")
        self.assertEqual(report["unverifiedPositions"], POSITIONS)
        json.dumps(report, allow_nan=False)

    def test_a_failed_read_is_reported_and_still_disconnects(self) -> None:
        robot = FakeReadOnlyRobot(error=RobotConnectionError("could not connect"))
        report = read_joint_telemetry.read_once(robot)
        self.assertIs(report["verified"], False)
        self.assertIn("error", report)
        self.assertEqual(robot.calls, ["connect", "disconnect"])

    def test_nothing_is_contacted_without_the_flag(self) -> None:
        factory = mock.Mock()
        with mock.patch.object(read_joint_telemetry, "load_cyberwave_twin_settings") as settings:
            out = io.StringIO()
            with redirect_stdout(out):
                code = read_joint_telemetry.main([], robot_factory=factory)
        self.assertEqual(code, 2)
        factory.assert_not_called()
        settings.assert_not_called()
        self.assertIn("Nothing was contacted", out.getvalue())

    def test_main_exits_3_for_unverified_telemetry(self) -> None:
        robot = FakeReadOnlyRobot(JointReading(positions=dict(ZEROS), telemetry_received=False))
        with mock.patch.object(
            read_joint_telemetry, "load_cyberwave_twin_settings", return_value=object()
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = read_joint_telemetry.main(["--confirm-connect"], robot_factory=lambda _s: robot)
        self.assertEqual(code, 3)
        self.assertIs(json.loads(out.getvalue())["verified"], False)

    def test_the_script_has_no_movement_or_simulation_start_call(self) -> None:
        tree = ast.parse((WORKER_DIR / "read_joint_telemetry.py").read_text(encoding="utf-8"))
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        forbidden = {
            "affect", "set_joints", "set_pose", "grip", "release", "run_movement",
            "move_joint", "joints", "publish", "simulations",
        }
        self.assertEqual(attributes & forbidden, set())


if __name__ == "__main__":
    unittest.main()
