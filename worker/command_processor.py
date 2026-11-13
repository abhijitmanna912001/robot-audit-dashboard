"""Process ONE command: claim, plan, validate, ask the critic, set the status.

Not a loop and it does not poll: each call handles at most one command, for a
controlled rehearsal. It ends at ``approved``, ``rejected`` or ``failed``.
NOTHING is executed: an approved plan stays ``approved`` (no
``execution_started``, no dry run, no Cyberwave, no movement).

What one call does
------------------
1. ``audit_store.claim_next_command(worker_id)``. No eligible command: stop.
2. Plan ``row["text"]`` with the deterministic planner (``planner.py``).
3. Planner failure (unsupported or unparseable command), unchanged:
   a. append ``plan_generated`` with ``{"outcome": "error", "error": {kind, detail}}``
   b. ONLY if that succeeded, ``set_command_status(received -> failed)``.
4. Planner success:
   a. new ``planId`` (UUID), freeze the actions in a ``PlanSnapshot`` and hash
      them with ``plan_hasher.hash_actions``.
   b. append ``plan_generated`` ``{"plan", "planId", "planHash"}``. If that
      fails, stop: no check runs and the status is not touched.
   c. run the safety validator on one copy of the snapshot, and send another
      copy to the critic (ONE request, no retry). Both always run, whatever
      the other one said.
   d. append ``validator_result``, then ``critic_result``. Each is attempted
      even if the other insert failed; neither is ever retried.
   e. ONLY if every result event was written: one conditional status update
      ``received -> approved | rejected | failed`` (with ``plan_id``), chosen by
      ``decide_final_status``.

Status precedence (docs/schema-draft.md section 6)::

    validator fail OR critic reject        -> rejected (even if the other errored)
    otherwise validator error OR critic error -> failed
    validator pass AND critic approve      -> approved (and only then)

A validator "error" means ``validate_plan`` raised or returned something
unusable. It cannot happen with the current validator, which turns its own
internal errors into ``fail``. The contract has no ``validator_result`` shape
for it, so no ``validator_result`` event is written in that case (open
question in worker/README.md); the status still follows the table above.

Before ``approved`` is written, ``execution_gate.decide_execution`` re-checks
the snapshot's request against the critic decision (re-validates, re-hashes,
compares ids). If it disagrees, the critic outcome counts as ``error``. This
is a consistency check only; nothing is executed.

Stranded commands (read this before a rehearsal)
------------------------------------------------
The claim has no lease and no recovery (docs/schema-draft.md section 3). A
claimed command stays ``received`` and claimed, skipped by every later claim,
if any event insert or the final status update fails. ``CommandProcessingError``
carries the command id, the plan id (when there is one), the failed ``stage``,
whether that stage's event was written, and which events were written.
Nothing is retried automatically: ``audit_events`` is append-only, so a blind
retry after an ambiguous failure could write a duplicate event that can never
be removed. The manual steps are in worker/README.md, "Manual recovery".

Before a rehearsal, check in the dashboard that no other ``received`` command
is waiting: the claim takes the OLDEST eligible command, which may not be the
one just typed.

Run from ``worker/`` (writes to Supabase and calls the critic, so it needs
``--confirm-write``)::

    .venv\\Scripts\\python.exe command_processor.py --confirm-write
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import audit_store
import critic_client
from audit_store import AuditStoreError, StateConflictError, WriteError
from config import redact
from critic_gate import (
    CATEGORY_APPROVED,
    ERROR,
    CATEGORY_REJECTED,
    REJECT,
    CriticDecision,
    CriticRequest,
    critic_failure,
)
from execution_gate import decide_execution
from plan_snapshot import PlanSnapshot, new_plan_id
from planner import PLANNER_ERROR, UNSUPPORTED_COMMAND, PlannerFailure, plan_command
from safety_validator import ValidationResult, validate_plan

__all__ = [
    "DEFAULT_WORKER_ID",
    "STAGE_CLAIM",
    "STAGE_CRITIC_EVENT_WRITE",
    "STAGE_EVENT_WRITE",
    "STAGE_STATUS_UPDATE",
    "STAGE_VALIDATOR_EVENT_WRITE",
    "CommandProcessingError",
    "ProcessResult",
    "decide_final_status",
    "planner_failure_message",
    "process_one_command",
]

# Stored in commands.claimed_by, which the anon key can read. Not a hostname.
DEFAULT_WORKER_ID = "rehearsal-worker"

# Where a CommandProcessingError happened.
STAGE_CLAIM = "claim"  # the claim returned an unusable row
STAGE_EVENT_WRITE = "event_write"  # plan_generated insert failed; status untouched
STAGE_VALIDATOR_EVENT_WRITE = "validator_event_write"  # validator_result insert failed
STAGE_CRITIC_EVENT_WRITE = "critic_event_write"  # critic_result insert failed
STAGE_STATUS_UPDATE = "status_update"  # events written; the status update failed

# Check outcomes used by decide_final_status.
VALIDATOR_PASS = "pass"
VALIDATOR_FAIL = "fail"
VALIDATOR_ERROR = "error"
CRITIC_APPROVE = "approve"
CRITIC_REJECT = "reject"
CRITIC_ERROR = "error"

Outcome = Literal["no_command", "planner_failed", "plan_checked"]
CriticCall = Callable[[CriticRequest], CriticDecision]


@dataclass(frozen=True)
class ProcessResult:
    """What one call did.

    * ``no_command``: nothing was claimed, nothing was written.
    * ``planner_failed``: ``plan_generated`` (error) written, status now ``failed``.
    * ``plan_checked``: a plan was generated and checked; ``plan_generated``,
      the result events and the final status (``final_status``: ``approved``,
      ``rejected`` or ``failed``) were all written. Nothing was executed.
    """

    outcome: Outcome
    command_id: str | None = None
    error_kind: str | None = None
    action_count: int | None = None
    plan_id: str | None = None
    plan_hash: str | None = None
    validator_outcome: str | None = None  # pass, fail or error
    critic_outcome: str | None = None  # approve, reject or error
    final_status: str | None = None


class CommandProcessingError(Exception):
    """Processing stopped after the claim. The command needs a person to look at it.

    ``stage`` is one of:

    * ``STAGE_CLAIM``: the claimed row was unusable. The claim itself may have
      set ``claimed_by``/``claimed_at``; no event and no status were written.
    * ``STAGE_EVENT_WRITE``: the ``plan_generated`` insert failed. The status
      was NOT changed, and for a valid plan no check was run.
    * ``STAGE_VALIDATOR_EVENT_WRITE`` / ``STAGE_CRITIC_EVENT_WRITE``: a result
      event insert failed. Both checks had run, the other result event was
      still attempted, and the status was NOT changed.
    * ``STAGE_STATUS_UPDATE``: every event was written, then the status update
      failed.

    ``event_written`` is about the failed stage's event: True, False, or None
    when it cannot be known (an ambiguous ``WriteError`` on the insert).
    ``events_written`` lists the event types confirmed written by this call.
    ``plan_id`` and ``intended_status`` are set when known, for manual recovery.
    """

    def __init__(
        self,
        message: str,
        *,
        command_id: str,
        stage: str,
        event_written: bool | None,
        plan_id: str | None = None,
        events_written: tuple[str, ...] = (),
        intended_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.command_id = command_id
        self.stage = stage
        self.event_written = event_written
        self.plan_id = plan_id
        self.events_written = events_written
        self.intended_status = intended_status


def planner_failure_message(kind: str) -> str:
    """The event ``message``. Matches the dashboard summary text (src/lib/summary.ts)."""
    if kind == UNSUPPORTED_COMMAND:
        return "Command not understood: nothing executed"
    return "Planning failed: nothing executed"


def decide_final_status(validator_outcome: str, critic_outcome: str) -> str:
    """The agreed precedence. Pure. An unknown outcome counts as an error.

    | validator | critic  | status   |
    |-----------|---------|----------|
    | fail      | any     | rejected |
    | any       | reject  | rejected |
    | pass      | approve | approved |
    | otherwise |         | failed   |
    """
    if validator_outcome == VALIDATOR_FAIL or critic_outcome == CRITIC_REJECT:
        return "rejected"
    if validator_outcome == VALIDATOR_PASS and critic_outcome == CRITIC_APPROVE:
        return "approved"
    return "failed"


def _describe(exc: BaseException) -> str:
    """Error text safe to show. audit_store errors are already sanitized."""
    if isinstance(exc, AuditStoreError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _written_flag(exc: BaseException) -> bool | None:
    # WriteError is ambiguous (the insert may have happened). Any other error
    # was raised before the request was sent.
    return None if isinstance(exc, WriteError) else False


def process_one_command(
    worker_id: str,
    *,
    client: Any | None = None,
    critic: CriticCall | None = None,
) -> ProcessResult:
    """Claim and process at most one command. See the module docstring.

    ``critic`` replaces the HTTP critic call (tests); by default it is
    ``critic_client.review_plan``, which reads ``CRITIC_URL`` and
    ``CRITIC_SHARED_SECRET`` from the worker configuration.

    Raises whatever ``claim_next_command`` raises (nothing is processed then),
    and ``CommandProcessingError`` when a write after the claim fails.
    """
    row = audit_store.claim_next_command(worker_id, client=client)
    if row is None:
        return ProcessResult(outcome="no_command")

    command_id = row.get("id")
    if not isinstance(command_id, str) or not command_id.strip():
        # Cannot happen with a real claim; refuse rather than guess.
        raise CommandProcessingError(
            "CLAIM RETURNED AN UNUSABLE ROW (no id). A command may already have "
            "been claimed (claimed_by/claimed_at set) and left 'received'. No "
            "audit event and no status change were written. Find it in the "
            "dashboard by claimed_by.",
            command_id="<unknown>",
            stage=STAGE_CLAIM,
            event_written=False,
        )

    plan = plan_command(row.get("text"))
    if plan.failure is not None:
        return _record_planner_failure(command_id, plan.failure, client)

    try:
        snapshot = PlanSnapshot.create(command_id, new_plan_id(), plan.actions or ())
    except Exception as exc:
        # The planner only emits finite numbers and strings, so this should not
        # happen. A plan that cannot be frozen and hashed is a planner error.
        return _record_planner_failure(
            command_id,
            PlannerFailure(
                PLANNER_ERROR, f"The plan could not be hashed ({type(exc).__name__})."
            ),
            client,
        )

    return _check_plan(snapshot, client, critic or critic_client.review_plan)


# --------------------------------------------------------------------------
# Planner failure (unchanged behaviour)
# --------------------------------------------------------------------------


def _record_planner_failure(
    command_id: str, failure: PlannerFailure, client: Any | None
) -> ProcessResult:
    try:
        audit_store.append_event(
            command_id,
            "plan_generated",
            planner_failure_message(failure.kind),
            failure.to_payload(),
            client=client,
        )
    except Exception as exc:
        written = _written_flag(exc)
        raise CommandProcessingError(
            f"EVENT WRITE FAILED for command {command_id}: plan_generated insert "
            f"failed ({_describe(exc)}). "
            + (
                "The event MAY have been written: check the timeline before "
                "writing it again. "
                if written is None
                else "No event was written. "
            )
            + "The status was NOT changed: the command is still 'received' and "
            "claimed. Manual recovery needed.",
            command_id=command_id,
            stage=STAGE_EVENT_WRITE,
            event_written=written,
        ) from None

    try:
        audit_store.set_command_status(
            command_id, expected_status="received", status="failed", client=client
        )
    except Exception as exc:
        raise CommandProcessingError(
            f"STATUS UPDATE FAILED for command {command_id}: plan_generated WAS "
            f"written, but received -> failed failed ({_describe(exc)}); "
            f"{_status_state(exc)}. Do NOT write the event again. Manual recovery "
            "needed: set the status only.",
            command_id=command_id,
            stage=STAGE_STATUS_UPDATE,
            event_written=True,
            events_written=("plan_generated",),
            intended_status="failed",
        ) from None

    return ProcessResult(
        outcome="planner_failed", command_id=command_id, error_kind=failure.kind
    )


def _status_state(exc: BaseException) -> str:
    if isinstance(exc, StateConflictError):
        return "the command was no longer 'received'"
    if isinstance(exc, WriteError):
        return "the update may or may not have been applied"
    return "the status was not changed"


# --------------------------------------------------------------------------
# Successful plan: plan_generated, both checks, result events, final status
# --------------------------------------------------------------------------


def _run_validator(snapshot: PlanSnapshot) -> tuple[str, ValidationResult | None]:
    """(outcome, result). Outcome ``error`` if the validator raised or misbehaved."""
    try:
        result = validate_plan(snapshot.plan_id, snapshot.actions())
    except Exception:
        return VALIDATOR_ERROR, None
    if not isinstance(result, ValidationResult) or result.plan_id != snapshot.plan_id:
        return VALIDATOR_ERROR, None
    if result.outcome == VALIDATOR_PASS and not result.errors:
        return VALIDATOR_PASS, result
    if result.outcome == VALIDATOR_FAIL and result.errors:
        return VALIDATOR_FAIL, result
    return VALIDATOR_ERROR, None


def _run_critic(
    snapshot: PlanSnapshot, critic: CriticCall
) -> tuple[str, CriticDecision, CriticRequest | None]:
    """(outcome, decision, request). Never raises; anything odd is ``error``."""
    try:
        request = snapshot.critic_request()
    except Exception as exc:
        return (
            CRITIC_ERROR,
            critic_failure("invalid_body", f"critic request not built ({type(exc).__name__})"),
            None,
        )
    try:
        decision = critic(request)
    except Exception as exc:
        decision = critic_failure(
            "invalid_body", f"critic check failed unexpectedly ({type(exc).__name__})"
        )
    if not isinstance(decision, CriticDecision):
        return CRITIC_ERROR, critic_failure("invalid_body", "critic check returned no decision"), request

    same_plan = (
        decision.command_id == snapshot.command_id and decision.plan_id == snapshot.plan_id
    )
    if decision.approved and same_plan and decision.plan_hash == snapshot.plan_hash:
        return CRITIC_APPROVE, decision, request
    if (
        decision.outcome == REJECT
        and decision.category == CATEGORY_REJECTED
        and same_plan
        and decision.plan_hash in (None, snapshot.plan_hash)
    ):
        return CRITIC_REJECT, decision, request
    if decision.outcome == ERROR and decision.category not in (CATEGORY_APPROVED, CATEGORY_REJECTED):
        return CRITIC_ERROR, decision, request
    # A verdict that does not belong to this exact command, plan and hash, or
    # a decision with an unknown outcome: never stored as given.
    return (
        CRITIC_ERROR,
        critic_failure("invalid_body", "critic decision does not match this plan"),
        request,
    )


def _validator_message(outcome: str) -> str:
    return "Validator passed" if outcome == VALIDATOR_PASS else "Validator rejected the plan"


def _critic_message(outcome: str) -> str:
    if outcome == CRITIC_APPROVE:
        return "Critic approved the plan"
    if outcome == CRITIC_REJECT:
        return "Critic rejected the plan"
    return "Critic unavailable: no verdict"


def _check_plan(snapshot: PlanSnapshot, client: Any | None, critic: CriticCall) -> ProcessResult:
    command_id = snapshot.command_id
    plan_id = snapshot.plan_id

    # 1. plan_generated (success). Nothing else happens unless it is written.
    try:
        audit_store.append_event(
            command_id,
            "plan_generated",
            f"Plan generated: {snapshot.action_count} action(s)",
            snapshot.plan_payload(),
            client=client,
        )
    except Exception as exc:
        written = _written_flag(exc)
        raise CommandProcessingError(
            f"EVENT WRITE FAILED for command {command_id}: plan_generated insert "
            f"failed for plan {plan_id} ({_describe(exc)}). "
            + (
                "The event MAY have been written: check the timeline before "
                "writing anything. "
                if written is None
                else "No event was written. "
            )
            + "The validator and the critic were NOT run. The status was NOT "
            "changed: the command is still 'received' and claimed. Manual "
            "recovery needed.",
            command_id=command_id,
            stage=STAGE_EVENT_WRITE,
            event_written=written,
            plan_id=plan_id,
        ) from None
    events_written: list[str] = ["plan_generated"]

    # 2. Both checks, each on its own copy of the snapshot. Neither depends on
    # the other, so a rejection by one never skips the other.
    validator_outcome, validation = _run_validator(snapshot)
    critic_outcome, decision, request = _run_critic(snapshot, critic)

    # Consistency check before an approval: the pure execution gate re-validates
    # and re-hashes the request and compares it with the critic decision.
    if (
        validator_outcome == VALIDATOR_PASS
        and critic_outcome == CRITIC_APPROVE
        and not decide_execution(request, decision).allowed
    ):
        critic_outcome = CRITIC_ERROR
        decision = critic_failure("invalid_body", "approval failed the final consistency check")

    final_status = decide_final_status(validator_outcome, critic_outcome)

    # 3. Result events. Each is attempted once, even if the other failed.
    failures: list[tuple[str, str, BaseException]] = []
    if validation is not None:
        try:
            audit_store.append_event(
                command_id,
                "validator_result",
                _validator_message(validator_outcome),
                validation.to_payload(),
                client=client,
            )
            events_written.append("validator_result")
        except Exception as exc:
            failures.append((STAGE_VALIDATOR_EVENT_WRITE, "validator_result", exc))
    try:
        audit_store.append_event(
            command_id,
            "critic_result",
            _critic_message(critic_outcome),
            decision.to_payload(),
            client=client,
        )
        events_written.append("critic_result")
    except Exception as exc:
        failures.append((STAGE_CRITIC_EVENT_WRITE, "critic_result", exc))

    if failures:
        stage, event_type, exc = failures[0]
        written = _written_flag(exc)
        lines = [
            f"{name} insert failed ({_describe(err)})"
            + ("; it MAY have been written" if _written_flag(err) is None else "; not written")
            for _, name, err in failures
        ]
        raise CommandProcessingError(
            f"EVENT WRITE FAILED for command {command_id}, plan {plan_id}: "
            + "; ".join(lines)
            + f". Written: {', '.join(events_written)}. Checks: validator "
            f"{validator_outcome}, critic {critic_outcome}; the status would be "
            f"'{final_status}'. The status was NOT changed: the command is still "
            "'received' and claimed. Check the timeline before writing anything; "
            "never write an event twice. Manual recovery needed.",
            command_id=command_id,
            stage=stage,
            event_written=written,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status=final_status,
        ) from None

    # 4. The single status write, conditional on 'received'.
    try:
        audit_store.set_command_status(
            command_id,
            expected_status="received",
            status=final_status,
            plan_id=plan_id,
            client=client,
        )
    except Exception as exc:
        raise CommandProcessingError(
            f"STATUS UPDATE FAILED for command {command_id}: "
            f"{', '.join(events_written)} WERE written, but received -> "
            f"{final_status} (plan_id {plan_id}) failed ({_describe(exc)}); "
            f"{_status_state(exc)}. Do NOT write the events again. Manual recovery "
            f"needed: set the status to '{final_status}' with plan_id {plan_id} only.",
            command_id=command_id,
            stage=STAGE_STATUS_UPDATE,
            event_written=True,
            plan_id=plan_id,
            events_written=tuple(events_written),
            intended_status=final_status,
        ) from None

    return ProcessResult(
        outcome="plan_checked",
        command_id=command_id,
        action_count=snapshot.action_count,
        plan_id=plan_id,
        plan_hash=snapshot.plan_hash,
        validator_outcome=validator_outcome,
        critic_outcome=critic_outcome,
        final_status=final_status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claim and process ONE command: plan, validate, critic, status. "
        "Writes to Supabase and calls the critic. Executes nothing."
    )
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="required: allow the claim, the critic request and the audit/status writes",
    )
    parser.add_argument("--worker-id", default=DEFAULT_WORKER_ID)
    args = parser.parse_args(argv)

    if not args.confirm_write:
        print("Refusing to run without --confirm-write. Nothing was sent.")
        return 2

    try:
        result = process_one_command(args.worker_id)
    except CommandProcessingError as exc:
        print(f"FAILED at {exc.stage}: {redact(exc)}")
        return 1
    except Exception as exc:
        print(f"Claim failed, nothing processed: {redact(_describe(exc))}")
        return 1

    if result.outcome == "no_command":
        print("No eligible command. Nothing claimed, nothing written.")
    elif result.outcome == "planner_failed":
        print(
            f"Command {result.command_id}: plan_generated ({result.error_kind}) "
            "written, status received -> failed."
        )
    else:
        print(
            f"Command {result.command_id}: plan {result.plan_id} "
            f"({result.action_count} action(s), {result.plan_hash}). "
            f"Validator: {result.validator_outcome}. Critic: {result.critic_outcome}. "
            f"Status received -> {result.final_status}. Nothing was executed."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
