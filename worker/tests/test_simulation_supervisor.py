"""Process supervision (simulation_supervisor.py), with REAL child processes.

Offline: the twin is the fake, built in each child after the network guard was
installed there. Tests cover the dispatch handshake, timeouts and termination,
kill escalation, the standby (fresh-session) stop and cleanup. Every test checks
that no child process is left running. A fake twin shows only how the worker
handles these situations, not how a real Cyberwave twin behaves.
"""

from __future__ import annotations

import multiprocessing
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import simulation_child
import simulation_supervisor as sv
from simulation_executor import StopResult
from simulation_supervisor import (
    TERMINATE_LIMIT_S,
    ProcessSessionProvider,
    ProcessTwinSession,
    SessionCrashed,
    SessionRefused,
    SessionStartError,
    SessionTimeout,
    SupervisionError,
    terminate_process,
)
from tests.fake_twin import ALL_CONFIRMED, HANG, FakeTwin
from tests.fake_twin_child import CRASH_EXIT_CODE, open_process_session, spec_for
from tests.test_simulation_executor import assert_no_child_processes
from tests.test_simulation_gates import use_canonical_gates


def soon(seconds: float = 5.0) -> float:
    return time.monotonic() + seconds


class SupervisorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(assert_no_child_processes, self)  # runs last
        use_canonical_gates(self, ALL_CONFIRMED)
        self.twin = FakeTwin()

    def session(self, **options: Any) -> ProcessTwinSession:
        return open_process_session(self, self.twin, **options)


class HandshakeTests(SupervisorCase):
    def test_the_acknowledgement_comes_before_the_child_may_move(self) -> None:
        session = self.session()
        seen_at_ack: list[list[str]] = []
        session.dispatch_move("_1", 0.25, soon(), lambda: seen_at_ack.append(self.twin.names()))
        self.assertEqual(seen_at_ack, [[]])  # nothing moved when the parent recorded it
        self.assertEqual(self.twin.moves(), [("_1", 0.25)])

    def test_a_third_movement_is_refused_and_never_acknowledged(self) -> None:
        session = self.session()
        acks: list[int] = []
        session.dispatch_move("_1", 0.25, soon(), lambda: acks.append(1))
        session.dispatch_move("_1", 0.1, soon(), lambda: acks.append(2))
        with self.assertRaises(SessionRefused) as ctx:
            session.dispatch_move("_1", 0.2, soon(), lambda: acks.append(3))
        self.assertEqual(ctx.exception.reason, "movement_limit_reached")
        self.assertEqual(acks, [1, 2])
        self.assertEqual(len(self.twin.moves()), 2)

    def test_go_without_an_intent_is_refused(self) -> None:
        session = self.session()
        session._send(session.main, ("go", 900, "not-a-real-nonce"))
        self.assertEqual(session._reply(session.main, 900, soon()), ("refused", 900, "no_pending_intent"))
        self.assertEqual(self.twin.moves(), [])

    def test_a_nonce_cannot_be_used_twice(self) -> None:
        session = self.session()
        session._send(session.main, ("prepare", 901, "n1", "_1", 0.25))
        self.assertEqual(session._reply(session.main, 901, soon())[0], "intent")
        session._send(session.main, ("go", 902, "n1"))
        self.assertEqual(session._reply(session.main, 902, soon())[0], "moved")
        session._send(session.main, ("prepare", 903, "n1", "_1", 0.1))
        self.assertEqual(session._reply(session.main, 903, soon()), ("refused", 903, "nonce_reused"))
        session._send(session.main, ("go", 904, "n1"))
        self.assertEqual(session._reply(session.main, 904, soon())[2], "no_pending_intent")
        self.assertEqual(len(self.twin.moves()), 1)

    def test_no_movement_after_a_stop(self) -> None:
        session = self.session()
        session.call_with_gates("stop", (1.0,), soon())
        with self.assertRaises(SessionRefused) as ctx:
            session.dispatch_move("_1", 0.25, soon(), lambda: self.fail("acknowledged"))
        self.assertEqual(ctx.exception.reason, "stop_already_called")

    def test_the_child_refuses_a_second_stop(self) -> None:
        session = self.session()
        session.call_with_gates("stop", (1.0,), soon())
        with self.assertRaises(SessionRefused):
            session.call_with_gates("stop", (1.0,), soon())
        self.assertEqual(self.twin.names().count("stop"), 1)

    def test_a_child_that_died_before_intent_was_never_acknowledged(self) -> None:
        session = self.session(fresh_session_stop="unsupported")
        session.main.process.kill()
        session.main.process.join(10)
        acks: list[bool] = []
        with self.assertRaises((SessionCrashed, sv.SessionUnusable)):
            session.dispatch_move("_1", 0.25, soon(), lambda: acks.append(True))
        self.assertEqual(acks, [])  # nothing may have been sent
        self.assertEqual(self.twin.moves(), [])

    def test_a_child_that_died_after_intent_counts_as_possibly_sent(self) -> None:
        session = self.session(fresh_session_stop="unsupported")
        acks: list[bool] = []

        def acknowledged() -> None:
            acks.append(True)  # recorded FIRST, then the child dies before GO
            session.main.process.kill()
            session.main.process.join(10)

        with self.assertRaises((SessionCrashed, sv.SessionUnusable)):
            session.dispatch_move("_1", 0.25, soon(), acknowledged)
        self.assertEqual(acks, [True])
        self.assertFalse(session.healthy)

    def test_a_child_that_crashes_inside_move_joint(self) -> None:
        session = self.session(fresh_session_stop="unsupported", crash_in=["move_joint"])
        acks: list[bool] = []
        with self.assertRaises(SessionCrashed):
            session.dispatch_move("_1", 0.25, soon(), lambda: acks.append(True))
        self.assertEqual(acks, [True])
        self.assertEqual(self.twin.moves(), [("_1", 0.25)])  # the call started: may have been sent
        self.assertEqual(session.main.exitcode, CRASH_EXIT_CODE)


