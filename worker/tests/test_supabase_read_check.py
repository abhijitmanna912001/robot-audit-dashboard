"""Unit tests for supabase_read_check. Offline.

The Supabase client is a fake that can only build ``select(...).limit(...)``.
It has no insert, update, delete, upsert or rpc, so any write attempt would
raise AttributeError. No real client, no network, no credentials.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import supabase_read_check as check
from config import ConfigError, SupabaseSettings

FAKE_URL = "https://fake-project.invalid"
FAKE_SECRET = "sb_secret_fake-key-for-tests"
SECRET_ROW = {"id": "row-id-that-must-never-be-printed", "text": "private command text"}
INIT_SQL = Path(__file__).resolve().parents[2] / "db" / "001_init.sql"


class FakeSelectOnlyClient:
    """Records every query. ``errors`` maps (table, columns) to an exception."""

    def __init__(self, errors: dict[tuple[str, str], Exception] | None = None) -> None:
        self.errors = errors or {}
        self.queries: list[tuple[str, str, int | None]] = []

    def table(self, name: str) -> "FakeSelectQuery":
        return FakeSelectQuery(self, name)


class FakeSelectQuery:
    def __init__(self, client: FakeSelectOnlyClient, table: str) -> None:
        self._client, self._table = client, table
        self._columns = ""
        self._limit: int | None = None

    def select(self, columns: str) -> "FakeSelectQuery":
        self._columns = columns
        return self

    def limit(self, size: int) -> "FakeSelectQuery":
        self._limit = size
        return self

    def execute(self) -> Any:
        self._client.queries.append((self._table, self._columns, self._limit))
        error = self._client.errors.get((self._table, self._columns))
        if error is not None:
            raise error
        # A misbehaving server that returns a row anyway: it must not be shown.
        return SimpleNamespace(data=[SECRET_ROW])


def run_with(client: Any = None, create_error: Exception | None = None) -> tuple[int, str]:
    settings = SupabaseSettings(url=FAKE_URL, secret_key=FAKE_SECRET)
    buffer = io.StringIO()
    with (
        mock.patch.object(check, "load_supabase_settings", return_value=settings),
        mock.patch.object(
            check, "create_client", mock.Mock(side_effect=create_error, return_value=client)
        ),
        mock.patch.dict(os.environ, {"SUPABASE_SECRET_KEY": FAKE_SECRET}),
        contextlib.redirect_stdout(buffer),
    ):
        code = check.run()
    return code, buffer.getvalue()


class ReadCheckTests(unittest.TestCase):
    def test_all_checks_pass_with_zero_row_selects_only(self) -> None:
        client = FakeSelectOnlyClient()
        code, output = run_with(client)
        self.assertEqual(code, 0)
        self.assertEqual(len(client.queries), 4)
        self.assertTrue(all(limit == 0 for _, _, limit in client.queries))
        self.assertEqual({table for table, _, _ in client.queries}, {"commands", "audit_events"})
        self.assertIn("Authentication and read access succeeded: True", output)
        self.assertIn("Expected schema appears to exist:         True", output)

    def test_expected_columns_match_the_sql_file(self) -> None:
        sql = INIT_SQL.read_text(encoding="utf-8")
        for table, columns in check.EXPECTED_COLUMNS.items():
            body = re.search(rf"create table public\.{table} \((.*?)\n\);", sql, flags=re.S).group(1)
            sql_columns = re.findall(r"^\s{2}([a-z_]+)\s+(?:uuid|text|timestamptz|jsonb)", body, flags=re.M)
            self.assertEqual(sorted(columns), sorted(sql_columns), table)

    def test_returned_data_is_never_printed(self) -> None:
        _code, output = run_with(FakeSelectOnlyClient())
        self.assertNotIn(SECRET_ROW["id"], output)
        self.assertNotIn(SECRET_ROW["text"], output)

    def test_credentials_and_url_are_never_printed(self) -> None:
        error = RuntimeError(f"GET {FAKE_URL}/rest/v1/commands failed, key {FAKE_SECRET}")
        client = FakeSelectOnlyClient({("commands", "id"): error})
        code, output = run_with(client)
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_SECRET, output)
        self.assertNotIn(FAKE_URL, output)
        self.assertNotIn("fake-project.invalid", output)
        self.assertIn("accessible (SELECT id, zero rows): False", output)

    def test_missing_table_is_reported_and_other_table_still_checked(self) -> None:
        client = FakeSelectOnlyClient({("audit_events", "id"): RuntimeError("table not found")})
        code, output = run_with(client)
        self.assertEqual(code, 1)
        self.assertIn("Authentication and read access succeeded: False", output)
        self.assertIn("table not found", output)
        self.assertIn(("commands", "id", 0), client.queries)

    def test_missing_column_is_a_schema_failure_not_an_access_failure(self) -> None:
        columns = ",".join(check.EXPECTED_COLUMNS["commands"])
        client = FakeSelectOnlyClient({("commands", columns): RuntimeError("column does not exist")})
        code, output = run_with(client)
        self.assertEqual(code, 1)
        self.assertIn("Authentication and read access succeeded: True", output)
        self.assertIn("Expected schema appears to exist:         False", output)

    def test_client_creation_failure_is_reported(self) -> None:
        code, output = run_with(create_error=RuntimeError("Invalid URL"))
        self.assertEqual(code, 1)
        self.assertIn("Client created: False", output)

    def test_missing_configuration_reports_false_and_no_values(self) -> None:
        buffer = io.StringIO()
        with (
            mock.patch.object(
                check, "load_supabase_settings", side_effect=ConfigError("Missing SUPABASE_URL")
            ),
            mock.patch.object(check, "create_client") as create_client,
            contextlib.redirect_stdout(buffer),
        ):
            code = check.run()
        self.assertEqual(code, 2)
        self.assertIn("Required variables configured: False", buffer.getvalue())
        create_client.assert_not_called()

    def test_long_errors_are_truncated(self) -> None:
        text = check.sanitise_error(RuntimeError("x" * 1000))
        self.assertLessEqual(len(text), check.MAX_ERROR_LENGTH)

    def test_script_source_has_no_write_calls(self) -> None:
        tree = ast.parse(Path(check.__file__).read_text(encoding="utf-8"))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertEqual(calls & {"insert", "update", "delete", "upsert", "rpc"}, set())
        self.assertNotIn("AuditStore", Path(check.__file__).read_text(encoding="utf-8").split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()
