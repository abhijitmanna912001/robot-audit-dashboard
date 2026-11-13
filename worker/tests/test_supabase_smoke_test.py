"""Unit tests for supabase_smoke_test. Offline.

Supabase is replaced with an in-memory fake that both the duplicate lookup and
the real ``AuditStore`` talk to. The fake can only SELECT and INSERT, so any
update, delete or upsert would raise AttributeError and fail the test.

Nothing here creates a real client, reads worker/.env, sends a request or
writes to a database. All values are fake.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import supabase_client
import supabase_smoke_test as smoke
from config import SupabaseSettings

EXPECTED_COMMAND_TEXT = "[smoke test] do not process"
EXPECTED_CLAIMANT = "supabase_smoke_test"
EXPECTED_EVENT_TYPE = "command_received"
EXPECTED_PAYLOAD = {"source": "smoke_test"}

FAKE_URL = "https://fake-project.invalid"
FAKE_SECRET = "sb_secret_fake-key-for-tests"
INIT_SQL = Path(__file__).resolve().parents[2] / "db" / "001_init.sql"


class FakeDatabase:
    """In-memory stand-in for the two tables, with the SQL defaults applied."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {"commands": [], "audit_events": []}
        self.operations: list[tuple[str, str]] = []
        self.sent_rows: dict[str, list[dict[str, Any]]] = {"commands": [], "audit_events": []}
        self.select_error: Exception | None = None
        self.select_result: Any = None  # when set, returned instead of real rows
        self.insert_errors: dict[str, Exception] = {}
        self._next_id = 0

    def new_id(self) -> str:
        self._next_id += 1
        return f"00000000-0000-4000-8000-{self._next_id:012d}"

    def count(self, table: str) -> int:
        return len(self.tables[table])


class FakeQuery:
    """Supports select/eq/order/limit and insert only."""

    def __init__(self, database: FakeDatabase, table: str) -> None:
        self._db = database
        self._table = table
        self._mode: str | None = None
        self._filters: list[tuple[str, Any]] = []
        self._limit: int | None = None
        self._row: dict[str, Any] | None = None

    def select(self, _columns: str) -> "FakeQuery":
        self._mode = "select"
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._filters.append((column, value))
        return self

    def order(self, _column: str) -> "FakeQuery":
        return self

    def limit(self, size: int) -> "FakeQuery":
        self._limit = size
        return self

    def insert(self, row: dict[str, Any]) -> "FakeQuery":
        self._mode = "insert"
        self._row = dict(row)
        return self

    def execute(self) -> Any:
        self._db.operations.append((str(self._mode), self._table))
        if self._mode == "select":
            if self._db.select_error is not None:
                raise self._db.select_error
            if self._db.select_result is not None:
                return self._db.select_result
            rows = [
                row
                for row in self._db.tables[self._table]
                if all(row.get(column) == value for column, value in self._filters)
            ]
            return SimpleNamespace(data=rows[: self._limit])

        assert self._mode == "insert" and self._row is not None
        if self._table in self._db.insert_errors:
            raise self._db.insert_errors[self._table]
        self._db.sent_rows[self._table].append(dict(self._row))
        now = datetime.now(timezone.utc).isoformat()
        stored = {"id": self._db.new_id(), "created_at": now, **self._row}
        if self._table == "commands":
            # Defaults from db/001_init.sql for columns the client did not send.
            stored.setdefault("status", "received")
            stored.setdefault("plan_id", None)
            stored.setdefault("updated_at", now)
        self._db.tables[self._table].append(stored)
        return SimpleNamespace(data=[stored])


class FakeSupabaseClient:
    def __init__(self, database: FakeDatabase) -> None:
        self._db = database

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self._db, name)


