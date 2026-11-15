"""Manual reconciliation of simulation commands stranded in ``executing`` (Mac/Linux only).

THE RULE: an ``executing`` command with no terminal execution event MUST be
treated as possibly moved. When the worker is killed outright (SIGKILL, power
loss, a crash it cannot handle) nothing can be recorded: the command stays
``executing`` with ``execution_started`` and no ``execution_completed`` /
``execution_failed``. A movement may have been sent and nothing confirmed a stop.

How "no worker is running" is proven
------------------------------------
Not by the database (there is no lease, heartbeat or process id in
``db/001_init.sql``; ``claimed_by`` is a shared label) and not by age (a run can
be slow, blocked or suspended). By the operating system: this script takes the
SAME exclusive lock as the worker (``worker_lock.py``) before it reads anything
and holds it for its whole run. If it gets the lock, no worker or other
reconcile run is running on this machine. It then takes the ACTIVITY lock
(``worker.activity.lock``) exclusively; every worker parent and every twin child
holds it shared for its whole life, so getting it proves no parent and no child
is alive on this machine. If either lock is held it refuses and touches nothing.
The locks do not see other machines, so ``--confirm-worker-stopped`` stays
mandatory: it is the operator's statement about those.

What it lists and changes
-------------------------
Default (no ``--apply``): READ ONLY. It lists

* ``executing`` commands (candidates and the cases that need a person);
* ``received`` commands that are already claimed: claimed but never decided,
  never executed. They are listed with an explanation and NEVER changed.

Candidates (and the only things ``--apply`` can do, for ONE ``--command-id``):

1. ``executing``, one simulation ``execution_started``, NO terminal event:
   write ONE ``execution_failed`` (kind ``worker_lost``, stop ``unconfirmed``,
   with the manual-verification notice), then the conditional ``executing ->
   failed``.
2. Status only: ``executing`` with exactly one terminal event already recorded
   (``execution_failed`` -> ``failed``, ``execution_completed`` -> ``completed``).
   Only the conditional status change is made; NO event is written. This
   finishes a run whose status update did not apply.

Anything else is listed and left alone (needs a person): a dry-run command, no or
several ``execution_started``, conflicting or duplicate terminal events, an
unrecognised payload, a status that is not ``executing``. Terminal statuses need
nothing.

Idempotent: a terminal event, once written, is never written again; a rerun can
only make the status change. Each write is attempted once. It never claims,
dispatches or retries anything and imports no Cyberwave, twin, executor or
supervisor module.

Usage, from ``worker/``::

    .venv/bin/python reconcile_executing.py                      # list (read only)
    .venv/bin/python reconcile_executing.py --command-id <uuid>  # dry run of one
    .venv/bin/python reconcile_executing.py --command-id <uuid> --apply --confirm-worker-stopped
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import audit_store
import worker_lock
from audit_store import AuditStoreError, StateConflictError, WriteError, _get_client, _rows, _safe_text
from config import redact
from simulation_payloads import (
    MANUAL_VERIFICATION_NOTICE,
    MODE_SIMULATION,
    STOP_UNCONFIRMED,
    build_execution_failed,
    validate_simulation_event,
)

__all__ = [
    "ACTION_CLAIMED_RECEIVED",
    "ACTION_FINISH_STATUS",
    "ACTION_NEEDS_PERSON",
    "ACTION_NOTHING_TO_DO",
    "ACTION_RECORD_FAILURE",
    "Assessment",
    "ReconcileError",
    "ReconcileRefused",
    "ReconcileResult",
    "RECONCILED_DETAIL",
    "RECONCILED_KIND",
    "RECONCILED_MESSAGE",
    "assess",
    "list_commands",
    "main",
    "reconcile_command",
]

ACTION_NOTHING_TO_DO = "nothing_to_do"
ACTION_RECORD_FAILURE = "record_failure"  # worker_lost execution_failed, then executing -> failed
ACTION_FINISH_STATUS = "finish_status"  # a terminal event is recorded: only the status change
ACTION_CLAIMED_RECEIVED = "claimed_received"  # claimed, still received: listed, never changed
ACTION_NEEDS_PERSON = "needs_person"  # nothing is written

TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected"})
TERMINAL_EVENTS = frozenset({"execution_completed", "execution_failed"})
_STATUS_FOR_EVENT = {"execution_failed": "failed", "execution_completed": "completed"}

RECONCILED_KIND = "worker_lost"
# The event message is the timeline title on the dashboard, so the warning is
# stated here too, not only in error.detail.
RECONCILED_MESSAGE = (
    "Simulation failed: worker lost. Command possibly moved. Manual verification required."
)
RECONCILED_DETAIL = (
    f"{MANUAL_VERIFICATION_NOTICE} Recorded by manual reconciliation: the worker ended "
    "while executing and wrote no terminal event. Treat the arm as possibly moved."
)

# More events than this for one command is not a normal timeline.
MAX_EVENTS = 200
LIST_LIMIT = 50

_COMMAND_COLUMNS = ("id", "status", "plan_id", "claimed_by", "claimed_at", "created_at", "updated_at")
_EVENT_COLUMNS = ("id", "event_type", "payload", "created_at")

POSSIBLY_MOVED = (
    "A command left 'executing' without a terminal event must be treated as possibly "
    "moved: look at the twin in Cyberwave before anything else."
)
ATTESTATION = (
    "--confirm-worker-stopped means: you checked that no worker or twin child process runs "
    "on any OTHER machine that could have run this command. The locks prove only that no "
    "worker and no twin child process is alive on THIS machine."
)


class ReconcileError(Exception):
    """A read failed or returned something unusable. Nothing was written."""


class ReconcileRefused(Exception):
    """The request itself was refused (for example --apply without the attestation)."""


@dataclass(frozen=True)
class Assessment:
    """What a command's row and timeline say, and what this script would do. Pure."""

    command_id: str
    action: str
    reason: str
    status: str | None
    mode: str | None  # "simulation", "dry_run", "unknown" or None (no execution_started)
    events: tuple[str, ...]  # event types, oldest first
    claimed_by: Any = None
    claimed_at: Any = None
    updated_at: Any = None
    target_status: str | None = None  # the status change the action makes, if any


