"""Telemetry validation (sim_telemetry.py) and the read-only script. Offline.

No Cyberwave connection is made: readings come from literals and from a fake
robot. The network guard is active.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    unix_seconds_to_iso_z,
    unix_seconds_to_utc,
    validate_telemetry,
)
from simulation_gates import CYBERWAVE_GATES, live_simulation_blockers
from simulation_payloads import parse_utc, validate_snapshot

WORKER_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 14, 8, 30, 1, 500000, tzinfo=timezone.utc)
POSITIONS = {"_1": 0.1, "_2": -0.2, "_3": 0.3, "_4": 0.0, "_5": 0.5, "_6": 0.05}
ZEROS = {name: 0.0 for name in POSITIONS}


NOW_TS = NOW.timestamp()  # Unix seconds, the unit of the incoming message's timestamp


def raw(**overrides: Any) -> RawTelemetry:
    """A fixture standing in for one incoming joint-state message. Not real telemetry."""
    values: dict[str, Any] = {
        "positions": dict(POSITIONS),
        "source_type": "sim",
        "timestamp": NOW_TS - 0.2,
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
    def test_a_reading_in_the_required_form_is_accepted(self) -> None:
        snapshot = validate_telemetry(raw(), now=NOW)
        self.assertIsInstance(snapshot, JointSnapshot)
        self.assertEqual(dict(snapshot.joints), POSITIONS)
        # The message's Unix timestamp, converted. A float near today's epoch
        # resolves to well under a microsecond.
        self.assertLessEqual(
            abs(snapshot.read_at - (NOW - timedelta(milliseconds=200))),
            timedelta(microseconds=1),
        )
        payload = snapshot.to_payload()
        self.assertEqual(payload["readAt"], "2026-11-14T08:30:01Z")
        validate_snapshot(payload)

    def test_the_raw_reading_keeps_the_original_message_fields(self) -> None:
        reading = raw()
        validate_telemetry(reading, now=NOW)
        self.assertEqual(reading.timestamp, NOW_TS - 0.2)  # the number, not a datetime
        self.assertEqual(reading.source_type, "sim")
        with self.assertRaises(AttributeError):
            reading.timestamp = NOW_TS  # type: ignore[misc]

    def test_raw_telemetry_has_no_caller_supplied_provenance_field(self) -> None:
        names = [field.name for field in dataclasses.fields(RawTelemetry)]
        self.assertEqual(names, ["positions", "source_type", "timestamp", "update_received"])
        for label in ({"source": "simulation"}, {"kind": "measured"}, {"observed_at": NOW}):
            with self.subTest(label=list(label)[0]):
                with self.assertRaises(TypeError):
                    RawTelemetry(positions=dict(POSITIONS), **label)
        for name in ("SOURCE_SIMULATION", "KIND_MEASURED"):
            self.assertFalse(hasattr(sim_telemetry, name), name)

    def test_the_snapshot_cannot_be_changed(self) -> None:
        snapshot = validate_telemetry(raw(), now=NOW)
        with self.assertRaises(TypeError):
            snapshot.joints["_1"] = 9.9  # type: ignore[index]
        with self.assertRaises(AttributeError):
            snapshot.read_at = NOW  # type: ignore[misc]

    def test_the_snapshot_time_is_utc_whatever_zone_now_is_given_in(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        snapshot = validate_telemetry(raw(), now=NOW.astimezone(ist))
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

    def test_only_the_exact_source_type_sim_is_accepted(self) -> None:
        self.assertEqual(sim_telemetry.SOURCE_TYPE_SIM, "sim")
        self.assertIsInstance(validate_telemetry(raw(source_type="sim"), now=NOW), JointSnapshot)

    def test_a_missing_source_type_is_refused(self) -> None:
        self.assertEqual(self.reason(raw(source_type=None)), "source_unverified")

    def test_command_messages_are_refused(self) -> None:
        # sim_tele and edit are commands from the frontend, not simulator state.
        for source_type in ("sim_tele", "edit"):
            with self.subTest(source_type=source_type):
                self.assertEqual(
                    self.reason(raw(source_type=source_type)), "commanded_not_measured"
                )

    def test_hardware_sources_are_refused(self) -> None:
        for source_type in ("edge", "edge_leader", "edge_follower", "tele"):
            with self.subTest(source_type=source_type):
                self.assertEqual(
                    self.reason(raw(source_type=source_type)), "not_simulation_source"
                )

    def test_near_misses_of_sim_are_refused(self) -> None:
        # No trimming, no case folding, no synonym.
        for source_type in (
            "Sim", "SIM", " sim", "sim ", "sim\n", "simulation", "simulator",
            "mujoco", "playground", "live", "",
        ):
            with self.subTest(source_type=repr(source_type)):
                self.assertEqual(self.reason(raw(source_type=source_type)), "source_unverified")

    def test_a_source_type_of_the_wrong_type_is_refused(self) -> None:
        class StrSubclass(str):
            pass

        for source_type in (True, 1, 0.0, b"sim", ["sim"], {"source_type": "sim"}, StrSubclass("sim")):
            with self.subTest(source_type=repr(source_type)):
                self.assertEqual(self.reason(raw(source_type=source_type)), "source_invalid")

    def test_the_source_sets_match_the_wire_contract_values(self) -> None:
        # The seven source_type values of cyberwave-contracts 0.1.4, each handled.
        self.assertEqual(sim_telemetry.COMMAND_SOURCE_TYPES, {"sim_tele", "edit"})
        self.assertEqual(
            sim_telemetry.HARDWARE_SOURCE_TYPES, {"edge", "edge_leader", "edge_follower", "tele"}
        )
        self.assertNotIn("sim", sim_telemetry.COMMAND_SOURCE_TYPES | sim_telemetry.HARDWARE_SOURCE_TYPES)

    def test_a_reading_without_a_received_update_is_refused(self) -> None:
        for value in (None, False, "yes", 1):
            with self.subTest(value=value):
                self.assertEqual(self.reason(raw(update_received=value)), "no_update_received")


class PlaceholderTests(TelemetryCase):
    def test_zero_filled_reading_is_not_automatically_valid(self) -> None:
        for name, reading in {
            "sdk zero fill": RawTelemetry(positions=dict(ZEROS)),
            "no update": raw(positions=dict(ZEROS), update_received=False),
            "no source": raw(positions=dict(ZEROS), source_type=None),
            "unknown source": raw(positions=dict(ZEROS), source_type="simulation"),
            "command zeros": raw(positions=dict(ZEROS), source_type="sim_tele"),
            "integer zeros": raw(positions={k: 0 for k in ZEROS}, update_received=None),
            "negative zeros": raw(positions={k: -0.0 for k in ZEROS}, source_type="edge"),
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(reading), "placeholder_suspected")

    def test_zeros_pass_only_when_the_message_says_sim_and_an_update_arrived(self) -> None:
        # Passing is still not proof that the arm is at zero: see the gates test.
        snapshot = validate_telemetry(raw(positions=dict(ZEROS)), now=NOW)
        self.assertEqual(dict(snapshot.joints), ZEROS)

    def test_a_successful_read_is_not_proof(self) -> None:
        # The call "worked" and returned six finite numbers. Still refused.
        reading = RawTelemetry(positions=dict(POSITIONS), update_received=True)
        self.assertEqual(self.reason(reading), "source_unverified")


class UnixTimestampConversionTests(unittest.TestCase):
    """Cyberwave's aggregated joint-state timestamp is Unix seconds. Fixtures only."""

    KNOWN = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)  # 1700000000

    def reason(self, value: Any) -> str:
        with self.assertRaises(TelemetryRejected) as ctx:
            unix_seconds_to_utc(value)
        return ctx.exception.reason

    def test_an_integer_timestamp_is_converted(self) -> None:
        moment = unix_seconds_to_utc(1700000000)
        self.assertEqual(moment, self.KNOWN)
        self.assertEqual(moment.utcoffset(), timedelta(0))
        self.assertEqual(unix_seconds_to_utc(1700000000.0), self.KNOWN)

    def test_a_fractional_timestamp_is_converted(self) -> None:
        self.assertEqual(
            unix_seconds_to_utc(1700000000.5), self.KNOWN + timedelta(milliseconds=500)
        )
        self.assertEqual(
            unix_seconds_to_utc(1700000000.25), self.KNOWN + timedelta(milliseconds=250)
        )

    def test_it_is_serialised_as_iso_8601_utc_ending_in_z(self) -> None:
        self.assertEqual(unix_seconds_to_iso_z(1700000000), "2023-11-14T22:13:20Z")
        # The payload form is whole seconds: a fraction is dropped, not rounded up.
        self.assertEqual(unix_seconds_to_iso_z(1700000000.5), "2023-11-14T22:13:20Z")
        self.assertEqual(unix_seconds_to_iso_z(1700000000.999), "2023-11-14T22:13:20Z")
        text = unix_seconds_to_iso_z(NOW_TS)
        self.assertEqual(text, "2026-11-14T08:30:01Z")
        self.assertTrue(text.endswith("Z"))
        self.assertNotIn("+00:00", text)

    def test_round_trip(self) -> None:
        for moment in (self.KNOWN, NOW.replace(microsecond=0), datetime(2030, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(moment=moment.isoformat()):
                seconds = moment.timestamp()
                self.assertEqual(unix_seconds_to_utc(seconds), moment)
                self.assertEqual(parse_utc(unix_seconds_to_iso_z(seconds)), moment)
                self.assertEqual(unix_seconds_to_utc(int(seconds)), moment)

    def test_the_serialised_form_is_the_agreed_snapshot_timestamp(self) -> None:
        snapshot = validate_telemetry(raw(), now=NOW)
        payload = snapshot.to_payload()
        self.assertEqual(payload["readAt"], unix_seconds_to_iso_z(NOW_TS - 0.2))
        self.assertEqual(set(payload), {"readAt", "joints"})  # the snapshot shape is unchanged
        validate_snapshot(payload)

    def test_a_missing_timestamp_is_refused(self) -> None:
        self.assertEqual(self.reason(None), "timestamp_missing")

    def test_a_boolean_is_refused(self) -> None:
        for value in (True, False):
            with self.subTest(value=value):
                self.assertEqual(self.reason(value), "timestamp_invalid")

    def test_a_string_or_any_other_type_is_refused(self) -> None:
        for name, value in {
            "numeric string": "1700000000",
            "iso string": "2026-11-14T08:30:01Z",
            "empty string": "",
            "aware datetime": NOW,
            "naive datetime": NOW.replace(tzinfo=None),
            "list": [1700000000],
            "decimal": Decimal("1700000000"),
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(value), "timestamp_invalid")

    def test_nan_and_infinity_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(self.reason(value), "timestamp_not_finite")

    def test_zero_and_negative_values_are_refused(self) -> None:
        for value in (0, 0.0, -0.0, -1, -0.001, -1700000000.0):
            with self.subTest(value=value):
                self.assertEqual(self.reason(value), "timestamp_not_positive")

    def test_millisecond_or_finer_values_are_refused_not_rescaled(self) -> None:
        for name, value in {
            "milliseconds": 1700000000000,
            "milliseconds float": NOW_TS * 1000,
            "microseconds": 1700000000000000,
            "nanoseconds": 1700000000000000000,
            "the limit itself": sim_telemetry.MAX_UNIX_SECONDS,
            "huge int": 10**400,
        }.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(value), "timestamp_not_seconds")
        # Just under the limit is still seconds (and then stale by millennia).
        self.assertIsInstance(unix_seconds_to_utc(4102444800), datetime)  # 2100-01-01


class TimestampTests(TelemetryCase):
    """The same rules through validate_telemetry, plus freshness and ordering."""

    def test_missing_timestamp_is_refused(self) -> None:
        self.assertEqual(self.reason(raw(timestamp=None)), "timestamp_missing")
        self.assertEqual(
            self.reason(RawTelemetry(positions=dict(POSITIONS), source_type="sim", update_received=True)),
            "timestamp_missing",
        )

    def test_invalid_timestamps_are_refused(self) -> None:
        for name, value, expected in (
            ("boolean", True, "timestamp_invalid"),
            ("numeric string", "1763109001", "timestamp_invalid"),
            ("iso string", "2026-11-14T08:30:01Z", "timestamp_invalid"),
            ("aware datetime", NOW, "timestamp_invalid"),
            ("naive datetime", NOW.replace(tzinfo=None), "timestamp_invalid"),
            ("nan", float("nan"), "timestamp_not_finite"),
            ("infinity", float("inf"), "timestamp_not_finite"),
            ("zero", 0, "timestamp_not_positive"),
            ("negative", -NOW_TS, "timestamp_not_positive"),
            ("milliseconds", NOW_TS * 1000, "timestamp_not_seconds"),
        ):
            with self.subTest(case=name):
                self.assertEqual(self.reason(raw(timestamp=value)), expected)

    def test_stale_readings_are_refused(self) -> None:
        self.assertEqual(self.reason(raw(timestamp=NOW_TS - 2.5)), "stale")
        self.assertEqual(self.reason(raw(timestamp=NOW_TS - 3600)), "stale")
        self.assertEqual(self.reason(raw(timestamp=1700000000)), "stale")
        validate_telemetry(raw(timestamp=NOW_TS - 1.9), now=NOW)

    def test_future_readings_are_refused(self) -> None:
        self.assertEqual(self.reason(raw(timestamp=NOW_TS + 5)), "timestamp_in_future")
        self.assertEqual(self.reason(raw(timestamp=NOW_TS + 0.6)), "timestamp_in_future")
        validate_telemetry(raw(timestamp=NOW_TS + 0.1), now=NOW)
        self.assertEqual(sim_telemetry.MAX_CLOCK_SKEW_S, 0.5)

    def test_a_reading_from_before_the_movement_is_refused(self) -> None:
        sent_at = NOW - timedelta(milliseconds=100)
        self.assertEqual(self.reason(raw(), not_before=sent_at), "stale_before_dispatch")
        validate_telemetry(raw(timestamp=NOW_TS), now=NOW, not_before=sent_at)
        validate_telemetry(raw(timestamp=int(NOW_TS) + 1), now=NOW, not_before=sent_at)

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


class TelemetryGatesStayClosedTests(TelemetryCase):
    """Passing the validator is necessary, not proof. It opens nothing."""

    TELEMETRY_GATES = (
        "telemetry_is_actual_state_confirmed",
        "telemetry_source_confirmed",
        "measured_vs_placeholder_confirmed",
    )

    def test_the_three_telemetry_gates_are_false(self) -> None:
        for name in self.TELEMETRY_GATES:
            with self.subTest(gate=name):
                self.assertIs(getattr(CYBERWAVE_GATES, name), False)

    def test_all_six_gates_are_false(self) -> None:
        self.assertEqual(len(dataclasses.fields(CYBERWAVE_GATES)), 6)
        self.assertEqual(len(CYBERWAVE_GATES.unconfirmed()), 6)
        self.assertFalse(CYBERWAVE_GATES.all_confirmed())

    def test_a_reading_that_passes_validation_opens_no_gate(self) -> None:
        self.assertIsInstance(validate_telemetry(raw(), now=NOW), JointSnapshot)
        self.assertEqual(len(live_simulation_blockers()), 6)
        for name in self.TELEMETRY_GATES:
            self.assertIn(name, CYBERWAVE_GATES.unconfirmed())

    def test_the_validator_cannot_reach_the_gates(self) -> None:
        tree = ast.parse((WORKER_DIR / "sim_telemetry.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
        self.assertEqual(
            imported, {"__future__", "math", "dataclasses", "datetime", "types", "typing", "simulation_payloads"}
        )

    def test_the_module_states_that_passing_is_not_proof(self) -> None:
        self.assertIn("NECESSARY, NOT PROOF", sim_telemetry.__doc__ or "")
        self.assertIn("necessary, not proof", JointSnapshot.__doc__ or "")


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
