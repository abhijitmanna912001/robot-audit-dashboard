"""Worker tests. Importing this package installs the network guard.

This runs before any ``tests.*`` module, so ``python -m unittest discover -s
tests -t .`` and ``python -m unittest tests.<module>`` are guarded too. Under
pytest, ``worker/conftest.py`` has already installed it (install is idempotent).
"""

from tests import net_guard as _net_guard

_net_guard.install()

# Tests must never touch the real lock file (~/.robot-audit/worker.lock). Every
# main() under test takes the single-instance lock, so point it at a private
# temporary file for this test process.
import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile

_lock_dir = _tempfile.mkdtemp(prefix="worker-test-lock-")
_atexit.register(_shutil.rmtree, _lock_dir, ignore_errors=True)
_os.environ["WORKER_LOCK_FILE"] = _os.path.join(_lock_dir, "worker.lock")
