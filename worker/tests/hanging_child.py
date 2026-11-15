"""TEST ONLY: a stand-in twin child that outlives its parent, for the activity-lock test.

It takes its activity lock through the REAL child helper
(``simulation_child.take_activity_lock``), then blocks without a watchdog. That
imitates a real child wedged in a native call, which its watchdog thread cannot
end. It never builds a twin and never touches a network (the guard is installed).
"""

from __future__ import annotations

import os
import time
from typing import Any

import simulation_child


def main(conn: Any, activity_lock_path: str) -> None:
    from tests import net_guard

    net_guard.install()
    simulation_child.take_activity_lock(activity_lock_path)
    conn.send(("locked", os.getpid()))
    conn.close()
    time.sleep(120)  # a hard bound, in case a test is aborted; tests kill it long before
