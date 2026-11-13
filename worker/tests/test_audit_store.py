"""Unit tests for audit_store. Offline: no network, no database, no .env.

Every test passes a FakeClient (or patches the client factory). Nothing here can
reach Supabase. The only "secret" is a made-up string set in the environment so
``config.redact`` has something to mask.

Run from worker/:  python -m pytest tests/test_audit_store.py -q
"""

from __future__ import annotations

import ast
import copy
import itertools
import re
import sys
import traceback
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import audit_store
from audit_store import (
    ALLOWED_TRANSITIONS,
    AuditStoreError,
    InvalidEventTypeError,
    InvalidPayloadError,
    InvalidTransitionError,
    StateConflictError,
    WriteError,
    append_event,
    claim_next_command,
    set_command_status,
)
from config import SECRET_ENV_VARS, SupabaseSettings, ConfigError

FAKE_SECRET = "sb_secret_FAKE_for_tests_0123456789"
MODULE_PATH = Path(audit_store.__file__)

EVENT_TYPES = [
    "command_received",
    "plan_generated",
    "validator_result",
    "critic_result",
    "execution_started",
    "execution_completed",
    "execution_failed",
]
STATUSES = ["received", "approved", "rejected", "executing", "completed", "failed"]
# Written out here on purpose, not imported, so a wrong table in the module fails.
ALLOWED = [
    ("received", "approved"),
    ("received", "rejected"),
    ("received", "failed"),
    ("approved", "executing"),
    ("approved", "failed"),
    ("executing", "completed"),
    ("executing", "failed"),
]
FORBIDDEN = [pair for pair in itertools.product(STATUSES, STATUSES) if pair not in ALLOWED]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known fake secret in the environment; no real credential can leak in."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", FAKE_SECRET)


# --------------------------------------------------------------------------
# Fake client
# --------------------------------------------------------------------------


@dataclass
class Call:
    table: str
    op: str
    values: Any = None
    columns: tuple[str, ...] = ()
    filters: list[tuple[str, str, Any]] = field(default_factory=list)
    order: list[tuple[str, bool]] = field(default_factory=list)
    limit: int | None = None


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    """Supports only what audit_store is allowed to use. No delete/upsert/rpc."""

    def __init__(self, client: FakeClient, table: str) -> None:
        self._client = client
        self._call = Call(table=table, op="")

    def select(self, *columns: str) -> FakeQuery:
        self._call.op, self._call.columns = "select", columns
        return self

    def insert(self, row: dict[str, Any]) -> FakeQuery:
        self._call.op, self._call.values = "insert", copy.deepcopy(row)
        return self

    def update(self, values: dict[str, Any]) -> FakeQuery:
        self._call.op, self._call.values = "update", copy.deepcopy(values)
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        self._call.filters.append(("eq", column, value))
        return self

    def is_(self, column: str, value: Any) -> FakeQuery:
        self._call.filters.append(("is", column, value))
        return self

    def order(self, column: str, *, desc: bool = False) -> FakeQuery:
        self._call.order.append((column, desc))
        return self

    def limit(self, size: int) -> FakeQuery:
        self._call.limit = size
        return self

    def execute(self) -> FakeResponse:
        return self._client._execute(self._call)


