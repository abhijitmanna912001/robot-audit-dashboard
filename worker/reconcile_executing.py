"""Manual reconciliation of ONE simulation command left ``executing`` with no terminal event.

When the simulation worker is killed outright (SIGKILL, power loss, a crash it
cannot handle), nothing can be recorded: the command stays ``executing`` with
``execution_started`` and no ``execution_completed`` / ``execution_failed``. Such
a command MUST be treated as possibly moved: a movement may have been sent and
nothing confirmed a stop.

Why this is manual, one command at a time, with an operator attestation
------------------------------------------------------------------------
The database cannot tell whether the worker that started a command has stopped:

* there is no lease, heartbeat or process id (``db/001_init.sql``), and the
  schema is not changed for this;
* ``claimed_by`` is a shared label (``rehearsal-worker`` by default), not a
  process identity;
* age proves nothing: a run can be slow or blocked, a process can be suspended,
  and clocks can differ.

So this script never decides on its own that a worker has stopped, and it has
no sweep mode. An operator names ONE command (``--command-id``) and, to write,
attests with ``--confirm-worker-stopped`` that they checked that the worker
process which ran it, and its twin child processes, are no longer running on
any machine that could run it.

Eligibility (the agreed stale-command rule)
-------------------------------------------
A command is a reconciliation candidate only when ALL of these hold:

1. its status is ``executing``;
2. it has no terminal event (``execution_completed`` / ``execution_failed``);
3. its timestamp is older than the maximum possible worker runtime from that
   timestamp plus ``STALE_SAFETY_MARGIN_S``. The timestamp is the
   ``execution_started`` row's ``created_at`` (set by the database) when it is
   valid; otherwise ``claimed_at`` (set by the worker), measured against the
   whole lifecycle from the claim. A missing or invalid timestamp, or one later
   than the current time, never qualifies.

The maximum runtime is the sum of ``LIFECYCLE_PHASES``. If ANY phase relevant
to the chosen timestamp has no finite upper bound, there is no maximum and no
command qualifies. TODAY THAT IS THE CASE: every Supabase request has only the
PostgREST per-operation timeouts (no total limit, DNS not covered) and the pure
computations have no configured timeout. So ``--apply`` writes nothing until
those phases are bounded by a reviewed change. Even a command that qualifies
is not thereby proven stopped: the attestation stays mandatory.

What it does
------------
Default: a dry run. It reads the command and its timeline and prints what it
would do, including the eligibility calculation. Nothing is written.
(``--list`` is read-only and informational, at most ``LIST_LIMIT`` rows with no
truncation notice: it does not establish that a worker has stopped and does not
authorize reconciliation.) ``--apply`` (with ``--confirm-worker-stopped``)
performs exactly what the dry run of ONE command reports:

* a candidate: write ONE ``execution_failed``
  (``{"dryRun": false, "mode": "simulation", "error": {"kind": "worker_lost",
  "detail": "Physical outcome unknown; manual verification required. ..."},
  "stop": "unconfirmed"}``), then the conditional ``executing -> failed``;
* exactly one execution_failed recorded, and it is this script's own
  ``worker_lost`` record with stop ``unconfirmed`` (an earlier reconciliation
  whose status update did not apply), status still ``executing``, and the same
  stale rule holds: ONLY the conditional ``executing -> failed``. No event is
  ever written in this case;
* either of the two above when the stale rule does not hold: nothing
  (``not_eligible``);
* status already ``completed`` / ``failed`` / ``rejected``: nothing to do;
* anything else needs a person and nothing is written: any other
  ``execution_failed`` (another kind, another stop, a reading, a malformed
  payload) while the status is still ``executing`` (set the status by hand; no
  second event is ever written), an ``execution_completed`` (this
  script never records a success), conflicting or duplicate terminal events, no
  or several ``execution_started``, a dry-run command, a command that is not
  ``executing``.

It never retries, never re-dispatches and never sends anything to a twin or a
robot: it imports no Cyberwave, twin, executor or supervisor module. Each write
is attempted once. After an ambiguous failure, run it again WITHOUT ``--apply``:
the timeline then shows whether the event was written; once it is, a later run
can only make the status change, so no second event can be written by a later
run.

Known limit: two reconciliations of the same command at the same moment could
both see no terminal event and both insert one (no database constraint prevents
it, and the schema is not changed). Run one reconciliation at a time.

Usage, from ``worker/``::

    .venv\\Scripts\\python.exe reconcile_executing.py --list
    .venv\\Scripts\\python.exe reconcile_executing.py --command-id <uuid>
    .venv\\Scripts\\python.exe reconcile_executing.py --command-id <uuid> --apply --confirm-worker-stopped
"""

