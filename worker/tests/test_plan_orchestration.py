"""Successful-plan orchestration in command_processor. Offline.

Autospecced audit_store functions and an offline critic (see
tests/test_command_processor.py), attached to one parent mock so the exact
call order is asserted. The network guard is active in every test.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import unittest
from typing import Any
from unittest import mock

import httpx

import command_processor
import critic_client
from audit_store import InvalidPayloadError, StateConflictError, WriteError
from config import CriticSettings
from command_processor import (
    STAGE_CRITIC_EVENT_WRITE,
    STAGE_EVENT_WRITE,
    STAGE_STATUS_UPDATE,
    STAGE_VALIDATOR_EVENT_WRITE,
    CommandProcessingError,
    decide_final_status,
    process_one_command,
)
from critic_gate import (
    MAX_DETAIL_LENGTH,
    MAX_REASON_LENGTH,
    CriticDecision,
    CriticRequest,
    critic_failure,
    evaluate_critic_response,
)
from plan_hasher import hash_actions
from planner import PlanResult
from safety_validator import VALIDATOR_VERSION, validate_plan
from tests.test_command_processor import (
    COMMAND_ID,
    WORKER_ID,
    ProcessorTestCase,
    command_row,
    critic_returning,
)

from tests.test_critic_client import FakeClock, TrackingStream, split

# The real HTTP critic client, captured before any test patches it. Only ever
# called here with an httpx.MockTransport.
REAL_REVIEW_PLAN = critic_client.review_plan

PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"
VALID_TEXT = "set joint _1 to 0.25 rad"
OUT_OF_RANGE_TEXT = "set joint _6 to -1.0 rad"  # the agreed live rejection rehearsal

SUCCESS_ORDER = [
    "claim_next_command",
    "append_event",  # plan_generated
    "review_plan",
    "append_event",  # validator_result
    "append_event",  # critic_result
    "set_command_status",
]


class OrchestrationTestCase(ProcessorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan_ids = mock.Mock(return_value=PLAN_ID)
        patcher = mock.patch.object(command_processor, "new_plan_id", self.plan_ids)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.use_text(VALID_TEXT)

    def use_text(self, text: str) -> None:
        self.calls.claim_next_command.return_value = command_row(text)

    def use_critic(self, verdict: str = "approve", status: int = 200, **overrides: Any) -> None:
        self.calls.review_plan.side_effect = critic_returning(verdict, status, **overrides)

    def events(self) -> list[tuple[str, str, dict[str, Any]]]:
        """(event_type, message, payload) of every append_event call, in order."""
        return [
            (c.args[1], c.args[2], c.args[3]) for c in self.calls.append_event.call_args_list
        ]

    def event(self, event_type: str) -> dict[str, Any]:
        matches = [p for t, _, p in self.events() if t == event_type]
        self.assertEqual(len(matches), 1, event_type)
        return matches[0]

    def sent_request(self) -> CriticRequest:
        self.calls.review_plan.assert_called_once()
        return self.calls.review_plan.call_args.args[0]

    def status(self) -> str:
        return self.bound("set_command_status")["status"]


class FinalStatusPrecedenceTests(unittest.TestCase):
    TABLE = {
        ("pass", "approve"): "approved",
        ("fail", "reject"): "rejected",
        ("fail", "error"): "rejected",
        ("error", "reject"): "rejected",
        ("pass", "reject"): "rejected",
        ("pass", "error"): "failed",
        ("error", "approve"): "failed",
        ("error", "error"): "failed",
        ("fail", "approve"): "rejected",
    }

    def test_agreed_table(self) -> None:
        for (validator, critic), expected in self.TABLE.items():
            with self.subTest(validator=validator, critic=critic):
                self.assertEqual(decide_final_status(validator, critic), expected)

    def test_only_pass_and_approve_can_approve(self) -> None:
        values = ["pass", "fail", "error", "approve", "reject", "", None, "PASS", "Approve"]
        for validator, critic in itertools.product(values, repeat=2):
            status = decide_final_status(validator, critic)  # type: ignore[arg-type]
            self.assertIn(status, {"approved", "rejected", "failed"})
            if status == "approved":
                self.assertEqual((validator, critic), ("pass", "approve"))

    def test_unknown_outcomes_are_never_better_than_failed(self) -> None:
        for validator, critic in (("pass", "maybe"), ("ok", "approve"), (None, None)):
            self.assertEqual(decide_final_status(validator, critic), "failed")  # type: ignore[arg-type]


class SuccessfulPlanTests(OrchestrationTestCase):
    def test_event_order_and_approved_status(self) -> None:
        result = process_one_command(WORKER_ID)
        self.assertEqual(self.call_names(), SUCCESS_ORDER)
        self.assertEqual(
            [t for t, _, _ in self.events()],
            ["plan_generated", "validator_result", "critic_result"],
        )
        self.assertEqual(
            self.bound("set_command_status"),
            {
                "command_id": COMMAND_ID,
                "expected_status": "received",
                "status": "approved",
                "plan_id": PLAN_ID,
                "client": None,
            },
        )
        self.assertEqual(result.outcome, "plan_checked")
        self.assertEqual(
            (result.validator_outcome, result.critic_outcome, result.final_status),
            ("pass", "approve", "approved"),
        )
        self.assertEqual(result.plan_id, PLAN_ID)

    def test_plan_generated_payload_and_message(self) -> None:
        process_one_command(WORKER_ID)
        event_type, message, payload = self.events()[0]
        self.assertEqual(event_type, "plan_generated")
        self.assertEqual(message, "Plan generated: 1 action(s)")
        self.assertEqual(set(payload), {"plan", "planId", "planHash"})
        self.assertNotIn("outcome", payload)
        self.assertNotIn("error", payload)
        plan = payload["plan"]
        self.assertEqual(set(plan), {"commandId", "planId", "planHash", "actions"})
        self.assertEqual(plan["commandId"], COMMAND_ID)
        self.assertEqual(plan["planId"], PLAN_ID)
        self.assertEqual(payload["planHash"], hash_actions(plan["actions"]))
        self.assertEqual(plan["planHash"], payload["planHash"])
        self.assertEqual(
            plan["actions"],
            [{"actionId": "a1", "type": "set_joint", "target": "_1", "value": 0.25,
              "unit": "rad", "reason": "Command clause 1: set joint _1 to 0.25 rad"}],
        )
        json.dumps(payload, allow_nan=False)  # plain JSON

    def test_plan_id_is_generated_once_and_used_everywhere(self) -> None:
        process_one_command(WORKER_ID)
        self.plan_ids.assert_called_once_with()
        request = self.sent_request()
        self.assertEqual(request.plan_id, PLAN_ID)
        self.assertEqual(self.event("plan_generated")["planId"], PLAN_ID)
        self.assertEqual(self.event("validator_result")["planId"], PLAN_ID)
        self.assertEqual(self.bound("set_command_status")["plan_id"], PLAN_ID)

    def test_critic_request_uses_the_hash_and_id_stored_in_the_audit_events(self) -> None:
        process_one_command(WORKER_ID)
        request = self.sent_request()
        plan_event = self.event("plan_generated")
        critic_event = self.event("critic_result")
        self.assertEqual(request.plan_hash, plan_event["planHash"])
        self.assertEqual(request.plan_hash, critic_event["planHash"])
        self.assertEqual(request.plan_hash, hash_actions(request.actions))
        self.assertEqual(request.actions, plan_event["plan"]["actions"])
        self.assertEqual(request.command_id, COMMAND_ID)

    def test_validator_and_critic_get_equal_but_separate_copies(self) -> None:
        with mock.patch.object(
            command_processor, "validate_plan", wraps=validate_plan
        ) as validator:
            process_one_command(WORKER_ID)
        validator.assert_called_once()
        plan_id, validated = validator.call_args.args
        request = self.sent_request()
        self.assertEqual(plan_id, PLAN_ID)
        self.assertEqual(validated, request.actions)
        self.assertIsNot(validated, request.actions)
        self.assertIsNot(validated[0], request.actions[0])
        self.assertEqual(validated, self.event("plan_generated")["plan"]["actions"])

    def test_hash_comes_from_the_snapshot_not_from_a_later_mutation(self) -> None:
        original = [
            {"actionId": "a1", "type": "set_joint", "target": "_1", "value": 0.25,
             "unit": "rad", "reason": "r"}
        ]
        expected_hash = hash_actions(original)

        def mutating_critic(request: CriticRequest, **_: Any) -> CriticDecision:
            # Change the planner's own dicts AND the critic's copy after hashing.
            original[0]["value"] = 3.0
            original.append({"actionId": "evil"})
            decision = critic_returning("approve")(request)
            request.actions[0]["value"] = 3.0
            return decision

        self.calls.review_plan.side_effect = mutating_critic
        with mock.patch.object(
            command_processor, "plan_command", return_value=PlanResult(actions=tuple(original))
        ), mock.patch.object(command_processor, "validate_plan", wraps=validate_plan) as validator:
            result = process_one_command(WORKER_ID)

        self.assertEqual(result.plan_hash, expected_hash)
        self.assertEqual(self.event("plan_generated")["planHash"], expected_hash)
        self.assertEqual(self.event("plan_generated")["plan"]["actions"][0]["value"], 0.25)
        self.assertEqual(validator.call_args.args[1][0]["value"], 0.25)
        self.assertEqual(self.event("validator_result")["outcome"], "pass")
        # The critic's copy changed after it approved: the final consistency
        # check refuses the approval, and the plan is NOT approved.
        self.assertEqual(result.critic_outcome, "error")
        self.assertEqual(self.status(), "failed")
        self.assertEqual(self.event("critic_result")["outcome"], "error")

    def test_validator_result_payload_matches_the_schema(self) -> None:
        self.use_text(OUT_OF_RANGE_TEXT)
        self.use_critic("reject")
        process_one_command(WORKER_ID)
        payload = self.event("validator_result")
        self.assertEqual(set(payload), {"outcome", "planId", "validatorVersion", "errors"})
        self.assertEqual(payload["outcome"], "fail")
        self.assertEqual(payload["validatorVersion"], VALIDATOR_VERSION)
        self.assertEqual(len(payload["errors"]), 1)
        error = payload["errors"][0]
        self.assertEqual(set(error), {"ruleId", "actionId", "field", "message"})
        self.assertEqual((error["ruleId"], error["actionId"], error["field"]),
                         ("JOINT_LIMIT_EXCEEDED", "a1", "value"))

    def test_critic_result_payload_is_sanitized_and_bounded(self) -> None:
        self.use_critic(
            "reject",
            reason="R" * 5000,
            triggeredRules=["joint_limit_exceeded"] * 50,
            internal={"headers": {"x-critic-secret": "test-secret-not-real"}},
            stack="Traceback ...",
        )
        process_one_command(WORKER_ID)
        payload = self.event("critic_result")
        self.assertEqual(set(payload), {"outcome", "planHash", "response"})
        self.assertLessEqual(
            set(payload["response"]),
            {"commandId", "planId", "planHash", "verdict", "reason", "triggeredRules"},
        )
        self.assertEqual(len(payload["response"]["reason"]), MAX_REASON_LENGTH)
        self.assertLessEqual(len(payload["response"]["triggeredRules"]), 20)
        self.assertNotIn("test-secret-not-real", json.dumps(self.events()))

    def test_critic_error_payload_is_bounded(self) -> None:
        def raising(_request: CriticRequest, **_: Any) -> CriticDecision:
            raise RuntimeError("x" * 10_000)

        self.calls.review_plan.side_effect = raising
        process_one_command(WORKER_ID)
        payload = self.event("critic_result")
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(set(payload), {"outcome", "error"})
        self.assertLessEqual(len(payload["error"]["detail"]), MAX_DETAIL_LENGTH)
        self.assertNotIn("xxxx", payload["error"]["detail"])
        self.assertEqual(self.status(), "failed")

    def test_messages(self) -> None:
        for verdict, status, validator_msg, critic_msg in (
            ("approve", 200, "Validator passed", "Critic approved the plan"),
            ("reject", 200, "Validator passed", "Critic rejected the plan"),
            ("approve", 503, "Validator passed", "Critic unavailable: no verdict"),
        ):
            with self.subTest(verdict=verdict, status=status):
                self.reset_calls()
                self.use_text(VALID_TEXT)
                self.use_critic(verdict, status)
                process_one_command(WORKER_ID)
                messages = {t: m for t, m, _ in self.events()}
                self.assertEqual(messages["validator_result"], validator_msg)
                self.assertEqual(messages["critic_result"], critic_msg)


class PrecedenceThroughTheProcessorTests(OrchestrationTestCase):
    """Every row of the agreed table, driven through process_one_command."""

    def arrange(self, validator: str, critic: str) -> Any:
        self.reset_calls()
        self.use_text(OUT_OF_RANGE_TEXT if validator == "fail" else VALID_TEXT)
        if critic == "approve":
            self.use_critic("approve")
        elif critic == "reject":
            self.use_critic("reject")
        else:
            self.use_critic("approve", status=500)
        if validator == "error":
            return mock.patch.object(
                command_processor, "validate_plan", side_effect=RuntimeError("boom")
            )
        return mock.patch.object(command_processor, "validate_plan", wraps=validate_plan)

    def test_every_row(self) -> None:
        for (validator, critic), expected in FinalStatusPrecedenceTests.TABLE.items():
            if (validator, critic) == ("fail", "approve"):
                continue  # the mock critic cannot approve while the real validator fails
            with self.subTest(validator=validator, critic=critic):
                with self.arrange(validator, critic) as patched_validator:
                    result = process_one_command(WORKER_ID)
                patched_validator.assert_called_once()  # the validator always ran
                self.calls.review_plan.assert_called_once()  # the critic always ran
                self.assertEqual(result.validator_outcome, validator)
                self.assertEqual(result.critic_outcome, critic)
                self.assertEqual(result.final_status, expected)
                self.assertEqual(self.status(), expected)
                written = [t for t, _, _ in self.events()]
                if validator == "error":
                    # No agreed validator_result shape for an error: none written.
                    self.assertEqual(written, ["plan_generated", "critic_result"])
                else:
                    self.assertEqual(written, ["plan_generated", "validator_result", "critic_result"])
                    self.assertEqual(self.event("validator_result")["outcome"], validator)
                self.assertEqual(self.event("critic_result")["outcome"], critic)
                self.assertEqual(self.call_names()[-1], "set_command_status")

    def test_critic_reject_is_never_turned_into_failed(self) -> None:
        with self.arrange("pass", "reject"):
            result = process_one_command(WORKER_ID)
        self.assertEqual(result.final_status, "rejected")
        self.assertEqual(self.event("critic_result")["outcome"], "reject")

    def test_critic_error_is_not_hidden_as_a_reject(self) -> None:
        with self.arrange("pass", "error"):
            result = process_one_command(WORKER_ID)
        self.assertEqual(result.final_status, "failed")
        self.assertEqual(self.event("critic_result")["outcome"], "error")

    def test_validator_returning_garbage_is_an_error_not_a_pass(self) -> None:
        for bad in (None, "pass", object()):
            with self.subTest(bad=type(bad).__name__):
                self.reset_calls()
                self.use_text(VALID_TEXT)
                with mock.patch.object(command_processor, "validate_plan", return_value=bad):
                    result = process_one_command(WORKER_ID)
                self.assertEqual(result.validator_outcome, "error")
                self.assertEqual(self.status(), "failed")

    def test_critic_decision_for_another_plan_is_an_error(self) -> None:
        def other_plan(request: CriticRequest, **_: Any) -> CriticDecision:
            forged = CriticRequest(request.command_id, "another-plan", request.plan_hash, request.actions)
            body = {"commandId": forged.command_id, "planId": forged.plan_id,
                    "planHash": forged.plan_hash, "verdict": "approve", "reason": "ok",
                    "triggeredRules": []}
            return evaluate_critic_response(forged, 200, body)

        for verdict_fn in (other_plan,):
            self.calls.review_plan.side_effect = verdict_fn
            result = process_one_command(WORKER_ID)
            self.assertEqual(result.critic_outcome, "error")
            self.assertEqual(self.status(), "failed")
            self.assertEqual(
                self.event("critic_result")["error"]["detail"],
                "critic decision does not match this plan",
            )

    def test_critic_returning_a_non_decision_is_an_error(self) -> None:
        self.calls.review_plan.side_effect = lambda request, **_: {"verdict": "approve"}
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.critic_outcome, "error")
        self.assertEqual(self.status(), "failed")

    def test_unexpected_critic_error_category_is_kept(self) -> None:
        self.calls.review_plan.side_effect = lambda request, **_: critic_failure("timeout", "slow")
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.critic_outcome, "error")
        self.assertEqual(self.event("critic_result")["error"], {"kind": "timeout", "detail": "slow"})


class PersistenceFailureTests(OrchestrationTestCase):
    def test_plan_event_failure_prevents_both_checks(self) -> None:
        for exc, written in (
            (WriteError("lost"), None),
            (InvalidPayloadError("too big"), False),
            (RuntimeError("x"), False),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.use_text(VALID_TEXT)
                self.calls.append_event.side_effect = exc
                with mock.patch.object(
                    command_processor, "validate_plan", wraps=validate_plan
                ) as validator, self.assertRaises(CommandProcessingError) as ctx:
                    process_one_command(WORKER_ID)
                validator.assert_not_called()
                self.calls.review_plan.assert_not_called()
                self.calls.set_command_status.assert_not_called()
                self.assertEqual(self.call_names(), ["claim_next_command", "append_event"])
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_EVENT_WRITE)
                self.assertIs(err.event_written, written)
                self.assertEqual(err.plan_id, PLAN_ID)
                self.assertEqual(err.command_id, COMMAND_ID)
                self.assertIn("were NOT run", str(err))
                self.assertIn("still 'received' and claimed", str(err))
                self.assertIsNone(err.__cause__)

    def test_validator_event_failure_still_writes_critic_and_skips_status(self) -> None:
        self.calls.append_event.side_effect = [{"id": "e1"}, WriteError("lost"), {"id": "e3"}]
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(
            self.call_names(),
            ["claim_next_command", "append_event", "review_plan", "append_event", "append_event"],
        )
        self.assertEqual(self.calls.append_event.call_count, 3)  # no retry
        self.calls.set_command_status.assert_not_called()
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_VALIDATOR_EVENT_WRITE)
        self.assertIsNone(err.event_written)
        self.assertEqual(err.events_written, ("plan_generated", "critic_result"))
        self.assertEqual(err.intended_status, "approved")
        self.assertEqual(err.plan_id, PLAN_ID)
        self.assertIn("MAY have been written", str(err))
        self.assertIn("status was NOT changed", str(err))

    def test_critic_event_failure_skips_status(self) -> None:
        self.use_text(OUT_OF_RANGE_TEXT)
        self.use_critic("reject")
        self.calls.append_event.side_effect = [{"id": "e1"}, {"id": "e2"}, InvalidPayloadError("bad")]
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(self.calls.append_event.call_count, 3)
        self.calls.set_command_status.assert_not_called()
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_CRITIC_EVENT_WRITE)
        self.assertIs(err.event_written, False)
        self.assertEqual(err.events_written, ("plan_generated", "validator_result"))
        self.assertEqual(err.intended_status, "rejected")

    def test_both_result_events_fail(self) -> None:
        self.calls.append_event.side_effect = [{"id": "e1"}, WriteError("a"), WriteError("b")]
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(self.calls.append_event.call_count, 3)
        self.calls.set_command_status.assert_not_called()
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATOR_EVENT_WRITE)
        self.assertEqual(ctx.exception.events_written, ("plan_generated",))
        self.assertIn("validator_result insert failed", str(ctx.exception))
        self.assertIn("critic_result insert failed", str(ctx.exception))

    def test_status_update_failure_is_surfaced(self) -> None:
        for exc, hint in (
            (StateConflictError("no row"), "no longer 'received'"),
            (WriteError("lost"), "may or may not have been applied"),
            (RuntimeError("x"), "status was not changed"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.use_text(VALID_TEXT)
                self.calls.set_command_status.side_effect = exc
                with self.assertRaises(CommandProcessingError) as ctx:
                    process_one_command(WORKER_ID)
                self.assertEqual(self.call_names(), SUCCESS_ORDER)
                self.assertEqual(self.calls.append_event.call_count, 3)
                self.calls.set_command_status.assert_called_once()
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_STATUS_UPDATE)
                self.assertIs(err.event_written, True)
                self.assertEqual(
                    err.events_written, ("plan_generated", "validator_result", "critic_result")
                )
                self.assertEqual(err.intended_status, "approved")
                self.assertEqual(err.plan_id, PLAN_ID)
                self.assertIn("Do NOT write the events again", str(err))
                self.assertIn(PLAN_ID, str(err))
                self.assertIn(hint, str(err))

    def test_stages_are_distinct(self) -> None:
        stages = {
            command_processor.STAGE_CLAIM, STAGE_EVENT_WRITE, STAGE_VALIDATOR_EVENT_WRITE,
            STAGE_CRITIC_EVENT_WRITE, STAGE_STATUS_UPDATE,
        }
        self.assertEqual(len(stages), 5)


class CriticDeadlineThroughProcessorTests(OrchestrationTestCase):
    """A critic call that runs past its total deadline never approves anything."""

    def use_slow_real_critic(self, verdict: str = "approve") -> None:
        clock = FakeClock()

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            data = json.dumps(
                {
                    "commandId": body["commandId"],
                    "planId": body["planId"],
                    "planHash": hash_actions(body["actions"]),
                    "verdict": verdict,
                    "reason": "ok" if verdict == "approve" else "Action a1: outside the limits.",
                    "triggeredRules": [] if verdict == "approve" else ["joint_limit_exceeded"],
                }
            ).encode()
            # The body trickles in: 0.5 s per 8-byte chunk, well past 10 s.
            return httpx.Response(200, stream=TrackingStream(split(data, 8), clock, step=0.5))

        settings = CriticSettings(url="https://critic.test/api/critic", shared_secret="test-secret-not-real")
        self.calls.review_plan.side_effect = lambda request, **_: REAL_REVIEW_PLAN(
            request, settings=settings, transport=httpx.MockTransport(handler), clock=clock
        )

    def test_late_approval_is_an_error_and_the_command_fails(self) -> None:
        self.use_slow_real_critic("approve")
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.critic_outcome, "error")
        self.assertEqual(result.final_status, "failed")
        self.assertEqual(self.status(), "failed")
        payload = self.event("critic_result")
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["error"]["kind"], "timeout")
        self.assertNotIn("test-secret-not-real", json.dumps(self.events()))

    def test_validator_rejection_still_wins_over_a_critic_timeout(self) -> None:
        self.use_text(OUT_OF_RANGE_TEXT)
        self.use_slow_real_critic("reject")
        result = process_one_command(WORKER_ID)
        self.assertEqual(
            (result.validator_outcome, result.critic_outcome, result.final_status),
            ("fail", "error", "rejected"),
        )

    def use_hanging_real_critic(self, deadline: float = 0.3) -> dict[str, int]:
        """The real client with a critic whose headers never arrive in time."""
        calls = {"requests": 0}

        async def handler(_request: httpx.Request) -> httpx.Response:
            calls["requests"] += 1
            await asyncio.sleep(30)
            return httpx.Response(500)

        settings = CriticSettings(url="https://critic.test/api/critic", shared_secret="test-secret-not-real")
        self.calls.review_plan.side_effect = lambda request, **_: REAL_REVIEW_PLAN(
            request, settings=settings, transport=httpx.MockTransport(handler), deadline_seconds=deadline
        )
        return calls

    def test_validator_pass_and_critic_deadline_cancelled_is_failed(self) -> None:
        calls = self.use_hanging_real_critic()
        start = time.monotonic()
        result = process_one_command(WORKER_ID)
        self.assertLess(time.monotonic() - start, 3.0)  # cancelled, not 30 s
        self.assertEqual(
            (result.validator_outcome, result.critic_outcome, result.final_status),
            ("pass", "error", "failed"),
        )
        self.assertEqual(self.status(), "failed")
        self.assertEqual(self.event("critic_result")["error"]["kind"], "timeout")
        self.assertEqual(calls["requests"], 1)  # no retry
        self.assertEqual(self.call_names(), SUCCESS_ORDER)

    def test_validator_fail_and_critic_deadline_cancelled_is_rejected(self) -> None:
        self.use_text(OUT_OF_RANGE_TEXT)
        calls = self.use_hanging_real_critic()
        result = process_one_command(WORKER_ID)
        self.assertEqual(
            (result.validator_outcome, result.critic_outcome, result.final_status),
            ("fail", "error", "rejected"),
        )
        self.assertEqual(self.status(), "rejected")
        self.assertEqual(calls["requests"], 1)

    def test_approve_with_triggered_rules_through_the_processor_fails(self) -> None:
        self.use_critic("approve", triggeredRules=["joint_limit_exceeded"])
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.critic_outcome, "error")
        self.assertEqual(self.status(), "failed")
        self.assertEqual(
            self.event("critic_result")["error"]["detail"], "an approve verdict has triggered rules"
        )


class RehearsalTests(OrchestrationTestCase):
    """The agreed next live test, offline: 'set joint _6 to -1.0 rad'."""

    def test_validator_rejects_and_critic_rejects(self) -> None:
        self.use_text(OUT_OF_RANGE_TEXT)
        self.use_critic("reject")
        result = process_one_command(WORKER_ID)
        self.assertEqual(self.call_names(), SUCCESS_ORDER)
        self.assertEqual(
            (result.validator_outcome, result.critic_outcome, result.final_status),
            ("fail", "reject", "rejected"),
        )
        self.assertEqual(self.event("plan_generated")["plan"]["actions"][0]["target"], "_6")

    def test_validator_rejects_even_if_the_critic_errors(self) -> None:
        for status in (401, 500):
            with self.subTest(status=status):
                self.reset_calls()
                self.use_text(OUT_OF_RANGE_TEXT)
                self.use_critic("approve", status=status)
                result = process_one_command(WORKER_ID)
                self.assertEqual(result.critic_outcome, "error")
                self.assertEqual(self.status(), "rejected")


if __name__ == "__main__":
    unittest.main()