class FakeClient:
    """Records every call and behaves like a tiny in-memory pair of tables."""

    def __init__(
        self,
        commands: list[dict[str, Any]] | None = None,
        audit_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "commands": copy.deepcopy(commands or []),
            "audit_events": copy.deepcopy(audit_events or []),
        }
        self.calls: list[Call] = []
        self._hooks: list[tuple[str, Any]] = []
        self._failures: list[tuple[str, str, BaseException]] = []
        self._override: dict[tuple[str, str], Any] = {}
        self._counter = 0

    # -- test controls ------------------------------------------------------
    def before_next(self, op: str, fn: Any) -> None:
        """Run ``fn(client)`` once, just before the next ``op`` is applied."""
        self._hooks.append((op, fn))

    def fail(self, table: str, op: str, exc: BaseException) -> None:
        """Make every (table, op) call raise ``exc`` (after it is recorded)."""
        self._failures.append((table, op, exc))

    def respond_with(self, table: str, op: str, data: Any) -> None:
        self._override[(table, op)] = data

    def calls_of(self, op: str) -> list[Call]:
        return [c for c in self.calls if c.op == op]

    # -- supabase-like surface ---------------------------------------------
    def table(self, name: str) -> FakeQuery:
        assert name in self.tables, f"unexpected table {name!r}"
        return FakeQuery(self, name)

    def _execute(self, call: Call) -> FakeResponse:
        self.calls.append(call)
        for i, (op, fn) in enumerate(self._hooks):
            if op == call.op:
                del self._hooks[i]
                fn(self)
                break
        for table, op, exc in self._failures:
            if table == call.table and op == call.op:
                raise exc
        if (call.table, call.op) in self._override:
            return FakeResponse(self._override[(call.table, call.op)])

        rows = self.tables[call.table]
        matched = [r for r in rows if self._matches(r, call.filters)]
        if call.op == "select":
            for column, desc in reversed(call.order):
                matched.sort(key=lambda r, c=column: r[c], reverse=desc)
            if call.limit is not None:
                matched = matched[: call.limit]
            keep = [c for c in call.columns if c != "*"]
            return FakeResponse(
                [{k: r[k] for k in keep} if keep else copy.deepcopy(r) for r in matched]
            )
        if call.op == "insert":
            self._counter += 1
            row = {
                "id": f"gen-{self._counter}",
                "created_at": "2026-11-14T10:00:00+00:00",
                **copy.deepcopy(call.values),
            }
            rows.append(row)
            return FakeResponse([copy.deepcopy(row)])
        if call.op == "update":
            for r in matched:
                r.update(copy.deepcopy(call.values))
                r["updated_at"] = "trigger-set"  # stands in for the DB trigger
            return FakeResponse(copy.deepcopy(matched))
        raise AssertionError(f"unsupported op {call.op!r}")

    @staticmethod
    def _matches(row: dict[str, Any], filters: list[tuple[str, str, Any]]) -> bool:
        for kind, column, value in filters:
            if kind == "eq" and row.get(column) != value:
                return False
            if kind == "is" and value is None and row.get(column) is not None:
                return False
        return True


def cmd(
    id: str,
    created: str,
    status: str = "received",
    claimed_by: str | None = None,
    claimed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "text": f"command {id}",
        "status": status,
        "plan_id": None,
        "claimed_by": claimed_by,
        "claimed_at": claimed_at,
        "created_at": created,
        "updated_at": created,
    }


class FakeAPIError(Exception):
    """Shaped like postgrest's APIError (code, message, details, hint)."""

    def __init__(self, message: str, code: str = "XX000", details: str = "") -> None:
        super().__init__(message)
        self.message, self.code, self.details, self.hint = message, code, details, None


def full_trace(err: BaseException) -> str:
    return "".join(traceback.format_exception(err))


def assert_clean(err: BaseException) -> None:
    """The error shows no secret, however it is printed or chained."""
    for text in (str(err), repr(err), full_trace(err)):
        assert FAKE_SECRET not in text
        assert FAKE_SECRET[:10] not in text
    assert err.__cause__ is None


# --------------------------------------------------------------------------
# append_event: event type
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_valid_event_type_inserts_once(event_type: str) -> None:
    client = FakeClient()
    row = append_event("c1", event_type, "hello", {"k": 1}, client=client)
    inserts = client.calls_of("insert")
    assert len(inserts) == 1
    assert inserts[0].table == "audit_events"
    assert inserts[0].values["event_type"] == event_type
    assert row["event_type"] == event_type


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Command_Received",
        "critic_pass",
        "validator_pass",
        "rejected",
        "plan_generated ",
        "execution_started\n",
        "unknown",
        None,
        5,
        ["plan_generated"],
        b"plan_generated",
    ],
)
def test_invalid_event_type_raises_before_any_database_call(bad: Any) -> None:
    client = FakeClient()
    with pytest.raises(InvalidEventTypeError):
        append_event("c1", bad, "hello", client=client)
    assert client.calls == []


