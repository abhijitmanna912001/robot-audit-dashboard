"""Unit tests for SimulatedRobot.connect() and load_existing_twin(). Offline.

The SDK's ``Cyberwave`` class is replaced with a fake that only records what
it is given. No real client is built, so there is no network call, no twin, no
environment, no simulation and no robot. The fake has no ``twin`` method (the
SDK's get-or-create), no ``affect`` method and no way to create anything, so
any call to one of them would fail the test. Every id and key below is
synthetic.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from config import CyberwaveSettings, CyberwaveTwinSettings
from cyberwave_client import RobotConnectionError, SimulatedRobot

WORKER_DIR = Path(__file__).resolve().parent.parent

FAKE_API_KEY = "fake-api-key-for-tests"
FAKE_WORKSPACE_ID = "fake-workspace-id-for-tests"
FAKE_ASSET_UUID = "00000000-0000-4000-8000-000000000000"
FAKE_ENVIRONMENT_ID = "11111111-1111-4111-8111-111111111111"
FAKE_TWIN_ID = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "99999999-9999-4999-8999-999999999999"


def twin_record(**overrides: Any) -> SimpleNamespace:
    """A twin record as the API would return it for the configured ids."""
    fields: dict[str, Any] = {
        "uuid": FAKE_TWIN_ID,
        "name": "fake so101",
        "environment_uuid": FAKE_ENVIRONMENT_ID,
        "asset_uuid": FAKE_ASSET_UUID,
        "capabilities": {"has_joints": True},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeTwins:
    """Stands in for ``client.twins``. Read methods only; it cannot create."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.record: Any = twin_record()
        self.members: Any = [twin_record(uuid=OTHER_UUID), twin_record()]
        self.get_error: BaseException | None = None
        self.list_error: BaseException | None = None

    def get_raw(self, twin_id: str) -> Any:
        self.calls.append(("get_raw", twin_id))
        if self.get_error is not None:
            raise self.get_error
        return self.record

    def list(self, environment_id: str | None = None) -> Any:
        self.calls.append(("list", environment_id))
        if self.list_error is not None:
            raise self.list_error
        return self.members


class FakeCyberwave:
    """Stands in for cyberwave.Cyberwave. Records arguments, does nothing else."""

    last_kwargs: dict[str, Any] = {}
    reported_runtime_mode = "simulation"
    reported_source_type = "sim"

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs
        self.config = SimpleNamespace(
            runtime_mode=type(self).reported_runtime_mode,
            source_type=type(self).reported_source_type,
        )
        self.twins = FakeTwins()
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


def twin_settings(**overrides: Any) -> CyberwaveTwinSettings:
    fields: dict[str, Any] = {
        "api_key": FAKE_API_KEY,
        "workspace_id": None,
        "environment_id": FAKE_ENVIRONMENT_ID,
        "twin_id": FAKE_TWIN_ID,
        "asset_uuid": FAKE_ASSET_UUID,
    }
    fields.update(overrides)
    return CyberwaveTwinSettings(**fields)


def connect_with(
    workspace_id: str | None = None,
    fake: type = FakeCyberwave,
    settings: Any = None,
) -> SimulatedRobot:
    if settings is None:
        settings = twin_settings(workspace_id=workspace_id)
    robot = SimulatedRobot(settings)
    with mock.patch("cyberwave.Cyberwave", fake):
        robot.connect()
    return robot


