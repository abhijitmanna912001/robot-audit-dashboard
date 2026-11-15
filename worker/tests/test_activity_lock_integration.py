"""The activity lock with REAL processes (POSIX): a killed parent, a child that lives on.

The parent holds the worker lock and the shared activity lock. Its child (a test
stand-in without a watchdog, see ``tests/hanging_child.py``) holds the shared
activity lock too. After the parent is SIGKILLed the worker lock is free, but the
reconcile script must still refuse while the child lives, and may proceed only
after the child is gone. Offline: no database, no network, no Cyberwave.
"""

from __future__ import annotations

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
from tests.fake_twin_child import pid_alive
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


if __name__ == "__main__":
    unittest.main()
