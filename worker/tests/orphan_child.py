"""TEST ONLY: the real ``child_main`` whose watchdog cannot see its parent die.

It stands in for a child whose parent is gone but which is still alive (for
example blocked where its watchdog thread cannot act in time). Only the parent
sentinel is replaced: a pipe whose write end this process keeps open, so the
sentinel never becomes ready. Everything else is the real ``child_main``. With
a dead parent, what the child does next is decided by its own activity lock alone.
"""

from __future__ import annotations

import multiprocessing
import os
from typing import Any

import simulation_child


class _NeverGone:
    def __init__(self) -> None:
        self._read, self._write = os.pipe()  # the write end stays open: never ready
        self.sentinel = self._read


def main(conn: Any, spec: dict[str, Any], role: str, start_limit_s: float) -> None:
    multiprocessing.parent_process = lambda: _NeverGone()  # type: ignore[assignment]
    simulation_child.child_main(conn, spec, role, start_limit_s)
