"""Telemetry validation for the SO-101 simulation rehearsal. Pure parsing only.

No Cyberwave, no network, no database, no file. Whatever talks to the SDK hands
a ``RawTelemetry`` to ``validate_telemetry``; this module decides whether it may
be treated as a MEASURED joint state of the SIMULATION. It never moves anything.

Why this exists (docs/robot-capabilities.md section 3, SDK 0.7.5 source): the
SDK's ``get_joints()`` returns a bare ``{joint: position}`` mapping with no
timestamp and no source, and it returns 0.0 for every joint when no update has
arrived. So a successful call proves nothing:

* a zero-filled reading is a placeholder unless proven otherwise;
* a commanded target is not a measured position;
* a cached or default value is not a measured position;
* a reading with no known source, or an old one, is not usable.

A reading is accepted only if ALL of these hold:

* exactly the six joints ``_1`` to ``_6``, each a finite number (radians);
* ``source`` is exactly ``"simulation"``: the adapter verified, by the method
  the Cyberwave engineers confirm, that the simulation produced it;
* ``kind`` is exactly ``"measured"``: actual state, not a commanded target, an
  edited default or a placeholder;
* ``update_received`` is ``True``: a state update really arrived for this read;
* ``observed_at`` is a timezone-aware acquisition time, not older than
  ``MAX_TELEMETRY_AGE_S``, not in the future, and (when ``not_before`` is given)
  not from before the movement it is meant to verify.

Anything else raises ``TelemetryRejected`` with a short reason code. There is no
default that turns a missing field into an accepted one.

OPEN, pending Cyberwave engineer confirmation: which SDK call and which fields
let an adapter truthfully set ``source``, ``kind``, ``update_received`` and
``observed_at``. Until then no real adapter can produce an accepted reading
(see ``simulation_gates.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping

from simulation_payloads import JOINT_NAMES, TOLERANCE_RAD, build_snapshot

__all__ = [
    "KIND_MEASURED",
    "MAX_CLOCK_SKEW_S",
    "MAX_TELEMETRY_AGE_S",
    "SOURCE_SIMULATION",
    "JointSnapshot",
    "RawTelemetry",
    "TelemetryRejected",
    "joints_outside_tolerance",
    "validate_telemetry",
]

SOURCE_SIMULATION = "simulation"
KIND_MEASURED = "measured"
# A reading older than this is stale. Conservative: the rehearsal compares
# positions before and after a movement, so an old reading is worse than none.
MAX_TELEMETRY_AGE_S = 2.0
# How far in the future an acquisition time may be (clock differences).
MAX_CLOCK_SKEW_S = 0.5


class TelemetryRejected(Exception):
    """A reading may not be used as measured simulation state.

    ``reason`` is a fixed short code (safe to store in an audit payload).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class RawTelemetry:
    """What an adapter reports for one read. Nothing here is trusted yet.

    Every field defaults to ``None``, which is always refused: an adapter that
    cannot establish a fact must leave it unset, never guess it.
    """

    positions: Any = None  # {joint name: radians}
    source: Any = None  # "simulation" only when the adapter verified it
    kind: Any = None  # "measured", or what it really is ("commanded", "placeholder")
    observed_at: Any = None  # timezone-aware datetime of acquisition
    update_received: Any = None  # True only if a state update arrived for this read


@dataclass(frozen=True)
class JointSnapshot:
    """A validated, measured reading of all six joints (radians)."""

    read_at: datetime  # timezone-aware, UTC
    joints: Mapping[str, float]

    def to_payload(self) -> dict[str, Any]:
        """The agreed snapshot payload: ``{"readAt": "...Z", "joints": {...}}``."""
        return build_snapshot(self.read_at, dict(self.joints))


def _reject(reason: str, message: str) -> None:
    raise TelemetryRejected(reason, message)


