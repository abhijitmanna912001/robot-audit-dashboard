"""Static robot data the safety validator checks plans against.

The SO-101 values come from the Cyberwave universal schema for our twin, as
recorded in docs/robot-capabilities.md section 6 (source label "Schema"). A
unit test keeps this file and that table in agreement.

What these numbers are, and are not:

* They are the position limits the platform's model of the asset declares.
* They are NOT a verified safe operating envelope. Nothing has been moved, so
  no limit has been confirmed by motion, and collisions are not considered.
* Joint names are the schema's own (``_1`` to ``_6``). Their physical meaning is
  unknown and no alias (for example ``joint_a``) maps onto them.
* The schema's velocity and effort values (10.0 for every joint) are left out
  on purpose: they are not confirmed to be real per-joint limits and must not
  be treated as safe operating values.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class JointLimit:
    """Inclusive position limits for one joint, in the profile's unit."""

    lower: float
    upper: float


@dataclass(frozen=True)
class RobotProfile:
    name: str
    joint_position_limits: Mapping[str, JointLimit]
    # The only unit string the validator accepts for joint positions. No
    # conversion is done: any other unit is rejected.
    joint_position_unit: str
    supported_action_types: frozenset[str]
    # Maximum number of actions in one plan. None means "not agreed yet", and
    # the MAX_ACTIONS_EXCEEDED rule is then not applied.
    max_actions: int | None = None


SO101_PROFILE = RobotProfile(
    name="so101",
    joint_position_limits=MappingProxyType(
        {
            "_1": JointLimit(-1.91986, 1.91986),
            "_2": JointLimit(-1.74533, 1.74533),
            "_3": JointLimit(-1.74533, 1.5708),
            "_4": JointLimit(-1.65806, 1.65806),
            "_5": JointLimit(-2.79253, 2.79253),
            "_6": JointLimit(-0.174533, 1.74533),
        }
    ),
    joint_position_unit="rad",
    # Only set_joint maps to an SDK call that exists for this twin. "stop" has
    # no SDK call for arm twins and "move_forward" does not apply to a fixed
    # arm (docs/robot-capabilities.md section 5).
    supported_action_types=frozenset({"set_joint"}),
    max_actions=None,
)
