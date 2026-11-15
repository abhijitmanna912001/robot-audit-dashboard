"""The single-instance lock (worker_lock.py). Offline; POSIX parts are skipped elsewhere."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import worker_lock
from worker_lock import ActivityLock, LockHeld, LockUnsupported, WorkerLock

WORKER_DIR = Path(__file__).resolve().parents[1]
POSIX = worker_lock.fcntl is not None


class LockTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "worker.lock"


class ActivityPathTests(unittest.TestCase):
    def test_the_activity_lock_sits_next_to_the_worker_lock(self) -> None:
        self.assertEqual(worker_lock.activity_path("/x/worker.lock"), Path("/x/worker.activity.lock"))
        self.assertEqual(worker_lock.activity_path("/x/foo.lock"), Path("/x/foo.activity.lock"))
        self.assertEqual(worker_lock.activity_path("/x/foo"), Path("/x/foo.activity.lock"))
        self.assertNotEqual(worker_lock.activity_path("/x/worker.lock"), Path("/x/worker.lock"))


@unittest.skipUnless(POSIX, "needs fcntl (macOS or Linux)")
class ActivityLockTests(LockTestCase):
    def test_shared_holders_coexist_and_block_an_exclusive_one(self) -> None:
        with ActivityLock(self.path, exclusive=False), ActivityLock(self.path, exclusive=False):
            with self.assertRaises(LockHeld) as ctx:
                ActivityLock(self.path, exclusive=True).acquire()
            self.assertEqual(ctx.exception.exit_code, worker_lock.EXIT_LOCK_HELD)
            self.assertIn("activity lock", str(ctx.exception))
        with ActivityLock(self.path, exclusive=True):  # all shared holders gone
            pass

    def test_an_exclusive_holder_blocks_shared_ones(self) -> None:
        with ActivityLock(self.path, exclusive=True):
            with self.assertRaises(LockHeld):
                ActivityLock(self.path, exclusive=False).acquire()

    def test_it_is_a_separate_lock_from_the_worker_lock(self) -> None:
        with WorkerLock(self.path), ActivityLock(worker_lock.activity_path(self.path), exclusive=True):
            pass

    def test_a_private_file_and_nothing_is_written_into_it(self) -> None:
        with ActivityLock(self.path, exclusive=False):
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.read_bytes(), b"")

    def test_a_symlink_is_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        target = self.path.parent / "target"
        target.write_text("")
        os.symlink(target, self.path)
        with self.assertRaises(worker_lock.LockUnavailable):
            ActivityLock(self.path, exclusive=False).acquire()

    def test_the_kernel_releases_it_when_the_holder_is_killed(self) -> None:
        self.path.parent.mkdir(parents=True)
        code = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {str(WORKER_DIR)!r})
            import worker_lock
            worker_lock.ActivityLock({str(self.path)!r}, exclusive=False).acquire()
            print("held", flush=True)
            time.sleep(60)
            """
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        with self.assertRaises(LockHeld):
            ActivityLock(self.path, exclusive=True).acquire()
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        with ActivityLock(self.path, exclusive=True):
            pass

    def test_without_fcntl_it_is_unsupported_and_nothing_is_created(self) -> None:
        with mock.patch.object(worker_lock, "fcntl", None):
            with self.assertRaises(LockUnsupported):
                ActivityLock(self.path, exclusive=False).acquire()
        self.assertFalse(self.path.parent.exists())


