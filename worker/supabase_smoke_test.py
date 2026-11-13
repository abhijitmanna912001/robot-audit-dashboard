"""Manual Supabase smoke test. Writes two rows once, only with --confirm-write.

Run from the worker directory:

    .venv\\Scripts\\python.exe supabase_smoke_test.py --confirm-write

It is a one-time test. On its first successful run it inserts:

* one row in ``public.commands``: the marker text below, ``status`` left to
  the database default ``received``, ``claimed_by = 'supabase_smoke_test'``
  and ``claimed_at`` set, so a worker looking for unclaimed commands skips it;
* one row in ``public.audit_events``: ``event_type = 'command_received'``
  with ``payload = {"source": "smoke_test"}``.

Nothing else is written: no plan, validator, critic or execution event.

The rows are permanent. ``audit_events`` is append-only, a command with audit
events cannot be deleted (ON DELETE RESTRICT), and this script never updates
or deletes a row.

Two protections must both be satisfied before anything is written:

1. ``--confirm-write`` must be given. Without it the script only checks that
   configuration is present and exits, with no network request.
2. No command with the exact marker text may already exist. The script looks
   for one first (a read-only SELECT). If one exists it refuses to write and
   prints the existing id. There is no override. If the lookup fails or
   returns anything unexpected, the script stops without writing (fail closed).

Exit codes: 0 success or dry run, 1 failure, 2 configuration error,
3 refused because the smoke-test command already exists.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from supabase import create_client

from config import ConfigError, load_supabase_settings, redact
from supabase_client import AuditStore, SupabaseWriteError

# The command text, and also the duplicate marker: a command with exactly this
# text means the smoke test has already written to this database.
SMOKE_TEST_COMMAND_TEXT = "[smoke test] do not process"
SMOKE_TEST_CLAIMANT = "supabase_smoke_test"
SMOKE_TEST_EVENT_TYPE = "command_received"
SMOKE_TEST_EVENT_MESSAGE = "Smoke test: synthetic command received"
SMOKE_TEST_EVENT_PAYLOAD = {"source": "smoke_test"}
# How many existing smoke-test commands to fetch for the report.
DUPLICATE_LOOKUP_LIMIT = 10

EXIT_DUPLICATE_REFUSED = 3


class DuplicateCheckError(Exception):
    """The duplicate lookup failed or returned something unexpected."""


def find_existing_smoke_test_commands(client: Any) -> list[dict[str, Any]]:
    """Return existing commands whose text is exactly the smoke-test marker.

    Read-only: one SELECT on ``public.commands``. Raises DuplicateCheckError on
    any failure or unexpected result, so the caller can refuse to write.
    """
    try:
        response = (
            client.table("commands")
            .select("id,text,created_at")
            .eq("text", SMOKE_TEST_COMMAND_TEXT)
            .order("created_at")
            .limit(DUPLICATE_LOOKUP_LIMIT)
            .execute()
        )
    except Exception as exc:
        raise DuplicateCheckError(f"lookup failed: {type(exc).__name__}: {exc}") from exc

    rows = getattr(response, "data", None)
    if not isinstance(rows, list):
        raise DuplicateCheckError(
            f"lookup returned an unexpected result ({type(rows).__name__}, not a list of rows)"
        )
    for row in rows:
        row_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(row_id, str) or not row_id.strip():
            raise DuplicateCheckError("lookup returned a row without a usable id")
        if row.get("text") != SMOKE_TEST_COMMAND_TEXT:
            # The filter asked for an exact match, so any other text means the
            # query did not do what was intended.
            raise DuplicateCheckError("lookup returned a row that does not match the marker")
    return rows


def run(confirm_write: bool) -> int:
    try:
        settings = load_supabase_settings()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    # Protection 1: nothing below this point runs without --confirm-write.
    if not confirm_write:
        print("Configuration found: SUPABASE_URL and SUPABASE_SECRET_KEY are set.")
        print("No request was sent and nothing was written.")
        print("Re-run with --confirm-write to insert, once and permanently:")
        print("  1 row in public.commands and 1 row in public.audit_events.")
        print("The script refuses to write if the smoke-test command already exists.")
        return 0

    # Protection 2: look for an earlier smoke-test command. Fail closed.
    try:
        lookup_client = create_client(settings.url, settings.secret_key)
        existing = find_existing_smoke_test_commands(lookup_client)
    except Exception as exc:
        print(f"FAILED: duplicate check could not be completed: {redact(exc)}")
        print("No database write performed.")
        return 1

    if existing:
        print(f"Found {len(existing)} existing smoke-test command(s):")
        for row in existing:
            print(f"  command_id: {row['id']}  created_at: {row.get('created_at')}")
        if len(existing) >= DUPLICATE_LOOKUP_LIMIT:
            print(f"  (showing the first {DUPLICATE_LOOKUP_LIMIT}; there may be more)")
        print("\nREFUSED: the smoke test has already written to this database.")
        print("No database write performed. The existing rows are untouched.")
        print("This is a one-time test and has no override.")
        return EXIT_DUPLICATE_REFUSED

    print("No existing smoke-test command found.\n")

    command_id: str | None = None
    try:
        store = AuditStore(settings)

        command = store.insert_command(
            SMOKE_TEST_COMMAND_TEXT, claimed_by=SMOKE_TEST_CLAIMANT
        )
        command_id = command["id"]
        print(f"Inserted command:     id={command_id} status={command['status']}")

        event = store.insert_audit_event(
            command_id=command_id,
            event_type=SMOKE_TEST_EVENT_TYPE,
            message=SMOKE_TEST_EVENT_MESSAGE,
            payload=dict(SMOKE_TEST_EVENT_PAYLOAD),
        )
        event_id = event["id"]
        print(f"Inserted audit event: id={event_id} event_type={event['event_type']}")
    except Exception as exc:
        # SupabaseWriteError is the expected failure. Anything else (for example
        # a response without an id) is reported the same way instead of as a
        # traceback, with secrets redacted.
        expected = isinstance(exc, SupabaseWriteError)
        label = "FAILED" if expected else f"FAILED (unexpected {type(exc).__name__})"
        print(f"{label}: {redact(exc)}")
        if command_id is not None:
            print(f"PARTIAL WRITE: command {command_id} was inserted, but its audit event was not.")
            print("That command is permanent. Later runs will be refused because it exists,")
            print("so this script cannot add the missing event. See worker/README.md.")
        else:
            print("No command id was returned, so it is not known whether a row was written.")
            print("Check the commands table before running this again.")
        return 1

    print("\nRESULT: Supabase smoke test passed.")
    print(f"  command_id:     {command_id}")
    print(f"  audit_event_id: {event_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="Required to send any request or insert rows. Without it nothing is written.",
    )
    args = parser.parse_args()
    return run(confirm_write=args.confirm_write)


if __name__ == "__main__":
    sys.exit(main())