from __future__ import annotations

import argparse
import math
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import audit_store
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
    "ACTION_FINISH_STATUS",
    "ACTION_NEEDS_PERSON",
    "ACTION_NOTHING_TO_DO",
    "ACTION_NOT_ELIGIBLE",
    "ACTION_RECORD_FAILURE",
    "Assessment",
    "Eligibility",
    "LIFECYCLE_PHASES",
    "Phase",
    "ReconcileError",
    "ReconcileRefused",
    "ReconcileResult",
    "RECONCILED_DETAIL",
    "RECONCILED_KIND",
    "RECONCILED_MESSAGE",
    "RuntimeBound",
    "SOURCE_CLAIMED_AT",
    "SOURCE_EXECUTION_STARTED",
    "STALE_SAFETY_MARGIN_S",
    "assess",
    "evaluate_eligibility",
    "list_executing",
    "main",
    "reconcile_command",
    "runtime_bound",
]

ACTION_NOTHING_TO_DO = "nothing_to_do"
ACTION_RECORD_FAILURE = "record_failure"  # execution_failed, then executing -> failed
# executing -> failed only: this script's own worker_lost event is already there
ACTION_FINISH_STATUS = "finish_status"
ACTION_NOT_ELIGIBLE = "not_eligible"  # would be one of the two above, but rule 3 does not hold
ACTION_NEEDS_PERSON = "needs_person"  # nothing is written

TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected"})
TERMINAL_EVENTS = frozenset({"execution_completed", "execution_failed"})

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
    "--confirm-worker-stopped means: you checked that the worker process that ran this "
    "command, and its twin child processes, are no longer running on any machine that "
    "could run it. The database cannot show this, and the command's age does not prove it."
)


# --------------------------------------------------------------------------
# Stale-command rule: lifecycle bounds and the safety margin
# --------------------------------------------------------------------------

# PROPOSED, to be agreed with Abhijit (seconds). It is added to the maximum
# runtime to absorb clock differences between the three clocks involved: the
# worker machine (claimed_at), the database (created_at) and this machine (the
# current time). 300 s tolerates a combined difference of up to about 4 minutes
# plus one minute of scheduling slack. It does not cover a suspended or stopped
# worker process: nothing can, which is why the attestation stays mandatory.
STALE_SAFETY_MARGIN_S = 300.0

SOURCE_EXECUTION_STARTED = "execution_started.created_at"
SOURCE_CLAIMED_AT = "claimed_at"


@dataclass(frozen=True)
class Phase:
    """One step of the worker lifecycle and its upper bound in seconds.

    ``bound_s`` None means the step has no finite upper bound in the code.
    ``after_started`` is True for the steps that can still run after the
    ``execution_started`` row was created by the database.
    """

    name: str
    bound_s: float | None
    basis: str
    after_started: bool

    def __post_init__(self) -> None:
        if self.bound_s is not None and (
            type(self.bound_s) is not float or not math.isfinite(self.bound_s) or self.bound_s <= 0
        ):
            raise ValueError(f"{self.name}: bound_s must be a positive finite float or None.")


_SUPABASE = (
    "no finite total: the PostgREST client's default 120 s timeout applies to each connect, "
    "read, write and pool wait separately, and DNS resolution is not covered"
)
_PURE = "no configured timeout (pure computation)"

