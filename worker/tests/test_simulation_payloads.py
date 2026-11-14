"""Strict simulation event payloads (simulation_payloads.py). Offline, pure.

The contracts are the ones agreed with Abhijit. Every test here is about a
payload being refused BEFORE it could be persisted: the builders validate what
they build, and ``simulation_processor._write_event`` validates before the one
``audit_store.append_event`` call.
"""

from __future__ import annotations

import ast
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import audit_store
import simulation_payloads as sp
import simulation_processor
from robot_profile import SO101_PROFILE
from simulation_payloads import (
    SimulationPayloadError,
    build_execution_completed,
    build_execution_failed,
    build_execution_started,
    build_snapshot,
    validate_execution_completed,
    validate_execution_failed,
    validate_execution_started,
    validate_simulation_event,
    validate_snapshot,
)

WORKER_DIR = Path(__file__).resolve().parents[1]
JOINTS = {"_1": 0.0, "_2": 0.0, "_3": 0.0, "_4": 0.0, "_5": 0.0, "_6": 0.0}


def snap(read_at: str = "2026-11-14T08:30:01Z", **joints: Any) -> dict[str, Any]:
    return {"readAt": read_at, "joints": {**JOINTS, **joints}}


def completed() -> dict[str, Any]:
    return {
        "dryRun": False,
        "mode": "simulation",
        "summary": "Simulation only. Joint _1 moved and was restored.",
        "telemetry": {
            "start": snap("2026-11-14T08:30:01Z"),
            "target": snap("2026-11-14T08:30:04Z", _1=0.25),
            "restored": snap("2026-11-14T08:30:07Z", _1=0.001),
        },
        "toleranceRad": 0.01,
    }


def failed(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dryRun": False,
        "mode": "simulation",
        "error": {"kind": "target_timeout", "detail": "the target was not reached in time"},
        "stop": "confirmed",
    }
    payload.update(overrides)
    return payload


class Base(unittest.TestCase):
    def refuse(self, validator: Any, payload: Any) -> None:
        with self.assertRaises(SimulationPayloadError):
            validator(payload)

    def refuse_each(self, validator: Any, good: Any, mutations: dict[str, Any]) -> None:
        validator(good())  # the unmodified payload is accepted
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                payload = good()
                mutate(payload)
                self.refuse(validator, payload)


class ExecutionStartedTests(Base):
    def test_exact_contract(self) -> None:
        payload = build_execution_started()
        self.assertEqual(payload, {"dryRun": False, "mode": "simulation"})
        self.assertIs(payload["dryRun"], False)
        self.assertEqual(json.dumps(payload, sort_keys=True), '{"dryRun": false, "mode": "simulation"}')

    def test_anything_else_is_refused(self) -> None:
        for name, payload in {
            "dry run payload": {"dryRun": True},
            "dryRun true": {"dryRun": True, "mode": "simulation"},
            "dryRun 0": {"dryRun": 0, "mode": "simulation"},
            "dryRun string": {"dryRun": "false", "mode": "simulation"},
            "dryRun none": {"dryRun": None, "mode": "simulation"},
            "missing mode": {"dryRun": False},
            "missing dryRun": {"mode": "simulation"},
            "live mode": {"dryRun": False, "mode": "live"},
            "hardware mode": {"dryRun": False, "mode": "hardware"},
            "mode casing": {"dryRun": False, "mode": "Simulation"},
            "mode none": {"dryRun": False, "mode": None},
            "extra key": {"dryRun": False, "mode": "simulation", "target": "live"},
            "not an object": [("dryRun", False)],
            "none": None,
        }.items():
            with self.subTest(case=name):
                self.refuse(validate_execution_started, payload)


