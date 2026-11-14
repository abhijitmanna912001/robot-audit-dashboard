"""Unit tests for command_processor. Offline: no network, no database, no .env.

Two layers:

* Most tests replace the three audit_store functions with AUTOSPECCED mocks, so
  a call that does not fit the real signature fails, and argument names are
  checked by binding each recorded call to the real signature.
* ``RealAuditStoreTests`` runs the real audit_store code against the in-memory
  FakeClient from test_audit_store, so the actual queries and rows are checked.

A guard patches ``audit_store._default_client`` to fail, so a test that forgot
to pass a client or a mock cannot build a real Supabase client.
"""

from __future__ import annotations

import ast
import functools
import inspect
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

import audit_store
import command_processor
import critic_client
from audit_store import InvalidPayloadError, StateConflictError, WriteError
from config import CriticSettings
from critic_gate import CriticDecision, CriticRequest, evaluate_critic_response
from plan_hasher import hash_actions
from command_processor import (
    STAGE_CLAIM,
    STAGE_EVENT_WRITE,
    STAGE_STATUS_UPDATE,
    CommandProcessingError,
    ProcessResult,
    main,
    process_one_command,
)
from planner import MAX_DETAIL_LENGTH
from tests.net_guard import NetworkBlocked, block_network
from tests.test_audit_store import FakeClient

WORKER_DIR = Path(__file__).resolve().parents[1]
WORKER_ID = "test-worker"
COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
REHEARSAL_TEXT = "Pick up the cup and move it to the left."
TEST_SECRET = "test-secret-not-real"
STORE_FUNCTIONS = ("claim_next_command", "append_event", "set_command_status")
# The real functions, captured before any test patches them.
REAL = {name: getattr(audit_store, name) for name in STORE_FUNCTIONS}


def command_row(text: object = REHEARSAL_TEXT, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": COMMAND_ID,
        "text": text,
        "status": "received",
        "plan_id": None,
        "claimed_by": WORKER_ID,
        "claimed_at": "2026-11-14T10:00:00+00:00",
        "created_at": "2026-11-14T09:59:00+00:00",
        "updated_at": "2026-11-14T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def critic_returning(
    verdict: str = "approve", status: int = 200, **overrides: Any
) -> Any:
    """An offline critic: builds a response body and judges it with the REAL critic_gate."""

    def review(request: CriticRequest, **_kwargs: Any) -> CriticDecision:
        body: dict[str, Any] = {
            "commandId": request.command_id,
            "planId": request.plan_id,
            "planHash": request.plan_hash,
            "verdict": verdict,
            "reason": "ok" if verdict == "approve" else "Action a1: value is outside the limits.",
            "triggeredRules": [] if verdict == "approve" else ["joint_limit_exceeded"],
        }
        body.update(overrides)
        return evaluate_critic_response(request, status, body)

    return review


def no_real_client(test: unittest.TestCase) -> None:
    guard = mock.patch.object(
        audit_store, "_default_client", side_effect=AssertionError("real client built")
    )
    guard.start()
    test.addCleanup(guard.stop)
    # No real critic request and no critic settings from .env, ever.
    block_network(test)