# Every step of one simulation run, in order (simulation_processor.py), with the
# bound the code enforces. A test keeps the numbers equal to their constants.
LIFECYCLE_PHASES: tuple[Phase, ...] = (
    # Decision phase (command_processor.decide_one_command); claimed_at is set
    # by the worker just before the claim update is sent.
    Phase("claim update", None, _SUPABASE, False),
    Phase("planner", None, _PURE, False),
    Phase("plan_generated insert", None, _SUPABASE, False),
    Phase("safety validator", None, _PURE, False),
    Phase("validator_result insert", None, _SUPABASE, False),
    Phase("critic request", 10.0, "critic_client.TOTAL_DEADLINE_SECONDS", False),
    Phase("critic_result insert", None, _SUPABASE, False),
    Phase("received -> approved update", None, _SUPABASE, False),
    Phase("simulation pre-checks", None, _PURE, False),
    Phase("execution_started insert, until the row is created", None, _SUPABASE, False),
    # Execution phase: everything after the execution_started row exists.
    Phase("execution_started insert, response", None, _SUPABASE, True),
    Phase("approved -> executing update", None, _SUPABASE, True),
    Phase(
        "twin session start",
        18.0,
        "simulation_supervisor.CHILD_START_LIMIT_S 15 + TERMINATE_LIMIT_S 1 + CLEANUP_LIMIT_S 2 "
        "(cleanup of a failed start); process.start() itself has no deadline",
        True,
    ),
    Phase(
        "rehearsal, stop and child terminations",
        78.0,
        "simulation_executor.Timeouts.total_s 75 + TERMINATE_LIMIT_S 1 (end of the recovery "
        "window) + TERMINATE_LIMIT_S 1 for each of at most two children",
        True,
    ),
    Phase("twin session close", 2.0, "simulation_supervisor.CLEANUP_LIMIT_S", True),
    Phase("building the outcome record", None, _PURE, True),
    Phase("terminal event insert", None, _SUPABASE, True),
    Phase("terminal status update", None, _SUPABASE, True),
)


@dataclass(frozen=True)
class RuntimeBound:
    maximum_s: float | None  # None: at least one relevant phase has no finite bound
    bounded_s: float  # the sum of the relevant phases that do have a bound
    unbounded: tuple[str, ...]  # the relevant phases without a finite bound
    why_unbounded: tuple[str, ...] = ()  # their distinct reasons


def runtime_bound(source: str, phases: Sequence[Phase] | None = None) -> RuntimeBound:
    """The maximum worker runtime from a timestamp of ``source``. Pure."""
    if source not in (SOURCE_EXECUTION_STARTED, SOURCE_CLAIMED_AT):
        raise ValueError(f"unknown timestamp source {source!r}")
    table = LIFECYCLE_PHASES if phases is None else phases
    relevant = [p for p in table if p.after_started or source == SOURCE_CLAIMED_AT]
    bounded = math.fsum(p.bound_s for p in relevant if p.bound_s is not None)
    open_ended = [p for p in relevant if p.bound_s is None]
    unbounded = tuple(p.name for p in open_ended)
    why = tuple(dict.fromkeys(p.basis for p in open_ended))
    return RuntimeBound(None if unbounded else bounded, bounded, unbounded, why)


@dataclass(frozen=True)
class Eligibility:
    """The stale-command rule's timestamp part (rule 3), with every number shown."""

    qualifies: bool
    reason: str
    source: str | None = None
    timestamp: str | None = None
    age_s: float | None = None
    maximum_s: float | None = None
    bounded_s: float | None = None
    unbounded: tuple[str, ...] = ()
    why_unbounded: tuple[str, ...] = ()
    margin_s: float = STALE_SAFETY_MARGIN_S
    threshold_s: float | None = None