def test_event_type_list_matches_sql_check() -> None:
    sql = (MODULE_PATH.parent.parent / "db" / "001_init.sql").read_text(encoding="utf-8")
    check = re.search(r"event_type\s+text not null\s+check \(event_type in \((.*?)\)\)", sql, re.S)
    assert check is not None
    in_sql = set(re.findall(r"'([a-z_]+)'", check.group(1)))
    assert in_sql == set(EVENT_TYPES)


def test_validation_runs_before_the_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise AssertionError("client factory must not be reached")

    monkeypatch.setattr(audit_store, "_default_client", boom)
    with pytest.raises(InvalidEventTypeError):
        append_event("c1", "nope", "hello")
    with pytest.raises(InvalidPayloadError):
        append_event("c1", "plan_generated", "")
    with pytest.raises(InvalidTransitionError):
        set_command_status("c1", "completed", "received")
    with pytest.raises(ValueError):
        claim_next_command("")


# --------------------------------------------------------------------------
# append_event: message
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\n\t", "x" * 501, "é" * 501, None, 5, b"x", ["x"]])
def test_bad_message_is_rejected_without_a_call(bad: Any) -> None:
    client = FakeClient()
    with pytest.raises(InvalidPayloadError):
        append_event("c1", "plan_generated", bad, client=client)
    assert client.calls == []


@pytest.mark.parametrize("good", ["a", "x" * 500, "é" * 500, "Plan ready: 3 actions"])
def test_good_message_is_stored_unchanged(good: str) -> None:
    client = FakeClient()
    row = append_event("c1", "plan_generated", good, client=client)
    assert row["message"] == good


# --------------------------------------------------------------------------
# append_event: payload
# --------------------------------------------------------------------------


def _circular() -> dict[str, Any]:
    d: dict[str, Any] = {}
    d["self"] = d
    return d


def _deep(levels: int) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for _ in range(levels):
        d = {"a": d}
    return d


BAD_PAYLOADS = [
    pytest.param({"a": float("nan")}, id="nan"),
    pytest.param({"a": float("inf")}, id="inf"),
    pytest.param({"a": float("-inf")}, id="-inf"),
    pytest.param({"a": [1, {"b": float("nan")}]}, id="nested-nan"),
    pytest.param([], id="list"),
    pytest.param([{"a": 1}], id="list-of-dict"),
    pytest.param("text", id="str"),
    pytest.param(5, id="int"),
    pytest.param(("a", 1), id="tuple"),
    pytest.param(object(), id="object"),
    pytest.param({"a": {1, 2}}, id="set"),
    pytest.param({"a": datetime(2026, 1, 1)}, id="datetime"),
    pytest.param({"a": b"bytes"}, id="bytes"),
    pytest.param({"a": Decimal("1.5")}, id="decimal"),
    pytest.param({"a": (1, 2)}, id="tuple-value"),
    pytest.param({"a": object()}, id="object-value"),
    pytest.param({1: "a"}, id="int-key"),
    pytest.param({None: "a"}, id="none-key"),
    pytest.param({"a": "\ud800"}, id="lone-surrogate"),
    pytest.param(_circular(), id="circular"),
    pytest.param(_deep(100), id="too-deep"),
]


@pytest.mark.parametrize("bad", BAD_PAYLOADS)
def test_bad_payload_is_rejected_without_a_call(bad: Any) -> None:
    client = FakeClient()
    with pytest.raises(InvalidPayloadError):
        append_event("c1", "plan_generated", "hello", bad, client=client)
    assert client.calls == []


def test_payload_none_is_stored_as_empty_object() -> None:
    client = FakeClient()
    row = append_event("c1", "plan_generated", "hello", client=client)
    assert client.calls_of("insert")[0].values["payload"] == {}
    assert row["payload"] == {}


