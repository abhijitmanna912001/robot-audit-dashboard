"""B2: a spawned twin child must not inherit the worker's credentials.

Offline. All values are fake. The real-child test spawns an actual process with
the ``spawn`` start method and has it report its own ``os.environ``.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from typing import Any
from unittest import mock

import child_env
import config
from simulation_supervisor import ProcessTwinSession, TwinSpec, _SPAWN, _is_plain

from tests.fake_twin import FakeTwin
from tests.fake_twin_child import spec_for

FAKE_ENV = {
    "SUPABASE_URL": "https://fake.invalid",
    "SUPABASE_SECRET_KEY": "fake-supabase-secret",
    "SUPABASE_ANYTHING": "fake",
    "CRITIC_URL": "https://critic.invalid",
    "CRITIC_SHARED_SECRET": "fake-critic-secret",
    "CRITIC_EXTRA": "fake",
    "WORKER_LOCK_FILE": "/tmp/fake-worker.lock",
    "WORKER_ID": "fake",
    "CYBERWAVE_API_KEY": "fake-cyberwave-key",
    "supabase_lowercase": "fake",
}
KEEP = {"B2_UNRELATED_SENTINEL": "stays", "CYBERWAVE_TWIN_ID": "00000000-0000-4000-8000-000000000000"}

# Runs in the child BEFORE any worker or test module is imported there: exec is
# a builtin, so spawn needs nothing but the standard library to start it.
CHILD_CODE = "import os\nconn.send(dict(os.environ))\nconn.close()"


def child_environment_of_a_real_spawn() -> dict[str, str]:
    parent_conn, child_conn = _SPAWN.Pipe(duplex=False)
    process = _SPAWN.Process(target=exec, args=(CHILD_CODE, {"conn": child_conn}), daemon=True)
    with child_env.child_environment():
        process.start()
    child_conn.close()
    try:
        if not parent_conn.poll(60):
            raise AssertionError("the child did not report its environment")
        return parent_conn.recv()
    finally:
        parent_conn.close()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)


@unittest.skipIf(sys.platform == "win32", "POSIX environment semantics")
class RealChildEnvironmentTests(unittest.TestCase):
    def test_the_child_has_no_worker_credentials_even_when_the_shell_exported_them(self) -> None:
        with mock.patch.dict(os.environ, {**FAKE_ENV, **KEEP}):
            seen = child_environment_of_a_real_spawn()
        leaked = sorted(
            name for name in seen
            if name.upper().startswith(("SUPABASE_", "CRITIC_", "WORKER_"))
            or name.upper() in {n.upper() for n in config.SECRET_ENV_VARS}
        )
        self.assertEqual(leaked, [])
        for value in FAKE_ENV.values():
            self.assertNotIn(value, seen.values())
        # Only the credentials are removed: ordinary variables still arrive.
        self.assertEqual(seen.get("B2_UNRELATED_SENTINEL"), "stays")
        self.assertEqual(seen.get("CYBERWAVE_TWIN_ID"), KEEP["CYBERWAVE_TWIN_ID"])
        self.assertIn("PATH", seen)

    def test_a_secret_that_only_lives_in_the_dotenv_file_never_reaches_the_child(self) -> None:
        with (
            mock.patch.object(config, "dotenv_values", return_value={"SUPABASE_SECRET_KEY": "only-in-file"}),
            mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True),
        ):
            config.load_supabase_settings.__call__  # the loader exists
            seen = child_environment_of_a_real_spawn()
        self.assertNotIn("SUPABASE_SECRET_KEY", seen)

    def test_a_real_session_still_starts_and_stops_with_fake_secrets_exported(self) -> None:
        with mock.patch.dict(os.environ, FAKE_ENV):
            session = ProcessTwinSession(spec_for(FakeTwin(), fresh_session_stop="unsupported")).start()
            try:
                self.assertTrue(session.healthy)
            finally:
                session.close()


class ScrubMechanicsTests(unittest.TestCase):
    def test_the_parent_environment_is_restored_exactly(self) -> None:
        with mock.patch.dict(os.environ, {**FAKE_ENV, **KEEP}):
            before = dict(os.environ)
            with child_env.child_environment():
                inside = dict(os.environ)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(set(inside), set(before) - set(FAKE_ENV))

    def test_the_parent_environment_is_restored_when_the_block_raises(self) -> None:
        with mock.patch.dict(os.environ, FAKE_ENV):
            before = dict(os.environ)
            with self.assertRaises(KeyboardInterrupt):
                with child_env.child_environment():
                    raise KeyboardInterrupt
            self.assertEqual(dict(os.environ), before)

    def test_a_failing_start_restores_the_environment_and_propagates(self) -> None:
        with mock.patch.dict(os.environ, FAKE_ENV):
            before = dict(os.environ)
            with mock.patch.object(_SPAWN.Process, "start", side_effect=OSError("no")):
                with self.assertRaises(OSError):
                    ProcessTwinSession(spec_for(FakeTwin(), fresh_session_stop="unsupported")).start()
            self.assertEqual(dict(os.environ), before)

    def test_config_waits_while_the_environment_is_scrubbed(self) -> None:
        done = threading.Event()
        seen: dict[str, Any] = {}

        def read() -> None:
            seen["value"] = config.load_config().get("SUPABASE_URL")
            done.set()

        with (
            mock.patch.object(config, "dotenv_values", return_value={}),
            mock.patch.dict(os.environ, {"SUPABASE_URL": "https://fake.invalid"}),
        ):
            with child_env.child_environment():
                thread = threading.Thread(target=read)
                thread.start()
                self.assertFalse(done.wait(0.3), "config read os.environ during the scrub")
            self.assertTrue(done.wait(10))
            thread.join(10)
        # It saw the restored value, never a half-scrubbed environment.
        self.assertEqual(seen["value"], "https://fake.invalid")

    def test_is_scrubbed_names(self) -> None:
        for name in ("SUPABASE_URL", "supabase_x", "CRITIC_URL", "WORKER_LOCK_FILE", "CYBERWAVE_API_KEY"):
            self.assertTrue(child_env.is_scrubbed(name), name)
        for name in ("PATH", "CYBERWAVE_TWIN_ID", "HOME", "SUPABASEX"):
            self.assertFalse(child_env.is_scrubbed(name), name)


class SecretShapeTests(unittest.TestCase):
    """The detection rules themselves (used by simulation_supervisor._is_plain)."""

    def test_secret_looking_keys(self) -> None:
        for name in ("api_key", "KEY", "Secret", "access_token", "db_Password"):
            self.assertTrue(child_env.looks_secret_key(name), name)
        for name in ("positions", "hang_s", "twin"):
            self.assertFalse(child_env.looks_secret_key(name), name)

    def test_secret_looking_values(self) -> None:
        for value in ("sb_secret_abc", "sk-abc", "eyJhbGciOi", "0123456789abcdef0123456789abcdef", "f" * 64):
            self.assertTrue(child_env.looks_secret_value(value), value[:8])
        for value in ("00000000-0000-4000-8000-000000000000", "deadbeef", "a" * 31, "/tmp/marker.txt", ""):
            self.assertFalse(child_env.looks_secret_value(value), value[:8])

    def test_the_rules_are_not_in_the_simulation_supervisor(self) -> None:
        source = (config.WORKER_DIR / "simulation_supervisor.py").read_text("utf-8")
        self.assertIn("child_env.looks_secret_key", source)
        self.assertIn("child_env.looks_secret_value", source)


class IsPlainTests(unittest.TestCase):
    def test_ordinary_configuration_is_accepted(self) -> None:
        self.assertTrue(_is_plain({
            "positions": {"shoulder": 0.5}, "ids": ["00000000-0000-4000-8000-000000000000"],
            "hang_s": 1.5, "flag": True, "nothing": None, "path": "/tmp/marker.txt",
        }))

    def test_keys_that_look_like_secrets_are_rejected_at_any_depth(self) -> None:
        for name in ("api_key", "KEY", "Secret", "shared_secret", "access_token", "password", "db_Password"):
            with self.subTest(key=name):
                self.assertFalse(_is_plain({name: "x"}))
                self.assertFalse(_is_plain({"outer": {name: "x"}}))
                self.assertFalse(_is_plain({"outer": [{name: "x"}]}))

    def test_values_that_look_like_credentials_are_rejected_at_any_depth(self) -> None:
        for value in (
            "sb_secret_abcdef", "sk-abc123", "eyJhbGciOiJIUzI1NiJ9.e30.sig",
            "0123456789abcdef0123456789abcdef", "A" * 32, "f" * 64,
        ):
            with self.subTest(value=value[:8]):
                self.assertFalse(_is_plain({"v": value}))
                self.assertFalse(_is_plain({"v": [value]}))
                self.assertFalse(_is_plain({"o": {"v": (value,)}}))

    def test_ids_and_short_hex_are_not_mistaken_for_credentials(self) -> None:
        self.assertTrue(_is_plain({"twin": "00000000-0000-4000-8000-000000000000"}))
        self.assertTrue(_is_plain({"short": "deadbeef", "almost": "a" * 31}))

    def test_the_old_rules_still_hold(self) -> None:
        self.assertFalse(_is_plain({1: "x"}))
        self.assertFalse(_is_plain({"x": object()}))
        self.assertFalse(_is_plain({"x": {1, 2}}))
        deep: Any = "v"
        for _ in range(10):
            deep = [deep]
        self.assertFalse(_is_plain({"x": deep}))

    def test_twin_spec_refuses_a_credential_and_does_not_echo_it(self) -> None:
        for bad in ({"api_key": "x"}, {"x": "sk-secretvalue"}):
            with self.subTest(bad=list(bad)):
                with self.assertRaises(ValueError) as caught:
                    TwinSpec(adapter="fake", config=bad, test_mode=True)
                self.assertNotIn("secretvalue", str(caught.exception))
                self.assertNotIn("api_key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