def _parse_timestamp(value: object) -> datetime | None:
    """An ISO 8601 timestamp WITH a time zone, as UTC; anything else is None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc)


def evaluate_eligibility(
    row: Mapping[str, Any],
    started_event: Mapping[str, Any] | None,
    now: datetime,
) -> Eligibility:
    """Rule 3: is the command's timestamp older than the maximum runtime plus the margin?

    Pure. A qualifying result is necessary for a write, never sufficient: it is
    not proof that the worker has stopped.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime.")
    margin = STALE_SAFETY_MARGIN_S
    if type(margin) is not float or not math.isfinite(margin) or margin < 0:
        return Eligibility(False, "the safety margin is not a finite, non-negative number",
                           margin_s=float("nan"))

    raw_started = started_event.get("created_at") if started_event is not None else None
    raw_claimed = row.get("claimed_at")
    moment = _parse_timestamp(raw_started)
    if moment is not None:
        source, raw, note = SOURCE_EXECUTION_STARTED, raw_started, ""
    else:
        moment = _parse_timestamp(raw_claimed)
        if moment is None:
            return Eligibility(
                False,
                "neither execution_started.created_at nor claimed_at is a valid timestamp "
                "with a time zone",
                margin_s=margin,
            )
        source, raw = SOURCE_CLAIMED_AT, raw_claimed
        note = " (execution_started has no valid timestamp, so the whole lifecycle from the claim is used)"

    bound = runtime_bound(source)
    age = (now.astimezone(timezone.utc) - moment).total_seconds()
    common = dict(
        source=source, timestamp=str(raw), age_s=age, maximum_s=bound.maximum_s,
        bounded_s=bound.bounded_s, unbounded=bound.unbounded,
        why_unbounded=bound.why_unbounded, margin_s=margin,
    )
    if age < 0:
        return Eligibility(
            False, f"the {source} timestamp is later than the current time (a clock difference)",
            **common,
        )
    if bound.maximum_s is None:
        return Eligibility(
            False,
            f"there is no finite maximum worker runtime from {source}{note}: "
            f"{len(bound.unbounded)} phase(s) have no finite upper bound",
            **common,
        )
    threshold = bound.maximum_s + margin
    if age > threshold:
        return Eligibility(
            True,
            f"age {age:.1f} s is older than the threshold {threshold:.1f} s{note}",
            threshold_s=threshold,
            **common,
        )
    return Eligibility(
        False,
        f"age {age:.1f} s is not older than the threshold {threshold:.1f} s{note}",
        threshold_s=threshold,
        **common,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


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
    eligibility: Eligibility | None = None  # set when rules 1 and 2 hold


@dataclass(frozen=True)
class ReconcileResult:
    assessment: Assessment
    applied: bool  # True only if --apply was given and the writes were attempted
    events_written: tuple[str, ...] = ()
    status_after: str | None = None  # set only when a status change was confirmed
    error: str | None = None  # a write failed; nothing after it was attempted
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


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


def _is_reconciliation_failure(payload: object) -> bool:
    """True only for this script's own record: a valid simulation execution_failed
    of kind worker_lost with stop unconfirmed and no reading."""
    try:
        validate_simulation_event("execution_failed", payload)
    except Exception:
        return False
    if not isinstance(payload, Mapping) or not isinstance(payload.get("error"), Mapping):
        return False
    error = payload["error"]
    return (
        error.get("kind") == RECONCILED_KIND
        and payload.get("stop") == STOP_UNCONFIRMED
        and "lastTelemetry" not in payload
    )


def assess(
    command_id: str,
    row: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
    now: datetime,
) -> Assessment:
    """Decide what to do from the command row, its timeline and the time. Pure, never writes."""
    types = tuple(str(e.get("event_type")) for e in events)
    info = {}
    if row is not None:
        info = {k: row.get(k) for k in ("claimed_by", "claimed_at", "updated_at")}

    def verdict(
        action: str, reason: str, mode: str | None = None, eligibility: Eligibility | None = None
    ) -> Assessment:
        status = row.get("status") if row is not None else None
        return Assessment(command_id, action, reason, status, mode, types, **info,
                          eligibility=eligibility)

    if row is None:
        return verdict(ACTION_NEEDS_PERSON, "no such command")
    status = row.get("status")
    if status in TERMINAL_STATUSES:
        return verdict(ACTION_NOTHING_TO_DO, f"status is already '{status}'")
    if len(events) >= MAX_EVENTS:
        return verdict(ACTION_NEEDS_PERSON, f"the timeline has {MAX_EVENTS} or more events")
    if status != "executing":
        return verdict(
            ACTION_NEEDS_PERSON,
            f"status is '{status}', not 'executing'; this script reconciles only 'executing' "
            "commands (see Manual recovery in worker/README.md)",
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
    if terminal == ["execution_failed"]:
        failed = next(e for e in events if e.get("event_type") == "execution_failed")
        if not _is_reconciliation_failure(failed.get("payload")):
            return verdict(
                ACTION_NEEDS_PERSON,
                "an execution_failed that this script did not write (or that does not match "
                "its worker_lost record) is recorded but the status is still 'executing'. No "
                "second event is ever written: set executing -> failed by hand (stage "
                "failed_update in Manual recovery)",
                mode,
            )
        # Exactly one worker_lost record of this script, stop unconfirmed: an
        # earlier reconciliation wrote it and its status update did not apply.
        eligibility = evaluate_eligibility(row, started[0], now)
        if not eligibility.qualifies:
            return verdict(
                ACTION_NOT_ELIGIBLE,
                "the worker_lost execution_failed is recorded and the status is still "
                f"'executing', but the stale rule does not hold: {eligibility.reason}",
                mode,
                eligibility,
            )
        return verdict(
            ACTION_FINISH_STATUS,
            "exactly one worker_lost execution_failed (stop unconfirmed) is recorded and the "
            "status is still 'executing': only the conditional executing -> failed is made, "
            "no event is written",
            mode,
            eligibility,
        )
    if terminal == ["execution_completed"]:
        return verdict(
            ACTION_NEEDS_PERSON,
            "execution_completed is recorded; this script never records a success. Set "
            "executing -> completed by hand (see Manual recovery)",
            mode,
        )
    if terminal:
        return verdict(
            ACTION_NEEDS_PERSON,
            f"conflicting or duplicate terminal events: {', '.join(terminal)}",
            mode,
        )

    # Rules 1 and 2 hold. Rule 3: the timestamp threshold.
    eligibility = evaluate_eligibility(row, started[0], now)
    if not eligibility.qualifies:
        return verdict(
            ACTION_NOT_ELIGIBLE,
            f"executing with no terminal event, but not a reconciliation candidate: "
            f"{eligibility.reason}",
            mode,
            eligibility,
        )
    return verdict(
        ACTION_RECORD_FAILURE,
        "executing with no terminal event and older than the stale threshold: a "
        "reconciliation candidate (not proof that the worker stopped)",
        mode,
        eligibility,
    )


# --------------------------------------------------------------------------
# Reconcile one command
# --------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    if isinstance(exc, AuditStoreError):
        return redact(f"{type(exc).__name__}: {exc}")
    return type(exc).__name__


def reconcile_command(
    command_id: str,
    *,
    apply: bool = False,
    worker_stopped_confirmed: bool = False,
    client: Any | None = None,
) -> ReconcileResult:
    """Assess ONE command; with ``apply`` and the attestation, record a candidate's failure.

    Every write is attempted at most once. A failed write stops the run: nothing
    after it is attempted, and the result says what is known.
    """
    if apply is not False and apply is not True:
        raise ReconcileRefused("apply must be a boolean.")
    if apply and worker_stopped_confirmed is not True:
        raise ReconcileRefused(
            "--apply needs --confirm-worker-stopped. Nothing was read or written. " + ATTESTATION
        )
    command_id = _require_uuid(command_id)
    db = _get_client(client)
    row = _read_command(command_id, db)
    events = _read_events(command_id, db) if row is not None else []
    found = assess(command_id, row, events, _utc_now())

    if not apply or found.action not in (ACTION_RECORD_FAILURE, ACTION_FINISH_STATUS):
        return ReconcileResult(found, applied=False)

    if found.action == ACTION_FINISH_STATUS:
        # The worker_lost event is already recorded: status only, never an event.
        return _set_failed(command_id, found, (), db)

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
    return _set_failed(command_id, found, ("execution_failed",), db)


def _set_failed(
    command_id: str, found: Assessment, written: tuple[str, ...], db: Any
) -> ReconcileResult:
    """ONE conditional executing -> failed. Never retried, never writes an event."""
    try:
        audit_store.set_command_status(command_id, "executing", "failed", client=db)
    except StateConflictError as exc:
        return ReconcileResult(
            found,
            applied=True,
            events_written=written,
            error=(
                f"executing -> failed matched no row ({_describe(exc)}): the status changed "
                "after it was read. It was not touched. Look at the command before anything else."
            ),
        )
    except Exception as exc:
        return ReconcileResult(
            found,
            applied=True,
            events_written=written,
            error=(
                f"executing -> failed failed ({_describe(exc)}); it may or may not have been "
                "applied. The worker_lost execution_failed IS recorded: never write it again. "
                "Run again WITHOUT --apply to see the status: while it is still 'executing', a "
                "later --apply makes only the status change and writes no event."
            ),
        )
    return ReconcileResult(found, applied=True, events_written=written, status_after="failed")


def list_executing(*, client: Any | None = None) -> list[Assessment]:
    """Read-only: every ``executing`` command (oldest first, at most LIST_LIMIT), assessed."""
    db = _get_client(client)
    try:
        response = (
            db.table("commands")
            .select(*_COMMAND_COLUMNS)
            .eq("status", "executing")
            .order("created_at")
            .limit(LIST_LIMIT)
            .execute()
        )
    except Exception as exc:
        raise ReconcileError(f"Could not list executing commands: {_safe_text(exc)}") from None
    try:
        rows = _rows(response, "Listing executing commands")
    except WriteError as exc:
        raise ReconcileError(str(exc)) from None
    now = _utc_now()
    found = []
    for row in rows:
        command_id = row.get("id")
        if not isinstance(command_id, str) or not command_id:
            raise ReconcileError("Listing executing commands: a row had no id.")
        found.append(assess(command_id, row, _read_events(command_id, db), now))
    return found


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
        "Would make ONLY the conditional executing -> failed. No event would be written."
    ),
    ACTION_NOT_ELIGIBLE: "Not a reconciliation candidate. This script writes nothing for it.",
    ACTION_NEEDS_PERSON: "Needs a person. This script writes nothing for it.",
}


