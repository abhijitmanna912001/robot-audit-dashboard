"""An in-memory fake of ``simulation_executor.SimulationTwin`` for offline tests.

It is NOT Cyberwave and cannot reach anything: no SDK import, no network, no
credentials. It has no ``affect`` method and no way to select a target, so a
test cannot start a simulation or route to hardware through it. Every call is
recorded in ``calls`` (and passed to ``recorder``, if set) so tests can assert
exactly what was "sent" and in which order.

Its telemetry is SYNTHETIC. Each reading's ``source_type`` (``"sim"`` by
default) and ``timestamp`` (the local clock) are test values written by this
file. They are not taken from any Cyberwave message, and a test that passes
with them shows only how the worker handles a reading of that form. It is not
evidence of what the real simulator publishes, and no real Cyberwave telemetry
is involved.

Behaviour is driven by simple attributes that a test sets before the run.
Reads are numbered from 1; on a fully successful run they are: 1 start,
2 target wait, 3 target verify, 4 return wait, 5 restored verify. Movements
are numbered 1 (target) and 2 (return).

Supervised (process) tests. ``spawn_config()`` turns these attributes into
plain data so ``tests.fake_twin_child`` can build an identical fake INSIDE a
supervised child process. The child's calls come back as records and
``mirror()`` appends them to this object's ``calls``, so the same assertions
work. ``HANG`` blocks for ``hang_s`` seconds; if it was not ended by then and
``resume_marker`` is set, it writes that file before raising: a test that
terminated the child checks the file never appears (no resumed work).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from sim_telemetry import RawTelemetry
from simulation_executor import StopResult, Timeouts
from simulation_gates import LiveSimulationGates

ALL_CONFIRMED = LiveSimulationGates(
    stop_method_confirmed=True,
    stop_success_contract_confirmed=True,
    telemetry_is_actual_state_confirmed=True,
    telemetry_source_confirmed=True,
    measured_vs_placeholder_confirmed=True,
    first_movement_approved=True,
)

START = {"_1": 0.1, "_2": -0.2, "_3": 0.3, "_4": 0.0, "_5": 0.5, "_6": 0.05}

# Tight limits so failure tests finish quickly. Timeouts only allows tightening.
FAST = Timeouts(
    connect_s=0.4,
    mode_check_s=0.4,
    telemetry_read_s=0.4,
    dispatch_s=0.4,
    target_move_s=0.25,
    target_verify_s=0.4,
    return_move_s=0.25,
    restore_verify_s=0.4,
    stop_s=0.3,
    disconnect_s=0.3,
    total_s=6.0,
    poll_interval_s=0.01,
)

HANG = "hang"


def _encode(value: Any) -> Any:
    """A fake's attribute value as plain data (see ``tests.fake_twin_child.decode``)."""
    if isinstance(value, BaseException):
        return {"raise": type(value).__name__}
    if isinstance(value, StopResult):
        return {"stop_result": value.confirmed}
    if isinstance(value, RawTelemetry):
        return {"raw_telemetry": {
            "positions": _encode(value.positions), "source_type": _encode(value.source_type),
            "timestamp": _encode(value.timestamp), "update_received": _encode(value.update_received),
        }}
    if isinstance(value, set):
        return {"set": [_encode(v) for v in sorted(value)]}
    return {"plain": value}


