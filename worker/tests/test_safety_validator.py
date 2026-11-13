"""Unit tests for safety_validator and robot_profile. Offline and pure.

No network, no database, no robot. The validator is a function of its inputs.
"""

from __future__ import annotations

import ast
import copy
import math
import re
import unittest
from pathlib import Path
from typing import Any

import safety_validator as sv
from robot_profile import SO101_PROFILE, JointLimit, RobotProfile
from safety_validator import validate_plan

PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"
REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DOC = REPO_ROOT / "docs" / "robot-capabilities.md"

EXPECTED_LIMITS = {
    "_1": (-1.91986, 1.91986),
    "_2": (-1.74533, 1.74533),
    "_3": (-1.74533, 1.5708),
    "_4": (-1.65806, 1.65806),
    "_5": (-2.79253, 2.79253),
    "_6": (-0.174533, 1.74533),
}


def action(**overrides: Any) -> dict[str, Any]:
    """A valid set_joint action, with fields replaced or (with DROP) removed."""
    base: dict[str, Any] = {
        "actionId": "a1",
        "type": "set_joint",
        "target": "_1",
        "value": 0.5,
        "unit": "rad",
        "reason": "test",
    }
    for key, value in overrides.items():
        if value is DROP:
            base.pop(key, None)
        else:
            base[key] = value
    return base


DROP = object()


def rules(result: sv.ValidationResult) -> list[str]:
    return [issue.rule_id for issue in result.errors]


def only_issue(test: unittest.TestCase, result: sv.ValidationResult) -> sv.ValidationIssue:
    test.assertEqual(result.outcome, "fail")
    test.assertEqual(len(result.errors), 1, [issue.to_payload() for issue in result.errors])
    return result.errors[0]


