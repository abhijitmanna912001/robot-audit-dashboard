"""Canonical writer for the ``commands`` and ``audit_events`` tables.

Three operations, with the rules of docs/schema-draft.md enforced in code
BEFORE the database is touched:

- ``claim_next_command``  claim the oldest unclaimed ``received`` command
- ``append_event``        append one audit event (exactly one insert, no retry)
- ``set_command_status``  move a command along the lifecycle (conditional update)

Column names and CHECK values follow ``db/001_init.sql``.

Ground rules
------------
- Every function takes an optional ``client`` (keyword-only) so tests can pass
  a fake. Without one, a Supabase client is built the way ``supabase_client.py``
  builds it, from ``config.load_supabase_settings()``. The ``supabase`` package
  is imported only then, so injecting a client never needs it installed.
- No automatic retries, anywhere. ``audit_events`` rows can never be deleted, so
  a blind retry after an ambiguous failure could write a duplicate for good.
- Error messages never carry keys, raw response bodies, or payload contents.
  Text taken from third-party exceptions goes through ``config.redact`` and is
  length-capped, and the original exception is not chained.
- This module is a state-machine guard, not the fail-closed gate. It does not
  check that the validator and critic passed before ``approved``/``executing``,
  and it does not check that the matching audit event exists. That is the
  caller's job (see ``execution_gate.py``). Write the event first, then the
  status: the two calls are NOT atomic.
- Single-writer rule (docs/schema-draft.md section 1): ``command_received`` is
  written by the dashboard route handler. ``append_event`` accepts it because
  the agreed interface lists it, but a worker should not write it.
- Both tables are readable with the public anon key. Never put secrets or
  personal data in ``message``, ``payload`` or ``worker_id``.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, cast

from config import load_supabase_settings, redact
from schemas import AUDIT_EVENT_TYPES, COMMAND_STATUSES, AuditEventRow, CommandRow

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AuditStoreError",
    "CLAIM_CANDIDATE_LIMIT",
    "InvalidEventTypeError",
    "InvalidPayloadError",
    "InvalidTransitionError",
    "MAX_ERROR_TEXT",
    "MAX_MESSAGE_CHARS",
    "MAX_PAYLOAD_BYTES",
    "StateConflictError",
    "WriteError",
    "append_event",
    "claim_next_command",
    "set_command_status",
]

# How many candidates one claim call tries, oldest first.
CLAIM_CANDIDATE_LIMIT = 5
# message: characters. payload: bytes of compact UTF-8 JSON. Module rules, not
# SQL rules: the database has no such limits.
MAX_MESSAGE_CHARS = 500
MAX_PAYLOAD_BYTES = 20_000
# Cap on text taken from a third-party exception.
MAX_ERROR_TEXT = 200
# Deeper payloads are rejected (also catches circular references).
_MAX_PAYLOAD_DEPTH = 50

# Mirrors the lifecycle in docs/schema-draft.md section 1, plus received ->
# failed and approved -> failed (agreed in the interface, not in the doc yet).
ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "received": frozenset({"approved", "rejected", "failed"}),
        "approved": frozenset({"executing", "failed"}),
        "executing": frozenset({"completed", "failed"}),
        "rejected": frozenset(),
        "completed": frozenset(),
        "failed": frozenset(),
    }
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class AuditStoreError(Exception):
    """Base class for every error this module raises on purpose."""


class InvalidEventTypeError(AuditStoreError):
    """``event_type`` is not one of the seven allowed values. Nothing was sent."""


class InvalidTransitionError(AuditStoreError):
    """The requested status change is not allowed. Nothing was sent."""


class InvalidPayloadError(AuditStoreError):
    """``message`` or ``payload`` was rejected. Nothing was sent."""


class StateConflictError(AuditStoreError):
    """A conditional status update matched no row.

    Either the command's status changed since it was read, or the command does
    not exist. The two cannot be told apart.
    """


class WriteError(AuditStoreError):
    """The database call failed, or returned something unusable.

    For an insert or an update the write MAY have happened (for example the
    response was lost). Do not blindly repeat it.
    """


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _safe_text(exc: BaseException) -> str:
    """Describe a third-party exception without leaking.

    A PostgREST error is reduced to its ``code`` and ``message`` (not
    ``details`` or ``hint``, which can echo row data). Anything else becomes its
    class name plus its text. The result is redacted FIRST and capped AFTER, so
    a secret cut by the cap cannot survive as a fragment.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    try:
        if isinstance(code, str) or isinstance(message, str):
            text = f"{type(exc).__name__} code={code} message={message}"
        else:
            text = f"{type(exc).__name__}: {exc}"
    except Exception:
        text = type(exc).__name__
    return redact(text)[:MAX_ERROR_TEXT]