class FakeTwin:
    def __init__(self, start: dict[str, float] | None = None) -> None:
        self.confirmations: Any = ALL_CONFIRMED
        self.positions = dict(START if start is None else start)
        self.calls: list[tuple[Any, ...]] = []
        self.recorder: Callable[[str], Any] | None = None
        self.reported_runtime_target: Any = "simulation"
        self.reported_twin_target: Any = "simulation"
        # SYNTHETIC test values, not telemetry. This fake writes the source label
        # and the timestamp itself; nothing here comes from a Cyberwave message.
        # A fake saying "sim" proves nothing about what the real simulator
        # publishes.
        self.source_type: Any = "sim"  # synthetic label, set by the test fixture
        self.update_received: Any = True  # synthetic flag
        self.age_s = 0.0  # how far in the past the synthetic timestamp is set
        self.omit_timestamp = False
        self.bad_timestamp: Any = None  # if set, used verbatim as the synthetic timestamp
        # Movement behaviour.
        self.ignore_moves: set[int] = set()  # movement numbers accepted but not applied
        self.move_offset = 0.0  # added to the commanded value when applied
        self.drift_on_move: dict[int, dict[str, float]] = {}  # other joints that drift
        self.stop_result: Any = StopResult(confirmed=True)
        # method name -> an exception to raise, or HANG to block until released
        self.fail: dict[str, Any] = {}
        self.fail_move_number: dict[int, Any] = {}
        self.fail_read_number: dict[int, Any] = {}
        # read number -> a RawTelemetry, or a function (positions) -> RawTelemetry
        self.read_overrides: dict[int, Any] = {}
        self.release = threading.Event()
        self.hang_s = 30.0  # how long HANG blocks before it gives up
        self.resume_marker: Any = None  # file written if a HANG ever resumes on its own
        self._moves = 0
        self._reads = 0

    # -- helpers ------------------------------------------------------------

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if self.recorder is not None:
            self.recorder(name)

    def _maybe_fail(self, name: str, specific: Any = None) -> None:
        behaviour = specific if specific is not None else self.fail.get(name)
        if behaviour == HANG:
            if not self.release.wait(timeout=self.hang_s) and self.resume_marker:
                with open(self.resume_marker, "a", encoding="utf-8") as marker:
                    marker.write(f"resumed after a hung {name}\n")
            raise RuntimeError("released")
        if isinstance(behaviour, BaseException):
            raise behaviour

    # -- supervised (process) tests -----------------------------------------

    def uses_hang(self) -> bool:
        """True if any configured behaviour is HANG (only a supervised session can end it)."""
        behaviours = [*self.fail.values(), *self.fail_move_number.values(), *self.fail_read_number.values()]
        return any(isinstance(b, str) and b == HANG for b in behaviours)

    def spawn_config(self) -> dict[str, Any]:
        """This fake's behaviour as plain data, for a fake built in a child process."""
        if any(callable(v) for v in self.read_overrides.values()):
            raise ValueError("a callable read override cannot be sent to a child process")
        return {
            "positions": dict(self.positions),
            "reported_runtime_target": _encode(self.reported_runtime_target),
            "reported_twin_target": _encode(self.reported_twin_target),
            "source_type": _encode(self.source_type),
            "update_received": _encode(self.update_received),
            "age_s": self.age_s,
            "omit_timestamp": self.omit_timestamp,
            "bad_timestamp": _encode(self.bad_timestamp),
            "ignore_moves": sorted(self.ignore_moves),
            "move_offset": self.move_offset,
            "drift_on_move": [[n, dict(d)] for n, d in self.drift_on_move.items()],
            "stop_result": _encode(self.stop_result),
            "fail": {name: _encode(b) for name, b in self.fail.items()},
            "fail_move_number": [[n, _encode(b)] for n, b in self.fail_move_number.items()],
            "fail_read_number": [[n, _encode(b)] for n, b in self.fail_read_number.items()],
            "read_overrides": [[n, _encode(v)] for n, v in self.read_overrides.items()],
            "hang_s": self.hang_s,
            "resume_marker": self.resume_marker,
        }

    def mirror(self, record: Any) -> None:
        """A call made by the fake in a child process, recorded here as if it ran here."""
        self.calls.append(tuple(record))
        if self.recorder is not None:
            self.recorder(record[0])

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def moves(self) -> list[tuple[str, float]]:
        return [(call[1], call[2]) for call in self.calls if call[0] == "move_joint"]

    def reading(self, positions: dict[str, Any] | None = None, **overrides: Any) -> RawTelemetry:
        """A SYNTHETIC reading in the accepted form (unless overridden).

        ``source_type`` is the fixture's own label and ``timestamp`` is this
        machine's clock in Unix seconds (the unit of the aggregated joint-state
        message). Neither is read from a message.
        """
        stamp: Any = time.time() - self.age_s  # synthetic: the local clock
        if self.bad_timestamp is not None:
            stamp = self.bad_timestamp
        if self.omit_timestamp:
            stamp = None
        values: dict[str, Any] = {
            "positions": dict(self.positions if positions is None else positions),
            "source_type": self.source_type,
            "timestamp": stamp,
            "update_received": self.update_received,
        }
        values.update(overrides)
        return RawTelemetry(**values)

    # -- SimulationTwin -----------------------------------------------------

    def connect(self, timeout_s: float) -> None:
        self._record("connect")
        self._maybe_fail("connect")

    def runtime_target(self) -> Any:
        self._record("runtime_target")
        self._maybe_fail("runtime_target")
        return self.reported_runtime_target

    def twin_target(self) -> Any:
        self._record("twin_target")
        self._maybe_fail("twin_target")
        return self.reported_twin_target

    def read_telemetry(self, timeout_s: float) -> Any:
        self._reads += 1
        self._record("read_telemetry", self._reads)
        self._maybe_fail("read_telemetry", self.fail_read_number.get(self._reads))
        override = self.read_overrides.get(self._reads)
        if callable(override):
            return override(dict(self.positions))
        if override is not None:
            return override
        return self.reading()

    def move_joint(self, joint: str, value_rad: float) -> None:
        self._moves += 1
        self._record("move_joint", joint, value_rad)
        self._maybe_fail("move_joint", self.fail_move_number.get(self._moves))
        if self._moves not in self.ignore_moves:
            self.positions[joint] = value_rad + self.move_offset
        for other, delta in self.drift_on_move.get(self._moves, {}).items():
            self.positions[other] += delta

    def stop(self, timeout_s: float) -> Any:
        self._record("stop")
        self._maybe_fail("stop")
        return self.stop_result

    def disconnect(self) -> None:
        self._record("disconnect")
        self._maybe_fail("disconnect")
