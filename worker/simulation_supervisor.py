"""Process supervision of the twin session (parent side). No movement logic.

The twin lives in a child process (``simulation_child.child_main``, started with
the ``spawn`` method on every platform). The parent sends one request at a time
and waits on the reply pipe and on the child's process sentinel until an
ABSOLUTE ``time.monotonic()`` deadline. A request that does not finish in time,
or a child that dies, ends the child: there is no worker thread to abandon, and
a terminated child cannot issue another command.

Deadlines and budgets (all absolute monotonic times, all fixed constants here;
``simulation_executor.Timeouts`` is unchanged):

* ``CHILD_START_LIMIT_S``: spawning the children and their READY, BEFORE the
  rehearsal clock starts. Nothing is connected or sent during start-up.
* ``TERMINATE_LIMIT_S``: the WHOLE termination of one child: terminate, wait,
  kill, wait, and the verification that it exited.
* ``CLEANUP_LIMIT_S``: the WHOLE ``close()`` of a session: graceful shutdown of
  every child, then termination of any child still alive.

What process supervision does NOT do: terminating the local process cannot
prove that a request already handed to a remote service was not accepted or
will not be processed later. A movement that may have been sent stays "may
have been sent".

Fresh-session stop. After the main child had to be terminated, the twin session
that sent the movement is gone. A stop from a NEW session (``standby_stop``) is
only possible if the trusted adapter configuration declares
``fresh_session_stop="best_effort"``; its result is NEVER a confirmation,
whatever it returns, because no documented guarantee says that a stop from one
session cancels a movement started by another. The production adapter key may
only declare ``"unsupported"``.
"""

from __future__ import annotations

import itertools
import multiprocessing
import secrets
import time
from dataclasses import dataclass, field
from multiprocessing.connection import wait
from typing import Any, Callable

import simulation_child
import simulation_gates
from simulation_gates import LiveSimulationGates, live_simulation_blockers

__all__ = [
    "CHILD_START_LIMIT_S",
    "CLEANUP_LIMIT_S",
    "FRESH_SESSION_STOP_VALUES",
    "TERMINATE_LIMIT_S",
    "ProcessSessionProvider",
    "ProcessTwinSession",
    "SessionCrashed",
    "SessionError",
    "SessionRefused",
    "SessionStartError",
    "SessionTimeout",
    "SessionUnusable",
    "StopAttempt",
    "SupervisionError",
    "TwinCallFailed",
    "TwinSpec",
    "canonical_gate_blockers",
    "merge_blockers",
    "terminate_process",
]

CHILD_START_LIMIT_S = 15.0
TERMINATE_LIMIT_S = 1.0
CLEANUP_LIMIT_S = 2.0
# Extra lifetime given to a child's watchdog beyond the parent's own deadline.
WATCHDOG_MARGIN_S = 2.0
FRESH_SESSION_STOP_VALUES = frozenset({"unsupported", "best_effort"})

_SPAWN = multiprocessing.get_context("spawn")
_PLAIN_SCALARS = (str, int, float, bool, type(None))


# --------------------------------------------------------------------------
# Errors. Messages are fixed text; a twin error is a class name only.
# --------------------------------------------------------------------------


class SessionError(Exception):
    """A supervised twin request did not produce a usable reply."""


class SessionStartError(SessionError):
    """A child did not start, refused its adapter, or was not READY in time."""


class SessionTimeout(SessionError):
    """A request did not finish by its deadline. The child was terminated."""


class SessionCrashed(SessionError):
    """The child exited, or broke the protocol, before replying."""


class SessionRefused(SessionError):
    """The child refused the request (for a movement: nothing was sent)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the twin process refused the request ({reason})")
        self.reason = reason


class SessionUnusable(SessionError):
    """The session has no usable main child (it was terminated or closed)."""


class TwinCallFailed(SessionError):
    """The twin method raised. Only its class name crosses the boundary."""

    def __init__(self, class_name: str) -> None:
        super().__init__(f"the twin call raised {class_name}")
        self.class_name = class_name


class SupervisionError(Exception):
    """A child could not be confirmed stopped (raised by ``close()`` only). A person has to look."""


# --------------------------------------------------------------------------
# Gates, as seen from the parent
# --------------------------------------------------------------------------


def canonical_gate_blockers() -> tuple[str, ...]:
    """The parent's own canonical gates. Nothing a twin or a child says changes them."""
    gates = simulation_gates.CYBERWAVE_GATES
    if type(gates) is not LiveSimulationGates:
        return ("the live-simulation gates are missing or not recognised",)
    return live_simulation_blockers(gates)


