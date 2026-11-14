"""pytest configuration for the worker. Loaded BEFORE any test module is collected.

pytest imports conftest.py files from the rootdir down to each collected
directory before it imports the test modules in them. ``worker/pytest.ini``
makes ``worker/`` the rootdir for any test path inside it, including runs
started from the repository root (``python -m pytest worker``), so this file is
always loaded first.

It installs the process-wide network guard (tests/net_guard.py): every real
outbound connection, DNS lookup, real httpx transport and the loading of real
critic settings or worker/.env raise ``NetworkBlocked`` for the whole run.
There is no opt-out. Tests use httpx.MockTransport, fakes and mocks only.
"""

from __future__ import annotations

from tests import net_guard

net_guard.install()


def pytest_report_header(config: object) -> str:
    return "worker network guard: installed (all outbound network access blocked)"
