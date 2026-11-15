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
import simulation_supervisor
import worker_lock
from simulation_supervisor import (
    WATCHDOG_MARGIN_S,
    ProcessTwinSession,
    SessionCrashed,
    SessionRefused,
    SessionStartError,
    TwinCallFailed,
    TwinSpec,
)
from tests.fake_twin import ALL_CONFIRMED, HANG, FakeTwin
from tests.fake_twin_child import (
    hold_shared_activity_lock, middle_parent_main, open_process_session, pid_alive, spec_for,
)
from tests.test_simulation_executor import assert_no_child_processes
from tests.test_simulation_gates import use_canonical_gates


class ChildCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(assert_no_child_processes, self)  # runs last
        use_canonical_gates(self, ALL_CONFIRMED)


class AdapterAllowlistTests(ChildCase):
    def test_the_spec_has_no_field_for_a_module_or_a_method(self) -> None:
        # activity_lock_path is the lock file the child locks, never something it loads or runs.
        self.assertEqual(
            {f.name for f in dataclasses.fields(TwinSpec)},
            {"adapter", "config", "test_mode", "fresh_session_stop", "activity_lock_path"},
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
        with tempfile.TemporaryDirectory() as tmp:
            path = hold_shared_activity_lock(str(Path(tmp) / "worker.activity.lock"))
            session = ProcessTwinSession(TwinSpec(adapter="production", activity_lock_path=path))
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


HELPER_HOLDS_SHARED = """
import sys
sys.path.insert(0, {worker_dir!r})
import worker_lock
worker_lock.ActivityLock({path!r}, exclusive=False).acquire()
print("held", flush=True)
sys.stdin.read()  # holds until the test closes stdin (or kills this process)
"""


def run_child_main(wire: dict, *, then_shutdown: bool = False) -> tuple[object, int | None]:
    """Spawn the real ``child_main`` directly: its first message and its exit code."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    process = ctx.Process(target=simulation_child.child_main, args=(child_conn, wire, "main", 30.0), daemon=True)
    process.start()
    child_conn.close()
    try:
        message = parent_conn.recv() if parent_conn.poll(60) else None
        if then_shutdown and message is not None and message[0] == "ready":
            parent_conn.send(("shutdown",))
        process.join(20)
        return message, process.exitcode
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        parent_conn.close()


@unittest.skipUnless(worker_lock.fcntl is not None, "needs fcntl (macOS or Linux)")
class ActivityLockTests(ChildCase):
    """Every child holds the activity lock SHARED before it builds a twin."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "worker.activity.lock")
        self.marker = str(Path(tmp.name) / "built")

    def spec(self, **options: object) -> TwinSpec:
        spec = spec_for(FakeTwin(), build_marker=self.marker)
        return dataclasses.replace(spec, **{"activity_lock_path": self.path, **options})

    def test_a_child_holds_the_shared_lock_for_its_whole_life(self) -> None:
        # The shared lock that start() needs is held by a SECOND process, and that
        # process lets go once the children are up. Neither this process nor the helper
        # holds anything when the assertions run, so an exclusive attempt can only fail
        # because of a child's own lock.
        helper = subprocess.Popen(
            [sys.executable, "-c", HELPER_HOLDS_SHARED.format(
                worker_dir=str(Path(__file__).resolve().parents[1]), path=self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(helper.kill)
        self.assertEqual(helper.stdout.readline().strip(), "held")
        with mock.patch.object(worker_lock, "holds_shared_activity_lock", return_value=True):
            session = ProcessTwinSession(self.spec()).start()  # the parent check is the helper's
        try:
            helper.stdin.close()
            self.assertEqual(helper.wait(timeout=10), 0)  # the helper's lock is gone
            self.assertFalse(worker_lock.holds_shared_activity_lock(self.path))  # and not ours
            self.assertEqual(len(session.children), 2)  # main and standby
            with self.assertRaises(worker_lock.LockHeld):
                worker_lock.ActivityLock(self.path, exclusive=True).acquire()
            with worker_lock.ActivityLock(self.path, exclusive=False):  # shared coexists
                pass
            # Each child holds its own: end the standby, the main child still holds it;
            # end the main child too and the lock is free.
            session._terminate(session.standby)
            with self.assertRaises(worker_lock.LockHeld):
                worker_lock.ActivityLock(self.path, exclusive=True).acquire()
            session._terminate(session.main)
            with worker_lock.ActivityLock(self.path, exclusive=True):
                pass
        finally:
            session.close()

    def test_the_session_refuses_before_any_child_when_the_parent_does_not_hold_the_lock(self) -> None:
        for path in (None, self.path):  # no path, and a path this process does not hold
            with self.subTest(path=path):
                session = ProcessTwinSession(self.spec(activity_lock_path=path))
                with mock.patch.object(simulation_supervisor._SPAWN, "Process") as spawn, \
                        self.assertRaises(SessionStartError) as ctx:
                    session.start()
                spawn.assert_not_called()  # no child was even created
                self.assertIn("does not hold the shared activity lock", str(ctx.exception))
                self.assertEqual(session.children, [])
                self.assertFalse(Path(self.marker).exists())
        self.assertFalse(Path(self.path).exists())  # nothing was created either

    def test_holding_is_per_path_counted_and_shared_only(self) -> None:
        # Holding is per process and per path: a second lock object on the same path
        # counts, and the claim ends only when the last one is released.
        first = worker_lock.ActivityLock(self.path, exclusive=False).acquire()
        second = worker_lock.ActivityLock(self.path, exclusive=False).acquire()
        self.assertTrue(worker_lock.holds_shared_activity_lock(self.path))
        first.unlock()
        self.assertTrue(worker_lock.holds_shared_activity_lock(self.path))
        second.unlock()
        self.assertFalse(worker_lock.holds_shared_activity_lock(self.path))
        with worker_lock.ActivityLock(self.path, exclusive=True) as exclusive:  # exclusive is not "shared"
            self.assertFalse(worker_lock.holds_shared_activity_lock(self.path))

    def test_a_child_with_the_lock_free_builds_its_twin_and_holds_the_lock(self) -> None:
        message, code = run_child_main(self.spec().to_wire(), then_shutdown=True)  # control
        self.assertEqual(message[0], "ready")
        self.assertTrue(Path(self.marker).exists())  # so "no marker" below means something
        self.assertEqual(code, 0)

    def test_reconcile_holds_the_lock_then_a_child_starts_and_builds_nothing(self) -> None:
        with worker_lock.ActivityLock(self.path, exclusive=True):  # reconcile side
            message, code = run_child_main(self.spec().to_wire())
        self.assertEqual(message, ("not_built", "ActivityLockRefused"))  # a class name only
        self.assertEqual(code, simulation_child.EXIT_NOT_BUILT)
        self.assertFalse(Path(self.marker).exists())  # the factory never ran

    def test_a_child_without_a_usable_lock_path_builds_nothing(self) -> None:
        target = Path(self.path + ".target")
        target.write_text("")
        os.symlink(target, self.path)  # a symlink is refused
        for path in (None, "", self.path):
            with self.subTest(path=path):
                message, code = run_child_main({**self.spec().to_wire(), "activity_lock_path": path})
                self.assertEqual(message, ("not_built", "ActivityLockRefused"))
                self.assertEqual(code, simulation_child.EXIT_NOT_BUILT)
                self.assertFalse(Path(self.marker).exists())

    def test_without_fcntl_the_helper_refuses(self) -> None:
        with mock.patch.object(simulation_child, "fcntl", None):
            with self.assertRaises(simulation_child.ActivityLockRefused):
                simulation_child.take_activity_lock(self.path)
        self.assertFalse(Path(self.path).exists())  # nothing was created


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

    def test_the_main_childs_connect_is_refused_when_the_gates_do_not_hold(self) -> None:
        twin = FakeTwin()
        twin.confirmations = dataclasses.replace(ALL_CONFIRMED)  # not the canonical object
        session = open_process_session(self, twin)
        with self.assertRaises(SessionRefused) as ctx:
            session.call("connect", (1.0,), time.monotonic() + 5)
        self.assertEqual(ctx.exception.reason, "gates_unconfirmed")
        self.assertEqual(twin.calls, [])  # the twin itself was never called
        self.assertTrue(session.healthy)  # a refusal is not a crash

    def test_stop_is_never_gated_in_the_child(self) -> None:
        # Agreed stop policy (e1871b3): one best-effort stop even when the gates fail.
        twin = FakeTwin()
        twin.confirmations = dataclasses.replace(ALL_CONFIRMED)
        session = open_process_session(self, twin)
        session.call("stop", (1.0,), time.monotonic() + 5)  # reaches the twin, no refusal
        self.assertIn("stop", twin.names())
        with self.assertRaises(SessionRefused) as again:
            session.call("stop", (1.0,), time.monotonic() + 5)
        self.assertEqual(again.exception.reason, "stop_already_called")  # still one stop only

    def test_the_standbys_connect_and_stop_are_not_gated(self) -> None:
        twin = FakeTwin()
        twin.confirmations = dataclasses.replace(ALL_CONFIRMED)
        session = open_process_session(self, twin)  # best_effort: a standby child exists
        attempt = session.standby_stop(time.monotonic() + 5)
        self.assertTrue(attempt.called)  # connect and stop both reached the standby twin
        self.assertEqual(twin.names().count("stop"), 1)

    def test_the_ungated_calls_still_work_when_the_gates_do_not_hold(self) -> None:
        # Read-only calls are not gated: only what reaches the simulation is.
        twin = FakeTwin()
        twin.confirmations = dataclasses.replace(ALL_CONFIRMED)
        session = open_process_session(self, twin)
        self.assertIsNotNone(session.call("runtime_target", (), time.monotonic() + 5))


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

    import worker_lock

    lock_path = str(Path(__file__).with_name("a.activity.lock"))
    worker_lock.ActivityLock(lock_path, exclusive=False).acquire()  # the parent's shared lock
    session = ProcessTwinSession(TwinSpec(adapter="production", activity_lock_path=lock_path))
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
