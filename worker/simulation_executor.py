"""The bounded movement sequence of the first SO-101 SIMULATION rehearsal.

Separate from the dry run (``dry_run.py``), which is unchanged and sends
nothing. This module writes no audit event and touches no database: it only
drives a twin SESSION and reports what happened. The audit lifecycle around it
is ``simulation_processor.py``.

IMPORTANT, current state: **nothing in this repository implements
``SimulationTwin`` for Cyberwave.** No code here or anywhere else in the worker
calls an SDK movement method, ``affect()`` or anything that starts a simulation.
The only implementation is the fake twin in ``tests/``. A real adapter may be
written only after the Cyberwave engineers confirm the stop method and the
telemetry provenance (``simulation_gates.CYBERWAVE_GATES``).

Simulation only. The target is the constant ``simulation_gates.EXECUTION_TARGET``.
The sequence refuses to send anything unless BOTH the SDK runtime and the twin
itself report exactly ``"simulation"``; a missing, ambiguous or different answer
is a refusal. There is no live-hardware value, selector or fallback.

Process supervision (A2). The twin runs in a supervised child process
(``simulation_supervisor``). Every twin call has an ABSOLUTE monotonic deadline;
a call that does not finish by it ends the child process, so nothing keeps
running in an abandoned local thread and a terminated child sends nothing else.
Ending a local process does not cancel anything a remote service may already
have accepted.

The sequence (``run_rehearsal``), for the one permitted plan (joint ``_1`` to
0.25 rad):

1. re-hash the approved ``PlanSnapshot`` and re-check the first-rehearsal
   restriction (the command text is never read);
2. require the gates: the session's own check (in the child: the twin carries
   the canonical ``CYBERWAVE_GATES`` object itself, exact type and identity)
   and the parent's canonical gates, every gate confirmed;
3. connect, then verify the SDK runtime target and the twin target;
4. read and validate the START positions of all six joints;
5. re-check the plan and the return target against ``shared/so101-limits.json``;
6. send ONE movement, through the two-phase dispatch handshake: ``_1`` to 0.25;
7. wait, bounded, until ``_1`` is within 0.01 rad of 0.25, then take a fresh
   TARGET reading and verify ``_1`` and that the other five joints did not move;
8. send ONE return movement: ``_1`` to its captured start position;
9. wait, bounded, until all six joints are back, then take a fresh RESTORED
   reading and verify all six within 0.01 rad of the start.

At most two movement calls are ever made, never a third, and nothing is
retried.

Deadlines (absolute ``time.monotonic()``; ``T0`` is when ``run_rehearsal``
starts, after the session's children were started and READY):

* overall ``D_total = T0 + total_s``;
* normal work ``D_work = D_total - recovery_reserve_s``. Every call, movements
  included, is cut off at ``D_work``, so no movement call can use the recovery
  reserve; a timeout of a call whose budget was capped by ``D_work`` is
  ``overall_timeout``;
* termination of a timed-out or crashed child: ``TERMINATE_LIMIT_S`` (inside
  the supervisor call, one absolute deadline per child, never granted twice),
  NOT taken from the stop budget. A child that cannot be confirmed stopped does
  not change the failure kind (the timeout or crash stays the primary failure);
  it is reported by the session's cleanup;
* recovery starts at ``R0`` (failure handling begins, after any termination).
  The stop deadline is ``min(R0 + stop_s, D_total + TERMINATE_LIMIT_S -
  telemetry_read_s - disconnect_s)``; no stop is attempted once it has passed.
  The last read and the disconnect keep their own budgets after that, and
  neither is started once ``D_total + TERMINATE_LIMIT_S`` has passed.

On a failure:

* before any movement may have been sent (the dispatch intent was not
  acknowledged): no stop is attempted, ``stop = "not_needed"``; a gate refusal
  always happens here, before anything is connected or sent;
* after a movement may have been sent (acknowledged intent: even if the call
  raised, hung or the child died, it may still have been sent): at most ONE
  best-effort stop call, never retried, even if the gates no longer pass.
  Through the live session it is ``"confirmed"`` only if the gates (parent and
  child) were valid before and after AND it returned exactly
  ``StopResult(confirmed=True)``. If the session's child had to be terminated,
  the only possible stop is from a FRESH session (``standby_stop``, only when
  the adapter declares it): that result is never a confirmation. Anything else
  is ``"unconfirmed"``. ``RehearsalFailure.manual_verification_required`` is
  True whenever a movement may have been sent and the stop is not confirmed:
  the physical outcome is then unknown;
* ``last_telemetry`` is set only from a reading that validated: one fresh read
  through a live session after a movement failure, or the start reading when
  nothing had moved.

Interruption. ``KeyboardInterrupt``, ``SystemExit`` or any other
``BaseException`` during the run: the stop rules above apply (a second
interruption abandons the stop: ``unconfirmed``), then the ORIGINAL exception
is re-raised with the outcome attached as ``rehearsal_outcome`` (a
``RehearsalFailure`` of kind ``interrupted``, or ``internal_error`` for an
unexpected ``BaseException``), so the caller can record it.

Error kinds are the fixed set in ``simulation_payloads.ERROR_KINDS``; details
are fixed text plus, at most, an exception class name. No SDK error text,
credential, header or URL is ever put in a detail.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from sim_telemetry import (
    JointSnapshot,
    TelemetryRejected,
    joints_outside_tolerance,
    validate_telemetry,
)
from simulation_gates import (
    EXECUTION_TARGET,
    FIRST_REHEARSAL_JOINT,
    FIRST_REHEARSAL_TARGET_RAD,
    GateError,
    LiveSimulationGates,
    load_shared_limits,
    require_first_rehearsal_plan,
    require_within_shared_limits,
    revalidate_against_shared_limits,
)
from simulation_payloads import (
    JOINT_NAMES,
    MAX_TEXT_LENGTH,
    STOP_CONFIRMED,
    STOP_NOT_NEEDED,
    STOP_UNCONFIRMED,
)
from simulation_supervisor import (
    CLEANUP_LIMIT_S,
    TERMINATE_LIMIT_S,
    SessionCrashed,
    SessionRefused,
    SessionTimeout,
    SessionUnusable,
    TwinCallFailed,
    canonical_gate_blockers,
    merge_blockers,
)

__all__ = [
    "DEFAULT_TIMEOUTS",
    "RehearsalFailure",
    "RehearsalSuccess",
    "SimulationTwin",
    "StopResult",
    "Timeouts",
    "interruption_kind",
    "run_rehearsal",
]


# --------------------------------------------------------------------------
# Time limits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Timeouts:
    """Every time limit of one rehearsal, in seconds. The defaults are the caps.

    A caller may pass tighter values (the tests do) but never looser ones: a
    value over its default is refused, as is 0, a negative, NaN or infinity.

    Worst case of the movement phases with the defaults: 10 + 3 + 3 + 3 + 5 +
    10 + 3 + 5 + 10 + 3 = 55 s. Failure recovery (stop, one last read,
    disconnect) is 5 + 3 + 3 = 11 s. ``total_s`` covers both; the movement
    phases are cut off early enough that recovery always fits inside it.
    """

    connect_s: float = 10.0  # SDK connection / initialisation
    mode_check_s: float = 3.0  # each of the two simulation-target checks
    telemetry_read_s: float = 3.0  # one telemetry read (start, polls, last)
    dispatch_s: float = 5.0  # one movement call returning
    target_move_s: float = 10.0  # waiting for _1 to reach the target
    target_verify_s: float = 3.0  # the fresh target reading
    return_move_s: float = 10.0  # waiting for the arm to return
    restore_verify_s: float = 3.0  # the fresh restored reading
    stop_s: float = 5.0  # the stop call
    disconnect_s: float = 3.0
    total_s: float = 75.0  # the whole sequence, recovery included
    poll_interval_s: float = 0.2  # pause between polls while waiting

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= item.default
            ):
                raise ValueError(
                    f"{item.name} must be over 0 and at most {item.default} seconds."
                )
        if self.recovery_reserve_s >= self.total_s:
            raise ValueError("total_s must be longer than the failure-recovery time.")

    @property
    def recovery_reserve_s(self) -> float:
        return self.stop_s + self.telemetry_read_s + self.disconnect_s


DEFAULT_TIMEOUTS = Timeouts()


# --------------------------------------------------------------------------
# The twin interface and the results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StopResult:
    """What a twin's ``stop`` returns.

    ``confirmed`` may be ``True`` ONLY when the engineer-confirmed stop method
    returned its documented success confirmation. Anything else is ``False``.
    """

    confirmed: bool = False


class SimulationTwin(Protocol):
    """What the sequence needs from a twin. No Cyberwave implementation exists.

    There is deliberately no method or argument that selects a target: an
    implementation is for the simulation and nothing else. Every method must
    honour its time limit; the supervisor also enforces it from outside.

    Construction must be side-effect free: building the twin (in the supervised
    child) must not open a connection or perform any remote operation. The
    first remote operation is the explicit, gated ``connect`` call.
    """

    # The confirmations this implementation relies on. It must BE
    # ``simulation_gates.CYBERWAVE_GATES`` (identity is checked); a copy or the
    # twin's own instance is refused, whatever its fields say.
    confirmations: LiveSimulationGates

    def connect(self, timeout_s: float) -> None:
        """Connect to the SIMULATION. Must not start a simulation session."""

    def runtime_target(self) -> Any:
        """The target the SDK runtime itself reports (``"simulation"`` expected)."""

    def twin_target(self) -> Any:
        """The target the SO-101 twin itself reports (``"simulation"`` expected)."""

    def read_telemetry(self, timeout_s: float) -> Any:
        """One read of all six joints, as ``sim_telemetry.RawTelemetry``."""

    def move_joint(self, joint: str, value_rad: float) -> None:
        """Send ONE joint position command in the simulation."""

    def stop(self, timeout_s: float) -> Any:
        """The engineer-confirmed stop method. Returns a ``StopResult``."""

    def disconnect(self) -> None:
        """Release the connection."""



@dataclass(frozen=True)
class RehearsalSuccess:
    """Target reached, verified, returned and all six joints verified restored."""

    start: JointSnapshot
    target: JointSnapshot
    restored: JointSnapshot


@dataclass(frozen=True)
class RehearsalFailure:
    kind: str  # one of simulation_payloads.ERROR_KINDS
    detail: str  # fixed text, at most 200 characters
    stop: str  # confirmed, unconfirmed or not_needed
    last_telemetry: JointSnapshot | None
    target_dispatched: bool  # the target movement may have been sent
    return_dispatched: bool  # the return movement may have been sent

    @property
    def manual_verification_required(self) -> bool:
        """A movement may have been sent and no reliable stop was established."""
        return (self.target_dispatched or self.return_dispatched) and self.stop != STOP_CONFIRMED


class _Abort(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail[:MAX_TEXT_LENGTH]


def interruption_kind(exc: BaseException) -> str:
    """``interrupted`` for Ctrl-C and exit requests; ``internal_error`` for anything else."""
    return "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "internal_error"


def _interruption_detail(exc: BaseException, dispatched: bool) -> str:
    name = type(exc).__name__
    sent = "after a movement may have been sent" if dispatched else "before any movement was sent"
    if interruption_kind(exc) == "interrupted":
        return f"the run was interrupted ({name}) {sent}"
    return f"the run ended on an unexpected {name} {sent}"


def _attach_outcome(exc: BaseException, outcome: Any) -> None:
    try:
        exc.rehearsal_outcome = outcome  # type: ignore[attr-defined]
    except Exception:
        pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SESSION_METHODS = ("gate_blockers", "arm", "call", "call_with_gates", "dispatch_move", "standby_stop")


# --------------------------------------------------------------------------
# The sequence
# --------------------------------------------------------------------------


class _Rehearsal:
    def __init__(self, snapshot: Any, session: Any, timeouts: Timeouts) -> None:
        self.snapshot = snapshot
        self.session = session
        self.timeouts = timeouts
        started = time.monotonic()
        self.deadline = started + timeouts.total_s  # D_total
        # Movement phases stop here, so recovery always fits before the deadline.
        self.work_deadline = self.deadline - timeouts.recovery_reserve_s  # D_work
        self.recovery_start: float | None = None  # R0
        self.target_dispatched = False
        self.return_dispatched = False
        self.start: JointSnapshot | None = None
        self.connect_attempted = False
        self.stop_attempted = False  # the stop method is called at most once

    # -- deadlines -----------------------------------------------------------

    def _work_budget(self, limit_s: float) -> tuple[float, bool]:
        """(absolute deadline of the call, whether ``D_work`` capped it)."""
        now = time.monotonic()
        remaining = self.work_deadline - now
        if remaining <= 0:
            raise _Abort("overall_timeout", "the total execution deadline passed")
        return now + min(limit_s, remaining), remaining <= limit_s

    def _stop_deadline(self) -> float:
        r0 = self.recovery_start if self.recovery_start is not None else time.monotonic()
        hard = (
            self.deadline + TERMINATE_LIMIT_S
            - self.timeouts.telemetry_read_s - self.timeouts.disconnect_s
        )
        return min(r0 + self.timeouts.stop_s, hard)

    # -- time-limited calls --------------------------------------------------

    def _abort_for(self, exc: Exception, what: str, kind: str, capped: bool) -> _Abort:
        if isinstance(exc, SessionTimeout):
            if capped or time.monotonic() >= self.work_deadline:
                return _Abort("overall_timeout", f"the total execution deadline passed during {what}")
            return _Abort(kind, f"{what} did not return within its time limit")
        if isinstance(exc, TwinCallFailed):
            return _Abort(kind, f"{what} failed ({exc.class_name})")
        if isinstance(exc, SessionCrashed):
            return _Abort(kind, f"{what} failed (the twin process exited unexpectedly)")
        if isinstance(exc, (SessionRefused, SessionUnusable)):
            return _Abort(kind, f"{what} could not be made (the twin session refused it)")
        return _Abort(kind, f"{what} failed ({type(exc).__name__})")

    def _call(self, what: str, method: str, args: Callable[[float], tuple[Any, ...]], limit_s: float, kind: str) -> Any:
        deadline, capped = self._work_budget(limit_s)
        try:
            return self.session.call(method, args(max(0.0, deadline - time.monotonic())), deadline)
        except Exception as exc:
            raise self._abort_for(exc, what, kind, capped) from None

    def _read(self, label: str, limit_s: float, not_before: datetime | None) -> JointSnapshot:
        raw = self._call(
            f"the {label} telemetry read",
            "read_telemetry",
            lambda budget: (budget,),
            limit_s,
            "telemetry_unavailable",
        )
        return validate_telemetry(raw, now=_utc_now(), not_before=not_before)

    def _read_or_abort(self, label: str, limit_s: float, not_before: datetime | None) -> JointSnapshot:
        try:
            return self._read(label, limit_s, not_before)
        except TelemetryRejected as exc:
            raise _Abort("telemetry_invalid", f"the {label} telemetry was rejected: {exc.reason}") from None

    def _wait_until(
        self,
        label: str,
        limit_s: float,
        not_before: datetime,
        reached: Callable[[JointSnapshot], bool],
        timeout_kind: str,
    ) -> None:
        """Poll telemetry until ``reached`` or the phase time limit. Bounded."""
        phase_deadline = min(time.monotonic() + limit_s, self.work_deadline)
        while True:
            remaining = phase_deadline - time.monotonic()
            if remaining <= 0:
                if time.monotonic() >= self.work_deadline:
                    raise _Abort("overall_timeout", f"the total execution deadline passed while waiting for the {label}")
                raise _Abort(timeout_kind, f"the {label} was not reached within its time limit")
            try:
                snapshot = self._read(label, min(self.timeouts.telemetry_read_s, remaining), not_before)
            except TelemetryRejected as exc:
                # A reading from before the movement is not evidence yet: keep
                # waiting. Any other refusal means the telemetry cannot be trusted.
                if exc.reason != "stale_before_dispatch":
                    raise _Abort(
                        "telemetry_invalid", f"the {label} telemetry was rejected: {exc.reason}"
                    ) from None
            else:
                if reached(snapshot):
                    return
            time.sleep(max(0.0, min(self.timeouts.poll_interval_s, phase_deadline - time.monotonic())))

    # -- steps ---------------------------------------------------------------

    def _gate_blockers(self) -> tuple[str, ...]:
        try:
            own = tuple(self.session.gate_blockers())
        except Exception:
            own = ("the twin session gate check failed",)
        return merge_blockers(own, canonical_gate_blockers())

    def _require_gates(self) -> None:
        blockers = self._gate_blockers()
        if blockers:
            raise _Abort("gate_unconfirmed", f"movement is blocked: {blockers[0]}")

    def _require_simulation(self) -> None:
        for what, method in (
            ("the SDK runtime target", "runtime_target"),
            ("the twin target", "twin_target"),
        ):
            reported = self._call(f"reading {what}", method, lambda _budget: (), self.timeouts.mode_check_s, "not_simulation")
            if type(reported) is not str or reported != EXECUTION_TARGET:
                raise _Abort("not_simulation", f"{what} is not verified as simulation; nothing was sent")

    def _move(self, what: str, joint: str, value_rad: float, *, returning: bool) -> None:
        """One movement through the dispatch handshake.

        The "may have been sent" flag is set by the session's acknowledgement,
        BEFORE the child is allowed to call ``move_joint``. A refusal or a crash
        before that acknowledgement means nothing was sent.
        """
        deadline, capped = self._work_budget(self.timeouts.dispatch_s)

        acknowledged_once = False

        def acknowledged() -> None:
            nonlocal acknowledged_once
            acknowledged_once = True
            if returning:
                self.return_dispatched = True
            else:
                self.target_dispatched = True

        try:
            self.session.dispatch_move(joint, value_rad, deadline, acknowledged)
        except SessionRefused:
            if acknowledged_once:
                # GO was refused AFTER the acknowledgement. The movement stays
                # recorded as possibly sent: this code cannot prove it was not.
                raise _Abort(
                    "dispatch_failed",
                    f"{what} was refused by the twin process after it was recorded as possibly "
                    "sent; it is treated as sent",
                ) from None
            raise _Abort(
                "dispatch_failed",
                f"{what} was refused by the twin process before any movement was sent",
            ) from None
        except Exception as exc:
            raise self._abort_for(exc, what, "dispatch_failed", capped) from None

    def _sequence(self) -> RehearsalSuccess:
        try:
            require_first_rehearsal_plan(self.snapshot)
        except GateError as exc:
            raise _Abort(exc.kind, exc.detail) from None
        self._require_gates()

        self.connect_attempted = True
        self._call(
            "the simulation connection",
            "connect",
            lambda budget: (budget,),
            self.timeouts.connect_s,
            "connection_failed",
        )
        self._require_simulation()

        start = self._read_or_abort("start", self.timeouts.telemetry_read_s, None)
        self.start = start

        # Immediately before movement: the plan again, from the same snapshot,
        # against the shared limits; and the return target too.
        try:
            action = require_first_rehearsal_plan(self.snapshot)
            limits = load_shared_limits()
            revalidate_against_shared_limits(self.snapshot.plan_id, [action], limits)
            require_within_shared_limits(
                FIRST_REHEARSAL_JOINT, start.joints[FIRST_REHEARSAL_JOINT], limits
            )
        except GateError as exc:
            raise _Abort(exc.kind, exc.detail) from None
        joint, target = action["target"], float(action["value"])
        if joint != FIRST_REHEARSAL_JOINT or target != FIRST_REHEARSAL_TARGET_RAD:
            raise _Abort("rehearsal_restriction", "the action is not the permitted first movement")
        others = {name: start.joints[name] for name in JOINT_NAMES if name != joint}

        # The one movement to the target, through the dispatch handshake.
        if self.target_dispatched or self.return_dispatched:
            raise _Abort("internal_error", "a movement was already sent in this run")
        sent_at = _utc_now()
        self._move("the target movement", joint, target, returning=False)
        self._wait_until(
            "target",
            self.timeouts.target_move_s,
            sent_at,
            lambda snapshot: not joints_outside_tolerance(snapshot, {joint: target}),
            "target_timeout",
        )
        reached = self._read_or_abort("target", self.timeouts.target_verify_s, sent_at)
        if joints_outside_tolerance(reached, {joint: target}):
            raise _Abort("target_not_verified", f"joint {joint} is not within tolerance of the target")
        moved = joints_outside_tolerance(reached, others)
        if moved:
            raise _Abort(
                "unexpected_joint_motion", f"joint(s) {', '.join(moved)} moved beyond the tolerance"
            )

        # The one return movement, only after the target was verified.
        if self.return_dispatched:
            raise _Abort("internal_error", "a return movement was already sent in this run")
        returned_at = _utc_now()
        self._move("the return movement", joint, start.joints[joint], returning=True)
        self._wait_until(
            "restored position",
            self.timeouts.return_move_s,
            returned_at,
            lambda snapshot: not joints_outside_tolerance(snapshot, start.joints),
            "return_timeout",
        )
        restored = self._read_or_abort("restored", self.timeouts.restore_verify_s, returned_at)
        not_restored = joints_outside_tolerance(restored, start.joints)
        if not_restored:
            raise _Abort(
                "restore_not_verified",
                f"joint(s) {', '.join(not_restored)} are not within tolerance of the start",
            )
        return RehearsalSuccess(start=start, target=reached, restored=restored)

    # -- failure recovery ----------------------------------------------------

    def _attempt_stop(self) -> str:
        """Best-effort stop after a possible dispatch: at most ONE call, never retried.

        Only reached once a movement may have been sent. Through the live
        session the call is made even when the gates no longer pass (a movement
        may be under way; trying to stop it costs nothing); the gates only
        decide whether the answer may be believed. ``confirmed`` requires ALL
        of: the parent's and the child's gate checks valid both before and after
        the call, and a result of exactly type ``StopResult`` with ``confirmed
        is True``. Returning without raising is never enough.

        If the session's child was terminated, the only stop left is one from a
        FRESH session (``standby_stop``, if the adapter declares it). No
        documented guarantee says that such a stop cancels a movement started by
        the terminated session, so its result is ALWAYS ``unconfirmed``.

        An expired stop deadline means no call. Anything else is ``unconfirmed``.
        """
        if self.stop_attempted:
            return STOP_UNCONFIRMED  # never a second call
        stop_deadline = self._stop_deadline()
        budget = stop_deadline - time.monotonic()
        if budget <= 0:
            return STOP_UNCONFIRMED
        self.stop_attempted = True
        if getattr(self.session, "healthy", False):
            parent_before = not self._gate_blockers()
            try:
                result, child_before, child_after = self.session.call_with_gates(
                    "stop", (budget,), stop_deadline
                )
            except Exception:
                return STOP_UNCONFIRMED
            if (
                parent_before
                and child_before
                and child_after
                and not self._gate_blockers()
                and type(result) is StopResult
                and result.confirmed is True
            ):
                return STOP_CONFIRMED
            return STOP_UNCONFIRMED
        try:
            self.session.standby_stop(stop_deadline)
        except Exception:
            pass
        return STOP_UNCONFIRMED  # a fresh session's stop is never a confirmation

    def _last_telemetry(self) -> JointSnapshot | None:
        """One fresh, independent read through the live session. None unless it validates."""
        if not getattr(self.session, "healthy", False):
            return None
        now = time.monotonic()
        deadline = min(now + self.timeouts.telemetry_read_s,
                       self.deadline + TERMINATE_LIMIT_S - self.timeouts.disconnect_s)
        if deadline - now <= 0:
            return None
        try:
            raw = self.session.call("read_telemetry", (deadline - now,), deadline)
            return validate_telemetry(raw, now=_utc_now())
        except (TelemetryRejected, Exception):
            return None

    def _disconnect(self) -> None:
        """One bounded disconnect through a live session. Never started after its deadline.

        Its absolute deadline is ``D_total + TERMINATE_LIMIT_S`` (the end of the
        recovery window), and at most ``disconnect_s`` from now.
        """
        if not self.connect_attempted or not getattr(self.session, "healthy", False):
            return
        now = time.monotonic()
        deadline = min(now + self.timeouts.disconnect_s, self.deadline + TERMINATE_LIMIT_S)
        if deadline <= now:
            return
        try:
            self.session.call("disconnect", (), deadline)
        except Exception:
            pass  # cleanup only; the outcome is already decided

    def _disconnect_quietly(self) -> BaseException | None:
        """The disconnect; a Ctrl-C during it is returned, not raised."""
        try:
            self._disconnect()
        except BaseException as exc:
            return exc
        return None

    def _failure(self, kind: str, detail: str) -> RehearsalFailure:
        if self.recovery_start is None:
            self.recovery_start = time.monotonic()
        if self.target_dispatched or self.return_dispatched:
            stop = self._attempt_stop()
            last = self._last_telemetry()
        else:
            stop, last = STOP_NOT_NEEDED, self.start
        return RehearsalFailure(
            kind=kind,
            detail=detail[:MAX_TEXT_LENGTH],
            stop=stop,
            last_telemetry=last,
            target_dispatched=self.target_dispatched,
            return_dispatched=self.return_dispatched,
        )

    def _interrupted(self, exc: BaseException) -> RehearsalFailure:
        """The failure after an interruption. Never raises."""
        if self.recovery_start is None:
            self.recovery_start = time.monotonic()
        dispatched = self.target_dispatched or self.return_dispatched
        if dispatched:
            try:
                stop = self._attempt_stop()
            except BaseException:  # a second interruption abandons the stop
                stop = STOP_UNCONFIRMED
        else:
            stop = STOP_NOT_NEEDED
        return RehearsalFailure(
            kind=interruption_kind(exc),
            detail=_interruption_detail(exc, dispatched)[:MAX_TEXT_LENGTH],
            stop=stop,
            last_telemetry=None if dispatched else self.start,
            target_dispatched=self.target_dispatched,
            return_dispatched=self.return_dispatched,
        )

    def _outcome(self) -> RehearsalSuccess | RehearsalFailure:
        try:
            return self._sequence()
        except _Abort as abort:
            return self._failure(abort.kind, abort.detail)
        except Exception as exc:  # fail closed: never a success
            return self._failure("internal_error", f"unexpected error ({type(exc).__name__})")

    def run(self) -> RehearsalSuccess | RehearsalFailure:
        try:
            self.session.arm(self.deadline + TERMINATE_LIMIT_S + CLEANUP_LIMIT_S)
        except Exception:
            pass  # a dead child shows up on the first call
        try:
            outcome = self._outcome()
        except BaseException as exc:
            # Interrupted (Ctrl-C, SystemExit, anything else). If a movement may
            # be under way, the stop is tried once; then the ORIGINAL exception
            # continues, carrying the outcome for the audit lifecycle.
            outcome = self._interrupted(exc)
            self._disconnect_quietly()
            _attach_outcome(exc, outcome)
            raise
        late = self._disconnect_quietly()
        if late is not None:
            _attach_outcome(late, outcome)
            raise late
        return outcome


def run_rehearsal(
    snapshot: Any, session: Any, timeouts: Timeouts = DEFAULT_TIMEOUTS
) -> RehearsalSuccess | RehearsalFailure:
    """Run the first-rehearsal sequence once. Never retries, never raises ``Exception``.

    ``snapshot`` is the immutable ``PlanSnapshot`` that was validated and
    approved; ``session`` is a STARTED twin session (in production a
    ``simulation_supervisor.ProcessTwinSession``); its owner closes it. The
    caller must already have written ``execution_started`` and moved the command
    to ``executing``. A ``BaseException`` (for example Ctrl-C) propagates, with
    the outcome attached as ``rehearsal_outcome``.
    """
    if not isinstance(timeouts, Timeouts):
        raise TypeError("timeouts must be a Timeouts instance.")
    if not all(callable(getattr(session, name, None)) for name in _SESSION_METHODS):
        raise TypeError("session must be a started twin session.")
    return _Rehearsal(snapshot, session, timeouts).run()
