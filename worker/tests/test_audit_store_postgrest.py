"""Regression tests: audit_store against the REAL postgrest builders, offline.

The FakeClient in test_audit_store.py never looks at HTTP headers, so it cannot
notice if an update stopped asking PostgREST for ``return=representation`` (in
which case ``response.data`` would be empty after a successful write and every
claim or status change would look like a lost race).

Here the real ``SyncPostgrestClient`` runs on an ``httpx.MockTransport``. The
URL is a made-up ``.invalid`` host and no request ever leaves the process. No
.env, no Supabase.

Run from worker/:  python -m pytest tests/test_audit_store_postgrest.py -q
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

pytest.importorskip("postgrest")
from postgrest import SyncPostgrestClient  # noqa: E402

from audit_store import (  # noqa: E402
    StateConflictError,
    append_event,
    claim_next_command,
    set_command_status,
)

REPRESENTATION = "return=representation"
COMMAND = {
    "id": "c1",
    "text": "command c1",
    "status": "received",
    "claimed_by": "w1",
    "claimed_at": "2026-11-14T10:00:00+00:00",
}
EVENT = {"id": "e1", "command_id": "c1", "event_type": "plan_generated", "message": "hi", "payload": {}}


class Recorder:
    """Real postgrest client on a mock transport; ``respond`` picks the JSON body."""

    def __init__(self, respond: Callable[[httpx.Request], Any]) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=respond(request))

        http = httpx.Client(
            base_url="http://postgrest.invalid/rest/v1",
            transport=httpx.MockTransport(handler),
        )
        self.client = SyncPostgrestClient("http://postgrest.invalid/rest/v1", http_client=http)

    def of(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]


def _params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params.multi_items())


def _prefer(request: httpx.Request) -> str | None:
    return request.headers.get("prefer")


def test_claim_update_asks_for_representation_and_parses_the_row() -> None:
    def respond(request: httpx.Request) -> Any:
        return [{"id": "c1"}] if request.method == "GET" else [COMMAND]

    rec = Recorder(respond)
    row = claim_next_command("w1", client=rec.client)

    assert row == COMMAND  # data of the PATCH response, not just the id
    (patch,) = rec.of("PATCH")
    assert _prefer(patch) == REPRESENTATION
    params = _params(patch)
    assert params["id"] == "eq.c1"
    assert params["status"] == "eq.received"
    assert params["claimed_at"] == "is.null"


def test_claim_select_carries_the_eligibility_conditions() -> None:
    rec = Recorder(lambda request: [])
    assert claim_next_command("w1", client=rec.client) is None

    (get,) = rec.of("GET")
    params = _params(get)
    assert params["select"] == "id"
    assert params["status"] == "eq.received"
    assert params["claimed_at"] == "is.null"
    assert params["order"] == "created_at.asc"
    assert params["limit"] == "5"
    assert rec.of("PATCH") == []  # nothing to claim, nothing written


def test_claim_empty_update_response_means_lost_race_then_none() -> None:
    def respond(request: httpx.Request) -> Any:
        return [{"id": "c1"}, {"id": "c2"}] if request.method == "GET" else []

    rec = Recorder(respond)
    assert claim_next_command("w1", client=rec.client) is None
    assert [_params(r)["id"] for r in rec.of("PATCH")] == ["eq.c1", "eq.c2"]
    assert all(_prefer(r) == REPRESENTATION for r in rec.of("PATCH"))


def test_set_command_status_asks_for_representation_and_parses_the_row() -> None:
    updated = {**COMMAND, "status": "approved"}
    rec = Recorder(lambda request: [updated])
    row = set_command_status("c1", "received", "approved", client=rec.client)

    assert row == updated
    (patch,) = rec.requests
    assert patch.method == "PATCH"
    assert _prefer(patch) == REPRESENTATION
    params = _params(patch)
    assert params["id"] == "eq.c1"
    assert params["status"] == "eq.received"
    assert "select" not in params  # nothing extra is needed to get the row back


def test_set_command_status_empty_response_is_a_state_conflict() -> None:
    rec = Recorder(lambda request: [])
    with pytest.raises(StateConflictError):
        set_command_status("c1", "received", "approved", client=rec.client)
    assert len(rec.requests) == 1  # no retry


def test_append_event_asks_for_representation_and_parses_the_row() -> None:
    rec = Recorder(lambda request: [EVENT])
    row = append_event("c1", "plan_generated", "hi", client=rec.client)

    assert row == EVENT
    (post,) = rec.requests
    assert post.method == "POST"
    assert _prefer(post) == REPRESENTATION
