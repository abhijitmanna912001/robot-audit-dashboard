"""The supervised twin child (simulation_child.py), with REAL child processes.

Offline: the only twin is the fake, built inside the child after the network
guard was installed there. Nothing reaches Cyberwave, a simulation, a robot or a
database. Every test checks that no child process is left running.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import simulation_child
from simulation_supervisor import (
    WATCHDOG_MARGIN_S,
    ProcessTwinSession,
    SessionCrashed,
    SessionStartError,
    TwinCallFailed,
    TwinSpec,
)
from tests.fake_twin import ALL_CONFIRMED, HANG, FakeTwin
from tests.fake_twin_child import middle_parent_main, open_process_session, pid_alive, spec_for
from tests.test_simulation_executor import assert_no_child_processes
from tests.test_simulation_gates import use_canonical_gates


class ChildCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(assert_no_child_processes, self)  # runs last
        use_canonical_gates(self, ALL_CONFIRMED)


class AdapterAllowlistTests(ChildCase):
    def test_the_spec_has_no_field_for_a_module_a_path_or_a_method(self) -> None:
        self.assertEqual(
            {f.name for f in dataclasses.fields(TwinSpec)},
            {"adapter", "config", "test_mode", "fresh_session_stop"},
        )
        self.assertEqual(simulation_child.ADAPTER_KEYS, frozenset({"production", "fake"}))

    def test_only_allowlisted_adapter_keys(self) -> None:
        for bad in ("tests.fake_twin_child", "tests.fake_twin_child:build", "os:system",
                    "simulation_processor._production_twin", "Fake", "", None, 1):
            with self.subTest(adapter=bad), self.assertRaises(ValueError):
                TwinSpec(adapter=bad, test_mode=True)  # type: ignore[arg-type]

    def test_the_fake_is_test_only_and_production_has_no_fresh_session_stop(self) -> None:
        with self.assertRaises(ValueError):
            TwinSpec(adapter="fake", test_mode=False)
        for options in ({"test_mode": True}, {"fresh_session_stop": "best_effort"}):
            with self.subTest(**options), self.assertRaises(ValueError):
                TwinSpec(adapter="production", **options)
        with self.assertRaises(ValueError):
            TwinSpec(adapter="fake", test_mode=True, fresh_session_stop="always")

    def test_the_configuration_must_be_plain_data(self) -> None:
        for bad in ({"f": lambda: None}, {"o": object()}, {1: "int key"}, {"t": FakeTwin()}, "x"):
            with self.subTest(config=type(bad).__name__), self.assertRaises(ValueError):
                TwinSpec(adapter="fake", config=bad, test_mode=True)  # type: ignore[arg-type]

    def test_the_production_adapter_refuses_inside_the_child(self) -> None:
        session = ProcessTwinSession(TwinSpec(adapter="production"))
        with self.assertRaises(SessionStartError) as ctx:
            session.start()
        self.assertIn("was not built", str(ctx.exception))
        self.assertFalse(session.healthy)

    def test_the_child_refuses_the_fake_without_test_mode(self) -> None:
        spec = spec_for(FakeTwin())
        wire = {**spec.to_wire(), "test_mode": False}  # what a forged spec would send
        with mock.patch.object(TwinSpec, "to_wire", lambda self: wire):
            session = ProcessTwinSession(spec)
            with self.assertRaises(SessionStartError):
                session.start()


class GuardAndFactoryTests(ChildCase):
    def test_the_network_guard_is_installed_before_any_worker_module_is_loaded(self) -> None:
        session = open_process_session(self, FakeTwin())
        for child in session.children:
            with self.subTest(role=child.role):
                self.assertIs(child.ready["guard_installed"], True)
                self.assertEqual(child.ready["preloaded_before_guard"], [])
                self.assertEqual(child.ready["gate_blockers"], [])

    def test_building_the_twin_is_side_effect_free(self) -> None:
        twin = FakeTwin()
        open_process_session(self, twin)  # main and standby both built and READY
        self.assertEqual(twin.calls, [])  # no connect, no read, nothing

    def test_a_twin_error_crosses_as_a_class_name_only(self) -> None:
        twin = FakeTwin()
        twin.fail["connect"] = RuntimeError("SECRET-TOKEN-should-not-cross")
        session = open_process_session(self, twin)
        with self.assertRaises(TwinCallFailed) as ctx:
            session.call("connect", (1.0,), time.monotonic() + 5)
        self.assertEqual(ctx.exception.class_name, "RuntimeError")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertTrue(session.healthy)  # a twin error is not a crash

    def test_the_child_gate_check_sees_the_twin_s_own_confirmations(self) -> None:
        twin = FakeTwin()
        twin.confirmations = dataclasses.replace(ALL_CONFIRMED)  # an equal copy: not canonical
        session = open_process_session(self, twin)
        self.assertTrue(any("not the canonical" in b for b in session.gate_blockers()))


SPAWN_SMOKE_SCRIPT = '''
import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, {worker_dir!r})
from tests import net_guard

net_guard.install()  # first, in the parent AND in every child that re-imports this script
import simulation_processor  # noqa: E402  re-imported by each spawned child, as in a production launch

if __name__ == "__mp_main__":  # this is a spawned child re-importing the main module
    Path(__file__).with_name("child_reimported.txt").write_text(
        str("simulation_processor" in sys.modules), encoding="utf-8"
    )

if __name__ == "__main__":
    from simulation_supervisor import ProcessTwinSession, SessionStartError, TwinSpec

    session = ProcessTwinSession(TwinSpec(adapter="production"))
    try:
        session.start()
        print("RESULT started")
    except SessionStartError as exc:
        print("RESULT refused:", exc)
    print("CHILDREN", len(multiprocessing.active_children()))
    print("BLOCKED", len(net_guard.BLOCKED_ATTEMPTS))
'''


class SignalAndLaunchTests(ChildCase):
    def test_every_child_reports_sigint_ignored(self) -> None:
        """The handler is installed. This is NOT evidence of how a real Windows
        console Ctrl-C is delivered to the children (that is not tested)."""
        session = open_process_session(self, FakeTwin())  # main and standby
        for child in session.children:
            with self.subTest(role=child.role):
                self.assertIs(child.ready["sigint_ignored"], True)

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX only: Windows has no per-process SIGINT delivery; a console Ctrl-C would "
        "interrupt the test runner too, and Ctrl-Break is a different signal",
    )
    def test_sigint_sent_to_a_running_child_does_not_end_it(self) -> None:  # pragma: no cover - not run on Windows
        twin = FakeTwin()
        session = open_process_session(self, twin, fresh_session_stop="unsupported")
        os.kill(session.main.ready["pid"], signal.SIGINT)
        time.sleep(0.5)
        self.assertTrue(session.main.process.is_alive())
        self.assertEqual(session.call("runtime_target", (), time.monotonic() + 5), "simulation")

    def test_a_production_style_spawn_from_a_main_script_refuses(self) -> None:
        """A script launched as __main__ that imports simulation_processor starts a
        session with the production adapter: every child re-imports that script
        (and simulation_processor), and the production adapter refuses. Shows the
        spawn mechanics only; there is no production adapter."""
        folder = Path(tempfile.mkdtemp())
        script = folder / "production_spawn_smoke.py"
        worker_dir = str(Path(__file__).resolve().parents[1])
        script.write_text(SPAWN_SMOKE_SCRIPT.format(worker_dir=worker_dir), encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-B", str(script)], cwd=folder, capture_output=True,
            text=True, timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("RESULT refused:", out.stdout)
        self.assertIn("was not built (_Refused)", out.stdout)  # the production adapter refused
        self.assertIn("CHILDREN 0", out.stdout)  # no leftover child process
        self.assertIn("BLOCKED 0", out.stdout)  # the guard saw no network attempt at all
        self.assertEqual((folder / "child_reimported.txt").read_text(encoding="utf-8"), "True")

    def test_the_blocked_production_main_starts_no_twin_process(self) -> None:
        import simulation_processor
        from simulation_supervisor import ProcessTwinSession

        with mock.patch.object(ProcessTwinSession, "start") as start, \
                mock.patch.object(simulation_processor, "decide_one_command") as decide, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = simulation_processor.main(["--simulation", "--confirm-write"])
        self.assertEqual(code, 3)
        self.assertIn("BLOCKED", out.getvalue())
        start.assert_not_called()
        decide.assert_not_called()
        self.assertEqual(multiprocessing.active_children(), [])


class LifetimeTests(ChildCase):
    def test_the_watchdog_ends_a_child_stuck_in_a_call_at_its_deadline(self) -> None:
        twin = FakeTwin()
        twin.fail["connect"] = HANG
        session = open_process_session(self, twin, fresh_session_stop="unsupported")
        session.arm(time.monotonic())  # a rehearsal deadline that has passed: margin only
        started = time.monotonic()
        with self.assertRaises(SessionCrashed):
            session.call("connect", (30.0,), time.monotonic() + 30.0)
        self.assertLess(time.monotonic() - started, WATCHDOG_MARGIN_S + 3.0)
        self.assertEqual(session.main.exitcode, simulation_child.EXIT_DEADLINE)

    def test_a_closed_pipe_ends_the_child_without_another_twin_call(self) -> None:
        twin = FakeTwin()
        session = open_process_session(self, twin, fresh_session_stop="unsupported")
        process = session.main.process
        session.main.conn.close()
        process.join(10)
        self.assertEqual(process.exitcode, simulation_child.EXIT_PIPE_CLOSED)
        self.assertEqual(twin.calls, [])  # not even a disconnect

    def test_the_child_exits_when_its_parent_disappears(self) -> None:
        """A stand-in worker parent starts a session, its child hangs in connect,
        the parent is killed outright: the child must end itself."""
        twin = FakeTwin()
        twin.fail["connect"] = HANG
        config = spec_for(twin).config
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        middle = ctx.Process(target=middle_parent_main, args=(writer, config), daemon=False)
        middle.start()
        writer.close()
        try:
            self.assertTrue(reader.poll(30), "the stand-in parent did not start its session")
            child_pid = reader.recv()
            time.sleep(0.5)  # the child is now inside the hung connect
            self.assertTrue(pid_alive(child_pid))
            middle.kill()
            middle.join(10)
            gone_after = None
            started = time.monotonic()
            while time.monotonic() - started < 10:
                if not pid_alive(child_pid):
                    gone_after = time.monotonic() - started
                    break
                time.sleep(0.02)
            self.assertIsNotNone(gone_after, "the twin child outlived its parent")
            self.assertLess(gone_after, 5.0)
        finally:
            reader.close()
            if middle.is_alive():
                middle.kill()
                middle.join(5)
            middle.close()

    def test_the_child_exits_when_its_parent_disappears_during_a_movement(self) -> None:
        """The child is stuck inside the fake's move_joint when its parent is killed.

        Shows only that the LOCAL child process ends and the local fake call
        never resumes. It says nothing about whether a remote Cyberwave movement
        would stop.
        """
        marker = Path(tempfile.mkdtemp()) / "resumed.txt"
        twin = FakeTwin()
        twin.fail_move_number[1] = HANG
        twin.hang_s = 2.0
        twin.resume_marker = str(marker)
        config = spec_for(twin).config
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        middle = ctx.Process(target=middle_parent_main, args=(writer, config, "move"), daemon=False)
        middle.start()
        writer.close()
        try:
            self.assertTrue(reader.poll(30), "the stand-in parent did not reach the movement")
            tag, child_pid = reader.recv()
            self.assertEqual(tag, "moving")  # the child is inside move_joint now
            self.assertTrue(pid_alive(child_pid))
            middle.kill()
            middle.join(10)
            gone_after = None
            started = time.monotonic()
            while time.monotonic() - started < 10:
                if not pid_alive(child_pid):
                    gone_after = time.monotonic() - started
                    break
                time.sleep(0.02)
            self.assertIsNotNone(gone_after, "the twin child outlived its parent")
            self.assertLess(gone_after, 5.0)
            time.sleep(twin.hang_s + 0.5)  # longer than the hang: resumed work would write the marker
            self.assertFalse(marker.exists(), "the hung local move_joint resumed after the parent died")
        finally:
            reader.close()
            if middle.is_alive():
                middle.kill()
                middle.join(5)
            middle.close()


if __name__ == "__main__":
    unittest.main()
