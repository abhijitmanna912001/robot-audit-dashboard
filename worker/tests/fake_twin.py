"""An in-memory fake of ``simulation_executor.SimulationTwin`` for offline tests.

It is NOT Cyberwave and cannot reach anything: no SDK import, no network, no
credentials. It has no ``affect`` method and no way to select a target, so a
test cannot start a simulation or route to hardware through it. Every call is
recorded in ``calls`` (and passed to ``recorder``, if set) so tests can assert
exactly what was "sent" and in which order.

Behaviour is driven by simple attributes that a test sets before the run.
Reads are numbered from 1; on a fully successful run they are: 1 start,
2 target wait, 3 target verify, 4 return wait, 5 restored verify. Movements
are numbered 1 (target) and 2 (return).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
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


class FakeTwin:
    def __init__(self, start: dict[str, float] | None = None) -> None:
        self.confirmations: Any = ALL_CONFIRMED
        self.positions = dict(START if start is None else start)
        self.calls: list[tuple[Any, ...]] = []
        self.recorder: Callable[[str], Any] | None = None
        self.reported_runtime_target: Any = "simulation"
        self.reported_twin_target: Any = "simulation"
        # Telemetry provenance the fake reports with each reading.
        self.source: Any = "simulation"
        self.kind: Any = "measured"
        self.update_received: Any = True
        self.age_s = 0.0  # how old each reading claims to be
        self.omit_timestamp = False
        self.naive_timestamp = False
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
            self.release.wait(timeout=30)
            raise RuntimeError("released")
        if isinstance(behaviour, BaseException):
            raise behaviour

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def moves(self) -> list[tuple[str, float]]:
        return [(call[1], call[2]) for call in self.calls if call[0] == "move_joint"]

    def reading(self, positions: dict[str, Any] | None = None, **overrides: Any) -> RawTelemetry:
        """A fresh, fully proven reading (unless overridden)."""
        observed: Any = datetime.now(timezone.utc) - timedelta(seconds=self.age_s)
        if self.naive_timestamp:
            observed = observed.replace(tzinfo=None)
        if self.omit_timestamp:
            observed = None
        values: dict[str, Any] = {
            "positions": dict(self.positions if positions is None else positions),
            "source": self.source,
            "kind": self.kind,
            "observed_at": observed,
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
