"""The bounded movement sequence of the first SO-101 SIMULATION rehearsal.

Separate from the dry run (``dry_run.py``), which is unchanged and sends
nothing. This module writes no audit event and touches no database: it only
drives a twin through ``SimulationTwin`` and reports what happened. The audit
lifecycle around it is ``simulation_processor.py``.

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

The sequence (``run_rehearsal``), for the one permitted plan (joint ``_1`` to
0.25 rad):

1. re-hash the approved ``PlanSnapshot`` and re-check the first-rehearsal
   restriction (the command text is never read);
2. require every live-simulation gate of the twin to be confirmed;
3. connect, then verify the SDK runtime target and the twin target;
4. read and validate the START positions of all six joints;
5. re-check the plan and the return target against ``shared/so101-limits.json``;
6. send ONE movement: ``_1`` to 0.25 rad;
7. wait, bounded, until ``_1`` is within 0.01 rad of 0.25, then take a fresh
   TARGET reading and verify ``_1`` and that the other five joints did not move;
8. send ONE return movement: ``_1`` to its captured start position;
9. wait, bounded, until all six joints are back, then take a fresh RESTORED
   reading and verify all six within 0.01 rad of the start.

At most two movement calls are ever made, never a third, and nothing is
retried. Every adapter call runs with a time limit; the whole sequence,
including failure recovery, has one total deadline (``Timeouts``).

On a failure:

* before any movement was sent: no stop is attempted, ``stop = "not_needed"``;
* after a movement call was started (even if that call raised or hung, since it
  may still have been sent): the twin's stop method is called ONCE, and the
  result is ``"confirmed"`` only if it returned ``StopResult(confirmed=True)``;
  an exception, a timeout or anything else is ``"unconfirmed"``. No further
  movement is sent: not a retry, and not a return to the start;
* ``last_telemetry`` is set only from a reading that validated: one fresh read
  after a movement failure, or the start reading when nothing had moved.

Error kinds are the fixed set in ``simulation_payloads.ERROR_KINDS``; details
are fixed text plus, at most, an exception class name. No SDK error text,
credential, header or URL is ever put in a detail.
"""

from __future__ import annotations

import math
import threading
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
    live_simulation_blockers,
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

