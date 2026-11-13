"""Process ONE command: claim, plan, and record a planner failure.

This is the first end-to-end slice of the worker. It is not a loop and it does
not poll: each call handles at most one command, for a controlled rehearsal.

What one call does
------------------
1. ``audit_store.claim_next_command(worker_id)``. No eligible command: stop.
2. Plan ``row["text"]`` with the deterministic planner (``planner.py``).
3. Planner failure (unsupported or unparseable command):
   a. append ``plan_generated`` with ``{"outcome": "error", "error": {kind, detail}}``
   b. ONLY if that succeeded, ``set_command_status(received -> failed)``.
4. Planner success: NOTHING is written. The validator, the critic call and
   execution are not built yet. The command stays ``received`` and claimed.

What it never does: call the critic, connect to Cyberwave, start a simulation
or move anything.

Stranded commands (read this before a rehearsal)
------------------------------------------------
The claim has no lease and no recovery (docs/schema-draft.md section 3). A
claimed command is skipped by every later claim, so it stays ``received`` and
claimed, with no further processing, if:

* the ``plan_generated`` write fails (the event may or may not exist; the
  status is not touched), or
* the event is written but the status update fails, or
* the planner accepts the command (step 4 above).

Recovery is manual. ``CommandProcessingError`` carries the command id, the
``stage`` that failed (``STAGE_CLAIM``, ``STAGE_EVENT_WRITE`` or
``STAGE_STATUS_UPDATE``) and whether the event was written. Nothing is retried
automatically: ``audit_events`` is append-only, so a blind retry after an
ambiguous failure could write a duplicate event that can never be removed. The
manual steps for each stage are in worker/README.md, "Manual recovery".

While a stranded command has a ``plan_generated`` error event but is still
``received``, the dashboard summary already reads "Command not understood:
nothing executed" (events win over status in src/lib/summary.ts), but the
status column still says ``received``.

Before a rehearsal, check in the dashboard that no other ``received`` command
is waiting: the claim takes the OLDEST eligible command, which may not be the
one just typed.

Run from ``worker/`` (writes to Supabase, so it needs ``--confirm-write``)::

    .venv\\Scripts\\python.exe command_processor.py --confirm-write
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Literal

import audit_store
from audit_store import AuditStoreError, StateConflictError, WriteError
from config import redact
from planner import UNSUPPORTED_COMMAND, plan_command

__all__ = [
    "DEFAULT_WORKER_ID",
    "STAGE_CLAIM",
    "STAGE_EVENT_WRITE",
    "STAGE_STATUS_UPDATE",
    "CommandProcessingError",
    "ProcessResult",
    "planner_failure_message",
    "process_one_command",
]

# Stored in commands.claimed_by, which the anon key can read. Not a hostname.
DEFAULT_WORKER_ID = "rehearsal-worker"

# Where a CommandProcessingError happened.
STAGE_CLAIM = "claim"  # the claim returned an unusable row
STAGE_EVENT_WRITE = "event_write"  # plan_generated insert failed; status untouched
STAGE_STATUS_UPDATE = "status_update"  # event written; received -> failed failed

Outcome = Literal["no_command", "planner_failed", "planned_not_processed"]


@dataclass(frozen=True)
class ProcessResult:
    """What one call did.

    * ``no_command``: nothing was claimed, nothing was written.
    * ``planner_failed``: ``plan_generated`` (error) written, status now ``failed``.
    * ``planned_not_processed``: the planner accepted the command, but the
      rest of the pipeline does not exist yet. Nothing was written, and the
      command is left ``received`` and claimed (stranded, see module doc).
    """

    outcome: Outcome
    command_id: str | None = None
    error_kind: str | None = None
    action_count: int | None = None


class CommandProcessingError(Exception):
    """Processing stopped after the claim. The command needs a person to look at it.

    ``stage`` is one of:

    * ``STAGE_CLAIM``: the claimed row was unusable. The claim itself may have
      set ``claimed_by``/``claimed_at``; no event and no status were written.
    * ``STAGE_EVENT_WRITE``: the ``plan_generated`` insert failed. The status
      was NOT changed.
    * ``STAGE_STATUS_UPDATE``: ``plan_generated`` was written, then the
      ``received -> failed`` update failed.

    ``event_written`` is True, False, or None when it cannot be known (an
    ambiguous ``WriteError`` on the insert).
    """

    def __init__(
        self, message: str, *, command_id: str, stage: str, event_written: bool | None
    ) -> None:
        super().__init__(message)
        self.command_id = command_id
        self.stage = stage
        self.event_written = event_written


def planner_failure_message(kind: str) -> str:
    """The event ``message``. Matches the dashboard summary text (src/lib/summary.ts)."""
    if kind == UNSUPPORTED_COMMAND:
        return "Command not understood: nothing executed"
    return "Planning failed: nothing executed"


def _describe(exc: BaseException) -> str:
    """Error text safe to show. audit_store errors are already sanitized."""
    if isinstance(exc, AuditStoreError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def process_one_command(worker_id: str, *, client: Any | None = None) -> ProcessResult:
    """Claim and process at most one command. See the module docstring.

    Raises whatever ``claim_next_command`` raises (nothing is processed then),
    and ``CommandProcessingError`` when a write after the claim fails.
    """
    row = audit_store.claim_next_command(worker_id, client=client)
    if row is None:
        return ProcessResult(outcome="no_command")

    command_id = row.get("id")
    if not isinstance(command_id, str) or not command_id.strip():
        # Cannot happen with a real claim; refuse rather than guess.
        raise CommandProcessingError(
            "CLAIM RETURNED AN UNUSABLE ROW (no id). A command may already have "
            "been claimed (claimed_by/claimed_at set) and left 'received'. No "
            "audit event and no status change were written. Find it in the "
            "dashboard by claimed_by.",
            command_id="<unknown>",
            stage=STAGE_CLAIM,
            event_written=False,
        )

    plan = plan_command(row.get("text"))
    if plan.failure is None:
        return ProcessResult(
            outcome="planned_not_processed",
            command_id=command_id,
            action_count=len(plan.actions or ()),
        )

    failure = plan.failure
    try:
        audit_store.append_event(
            command_id,
            "plan_generated",
            planner_failure_message(failure.kind),
            failure.to_payload(),
            client=client,
        )
    except Exception as exc:
        # WriteError is ambiguous (the insert may have happened). Any other
        # error was raised before the request was sent.
        written = None if isinstance(exc, WriteError) else False
        raise CommandProcessingError(
            f"EVENT WRITE FAILED for command {command_id}: plan_generated insert "
            f"failed ({_describe(exc)}). "
            + (
                "The event MAY have been written: check the timeline before "
                "writing it again. "
                if written is None
                else "No event was written. "
            )
            + "The status was NOT changed: the command is still 'received' and "
            "claimed. Manual recovery needed.",
            command_id=command_id,
            stage=STAGE_EVENT_WRITE,
            event_written=written,
        ) from None

    try:
        audit_store.set_command_status(
            command_id, expected_status="received", status="failed", client=client
        )
    except Exception as exc:
        if isinstance(exc, StateConflictError):
            state = "the command was no longer 'received'"
        elif isinstance(exc, WriteError):
            state = "the update may or may not have been applied"
        else:
            state = "the status was not changed"
        raise CommandProcessingError(
            f"STATUS UPDATE FAILED for command {command_id}: plan_generated WAS "
            f"written, but received -> failed failed ({_describe(exc)}); {state}. "
            "Do NOT write the event again. Manual recovery needed: set the "
            "status only.",
            command_id=command_id,
            stage=STAGE_STATUS_UPDATE,
            event_written=True,
        ) from None

    return ProcessResult(
        outcome="planner_failed", command_id=command_id, error_kind=failure.kind
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claim and process ONE command (planner failures only). "
        "Writes to Supabase."
    )
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="required: allow the claim and the audit/status writes",
    )
    parser.add_argument("--worker-id", default=DEFAULT_WORKER_ID)
    args = parser.parse_args(argv)

    if not args.confirm_write:
        print("Refusing to run without --confirm-write. Nothing was sent.")
        return 2

    try:
        result = process_one_command(args.worker_id)
    except CommandProcessingError as exc:
        print(f"FAILED at {exc.stage}: {redact(exc)}")
        return 1
    except Exception as exc:
        print(f"Claim failed, nothing processed: {redact(_describe(exc))}")
        return 1

    if result.outcome == "no_command":
        print("No eligible command. Nothing claimed, nothing written.")
    elif result.outcome == "planner_failed":
        print(
            f"Command {result.command_id}: plan_generated ({result.error_kind}) "
            "written, status received -> failed."
        )
    else:
        print(
            f"Command {result.command_id}: planner produced {result.action_count} "
            "action(s), but validation, critic and execution are not implemented. "
            "Nothing was written and the command was NOT failed. It stays "
            "'received' and claimed (stranded; see README, 'Manual recovery')."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