class TerminationTests(SupervisorCase):
    def test_a_timeout_terminates_the_child_and_hung_work_never_resumes(self) -> None:
        marker = Path(tempfile.mkdtemp()) / "resumed.txt"
        self.twin.fail["connect"] = HANG
        self.twin.hang_s = 1.0
        self.twin.resume_marker = str(marker)
        session = self.session(fresh_session_stop="unsupported")
        started = time.monotonic()
        with self.assertRaises(SessionTimeout):
            session.call("connect", (5.0,), time.monotonic() + 0.3)
        self.assertLess(time.monotonic() - started, 0.3 + TERMINATE_LIMIT_S + 0.2)
        self.assertTrue(session.main.dead)
        self.assertTrue(session.main.closed)  # exit verified, pipe and handle released
        self.assertIsNotNone(session.main.exitcode)
        time.sleep(1.5)  # longer than the hang: a surviving child would write the marker
        self.assertFalse(marker.exists(), "the hung twin call resumed after termination")
        with self.assertRaises(sv.SessionUnusable):
            session.call("runtime_target", (), soon())

    def test_termination_escalates_to_kill_within_one_budget(self) -> None:
        class Stubborn:
            """Ignores terminate(); dies on kill()."""

            def __init__(self) -> None:
                self.alive, self.killed, self.exitcode = True, False, None

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                self.killed, self.alive, self.exitcode = True, False, -9

            def join(self, timeout: float) -> None:
                time.sleep(min(timeout, 0.05))

            def is_alive(self) -> bool:
                return self.alive

        stubborn = Stubborn()
        self.assertTrue(terminate_process(stubborn, 0.5))
        self.assertTrue(stubborn.killed)

    def test_a_process_that_will_not_die_is_reported_within_the_budget(self) -> None:
        class Immortal:
            exitcode = None

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

            def join(self, timeout: float) -> None:
                time.sleep(timeout)

            def is_alive(self) -> bool:
                return True

        started = time.monotonic()
        self.assertFalse(terminate_process(Immortal(), 0.3))
        self.assertLess(time.monotonic() - started, 0.3 + 0.1)  # one total budget
        session = ProcessTwinSession(spec_for(self.twin))
        child = sv._Child("main", Immortal(), mock.Mock())
        # A request path never raises SupervisionError in place of its own error:
        # the failure is recorded as diagnostic information instead.
        self.assertFalse(session._terminate(child))
        self.assertEqual(session.cleanup_failure, "the main twin process could not be confirmed stopped")

    @unittest.skipIf(sys.platform == "win32", "on Windows kill() is the same TerminateProcess call as terminate()")
    def test_real_kill_escalation_on_posix(self) -> None:  # pragma: no cover - not run on Windows
        session = self.session(fresh_session_stop="unsupported", ignore_sigterm=True)
        process = session.main.process
        self.assertTrue(terminate_process(process, 2.0))
        self.assertEqual(process.exitcode, -9)

    def test_close_finishes_every_child_even_if_ctrl_c_arrives(self) -> None:
        session = self.session()  # main and standby
        main_process = session.main.process
        real_join = main_process.join
        calls = {"n": 0}

        def interrupted_join(timeout: float | None = None) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            real_join(timeout)

        with mock.patch.object(main_process, "join", interrupted_join):
            with self.assertRaises(KeyboardInterrupt):
                session.close()
        for child in session.children:
            with self.subTest(role=child.role):
                self.assertTrue(child.closed)
                self.assertIsNotNone(child.exitcode)

    def test_close_is_idempotent_and_releases_everything(self) -> None:
        session = self.session()
        session.close()
        session.close()
        for child in session.children:
            self.assertTrue(child.dead and child.closed)

    def test_a_start_up_failure_leaves_nothing_behind(self) -> None:
        session = ProcessTwinSession(spec_for(self.twin), start_limit_s=0.05)
        with self.assertRaises(SessionStartError):
            session.start()
        for child in session.children:
            self.assertTrue(child.closed)

    def test_ctrl_c_while_a_request_is_in_flight_ends_the_child(self) -> None:
        self.twin.fail["connect"] = HANG
        session = self.session(fresh_session_stop="unsupported")

        def interrupt(name: str) -> None:
            if name == "connect":
                raise KeyboardInterrupt

        self.twin.recorder = interrupt
        with self.assertRaises(KeyboardInterrupt):
            session.call("connect", (5.0,), soon())
        self.assertTrue(session.main.dead and session.main.closed)


