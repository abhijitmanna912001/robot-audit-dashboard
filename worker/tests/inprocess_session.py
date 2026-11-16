"""TEST ONLY: a twin session that calls the fake twin directly, in this process.

For the many sequence-logic tests whose fake twins are configured with Python
callables (which cannot be sent to a child process). It has no timeouts and no
threads: it refuses a fake configured with ``HANG``, and those tests use a
supervised session (``tests.fake_twin_child.open_process_session``) instead.
Process supervision itself is tested only with real child processes.

It lives in ``tests/`` on purpose: production code has no unsupervised session.
"""

from __future__ import annotations

from typing import Any, Callable

from simulation_child import CALL_METHODS, MAX_MOVES
from simulation_gates import twin_gate_blockers
from simulation_supervisor import SessionRefused, StopAttempt, TwinCallFailed


class InProcessSession:
    fresh_session_stop = "unsupported"

    def __init__(self, twin: Any) -> None:
        uses_hang = getattr(twin, "uses_hang", None)
        if callable(uses_hang) and uses_hang():
            raise ValueError("a fake configured with HANG needs a supervised session")
        self.twin = twin
        self.moves = 0
        self.closed = False

    @property
    def healthy(self) -> bool:
        return not self.closed

    def gate_blockers(self) -> tuple[str, ...]:
        return twin_gate_blockers(self.twin)

    def arm(self, until_monotonic: float) -> None:
        pass

    def call(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        return self.call_with_gates(method, args, deadline)[0]

    def call_with_gates(self, method: str, args: tuple[Any, ...], deadline: float) -> tuple[Any, bool, bool]:
        if method not in CALL_METHODS:
            raise SessionRefused("method_not_allowed")
        before = not twin_gate_blockers(self.twin)
        try:
            value = getattr(self.twin, method)(*args)
        except Exception as exc:  # a BaseException (Ctrl-C) propagates unchanged
            raise TwinCallFailed(type(exc).__name__) from None
        return value, before, not twin_gate_blockers(self.twin)

    def dispatch_move(
        self, joint: str, value_rad: float, deadline: float, on_acknowledged: Callable[[], None]
    ) -> None:
        if self.moves >= MAX_MOVES:
            raise SessionRefused("movement_limit_reached")
        on_acknowledged()
        self.moves += 1
        try:
            self.twin.move_joint(joint, value_rad)
        except Exception as exc:
            raise TwinCallFailed(type(exc).__name__) from None

    def standby_stop(self, deadline: float) -> StopAttempt:
        return StopAttempt(called=False)

    def close(self, limit_s: float = 0.0) -> None:
        self.closed = True


class InProcessProvider:
    """What a processor test passes as ``twin``: starts an ``InProcessSession``."""

    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.sessions: list[InProcessSession] = []

    def gate_blockers(self) -> tuple[str, ...]:
        return twin_gate_blockers(self.twin)

    def start(self) -> InProcessSession:
        session = InProcessSession(self.twin)
        self.sessions.append(session)
        return session