def _seconds(value: float | None) -> str:
    return "none" if value is None else f"{value:.1f} s"


def _describe_eligibility(e: Eligibility | None) -> list[str]:
    if e is None:
        return ["  Stale rule: not evaluated (rules 1 and 2 do not both hold: status "
                "'executing' with exactly one simulation execution_started and no terminal event)."]
    maximum = (
        _seconds(e.maximum_s) if e.maximum_s is not None
        else f"NOT FINITE (bounded phases {_seconds(e.bounded_s)}; no finite upper bound: "
             f"{'; '.join(e.unbounded)})"
    )
    why = [f"  Why there is no finite bound: {'; '.join(e.why_unbounded)}."] if e.why_unbounded else []
    return [
        f"  Stale rule: timestamp source {e.source or 'none'}; timestamp {e.timestamp or 'none'}; "
        f"age {_seconds(e.age_s)}.",
        f"  Maximum worker runtime from that timestamp: {maximum}.",
        *why,
        f"  Safety margin STALE_SAFETY_MARGIN_S: {_seconds(e.margin_s)}. Effective threshold: "
        f"{_seconds(e.threshold_s)}.",
        f"  Qualifies: {'YES' if e.qualifies else 'NO'}: {e.reason}. Age is never proof that "
        "the worker has stopped.",
    ]