__all__ = [
    "DEFAULT_TIMEOUTS",
    "RehearsalFailure",
    "RehearsalSuccess",
    "SimulationTwin",
    "StopResult",
    "Timeouts",
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
    honour its time limit; the sequence also enforces it from outside.
    """

    # The confirmations this implementation relies on. A Cyberwave adapter must
    # use ``simulation_gates.CYBERWAVE_GATES`` itself, never a copy.
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
    target_dispatched: bool  # a movement call to the target was started
    return_dispatched: bool  # a return movement call was started


class _Abort(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail[:MAX_TEXT_LENGTH]


class _CallTimeout(Exception):
    """An adapter call did not return within its time limit."""


def _bounded(call: Callable[[], Any], timeout_s: float) -> Any:
    """Run ``call`` on a daemon thread and wait at most ``timeout_s``.

    A call that does not return in time is abandoned (the thread cannot be
    killed) and ``_CallTimeout`` is raised, so no adapter can block the worker.
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=run, name="simulation-call", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise _CallTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# The sequence
# --------------------------------------------------------------------------


class _Rehearsal:
    def __init__(self, snapshot: Any, twin: Any, timeouts: Timeouts) -> None:
        self.snapshot = snapshot
        self.twin = twin
        self.timeouts = timeouts
        started = time.monotonic()
        self.deadline = started + timeouts.total_s
        # Movement phases stop here, so recovery always fits before the deadline.
        self.work_deadline = self.deadline - timeouts.recovery_reserve_s
        self.target_dispatched = False
        self.return_dispatched = False
        self.start: JointSnapshot | None = None
        self.connect_attempted = False

    # -- time-limited calls --------------------------------------------------

    def _budget(self, limit_s: float) -> float:
        remaining = self.work_deadline - time.monotonic()
        if remaining <= 0:
            raise _Abort("overall_timeout", "the total execution deadline passed")
        return min(limit_s, remaining)

    def _call(self, what: str, call: Callable[[float], Any], limit_s: float, kind: str) -> Any:
        budget = self._budget(limit_s)
        try:
            return _bounded(lambda: call(budget), budget)
        except _CallTimeout:
            if time.monotonic() >= self.work_deadline:
                raise _Abort("overall_timeout", f"the total execution deadline passed during {what}") from None
            raise _Abort(kind, f"{what} did not return within its time limit") from None
        except Exception as exc:
            raise _Abort(kind, f"{what} failed ({type(exc).__name__})") from None

    def _read(self, label: str, limit_s: float, not_before: datetime | None) -> JointSnapshot:
        raw = self._call(
            f"the {label} telemetry read",
            lambda budget: self.twin.read_telemetry(budget),
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

    def _require_gates(self) -> None:
        gates = getattr(self.twin, "confirmations", None)
        blockers = live_simulation_blockers(gates)
        if blockers:
            raise _Abort("gate_unconfirmed", f"movement is blocked: {blockers[0]}")

    def _require_simulation(self) -> None:
        for what, read in (
            ("the SDK runtime target", lambda _budget: self.twin.runtime_target()),
            ("the twin target", lambda _budget: self.twin.twin_target()),
        ):
            reported = self._call(f"reading {what}", read, self.timeouts.mode_check_s, "not_simulation")
            if type(reported) is not str or reported != EXECUTION_TARGET:
                raise _Abort("not_simulation", f"{what} is not verified as simulation; nothing was sent")

    def _move(self, what: str, joint: str, value_rad: float) -> None:
        self._call(
            what,
            lambda _budget: self.twin.move_joint(joint, value_rad),
            self.timeouts.dispatch_s,
            "dispatch_failed",
        )

    def _sequence(self) -> RehearsalSuccess:
        try:
            require_first_rehearsal_plan(self.snapshot)
        except GateError as exc:
            raise _Abort(exc.kind, exc.detail) from None
        self._require_gates()

        self.connect_attempted = True
        self._call(
            "the simulation connection",
            lambda budget: self.twin.connect(budget),
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

        # The one movement to the target. Marked as dispatched BEFORE the call:
        # a call that raises or hangs may still have been sent.
        if self.target_dispatched or self.return_dispatched:
            raise _Abort("internal_error", "a movement was already sent in this run")
        self.target_dispatched = True
        sent_at = _utc_now()
        self._move("the target movement", joint, target)
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
        self.return_dispatched = True
        returned_at = _utc_now()
        self._move("the return movement", joint, start.joints[joint])
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
        """Call the twin's stop ONCE. ``confirmed`` only on the documented success."""
        budget = min(self.timeouts.stop_s, self.deadline - time.monotonic())
        if budget <= 0 or live_simulation_blockers(getattr(self.twin, "confirmations", None)):
            return STOP_UNCONFIRMED
        try:
            result = _bounded(lambda: self.twin.stop(budget), budget)
        except (_CallTimeout, Exception):
            return STOP_UNCONFIRMED
        if isinstance(result, StopResult) and result.confirmed is True:
            return STOP_CONFIRMED
        return STOP_UNCONFIRMED

    def _last_telemetry(self) -> JointSnapshot | None:
        """One fresh, independent read. None unless it validates."""
        budget = min(self.timeouts.telemetry_read_s, self.deadline - time.monotonic())
        if budget <= 0:
            return None
        try:
            raw = _bounded(lambda: self.twin.read_telemetry(budget), budget)
            return validate_telemetry(raw, now=_utc_now())
        except (_CallTimeout, TelemetryRejected, Exception):
            return None

    def _disconnect(self) -> None:
        if not self.connect_attempted:
            return
        budget = max(0.05, min(self.timeouts.disconnect_s, self.deadline - time.monotonic()))
        try:
            _bounded(self.twin.disconnect, budget)
        except (_CallTimeout, Exception):
            pass  # cleanup only; the outcome is already decided

    def _failure(self, kind: str, detail: str) -> RehearsalFailure:
        if self.target_dispatched:
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

    def run(self) -> RehearsalSuccess | RehearsalFailure:
        try:
            try:
                return self._sequence()
            except _Abort as abort:
                return self._failure(abort.kind, abort.detail)
            except Exception as exc:  # fail closed: never a success
                return self._failure("internal_error", f"unexpected error ({type(exc).__name__})")
            except BaseException:
                # Interrupted (for example Ctrl-C). If a movement may be under
                # way, try the stop once before the interruption continues.
                if self.target_dispatched:
                    self._attempt_stop()
                raise
        finally:
            self._disconnect()


def run_rehearsal(
    snapshot: Any, twin: Any, timeouts: Timeouts = DEFAULT_TIMEOUTS
) -> RehearsalSuccess | RehearsalFailure:
    """Run the first-rehearsal sequence once. Never retries, never raises ``Exception``.

    ``snapshot`` is the immutable ``PlanSnapshot`` that was validated and
    approved; ``twin`` implements ``SimulationTwin``. The caller must already
    have written ``execution_started`` and moved the command to ``executing``.
    """
    if not isinstance(timeouts, Timeouts):
        raise TypeError("timeouts must be a Timeouts instance.")
    return _Rehearsal(snapshot, twin, timeouts).run()