class ProcessorTestCase(unittest.TestCase):
    """Autospecced audit_store functions, attached to one parent so call order is visible."""

    def setUp(self) -> None:
        no_real_client(self)
        self._patchers: list[Any] = []
        self.addCleanup(self._stop_patchers)
        self.reset_calls()

    def _stop_patchers(self) -> None:
        while self._patchers:
            self._patchers.pop().stop()

    def reset_calls(self) -> None:
        """Fresh autospecced mocks with default return values.

        Rebuilt rather than reset: in Python 3.12, ``reset_mock()`` on a parent
        with autospecced function children raises TypeError.
        """
        self._stop_patchers()
        self.calls = mock.Mock()
        for name in STORE_FUNCTIONS:
            patcher = mock.patch.object(audit_store, name, autospec=True)
            self.calls.attach_mock(patcher.start(), name)
            self._patchers.append(patcher)
        self.calls.claim_next_command.return_value = command_row()
        self.calls.append_event.return_value = {"id": "event-1"}
        self.calls.set_command_status.return_value = command_row(status="failed")
        # The default critic is replaced by an offline fake (approve), attached
        # to the same parent so its place in the call order is visible.
        patcher = mock.patch.object(critic_client, "review_plan", autospec=True)
        self.calls.attach_mock(patcher.start(), "review_plan")
        self._patchers.append(patcher)
        self.calls.review_plan.side_effect = critic_returning("approve")

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls.mock_calls]

    def bound(self, name: str) -> dict[str, Any]:
        """The last call to ``name``, bound to the REAL audit_store signature."""
        return self.bound_all(name)[-1]

    def bound_all(self, name: str) -> list[dict[str, Any]]:
        """Every call to ``name``, in order, bound to the REAL audit_store signature."""
        result = []
        for call in getattr(self.calls, name).call_args_list:
            bound = inspect.signature(REAL[name]).bind(*call.args, **call.kwargs)
            bound.apply_defaults()
            result.append(dict(bound.arguments))
        return result


class NoCommandTests(ProcessorTestCase):
    def test_no_eligible_command(self) -> None:
        self.calls.claim_next_command.return_value = None
        result = process_one_command(WORKER_ID)
        self.assertEqual(result, ProcessResult(outcome="no_command"))
        self.assertEqual(self.call_names(), ["claim_next_command"])
        self.assertEqual(self.bound("claim_next_command"), {"worker_id": WORKER_ID, "client": None})

    def test_claim_error_propagates_and_nothing_else_runs(self) -> None:
        self.calls.claim_next_command.side_effect = WriteError("claim lost")
        with self.assertRaises(WriteError):
            process_one_command(WORKER_ID)
        self.assertEqual(self.call_names(), ["claim_next_command"])

    def test_client_is_passed_through(self) -> None:
        client = object()
        process_one_command(WORKER_ID, client=client)
        for name in STORE_FUNCTIONS:
            self.assertIs(self.bound(name)["client"], client)


class AutospecTests(ProcessorTestCase):
    def test_mocks_reject_calls_that_do_not_fit_the_real_signature(self) -> None:
        with self.assertRaises(TypeError):
            audit_store.set_command_status(COMMAND_ID, expected="received", status="failed")
        with self.assertRaises(TypeError):
            audit_store.append_event(COMMAND_ID, "plan_generated")
        with self.assertRaises(TypeError):
            audit_store.claim_next_command(WORKER_ID, COMMAND_ID)

    def test_real_signatures_are_the_ones_the_processor_relies_on(self) -> None:
        self.assertEqual(
            list(inspect.signature(REAL["set_command_status"]).parameters),
            ["command_id", "expected_status", "status", "plan_id", "client"],
        )
        self.assertEqual(
            list(inspect.signature(REAL["append_event"]).parameters),
            ["command_id", "event_type", "message", "payload", "client"],
        )
        self.assertEqual(
            list(inspect.signature(REAL["claim_next_command"]).parameters),
            ["worker_id", "client"],
        )


class OneCommandPerCallTests(ProcessorTestCase):
    def test_each_call_claims_exactly_once(self) -> None:
        for row in (None, command_row(), command_row("set joint _1 to 0.1 rad")):
            with self.subTest(row=None if row is None else row["text"]):
                self.reset_calls()
                self.calls.claim_next_command.return_value = row
                process_one_command(WORKER_ID)
                self.assertEqual(self.calls.claim_next_command.call_count, 1)
                # plan_generated, validator_result, critic_result, then (dry run)
                # execution_started and execution_completed at most.
                self.assertLessEqual(self.calls.append_event.call_count, 5)
                # received -> approved, approved -> executing, executing -> completed.
                self.assertLessEqual(self.calls.set_command_status.call_count, 3)
                self.assertLessEqual(self.calls.review_plan.call_count, 1)
                command_ids = {c.args[0] for c in self.calls.append_event.call_args_list}
                self.assertLessEqual(len(command_ids), 1)


