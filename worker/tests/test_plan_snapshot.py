"""Unit tests for plan_snapshot. Pure and offline."""

from __future__ import annotations

import copy
import dataclasses
import math
import unittest
import uuid
from typing import Any

from plan_hasher import PlanHashError, canonicalize_actions, hash_actions
from plan_snapshot import PlanSnapshot, new_plan_id
from planner import plan_command

COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"


def planner_actions(text: str = "set joint _1 to 0.25 rad; set joint _6 to -1.0 rad") -> list[dict[str, Any]]:
    result = plan_command(text)
    assert result.actions is not None
    return list(result.actions)


class SnapshotTests(unittest.TestCase):
    def test_hash_is_hash_actions_of_the_original_plan(self) -> None:
        original = planner_actions()
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, original)
        self.assertEqual(snapshot.plan_hash, hash_actions(original))
        self.assertEqual(snapshot.canonical_actions, canonicalize_actions(original))
        self.assertEqual(snapshot.action_count, 2)

    def test_mutating_the_planner_dicts_does_not_change_the_snapshot(self) -> None:
        original = planner_actions()
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, original)
        before = (snapshot.plan_hash, snapshot.actions())
        original[0]["value"] = 99.0
        original[1]["target"] = "_9"
        original.append({"actionId": "a9"})
        self.assertEqual((snapshot.plan_hash, snapshot.actions()), before)
        self.assertNotEqual(hash_actions(original), snapshot.plan_hash)

    def test_every_copy_is_independent(self) -> None:
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, planner_actions())
        first, second = snapshot.actions(), snapshot.actions()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first[0], second[0])
        first[0]["value"] = 5
        first.clear()
        self.assertEqual(snapshot.actions(), second)
        self.assertEqual(hash_actions(snapshot.actions()), snapshot.plan_hash)

    def test_snapshot_fields_cannot_be_reassigned(self) -> None:
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, planner_actions())
        for name in ("plan_hash", "plan_id", "canonical_actions", "command_id"):
            with self.subTest(field=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(snapshot, name, "x")
        self.assertIsInstance(snapshot.canonical_actions, bytes)

    def test_critic_request_uses_the_snapshot_ids_and_hash(self) -> None:
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, planner_actions())
        request = snapshot.critic_request()
        self.assertEqual(request.command_id, COMMAND_ID)
        self.assertEqual(request.plan_id, PLAN_ID)
        self.assertEqual(request.plan_hash, snapshot.plan_hash)
        self.assertEqual(request.actions, snapshot.actions())
        request.actions[0]["value"] = 42
        self.assertNotEqual(snapshot.actions()[0]["value"], 42)

    def test_plan_payload_matches_the_agreed_shape(self) -> None:
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, planner_actions())
        payload = snapshot.plan_payload()
        self.assertEqual(set(payload), {"plan", "planId", "planHash"})
        self.assertEqual(set(payload["plan"]), {"commandId", "planId", "planHash", "actions"})
        self.assertEqual(payload["planId"], PLAN_ID)
        self.assertEqual(payload["planHash"], snapshot.plan_hash)
        self.assertEqual(payload["plan"]["planHash"], snapshot.plan_hash)
        self.assertEqual(payload["plan"]["actions"], snapshot.actions())
        self.assertEqual(hash_actions(payload["plan"]["actions"]), snapshot.plan_hash)

    def test_numbers_keep_their_value_through_the_canonical_form(self) -> None:
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, planner_actions())
        values = [a["value"] for a in snapshot.actions()]
        self.assertEqual(values, [0.25, -1])

    def test_unhashable_actions_and_bad_ids_are_refused(self) -> None:
        bad_value = planner_actions("set joint _1 to 0.1 rad")
        bad_value[0]["value"] = math.nan
        for args in (
            (COMMAND_ID, PLAN_ID, bad_value),
            (COMMAND_ID, PLAN_ID, "not a list"),
            (COMMAND_ID, PLAN_ID, [{"x": object()}]),
        ):
            with self.subTest(args=repr(args)[:60]), self.assertRaises(PlanHashError):
                PlanSnapshot.create(*args)  # type: ignore[arg-type]
        for command_id, plan_id in (("", PLAN_ID), (COMMAND_ID, " "), (None, PLAN_ID)):
            with self.assertRaises(ValueError):
                PlanSnapshot.create(command_id, plan_id, planner_actions())  # type: ignore[arg-type]

    def test_repr_does_not_dump_the_actions(self) -> None:
        snapshot = PlanSnapshot.create(COMMAND_ID, PLAN_ID, planner_actions())
        self.assertNotIn("canonical_actions", repr(snapshot))

    def test_new_plan_id_is_a_fresh_uuid(self) -> None:
        first, second = new_plan_id(), new_plan_id()
        self.assertNotEqual(first, second)
        self.assertEqual(str(uuid.UUID(first)), first)

    def test_original_is_not_modified_by_creating_a_snapshot(self) -> None:
        original = planner_actions()
        before = copy.deepcopy(original)
        PlanSnapshot.create(COMMAND_ID, PLAN_ID, original)
        self.assertEqual(original, before)


if __name__ == "__main__":
    unittest.main()