def _short(value: object) -> str:
    """A short, redacted repr of a caller-supplied value, for error messages."""
    return redact(repr(value))[:40]


def _default_client() -> Any:
    """Build the Supabase client the way ``supabase_client.py`` does."""
    try:
        settings = load_supabase_settings()
        from supabase import create_client  # lazy: see module docstring

        return create_client(settings.url, settings.secret_key)
    except Exception as exc:
        raise WriteError(
            f"Could not create Supabase client: {_safe_text(exc)}"
        ) from None


def _get_client(client: Any | None) -> Any:
    return client if client is not None else _default_client()


def _require_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _rows(response: object, what: str) -> list[dict[str, Any]]:
    """The ``data`` list of a response, or WriteError if it is not a list of dicts."""
    data = getattr(response, "data", None)
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise WriteError(f"{what}: unexpected response from the database.")
    return cast(list[dict[str, Any]], data)


def _check_json_value(value: object, depth: int) -> None:
    """Allow only plain JSON: dict (str keys), list, str, bool, int, finite float, None.

    Stricter than ``json.dumps``, which would silently turn tuples into lists
    and non-string keys into strings. What is measured and stored is what the
    caller passed.
    """
    if depth > _MAX_PAYLOAD_DEPTH:
        raise InvalidPayloadError(
            f"payload is nested deeper than {_MAX_PAYLOAD_DEPTH} levels."
        )
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidPayloadError("payload contains NaN or Infinity.")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise InvalidPayloadError("payload keys must all be strings.")
            _check_json_value(item, depth + 1)
        return
    if type(value) is list:
        for item in value:
            _check_json_value(item, depth + 1)
        return
    raise InvalidPayloadError(
        f"payload contains a value that is not plain JSON ({type(value).__name__})."
    )