class UnsupportedCommandTests(ProcessorTestCase):
    def test_writes_plan_generated_error_event(self) -> None:
        result = process_one_command(WORKER_ID)
        self.assertEqual(
            result,
            ProcessResult(
                outcome="planner_failed", command_id=COMMAND_ID, error_kind="unsupported_command"
            ),
        )
        self.calls.append_event.assert_called_once()
        args = self.bound("append_event")
        self.assertEqual(args["command_id"], COMMAND_ID)
        self.assertEqual(args["event_type"], "plan_generated")
        self.assertEqual(args["message"], "Command not understood: nothing executed")
        payload = args["payload"]
        self.assertEqual(set(payload), {"outcome", "error"})
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(set(payload["error"]), {"kind", "detail"})
        self.assertEqual(payload["error"]["kind"], "unsupported_command")
        self.assertIsInstance(payload["error"]["detail"], str)
        self.assertTrue(payload["error"]["detail"].strip())

    def test_status_moves_received_to_failed_after_the_event(self) -> None:
        process_one_command(WORKER_ID)
        self.assertEqual(
            self.call_names(), ["claim_next_command", "append_event", "set_command_status"]
        )
        self.assertEqual(
            self.bound("set_command_status"),
            {
                "command_id": COMMAND_ID,
                "expected_status": "received",
                "status": "failed",
                "plan_id": None,
                "client": None,
            },
        )

    def test_error_detail_is_capped(self) -> None:
        cases = ["set joint _1 to 0.1 " + "q" * 5000, "z" * 10_000, "; ".join(["bad"] * 50)]
        for text in cases:
            with self.subTest(length=len(text)):
                self.reset_calls()
                self.calls.claim_next_command.return_value = command_row(text)
                process_one_command(WORKER_ID)
                detail = self.bound("append_event")["payload"]["error"]["detail"]
                self.assertLessEqual(len(detail), MAX_DETAIL_LENGTH)

    def test_non_string_text_is_planner_error(self) -> None:
        self.calls.claim_next_command.return_value = command_row(None)
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.error_kind, "planner_error")
        args = self.bound("append_event")
        self.assertEqual(args["message"], "Planning failed: nothing executed")
        self.assertEqual(args["payload"]["error"]["kind"], "planner_error")
        self.assertEqual(self.call_names()[-1], "set_command_status")

    def test_non_ascii_command_is_not_understood(self) -> None:
        self.calls.claim_next_command.return_value = command_row("set joint _1 to ١ rad")
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.error_kind, "unsupported_command")
        self.assertEqual(self.call_names()[-1], "set_command_status")