@dataclass(frozen=True)
class ReconcileResult:
    assessment: Assessment
    applied: bool  # True only if --apply was given and a write was attempted
    events_written: tuple[str, ...] = ()
    status_after: str | None = None  # set only when a status change was confirmed
    error: str | None = None  # a write failed (or the rows changed); nothing after it was attempted
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def _require_lock(lock: object, activity_lock: object) -> None:
    """The locks are the proof that no worker or child runs here: no locks, no reads, no writes."""
    if not isinstance(lock, worker_lock.WorkerLock) or not lock.held:
        raise ReconcileRefused(
            "the worker lock is not held. Nothing was read or written."
        )
    if (
        not isinstance(activity_lock, worker_lock.ActivityLock)
        or not activity_lock.held
        or not activity_lock.exclusive
    ):
        raise ReconcileRefused(
            "the activity lock is not held exclusively. Nothing was read or written."
        )


def _require_uuid(command_id: object) -> str:
    if not isinstance(command_id, str):
        raise ReconcileRefused("command id must be a UUID string.")
    try:
        return str(uuid.UUID(command_id))
    except ValueError:
        raise ReconcileRefused("command id is not a valid UUID.") from None


def _read_command(command_id: str, db: Any) -> dict[str, Any] | None:
    try:
        response = (
            db.table("commands").select(*_COMMAND_COLUMNS).eq("id", command_id).limit(2).execute()
        )
    except Exception as exc:
        raise ReconcileError(f"Could not read the command: {_safe_text(exc)}") from None
    try:
        rows = _rows(response, "Reading the command")
    except WriteError as exc:
        raise ReconcileError(str(exc)) from None
    if len(rows) > 1:
        raise ReconcileError("Reading the command returned more than one row.")
    return rows[0] if rows else None