def _validate_payload(payload: object) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise InvalidPayloadError("payload must be a dict or None.")
    _check_json_value(payload, 0)
    try:
        encoded = json.dumps(
            payload, allow_nan=False, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise InvalidPayloadError("payload is not serializable as JSON.") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise InvalidPayloadError(
            f"payload is {len(encoded)} bytes as JSON; the limit is {MAX_PAYLOAD_BYTES}."
        )
    return payload


# --------------------------------------------------------------------------
# claim_next_command
# --------------------------------------------------------------------------


def claim_next_command(worker_id: str, *, client: Any | None = None) -> CommandRow | None:
    """Claim the oldest unclaimed ``received`` command, or return None.

    Selects up to ``CLAIM_CANDIDATE_LIMIT`` candidates (status ``received``,
    ``claimed_at`` null, oldest ``created_at`` first), then tries each with a
    conditional update (id AND status ``received`` AND ``claimed_at`` null) that
    sets ``claimed_by`` and ``claimed_at``. The first update that returns a row
    wins, and that complete row is returned.

    Why this is safe with several workers: the SELECT is only a hint. The claim
    is one UPDATE ... WHERE ... RETURNING. If two workers target the same row,
    the second waits for the first to commit, re-checks the WHERE against the
    new row version, sees ``claimed_at`` set, and updates nothing.

    Limits: there is no lease, so a worker that claims and then dies strands the
    command (recovery is manual, docs/schema-draft.md section 3). Under heavy
    contention all candidates can be lost and None returned while eligible
    commands remain. The claim does not change ``status``.

    An error (select or update) raises WriteError and stops. It does NOT move on
    to the next candidate: after a lost response the claim may have succeeded.

    ``worker_id`` is stored in ``claimed_by``, which the public anon key can
    read. Do not use a secret or a sensitive hostname.
    """
    _require_id(worker_id, "worker_id")
    db = _get_client(client)

    try:
        response = (
            db.table("commands")
            .select("id")
            .eq("status", "received")
            .is_("claimed_at", None)
            .order("created_at")
            .limit(CLAIM_CANDIDATE_LIMIT)
            .execute()
        )
    except Exception as exc:
        raise WriteError(
            f"Could not list claimable commands: {_safe_text(exc)}"
        ) from None
    candidates = _rows(response, "Listing claimable commands")

    for candidate in candidates:
        command_id = candidate.get("id")
        if not isinstance(command_id, str) or not command_id:
            raise WriteError("Listing claimable commands: a row had no id.")
        try:
            response = (
                db.table("commands")
                .update(
                    {
                        "claimed_by": worker_id,
                        "claimed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .eq("id", command_id)
                .eq("status", "received")
                .is_("claimed_at", None)
                .execute()
            )
        except Exception as exc:
            raise WriteError(
                "Claim update failed; the claim may or may not have succeeded, "
                f"so no other candidate was tried: {_safe_text(exc)}"
            ) from None
        won = _rows(response, "Claim update")
        if won:
            return cast(CommandRow, won[0])
        # Zero rows: another worker won this one. Try the next candidate.

    return None


# --------------------------------------------------------------------------
# append_event
# --------------------------------------------------------------------------


def append_event(
    command_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    client: Any | None = None,
) -> AuditEventRow:
    """Insert exactly one audit event and return the inserted row.

    Everything is validated first; a rejection never reaches the database and
    nothing is truncated. ``payload`` None is stored as ``{}``.

    There is NO retry. If this raises WriteError the event may or may not have
    been written: look at the timeline before appending again.

    ``command_received`` is accepted but belongs to the dashboard route handler
    (single-writer rule).
    """
    if not isinstance(event_type, str) or event_type not in AUDIT_EVENT_TYPES:
        raise InvalidEventTypeError(
            f"Unknown event_type {_short(event_type)}. "
            f"Allowed: {', '.join(sorted(AUDIT_EVENT_TYPES))}."
        )
    _require_id(command_id, "command_id")
    if not isinstance(message, str) or not message.strip():
        raise InvalidPayloadError("message must be a non-empty string.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise InvalidPayloadError(
            f"message is {len(message)} characters; the limit is {MAX_MESSAGE_CHARS}."
        )
    clean_payload = _validate_payload(payload)

    db = _get_client(client)
    row = {
        "command_id": command_id,
        "event_type": event_type,
        "message": message,
        "payload": clean_payload,
    }
    try:
        response = db.table("audit_events").insert(row).execute()
    except Exception as exc:
        raise WriteError(
            "Insert into audit_events failed; the event may or may not have been "
            f"written, so do not retry blindly: {_safe_text(exc)}"
        ) from None
    inserted = _rows(response, "Insert into audit_events")
    if not inserted:
        raise WriteError("Insert into audit_events returned no row.")
    return cast(AuditEventRow, inserted[0])


# --------------------------------------------------------------------------
# set_command_status
# --------------------------------------------------------------------------


def set_command_status(
    command_id: str,
    expected_status: str,
    status: str,
    plan_id: str | None = None,
    *,
    client: Any | None = None,
) -> CommandRow:
    """Move a command from ``expected_status`` to ``status`` and return the row.

    The transition is checked against ``ALLOWED_TRANSITIONS`` before any call.
    The update is conditional (id AND status = ``expected_status``), so a command
    whose status changed meanwhile is not touched. No matching row raises
    StateConflictError (status changed, or no such command). ``plan_id`` is sent
    only when given. ``updated_at`` is left to the database trigger.
    """
    for name, value in (("expected_status", expected_status), ("status", status)):
        if not isinstance(value, str) or value not in COMMAND_STATUSES:
            raise InvalidTransitionError(
                f"Unknown {name} {_short(value)}. "
                f"Allowed: {', '.join(sorted(COMMAND_STATUSES))}."
            )
    if status not in ALLOWED_TRANSITIONS[expected_status]:
        raise InvalidTransitionError(
            f"Transition {expected_status} -> {status} is not allowed."
        )
    _require_id(command_id, "command_id")
    if plan_id is not None:
        _require_id(plan_id, "plan_id")

    values: dict[str, Any] = {"status": status}
    if plan_id is not None:
        values["plan_id"] = plan_id

    db = _get_client(client)
    try:
        response = (
            db.table("commands")
            .update(values)
            .eq("id", command_id)
            .eq("status", expected_status)
            .execute()
        )
    except Exception as exc:
        raise WriteError(
            f"Status update {expected_status} -> {status} failed; it may or may not "
            f"have been applied: {_safe_text(exc)}"
        ) from None
    updated = _rows(response, "Status update")
    if not updated:
        raise StateConflictError(
            f"Status update {expected_status} -> {status} matched no row: the "
            "command's status changed, or the command does not exist."
        )
    return cast(CommandRow, updated[0])
