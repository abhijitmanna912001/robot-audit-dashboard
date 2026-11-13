"""Unit tests for the deterministic planner. Pure: no network, no database."""

from __future__ import annotations

import math
import unittest
from types import MappingProxyType
from unittest import mock

import planner
from plan_hasher import hash_actions
from planner import (
    MAX_DETAIL_LENGTH,
    MAX_REASON_LENGTH,
    PLANNER_ERROR,
    UNSUPPORTED_COMMAND,
    plan_command,
)
from robot_profile import SO101_PROFILE, JointLimit, RobotProfile
from safety_validator import validate_plan

ACTION_FIELDS = {"actionId", "type", "target", "value", "unit", "reason"}
# The agreed contract (docs/schema-draft.md). Written out, not imported, so a
# changed profile value fails test_so101_cap_is_the_agreed_ten.
AGREED_MAX_ACTIONS = 10


class SupportedGrammarTests(unittest.TestCase):
    def test_single_clause(self) -> None:
        result = plan_command("set joint _1 to 0.25 rad")
        self.assertTrue(result.ok)
        self.assertIsNone(result.failure)
        self.assertEqual(
            list(result.actions or ()),
            [
                {
                    "actionId": "a1",
                    "type": "set_joint",
                    "target": "_1",
                    "value": 0.25,
                    "unit": "rad",
                    "reason": "Command clause 1: set joint _1 to 0.25 rad",
                }
            ],
        )

    def test_every_supported_joint(self) -> None:
        for joint in ("_1", "_2", "_3", "_4", "_5", "_6"):
            with self.subTest(joint=joint):
                result = plan_command(f"set joint {joint} to 0.1 rad")
                self.assertTrue(result.ok)
                self.assertEqual(result.actions[0]["target"], joint)  # type: ignore[index]

    def test_so101_profile_has_six_joints(self) -> None:
        self.assertEqual(
            sorted(SO101_PROFILE.joint_position_limits), ["_1", "_2", "_3", "_4", "_5", "_6"]
        )

    def test_joint_names_are_derived_from_the_profile(self) -> None:
        # Swap the profile the planner reads. If joint names were hardcoded,
        # _2 would still be accepted and _9 refused.
        fake = RobotProfile(
            name="fake",
            joint_position_limits=MappingProxyType(
                {"_1": JointLimit(-1, 1), "_9": JointLimit(-1, 1)}
            ),
            joint_position_unit="rad",
            supported_action_types=frozenset({"set_joint"}),
            max_actions=10,
        )
        with mock.patch.object(planner, "SO101_PROFILE", fake):
            self.assertTrue(plan_command("set joint _9 to 0.1 rad").ok)
            failure = plan_command("set joint _2 to 0.1 rad").failure
            assert failure is not None
            self.assertIn("Only _1, _9.", failure.detail)
            hint = plan_command("hello").failure
            assert hint is not None
            self.assertIn("joints _1 to _9", hint.detail)
        self.assertTrue(plan_command("set joint _2 to 0.1 rad").ok)
        self.assertFalse(plan_command("set joint _9 to 0.1 rad").ok)

    def test_unit_is_derived_from_the_profile(self) -> None:
        self.assertEqual(SO101_PROFILE.joint_position_unit, "rad")
        self.assertEqual(plan_command("set joint _1 to 0.1 rad").actions[0]["unit"], "rad")  # type: ignore[index]

    def test_so101_cap_is_the_agreed_ten(self) -> None:
        self.assertEqual(SO101_PROFILE.max_actions, AGREED_MAX_ACTIONS)

    def test_planner_has_no_cap_constant_of_its_own(self) -> None:
        self.assertFalse(hasattr(planner, "MAX_ACTIONS"))

    def test_action_cap_is_derived_from_the_profile(self) -> None:
        def profile(cap: int | None) -> RobotProfile:
            return RobotProfile(
                name="fake",
                joint_position_limits=SO101_PROFILE.joint_position_limits,
                joint_position_unit="rad",
                supported_action_types=frozenset({"set_joint"}),
                max_actions=cap,
            )

        def clauses(n: int) -> str:
            return "; ".join("set joint _1 to 0.1 rad" for _ in range(n))

        with mock.patch.object(planner, "SO101_PROFILE", profile(3)):
            self.assertEqual(len(plan_command(clauses(3)).actions or ()), 3)
            failure = plan_command(clauses(4)).failure
            assert failure is not None
            self.assertIn("at most 3 are allowed", failure.detail)
        with mock.patch.object(planner, "SO101_PROFILE", profile(None)):
            self.assertEqual(len(plan_command(clauses(25)).actions or ()), 25)

    def test_reason_is_capped(self) -> None:
        number = "0." + "1" * 400
        result = plan_command(f"set joint _1 to {number} rad")
        self.assertTrue(result.ok)
        reason = result.actions[0]["reason"]  # type: ignore[index]
        self.assertEqual(len(reason), MAX_REASON_LENGTH)
        self.assertTrue(reason.startswith("Command clause 1: set joint _1 to 0.111"))
        self.assertTrue(math.isfinite(result.actions[0]["value"]))  # type: ignore[index]

    def test_short_reason_is_unchanged(self) -> None:
        reason = plan_command("set joint _1 to 0.25 rad").actions[0]["reason"]  # type: ignore[index]
        self.assertLess(len(reason), MAX_REASON_LENGTH)
        self.assertEqual(reason, "Command clause 1: set joint _1 to 0.25 rad")

    def test_multiple_clauses_get_unique_ordered_action_ids(self) -> None:
        result = plan_command("set joint _1 to 0.25 rad; set joint _2 to -0.5 rad;set joint _6 to 1 rad")
        self.assertTrue(result.ok)
        actions = list(result.actions or ())
        self.assertEqual([a["actionId"] for a in actions], ["a1", "a2", "a3"])
        self.assertEqual([a["target"] for a in actions], ["_1", "_2", "_6"])
        self.assertEqual([a["value"] for a in actions], [0.25, -0.5, 1.0])

    def test_action_shape_has_only_agreed_fields_and_no_nulls(self) -> None:
        result = plan_command("set joint _3 to 0.2 rad; set joint _4 to -0.2 rad")
        for action in result.actions or ():
            self.assertEqual(set(action), ACTION_FIELDS)
            self.assertNotIn(None, action.values())
            self.assertIsInstance(action["value"], float)

    def test_number_forms(self) -> None:
        cases = {"0.25": 0.25, "-1": -1.0, "+0.5": 0.5, ".5": 0.5, "-.5": -0.5, "2": 2.0, "-0": 0.0}
        for token, expected in cases.items():
            with self.subTest(token=token):
                result = plan_command(f"set joint _1 to {token} rad")
                self.assertTrue(result.ok)
                self.assertEqual(result.actions[0]["value"], expected)  # type: ignore[index]

    def test_negative_zero_is_normalised(self) -> None:
        value = plan_command("set joint _1 to -0 rad").actions[0]["value"]  # type: ignore[index]
        self.assertEqual(str(value), "0.0")

    def test_keywords_case_insensitive_and_whitespace_tolerant(self) -> None:
        result = plan_command("  SET   Joint _2   TO 0.3   RAD  ;  set joint _1 to 0 Rad ")
        self.assertTrue(result.ok)
        self.assertEqual([a["unit"] for a in result.actions or ()], ["rad", "rad"])

    def test_exactly_max_actions_is_accepted(self) -> None:
        text = "; ".join(f"set joint _{(i % 6) + 1} to 0.1 rad" for i in range(AGREED_MAX_ACTIONS))
        result = plan_command(text)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.actions or ()), AGREED_MAX_ACTIONS)
        ids = [a["actionId"] for a in result.actions or ()]
        self.assertEqual(len(set(ids)), AGREED_MAX_ACTIONS)

    def test_duplicate_joint_targets_are_kept_not_overwritten(self) -> None:
        result = plan_command("set joint _1 to 0.1 rad; set joint _1 to 0.2 rad")
        self.assertTrue(result.ok)
        actions = list(result.actions or ())
        self.assertEqual([(a["actionId"], a["target"], a["value"]) for a in actions],
                         [("a1", "_1", 0.1), ("a2", "_1", 0.2)])
        # ...and the validator rejects them (it did not before DUPLICATE_JOINT_TARGET).
        verdict = validate_plan("p1", actions).to_payload()
        self.assertEqual(verdict["outcome"], "fail")
        self.assertEqual(
            [(e["ruleId"], e["actionId"]) for e in verdict["errors"]],
            [("DUPLICATE_JOINT_TARGET", "a2")],
        )

    def test_out_of_limit_value_is_left_to_the_validator(self) -> None:
        result = plan_command("set joint _1 to 3.5 rad")
        self.assertTrue(result.ok)
        verdict = validate_plan("p1", list(result.actions or ())).to_payload()
        self.assertEqual(verdict["outcome"], "fail")
        self.assertEqual([e["ruleId"] for e in verdict["errors"]], ["JOINT_LIMIT_EXCEEDED"])

    def test_valid_plan_passes_validator_and_hashes(self) -> None:
        actions = list(plan_command("set joint _1 to 0.25 rad; set joint _6 to 1 rad").actions or ())
        self.assertEqual(validate_plan("p1", actions).to_payload()["outcome"], "pass")
        self.assertRegex(hash_actions(actions), r"^sha256:[0-9a-f]{64}$")

    def test_deterministic(self) -> None:
        text = "set joint _1 to 0.25 rad; set joint _2 to 0.5 rad"
        self.assertEqual(plan_command(text), plan_command(text))