class EventWriteFailureTests(ProcessorTestCase):
    def test_event_failure_prevents_status_transition(self) -> None:
        for exc, written in (
            (WriteError("insert lost"), None),
            (InvalidPayloadError("bad payload"), False),
            (RuntimeError("unexpected"), False),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.calls.append_event.side_effect = exc
                with self.assertRaises(CommandProcessingError) as ctx:
                    process_one_command(WORKER_ID)
                self.calls.set_command_status.assert_not_called()
                self.assertEqual(self.call_names(), ["claim_next_command", "append_event"])
                err = ctx.exception
                self.assertEqual(err.command_id, COMMAND_ID)
                self.assertEqual(err.stage, STAGE_EVENT_WRITE)
                self.assertIs(err.event_written, written)
                self.assertTrue(str(err).startswith("EVENT WRITE FAILED"))
                self.assertIn(COMMAND_ID, str(err))
                self.assertIn("status was NOT changed", str(err))
                self.assertIn("still 'received' and claimed", str(err))
                self.assertIsNone(err.__cause__)
                if written is None:
                    self.assertIn("MAY have been written", str(err))
                else:
                    self.assertIn("No event was written", str(err))

    def test_unexpected_error_text_is_not_echoed(self) -> None:
        self.calls.append_event.side_effect = RuntimeError("sb_secret_LEAK")
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertNotIn("sb_secret_LEAK", str(ctx.exception))


class StatusFailureTests(ProcessorTestCase):
    def test_status_failure_is_surfaced(self) -> None:
        for exc, hint in (
            (StateConflictError("matched no row"), "no longer 'received'"),
            (WriteError("update lost"), "may or may not have been applied"),
            (RuntimeError("unexpected"), "status was not changed"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.calls.set_command_status.side_effect = exc
                with self.assertRaises(CommandProcessingError) as ctx:
                    process_one_command(WORKER_ID)
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_STATUS_UPDATE)
                self.assertIs(err.event_written, True)
                self.assertEqual(err.command_id, COMMAND_ID)
                self.assertTrue(str(err).startswith("STATUS UPDATE FAILED"))
                self.assertIn("plan_generated WAS written", str(err))
                self.assertIn("Do NOT write the event again", str(err))
                self.assertIn(hint, str(err))
                self.calls.append_event.assert_called_once()
                self.calls.set_command_status.assert_called_once()

    def test_stages_are_distinct(self) -> None:
        self.assertEqual(len({STAGE_CLAIM, STAGE_EVENT_WRITE, STAGE_STATUS_UPDATE}), 3)


class SupportedCommandTests(ProcessorTestCase):
    def test_supported_command_is_approved_then_dry_run_completed(self) -> None:
        # A valid plan goes through both checks, is approved, and then runs the
        # DRY-RUN lifecycle only: it ends 'completed' with dryRun events.
        self.calls.claim_next_command.return_value = command_row(
            "set joint _1 to 0.25 rad; set joint _2 to 0.1 rad"
        )
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.outcome, "plan_checked")
        self.assertEqual(result.command_id, COMMAND_ID)
        self.assertEqual(result.action_count, 2)
        self.assertEqual(result.decision_status, "approved")
        self.assertEqual((result.execution, result.final_status), ("completed", "completed"))
        self.assertEqual(
            self.call_names(),
            [
                "claim_next_command",
                "append_event",  # plan_generated
                "review_plan",
                "append_event",  # validator_result
                "append_event",  # critic_result
                "set_command_status",  # received -> approved
                "append_event",  # execution_started
                "set_command_status",  # approved -> executing
                "append_event",  # execution_completed
                "set_command_status",  # executing -> completed
            ],
        )
        self.assertEqual(
            [(c["expected_status"], c["status"]) for c in self.bound_all("set_command_status")],
            [("received", "approved"), ("approved", "executing"), ("executing", "completed")],
        )
        for call in self.bound_all("append_event")[3:]:
            self.assertIs(call["payload"]["dryRun"], True)

    def test_row_without_id_writes_nothing_and_says_the_claim_may_exist(self) -> None:
        self.calls.claim_next_command.return_value = command_row(id=None)
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_CLAIM)
        self.assertIs(err.event_written, False)
        self.assertIn("may already have been claimed", str(err))
        self.assertNotIn("nothing was written", str(err).lower())
        self.assertEqual(self.call_names(), ["claim_next_command"])


