"""Gates for the simulation-only SO-101 rehearsal. Decisions only, no movement.

No Cyberwave, no network, no database. The only I/O is reading
``shared/so101-limits.json``. Nothing in this module can move anything.

Three things live here, all hard-coded on purpose:

1. **The live-simulation gates.** ``CYBERWAVE_GATES`` records what the Cyberwave
   engineers and Abhijit have confirmed. EVERY entry is ``False`` today, so the
   live path is blocked. They are literals in source: there is no environment
   variable, configuration setting or command-line flag that changes them, so
   unblocking is a reviewed code change, never a runtime switch.
2. **The execution target.** ``EXECUTION_TARGET`` is the string
   ``"simulation"``. There is no selector and no live-hardware value.
3. **The first-rehearsal restriction.** Exactly one action: ``set_joint``,
   joint ``_1``, 0.25 rad. Not derived from the shared limits, which allow much
   more: a wider plan is refused even though the validator and critic pass it.

``GateError`` carries a fixed error kind (``simulation_payloads.ERROR_KINDS``)
and a short detail that is safe to store in an audit payload.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from plan_hasher import hash_actions
from plan_snapshot import PlanSnapshot
from safety_validator import ACTION_FIELDS, validate_plan
from simulation_payloads import JOINT_NAMES

__all__ = [
    "CYBERWAVE_GATES",
    "EXECUTION_TARGET",
    "FIRST_REHEARSAL_JOINT",
    "FIRST_REHEARSAL_TARGET_RAD",
    "SHARED_LIMITS_FILE",
    "GateError",
    "LiveSimulationGates",
    "live_simulation_blockers",
    "load_shared_limits",
    "require_first_rehearsal_plan",
    "require_within_shared_limits",
    "revalidate_against_shared_limits",
]

# The one and only execution target. Not configurable.
EXECUTION_TARGET = "simulation"

# The first rehearsal: set joint _1 to 0.25 rad, nothing else.
FIRST_REHEARSAL_JOINT = "_1"
FIRST_REHEARSAL_TARGET_RAD = 0.25
_REHEARSAL_ACTION_TYPE = "set_joint"
_REHEARSAL_UNIT = "rad"

SHARED_LIMITS_FILE = Path(__file__).resolve().parent.parent / "shared" / "so101-limits.json"


class GateError(Exception):
    """A gate refused. ``kind`` and ``detail`` are safe for an audit payload."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# --------------------------------------------------------------------------
# 1. Live-simulation gates
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveSimulationGates:
    """What must be confirmed before a simulated movement may be sent.

    Each field is ``True`` only once the named fact is confirmed in writing by
    the Cyberwave engineers (the first five) or approved by Abhijit (the last).
    """

    # 1. The supported method to stop or cancel a joint movement on an arm twin,
    #    with its arguments and its behaviour when the connection is lost.
    stop_method_confirmed: bool = False
    # 2. What that method returns when the arm has actually stopped.
    stop_success_contract_confirmed: bool = False
    # 3. That get_joints (or the documented alternative) returns actual
    #    simulation state.
    telemetry_is_actual_state_confirmed: bool = False
    # 4. How a reading identifies the simulation as its source.
    telemetry_source_confirmed: bool = False
    # 5. How to tell measured positions from placeholders and commanded targets.
    measured_vs_placeholder_confirmed: bool = False
    # 6. Abhijit's explicit approval of the first simulated movement.
    first_movement_approved: bool = False

    def unconfirmed(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self) if getattr(self, f.name) is not True)

    def all_confirmed(self) -> bool:
        return not self.unconfirmed()


# The real state of the confirmations for Cyberwave. All False: BLOCKED.
CYBERWAVE_GATES = LiveSimulationGates()

_GATE_TEXT = {
    "stop_method_confirmed": "the supported stop/cancel method for an arm twin is not confirmed",
    "stop_success_contract_confirmed": "what proves the arm has stopped is not confirmed",
    "telemetry_is_actual_state_confirmed": (
        "it is not confirmed that joint telemetry is actual simulation state"
    ),
    "telemetry_source_confirmed": "how telemetry identifies its simulation source is not confirmed",
    "measured_vs_placeholder_confirmed": (
        "how to tell measured positions from placeholders or commanded targets is not confirmed"
    ),
    "first_movement_approved": "Abhijit has not approved the first simulated movement",
}


def live_simulation_blockers(gates: Any = CYBERWAVE_GATES) -> tuple[str, ...]:
    """Why live simulated movement is blocked. Empty only when nothing blocks it."""
    if not isinstance(gates, LiveSimulationGates):
        return ("the live-simulation gates are missing or not recognised",)
    return tuple(_GATE_TEXT[name] for name in gates.unconfirmed())