class ConnectArgumentTests(unittest.TestCase):
    def test_missing_workspace_id_is_passed_as_none(self) -> None:
        connect_with(None)
        self.assertIn("workspace_id", FakeCyberwave.last_kwargs)
        self.assertIsNone(FakeCyberwave.last_kwargs["workspace_id"])

    def test_provided_workspace_id_is_passed_through(self) -> None:
        connect_with(FAKE_WORKSPACE_ID)
        self.assertEqual(FakeCyberwave.last_kwargs["workspace_id"], FAKE_WORKSPACE_ID)

    def test_simulation_mode_is_always_requested(self) -> None:
        for workspace_id in (None, FAKE_WORKSPACE_ID):
            with self.subTest(workspace_id=workspace_id):
                connect_with(workspace_id)
                self.assertEqual(FakeCyberwave.last_kwargs["mode"], "simulation")
                self.assertEqual(FakeCyberwave.last_kwargs["api_key"], FAKE_API_KEY)

    def test_the_configured_environment_is_passed_to_the_client(self) -> None:
        connect_with(None)
        self.assertEqual(FakeCyberwave.last_kwargs["environment_id"], FAKE_ENVIRONMENT_ID)

    def test_connect_refuses_a_client_that_is_not_in_simulation_mode(self) -> None:
        class LiveFake(FakeCyberwave):
            reported_runtime_mode = "live"
            reported_source_type = "edge"

        with self.assertRaises(RobotConnectionError):
            connect_with(None, fake=LiveFake)

    def test_connect_itself_looks_nothing_up(self) -> None:
        robot = connect_with(None)
        self.assertEqual(robot.sdk_client.twins.calls, [])