@unittest.skipUnless(POSIX, "needs fcntl (macOS or Linux)")
class AcquireTests(LockTestCase):
    def test_acquire_creates_a_private_file_and_release_frees_it(self) -> None:
        lock = WorkerLock(self.path).acquire()
        self.assertTrue(lock.held)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        lock.unlock()
        self.assertFalse(lock.held)
        with WorkerLock(self.path):  # free again
            pass

    def test_a_second_holder_is_refused_and_the_first_keeps_it(self) -> None:
        with WorkerLock(self.path):
            with self.assertRaises(LockHeld) as ctx:
                WorkerLock(self.path).acquire()
            self.assertEqual(ctx.exception.exit_code, worker_lock.EXIT_LOCK_HELD)
            self.assertIn("another worker or reconcile run holds the lock", str(ctx.exception))
            with self.assertRaises(LockHeld):  # still held after the refusal
                WorkerLock(self.path).acquire()

    def test_a_refused_attempt_does_not_rewrite_the_holders_file(self) -> None:
        with WorkerLock(self.path):
            before = self.path.read_text()
            with self.assertRaises(LockHeld):
                WorkerLock(self.path).acquire()
            self.assertEqual(self.path.read_text(), before)

    def test_pid_and_start_time_are_written_for_humans_and_quoted_on_refusal(self) -> None:
        with WorkerLock(self.path):
            text = self.path.read_text()
            self.assertIn(f"pid={os.getpid()}", text)
            self.assertIn("started=", text)
            with self.assertRaises(LockHeld) as ctx:
                WorkerLock(self.path).acquire()
            self.assertIn("informational, may be stale", str(ctx.exception))

    def test_the_file_content_never_decides_anything(self) -> None:
        # A file naming a live pid (this process) with nobody holding the lock: free.
        self.path.parent.mkdir(parents=True)
        self.path.write_text(f"pid={os.getpid()}\nstarted=2026-01-01T00:00:00Z\n")
        with WorkerLock(self.path):
            pass
        # A file naming a dead pid while the lock IS held: still refused.
        self.path.write_text("pid=1\nstarted=never\n")
        holder = WorkerLock(self.path).acquire()
        self.addCleanup(holder.unlock)
        with self.assertRaises(LockHeld):
            WorkerLock(self.path).acquire()

    def test_a_symlink_is_not_followed(self) -> None:
        self.path.parent.mkdir(parents=True)
        target = self.path.parent / "target"
        target.write_text("x")
        self.path.symlink_to(target)
        with self.assertRaises(worker_lock.LockUnavailable) as ctx:
            WorkerLock(self.path).acquire()
        self.assertNotIsInstance(ctx.exception, LockHeld)

    def test_double_acquire_by_the_same_object_is_an_error(self) -> None:
        with WorkerLock(self.path) as lock:
            with self.assertRaises(worker_lock.LockUnavailable):
                lock.acquire()

    def test_the_descriptor_is_not_inheritable(self) -> None:
        with WorkerLock(self.path) as lock:
            self.assertFalse(os.get_inheritable(lock._fd))  # type: ignore[arg-type]


class PathTests(unittest.TestCase):
    def test_the_flag_beats_the_environment_beats_the_default(self) -> None:
        with mock.patch.dict(os.environ, {worker_lock.ENV_LOCK_FILE: "/tmp/env.lock"}):
            self.assertEqual(worker_lock.resolve_path("/tmp/flag.lock"), Path("/tmp/flag.lock"))
            self.assertEqual(worker_lock.resolve_path(None), Path("/tmp/env.lock"))
        with mock.patch.dict(os.environ):
            os.environ.pop(worker_lock.ENV_LOCK_FILE, None)
            self.assertEqual(
                worker_lock.resolve_path(None), Path.home() / ".robot-audit" / "worker.lock"
            )

    def test_the_default_is_outside_the_repository(self) -> None:
        default = Path(worker_lock.DEFAULT_LOCK_PATH).expanduser()
        self.assertNotIn(WORKER_DIR.parent, default.parents)


class NoFcntlTests(LockTestCase):
    def test_without_fcntl_the_lock_is_unsupported_and_nothing_is_created(self) -> None:
        with mock.patch.object(worker_lock, "fcntl", None):
            with self.assertRaises(LockUnsupported):
                WorkerLock(self.path).acquire()
        self.assertFalse(self.path.exists())


_HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {worker!r})
    import worker_lock
    worker_lock.WorkerLock({path!r}).acquire()
    print("held", flush=True)
    time.sleep(120)
    """
)


@unittest.skipUnless(POSIX, "needs fcntl and POSIX signals")
class KilledHolderTests(LockTestCase):
    def test_the_lock_is_released_when_the_holder_is_sigkilled(self) -> None:
        """A real child process holds the lock; this test starts it and kills only it."""
        code = _HOLDER.format(worker=str(WORKER_DIR), path=str(self.path))
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(proc.stdout.close)  # type: ignore[union-attr]
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")  # type: ignore[union-attr]
            with self.assertRaises(LockHeld):
                WorkerLock(self.path).acquire()
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            try:
                with WorkerLock(self.path):
                    break
            except LockHeld:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
