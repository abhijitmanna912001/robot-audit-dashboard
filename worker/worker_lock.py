"""Single-instance lock for the worker and the reconcile script (Mac/Linux only).

Why this exists
---------------
The database cannot tell whether the worker that started a command is still
running: there is no lease, heartbeat or process id (db/001_init.sql). So
``reconcile_executing.py`` cannot learn from the database that "no worker is
running". It proves it with the operating system instead: every process that
may claim or move a command first takes this exclusive lock, and the reconcile
script takes the SAME lock. If the reconcile script holds it, no worker runs on
this machine.

What it is
----------
An exclusive, non-blocking ``fcntl.flock`` on one lock file (default
``~/.robot-audit/worker.lock``, outside the repository). The lock belongs to the
open file description, so the kernel releases it when the process ends, however
it ends (exit, crash, SIGKILL, power loss on this machine). There is no stale
lock to clean up and no timeout to guess.

The pid and the start time written into the file are for HUMANS only (so a
refusal can say who probably holds it). Nothing here reads them to decide
anything: only the lock decides.

Limits (be honest about them)
-----------------------------
* It covers ONE machine. It cannot see a worker on another machine, and flock on
  a network file system is not reliable. Keep the lock file on a local disk.
* Twin child processes do not hold THIS lock: the descriptor is not inheritable
  and the children are started with ``spawn``. When the parent dies this lock is
  released at once, while a child may live on (one stuck in a native call can
  outlive its watchdog). That is what the ACTIVITY lock (``ActivityLock``) is
  for: the parent and every child hold it shared for their whole lives and the
  reconcile script takes it exclusively, which proves no parent and no child is
  alive on this machine. An ``executing`` command without a terminal event is
  still "possibly moved", and the reconcile script still needs the operator's
  attestation (other machines).
* Without ``fcntl`` (Windows) there is no lock. The simulation path, the dry-run
  ``command_processor.py`` and the reconcile script all refuse to run (see
  ``LockUnsupported``), and a twin child without ``fcntl`` builds no twin.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

from config import load_config

try:  # POSIX only. Its absence is handled, never ignored.
    import fcntl
except ImportError:  # pragma: no cover - exercised by patching ``fcntl`` to None
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_LOCK_PATH",
    "ENV_LOCK_FILE",
    "EXIT_LOCK_HELD",
    "EXIT_LOCK_UNAVAILABLE",
    "LockError",
    "LockHeld",
    "LockUnavailable",
    "LockUnsupported",
    "ActivityLock",
    "WorkerLock",
    "activity_path",
    "holds_shared_activity_lock",
    "add_lock_argument",
    "resolve_path",
]

DEFAULT_LOCK_PATH = "~/.robot-audit/worker.lock"
ENV_LOCK_FILE = "WORKER_LOCK_FILE"

EXIT_LOCK_HELD = 6
EXIT_LOCK_UNAVAILABLE = 7

_HOLDER_READ_BYTES = 200
_PRINTABLE = re.compile(r"[^\x20-\x7e\n]")


class LockError(Exception):
    """The lock was not taken. ``exit_code`` is what the command line returns."""

    exit_code = EXIT_LOCK_UNAVAILABLE


class LockHeld(LockError):
    """Another process on this machine holds the lock."""

    exit_code = EXIT_LOCK_HELD


class LockUnavailable(LockError):
    """The lock file could not be opened or locked (permissions, a symlink, ...)."""


class LockUnsupported(LockUnavailable):
    """This platform has no ``fcntl``: there is no lock to take."""


def resolve_path(cli_value: str | None = None) -> Path:
    """The lock file: ``--lock-file`` wins, then ``WORKER_LOCK_FILE``, then the default."""
    raw = cli_value or load_config().get(ENV_LOCK_FILE) or DEFAULT_LOCK_PATH
    return Path(raw).expanduser()


def activity_path(lock_path: Path | str) -> Path:
    """The activity lock next to the worker lock: ``worker.lock`` -> ``worker.activity.lock``."""
    path = Path(lock_path)
    return path.with_name(f"{path.stem}.activity{path.suffix or '.lock'}")


# Shared activity locks THIS process holds: (pid, resolved path) -> count. The pid
# keeps a forked copy of the table from claiming a lock it does not own.
_SHARED_HELD: dict[tuple[int, str], int] = {}


def _held_key(path: Path) -> tuple[int, str]:
    return (os.getpid(), os.path.realpath(path))


def holds_shared_activity_lock(path: Path | str | None) -> bool:
    """Whether THIS process holds the shared activity lock on ``path`` right now."""
    if path is None or not str(path):
        return False
    return _SHARED_HELD.get(_held_key(Path(path)), 0) > 0


def add_lock_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lock-file",
        default=None,
        help=f"lock file (default: ${ENV_LOCK_FILE}, else {DEFAULT_LOCK_PATH})",
    )


def _holder_note(fd: int) -> str:
    """Best-effort text from the file, shown to a person only. Never used to decide."""
    try:
        text = os.pread(fd, _HOLDER_READ_BYTES, 0).decode("ascii", "replace")
    except OSError:
        return ""
    text = _PRINTABLE.sub("?", text).strip().replace("\n", ", ")
    return text


class WorkerLock:
    """``acquire()`` once, ``unlock()`` in a ``finally``; also a context manager."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "WorkerLock":
        if self._fd is not None:
            raise LockUnavailable("this process already holds the lock")
        if fcntl is None:
            raise LockUnsupported(
                "this platform has no fcntl: the worker lock needs macOS or Linux"
            )
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
        except OSError as exc:
            raise LockUnavailable(
                f"could not open the lock file {self.path} ({type(exc).__name__})"
            ) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            note = _holder_note(fd)
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                raise LockHeld(
                    f"another worker or reconcile run holds the lock {self.path}"
                    + (f" (informational, may be stale: {note})" if note else "")
                ) from None
            raise LockUnavailable(
                f"could not lock {self.path} ({type(exc).__name__})"
            ) from None
        self._fd = fd
        # For humans only. A failure to write it never gives up the lock.
        try:
            os.ftruncate(fd, 0)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            os.pwrite(fd, f"pid={os.getpid()}\nstarted={stamp}\n".encode("ascii"), 0)
        except OSError:
            pass
        return self

    def unlock(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the descriptor releases the flock

    def __enter__(self) -> "WorkerLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.unlock()


class ActivityLock:
    """The activity lock: SHARED for the parent and every twin child, EXCLUSIVE for reconcile.

    Same file rules as ``WorkerLock``, non-blocking. Nothing is written into the
    file (many holders share it). Each holder opens the file itself, so each
    holds its own open file description; the kernel drops it when that process
    dies, however it dies.
    """

    def __init__(self, path: Path | str, *, exclusive: bool) -> None:
        self.path = Path(path)
        self.exclusive = exclusive
        self._fd: int | None = None
        self._key: tuple[int, str] | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "ActivityLock":
        if self._fd is not None:
            raise LockUnavailable("this process already holds the activity lock")
        if fcntl is None:
            raise LockUnsupported(
                "this platform has no fcntl: the activity lock needs macOS or Linux"
            )
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
        except OSError as exc:
            raise LockUnavailable(
                f"could not open the activity lock file {self.path} ({type(exc).__name__})"
            ) from None
        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                raise LockHeld(
                    f"a worker or twin child process still holds the activity lock "
                    f"{self.path} on this machine"
                ) from None
            raise LockUnavailable(
                f"could not lock {self.path} ({type(exc).__name__})"
            ) from None
        self._fd = fd
        if not self.exclusive:
            self._key = _held_key(self.path)
            _SHARED_HELD[self._key] = _SHARED_HELD.get(self._key, 0) + 1
        return self

    def unlock(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
            key, self._key = self._key, None
            if key is not None:
                left = _SHARED_HELD.get(key, 0) - 1
                if left > 0:
                    _SHARED_HELD[key] = left
                else:
                    _SHARED_HELD.pop(key, None)

    def __enter__(self) -> "ActivityLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.unlock()
