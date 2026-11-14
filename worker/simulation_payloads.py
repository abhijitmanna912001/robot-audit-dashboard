"""Strict builders and validators for the SIMULATION execution event payloads.

Pure: no Cyberwave, no network, no database, no file, no clock. Nothing here can
move anything. It only decides whether a payload has exactly the agreed shape.

The three shapes were agreed with Abhijit for the simulation-only SO-101
rehearsal. They are separate from the dry-run payloads, which are unchanged
(``{"dryRun": true}`` and so on, with no ``mode`` field; see
``command_processor.py``).

``execution_started``::

    {"dryRun": false, "mode": "simulation"}

``execution_completed``::

    {"dryRun": false, "mode": "simulation", "summary": "<= 200 characters",
     "telemetry": {"start": <snapshot>, "target": <snapshot>, "restored": <snapshot>},
     "toleranceRad": 0.01}

``execution_failed``::

    {"dryRun": false, "mode": "simulation",
     "error": {"kind": "short_snake_case", "detail": "<= 200 characters"},
     "stop": "confirmed" | "unconfirmed" | "not_needed",
     "lastTelemetry": <snapshot>}          # optional, only after a real read

A snapshot is ``{"readAt": "<UTC, ...Z>", "joints": {"_1": .., ... "_6": ..}}``
with exactly the six joints and finite numbers in radians.

Every validator raises ``SimulationPayloadError`` on anything else: a missing or
extra key, a wrong type (a boolean or a numeric string is not a number), NaN or
infinity, a timestamp that is not canonical UTC, a text over 200 characters, a
tolerance other than 0.01. ``validate_simulation_event`` is called by the
simulation processor before every write, so a malformed payload is refused
before it can reach ``audit_events`` (which is append-only).

Beyond the shape, ``execution_completed`` is also refused when its own numbers
contradict "completed": a restored joint more than ``toleranceRad`` away from
its start value, or snapshots that are out of time order.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ERROR_KINDS",
    "JOINT_NAMES",
    "MAX_ERROR_KIND_LENGTH",
    "MAX_TEXT_LENGTH",
    "MODE_SIMULATION",
    "STOP_CONFIRMED",
    "STOP_NOT_NEEDED",
    "STOP_UNCONFIRMED",
    "STOP_VALUES",
    "TOLERANCE_RAD",
    "SimulationPayloadError",
    "build_execution_completed",
    "build_execution_failed",
    "build_execution_started",
    "build_snapshot",
    "format_utc",
    "parse_utc",
    "validate_execution_completed",
    "validate_execution_failed",
    "validate_execution_started",
    "validate_simulation_event",
    "validate_snapshot",
]

MODE_SIMULATION = "simulation"
# The only joints of the SO-101 twin (docs/robot-capabilities.md section 2). A
# test keeps this equal to robot_profile.SO101_PROFILE and shared/so101-limits.json.
JOINT_NAMES: tuple[str, ...] = ("_1", "_2", "_3", "_4", "_5", "_6")
# Fixed for the first rehearsal. Not a parameter: no command or caller can
# choose a looser tolerance.
TOLERANCE_RAD = 0.01
MAX_TEXT_LENGTH = 200
MAX_ERROR_KIND_LENGTH = 40

STOP_CONFIRMED = "confirmed"
STOP_UNCONFIRMED = "unconfirmed"
STOP_NOT_NEEDED = "not_needed"
STOP_VALUES = frozenset({STOP_CONFIRMED, STOP_UNCONFIRMED, STOP_NOT_NEEDED})

# The only error kinds the worker writes. ``build_execution_failed`` refuses any
# other kind; ``validate_execution_failed`` checks the snake_case form.
ERROR_KINDS = frozenset(
    {
        "approval_invalid",  # the approved snapshot or its approvals did not re-verify
        "rehearsal_restriction",  # not exactly: set joint _1 to 0.25 rad
        "gate_unconfirmed",  # an engineer confirmation or approval is missing
        "event_write_failed",  # execution_started could not be recorded
        "status_update_failed",  # approved -> executing did not apply
        "connection_failed",  # the simulation connection could not be made
        "not_simulation",  # the runtime or the twin is not verified as simulation
        "telemetry_unavailable",  # a telemetry read failed or timed out
        "telemetry_invalid",  # a reading was refused (provenance, age, shape)
        "limits_revalidation_failed",  # shared/so101-limits.json re-check failed
        "dispatch_failed",  # a movement call failed or did not return
        "target_timeout",  # joint _1 did not reach the target in time
        "target_not_verified",  # the target reading did not hold up
        "unexpected_joint_motion",  # another joint moved beyond the tolerance
        "return_timeout",  # the arm did not return in time
        "restore_not_verified",  # the restored reading did not hold up
        "overall_timeout",  # the total execution deadline passed
        "internal_error",  # anything else
    }
)

_ERROR_KIND = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", re.ASCII)
# Canonical UTC only: a "Z" suffix, optional fractional seconds. An offset such
# as "+00:00" or "+05:30", a missing zone, or a space separator is refused.
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", re.ASCII
)

_STARTED_KEYS = frozenset({"dryRun", "mode"})
_COMPLETED_KEYS = frozenset({"dryRun", "mode", "summary", "telemetry", "toleranceRad"})
_FAILED_KEYS = frozenset({"dryRun", "mode", "error", "stop"})
_FAILED_OPTIONAL_KEYS = frozenset({"lastTelemetry"})
_TELEMETRY_KEYS = ("start", "target", "restored")
_SNAPSHOT_KEYS = frozenset({"readAt", "joints"})
_ERROR_KEYS = frozenset({"kind", "detail"})


class SimulationPayloadError(ValueError):
    """A simulation event payload does not have the agreed shape. Nothing was written."""


def _fail(message: str) -> None:
    raise SimulationPayloadError(message)


def _names(keys: Any) -> str:
    return ", ".join(sorted(str(key)[:40] for key in keys)) or "none"


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{where} must be an object, got {type(value).__name__}.")
    return value


def _require_keys(
    obj: dict[str, Any],
    required: frozenset[str],
    where: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(obj)
    if any(type(key) is not str for key in keys):
        _fail(f"{where} has a key that is not a string.")
    missing = required - keys
    if missing:
        _fail(f"{where} is missing: {_names(missing)}.")
    extra = keys - required - optional
    if extra:
        _fail(f"{where} has unexpected key(s): {_names(extra)}.")


def _require_text(value: Any, where: str) -> str:
    """Non-empty printable text, no surrounding whitespace, at most 200 characters."""
    if type(value) is not str:
        _fail(f"{where} must be a string, got {type(value).__name__}.")
    if not value.strip():
        _fail(f"{where} must not be empty.")
    if len(value) > MAX_TEXT_LENGTH:
        _fail(f"{where} is {len(value)} characters; the limit is {MAX_TEXT_LENGTH}.")
    if value != value.strip() or not value.isprintable():
        _fail(f"{where} has surrounding whitespace or a control character.")
    return value


def _require_mode_header(obj: dict[str, Any], where: str) -> None:
    if obj["dryRun"] is not False:
        _fail(f"{where}.dryRun must be the JSON boolean false.")
    if type(obj["mode"]) is not str or obj["mode"] != MODE_SIMULATION:
        _fail(f"{where}.mode must be {MODE_SIMULATION!r}.")


def _require_radians(value: Any, where: str) -> float:
    # bool is an int in Python, and "0.25" is a string: neither is a number here.
    if type(value) not in (int, float):
        _fail(f"{where} must be a number, got {type(value).__name__}.")
    try:
        number = float(value)
    except OverflowError:
        _fail(f"{where} is not a finite number.")
    if not math.isfinite(number):
        _fail(f"{where} must be a finite number.")
    return number


def parse_utc(value: Any, where: str = "readAt") -> datetime:
    """The aware UTC datetime of a canonical ``...Z`` timestamp, or raise."""
    if type(value) is not str:
        _fail(f"{where} must be a string, got {type(value).__name__}.")
    if _UTC_TIMESTAMP.fullmatch(value) is None:
        _fail(f"{where} must be an ISO-8601 UTC timestamp ending in 'Z'.")
    try:
        parsed = datetime.fromisoformat(value[:-1])
    except ValueError:
        _fail(f"{where} is not a real date and time.")
    return parsed.replace(tzinfo=timezone.utc)


def format_utc(moment: Any) -> str:
    """The canonical payload timestamp: UTC, whole seconds, ``Z`` suffix."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        _fail("a timestamp must be a timezone-aware datetime.")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_snapshot(value: Any, where: str = "snapshot") -> dict[str, float]:
    """Check one telemetry snapshot. Returns its joints as floats."""
    snapshot = _require_object(value, where)
    _require_keys(snapshot, _SNAPSHOT_KEYS, where)
    parse_utc(snapshot["readAt"], f"{where}.readAt")
    joints = _require_object(snapshot["joints"], f"{where}.joints")
    _require_keys(joints, frozenset(JOINT_NAMES), f"{where}.joints")
    return {
        name: _require_radians(joints[name], f"{where}.joints.{name}") for name in JOINT_NAMES
    }


