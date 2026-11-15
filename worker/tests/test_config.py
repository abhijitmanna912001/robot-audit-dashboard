"""Unit tests for config. Offline: worker/.env is never read.

``dotenv_values`` is replaced with a mock and ``os.environ`` is swapped for a
controlled dict, so these tests cannot see or print real credentials. All
values below are fake.
"""

from __future__ import annotations

import dataclasses
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
        mock.patch.object(config, "dotenv_values", return_value={}) as fake_load_dotenv,
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
    """load_cyberwave_settings: asset discovery only. The identifier is free-form
    there and is only shown. Twin loading is stricter: see TwinSettingsTests."""

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


FAKE_ENVIRONMENT_ID = "11111111-1111-4111-8111-111111111111"
FAKE_TWIN_ID = "22222222-2222-4222-8222-222222222222"

# The minimum valid configuration for loading the existing twin.
TWIN_ENV = {
    "CYBERWAVE_API_KEY": FAKE_API_KEY,
    "CYBERWAVE_ENVIRONMENT_ID": FAKE_ENVIRONMENT_ID,
    "CYBERWAVE_TWIN_ID": FAKE_TWIN_ID,
    "ROBOT_ASSET_IDENTIFIER": FAKE_ASSET_UUID,
}
TWIN_ID_VARS = ("CYBERWAVE_ENVIRONMENT_ID", "CYBERWAVE_TWIN_ID", "ROBOT_ASSET_IDENTIFIER")


def load_twin_with_env(env: dict[str, str]) -> config.CyberwaveTwinSettings:
    """Run load_cyberwave_twin_settings against exactly ``env`` and no .env file."""
    with (
        mock.patch.object(config, "dotenv_values", return_value={}) as fake_load_dotenv,
        mock.patch.dict(os.environ, env, clear=True),
    ):
        try:
            return config.load_cyberwave_twin_settings()
        finally:
            # The loader was asked for the env file, but the mock read nothing.
            fake_load_dotenv.assert_called()


