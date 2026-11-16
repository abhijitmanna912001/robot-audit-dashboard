"""The child-process side of a supervised twin session. No movement logic.

Started ONLY by ``simulation_supervisor`` through ``multiprocessing`` with the
``spawn`` start method. The child owns one twin for its whole life and runs at
most one requested twin method at a time. Every decision (the sequence, the
deadlines, the stop policy, whether a movement may have been sent, the audit
lifecycle) stays in the parent.

Import isolation. ``spawn`` imports this module in the child BEFORE the entry
function runs, so it imports only the standard library at module level. Worker
and test modules are imported inside ``child_main``; in test mode that happens
only after ``tests.net_guard`` is installed. This is the only simulation module
allowed to use ``os``, and only ``os._exit``, only in ``_exit_now`` (a test
checks both).

What the child guarantees:

* **No twin before the factory, no factory from input.** The adapter is a key in
  ``ADAPTER_KEYS``; the factory for each key is fixed here. ``production``
  always refuses (there is no Cyberwave adapter). ``fake`` is the test fake and
  is accepted only in test mode with the network guard installed. Building a
  twin must be side-effect free: no connection and no remote operation before
  the explicit ``connect`` request.
* **Movement only through the two-phase handshake.** ``prepare`` -> ``intent``
  (nothing sent yet) -> the parent records that a movement may now be sent ->
  ``go`` -> ``move_joint``. At most two movements per session, each nonce once,
  never after a stop.
* **No commands without a parent.** If the parent closes the pipe or dies, the
  child exits at once without another twin call. A watchdog thread waits on the
  parent's process sentinel and on a deadline (re-armed by the parent with the
  rehearsal deadline), so even a child whose main thread is stuck inside a twin
  call cannot outlive its parent or the rehearsal.
* **Ctrl-C is the parent's decision.** The child ignores SIGINT from the first
  line of ``child_main`` and reports that in READY (``sigint_ignored``). Before
  that line (while spawn is still importing modules) a Ctrl-C can end the child;
  that surfaces as a start-up failure, before anything is connected or sent.
  Delivery of a real Windows console Ctrl-C to the children is not tested.
* **Errors cross the boundary as a class name only**, never as SDK text.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import signal
import sys
import threading
import time
from multiprocessing.connection import wait
from typing import Any

__all__ = ["ADAPTER_KEYS", "CALL_METHODS", "MAX_MOVES", "child_main"]

ADAPTER_PRODUCTION = "production"
ADAPTER_FAKE = "fake"
ADAPTER_KEYS = frozenset({ADAPTER_PRODUCTION, ADAPTER_FAKE})
# Fixed here, never taken from a message: the only module the fake key loads.
_FAKE_FACTORY_MODULE = "tests.fake_twin_child"

# Twin methods a ``call`` request may name. ``move_joint`` is not here: a
# movement is possible only through ``prepare``/``go``.
CALL_METHODS = frozenset(
    {"connect", "runtime_target", "twin_target", "read_telemetry", "stop", "disconnect"}
)
MAX_MOVES = 2  # the target movement and the return movement

# Exit codes, for diagnosis in tests.
EXIT_PARENT_GONE = 70
EXIT_DEADLINE = 71
EXIT_PIPE_CLOSED = 72
EXIT_NOT_BUILT = 73
EXIT_PROTOCOL = 74

# Worker and test modules that must NOT be loaded before the guard (test mode).
_GUARDED_MODULES = (
    "audit_store", "command_processor", "config", "critic_client", "cyberwave",
    "sim_telemetry", "simulation_executor", "simulation_gates", "simulation_payloads",
    "simulation_processor", "simulation_supervisor", "supabase_client",
    "tests.fake_twin", "tests.fake_twin_child",
)

_WATCHDOG_POLL_S = 0.05


def _exit_now(code: int) -> None:
    """End this child process immediately: no cleanup, no further twin call."""
    os._exit(code)


class _Watchdog:
    """Ends the child when the parent is gone or the deadline passes.

    Runs on its own daemon thread, so it fires even while the main thread is
    stuck inside a twin call.
    """

    def __init__(self, limit_s: float) -> None:
        self._lock = threading.Lock()
        self._deadline = time.monotonic() + limit_s

    def rearm(self, seconds: float) -> None:
        with self._lock:
            self._deadline = time.monotonic() + seconds

    def run(self) -> None:
        parent = multiprocessing.parent_process()
        sentinel = parent.sentinel if parent is not None else None
        while True:
            with self._lock:
                remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                _exit_now(EXIT_DEADLINE)
            if sentinel is None:
                _exit_now(EXIT_PARENT_GONE)  # not started by a supervisor: refuse to run
            if wait([sentinel], timeout=min(remaining, _WATCHDOG_POLL_S)):
                _exit_now(EXIT_PARENT_GONE)


class _Refused(Exception):
    """The adapter key, the mode or the factory was not acceptable."""


def _send(conn: Any, message: tuple[Any, ...]) -> None:
    try:
        conn.send(message)
    except (EOFError, OSError, ValueError):
        _exit_now(EXIT_PIPE_CLOSED)


def _build_twin(adapter: Any, config: Any, test_mode: bool, guard: bool, notify: Any) -> Any:
    if adapter == ADAPTER_PRODUCTION:
        raise _Refused("no Cyberwave simulation adapter is implemented")
    if adapter == ADAPTER_FAKE:
        if not (test_mode and guard):
            raise _Refused("the fake twin needs test mode with the network guard installed")
        factory = importlib.import_module(_FAKE_FACTORY_MODULE)
        return factory.build(config, notify)
    raise _Refused("unknown adapter")


def child_main(conn: Any, spec: dict[str, Any], role: str, start_limit_s: float) -> None:
    """Entry point of a supervised twin child. Only ``simulation_supervisor`` starts it."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    watchdog = _Watchdog(float(start_limit_s))
    threading.Thread(target=watchdog.run, name="twin-child-watchdog", daemon=True).start()

    test_mode = type(spec) is dict and spec.get("test_mode") is True
    preloaded = sorted(name for name in _GUARDED_MODULES if name in sys.modules)
    guard = False
    try:
        if test_mode:
            net_guard = importlib.import_module("tests.net_guard")
            net_guard.install()
            guard = net_guard.is_installed() is True
        twin = _build_twin(spec.get("adapter"), spec.get("config"), test_mode, guard,
                           lambda record: _send(conn, ("record", tuple(record))))
        simulation_gates = importlib.import_module("simulation_gates")
        blockers = tuple(simulation_gates.twin_gate_blockers(twin))
    except BaseException as exc:  # nothing was built or the build itself failed
        _send(conn, ("not_built", type(exc).__name__))
        _exit_now(EXIT_NOT_BUILT)
        return

    def gates_ok() -> bool:
        try:
            return not simulation_gates.twin_gate_blockers(twin)
        except BaseException:
            return False

    _send(conn, ("ready", {
        "role": role,
        "pid": multiprocessing.current_process().pid,
        "sigint_ignored": signal.getsignal(signal.SIGINT) is signal.SIG_IGN,
        "guard_installed": guard,
        "preloaded_before_guard": preloaded,
        "gate_blockers": list(blockers),
    }))

    moves = 0
    stopped = False
    pending: tuple[str, str, float] | None = None
    used_nonces: set[str] = set()
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            _exit_now(EXIT_PIPE_CLOSED)
            return
        if type(message) is not tuple or not message:
            _exit_now(EXIT_PROTOCOL)
            return
        op = message[0]

        if op == "shutdown":
            conn.close()
            return
        if op == "arm" and len(message) == 2:
            watchdog.rearm(max(0.0, float(message[1])))
            continue

        if op == "call" and len(message) == 4:
            _, seq, method, args = message
            if method not in CALL_METHODS or type(args) is not tuple:
                _send(conn, ("refused", seq, "method_not_allowed"))
                continue
            if method == "stop":
                if stopped:
                    _send(conn, ("refused", seq, "stop_already_called"))
                    continue
                stopped = True
            before = gates_ok()
            try:
                value = getattr(twin, method)(*args)
            except BaseException as exc:
                _send(conn, ("error", seq, type(exc).__name__))
                continue
            after = gates_ok()
            try:
                conn.send(("result", seq, value, before, after))
            except (EOFError, OSError):
                _exit_now(EXIT_PIPE_CLOSED)
            except Exception:  # the result could not be pickled
                _send(conn, ("error", seq, "UnsendableResult"))
            continue

        if op == "prepare" and len(message) == 5:
            _, seq, nonce, joint, value = message
            if pending is not None:
                reason = "intent_already_pending"
            elif type(nonce) is not str or nonce in used_nonces:
                reason = "nonce_reused"
            elif moves >= MAX_MOVES:
                reason = "movement_limit_reached"
            elif stopped:
                reason = "stop_already_called"
            elif not gates_ok():
                reason = "gates_unconfirmed"
            else:
                reason = None
            if reason is not None:
                _send(conn, ("refused", seq, reason))
                continue
            pending = (nonce, joint, value)
            used_nonces.add(nonce)
            _send(conn, ("intent", seq, nonce))
            continue

        if op == "go" and len(message) == 3:
            _, seq, nonce = message
            if pending is None or pending[0] != nonce:
                _send(conn, ("refused", seq, "no_pending_intent"))
                continue
            _, joint, value = pending
            pending = None
            moves += 1
            try:
                twin.move_joint(joint, value)
            except BaseException as exc:
                _send(conn, ("error", seq, type(exc).__name__))
                continue
            _send(conn, ("moved", seq, nonce))
            continue

        _exit_now(EXIT_PROTOCOL)
        return