class Immortal:
    """A process stand-in that never exits; ``join`` waits its timeout."""

    exitcode = None

    def __init__(self, interrupt_first_join: bool = False) -> None:
        self.interrupt_first_join = interrupt_first_join

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        if self.interrupt_first_join:
            self.interrupt_first_join = False
            raise KeyboardInterrupt
        time.sleep(timeout or 0.0)

    def is_alive(self) -> bool:
        return True


def stuck_session(*processes: Any) -> ProcessTwinSession:
    """A session whose children are stand-ins that cannot be stopped."""
    session = ProcessTwinSession(spec_for(FakeTwin()))
    session.main = sv._Child("main", processes[0], mock.Mock())
    if len(processes) > 1:
        session.standby = sv._Child("standby", processes[1], mock.Mock())
    session._started = True
    return session


class ProtocolTests(SupervisorCase):
    def test_ctrl_c_during_a_send_makes_the_session_unusable(self) -> None:
        session = self.session(fresh_session_stop="unsupported")
        connection_type = type(session.main.conn)
        with mock.patch.object(connection_type, "send", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                session.call("runtime_target", (), soon())
        self.assertTrue(session.main.dead and session.main.closed)
        self.assertFalse(session.healthy)
        with self.assertRaises(sv.SessionUnusable):  # nothing goes through the uncertain channel
            session.call("twin_target", (), soon())
        self.assertEqual(self.twin.calls, [])

    def test_an_out_of_order_reply_ends_the_child(self) -> None:
        session = self.session(fresh_session_stop="unsupported")
        session._send(session.main, ("call", 70, "runtime_target", ()))
        with self.assertRaises(SessionCrashed):
            session._reply(session.main, 71, soon())
        self.assertTrue(session.main.dead and session.main.closed)
        with self.assertRaises(sv.SessionUnusable):
            session.call("twin_target", (), soon())

    def test_an_unpicklable_result_is_a_twin_error_not_a_crash(self) -> None:
        session = self.session(fresh_session_stop="unsupported", unpicklable_runtime_target=True)
        with self.assertRaises(sv.TwinCallFailed) as ctx:
            session.call("runtime_target", (), soon())
        self.assertEqual(ctx.exception.class_name, "UnsendableResult")
        self.assertTrue(session.healthy)  # the channel was not used: the next call works
        self.assertEqual(session.call("twin_target", (), soon()), "simulation")

    def test_a_protocol_violation_ends_the_child(self) -> None:
        for message in (("bogus",), "not a tuple", ()):
            with self.subTest(message=repr(message)):
                self.twin = FakeTwin()
                session = self.session(fresh_session_stop="unsupported")
                session.main.conn.send(message)
                with self.assertRaises(SessionCrashed):
                    session.call("runtime_target", (), soon())
                self.assertEqual(session.main.exitcode, simulation_child.EXIT_PROTOCOL)
                self.assertEqual(self.twin.calls, [])
                session.close()


class BudgetTests(SupervisorCase):
    def test_a_second_termination_attempt_gets_no_new_budget(self) -> None:
        session = stuck_session(Immortal())
        started = time.monotonic()
        self.assertFalse(session._terminate(session.main))
        first = time.monotonic() - started
        started = time.monotonic()
        self.assertFalse(session._terminate(session.main))
        second = time.monotonic() - started
        self.assertLess(first, TERMINATE_LIMIT_S + 0.15)
        self.assertLess(second, 0.1)  # the same absolute deadline, already passed

    def test_close_has_one_total_budget_even_with_stuck_children(self) -> None:
        session = stuck_session(Immortal(), Immortal())
        started = time.monotonic()
        with self.assertRaises(SupervisionError):
            session.close(limit_s=0.6)
        self.assertLess(time.monotonic() - started, 0.6 + 0.15)
        self.assertIn("main, standby", session.cleanup_failure)

    def test_a_stuck_child_and_ctrl_c_during_close_keep_the_interruption(self) -> None:
        session = stuck_session(Immortal(interrupt_first_join=True))
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt) as ctx:  # NOT hidden behind SupervisionError
            session.close(limit_s=0.6)
        self.assertLess(time.monotonic() - started, 0.6 + 0.15)
        notes = " ".join(getattr(ctx.exception, "__notes__", []))
        self.assertIn("could not be confirmed stopped", notes)
        self.assertIsNotNone(session.cleanup_failure)

    def test_a_timeout_whose_child_cannot_be_stopped_keeps_its_own_error(self) -> None:
        self.twin.fail["connect"] = HANG
        session = self.session(fresh_session_stop="unsupported")
        with mock.patch.object(sv, "terminate_process", return_value=False):
            with self.assertRaises(SessionTimeout):  # the primary error, not SupervisionError
                session.call("connect", (5.0,), time.monotonic() + 0.3)
        self.assertEqual(session.cleanup_failure, "the main twin process could not be confirmed stopped")
        session.close()  # the real termination, within the SAME termination deadline
        self.assertTrue(session.main.closed)
        self.assertIsNone(session.cleanup_failure)


