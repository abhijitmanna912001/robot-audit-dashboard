"""Offline dry run of an approved plan. It sends nothing anywhere.

``run_dry_run(snapshot)`` is the ONLY thing that happens between
``execution_started`` and ``execution_completed``. It does not move, simulate
or contact anything: there is no robot, Cyberwave, simulation, network,
database, file or environment access here, and no setting, flag or
environment variable can make it do more. It is not an executor interface and
nothing can be plugged into it.

What it does: takes the immutable ``PlanSnapshot`` that the validator checked
and the critic approved, re-hashes a fresh copy of its actions to confirm it
is still exactly the approved plan (same ``planHash``, same action count), and
returns a short human-readable summary of what WOULD have been sent. The
command text is never read or parsed again.

Raises ``DryRunError`` if the snapshot is not a ``PlanSnapshot`` or no longer
matches its own hash. Any exception counts as a failed dry run for the caller.
"""

from __future__ import annotations

from plan_hasher import hash_actions
from plan_snapshot import PlanSnapshot

__all__ = ["MAX_SUMMARY_LENGTH", "DryRunError", "run_dry_run"]

# Same cap as other free text in audit payloads.
MAX_SUMMARY_LENGTH = 200


class DryRunError(Exception):
    """The dry run refused the plan. Nothing was sent anywhere either way."""


def run_dry_run(snapshot: PlanSnapshot) -> str:
    """Check the approved snapshot and describe it. Pure; sends nothing."""
    if not isinstance(snapshot, PlanSnapshot):
        raise DryRunError("the dry run needs the approved plan snapshot")
    actions = snapshot.actions()
    if hash_actions(actions) != snapshot.plan_hash:
        raise DryRunError("the plan no longer matches its approved hash")
    if len(actions) != snapshot.action_count or not actions:
        raise DryRunError("the plan's action count does not match the approved plan")

    steps = "; ".join(
        f"{action['actionId']}: {action['target']} = {action['value']} {action['unit']}"
        for action in actions
    )
    summary = f"Dry run only, nothing sent to a robot. Would set {steps}."
    return summary[:MAX_SUMMARY_LENGTH]