def validate_execution_started(payload: Any) -> None:
    obj = _require_object(payload, "execution_started")
    _require_keys(obj, _STARTED_KEYS, "execution_started")
    _require_mode_header(obj, "execution_started")


def validate_execution_completed(payload: Any) -> None:
    where = "execution_completed"
    obj = _require_object(payload, where)
    _require_keys(obj, _COMPLETED_KEYS, where)
    _require_mode_header(obj, where)
    _require_text(obj["summary"], f"{where}.summary")

    tolerance = obj["toleranceRad"]
    if type(tolerance) is not float or tolerance != TOLERANCE_RAD:
        _fail(f"{where}.toleranceRad must be exactly {TOLERANCE_RAD}.")

    telemetry = _require_object(obj["telemetry"], f"{where}.telemetry")
    _require_keys(telemetry, frozenset(_TELEMETRY_KEYS), f"{where}.telemetry")
    joints = {
        name: validate_snapshot(telemetry[name], f"{where}.telemetry.{name}")
        for name in _TELEMETRY_KEYS
    }
    times = [
        parse_utc(telemetry[name]["readAt"], f"{where}.telemetry.{name}.readAt")
        for name in _TELEMETRY_KEYS
    ]
    if not times[0] <= times[1] <= times[2]:
        _fail(f"{where}.telemetry snapshots are not in time order (start, target, restored).")
    # "completed" means restored: the payload's own numbers must say so.
    for name in JOINT_NAMES:
        if abs(joints["restored"][name] - joints["start"][name]) > TOLERANCE_RAD:
            _fail(
                f"{where}.telemetry: joint {name} is not restored within "
                f"{TOLERANCE_RAD} rad of its start value."
            )


