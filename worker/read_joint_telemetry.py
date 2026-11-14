"""Manual, READ-ONLY script: read the six SO-101 joints and say whether to believe them.

It never moves anything. It uses only the read-only ``SimulatedRobot`` wrapper
(``cyberwave_client.py``): no ``affect()``, no joint, gripper or motion command,
no simulation start. It prints one JSON object.

    .venv\\Scripts\\python.exe read_joint_telemetry.py --confirm-connect

Without ``--confirm-connect`` it contacts nothing. With it, it makes the same
read-only calls as ``test_cyberwave_connection.py``: it looks up the EXISTING
twin (CYBERWAVE_TWIN_ID in CYBERWAVE_ENVIRONMENT_ID) and creates nothing.

WHAT TO EXPECT TODAY: ``"verified": false``. This is deliberate, not a bug.

Checked against the installed SDK source (cyberwave 0.7.5), not guessed:

* ``twin.get_joints()`` returns a ``{joint: position}`` mapping only. It has no
  timestamp and no source field.
* With no update received it returns 0.0 for every joint instead of failing.
* The joint update callback also receives positions only.
* Real positions need a running simulation, which this script does not start
  (starting one may be billable; that is a separate, approved step).

So this script cannot establish where a reading came from, when it was
measured, or whether it is a measurement at all. It reports the values as
UNVERIFIED and passes an honest ``RawTelemetry`` (source, kind and time left
unset) to ``sim_telemetry.validate_telemetry``, which refuses it. Once the
Cyberwave engineers confirm how telemetry identifies its simulation source and
how to tell measured state from placeholders, the mapping in ``_to_raw`` is the
one place to change.

Exit codes: 0 verified, 2 not confirmed or configuration error, 1 read failed,
3 read worked but the telemetry is not verified.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from config import ConfigError, load_cyberwave_twin_settings, redact
from cyberwave_client import JointReading, RobotConnectionError, SimulatedRobot
from sim_telemetry import RawTelemetry, TelemetryRejected, validate_telemetry

__all__ = ["main", "read_once"]

READ_TIMEOUT_S = 3.0


def _to_raw(reading: JointReading) -> RawTelemetry:
    """What the SDK read honestly supports today: positions and "an update arrived".

    ``source``, ``kind`` and ``observed_at`` stay unset. The SDK gives no field
    for them, and inventing them would turn an unverified reading into a
    "verified" one.
    """
    return RawTelemetry(
        positions=dict(reading.positions),
        update_received=reading.telemetry_received is True,
    )


def _finite_positions(positions: Any) -> dict[str, float] | None:
    try:
        return {str(name): float(value) for name, value in dict(positions).items()}
    except Exception:
        return None


def read_once(robot: Any, timeout_s: float = READ_TIMEOUT_S) -> dict[str, Any]:
    """Connect read-only, read the joints once, disconnect. Returns the report."""
    report: dict[str, Any] = {"verified": False, "movementSent": False}
    try:
        robot.connect()
        report["runtimeMode"] = str(robot.runtime_mode)
        robot.load_existing_twin()
        reading = robot.read_joint_positions(timeout_s=timeout_s)
    except RobotConnectionError as exc:
        report["error"] = redact(exc)[:200]
        return report
    except Exception as exc:
        report["error"] = f"unexpected {type(exc).__name__}"
        return report
    finally:
        robot.disconnect()

    report["updateReceived"] = reading.telemetry_received is True
    try:
        snapshot = validate_telemetry(_to_raw(reading), now=datetime.now(timezone.utc))
    except TelemetryRejected as exc:
        report["rejected"] = exc.reason
        report["reason"] = str(exc)
        # Shown for diagnosis only. These are NOT measurements.
        report["unverifiedPositions"] = _finite_positions(reading.positions)
        return report
    report["verified"] = True
    report["telemetry"] = snapshot.to_payload()
    return report


def main(
    argv: list[str] | None = None,
    robot_factory: Callable[[Any], Any] = SimulatedRobot,
) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only: read the six SO-101 joint positions and report whether they "
        "are verified simulation telemetry. Never moves anything, never starts a simulation."
    )
    parser.add_argument(
        "--confirm-connect",
        action="store_true",
        help="required: allow the read-only Cyberwave connection (no simulation is started)",
    )
    args = parser.parse_args(argv)
    if not args.confirm_connect:
        print("Refusing to connect without --confirm-connect. Nothing was contacted.")
        return 2

    try:
        settings = load_cyberwave_twin_settings()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    report = read_once(robot_factory(settings))
    print(json.dumps(report, indent=2, sort_keys=True))
    if "error" in report:
        return 1
    return 0 if report["verified"] else 3


if __name__ == "__main__":
    sys.exit(main())