class Harness:
    """Runs the script against the fake database, using the real AuditStore."""

    def __init__(self, database: FakeDatabase | None = None) -> None:
        self.db = database or FakeDatabase()
        self.client = FakeSupabaseClient(self.db)
        self.lookup_create_client = mock.Mock(return_value=self.client)
        self.store_create_client = mock.Mock(return_value=self.client)
        self.output = ""

    def _patches(self) -> contextlib.ExitStack:
        settings = SupabaseSettings(url=FAKE_URL, secret_key=FAKE_SECRET)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(smoke, "load_supabase_settings", return_value=settings))
        stack.enter_context(mock.patch.object(smoke, "create_client", self.lookup_create_client))
        stack.enter_context(
            mock.patch.object(supabase_client, "create_client", self.store_create_client)
        )
        # Lets redact() recognise the fake key if an error message echoes it.
        stack.enter_context(mock.patch.dict(os.environ, {"SUPABASE_SECRET_KEY": FAKE_SECRET}))
        return stack

    def run(self, confirm_write: bool = True) -> int:
        buffer = io.StringIO()
        with self._patches(), contextlib.redirect_stdout(buffer):
            code = smoke.run(confirm_write=confirm_write)
        self.output = buffer.getvalue()
        return code

    def run_main(self, argv: list[str]) -> int:
        buffer, errors = io.StringIO(), io.StringIO()
        with (
            self._patches(),
            mock.patch("sys.argv", ["supabase_smoke_test.py", *argv]),
            contextlib.redirect_stdout(buffer),
            contextlib.redirect_stderr(errors),
        ):
            try:
                code = smoke.main()
            except SystemExit as exit_request:
                code = int(exit_request.code or 0)
        self.output = buffer.getvalue() + errors.getvalue()
        return code

    def assert_nothing_written(self, test: unittest.TestCase) -> None:
        test.assertEqual(self.db.count("commands"), 0)
        test.assertEqual(self.db.count("audit_events"), 0)
        test.assertNotIn(("insert", "commands"), self.db.operations)
        test.assertNotIn(("insert", "audit_events"), self.db.operations)


class FirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.started = datetime.now(timezone.utc)
        self.harness = Harness()
        self.code = self.harness.run()
        self.db = self.harness.db

    def test_first_run_succeeds_and_writes_exactly_one_pair(self) -> None:
        self.assertEqual(self.code, 0)
        self.assertEqual(
            self.db.operations,
            [("select", "commands"), ("insert", "commands"), ("insert", "audit_events")],
        )
        self.assertEqual(self.db.count("commands"), 1)
        self.assertEqual(self.db.count("audit_events"), 1)

    def test_command_text_is_exact(self) -> None:
        self.assertEqual(smoke.SMOKE_TEST_COMMAND_TEXT, EXPECTED_COMMAND_TEXT)
        self.assertEqual(self.db.sent_rows["commands"][0]["text"], EXPECTED_COMMAND_TEXT)

    def test_command_row_sends_only_text_and_claim_fields(self) -> None:
        sent = self.db.sent_rows["commands"][0]
        self.assertEqual(set(sent), {"text", "claimed_by", "claimed_at"})

    def test_claim_fields_are_populated(self) -> None:
        sent = self.db.sent_rows["commands"][0]
        self.assertEqual(sent["claimed_by"], EXPECTED_CLAIMANT)
        claimed_at = datetime.fromisoformat(sent["claimed_at"])
        self.assertEqual(claimed_at.utcoffset(), timedelta(0))
        self.assertLessEqual(self.started - timedelta(seconds=1), claimed_at)
        self.assertLessEqual(claimed_at, datetime.now(timezone.utc) + timedelta(seconds=1))

    def test_status_is_received(self) -> None:
        # The script sends no status, so the database default applies.
        self.assertNotIn("status", self.db.sent_rows["commands"][0])
        sql = INIT_SQL.read_text(encoding="utf-8")
        self.assertRegex(sql, r"status\s+text not null default 'received'")
        self.assertEqual(self.db.tables["commands"][0]["status"], "received")
        self.assertIn("status=received", self.harness.output)
        self.assertIsNone(self.db.tables["commands"][0]["plan_id"])

    def test_exactly_one_audit_event_with_the_agreed_content(self) -> None:
        command_id = self.db.tables["commands"][0]["id"]
        self.assertEqual(len(self.db.sent_rows["audit_events"]), 1)
        self.assertEqual(
            self.db.sent_rows["audit_events"][0],
            {
                "command_id": command_id,
                "event_type": EXPECTED_EVENT_TYPE,
                "message": "Smoke test: synthetic command received",
                "payload": EXPECTED_PAYLOAD,
            },
        )

    def test_no_plan_validator_critic_or_execution_event_is_written(self) -> None:
        event_types = [row["event_type"] for row in self.db.tables["audit_events"]]
        self.assertEqual(event_types, ["command_received"])

    def test_both_generated_ids_are_reported(self) -> None:
        self.assertIn(self.db.tables["commands"][0]["id"], self.harness.output)
        self.assertIn(self.db.tables["audit_events"][0]["id"], self.harness.output)

    def test_lookup_used_the_exact_marker(self) -> None:
        # A database holding only a different command must not count as a duplicate.
        database = FakeDatabase()
        database.tables["commands"].append(
            {"id": database.new_id(), "text": "[smoke-test] synthetic command, do not execute"}
        )
        harness = Harness(database)
        self.assertEqual(harness.run(), 0)
        self.assertEqual(database.count("commands"), 2)