class SnapshotTests(Base):
    def test_exact_shape_is_accepted(self) -> None:
        self.assertEqual(validate_snapshot(snap(_1=0.25)), {**JOINTS, "_1": 0.25})
        validate_snapshot(snap("2026-11-14T08:30:01.123456Z"))
        validate_snapshot(snap(_2=1))  # a JSON integer is a number

    def test_joint_keys_are_exactly_the_six_so101_joints(self) -> None:
        self.assertEqual(sp.JOINT_NAMES, ("_1", "_2", "_3", "_4", "_5", "_6"))
        self.assertEqual(set(sp.JOINT_NAMES), set(SO101_PROFILE.joint_position_limits))
        shared = json.loads((WORKER_DIR.parent / "shared" / "so101-limits.json").read_text("utf-8"))
        self.assertEqual(set(sp.JOINT_NAMES), set(shared["joints"]))

    def test_malformed_snapshots_are_refused(self) -> None:
        def drop(key: str) -> Any:
            return lambda s: s.pop(key)

        def joint(name: str, value: Any) -> Any:
            return lambda s: s["joints"].__setitem__(name, value)

        def read_at(value: Any) -> Any:
            return lambda s: s.__setitem__("readAt", value)

        self.refuse_each(
            validate_snapshot,
            snap,
            {
                "missing readAt": drop("readAt"),
                "missing joints": drop("joints"),
                "extra key": lambda s: s.__setitem__("source", "sim"),
                "extra joint": joint("_7", 0.0),
                "alias joint": joint("shoulder_pan", 0.0),
                "missing joint": lambda s: s["joints"].pop("_6"),
                "joints is a list": lambda s: s.__setitem__("joints", [0.0] * 6),
                "nan": joint("_1", float("nan")),
                "infinity": joint("_1", float("inf")),
                "negative infinity": joint("_1", float("-inf")),
                "huge int": joint("_1", 10**400),
                "boolean true": joint("_1", True),
                "boolean false": joint("_1", False),
                "numeric string": joint("_1", "0.25"),
                "none": joint("_1", None),
                "list": joint("_1", [0.25]),
                "readAt none": read_at(None),
                "readAt number": read_at(1763109001),
                "readAt empty": read_at(""),
                "readAt date only": read_at("2026-11-14"),
                "readAt no zone": read_at("2026-11-14T08:30:01"),
                "readAt offset": read_at("2026-11-14T14:00:01+05:30"),
                "readAt zero offset": read_at("2026-11-14T08:30:01+00:00"),
                "readAt lowercase z": read_at("2026-11-14T08:30:01z"),
                "readAt space separator": read_at("2026-11-14 08:30:01Z"),
                "readAt impossible date": read_at("2026-13-45T08:30:01Z"),
                "readAt impossible time": read_at("2026-11-14T25:61:61Z"),
                "readAt trailing text": read_at("2026-11-14T08:30:01Z "),
                "readAt words": read_at("yesterday"),
            },
        )

    def test_build_snapshot_writes_canonical_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        built = build_snapshot(datetime(2026, 11, 14, 14, 0, 1, 987654, tzinfo=ist), dict(JOINTS))
        self.assertEqual(built["readAt"], "2026-11-14T08:30:01Z")
        self.assertTrue(all(type(v) is float for v in built["joints"].values()))
        with self.assertRaises(SimulationPayloadError):
            build_snapshot(datetime(2026, 11, 14, 8, 30, 1), dict(JOINTS))  # naive
        with self.assertRaises(SimulationPayloadError):
            build_snapshot("2026-11-14T08:30:01Z", dict(JOINTS))
        with self.assertRaises(SimulationPayloadError):
            build_snapshot(datetime.now(timezone.utc), {**JOINTS, "_1": "0.25"})


