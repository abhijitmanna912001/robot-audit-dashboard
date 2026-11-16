"""The activity lock with REAL processes (POSIX): a killed parent, a child that lives on.

The parent holds the worker lock and the shared activity lock. Its child (a test
stand-in without a watchdog, see ``tests/hanging_child.py``) holds the shared
activity lock too. After the parent is SIGKILLed the worker lock is free, but the
reconcile script must still refuse while the child lives, and may proceed only
after the child is gone. Offline: no database, no network, no Cyberwave.
"""

from __future__ import annotations

import dataclasses
import io
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import reconcile_executing as rc
import worker_lock
from tests import net_guard
from tests.fake_twin import FakeTwin
from tests.fake_twin_child import pid_alive, spec_for
from tests.test_reconcile_executing import stranded_client

WORKER_DIR = Path(__file__).resolve().parents[1]
POSIX = worker_lock.fcntl is not None

PARENT_SCRIPT = textwrap.dedent(
    """
    import multiprocessing, sys, time
    sys.path.insert(0, {worker_dir!r})
    import worker_lock
    from tests import hanging_child, net_guard

    if __name__ == "__main__":
        net_guard.install()
        lock_path = {lock_path!r}
        worker = worker_lock.WorkerLock(lock_path).acquire()
        activity = worker_lock.ActivityLock(worker_lock.activity_path(lock_path), exclusive=False).acquire()
        ctx = multiprocessing.get_context("spawn")
        parent_end, child_end = ctx.Pipe()
        child = ctx.Process(target=hanging_child.main,
                            args=(child_end, str(worker_lock.activity_path(lock_path))))
        child.start()
        kind, pid = parent_end.recv()
        print("child-locked", pid, flush=True)
        time.sleep(120)
    """
)


