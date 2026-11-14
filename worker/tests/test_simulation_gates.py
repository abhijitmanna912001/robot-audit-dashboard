"""Gates for the simulation rehearsal (simulation_gates.py). Offline, no movement."""

from __future__ import annotations

import ast
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import simulation_gates as gates
from plan_snapshot import PlanSnapshot
from robot_profile import SO101_PROFILE
from simulation_gates import (
    CYBERWAVE_GATES,
    GateError,
    LiveSimulationGates,
    live_simulation_blockers,
    load_shared_limits,
    require_first_rehearsal_plan,
    require_within_shared_limits,
    revalidate_against_shared_limits,
)

WORKER_DIR = Path(__file__).resolve().parents[1]
COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"


def action(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "actionId": "a1",
        "type": "set_joint",
        "target": "_1",
        "value": 0.25,
        "unit": "rad",
        "reason": "Command clause 1: set joint _1 to 0.25 rad",
    }
    base.update(overrides)
    return base


def snapshot(*actions: dict[str, Any]) -> PlanSnapshot:
    return PlanSnapshot.create(COMMAND_ID, PLAN_ID, list(actions) or [action()])


class LiveGateTests(unittest.TestCase):
    def test_every_cyberwave_gate_is_unconfirmed_today(self) -> None:
        for field in dataclasses.fields(CYBERWAVE_GATES):
            with self.subTest(gate=field.name):
                self.assertIs(getattr(CYBERWAVE_GATES, field.name), False)
        self.assertFalse(CYBERWAVE_GATES.all_confirmed())
        self.assertEqual(len(live_simulation_blockers()), 6)

    def test_the_defaults_are_blocked(self) -> None:
        self.assertEqual(LiveSimulationGates(), CYBERWAVE_GATES)
        self.assertEqual(len(LiveSimulationGates().unconfirmed()), 6)

    def test_one_missing_confirmation_still_blocks(self) -> None:
        names = [f.name for f in dataclasses.fields(LiveSimulationGates)]
        for missing in names:
            with self.subTest(missing=missing):
                partial = LiveSimulationGates(**{n: n != missing for n in names})
                self.assertEqual(partial.unconfirmed(), (missing,))
                self.assertEqual(len(live_simulation_blockers(partial)), 1)
        self.assertEqual(live_simulation_blockers(LiveSimulationGates(**{n: True for n in names})), ())

    def test_a_truthy_value_is_not_a_confirmation(self) -> None:
        sloppy = LiveSimulationGates(stop_method_confirmed="yes")  # type: ignore[arg-type]
        self.assertIn("stop_method_confirmed", sloppy.unconfirmed())

    def test_missing_or_unknown_gates_block(self) -> None:
        for value in (None, True, {"stop_method_confirmed": True}, "confirmed"):
            with self.subTest(value=value):
                self.assertTrue(live_simulation_blockers(value))

    def test_gates_are_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            CYBERWAVE_GATES.stop_method_confirmed = True  # type: ignore[misc]

    def test_the_target_is_a_constant(self) -> None:
        self.assertEqual(gates.EXECUTION_TARGET, "simulation")


class FirstRehearsalRestrictionTests(unittest.TestCase):
    def kind(self, snap: Any) -> str:
        with self.assertRaises(GateError) as ctx:
            require_first_rehearsal_plan(snap)
        self.assertLessEqual(len(ctx.exception.detail), 200)
        return ctx.exception.kind

    def test_only_joint_1_to_0_25_rad_is_accepted(self) -> None:
        accepted = require_first_rehearsal_plan(snapshot())
        self.assertEqual((accepted["type"], accepted["target"], accepted["value"], accepted["unit"]),
                         ("set_joint", "_1", 0.25, "rad"))
        self.assertEqual((gates.FIRST_REHEARSAL_JOINT, gates.FIRST_REHEARSAL_TARGET_RAD), ("_1", 0.25))

    def test_everything_else_is_refused_even_though_the_limits_allow_it(self) -> None:
        cases = {
            "another joint": snapshot(action(target="_2")),
            "another target": snapshot(action(value=0.26)),
            "negative target": snapshot(action(value=-0.25)),
            "zero": snapshot(action(value=0.0)),
            "close to target": snapshot(action(value=0.2500001)),
            "two actions": snapshot(action(), action(actionId="a2", target="_2")),
            "same action twice": snapshot(action(), action(actionId="a2")),
            "another type": snapshot(action(type="move_forward")),
            "another unit": snapshot(action(unit="deg")),
            "extra field": snapshot({**action(), "speed": 1.0}),
            "missing field": snapshot({k: v for k, v in action().items() if k != "reason"}),
            "string value": snapshot(action(value="0.25")),
            "boolean value": snapshot(action(value=True)),
            "not an object": snapshot("set joint _1 to 0.25 rad"),  # type: ignore[arg-type]
        }
        for name, snap in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.kind(snap), "rehearsal_restriction")

    def test_command_text_and_other_inputs_are_not_a_snapshot(self) -> None:
        for value in ("set joint _1 to 0.25 rad", [action()], action(), None):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(self.kind(value), "approval_invalid")

    def test_a_snapshot_that_no_longer_matches_its_hash_is_refused(self) -> None:
        good = snapshot()
        other = snapshot(action(value=0.3))
        forged = dataclasses.replace(good, canonical_actions=other.canonical_actions)
        self.assertEqual(self.kind(forged), "approval_invalid")
        wrong_count = dataclasses.replace(good, action_count=2)
        self.assertEqual(self.kind(wrong_count), "approval_invalid")

    def test_each_call_returns_a_fresh_copy(self) -> None:
        snap = snapshot()
        first = require_first_rehearsal_plan(snap)
        first["value"] = 1.5
        self.assertEqual(require_first_rehearsal_plan(snap)["value"], 0.25)


