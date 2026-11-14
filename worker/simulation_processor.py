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
5. ``run_rehearsal``: verify simulation, start reading, move, verify, return,
   verify restored. See ``simulation_executor.py``.
6. Success: ``execution_completed`` (start, target and restored readings), then
   ``executing -> completed``. The completion event is built only from a
   verified restoration.
7. Failure: ``execution_failed`` (fixed error kind, the real stop result, the
   last reading only if one validated), then ``executing -> failed``.

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
import json
import sys
from dataclasses import dataclass
from typing import Any

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
from config import redact
from execution_gate import decide_execution
from plan_snapshot import PlanSnapshot
from safety_validator import ValidationResult
from simulation_executor import (
    DEFAULT_TIMEOUTS,
    RehearsalFailure,
    RehearsalSuccess,
    Timeouts,
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
)
from simulation_payloads import (
    MAX_TEXT_LENGTH,
    STOP_NOT_NEEDED,
    STOP_UNCONFIRMED,
    TOLERANCE_RAD,
    SimulationPayloadError,
    build_execution_completed,
    build_execution_failed,
    build_execution_started,
    validate_simulation_event,
)

__all__ = [
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

    ``twin`` implements ``simulation_executor.SimulationTwin`` and is required:
    there is no default and no Cyberwave implementation (tests pass a fake). If
    its gates are not all confirmed, ``SimulationBlockedError`` is raised BEFORE
    anything is claimed. ``timeouts`` may only be tightened.

    Raises whatever the claim raises, ``CommandProcessingError`` for a failed
    write in the decision part, and ``SimulationProcessingError`` for one in the
    simulation lifecycle.
    """
    blockers = live_simulation_blockers(getattr(twin, "confirmations", None))
    if blockers:
        raise SimulationBlockedError("; ".join(blockers))
    if not isinstance(timeouts, Timeouts):
        raise TypeError("timeouts must be a Timeouts instance.")

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


def _write_event(
    command_id: str, event_type: str, message: str, payload: dict[str, Any], client: Any | None
) -> None:
    """The only way a simulation event is written: validated first, one insert."""
    validate_simulation_event(event_type, payload)
    audit_store.append_event(command_id, event_type, message, payload, client=client)


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
    blockers = live_simulation_blockers(getattr(twin, "confirmations", None))
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


def _simulation_lifecycle(
    decision: ProcessResult,
    approved: ApprovedPlan,
    twin: Any,
    client: Any | None,
    timeouts: Timeouts,
) -> SimulationResult:
    snapshot = approved.snapshot
    command_id = snapshot.command_id
    plan_id = snapshot.plan_id
    events_written = list(approved.events_written)

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
        return _record_failure(
            decision, snapshot, client, events_written,
            known_status=known_status, kind=kind, detail=detail, context=context,
            stop=stop, last=last, dispatched=dispatched,
        )

    # 1. Pre-checks. Pure: nothing is connected, read or sent.
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

    # 2. execution_started. Nothing else happens unless it is written.
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
    events_written.append("execution_started")

    # 3. approved -> executing. The simulation is touched only if this succeeded.
    try:
        audit_store.set_command_status(
            command_id, expected_status="approved", status="executing", client=client
        )
    except StateConflictError as exc:
        # Someone else changed the status: it is not known, so it is not touched.
        note = _append_failed_once(
            command_id,
            build_execution_failed(
                "status_update_failed",
                "approved -> executing matched no row; nothing was sent",
                STOP_NOT_NEEDED,
            ),
            client,
            events_written,
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

    # 4. The rehearsal, on the snapshot that was validated and approved.
    outcome = run_rehearsal(snapshot, twin, timeouts)
    if isinstance(outcome, RehearsalFailure):
        moved = (
            "A movement call WAS started: the arm may have moved."
            if outcome.target_dispatched
            else "No movement was sent."
        )
        return fail(
            "executing", outcome.kind, outcome.detail,
            f"the simulation failed ({outcome.kind}). {moved} Stop: {outcome.stop}.",
            stop=outcome.stop,
            last=_snapshot_payload(outcome.last_telemetry),
            dispatched=outcome.target_dispatched,
        )
    if not isinstance(outcome, RehearsalSuccess):
        return fail(
            "executing", "internal_error", "the rehearsal returned no usable result",
            "the rehearsal returned no usable result; whether a movement was sent is unknown.",
            stop=STOP_UNCONFIRMED, dispatched=True,
        )

    # 5. execution_completed, built only from the verified readings.
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
    events_written.append("execution_completed")

    # 6. executing -> completed. Never retried; the event is never written again.
    try:
        audit_store.set_command_status(
            command_id, expected_status="executing", status="completed", client=client
        )
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

    return SimulationResult(
        outcome="executed",
        decision=decision,
        final_status="completed",
        execution="completed",
        movement_dispatched=True,
        telemetry=telemetry,
    )


def _append_failed_once(
    command_id: str, payload: dict[str, Any], client: Any | None, events_written: list[str]
) -> str:
    """Try ONCE to write execution_failed. Returns a sentence for an error message."""
    try:
        _write_event(command_id, "execution_failed", SIMULATION_FAILED_MESSAGE, payload, client)
    except Exception as exc:
        written = _written_flag(exc)
        return (
            f"The execution_failed insert ALSO failed ({_describe(exc)})"
            + ("; it MAY have been written." if written is None else "; it was not written.")
            + " The failure is NOT recorded in the audit log."
        )
    events_written.append("execution_failed")
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
) -> SimulationResult:
    """execution_failed first; ONLY if written, ``known_status -> failed``.

    ``known_status`` is ``approved`` or ``executing``. The update is conditional
    on it, so a status that changed meanwhile is never overwritten.
    """
    command_id = snapshot.command_id
    plan_id = snapshot.plan_id
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
    )
    extra = {
        "movement_dispatched": dispatched,
        "failure_kind": kind,
        "failure_detail": detail,
        "stop": stop,
        "last_telemetry": last,
    }
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
    events_written.append("execution_failed")

    try:
        audit_store.set_command_status(
            command_id, expected_status=known_status, status="failed", client=client
        )
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
    if result.telemetry is not None:
        line += " Readings: " + json.dumps(result.telemetry, sort_keys=True)
    return line + " Simulation only: no physical hardware."


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
    args = parser.parse_args(argv)

    if not (args.simulation and args.confirm_write):
        print(
            "Refusing to run: the simulation needs BOTH --simulation and --confirm-write. "
            "Nothing was claimed, written or sent. (The default worker, "
            "command_processor.py, is dry-run only.)"
        )
        return 2

    blockers = live_simulation_blockers()
    if blockers:
        print(
            f"BLOCKED: {EXECUTION_TARGET} execution is not enabled. "
            "Nothing was claimed, written or sent. Open items:"
        )
        for blocker in blockers:
            print(f"  - {blocker}")
        return 3

    try:
        twin = _production_twin()
        result = process_one_simulation_command(args.worker_id, twin=twin)
    except SimulationBlockedError as exc:
        print(f"BLOCKED: {exc}. Nothing was claimed, written or sent.")
        return 3
    except CommandProcessingError as exc:
        print(f"FAILED at {exc.stage}: {redact(exc)}")
        return 1
    except Exception as exc:
        print(f"Claim failed, nothing processed: {redact(_describe(exc))}")
        return 1

    print(_report(result))
    return 0 if result.execution != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