class MainTests(ProcessorTestCase):
    def run_main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_refuses_without_confirm_write(self) -> None:
        code, out = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("Nothing was sent", out)
        self.assertEqual(self.calls.mock_calls, [])

    def test_refuses_with_only_a_worker_id(self) -> None:
        code, _ = self.run_main("--worker-id", "someone")
        self.assertEqual(code, 2)
        self.assertEqual(self.calls.mock_calls, [])

    def test_processes_one_with_confirm_write(self) -> None:
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 0)
        self.assertIn("received -> failed", out)
        self.assertEqual(
            self.bound("claim_next_command"),
            {"worker_id": command_processor.DEFAULT_WORKER_ID, "client": None},
        )

    def test_event_failure_exit_code_and_stage(self) -> None:
        self.calls.append_event.side_effect = WriteError("lost")
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 1)
        self.assertIn(f"FAILED at {STAGE_EVENT_WRITE}", out)

    def test_status_failure_exit_code_and_stage(self) -> None:
        self.calls.set_command_status.side_effect = StateConflictError("no row")
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 1)
        self.assertIn(f"FAILED at {STAGE_STATUS_UPDATE}", out)

    def test_valid_command_message_reports_checks_and_dry_run(self) -> None:
        self.calls.claim_next_command.return_value = command_row("set joint _1 to 0.1 rad")
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 0)
        self.assertIn("Validator: pass", out)
        self.assertIn("Critic: approve", out)
        self.assertIn("received -> approved", out)
        self.assertIn("Dry run: completed; status now completed", out)
        self.assertIn("Nothing was sent to a robot", out)

    def test_rejected_command_message(self) -> None:
        self.calls.claim_next_command.return_value = command_row("set joint _6 to -1.0 rad")
        self.calls.review_plan.side_effect = critic_returning("reject")
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 0)
        self.assertIn("Validator: fail", out)
        self.assertIn("Critic: reject", out)
        self.assertIn("received -> rejected", out)

    def test_result_event_failure_exit_code_and_stage(self) -> None:
        self.calls.claim_next_command.return_value = command_row("set joint _1 to 0.1 rad")
        self.calls.append_event.side_effect = [{"id": "e1"}, WriteError("lost"), {"id": "e3"}]
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 1)
        self.assertIn(f"FAILED at {command_processor.STAGE_VALIDATOR_EVENT_WRITE}", out)