class SecondRunTests(unittest.TestCase):
    def test_second_run_is_refused_and_reports_the_existing_id(self) -> None:
        harness = Harness()
        self.assertEqual(harness.run(), 0)
        first_command_id = harness.db.tables["commands"][0]["id"]
        operations_after_first = list(harness.db.operations)

        code = harness.run()

        self.assertEqual(code, smoke.EXIT_DUPLICATE_REFUSED)
        self.assertIn("REFUSED", harness.output)
        self.assertIn(first_command_id, harness.output)
        # Only the lookup ran. No second command, no second event.
        self.assertEqual(
            harness.db.operations[len(operations_after_first):], [("select", "commands")]
        )
        self.assertEqual(harness.db.count("commands"), 1)
        self.assertEqual(harness.db.count("audit_events"), 1)

    def test_refusal_never_builds_the_audit_store(self) -> None:
        harness = Harness()
        harness.run()
        harness.store_create_client.reset_mock()
        harness.run()
        harness.store_create_client.assert_not_called()

    def test_there_is_no_duplicate_override(self) -> None:
        harness = Harness()
        harness.run()
        code = harness.run_main(["--confirm-write", "--allow-duplicate"])
        self.assertEqual(code, 2)  # argparse rejects the unknown flag
        self.assertIn("unrecognized arguments", harness.output)
        self.assertEqual(harness.db.count("commands"), 1)
        source = Path(smoke.__file__).read_text(encoding="utf-8")
        self.assertNotIn("allow_duplicate", source)
        self.assertNotIn("--allow-duplicate", source)

    def test_a_half_finished_first_run_also_blocks_a_retry(self) -> None:
        # Known limitation: the command is written, the event insert fails, and
        # the next run is refused because the command now exists.
        harness = Harness()
        harness.db.insert_errors["audit_events"] = RuntimeError("HTTP 500")
        self.assertEqual(harness.run(), 1)
        self.assertEqual(harness.db.count("commands"), 1)
        self.assertEqual(harness.db.count("audit_events"), 0)
        # The partial write is reported with the id of the orphaned command.
        self.assertIn("PARTIAL WRITE", harness.output)
        self.assertIn(harness.db.tables["commands"][0]["id"], harness.output)
        self.assertNotIn("RESULT: Supabase smoke test passed", harness.output)

        harness.db.insert_errors.clear()
        self.assertEqual(harness.run(), smoke.EXIT_DUPLICATE_REFUSED)
        self.assertEqual(harness.db.count("audit_events"), 0)


