"""Unit tests for critic_gate and execution_gate. Offline and pure.

No HTTP request is made: critic responses are plain dicts built in the tests.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import critic_gate
import execution_gate
from critic_gate import (
    CriticDecision,
    build_critic_request,
    critic_failure,
    critic_network_failure,
    evaluate_critic_response,
)
from execution_gate import decide_execution
from plan_hasher import PlanHashError, hash_actions

COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"
OTHER_HASH = "sha256:" + "0" * 64
WORKER_DIR = Path(__file__).resolve().parents[1]
DROP = object()


def valid_actions() -> list[dict[str, Any]]:
    return [
        {"actionId": "a1", "type": "set_joint", "target": "_1", "value": 0.5, "unit": "rad", "reason": "r"},
        {"actionId": "a2", "type": "set_joint", "target": "_6", "value": 1.0, "unit": "rad", "reason": "r"},
    ]


def verdict(request: Any, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "commandId": request.command_id,
        "planId": request.plan_id,
        "planHash": request.plan_hash,
        "verdict": "approve",
        "reason": "ok",
        "triggeredRules": [],
    }
    for key, value in overrides.items():
        if value is DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return body


class CriticRequestTests(unittest.TestCase):
    def test_request_body_has_the_contract_fields_and_the_plan_hash(self) -> None:
        actions = valid_actions()
        request = build_critic_request(COMMAND_ID, PLAN_ID, actions)
        self.assertEqual(
            request.to_body(),
            {
                "commandId": COMMAND_ID,
                "planId": PLAN_ID,
                "planHash": hash_actions(actions),
                "actions": actions,
            },
        )
        self.assertRegex(request.plan_hash, r"^sha256:[0-9a-f]{64}$")

    def test_ids_must_be_non_empty_strings(self) -> None:
        for command_id, plan_id in (("", PLAN_ID), (COMMAND_ID, "  "), (None, PLAN_ID), (COMMAND_ID, 5)):
            with self.subTest(command_id=command_id, plan_id=plan_id):
                with self.assertRaises(ValueError):
                    build_critic_request(command_id, plan_id, valid_actions())  # type: ignore[arg-type]

    def test_unhashable_actions_are_refused(self) -> None:
        with self.assertRaises(PlanHashError):
            build_critic_request(COMMAND_ID, PLAN_ID, [{"value": float("nan")}])


class CriticApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())

    def test_matching_200_approve_is_an_approval(self) -> None:
        decision = evaluate_critic_response(self.request, 200, verdict(self.request))
        self.assertTrue(decision.approved)
        self.assertEqual(decision.category, critic_gate.CATEGORY_APPROVED)
        self.assertEqual(decision.command_id, COMMAND_ID)
        self.assertEqual(decision.plan_id, PLAN_ID)
        self.assertEqual(
            decision.to_payload(),
            {
                "outcome": "approve",
                "planHash": self.request.plan_hash,
                "response": verdict(self.request),
            },
        )

    def test_unexpected_extra_response_fields_do_not_change_the_decision(self) -> None:
        body = verdict(self.request, extra="ignored", approved=True, verdictOverride="approve")
        self.assertTrue(evaluate_critic_response(self.request, 200, body).approved)
        rejected = verdict(self.request, verdict="reject", approved=True, outcome="approve")
        self.assertFalse(evaluate_critic_response(self.request, 200, rejected).approved)

    def test_internal_category_is_not_in_the_audit_payload(self) -> None:
        decisions = [
            evaluate_critic_response(self.request, 200, verdict(self.request)),
            evaluate_critic_response(self.request, 200, verdict(self.request, planHash=OTHER_HASH)),
            critic_failure("timeout"),
        ]
        for decision in decisions:
            payload = decision.to_payload()
            self.assertLessEqual(set(payload), {"outcome", "planHash", "response", "error"})
            if "error" in payload:
                self.assertLessEqual(set(payload["error"]), {"kind", "httpStatus", "detail"})
                self.assertIn(payload["error"]["kind"], critic_gate.ERROR_KINDS)

    def test_only_an_exact_approve_verdict_approves(self) -> None:
        for value in ("approved", "APPROVE", "Approve", " approve", "approve ", "yes", "ok", "", True, 1, None, ["approve"]):
            with self.subTest(verdict=repr(value)):
                decision = evaluate_critic_response(self.request, 200, verdict(self.request, verdict=value))
                self.assertFalse(decision.approved)
                self.assertEqual(decision.outcome, "error")
                self.assertEqual(decision.category, critic_gate.CATEGORY_MALFORMED_RESPONSE)


class CriticRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())

    def test_ordinary_reject_is_recorded_as_reject(self) -> None:
        body = verdict(self.request, verdict="reject", reason="no", triggeredRules=["joint_out_of_range"])
        decision = evaluate_critic_response(self.request, 200, body)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.outcome, "reject")
        self.assertEqual(decision.category, critic_gate.CATEGORY_REJECTED)
        self.assertEqual(decision.to_payload(), {"outcome": "reject", "planHash": self.request.plan_hash, "response": body})

    def test_reject_without_a_plan_hash_is_still_a_reject(self) -> None:
        # The critic omits planHash when it could not compute one (plan_hash_error).
        body = verdict(self.request, verdict="reject", planHash=DROP, triggeredRules=["plan_hash_error"])
        decision = evaluate_critic_response(self.request, 200, body)
        self.assertEqual(decision.outcome, "reject")
        self.assertNotIn("planHash", decision.to_payload())


class PlanHashIntegrityTests(unittest.TestCase):
    """A response whose planHash differs from the request must never approve."""

    def setUp(self) -> None:
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())

    def test_200_approve_with_a_different_hash_is_not_an_approval(self) -> None:
        body = verdict(self.request, planHash=OTHER_HASH)
        decision = evaluate_critic_response(self.request, 200, body)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.outcome, "error")
        self.assertEqual(decision.category, critic_gate.CATEGORY_PLAN_HASH_MISMATCH)
        self.assertFalse(decide_execution(self.request, decision).allowed)

    def test_mismatch_is_an_error_not_an_ordinary_reject(self) -> None:
        # This is exactly what the critic returns for its plan_hash_mismatch rule.
        body = verdict(self.request, verdict="reject", planHash=OTHER_HASH, triggeredRules=["plan_hash_mismatch"])
        decision = evaluate_critic_response(self.request, 200, body)
        self.assertEqual(decision.outcome, "error")
        self.assertNotEqual(decision.category, critic_gate.CATEGORY_REJECTED)
        self.assertEqual(decision.category, critic_gate.CATEGORY_PLAN_HASH_MISMATCH)

    def test_mismatch_payload_shows_both_the_supplied_and_the_computed_hash(self) -> None:
        body = verdict(self.request, verdict="reject", planHash=OTHER_HASH, triggeredRules=["plan_hash_mismatch"])
        payload = evaluate_critic_response(self.request, 200, body).to_payload()
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["planHash"], self.request.plan_hash)  # supplied by the worker
        self.assertEqual(payload["response"]["planHash"], OTHER_HASH)  # computed by the critic
        self.assertEqual(payload["error"]["kind"], "invalid_body")
        self.assertEqual(payload["error"]["httpStatus"], 200)
        self.assertIn("planHash", payload["error"]["detail"])

    def test_near_miss_hashes_do_not_match(self) -> None:
        ours = self.request.plan_hash
        for wrong in (ours.upper(), ours + " ", " " + ours, ours[7:], ours[:-1], ours.replace("sha256:", "SHA256:")):
            with self.subTest(hash=wrong[:20]):
                decision = evaluate_critic_response(self.request, 200, verdict(self.request, planHash=wrong))
                self.assertFalse(decision.approved)
                self.assertEqual(decision.category, critic_gate.CATEGORY_PLAN_HASH_MISMATCH)

    def test_approve_with_no_or_non_string_hash_is_not_an_approval(self) -> None:
        for value in (DROP, None, 123, ["sha256:x"], {"hash": "x"}):
            with self.subTest(planHash=repr(value)):
                decision = evaluate_critic_response(self.request, 200, verdict(self.request, planHash=value))
                self.assertFalse(decision.approved)
                self.assertEqual(decision.outcome, "error")


class CriticErrorClassificationTests(unittest.TestCase):
    """Each failure has its own category and none of them is a reject."""

    def setUp(self) -> None:
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())

    def test_command_id_mismatch(self) -> None:
        for body in (verdict(self.request, commandId="another"), verdict(self.request, commandId="another", verdict="reject")):
            decision = evaluate_critic_response(self.request, 200, body)
            self.assertEqual(decision.outcome, "error")
            self.assertEqual(decision.category, critic_gate.CATEGORY_COMMAND_ID_MISMATCH)
            self.assertNotIn("response", decision.to_payload())

    def test_plan_id_mismatch(self) -> None:
        for body in (verdict(self.request, planId="another"), verdict(self.request, planId="another", verdict="reject")):
            decision = evaluate_critic_response(self.request, 200, body)
            self.assertEqual(decision.outcome, "error")
            self.assertEqual(decision.category, critic_gate.CATEGORY_PLAN_ID_MISMATCH)

    def test_authentication_failure_is_distinguished_from_other_statuses(self) -> None:
        # The critic answers 401 with a reject-shaped body. It is still an error.
        body = {"commandId": "unknown", "planId": "unknown", "verdict": "reject", "reason": "Unauthorized.", "triggeredRules": ["unauthorized"]}
        decision = evaluate_critic_response(self.request, 401, body)
        self.assertEqual(decision.outcome, "error")
        self.assertEqual(decision.category, critic_gate.CATEGORY_UNAUTHORIZED)
        self.assertEqual(decision.to_payload(), {"outcome": "error", "error": {"kind": "http_status", "httpStatus": 401}})

    def test_non_200_statuses_are_errors_even_with_an_approve_body(self) -> None:
        for status in (400, 403, 404, 500, 502, 201, 204, 302):
            with self.subTest(status=status):
                decision = evaluate_critic_response(self.request, status, verdict(self.request))
                self.assertFalse(decision.approved)
                self.assertEqual(decision.category, critic_gate.CATEGORY_HTTP_STATUS)
                self.assertEqual(
                    decision.to_payload(),
                    {"outcome": "error", "error": {"kind": "http_status", "httpStatus": status}},
                )

    def test_missing_response_is_an_error(self) -> None:
        for status, body in ((None, None), (200, None), (None, verdict(self.request))):
            with self.subTest(status=status, has_body=body is not None):
                decision = evaluate_critic_response(self.request, status, body)
                self.assertFalse(decision.approved)
                self.assertEqual(decision.outcome, "error")

    def test_unusable_status_values_are_errors(self) -> None:
        for status in ("200", 200.0, True, [200]):
            with self.subTest(status=repr(status)):
                decision = evaluate_critic_response(self.request, status, verdict(self.request))
                self.assertFalse(decision.approved)
                self.assertEqual(decision.error["kind"], "http_status")

    def test_malformed_bodies_are_errors(self) -> None:
        bodies: dict[str, Any] = {
            "string": "approve",
            "list": [verdict(self.request)],
            "number": 200,
            "empty object": {},
            "no verdict": verdict(self.request, verdict=DROP),
            "no reason": verdict(self.request, reason=DROP),
            "reason not a string": verdict(self.request, reason=5),
            "no commandId": verdict(self.request, commandId=DROP),
            "no planId": verdict(self.request, planId=DROP),
            "commandId not a string": verdict(self.request, commandId=5),
            "planId null": verdict(self.request, planId=None),
            "no triggeredRules": verdict(self.request, triggeredRules=DROP),
            "triggeredRules not a list": verdict(self.request, triggeredRules="none"),
            "triggeredRules with a non-string": verdict(self.request, triggeredRules=[1]),
        }
        for label, body in bodies.items():
            with self.subTest(case=label):
                decision = evaluate_critic_response(self.request, 200, body)
                self.assertFalse(decision.approved)
                self.assertEqual(decision.outcome, "error")
                self.assertEqual(decision.category, critic_gate.CATEGORY_MALFORMED_RESPONSE)
                self.assertEqual(decision.error["kind"], "invalid_body")

    def test_timeout_and_network_failure(self) -> None:
        timeout = critic_failure("timeout", "no response in 10s")
        self.assertEqual(timeout.category, critic_gate.CATEGORY_TIMEOUT)
        self.assertEqual(
            timeout.to_payload(),
            {"outcome": "error", "error": {"kind": "timeout", "detail": "no response in 10s"}},
        )
        network = critic_network_failure("connection refused")
        self.assertEqual(network.outcome, "error")
        self.assertEqual(network.category, critic_gate.CATEGORY_NETWORK_ERROR)
        self.assertFalse(network.approved)
        # Provisional: nearest documented kind, with the real cause in the detail.
        self.assertIn(network.error["kind"], critic_gate.ERROR_KINDS)
        self.assertIn("network error", network.error["detail"])
        self.assertIn("connection refused", network.error["detail"])

    def test_every_failure_category_is_distinct_from_a_reject(self) -> None:
        request = self.request
        failures = {
            "network": critic_network_failure("dns"),
            "timeout": critic_failure("timeout"),
            "malformed": evaluate_critic_response(request, 200, "nope"),
            "auth": evaluate_critic_response(request, 401, None),
            "http": evaluate_critic_response(request, 500, None),
            "command id": evaluate_critic_response(request, 200, verdict(request, commandId="x")),
            "plan id": evaluate_critic_response(request, 200, verdict(request, planId="x")),
            "plan hash": evaluate_critic_response(request, 200, verdict(request, planHash=OTHER_HASH)),
        }
        categories = {label: decision.category for label, decision in failures.items()}
        self.assertEqual(len(set(categories.values())), len(categories), categories)
        for label, decision in failures.items():
            with self.subTest(case=label):
                self.assertEqual(decision.outcome, "error")
                self.assertNotEqual(decision.category, critic_gate.CATEGORY_REJECTED)
                self.assertFalse(decision.approved)
                self.assertFalse(decide_execution(request, decision).allowed)

    def test_never_raises(self) -> None:
        class Explodes(dict):
            def get(self, *_args: Any) -> Any:
                raise RuntimeError("boom")

        decision = evaluate_critic_response(self.request, 200, Explodes(verdict(self.request)))
        self.assertEqual(decision.outcome, "error")
        self.assertEqual(evaluate_critic_response(None, 200, verdict(self.request)).outcome, "error")  # type: ignore[arg-type]

    def test_failure_helper_edge_cases(self) -> None:
        self.assertEqual(critic_failure("made_up").error["kind"], "invalid_body")
        self.assertFalse(critic_failure("made_up").approved)
        self.assertLessEqual(len(critic_failure("timeout", "x" * 5000).error["detail"]), 200)


class ExecutionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())
        self.approval = evaluate_critic_response(self.request, 200, verdict(self.request))

    def test_allowed_only_when_validator_and_critic_both_pass(self) -> None:
        decision = decide_execution(self.request, self.approval)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())
        self.assertTrue(decision.validation.passed)

    def test_gate_revalidates_and_rehashes_itself(self) -> None:
        with (
            mock.patch.object(execution_gate, "validate_plan", wraps=execution_gate.validate_plan) as validate,
            mock.patch.object(execution_gate, "hash_actions", wraps=execution_gate.hash_actions) as rehash,
        ):
            decide_execution(self.request, self.approval)
        validate.assert_called_once()
        rehash.assert_called_once_with(self.request.actions)

    def test_critic_reject_blocks(self) -> None:
        rejection = evaluate_critic_response(self.request, 200, verdict(self.request, verdict="reject"))
        decision = decide_execution(self.request, rejection)
        self.assertFalse(decision.allowed)
        self.assertIn("critic", decision.reasons[0])

    def test_missing_or_malformed_critic_result_blocks_without_raising(self) -> None:
        for critic in (None, {}, {"outcome": "approve"}, "approve", True, 200):
            with self.subTest(critic=repr(critic)):
                decision = decide_execution(self.request, critic)
                self.assertFalse(decision.allowed)
                self.assertIn("no critic result", decision.reasons)

    def test_missing_or_malformed_request_blocks_without_raising(self) -> None:
        for request in (None, {}, self.request.to_body(), "plan"):
            with self.subTest(request=type(request).__name__):
                decision = decide_execution(request, self.approval)
                self.assertFalse(decision.allowed)
                self.assertIsNone(decision.validation)

    def test_validator_failure_blocks_even_if_the_critic_approved(self) -> None:
        actions = valid_actions()
        actions[0]["value"] = 99  # outside the schema limit for _1
        request = build_critic_request(COMMAND_ID, PLAN_ID, actions)
        approval = evaluate_critic_response(request, 200, verdict(request))
        self.assertTrue(approval.approved)
        decision = decide_execution(request, approval)
        self.assertFalse(decision.allowed)
        self.assertIn("JOINT_LIMIT_EXCEEDED", decision.reasons[0])

    def test_value_the_mock_critic_would_approve_is_still_blocked(self) -> None:
        # _6 at -1.0 rad is inside a generic +/-1.57 rad rule but below the
        # schema lower limit for _6 (-0.174533). The validator must stop it.
        actions = [{"actionId": "a1", "type": "set_joint", "target": "_6", "value": -1.0, "unit": "rad", "reason": "r"}]
        request = build_critic_request(COMMAND_ID, PLAN_ID, actions)
        approval = evaluate_critic_response(request, 200, verdict(request))
        decision = decide_execution(request, approval)
        self.assertFalse(decision.allowed)
        self.assertIn("JOINT_LIMIT_EXCEEDED", decision.reasons[0])

    def test_invalid_plans_block(self) -> None:
        for actions in ([], [{}], [None], [{"actionId": "a1"}]):
            with self.subTest(actions=repr(actions)):
                request = build_critic_request(COMMAND_ID, PLAN_ID, actions)
                approval = evaluate_critic_response(request, 200, verdict(request))
                self.assertFalse(decide_execution(request, approval).allowed)

    def test_hash_reference_vector_is_blocked_by_the_validator(self) -> None:
        vector = [
            {"actionId": "a1", "type": "set_joint", "target": "joint_a", "value": 1.0, "unit": "rad", "reason": "x"},
            {"actionId": "a2", "type": "set_joint", "target": "joint_b", "value": 0.30000000000000004, "unit": "rad", "reason": "y"},
        ]
        request = build_critic_request(COMMAND_ID, PLAN_ID, vector)
        approval = evaluate_critic_response(request, 200, verdict(request))
        decision = decide_execution(request, approval)
        self.assertFalse(decision.allowed)
        self.assertIn("UNKNOWN_JOINT", decision.reasons[0])

    def test_actions_changed_after_approval_are_blocked(self) -> None:
        # Still within limits, so the validator passes. Only the hash catches it.
        self.request.actions[0]["value"] = 0.6
        decision = decide_execution(self.request, self.approval)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.validation.passed)
        self.assertTrue(any("changed" in reason for reason in decision.reasons))

    def test_action_added_or_reordered_after_approval_is_blocked(self) -> None:
        added = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())
        approval = evaluate_critic_response(added, 200, verdict(added))
        added.actions.append({"actionId": "a3", "type": "set_joint", "target": "_2", "value": 0.1, "unit": "rad", "reason": "r"})
        self.assertFalse(decide_execution(added, approval).allowed)

        reordered = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())
        approval = evaluate_critic_response(reordered, 200, verdict(reordered))
        reordered.actions.reverse()
        self.assertFalse(decide_execution(reordered, approval).allowed)

    def test_approval_for_a_different_plan_hash_is_blocked(self) -> None:
        other = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions()[:1])
        other_approval = evaluate_critic_response(other, 200, verdict(other))
        decision = decide_execution(self.request, other_approval)
        self.assertFalse(decision.allowed)
        self.assertIn("the critic approved a different plan hash", decision.reasons)

    def test_approval_for_another_command_or_plan_with_identical_actions_is_blocked(self) -> None:
        # Same actions, so the same hash. Only the ids tell the two apart.
        for command_id, plan_id, expected in (
            ("another-command", PLAN_ID, "different command"),
            (COMMAND_ID, "another-plan", "different plan"),
        ):
            with self.subTest(command_id=command_id, plan_id=plan_id):
                other = build_critic_request(command_id, plan_id, valid_actions())
                self.assertEqual(other.plan_hash, self.request.plan_hash)
                other_approval = evaluate_critic_response(other, 200, verdict(other))
                self.assertTrue(other_approval.approved)
                decision = decide_execution(self.request, other_approval)
                self.assertFalse(decision.allowed)
                self.assertTrue(any(expected in reason for reason in decision.reasons))

    def test_hand_built_approvals_are_blocked(self) -> None:
        forged = [
            CriticDecision(outcome="approve", category=critic_gate.CATEGORY_APPROVED),
            CriticDecision(outcome="approve", category=critic_gate.CATEGORY_APPROVED, plan_hash=self.request.plan_hash),
            CriticDecision(outcome="approve", category=critic_gate.CATEGORY_REJECTED, command_id=COMMAND_ID, plan_id=PLAN_ID, plan_hash=self.request.plan_hash),
        ]
        for decision in forged:
            with self.subTest(decision=decision):
                self.assertFalse(decide_execution(self.request, decision).allowed)

    def test_unhashable_plan_is_blocked(self) -> None:
        self.request.actions[0]["reason"] = "\ud800"  # not valid UTF-8, cannot be hashed
        decision = decide_execution(self.request, self.approval)
        self.assertFalse(decision.allowed)
        self.assertIn("the actions cannot be hashed", decision.reasons)

    def test_all_reasons_are_reported(self) -> None:
        self.request.actions[0]["target"] = "joint_a"
        decision = decide_execution(self.request, critic_failure("timeout"))
        self.assertEqual(len(decision.reasons), 3)

    def test_internal_error_is_a_refusal(self) -> None:
        with mock.patch.object(execution_gate, "validate_plan", side_effect=RuntimeError("boom")):
            decision = decide_execution(self.request, self.approval)
        self.assertFalse(decision.allowed)
        self.assertIn("refusing", decision.reasons[0])
        with mock.patch.object(execution_gate, "hash_actions", side_effect=RuntimeError("boom")):
            self.assertFalse(decide_execution(self.request, self.approval).allowed)


class TriggeredRulesTests(unittest.TestCase):
    """An approval must carry no triggered rules (Abhijit's review of a173b98)."""

    def setUp(self) -> None:
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, valid_actions())

    def test_approve_with_empty_rules_is_an_approval(self) -> None:
        decision = evaluate_critic_response(self.request, 200, verdict(self.request, triggeredRules=[]))
        self.assertTrue(decision.approved)

    def test_approve_with_rules_is_invalid_body(self) -> None:
        for rules in (["joint_limit_exceeded"], ["joint_limit_exceeded", "unit_mismatch"], [""]):
            with self.subTest(rules=rules):
                decision = evaluate_critic_response(
                    self.request, 200, verdict(self.request, triggeredRules=rules)
                )
                self.assertFalse(decision.approved)
                self.assertEqual(decision.outcome, "error")  # never a reject
                self.assertEqual(decision.category, critic_gate.CATEGORY_MALFORMED_RESPONSE)
                self.assertEqual(
                    decision.to_payload(),
                    {
                        "outcome": "error",
                        "error": {
                            "kind": "invalid_body",
                            "httpStatus": 200,
                            "detail": "an approve verdict has triggered rules",
                        },
                    },
                )

    def test_reject_with_rules_is_still_a_reject(self) -> None:
        for rules in (["joint_limit_exceeded"], ["joint_limit_exceeded", "duplicate_joint_target"]):
            with self.subTest(rules=rules):
                body = verdict(self.request, verdict="reject", triggeredRules=rules)
                decision = evaluate_critic_response(self.request, 200, body)
                self.assertEqual(decision.outcome, "reject")
                self.assertEqual(decision.category, critic_gate.CATEGORY_REJECTED)
                self.assertEqual(decision.to_payload()["response"]["triggeredRules"], rules)

    def test_missing_or_malformed_rules_are_errors(self) -> None:
        for verdict_value in ("approve", "reject"):
            for rules in (DROP, None, "joint_limit_exceeded", {"a": 1}, [1], [None], ["ok", 2]):
                with self.subTest(verdict=verdict_value, rules=rules):
                    decision = evaluate_critic_response(
                        self.request, 200, verdict(self.request, verdict=verdict_value, triggeredRules=rules)
                    )
                    self.assertEqual(decision.outcome, "error")
                    self.assertEqual(decision.error["kind"], "invalid_body")


class PurityTests(unittest.TestCase):
    def test_gates_do_no_io_and_cannot_reach_a_robot(self) -> None:
        allowed = {
            "__future__", "dataclasses", "typing", "plan_hasher", "schemas", "critic_gate",
            "robot_profile", "safety_validator",
        }
        for module in ("critic_gate.py", "execution_gate.py"):
            source = (WORKER_DIR / module).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = {
                (node.module if isinstance(node, ast.ImportFrom) else alias.name)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in (node.names if isinstance(node, ast.Import) else [None])
            }
            self.assertEqual(imported - allowed, set(), module)
            calls = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            self.assertEqual(calls & {"set_joints", "affect", "grip", "release", "publish", "post", "insert"}, set())


if __name__ == "__main__":
    unittest.main()