def _positions(raw_positions: Any) -> dict[str, float]:
    if not isinstance(raw_positions, Mapping):
        _reject("positions_missing", "the reading has no joint positions")
    if set(raw_positions) != set(JOINT_NAMES) or len(raw_positions) != len(JOINT_NAMES):
        _reject("joint_set_mismatch", "the reading does not have exactly joints _1 to _6")
    positions: dict[str, float] = {}
    for name in JOINT_NAMES:
        value = raw_positions[name]
        # bool is an int in Python, and a numeric string is not a number.
        if type(value) not in (int, float):
            _reject("non_numeric_value", f"joint {name} is not a number")
        try:
            number = float(value)
        except OverflowError:
            _reject("non_finite_value", f"joint {name} is not finite")
        if not math.isfinite(number):
            _reject("non_finite_value", f"joint {name} is not finite")
        positions[name] = number + 0.0  # -0.0 becomes 0.0
    return positions


def _observed_at(value: Any, now: datetime, not_before: datetime | None, max_age_s: float) -> datetime:
    if value is None:
        _reject("timestamp_missing", "the reading has no acquisition time")
    if not isinstance(value, datetime) or value.utcoffset() is None:
        _reject("timestamp_invalid", "the acquisition time is not a timezone-aware datetime")
    observed = value.astimezone(timezone.utc)
    if observed > now + timedelta(seconds=MAX_CLOCK_SKEW_S):
        _reject("timestamp_in_future", "the acquisition time is in the future")
    if now - observed > timedelta(seconds=max_age_s):
        _reject("stale", f"the reading is older than {max_age_s:g} s")
    if not_before is not None and observed < not_before:
        _reject("stale_before_dispatch", "the reading is from before the movement was sent")
    return observed


def validate_telemetry(
    raw: Any,
    *,
    now: datetime,
    not_before: datetime | None = None,
    max_age_s: float = MAX_TELEMETRY_AGE_S,
) -> JointSnapshot:
    """Return a ``JointSnapshot`` or raise ``TelemetryRejected``. Pure.

    ``now`` and ``not_before`` are timezone-aware datetimes. ``max_age_s`` may be
    tightened by a caller but never loosened past ``MAX_TELEMETRY_AGE_S``.
    """
    for name, moment in (("now", now), ("not_before", not_before)):
        if moment is not None and (not isinstance(moment, datetime) or moment.utcoffset() is None):
            raise ValueError(f"{name} must be a timezone-aware datetime.")
    if (
        type(max_age_s) not in (int, float)
        or not math.isfinite(max_age_s)
        or not 0 < max_age_s <= MAX_TELEMETRY_AGE_S
    ):
        raise ValueError(f"max_age_s must be over 0 and at most {MAX_TELEMETRY_AGE_S}.")
    if not isinstance(raw, RawTelemetry):
        _reject("not_telemetry", "the read did not return a telemetry reading")

    positions = _positions(raw.positions)

    measured = type(raw.kind) is str and raw.kind == KIND_MEASURED
    from_simulation = type(raw.source) is str and raw.source == SOURCE_SIMULATION
    update_arrived = raw.update_received is True

    # The SDK returns 0.0 for every joint when no update arrived. Zeros are only
    # believed with full proof that they were measured.
    if all(value == 0.0 for value in positions.values()) and not (
        measured and from_simulation and update_arrived
    ):
        _reject(
            "placeholder_suspected",
            "every joint is 0.0 and nothing proves it was measured (SDK zero-fill)",
        )
    if raw.source is None:
        _reject("source_unverified", "the reading has no verified source")
    if not from_simulation:
        _reject("not_simulation_source", "the reading is not from the simulation")
    if raw.kind == "commanded":
        _reject("commanded_not_measured", "a commanded target is not a measured position")
    if raw.kind == "placeholder":
        _reject("placeholder", "the reading is a placeholder, not a measurement")
    if not measured:
        _reject("kind_unverified", "the reading is not verified as measured state")
    if not update_arrived:
        _reject("no_update_received", "no state update arrived for this read")

    observed = _observed_at(raw.observed_at, now.astimezone(timezone.utc), not_before, max_age_s)
    return JointSnapshot(read_at=observed, joints=MappingProxyType(positions))


def joints_outside_tolerance(
    snapshot: JointSnapshot, expected: Mapping[str, float]
) -> tuple[str, ...]:
    """The joints in ``expected`` whose reading differs by more than ``TOLERANCE_RAD``.

    The tolerance is the fixed first-rehearsal value; it is not a parameter.
    """
    return tuple(
        name
        for name in JOINT_NAMES
        if name in expected and abs(snapshot.joints[name] - expected[name]) > TOLERANCE_RAD
    )
