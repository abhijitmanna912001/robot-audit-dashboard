"""Deterministic safety validator for a plan's ``actions`` array.

Pure Python: no network, no database, no robot, no randomness, no clock. The
same input always gives the same result. It never raises on bad input: every
problem, including an internal error, becomes a ``fail`` result (fail closed).

Result shape and rule ids follow docs/schema-draft.md, "Validator result".

WHAT THIS VALIDATOR IS NOT. Passing it does not make a plan safe to execute.
It checks structure, vocabulary, units and schema position limits. It does
not check, because nothing confirmed exists to check against:

* velocity, acceleration or effort
* collisions, with the arm itself or with anything around it
* the size of a move relative to the current position (no joint position has
  ever been measured)
* the order or combination of actions
* whether the schema limits are physically safe (they are unverified)

It is one of two required gates. The other is the critic. Execution must stay
in dry-run mode until both are integrated (docs/schema-draft.md section 6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from robot_profile import SO101_PROFILE, RobotProfile

# DECIDED (docs/schema-draft.md, "Validator result"): the plain string "1.0",
# bumped by hand when the rules change. Not semver, a git sha or a date.
VALIDATOR_VERSION = "1.0"

# Rule ids agreed in docs/schema-draft.md.
UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
UNKNOWN_JOINT = "UNKNOWN_JOINT"
JOINT_LIMIT_EXCEEDED = "JOINT_LIMIT_EXCEEDED"
NON_FINITE_VALUE = "NON_FINITE_VALUE"
UNIT_MISMATCH = "UNIT_MISMATCH"
MAX_ACTIONS_EXCEEDED = "MAX_ACTIONS_EXCEEDED"
# A joint may be targeted by only one action per plan. Matches the critic's
# duplicate_joint_target rule.
DUPLICATE_JOINT_TARGET = "DUPLICATE_JOINT_TARGET"
AGREED_RULE_IDS = frozenset(
    {
        UNSUPPORTED_ACTION,
        MISSING_REQUIRED_FIELD,
        UNKNOWN_JOINT,
        JOINT_LIMIT_EXCEEDED,
        NON_FINITE_VALUE,
        UNIT_MISMATCH,
        MAX_ACTIONS_EXCEEDED,
        DUPLICATE_JOINT_TARGET,
    }
)

# Rule ids the worker added. docs/schema-draft.md ("Validator result") now
# lists them as agreed; the separate set is kept so existing imports still work.
INVALID_PLAN = "INVALID_PLAN"  # not an array, empty, or an action is not an object
INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"  # a field has the wrong JSON type
DUPLICATE_ACTION_ID = "DUPLICATE_ACTION_ID"
UNEXPECTED_FIELD = "UNEXPECTED_FIELD"  # a field outside the agreed action shape
PROPOSED_RULE_IDS = frozenset(
    {INVALID_PLAN, INVALID_FIELD_TYPE, DUPLICATE_ACTION_ID, UNEXPECTED_FIELD}
)

# The action shape in docs/schema-draft.md, "Planner output".
ACTION_FIELDS = frozenset({"actionId", "type", "target", "value", "unit", "reason"})


@dataclass(frozen=True)
class ValidationIssue:
    rule_id: str
    action_id: str | None
    field: str | None
    message: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "actionId": self.action_id,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationResult:
    outcome: str  # "pass" or "fail"
    plan_id: str
    validator_version: str
    errors: tuple[ValidationIssue, ...]

    @property
    def passed(self) -> bool:
        return self.outcome == "pass" and not self.errors

    def to_payload(self) -> dict[str, Any]:
        """The ``validator_result`` audit event payload."""
        return {
            "outcome": self.outcome,
            "planId": self.plan_id,
            "validatorVersion": self.validator_version,
            "errors": [issue.to_payload() for issue in self.errors],
        }


def validate_plan(
    plan_id: Any, actions: Any, profile: RobotProfile = SO101_PROFILE
) -> ValidationResult:
    """Validate ``actions`` against ``profile``. Never raises."""
    try:
        errors = _collect_errors(plan_id, actions, profile)
    except Exception as exc:  # fail closed: an internal error is a failed plan
        errors = [
            ValidationIssue(
                INVALID_PLAN,
                None,
                None,
                f"Validator failed unexpectedly ({type(exc).__name__}); failing closed.",
            )
        ]
    return ValidationResult(
        outcome="fail" if errors else "pass",
        plan_id=plan_id if isinstance(plan_id, str) else "",
        validator_version=VALIDATOR_VERSION,
        errors=tuple(errors),
    )


def _collect_errors(plan_id: Any, actions: Any, profile: RobotProfile) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []

    if not isinstance(plan_id, str) or not plan_id.strip():
        errors.append(
            ValidationIssue(INVALID_PLAN, None, "planId", "planId must be a non-empty string.")
        )

    if not isinstance(actions, list):
        errors.append(
            ValidationIssue(INVALID_PLAN, None, "actions", "actions must be an array.")
        )
        return errors
    if not actions:
        errors.append(ValidationIssue(INVALID_PLAN, None, "actions", "The plan has no actions."))
        return errors
    if profile.max_actions is not None and len(actions) > profile.max_actions:
        errors.append(
            ValidationIssue(
                MAX_ACTIONS_EXCEEDED,
                None,
                "actions",
                f"The plan has {len(actions)} actions; the maximum is {profile.max_actions}.",
            )
        )

    seen_ids: set[str] = set()
    seen_targets: set[str] = set()
    for index, action in enumerate(actions):
        errors.extend(_validate_action(index, action, profile, seen_ids, seen_targets))
    return errors


def _validate_action(
    index: int,
    action: Any,
    profile: RobotProfile,
    seen_ids: set[str],
    seen_targets: set[str],
) -> list[ValidationIssue]:
    if not isinstance(action, dict):
        return [
            ValidationIssue(INVALID_PLAN, None, None, f"Action #{index} is not an object.")
        ]

    raw_id = action.get("actionId")
    action_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else None
    label = f"Action {action_id}" if action_id else f"Action #{index}"
    issues: list[ValidationIssue] = []

    def add(rule_id: str, field: str | None, message: str) -> None:
        issues.append(ValidationIssue(rule_id, action_id, field, f"{label}: {message}"))

    for key in action:
        if key not in ACTION_FIELDS:
            add(UNEXPECTED_FIELD, str(key), f"unexpected field {str(key)!r}.")

    if _required_string(action, "actionId", add) is not None:
        assert action_id is not None
        if action_id in seen_ids:
            add(DUPLICATE_ACTION_ID, "actionId", "actionId is used by an earlier action.")
        seen_ids.add(action_id)

    _required_string(action, "reason", add)

    action_type = _required_string(action, "type", add)
    if action_type is None:
        return issues
    handler = _ACTION_HANDLERS.get(action_type)
    if action_type not in profile.supported_action_types or handler is None:
        add(UNSUPPORTED_ACTION, "type", f"action type {action_type!r} is not supported.")
        return issues

    handler(action, profile, add)

    # One action per joint. Like the critic, only a known joint counts (an
    # unknown one already fails with UNKNOWN_JOINT), and the later action is
    # the one flagged, whatever else is wrong with it.
    target = action.get("target")
    if isinstance(target, str) and target in profile.joint_position_limits:
        if target in seen_targets:
            add(
                DUPLICATE_JOINT_TARGET,
                "target",
                f"joint {target!r} is targeted by an earlier action.",
            )
        seen_targets.add(target)
    return issues


_AddIssue = Callable[[str, "str | None", str], None]


def _required_string(action: dict[str, Any], name: str, add: _AddIssue) -> str | None:
    """Return the field if it is a non-empty string, else record why not."""
    if name not in action:
        add(MISSING_REQUIRED_FIELD, name, f"{name} is required.")
        return None
    value = action[name]
    if value is None:
        add(MISSING_REQUIRED_FIELD, name, f"{name} is null. Unset fields must be omitted.")
        return None
    if not isinstance(value, str):
        add(INVALID_FIELD_TYPE, name, f"{name} must be a string.")
        return None
    if not value.strip():
        add(MISSING_REQUIRED_FIELD, name, f"{name} must not be empty.")
        return None
    return value


def _required_finite_number(action: dict[str, Any], name: str, add: _AddIssue) -> float | int | None:
    if name not in action:
        add(MISSING_REQUIRED_FIELD, name, f"{name} is required.")
        return None
    value = action[name]
    if value is None:
        add(MISSING_REQUIRED_FIELD, name, f"{name} is null. Unset fields must be omitted.")
        return None
    # bool is a subclass of int in Python, but true/false is not a number here.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        add(INVALID_FIELD_TYPE, name, f"{name} must be a number.")
        return None
    if isinstance(value, float) and not math.isfinite(value):
        add(NON_FINITE_VALUE, name, f"{name} must be a finite number.")
        return None
    return value


def _validate_set_joint(action: dict[str, Any], profile: RobotProfile, add: _AddIssue) -> None:
    target = _required_string(action, "target", add)
    limit = None
    if target is not None:
        limit = profile.joint_position_limits.get(target)
        if limit is None:
            add(UNKNOWN_JOINT, "target", f"unknown joint {target!r}.")

    value = _required_finite_number(action, "value", add)

    unit = _required_string(action, "unit", add)
    unit_ok = unit == profile.joint_position_unit
    if unit is not None and not unit_ok:
        add(
            UNIT_MISMATCH,
            "unit",
            f"unit must be {profile.joint_position_unit!r}, got {unit!r}. No conversion is done.",
        )

    # The limit is only meaningful for a known joint, a real number and the
    # expected unit. Otherwise the errors above already fail the action.
    if limit is not None and value is not None and unit_ok:
        if not (limit.lower <= value <= limit.upper):
            add(
                JOINT_LIMIT_EXCEEDED,
                "value",
                f"value {value!r} is outside the limits for {target!r}: "
                f"{limit.lower} to {limit.upper} {profile.joint_position_unit}.",
            )


# Action types this validator knows how to check. A type must be both here and
# in the profile's supported_action_types to be accepted.
_ACTION_HANDLERS: dict[str, Callable[[dict[str, Any], RobotProfile, _AddIssue], None]] = {
    "set_joint": _validate_set_joint,
}
