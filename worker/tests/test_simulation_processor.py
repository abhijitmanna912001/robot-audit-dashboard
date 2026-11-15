"""The simulation lifecycle and its command line (simulation_processor.py). Offline.

Autospecced audit_store functions, an offline critic and a fake twin are
attached to ONE parent mock, so the exact order of every event write, status
update and twin call is asserted. Nothing here can reach Cyberwave, a
simulation session, a robot or a database. The network guard is active.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import io
import json
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import command_processor
import simulation_executor
import simulation_processor
import worker_lock
from audit_store import InvalidPayloadError, StateConflictError, WriteError
from command_processor import (
    STAGE_COMPLETED_UPDATE,
    STAGE_EXECUTING_UPDATE,
    STAGE_EXECUTION_COMPLETED_WRITE,
    STAGE_EXECUTION_FAILED_WRITE,
    STAGE_FAILED_UPDATE,
    ApprovedPlan,
    ProcessResult,
)
from plan_snapshot import PlanSnapshot
from simulation_gates import CYBERWAVE_GATES, LiveSimulationGates, load_shared_limits
from simulation_payloads import (
    validate_execution_completed,
    validate_execution_failed,
    validate_execution_started,
)
from simulation_executor import StopResult
from simulation_payloads import build_execution_failed
from simulation_processor import (
    MANUAL_VERIFICATION_NOTICE,
    SIMULATION_COMPLETED_MESSAGE,
    SIMULATION_FAILED_MESSAGE,
    SIMULATION_STARTED_MESSAGE,
    STAGE_INTERRUPTED_RECORDING,
    STAGE_INTERRUPTED_WRITE,
    STAGE_SESSION_CLEANUP,
    SimulationBlockedError,
    SimulationProcessingError,
    main,
    process_one_simulation_command,
)
from simulation_supervisor import SupervisionError
from tests import net_guard
from tests.fake_twin import ALL_CONFIRMED, FAST, START, FakeTwin
from tests.inprocess_session import InProcessProvider
from tests.test_audit_store import FakeClient
from tests.test_command_processor import WORKER_ID, command_row, critic_returning
from tests.test_simulation_gates import ALL_TRUE, REAL_CYBERWAVE_GATES, use_canonical_gates
from tests.test_plan_orchestration import (
    OUT_OF_RANGE_TEXT,
    PLAN_ID,
    SUCCESS_ORDER,
    VALID_TEXT,
    OrchestrationTestCase,
)

WORKER_DIR = Path(__file__).resolve().parents[1]
SIMULATION_MODULES = (
    "simulation_payloads",
    "sim_telemetry",
    "simulation_gates",
    "simulation_executor",
    "simulation_processor",
    "simulation_supervisor",
    "simulation_child",
)
# The ONE narrow exception to the import-isolation policy: the child entry module
# may import ``os``, only to call ``os._exit`` inside ``_exit_now`` (its watchdog
# and its closed-pipe exit). Every other simulation module keeps the full list.
OS_EXIT_MODULE = "simulation_child"
DECISION_EVENTS = ["plan_generated", "validator_result", "critic_result"]
TWIN_SUCCESS = [
    "connect", "runtime_target", "twin_target", "read_telemetry", "move_joint",
    "read_telemetry", "read_telemetry", "move_joint", "read_telemetry", "read_telemetry",
    "disconnect",
]


def source_tree(module: str) -> ast.Module:
    return ast.parse((WORKER_DIR / f"{module}.py").read_text(encoding="utf-8"))


def imports_of(module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(source_tree(module)):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class SimulationTestCase(OrchestrationTestCase):
    """OrchestrationTestCase plus a fake twin whose calls join the same call order."""

    def setUp(self) -> None:
        super().setUp()
        # TEST ONLY: the fake twin's ALL_CONFIRMED becomes the canonical object,
        # so the identity check can pass. Undone after each test.
        use_canonical_gates(self, ALL_CONFIRMED)

    def use_gates(self, gates: Any) -> None:
        """Make ``gates`` canonical AND give it to the twin, so its fields are what is tested."""
        use_canonical_gates(self, gates)
        self.twin.confirmations = gates

    def reset_calls(self) -> None:
        super().reset_calls()
        self.use_text(VALID_TEXT)
        if getattr(self, "twin", None) is not None:
            self.twin.release.set()
        self.twin = FakeTwin()
        self.twin.recorder = lambda name: self.calls.twin(name)
        self.addCleanup(self.twin.release.set)

    def run_sim(self, **kwargs: Any) -> Any:
        return process_one_simulation_command(
            WORKER_ID, twin=InProcessProvider(self.twin), timeouts=FAST, **kwargs
        )

    def store_side_effects(
        self,
        *,
        events: dict[str, Any] | None = None,
        statuses: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        events = events or {}
        statuses = statuses or {}

        def append(command_id: str, event_type: str, message: str, payload: Any = None, **_: Any) -> Any:
            if event_type in events:
                raise events[event_type]
            return {"id": f"event-{event_type}"}

        def set_status(command_id: str, expected_status: str, status: str, plan_id: Any = None, **_: Any) -> Any:
            if (expected_status, status) in statuses:
                raise statuses[(expected_status, status)]
            return command_row(status=status)

        self.calls.append_event.side_effect = append
        self.calls.set_command_status.side_effect = set_status

    def event_types(self) -> list[str]:
        return [t for t, _, _ in self.events()]

    def order(self) -> list[str]:
        """Every call in order; twin calls as ``twin:<method>``, events as their type."""
        result = []
        for call in self.calls.mock_calls:
            name = call[0]
            if name == "twin":
                result.append(f"twin:{call.args[0]}")
            elif name == "append_event":
                result.append(call.args[1])
            elif name == "set_command_status":
                result.append(f"status:{call.kwargs.get('status')}")
            else:
                result.append(name)
        return result

    def assert_nothing_sent(self) -> None:
        self.assertEqual(self.twin.moves(), [])
        self.assertNotIn("stop", self.twin.names())

    def assert_twin_untouched(self) -> None:
        self.assertEqual(self.twin.calls, [])


# --------------------------------------------------------------------------
# Command line and gating
# --------------------------------------------------------------------------


class CommandLineTests(SimulationTestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def test_simulation_requires_both_flags(self) -> None:
        for argv in ([], ["--simulation"], ["--confirm-write"], ["--worker-id", "w"]):
            with self.subTest(argv=argv):
                self.reset_calls()
                with mock.patch.object(simulation_processor, "decide_one_command") as decide, \
                        mock.patch.object(command_processor, "process_one_command") as dry_run:
                    code, text = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("BOTH --simulation and --confirm-write", text)
                self.assertIn("Nothing was claimed", text)
                decide.assert_not_called()
                dry_run.assert_not_called()  # no fallback to a dry run either
                self.assertEqual(self.call_names(), [])
                self.assert_twin_untouched()

    def test_both_flags_are_blocked_today_before_anything_is_claimed(self) -> None:
        code, text = self.run_main(["--simulation", "--confirm-write"])
        self.assertEqual(code, 3)
        self.assertIn("BLOCKED", text)
        self.assertIn("Nothing was claimed, written or sent", text)
        self.assertEqual(text.count("  - "), 6)  # every open item is listed
        self.assertIn("stop/cancel", text)
        self.assertEqual(self.call_names(), [])  # no claim, no event, no status

    def test_no_cyberwave_adapter_exists_even_if_every_gate_were_confirmed(self) -> None:
        with self.assertRaises(SimulationBlockedError):
            simulation_processor._production_twin()
        with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()):
            code, text = self.run_main(["--simulation", "--confirm-write"])
        self.assertEqual(code, 3)
        self.assertIn("no Cyberwave simulation adapter is implemented", text)
        self.assertEqual(self.call_names(), [])

    def test_a_held_lock_refuses_before_anything_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "worker.lock")
            with worker_lock.WorkerLock(path), \
                    mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                    mock.patch.object(simulation_processor, "_production_twin") as twin:
                code, text = self.run_main(["--simulation", "--confirm-write", "--lock-file", path])
        self.assertEqual(code, worker_lock.EXIT_LOCK_HELD)
        self.assertIn("REFUSED", text)
        self.assertIn("Nothing was claimed, written or sent", text)
        self.assertEqual(self.call_names(), [])  # no claim, no event, no status
        twin.assert_not_called()

    def test_without_fcntl_the_simulation_path_refuses_before_anything_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "worker.lock")
            with mock.patch.object(worker_lock, "fcntl", None), \
                    mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                    mock.patch.object(simulation_processor, "_production_twin") as twin:
                code, text = self.run_main(["--simulation", "--confirm-write", "--lock-file", path])
        self.assertEqual(code, worker_lock.EXIT_LOCK_UNAVAILABLE)
        self.assertIn("no fcntl", text)
        self.assertEqual(self.call_names(), [])
        twin.assert_not_called()

    def test_the_lock_is_held_during_the_run_and_released_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "worker.lock")
            seen: list[bool] = []

            def probe(*_a: Any, **_k: Any) -> Any:
                try:
                    worker_lock.WorkerLock(path).acquire()
                    seen.append(False)
                except worker_lock.LockHeld:
                    seen.append(True)
                raise SimulationBlockedError("probe stops the run here")

            with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                    mock.patch.object(simulation_processor, "_production_twin", side_effect=probe):
                code, _ = self.run_main(["--simulation", "--confirm-write", "--lock-file", path])
            self.assertEqual(code, 3)
            self.assertEqual(seen, [True])
            with worker_lock.WorkerLock(path):  # released after main returned
                pass

    def test_the_only_options_are_the_two_flags_and_the_worker_id(self) -> None:
        options = {
            arg.value
            for node in ast.walk(source_tree("simulation_processor"))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant)
        }
        self.assertEqual(options, {"--simulation", "--confirm-write", "--worker-id"})
        for flag in ("--live", "--hardware", "--target", "--mode", "--real", "--force", "--retry", "--poll"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                        mock.patch("sys.stderr", io.StringIO()):
                    main(["--simulation", "--confirm-write", flag])
        self.assertEqual(self.call_names(), [])

    def test_the_dry_run_worker_has_no_simulation_option(self) -> None:
        options = {
            arg.value
            for node in ast.walk(source_tree("command_processor"))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant)
        }
        self.assertEqual(options, {"--confirm-write", "--worker-id"})
        self.assertFalse(imports_of("command_processor") & set(SIMULATION_MODULES))
        self.assertFalse(imports_of("dry_run") & set(SIMULATION_MODULES))

    def test_no_environment_variable_or_setting_selects_a_target(self) -> None:
        for module in SIMULATION_MODULES:
            tree = source_tree(module)
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            with self.subTest(module=module):
                self.assertFalse({"environ", "getenv", "load_dotenv"} & (names | attrs))
                if module != OS_EXIT_MODULE:
                    self.assertNotIn("os", names | attrs)
                    self.assertNotIn("os", imports_of(module))
        with mock.patch.dict("os.environ", {
            "CYBERWAVE_MODE": "live", "SIMULATION_TARGET": "live", "EXECUTION_TARGET": "hardware",
            "CYBERWAVE_SOURCE_TYPE": "edge", "LIVE": "1", "ROBOT_LIVE": "true",
        }):
            code, _ = self.run_main(["--simulation", "--confirm-write"])
            self.assertEqual(code, 3)
            result = self.run_sim()
        self.assertEqual(result.execution, "completed")
        self.assertEqual(self.event("execution_started"), {"dryRun": False, "mode": "simulation"})

    def test_the_twin_is_required_and_there_is_no_target_parameter(self) -> None:
        parameters = inspect.signature(process_one_simulation_command).parameters
        self.assertEqual(list(parameters), ["worker_id", "twin", "client", "critic", "timeouts"])
        self.assertIs(parameters["twin"].default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            process_one_simulation_command(WORKER_ID)  # type: ignore[call-arg]

    def test_a_full_run_through_main_reports_safely(self) -> None:
        with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                mock.patch.object(simulation_processor, "_production_twin", return_value=InProcessProvider(self.twin)):
            code, text = self.run_main(["--simulation", "--confirm-write"])
        self.assertEqual(code, 0)
        self.assertIn("Simulation completed; status now completed", text)
        self.assertIn(PLAN_ID, text)
        self.assertIn("no physical hardware", text)
        self.assertEqual(self.calls.claim_next_command.call_count, 1)


class GateBlockTests(SimulationTestCase):
    def test_unconfirmed_gates_block_before_the_claim(self) -> None:
        names = [f.name for f in dataclasses.fields(ALL_CONFIRMED)]
        for missing in names:
            with self.subTest(missing=missing):
                self.reset_calls()
                self.use_gates(dataclasses.replace(ALL_CONFIRMED, **{missing: False}))
                with self.assertRaises(SimulationBlockedError):
                    self.run_sim()
                self.assertEqual(self.call_names(), [])
                self.assert_twin_untouched()

    def test_a_twin_using_the_real_cyberwave_gates_is_blocked(self) -> None:
        self.use_gates(CYBERWAVE_GATES)  # the real, unpatched object (imported above)
        with self.assertRaises(SimulationBlockedError) as ctx:
            self.run_sim()
        self.assertIn("stop/cancel", str(ctx.exception))
        self.assertEqual(self.call_names(), [])

    def test_a_twin_without_gates_is_blocked(self) -> None:
        with self.assertRaises(SimulationBlockedError):
            process_one_simulation_command(WORKER_ID, twin=object())
        self.assertEqual(self.call_names(), [])

    def test_timeouts_cannot_be_replaced_by_something_else(self) -> None:
        with self.assertRaises(TypeError):
            process_one_simulation_command(WORKER_ID, twin=self.twin, timeouts=None)  # type: ignore[arg-type]
        self.assertEqual(self.call_names(), [])


class CanonicalGateTests(SimulationTestCase):
    """The twin cannot supply replacement confirmations (processor path)."""

    def assert_blocked_before_the_claim(self, fragment: str) -> None:
        with self.assertRaises(SimulationBlockedError) as ctx:
            self.run_sim()
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(self.call_names(), [])  # no claim, no event, no status
        self.assert_twin_untouched()

    def test_a_twin_with_its_own_all_true_gates_is_blocked_with_the_real_gates(self) -> None:
        use_canonical_gates(self, REAL_CYBERWAVE_GATES)  # production state: all False
        self.twin.confirmations = LiveSimulationGates(**ALL_TRUE)
        self.assert_blocked_before_the_claim("not the canonical")

    def test_an_equal_all_true_copy_is_blocked_even_when_canonical_is_all_true(self) -> None:
        copy = LiveSimulationGates(**ALL_TRUE)
        self.assertEqual(copy, ALL_CONFIRMED)
        self.twin.confirmations = copy
        self.assert_blocked_before_the_claim("not the canonical")

    def test_a_different_all_false_instance_is_blocked(self) -> None:
        use_canonical_gates(self, REAL_CYBERWAVE_GATES)
        self.twin.confirmations = LiveSimulationGates()
        self.assert_blocked_before_the_claim("not the canonical")

    def test_missing_confirmations_are_blocked(self) -> None:
        del self.twin.confirmations
        self.assert_blocked_before_the_claim("carries no live-simulation gates")

    def test_an_incorrect_confirmations_type_is_blocked(self) -> None:
        class Subclass(LiveSimulationGates):
            pass

        for value in (dict(ALL_TRUE), types.SimpleNamespace(**ALL_TRUE), Subclass(**ALL_TRUE)):
            with self.subTest(value=type(value).__name__):
                self.reset_calls()
                self.twin.confirmations = value
                self.assert_blocked_before_the_claim("not a LiveSimulationGates object")

    def test_the_canonical_object_itself_is_accepted(self) -> None:
        import simulation_gates

        self.assertIs(self.twin.confirmations, simulation_gates.CYBERWAVE_GATES)
        self.assertEqual(self.run_sim().execution, "completed")
        self.assertEqual(self.twin.names(), TWIN_SUCCESS)


# --------------------------------------------------------------------------
# Success path
# --------------------------------------------------------------------------


class SuccessPathTests(SimulationTestCase):
    def test_exact_order_of_events_statuses_and_twin_calls(self) -> None:
        result = self.run_sim()
        self.assertEqual(
            self.order(),
            ["claim_next_command", "plan_generated", "review_plan", "validator_result",
             "critic_result", "status:approved",
             "execution_started", "status:executing"]
            + [f"twin:{name}" for name in TWIN_SUCCESS]
            + ["execution_completed", "status:completed"],
        )
        self.assertEqual(
            self.transitions(),
            [("received", "approved"), ("approved", "executing"), ("executing", "completed")],
        )
        self.assertEqual((result.outcome, result.execution, result.final_status),
                         ("executed", "completed", "completed"))
        self.assertTrue(result.movement_dispatched)

    def test_execution_started_is_written_before_anything_executes(self) -> None:
        self.run_sim()
        order = self.order()
        first_twin = next(i for i, name in enumerate(order) if name.startswith("twin:"))
        self.assertLess(order.index("execution_started"), order.index("status:executing"))
        self.assertLess(order.index("status:executing"), first_twin)
        self.assertLess(order.index("status:executing"), order.index("twin:move_joint"))

    def test_completion_is_written_only_after_restoration_was_verified(self) -> None:
        self.run_sim()
        order = self.order()
        last_read = len(order) - 1 - order[::-1].index("twin:read_telemetry")
        self.assertLess(last_read, order.index("execution_completed"))
        self.assertLess(order.index("execution_completed"), order.index("status:completed"))

    def test_exactly_one_target_movement_and_one_restoration_movement(self) -> None:
        self.run_sim()
        self.assertEqual(self.twin.moves(), [("_1", 0.25), ("_1", START["_1"])])
        self.assertNotIn("stop", self.twin.names())

    def test_only_one_command_per_invocation(self) -> None:
        self.run_sim()
        self.assertEqual(self.calls.claim_next_command.call_count, 1)
        self.assertEqual(self.event_types().count("execution_started"), 1)
        tree = source_tree("simulation_processor")
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.While, ast.AsyncFor))])

    def test_payloads_match_the_agreed_contracts_exactly(self) -> None:
        self.run_sim()
        messages = {t: m for t, m, _ in self.events()}
        started = self.event("execution_started")
        self.assertEqual(started, {"dryRun": False, "mode": "simulation"})
        self.assertIs(started["dryRun"], False)
        validate_execution_started(started)

        completed = self.event("execution_completed")
        validate_execution_completed(completed)
        self.assertEqual(set(completed), {"dryRun", "mode", "summary", "telemetry", "toleranceRad"})
        self.assertIs(completed["dryRun"], False)
        self.assertEqual(completed["mode"], "simulation")
        self.assertEqual(completed["toleranceRad"], 0.01)
        self.assertLessEqual(len(completed["summary"]), 200)
        telemetry = completed["telemetry"]
        self.assertEqual(list(telemetry), ["start", "target", "restored"])
        self.assertEqual(telemetry["start"]["joints"], START)
        self.assertEqual(telemetry["target"]["joints"], {**START, "_1": 0.25})
        self.assertEqual(telemetry["restored"]["joints"], START)
        for snapshot in telemetry.values():
            self.assertEqual(set(snapshot), {"readAt", "joints"})
            self.assertRegex(snapshot["readAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        json.dumps(completed, allow_nan=False)
        self.assertEqual(messages["execution_started"], SIMULATION_STARTED_MESSAGE)
        self.assertEqual(messages["execution_completed"], SIMULATION_COMPLETED_MESSAGE)

    def test_the_approved_immutable_snapshot_is_the_one_executed(self) -> None:
        created: list[PlanSnapshot] = []
        real_create = PlanSnapshot.create

        def capture(*args: Any, **kwargs: Any) -> PlanSnapshot:
            created.append(real_create(*args, **kwargs))
            return created[-1]

        with mock.patch.object(command_processor.PlanSnapshot, "create", side_effect=capture), \
                mock.patch.object(simulation_processor, "run_rehearsal",
                                  wraps=simulation_processor.run_rehearsal) as rehearsal:
            self.run_sim()
        self.assertEqual(len(created), 1)
        rehearsal.assert_called_once()
        self.assertIs(rehearsal.call_args.args[0], created[0])  # the very same object
        request = self.sent_request()
        self.assertEqual(created[0].plan_hash, request.plan_hash)
        self.assertEqual(self.event("critic_result")["planHash"], created[0].plan_hash)

    def test_command_text_is_parsed_exactly_once(self) -> None:
        with mock.patch.object(command_processor, "plan_command",
                               wraps=command_processor.plan_command) as planner:
            self.run_sim()
        planner.assert_called_once()
        for module in ("simulation_executor", "simulation_gates", "simulation_processor"):
            self.assertNotIn("planner", imports_of(module))
            names = {n.id for n in ast.walk(source_tree(module)) if isinstance(n, ast.Name)}
            self.assertNotIn("plan_command", names)

    def test_no_duplicate_events(self) -> None:
        self.run_sim()
        self.assertEqual(self.event_types(), DECISION_EVENTS + ["execution_started", "execution_completed"])


# --------------------------------------------------------------------------
# Not approved, and approved plans outside the first rehearsal
# --------------------------------------------------------------------------


class ApprovalRequiredTests(SimulationTestCase):
    def test_nothing_is_touched_without_both_approvals(self) -> None:
        cases = {
            "validator fail, critic reject": (OUT_OF_RANGE_TEXT, "reject", 200),
            "validator pass, critic reject": (VALID_TEXT, "reject", 200),
            "validator pass, critic error": (VALID_TEXT, "approve", 500),
            "unsupported command": ("pick up the cup", "approve", 200),
        }
        for name, (text, verdict, status) in cases.items():
            with self.subTest(case=name):
                self.reset_calls()
                self.use_text(text)
                self.use_critic(verdict, status)
                result = self.run_sim()
                self.assertEqual(result.outcome, "not_approved")
                self.assertIn(result.final_status, {"rejected", "failed"})
                self.assertIsNone(result.execution)
                self.assert_twin_untouched()
                self.assertFalse(any(t.startswith("execution_") for t in self.event_types()))

    def test_no_command(self) -> None:
        self.calls.claim_next_command.return_value = None
        result = self.run_sim()
        self.assertEqual(result.outcome, "no_command")
        self.assertEqual(self.call_names(), ["claim_next_command"])
        self.assert_twin_untouched()

    def test_approval_status_not_written_means_no_execution(self) -> None:
        self.store_side_effects(statuses={("received", "approved"): WriteError("lost")})
        with self.assertRaises(command_processor.CommandProcessingError):
            self.run_sim()
        self.assert_twin_untouched()
        self.assertNotIn("execution_started", self.event_types())

    def test_approved_plans_outside_the_first_rehearsal_are_refused(self) -> None:
        for text in (
            "set joint _2 to 0.25 rad",
            "set joint _1 to 0.3 rad",
            "set joint _1 to -0.25 rad",
            "set joint _1 to 0.25 rad; set joint _2 to 0.1 rad",
        ):
            with self.subTest(text=text):
                self.reset_calls()
                self.use_text(text)
                result = self.run_sim()
                self.assertEqual(result.decision.decision_status, "approved")  # the checks passed it
                self.assertEqual((result.execution, result.final_status), ("failed", "failed"))
                self.assertEqual(result.error_kind, "rehearsal_restriction")
                self.assert_twin_untouched()
                self.assertNotIn("execution_started", self.event_types())
                failed = self.event("execution_failed")
                validate_execution_failed(failed)
                self.assertEqual(failed["stop"], "not_needed")
                self.assertNotIn("lastTelemetry", failed)
                self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "failed")])


class ApprovalReverificationTests(SimulationTestCase):
    """The lifecycle re-verifies the approval for the exact snapshot it is given."""

    def approved(self) -> ApprovedPlan:
        with mock.patch.object(simulation_processor, "_simulation_lifecycle") as lifecycle:
            self.run_sim()
        approved = lifecycle.call_args.args[1]
        self.assertIsInstance(approved, ApprovedPlan)
        return approved

    def lifecycle(self, approved: Any) -> Any:
        result = ProcessResult(outcome="plan_checked", command_id=approved.snapshot.command_id)
        return simulation_processor._simulation_lifecycle(
            result, approved, InProcessProvider(self.twin), None, FAST
        )

    def assert_refused(self, approved: Any, kind: str = "approval_invalid") -> None:
        outcome = self.lifecycle(approved)
        self.assertEqual((outcome.execution, outcome.error_kind), ("failed", kind))
        self.assertEqual(outcome.stop, "not_needed")
        self.assert_twin_untouched()
        self.assertNotIn("execution_started", self.event_types())

    def test_the_untouched_approval_is_accepted(self) -> None:
        self.assertEqual(self.lifecycle(self.approved()).execution, "completed")

    def test_a_substituted_snapshot_is_refused(self) -> None:
        approved = self.approved()
        other = PlanSnapshot.create(approved.snapshot.command_id, "another-plan-id", approved.snapshot.actions())
        self.assert_refused(dataclasses.replace(approved, snapshot=other))

    def test_a_snapshot_for_another_command_is_refused(self) -> None:
        approved = self.approved()
        other = PlanSnapshot.create("another-command", approved.snapshot.plan_id, approved.snapshot.actions())
        self.assert_refused(dataclasses.replace(approved, snapshot=other))

    def test_changed_actions_under_the_approved_hash_are_refused(self) -> None:
        approved = self.approved()
        changed = PlanSnapshot.create(
            approved.snapshot.command_id, approved.snapshot.plan_id,
            [{**approved.snapshot.actions()[0], "value": 1.5}],
        )
        forged = dataclasses.replace(approved.snapshot, canonical_actions=changed.canonical_actions)
        self.assert_refused(dataclasses.replace(approved, snapshot=forged))
        self.assert_refused(dataclasses.replace(approved, snapshot=changed))  # new hash, old approval

    def test_a_missing_or_non_approving_critic_decision_is_refused(self) -> None:
        approved = self.approved()
        reject = critic_returning("reject")(approved.request)
        for decision in (None, reject, "approve"):
            with self.subTest(decision=type(decision).__name__):
                self.reset_calls()
                self.assert_refused(dataclasses.replace(approved, decision=decision))

    def test_a_missing_validator_pass_is_refused(self) -> None:
        approved = self.approved()
        failing = command_processor.validate_plan(approved.snapshot.plan_id, [])
        for validation in (None, failing, True):
            with self.subTest(validation=type(validation).__name__):
                self.reset_calls()
                self.assert_refused(dataclasses.replace(approved, validation=validation))

    def test_something_that_is_not_an_approved_plan_is_refused(self) -> None:
        approved = self.approved()
        fake = types.SimpleNamespace(
            snapshot=approved.snapshot, request=approved.request, decision=approved.decision,
            validation=approved.validation, events_written=approved.events_written,
        )
        self.assert_refused(fake)

    def test_gates_are_rechecked_inside_the_lifecycle(self) -> None:
        approved = self.approved()
        self.use_gates(dataclasses.replace(ALL_CONFIRMED, stop_method_confirmed=False))
        self.assert_refused(approved, kind="gate_unconfirmed")

    def test_own_gates_swapped_in_after_the_claim_are_refused_inside_the_lifecycle(self) -> None:
        approved = self.approved()  # passed the pre-claim check with the canonical object
        for value in (LiveSimulationGates(**ALL_TRUE), LiveSimulationGates(), None):
            with self.subTest(value=type(value).__name__):
                self.twin.calls.clear()
                self.twin.confirmations = value
                self.assert_refused(approved, kind="gate_unconfirmed")


# --------------------------------------------------------------------------
# Failures before any movement
# --------------------------------------------------------------------------


class NoMovementTests(SimulationTestCase):
    def assert_failed_before_movement(self, result: Any, kind: str, from_status: str) -> dict[str, Any]:
        self.assertEqual((result.execution, result.final_status, result.error_kind), ("failed", "failed", kind))
        self.assertFalse(result.movement_dispatched)
        self.assert_nothing_sent()
        failed = self.event("execution_failed")
        validate_execution_failed(failed)
        self.assertEqual(failed["stop"], "not_needed")
        self.assertEqual(failed["error"]["kind"], kind)
        self.assertEqual(self.transitions()[-1], (from_status, "failed"))
        self.assertNotIn("execution_completed", self.event_types())
        return failed

    def test_no_movement_when_event_persistence_fails(self) -> None:
        for exc in (WriteError("lost"), InvalidPayloadError("bad"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.store_side_effects(events={"execution_started": exc})
                result = self.run_sim()
                self.assert_failed_before_movement(result, "event_write_failed", "approved")
                self.assert_twin_untouched()  # not even a connection or a read
                self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "failed")])

    def test_no_movement_when_the_status_transition_fails(self) -> None:
        for exc in (WriteError("lost"), RuntimeError("refused before sending")):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.store_side_effects(statuses={("approved", "executing"): exc})
                result = self.run_sim()
                self.assert_failed_before_movement(result, "status_update_failed", "approved")
                self.assert_twin_untouched()
                # Conditional on 'approved': can never overwrite 'executing'.
                self.assertEqual(
                    self.transitions(),
                    [("received", "approved"), ("approved", "executing"), ("approved", "failed")],
                )

    def test_a_concurrent_status_change_is_not_overwritten(self) -> None:
        self.store_side_effects(statuses={("approved", "executing"): StateConflictError("no row")})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_EXECUTING_UPDATE)
        self.assertIs(err.movement_dispatched, False)
        self.assertEqual(err.stop, "not_needed")
        self.assert_twin_untouched()
        failed = self.event("execution_failed")
        validate_execution_failed(failed)
        self.assertEqual((failed["stop"], failed["error"]["kind"]), ("not_needed", "status_update_failed"))
        # No further transition: the status is unknown and is left alone.
        self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "executing")])
        self.assertIn("Nothing was connected, read or sent", str(err))
        self.assertIn("Manual recovery needed", str(err))

    def test_no_movement_when_simulation_mode_is_unknown(self) -> None:
        for value in (None, "", "unknown"):
            with self.subTest(runtime=value):
                self.reset_calls()
                self.twin.reported_runtime_target = value
                self.assert_failed_before_movement(self.run_sim(), "not_simulation", "executing")

    def test_no_movement_when_the_twin_reports_live_hardware(self) -> None:
        for value in ("live", "hardware"):
            with self.subTest(twin=value):
                self.reset_calls()
                self.twin.reported_twin_target = value
                failed = self.assert_failed_before_movement(self.run_sim(), "not_simulation", "executing")
                self.assertNotIn("lastTelemetry", failed)
                self.assertNotIn("read_telemetry", self.twin.names())

    def test_no_movement_when_telemetry_provenance_is_unverified(self) -> None:
        for name, arrange in {
            "no source": lambda t: setattr(t, "source_type", None),
            "command message": lambda t: setattr(t, "source_type", "sim_tele"),
            "hardware source": lambda t: setattr(t, "source_type", "edge"),
            "no update": lambda t: setattr(t, "update_received", None),
            "stale": lambda t: setattr(t, "age_s", 60.0),
            "no timestamp": lambda t: setattr(t, "omit_timestamp", True),
        }.items():
            with self.subTest(case=name):
                self.reset_calls()
                arrange(self.twin)
                failed = self.assert_failed_before_movement(self.run_sim(), "telemetry_invalid", "executing")
                self.assertNotIn("lastTelemetry", failed)

    def test_shared_joint_limits_are_revalidated_before_dispatch(self) -> None:
        narrow = {**load_shared_limits(), "_1": (-0.2, 0.2)}
        with mock.patch.object(simulation_executor, "load_shared_limits", return_value=narrow):
            result = self.run_sim()
        failed = self.assert_failed_before_movement(result, "limits_revalidation_failed", "executing")
        # The start reading was real, so it is kept as the last reading.
        self.assertEqual(failed["lastTelemetry"]["joints"], START)

    def test_the_limits_are_also_checked_before_execution_starts(self) -> None:
        with mock.patch.object(
            simulation_processor, "revalidate_against_shared_limits",
            side_effect=simulation_processor.GateError("limits_revalidation_failed", "outside the limits"),
        ):
            result = self.run_sim()
        self.assert_failed_before_movement(result, "limits_revalidation_failed", "approved")
        self.assert_twin_untouched()
        self.assertNotIn("execution_started", self.event_types())


# --------------------------------------------------------------------------
# Failures after a movement was sent
# --------------------------------------------------------------------------


class MovementFailureTests(SimulationTestCase):
    def assert_failed_after_movement(self, result: Any, kind: str, stop: str, moves: int) -> dict[str, Any]:
        self.assertEqual((result.execution, result.final_status, result.error_kind), ("failed", "failed", kind))
        self.assertTrue(result.movement_dispatched)
        self.assertEqual(result.stop, stop)
        self.assertEqual(len(self.twin.moves()), moves)
        self.assertEqual(self.twin.names().count("stop"), 1)
        failed = self.event("execution_failed")
        validate_execution_failed(failed)
        self.assertEqual((failed["stop"], failed["error"]["kind"]), (stop, kind))
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))
        self.assertNotIn("execution_completed", self.event_types())
        order = self.order()
        self.assertLess(order.index("twin:stop"), order.index("execution_failed"))
        self.assertLess(order.index("execution_failed"), order.index("status:failed"))
        return failed

    def test_target_timeout(self) -> None:
        self.twin.ignore_moves = {1}
        failed = self.assert_failed_after_movement(self.run_sim(), "target_timeout", "confirmed", 1)
        self.assertEqual(failed["lastTelemetry"]["joints"], START)
        self.assertEqual(self.event_types()[-2:], ["execution_started", "execution_failed"])

    def test_failed_or_ambiguous_stop_records_unconfirmed(self) -> None:
        for name, arrange in {
            "stop raises": lambda t: t.fail.__setitem__("stop", RuntimeError("connection lost")),
            "stop returns none": lambda t: setattr(t, "stop_result", None),
            "stop returns true": lambda t: setattr(t, "stop_result", True),
        }.items():
            with self.subTest(case=name):
                self.reset_calls()
                self.twin.ignore_moves = {1}
                arrange(self.twin)
                self.assert_failed_after_movement(self.run_sim(), "target_timeout", "unconfirmed", 1)

    def test_failure_after_the_target_was_reached_sends_no_return_movement(self) -> None:
        self.twin.drift_on_move = {1: {"_2": 0.04}}
        failed = self.assert_failed_after_movement(self.run_sim(), "unexpected_joint_motion", "confirmed", 1)
        # The arm is still at the target; nothing claims it was restored.
        self.assertEqual(failed["lastTelemetry"]["joints"]["_1"], 0.25)

    def test_return_movement_failure_sends_no_second_return(self) -> None:
        self.twin.fail_move_number[2] = RuntimeError("publish failed")
        self.assert_failed_after_movement(self.run_sim(), "dispatch_failed", "confirmed", 2)

    def test_return_timeout(self) -> None:
        self.twin.ignore_moves = {2}
        failed = self.assert_failed_after_movement(self.run_sim(), "return_timeout", "confirmed", 2)
        self.assertEqual(failed["lastTelemetry"]["joints"]["_1"], 0.25)

    def test_last_telemetry_is_omitted_when_the_read_did_not_validate(self) -> None:
        self.twin.fail_move_number[1] = RuntimeError("publish failed")
        self.twin.fail_read_number[2] = RuntimeError("read failed")
        failed = self.assert_failed_after_movement(self.run_sim(), "dispatch_failed", "confirmed", 1)
        self.assertNotIn("lastTelemetry", failed)

    def test_no_raw_sdk_error_reaches_an_event(self) -> None:
        secret = "https://api.example.invalid/v1?api_key=sk-LEAK-123 Authorization: Bearer LEAK-TOKEN"
        for arrange in (
            lambda t: t.fail.__setitem__("connect", RuntimeError(secret)),
            lambda t: t.fail_move_number.__setitem__(1, RuntimeError(secret)),
            lambda t: t.fail.__setitem__("stop", RuntimeError(secret)),
        ):
            self.reset_calls()
            self.twin.ignore_moves = {1}
            arrange(self.twin)
            result = self.run_sim()
            stored = json.dumps(self.events()) + json.dumps(dataclasses.asdict(result), default=str)
            for fragment in ("LEAK", "https://", "Bearer", "api_key"):
                self.assertNotIn(fragment, stored)
            self.assertEqual(self.event("execution_failed")["error"]["kind"], result.error_kind)
            self.assertEqual(self.events()[-1][1], SIMULATION_FAILED_MESSAGE)

    def test_nothing_is_retried_after_a_failure(self) -> None:
        self.twin.ignore_moves = {1}
        self.run_sim()
        self.assertEqual(self.calls.claim_next_command.call_count, 1)
        self.assertEqual(self.twin.names().count("connect"), 1)
        self.assertEqual(self.event_types().count("execution_started"), 1)
        self.assertEqual(self.event_types().count("execution_failed"), 1)


# --------------------------------------------------------------------------
# Failures of the audit writes themselves
# --------------------------------------------------------------------------


class AuditWriteFailureTests(SimulationTestCase):
    def test_failure_event_not_persisted_is_reported_not_hidden(self) -> None:
        for exc, written in ((WriteError("lost"), None), (InvalidPayloadError("bad"), False)):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.twin.ignore_moves = {1}
                self.store_side_effects(events={"execution_failed": exc})
                with self.assertRaises(SimulationProcessingError) as ctx:
                    self.run_sim()
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_EXECUTION_FAILED_WRITE)
                self.assertIs(err.event_written, written)
                self.assertNotIn("execution_failed", err.events_written)
                # Event first: without the event the status is not moved to failed.
                self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "executing")])
                # Manual-recovery information is preserved.
                self.assertIs(err.movement_dispatched, True)
                self.assertEqual((err.failure_kind, err.stop), ("target_timeout", "confirmed"))
                self.assertEqual(err.last_telemetry["joints"], START)
                text = str(err)
                self.assertIn("EXECUTION FAILURE NOT RECORDED", text)
                self.assertIn("should still be 'executing'", text)
                self.assertIn("Movement sent: YES", text)
                self.assertIn("Stop: confirmed", text)
                self.assertIn('"_1": 0.1', text)

    def test_failure_event_not_persisted_before_any_movement(self) -> None:
        self.store_side_effects(
            events={"execution_started": WriteError("a"), "execution_failed": WriteError("b")}
        )
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        self.assertEqual(ctx.exception.stage, STAGE_EXECUTION_FAILED_WRITE)
        self.assertIs(ctx.exception.movement_dispatched, False)
        self.assertEqual(self.transitions(), [("received", "approved")])
        self.assert_twin_untouched()
        self.assertIn("should still be 'approved'", str(ctx.exception))
        self.assertIn("Movement sent: no", str(ctx.exception))

    def test_failed_status_transition_after_the_failure_event(self) -> None:
        self.twin.ignore_moves = {1}
        self.store_side_effects(statuses={("executing", "failed"): StateConflictError("no row")})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_FAILED_UPDATE)
        self.assertEqual(self.event_types().count("execution_failed"), 1)  # not written twice
        self.assertEqual(self.transitions().count(("executing", "failed")), 1)  # not retried
        self.assertIn("Do NOT write execution_failed again", str(err))
        self.assertIs(err.movement_dispatched, True)

    def test_completion_event_write_fails(self) -> None:
        self.store_side_effects(events={"execution_completed": WriteError("lost")})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        err = ctx.exception
        self.assertEqual(err.stage, STAGE_EXECUTION_COMPLETED_WRITE)
        self.assertIsNone(err.event_written)
        self.assertEqual(self.transitions(), [("received", "approved"), ("approved", "executing")])
        self.assertNotIn("execution_failed", self.event_types())  # it did not fail
        self.assertEqual(len(self.twin.moves()), 2)  # the simulation did not run again
        self.assertIn("verified restored", str(err))
        self.assertIn("Do NOT run the simulation again", str(err))
        self.assertEqual(err.last_telemetry["joints"], START)

    def test_final_status_transition_fails_without_retry_or_duplicates(self) -> None:
        for exc in (StateConflictError("no row"), WriteError("lost"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.reset_calls()
                self.store_side_effects(statuses={("executing", "completed"): exc})
                with self.assertRaises(SimulationProcessingError) as ctx:
                    self.run_sim()
                err = ctx.exception
                self.assertEqual(err.stage, STAGE_COMPLETED_UPDATE)
                self.assertEqual(err.intended_status, "completed")
                self.assertEqual(self.event_types().count("execution_completed"), 1)
                self.assertNotIn("execution_failed", self.event_types())
                self.assertEqual(self.transitions().count(("executing", "completed")), 1)
                self.assertEqual(self.order()[-1], "status:completed")
                self.assertEqual(len(self.twin.moves()), 2)
                self.assertIn("set executing -> completed only", str(err))

    def test_an_unbuildable_completion_record_is_a_recorded_failure(self) -> None:
        with mock.patch.object(simulation_processor, "build_execution_completed",
                               side_effect=simulation_processor.SimulationPayloadError("bad")):
            result = self.run_sim()
        self.assertEqual((result.execution, result.error_kind), ("failed", "internal_error"))
        self.assertNotIn("execution_completed", self.event_types())
        failed = self.event("execution_failed")
        validate_execution_failed(failed)
        self.assertEqual(failed["stop"], "unconfirmed")  # no stop was confirmed


# --------------------------------------------------------------------------
# Real audit_store code against the in-memory fake database
# --------------------------------------------------------------------------


class RealStoreTests(SimulationTestCase):
    def run_real(self, text: str = VALID_TEXT) -> tuple[Any, FakeClient]:
        client = FakeClient(commands=[command_row(text, claimed_by=None, claimed_at=None)])
        critic = self.calls.review_plan.side_effect
        self._stop_patchers()  # use the real audit_store functions
        result = process_one_simulation_command(
            WORKER_ID, twin=InProcessProvider(self.twin), client=client, critic=critic, timeouts=FAST
        )
        return result, client

    def test_successful_rehearsal_end_to_end(self) -> None:
        result, client = self.run_real()
        events = client.tables["audit_events"]
        self.assertEqual(
            [e["event_type"] for e in events],
            DECISION_EVENTS + ["execution_started", "execution_completed"],
        )
        self.assertEqual(events[3]["payload"], {"dryRun": False, "mode": "simulation"})
        validate_execution_completed(events[4]["payload"])
        self.assertEqual(client.tables["commands"][0]["status"], "completed")
        self.assertEqual(result.final_status, "completed")

    def test_failed_rehearsal_end_to_end(self) -> None:
        self.twin.ignore_moves = {1}
        result, client = self.run_real()
        events = client.tables["audit_events"]
        self.assertEqual(
            [e["event_type"] for e in events],
            DECISION_EVENTS + ["execution_started", "execution_failed"],
        )
        validate_execution_failed(events[4]["payload"])
        self.assertEqual(client.tables["commands"][0]["status"], "failed")
        self.assertEqual(result.stop, "confirmed")

    def test_the_dry_run_worker_is_unchanged_next_to_it(self) -> None:
        client = FakeClient(commands=[command_row(VALID_TEXT, claimed_by=None, claimed_at=None)])
        critic = self.calls.review_plan.side_effect
        self._stop_patchers()
        command_processor.process_one_command(WORKER_ID, client=client, critic=critic)
        payloads = {e["event_type"]: e["payload"] for e in client.tables["audit_events"]}
        self.assertEqual(payloads["execution_started"], {"dryRun": True})
        self.assertEqual(set(payloads["execution_completed"]), {"dryRun", "summary"})
        self.assertIs(payloads["execution_completed"]["dryRun"], True)
        self.assert_twin_untouched()


# --------------------------------------------------------------------------
# Network and SDK isolation
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Interruptions and process supervision in the audit lifecycle (A2)
# --------------------------------------------------------------------------


class SigtermTests(SimulationTestCase):
    """SIGTERM while main() runs: SystemExit(143) through the recorded-failure path (B1).

    ``signal.raise_signal`` delivers a real SIGTERM to this process (also on
    Windows, where the C runtime raises it in-process), and the handler that
    ``main`` installed turns it into SystemExit in the main thread.
    """

    def setUp(self) -> None:
        super().setUp()
        self.original_handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, self.original_handler)

    def sigterm_on(self, *twin_calls: str) -> None:
        """Deliver one SIGTERM at the first occurrence of each named twin call."""
        pending = list(twin_calls)
        record = self.twin.recorder

        def recorder(name: str) -> None:
            record(name)
            if pending and name == pending[0]:
                pending.pop(0)
                signal.raise_signal(signal.SIGTERM)

        self.twin.recorder = recorder

    def run_main_terminated(self) -> tuple[SystemExit, str]:
        out = io.StringIO()
        with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                mock.patch.object(simulation_processor, "_production_twin",
                                  return_value=InProcessProvider(self.twin)), \
                redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--simulation", "--confirm-write"])
        return ctx.exception, out.getvalue()

    def failed_event(self) -> dict[str, Any]:
        failed = self.event("execution_failed")  # exactly one write attempt
        validate_execution_failed(failed)
        return failed

    def test_sigterm_after_go_is_recorded_through_the_failure_lifecycle(self) -> None:
        self.sigterm_on("move_joint")  # the first movement call, i.e. after GO
        raised, text = self.run_main_terminated()
        self.assertEqual(raised.code, simulation_processor.SIGTERM_EXIT_CODE)
        self.assertEqual(raised.code, 143)
        failed = self.failed_event()
        self.assertEqual((failed["error"]["kind"], failed["stop"]), ("interrupted", "confirmed"))
        self.assertIn("SystemExit", failed["error"]["detail"])
        self.assertEqual(len(self.twin.moves()), 1)  # no return movement, nothing re-sent
        self.assertEqual(self.twin.names().count("stop"), 1)
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))
        order = self.order()
        self.assertLess(order.index("twin:stop"), order.index("execution_failed"))
        self.assertLess(order.index("execution_failed"), order.index("status:failed"))
        self.assertNotIn("execution_completed", self.event_types())
        self.assertIn("status failed", text)  # the outcome note is printed
        self.assertIn("movement sent yes", text)
        self.assertIs(signal.getsignal(signal.SIGTERM), self.original_handler)

    def test_sigterm_after_go_without_a_reliable_stop_says_the_outcome_is_unknown(self) -> None:
        self.twin.stop_result = StopResult(confirmed=False)
        self.sigterm_on("move_joint")
        raised, text = self.run_main_terminated()
        self.assertEqual(raised.code, 143)
        failed = self.failed_event()
        self.assertEqual((failed["error"]["kind"], failed["stop"]), ("interrupted", "unconfirmed"))
        self.assertTrue(failed["error"]["detail"].startswith(MANUAL_VERIFICATION_NOTICE))
        self.assertIn(MANUAL_VERIFICATION_NOTICE, text)
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))

    def test_a_second_sigterm_cannot_abandon_the_stop_or_the_recording(self) -> None:
        self.sigterm_on("move_joint", "stop")  # the second one arrives during the stop
        raised, _ = self.run_main_terminated()
        self.assertEqual(raised.code, 143)
        failed = self.failed_event()
        self.assertEqual(failed["stop"], "confirmed")  # not abandoned as 'unconfirmed'
        self.assertEqual(self.event_types().count("execution_failed"), 1)
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))

    def test_sigterm_before_any_movement_is_recorded_as_not_needed(self) -> None:
        self.sigterm_on("connect")
        raised, _ = self.run_main_terminated()
        self.assertEqual(raised.code, 143)
        failed = self.failed_event()
        self.assertEqual((failed["error"]["kind"], failed["stop"]), ("interrupted", "not_needed"))
        self.assert_nothing_sent()
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))

    def test_the_handler_is_only_active_while_main_runs(self) -> None:
        with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                mock.patch.object(simulation_processor, "_production_twin",
                                  return_value=InProcessProvider(self.twin)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--simulation", "--confirm-write"]), 0)
        self.assertIs(signal.getsignal(signal.SIGTERM), self.original_handler)
        # The library entry point installs nothing: only the command line does.
        seen: list[Any] = []
        self.reset_calls()
        self.twin.recorder = lambda name: seen.append(signal.getsignal(signal.SIGTERM))
        self.run_sim()
        self.assertTrue(seen)
        self.assertTrue(all(handler is self.original_handler for handler in seen))

    def test_blocked_runs_install_no_handler(self) -> None:
        with mock.patch.object(signal, "signal", wraps=signal.signal) as installed, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--simulation", "--confirm-write"]), 3)
        installed.assert_not_called()


class CustomStop(BaseException):
    """An unexpected BaseException that is neither Ctrl-C nor SystemExit."""


class InterruptionLifecycleTests(SimulationTestCase):
    def run_interrupted(self, exc_type: type[BaseException]) -> BaseException:
        with self.assertRaises(exc_type) as ctx:
            self.run_sim()
        return ctx.exception

    def failed_event(self) -> dict[str, Any]:
        failed = self.event("execution_failed")  # exactly one write attempt
        validate_execution_failed(failed)
        return failed

    def test_ctrl_c_before_execution_started_is_recorded_in_the_documented_order(self) -> None:
        interrupt = KeyboardInterrupt()
        with mock.patch.object(simulation_processor, "_preflight", side_effect=interrupt):
            raised = self.run_interrupted(KeyboardInterrupt)
        self.assertIs(raised, interrupt)
        failed = self.failed_event()
        self.assertEqual((failed["error"]["kind"], failed["stop"]), ("interrupted", "not_needed"))
        self.assertNotIn("execution_started", self.event_types())
        self.assertEqual(self.transitions()[-1], ("approved", "failed"))
        self.assertIn("status failed", " ".join(raised.__notes__))
        self.assert_twin_untouched()

    def test_ctrl_c_before_dispatch(self) -> None:
        self.twin.fail["connect"] = KeyboardInterrupt()
        self.run_interrupted(KeyboardInterrupt)
        failed = self.failed_event()
        self.assertEqual((failed["error"]["kind"], failed["stop"]), ("interrupted", "not_needed"))
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))
        self.assert_nothing_sent()
        order = self.order()
        self.assertLess(order.index("execution_failed"), order.index("status:failed"))

    def test_ctrl_c_after_dispatch_with_a_confirmed_stop(self) -> None:
        self.twin.fail_move_number[1] = KeyboardInterrupt()
        raised = self.run_interrupted(KeyboardInterrupt)
        failed = self.failed_event()
        self.assertEqual((failed["error"]["kind"], failed["stop"]), ("interrupted", "confirmed"))
        self.assertEqual(self.twin.names().count("stop"), 1)
        self.assertEqual(len(self.twin.moves()), 1)
        self.assertNotIn(MANUAL_VERIFICATION_NOTICE, failed["error"]["detail"])
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))
        order = self.order()
        self.assertLess(order.index("twin:stop"), order.index("execution_failed"))
        self.assertIn("movement sent yes", " ".join(raised.__notes__))

    def test_ctrl_c_after_dispatch_without_a_reliable_stop_says_the_outcome_is_unknown(self) -> None:
        self.twin.fail_move_number[1] = KeyboardInterrupt()
        self.twin.stop_result = StopResult(confirmed=False)
        raised = self.run_interrupted(KeyboardInterrupt)
        failed = self.failed_event()
        self.assertEqual(failed["stop"], "unconfirmed")
        self.assertTrue(failed["error"]["detail"].startswith(MANUAL_VERIFICATION_NOTICE))
        self.assertIn(MANUAL_VERIFICATION_NOTICE, " ".join(raised.__notes__))

    def test_system_exit_after_dispatch_keeps_its_code(self) -> None:
        exit_request = SystemExit(7)
        self.twin.fail_move_number[1] = exit_request
        raised = self.run_interrupted(SystemExit)
        self.assertIs(raised, exit_request)
        self.assertEqual(raised.code, 7)
        self.assertEqual(self.failed_event()["error"]["kind"], "interrupted")
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))

    def test_a_custom_base_exception_is_an_internal_error_and_continues(self) -> None:
        custom = CustomStop()
        self.twin.fail_move_number[1] = custom
        raised = self.run_interrupted(CustomStop)
        self.assertIs(raised, custom)
        failed = self.failed_event()
        self.assertEqual(failed["error"]["kind"], "internal_error")
        self.assertIn("CustomStop", failed["error"]["detail"])

    def test_a_failed_execution_failed_write_after_ctrl_c_needs_manual_recovery(self) -> None:
        self.twin.fail_move_number[1] = KeyboardInterrupt()
        self.store_side_effects(events={"execution_failed": WriteError("insert failed")})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        self.assertEqual(ctx.exception.stage, STAGE_EXECUTION_FAILED_WRITE)
        self.assertIsInstance(ctx.exception.__cause__, KeyboardInterrupt)
        self.assertTrue(ctx.exception.movement_dispatched)
        self.assertNotIn(("executing", "failed"), self.transitions())  # never claimed failed
        self.assertEqual(self.event_types().count("execution_failed"), 1)

    def test_a_failed_status_change_after_ctrl_c_needs_manual_recovery(self) -> None:
        self.twin.fail_move_number[1] = KeyboardInterrupt()
        self.store_side_effects(statuses={("executing", "failed"): StateConflictError("no row")})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        self.assertEqual(ctx.exception.stage, STAGE_FAILED_UPDATE)
        self.assertIsInstance(ctx.exception.__cause__, KeyboardInterrupt)
        self.assertEqual(self.event_types().count("execution_failed"), 1)  # not written again
        self.assertEqual(self.transitions().count(("executing", "failed")), 1)  # attempted once only

    def test_ctrl_c_during_an_audit_write_writes_nothing_further(self) -> None:
        self.store_side_effects(events={"execution_started": KeyboardInterrupt()})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        self.assertEqual(ctx.exception.stage, STAGE_INTERRUPTED_WRITE)
        self.assertIsInstance(ctx.exception.__cause__, KeyboardInterrupt)
        self.assertIsNone(ctx.exception.event_written)  # unknown
        self.assertNotIn("execution_failed", self.event_types())
        self.assertNotIn(("approved", "executing"), self.transitions())
        self.assert_twin_untouched()

    def test_ctrl_c_during_the_failed_status_change_writes_nothing_further(self) -> None:
        self.twin.ignore_moves = {1}
        self.store_side_effects(statuses={("executing", "failed"): KeyboardInterrupt()})
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        self.assertEqual(ctx.exception.stage, STAGE_INTERRUPTED_WRITE)
        self.assertEqual(self.event_types().count("execution_failed"), 1)
        self.assertTrue(ctx.exception.movement_dispatched)

    def test_a_twin_session_that_cannot_start_is_recorded_before_anything_is_sent(self) -> None:
        provider = InProcessProvider(self.twin)
        with mock.patch.object(provider, "start", side_effect=RuntimeError("spawn failed")):
            result = process_one_simulation_command(WORKER_ID, twin=provider, timeouts=FAST)
        self.assertEqual((result.execution, result.error_kind), ("failed", "connection_failed"))
        self.assertEqual(self.failed_event()["stop"], "not_needed")
        self.assertEqual(self.transitions()[-1], ("executing", "failed"))
        self.assert_twin_untouched()

    def test_a_twin_process_that_cannot_be_confirmed_stopped_is_reported_after_recording(self) -> None:
        provider = InProcessProvider(self.twin)
        real_start = provider.start

        def start_with_stuck_close() -> Any:
            session = real_start()
            session.close = mock.Mock(side_effect=SupervisionError("stuck"))
            return session

        with mock.patch.object(provider, "start", start_with_stuck_close):
            with self.assertRaises(SimulationProcessingError) as ctx:
                process_one_simulation_command(WORKER_ID, twin=provider, timeouts=FAST)
        self.assertEqual(ctx.exception.stage, STAGE_SESSION_CLEANUP)
        self.assertIn("execution_completed", self.event_types())  # the outcome was recorded first
        self.assertEqual(self.transitions()[-1], ("executing", "completed"))

    def test_raw_twins_are_not_session_providers(self) -> None:
        with self.assertRaises(SimulationBlockedError):
            process_one_simulation_command(WORKER_ID, twin=self.twin, timeouts=FAST)
        self.assertEqual(self.call_names(), [])

    def test_the_cli_reports_an_unknown_physical_outcome(self) -> None:
        self.twin.ignore_moves = {1}
        self.twin.stop_result = StopResult(confirmed=False)
        result = self.run_sim()
        self.assertTrue(result.manual_verification_required)
        self.assertTrue(result.error_detail.startswith(MANUAL_VERIFICATION_NOTICE))
        self.assertIn("PHYSICAL OUTCOME UNKNOWN", simulation_processor._report(result))
        self.assertIn("Manual verification required", simulation_processor._report(result))

    def test_the_cli_re_raises_ctrl_c_after_printing_what_was_recorded(self) -> None:
        interrupt = KeyboardInterrupt()
        interrupt.add_note("Simulation outcome recorded before the interruption continued: test")
        out = io.StringIO()
        with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                mock.patch.object(simulation_processor, "_production_twin", return_value=InProcessProvider(self.twin)), \
                mock.patch.object(simulation_processor, "process_one_simulation_command", side_effect=interrupt), \
                redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                main(["--simulation", "--confirm-write"])
        self.assertIs(ctx.exception, interrupt)
        self.assertIn("Simulation outcome recorded", out.getvalue())

    def test_the_cli_re_raises_ctrl_c_after_a_manual_recovery_message(self) -> None:
        interrupt = KeyboardInterrupt()
        error = SimulationProcessingError(
            "INTERRUPTED DURING A WRITE: test", command_id="c", stage=STAGE_INTERRUPTED_WRITE,
            event_written=None, movement_dispatched=False,
        )
        error.__cause__ = interrupt
        out = io.StringIO()
        with mock.patch.object(simulation_processor, "live_simulation_blockers", return_value=()), \
                mock.patch.object(simulation_processor, "_production_twin", return_value=InProcessProvider(self.twin)), \
                mock.patch.object(simulation_processor, "process_one_simulation_command", side_effect=error), \
                redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                main(["--simulation", "--confirm-write"])
        self.assertIn("FAILED at interrupted_write", out.getvalue())

    # -- recording windows: interruptions OUTSIDE an audit-store call -------

    def interrupted_recording(self) -> SimulationProcessingError:
        with self.assertRaises(SimulationProcessingError) as ctx:
            self.run_sim()
        error = ctx.exception
        self.assertEqual(error.stage, STAGE_INTERRUPTED_RECORDING)
        self.assertIsInstance(error.__cause__, KeyboardInterrupt)  # the original, kept
        self.assertIn("Manual recovery needed", str(error))
        self.assertIn("do NOT write a second execution_failed", str(error))
        return error

    def raise_after(self, method: str, value: str) -> Any:
        """Patch _Progress.<method> so it raises Ctrl-C right AFTER confirming ``value``."""
        original = getattr(simulation_processor._Progress, method)

        def patched(progress: Any, confirmed: str) -> None:
            original(progress, confirmed)
            if confirmed == value:
                raise KeyboardInterrupt

        return mock.patch.object(simulation_processor._Progress, method, patched)

    def test_window_while_building_the_execution_failed_payload(self) -> None:
        self.twin.ignore_moves = {1}  # an ordinary failure after dispatch
        self.twin.stop_result = StopResult(confirmed=False)  # so the outcome is unknown
        with mock.patch.object(simulation_processor, "build_execution_failed", side_effect=KeyboardInterrupt):
            error = self.interrupted_recording()
        self.assertIn("building the execution_failed payload (not written)", str(error))
        self.assertIn("last status confirmed by this run: 'executing'", str(error))
        self.assertIs(error.event_written, False)  # never claimed written
        self.assertNotIn("execution_failed", self.event_types())  # not even attempted
        self.assertNotIn(("executing", "failed"), self.transitions())
        self.assertIn(MANUAL_VERIFICATION_NOTICE, str(error))  # a movement may have been sent

    def test_window_after_execution_failed_was_written_before_the_status_change(self) -> None:
        self.twin.ignore_moves = {1}
        with self.raise_after("confirm", "execution_failed"):
            error = self.interrupted_recording()
        self.assertIn("after execution_failed was written", str(error))
        self.assertIs(error.event_written, True)
        self.assertEqual(self.event_types().count("execution_failed"), 1)  # no second attempt
        self.assertNotIn(("executing", "failed"), self.transitions())  # no status change
        self.assertIn("execution_failed", error.events_written)

    def test_window_after_the_failed_status_change(self) -> None:
        self.twin.ignore_moves = {1}
        with self.raise_after("confirm_status", "failed"):
            error = self.interrupted_recording()
        self.assertIn("last status confirmed by this run: 'failed'", str(error))
        self.assertEqual(self.transitions().count(("executing", "failed")), 1)
        self.assertEqual(self.event_types().count("execution_failed"), 1)

    def test_window_while_building_the_completion_record(self) -> None:
        with mock.patch.object(simulation_processor, "build_execution_completed", side_effect=KeyboardInterrupt):
            error = self.interrupted_recording()
        self.assertIn("building the execution_completed record (not written)", str(error))
        self.assertIn("verified the restoration", str(error))
        self.assertNotIn("execution_completed", self.event_types())
        self.assertNotIn("execution_failed", self.event_types())  # nothing invented instead
        self.assertEqual(self.transitions()[-1], ("approved", "executing"))

    def test_window_after_execution_completed_was_written_before_the_status_change(self) -> None:
        with self.raise_after("confirm", "execution_completed"):
            error = self.interrupted_recording()
        self.assertIs(error.event_written, True)
        self.assertEqual(self.event_types().count("execution_completed"), 1)
        self.assertNotIn(("executing", "completed"), self.transitions())
        self.assertNotIn("execution_failed", self.event_types())

    def test_window_after_execution_started_was_written_before_executing(self) -> None:
        with self.raise_after("confirm", "execution_started"):
            error = self.interrupted_recording()
        self.assertIn("last status confirmed by this run: 'approved'", str(error))
        self.assertNotIn(("approved", "executing"), self.transitions())
        self.assertNotIn("execution_failed", self.event_types())
        self.assert_twin_untouched()

    def test_window_while_recording_an_executing_conflict(self) -> None:
        self.store_side_effects(statuses={("approved", "executing"): StateConflictError("no row")})
        with mock.patch.object(simulation_processor, "build_execution_failed", side_effect=KeyboardInterrupt):
            error = self.interrupted_recording()
        self.assertIn("approved -> executing matched no row", str(error))
        self.assertNotIn("execution_failed", self.event_types())
        self.assert_twin_untouched()

    def test_window_while_recording_an_earlier_interruption(self) -> None:
        first = KeyboardInterrupt()
        with mock.patch.object(simulation_processor, "_preflight", side_effect=first), \
                mock.patch.object(simulation_processor, "build_execution_failed", side_effect=KeyboardInterrupt):
            error = self.interrupted_recording()
        self.assertIsNot(error.__cause__, first)  # the interruption of the recording
        self.assertIn("interrupted the recording of an earlier KeyboardInterrupt", str(error))
        self.assertNotIn("execution_failed", self.event_types())
        self.assertEqual(self.transitions()[-1], ("received", "approved"))

    def test_a_cleanup_failure_keeps_the_primary_failure_kind(self) -> None:
        self.twin.reported_twin_target = "live"  # not_simulation: nothing sent
        provider = InProcessProvider(self.twin)
        real_start = provider.start

        def start_with_stuck_close() -> Any:
            session = real_start()
            session.close = mock.Mock(side_effect=SupervisionError("stuck"))
            return session

        with mock.patch.object(provider, "start", start_with_stuck_close):
            with self.assertRaises(SimulationProcessingError) as ctx:
                process_one_simulation_command(WORKER_ID, twin=provider, timeouts=FAST)
        self.assertEqual(ctx.exception.stage, STAGE_SESSION_CLEANUP)
        self.assertEqual(ctx.exception.failure_kind, "not_simulation")
        self.assertEqual(self.failed_event()["error"]["kind"], "not_simulation")

    def test_ctrl_c_during_cleanup_with_a_stuck_child_keeps_both(self) -> None:
        provider = InProcessProvider(self.twin)
        real_start = provider.start
        interrupt = KeyboardInterrupt()

        def start_with_interrupted_close() -> Any:
            session = real_start()
            session.cleanup_failure = "the main twin process could not be confirmed stopped"
            session.close = mock.Mock(side_effect=interrupt)
            return session

        with mock.patch.object(provider, "start", start_with_interrupted_close):
            with self.assertRaises(SimulationProcessingError) as ctx:
                process_one_simulation_command(WORKER_ID, twin=provider, timeouts=FAST)
        self.assertEqual(ctx.exception.stage, STAGE_SESSION_CLEANUP)
        self.assertIs(ctx.exception.__cause__, interrupt)  # the interruption is not lost
        self.assertIn("could not be confirmed stopped", str(ctx.exception))
        self.assertIn("execution_completed", self.event_types())  # the outcome was recorded first

    def test_interrupted_is_on_the_payload_allowlist(self) -> None:
        validate_execution_failed(build_execution_failed("interrupted", "the run was interrupted", "not_needed"))


class IsolationTests(SimulationTestCase):
    FORBIDDEN_IMPORTS = {
        "cyberwave", "cyberwave_client", "discover_cyberwave_assets", "test_cyberwave_connection",
        "read_joint_telemetry", "subprocess", "socket", "httpx", "requests", "urllib", "os",
    }
    # SDK calls that move something or start a simulation.
    FORBIDDEN_CALLS = {"affect", "set_joints", "set_pose", "grip", "release", "run_movement"}

    def test_the_network_guard_is_installed(self) -> None:
        self.assertTrue(net_guard.is_installed())
        before = len(net_guard.BLOCKED_ATTEMPTS)
        self.run_sim()
        self.assertEqual(len(net_guard.BLOCKED_ATTEMPTS), before)  # nothing even tried

    def test_simulation_modules_do_not_import_the_sdk_or_any_network_library(self) -> None:
        for module in SIMULATION_MODULES:
            with self.subTest(module=module):
                forbidden = self.FORBIDDEN_IMPORTS - ({"os"} if module == OS_EXIT_MODULE else set())
                self.assertFalse(imports_of(module) & forbidden)

    def test_the_os_exception_is_only_os_exit_in_one_function(self) -> None:
        tree = source_tree(OS_EXIT_MODULE)
        uses = []
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                        uses.append((function.name, node.attr))
        self.assertEqual(sorted(set(uses)), [("_exit_now", "_exit")])
        all_os = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "os"]
        self.assertEqual(len(all_os), 1)  # the one os._exit call, nothing else
        self.assertEqual(imports_of(OS_EXIT_MODULE) & self.FORBIDDEN_IMPORTS, {"os"})
        # The child module imports only the standard library at module level:
        # spawn imports it in the child before the network guard can be installed.
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        self.assertEqual(
            top_level,
            {"__future__", "importlib", "multiprocessing", "os", "signal", "sys", "threading",
             "time", "typing"},
        )

    def test_no_worker_module_calls_affect_or_a_movement_method(self) -> None:
        modules = sorted(p.stem for p in WORKER_DIR.glob("*.py"))
        self.assertIn("simulation_executor", modules)
        self.assertIn("cyberwave_client", modules)
        for module in modules:
            tree = source_tree(module)
            called = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            with self.subTest(module=module):
                self.assertEqual(called & self.FORBIDDEN_CALLS, set())

    def test_the_fake_twin_cannot_start_a_simulation_or_reach_hardware(self) -> None:
        for name in ("affect", "set_joints", "client", "sdk_client", "mode", "target"):
            self.assertFalse(hasattr(self.twin, name), name)
        self.assertFalse(imports_of("simulation_executor") & {"cyberwave", "cyberwave_client"})
        fake_source = (WORKER_DIR / "tests" / "fake_twin.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(fake_source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
        self.assertEqual(
            imported,
            {"__future__", "threading", "time", "typing", "sim_telemetry",
             "simulation_executor", "simulation_gates"},
        )

    def test_a_full_run_never_touches_the_cyberwave_sdk(self) -> None:
        class Trap(types.ModuleType):
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"cyberwave.{name} was touched")

        with mock.patch.dict(sys.modules, {"cyberwave": Trap("cyberwave")}):
            self.assertEqual(self.run_sim().execution, "completed")
            self.reset_calls()
            self.twin.ignore_moves = {1}
            self.assertEqual(self.run_sim().execution, "failed")

    def test_importing_the_simulation_modules_loads_no_cyberwave_module(self) -> None:
        code = (
            "import sys, simulation_processor, simulation_executor, sim_telemetry, "
            "simulation_gates, simulation_payloads, simulation_supervisor, simulation_child; "
            "print(sorted(m for m in sys.modules if 'cyberwave' in m))"
        )
        out = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=WORKER_DIR, capture_output=True,
            text=True, timeout=60, check=True,
        )
        self.assertEqual(out.stdout.strip(), "[]")

    def test_the_default_dry_run_path_loads_no_cyberwave_or_simulation_module(self) -> None:
        code = (
            "import sys, command_processor; "
            "print(sorted(m for m in sys.modules if 'cyberwave' in m or 'simulation' in m "
            "or m == 'sim_telemetry'))"
        )
        out = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=WORKER_DIR, capture_output=True,
            text=True, timeout=60, check=True,
        )
        self.assertEqual(out.stdout.strip(), "[]")

    def test_no_credentials_or_endpoints_in_the_simulation_modules(self) -> None:
        for module in SIMULATION_MODULES + ("read_joint_telemetry",):
            source = (WORKER_DIR / f"{module}.py").read_text(encoding="utf-8")
            with self.subTest(module=module):
                for fragment in ("http://", "https://", "api_key=", "sb_secret", "mqtt://"):
                    self.assertNotIn(fragment, source)
        for module in SIMULATION_MODULES:
            self.assertNotIn("load_cyberwave_settings", (WORKER_DIR / f"{module}.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