class PassingPlanTests(unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        result = validate_plan(PLAN_ID, [action(), action(actionId="a2", target="_6", value=1.0)])
        self.assertTrue(result.passed)
        self.assertEqual(result.outcome, "pass")
        self.assertEqual(result.errors, ())

    def test_integer_value_is_accepted(self) -> None:
        self.assertTrue(validate_plan(PLAN_ID, [action(value=1)]).passed)
        self.assertTrue(validate_plan(PLAN_ID, [action(value=0)]).passed)

    def test_pass_payload_matches_the_decided_shape(self) -> None:
        payload = validate_plan(PLAN_ID, [action()]).to_payload()
        self.assertEqual(
            payload,
            {
                "outcome": "pass",
                "planId": PLAN_ID,
                "validatorVersion": sv.VALIDATOR_VERSION,
                "errors": [],
            },
        )

    def test_fail_payload_matches_the_decided_shape(self) -> None:
        payload = validate_plan(PLAN_ID, [action(target="nope")]).to_payload()
        self.assertEqual(payload["outcome"], "fail")
        self.assertEqual(payload["planId"], PLAN_ID)
        self.assertIsInstance(payload["validatorVersion"], str)
        self.assertEqual(len(payload["errors"]), 1)
        error = payload["errors"][0]
        self.assertEqual(set(error), {"ruleId", "actionId", "field", "message"})
        self.assertEqual(error["ruleId"], "UNKNOWN_JOINT")
        self.assertEqual(error["actionId"], "a1")
        self.assertEqual(error["field"], "target")
        self.assertIsInstance(error["message"], str)


class JointLimitTests(unittest.TestCase):
    def test_profile_limits_are_the_confirmed_schema_values(self) -> None:
        actual = {
            name: (limit.lower, limit.upper)
            for name, limit in SO101_PROFILE.joint_position_limits.items()
        }
        self.assertEqual(actual, EXPECTED_LIMITS)

    def test_profile_limits_match_the_capabilities_document(self) -> None:
        doc = CAPABILITIES_DOC.read_text(encoding="utf-8")
        rows = re.findall(
            r"^\| `(_\d)` \| revolute \| (-?[\d.]+) \| (-?[\d.]+) \|", doc, flags=re.M
        )
        from_doc = {name: (float(lower), float(upper)) for name, lower, upper in rows}
        self.assertEqual(from_doc, EXPECTED_LIMITS)

    def test_both_limits_are_inclusive_for_every_joint(self) -> None:
        for name, (lower, upper) in EXPECTED_LIMITS.items():
            for value in (lower, upper):
                with self.subTest(joint=name, value=value):
                    self.assertTrue(validate_plan(PLAN_ID, [action(target=name, value=value)]).passed)

    def test_the_next_float_inside_either_limit_is_accepted(self) -> None:
        for name, (lower, upper) in EXPECTED_LIMITS.items():
            for value in (math.nextafter(lower, math.inf), math.nextafter(upper, -math.inf)):
                with self.subTest(joint=name, value=value):
                    self.assertTrue(validate_plan(PLAN_ID, [action(target=name, value=value)]).passed)

    def test_zero_and_midpoints_are_accepted_for_every_joint(self) -> None:
        for name, (lower, upper) in EXPECTED_LIMITS.items():
            for value in (0.0, -0.0, 0, (lower + upper) / 2):
                with self.subTest(joint=name, value=value):
                    self.assertTrue(validate_plan(PLAN_ID, [action(target=name, value=value)]).passed)

    def test_clearly_outside_values_are_rejected_for_every_joint(self) -> None:
        for name, (lower, upper) in EXPECTED_LIMITS.items():
            for value in (lower - 0.001, upper + 0.001, lower - 1, upper + 1, -math.pi * 2, math.pi * 2):
                with self.subTest(joint=name, value=value):
                    self.assertEqual(
                        rules(validate_plan(PLAN_ID, [action(target=name, value=value)])),
                        ["JOINT_LIMIT_EXCEEDED"],
                    )

    def test_limits_use_position_only_never_velocity_or_effort(self) -> None:
        # The schema's velocity and effort values (10.0) are not safe operating
        # limits and must not appear in the profile.
        self.assertEqual(
            {field for field in vars(SO101_PROFILE)},
            {"name", "joint_position_limits", "joint_position_unit", "supported_action_types", "max_actions"},
        )
        for limit in SO101_PROFILE.joint_position_limits.values():
            self.assertEqual(set(vars(limit)), {"lower", "upper"})
            self.assertLess(limit.lower, limit.upper)

    def test_the_next_float_outside_either_limit_is_rejected(self) -> None:
        for name, (lower, upper) in EXPECTED_LIMITS.items():
            for value in (math.nextafter(lower, -math.inf), math.nextafter(upper, math.inf)):
                with self.subTest(joint=name, value=value):
                    issue = only_issue(self, validate_plan(PLAN_ID, [action(target=name, value=value)]))
                    self.assertEqual(issue.rule_id, "JOINT_LIMIT_EXCEEDED")
                    self.assertEqual(issue.field, "value")
                    self.assertEqual(issue.action_id, "a1")

    def test_asymmetric_limits_are_respected(self) -> None:
        # _3 allows -1.74533 but only +1.5708; _6 allows +1.74533 but only -0.174533.
        self.assertTrue(validate_plan(PLAN_ID, [action(target="_3", value=-1.7)]).passed)
        self.assertEqual(rules(validate_plan(PLAN_ID, [action(target="_3", value=1.7)])), ["JOINT_LIMIT_EXCEEDED"])
        self.assertTrue(validate_plan(PLAN_ID, [action(target="_6", value=1.7)]).passed)
        self.assertEqual(rules(validate_plan(PLAN_ID, [action(target="_6", value=-0.2)])), ["JOINT_LIMIT_EXCEEDED"])

    def test_a_value_valid_for_one_joint_is_rejected_for_another(self) -> None:
        self.assertTrue(validate_plan(PLAN_ID, [action(target="_5", value=2.5)]).passed)
        self.assertEqual(rules(validate_plan(PLAN_ID, [action(target="_1", value=2.5)])), ["JOINT_LIMIT_EXCEEDED"])

    def test_huge_numbers_are_rejected_without_crashing(self) -> None:
        for value in (1e308, -1e308, 10**400, -(10**400)):
            with self.subTest(value=str(value)[:12]):
                self.assertEqual(rules(validate_plan(PLAN_ID, [action(value=value)])), ["JOINT_LIMIT_EXCEEDED"])


class NumericValueTests(unittest.TestCase):
    def test_non_finite_values_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(value=value)]))
                self.assertEqual(issue.rule_id, "NON_FINITE_VALUE")
                self.assertEqual(issue.field, "value")

    def test_non_numeric_values_are_rejected(self) -> None:
        for value in ("0.5", "", True, False, [0.5], {"v": 0.5}):
            with self.subTest(value=repr(value)):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(value=value)]))
                self.assertEqual(issue.rule_id, "INVALID_FIELD_TYPE")
                self.assertEqual(issue.field, "value")

    def test_missing_or_null_value_is_rejected(self) -> None:
        for value in (DROP, None):
            with self.subTest(value="missing" if value is DROP else "null"):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(value=value)]))
                self.assertEqual(issue.rule_id, "MISSING_REQUIRED_FIELD")
                self.assertEqual(issue.field, "value")