def wait_until(predicate, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@unittest.skipUnless(POSIX, "needs fcntl (macOS or Linux)")
class KilledParentLeavesAChildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.lock_path = self.folder / "worker.lock"
        self.activity_path = worker_lock.activity_path(self.lock_path)
        self.child_pid: int | None = None
        self.parent = subprocess.Popen(
            [sys.executable, "-c",
             PARENT_SCRIPT.format(worker_dir=str(WORKER_DIR), lock_path=str(self.lock_path))],
            stdout=subprocess.PIPE, text=True, cwd=self.folder,
        )
        self.addCleanup(self.cleanup_processes)
        line = self.parent.stdout.readline().split()  # blocks until the child holds its lock
        self.assertEqual(line[:1], ["child-locked"], "the parent script did not start the child")
        self.child_pid = int(line[1])

    def cleanup_processes(self) -> None:
        if self.child_pid is not None:
            try:
                os.kill(self.child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.parent.kill()
        self.parent.wait(timeout=10)
        if self.parent.stdout is not None:
            self.parent.stdout.close()

    def run_reconcile_script(self) -> subprocess.CompletedProcess[str]:
        # It refuses before any read, so this never reaches a database.
        return subprocess.run(
            [sys.executable, str(WORKER_DIR / "reconcile_executing.py"),
             "--lock-file", str(self.lock_path)],
            capture_output=True, text=True, timeout=60, cwd=self.folder,
        )

    def test_reconcile_refuses_while_a_child_lives_and_proceeds_when_it_is_gone(self) -> None:
        # 1. Parent and child alive: refused (on the worker lock).
        self.assertEqual(self.run_reconcile_script().returncode, worker_lock.EXIT_LOCK_HELD)

        # 2. SIGKILL the parent. The OS frees its worker lock at once...
        self.parent.send_signal(signal.SIGKILL)
        self.parent.wait(timeout=10)
        self.assertTrue(pid_alive(self.child_pid), "the child must outlive its parent")
        with worker_lock.WorkerLock(self.lock_path):
            pass  # ...so the worker lock alone would NOT have protected us.

        # 3. The child still holds the activity lock: reconcile refuses.
        result = self.run_reconcile_script()
        self.assertEqual(result.returncode, worker_lock.EXIT_LOCK_HELD, result.stdout + result.stderr)
        self.assertIn("REFUSED", result.stdout)
        self.assertIn("activity lock", result.stdout)
        self.assertIn("Nothing was read or written", result.stdout)
        with self.assertRaises(worker_lock.LockHeld):
            worker_lock.ActivityLock(self.activity_path, exclusive=True).acquire()
        self.assertTrue(pid_alive(self.child_pid))
        with worker_lock.WorkerLock(self.lock_path):  # the refusal released the worker lock
            pass

        # 4. End the child: now both locks can be taken.
        os.kill(self.child_pid, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not pid_alive(self.child_pid)), "the child did not end")
        with worker_lock.WorkerLock(self.lock_path), \
                worker_lock.ActivityLock(self.activity_path, exclusive=True):
            pass

        # 5. And the reconcile script itself now gets past both locks (a fake client,
        #    the network guard stays on; the real script would call the database).
        before = len(net_guard.BLOCKED_ATTEMPTS)
        out = io.StringIO()
        with mock.patch.object(rc, "_get_client", return_value=stranded_client()), redirect_stdout(out):
            code = rc.main(["--lock-file", str(self.lock_path)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Read only", out.getvalue())
        self.assertEqual(len(net_guard.BLOCKED_ATTEMPTS), before)


# A stand-in worker parent whose twin child is slow to START: the child blocks while
# spawn re-imports this script (as ``__mp_main__``), before ``child_main`` and so
# before it has taken its lock or built anything. The parent holds its own shared lock,
# as a real worker does, and reports the child's pid. ``target`` is the real
# ``child_main`` (a real watchdog) or ``orphan_child.main`` (the real ``child_main`` whose
# watchdog cannot see the parent die, so only the child's own lock decides what it does).
SLOW_START_PARENT = textwrap.dedent(
    """
    import multiprocessing, os, sys, time
    GATE = {gate!r}
    if __name__ == "__mp_main__":
        end = time.monotonic() + 60
        while not os.path.exists(GATE) and time.monotonic() < end:
            time.sleep(0.02)
    sys.path.insert(0, {worker_dir!r})
    import simulation_child, worker_lock
    from tests import orphan_child

    if __name__ == "__main__":
        lock_path = {lock_path!r}
        worker_lock.ActivityLock(lock_path, exclusive=False).acquire()
        wire = {wire!r}
        ctx = multiprocessing.get_context("spawn")
        parent_end, child_end = ctx.Pipe()
        child = ctx.Process(target={target}, args=(child_end, wire, "main", 60.0))
        child.start()
        print("child", child.pid, flush=True)
        time.sleep(120)
    """
)


@unittest.skipUnless(POSIX, "needs fcntl (macOS or Linux)")
class ParentDiesWhileChildStartsTests(unittest.TestCase):
    """The window: the parent is killed after the child exists but before it has its lock.

    The fake twin's build creates a marker file and then WAITS (``build_hang_s``). A child
    that wrongly got past its lock check is therefore alive and has a marker, whatever the
    timing; a child that was refused has neither. With ``orphan_child.main`` the watchdog
    cannot end the child early, so only the child's own lock decides what it does.
    """

    REAL = "simulation_child.child_main"
    ORPHAN = "orphan_child.main"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.activity_path = self.folder / "worker.activity.lock"
        self.gate = self.folder / "gate"
        self.marker = self.folder / "built"
        self.child_pid: int | None = None
        self.parent: subprocess.Popen[str] | None = None
        self.addCleanup(self.cleanup_processes)

    def start_parent(self, target: str) -> None:
        wire = dataclasses.replace(
            spec_for(FakeTwin(), build_marker=str(self.marker), build_hang_s=60.0),
            activity_lock_path=str(self.activity_path),
        ).to_wire()
        self.parent = subprocess.Popen(
            [sys.executable, "-c", SLOW_START_PARENT.format(
                gate=str(self.gate), worker_dir=str(WORKER_DIR), lock_path=str(self.activity_path),
                wire=wire, target=target)],
            stdout=subprocess.PIPE, text=True, cwd=self.folder,
        )
        line = self.parent.stdout.readline().split()
        self.assertEqual(line[:1], ["child"], "the parent script did not start the child")
        self.child_pid = int(line[1])

    def cleanup_processes(self) -> None:
        self.gate.write_text("")  # never leave a child waiting at the gate
        if self.child_pid is not None:
            try:
                os.kill(self.child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.parent is not None:
            self.parent.kill()
            self.parent.wait(timeout=10)
            self.parent.stdout.close()

    def kill_parent_before_the_child_has_its_lock(self) -> None:
        self.assertTrue(pid_alive(self.child_pid))
        self.assertFalse(self.gate.exists())  # the child is still blocked in start-up
        self.parent.send_signal(signal.SIGKILL)
        self.parent.wait(timeout=10)
        self.assertTrue(pid_alive(self.child_pid), "the child must outlive its parent here")

    def test_control_a_booting_child_with_a_living_parent_builds_its_twin(self) -> None:
        # So the "no marker" assertions are not vacuous: this script and wire do build.
        self.start_parent(self.REAL)
        self.assertFalse(self.marker.exists())
        self.gate.write_text("")
        self.assertTrue(wait_until(self.marker.exists), "the control child never built its twin")
        with self.assertRaises(worker_lock.LockHeld):  # parent and child both hold shared
            worker_lock.ActivityLock(self.activity_path, exclusive=True).acquire()

    def test_reconcile_wins_the_race_and_the_booting_child_never_builds_a_twin(self) -> None:
        self.start_parent(self.ORPHAN)
        self.kill_parent_before_the_child_has_its_lock()
        # Parent dead, child lockless: the exclusive lock IS available. That is the window.
        with worker_lock.ActivityLock(self.activity_path, exclusive=True):
            self.gate.write_text("")  # the child now runs child_main and asks for its lock
            # Refused, it cannot report (its pipe is dead) and exits. Had it got past the
            # lock it would have created the marker and be waiting in its build.
            self.assertTrue(wait_until(lambda: not pid_alive(self.child_pid)),
                            "the child did not end: it got past its lock check")
            self.assertFalse(self.marker.exists(), "a refused child must not build its twin")

    def test_an_orphan_child_that_got_its_lock_holds_it_until_it_ends(self) -> None:
        # No reconcile. The parent is dead and the child's watchdog is blind to that, so
        # the only thing keeping this lock held is the child's own flock.
        self.start_parent(self.ORPHAN)
        self.kill_parent_before_the_child_has_its_lock()
        self.gate.write_text("")
        self.assertTrue(wait_until(self.marker.exists), "the child never got to build its twin")
        self.assertTrue(pid_alive(self.child_pid))
        with self.assertRaises(worker_lock.LockHeld):
            worker_lock.ActivityLock(self.activity_path, exclusive=True).acquire()
        os.kill(self.child_pid, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not pid_alive(self.child_pid)), "the child did not end")
        with worker_lock.ActivityLock(self.activity_path, exclusive=True):
            pass  # released by the kernel when the child died

    def test_with_a_real_watchdog_a_booting_child_ends_with_its_dead_parent(self) -> None:
        # This is the watchdog's doing, not the lock's; the lock tests are the ones above.
        self.start_parent(self.REAL)
        self.kill_parent_before_the_child_has_its_lock()
        self.gate.write_text("")
        self.assertTrue(wait_until(lambda: not pid_alive(self.child_pid)), "the child did not end")
        with worker_lock.ActivityLock(self.activity_path, exclusive=True):
            pass  # its lock went with it


if __name__ == "__main__":
    unittest.main()
