"""Supabase access for the worker: the ``commands`` and ``audit_events`` tables.

Column names and constraints follow ``db/001_init.sql``. Uses the backend
secret key (SUPABASE_SECRET_KEY), which bypasses RLS, so this module must only
ever run server-side.

``audit_events`` is append-only (a database trigger blocks UPDATE and DELETE),
so this module offers no way to change or remove an event.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from postgrest.exceptions import APIError
from supabase import Client, create_client

from config import SupabaseSettings
from schemas import AUDIT_EVENT_TYPES, AuditEventRow, CommandRow, JsonValue


class SupabaseWriteError(Exception):
    """An insert was rejected, failed, or returned no row."""


class AuditStore:
    """Thin, reusable wrapper over the Supabase client."""

    def __init__(self, settings: SupabaseSettings) -> None:
        try:
            self._client: Client = create_client(settings.url, settings.secret_key)
        except Exception as exc:
            raise SupabaseWriteError(f"Could not create Supabase client: {exc}") from exc

    def insert_command(self, text: str, *, claimed_by: str | None = None) -> CommandRow:
        """Insert a command row. ``status`` defaults to 'received' in the database.

        Pass ``claimed_by`` to insert the row already claimed, so a worker
        looking for unclaimed 'received' commands will skip it.
        """
        if not text.strip():
            raise ValueError("Command text must not be empty.")
        row: dict[str, Any] = {"text": text}
        if claimed_by is not None:
            row["claimed_by"] = claimed_by
            row["claimed_at"] = datetime.now(timezone.utc).isoformat()
        return cast(CommandRow, self._insert_one("commands", row))

    def insert_audit_event(
        self,
        command_id: str,
        event_type: str,
        message: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> AuditEventRow:
        """Append one audit event for a command."""
        if event_type not in AUDIT_EVENT_TYPES:
            raise ValueError(
                f"Unknown event_type {event_type!r}. Allowed: {sorted(AUDIT_EVENT_TYPES)}"
            )
        row: dict[str, Any] = {
            "command_id": command_id,
            "event_type": event_type,
            "message": message,
            "payload": payload if payload is not None else {},
        }
        return cast(AuditEventRow, self._insert_one("audit_events", row))

    def _insert_one(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.table(table).insert(row).execute()
        except APIError as exc:
            raise SupabaseWriteError(
                f"Insert into {table} rejected: {exc.message} (code {exc.code})"
            ) from exc
        except Exception as exc:
            raise SupabaseWriteError(f"Insert into {table} failed: {exc}") from exc
        if not response.data:
            raise SupabaseWriteError(f"Insert into {table} returned no row.")
        return cast(dict[str, Any], response.data[0])