def merge_blockers(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Concatenate blocker lists, first occurrence wins."""
    seen: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.append(item)
    return tuple(seen)


# --------------------------------------------------------------------------
# The trusted adapter specification
# --------------------------------------------------------------------------


def _is_plain(value: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if type(value) in _PLAIN_SCALARS:
        return True
    if type(value) in (list, tuple):
        return all(_is_plain(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(k) is str and _is_plain(v, depth + 1) for k, v in value.items())
    return False


@dataclass(frozen=True)
class TwinSpec:
    """Which adapter the child builds. There is no field for a module or a method.

    ``adapter`` is a key of ``simulation_child.ADAPTER_KEYS``; the factory for
    each key is fixed in ``simulation_child``. ``config`` is plain data only.
    """

    adapter: str
    config: Any = field(default_factory=dict)
    test_mode: bool = False
    fresh_session_stop: str = "unsupported"

    def __post_init__(self) -> None:
        if type(self.adapter) is not str or self.adapter not in simulation_child.ADAPTER_KEYS:
            raise ValueError("the twin adapter is not on the allowlist")
        if type(self.test_mode) is not bool:
            raise ValueError("test_mode must be a boolean")
        if self.fresh_session_stop not in FRESH_SESSION_STOP_VALUES:
            raise ValueError("fresh_session_stop must be 'unsupported' or 'best_effort'")
        if type(self.config) is not dict or not _is_plain(self.config):
            raise ValueError("the twin configuration must be a dict of plain data")
        if self.adapter == "production" and (self.test_mode or self.fresh_session_stop != "unsupported"):
            raise ValueError("the production adapter has no test mode and no fresh-session stop")
        if self.adapter == "fake" and not self.test_mode:
            raise ValueError("the fake twin is for tests only")

    def to_wire(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "config": self.config,
            "test_mode": self.test_mode,
        }


@dataclass(frozen=True)
class StopAttempt:
    """What a fresh-session stop did. Never evidence that the arm stopped."""

    called: bool
    value: Any = None
    fresh_session: bool = True


# --------------------------------------------------------------------------
# Termination of one process, with a single absolute deadline
# --------------------------------------------------------------------------


def terminate_process(
    process: Any, limit_s: float = TERMINATE_LIMIT_S, *, deadline: float | None = None
) -> bool:
    """terminate, wait, kill, wait, verify: all by ONE absolute deadline.

    The deadline is ``now + limit_s``, or the earlier ``deadline`` if given. A
    deadline that has already passed still sends terminate/kill and checks once,
    without waiting. Returns True only when the process is verified to have
    exited (``exitcode`` set and ``is_alive()`` False). On Windows ``kill`` is the
    same TerminateProcess call as ``terminate``; elsewhere it escalates to SIGKILL.
    """
    end = time.monotonic() + limit_s
    if deadline is not None:
        end = min(end, deadline)
    if process.exitcode is not None and not process.is_alive():
        return True
    try:
        process.terminate()
    except Exception:
        pass
    process.join(max(0.0, (end - time.monotonic()) / 2))
    if process.is_alive():
        try:
            process.kill()
        except Exception:
            pass
        process.join(max(0.0, end - time.monotonic()))
    return process.exitcode is not None and not process.is_alive()


class _Child:
    def __init__(self, role: str, process: Any, conn: Any) -> None:
        self.role = role
        self.process = process
        self.conn = conn
        self.ready: dict[str, Any] | None = None
        self.dead = False  # terminated, crashed, interrupted or shut down: never used again
        self.closed = False  # pipe and process handle released
        self.exitcode: int | None = None  # kept after the process handle is closed
        # The ONE absolute termination deadline of this child, set by the first
        # termination attempt. Later attempts reuse it: no second full budget.
        self.terminate_deadline: float | None = None

    def release_handles(self) -> None:
        """Close the pipe, and the process handle if the process has exited."""
        try:
            self.conn.close()
        except Exception:
            pass
        if self.closed:
            return
        if self.process.exitcode is not None and not self.process.is_alive():
            self.exitcode = self.process.exitcode
            try:
                self.process.close()
            except Exception:
                pass
            self.closed = True


# --------------------------------------------------------------------------
# The supervised session
# --------------------------------------------------------------------------


class ProcessTwinSession:
    """One supervised twin session: a main child, and an optional standby child.

    The main child owns the twin that connects, reads and moves. The standby
    child (only with ``fresh_session_stop="best_effort"``) stays idle until a
    stop is needed after the main child was terminated. Both are started and
    must be READY before the rehearsal clock starts.

    ``cleanup_failure`` is None, or a fixed text naming the child(ren) that could
    not be confirmed stopped. It is diagnostic information: a request that timed
    out or crashed still raises its own error (``SessionTimeout``,
    ``SessionCrashed``, ...), and ``close()`` reports a stuck child.
    """

    def __init__(
        self,
        spec: TwinSpec,
        *,
        on_record: Callable[[tuple[Any, ...]], Any] | None = None,
        start_limit_s: float = CHILD_START_LIMIT_S,
    ) -> None:
        if not isinstance(spec, TwinSpec):
            raise TypeError("spec must be a TwinSpec")
        self.spec = spec
        self.fresh_session_stop = spec.fresh_session_stop
        self._on_record = on_record
        self._start_limit_s = float(start_limit_s)
        self._seq = itertools.count(1)
        self.main: _Child | None = None
        self.standby: _Child | None = None
        self._started = False
        self._close_done = False
        self.cleanup_failure: str | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def children(self) -> list[_Child]:
        return [c for c in (self.main, self.standby) if c is not None]

    @property
    def healthy(self) -> bool:
        return self._started and self.main is not None and not self.main.dead

    def start(self) -> "ProcessTwinSession":
        """Spawn the children and wait for READY. Cleans up after any failure."""
        if self._started or self.main is not None:
            raise RuntimeError("a session can be started only once")
        deadline = time.monotonic() + self._start_limit_s
        try:
            self.main = self._spawn("main")
            if self.spec.fresh_session_stop == "best_effort":
                self.standby = self._spawn("standby")
            for child in self.children:
                self._await_ready(child, deadline)
        except BaseException:
            self._close_quietly()
            raise
        self._started = True
        return self

    def _spawn(self, role: str) -> _Child:
        parent_conn, child_conn = _SPAWN.Pipe(duplex=True)
        process = _SPAWN.Process(
            target=simulation_child.child_main,
            args=(child_conn, self.spec.to_wire(), role, self._start_limit_s + WATCHDOG_MARGIN_S),
            name=f"simulation-twin-{role}",
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent_conn.close()
            child_conn.close()
            raise
        child_conn.close()  # the child has its own copy; EOF now means the child is gone
        return _Child(role, process, parent_conn)

    def _await_ready(self, child: _Child, deadline: float) -> None:
        try:
            message = self._receive(child, deadline)
        except SessionTimeout:
            raise SessionStartError(f"the {child.role} twin process was not ready in time") from None
        except SessionCrashed:
            raise SessionStartError(f"the {child.role} twin process exited during start-up") from None
        if message[0] == "not_built":
            self._terminate(child)
            raise SessionStartError(f"the {child.role} twin was not built ({message[1]})")
        if message[0] != "ready" or len(message) != 2 or type(message[1]) is not dict:
            self._terminate(child)
            raise SessionStartError(f"the {child.role} twin process sent no valid READY")
        child.ready = message[1]

    def arm(self, until_monotonic: float) -> None:
        """Bound every child's lifetime by the rehearsal deadline (plus a margin)."""
        seconds = max(0.0, until_monotonic - time.monotonic()) + WATCHDOG_MARGIN_S
        for child in self.children:
            if not child.dead:
                self._send(child, ("arm", seconds))

    def close(self, limit_s: float = CLEANUP_LIMIT_S) -> None:
        """Shut down every child by ONE absolute deadline, ``now + limit_s``. Idempotent.

        The first half of the budget is for a graceful SHUTDOWN of every live
        child; the rest is for terminating whatever is still alive (each child
        also within its own termination deadline, if it already had one). Every
        step runs even if a Ctrl-C arrives meanwhile.

        At the end: an interruption received during cleanup is re-raised FIRST,
        with any cleanup failure attached as a note (it is never hidden behind a
        ``SupervisionError``); otherwise ``SupervisionError`` if a child could not
        be confirmed stopped. ``cleanup_failure`` records the same failure.
        """
        if self._close_done:
            return
        start = time.monotonic()
        deadline = start + limit_s
        graceful_deadline = start + limit_s / 2
        held: BaseException | None = None
        for child in self.children:
            if child.closed:
                continue
            try:
                if not child.dead and child.process.is_alive():
                    try:
                        child.conn.send(("shutdown",))
                    except Exception:
                        pass
                    child.process.join(max(0.0, graceful_deadline - time.monotonic()))
            except BaseException as exc:
                held = held or exc
            child.dead = True
        stuck: list[str] = []
        for child in self.children:
            if child.closed:
                continue
            verified = False
            try:
                verified = self._terminate(child, deadline)
            except BaseException as exc:  # Ctrl-C during termination: try once more, same deadline
                held = held or exc
                try:
                    verified = self._terminate(child, deadline)
                except BaseException:
                    verified = False
            if not verified:
                stuck.append(child.role)
        self._close_done = True
        if stuck:
            self.cleanup_failure = (
                "twin process(es) " + ", ".join(stuck) + " could not be confirmed stopped"
            )
        elif all(child.closed for child in self.children):
            self.cleanup_failure = None
        if held is not None:
            if stuck:
                try:
                    held.add_note(
                        f"Cleanup after this interruption: {self.cleanup_failure}; a local twin "
                        "process may still be running."
                    )
                except Exception:
                    pass
            raise held
        if stuck:
            raise SupervisionError(self.cleanup_failure)

    def _close_quietly(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    # -- requests ------------------------------------------------------------

    def gate_blockers(self) -> tuple[str, ...]:
        """The main child's own gate check (from READY) plus the parent's canonical gates."""
        child_blockers: tuple[str, ...] = ()
        if self.main is None or self.main.ready is None:
            child_blockers = ("the twin process is not ready",)
        else:
            raw = self.main.ready.get("gate_blockers")
            if type(raw) is not list or not all(type(item) is str for item in raw):
                child_blockers = ("the twin process reported no usable gate check",)
            else:
                child_blockers = tuple(raw)
        if not self.healthy:
            child_blockers = child_blockers + ("the twin process is not running",)
        return merge_blockers(child_blockers, canonical_gate_blockers())

    def call(self, method: str, args: tuple[Any, ...], deadline: float) -> Any:
        """One twin method on the main child, finished by ``deadline``."""
        value, _before, _after = self.call_with_gates(method, args, deadline)
        return value

    def call_with_gates(
        self, method: str, args: tuple[Any, ...], deadline: float
    ) -> tuple[Any, bool, bool]:
        """Like ``call``; also returns the child's own gate check before and after."""
        child = self._require_main()
        if method not in simulation_child.CALL_METHODS or type(args) is not tuple:
            raise SessionRefused("method_not_allowed")
        seq = next(self._seq)
        self._send(child, ("call", seq, method, args))
        message = self._reply(child, seq, deadline)
        return self._result(child, message, ("result",))

    def dispatch_move(
        self,
        joint: str,
        value_rad: float,
        deadline: float,
        on_acknowledged: Callable[[], None],
    ) -> None:
        """The two-phase movement handshake.

        1. PREPARE: the child checks it may move and answers INTENT. Nothing is sent.
        2. ``on_acknowledged()``: the parent records that the movement may now be
           sent. This happens BEFORE step 3, so "GO written" always implies
           "recorded as possibly sent", even if the child dies right after.
        3. GO: only now may the child call ``move_joint``.
        """
        child = self._require_main()
        nonce = secrets.token_hex(8)
        seq = next(self._seq)
        self._send(child, ("prepare", seq, nonce, joint, value_rad))
        intent = self._reply(child, seq, deadline)
        if intent[0] == "refused" and len(intent) == 3:
            raise SessionRefused(str(intent[2]))
        if intent[0] != "intent" or len(intent) != 3 or intent[2] != nonce:
            self._terminate(child)
            raise SessionCrashed("the twin process broke the movement handshake")
        on_acknowledged()
        seq = next(self._seq)
        self._send(child, ("go", seq, nonce))
        moved = self._reply(child, seq, deadline)
        if moved[0] == "moved" and len(moved) == 3 and moved[2] == nonce:
            return
        self._result(child, moved, ())

    def standby_stop(self, deadline: float) -> StopAttempt:
        """One stop from the standby child's NEW session. Never a confirmation.

        Connects the standby twin and calls ``stop`` once, both by ``deadline``.
        If the standby is missing or its connect fails, no stop is sent.
        """
        child = self.standby
        if self.fresh_session_stop != "best_effort" or child is None or child.dead:
            return StopAttempt(called=False)
        try:
            seq = next(self._seq)
            self._send(child, ("call", seq, "connect", (max(0.0, deadline - time.monotonic()),)))
            self._result(child, self._reply(child, seq, deadline), ("result",))
        except SessionError:
            return StopAttempt(called=False)
        seq = next(self._seq)
        try:
            self._send(child, ("call", seq, "stop", (max(0.0, deadline - time.monotonic()),)))
            value, _b, _a = self._result(child, self._reply(child, seq, deadline), ("result",))
        except SessionError:
            return StopAttempt(called=True)
        return StopAttempt(called=True, value=value)

    # -- plumbing ------------------------------------------------------------

    def _require_main(self) -> _Child:
        if not self.healthy or self.main is None:
            raise SessionUnusable("the twin session is not usable")
        return self.main

    def _send(self, child: _Child, message: tuple[Any, ...]) -> None:
        """Send one message. A send that did not complete leaves the channel unusable.

        A broken pipe, or an interruption (Ctrl-C or any BaseException) during
        the send, may have left a partial message in the pipe: the child is
        terminated and never used again, so no later request goes through an
        uncertain channel. The original interruption continues.
        """
        if child.dead:
            raise SessionUnusable("the twin process is not running")
        try:
            child.conn.send(message)
        except (EOFError, OSError, ValueError):
            self._terminate(child)
            raise SessionCrashed("the twin process is gone") from None
        except BaseException:
            self._terminate(child)
            raise

    def _result(self, child: _Child, message: tuple[Any, ...], ok: tuple[str, ...]) -> Any:
        if message[0] in ok and message[0] == "result" and len(message) == 5:
            return message[2], message[3] is True, message[4] is True
        if message[0] == "error" and len(message) == 3:
            raise TwinCallFailed(str(message[2]))
        if message[0] == "refused" and len(message) == 3:
            raise SessionRefused(str(message[2]))
        self._terminate(child)
        raise SessionCrashed("the twin process sent an unexpected reply")

    def _reply(self, child: _Child, seq: int, deadline: float) -> tuple[Any, ...]:
        """Wait for the reply to ``seq``. Any interruption ends the child.

        A request in flight that is not answered (timeout, crash, Ctrl-C or any
        other BaseException in this process) leaves the child in an unknown
        state, so the child is terminated (within its one termination deadline)
        before the ORIGINAL error propagates.
        """
        try:
            message = self._receive(child, deadline)
        except BaseException:
            self._terminate(child)
            raise
        if len(message) < 2 or message[1] != seq:
            self._terminate(child)
            raise SessionCrashed("the twin process answered out of order")
        return message

    def _receive(self, child: _Child, deadline: float) -> tuple[Any, ...]:
        """The next non-record message from ``child``, by ``deadline``."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate(child)
                raise SessionTimeout("the twin request did not finish in time")
            ready = wait([child.conn, child.process.sentinel], timeout=remaining)
            if child.conn in ready or child.conn.poll(0):
                try:
                    message = child.conn.recv()
                except (EOFError, OSError):
                    self._terminate(child)
                    raise SessionCrashed("the twin process exited") from None
                if type(message) is not tuple or not message:
                    self._terminate(child)
                    raise SessionCrashed("the twin process sent an unusable message")
                if message[0] == "record":
                    if self._on_record is not None and len(message) == 2:
                        self._on_record(message[1])
                    continue
                return message
            if child.process.sentinel in ready:
                self._terminate(child)
                raise SessionCrashed("the twin process exited")
            if not ready:
                continue  # wait() returned early; the deadline check decides

    def _terminate(self, child: _Child, deadline: float | None = None) -> bool:
        """End one child by its ONE termination deadline. Never raises.

        The first call sets ``child.terminate_deadline = now + TERMINATE_LIMIT_S``;
        every later call (a request path, then ``close()``) reuses it, never a
        new full budget, optionally cut further by ``deadline``. Returns True if
        the child is verified to have exited; otherwise records
        ``cleanup_failure`` (diagnostic) and returns False. The caller raises its
        own, primary error.
        """
        child.dead = True
        if child.closed:
            return True
        if child.terminate_deadline is None:
            child.terminate_deadline = time.monotonic() + TERMINATE_LIMIT_S
        end = child.terminate_deadline if deadline is None else min(child.terminate_deadline, deadline)
        verified = False
        try:
            verified = terminate_process(child.process, max(0.0, end - time.monotonic()), deadline=end)
        except Exception:
            verified = False
        finally:
            child.release_handles()
        if not verified:
            self.cleanup_failure = f"the {child.role} twin process could not be confirmed stopped"
        return verified


class ProcessSessionProvider:
    """What ``simulation_processor`` starts a supervised session from."""

    def __init__(
        self,
        spec: TwinSpec,
        *,
        on_record: Callable[[tuple[Any, ...]], Any] | None = None,
    ) -> None:
        if not isinstance(spec, TwinSpec):
            raise TypeError("spec must be a TwinSpec")
        self.spec = spec
        self._on_record = on_record

    def gate_blockers(self) -> tuple[str, ...]:
        """Before any child exists: the parent's canonical gates only."""
        return canonical_gate_blockers()

    def start(self) -> ProcessTwinSession:
        return ProcessTwinSession(self.spec, on_record=self._on_record).start()
