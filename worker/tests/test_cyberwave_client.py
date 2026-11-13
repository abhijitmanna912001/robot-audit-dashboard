"""Unit tests for SimulatedRobot.connect() and load_twin(). Offline.

The SDK's ``Cyberwave`` class is replaced with a fake that only records what
it is given. No real client is built, so there is no network call, no twin, no
environment, no simulation and no robot. The fake has no ``affect`` method, so
any call to it would fail the test.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from config import CyberwaveSettings
from cyberwave_client import RobotConnectionError, SimulatedRobot

FAKE_API_KEY = "fake-api-key-for-tests"
FAKE_WORKSPACE_ID = "fake-workspace-id-for-tests"
FAKE_ASSET_UUID = "00000000-0000-4000-8000-000000000000"


class FakeCyberwave:
    """Stands in for cyberwave.Cyberwave. Records arguments, does nothing else."""

    last_kwargs: dict[str, Any] = {}
    twin_requests: list[str] = []
    reported_runtime_mode = "simulation"
    reported_source_type = "sim"

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs
        self.config = SimpleNamespace(
            runtime_mode=type(self).reported_runtime_mode,
            source_type=type(self).reported_source_type,
        )
        self.disconnected = False

    def twin(self, asset_key: str) -> Any:
        """Fake of the SDK method: records the identifier, creates nothing."""
        FakeCyberwave.twin_requests.append(asset_key)
        return SimpleNamespace(uuid="fake-twin", name="fake", environment_id="fake-env")

    def disconnect(self) -> None:
        self.disconnected = True


def connect_with(
    workspace_id: str | None = None,
    asset_identifier: str | None = FAKE_ASSET_UUID,
    fake: type = FakeCyberwave,
) -> SimulatedRobot:
    settings = CyberwaveSettings(
        api_key=FAKE_API_KEY,
        workspace_id=workspace_id,
        robot_asset_identifier=asset_identifier,
    )
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

    def test_connect_refuses_a_client_that_is_not_in_simulation_mode(self) -> None:
        class LiveFake(FakeCyberwave):
            reported_runtime_mode = "live"
            reported_source_type = "edge"

        with self.assertRaises(RobotConnectionError):
            connect_with(None, fake=LiveFake)


class LoadTwinTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCyberwave.twin_requests = []

    def test_configured_identifier_is_passed_to_the_sdk_unchanged(self) -> None:
        robot = connect_with(asset_identifier=FAKE_ASSET_UUID)
        robot.load_twin()
        self.assertEqual(FakeCyberwave.twin_requests, [FAKE_ASSET_UUID])

    def test_missing_identifier_is_refused_before_any_sdk_call(self) -> None:
        robot = connect_with(asset_identifier=None)
        with self.assertRaises(RobotConnectionError) as caught:
            robot.load_twin()
        self.assertIn("ROBOT_ASSET_IDENTIFIER", str(caught.exception))
        self.assertEqual(FakeCyberwave.twin_requests, [])


if __name__ == "__main__":
    unittest.main()
