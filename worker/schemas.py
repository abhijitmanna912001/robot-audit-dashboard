"""Typed shapes shared by the worker modules.

Everything in this file mirrors ``db/001_init.sql``, which is the source of
truth for the database. The action and critic-verdict JSON schemas are still
being agreed (see docs/schema-draft.md), so they are deliberately NOT modelled
here: a plan's actions are treated as opaque JSON.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, get_args

# JSON-compatible value. Kept loose on purpose; see module docstring.
JsonValue = Any

CommandStatus = Literal[
    "received", "approved", "rejected", "executing", "completed", "failed"
]

AuditEventType = Literal[
    "command_received",
    "plan_generated",
    "validator_result",
    "critic_result",
    "execution_started",
    "execution_completed",
    "execution_failed",
]

# Runtime copies of the CHECK constraint lists in db/001_init.sql.
COMMAND_STATUSES: frozenset[str] = frozenset(get_args(CommandStatus))
AUDIT_EVENT_TYPES: frozenset[str] = frozenset(get_args(AuditEventType))


class CommandRow(TypedDict):
    """A row of ``public.commands``."""

    id: str
    text: str
    status: CommandStatus
    plan_id: str | None
    claimed_by: str | None
    claimed_at: str | None
    created_at: str
    updated_at: str


class AuditEventRow(TypedDict):
    """A row of ``public.audit_events`` (append-only)."""

    id: str
    command_id: str
    event_type: AuditEventType
    message: str
    payload: dict[str, JsonValue]
    created_at: str