class LoadExistingTwinTests(unittest.TestCase):
    """The twin is looked up by id, verified, and never created."""

    def assert_refused(self, robot: SimulatedRobot, *fragments: str) -> str:
        with self.assertRaises(RobotConnectionError) as caught:
            robot.load_existing_twin()
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        # A refusal leaves no twin loaded.
        with self.assertRaises(RobotConnectionError):
            robot.twin_summary()
        return message

    def test_the_existing_twin_is_looked_up_and_verified(self) -> None:
        robot = connect_with()
        robot.load_existing_twin()
        self.assertEqual(
            robot.sdk_client.twins.calls,
            [("get_raw", FAKE_TWIN_ID), ("list", FAKE_ENVIRONMENT_ID)],
        )
        self.assertEqual(
            robot.twin_summary(),
            {
                "uuid": FAKE_TWIN_ID,
                "name": "fake so101",
                "environment_uuid": FAKE_ENVIRONMENT_ID,
                "asset_uuid": FAKE_ASSET_UUID,
            },
        )
        self.assertEqual(robot.capabilities(), {"has_joints": True})

    def test_only_lookup_methods_exist_on_the_fake_so_nothing_can_be_created(self) -> None:
        robot = connect_with()
        robot.load_existing_twin()
        client = robot.sdk_client
        for name in ("twin", "affect", "get_or_create_quickstart_environment"):
            self.assertFalse(hasattr(client, name), name)
        for name in ("create", "update", "delete"):
            self.assertFalse(hasattr(client.twins, name), name)

    def test_api_records_given_as_dicts_and_in_upper_case_are_accepted(self) -> None:
        robot = connect_with()
        twins = robot.sdk_client.twins
        twins.record = {
            "uuid": FAKE_TWIN_ID.upper(),
            "name": "fake so101",
            "environment_uuid": FAKE_ENVIRONMENT_ID.upper(),
            "asset_uuid": FAKE_ASSET_UUID.upper(),
        }
        twins.members = [{"uuid": FAKE_TWIN_ID.upper()}]
        robot.load_existing_twin()
        self.assertEqual(robot.capabilities(), {})

    # -- settings ------------------------------------------------------------

    def test_discovery_settings_cannot_load_a_twin(self) -> None:
        # Asset-only settings: there is no fallback to an asset-based lookup.
        settings = CyberwaveSettings(
            api_key=FAKE_API_KEY, workspace_id=None, robot_asset_identifier=FAKE_ASSET_UUID
        )
        robot = connect_with(settings=settings)
        message = self.assert_refused(robot, "CYBERWAVE_ENVIRONMENT_ID", "CYBERWAVE_TWIN_ID")
        self.assertIn("Nothing was looked up or created", message)
        self.assertEqual(robot.sdk_client.twins.calls, [])

    def test_missing_or_malformed_ids_are_refused_before_any_sdk_call(self) -> None:
        cases = {
            "CYBERWAVE_ENVIRONMENT_ID": "environment_id",
            "CYBERWAVE_TWIN_ID": "twin_id",
            "ROBOT_ASSET_IDENTIFIER": "asset_uuid",
        }
        for variable, field in cases.items():
            for value in (None, "", "not-a-uuid", "the-robot-studio/so101"):
                with self.subTest(variable=variable, value=value):
                    robot = connect_with(settings=twin_settings(**{field: value}))
                    self.assert_refused(robot, variable, "Nothing was looked up")
                    self.assertEqual(robot.sdk_client.twins.calls, [])

    def test_not_connected_is_refused(self) -> None:
        robot = SimulatedRobot(twin_settings())
        with self.assertRaises(RobotConnectionError):
            robot.load_existing_twin()

    # -- the lookup ----------------------------------------------------------

    def test_a_failed_lookup_is_refused_and_nothing_else_is_called(self) -> None:
        robot = connect_with()
        robot.sdk_client.twins.get_error = RuntimeError("HTTP 404")
        self.assert_refused(robot, "nothing was created")
        self.assertEqual(robot.sdk_client.twins.calls, [("get_raw", FAKE_TWIN_ID)])

    def test_an_empty_lookup_result_is_refused(self) -> None:
        robot = connect_with()
        robot.sdk_client.twins.record = None
        self.assert_refused(robot, "returned nothing")
        self.assertEqual(robot.sdk_client.twins.calls, [("get_raw", FAKE_TWIN_ID)])

    def test_a_record_for_another_twin_is_refused(self) -> None:
        robot = connect_with()
        robot.sdk_client.twins.record = twin_record(uuid=OTHER_UUID)
        self.assert_refused(robot, "CYBERWAVE_TWIN_ID")
        self.assertEqual(robot.sdk_client.twins.calls, [("get_raw", FAKE_TWIN_ID)])

    # -- environment membership ----------------------------------------------

    def test_a_twin_in_another_environment_is_refused(self) -> None:
        robot = connect_with()
        robot.sdk_client.twins.record = twin_record(environment_uuid=OTHER_UUID)
        self.assert_refused(robot, "CYBERWAVE_ENVIRONMENT_ID")
        # Refused on the record alone: the environment is not even listed.
        self.assertEqual(robot.sdk_client.twins.calls, [("get_raw", FAKE_TWIN_ID)])

    def test_an_unreadable_environment_on_the_record_is_refused(self) -> None:
        for value in (None, "", "fake-env", 5):
            with self.subTest(value=value):
                robot = connect_with()
                robot.sdk_client.twins.record = twin_record(environment_uuid=value)
                self.assert_refused(robot, "CYBERWAVE_ENVIRONMENT_ID")

    def test_the_environment_must_list_the_twin_itself(self) -> None:
        # The record claims the right environment, but the environment's own
        # twin list does not contain the twin: the second, independent check.
        for members in ([], [twin_record(uuid=OTHER_UUID)], [SimpleNamespace(uuid=None)]):
            with self.subTest(members=len(members)):
                robot = connect_with()
                robot.sdk_client.twins.members = members
                self.assert_refused(robot, "does not list it")
                self.assertEqual(
                    robot.sdk_client.twins.calls,
                    [("get_raw", FAKE_TWIN_ID), ("list", FAKE_ENVIRONMENT_ID)],
                )

    def test_a_failed_or_unreadable_environment_listing_is_refused(self) -> None:
        robot = connect_with()
        robot.sdk_client.twins.list_error = RuntimeError("HTTP 403")
        self.assert_refused(robot, "Could not list the twins")

        for unreadable in (None, {"twins": []}, "not a list"):
            with self.subTest(members=unreadable):
                robot = connect_with()
                robot.sdk_client.twins.members = unreadable
                self.assert_refused(robot, "could not be read")

    # -- the asset -----------------------------------------------------------

    def test_a_twin_of_another_asset_is_refused(self) -> None:
        robot = connect_with()
        robot.sdk_client.twins.record = twin_record(asset_uuid=OTHER_UUID)
        self.assert_refused(robot, "ROBOT_ASSET_IDENTIFIER")
        self.assertEqual(robot.sdk_client.twins.calls, [("get_raw", FAKE_TWIN_ID)])

    def test_an_asset_field_that_is_not_a_uuid_is_never_compared(self) -> None:
        # A registry id on the record is a different kind of identifier.
        for value in ("the-robot-studio/so101", None, ""):
            with self.subTest(value=value):
                robot = connect_with()
                robot.sdk_client.twins.record = twin_record(asset_uuid=value)
                self.assert_refused(robot, "ROBOT_ASSET_IDENTIFIER")

    # -- state ---------------------------------------------------------------

    def test_a_refusal_after_a_successful_load_leaves_no_twin(self) -> None:
        robot = connect_with()
        robot.load_existing_twin()
        robot.sdk_client.twins.record = twin_record(environment_uuid=OTHER_UUID)
        self.assert_refused(robot, "CYBERWAVE_ENVIRONMENT_ID")

    def test_nothing_is_readable_before_the_twin_is_loaded(self) -> None:
        robot = connect_with()
        with mock.patch("cyberwave.twin.create_twin") as create_twin:
            for read in (robot.twin_summary, robot.capabilities, robot.joint_descriptors):
                with self.subTest(read=read.__name__):
                    with self.assertRaises(RobotConnectionError):
                        read()
        create_twin.assert_not_called()

    def test_the_sdk_twin_object_is_built_from_the_verified_record_only(self) -> None:
        robot = connect_with()
        robot.load_existing_twin()
        sdk_twin = SimpleNamespace(get_schema=lambda _path: [{"name": "_1", "type": "revolute"}])
        with mock.patch("cyberwave.twin.create_twin", return_value=sdk_twin) as create_twin:
            self.assertEqual(robot.joint_descriptors(), [{"name": "_1", "type": "revolute"}])
            robot.joint_descriptors()
        # Built once, locally, from the record the lookup verified.
        create_twin.assert_called_once_with(robot.sdk_client, robot.sdk_client.twins.record)


