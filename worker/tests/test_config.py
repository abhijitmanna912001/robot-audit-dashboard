"""Unit tests for config. Offline: worker/.env is never read.

``load_dotenv`` is replaced with a mock and ``os.environ`` is swapped for a
controlled dict, so these tests cannot see or print real credentials. All
values below are fake.
"""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

import config

FAKE_API_KEY = "fake-api-key-for-tests"
FAKE_WORKSPACE_ID = "fake-workspace-id-for-tests"
FAKE_ASSET_UUID = "00000000-0000-4000-8000-000000000000"
FAKE_LEGACY_VALUE = "fake-vendor/fake-model"

# The minimum valid Cyberwave configuration.
BASE_ENV = {"CYBERWAVE_API_KEY": FAKE_API_KEY, "ROBOT_ASSET_IDENTIFIER": FAKE_ASSET_UUID}


def load_with_env(env: dict[str, str], **kwargs: Any) -> config.CyberwaveSettings:
    """Run load_cyberwave_settings against exactly ``env`` and no .env file."""
    with (
        mock.patch.object(config, "load_dotenv") as fake_load_dotenv,
        mock.patch.dict(os.environ, env, clear=True),
    ):
        try:
            return config.load_cyberwave_settings(**kwargs)
        finally:
            # The loader was asked for the env file, but the mock read nothing.
            fake_load_dotenv.assert_called()


class CyberwaveWorkspaceIdTests(unittest.TestCase):
    def test_missing_workspace_id_is_accepted(self) -> None:
        settings = load_with_env(BASE_ENV)
        self.assertIsNone(settings.workspace_id)
        self.assertEqual(settings.api_key, FAKE_API_KEY)

    def test_empty_or_blank_workspace_id_is_treated_as_missing(self) -> None:
        for blank in ("", "   "):
            with self.subTest(value=repr(blank)):
                settings = load_with_env({**BASE_ENV, "CYBERWAVE_WORKSPACE_ID": blank})
                self.assertIsNone(settings.workspace_id)

    def test_provided_workspace_id_is_preserved(self) -> None:
        settings = load_with_env({**BASE_ENV, "CYBERWAVE_WORKSPACE_ID": FAKE_WORKSPACE_ID})
        self.assertEqual(settings.workspace_id, FAKE_WORKSPACE_ID)


class CyberwaveApiKeyTests(unittest.TestCase):
    def test_missing_api_key_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            load_with_env(
                {"ROBOT_ASSET_IDENTIFIER": FAKE_ASSET_UUID, "CYBERWAVE_WORKSPACE_ID": FAKE_WORKSPACE_ID}
            )
        message = str(caught.exception)
        self.assertIn("CYBERWAVE_API_KEY", message)
        self.assertNotIn("CYBERWAVE_WORKSPACE_ID", message)
        self.assertNotIn("ROBOT_ASSET_IDENTIFIER", message)

    def test_blank_api_key_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            load_with_env({**BASE_ENV, "CYBERWAVE_API_KEY": "   "})

    def test_api_key_is_not_in_repr(self) -> None:
        self.assertNotIn(FAKE_API_KEY, repr(load_with_env(BASE_ENV)))


class RobotAssetIdentifierTests(unittest.TestCase):
    def test_identifier_is_preserved_as_given(self) -> None:
        for value in (FAKE_ASSET_UUID, "fake-vendor/fake-model"):
            with self.subTest(value=value):
                settings = load_with_env({**BASE_ENV, "ROBOT_ASSET_IDENTIFIER": value})
                self.assertEqual(settings.robot_asset_identifier, value)

    def test_missing_identifier_is_rejected_with_no_default(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            load_with_env({"CYBERWAVE_API_KEY": FAKE_API_KEY})
        self.assertIn("ROBOT_ASSET_IDENTIFIER", str(caught.exception))
        self.assertNotIn("CYBERWAVE_API_KEY", str(caught.exception))

    def test_blank_identifier_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            load_with_env({**BASE_ENV, "ROBOT_ASSET_IDENTIFIER": "  "})

    def test_everything_missing_reports_both_required_names(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            load_with_env({})
        message = str(caught.exception)
        self.assertIn("CYBERWAVE_API_KEY", message)
        self.assertIn("ROBOT_ASSET_IDENTIFIER", message)
        self.assertNotIn("CYBERWAVE_WORKSPACE_ID", message)

    def test_identifier_is_optional_for_discovery(self) -> None:
        settings = load_with_env(
            {"CYBERWAVE_API_KEY": FAKE_API_KEY}, require_asset_identifier=False
        )
        self.assertIsNone(settings.robot_asset_identifier)

    def test_environment_id_stays_optional_and_is_not_invented(self) -> None:
        self.assertIsNone(load_with_env(BASE_ENV).environment_id)
        settings = load_with_env({**BASE_ENV, "CYBERWAVE_ENVIRONMENT_ID": "fake-env-id"})
        self.assertEqual(settings.environment_id, "fake-env-id")


class LegacyRobotAssetSlugTests(unittest.TestCase):
    def test_old_name_alone_is_an_error_asking_for_the_rename(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            load_with_env({"CYBERWAVE_API_KEY": FAKE_API_KEY, "ROBOT_ASSET_SLUG": FAKE_LEGACY_VALUE})
        message = str(caught.exception)
        self.assertIn("ROBOT_ASSET_SLUG", message)
        self.assertIn("ROBOT_ASSET_IDENTIFIER", message)
        self.assertIn("rename", message.lower())

    def test_old_name_is_never_used_as_a_fallback(self) -> None:
        # Even where the identifier is optional, the old variable is refused
        # rather than read.
        with self.assertRaises(config.ConfigError):
            load_with_env(
                {"CYBERWAVE_API_KEY": FAKE_API_KEY, "ROBOT_ASSET_SLUG": FAKE_LEGACY_VALUE},
                require_asset_identifier=False,
            )

    def test_new_name_wins_when_both_are_set(self) -> None:
        settings = load_with_env({**BASE_ENV, "ROBOT_ASSET_SLUG": FAKE_LEGACY_VALUE})
        self.assertEqual(settings.robot_asset_identifier, FAKE_ASSET_UUID)

    def test_blank_old_name_is_ignored(self) -> None:
        settings = load_with_env({**BASE_ENV, "ROBOT_ASSET_SLUG": "   "})
        self.assertEqual(settings.robot_asset_identifier, FAKE_ASSET_UUID)


if __name__ == "__main__":
    unittest.main()