def test_plain_json_payload_is_stored_as_given() -> None:
    payload = {"s": "é", "i": 3, "f": 1.5, "b": True, "n": None, "l": [1, {"x": []}], "d": {"k": "v"}}
    client = FakeClient()
    row = append_event("c1", "validator_result", "hello", payload, client=client)
    assert row["payload"] == payload


def test_payload_size_limit_is_exact_and_counts_bytes() -> None:
    overhead = len('{"k":""}')
    at_limit = {"k": "x" * (audit_store.MAX_PAYLOAD_BYTES - overhead)}
    over_limit = {"k": "x" * (audit_store.MAX_PAYLOAD_BYTES - overhead + 1)}
    client = FakeClient()
    append_event("c1", "plan_generated", "hello", at_limit, client=client)
    with pytest.raises(InvalidPayloadError):
        append_event("c1", "plan_generated", "hello", over_limit, client=client)
    # 10_001 two-byte characters: few characters, too many bytes.
    with pytest.raises(InvalidPayloadError):
        append_event("c1", "plan_generated", "hello", {"k": "é" * 10_001}, client=client)
    assert len(client.calls_of("insert")) == 1


@pytest.mark.parametrize("bad", ["", "  ", None, 5])
def test_bad_command_id_is_a_value_error_without_a_call(bad: Any) -> None:
    client = FakeClient()
    with pytest.raises(ValueError):
        append_event(bad, "plan_generated", "hello", client=client)
    assert client.calls == []


# --------------------------------------------------------------------------
# append_event: one insert, no retry, failures
# --------------------------------------------------------------------------


def test_append_event_sends_exactly_the_real_columns() -> None:
    client = FakeClient()
    append_event("c1", "critic_result", "Critic approved", {"outcome": "approve"}, client=client)
    (call,) = client.calls
    assert (call.table, call.op) == ("audit_events", "insert")
    assert call.values == {
        "command_id": "c1",
        "event_type": "critic_result",
        "message": "Critic approved",
        "payload": {"outcome": "approve"},
    }


def test_append_event_does_not_retry_on_failure() -> None:
    client = FakeClient()
    client.fail("audit_events", "insert", RuntimeError("connection reset"))
    with pytest.raises(WriteError):
        append_event("c1", "plan_generated", "hello", client=client)
    assert len(client.calls_of("insert")) == 1
    assert len(client.calls) == 1
    assert client.tables["audit_events"] == []


def test_append_event_failure_says_the_write_may_have_happened() -> None:
    client = FakeClient()
    client.fail("audit_events", "insert", RuntimeError("timeout"))
    with pytest.raises(WriteError, match="may or may not"):
        append_event("c1", "plan_generated", "hello", client=client)


@pytest.mark.parametrize("data", [[], None, "x", [1]])
def test_append_event_bad_or_empty_response_is_a_write_error(data: Any) -> None:
    client = FakeClient()
    client.respond_with("audit_events", "insert", data)
    with pytest.raises(WriteError):
        append_event("c1", "plan_generated", "hello", client=client)
    assert len(client.calls_of("insert")) == 1


# --------------------------------------------------------------------------
# claim_next_command
# --------------------------------------------------------------------------

T0 = "2026-11-14T09:00:00+00:00"
T1 = "2026-11-14T10:00:00+00:00"
T2 = "2026-11-14T11:00:00+00:00"
T3 = "2026-11-14T12:00:00+00:00"
T4 = "2026-11-14T13:00:00+00:00"


def test_claim_returns_the_oldest_eligible_command() -> None:
    client = FakeClient(
        commands=[
            cmd("new", T3),
            cmd("taken", T0, claimed_by="other", claimed_at=T0),
            cmd("approved", T0, status="approved"),
            cmd("oldest", T1),
            cmd("middle", T2),
        ]
    )
    row = claim_next_command("worker-1", client=client)
    assert row is not None
    assert row["id"] == "oldest"
    assert row["claimed_by"] == "worker-1"
    assert row["status"] == "received"  # claiming does not change status
    # The complete row comes back, not just the claim columns.
    assert {"id", "text", "status", "plan_id", "claimed_by", "claimed_at", "created_at"} <= set(row)