class UnsupportedCommandTests(unittest.TestCase):
    def assert_unsupported(self, text: object) -> str:
        result = plan_command(text)
        self.assertFalse(result.ok)
        self.assertIsNone(result.actions)
        assert result.failure is not None
        self.assertEqual(result.failure.kind, UNSUPPORTED_COMMAND)
        self.assertTrue(result.failure.detail.strip())
        self.assertLessEqual(len(result.failure.detail), MAX_DETAIL_LENGTH)
        return result.failure.detail

    def test_rehearsal_natural_language_command(self) -> None:
        detail = self.assert_unsupported("Pick up the cup and move it to the left.")
        self.assertIn("Clause 1 is not a supported command", detail)

    def test_payload_shape(self) -> None:
        failure = plan_command("Pick up the cup and move it to the left.").failure
        assert failure is not None
        payload = failure.to_payload()
        self.assertEqual(set(payload), {"outcome", "error"})
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(set(payload["error"]), {"kind", "detail"})
        self.assertEqual(payload["error"]["kind"], "unsupported_command")
        self.assertNotIn("plan", payload)
        self.assertNotIn("planId", payload)
        self.assertNotIn("planHash", payload)

    def test_rejected_forms(self) -> None:
        cases = [
            "",
            "   ",
            "set joint _0 to 0.1 rad",
            "set joint _7 to 0.1 rad",
            "set joint 1 to 0.1 rad",
            "set joint joint_a to 0.1 rad",
            "set joint _1 to 0.1 deg",
            "set joint _1 to 0.1 degrees",
            "set joint _1 to 0.1",
            "set joint _1 to 0.1 rad please",
            "set joint _1 0.1 rad",
            "move joint _1 to 0.1 rad",
            "set joint _1 to nan rad",
            "set joint _1 to inf rad",
            "set joint _1 to 1e-1 rad",
            "set joint _1 to 0x1 rad",
            "set joint _1 to 1. rad",
            "set joint _1 to 0,5 rad",
            "set joint _1 to " + "9" * 400 + " rad",
            "set joint _1 to 0.1 rad;",
            "; set joint _1 to 0.1 rad",
            "set joint _1 to 0.1 rad;; set joint _2 to 0.1 rad",
            "set joint _1 to 0.1 rad; pick up the cup",
            "set joint _1 to 0.1 rad\nset joint _2 to 0.1 rad",
            "stop",
            "move forward",
        ]
        for text in cases:
            with self.subTest(text=text[:40]):
                self.assert_unsupported(text)

    def test_too_many_clauses(self) -> None:
        text = "; ".join("set joint _1 to 0.1 rad" for _ in range(AGREED_MAX_ACTIONS + 1))
        detail = self.assert_unsupported(text)
        self.assertIn(str(AGREED_MAX_ACTIONS), detail)

    def test_a_bad_clause_anywhere_rejects_the_whole_command(self) -> None:
        detail = self.assert_unsupported("set joint _1 to 0.1 rad; set joint _9 to 0.1 rad")
        self.assertIn("Clause 2", detail)

    def test_detail_names_the_problem(self) -> None:
        self.assertIn("joint '_7'", self.assert_unsupported("set joint _7 to 0.1 rad"))
        self.assertIn("unit 'deg'", self.assert_unsupported("set joint _1 to 0.1 deg"))

    def test_detail_is_capped_even_with_long_echoed_tokens(self) -> None:
        long = "x" * 5000
        for text in (f"set joint _{long} to 0.1 rad", f"set joint _1 to 0.1 {long}", long):
            with self.subTest(length=len(text)):
                detail = self.assert_unsupported(text)
                self.assertNotIn(long[:30], detail)

    def test_non_string_text_is_a_planner_error(self) -> None:
        for value in (None, 5, ["set joint _1 to 0.1 rad"]):
            with self.subTest(value=value):
                result = plan_command(value)
                assert result.failure is not None
                self.assertEqual(result.failure.kind, PLANNER_ERROR)
                self.assertLessEqual(len(result.failure.detail), MAX_DETAIL_LENGTH)

    def test_non_ascii_lookalikes_are_rejected(self) -> None:
        cases = {
            "arabic-indic digit": "set joint _1 to ١ rad",
            "extended arabic-indic digit": "set joint _1 to ۱ rad",
            "fullwidth digit": "set joint _1 to １ rad",
            "fullwidth digit in joint": "set joint _１ to 0.1 rad",
            "devanagari digit": "set joint _1 to १ rad",
            "superscript digit": "set joint _1 to ¹ rad",
            "unicode minus": "set joint _1 to −0.1 rad",
            "fullwidth minus": "set joint _1 to －0.1 rad",
            "long s keyword": "ſet joint _1 to 0.1 rad",
            "fullwidth keyword": "ｓｅｔ joint _1 to 0.1 rad",
            "cyrillic a in rad": "set joint _1 to 0.1 rаd",
            "greek omicron in to": "set joint _1 tο 0.1 rad",
            "dotless i in joint": "set joınt _1 to 0.1 rad",
            "non-breaking space between tokens": "set joint _1 to 0.1 rad",
            "non-breaking space at the start": " set joint _1 to 0.1 rad",
            "ideographic space": "set joint _1 to 0.1　rad",
            "zero-width space": "set joint _1​ to 0.1 rad",
            "fullwidth semicolon": "set joint _1 to 0.1 rad； set joint _2 to 0.1 rad",
            "combining mark": "set joint _1 to 0.1 rád",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                detail = self.assert_unsupported(text)
                self.assertIn("non-ASCII", detail)

    def test_ascii_control_characters_are_not_stripped_as_whitespace(self) -> None:
        # str.strip() with no argument would remove these; the planner must not.
        for ch in ("\x1c", "\x1d", "\x1e", "\x1f", "\x00"):
            with self.subTest(ch=repr(ch)):
                self.assert_unsupported(f"{ch}set joint _1 to 0.1 rad")

    def test_internal_error_fails_closed(self) -> None:
        with mock.patch.object(planner, "_plan", side_effect=RuntimeError("secret-ish text")):
            result = plan_command("set joint _1 to 0.1 rad")
        assert result.failure is not None
        self.assertEqual(result.failure.kind, PLANNER_ERROR)
        self.assertIsNone(result.actions)
        self.assertNotIn("secret-ish", result.failure.detail)


if __name__ == "__main__":
    unittest.main()
