"""Simulation-only entry point: process ONE command through the SO-101 rehearsal.

CURRENT STATE: **live simulated movement is BLOCKED.** Running this file with
both flags prints why and exits before anything is claimed or sent:

* every entry of ``simulation_gates.CYBERWAVE_GATES`` is unconfirmed (the stop
  method, its success contract, the telemetry provenance, Abhijit's approval);
* no Cyberwave implementation of ``simulation_executor.SimulationTwin`` exists,
  so ``_production_twin`` always refuses.

Both are source code, not settings. No flag, environment variable or
configuration value can unblock it; that takes a reviewed code change.

What is here is the audited lifecycle around ``simulation_executor.run_rehearsal``,
exercised only by offline tests with a fake twin:

1. ``command_processor.decide_one_command``: claim ONE command, plan, validator,
   critic, ``received -> approved | rejected | failed``. Unchanged pipeline. A
   command that is not approved ends exactly as in the dry-run worker.
2. Pre-checks on the approved, immutable ``PlanSnapshot`` (the command text is
   never read again): the approval re-verifies (validator pass, critic approval
   of this command, plan and hash, via ``execution_gate``), the plan is exactly
   the first-rehearsal action, the twin's gates are confirmed, the shared limits
   hold. A refusal is recorded as ``execution_failed`` (``stop: "not_needed"``)
   and ``approved -> failed``.
3. ``execution_started`` ``{"dryRun": false, "mode": "simulation"}``. If it
   cannot be written: nothing is connected, read or moved.
4. ``approved -> executing``. If it does not apply: nothing is connected, read
   or moved; ``execution_failed`` with ``stop: "not_needed"``.
5. The supervised twin session is started (``twin.start()``; children READY
   before the rehearsal clock), then ``run_rehearsal``: verify simulation, start
   reading, move, verify, return, verify restored. See ``simulation_executor.py``
   and ``simulation_supervisor.py``. The session is closed (every child process
   verified gone) before the outcome is recorded.
6. Success: ``execution_completed`` (start, target and restored readings), then
   ``executing -> completed``. The completion event is built only from a
   verified restoration.
7. Failure: ``execution_failed`` (fixed error kind, the real stop result, the
   last reading only if one validated), then ``executing -> failed``. When a
   movement may have been sent and the stop is not confirmed, the recorded detail
   and the CLI output say that the physical outcome is unknown and manual
   verification is required.

SIGTERM. ``main`` installs a handler only around the processing part of the run
(``_production_twin()`` and ``process_one_simulation_command``), after the flag
and gate checks, and restores the previous handler when that part ends. While it
is installed, the FIRST SIGTERM delivered to THIS process raises
``SystemExit(143)`` in the main thread; later ones are ignored. What that means
depends on where the run is:

* during the claim, planner, validator or critic (``decide_one_command``): the
  ``SystemExit`` ends the run like a Ctrl-C there. Nothing has been connected or
  sent; no execution event is written, and the command can be left claimed and
  ``received`` (or decided) for manual recovery;
* during the simulation lifecycle: the interruption path below (kind
  ``interrupted``). A later SIGTERM cannot abandon the stop attempt or interrupt
  the failure recording.

Limits. This holds only for a SIGTERM delivered to the worker process itself. A
SIGTERM sent to the whole process group (``kill -TERM -<pgid>``) or to a systemd
control group (the default ``KillMode=control-group``) also reaches the twin
child processes, which do not handle SIGTERM: a child can die before the parent
sends the stop. A stop through a dead child cannot be confirmed, so the stop is
``unconfirmed`` and, if a movement may have been sent, manual verification is
required. On Windows, forced termination
(``taskkill /F``, ``TerminateProcess``) runs no Python handler at all, like
SIGKILL anywhere: nothing is recorded. Ctrl-C still interrupts as before.

Interruption (Ctrl-C, ``SystemExit``, any other ``BaseException``): outside an
audit write, exactly one ``execution_failed`` (kind ``interrupted``, or
``internal_error`` for an unexpected ``BaseException``) is attempted, then the
conditional status change only if it was written, then the ORIGINAL exception
continues with a note describing what was recorded. An interruption DURING an
audit write leaves that write's outcome unknown: nothing further is written and
``SimulationProcessingError`` (stage ``interrupted_write``) asks for manual
recovery. If this process is killed outright, nothing can be recorded: the
command stays as it was (for example ``executing``) for manual recovery, and the
twin children end themselves (their watchdog sees the parent gone). Such a
command must be treated as possibly moved; ``reconcile_executing.py`` can record
it by hand while holding the worker lock (``worker_lock.py``), which this entry
point takes before its first claim and holds until the run ends.

Every payload is checked by ``simulation_payloads.validate_simulation_event``
before the write. The event is always written before its status change, each
status change is conditional on the status this run knows, and nothing is
retried: not a movement, not an event, not a status update. When a write fails
in a way that leaves the command needing a person, ``SimulationProcessingError``
says exactly what was and was not done, including whether a movement was sent,
the stop result and the last reading.

The dry-run worker (``command_processor.py``) is the default and is unchanged:
it has no simulation option. This file needs BOTH flags::

    .venv\\Scripts\\python.exe simulation_processor.py --simulation --confirm-write

With either flag missing it does nothing at all. It does not fall back to a dry
run, and it processes at most one command per run (no loop, no polling).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import sys
from dataclasses import dataclass
from typing import Any, NoReturn

import audit_store
from audit_store import StateConflictError
from command_processor import (
    DEFAULT_WORKER_ID,
    STAGE_COMPLETED_UPDATE,
    STAGE_EXECUTING_UPDATE,
    STAGE_EXECUTION_COMPLETED_WRITE,
    STAGE_EXECUTION_FAILED_WRITE,
    STAGE_FAILED_UPDATE,
    ApprovedPlan,
    CommandProcessingError,
    CriticCall,
    ProcessResult,
    _describe,
    _status_state,
    _written_flag,
    decide_one_command,
)
from config import ConfigError, load_cyberwave_endpoints, redact
from execution_gate import decide_execution
from plan_snapshot import PlanSnapshot
from safety_validator import ValidationResult
from simulation_executor import (
    DEFAULT_TIMEOUTS,
    RehearsalFailure,
    RehearsalSuccess,
    Timeouts,
    interruption_kind,
    run_rehearsal,
)
from simulation_gates import (
    EXECUTION_TARGET,
    FIRST_REHEARSAL_JOINT,
    FIRST_REHEARSAL_TARGET_RAD,
    GateError,
    live_simulation_blockers,
    require_first_rehearsal_plan,
    revalidate_against_shared_limits,
    twin_gate_blockers,
)
from simulation_payloads import (
    MANUAL_VERIFICATION_NOTICE,
    MAX_TEXT_LENGTH,
    STOP_CONFIRMED,
    STOP_NOT_NEEDED,
    STOP_UNCONFIRMED,
    TOLERANCE_RAD,
    SimulationPayloadError,
    build_execution_completed,
    build_execution_failed,
    build_execution_started,
    validate_simulation_event,
)
import worker_lock
from simulation_supervisor import SupervisionError, canonical_gate_blockers, merge_blockers

__all__ = [
    "MANUAL_VERIFICATION_NOTICE",
    "SIGTERM_EXIT_CODE",
    "SIMULATION_COMPLETED_MESSAGE",
    "SIMULATION_FAILED_MESSAGE",
    "SIMULATION_STARTED_MESSAGE",
    "SimulationBlockedError",
    "SimulationProcessingError",
    "SimulationResult",
    "main",
    "process_one_simulation_command",
]

# Event messages. The dashboard reads the payloads.
SIMULATION_STARTED_MESSAGE = "Simulation started: SO-101 digital twin, simulation only"
SIMULATION_COMPLETED_MESSAGE = "Simulation completed: joint _1 moved, verified and restored"
SIMULATION_FAILED_MESSAGE = "Simulation failed: see the error and the stop result"

# Stages of SimulationProcessingError added by process supervision (A2).
STAGE_INTERRUPTED_WRITE = "interrupted_write"  # an audit write was interrupted: unknown
STAGE_SESSION_CLEANUP = "session_cleanup"  # a twin child could not be confirmed stopped
# An interruption OUTSIDE an audit call while the lifecycle was building or
# between records (for example while the execution_failed payload was built,
# or after an event was written but before its status change).
STAGE_INTERRUPTED_RECORDING = "interrupted_recording"

# SIGTERM ends a run as SystemExit with the conventional 128 + 15 exit code.
SIGTERM_EXIT_CODE = 143


class SimulationBlockedError(Exception):
    """Simulation execution is blocked. Nothing was claimed, written or sent."""


class SimulationProcessingError(CommandProcessingError):
    """A write failed during the simulation lifecycle. A person has to look.

    ``stage`` and the other ``CommandProcessingError`` fields mean the same as
    in the dry-run worker. The extra fields are what manual recovery needs:

    * ``movement_dispatched``: True if a movement call was started (the arm may
      have moved), False if nothing was sent;
    * ``failure_kind`` / ``failure_detail``: the execution failure, if any;
    * ``stop``: ``confirmed``, ``unconfirmed`` or ``not_needed``, if a failure
      was being recorded;
    * ``last_telemetry``: the last validated reading (snapshot payload), if any.
    """

    def __init__(
        self,
        message: str,
        *,
        movement_dispatched: bool,
        failure_kind: str | None = None,
        failure_detail: str | None = None,
        stop: str | None = None,
        last_telemetry: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(message, **fields)
        self.movement_dispatched = movement_dispatched
        self.failure_kind = failure_kind
        self.failure_detail = failure_detail
        self.stop = stop
        self.last_telemetry = last_telemetry


@dataclass(frozen=True)
class SimulationResult:
    """What one call did.

    * ``no_command``: nothing was claimed.
    * ``not_approved``: the command was decided ``rejected`` or ``failed`` by
      the checks (or the planner). Nothing was connected or sent.
    * ``executed``: an approved plan went into the simulation lifecycle.
      ``execution`` is ``completed`` or ``failed``.
    """

    outcome: str
    decision: ProcessResult  # the claim, planner, validator and critic part
    final_status: str | None = None
    execution: str | None = None
    error_kind: str | None = None
    error_detail: str | None = None
    stop: str | None = None  # set for a failed execution
    movement_dispatched: bool = False
    # completed: {"start", "target", "restored"}; failed: {"last": ...} if read
    telemetry: dict[str, Any] | None = None
    # A movement may have been sent and the stop is not confirmed.
    manual_verification_required: bool = False


def _production_twin() -> Any:
    """The Cyberwave twin for a real simulation run. Not implemented: always refuses.

    Writing it needs the confirmed stop method and telemetry contract. It must
    never be a live-hardware client, and must never call ``affect()``.
    """
    raise SimulationBlockedError(
        "no Cyberwave simulation adapter is implemented; it cannot be written until "
        "the Cyberwave engineers confirm the stop method and the telemetry provenance"
    )


def process_one_simulation_command(
    worker_id: str,
    *,
    twin: Any,
    client: Any | None = None,
    critic: CriticCall | None = None,
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
) -> SimulationResult:
    """Claim and process at most one command through the simulation lifecycle.

    ``twin`` is the supervised twin-session provider and is required: there is
    no default and no Cyberwave implementation (in production it would be a
    ``simulation_supervisor.ProcessSessionProvider``; tests pass a provider of a
    fake). Its gate check AND the parent's canonical ``CYBERWAVE_GATES`` must be
    fully confirmed, otherwise ``SimulationBlockedError`` is raised BEFORE
    anything is claimed; so is it for anything that cannot start a supervised
    session. ``timeouts`` may only be tightened.

    Raises whatever the claim raises, ``CommandProcessingError`` for a failed
    write in the decision part, and ``SimulationProcessingError`` for one in the
    simulation lifecycle.
    """
    blockers = _provider_gate_blockers(twin)
    if blockers:
        raise SimulationBlockedError("; ".join(blockers))
    if not isinstance(timeouts, Timeouts):
        raise TypeError("timeouts must be a Timeouts instance.")
    if not callable(getattr(twin, "start", None)):
        raise SimulationBlockedError("no supervised twin session provider was given")

    decided = decide_one_command(worker_id, client=client, critic=critic)
    if decided.approved is None:
        if decided.result.outcome == "no_command":
            return SimulationResult(outcome="no_command", decision=decided.result)
        # A planner failure carries no final_status of its own: it was set to 'failed'.
        status = decided.result.final_status or "failed"
        return SimulationResult(outcome="not_approved", decision=decided.result, final_status=status)
    return _simulation_lifecycle(decided.result, decided.approved, twin, client, timeouts)


# --------------------------------------------------------------------------
# The lifecycle
# --------------------------------------------------------------------------


def _provider_gate_blockers(twin: Any) -> tuple[str, ...]:
    """The session provider's own gate check plus the parent's canonical gates."""
    if callable(getattr(type(twin), "gate_blockers", None)):
        try:
            own = tuple(twin.gate_blockers())
        except Exception:
            own = ("the twin session gate check failed",)
    else:
        own = twin_gate_blockers(twin)
    return merge_blockers(own, canonical_gate_blockers())


def _write_event(
    command_id: str, event_type: str, message: str, payload: dict[str, Any], client: Any | None
) -> None:
    """The only way a simulation event is written: validated first, one insert."""
    validate_simulation_event(event_type, payload)
    try:
        audit_store.append_event(command_id, event_type, message, payload, client=client)
    except Exception:
        raise
    except BaseException as exc:
        raise _InterruptedWrite(f"the {event_type} insert", exc) from exc


def _verify_approval(approved: Any) -> PlanSnapshot:
    """The approval must still hold for exactly this snapshot. Pure."""
    if not isinstance(approved, ApprovedPlan) or not isinstance(approved.snapshot, PlanSnapshot):
        raise GateError("approval_invalid", "no approved plan was given")
    snapshot = approved.snapshot
    validation = approved.validation
    if (
        not isinstance(validation, ValidationResult)
        or not validation.passed
        or validation.plan_id != snapshot.plan_id
    ):
        raise GateError("approval_invalid", "there is no validator pass for this plan")
    try:
        request = snapshot.critic_request()  # rebuilt from the snapshot, re-hashed
    except Exception as exc:
        raise GateError(
            "approval_invalid", f"the approved plan could not be re-hashed ({type(exc).__name__})"
        ) from None
    gate = decide_execution(request, approved.decision)
    if not gate.allowed:
        reason = gate.reasons[0] if gate.reasons else "no reason given"
        raise GateError(
            "approval_invalid", f"the approval did not re-verify: {reason}"[:MAX_TEXT_LENGTH]
        )
    return snapshot


def _preflight(approved: ApprovedPlan, twin: Any) -> None:
    snapshot = _verify_approval(approved)
    action = require_first_rehearsal_plan(snapshot)
    blockers = _provider_gate_blockers(twin)
    if blockers:
        raise GateError("gate_unconfirmed", f"movement is blocked: {blockers[0]}")
    revalidate_against_shared_limits(snapshot.plan_id, [action])


def _summary(success: RehearsalSuccess) -> str:
    joint = FIRST_REHEARSAL_JOINT
    text = (
        f"Simulation only. Joint {joint} moved to {FIRST_REHEARSAL_TARGET_RAD} rad "
        f"(read {success.target.joints[joint]:.4f}) and was restored to "
        f"{success.start.joints[joint]:.4f} rad (read {success.restored.joints[joint]:.4f}). "
        f"All six joints verified within {TOLERANCE_RAD} rad."
    )
    return text[:MAX_TEXT_LENGTH].strip()


def _snapshot_payload(snapshot: Any) -> dict[str, Any] | None:
    """The payload of a validated reading, or None if it cannot be built."""
    if snapshot is None:
        return None
    try:
        return snapshot.to_payload()
    except Exception:
        return None


@dataclass
class _Progress:
    """What this run knows for certain, for a manual-recovery message.

    ``events_written`` and ``status`` change ONLY after the audit store confirmed
    the write (``confirm`` / ``confirm_status``), so a report never claims an
    event that was merely built. ``stage`` names the lifecycle step in progress.
    """

    events_written: list[str]
    dispatched: bool = False
    stop: str | None = None
    kind: str | None = None
    detail: str | None = None
    last: dict[str, Any] | None = None
    restored_verified: bool = False
    status: str = "approved"  # the last status this run confirmed
    stage: str = "the pre-checks"
    stage_event_written: bool | None = False  # the event this stage is about
    intended_status: str | None = None
    earlier_interruption: str | None = None
    # An interruption re-raised on purpose, matched by IDENTITY. Known minor limit:
    # if the very same exception object were raised again in a later recording
    # window it would pass through unconverted. A real Ctrl-C creates a new object
    # each time; only a test that reuses one instance could hit this.
    passthrough: BaseException | None = None
    session: Any = None  # a twin session that is open and must be closed

    def at(self, stage: str, *, event_written: bool | None = False, intended: str | None = None) -> None:
        self.stage = stage
        self.stage_event_written = event_written
        self.intended_status = intended

    def confirm(self, event_type: str) -> None:
        """``event_type`` was confirmed written by the audit store."""
        self.events_written.append(event_type)
        self.at(f"after {event_type} was written", event_written=True, intended=self.intended_status)

    def confirm_status(self, status: str) -> None:
        """The conditional status change to ``status`` was confirmed."""
        self.status = status
        self.at(f"after the status change to '{status}'", event_written=True)

    def durable(self) -> str:
        return (
            f"events confirmed written: {', '.join(self.events_written) or 'none'}; "
            f"last status confirmed by this run: '{self.status}'"
        )

    def summary(self) -> str:
        if self.restored_verified:
            return "Movement sent; this run verified the restoration of all six joints."
        text = (
            f"Movement sent: {'YES (it may have been sent)' if self.dispatched else 'no'}. "
            f"Stop: {self.stop or 'not attempted'}."
        )
        if self.dispatched and self.stop != STOP_CONFIRMED:
            text += f" {MANUAL_VERIFICATION_NOTICE}"
        return text


class _InterruptedWrite(BaseException):
    """An audit write was interrupted: its outcome is unknown. Not an Exception on
    purpose, so no ``except Exception`` handler treats it as an ordinary failure
    (which would write a further event)."""

    def __init__(self, label: str, original: BaseException) -> None:
        super().__init__(label)
        self.label = label
        self.original = original


def _set_status(command_id: str, expected: str, status: str, client: Any | None) -> Any:
    """The only way a simulation status change is made: one conditional update."""
    try:
        return audit_store.set_command_status(
            command_id, expected_status=expected, status=status, client=client
        )
    except Exception:
        raise
    except BaseException as exc:
        raise _InterruptedWrite(f"the {expected} -> {status} status update", exc) from exc


def _add_outcome_note(exc: BaseException, result: SimulationResult) -> None:
    note = (
        "Simulation outcome recorded before the interruption continued: "
        f"execution {result.execution}, status {result.final_status}, "
        f"error {result.error_kind or 'none'}, "
        f"movement sent {'yes' if result.movement_dispatched else 'no'}, "
        f"stop {result.stop or 'n/a'}."
    )
    if result.manual_verification_required:
        note += f" {MANUAL_VERIFICATION_NOTICE}"
    try:
        exc.add_note(note)
    except Exception:
        pass


def _record_then_reraise(progress: _Progress, exc: BaseException, record: Any) -> NoReturn:
    """Record the failure caused by ``exc``, then continue with ``exc`` itself."""
    progress.earlier_interruption = type(exc).__name__
    try:
        result = record()
    except SimulationProcessingError as error:
        raise error from exc
    _add_outcome_note(exc, result)
    progress.passthrough = exc
    raise exc


def _interrupted_text(exc: BaseException, when: str) -> str:
    name = type(exc).__name__
    if interruption_kind(exc) == "interrupted":
        return f"the run was interrupted ({name}) {when}"
    return f"the run ended on an unexpected {name} {when}"


def _close_session(session: Any) -> tuple[str | None, BaseException | None]:
    """Close the session. Never raises. Returns (a cleanup problem, an interruption).

    An interruption during cleanup is returned as is (``close()`` finished every
    termination step first); a cleanup failure that came with it is still
    returned as the problem, never lost.
    """
    try:
        session.close()
    except SupervisionError:
        return "the twin process could not be confirmed stopped", None
    except Exception as exc:
        return f"closing the twin session failed ({type(exc).__name__})", None
    except BaseException as exc:
        failure = getattr(session, "cleanup_failure", None)
        return (failure if type(failure) is str else None), exc
    return None, None


def _close_open_session(progress: _Progress) -> str:
    """Close a session still open after an unexpected interruption. Never raises."""
    session, progress.session = progress.session, None
    if session is None:
        return ""
    problem, _late = _close_session(session)
    return f" Twin session closed; cleanup: {problem}." if problem else " Twin session closed."


def _simulation_lifecycle(
    decision: ProcessResult,
    approved: ApprovedPlan,
    twin: Any,
    client: Any | None,
    timeouts: Timeouts,
) -> SimulationResult:
    progress = _Progress(events_written=list(approved.events_written))
    snapshot = approved.snapshot
    try:
        return _lifecycle_steps(decision, approved, twin, client, timeouts, progress)
    except _InterruptedWrite as stopped:
        cleanup = _close_open_session(progress)
        raise SimulationProcessingError(
            f"INTERRUPTED DURING A WRITE for command {snapshot.command_id}, plan "
            f"{snapshot.plan_id}: {stopped.label} was interrupted "
            f"({type(stopped.original).__name__}). Its outcome is unknown, so nothing further "
            "was written and this run changed no status after it. Last confirmed durable "
            f"state: {progress.durable()}. {progress.summary()}{cleanup} Manual recovery "
            "needed: check the timeline and the status before changing anything.",
            command_id=snapshot.command_id,
            stage=STAGE_INTERRUPTED_WRITE,
            event_written=None,
            plan_id=snapshot.plan_id,
            events_written=tuple(progress.events_written),
            intended_status=progress.intended_status,
            movement_dispatched=progress.dispatched,
            failure_kind=progress.kind,
            failure_detail=progress.detail,
            stop=progress.stop,
            last_telemetry=progress.last,
        ) from stopped.original
    except Exception:
        raise
    except BaseException as exc:
        if exc is progress.passthrough:
            raise  # recorded first; the interruption continues on purpose
        # An interruption OUTSIDE an audit call: while a record was being built,
        # or between a confirmed write and the next step. Nothing further is
        # written, nothing is retried and no status is changed.
        cleanup = _close_open_session(progress)
        earlier = (
            f" (it interrupted the recording of an earlier {progress.earlier_interruption})"
            if progress.earlier_interruption else ""
        )
        raise SimulationProcessingError(
            f"INTERRUPTED WHILE RECORDING for command {snapshot.command_id}, plan "
            f"{snapshot.plan_id}: {type(exc).__name__}{earlier} during {progress.stage}. Last "
            f"confirmed durable state: {progress.durable()}. Nothing further was written, no "
            f"write was retried and no status was changed after the interruption. "
            f"{progress.summary()}{cleanup} Manual recovery needed: check the timeline and the "
            "status before changing anything; do NOT write a second execution_failed.",
            command_id=snapshot.command_id,
            stage=STAGE_INTERRUPTED_RECORDING,
            event_written=progress.stage_event_written,
            plan_id=snapshot.plan_id,
            events_written=tuple(progress.events_written),
            intended_status=progress.intended_status,
            movement_dispatched=progress.dispatched,
            failure_kind=progress.kind,
            failure_detail=progress.detail,
            stop=progress.stop,
            last_telemetry=progress.last,
        ) from exc


def _lifecycle_steps(
    decision: ProcessResult,
    approved: ApprovedPlan,
    twin: Any,
    client: Any | None,
    timeouts: Timeouts,
    progress: _Progress,
) -> SimulationResult:
    snapshot = approved.snapshot
    command_id = snapshot.command_id
    plan_id = snapshot.plan_id
    events_written = progress.events_written

    def fail(
        known_status: str,
        kind: str,
        detail: str,
        context: str,
        *,
        stop: str = STOP_NOT_NEEDED,
        last: dict[str, Any] | None = None,
        dispatched: bool = False,
    ) -> SimulationResult:
        progress.dispatched, progress.stop = dispatched, stop
        progress.kind, progress.detail, progress.last = kind, detail, last
        return _record_failure(
            decision, snapshot, client, events_written,
            known_status=known_status, kind=kind, detail=detail, context=context,
            stop=stop, last=last, dispatched=dispatched, progress=progress,
        )

    # 1. Pre-checks. Pure: nothing is connected, read or sent.
    progress.at("the pre-checks (nothing connected or sent)")
    try:
        _preflight(approved, twin)
    except GateError as exc:
        return fail(
            "approved", exc.kind, exc.detail,
            f"the simulation was refused before it started ({exc.kind}). Nothing was sent.",
        )
    except Exception as exc:  # fail closed
        return fail(
            "approved", "internal_error", f"pre-check failed unexpectedly ({type(exc).__name__})",
            f"a pre-check failed unexpectedly ({type(exc).__name__}). Nothing was sent.",
        )
    except BaseException as exc:
        # Interrupted before execution_started: execution_failed, then
        # approved -> failed (the documented order), then the interruption.
        interrupted = exc
        _record_then_reraise(progress, interrupted, lambda: fail(
            "approved", interruption_kind(interrupted),
            _interrupted_text(interrupted, "before the simulation started"),
            f"the run was interrupted ({type(interrupted).__name__}) before the simulation "
            "started. Nothing was connected or sent.",
        ))

    # 2. execution_started. Nothing else happens unless it is written.
    progress.at("the execution_started insert (its outcome is unknown if interrupted here)",
                event_written=None)
    try:
        _write_event(
            command_id, "execution_started", SIMULATION_STARTED_MESSAGE,
            build_execution_started(), client,
        )
    except Exception as exc:
        started = _written_flag(exc)
        return fail(
            "approved",
            "event_write_failed",
            f"execution_started could not be recorded ({type(exc).__name__}); nothing was sent",
            f"execution_started insert failed ({_describe(exc)})"
            + ("; it MAY have been written" if started is None else "; not written")
            + ". Nothing was connected, read or sent.",
        )
    progress.confirm("execution_started")

    # 3. approved -> executing. The simulation is touched only if this succeeded.
    progress.at("the approved -> executing status update (its outcome is unknown if "
                "interrupted here)", event_written=True, intended="executing")
    try:
        _set_status(command_id, "approved", "executing", client)
    except StateConflictError as exc:
        # Someone else changed the status: it is not known, so it is not touched.
        progress.at("recording that approved -> executing matched no row: building "
                    "execution_failed (not written)", event_written=False)
        note = _append_failed_once(
            command_id,
            build_execution_failed(
                "status_update_failed",
                "approved -> executing matched no row; nothing was sent",
                STOP_NOT_NEEDED,
            ),
            client,
            progress,
        )
        raise SimulationProcessingError(
            f"EXECUTING UPDATE FAILED for command {command_id}, plan {plan_id}: approved -> "
            f"executing matched no row ({_describe(exc)}): the status is no longer 'approved'. "
            f"Nothing was connected, read or sent to the simulation. {note} Written: "
            f"{', '.join(events_written)}. The status was NOT changed by this run; it is "
            "unknown. Manual recovery needed: look at the command before changing anything.",
            command_id=command_id,
            stage=STAGE_EXECUTING_UPDATE,
            event_written=True,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status=None,
            movement_dispatched=False,
            failure_kind="status_update_failed",
            stop=STOP_NOT_NEEDED,
        ) from None
    except Exception as exc:
        # Not applied, or ambiguous. The conditional approved -> failed can only
        # apply if the command is still 'approved'.
        return fail(
            "approved",
            "status_update_failed",
            f"approved -> executing failed ({type(exc).__name__}); nothing was sent",
            f"approved -> executing failed ({_describe(exc)}); {_status_state(exc)}. "
            "Nothing was connected, read or sent.",
        )
    progress.confirm_status("executing")

    # 4. The supervised twin session: children started and READY before the
    #    rehearsal clock. Nothing is connected or sent while it starts.
    progress.at("starting the twin session (nothing connected or sent)")

    def remember_session(created: Any) -> None:
        # Called by the provider BEFORE any child is spawned, so there is no
        # moment (between start() returning and an assignment here) in which a
        # live session exists that an interrupt could not find and close.
        progress.session = created

    try:
        session = twin.start(on_created=remember_session)
    except Exception as exc:
        progress.session = None  # start() already cleaned up after a failed start
        return fail(
            "executing", "connection_failed",
            f"the twin process could not be started ({type(exc).__name__}); nothing was sent",
            f"the twin process could not be started ({type(exc).__name__}). Nothing was "
            "connected or sent.",
        )
    except BaseException as exc:
        interrupted = exc
        # The session was handed over before any child existed (remember_session):
        # close it here, whatever point of start() the interruption reached. A
        # session that start() already cleaned up closes again harmlessly.
        cleanup = _close_open_session(progress)
        _record_then_reraise(progress, interrupted, lambda: fail(
            "executing", interruption_kind(interrupted),
            _interrupted_text(interrupted, "while the twin process was starting"),
            f"the run was interrupted ({type(interrupted).__name__}) while the twin process "
            f"was starting. Nothing was connected or sent.{cleanup}",
        ))

    # 5. The rehearsal, on the snapshot that was validated and approved. The
    #    session is closed (every child verified gone) before anything is recorded.
    progress.session = session  # already set by remember_session; kept for providers that ignore on_created
    progress.at("the rehearsal")
    interrupt: BaseException | None = None
    try:
        outcome: Any = run_rehearsal(snapshot, session, timeouts)
    except Exception as exc:  # run_rehearsal never raises Exception; fail closed anyway
        outcome = RehearsalFailure(
            kind="internal_error",
            detail=f"the rehearsal raised unexpectedly ({type(exc).__name__})",
            stop=STOP_UNCONFIRMED, last_telemetry=None,
            target_dispatched=True, return_dispatched=False,
        )
    except BaseException as exc:
        interrupt = exc
        outcome = getattr(exc, "rehearsal_outcome", None)
    progress.at("closing the twin session")
    cleanup_problem, late = _close_session(session)
    progress.session = None
    if interrupt is None:
        interrupt = late
    if interrupt is not None:
        progress.earlier_interruption = type(interrupt).__name__
    if interrupt is not None and not isinstance(outcome, (RehearsalSuccess, RehearsalFailure)):
        outcome = RehearsalFailure(
            kind=interruption_kind(interrupt),
            detail="the run was interrupted before it reported an outcome",
            stop=STOP_UNCONFIRMED, last_telemetry=None,
            target_dispatched=True, return_dispatched=False,
        )

    try:
        result = _record_rehearsal(
            outcome, fail, decision, snapshot, client, events_written, progress
        )
    except SimulationProcessingError as error:
        if interrupt is not None:
            raise error from interrupt
        raise
    if cleanup_problem is not None:
        dispatched = bool(result.movement_dispatched)
        raise SimulationProcessingError(
            f"TWIN PROCESS CLEANUP FAILED for command {command_id}, plan {plan_id}: "
            f"{cleanup_problem}. The outcome was recorded (execution {result.execution}, "
            f"status {result.final_status}). A local twin process may still be running: "
            "stop it before anything else."
            + (f" {MANUAL_VERIFICATION_NOTICE}" if dispatched else ""),
            command_id=command_id,
            stage=STAGE_SESSION_CLEANUP,
            event_written=True,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status=result.final_status,
            movement_dispatched=dispatched,
            failure_kind=result.error_kind,
            failure_detail=result.error_detail,
            stop=result.stop,
        ) from interrupt
    if interrupt is not None:
        _add_outcome_note(interrupt, result)
        progress.passthrough = interrupt
        raise interrupt
    return result


def _record_rehearsal(
    outcome: Any,
    fail: Any,
    decision: ProcessResult,
    snapshot: PlanSnapshot,
    client: Any | None,
    events_written: list[str],
    progress: _Progress,
) -> SimulationResult:
    command_id = snapshot.command_id
    plan_id = snapshot.plan_id
    if isinstance(outcome, RehearsalFailure):
        moved = (
            "A movement call MAY have been sent: the arm may have moved."
            if outcome.target_dispatched
            else "No movement was sent."
        )
        return fail(
            "executing", outcome.kind, outcome.detail,
            f"the simulation failed ({outcome.kind}). {moved} Stop: {outcome.stop}.",
            stop=outcome.stop,
            last=_snapshot_payload(outcome.last_telemetry),
            dispatched=outcome.target_dispatched or outcome.return_dispatched,
        )
    if not isinstance(outcome, RehearsalSuccess):
        return fail(
            "executing", "internal_error", "the rehearsal returned no usable result",
            "the rehearsal returned no usable result; whether a movement was sent is unknown.",
            stop=STOP_UNCONFIRMED, dispatched=True,
        )
    progress.dispatched, progress.stop, progress.restored_verified = True, None, True

    # 6. execution_completed, built only from the verified readings.
    progress.at("building the execution_completed record (not written)",
                event_written=False, intended="completed")
    try:
        telemetry = {
            "start": outcome.start.to_payload(),
            "target": outcome.target.to_payload(),
            "restored": outcome.restored.to_payload(),
        }
        completed = build_execution_completed(_summary(outcome), **telemetry)
    except Exception as exc:
        # The arm moved and was verified restored, but the record cannot be built.
        return fail(
            "executing",
            "internal_error",
            f"the completion record could not be built ({type(exc).__name__})",
            f"the movement ran and the restoration was verified, but the completion payload "
            f"could not be built ({type(exc).__name__}).",
            stop=STOP_UNCONFIRMED,
            last=_snapshot_payload(outcome.restored),
            dispatched=True,
        )
    progress.at("the execution_completed insert (its outcome is unknown if interrupted here)",
                event_written=None, intended="completed")
    try:
        _write_event(command_id, "execution_completed", SIMULATION_COMPLETED_MESSAGE, completed, client)
    except Exception as exc:
        written = _written_flag(exc)
        raise SimulationProcessingError(
            f"EVENT WRITE FAILED for command {command_id}, plan {plan_id}: the simulated "
            "movement SUCCEEDED and all six joints were verified restored, but the "
            f"execution_completed insert failed ({_describe(exc)})"
            + (
                "; it MAY have been written: check the timeline. "
                if written is None
                else "; it was not written. "
            )
            + f"Written: {', '.join(events_written)}. The status is still 'executing'. Do NOT "
            "run the simulation again. Manual recovery needed. Readings: "
            + json.dumps(telemetry, sort_keys=True),
            command_id=command_id,
            stage=STAGE_EXECUTION_COMPLETED_WRITE,
            event_written=written,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status="completed",
            movement_dispatched=True,
            last_telemetry=telemetry["restored"],
        ) from None
    progress.confirm("execution_completed")

    # 7. executing -> completed. Never retried; the event is never written again.
    progress.at("the executing -> completed status update (its outcome is unknown if "
                "interrupted here)", event_written=True, intended="completed")
    try:
        _set_status(command_id, "executing", "completed", client)
    except Exception as exc:
        raise SimulationProcessingError(
            f"STATUS UPDATE FAILED for command {command_id}, plan {plan_id}: "
            "execution_completed WAS written (simulation, arm verified restored), but "
            f"executing -> completed failed ({_describe(exc)}); {_status_state(exc)}. Do NOT "
            "write execution_completed again and do NOT run the simulation again. Manual "
            "recovery needed: set executing -> completed only.",
            command_id=command_id,
            stage=STAGE_COMPLETED_UPDATE,
            event_written=True,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status="completed",
            movement_dispatched=True,
            last_telemetry=telemetry["restored"],
        ) from None
    progress.confirm_status("completed")

    return SimulationResult(
        outcome="executed",
        decision=decision,
        final_status="completed",
        execution="completed",
        movement_dispatched=True,
        telemetry=telemetry,
    )


def _append_failed_once(
    command_id: str, payload: dict[str, Any], client: Any | None, progress: _Progress
) -> str:
    """Try ONCE to write execution_failed. Returns a sentence for an error message."""
    progress.at("the execution_failed insert (its outcome is unknown if interrupted here)",
                event_written=None)
    try:
        _write_event(command_id, "execution_failed", SIMULATION_FAILED_MESSAGE, payload, client)
    except Exception as exc:
        written = _written_flag(exc)
        return (
            f"The execution_failed insert ALSO failed ({_describe(exc)})"
            + ("; it MAY have been written." if written is None else "; it was not written.")
            + " The failure is NOT recorded in the audit log."
        )
    progress.confirm("execution_failed")
    return "execution_failed was written."


def _record_failure(
    decision: ProcessResult,
    snapshot: PlanSnapshot,
    client: Any | None,
    events_written: list[str],
    *,
    known_status: str,
    kind: str,
    detail: str,
    context: str,
    stop: str,
    last: dict[str, Any] | None,
    dispatched: bool,
    progress: _Progress,
) -> SimulationResult:
    """execution_failed first; ONLY if written, ``known_status -> failed``.

    ``known_status`` is ``approved`` or ``executing``. The update is conditional
    on it, so a status that changed meanwhile is never overwritten.
    """
    command_id = snapshot.command_id
    plan_id = snapshot.plan_id
    progress.at("building the execution_failed payload (not written)",
                event_written=False, intended="failed")
    manual = dispatched and stop != STOP_CONFIRMED
    if manual:
        # The notice comes first so a long detail can never truncate it away.
        detail = f"{MANUAL_VERIFICATION_NOTICE} {detail}"[:MAX_TEXT_LENGTH].rstrip()
    try:
        payload = build_execution_failed(kind, detail, stop, last)
    except SimulationPayloadError:
        # Never drop the failure because its description was unusable.
        kind, detail, last = "internal_error", "the failure could not be described", None
        payload = build_execution_failed(kind, detail, stop)

    recovery = (
        f"Failure: {kind} ({detail}). Movement sent: {'YES' if dispatched else 'no'}. "
        f"Stop: {stop}. Last reading: "
        + (json.dumps(last, sort_keys=True) if last is not None else "none")
        + "."
        + (f" {MANUAL_VERIFICATION_NOTICE}" if manual else "")
    )
    extra = {
        "movement_dispatched": dispatched,
        "failure_kind": kind,
        "failure_detail": detail,
        "stop": stop,
        "last_telemetry": last,
    }
    progress.at("the execution_failed insert (its outcome is unknown if interrupted here)",
                event_written=None, intended="failed")
    try:
        _write_event(command_id, "execution_failed", SIMULATION_FAILED_MESSAGE, payload, client)
    except Exception as exc:
        written = _written_flag(exc)
        raise SimulationProcessingError(
            f"EXECUTION FAILURE NOT RECORDED for command {command_id}, plan {plan_id}: "
            f"{context} The execution_failed insert then failed ({_describe(exc)})"
            + (
                "; it MAY have been written: check the timeline. "
                if written is None
                else "; it was not written, so the failure is NOT in the audit log. "
            )
            + f"Written: {', '.join(events_written)}. The status was NOT changed (it should "
            f"still be '{known_status}'). Manual recovery needed. {recovery}",
            command_id=command_id,
            stage=STAGE_EXECUTION_FAILED_WRITE,
            event_written=written,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status="failed",
            **extra,
        ) from None
    progress.confirm("execution_failed")

    progress.at(f"the {known_status} -> failed status update (its outcome is unknown if "
                "interrupted here)", event_written=True, intended="failed")
    try:
        _set_status(command_id, known_status, "failed", client)
    except Exception as exc:
        raise SimulationProcessingError(
            f"STATUS UPDATE FAILED for command {command_id}, plan {plan_id}: {context} "
            f"execution_failed WAS written, but {known_status} -> failed failed "
            f"({_describe(exc)}); {_status_state(exc)}. Do NOT write execution_failed again. "
            f"Manual recovery needed: check the status, then set {known_status} -> failed only "
            f"if it is still '{known_status}'. {recovery}",
            command_id=command_id,
            stage=STAGE_FAILED_UPDATE,
            event_written=True,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status="failed",
            **extra,
        ) from None
    progress.confirm_status("failed")

    return SimulationResult(
        outcome="executed",
        decision=decision,
        final_status="failed",
        execution="failed",
        error_kind=kind,
        error_detail=detail,
        stop=stop,
        movement_dispatched=dispatched,
        telemetry={"last": last} if last is not None else None,
        manual_verification_required=manual,
    )


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _report(result: SimulationResult) -> str:
    decision = result.decision
    if result.outcome == "no_command":
        return "No eligible command. Nothing claimed, nothing written, nothing sent."
    if result.outcome == "not_approved":
        return (
            f"Command {decision.command_id}: not approved (status {result.final_status}; "
            f"validator {decision.validator_outcome}, critic {decision.critic_outcome}). "
            "Nothing was connected or sent."
        )
    line = (
        f"Command {decision.command_id}: plan {decision.plan_id}. Simulation "
        f"{result.execution}; status now {result.final_status}. "
        f"Movement sent: {'yes' if result.movement_dispatched else 'no'}."
    )
    if result.execution == "failed":
        line += f" Error: {result.error_kind} ({result.error_detail}). Stop: {result.stop}."
    if result.manual_verification_required:
        line += (
            " PHYSICAL OUTCOME UNKNOWN: a movement may have been sent and no reliable stop "
            "was established. Manual verification required."
        )
    if result.telemetry is not None:
        line += " Readings: " + json.dumps(result.telemetry, sort_keys=True)
    return line + " Simulation only: no physical hardware."


@contextlib.contextmanager
def _sigterm_as_system_exit() -> Any:
    """While active, the first SIGTERM to this process raises ``SystemExit(SIGTERM_EXIT_CODE)``.

    Python runs signal handlers in the main thread between bytecodes, so the
    exit request surfaces where a Ctrl-C would. Later SIGTERMs are ignored: the
    run is already ending, and a second interruption would abandon the stop
    attempt or interrupt the execution_failed write. The previous handler is
    always restored. It cannot protect the twin child processes from a SIGTERM
    sent to the whole process group (see the module docstring).
    """
    received = False

    def handler(_signum: int, _frame: Any) -> None:
        nonlocal received
        if received:
            return
        received = True
        raise SystemExit(SIGTERM_EXIT_CODE)

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Process ONE command through the simulation-only SO-101 rehearsal "
        "(set joint _1 to 0.25 rad, verify, return, verify). Needs BOTH flags. "
        "Currently BLOCKED pending Cyberwave engineer confirmation. There is no "
        "physical-hardware mode."
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="required: ask for the simulation rehearsal instead of doing nothing",
    )
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="required: allow the claim, the critic request and the audit/status writes",
    )
    parser.add_argument("--worker-id", default=DEFAULT_WORKER_ID)
    worker_lock.add_lock_argument(parser)
    args = parser.parse_args(argv)

    if not (args.simulation and args.confirm_write):
        print(
            "Refusing to run: the simulation needs BOTH --simulation and --confirm-write. "
            "Nothing was claimed, written or sent. (The default worker, "
            "command_processor.py, is dry-run only.)"
        )
        return 2

    # The SDK reads CYBERWAVE_* endpoint variables on its own; a stray host would
    # receive the API key. Refuse before the lock, the claim or any write.
    try:
        load_cyberwave_endpoints()
    except ConfigError as exc:
        print(f"REFUSED: {exc}")
        return 4

    blockers = live_simulation_blockers()
    if blockers:
        print(
            f"BLOCKED: {EXECUTION_TARGET} execution is not enabled. "
            "Nothing was claimed, written or sent. Open items:"
        )
        for blocker in blockers:
            print(f"  - {blocker}")
        return 3

    # The single-instance lock comes before the first claim and is held until
    # this run ends. Without fcntl there is no lock, so the simulation refuses.
    lock = worker_lock.WorkerLock(worker_lock.resolve_path(args.lock_file))
    try:
        lock.acquire()
    except worker_lock.LockError as exc:
        print(f"REFUSED: {exc}. Nothing was claimed, written or sent.")
        return exc.exit_code
    try:
        return _run_locked(args)
    finally:
        lock.unlock()


def _run_locked(args: argparse.Namespace) -> int:
    try:
        with _sigterm_as_system_exit():
            twin = _production_twin()
            result = process_one_simulation_command(args.worker_id, twin=twin)
    except (KeyboardInterrupt, SystemExit) as exc:
        # The outcome was recorded first (see the notes); the interruption then
        # ends the program exactly as it would have.
        for note in getattr(exc, "__notes__", None) or ():
            print(redact(note))
        raise
    except SimulationBlockedError as exc:
        print(f"BLOCKED: {exc}. Nothing was claimed, written or sent.")
        return 3
    except CommandProcessingError as exc:
        print(f"FAILED at {exc.stage}: {redact(exc)}")
        if isinstance(exc.__cause__, (KeyboardInterrupt, SystemExit)):
            raise exc.__cause__
        return 1
    except Exception as exc:
        print(f"Claim failed, nothing processed: {redact(_describe(exc))}")
        return 1

    print(_report(result))
    return 0 if result.execution != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