class RequiredFieldTests(unittest.TestCase):
    def test_each_required_field_missing(self) -> None:
        for field in ("actionId", "type", "target", "value", "unit", "reason"):
            with self.subTest(field=field):
                result = validate_plan(PLAN_ID, [action(**{field: DROP})])
                issue = only_issue(self, result)
                self.assertEqual(issue.rule_id, "MISSING_REQUIRED_FIELD")
                self.assertEqual(issue.field, field)

    def test_each_field_null(self) -> None:
        # Planner rule: unset fields are omitted, never null.
        for field in ("actionId", "type", "target", "value", "unit", "reason"):
            with self.subTest(field=field):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(**{field: None})]))
                self.assertEqual(issue.rule_id, "MISSING_REQUIRED_FIELD")
                self.assertEqual(issue.field, field)
                self.assertIn("null", issue.message)

    def test_empty_or_blank_strings_count_as_missing(self) -> None:
        for field in ("actionId", "type", "target", "unit", "reason"):
            for blank in ("", "   "):
                with self.subTest(field=field, blank=repr(blank)):
                    issue = only_issue(self, validate_plan(PLAN_ID, [action(**{field: blank})]))
                    self.assertEqual(issue.rule_id, "MISSING_REQUIRED_FIELD")
                    self.assertEqual(issue.field, field)

    def test_string_fields_of_the_wrong_type(self) -> None:
        for field in ("actionId", "type", "target", "unit", "reason"):
            for wrong in (5, 1.5, True, ["x"], {"x": 1}):
                with self.subTest(field=field, wrong=repr(wrong)):
                    issue = only_issue(self, validate_plan(PLAN_ID, [action(**{field: wrong})]))
                    self.assertEqual(issue.rule_id, "INVALID_FIELD_TYPE")
                    self.assertEqual(issue.field, field)

    def test_fields_the_contract_calls_optional_are_required_for_set_joint(self) -> None:
        # docs/schema-draft.md lists target, value and unit as optional on an
        # action in general. A set_joint cannot be checked without all three, so
        # the validator requires them. Requiring `unit` in particular is a worker
        # decision that still needs confirming: without it the number is ambiguous.
        for field in ("target", "value", "unit"):
            with self.subTest(field=field):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(**{field: DROP})]))
                self.assertEqual((issue.rule_id, issue.field), ("MISSING_REQUIRED_FIELD", field))

    def test_no_field_is_silently_defaulted(self) -> None:
        # A missing unit must not be read as radians, and a missing value not as 0.
        self.assertFalse(validate_plan(PLAN_ID, [action(unit=DROP, value=0.0)]).passed)
        self.assertFalse(validate_plan(PLAN_ID, [action(value=DROP)]).passed)

    def test_deeply_nested_values_are_rejected_without_crashing(self) -> None:
        deep: list[Any] = []
        current = deep
        for _ in range(5000):
            inner: list[Any] = []
            current.append(inner)
            current = inner
        self.assertEqual(rules(validate_plan(PLAN_ID, [action(value=deep)])), ["INVALID_FIELD_TYPE"])
        self.assertEqual(validate_plan(PLAN_ID, [deep]).outcome, "fail")

    def test_action_without_a_usable_id_reports_null_action_id(self) -> None:
        issue = only_issue(self, validate_plan(PLAN_ID, [action(actionId=DROP)]))
        self.assertIsNone(issue.action_id)
        self.assertIn("#0", issue.message)


