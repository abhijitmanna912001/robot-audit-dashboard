"""An immutable snapshot of one generated plan.

The planner returns action dicts, which are mutable. A frozen dataclass that
holds them would still let anyone change an action after it was hashed, so the
validator, the critic and the audit event could each see a different plan.

The snapshot keeps the actions ONLY as their RFC 8785 canonical bytes
(``plan_hasher.canonicalize_actions``). ``bytes`` cannot be changed, so the
snapshot is immutable all the way down. Every consumer gets its own freshly
decoded copy from ``actions()``: changing one copy cannot affect another copy,
the hash, or the snapshot.

The hash is ``plan_hasher.hash_actions`` of the decoded canonical actions,
which by construction is the hash of exactly the bytes held. Creation also
checks that decoding and re-canonicalizing gives the same bytes, so what is
handed out is what was hashed.

Pure: no network, no database, no robot.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from critic_gate import CriticRequest, build_critic_request
from plan_hasher import PlanHashError, canonicalize_actions, hash_actions

__all__ = ["PlanSnapshot", "new_plan_id"]


def new_plan_id() -> str:
    """A new plan id: a random UUID (docs/schema-draft.md, ``plan_id``)."""
    return str(uuid.uuid4())


def _reject_constant(name: str) -> Any:
    raise PlanHashError(f"non-standard JSON constant {name}")


def _decode(canonical: bytes) -> list[Any]:
    value = json.loads(canonical.decode("utf-8"), parse_constant=_reject_constant)
    if not isinstance(value, list):
        raise PlanHashError("canonical actions did not decode to an array")
    return value


@dataclass(frozen=True)
class PlanSnapshot:
    """One plan, fixed at creation. Build it with ``PlanSnapshot.create``."""

    command_id: str
    plan_id: str
    plan_hash: str
    action_count: int
    # RFC 8785 canonical UTF-8 bytes of the actions array. Immutable.
    canonical_actions: bytes = field(repr=False)

    @classmethod
    def create(
        cls, command_id: str, plan_id: str, actions: Sequence[Any]
    ) -> PlanSnapshot:
        """Freeze ``actions``. Raises PlanHashError or ValueError if they cannot be."""
        for name, value in (("command_id", command_id), ("plan_id", plan_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string.")
        canonical = canonicalize_actions(actions)
        decoded = _decode(canonical)
        # Round trip: the decoded copy must canonicalize to the same bytes.
        if canonicalize_actions(decoded) != canonical:
            raise PlanHashError("actions do not survive a canonical round trip")
        return cls(
            command_id=command_id,
            plan_id=plan_id,
            plan_hash=hash_actions(decoded),
            action_count=len(decoded),
            canonical_actions=canonical,
        )

    def actions(self) -> list[Any]:
        """A new, independent copy of the actions. Safe to change."""
        return _decode(self.canonical_actions)

    def critic_request(self) -> CriticRequest:
        """The critic request for this plan, built from its own copy.

        ``build_critic_request`` hashes that copy again; a different hash would
        mean the snapshot is broken, so it raises instead of sending it.
        """
        request = build_critic_request(self.command_id, self.plan_id, self.actions())
        if request.plan_hash != self.plan_hash:
            raise PlanHashError("critic request hash differs from the snapshot hash")
        return request

    def plan_payload(self) -> dict[str, Any]:
        """The ``plan_generated`` success payload (docs/schema-draft.md, "Event payloads").

        ``{"plan": <planner output>, "planId", "planHash"}``, where the planner
        output is the critic request body. No ``outcome`` and no ``error``.
        """
        return {
            "plan": {
                "commandId": self.command_id,
                "planId": self.plan_id,
                "planHash": self.plan_hash,
                "actions": self.actions(),
            },
            "planId": self.plan_id,
            "planHash": self.plan_hash,
        }