class ExecutionCompletedTests(Base):
    def test_exact_contract(self) -> None:
        payload = completed()
        validate_execution_completed(payload)
        self.assertEqual(set(payload), {"dryRun", "mode", "summary", "telemetry", "toleranceRad"})
        self.assertEqual(list(payload["telemetry"]), ["start", "target", "restored"])
        built = build_execution_completed(
            payload["summary"], **copy.deepcopy(payload["telemetry"])
        )
        self.assertEqual(built, payload)
        self.assertEqual(built["toleranceRad"], 0.01)
        json.dumps(built, allow_nan=False)

    def test_malformed_payloads_are_refused(self) -> None:
        def put(key: str, value: Any) -> Any:
            return lambda p: p.__setitem__(key, value)

        def telemetry(mutate: Any) -> Any:
            return lambda p: mutate(p["telemetry"])

        self.refuse_each(
            validate_execution_completed,
            completed,
            {
                "dryRun true": put("dryRun", True),
                "mode live": put("mode", "live"),
                "missing mode": lambda p: p.pop("mode"),
                "extra key": put("planId", "x"),
                "missing summary": lambda p: p.pop("summary"),
                "empty summary": put("summary", "   "),
                "summary not text": put("summary", 5),
                "summary too long": put("summary", "x" * 201),
                "summary control character": put("summary", "line one\nline two"),
                "missing tolerance": lambda p: p.pop("toleranceRad"),
                "looser tolerance": put("toleranceRad", 0.05),
                "tighter tolerance": put("toleranceRad", 0.001),
                "tolerance string": put("toleranceRad", "0.01"),
                "tolerance boolean": put("toleranceRad", True),
                "tolerance none": put("toleranceRad", None),
                "tolerance zero": put("toleranceRad", 0),
                "tolerance nan": put("toleranceRad", float("nan")),
                "missing telemetry": lambda p: p.pop("telemetry"),
                "missing start": telemetry(lambda t: t.pop("start")),
                "missing target": telemetry(lambda t: t.pop("target")),
                "missing restored": telemetry(lambda t: t.pop("restored")),
                "extra snapshot": telemetry(lambda t: t.__setitem__("midpoint", snap())),
                "empty target": telemetry(lambda t: t.__setitem__("target", {"readAt": "...", "joints": {}})),
                "target missing readAt": telemetry(lambda t: t["target"].pop("readAt")),
                "restored nan": telemetry(lambda t: t["restored"]["joints"].__setitem__("_3", float("nan"))),
                "start extra joint": telemetry(lambda t: t["start"]["joints"].__setitem__("_7", 0.0)),
                "not restored": telemetry(lambda t: t["restored"]["joints"].__setitem__("_1", 0.25)),
                "another joint not restored": telemetry(
                    lambda t: t["restored"]["joints"].__setitem__("_4", 0.02)
                ),
                "out of time order": telemetry(
                    lambda t: t["restored"].__setitem__("readAt", "2026-11-14T08:29:00Z")
                ),
            },
        )

    def test_summary_of_exactly_200_characters_is_accepted(self) -> None:
        payload = completed()
        payload["summary"] = "x" * 200
        validate_execution_completed(payload)

    def test_the_builder_has_no_tolerance_parameter(self) -> None:
        import inspect

        self.assertEqual(
            list(inspect.signature(build_execution_completed).parameters),
            ["summary", "start", "target", "restored"],
        )
        self.assertEqual(sp.TOLERANCE_RAD, 0.01)


class ExecutionFailedTests(Base):
    def test_exact_contract(self) -> None:
        payload = build_execution_failed("target_timeout", "the target was not reached in time", "confirmed")
        self.assertEqual(payload, failed())
        self.assertNotIn("lastTelemetry", payload)
        for stop in ("confirmed", "unconfirmed", "not_needed"):
            validate_execution_failed(failed(stop=stop))
        self.assertEqual(sp.STOP_VALUES, {"confirmed", "unconfirmed", "not_needed"})

    def test_optional_last_telemetry_uses_the_snapshot_shape(self) -> None:
        payload = build_execution_failed("return_timeout", "the arm did not return", "unconfirmed", snap(_1=0.2))
        self.assertEqual(set(payload), {"dryRun", "mode", "error", "stop", "lastTelemetry"})
        self.assertEqual(payload["lastTelemetry"], snap(_1=0.2))
        self.refuse(validate_execution_failed, failed(lastTelemetry=None))
        self.refuse(validate_execution_failed, failed(lastTelemetry={"readAt": "2026-11-14T08:30:01Z"}))
        self.refuse(validate_execution_failed, failed(lastTelemetry=snap(_1=float("inf"))))

    def test_malformed_payloads_are_refused(self) -> None:
        def error(key: str, value: Any) -> Any:
            return lambda p: p["error"].__setitem__(key, value)

        self.refuse_each(
            validate_execution_failed,
            failed,
            {
                "dry run shape": lambda p: p.__setitem__("error", "dry run failed"),
                "dryRun true": lambda p: p.__setitem__("dryRun", True),
                "mode live": lambda p: p.__setitem__("mode", "live"),
                "missing stop": lambda p: p.pop("stop"),
                "missing error": lambda p: p.pop("error"),
                "extra key": lambda p: p.__setitem__("traceback", "..."),
                "stop stopped": lambda p: p.__setitem__("stop", "stopped"),
                "stop boolean": lambda p: p.__setitem__("stop", True),
                "stop none": lambda p: p.__setitem__("stop", None),
                "stop casing": lambda p: p.__setitem__("stop", "Confirmed"),
                "kind uppercase": error("kind", "TARGET_TIMEOUT"),
                "kind camel case": error("kind", "targetTimeout"),
                "kind spaces": error("kind", "target timeout"),
                "kind hyphen": error("kind", "target-timeout"),
                "kind leading underscore": error("kind", "_timeout"),
                "kind trailing underscore": error("kind", "timeout_"),
                "kind double underscore": error("kind", "target__timeout"),
                "kind empty": error("kind", ""),
                "kind too long": error("kind", "a" * 41),
                "kind number": error("kind", 500),
                "kind none": error("kind", None),
                "detail empty": error("detail", ""),
                "detail whitespace": error("detail", "  \t "),
                "detail too long": error("detail", "x" * 201),
                "detail not text": error("detail", {"message": "x"}),
                "detail none": error("detail", None),
                "detail newline": error("detail", "Traceback:\n  File x"),
                "detail padded": error("detail", " padded "),
                "error extra key": error("httpStatus", 500),
                "error missing detail": lambda p: p["error"].pop("detail"),
            },
        )

    def test_the_builder_only_writes_the_fixed_error_kinds(self) -> None:
        for kind in sorted(sp.ERROR_KINDS):
            validate_execution_failed(build_execution_failed(kind, "detail", "not_needed"))
        with self.assertRaises(SimulationPayloadError):
            build_execution_failed("some_new_kind", "detail", "not_needed")
        with self.assertRaises(SimulationPayloadError):
            build_execution_failed("target_timeout", "detail", "probably")


