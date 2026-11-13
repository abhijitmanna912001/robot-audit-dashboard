"""Read-only Cyberwave wrapper for the SO-101 digital twin, simulation only.

Written against the installed SDK (cyberwave 0.7.5). Points that drove the
design, all taken from the SDK source:

* ``Cyberwave(...)`` defaults to ``mode="live"``. This wrapper always passes
  ``mode="simulation"`` and verifies the resulting config before going on.
* ``Cyberwave.affect("sim" | "simulation" | "mujoco")`` auto-starts a BILLABLE
  MuJoCo cloud instance. This wrapper never calls ``affect``. Passing
  ``mode="simulation"`` to the constructor sets the runtime mode without
  starting anything.
* Assigning to ``twin.joints.<name>`` or calling ``set_joints`` publishes a
  joint command. Nothing in this module does either. Movement is out of scope
  until the validator and critic gates exist.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from config import CyberwaveSettings

SIMULATION_MODE = "simulation"
SIMULATION_SOURCE_TYPE = "sim"  # cyberwave.constants.SOURCE_TYPE_SIM


class RobotConnectionError(Exception):
    """A Cyberwave call failed or the client is not in the expected state."""


@dataclass(frozen=True)
class JointReading:
    """Joint positions as reported by the SDK (radians per the SDK docs).

    ``telemetry_received`` is False when no joint update arrived over MQTT in
    the read window. In that case the SDK fills every joint with 0.0, so the
    positions are placeholders and must not be treated as measurements.
    """

    positions: dict[str, float]
    telemetry_received: bool


class SimulatedRobot:
    """Connects to one twin in simulation mode and reads its joints."""

    def __init__(self, settings: CyberwaveSettings) -> None:
        self._settings = settings
        self._client: Any = None
        self._twin: Any = None
        self._joint_subscription: Any = None
        self._telemetry_seen = threading.Event()

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Build the SDK client in simulation mode. Makes no network call."""
        from cyberwave import Cyberwave

        try:
            client = Cyberwave(
                api_key=self._settings.api_key,
                # None is valid: the SDK signature is Optional[str] = None, and
                # a single-workspace token needs no workspace id.
                workspace_id=self._settings.workspace_id,
                environment_id=self._settings.environment_id,
                mode=SIMULATION_MODE,
            )
        except Exception as exc:
            raise RobotConnectionError(f"Could not create Cyberwave client: {exc}") from exc

        # Fail closed: refuse to continue unless both the runtime mode and the
        # MQTT source type say simulation. A stray CYBERWAVE_SOURCE_TYPE in the
        # environment could otherwise leave the source type on a live value.
        runtime_mode = getattr(client.config, "runtime_mode", None)
        source_type = getattr(client.config, "source_type", None)
        if runtime_mode != SIMULATION_MODE or source_type != SIMULATION_SOURCE_TYPE:
            client.disconnect()
            raise RobotConnectionError(
                "Refusing to continue: client is not in simulation mode "
                f"(runtime_mode={runtime_mode!r}, source_type={source_type!r})."
            )
        self._client = client

    def verify_auth(self) -> int:
        """Make one authenticated read. Returns the number of visible workspaces."""
        client = self._require_client()
        try:
            return len(client.workspaces.list())
        except Exception as exc:
            raise RobotConnectionError(f"Authentication check failed: {exc}") from exc

    def load_twin(self) -> None:
        """Fetch the twin for the configured asset, creating it if absent.

        ``Cyberwave.twin`` is get-or-create: with no twin of this asset in the
        environment it creates one, and with no environment configured it
        reuses or creates a "Quickstart Environment". Neither starts a
        simulation.

        The identifier should be the asset UUID. SDK 0.7.5 resolves a UUID with
        a direct fetch, but resolves a registry id ("vendor/model") through a
        list call that returns nothing once the client carries a workspace
        filter, which then surfaces as "Asset not found".
        """
        client = self._require_client()
        identifier = self._settings.robot_asset_identifier
        if not identifier:
            raise RobotConnectionError(
                "No robot asset configured. Set ROBOT_ASSET_IDENTIFIER to the asset UUID."
            )
        try:
            self._twin = client.twin(identifier)
        except Exception as exc:
            raise RobotConnectionError(f"Could not load twin '{identifier}': {exc}") from exc

    def disconnect(self) -> list[str]:
        """Release everything. Never raises; returns any cleanup errors."""
        errors: list[str] = []
        steps = (
            ("cancel joint subscription", self._cancel_joint_subscription),
            ("twin.disconnect", lambda: self._twin.disconnect() if self._twin else None),
            ("client.disconnect", lambda: self._client.disconnect() if self._client else None),
        )
        for label, step in steps:
            try:
                step()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        self._twin = None
        self._client = None
        return errors

    def __enter__(self) -> "SimulatedRobot":
        self.connect()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.disconnect()

    # -- read-only twin information ----------------------------------------

    @property
    def runtime_mode(self) -> str:
        return str(self._require_client().config.runtime_mode)

    @property
    def sdk_client(self) -> Any:
        """The connected SDK client, for read-only diagnostic scripts."""
        return self._require_client()

    def twin_summary(self) -> dict[str, str]:
        twin = self._require_twin()
        return {
            "class": type(twin).__name__,
            "uuid": str(twin.uuid),
            "name": str(twin.name),
            "environment_id": str(twin.environment_id),
        }

    def capabilities(self) -> dict[str, Any]:
        return dict(self._require_twin().capabilities)

    def joint_descriptors(self) -> list[dict[str, Any]]:
        """Raw joint entries from the twin's universal schema (REST only)."""
        twin = self._require_twin()
        try:
            joints = twin.get_schema("/joints")
        except Exception as exc:
            raise RobotConnectionError(f"Could not read joint schema: {exc}") from exc
        if not isinstance(joints, list):
            return []
        return [joint for joint in joints if isinstance(joint, dict)]

    def joint_names(self) -> list[str]:
        """Controllable joint names, as the SDK lists them (sorted by name)."""
        try:
            return list(self._joints_handle().list())
        except RobotConnectionError:
            raise
        except Exception as exc:
            raise RobotConnectionError(f"Could not list joints: {exc}") from exc

    def read_joint_positions(self, timeout_s: float = 3.0) -> JointReading:
        """Read current joint positions without commanding anything."""
        twin = self._require_twin()
        self._joints_handle()  # make sure the telemetry listener is attached
        try:
            view = twin.get_joints(timeout=timeout_s)
            try:
                positions = {str(name): float(value) for name, value in view.items()}
            finally:
                view.stop()
        except Exception as exc:
            raise RobotConnectionError(f"Could not read joint positions: {exc}") from exc
        return JointReading(
            positions=positions, telemetry_received=self._telemetry_seen.is_set()
        )

    # -- internals ----------------------------------------------------------

    def _joints_handle(self) -> Any:
        """Return ``twin.joints``, subscribing once to inbound joint updates.

        First access connects MQTT and subscribes to the twin's joint topic.
        The subscription only records that an update arrived, so a zero-filled
        fallback can be told apart from real telemetry. Note the SDK notifies
        for updates of any source type, including live ones.
        """
        twin = self._require_twin()
        try:
            handle = twin.joints
        except AttributeError as exc:
            raise RobotConnectionError(
                f"Twin class {type(twin).__name__} has no joints handle."
            ) from exc
        except Exception as exc:
            raise RobotConnectionError(f"Could not open joint telemetry: {exc}") from exc
        if self._joint_subscription is None:
            self._joint_subscription = handle.on_update(
                lambda _snapshot: self._telemetry_seen.set()
            )
        return handle

    def _cancel_joint_subscription(self) -> None:
        if self._joint_subscription is not None:
            self._joint_subscription.cancel()
            self._joint_subscription = None

    def _require_client(self) -> Any:
        if self._client is None:
            raise RobotConnectionError("Not connected. Call connect() first.")
        return self._client

    def _require_twin(self) -> Any:
        if self._twin is None:
            raise RobotConnectionError("No twin loaded. Call load_twin() first.")
        return self._twin