class TwinSettingsTests(unittest.TestCase):
    """Loading the EXISTING twin needs the environment id, the twin id and the asset UUID."""

    def test_all_three_ids_are_returned(self) -> None:
        settings = load_twin_with_env(TWIN_ENV)
        self.assertEqual(settings.environment_id, FAKE_ENVIRONMENT_ID)
        self.assertEqual(settings.twin_id, FAKE_TWIN_ID)
        self.assertEqual(settings.asset_uuid, FAKE_ASSET_UUID)
        self.assertEqual(settings.api_key, FAKE_API_KEY)
        self.assertIsNone(settings.workspace_id)

    def test_each_missing_id_is_rejected_by_name(self) -> None:
        for missing in TWIN_ID_VARS:
            with self.subTest(missing=missing):
                env = {name: value for name, value in TWIN_ENV.items() if name != missing}
                with self.assertRaises(config.ConfigError) as caught:
                    load_twin_with_env(env)
                message = str(caught.exception)
                self.assertIn(missing, message)
                for present in set(TWIN_ID_VARS) - {missing}:
                    self.assertNotIn(present, message)

    def test_each_blank_id_is_rejected(self) -> None:
        for blank_name in TWIN_ID_VARS:
            for blank in ("", "   "):
                with self.subTest(name=blank_name, value=repr(blank)):
                    with self.assertRaises(config.ConfigError) as caught:
                        load_twin_with_env({**TWIN_ENV, blank_name: blank})
                    self.assertIn(blank_name, str(caught.exception))

    def test_everything_missing_reports_every_required_name(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            load_twin_with_env({})
        message = str(caught.exception)
        for name in ("CYBERWAVE_API_KEY", *TWIN_ID_VARS):
            self.assertIn(name, message)
        self.assertNotIn("CYBERWAVE_WORKSPACE_ID", message)

    def test_an_environment_id_alone_is_not_enough(self) -> None:
        # No default twin and no fallback to the asset: the twin id is required.
        env = {name: value for name, value in TWIN_ENV.items() if name != "CYBERWAVE_TWIN_ID"}
        with self.assertRaises(config.ConfigError):
            load_twin_with_env(env)

    def test_each_id_must_be_a_canonical_uuid(self) -> None:
        malformed = (
            "not-a-uuid",
            "fake-env-id",
            "22222222222242228222222222222222",  # 32 hex digits, no hyphens
            "{22222222-2222-4222-8222-222222222222}",
            "urn:uuid:22222222-2222-4222-8222-222222222222",
            "22222222-2222-4222-8222-22222222222",  # one digit short
            "acme/twins/arm-station-1",  # a slug
        )
        for name in TWIN_ID_VARS:
            for value in malformed:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(config.ConfigError) as caught:
                        load_twin_with_env({**TWIN_ENV, name: value})
                    message = str(caught.exception)
                    self.assertIn(name, message)
                    self.assertIn("canonical UUID", message)
                    self.assertNotIn(value, message)  # the value is never echoed

    def test_a_registry_id_is_refused_as_the_asset_and_never_resolved(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            load_twin_with_env({**TWIN_ENV, "ROBOT_ASSET_IDENTIFIER": "the-robot-studio/so101"})
        message = str(caught.exception)
        self.assertIn("ROBOT_ASSET_IDENTIFIER", message)
        self.assertIn("not a registry id", message)
        self.assertNotIn("CYBERWAVE_TWIN_ID", message)

    def test_upper_case_ids_are_normalised_to_lower_case(self) -> None:
        upper = "ABCDEF12-3456-4789-8ABC-DEF123456789"
        settings = load_twin_with_env({**TWIN_ENV, "CYBERWAVE_TWIN_ID": upper})
        self.assertEqual(settings.twin_id, upper.lower())

    def test_workspace_id_stays_optional(self) -> None:
        settings = load_twin_with_env({**TWIN_ENV, "CYBERWAVE_WORKSPACE_ID": FAKE_WORKSPACE_ID})
        self.assertEqual(settings.workspace_id, FAKE_WORKSPACE_ID)

    def test_api_key_is_required_and_not_in_repr(self) -> None:
        env = {name: value for name, value in TWIN_ENV.items() if name != "CYBERWAVE_API_KEY"}
        with self.assertRaises(config.ConfigError) as caught:
            load_twin_with_env(env)
        self.assertIn("CYBERWAVE_API_KEY", str(caught.exception))
        self.assertNotIn(FAKE_API_KEY, repr(load_twin_with_env(TWIN_ENV)))

    def test_the_old_asset_variable_is_refused_not_used(self) -> None:
        env = {name: value for name, value in TWIN_ENV.items() if name != "ROBOT_ASSET_IDENTIFIER"}
        with self.assertRaises(config.ConfigError) as caught:
            load_twin_with_env({**env, "ROBOT_ASSET_SLUG": FAKE_ASSET_UUID})
        self.assertIn("rename", str(caught.exception).lower())

    def test_the_settings_have_no_default_ids(self) -> None:
        fields = config.CyberwaveTwinSettings.__dataclass_fields__
        for name in ("environment_id", "twin_id", "asset_uuid"):
            with self.subTest(field=name):
                self.assertIs(fields[name].default, dataclasses.MISSING)
                self.assertIs(fields[name].default_factory, dataclasses.MISSING)


class CanonicalUuidTests(unittest.TestCase):
    def test_only_the_hyphenated_form_is_accepted(self) -> None:
        self.assertEqual(config.canonical_uuid(FAKE_TWIN_ID), FAKE_TWIN_ID)
        self.assertEqual(config.canonical_uuid(FAKE_TWIN_ID.upper()), FAKE_TWIN_ID)
        for value in (
            None, "", 123, b"22222222-2222-4222-8222-222222222222",
            FAKE_TWIN_ID.replace("-", ""), "{" + FAKE_TWIN_ID + "}", " " + FAKE_TWIN_ID,
            "the-robot-studio/so101",
        ):
            with self.subTest(value=value):
                self.assertIsNone(config.canonical_uuid(value))


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


FAKE_SECRET = "fake-supabase-secret-only-in-the-file"


def load_config_with(file_values: dict[str, Any], env: dict[str, str]) -> config.WorkerConfig:
    with (
        mock.patch.object(config, "dotenv_values", return_value=file_values),
        mock.patch.dict(os.environ, env, clear=True),
    ):
        return config.load_config()


class ConfigObjectTests(unittest.TestCase):
    """The file goes into a config object, never into os.environ."""

    def test_loading_never_writes_to_os_environ(self) -> None:
        file_values = {"SUPABASE_SECRET_KEY": FAKE_SECRET, "SUPABASE_URL": "https://example.invalid"}
        with (
            mock.patch.object(config, "dotenv_values", return_value=file_values),
            mock.patch.dict(os.environ, {"PATH": "/bin"}, clear=True),
        ):
            before = dict(os.environ)
            cfg = config.load_config()
            settings = config.load_supabase_settings()
            config.redact("x")
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("SUPABASE_SECRET_KEY", os.environ)
        self.assertEqual(cfg.get("SUPABASE_SECRET_KEY"), FAKE_SECRET)
        self.assertEqual(settings.secret_key, FAKE_SECRET)

    def test_real_environment_wins_over_the_file(self) -> None:
        cfg = load_config_with({"A": "from-file", "B": "file-only"}, {"A": "from-env"})
        self.assertEqual(cfg.get("A"), "from-env")
        self.assertEqual(cfg.get("B"), "file-only")

    def test_an_empty_real_variable_beats_the_file_and_counts_as_missing(self) -> None:
        cfg = load_config_with({"A": "from-file"}, {"A": ""})
        self.assertIsNone(cfg.get("A"))

    def test_file_key_without_a_value_is_absent_and_values_are_stripped(self) -> None:
        cfg = load_config_with({"A": None, "B": "  padded  "}, {})
        self.assertIsNone(cfg.get("A"))
        self.assertEqual(cfg.get("B"), "padded")

    def test_redact_masks_a_secret_that_only_exists_in_the_file(self) -> None:
        with (
            mock.patch.object(config, "dotenv_values", return_value={"SUPABASE_SECRET_KEY": FAKE_SECRET}),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(config.redact(f"boom {FAKE_SECRET}"), "boom ***")

    def test_repr_shows_no_values(self) -> None:
        self.assertNotIn(FAKE_SECRET, repr(load_config_with({"K": FAKE_SECRET}, {})))

    def test_the_loader_refuses_inside_a_multiprocessing_child(self) -> None:
        with mock.patch.object(config.multiprocessing, "parent_process", return_value=object()):
            with self.assertRaises(config.ConfigError):
                load_config_with({}, {})

    def test_importing_config_reads_nothing(self) -> None:
        """N1: a spawn child re-imports the main module. Import must not read .env."""
        import subprocess
        import sys

        code = (
            "import dotenv, os\n"
            "def boom(*a, **k): raise SystemExit('dotenv read at import')\n"
            "dotenv.dotenv_values = boom\n"
            "dotenv.load_dotenv = boom\n"
            "import config, worker_lock, simulation_supervisor\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(config.WORKER_DIR)!r})\n" + code],
            capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