class DispatchAndPersistenceTests(Base):
    def test_only_the_three_execution_events_have_a_simulation_shape(self) -> None:
        validate_simulation_event("execution_started", build_execution_started())
        validate_simulation_event("execution_completed", completed())
        validate_simulation_event("execution_failed", failed())
        for event_type in ("plan_generated", "command_received", "", None, 3):
            with self.subTest(event_type=event_type):
                self.refuse(lambda p: validate_simulation_event(event_type, p), {})
        # A payload of one event is not accepted for another.
        self.refuse(lambda p: validate_simulation_event("execution_completed", p), failed())
        self.refuse(lambda p: validate_simulation_event("execution_started", p), completed())

    def test_a_malformed_payload_never_reaches_the_audit_store(self) -> None:
        bad_completed = completed()
        bad_completed["telemetry"]["target"]["joints"]["_1"] = float("nan")
        cases = [
            ("execution_started", {"dryRun": False, "mode": "live"}),
            ("execution_started", {"dryRun": True}),
            ("execution_completed", bad_completed),
            ("execution_failed", failed(stop="maybe")),
            ("execution_failed", failed(extra="x")),
        ]
        with mock.patch.object(audit_store, "append_event", autospec=True) as append:
            for event_type, payload in cases:
                with self.subTest(event_type=event_type):
                    with self.assertRaises(SimulationPayloadError):
                        simulation_processor._write_event("c1", event_type, "message", payload, None)
            append.assert_not_called()
            simulation_processor._write_event(
                "c1", "execution_started", "message", build_execution_started(), None
            )
            append.assert_called_once()


class DryRunPayloadsUnchangedTests(unittest.TestCase):
    """The dry-run payloads keep their shape: dryRun true and no mode field."""

    def test_dry_run_payload_literals_in_the_dry_run_worker(self) -> None:
        source = (WORKER_DIR / "command_processor.py").read_text(encoding="utf-8")
        payloads = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                if "dryRun" in keys:
                    payloads.append((sorted(keys), node))
        self.assertEqual(
            sorted(keys for keys, _ in payloads),
            [["dryRun"], ["dryRun", "error"], ["dryRun", "error"], ["dryRun", "summary"]],
        )
        for _, node in payloads:
            value = dict(zip((k.value for k in node.keys), node.values))["dryRun"]  # type: ignore[union-attr]
            self.assertIsInstance(value, ast.Constant)
            self.assertIs(value.value, True)  # type: ignore[attr-defined]
        self.assertNotIn('"mode"', source)
        self.assertNotIn("simulation_payloads", source)

    def test_a_dry_run_payload_is_not_a_valid_simulation_payload(self) -> None:
        with self.assertRaises(SimulationPayloadError):
            validate_execution_started({"dryRun": True})
        with self.assertRaises(SimulationPayloadError):
            validate_execution_completed({"dryRun": True, "summary": "Dry run only."})
        with self.assertRaises(SimulationPayloadError):
            validate_execution_failed({"dryRun": True, "error": "dry run failed"})


class PurityTests(unittest.TestCase):
    def test_the_payload_module_imports_nothing_that_could_act(self) -> None:
        tree = ast.parse((WORKER_DIR / "simulation_payloads.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "datetime", "math", "re", "typing"})


if __name__ == "__main__":
    unittest.main()