class StandbyTests(SupervisorCase):
    def test_a_standby_child_exists_only_for_best_effort(self) -> None:
        self.assertIsNone(self.session(fresh_session_stop="unsupported").standby)
        self.twin = FakeTwin()
        session = self.session(fresh_session_stop="best_effort")
        self.assertIsNotNone(session.standby)
        self.assertEqual([c.role for c in session.children], ["main", "standby"])

    def test_the_standby_stop_is_one_call_from_a_fresh_session(self) -> None:
        session = self.session()
        attempt = session.standby_stop(soon())
        self.assertTrue(attempt.called and attempt.fresh_session)
        self.assertEqual(attempt.value, StopResult(confirmed=True))  # what the fake said: NOT a confirmation
        self.assertEqual(self.twin.names(), ["connect", "stop"])  # the standby's own session

    def test_unsupported_means_no_fresh_session_stop(self) -> None:
        session = self.session(fresh_session_stop="unsupported")
        self.assertFalse(session.standby_stop(soon()).called)
        self.assertEqual(self.twin.calls, [])

    def test_the_standby_shuts_down_with_the_session(self) -> None:
        session = self.session()
        standby = session.standby
        session.close()
        self.assertTrue(standby.dead and standby.closed)
        self.assertIsNotNone(standby.exitcode)


class ProviderHandOverTests(SupervisorCase):
    def test_the_session_is_handed_over_before_any_child_is_spawned(self) -> None:
        provider = ProcessSessionProvider(spec_for(self.twin, fresh_session_stop="unsupported"))
        seen: list[Any] = []

        def on_created(session: Any) -> None:
            self.assertIsNone(session.main)  # nothing spawned yet
            seen.append(session)

        with mock.patch.object(ProcessTwinSession, "_spawn", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                provider.start(on_created=on_created)
        self.assertEqual(len(seen), 1)  # the caller held the session when the interrupt hit
        seen[0].close()  # safe on a session that never finished starting
        self.assertEqual(multiprocessing.active_children(), [])

    def test_start_without_a_callback_still_works(self) -> None:
        provider = ProcessSessionProvider(spec_for(self.twin, fresh_session_stop="unsupported"))
        session = provider.start()
        self.addCleanup(session.close)
        self.assertTrue(session.healthy)


if __name__ == "__main__":
    unittest.main()