class NoCreatePathTests(unittest.TestCase):
    """Source checks: the get-or-create path is gone and cannot come back unnoticed."""

    # Modules that talk to the Cyberwave SDK.
    CYBERWAVE_MODULES = (
        "cyberwave_client",
        "test_cyberwave_connection",
        "read_joint_telemetry",
        "discover_cyberwave_assets",
    )

    @staticmethod
    def called_attributes(module: str) -> set[str]:
        tree = ast.parse((WORKER_DIR / f"{module}.py").read_text(encoding="utf-8"))
        return {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    def test_no_worker_module_calls_the_get_or_create_twin_method(self) -> None:
        modules = sorted(path.stem for path in WORKER_DIR.glob("*.py"))
        self.assertIn("cyberwave_client", modules)
        forbidden = {"twin", "get_or_create_quickstart_environment"}
        for module in modules:
            with self.subTest(module=module):
                self.assertEqual(self.called_attributes(module) & forbidden, set())

    def test_no_cyberwave_module_calls_a_create_method(self) -> None:
        for module in self.CYBERWAVE_MODULES:
            with self.subTest(module=module):
                self.assertNotIn("create", self.called_attributes(module))

    def test_the_wrapper_has_no_get_or_create_loader(self) -> None:
        self.assertFalse(hasattr(SimulatedRobot, "load_twin"))
        self.assertTrue(callable(SimulatedRobot.load_existing_twin))

    def test_the_wrapper_uses_only_the_two_lookup_calls_for_the_twin(self) -> None:
        tree = ast.parse((WORKER_DIR / "cyberwave_client.py").read_text(encoding="utf-8"))
        loader = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "load_existing_twin"
        )
        sdk_calls = {
            node.func.attr
            for node in ast.walk(loader)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "twins"
        }
        self.assertEqual(sdk_calls, {"get_raw", "list"})


if __name__ == "__main__":
    unittest.main()