def test_claim_select_carries_the_eligibility_conditions() -> None:
    client = FakeClient(commands=[cmd("a", T1)])
    claim_next_command("worker-1", client=client)
    (select,) = client.calls_of("select")
    assert select.table == "commands"
    assert ("eq", "status", "received") in select.filters
    assert ("is", "claimed_at", None) in select.filters
    assert select.order == [("created_at", False)]  # oldest first
    assert select.limit == 5


def test_claim_update_carries_id_status_and_claimed_at_null_conditions() -> None:
    client = FakeClient(commands=[cmd("a", T1)])
    before = datetime.now(timezone.utc)
    claim_next_command("worker-1", client=client)
    after = datetime.now(timezone.utc)
    (update,) = client.calls_of("update")
    assert update.table == "commands"
    assert sorted(update.filters, key=str) == sorted(
        [("eq", "id", "a"), ("eq", "status", "received"), ("is", "claimed_at", None)], key=str
    )
    assert set(update.values) == {"claimed_by", "claimed_at"}  # no status, no updated_at
    assert update.values["claimed_by"] == "worker-1"
    claimed_at = datetime.fromisoformat(update.values["claimed_at"])
    assert claimed_at.tzinfo is not None and claimed_at.utcoffset() == timedelta(0)
    assert before <= claimed_at <= after


def test_claim_skips_a_candidate_another_worker_took() -> None:
    client = FakeClient(commands=[cmd("first", T1), cmd("second", T2), cmd("third", T3)])

    def other_worker_takes_first(c: FakeClient) -> None:
        for r in c.tables["commands"]:
            if r["id"] == "first":
                r["claimed_by"], r["claimed_at"] = "worker-2", T4

    client.before_next("update", other_worker_takes_first)
    row = claim_next_command("worker-1", client=client)
    assert row is not None and row["id"] == "second"
    assert [c.filters[0] for c in client.calls_of("update")] == [("eq", "id", "first"), ("eq", "id", "second")]
    first = next(r for r in client.tables["commands"] if r["id"] == "first")
    assert first["claimed_by"] == "worker-2"  # the other worker's claim is untouched


def test_claim_returns_none_when_nothing_is_eligible() -> None:
    client = FakeClient(
        commands=[
            cmd("taken", T1, claimed_by="w", claimed_at=T1),
            cmd("done", T1, status="completed"),
            cmd("running", T1, status="executing"),
        ]
    )
    assert claim_next_command("worker-1", client=client) is None
    assert client.calls_of("update") == []
    assert len(client.calls_of("select")) == 1


def test_claim_returns_none_on_an_empty_table() -> None:
    assert claim_next_command("worker-1", client=FakeClient()) is None


def test_claim_returns_none_when_every_candidate_is_lost_and_tries_at_most_five() -> None:
    client = FakeClient(commands=[cmd(f"c{i}", f"2026-11-14T10:0{i}:00+00:00") for i in range(7)])

    def steal_next(c: FakeClient) -> None:
        for r in c.tables["commands"]:
            if r["claimed_at"] is None:
                r["claimed_by"], r["claimed_at"] = "worker-2", T4
                return

    for _ in range(5):
        client.before_next("update", steal_next)
    # Hook runs before the update is applied, so each update finds its row already taken.
    assert claim_next_command("worker-1", client=client) is None
    ids = [c.filters[0][2] for c in client.calls_of("update")]
    assert ids == ["c0", "c1", "c2", "c3", "c4"]


@pytest.mark.parametrize("bad", ["", "   ", None, 5, b"w"])
def test_claim_rejects_a_bad_worker_id_without_a_call(bad: Any) -> None:
    client = FakeClient(commands=[cmd("a", T1)])
    with pytest.raises(ValueError):
        claim_next_command(bad, client=client)
    assert client.calls == []


def test_claim_select_failure_is_a_write_error_and_nothing_is_claimed() -> None:
    client = FakeClient(commands=[cmd("a", T1)])
    client.fail("commands", "select", RuntimeError("down"))
    with pytest.raises(WriteError):
        claim_next_command("worker-1", client=client)
    assert client.calls_of("update") == []


