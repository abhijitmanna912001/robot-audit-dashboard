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
import inspect
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import audit_store
import command_processor
from audit_store import InvalidPayloadError, StateConflictError, WriteError
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
from tests.test_audit_store import FakeClient

WORKER_DIR = Path(__file__).resolve().parents[1]
WORKER_ID = "test-worker"
COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
REHEARSAL_TEXT = "Pick up the cup and move it to the left."
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


def no_real_client(test: unittest.TestCase) -> None:
    guard = mock.patch.object(
        audit_store, "_default_client", side_effect=AssertionError("real client built")
    )
    guard.start()
    test.addCleanup(guard.stop)


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

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls.mock_calls]

    def bound(self, name: str) -> dict[str, Any]:
        """The last call to ``name``, bound to the REAL audit_store signature."""
        call = getattr(self.calls, name).call_args
        bound = inspect.signature(REAL[name]).bind(*call.args, **call.kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)


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
                self.assertLessEqual(self.calls.append_event.call_count, 1)
                self.assertLessEqual(self.calls.set_command_status.call_count, 1)


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
    def test_supported_command_writes_nothing_and_is_not_failed(self) -> None:
        self.calls.claim_next_command.return_value = command_row(
            "set joint _1 to 0.25 rad; set joint _2 to 0.1 rad"
        )
        result = process_one_command(WORKER_ID)
        self.assertEqual(
            result,
            ProcessResult(
                outcome="planned_not_processed", command_id=COMMAND_ID, action_count=2
            ),
        )
        self.assertEqual(self.call_names(), ["claim_next_command"])

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

    def test_valid_command_message_says_not_failed(self) -> None:
        self.calls.claim_next_command.return_value = command_row("set joint _1 to 0.1 rad")
        code, out = self.run_main("--confirm-write")
        self.assertEqual(code, 0)
        self.assertIn("NOT failed", out)
        self.assertIn("stranded", out)


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

    def test_valid_command_is_claimed_but_nothing_else(self) -> None:
        self.client = FakeClient(
            commands=[command_row("set joint _1 to 0.25 rad", claimed_by=None, claimed_at=None)]
        )
        result = process_one_command(WORKER_ID, client=self.client)
        self.assertEqual(result.outcome, "planned_not_processed")
        self.assertEqual(self.ops(), [("commands", "select"), ("commands", "update")])
        self.assertEqual(self.client.tables["audit_events"], [])
        self.assertEqual(self.command()["status"], "received")

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
