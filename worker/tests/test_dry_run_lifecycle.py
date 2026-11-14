"""Dry-run lifecycle for approved commands (Abhijit's request). Offline.

Autospecced audit_store functions, an offline critic and an autospecced
``run_dry_run`` are attached to ONE parent mock, so the exact order of every
event write, status update and the dry run itself is asserted. The network
guard is active. Nothing here can reach a robot, Cyberwave or a database.

Contract: docs/schema-draft.md sections 1 (transitions), 2 (event to status),
4 ("Event payloads") and 6 ("Dry-run mode").
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any
from unittest import mock

import command_processor
import dry_run
from audit_store import InvalidPayloadError, StateConflictError, WriteError
from command_processor import (
    DRY_RUN_COMPLETED_MESSAGE,
    DRY_RUN_FAILED_MESSAGE,
    DRY_RUN_STARTED_MESSAGE,
    STAGE_COMPLETED_UPDATE,
    STAGE_EXECUTING_UPDATE,
    STAGE_EXECUTION_COMPLETED_WRITE,
    STAGE_EXECUTION_FAILED_WRITE,
    STAGE_FAILED_UPDATE,
    STAGE_STATUS_UPDATE,
    CommandProcessingError,
    process_one_command,
)
from plan_snapshot import PlanSnapshot
from safety_validator import validate_plan
from tests.test_audit_store import FakeClient
from tests.test_command_processor import WORKER_ID, command_row
from tests.test_plan_orchestration import (
    OUT_OF_RANGE_TEXT,
    PLAN_ID,
    SIX_EVENTS,
    SUCCESS_ORDER,
    VALID_TEXT,
    OrchestrationTestCase,
)

WORKER_DIR = Path(__file__).resolve().parents[1]

# Every call of a fully successful approved command, in order.
LIFECYCLE_ORDER = SUCCESS_ORDER + [
    "append_event",  # execution_started
    "set_command_status",  # approved -> executing
    "run_dry_run",
    "append_event",  # execution_completed
    "set_command_status",  # executing -> completed
]


class DryRunTestCase(OrchestrationTestCase):
    """OrchestrationTestCase plus an autospecced, order-tracked run_dry_run."""

    def reset_calls(self) -> None:
        super().reset_calls()
        patcher = mock.patch.object(
            command_processor, "run_dry_run", autospec=True, side_effect=dry_run.run_dry_run
        )
        self.calls.attach_mock(patcher.start(), "run_dry_run")
        self._patchers.append(patcher)
        self.use_text(VALID_TEXT)

    def store_side_effects(self, *, events: dict[str, Any] | None = None, statuses: dict[tuple[str, str], Any] | None = None) -> None:
        """Make specific event writes or status updates raise, by type/transition."""
        events = events or {}
        statuses = statuses or {}

        def append(command_id: str, event_type: str, message: str, payload: Any = None, **_: Any) -> Any:
            if event_type in events:
                raise events[event_type]
            return {"id": f"event-{event_type}"}

        def set_status(command_id: str, expected_status: str, status: str, plan_id: Any = None, **_: Any) -> Any:
            if (expected_status, status) in statuses:
                raise statuses[(expected_status, status)]
            return command_row(status=status)

        self.calls.append_event.side_effect = append
        self.calls.set_command_status.side_effect = set_status

    def event_types(self) -> list[str]:
        return [t for t, _, _ in self.events()]

    def payload_of(self, event_type: str) -> dict[str, Any]:
        return self.event(event_type)


# --------------------------------------------------------------------------
# Success path
# --------------------------------------------------------------------------


class SuccessPathTests(DryRunTestCase):
    def test_exact_six_event_order_and_call_order(self) -> None:
        result = process_one_command(WORKER_ID)
        # command_received is written by the dashboard route; the worker writes the other five.
        self.assertEqual(self.event_types(), SIX_EVENTS)
        self.assertEqual(self.call_names(), LIFECYCLE_ORDER)
        self.assertEqual(
            self.transitions(),
            [("received", "approved"), ("approved", "executing"), ("executing", "completed")],
        )
        self.assertEqual(
            (result.decision_status, result.execution, result.final_status, result.execution_error),
            ("approved", "completed", "completed", None),
        )

    def test_status_updates_are_conditional_on_the_previous_status(self) -> None:
        process_one_command(WORKER_ID)
        calls = self.bound_all("set_command_status")
        self.assertEqual([c["expected_status"] for c in calls], ["received", "approved", "executing"])
        self.assertEqual(calls[0]["plan_id"], PLAN_ID)  # plan_id set once, with the decision

    def test_execution_started_is_written_before_the_transition_and_the_dry_run(self) -> None:
        process_one_command(WORKER_ID)
        names = self.call_names()
        started = [i for i, c in enumerate(self.calls.mock_calls) if c[0] == "append_event" and c.args[1] == "execution_started"]
        self.assertEqual(len(started), 1)
        executing = names.index("set_command_status", started[0])
        dry = names.index("run_dry_run")
        self.assertLess(started[0], executing)
        self.assertLess(executing, dry)
        self.assertEqual(self.bound_all("set_command_status")[1]["status"], "executing")

    def test_dry_run_receives_the_snapshot_that_was_validated_and_approved(self) -> None:
        created: list[PlanSnapshot] = []
        real_create = PlanSnapshot.create

        def capture(*args: Any, **kwargs: Any) -> PlanSnapshot:
            snapshot = real_create(*args, **kwargs)
            created.append(snapshot)
            return snapshot

        with mock.patch.object(command_processor.PlanSnapshot, "create", side_effect=capture), \
                mock.patch.object(command_processor, "validate_plan", wraps=validate_plan) as validator:
            process_one_command(WORKER_ID)
        self.assertEqual(len(created), 1)
        (snapshot,) = self.calls.run_dry_run.call_args.args
        self.assertIs(snapshot, created[0])  # the very same immutable object
        request = self.sent_request()
        self.assertEqual((snapshot.plan_id, snapshot.plan_hash), (request.plan_id, request.plan_hash))
        self.assertEqual(snapshot.actions(), request.actions)
        self.assertEqual(snapshot.actions(), validator.call_args.args[1])
        self.assertEqual(self.event("critic_result")["planHash"], snapshot.plan_hash)

    def test_command_text_is_parsed_exactly_once(self) -> None:
        with mock.patch.object(
            command_processor, "plan_command", wraps=command_processor.plan_command
        ) as planner:
            process_one_command(WORKER_ID)
        planner.assert_called_once()
        self.calls.run_dry_run.assert_called_once()
        (argument,) = self.calls.run_dry_run.call_args.args
        self.assertIsInstance(argument, PlanSnapshot)  # never the text
        self.assertEqual(self.calls.run_dry_run.call_args.kwargs, {})

    def test_payloads_match_the_documented_schema(self) -> None:
        process_one_command(WORKER_ID)
        messages = {t: m for t, m, _ in self.events()}
        self.assertEqual(self.payload_of("execution_started"), {"dryRun": True})
        completed = self.payload_of("execution_completed")
        self.assertEqual(set(completed), {"dryRun", "summary"})  # { dryRun, summary? }
        self.assertIs(completed["dryRun"], True)
        self.assertIsInstance(completed["summary"], str)
        self.assertLessEqual(len(completed["summary"]), 200)
        self.assertIn("nothing sent to a robot", completed["summary"])
        self.assertEqual(messages["execution_started"], DRY_RUN_STARTED_MESSAGE)
        self.assertEqual(messages["execution_completed"], DRY_RUN_COMPLETED_MESSAGE)
        json.dumps(completed, allow_nan=False)


class ApprovalRequiredTests(DryRunTestCase):
    """The dry run happens only after BOTH checks approved and status is 'approved'."""

    def arrange(self, text: str, verdict: str, status: int = 200, validator_error: bool = False) -> Any:
        self.reset_calls()
        self.use_text(text)
        self.use_critic(verdict, status)
        if validator_error:
            return mock.patch.object(command_processor, "validate_plan", side_effect=RuntimeError("x"))
        return mock.patch.object(command_processor, "validate_plan", wraps=validate_plan)

    def test_no_execution_without_both_approvals(self) -> None:
        cases = {
            "validator fail, critic reject": (OUT_OF_RANGE_TEXT, "reject", 200, False),
            "validator fail, critic error": (OUT_OF_RANGE_TEXT, "approve", 500, False),
            "validator pass, critic reject": (VALID_TEXT, "reject", 200, False),
            "validator pass, critic error": (VALID_TEXT, "approve", 500, False),
            "validator error, critic approve": (VALID_TEXT, "approve", 200, True),
            "validator error, critic reject": (VALID_TEXT, "reject", 200, True),
        }
        for name, (text, verdict, status, validator_error) in cases.items():
            with self.subTest(case=name):
                with self.arrange(text, verdict, status, validator_error):
                    result = process_one_command(WORKER_ID)
                self.calls.run_dry_run.assert_not_called()
                self.assertFalse(any(t.startswith("execution_") for t in self.event_types()))
                self.assertEqual(len(self.transitions()), 1)
                self.assertIn(result.final_status, {"rejected", "failed"})
                self.assertIsNone(result.execution)

    def test_no_execution_if_the_approval_status_was_not_written(self) -> None:
        self.store_side_effects(statuses={("received", "approved"): WriteError("lost")})
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(ctx.exception.stage, STAGE_STATUS_UPDATE)
        self.calls.run_dry_run.assert_not_called()
        self.assertNotIn("execution_started", self.event_types())

    def test_no_execution_if_the_final_consistency_check_disagrees(self) -> None:
        def mutating_critic(request: Any, **_: Any) -> Any:
            from tests.test_command_processor import critic_returning

            decision = critic_returning("approve")(request)
            request.actions[0]["value"] = 3.0  # changed after the approval
            return decision

        self.calls.review_plan.side_effect = mutating_critic
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.decision_status, "failed")
        self.calls.run_dry_run.assert_not_called()


# --------------------------------------------------------------------------
# Failure cases A to E
# --------------------------------------------------------------------------


class ExecutionStartedWriteFailsTests(DryRunTestCase):
    """A. execution_started write fails."""

    def test_no_dry_run_failure_recorded_and_approved_to_failed(self) -> None:
        for exc in (WriteError("lost"), InvalidPayloadError("bad"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.store_side_effects(events={"execution_started": exc})
                result = process_one_command(WORKER_ID)
                self.calls.run_dry_run.assert_not_called()
                self.assertEqual(
                    self.event_types(),
                    ["plan_generated", "validator_result", "critic_result", "execution_started", "execution_failed"],
                )
                self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "failed")])
                self.assertEqual((result.execution, result.final_status), ("failed", "failed"))
                self.assertIn("execution_started could not be recorded", result.execution_error)
                self.assertIn(type(exc).__name__, result.execution_error)
                failed = self.payload_of("execution_failed")
                self.assertEqual(set(failed), {"dryRun", "error"})
                self.assertIs(failed["dryRun"], True)
                self.assertEqual(failed["error"], result.execution_error)


class ExecutingUpdateFailsTests(DryRunTestCase):
    """B. execution_started written, approved -> executing fails."""

    def test_state_conflict_records_failure_and_leaves_status_for_manual_recovery(self) -> None:
        self.store_side_effects(statuses={("approved", "executing"): StateConflictError("no row")})
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_EXECUTING_UPDATE)
        self.calls.run_dry_run.assert_not_called()
        self.assertEqual(self.event_types()[-2:], ["execution_started", "execution_failed"])
        # The status is not known, so no further transition is attempted.
        self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "executing")])
        self.assertIn("Manual recovery needed", str(err))
        self.assertIn("execution_failed was written", str(err))
        self.assertEqual(err.events_written[-2:], ("execution_started", "execution_failed"))

    def test_state_conflict_and_failure_event_also_fails(self) -> None:
        self.store_side_effects(
            events={"execution_failed": WriteError("lost")},
            statuses={("approved", "executing"): StateConflictError("no row")},
        )
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(ctx.exception.stage, STAGE_EXECUTING_UPDATE)
        self.assertIn("NOT recorded in the audit log", str(ctx.exception))
        self.assertNotIn("execution_failed", ctx.exception.events_written)
        self.calls.run_dry_run.assert_not_called()

    def test_update_not_applied_records_failure_and_approved_to_failed(self) -> None:
        for exc in (WriteError("lost"), RuntimeError("refused before sending")):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.store_side_effects(statuses={("approved", "executing"): exc})
                result = process_one_command(WORKER_ID)
                self.calls.run_dry_run.assert_not_called()
                self.assertEqual(self.event_types()[-2:], ["execution_started", "execution_failed"])
                # Conditional on 'approved': can never overwrite 'executing'.
                self.assertEqual(
                    self.transitions(),
                    [("received", "approved"), ("approved", "executing"), ("approved", "failed")],
                )
                self.assertEqual(result.final_status, "failed")

    def test_ambiguous_update_that_did_apply_is_left_for_manual_recovery(self) -> None:
        # approved -> executing actually applied but the response was lost, so
        # the conditional approved -> failed then matches no row.
        self.store_side_effects(
            statuses={
                ("approved", "executing"): WriteError("lost"),
                ("approved", "failed"): StateConflictError("no row"),
            }
        )
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(ctx.exception.stage, STAGE_FAILED_UPDATE)
        self.assertIn("execution_failed WAS written", str(ctx.exception))
        self.assertIn("Do NOT write execution_failed again", str(ctx.exception))
        self.calls.run_dry_run.assert_not_called()


class DryRunFailsTests(DryRunTestCase):
    """C. The dry-run handler fails."""

    def test_failure_recorded_and_executing_to_failed(self) -> None:
        for side_effect in (
            RuntimeError("detail-one-7f3a"),
            dry_run.DryRunError("detail-two-7f3a"),
            ValueError("detail-three-7f3a"),
        ):
            with self.subTest(exc=type(side_effect).__name__):
                self.reset_calls()
                self.calls.run_dry_run.side_effect = side_effect
                result = process_one_command(WORKER_ID)
                self.calls.run_dry_run.assert_called_once()
                self.assertEqual(self.event_types()[-2:], ["execution_started", "execution_failed"])
                self.assertNotIn("execution_completed", self.event_types())
                self.assertEqual(
                    self.transitions(),
                    [("received", "approved"), ("approved", "executing"), ("executing", "failed")],
                )
                self.assertEqual((result.execution, result.final_status), ("failed", "failed"))
                failed = self.payload_of("execution_failed")
                self.assertEqual(set(failed), {"dryRun", "error"})
                self.assertIs(failed["dryRun"], True)
                self.assertIn(type(side_effect).__name__, failed["error"])
                self.assertNotIn("7f3a", failed["error"])  # class name only, never the text
                self.assertLessEqual(len(failed["error"]), 200)
                messages = {t: m for t, m, _ in self.events()}
                self.assertEqual(messages["execution_failed"], DRY_RUN_FAILED_MESSAGE)

    def test_empty_summary_counts_as_a_failure(self) -> None:
        self.calls.run_dry_run.side_effect = None
        self.calls.run_dry_run.return_value = "   "
        result = process_one_command(WORKER_ID)
        self.assertEqual(result.final_status, "failed")
        self.assertNotIn("execution_completed", self.event_types())

    def test_exception_text_is_not_stored(self) -> None:
        self.calls.run_dry_run.side_effect = RuntimeError("sb_secret_LEAK test-secret-not-real")
        process_one_command(WORKER_ID)
        self.assertNotIn("LEAK", json.dumps(self.events()))


class CompletedStatusFailsTests(DryRunTestCase):
    """D. execution_completed written, executing -> completed fails."""

    def test_no_retry_no_duplicate_event_no_rerun(self) -> None:
        for exc in (StateConflictError("no row"), WriteError("lost"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.store_side_effects(statuses={("executing", "completed"): exc})
                with self.assertRaises(CommandProcessingError) as ctx:
                    process_one_command(WORKER_ID)
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_COMPLETED_UPDATE)
                self.assertIs(err.event_written, True)
                self.assertEqual(err.intended_status, "completed")
                self.assertEqual(self.event_types().count("execution_completed"), 1)
                self.assertNotIn("execution_failed", self.event_types())
                self.calls.run_dry_run.assert_called_once()
                self.assertEqual(self.transitions().count(("executing", "completed")), 1)
                self.assertEqual(self.call_names()[-1], "set_command_status")
                self.assertIn("execution_completed WAS written", str(err))
                self.assertIn("do NOT run the dry run again", str(err))
                self.assertIn("Manual recovery needed", str(err))

    def test_completion_event_write_fails(self) -> None:
        self.store_side_effects(events={"execution_completed": WriteError("lost")})
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_EXECUTION_COMPLETED_WRITE)
        self.assertIsNone(err.event_written)
        self.calls.run_dry_run.assert_called_once()
        self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "executing")])
        self.assertNotIn("execution_failed", self.event_types())
        self.assertIn("Do NOT run the dry run again", str(err))


class FailureEventWriteFailsTests(DryRunTestCase):
    """E. execution_failed cannot be persisted."""

    def test_after_a_dry_run_failure(self) -> None:
        for exc, written in ((WriteError("lost"), None), (InvalidPayloadError("bad"), False)):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.calls.run_dry_run.side_effect = RuntimeError("boom")
                self.store_side_effects(events={"execution_failed": exc})
                with self.assertRaises(CommandProcessingError) as ctx:
                    process_one_command(WORKER_ID)
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_EXECUTION_FAILED_WRITE)
                self.assertIs(err.event_written, written)
                self.assertNotIn("execution_failed", err.events_written)
                self.assertIn("EXECUTION FAILURE NOT RECORDED", str(err))
                # Event first: without the event, the status is not changed.
                self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "executing")])
                self.assertIn("should still be 'executing'", str(err))

    def test_after_execution_started_failed(self) -> None:
        self.store_side_effects(
            events={"execution_started": WriteError("a"), "execution_failed": WriteError("b")}
        )
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(ctx.exception.stage, STAGE_EXECUTION_FAILED_WRITE)
        self.assertEqual(self.transitions(), [("received", "approved")])
        self.calls.run_dry_run.assert_not_called()
        self.assertIn("should still be 'approved'", str(ctx.exception))

    def test_failed_status_update_after_the_event(self) -> None:
        self.calls.run_dry_run.side_effect = RuntimeError("boom")
        self.store_side_effects(statuses={("executing", "failed"): StateConflictError("no row")})
        with self.assertRaises(CommandProcessingError) as ctx:
            process_one_command(WORKER_ID)
        self.assertEqual(ctx.exception.stage, STAGE_FAILED_UPDATE)
        self.assertEqual(self.event_types().count("execution_failed"), 1)


# --------------------------------------------------------------------------
# Real audit_store code against the in-memory fake
# --------------------------------------------------------------------------


class RealStoreDryRunTests(DryRunTestCase):
    def test_failed_dry_run_end_to_end_with_the_fake_database(self) -> None:
        client = FakeClient(commands=[command_row(VALID_TEXT, claimed_by=None, claimed_at=None)])
        # Use the real audit_store functions for this test.
        self._stop_patchers()
        with mock.patch.object(command_processor, "run_dry_run", side_effect=RuntimeError("boom")), \
                mock.patch.object(command_processor.critic_client, "review_plan", side_effect=self.calls.review_plan.side_effect):
            result = process_one_command(WORKER_ID, client=client)
        events = [e["event_type"] for e in client.tables["audit_events"]]
        self.assertEqual(events, ["plan_generated", "validator_result", "critic_result", "execution_started", "execution_failed"])
        self.assertEqual(client.tables["commands"][0]["status"], "failed")
        self.assertEqual(result.final_status, "failed")


# --------------------------------------------------------------------------
# No robot, no Cyberwave, no execution switch
# --------------------------------------------------------------------------


def _source(module: str) -> ast.Module:
    return ast.parse((WORKER_DIR / f"{module}.py").read_text(encoding="utf-8"))


class NoRealExecutionPathTests(DryRunTestCase):
    FORBIDDEN_IMPORTS = {
        "cyberwave", "cyberwave_client", "discover_cyberwave_assets", "test_cyberwave_connection",
        "os", "subprocess", "socket", "httpx", "requests", "urllib", "importlib", "config",
    }

    def test_dry_run_module_imports_nothing_that_could_act(self) -> None:
        imported = set()
        for node in ast.walk(_source("dry_run")):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "plan_hasher", "plan_snapshot"})
        self.assertFalse(imported & self.FORBIDDEN_IMPORTS)

    def test_no_environment_or_configuration_switch(self) -> None:
        for module in ("dry_run", "command_processor"):
            names = {n.id for n in ast.walk(_source(module)) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(_source(module)) if isinstance(n, ast.Attribute)}
            with self.subTest(module=module):
                self.assertFalse({"environ", "getenv", "os"} & (names | attrs))

    def test_cli_has_no_execution_option(self) -> None:
        options = {
            arg.value
            for node in ast.walk(_source("command_processor"))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant)
        }
        self.assertEqual(options, {"--confirm-write", "--worker-id"})

    def test_no_injectable_executor(self) -> None:
        self.assertEqual(
            list(inspect.signature(process_one_command).parameters), ["worker_id", "client", "critic"]
        )
        self.assertEqual(list(inspect.signature(dry_run.run_dry_run).parameters), ["snapshot"])

    def test_dry_run_payloads_never_claim_real_execution(self) -> None:
        process_one_command(WORKER_ID)
        for event_type in ("execution_started", "execution_completed"):
            self.assertIs(self.payload_of(event_type)["dryRun"], True)
        source = (WORKER_DIR / "command_processor.py").read_text(encoding="utf-8")
        self.assertNotIn('"dryRun": False', source)