class ActionTypeTests(unittest.TestCase):
    def test_only_set_joint_is_supported(self) -> None:
        self.assertEqual(SO101_PROFILE.supported_action_types, frozenset({"set_joint"}))

    def test_other_action_types_are_rejected(self) -> None:
        for action_type in ("stop", "move_forward", "grip", "release", "set_pose", "SET_JOINT", "set_joints"):
            with self.subTest(type=action_type):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(type=action_type)]))
                self.assertEqual(issue.rule_id, "UNSUPPORTED_ACTION")
                self.assertEqual(issue.field, "type")

    def test_a_profile_cannot_enable_a_type_the_validator_has_no_checks_for(self) -> None:
        profile = RobotProfile(
            name="test",
            joint_position_limits=SO101_PROFILE.joint_position_limits,
            joint_position_unit="rad",
            supported_action_types=frozenset({"set_joint", "stop"}),
        )
        self.assertEqual(rules(validate_plan(PLAN_ID, [action(type="stop")], profile)), ["UNSUPPORTED_ACTION"])


class TargetTests(unittest.TestCase):
    def test_all_six_schema_joint_names_are_accepted(self) -> None:
        for name in ("_1", "_2", "_3", "_4", "_5", "_6"):
            with self.subTest(joint=name):
                self.assertTrue(validate_plan(PLAN_ID, [action(target=name, value=0.0)]).passed)

    def test_unknown_targets_are_rejected(self) -> None:
        for target in ("joint_a", "joint_b", "_7", "_0", "1", "_01", " _1", "_1 ", "shoulder_pan", "gripper", "J1"):
            with self.subTest(target=repr(target)):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(target=target, value=0.0)]))
                self.assertEqual(issue.rule_id, "UNKNOWN_JOINT")
                self.assertEqual(issue.field, "target")

    def test_hash_reference_vector_is_test_data_not_a_valid_plan(self) -> None:
        # docs/schema-draft.md: the vector's joint names are test data, not the
        # real vocabulary. No alias maps joint_a or joint_b to a real joint.
        vector = [
            {"actionId": "a1", "type": "set_joint", "target": "joint_a", "value": 1.0, "unit": "rad", "reason": "x"},
            {"actionId": "a2", "type": "set_joint", "target": "joint_b", "value": 0.30000000000000004, "unit": "rad", "reason": "y"},
        ]
        result = validate_plan(PLAN_ID, vector)
        self.assertEqual(rules(result), ["UNKNOWN_JOINT", "UNKNOWN_JOINT"])
        self.assertEqual([issue.action_id for issue in result.errors], ["a1", "a2"])


class UnitTests(unittest.TestCase):
    def test_only_the_exact_unit_string_is_accepted(self) -> None:
        self.assertEqual(SO101_PROFILE.joint_position_unit, "rad")
        for unit in ("deg", "degree", "degrees", "radians", "RAD", "Rad", " rad", "rad ", "m"):
            with self.subTest(unit=repr(unit)):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(unit=unit, value=0.5)]))
                self.assertEqual(issue.rule_id, "UNIT_MISMATCH")
                self.assertEqual(issue.field, "unit")

    def test_no_conversion_and_no_limit_error_for_a_wrong_unit(self) -> None:
        # 90 "deg" must not be silently read as 90 rad, nor converted.
        self.assertEqual(rules(validate_plan(PLAN_ID, [action(unit="deg", value=90)])), ["UNIT_MISMATCH"])


