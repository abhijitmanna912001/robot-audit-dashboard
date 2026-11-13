"""The execution gate: both checks must pass, on the same plan.

Pure decision function. It does not execute anything, it imports nothing that
can reach a robot or a network, and no execution code exists yet.
``allowed=True`` means only that the deterministic validator and the critic
both approved exactly these actions for exactly this command and plan. It is
not permission to move a robot: per docs/schema-draft.md section 6, command
execution stays in dry-run mode until both checks are integrated.

The gate does not trust earlier results. It re-runs the validator on the
actions it is given and recomputes their hash, so a plan that was changed
after it was validated or after it was sent to the critic is refused. It also
refuses an approval that was given for a different command or plan, even if
the actions (and so the hash) are identical.

It never raises. A missing or malformed input, or any internal error, is a
refusal (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from critic_gate import CriticDecision, CriticRequest
from plan_hasher import PlanHashError, hash_actions
from robot_profile import SO101_PROFILE, RobotProfile
from safety_validator import ValidationResult, validate_plan


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    reasons: tuple[str, ...]
    # None only when the gate could not get as far as validating.
    validation: ValidationResult | None = None


def decide_execution(
    request: Any, critic: Any, profile: RobotProfile = SO101_PROFILE
) -> ExecutionDecision:
    """Allow only if the validator passes and the critic approved this exact plan."""
    try:
        return _decide(request, critic, profile)
    except Exception as exc:  # fail closed
        return ExecutionDecision(
            allowed=False,
            reasons=(f"execution gate failed unexpectedly ({type(exc).__name__}); refusing",),
        )


def _decide(request: Any, critic: Any, profile: RobotProfile) -> ExecutionDecision:
    if not isinstance(request, CriticRequest):
        return ExecutionDecision(False, ("no valid critic request to check",))

    reasons: list[str] = []

    validation = validate_plan(request.plan_id, request.actions, profile)
    if not validation.passed:
        rule_ids = sorted({issue.rule_id for issue in validation.errors})
        reasons.append("validator failed: " + ", ".join(rule_ids))

    current_hash: str | None
    try:
        current_hash = hash_actions(request.actions)
    except PlanHashError:
        current_hash = None
        reasons.append("the actions cannot be hashed")
    if current_hash is not None and current_hash != request.plan_hash:
        reasons.append("the actions changed after the plan hash was computed")

    if not isinstance(critic, CriticDecision):
        reasons.append("no critic result")
    elif not critic.approved:
        reasons.append(f"critic outcome is {critic.outcome!r} ({critic.category}), not an approval")
    else:
        if critic.command_id != request.command_id:
            reasons.append("the critic approval is for a different command")
        if critic.plan_id != request.plan_id:
            reasons.append("the critic approval is for a different plan")
        if critic.plan_hash is None or critic.plan_hash != current_hash:
            reasons.append("the critic approved a different plan hash")

    return ExecutionDecision(allowed=not reasons, reasons=tuple(reasons), validation=validation)