# --------------------------------------------------------------------------
# 2. First-rehearsal restriction
# --------------------------------------------------------------------------


def require_first_rehearsal_plan(snapshot: Any) -> dict[str, Any]:
    """Re-hash the approved snapshot and require exactly the one permitted action.

    Works on the immutable ``PlanSnapshot`` only. The command text is never
    read here. Returns a fresh copy of the single action.
    """
    if not isinstance(snapshot, PlanSnapshot):
        raise GateError("approval_invalid", "no approved plan snapshot was given")
    try:
        actions = snapshot.actions()
        current_hash = hash_actions(actions)
    except Exception as exc:
        raise GateError(
            "approval_invalid", f"the approved plan could not be re-hashed ({type(exc).__name__})"
        ) from None
    if current_hash != snapshot.plan_hash:
        raise GateError("approval_invalid", "the plan no longer matches its approved hash")
    if len(actions) != snapshot.action_count:
        raise GateError("approval_invalid", "the plan's action count does not match the approval")

    restriction = (
        f"the first rehearsal allows only: set joint {FIRST_REHEARSAL_JOINT} "
        f"to {FIRST_REHEARSAL_TARGET_RAD} rad"
    )
    if len(actions) != 1:
        raise GateError(
            "rehearsal_restriction", f"{restriction}; the plan has {len(actions)} actions"
        )
    action = actions[0]
    if type(action) is not dict or set(action) != set(ACTION_FIELDS):
        raise GateError("rehearsal_restriction", f"{restriction}; the action shape is not allowed")
    if action["type"] != _REHEARSAL_ACTION_TYPE:
        raise GateError("rehearsal_restriction", f"{restriction}; the action type is not allowed")
    if action["target"] != FIRST_REHEARSAL_JOINT:
        raise GateError("rehearsal_restriction", f"{restriction}; another joint is not allowed")
    if action["unit"] != _REHEARSAL_UNIT:
        raise GateError("rehearsal_restriction", f"{restriction}; another unit is not allowed")
    value = action["value"]
    if type(value) not in (int, float) or value != FIRST_REHEARSAL_TARGET_RAD:
        raise GateError("rehearsal_restriction", f"{restriction}; another target is not allowed")
    return action


# --------------------------------------------------------------------------
# 3. shared/so101-limits.json
# --------------------------------------------------------------------------


def load_shared_limits(path: Path = SHARED_LIMITS_FILE) -> dict[str, tuple[float, float]]:
    """``{joint: (min, max)}`` in radians from the shared limits file, strictly."""
    failed = GateError("limits_revalidation_failed", "the shared joint limits could not be read")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        raise failed from None
    if type(data) is not dict or data.get("robot") != "so101" or data.get("unit") != "rad":
        raise failed
    joints = data.get("joints")
    if type(joints) is not dict or set(joints) != set(JOINT_NAMES):
        raise failed
    limits: dict[str, tuple[float, float]] = {}
    for name in JOINT_NAMES:
        entry = joints[name]
        if type(entry) is not dict:
            raise failed
        low, high = entry.get("min"), entry.get("max")
        for bound in (low, high):
            if type(bound) not in (int, float) or not math.isfinite(bound):
                raise failed
        if not low < high:
            raise failed
        limits[name] = (float(low), float(high))
    return limits


def require_within_shared_limits(
    joint: Any, value: Any, limits: Mapping[str, tuple[float, float]]
) -> None:
    """Refuse unless ``value`` is a finite number inside ``joint``'s inclusive limits."""
    if type(joint) is not str or joint not in limits:
        raise GateError("limits_revalidation_failed", "a joint is not in the shared joint limits")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise GateError("limits_revalidation_failed", f"the value for {joint} is not a finite number")
    low, high = limits[joint]
    if not low <= value <= high:
        raise GateError(
            "limits_revalidation_failed", f"the value for {joint} is outside the shared joint limits"
        )


def revalidate_against_shared_limits(
    plan_id: Any, actions: Any, limits: Mapping[str, tuple[float, float]] | None = None
) -> None:
    """Re-check every action immediately before movement.

    Two independent checks, both must pass: the deterministic safety validator
    (``robot_profile.SO101_PROFILE``), and each value against the limits freshly
    read from ``shared/so101-limits.json``.
    """
    if limits is None:
        limits = load_shared_limits()
    validation = validate_plan(plan_id, actions)
    if not validation.passed:
        rule_ids = ", ".join(sorted({issue.rule_id for issue in validation.errors}))
        raise GateError(
            "limits_revalidation_failed", f"the safety validator no longer passes ({rule_ids})"[:200]
        )
    for action in actions:
        if action.get("unit") != _REHEARSAL_UNIT:
            raise GateError("limits_revalidation_failed", "an action is not in radians")
        require_within_shared_limits(action.get("target"), action.get("value"), limits)