def _read_events(command_id: str, db: Any) -> list[dict[str, Any]]:
    try:
        response = (
            db.table("audit_events")
            .select(*_EVENT_COLUMNS)
            .eq("command_id", command_id)
            .order("created_at")
            .limit(MAX_EVENTS)
            .execute()
        )
    except Exception as exc:
        raise ReconcileError(f"Could not read the timeline: {_safe_text(exc)}") from None
    try:
        return _rows(response, "Reading the timeline")
    except WriteError as exc:
        raise ReconcileError(str(exc)) from None


# --------------------------------------------------------------------------
# Assessment (pure)
# --------------------------------------------------------------------------


def _mode(payload: object) -> str:
    if isinstance(payload, Mapping):
        if set(payload) == {"dryRun", "mode"} and payload.get("dryRun") is False \
                and payload.get("mode") == MODE_SIMULATION:
            return "simulation"
        if set(payload) == {"dryRun"} and payload.get("dryRun") is True:
            return "dry_run"
    return "unknown"


def _valid_simulation_event(event_type: str, payload: object) -> bool:
    try:
        validate_simulation_event(event_type, payload)
    except Exception:
        return False
    return True


def assess(
    command_id: str,
    row: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> Assessment:
    """Decide what to do from the command row and its timeline. Pure, never writes.

    There is no clock in this decision: whether a worker is still running is
    settled by the lock the caller holds, not by the age of a row.
    """
    types = tuple(str(e.get("event_type")) for e in events)
    info = {}
    if row is not None:
        info = {k: row.get(k) for k in ("claimed_by", "claimed_at", "updated_at")}

    def verdict(
        action: str, reason: str, mode: str | None = None, target: str | None = None
    ) -> Assessment:
        status = row.get("status") if row is not None else None
        return Assessment(command_id, action, reason, status, mode, types, **info,
                          target_status=target)

    if row is None:
        return verdict(ACTION_NEEDS_PERSON, "no such command")
    status = row.get("status")
    if status in TERMINAL_STATUSES:
        return verdict(ACTION_NOTHING_TO_DO, f"status is already '{status}'")
    if status == "received":
        if row.get("claimed_by") is not None or row.get("claimed_at") is not None:
            return verdict(
                ACTION_CLAIMED_RECEIVED,
                "claimed but still 'received': a worker claimed it and never decided it, so "
                "nothing was executed. This script never changes it (see Manual recovery in "
                "worker/README.md)",
            )
        return verdict(ACTION_NOTHING_TO_DO, "status is 'received' and nobody has claimed it")
    if len(events) >= MAX_EVENTS:
        return verdict(ACTION_NEEDS_PERSON, f"the timeline has {MAX_EVENTS} or more events")
    if status != "executing":
        return verdict(
            ACTION_NEEDS_PERSON,
            f"status is '{status}'; this script reconciles only 'executing' commands "
            "(see Manual recovery in worker/README.md)",
        )

    started = [e for e in events if e.get("event_type") == "execution_started"]
    if len(started) != 1:
        return verdict(
            ACTION_NEEDS_PERSON,
            f"expected exactly one execution_started, found {len(started)}",
        )
    mode = _mode(started[0].get("payload"))
    if mode == "dry_run":
        return verdict(
            ACTION_NEEDS_PERSON,
            "this is a dry-run command (nothing could have moved); recover it by hand as "
            "described under Manual recovery in worker/README.md",
            mode,
        )
    if mode != "simulation":
        return verdict(ACTION_NEEDS_PERSON, "the execution_started payload is not recognised", mode)

    terminal = [t for t in types if t in TERMINAL_EVENTS]
    if not terminal:
        return verdict(
            ACTION_RECORD_FAILURE,
            "executing with no terminal event: possibly moved. Would record worker_lost "
            "(stop unconfirmed) and set executing -> failed",
            mode,
            "failed",
        )
    if len(terminal) == 1:
        event = next(e for e in events if e.get("event_type") == terminal[0])
        if not _valid_simulation_event(terminal[0], event.get("payload")):
            return verdict(
                ACTION_NEEDS_PERSON,
                f"the {terminal[0]} payload is not a valid simulation payload; set the status "
                "by hand (see Manual recovery)",
                mode,
            )
        target = _STATUS_FOR_EVENT[terminal[0]]
        return verdict(
            ACTION_FINISH_STATUS,
            f"{terminal[0]} is recorded and the status is still 'executing': only the "
            f"conditional executing -> {target} is made, no event is written",
            mode,
            target,
        )
    return verdict(
        ACTION_NEEDS_PERSON,
        f"conflicting or duplicate terminal events: {', '.join(terminal)}",
        mode,
    )


# --------------------------------------------------------------------------
# Reconcile one command
# --------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    if isinstance(exc, AuditStoreError):
        return redact(f"{type(exc).__name__}: {exc}")
    return type(exc).__name__


def _read_assessment(command_id: str, db: Any) -> Assessment:
    row = _read_command(command_id, db)
    events = _read_events(command_id, db) if row is not None else []
    return assess(command_id, row, events)


def reconcile_command(
    command_id: str,
    *,
    lock: worker_lock.WorkerLock,
    activity_lock: worker_lock.ActivityLock,
    apply: bool = False,
    worker_stopped_confirmed: bool = False,
    client: Any | None = None,
) -> ReconcileResult:
    """Assess ONE command; with ``apply`` and the attestation, make what the assessment says.

    ``lock`` must be the held worker lock and ``activity_lock`` the activity lock held
    exclusively. Every write is attempted at most once.
    A failed write stops the run: nothing after it is attempted.
    """
    if apply is not False and apply is not True:
        raise ReconcileRefused("apply must be a boolean.")
    if apply and worker_stopped_confirmed is not True:
        raise ReconcileRefused(
            "--apply needs --confirm-worker-stopped. Nothing was read or written. " + ATTESTATION
        )
    _require_lock(lock, activity_lock)
    command_id = _require_uuid(command_id)
    db = _get_client(client)
    found = _read_assessment(command_id, db)

    if not apply or found.action not in (ACTION_RECORD_FAILURE, ACTION_FINISH_STATUS):
        return ReconcileResult(found, applied=False)

    # Read again immediately before the first write: the second read must say the
    # same thing, otherwise nothing is written (a terminal event may have landed).
    again = _read_assessment(command_id, db)
    if (again.action, again.target_status) != (found.action, found.target_status):
        return ReconcileResult(
            again,
            applied=False,
            error="the command changed between two reads, so nothing was written. Run again "
            "without --apply and look at the timeline.",
        )

    if found.action == ACTION_FINISH_STATUS:
        # A terminal event is already recorded: status only, never an event.
        return _set_status(command_id, found, (), db)

    payload = build_execution_failed(RECONCILED_KIND, RECONCILED_DETAIL, STOP_UNCONFIRMED)
    validate_simulation_event("execution_failed", payload)
    try:
        audit_store.append_event(
            command_id, "execution_failed", RECONCILED_MESSAGE, payload, client=db
        )
    except Exception as exc:
        maybe = isinstance(exc, WriteError)
        return ReconcileResult(
            found,
            applied=True,
            error=(
                f"the execution_failed insert failed ({_describe(exc)}); it "
                + ("MAY have been written" if maybe else "was not written")
                + ". The status was not changed. Run again WITHOUT --apply to see the "
                "timeline: once the event is there, no later run writes a second one."
            ),
        )
    return _set_status(command_id, found, ("execution_failed",), db)


def _set_status(
    command_id: str, found: Assessment, written: tuple[str, ...], db: Any
) -> ReconcileResult:
    """ONE conditional executing -> target. Never retried, never writes an event."""
    target = found.target_status or "failed"
    try:
        audit_store.set_command_status(command_id, "executing", target, client=db)
    except StateConflictError as exc:
        return ReconcileResult(
            found,
            applied=True,
            events_written=written,
            error=(
                f"executing -> {target} matched no row ({_describe(exc)}): the status changed "
                "after it was read. It was not touched. Look at the command before anything else."
            ),
        )
    except Exception as exc:
        return ReconcileResult(
            found,
            applied=True,
            events_written=written,
            error=(
                f"executing -> {target} failed ({_describe(exc)}); it may or may not have been "
                "applied. A terminal event IS recorded: never write one again. Run again "
                "WITHOUT --apply to see the status: while it is still 'executing', a later "
                "--apply makes only the status change and writes no event."
            ),
        )
    return ReconcileResult(found, applied=True, events_written=written, status_after=target)


def _read_rows(db: Any, status: str, what: str) -> tuple[list[dict[str, Any]], bool]:
    """Up to LIST_LIMIT rows with ``status`` (oldest first) and whether more exist."""
    try:
        response = (
            db.table("commands")
            .select(*_COMMAND_COLUMNS)
            .eq("status", status)
            .order("created_at")
            .limit(LIST_LIMIT + 1)
            .execute()
        )
    except Exception as exc:
        raise ReconcileError(f"Could not list {what}: {_safe_text(exc)}") from None
    try:
        rows = _rows(response, f"Listing {what}")
    except WriteError as exc:
        raise ReconcileError(str(exc)) from None
    return rows[:LIST_LIMIT], len(rows) > LIST_LIMIT


def list_commands(
    *, lock: worker_lock.WorkerLock, activity_lock: worker_lock.ActivityLock,
    client: Any | None = None,
) -> tuple[list[Assessment], bool]:
    """Read-only: ``executing`` commands and claimed ``received`` commands, assessed.

    Returns the assessments and whether either list was cut at LIST_LIMIT.
    """
    _require_lock(lock, activity_lock)
    db = _get_client(client)
    found: list[Assessment] = []
    truncated = False
    executing, more = _read_rows(db, "executing", "executing commands")
    truncated |= more
    received, more = _read_rows(db, "received", "received commands")
    truncated |= more
    claimed = [r for r in received if r.get("claimed_by") is not None or r.get("claimed_at") is not None]
    for row in [*executing, *claimed]:
        command_id = row.get("id")
        if not isinstance(command_id, str) or not command_id:
            raise ReconcileError("Listing commands: a row had no id.")
        found.append(assess(command_id, row, _read_events(command_id, db)))
    return found, truncated


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

_WOULD = {
    ACTION_NOTHING_TO_DO: "Nothing to do.",
    ACTION_RECORD_FAILURE: (
        f"Would write ONE execution_failed (kind {RECONCILED_KIND}, stop {STOP_UNCONFIRMED}, "
        "with the manual verification notice), then executing -> failed."
    ),
    ACTION_FINISH_STATUS: (
        "Would make ONLY the conditional status change. No event would be written."
    ),
    ACTION_CLAIMED_RECEIVED: "Listed only. This script never changes a 'received' command.",
    ACTION_NEEDS_PERSON: "Needs a person. This script writes nothing for it.",
}


def _describe_assessment(found: Assessment) -> list[str]:
    return [
        f"Command {found.command_id}: status {found.status}, mode {found.mode or 'n/a'}.",
        f"  Timeline: {', '.join(found.events) or 'no events'}.",
        f"  claimed_by={found.claimed_by!r} claimed_at={found.claimed_at!r} "
        f"updated_at={found.updated_at!r}.",
        f"  Assessment: {found.action}: {found.reason}.",
    ]


def _report(result: ReconcileResult) -> list[str]:
    found = result.assessment
    lines = _describe_assessment(found)
    possibly_moved = found.action in (ACTION_RECORD_FAILURE, ACTION_FINISH_STATUS)
    if not result.applied:
        if result.error is not None:
            lines.append(f"  NOT WRITTEN: {result.error}")
            return lines
        lines.append(f"  DRY RUN: {_WOULD[found.action]} Nothing was written.")
        if possibly_moved:
            lines.append(f"  {POSSIBLY_MOVED}")
        if found.action in (ACTION_RECORD_FAILURE, ACTION_FINISH_STATUS):
            lines.append(f"  To write it: add --apply --confirm-worker-stopped. {ATTESTATION}")
        return lines
    if result.error is not None:
        lines.append(f"  WRITE FAILED: {result.error}")
        lines.append(f"  Events written by this run: {', '.join(result.events_written) or 'none'}.")
    else:
        lines.append(
            f"  APPLIED: events written: {', '.join(result.events_written) or 'none'}; "
            f"status now {result.status_after}."
        )
    lines.append(f"  {MANUAL_VERIFICATION_NOTICE} {POSSIBLY_MOVED}")
    return lines


_EXIT_CODES = {
    ACTION_NOTHING_TO_DO: 0,
    ACTION_RECORD_FAILURE: 0,
    ACTION_FINISH_STATUS: 0,
    ACTION_NEEDS_PERSON: 4,
    ACTION_CLAIMED_RECEIVED: 4,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List commands stranded in 'executing' (read only by default). With "
        "--command-id, show what would be done for ONE command; with --apply and "
        "--confirm-worker-stopped, do it. Holds the worker lock and the activity lock "
        "(exclusively) for the whole run. Never "
        "claims, retries or sends any movement."
    )
    parser.add_argument("--command-id", help="the ONE command to examine or reconcile (UUID)")
    parser.add_argument(
        "--apply", action="store_true", help="write what the dry run reports (needs the attestation)"
    )
    parser.add_argument(
        "--confirm-worker-stopped",
        action="store_true",
        help="attest: no worker on another machine, no lingering twin child process",
    )
    worker_lock.add_lock_argument(parser)
    args = parser.parse_args(argv)

    if args.apply and not args.confirm_worker_stopped:
        print(f"Refusing: --apply needs --confirm-worker-stopped. Nothing was read or written. {ATTESTATION}")
        return 2
    if args.apply and not args.command_id:
        print("Refusing: --apply acts on ONE command; name it with --command-id. "
              "Nothing was read or written.")
        return 2

    # The lock comes before the first read and is held until this run ends.
    lock = worker_lock.WorkerLock(worker_lock.resolve_path(args.lock_file))
    try:
        lock.acquire()
    except worker_lock.LockError as exc:
        print(f"REFUSED: {exc}. Nothing was read or written.")
        return exc.exit_code
    # Then the activity lock, EXCLUSIVELY: it is held shared by every worker
    # parent and twin child, so getting it proves none is alive on this machine.
    activity = worker_lock.ActivityLock(worker_lock.activity_path(lock.path), exclusive=True)
    try:
        activity.acquire()
    except worker_lock.LockError as exc:
        lock.unlock()
        print(f"REFUSED: {exc}. Nothing was read or written.")
        return exc.exit_code
    try:
        return _run_locked(args, lock, activity)
    finally:
        activity.unlock()
        lock.unlock()


def _run_locked(
    args: argparse.Namespace, lock: worker_lock.WorkerLock, activity: worker_lock.ActivityLock
) -> int:
    try:
        if not args.command_id:
            found, truncated = list_commands(lock=lock, activity_lock=activity)
            if not found:
                print("No 'executing' commands and no claimed 'received' commands. "
                      "Nothing was written.")
            for item in found:
                print("\n".join(_describe_assessment(item)))
            print(
                "Read only: nothing was written. The worker lock and the activity lock are held, "
                "so no worker and no twin child process is running on THIS machine; that says "
                "nothing about other machines."
                + (f" The list was cut at {LIST_LIMIT} rows per status." if truncated else "")
            )
            return 0
        result = reconcile_command(
            args.command_id,
            lock=lock,
            activity_lock=activity,
            apply=args.apply,
            worker_stopped_confirmed=args.confirm_worker_stopped,
        )
    except ReconcileRefused as exc:
        print(f"Refusing: {exc}")
        return 2
    except (ReconcileError, AuditStoreError) as exc:
        print(f"FAILED, nothing was written: {redact(str(exc))}")
        return 1

    print("\n".join(_report(result)))
    if result.error is not None:
        return 1
    return _EXIT_CODES[result.assessment.action]


if __name__ == "__main__":
    sys.exit(main())
