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
* ``Cyberwave.twin()`` is get-or-create: with no matching twin it CREATES one,
  and with no environment it creates a "Quickstart Environment"
  (``client.py:942-1036``). This wrapper never calls it. The twin is fetched by
  its id with ``client.twins.get_raw()``, which for a UUID is one
  ``GET /api/v1/twins/{uuid}`` and has no create path (``resources.py:2950-2970``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from config import CyberwaveSettings, CyberwaveTwinSettings, canonical_uuid

SIMULATION_MODE = "simulation"
SIMULATION_SOURCE_TYPE = "sim"  # cyberwave.constants.SOURCE_TYPE_SIM


def _record_field(record: Any, name: str) -> Any:
    """A field of an API record, whether it is a model object or a dict."""
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _record_uuid(record: Any, name: str) -> str | None:
    """A UUID field of an API record in canonical form, or None if it is not one."""
    return canonical_uuid(_record_field(record, name))


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

    def __init__(self, settings: CyberwaveSettings | CyberwaveTwinSettings) -> None:
        self._settings = settings
        self._client: Any = None
        # The verified API record of the existing twin (plain data).
        self._twin_record: Any = None
        # The SDK Twin object. Built only by the older MQTT-path readers below.
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

    def load_existing_twin(self) -> None:
        """Look up the EXISTING twin by id and verify it. Creates nothing.

        Needs ``CyberwaveTwinSettings``: the environment id, the twin id and the
        asset UUID, all canonical UUIDs. Two read-only calls, both GET:

        1. ``client.twins.get_raw(twin_id)``: the twin record.
        2. ``client.twins.list(environment_id=...)``: the environment's twins.

        The twin is accepted only if ALL of these hold:

        * the record's ``uuid`` is the configured twin id;
        * the record's ``environment_uuid`` is the configured environment id;
        * the record's ``asset_uuid`` is the configured asset UUID (UUID is
          compared with UUID; a registry id is never compared);
        * independently of the record, the environment's own twin list contains
          the twin id.

        Anything else (a missing setting, a failed lookup, an unreadable field,
        any mismatch) raises ``RobotConnectionError`` and leaves no twin loaded.
        There is no fallback: no asset-based lookup, no twin creation and no
        environment creation.
        """
        client = self._require_client()
        self._twin_record = None
        self._twin = None

        settings = self._settings
        if not isinstance(settings, CyberwaveTwinSettings):
            raise RobotConnectionError(
                "Twin loading needs CYBERWAVE_ENVIRONMENT_ID, CYBERWAVE_TWIN_ID and "
                "ROBOT_ASSET_IDENTIFIER (config.load_cyberwave_twin_settings). "
                "Nothing was looked up or created."
            )
        # Checked again here: settings can be built without the loader.
        wanted = {
            "CYBERWAVE_ENVIRONMENT_ID": canonical_uuid(settings.environment_id),
            "CYBERWAVE_TWIN_ID": canonical_uuid(settings.twin_id),
            "ROBOT_ASSET_IDENTIFIER": canonical_uuid(settings.asset_uuid),
        }
        malformed = [name for name, value in wanted.items() if value is None]
        if malformed:
            raise RobotConnectionError(
                "Not a canonical UUID: " + ", ".join(malformed) + ". Nothing was looked up."
            )
        environment_id = wanted["CYBERWAVE_ENVIRONMENT_ID"]
        twin_id = wanted["CYBERWAVE_TWIN_ID"]
        asset_uuid = wanted["ROBOT_ASSET_IDENTIFIER"]

        try:
            record = client.twins.get_raw(twin_id)
        except Exception as exc:
            raise RobotConnectionError(
                f"Could not look up the existing twin (nothing was created): {exc}"
            ) from exc
        if record is None:
            raise RobotConnectionError("The twin lookup returned nothing. Nothing was created.")

        if _record_uuid(record, "uuid") != twin_id:
            raise RobotConnectionError(
                "Refusing the twin: the record returned is not the configured CYBERWAVE_TWIN_ID."
            )
        if _record_uuid(record, "environment_uuid") != environment_id:
            raise RobotConnectionError(
                "Refusing the twin: it does not belong to the configured "
                "CYBERWAVE_ENVIRONMENT_ID (or its environment could not be read)."
            )
        if _record_uuid(record, "asset_uuid") != asset_uuid:
            raise RobotConnectionError(
                "Refusing the twin: its asset is not the configured ROBOT_ASSET_IDENTIFIER "
                "(or its asset UUID could not be read)."
            )

        # Independent of the record above: ask the environment for its twins.
        try:
            members = client.twins.list(environment_id=environment_id)
        except Exception as exc:
            raise RobotConnectionError(
                f"Could not list the twins of the configured environment: {exc}"
            ) from exc
        if not isinstance(members, (list, tuple)):
            raise RobotConnectionError(
                "Refusing the twin: the environment's twin list could not be read."
            )
        if twin_id not in {_record_uuid(member, "uuid") for member in members}:
            raise RobotConnectionError(
                "Refusing the twin: the configured environment does not list it."
            )

        self._twin_record = record

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
        self._twin_record = None
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
        """Identity of the verified twin record. No SDK twin object is needed."""
        record = self._require_twin_record()
        return {
            "uuid": str(_record_field(record, "uuid")),
            "name": str(_record_field(record, "name")),
            "environment_uuid": str(_record_field(record, "environment_uuid")),
            "asset_uuid": str(_record_field(record, "asset_uuid")),
        }

    def capabilities(self) -> dict[str, Any]:
        """Capability fields as the API reported them on the twin record."""
        reported = _record_field(self._require_twin_record(), "capabilities")
        return dict(reported) if isinstance(reported, dict) else {}

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

    def _require_twin_record(self) -> Any:
        if self._twin_record is None:
            raise RobotConnectionError("No twin loaded. Call load_existing_twin() first.")
        return self._twin_record

    def _require_twin(self) -> Any:
        """The SDK twin object, for the older MQTT-path readers in this class only.

        Built locally from the record that ``load_existing_twin`` verified
        (``cyberwave.twin.create_twin`` picks a class and instantiates it; it is
        what ``client.twins.get()`` does after its own lookup). It is never
        obtained through ``Cyberwave.twin()``, so nothing can be created.
        """
        record = self._require_twin_record()
        if self._twin is None:
            from cyberwave.twin import create_twin

            try:
                self._twin = create_twin(self._require_client(), record)
            except Exception as exc:
                raise RobotConnectionError(f"Could not open the existing twin: {exc}") from exc
        return self._twin