class PlanLevelTests(unittest.TestCase):
    def test_actions_must_be_an_array(self) -> None:
        for bad in (None, {}, {"actions": []}, "[]", 5, (action(),)):
            with self.subTest(value=repr(bad)[:30]):
                issue = only_issue(self, validate_plan(PLAN_ID, bad))
                self.assertEqual(issue.rule_id, "INVALID_PLAN")
                self.assertEqual(issue.field, "actions")
                self.assertIsNone(issue.action_id)

    def test_empty_plan_is_rejected(self) -> None:
        issue = only_issue(self, validate_plan(PLAN_ID, []))
        self.assertEqual(issue.rule_id, "INVALID_PLAN")

    def test_actions_that_are_not_objects_are_rejected(self) -> None:
        for bad in (None, "set_joint", 5, [action()], True):
            with self.subTest(value=repr(bad)[:30]):
                issue = only_issue(self, validate_plan(PLAN_ID, [bad]))
                self.assertEqual(issue.rule_id, "INVALID_PLAN")

    def test_plan_id_must_be_a_non_empty_string(self) -> None:
        for bad in (None, "", "  ", 5):
            with self.subTest(plan_id=repr(bad)):
                result = validate_plan(bad, [action()])
                issue = only_issue(self, result)
                self.assertEqual(issue.rule_id, "INVALID_PLAN")
                self.assertEqual(issue.field, "planId")
                self.assertIsInstance(result.to_payload()["planId"], str)

    def test_duplicate_action_ids_are_rejected(self) -> None:
        result = validate_plan(PLAN_ID, [action(), action(target="_2"), action(actionId="a2")])
        issue = only_issue(self, result)
        self.assertEqual(issue.rule_id, "DUPLICATE_ACTION_ID")
        self.assertEqual(issue.action_id, "a1")
        self.assertEqual(issue.field, "actionId")

    def test_unexpected_fields_are_rejected(self) -> None:
        for extra in ("speed", "velocity", "effort", "planId", "note"):
            with self.subTest(field=extra):
                issue = only_issue(self, validate_plan(PLAN_ID, [action(**{extra: 1})]))
                self.assertEqual(issue.rule_id, "UNEXPECTED_FIELD")
                self.assertEqual(issue.field, extra)

    def test_max_actions_is_not_enforced_until_a_number_is_agreed(self) -> None:
        self.assertIsNone(SO101_PROFILE.max_actions)
        many = [action(actionId=f"a{i}") for i in range(200)]
        self.assertTrue(validate_plan(PLAN_ID, many).passed)

    def test_max_actions_is_enforced_when_a_profile_sets_it(self) -> None:
        profile = RobotProfile(
            name="test",
            joint_position_limits=SO101_PROFILE.joint_position_limits,
            joint_position_unit="rad",
            supported_action_types=frozenset({"set_joint"}),
            max_actions=2,
        )
        two = [action(actionId="a1"), action(actionId="a2")]
        self.assertTrue(validate_plan(PLAN_ID, two, profile).passed)
        issue = only_issue(self, validate_plan(PLAN_ID, two + [action(actionId="a3")], profile))
        self.assertEqual(issue.rule_id, "MAX_ACTIONS_EXCEEDED")
        self.assertIsNone(issue.action_id)