class RealAuditStoreTests(unittest.TestCase):
    """The real audit_store functions against the in-memory FakeClient."""

    def setUp(self) -> None:
        no_real_client(self)
        self.client = FakeClient(commands=[command_row(claimed_by=None, claimed_at=None)])

    def ops(self) -> list[tuple[str, str]]:
        return [(c.table, c.op) for c in self.client.calls]

    def command(self) -> dict[str, Any]:
        return self.client.tables["commands"][0]

    def test_rehearsal_command_end_to_end(self) -> None:
        result = process_one_command(WORKER_ID, client=self.client)
        self.assertEqual(result.outcome, "planner_failed")
        self.assertEqual(
            self.ops(),
            [
                ("commands", "select"),
                ("commands", "update"),  # claim
                ("audit_events", "insert"),
                ("commands", "update"),  # received -> failed
            ],
        )
        events = self.client.tables["audit_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["command_id"], COMMAND_ID)
        self.assertEqual(events[0]["event_type"], "plan_generated")
        self.assertEqual(events[0]["message"], "Command not understood: nothing executed")
        self.assertEqual(events[0]["payload"]["outcome"], "error")
        self.assertEqual(events[0]["payload"]["error"]["kind"], "unsupported_command")
        self.assertEqual(self.command()["status"], "failed")
        self.assertEqual(self.command()["claimed_by"], WORKER_ID)
        status_update = self.client.calls[-1]
        self.assertEqual(status_update.values, {"status": "failed"})
        self.assertIn(("eq", "status", "received"), status_update.filters)
        self.assertIn(("eq", "id", COMMAND_ID), status_update.filters)

    def test_insert_failure_leaves_status_received_and_claimed(self) -> None:
        self.client.fail("audit_events", "insert", RuntimeError("network"))
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID, client=self.client)
        self.assertEqual(ctx.exception.stage, STAGE_EVENT_WRITE)
        self.assertIsNone(ctx.exception.event_written)
        self.assertEqual(self.ops()[-1], ("audit_events", "insert"))
        self.assertEqual(len(self.client.calls_of("insert")), 1)  # no retry
        self.assertEqual(self.command()["status"], "received")
        self.assertEqual(self.command()["claimed_by"], WORKER_ID)

    def test_status_conflict_after_event(self) -> None:
        # Someone changes the status between the event insert and the update.
        def change_status(client: FakeClient) -> None:
            client.tables["commands"][0]["status"] = "rejected"

        self.client.before_next("insert", change_status)
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID, client=self.client)
        self.assertEqual(ctx.exception.stage, STAGE_STATUS_UPDATE)
        self.assertEqual(len(self.client.tables["audit_events"]), 1)
        self.assertEqual(self.command()["status"], "rejected")

    def mock_critic(self, verdict: str = "approve", status: int = 200) -> tuple[Any, list[httpx.Request]]:
        """The REAL critic_client on an httpx.MockTransport that answers like the critic."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            rejecting = verdict == "reject"
            return httpx.Response(
                status,
                json={
                    "commandId": body["commandId"],
                    "planId": body["planId"],
                    "planHash": hash_actions(body["actions"]),  # recomputed, like the critic
                    "verdict": verdict,
                    "reason": "Action a1: value is outside the limits." if rejecting else "ok",
                    "triggeredRules": ["joint_limit_exceeded"] if rejecting else [],
                },
            )

        settings = CriticSettings(url="https://critic.test/api/critic", shared_secret=TEST_SECRET)
        transport = httpx.MockTransport(handler)
        return (
            functools.partial(critic_client.review_plan, settings=settings, transport=transport),
            seen,
        )

    def test_valid_command_end_to_end_is_approved_then_dry_run_completed(self) -> None:
        self.client = FakeClient(
            commands=[command_row("set joint _1 to 0.25 rad", claimed_by=None, claimed_at=None)]
        )
        critic, seen = self.mock_critic("approve")
        result = process_one_command(WORKER_ID, client=self.client, critic=critic)
        self.assertEqual(result.decision_status, "approved")
        self.assertEqual(result.final_status, "completed")
        self.assertEqual(
            self.ops(),
            [
                ("commands", "select"),
                ("commands", "update"),  # claim
                ("audit_events", "insert"),  # plan_generated
                ("audit_events", "insert"),  # validator_result
                ("audit_events", "insert"),  # critic_result
                ("commands", "update"),  # received -> approved
                ("audit_events", "insert"),  # execution_started
                ("commands", "update"),  # approved -> executing
                ("audit_events", "insert"),  # execution_completed
                ("commands", "update"),  # executing -> completed
            ],
        )
        events = self.client.tables["audit_events"]
        self.assertEqual(
            [e["event_type"] for e in events],
            [
                "plan_generated",
                "validator_result",
                "critic_result",
                "execution_started",
                "execution_completed",
            ],
        )
        self.assertEqual(events[3]["payload"], {"dryRun": True})
        self.assertIs(events[4]["payload"]["dryRun"], True)
        self.assertEqual(self.command()["status"], "completed")
        self.assertEqual(self.command()["plan_id"], result.plan_id)
        updates = [c for c in self.client.calls if c.table == "commands" and c.op == "update"][1:]
        self.assertEqual(
            [(u.values, ("eq", "status", prev) in u.filters) for u, prev in zip(updates, ("received", "approved", "executing"))],
            [
                ({"status": "approved", "plan_id": result.plan_id}, True),
                ({"status": "executing"}, True),
                ({"status": "completed"}, True),
            ],
        )
        self.assertEqual(len(seen), 1)
        sent = json.loads(seen[0].content)
        self.assertEqual(sent["planId"], events[0]["payload"]["planId"])
        self.assertEqual(sent["planHash"], events[0]["payload"]["planHash"])
        self.assertEqual(events[2]["payload"]["planHash"], sent["planHash"])
        self.assertNotIn(TEST_SECRET, json.dumps(events))

    def test_joint_6_below_limit_rehearsal_is_rejected(self) -> None:
        # The agreed next live test: 'set joint _6 to -1.0 rad' (lower limit -0.174533).
        for critic_verdict, critic_status in (("reject", 200), ("approve", 500)):
            with self.subTest(critic=f"{critic_verdict}/{critic_status}"):
                self.client = FakeClient(
                    commands=[
                        command_row("set joint _6 to -1.0 rad", claimed_by=None, claimed_at=None)
                    ]
                )
                critic, seen = self.mock_critic(critic_verdict, critic_status)
                result = process_one_command(WORKER_ID, client=self.client, critic=critic)
                self.assertEqual(result.validator_outcome, "fail")
                self.assertEqual(result.critic_outcome, "reject" if critic_status == 200 else "error")
                self.assertEqual(result.final_status, "rejected")
                self.assertEqual(self.command()["status"], "rejected")
                self.assertEqual(len(seen), 1)  # the critic ran despite the validator failure
                events = self.client.tables["audit_events"]
                validator = events[1]["payload"]
                self.assertEqual(validator["outcome"], "fail")
                self.assertEqual(
                    [e["ruleId"] for e in validator["errors"]], ["JOINT_LIMIT_EXCEEDED"]
                )
                self.assertEqual(events[0]["payload"]["plan"]["actions"][0]["value"], -1)

    def test_validator_event_failure_leaves_status_received(self) -> None:
        self.client = FakeClient(
            commands=[command_row("set joint _1 to 0.25 rad", claimed_by=None, claimed_at=None)]
        )
        written: list[str] = []

        def fail_second_insert(client: FakeClient) -> None:
            written.append("x")
            if len(written) == 2:
                raise WriteError("insert lost")

        for _ in range(3):
            self.client.before_next("insert", fail_second_insert)
        critic, _ = self.mock_critic("approve")
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID, client=self.client, critic=critic)
        self.assertEqual(ctx.exception.stage, command_processor.STAGE_VALIDATOR_EVENT_WRITE)
        self.assertEqual(len(self.client.calls_of("insert")), 3)  # no retry
        self.assertEqual(self.command()["status"], "received")
        self.assertIsNone(self.command()["plan_id"])

    def test_only_the_oldest_command_is_processed(self) -> None:
        older = command_row(claimed_by=None, claimed_at=None)
        newer = command_row(
            id="newer", created_at="2026-11-14T10:30:00+00:00", claimed_by=None, claimed_at=None
        )
        self.client = FakeClient(commands=[newer, older])
        result = process_one_command(WORKER_ID, client=self.client)
        self.assertEqual(result.command_id, COMMAND_ID)
        rows = {r["id"]: r for r in self.client.tables["commands"]}
        self.assertEqual(rows["newer"]["status"], "received")
        self.assertIsNone(rows["newer"]["claimed_at"])


class NoRobotImportTests(unittest.TestCase):
    FORBIDDEN = {"cyberwave", "cyberwave_client", "discover_cyberwave_assets", "test_cyberwave_connection"}

    @staticmethod
    def imports(module: str) -> set[str]:
        tree = ast.parse((WORKER_DIR / f"{module}.py").read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_no_cyberwave_import_anywhere_in_the_local_import_graph(self) -> None:
        seen: set[str] = set()
        todo = ["planner", "command_processor"]
        while todo:
            module = todo.pop()
            if module in seen:
                continue
            seen.add(module)
            imported = self.imports(module)
            self.assertFalse(imported & self.FORBIDDEN, f"{module} imports {imported & self.FORBIDDEN}")
            todo.extend(m for m in imported if (WORKER_DIR / f"{m}.py").is_file())
        self.assertIn("audit_store", seen)
        self.assertIn("robot_profile", seen)
        for module in ("critic_client", "critic_gate", "plan_snapshot", "safety_validator", "execution_gate"):
            self.assertIn(module, seen)

    def test_importing_the_processor_loads_no_cyberwave_module(self) -> None:
        # A fresh interpreter, so modules imported by other tests do not count.
        code = (
            "import sys, command_processor; "
            "print(sorted(m for m in sys.modules if 'cyberwave' in m))"
        )
        out = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=WORKER_DIR,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