def test_claim_update_failure_stops_and_does_not_try_the_next_candidate() -> None:
    client = FakeClient(commands=[cmd("a", T1), cmd("b", T2)])
    client.fail("commands", "update", RuntimeError("response lost"))
    with pytest.raises(WriteError, match="may or may not"):
        claim_next_command("worker-1", client=client)
    assert len(client.calls_of("update")) == 1


@pytest.mark.parametrize("data", [None, "x", [1]])
def test_claim_unusable_responses_are_write_errors(data: Any) -> None:
    client = FakeClient(commands=[cmd("a", T1)])
    client.respond_with("commands", "select", data)
    with pytest.raises(WriteError):
        claim_next_command("worker-1", client=client)
    client = FakeClient(commands=[cmd("a", T1)])
    client.respond_with("commands", "update", data)
    with pytest.raises(WriteError):
        claim_next_command("worker-1", client=client)


# --------------------------------------------------------------------------
# set_command_status
# --------------------------------------------------------------------------


@pytest.mark.parametrize("expected,new", ALLOWED)
def test_every_allowed_transition_works_and_carries_the_expected_status(expected: str, new: str) -> None:
    client = FakeClient(commands=[cmd("c1", T1, status=expected)])
    row = set_command_status("c1", expected, new, client=client)
    assert row["status"] == new
    (update,) = client.calls
    assert (update.table, update.op) == ("commands", "update")
    assert update.filters == [("eq", "id", "c1"), ("eq", "status", expected)]
    assert update.values == {"status": new}