def validate_execution_failed(payload: Any) -> None:
    where = "execution_failed"
    obj = _require_object(payload, where)
    _require_keys(obj, _FAILED_KEYS, where, optional=_FAILED_OPTIONAL_KEYS)
    _require_mode_header(obj, where)

    error = _require_object(obj["error"], f"{where}.error")
    _require_keys(error, _ERROR_KEYS, f"{where}.error")
    kind = error["kind"]
    if (
        type(kind) is not str
        or len(kind) > MAX_ERROR_KIND_LENGTH
        or _ERROR_KIND.fullmatch(kind) is None
    ):
        _fail(
            f"{where}.error.kind must be short snake_case "
            f"(at most {MAX_ERROR_KIND_LENGTH} characters)."
        )
    _require_text(error["detail"], f"{where}.error.detail")

    stop = obj["stop"]
    if type(stop) is not str or stop not in STOP_VALUES:
        _fail(f"{where}.stop must be one of: {_names(STOP_VALUES)}.")

    if "lastTelemetry" in obj:
        validate_snapshot(obj["lastTelemetry"], f"{where}.lastTelemetry")


_VALIDATORS = {
    "execution_started": validate_execution_started,
    "execution_completed": validate_execution_completed,
    "execution_failed": validate_execution_failed,
}


def validate_simulation_event(event_type: Any, payload: Any) -> None:
    """Check ``payload`` against the agreed shape for ``event_type``, or raise."""
    validator = _VALIDATORS.get(event_type) if type(event_type) is str else None
    if validator is None:
        _fail("not a simulation execution event type.")
    validator(payload)


# --------------------------------------------------------------------------
# Builders. Each one validates what it built before returning it.
# --------------------------------------------------------------------------


def build_snapshot(read_at: Any, joints: Any) -> dict[str, Any]:
    """A snapshot payload from an aware datetime and a ``{joint: radians}`` mapping."""
    if type(joints) is not dict:
        _fail("joints must be a dict.")
    snapshot = {
        "readAt": format_utc(read_at),
        "joints": {
            name: (float(value) if type(value) in (int, float) else value)
            for name, value in joints.items()
        },
    }
    validate_snapshot(snapshot)
    return snapshot


def build_execution_started() -> dict[str, Any]:
    payload = {"dryRun": False, "mode": MODE_SIMULATION}
    validate_execution_started(payload)
    return payload


def build_execution_completed(
    summary: Any, start: Any, target: Any, restored: Any
) -> dict[str, Any]:
    """``start``, ``target`` and ``restored`` are snapshot payloads (``build_snapshot``)."""
    payload = {
        "dryRun": False,
        "mode": MODE_SIMULATION,
        "summary": summary,
        "telemetry": {"start": start, "target": target, "restored": restored},
        "toleranceRad": TOLERANCE_RAD,
    }
    validate_execution_completed(payload)
    return payload


def build_execution_failed(
    kind: Any, detail: Any, stop: Any, last_telemetry: Any = None
) -> dict[str, Any]:
    """``last_telemetry`` is a snapshot payload, given only after a real read succeeded."""
    if type(kind) is not str or kind not in ERROR_KINDS:
        _fail("error kind is not one of the fixed simulation error kinds.")
    payload: dict[str, Any] = {
        "dryRun": False,
        "mode": MODE_SIMULATION,
        "error": {"kind": kind, "detail": detail},
        "stop": stop,
    }
    if last_telemetry is not None:
        payload["lastTelemetry"] = last_telemetry
    validate_execution_failed(payload)
    return payload