class BehaviourTests(unittest.TestCase):
    def test_all_problems_are_reported_in_action_order(self) -> None:
        plan = [
            action(actionId="a1", target="nope"),
            action(actionId="a2", value=float("nan")),
            action(actionId="a3", unit="deg"),
            action(actionId="a4", value=99),
            action(actionId="a5", type="stop"),
        ]
        result = validate_plan(PLAN_ID, plan)
        self.assertEqual(
            [(issue.action_id, issue.rule_id) for issue in result.errors],
            [
                ("a1", "UNKNOWN_JOINT"),
                ("a2", "NON_FINITE_VALUE"),
                ("a3", "UNIT_MISMATCH"),
                ("a4", "JOINT_LIMIT_EXCEEDED"),
                ("a5", "UNSUPPORTED_ACTION"),
            ],
        )

    def test_one_bad_action_fails_the_whole_plan(self) -> None:
        result = validate_plan(PLAN_ID, [action(), action(actionId="a2", value=99)])
        self.assertFalse(result.passed)
        self.assertEqual(result.outcome, "fail")

    def test_result_is_deterministic(self) -> None:
        plan = [action(target="nope", unit="deg", value=None), action(actionId="a2", bogus=1)]
        first = validate_plan(PLAN_ID, plan).to_payload()
        for _ in range(5):
            self.assertEqual(validate_plan(PLAN_ID, plan).to_payload(), first)

    def test_input_is_not_modified(self) -> None:
        plan = [action(), action(actionId="a2", target="nope", value=float("inf"))]
        snapshot = copy.deepcopy(plan)
        validate_plan(PLAN_ID, plan)
        self.assertEqual(repr(plan), repr(snapshot))

    def test_never_raises_on_arbitrary_input(self) -> None:
        odd_values: list[Any] = [
            None, 0, -1, 1.5, float("nan"), "", "x", [], {}, [[]], [{}], [None], [1, "a", None],
            [{"actionId": []}], [{"type": {}}], [{1: 2}], [{"actionId": "a", "type": "set_joint", "value": []}],
            {"actionId": "a1"}, object(), b"bytes", {1, 2},
        ]
        for value in odd_values:
            with self.subTest(value=repr(value)[:40]):
                result = validate_plan(PLAN_ID, value)
                self.assertEqual(result.outcome, "fail")
                self.assertGreaterEqual(len(result.errors), 1)

    def test_internal_error_fails_closed(self) -> None:
        class Explodes(dict):
            def get(self, *_args: Any) -> Any:
                raise RuntimeError("boom")

        result = validate_plan(PLAN_ID, [Explodes(actionId="a1")])
        issue = only_issue(self, result)
        self.assertEqual(issue.rule_id, "INVALID_PLAN")
        self.assertIn("failing closed", issue.message)

    def test_rule_ids(self) -> None:
        self.assertEqual(
            sv.AGREED_RULE_IDS,
            {
                "UNSUPPORTED_ACTION", "MISSING_REQUIRED_FIELD", "UNKNOWN_JOINT",
                "JOINT_LIMIT_EXCEEDED", "NON_FINITE_VALUE", "UNIT_MISMATCH", "MAX_ACTIONS_EXCEEDED",
            },
        )
        self.assertEqual(sv.AGREED_RULE_IDS & sv.PROPOSED_RULE_IDS, frozenset())

    def test_profile_is_read_only(self) -> None:
        with self.assertRaises(TypeError):
            SO101_PROFILE.joint_position_limits["_7"] = JointLimit(-1, 1)  # type: ignore[index]
        with self.assertRaises(Exception):
            SO101_PROFILE.max_actions = 5  # type: ignore[misc]

    def test_validator_has_no_io_dependencies(self) -> None:
        for module in ("safety_validator.py", "robot_profile.py"):
            tree = ast.parse((REPO_ROOT / "worker" / module).read_text(encoding="utf-8"))
            imported = {
                (node.module if isinstance(node, ast.ImportFrom) else alias.name)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in (node.names if isinstance(node, ast.Import) else [None])
            }
            self.assertEqual(
                imported - {"__future__", "math", "dataclasses", "typing", "types", "robot_profile"},
                set(),
                module,
            )


if __name__ == "__main__":
    unittest.main()