def test_allowed_transition_table_in_the_module_matches_the_agreed_one() -> None:
    actual = {(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
    assert actual == set(ALLOWED)
    assert set(ALLOWED_TRANSITIONS) == set(STATUSES)


@pytest.mark.parametrize("expected,new", FORBIDDEN)
def test_every_other_transition_is_refused_before_the_database(expected: str, new: str) -> None:
    client = FakeClient(commands=[cmd("c1", T1, status=expected)])
    with pytest.raises(InvalidTransitionError):
        set_command_status("c1", expected, new, client=client)
    assert client.calls == []


@pytest.mark.parametrize(
    "expected,new",
    [
        ("rejected", "approved"),  # terminal
        ("completed", "failed"),  # terminal
        ("failed", "received"),  # terminal
        ("received", "executing"),  # skips approved
        ("received", "completed"),  # skips two states
        ("approved", "completed"),  # skips executing
        ("approved", "received"),  # backwards
        ("executing", "approved"),  # backwards
        ("executing", "rejected"),  # rejected is only for received
        ("received", "received"),  # no-op
    ],
)
def test_representative_forbidden_transitions(expected: str, new: str) -> None:
    client = FakeClient()
    with pytest.raises(InvalidTransitionError):
        set_command_status("c1", expected, new, client=client)
    assert client.calls == []


@pytest.mark.parametrize("expected,new", [("bogus", "approved"), ("received", "bogus"), (None, "approved"), ("received", 5), ("RECEIVED", "approved")])
def test_unknown_statuses_are_refused_before_the_database(expected: Any, new: Any) -> None:
    client = FakeClient()
    with pytest.raises(InvalidTransitionError):
        set_command_status("c1", expected, new, client=client)
    assert client.calls == []


def test_plan_id_is_sent_only_when_given() -> None:
    client = FakeClient(commands=[cmd("c1", T1), cmd("c2", T1)])
    set_command_status("c1", "received", "approved", client=client)
    set_command_status("c2", "received", "approved", plan_id="plan-123", client=client)
    first, second = client.calls
    assert first.values == {"status": "approved"}
    assert second.values == {"status": "approved", "plan_id": "plan-123"}
    for call in client.calls:
        assert "updated_at" not in call.values  # the database trigger owns it


def test_updated_at_is_left_to_the_trigger() -> None:
    client = FakeClient(commands=[cmd("c1", T1)])
    row = set_command_status("c1", "received", "rejected", client=client)
    assert row["updated_at"] == "trigger-set"


def test_state_conflict_when_the_status_changed_meanwhile() -> None:
    client = FakeClient(commands=[cmd("c1", T1, status="approved")])
    with pytest.raises(StateConflictError):
        set_command_status("c1", "received", "rejected", client=client)
    assert client.tables["commands"][0]["status"] == "approved"  # untouched
    assert client.calls[0].filters == [("eq", "id", "c1"), ("eq", "status", "received")]


def test_state_conflict_when_the_command_does_not_exist() -> None:
    with pytest.raises(StateConflictError):
        set_command_status("missing", "received", "approved", client=FakeClient())


def test_conflict_is_distinct_from_write_error_and_both_are_store_errors() -> None:
    assert not issubclass(StateConflictError, WriteError)
    assert not issubclass(WriteError, StateConflictError)
    for cls in (InvalidEventTypeError, InvalidTransitionError, InvalidPayloadError, StateConflictError, WriteError):
        assert issubclass(cls, AuditStoreError)


def test_status_update_failure_is_a_write_error_not_a_conflict() -> None:
    client = FakeClient(commands=[cmd("c1", T1)])
    client.fail("commands", "update", RuntimeError("down"))
    with pytest.raises(WriteError) as info:
        set_command_status("c1", "received", "approved", client=client)
    assert not isinstance(info.value, StateConflictError)
    assert len(client.calls) == 1  # no retry


@pytest.mark.parametrize("data", [None, "x", [1]])
def test_status_update_unusable_response_is_a_write_error(data: Any) -> None:
    client = FakeClient(commands=[cmd("c1", T1)])
    client.respond_with("commands", "update", data)
    with pytest.raises(WriteError):
        set_command_status("c1", "received", "approved", client=client)


@pytest.mark.parametrize("command_id,plan_id", [("", None), (None, None), ("c1", ""), ("c1", 5)])
def test_bad_ids_are_value_errors_without_a_call(command_id: Any, plan_id: Any) -> None:
    client = FakeClient()
    with pytest.raises(ValueError):
        set_command_status(command_id, "received", "approved", plan_id=plan_id, client=client)
    assert client.calls == []


# --------------------------------------------------------------------------
# Errors never contain secrets
# --------------------------------------------------------------------------

LEAKY = f"upstream said: key={FAKE_SECRET} was rejected"


def _leaky_calls() -> list[Any]:
    def append(client: FakeClient) -> None:
        client.fail("audit_events", "insert", RuntimeError(LEAKY))
        append_event("c1", "plan_generated", "hello", client=client)

    def claim_select(client: FakeClient) -> None:
        client.fail("commands", "select", RuntimeError(LEAKY))
        claim_next_command("w", client=client)

    def claim_update(client: FakeClient) -> None:
        client.fail("commands", "update", RuntimeError(LEAKY))
        claim_next_command("w", client=client)

    def status(client: FakeClient) -> None:
        client.fail("commands", "update", RuntimeError(LEAKY))
        set_command_status("c1", "received", "approved", client=client)

    return [append, claim_select, claim_update, status]


@pytest.mark.parametrize("run", _leaky_calls(), ids=["append", "claim-select", "claim-update", "status"])
def test_secret_in_exception_text_is_redacted(run: Any) -> None:
    client = FakeClient(commands=[cmd("c1", T1)])
    with pytest.raises(WriteError) as info:
        run(client)
    assert "***" in str(info.value)
    assert_clean(info.value)


def test_api_error_details_and_hint_are_not_included_and_message_is_redacted() -> None:
    client = FakeClient()
    exc = FakeAPIError(f"bad {FAKE_SECRET}", code="23514", details=f"Failing row {FAKE_SECRET} PRIVATE-DETAIL")
    exc.hint = "PRIVATE-HINT"
    client.fail("audit_events", "insert", exc)
    with pytest.raises(WriteError) as info:
        append_event("c1", "plan_generated", "hello", client=client)
    text = str(info.value)
    assert "23514" in text
    assert "PRIVATE-DETAIL" not in text and "PRIVATE-HINT" not in text
    assert_clean(info.value)


def test_long_exception_text_is_capped() -> None:
    client = FakeClient()
    client.fail("audit_events", "insert", RuntimeError("x" * 10_000))
    with pytest.raises(WriteError) as info:
        append_event("c1", "plan_generated", "hello", client=client)
    assert len(str(info.value)) < audit_store.MAX_ERROR_TEXT + 200


def test_a_secret_cut_by_the_length_cap_is_not_left_as_a_fragment() -> None:
    client = FakeClient()
    text = "a" * (audit_store.MAX_ERROR_TEXT - 10) + FAKE_SECRET
    client.fail("audit_events", "insert", RuntimeError(text))
    with pytest.raises(WriteError) as info:
        append_event("c1", "plan_generated", "hello", client=client)
    assert_clean(info.value)


def test_validation_errors_do_not_echo_caller_values() -> None:
    client = FakeClient()
    with pytest.raises(InvalidEventTypeError) as e1:
        append_event("c1", FAKE_SECRET, "hello", client=client)
    assert_clean(e1.value)
    with pytest.raises(InvalidTransitionError) as e2:
        set_command_status("c1", FAKE_SECRET, "approved", client=client)
    assert_clean(e2.value)
    with pytest.raises(InvalidPayloadError) as e3:
        append_event("c1", "plan_generated", "hello", {FAKE_SECRET: object()}, client=client)
    assert_clean(e3.value)
    with pytest.raises(InvalidEventTypeError) as e4:
        append_event("c1", "x" * 5000, "hello", client=client)
    assert len(str(e4.value)) < 300


# --------------------------------------------------------------------------
# Default client factory (never reaches a real client or .env)
# --------------------------------------------------------------------------


def _fake_supabase_module(create_client: Any) -> types.ModuleType:
    module = types.ModuleType("supabase")
    module.create_client = create_client  # type: ignore[attr-defined]
    return module


def test_default_client_is_built_from_settings_like_supabase_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []
    fake = FakeClient()

    def create_client(url: str, key: str) -> FakeClient:
        seen.append((url, key))
        return fake

    monkeypatch.setitem(sys.modules, "supabase", _fake_supabase_module(create_client))
    monkeypatch.setattr(
        audit_store, "load_supabase_settings", lambda: SupabaseSettings(url="https://example.invalid", secret_key="k-fake")
    )
    append_event("c1", "plan_generated", "hello")
    assert seen == [("https://example.invalid", "k-fake")]
    assert len(fake.calls_of("insert")) == 1


def test_default_client_failure_is_a_redacted_write_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def create_client(url: str, key: str) -> Any:
        raise RuntimeError(f"bad key {FAKE_SECRET}")

    monkeypatch.setitem(sys.modules, "supabase", _fake_supabase_module(create_client))
    monkeypatch.setattr(
        audit_store, "load_supabase_settings", lambda: SupabaseSettings(url="https://example.invalid", secret_key="k")
    )
    with pytest.raises(WriteError) as info:
        append_event("c1", "plan_generated", "hello")
    assert_clean(info.value)


def test_missing_configuration_is_a_write_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise ConfigError("Missing required environment variable(s): SUPABASE_URL.")

    monkeypatch.setattr(audit_store, "load_supabase_settings", missing)
    with pytest.raises(WriteError, match="SUPABASE_URL"):
        claim_next_command("w")


# --------------------------------------------------------------------------
# Static checks on the module itself
# --------------------------------------------------------------------------


def test_module_never_deletes_upserts_calls_rpc_sleeps_or_enables_client_retry() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\.(delete|upsert|rpc|retry)\(", source)
    assert not re.search(r"\bsleep\(", source)
    assert not any(isinstance(n, ast.While) for n in ast.walk(ast.parse(source)))


def test_supabase_is_not_imported_at_module_level() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(n.split(".")[0] in {"supabase", "postgrest"} for n in names)
