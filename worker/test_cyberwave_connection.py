"""Manual Cyberwave connection test for the SO-101 twin. Read-only.

Run from the worker directory:

    .venv\\Scripts\\python.exe test_cyberwave_connection.py

What it does: authenticates, selects simulation mode, looks up the EXISTING
twin named by CYBERWAVE_TWIN_ID, verifies that it belongs to
CYBERWAVE_ENVIRONMENT_ID and is the asset named by ROBOT_ASSET_IDENTIFIER (the
SO-101 asset UUID), lists its joints, reads current joint positions, and
disconnects.

What it does NOT do: it never calls ``affect()`` (which would start a billable
MuJoCo simulation), never targets live hardware, and never sends a joint,
gripper or motion command. It never creates a twin or an environment: all three
ids are required, and a missing or mismatching one stops the script.

Side effect to know about: reading joint positions opens the SDK's MQTT
connection, which can publish session messages for the twin (no command).

This is a script, not a unit test. It contains no TestCase classes, so unittest
discovery imports it without running anything.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

from config import ConfigError, load_cyberwave_twin_settings, redact
from cyberwave_client import RobotConnectionError, SimulatedRobot


def _step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")


def _print_joint_descriptors(descriptors: list[dict[str, Any]]) -> None:
    """Print each joint's name, type and any limit data exactly as returned."""
    if not descriptors:
        print("    No joint entries returned by the universal schema.")
        return
    for joint in descriptors:
        limit_fields = {
            key: value for key, value in joint.items() if "limit" in key.lower()
        }
        limits = json.dumps(limit_fields, sort_keys=True) if limit_fields else "none in schema"
        print(
            f"    - name={joint.get('name')!r} type={joint.get('type')!r} "
            f"limit fields: {limits}"
        )


def run(timeout_s: float) -> int:
    try:
        settings = load_cyberwave_twin_settings()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    robot = SimulatedRobot(settings)
    try:
        _step(1, "Create client in simulation mode (no simulation is started)")
        robot.connect()
        print(f"    runtime mode: {robot.runtime_mode}")

        _step(2, "Authenticate (one read-only API call)")
        workspace_count = robot.verify_auth()
        print(f"    OK. Workspaces visible to this key: {workspace_count}")

        _step(3, "Look up the existing twin and verify its environment and asset (no create)")
        robot.load_existing_twin()
        for key, value in robot.twin_summary().items():
            print(f"    {key}: {value}")

        _step(4, "Twin capabilities (as reported by the API)")
        print("    " + json.dumps(robot.capabilities(), sort_keys=True, default=str))

        _step(5, "Joint entries from the universal schema")
        _print_joint_descriptors(robot.joint_descriptors())

        _step(6, "Controllable joint names (twin.joints.list())")
        names = robot.joint_names()
        print(f"    {len(names)} joint(s): {names}")

        _step(7, f"Current joint positions (waiting up to {timeout_s:g}s for telemetry)")
        reading = robot.read_joint_positions(timeout_s=timeout_s)
        if reading.telemetry_received:
            print("    Joint update received over MQTT. Values from the SDK (radians):")
        else:
            print(
                "    NO joint update received. The values below are the SDK's "
                "zero-fill placeholders, NOT measurements.\n"
                "    This is expected when no simulation is running for the environment."
            )
        for name, position in sorted(reading.positions.items()):
            print(f"    - {name}: {position:.6f} rad ({math.degrees(position):.3f} deg)")

        print("\nRESULT: connection test completed.")
        return 0
    except RobotConnectionError as exc:
        print(f"\nFAILED: {redact(exc)}")
        return 1
    except Exception as exc:  # unexpected SDK error: report it, still clean up
        print(f"\nFAILED (unexpected {type(exc).__name__}): {redact(exc)}")
        return 1
    finally:
        cleanup_errors = robot.disconnect()
        if cleanup_errors:
            print("Disconnect finished with errors: " + redact("; ".join(cleanup_errors)))
        else:
            print("Disconnected cleanly.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for the first joint telemetry message (default: 3).",
    )
    args = parser.parse_args()
    return run(timeout_s=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