class SharedLimitsTests(unittest.TestCase):
    def test_limits_come_from_the_shared_file_and_match_the_profile(self) -> None:
        limits = load_shared_limits()
        self.assertEqual(gates.SHARED_LIMITS_FILE, WORKER_DIR.parent / "shared" / "so101-limits.json")
        self.assertEqual(set(limits), set(SO101_PROFILE.joint_position_limits))
        for name, limit in SO101_PROFILE.joint_position_limits.items():
            self.assertEqual(limits[name], (limit.lower, limit.upper))

    def test_the_rehearsal_action_passes(self) -> None:
        revalidate_against_shared_limits(PLAN_ID, [action()])

    def test_actions_are_rechecked_against_the_file_not_a_cached_copy(self) -> None:
        narrow = {**load_shared_limits(), "_1": (-0.1, 0.1)}
        with self.assertRaises(GateError) as ctx:
            revalidate_against_shared_limits(PLAN_ID, [action()], narrow)
        self.assertEqual(ctx.exception.kind, "limits_revalidation_failed")

    def test_a_plan_the_validator_rejects_is_refused(self) -> None:
        for name, actions in {
            "out of range": [action(target="_6", value=-1.0)],
            "unknown joint": [action(target="_9")],
            "wrong unit": [action(unit="deg")],
            "nan": [action(value=float("nan"))],
            "empty": [],
        }.items():
            with self.subTest(case=name):
                with self.assertRaises(GateError) as ctx:
                    revalidate_against_shared_limits(PLAN_ID, actions)
                self.assertEqual(ctx.exception.kind, "limits_revalidation_failed")

    def test_values_are_checked_inclusively(self) -> None:
        limits = load_shared_limits()
        require_within_shared_limits("_1", 1.91986, limits)
        require_within_shared_limits("_6", -0.174533, limits)
        for joint, value in (("_1", 1.92), ("_6", -0.2), ("_9", 0.0), ("_1", float("nan")), ("_1", True), ("_1", "0.1")):
            with self.subTest(joint=joint, value=value):
                with self.assertRaises(GateError):
                    require_within_shared_limits(joint, value, limits)

    def test_an_unreadable_or_malformed_limits_file_fails_closed(self) -> None:
        good = json.loads(gates.SHARED_LIMITS_FILE.read_text(encoding="utf-8"))

        def without_joint() -> dict[str, Any]:
            data = json.loads(json.dumps(good))
            del data["joints"]["_6"]
            return data

        def inverted() -> dict[str, Any]:
            data = json.loads(json.dumps(good))
            data["joints"]["_1"] = {"min": 1.0, "max": -1.0}
            return data

        cases: dict[str, str] = {
            "not json": "{not json",
            "a list": "[]",
            "another robot": json.dumps({**good, "robot": "ur5"}),
            "degrees": json.dumps({**good, "unit": "deg"}),
            "missing joint": json.dumps(without_joint()),
            "inverted limits": json.dumps(inverted()),
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, text in cases.items():
                with self.subTest(case=name):
                    path = Path(folder) / "limits.json"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(GateError) as ctx:
                        load_shared_limits(path)
                    self.assertEqual(ctx.exception.kind, "limits_revalidation_failed")
            with self.assertRaises(GateError):
                load_shared_limits(Path(folder) / "missing.json")


class NoSwitchTests(unittest.TestCase):
    """No environment variable, setting or network can change a gate."""

    def test_the_gate_module_reads_no_environment_and_no_configuration(self) -> None:
        tree = ast.parse((WORKER_DIR / "simulation_gates.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(
            imported,
            {"__future__", "dataclasses", "json", "math", "pathlib", "typing",
             "plan_hasher", "plan_snapshot", "safety_validator", "simulation_payloads"},
        )
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse({"environ", "getenv", "os", "argv"} & (names | attrs))

    def test_the_cyberwave_gates_are_literal_false_in_source(self) -> None:
        tree = ast.parse((WORKER_DIR / "simulation_gates.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LiveSimulationGates")
        defaults = {
            n.target.id: n.value  # type: ignore[union-attr]
            for n in cls.body
            if isinstance(n, ast.AnnAssign)
        }
        self.assertEqual(len(defaults), 6)
        for name, value in defaults.items():
            with self.subTest(gate=name):
                self.assertIsInstance(value, ast.Constant)
                self.assertIs(value.value, False)  # type: ignore[union-attr]
        assign = next(
            n for n in tree.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "CYBERWAVE_GATES"
        )
        call = assign.value
        self.assertIsInstance(call, ast.Call)
        self.assertEqual((call.args, call.keywords), ([], []))  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
