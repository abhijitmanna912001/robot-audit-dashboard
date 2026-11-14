"""Worker side of the critic contract: build the request, judge the response.

Pure functions. Nothing here sends an HTTP request: the caller makes the call
and hands the status code and parsed body to ``evaluate_critic_response``.
The HTTP client is ``critic_client.py``; it judges every response here.

Only the agreed verdict fields of a response, length-capped, are kept in
a decision (``sanitize_response``): the ``critic_result`` payload is public.

Contract: docs/schema-draft.md on main, "Critic contract", "Plan hash
contract" and section 6 "Fail-closed rule". The worker may treat a plan as
approved by the critic only if ALL of these hold:

* HTTP status is 200
* the body is a valid critic verdict
* ``commandId``, ``planId`` and ``planHash`` in the response equal the request
* ``verdict`` is ``"approve"``
* ``triggeredRules`` is an empty list (an approval with rules is ``invalid_body``)

Anything else is not an approval. In particular a response whose ``planHash``
differs from the request is a protocol-integrity failure and is recorded as
outcome ``error``, never ``approve`` and never an ordinary ``reject``, whatever
its status code and verdict say.

``evaluate_critic_response`` never raises: an unexpected problem becomes an
``error`` decision (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plan_hasher import hash_actions
from schemas import JsonValue

# Outcomes written to the critic_result audit payload (docs/schema-draft.md).
APPROVE = "approve"
REJECT = "reject"
ERROR = "error"

# Error kinds allowed in the critic_result payload (docs/schema-draft.md).
ERROR_KINDS = frozenset({"http_status", "timeout", "invalid_body"})
MAX_DETAIL_LENGTH = 200

# What of a critic response body is copied into the critic_result payload,
# which anyone with the anon key can read. Only the agreed verdict fields
# (docs/schema-draft.md, "Critic verdict"), each length-capped. Secret
# redaction is added by critic_client: this module stays pure and imports no
# configuration.
# Any other field the critic returns is dropped.
MAX_REASON_LENGTH = 200
MAX_TRIGGERED_RULES = 20
MAX_RULE_LENGTH = 64
MAX_ID_LENGTH = 100
_RESPONSE_STRING_FIELDS = (
    ("commandId", MAX_ID_LENGTH),
    ("planId", MAX_ID_LENGTH),
    ("planHash", MAX_ID_LENGTH),
    ("verdict", MAX_ID_LENGTH),
    ("reason", MAX_REASON_LENGTH),
)


def _safe_text(value: str, limit: int) -> str:
    return value[:limit]


def sanitize_response(body: dict[str, Any]) -> dict[str, Any]:
    """The part of a critic response body that may be stored in an audit event.

    Keeps only the agreed verdict fields that have the agreed type. Strings are
    capped (``reason`` at ``MAX_REASON_LENGTH``), and
    ``triggeredRules`` keeps at most ``MAX_TRIGGERED_RULES`` strings.
    """
    safe: dict[str, Any] = {}
    for field, limit in _RESPONSE_STRING_FIELDS:
        value = body.get(field)
        if isinstance(value, str):
            safe[field] = _safe_text(value, limit)
    rules = body.get("triggeredRules")
    if isinstance(rules, list):
        safe["triggeredRules"] = [
            _safe_text(rule, MAX_RULE_LENGTH)
            for rule in rules[:MAX_TRIGGERED_RULES]
            if isinstance(rule, str)
        ]
    return safe

# Internal classification of a decision, so that worker code and tests can tell
# the cases apart. These strings are NOT part of the audit payload and are not
# an external contract.
CATEGORY_APPROVED = "approved"
CATEGORY_REJECTED = "rejected"  # the critic reviewed the plan and said no
CATEGORY_UNAUTHORIZED = "unauthorized"  # HTTP 401: missing or wrong shared secret
CATEGORY_HTTP_STATUS = "http_status"  # any other non-200 status
CATEGORY_TIMEOUT = "timeout"
CATEGORY_NETWORK_ERROR = "network_error"
CATEGORY_MALFORMED_RESPONSE = "malformed_response"
CATEGORY_COMMAND_ID_MISMATCH = "command_id_mismatch"
CATEGORY_PLAN_ID_MISMATCH = "plan_id_mismatch"
CATEGORY_PLAN_HASH_MISMATCH = "plan_hash_mismatch"
CATEGORY_NOT_SENT = "not_sent"  # no request was made (configuration missing or unusable)


@dataclass(frozen=True)
class CriticRequest:
    command_id: str
    plan_id: str
    plan_hash: str
    actions: list[JsonValue]

    def to_body(self) -> dict[str, Any]:
        """The JSON body for POST /api/critic."""
        return {
            "commandId": self.command_id,
            "planId": self.plan_id,
            "planHash": self.plan_hash,
            "actions": self.actions,
        }


@dataclass(frozen=True)
class CriticDecision:
    outcome: str  # APPROVE, REJECT or ERROR
    category: str  # one of the CATEGORY_* values above (internal)
    # The request this decision was evaluated for. Set by
    # evaluate_critic_response for approve and reject, so the execution gate can
    # refuse an approval that belongs to a different command or plan.
    command_id: str | None = None
    plan_id: str | None = None
    plan_hash: str | None = None
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @property
    def approved(self) -> bool:
        return self.outcome == APPROVE and self.category == CATEGORY_APPROVED

    def to_payload(self) -> dict[str, Any]:
        """The ``critic_result`` audit event payload. Unset parts are omitted."""
        payload: dict[str, Any] = {"outcome": self.outcome}
        if self.plan_hash is not None:
            payload["planHash"] = self.plan_hash
        if self.response is not None:
            payload["response"] = self.response
        if self.error is not None:
            payload["error"] = self.error
        return payload


def build_critic_request(command_id: str, plan_id: str, actions: list[JsonValue]) -> CriticRequest:
    """Hash ``actions`` and build the request. Raises PlanHashError if unhashable."""
    for name, value in (("command_id", command_id), ("plan_id", plan_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
    return CriticRequest(command_id, plan_id, hash_actions(actions), actions)


def _error(
    category: str,
    kind: str,
    detail: str | None = None,
    http_status: int | None = None,
    plan_hash: str | None = None,
    response: dict[str, Any] | None = None,
) -> CriticDecision:
    error: dict[str, Any] = {"kind": kind}
    if http_status is not None:
        error["httpStatus"] = http_status
    if detail:
        error["detail"] = detail[:MAX_DETAIL_LENGTH]
    return CriticDecision(
        outcome=ERROR, category=category, plan_hash=plan_hash, response=response, error=error
    )


def critic_failure(
    kind: str, detail: str | None = None, http_status: int | None = None
) -> CriticDecision:
    """An ``error`` decision for one of the three kinds the contract defines."""
    if kind == "timeout":
        return _error(CATEGORY_TIMEOUT, "timeout", detail, http_status)
    if kind == "http_status":
        category = CATEGORY_UNAUTHORIZED if http_status == 401 else CATEGORY_HTTP_STATUS
        return _error(category, "http_status", detail, http_status)
    if kind == "invalid_body":
        return _error(CATEGORY_MALFORMED_RESPONSE, "invalid_body", detail, http_status)
    # Unknown kind: still an error, never anything better.
    return _error(
        CATEGORY_MALFORMED_RESPONSE, "invalid_body", f"unknown error kind reported: {kind!r}"
    )


def critic_network_failure(detail: str | None = None) -> CriticDecision:
    """An ``error`` decision for a request that got no response (not a timeout).

    PROVISIONAL payload. The contract defines no error kind for a connection or
    DNS failure. Until one is agreed this writes kind ``timeout``, the nearest
    documented kind (no response arrived), and states the real cause in
    ``detail``. The internal category is ``network_error``.
    """
    text = "network error, no response received"
    if detail:
        text += f": {detail}"
    return _error(CATEGORY_NETWORK_ERROR, "timeout", text)


def critic_not_sent(detail: str) -> CriticDecision:
    """An ``error`` decision for a critic request that was never sent.

    For example ``CRITIC_URL`` or ``CRITIC_SHARED_SECRET`` is missing, or the
    URL is unusable. The critic gave no verdict, so this is an error, never a
    skip and never an approval.

    PROVISIONAL payload, for the same reason as ``critic_network_failure``: the
    contract has no error kind for "no request was sent". It writes kind
    ``timeout`` (no response arrived) and states the real cause in ``detail``.
    The internal category is ``not_sent``.
    """
    return _error(CATEGORY_NOT_SENT, "timeout", f"critic request not sent: {detail}")


def evaluate_critic_response(request: CriticRequest, http_status: Any, body: Any) -> CriticDecision:
    """Decide what a critic response means for ``request``. Never raises."""
    try:
        return _evaluate(request, http_status, body)
    except Exception as exc:  # fail closed
        return critic_failure("invalid_body", f"could not evaluate response: {type(exc).__name__}")


def _evaluate(request: CriticRequest, http_status: Any, body: Any) -> CriticDecision:
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        return critic_failure("http_status", "no usable HTTP status")
    if http_status != 200:
        # Deliberately ignores the body: a non-200 response is never a verdict.
        return critic_failure("http_status", http_status=http_status)

    problem = _verdict_shape_problem(body)
    if problem is not None:
        return critic_failure("invalid_body", problem, http_status=200)

    if body["commandId"] != request.command_id:
        return _error(
            CATEGORY_COMMAND_ID_MISMATCH,
            "invalid_body",
            "commandId in the response does not match the request",
            http_status=200,
        )
    if body["planId"] != request.plan_id:
        return _error(
            CATEGORY_PLAN_ID_MISMATCH,
            "invalid_body",
            "planId in the response does not match the request",
            http_status=200,
        )

    if "planHash" in body and body["planHash"] != request.plan_hash:
        # Protocol-integrity failure, for either verdict. Recorded with the hash
        # the worker supplied and the critic's response (which carries the hash
        # the critic computed), so the audit event shows both values.
        return _error(
            CATEGORY_PLAN_HASH_MISMATCH,
            "invalid_body",
            "planHash in the response does not match the request",
            http_status=200,
            plan_hash=request.plan_hash,
            response=sanitize_response(body),
        )

    # From here the ids match and the hash, if present, is ours.
    outcome = APPROVE if body["verdict"] == APPROVE else REJECT
    return CriticDecision(
        outcome=outcome,
        category=CATEGORY_APPROVED if outcome == APPROVE else CATEGORY_REJECTED,
        command_id=request.command_id,
        plan_id=request.plan_id,
        plan_hash=body.get("planHash"),
        response=sanitize_response(body),
    )


def _verdict_shape_problem(body: Any) -> str | None:
    """Return why ``body`` is not a valid critic verdict, or None if it is."""
    if not isinstance(body, dict):
        return "body is not a JSON object"
    for field in ("commandId", "planId", "verdict", "reason"):
        if not isinstance(body.get(field), str):
            return f"{field} is missing or not a string"
    if body["verdict"] not in (APPROVE, REJECT):
        return "verdict is neither approve nor reject"
    rules = body.get("triggeredRules")
    if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
        return "triggeredRules is missing or not a list of strings"
    if "planHash" in body and not isinstance(body["planHash"], str):
        return "planHash is not a string"
    if body["verdict"] == APPROVE and "planHash" not in body:
        return "an approve verdict has no planHash"
    if body["verdict"] == APPROVE and rules:
        # The critic approves only when no rule fired (src/app/api/critic/route.ts
        # returns triggeredRules: [] with every approval). A contradictory body is
        # invalid, never an approval and never an ordinary reject.
        return "an approve verdict has triggered rules"
    return None
