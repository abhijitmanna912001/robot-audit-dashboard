"""Telemetry validation for the SO-101 simulation rehearsal. Pure parsing only.

No Cyberwave, no network, no database, no file. A caller hands a
``RawTelemetry`` to ``validate_telemetry``; this module decides whether the
reading it was given has the form a simulation joint-state message must have.
It never moves anything.

WHAT THIS MODULE IS: a validation layer for the source label and the timestamp
SUPPLIED to it. It is not a Cyberwave telemetry integration.

* ``RawTelemetry`` is the validator's input structure, nothing more. Its
  ``source_type`` and ``timestamp`` fields are named after fields of a
  Cyberwave joint-state message, and a future adapter is expected to copy them
  from a message it actually received.
* Nothing here can check that it did. Any caller can construct a
  ``RawTelemetry`` with ``source_type="sim"`` and a current timestamp, and it
  will pass. Today the only readings that carry a ``source_type`` and a
  ``timestamp`` are built by test fixtures and by the fake twin
  (``tests/fake_twin.py``), with synthetic values.
* So a pass means "the supplied label is exactly ``sim`` and the supplied
  timestamp is fresh". It does NOT establish telemetry provenance: it does not
  show that a message was received, who sent it, or that its fields were
  copied faithfully.
* No worker code path gives this validator fields taken from a real incoming
  message, and no real Cyberwave telemetry has been received or verified by the
  worker. ``read_joint_telemetry.py`` reads through ``get_joints()``, which
  provides neither field, so its readings are always refused.

IMPORTANT: passing this validator is NECESSARY, NOT PROOF. Even with a real
adapter, ``source_type == "sim"`` and a fresh timestamp would only be what the
sender declared. That would not establish that the positions are measured
simulator state rather than an echoed target, an interpolated command or an
initial placeholder. Whether they are is an open question for the Cyberwave
engineers, and these gates in ``simulation_gates.CYBERWAVE_GATES`` stay
``False`` until it is answered: ``telemetry_is_actual_state_confirmed``,
``telemetry_source_confirmed``, ``measured_vs_placeholder_confirmed``. The
executor refuses before any read while they are ``False``.

Why this exists (docs/robot-capabilities.md section 3, SDK 0.7.5 source): the
SDK's ``get_joints()`` returns a bare ``{joint: position}`` mapping with no
timestamp and no source, it returns 0.0 for every joint when no update has
arrived, and a joint command writes its target into the same cache. So a
successful call proves nothing.

The earlier fields ``source="simulation"`` and ``kind="measured"`` were an
adapter's own conclusion about a reading. They are gone: the validator now
looks at a wire-level label and time instead. That narrows what a caller is
asked to state. It does not stop a caller from stating it falsely.

A reading is accepted only if ALL of these hold:

* exactly the six joints ``_1`` to ``_6``, each a finite number (radians);
* ``source_type`` is exactly the string ``"sim"`` (no trimming, no case
  folding). In the Cyberwave wire contract
  (``cyberwave_contracts/source_type_axes.yml``) ``sim`` is state published by
  the simulator, while ``sim_tele`` and ``edit`` are commands from the frontend
  and ``edge``, ``edge_leader``, ``edge_follower`` and ``tele`` concern
  hardware;
* ``update_received`` is ``True``: the caller reports that a state update
  arrived for this read;
* ``timestamp`` is a numeric Unix time in seconds (the unit of the aggregated
  joint-state message), finite and positive, not older than
  ``MAX_TELEMETRY_AGE_S``, not in the future, and (when ``not_before`` is
  given) not from before the movement it is meant to verify.

Anything else raises ``TelemetryRejected`` with a short fixed reason code.
There is no default that turns a missing field into an accepted one.

The Unix timestamp is converted to an aware UTC datetime and, in a payload, to
ISO 8601 UTC with a trailing ``Z`` (``simulation_payloads.format_utc``). The
original number stays in ``RawTelemetry.timestamp``.

Not tested against Cyberwave: every reading in the tests is a fixture or comes
from the fake twin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping

from simulation_payloads import JOINT_NAMES, TOLERANCE_RAD, build_snapshot, format_utc

__all__ = [
    "COMMAND_SOURCE_TYPES",
    "HARDWARE_SOURCE_TYPES",
    "MAX_CLOCK_SKEW_S",
    "MAX_TELEMETRY_AGE_S",
    "MAX_UNIX_SECONDS",
    "SOURCE_TYPE_SIM",
    "JointSnapshot",
    "RawTelemetry",
    "TelemetryRejected",
    "joints_outside_tolerance",
    "unix_seconds_to_iso_z",
    "unix_seconds_to_utc",
    "validate_telemetry",
]

# The one accepted source_type: state published by the simulator.
SOURCE_TYPE_SIM = "sim"
# Commands, not state (cyberwave_contracts/source_type_axes.yml, direction: command).
COMMAND_SOURCE_TYPES = frozenset({"sim_tele", "edit"})
# Hardware, not the simulation (substrate: hardware).
HARDWARE_SOURCE_TYPES = frozenset({"edge", "edge_leader", "edge_follower", "tele"})

# A reading older than this is stale. Conservative: the rehearsal compares
# positions before and after a movement, so an old reading is worse than none.
MAX_TELEMETRY_AGE_S = 2.0
# How far in the future a timestamp may be (clock differences).
MAX_CLOCK_SKEW_S = 0.5
# Unix SECONDS stay far below this (it is the year 5138). The same moment in
# milliseconds, microseconds or nanoseconds is far above it, so a timestamp at
# or over this value is in the wrong unit and is refused, never rescaled.
MAX_UNIX_SECONDS = 1e11


class TelemetryRejected(Exception):
    """A reading does not have the form of a fresh simulation joint-state message.

    ``reason`` is a fixed short code (safe to store in an audit payload).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class RawTelemetry:
    """The input of ``validate_telemetry`` for one read. A validation input, not evidence.

    ``source_type`` and ``timestamp`` are named after fields of a Cyberwave
    joint-state message, and an adapter should copy them unchanged from a
    message it received. This class cannot enforce that. It is a plain
    dataclass: any caller can construct it with any values, and today only test
    fixtures and the fake twin set ``source_type`` and ``timestamp``, with
    synthetic values. A value here is a label that was supplied to the
    validator. It does not establish where a reading came from.

    Each field defaults to ``None``, which is always refused: a caller that
    does not have a field must leave it unset, never fill it in.
    """

    positions: Any = None  # {joint name: radians}
    source_type: Any = None  # the supplied source label (intended: the message's source_type)
    timestamp: Any = None  # the supplied Unix seconds (intended: the message's timestamp)
    update_received: Any = None  # True only if a state update arrived for this read