def _describe_assessment(found: Assessment) -> list[str]:
    return [
        f"Command {found.command_id}: status {found.status}, mode {found.mode or 'n/a'}.",
        f"  Timeline: {', '.join(found.events) or 'no events'}.",
        f"  claimed_by={found.claimed_by!r} claimed_at={found.claimed_at!r} "
        f"updated_at={found.updated_at!r}.",
        *_describe_eligibility(found.eligibility),
        f"  Assessment: {found.action}: {found.reason}.",
    ]


def _report(result: ReconcileResult) -> list[str]:
    found = result.assessment
    lines = _describe_assessment(found)
    possibly_moved = found.action in (ACTION_RECORD_FAILURE, ACTION_FINISH_STATUS, ACTION_NOT_ELIGIBLE)
    if not result.applied:
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
    ACTION_NOT_ELIGIBLE: 5,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manual reconciliation of ONE simulation command left 'executing' with "
        "no terminal event. Dry run by default; never retries or sends any movement."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--command-id", help="the ONE command to reconcile (UUID)")
    target.add_argument(
        "--list", action="store_true", help="read-only: list every 'executing' command, assessed"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write what the dry run reports (needs the attestation)"
    )
    parser.add_argument(
        "--confirm-worker-stopped",
        action="store_true",
        help="attest that the worker and its twin processes are no longer running",
    )
    args = parser.parse_args(argv)

    if args.list and (args.apply or args.confirm_worker_stopped):
        print("Refusing: --list is read-only and takes no --apply. Nothing was read or written.")
        return 2
    if args.apply and not args.confirm_worker_stopped:
        print(f"Refusing: --apply needs --confirm-worker-stopped. Nothing was read or written. {ATTESTATION}")
        return 2

    try:
        if args.list:
            found = list_executing()
            if not found:
                print("No 'executing' commands. Nothing was written.")
            for item in found:
                print("\n".join(_describe_assessment(item)))
            print(
                f"Read only: nothing was written. At most {LIST_LIMIT} rows are shown. This list "
                "does not show that any worker has stopped and does not authorize reconciliation."
            )
            return 0
        result = reconcile_command(
            args.command_id,
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