class WriteFailureTests(unittest.TestCase):
    def test_command_insert_failure_writes_nothing_and_reports_it(self) -> None:
        harness = Harness()
        harness.db.insert_errors["commands"] = RuntimeError("permission denied")
        self.assertEqual(harness.run(), 1)
        # The command insert was attempted once and failed, so no row exists.
        self.assertEqual(harness.db.operations, [("select", "commands"), ("insert", "commands")])
        self.assertEqual(harness.db.count("commands"), 0)
        self.assertEqual(harness.db.count("audit_events"), 0)
        self.assertIn("FAILED", harness.output)
        self.assertNotIn("PARTIAL WRITE", harness.output)
        # The event insert was never attempted.
        self.assertNotIn(("insert", "audit_events"), harness.db.operations)

    def test_unexpected_error_is_reported_without_a_traceback(self) -> None:
        # A response row without an id: not a SupabaseWriteError, and it used to
        # escape as a KeyError traceback.
        harness = Harness()
        store = mock.Mock()
        store.insert_command.return_value = {"status": "received"}
        with mock.patch.object(smoke, "AuditStore", return_value=store):
            code = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("FAILED (unexpected KeyError)", harness.output)
        self.assertIn("not known whether a row was written", harness.output)
        store.insert_audit_event.assert_not_called()

    def test_secret_in_a_write_error_is_redacted(self) -> None:
        harness = Harness()
        harness.db.insert_errors["commands"] = RuntimeError(f"rejected, key {FAKE_SECRET}")
        self.assertEqual(harness.run(), 1)
        self.assertNotIn(FAKE_SECRET, harness.output)

    def test_script_never_updates_deletes_or_touches_cyberwave(self) -> None:
        source = Path(smoke.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertEqual(calls & {"update", "delete", "upsert", "rpc"}, set())
        imported = {
            (node.module if isinstance(node, ast.ImportFrom) else alias.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [None])
        }
        self.assertEqual(imported, {"__future__", "argparse", "sys", "typing", "supabase", "config", "supabase_client"})
        for module in ("supabase_client.py", "supabase_read_check.py"):
            other = (Path(smoke.__file__).parent / module).read_text(encoding="utf-8")
            self.assertNotIn("cyberwave", other.lower())


class ConfirmWriteTests(unittest.TestCase):
    def test_without_confirm_write_no_client_is_created_and_nothing_is_sent(self) -> None:
        harness = Harness()
        code = harness.run(confirm_write=False)
        self.assertEqual(code, 0)
        harness.lookup_create_client.assert_not_called()
        harness.store_create_client.assert_not_called()
        self.assertEqual(harness.db.operations, [])
        harness.assert_nothing_written(self)
        self.assertIn("No request was sent and nothing was written", harness.output)

    def test_main_without_the_flag_does_not_write(self) -> None:
        harness = Harness()
        self.assertEqual(harness.run_main([]), 0)
        harness.lookup_create_client.assert_not_called()
        self.assertEqual(harness.db.operations, [])

    def test_main_with_the_flag_writes_once(self) -> None:
        harness = Harness()
        self.assertEqual(harness.run_main(["--confirm-write"]), 0)
        self.assertEqual(harness.db.count("commands"), 1)
        self.assertEqual(harness.db.count("audit_events"), 1)


class FailClosedTests(unittest.TestCase):
    def assert_fails_closed(self, harness: Harness) -> None:
        code = harness.run()
        self.assertEqual(code, 1)
        self.assertIn("duplicate check could not be completed", harness.output)
        self.assertIn("No database write performed", harness.output)
        harness.assert_nothing_written(self)
        harness.store_create_client.assert_not_called()

    def test_lookup_error_stops_before_any_insert(self) -> None:
        harness = Harness()
        harness.db.select_error = RuntimeError("connection refused")
        self.assert_fails_closed(harness)

    def test_client_creation_error_stops_before_any_insert(self) -> None:
        harness = Harness()
        harness.lookup_create_client.side_effect = RuntimeError("Invalid URL")
        self.assert_fails_closed(harness)
        self.assertEqual(harness.db.operations, [])

    def test_unexpected_result_shapes_stop_before_any_insert(self) -> None:
        marker = EXPECTED_COMMAND_TEXT
        unexpected = {
            "data is None": SimpleNamespace(data=None),
            "data is a dict": SimpleNamespace(data={"id": "x"}),
            "no data attribute": SimpleNamespace(),
            "row is not a dict": SimpleNamespace(data=["not-a-row"]),
            "row has no id": SimpleNamespace(data=[{"text": marker}]),
            "row id is blank": SimpleNamespace(data=[{"id": "  ", "text": marker}]),
            "row id is not a string": SimpleNamespace(data=[{"id": 42, "text": marker}]),
            "row text is not the marker": SimpleNamespace(
                data=[{"id": "some-id", "text": "some other command"}]
            ),
        }
        for label, result in unexpected.items():
            with self.subTest(case=label):
                harness = Harness()
                harness.db.select_result = result
                self.assert_fails_closed(harness)

    def test_secret_in_an_error_message_is_redacted(self) -> None:
        harness = Harness()
        harness.db.select_error = RuntimeError(f"request failed, header had {FAKE_SECRET}")
        self.assertEqual(harness.run(), 1)
        self.assertNotIn(FAKE_SECRET, harness.output)
        self.assertIn("***", harness.output)


class OutputTests(unittest.TestCase):
    def test_no_credentials_or_url_are_printed_on_any_path(self) -> None:
        harness = Harness()
        outputs = []
        harness.run(confirm_write=False)
        outputs.append(harness.output)
        harness.run()  # first run
        outputs.append(harness.output)
        harness.run()  # refused second run
        outputs.append(harness.output)
        for output in outputs:
            self.assertNotIn(FAKE_SECRET, output)
            self.assertNotIn(FAKE_URL, output)


class RealQueryEncodingTests(unittest.TestCase):
    def test_marker_reaches_postgrest_as_an_exact_eq_filter(self) -> None:
        # Builds the real PostgREST query object but never calls execute(), so
        # no request is sent. This guards against the client quoting or
        # altering the marker, which would make the duplicate check miss.
        from postgrest import SyncPostgrestClient

        client = SyncPostgrestClient("http://localhost.invalid/rest/v1")
        query = (
            client.from_("commands")
            .select("id,text,created_at")
            .eq("text", smoke.SMOKE_TEST_COMMAND_TEXT)
            .order("created_at")
            .limit(smoke.DUPLICATE_LOOKUP_LIMIT)
        )
        params = dict(query.request.params)
        self.assertEqual(params["text"], f"eq.{EXPECTED_COMMAND_TEXT}")
        self.assertEqual(str(query.request.http_method).upper().split(".")[-1], "GET")
        self.assertTrue(re.fullmatch(r"\[smoke test\] do not process", EXPECTED_COMMAND_TEXT))


if __name__ == "__main__":
    unittest.main()
