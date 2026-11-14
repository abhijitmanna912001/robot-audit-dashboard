"""Deterministic command planner. No language model, no network, no randomness.

It turns a command's text into a list of ``set_joint`` actions, or into a
structured planner error. It only parses. It does NOT decide whether a plan is
safe: joint limits and duplicate joint targets are left to the safety
validator and the critic, which must both run before anything executes.

Accepted grammar (ASCII only; keywords are case-insensitive; ASCII spaces
and tabs around tokens are ignored)::

    set joint _1 to 0.25 rad
    set joint _1 to 0.25 rad; set joint _6 to -0.1 rad

* the whole command must be ASCII. Any other character (Arabic-Indic or
  fullwidth digits, a long s, a non-breaking space, ...) is rejected before
  parsing, so a lookalike can never be read as a digit or a keyword
* one or more clauses separated by ``;``. An empty clause (for example from a
  trailing ``;``) is rejected rather than skipped
* joint: one of the joints in ``robot_profile.SO101_PROFILE`` (``_1`` to ``_6``)
* value: a plain decimal number with an optional sign (``0.25``, ``-1``,
  ``.5``). No exponent, no ``nan`` or ``inf``
* unit: ``rad`` only. No conversion is done
* at most ``SO101_PROFILE.max_actions`` clauses (10, docs/schema-draft.md,
  "Action vocabulary"). The validator uses the same profile value

Values outside the joint limits are NOT rejected here, and a joint named twice
produces two actions. The safety validator rejects both
(``JOINT_LIMIT_EXCEEDED``, ``DUPLICATE_JOINT_TARGET``), and so does the critic.
The planner must not hide them by clamping or overwriting.

Each action's ``reason`` is at most ``MAX_REASON_LENGTH`` characters.

Error payloads follow docs/schema-draft.md, "Event payloads" (planner failure):
``{"outcome": "error", "error": {"kind": ..., "detail": ...}}`` with ``detail``
at most ``MAX_DETAIL_LENGTH`` characters.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from robot_profile import SO101_PROFILE

__all__ = [
    "MAX_DETAIL_LENGTH",
    "MAX_REASON_LENGTH",
    "PLANNER_ERROR",
    "UNSUPPORTED_COMMAND",
    "PlanResult",
    "PlannerFailure",
    "plan_command",
]

# Planner error kinds (docs/schema-draft.md, "Planner error kinds").
UNSUPPORTED_COMMAND = "unsupported_command"
PLANNER_ERROR = "planner_error"

# Same cap as the critic error detail (critic_gate.MAX_DETAIL_LENGTH).
MAX_DETAIL_LENGTH = 200
# Cap on a generated action's reason text.
MAX_REASON_LENGTH = 200
# Echoed user tokens are cut to this length inside an error detail.
_MAX_TOKEN_ECHO = 20



# Joint names, the unit and the action cap are read from SO101_PROFILE on
# every call, never copied, so the planner cannot drift from the profile the
# validator uses.
def _supported_joints() -> tuple[str, ...]:
    return tuple(sorted(SO101_PROFILE.joint_position_limits))


def _unit() -> str:
    return SO101_PROFILE.joint_position_unit  # "rad"


def _max_actions() -> int | None:
    """The plan cap from the profile (10 for SO-101). None means no cap."""
    return SO101_PROFILE.max_actions


def _hint() -> str:
    joints = _supported_joints()
    return (
        f"Supported form: 'set joint {joints[0]} to 0.25 {_unit()}' "
        f"(joints {joints[0]} to {joints[-1]}, radians only), "
        "clauses separated by ';'."
    )


# The clause shape. The three captured tokens are checked separately so the
# error can say which one is wrong. re.ASCII keeps \s, \S and \d (and the
# case-insensitive keyword match) to ASCII, as a second line behind the
# isascii() check in _plan.
_CLAUSE = re.compile(
    r"set\s+joint\s+(\S+)\s+to\s+(\S+)\s+(\S+)", re.IGNORECASE | re.ASCII
)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", re.ASCII)
# str.strip() with no argument also strips Unicode spaces and some control
# characters; strip ASCII whitespace only.
_ASCII_WS = " \t\n\r\f\v"


@dataclass(frozen=True)
class PlannerFailure:
    kind: str
    detail: str

    def to_payload(self) -> dict[str, Any]:
        """The ``plan_generated`` payload for a planner failure."""
        return {"outcome": "error", "error": {"kind": self.kind, "detail": self.detail}}


@dataclass(frozen=True)
class PlanResult:
    """Exactly one of ``actions`` and ``failure`` is set."""

    actions: tuple[dict[str, Any], ...] | None = None
    failure: PlannerFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def _cap(detail: str) -> str:
    return detail[:MAX_DETAIL_LENGTH]


def _echo(token: str) -> str:
    return repr(token[:_MAX_TOKEN_ECHO])


def _fail(kind: str, detail: str) -> PlanResult:
    return PlanResult(failure=PlannerFailure(kind=kind, detail=_cap(detail)))


def _unsupported(detail: str) -> PlanResult:
    return _fail(UNSUPPORTED_COMMAND, detail)


def _parse_clause(index: int, clause: str) -> dict[str, Any] | PlanResult:
    """One action dict, or a failed PlanResult."""
    match = _CLAUSE.fullmatch(clause)
    if match is None:
        return _unsupported(f"Clause {index} is not a supported command. {_hint()}")
    joint, number, unit = match.groups()

    joints = _supported_joints()
    expected_unit = _unit()
    if joint not in joints:
        return _unsupported(
            f"Clause {index}: joint {_echo(joint)} is not supported. "
            f"Only {', '.join(joints)}."
        )
    if unit.lower() != expected_unit:
        return _unsupported(
            f"Clause {index}: unit {_echo(unit)} is not supported. Only {expected_unit}."
        )
    if _NUMBER.fullmatch(number) is None:
        return _unsupported(
            f"Clause {index}: value {_echo(number)} is not a plain decimal number."
        )
    value = float(number)
    if not math.isfinite(value):
        # A very long digit string overflows to infinity.
        return _unsupported(f"Clause {index}: value is not a finite number.")
    value += 0.0  # turn -0.0 into 0.0

    return {
        "actionId": f"a{index}",
        "type": "set_joint",
        "target": joint,
        "value": value,
        "unit": expected_unit,
        "reason": f"Command clause {index}: set joint {joint} to {number} {expected_unit}"[
            :MAX_REASON_LENGTH
        ],
    }


def _plan(text: object) -> PlanResult:
    if not isinstance(text, str):
        return _fail(PLANNER_ERROR, "Command text is missing or is not a string.")
    if not text.isascii():
        return _unsupported(f"The command contains non-ASCII characters. {_hint()}")
    if not text.strip(_ASCII_WS):
        return _unsupported(f"The command is empty. {_hint()}")

    clauses = [c.strip(_ASCII_WS) for c in text.split(";")]
    max_actions = _max_actions()
    if max_actions is not None and len(clauses) > max_actions:
        return _unsupported(
            f"The command has {len(clauses)} clauses; at most {max_actions} are allowed."
        )

    actions: list[dict[str, Any]] = []
    for index, clause in enumerate(clauses, start=1):
        if not clause:
            return _unsupported(f"Clause {index} is empty. {_hint()}")
        parsed = _parse_clause(index, clause)
        if isinstance(parsed, PlanResult):
            return parsed
        actions.append(parsed)
    return PlanResult(actions=tuple(actions))


def plan_command(text: object) -> PlanResult:
    """Plan ``text``. Never raises: an internal error becomes ``planner_error``."""
    try:
        return _plan(text)
    except Exception as exc:  # fail closed: no plan
        return _fail(PLANNER_ERROR, f"Internal planner error ({type(exc).__name__}).")
