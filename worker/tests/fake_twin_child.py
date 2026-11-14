"""TEST ONLY: the fake twin inside a supervised child process, and test helpers.

``build`` runs in the CHILD. ``simulation_child`` loads this module (a fixed
name, never taken from input) only in test mode and only after it installed
``tests.net_guard``. It builds a ``tests.fake_twin.FakeTwin`` from plain data and
forwards every call the fake records to the parent, where ``FakeTwin.mirror``
appends it to the parent's fake. Building is side-effect free: no call is made.

Test-only child behaviour, all from the plain-data config:

* ``canonical``: the six gate values the child's own ``CYBERWAVE_GATES`` gets
  (the child-process counterpart of ``use_canonical_gates`` in the parent);
* ``confirmations``: ``"canonical"``, ``["own", {...}]``, ``"missing"`` or
  ``"invalid"``, so the child's identity check sees what the parent's fake has;
* ``crash_in``: twin methods that end the child process abruptly (``os._exit``);
* ``ignore_sigterm``: POSIX only, to exercise kill escalation.

Nothing here is evidence of how a real Cyberwave twin, its telemetry or its stop
behaves.
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import signal
import sys
import time
from typing import Any

import simulation_gates
from sim_telemetry import RawTelemetry
from simulation_executor import StopResult
from simulation_gates import LiveSimulationGates
from simulation_supervisor import ProcessTwinSession, TwinSpec
from tests.fake_twin import FakeTwin

_EXCEPTIONS = {
    "RuntimeError": RuntimeError,
    "ValueError": ValueError,
    "OSError": OSError,
    "TimeoutError": TimeoutError,
    "ConnectionError": ConnectionError,
    "KeyboardInterrupt": KeyboardInterrupt,
    "SystemExit": SystemExit,
}
CRASH_EXIT_CODE = 99


# --------------------------------------------------------------------------
# Child side
# --------------------------------------------------------------------------


def decode(value: Any) -> Any:
    if type(value) is not dict or len(value) != 1:
        raise ValueError("not an encoded fake value")
    (kind, inner), = value.items()
    if kind == "plain":
        return inner
    if kind == "raise":
        return _EXCEPTIONS[inner]("fake failure")
    if kind == "stop_result":
        return StopResult(confirmed=inner)
    if kind == "set":
        return {decode(v) for v in inner}
    if kind == "raw_telemetry":
        return RawTelemetry(**{k: decode(v) for k, v in inner.items()})
    raise ValueError("unknown encoded fake value")


def build(config: dict[str, Any], notify: Any) -> FakeTwin:
    """Build the fake twin in the child. No twin method is called here."""
    canonical = config.get("canonical")
    if canonical is not None:
        # Test-only, in this child process only: the counterpart of use_canonical_gates.
        simulation_gates.CYBERWAVE_GATES = LiveSimulationGates(**canonical)
    twin = FakeTwin(dict(config["positions"]))
    for name in ("reported_runtime_target", "reported_twin_target", "source_type",
                 "update_received", "bad_timestamp", "stop_result"):
        setattr(twin, name, decode(config[name]))
    twin.age_s = config["age_s"]
    twin.omit_timestamp = config["omit_timestamp"]
    twin.ignore_moves = set(config["ignore_moves"])
    twin.move_offset = config["move_offset"]
    twin.drift_on_move = {n: dict(d) for n, d in config["drift_on_move"]}
    twin.fail = {name: decode(b) for name, b in config["fail"].items()}
    twin.fail_move_number = {n: decode(b) for n, b in config["fail_move_number"]}
    twin.fail_read_number = {n: decode(b) for n, b in config["fail_read_number"]}
    twin.read_overrides = {n: decode(v) for n, v in config["read_overrides"]}
    twin.hang_s = config["hang_s"]
    twin.resume_marker = config["resume_marker"]

    mode = config.get("confirmations", "canonical")
    if mode == "canonical":
        twin.confirmations = simulation_gates.CYBERWAVE_GATES
    elif mode == "missing":
        del twin.confirmations
    elif mode == "invalid":
        twin.confirmations = {"stop_method_confirmed": True}
    else:
        twin.confirmations = LiveSimulationGates(**mode[1])

    original_record = twin._record

    def record(name: str, *args: Any) -> None:
        original_record(name, *args)
        notify((name, *args))
        if name in config.get("crash_in", ()):
            os._exit(CRASH_EXIT_CODE)  # an abrupt crash inside the twin call

    twin._record = record  # type: ignore[method-assign]
    if config.get("unpicklable_runtime_target"):
        twin.reported_runtime_target = lambda: "simulation"  # a result that cannot be pickled
    if config.get("ignore_sigterm") and hasattr(signal, "SIGTERM") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return twin


# --------------------------------------------------------------------------
# Parent side helpers
# --------------------------------------------------------------------------


def _gate_fields(gates: Any) -> dict[str, bool]:
    return {f.name: getattr(gates, f.name) for f in dataclasses.fields(LiveSimulationGates)}


def spec_for(twin: FakeTwin, *, fresh_session_stop: str = "best_effort", **extra: Any) -> TwinSpec:
    """A test spec that reproduces ``twin`` (and the parent's canonical gates) in a child."""
    canonical = simulation_gates.CYBERWAVE_GATES
    config = twin.spawn_config()
    config["canonical"] = _gate_fields(canonical) if type(canonical) is LiveSimulationGates else None
    confirmations = twin.__dict__.get("confirmations", _MISSING)
    if confirmations is _MISSING:
        config["confirmations"] = "missing"
    elif confirmations is canonical:
        config["confirmations"] = "canonical"
    elif type(confirmations) is LiveSimulationGates:
        config["confirmations"] = ["own", _gate_fields(confirmations)]
    else:
        config["confirmations"] = "invalid"
    config.update(extra)
    return TwinSpec(adapter="fake", config=config, test_mode=True, fresh_session_stop=fresh_session_stop)


_MISSING = object()


def open_process_session(test: Any, twin: FakeTwin, **spec_options: Any) -> ProcessTwinSession:
    """Start a supervised session for ``twin``; it is closed when ``test`` ends."""
    session = ProcessTwinSession(spec_for(twin, **spec_options), on_record=twin.mirror)
    session.start()
    test.addCleanup(session.close)
    return session


def pid_alive(pid: int) -> bool:
    """Whether a process id is still running. Windows: Win32 API; POSIX: signal 0."""
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def middle_parent_main(report: Any, config: dict[str, Any], mode: str = "connect") -> None:
    """A stand-in worker parent, run in its own process by the parent-death tests.

    Installs the network guard and starts a supervised session. ``connect``:
    reports the twin child's pid, then blocks in a hung connect. ``move``:
    connects, then dispatches a movement (through the handshake) whose fake
    ``move_joint`` hangs, and reports ``("moving", pid)`` once the child has
    started that call. Either way it then waits until it is killed.
    """
    from tests import net_guard

    net_guard.install()
    holder: dict[str, Any] = {}

    def on_record(record: Any) -> None:
        if mode == "move" and record[0] == "move_joint":
            report.send(("moving", holder["pid"]))

    session = ProcessTwinSession(
        TwinSpec(adapter="fake", config=config, test_mode=True), on_record=on_record
    ).start()
    holder["pid"] = session.main.ready["pid"]
    try:
        if mode == "move":
            session.call("connect", (5.0,), time.monotonic() + 5.0)
            session.dispatch_move("_1", 0.25, time.monotonic() + 60.0, lambda: None)
        else:
            report.send(holder["pid"])
            session.call("connect", (60.0,), time.monotonic() + 60.0)
    finally:
        report.close()
