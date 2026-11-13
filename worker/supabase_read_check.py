"""Read-only Supabase connectivity check. SELECT only, zero rows requested.

Run from the worker directory:

    .venv\\Scripts\\python.exe supabase_read_check.py

For ``commands`` and ``audit_events`` it sends two GET requests each:

    select id                 limit 0   -> is the table reachable with this key?
    select <expected columns> limit 0   -> do the columns in db/001_init.sql exist?

``limit 0`` asks for no rows, so no record is fetched, and whatever comes back
is discarded without being printed. The script prints only True/False results
and sanitised error text. It never prints a credential, a URL, a record, an id,
a response body or a header.

It contains no insert, update, delete, upsert or rpc call, and it does not use
``AuditStore`` (whose methods write).

A passing check proves read access only. Both tables are readable with the
anon key as well, so this does not prove the key is allowed to write.

Exit codes: 0 every check passed, 1 at least one check failed,
2 configuration error.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from postgrest.exceptions import APIError
from supabase import create_client

from config import ConfigError, load_supabase_settings, redact
from schemas import AuditEventRow, CommandRow

# Column names from db/001_init.sql, via the row types that mirror it.
EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "commands": tuple(CommandRow.__annotations__),
    "audit_events": tuple(AuditEventRow.__annotations__),
}
MAX_ERROR_LENGTH = 200


@dataclass(frozen=True)
class TableCheck:
    table: str
    accessible: bool
    schema_ok: bool
    error: str | None = None


def sanitise_error(exc: BaseException, supabase_url: str = "") -> str:
    """Short error text with secrets and the project host removed."""
    if isinstance(exc, APIError):
        text = f"APIError code={exc.code}: {exc.message}"
    else:
        text = f"{type(exc).__name__}: {exc}"
    text = redact(text)
    host = urlsplit(supabase_url).hostname or ""
    if host:
        text = text.replace(supabase_url, "<SUPABASE_URL>").replace(host, "<supabase-host>")
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_LENGTH else text[: MAX_ERROR_LENGTH - 3] + "..."


def _select_zero_rows(client: Any, table: str, columns: str) -> None:
    """Run one zero-row SELECT. The response is deliberately discarded."""
    client.table(table).select(columns).limit(0).execute()


def check_table(client: Any, table: str, supabase_url: str = "") -> TableCheck:
    try:
        _select_zero_rows(client, table, "id")
    except Exception as exc:
        return TableCheck(table, False, False, sanitise_error(exc, supabase_url))
    try:
        _select_zero_rows(client, table, ",".join(EXPECTED_COLUMNS[table]))
    except Exception as exc:
        return TableCheck(table, True, False, sanitise_error(exc, supabase_url))
    return TableCheck(table, True, True)


def run() -> int:
    try:
        settings = load_supabase_settings()
    except ConfigError as exc:
        # ConfigError names the missing variables only, never a value.
        print("Required variables configured: False")
        print(f"CONFIG ERROR: {exc}")
        return 2
    print("Required variables configured: True  (SUPABASE_URL, SUPABASE_SECRET_KEY)")
    print(f"Key has the sb_secret_ prefix: {settings.secret_key.startswith('sb_secret_')}")

    try:
        client = create_client(settings.url, settings.secret_key)
    except Exception as exc:
        print("Client created: False")
        print(f"  error: {sanitise_error(exc, settings.url)}")
        return 1
    print("Client created: True")

    results = [check_table(client, table, settings.url) for table in EXPECTED_COLUMNS]
    for result in results:
        print(f"Table {result.table}:")
        print(f"  accessible (SELECT id, zero rows): {result.accessible}")
        print(f"  expected columns present:          {result.schema_ok}")
        if result.error:
            print(f"  error: {result.error}")

    access_ok = all(result.accessible for result in results)
    schema_ok = all(result.schema_ok for result in results)
    print(f"Authentication and read access succeeded: {access_ok}")
    print(f"Expected schema appears to exist:         {schema_ok}")
    print("No rows were requested and nothing was written.")
    return 0 if access_ok and schema_ok else 1


if __name__ == "__main__":
    sys.exit(run())
