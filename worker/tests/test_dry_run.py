"""Unit tests for dry_run.run_dry_run. Pure and offline."""

from __future__ import annotations

import dataclasses
import unittest

from dry_run import MAX_SUMMARY_LENGTH, DryRunError, run_dry_run
from plan_snapshot import PlanSnapshot
from planner import plan_command

COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"


def snapshot(text: str = "set joint _1 to 0.25 rad; set joint _6 to 0.5 rad") -> PlanSnapshot:
    plan = plan_command(text)
    assert plan.actions is not None
    return PlanSnapshot.create(COMMAND_ID, PLAN_ID, plan.actions)


class DryRunTests(unittest.TestCase):
    def test_summary_describes_the_plan_and_sends_nothing(self) -> None:
        summary = run_dry_run(snapshot())
        self.assertTrue(summary.startswith("Dry run only, nothing sent to a robot."))
        self.assertIn("a1: _1 = 0.25 rad", summary)
        self.assertIn("a2: _6 = 0.5 rad", summary)
        self.assertLessEqual(len(summary), MAX_SUMMARY_LENGTH)

    def test_summary_is_capped(self) -> None:
        text = "; ".join(f"set joint _{i} to 0.1 rad" for i in range(1, 7))
        self.assertLessEqual(len(run_dry_run(snapshot(text))), MAX_SUMMARY_LENGTH)

    def test_refuses_anything_but_a_snapshot(self) -> None:
        for value in (None, "set joint _1 to 0.25 rad", [{"actionId": "a1"}], {}):
            with self.subTest(value=value), self.assertRaises(DryRunError):
                run_dry_run(value)  # type: ignore[arg-type]

    def test_refuses_a_snapshot_that_does_not_match_its_hash(self) -> None:
        good = snapshot()
        tampered = dataclasses.replace(good, plan_hash="sha256:" + "0" * 64)
        with self.assertRaises(DryRunError):
            run_dry_run(tampered)
        miscounted = dataclasses.replace(good, action_count=5)
        with self.assertRaises(DryRunError):
            run_dry_run(miscounted)

    def test_snapshot_is_not_changed(self) -> None:
        s = snapshot()
        before = (s.plan_hash, s.canonical_actions)
        run_dry_run(s)
        self.assertEqual((s.plan_hash, s.canonical_actions), before)


if __name__ == "__main__":
    unittest.main()