@dataclass(frozen=True)
class JointSnapshot:
    """A reading of all six joints (radians) that passed ``validate_telemetry``.

    Passing is necessary, not proof that the positions are measured simulator
    state, and it does not establish provenance: see the module docstring.
    ``read_at`` is the supplied timestamp converted to UTC.
    """

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


def _is_sim_source(source_type: Any) -> bool:
    """Exactly the string ``"sim"``: no trimming, no case folding, no subclass."""
    return type(source_type) is str and source_type == SOURCE_TYPE_SIM


def _require_sim_source(source_type: Any) -> None:
    """Refuse unless the supplied ``source_type`` is exactly ``"sim"``.

    This checks the label it is given. It cannot tell where the label came from.
    """
    if source_type is None:
        _reject("source_unverified", "the reading carries no source_type")
    if type(source_type) is not str:
        _reject("source_invalid", "the reading's source_type is not a string")
    if source_type == SOURCE_TYPE_SIM:
        return
    if source_type in COMMAND_SOURCE_TYPES:
        _reject("commanded_not_measured", "the source_type marks a command, not simulator state")
    if source_type in HARDWARE_SOURCE_TYPES:
        _reject("not_simulation_source", "the source_type marks hardware, not the simulation")
    _reject("source_unverified", "the reading's source_type is not recognised")


def unix_seconds_to_utc(value: Any) -> datetime:
    """A numeric Unix timestamp in SECONDS as an aware UTC datetime, or raise.

    Refused, each with its own reason: a missing value, anything that is not an
    ``int`` or a ``float`` (a boolean and a numeric string included), NaN or
    infinity, zero or a negative number, and a magnitude that can only be
    milliseconds or finer. Nothing is guessed or rescaled.
    """
    if value is None:
        _reject("timestamp_missing", "the reading has no timestamp")
    # bool is an int in Python; "1763109001" is a string. Neither is a timestamp.
    if type(value) not in (int, float):
        _reject("timestamp_invalid", "the timestamp is not a number")
    try:
        seconds = float(value)
    except OverflowError:
        _reject("timestamp_not_seconds", "the timestamp is too large to be Unix seconds")
    if not math.isfinite(seconds):
        _reject("timestamp_not_finite", "the timestamp is not a finite number")
    if seconds <= 0:
        _reject("timestamp_not_positive", "the timestamp is zero or negative")
    if seconds >= MAX_UNIX_SECONDS:
        _reject(
            "timestamp_not_seconds",
            "the timestamp is too large to be Unix seconds (milliseconds or finer?)",
        )
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        _reject("timestamp_invalid", "the timestamp is not a real date and time")
    return moment


def unix_seconds_to_iso_z(value: Any) -> str:
    """A Unix timestamp in seconds as the payload form: ISO 8601 UTC ending in ``Z``.

    Whole seconds, the same form ``simulation_payloads.build_snapshot`` writes
    (for example ``2026-11-14T08:30:01Z``). Raises ``TelemetryRejected`` for
    anything ``unix_seconds_to_utc`` refuses.
    """
    return format_utc(unix_seconds_to_utc(value))


def _observed_at(
    timestamp: Any, now: datetime, not_before: datetime | None, max_age_s: float
) -> datetime:
    observed = unix_seconds_to_utc(timestamp)
    if observed > now + timedelta(seconds=MAX_CLOCK_SKEW_S):
        _reject("timestamp_in_future", "the timestamp is in the future")
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

    A returned snapshot passed the checks in the module docstring on the values
    it was given. That is necessary for using a reading. It is not proof of
    measured state and it does not establish telemetry provenance.
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
    update_arrived = raw.update_received is True

    # The SDK returns 0.0 for every joint when no update arrived. An all-zero
    # reading is refused as a placeholder unless it is labelled "sim" and is
    # marked as having received an update for this read.
    if all(value == 0.0 for value in positions.values()) and not (
        _is_sim_source(raw.source_type) and update_arrived
    ):
        _reject(
            "placeholder_suspected",
            "every joint is 0.0 and the reading is not labelled as simulator state "
            "(SDK zero-fill)",
        )
    _require_sim_source(raw.source_type)
    if not update_arrived:
        _reject("no_update_received", "no state update arrived for this read")

    observed = _observed_at(raw.timestamp, now.astimezone(timezone.utc), not_before, max_age_s)
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
