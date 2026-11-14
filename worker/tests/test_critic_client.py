"""Unit tests for critic_client. Offline: every request goes to an httpx.MockTransport.

The network guard (tests/net_guard.py) is active in every test, so a request
that would reach a real transport, or a load of the real critic settings,
fails the test loudly.
"""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import math
import ssl
import time
import unittest
from typing import Any, Callable
from unittest import mock

import httpcore
import httpx

import critic_client
from config import ConfigError, CriticSettings
from critic_client import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    SECRET_HEADER,
    TOTAL_DEADLINE_SECONDS,
    build_timeout,
    review_plan,
)
from critic_gate import (
    CATEGORY_APPROVED,
    CATEGORY_COMMAND_ID_MISMATCH,
    CATEGORY_HTTP_STATUS,
    CATEGORY_MALFORMED_RESPONSE,
    CATEGORY_NETWORK_ERROR,
    CATEGORY_NOT_SENT,
    CATEGORY_PLAN_HASH_MISMATCH,
    CATEGORY_PLAN_ID_MISMATCH,
    CATEGORY_REJECTED,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAUTHORIZED,
    MAX_REASON_LENGTH,
    CriticDecision,
    build_critic_request,
)
from tests.net_guard import block_network

SECRET = "test-secret-not-real"
URL = "https://critic.test/api/critic"
COMMAND_ID = "5b0c1f7e-0000-4000-8000-000000000001"
PLAN_ID = "9d2e4a10-0000-4000-8000-000000000002"
OTHER_HASH = "sha256:" + "0" * 64
DROP = object()

Handler = Callable[[httpx.Request], httpx.Response]


def actions() -> list[dict[str, Any]]:
    return [
        {"actionId": "a1", "type": "set_joint", "target": "_6", "value": -1.0, "unit": "rad", "reason": "r"}
    ]


class CriticClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        block_network(self)
        self.request = build_critic_request(COMMAND_ID, PLAN_ID, actions())
        self.settings = CriticSettings(url=URL, shared_secret=SECRET)
        self.seen: list[httpx.Request] = []

    def verdict(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "commandId": COMMAND_ID,
            "planId": PLAN_ID,
            "planHash": self.request.plan_hash,
            "verdict": "approve",
            "reason": "All actions passed all critic safety rules.",
            "triggeredRules": [],
        }
        for key, value in overrides.items():
            if value is DROP:
                body.pop(key, None)
            else:
                body[key] = value
        return body

    def send(self, handler: Handler, **kwargs: Any) -> CriticDecision:
        def recording(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return handler(request)

        kwargs.setdefault("settings", self.settings)
        return review_plan(self.request, transport=httpx.MockTransport(recording), **kwargs)

    def respond(self, status: int = 200, body: Any = None, **kwargs: Any) -> CriticDecision:
        def handler(_request: httpx.Request) -> httpx.Response:
            if isinstance(body, (bytes, str)):
                return httpx.Response(status, content=body)
            return httpx.Response(status, json=body)

        return self.send(handler, **kwargs)

    def assert_secret_absent(self, decision: CriticDecision) -> None:
        for text in (repr(decision), json.dumps(decision.to_payload()), str(decision.error)):
            self.assertNotIn(SECRET, text)

    def assert_error(self, decision: CriticDecision, category: str, kind: str) -> None:
        self.assertEqual(decision.outcome, "error")
        self.assertFalse(decision.approved)
        self.assertEqual(decision.category, category)
        self.assertEqual(decision.error["kind"], kind)
        self.assert_secret_absent(decision)


class RequestTests(CriticClientTestCase):
    def test_sends_secret_header_and_contract_body(self) -> None:
        decision = self.respond(200, self.verdict())
        self.assertTrue(decision.approved)
        self.assertEqual(len(self.seen), 1)
        sent = self.seen[0]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(str(sent.url), URL)
        self.assertEqual(sent.headers[SECRET_HEADER], SECRET)
        self.assertEqual(sent.headers["content-type"], "application/json")
        body = json.loads(sent.content)
        self.assertEqual(body, self.request.to_body())
        self.assertEqual(set(body), {"commandId", "planId", "planHash", "actions"})

    def test_secret_only_in_the_header(self) -> None:
        self.respond(200, self.verdict())
        sent = self.seen[0]
        self.assertNotIn(SECRET, sent.content.decode())
        self.assertNotIn(SECRET, str(sent.url))
        others = {k: v for k, v in sent.headers.items() if k.lower() != SECRET_HEADER}
        self.assertNotIn(SECRET, json.dumps(others))
        self.assertNotIn("authorization", {k.lower() for k in sent.headers})

    def test_settings_repr_hides_the_secret(self) -> None:
        self.assertNotIn(SECRET, repr(self.settings))

    def test_client_is_built_without_redirects_and_without_environment(self) -> None:
        with mock.patch.object(critic_client.httpx, "AsyncClient", wraps=httpx.AsyncClient) as client_cls:
            self.respond(200, self.verdict())
        kwargs = client_cls.call_args.kwargs
        self.assertIs(kwargs["follow_redirects"], False)
        self.assertIs(kwargs["trust_env"], False)
        timeout = kwargs["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)


class TimeoutConfigTests(CriticClientTestCase):
    def test_default_timeout_is_finite_on_every_phase(self) -> None:
        timeout = build_timeout()
        for phase in (timeout.connect, timeout.read, timeout.write, timeout.pool):
            self.assertIsNotNone(phase)
            self.assertTrue(math.isfinite(phase))
            self.assertEqual(phase, DEFAULT_TIMEOUT_SECONDS)

    def test_request_carries_the_finite_timeout(self) -> None:
        self.respond(200, self.verdict())
        sent_timeout = self.seen[0].extensions["timeout"]
        self.assertEqual(set(sent_timeout), {"connect", "read", "write", "pool"})
        for value in sent_timeout.values():
            self.assertIsNotNone(value)
            self.assertTrue(math.isfinite(value))

    def test_invalid_timeouts_are_refused(self) -> None:
        for value in (0, -1, 0.0, math.nan, math.inf, None, True, "10", 61, 1e9):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_timeout(value)  # type: ignore[arg-type]

    def test_invalid_timeout_fails_closed_without_a_request(self) -> None:
        for value in (0, math.inf, None):
            with self.subTest(value=value):
                self.seen.clear()
                decision = self.respond(200, self.verdict(), timeout_seconds=value)
                self.assert_error(decision, CATEGORY_NOT_SENT, "timeout")
                self.assertEqual(self.seen, [])


class TransportFailureTests(CriticClientTestCase):
    def test_timeout_is_an_error(self) -> None:
        for exc_type in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            with self.subTest(exc=exc_type.__name__):
                self.seen.clear()

                def handler(request: httpx.Request, exc_type: Any = exc_type) -> httpx.Response:
                    raise exc_type(f"timed out {SECRET}", request=request)

                decision = self.send(handler)
                self.assert_error(decision, CATEGORY_TIMEOUT, "timeout")
                self.assertEqual(len(self.seen), 1)  # no retry

    def test_connection_failure_is_an_error(self) -> None:
        for exc_type in (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            with self.subTest(exc=exc_type.__name__):
                self.seen.clear()

                def handler(request: httpx.Request, exc_type: Any = exc_type) -> httpx.Response:
                    raise exc_type(f"boom {SECRET}", request=request)

                decision = self.send(handler)
                self.assert_error(decision, CATEGORY_NETWORK_ERROR, "timeout")
                self.assertIn(exc_type.__name__, decision.error["detail"])
                self.assertEqual(len(self.seen), 1)  # no retry

    def test_unexpected_exception_fails_closed(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise RuntimeError(f"surprise {SECRET}")

        decision = self.send(handler)
        self.assertEqual(decision.outcome, "error")
        self.assert_secret_absent(decision)


class StatusTests(CriticClientTestCase):
    def test_401_is_an_unauthorized_error(self) -> None:
        body = {"commandId": "unknown", "planId": "unknown", "verdict": "reject",
                "reason": "Unauthorized.", "triggeredRules": ["unauthorized"]}
        decision = self.respond(401, body)
        self.assert_error(decision, CATEGORY_UNAUTHORIZED, "http_status")
        self.assertEqual(decision.error["httpStatus"], 401)
        self.assertNotIn("response", decision.to_payload())

    def test_other_non_200_statuses_are_errors_even_with_an_approve_body(self) -> None:
        for status in (201, 204, 400, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.seen.clear()
                decision = self.respond(status, self.verdict())
                self.assert_error(decision, CATEGORY_HTTP_STATUS, "http_status")
                self.assertEqual(decision.error["httpStatus"], status)
                self.assertEqual(len(self.seen), 1)

    def test_redirects_are_errors_and_are_not_followed(self) -> None:
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.seen.clear()

                def handler(request: httpx.Request, status: int = status) -> httpx.Response:
                    if request.url.host != "critic.test":
                        return httpx.Response(200, json=self.verdict())
                    return httpx.Response(
                        status, headers={"location": "https://evil.test/api/critic"}
                    )

                decision = self.send(handler)
                self.assert_error(decision, CATEGORY_HTTP_STATUS, "http_status")
                self.assertEqual(decision.error["httpStatus"], status)
                self.assertEqual(decision.error["detail"], "redirect not followed")
                # One request, to the configured host only: the secret never left it.
                self.assertEqual([r.url.host for r in self.seen], ["critic.test"])


class BodyTests(CriticClientTestCase):
    def test_malformed_json_is_an_error(self) -> None:
        for raw in (b"", b"not json", b"{", b'{"verdict": "approve"', b"NaN", b"\xff\xfe"):
            with self.subTest(raw=raw):
                decision = self.respond(200, raw)
                self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")
                self.assertEqual(decision.error["httpStatus"], 200)

    def test_non_object_json_is_an_error(self) -> None:
        for body in ([], [self.verdict()], "approve", 1, True, None):
            with self.subTest(body=body):
                decision = self.respond(200, json.dumps(body).encode())
                self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")

    def test_missing_required_fields_are_errors(self) -> None:
        for field in ("commandId", "planId", "verdict", "reason", "triggeredRules", "planHash"):
            with self.subTest(field=field):
                decision = self.respond(200, self.verdict(**{field: DROP}))
                self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")

    def test_unexpected_verdicts_are_errors(self) -> None:
        for value in ("APPROVE", "approved", "Approve", " approve", "", "yes", None, True, 1):
            with self.subTest(value=value):
                decision = self.respond(200, self.verdict(verdict=value))
                self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")

    def test_oversized_body_is_an_error(self) -> None:
        body = self.verdict(reason="x" * (MAX_RESPONSE_BYTES + 1))
        decision = self.respond(200, body)
        self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")


class VerdictTests(CriticClientTestCase):
    def test_exact_approve_with_matching_ids_and_hash_approves(self) -> None:
        decision = self.respond(200, self.verdict())
        self.assertTrue(decision.approved)
        self.assertEqual(decision.category, CATEGORY_APPROVED)
        self.assertEqual(
            (decision.command_id, decision.plan_id, decision.plan_hash),
            (COMMAND_ID, PLAN_ID, self.request.plan_hash),
        )
        self.assertEqual(decision.to_payload()["outcome"], "approve")
        self.assertEqual(decision.to_payload()["planHash"], self.request.plan_hash)

    def test_valid_reject_is_a_reject(self) -> None:
        body = self.verdict(
            verdict="reject",
            reason="Action a1: value is outside the limits for _6.",
            triggeredRules=["joint_limit_exceeded"],
        )
        decision = self.respond(200, body)
        self.assertEqual(decision.outcome, "reject")
        self.assertEqual(decision.category, CATEGORY_REJECTED)
        self.assertFalse(decision.approved)
        payload = decision.to_payload()
        self.assertEqual(payload["response"]["triggeredRules"], ["joint_limit_exceeded"])
        self.assertNotIn("error", payload)

    def test_approve_with_triggered_rules_is_invalid_body(self) -> None:
        for rules in (["joint_limit_exceeded"], ["unit_mismatch", "unknown_joint"]):
            with self.subTest(rules=rules):
                decision = self.respond(200, self.verdict(triggeredRules=rules))
                self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")
                self.assertEqual(decision.error["detail"], "an approve verdict has triggered rules")

    def test_wrong_command_id_is_an_error(self) -> None:
        decision = self.respond(200, self.verdict(commandId="other"))
        self.assert_error(decision, CATEGORY_COMMAND_ID_MISMATCH, "invalid_body")

    def test_wrong_plan_id_is_an_error(self) -> None:
        decision = self.respond(200, self.verdict(planId="other"))
        self.assert_error(decision, CATEGORY_PLAN_ID_MISMATCH, "invalid_body")

    def test_wrong_plan_hash_is_an_error_even_when_the_verdict_approves(self) -> None:
        for verdict in ("approve", "reject"):
            with self.subTest(verdict=verdict):
                decision = self.respond(200, self.verdict(verdict=verdict, planHash=OTHER_HASH))
                self.assert_error(decision, CATEGORY_PLAN_HASH_MISMATCH, "invalid_body")
                payload = decision.to_payload()
                self.assertEqual(payload["planHash"], self.request.plan_hash)
                self.assertEqual(payload["response"]["planHash"], OTHER_HASH)


class SanitizationTests(CriticClientTestCase):
    def test_payload_keeps_only_agreed_fields_and_caps_text(self) -> None:
        body = self.verdict(
            verdict="reject",
            reason="r" * 5000,
            triggeredRules=["joint_limit_exceeded" * 10] * 100,
            headers={"x-critic-secret": SECRET},
            debug="stack trace here",
        )
        decision = self.respond(200, body)
        response = decision.to_payload()["response"]
        self.assertLessEqual(
            set(response), {"commandId", "planId", "planHash", "verdict", "reason", "triggeredRules"}
        )
        self.assertEqual(len(response["reason"]), MAX_REASON_LENGTH)
        self.assertLessEqual(len(response["triggeredRules"]), 20)
        self.assertTrue(all(len(rule) <= 64 for rule in response["triggeredRules"]))
        self.assert_secret_absent(decision)

    def test_echoed_secret_is_redacted_before_capping(self) -> None:
        # The secret straddles the 200-character cap: redaction runs first.
        reason = "x" * (MAX_REASON_LENGTH - 5) + SECRET
        body = self.verdict(verdict="reject", reason=reason, triggeredRules=[SECRET])
        decision = self.respond(200, body)
        response = decision.to_payload()["response"]
        self.assertEqual(response["triggeredRules"], ["***"])
        self.assertNotIn(SECRET[:5], response["reason"][-5:])
        self.assert_secret_absent(decision)


class ConfigurationTests(CriticClientTestCase):
    def test_missing_configuration_fails_closed_without_a_request(self) -> None:
        with mock.patch.object(
            critic_client, "load_critic_settings", side_effect=ConfigError("Missing CRITIC_URL")
        ):
            decision = review_plan(self.request, transport=httpx.MockTransport(self.fail_if_called))
        self.assert_error(decision, CATEGORY_NOT_SENT, "timeout")
        self.assertIn("not set", decision.error["detail"])

    def fail_if_called(self, _request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    def test_empty_or_unsafe_settings_fail_closed_without_a_request(self) -> None:
        cases = {
            "empty url": CriticSettings(url="", shared_secret=SECRET),
            "blank secret": CriticSettings(url=URL, shared_secret="   "),
            "plain http": CriticSettings(url="http://critic.test/api/critic", shared_secret=SECRET),
            "credentials": CriticSettings(url="https://u:p@critic.test/api/critic", shared_secret=SECRET),
            "query": CriticSettings(url=URL + "?k=v", shared_secret=SECRET),
            "fragment": CriticSettings(url=URL + "#x", shared_secret=SECRET),
            "no host": CriticSettings(url="https:///api/critic", shared_secret=SECRET),
            "other scheme": CriticSettings(url="ftp://critic.test/api", shared_secret=SECRET),
        }
        for name, settings in cases.items():
            with self.subTest(case=name):
                decision = review_plan(
                    self.request, settings=settings, transport=httpx.MockTransport(self.fail_if_called)
                )
                self.assert_error(decision, CATEGORY_NOT_SENT, "timeout")

    def test_loopback_http_is_allowed_for_local_development(self) -> None:
        for url in ("http://localhost:3000/api/critic", "http://127.0.0.1:3000/api/critic"):
            with self.subTest(url=url):
                decision = self.respond(
                    200, self.verdict(), settings=CriticSettings(url=url, shared_secret=SECRET)
                )
                self.assertTrue(decision.approved)

    def test_no_valid_request_fails_closed(self) -> None:
        decision = review_plan(None, settings=self.settings)  # type: ignore[arg-type]
        self.assertEqual(decision.outcome, "error")


class NoRetryTests(CriticClientTestCase):
    def test_exactly_one_request_for_every_failure(self) -> None:
        def raising(exc_type: Any) -> Handler:
            def handler(request: httpx.Request) -> httpx.Response:
                raise exc_type("x", request=request)
            return handler

        handlers: dict[str, Handler] = {
            "timeout": raising(httpx.ReadTimeout),
            "connect": raising(httpx.ConnectError),
            "500": lambda _r: httpx.Response(500),
            "401": lambda _r: httpx.Response(401),
            "302": lambda _r: httpx.Response(302, headers={"location": URL}),
            "malformed": lambda _r: httpx.Response(200, content=b"nope"),
            "mismatch": lambda _r: httpx.Response(200, json=self.verdict(planHash=OTHER_HASH)),
            "reject": lambda _r: httpx.Response(200, json=self.verdict(verdict="reject")),
        }
        for name, handler in handlers.items():
            with self.subTest(case=name):
                self.seen.clear()
                decision = self.send(handler)
                self.assertFalse(decision.approved)
                self.assertEqual(len(self.seen), 1)


class FakeClock:
    """A controllable monotonic clock: time moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TrackingStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A streamed body that records how much was read and whether it was closed.

    ``step`` advances a FakeClock per chunk (deterministic checkpoints);
    ``delay`` really waits that long (asyncio.sleep) before each chunk, so the
    total-deadline cancellation can be observed with a short deadline.
    """

    def __init__(
        self,
        chunks: list[bytes],
        clock: FakeClock | None = None,
        step: float = 0.0,
        delay: float = 0.0,
    ) -> None:
        self.chunks = chunks
        self.clock = clock
        self.step = step
        self.delay = delay
        self.yielded = 0
        self.closed = False
        self.cancelled = False

    def __iter__(self) -> Any:
        for chunk in self.chunks:
            if self.clock is not None:
                self.clock.advance(self.step)  # this chunk took `step` seconds to arrive
            self.yielded += 1
            yield chunk

    async def __aiter__(self) -> Any:
        try:
            for chunk in self.chunks:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.clock is not None:
                    self.clock.advance(self.step)
                self.yielded += 1
                yield chunk
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


def split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


class DeadlineTests(CriticClientTestCase):
    """Total wall-clock deadline (Abhijit's review of a173b98). No real waiting."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock()

    def body_bytes(self, **overrides: Any) -> bytes:
        return json.dumps(self.verdict(**overrides)).encode()

    def streamed(self, stream: TrackingStream, before_headers: float = 0.0) -> CriticDecision:
        def handler(_request: httpx.Request) -> httpx.Response:
            self.clock.advance(before_headers)  # time to connect and get headers
            return httpx.Response(200, stream=stream)

        return self.send(handler, clock=self.clock)

    def assert_deadline_error(self, decision: CriticDecision) -> None:
        self.assert_error(decision, CATEGORY_TIMEOUT, "timeout")
        self.assertIn("total deadline", decision.error["detail"])
        self.assertEqual(len(self.seen), 1)  # no retry

    def test_default_deadline_is_ten_seconds_on_a_monotonic_clock(self) -> None:
        self.assertEqual(TOTAL_DEADLINE_SECONDS, 10.0)
        params = inspect.signature(review_plan).parameters
        self.assertEqual(params["deadline_seconds"].default, TOTAL_DEADLINE_SECONDS)
        self.assertIs(params["clock"].default, time.monotonic)

    def test_response_within_the_deadline_is_judged(self) -> None:
        stream = TrackingStream(split(self.body_bytes(), 16), self.clock, step=0.1)
        decision = self.streamed(stream, before_headers=1.0)
        self.assertTrue(decision.approved)
        self.assertTrue(stream.closed)

    def test_slow_response_headers_exceed_the_deadline(self) -> None:
        stream = TrackingStream([self.body_bytes()], self.clock)
        decision = self.streamed(stream, before_headers=10.5)
        self.assert_deadline_error(decision)
        self.assertEqual(stream.yielded, 0)  # the body was not read
        self.assertTrue(stream.closed)

    def test_slow_body_exceeds_the_deadline(self) -> None:
        stream = TrackingStream(split(self.body_bytes(), 16), self.clock, step=11.0)
        decision = self.streamed(stream)
        self.assert_deadline_error(decision)
        self.assertEqual(stream.yielded, 1)  # stopped at the first late chunk
        self.assertTrue(stream.closed)

    def test_small_chunks_each_within_read_timeout_but_over_the_total(self) -> None:
        # Every chunk arrives 0.25 s after the last: far inside the 10 s read
        # timeout, but 200 of them take 50 s in total.
        chunks = split(self.body_bytes(reason="x" * 150), 1)
        self.assertGreaterEqual(len(chunks), 200)
        stream = TrackingStream(chunks, self.clock, step=0.25)
        decision = self.streamed(stream)
        self.assert_deadline_error(decision)
        self.assertLess(stream.yielded, len(chunks))  # reading stopped early
        self.assertLessEqual(stream.yielded, 41)  # 10 s / 0.25 s, plus the chunk that crossed it
        self.assertTrue(stream.closed)

    def test_deadline_error_is_never_an_approval(self) -> None:
        for before, step in ((10.5, 0.0), (0.0, 11.0), (9.0, 0.6)):
            with self.subTest(before=before, step=step):
                self.seen.clear()
                self.clock = FakeClock()
                stream = TrackingStream(split(self.body_bytes(), 32), self.clock, step=step)
                decision = self.streamed(stream, before_headers=before)
                self.assertFalse(decision.approved)
                self.assertEqual(decision.outcome, "error")

    def test_deadline_also_applies_to_non_200_and_redirects(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            self.clock.advance(12.0)
            return httpx.Response(302, headers={"location": URL})

        decision = self.send(handler, clock=self.clock)
        self.assert_deadline_error(decision)

    def test_phase_timeouts_are_capped_at_the_deadline(self) -> None:
        self.respond(200, self.verdict(), deadline_seconds=2.5)
        for value in self.seen[0].extensions["timeout"].values():
            self.assertEqual(value, 2.5)
        self.seen.clear()
        self.respond(200, self.verdict(), deadline_seconds=30.0, timeout_seconds=5.0)
        for value in self.seen[0].extensions["timeout"].values():
            self.assertEqual(value, 5.0)

    def test_invalid_deadlines_fail_closed_without_a_request(self) -> None:
        for value in (0, -1, math.nan, math.inf, None, True, "10", 61):
            with self.subTest(value=value):
                self.seen.clear()
                decision = self.respond(200, self.verdict(), deadline_seconds=value)
                self.assert_error(decision, CATEGORY_NOT_SENT, "timeout")
                self.assertEqual(self.seen, [])

    def test_no_retry_after_a_deadline_error(self) -> None:
        stream = TrackingStream(split(self.body_bytes(), 8), self.clock, step=5.0)
        self.streamed(stream)
        self.assertEqual(len(self.seen), 1)


class EncodingAndSizeTests(CriticClientTestCase):
    """Accept-Encoding identity and the raw body cap (Abhijit's review of a173b98)."""

    def exact_body(self, size: int) -> bytes:
        body = self.verdict(reason="")
        base = len(json.dumps(body, separators=(",", ":")).encode())
        body["reason"] = "x" * (size - base)
        data = json.dumps(body, separators=(",", ":")).encode()
        self.assertEqual(len(data), size)
        return data

    def stream_response(
        self, stream: TrackingStream, headers: dict[str, str] | None = None
    ) -> CriticDecision:
        return self.send(lambda _r: httpx.Response(200, headers=headers or {}, stream=stream))

    def test_request_asks_for_identity_encoding(self) -> None:
        self.respond(200, self.verdict())
        self.assertEqual(self.seen[0].headers["accept-encoding"], "identity")

    def test_identity_response_below_the_limit_succeeds(self) -> None:
        for headers in ({}, {"content-encoding": "identity"}, {"content-encoding": " Identity "}):
            with self.subTest(headers=headers):
                stream = TrackingStream(split(json.dumps(self.verdict()).encode(), 64))
                decision = self.stream_response(stream, headers)
                self.assertTrue(decision.approved)
                self.assertTrue(stream.closed)

    def test_body_of_exactly_the_limit_is_accepted(self) -> None:
        data = self.exact_body(MAX_RESPONSE_BYTES)
        # Streamed without a Content-Length, and in memory with one.
        self.assertTrue(self.stream_response(TrackingStream(split(data, 4096))).approved)
        self.assertTrue(self.respond(200, data).approved)

    def test_body_one_byte_over_the_limit_is_rejected(self) -> None:
        data = self.exact_body(MAX_RESPONSE_BYTES + 1)
        stream = TrackingStream(split(data, 4096))
        with mock.patch.object(critic_client.json, "loads", wraps=json.loads) as loads:
            decision = self.stream_response(stream)
            self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")
            self.assertEqual(decision.error["detail"], "response body is too large")
            loads.assert_not_called()
        self.assertTrue(stream.closed)
        self.assertEqual(self.respond(200, data).error["detail"], "response body is too large")

    def test_reading_stops_at_the_limit(self) -> None:
        chunks = [b"x" * 1024] * 1000  # about 1 MB offered
        stream = TrackingStream(chunks)
        decision = self.stream_response(stream)
        self.assertEqual(decision.error["detail"], "response body is too large")
        self.assertEqual(stream.yielded, MAX_RESPONSE_BYTES // 1024 + 1)
        self.assertTrue(stream.closed)

    def test_declared_length_over_the_limit_is_refused_before_reading(self) -> None:
        stream = TrackingStream([b"{}"])
        decision = self.stream_response(stream, {"content-length": str(MAX_RESPONSE_BYTES + 1)})
        self.assertEqual(decision.error["detail"], "response body is too large")
        self.assertEqual(stream.yielded, 0)
        self.assertTrue(stream.closed)

    def test_invalid_declared_length_is_an_error(self) -> None:
        stream = TrackingStream([b"{}"])
        decision = self.stream_response(stream, {"content-length": "lots"})
        self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")
        self.assertEqual(stream.yielded, 0)

    def test_gzip_response_is_rejected_without_decompressing(self) -> None:
        compressed = gzip.compress(json.dumps(self.verdict()).encode())
        stream = TrackingStream([compressed])
        with mock.patch.object(critic_client.json, "loads", wraps=json.loads) as loads:
            decision = self.stream_response(stream, {"content-encoding": "gzip"})
            loads.assert_not_called()
        self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")
        self.assertEqual(decision.error["detail"], "unsupported response content-encoding")
        self.assertEqual(stream.yielded, 0)
        self.assertTrue(stream.closed)

    def test_unexpected_content_encodings_are_rejected(self) -> None:
        for encoding in ("br", "deflate", "zstd", "x-custom", "", "identity, gzip", "gzip, identity"):
            with self.subTest(encoding=encoding):
                stream = TrackingStream([json.dumps(self.verdict()).encode()])
                decision = self.stream_response(stream, {"content-encoding": encoding})
                self.assert_error(decision, CATEGORY_MALFORMED_RESPONSE, "invalid_body")
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed)

    def test_strict_json_parser_is_unchanged(self) -> None:
        for raw in (b'{"verdict": NaN}', b"Infinity", b"[1, 2"):
            with self.subTest(raw=raw):
                decision = self.stream_response(TrackingStream([raw]))
                self.assertEqual(decision.error["detail"], "response body is not valid JSON")


SHORT_DEADLINE = 0.3  # seconds; injected so no test waits 10 real seconds
_TEST_SSL_CONTEXT = ssl.create_default_context()  # never used for a real handshake
GENEROUS = 3.0  # an upper bound on elapsed time that only a missing cancellation would exceed


class TrickleNetworkStream(httpcore.AsyncNetworkStream):
    """A scripted network connection: (delay, bytes) segments, one per read.

    Lets the REAL httpcore/h11 HTTP/1.1 parser receive headers or body bytes
    as slowly as a misbehaving server would, with no socket at all.
    """

    def __init__(self, segments: list[tuple[float, bytes]]) -> None:
        self.segments = list(segments)
        self.reads = 0
        self.read_timeouts: list[float | None] = []
        self.closed = False
        self.cancelled = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.read_timeouts.append(timeout)
        if not self.segments:
            return b""
        delay, data = self.segments.pop(0)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.reads += 1
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None) -> Any:
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class TrickleBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, stream: TrickleNetworkStream) -> None:
        self.stream = stream

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> Any:
        return self.stream

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _HttpcoreBody(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    async def __aiter__(self) -> Any:
        async for chunk in self._response.aiter_stream():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class HttpcoreTransport(httpx.AsyncBaseTransport):
    """Test-only: httpx -> real httpcore AsyncHTTPConnection -> scripted backend.

    Public httpcore API only. Used to prove that the total deadline cancels the
    real HTTP/1.1 header and body parsing, not just a mock handler.
    """

    def __init__(self, stream: TrickleNetworkStream) -> None:
        self.stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        connection = httpcore.AsyncHTTPConnection(
            httpcore.Origin(b"https", request.url.host.encode(), 443),
            # Built once at import: loading the CA bundle would otherwise eat
            # the short test deadline before the first byte is parsed.
            ssl_context=_TEST_SSL_CONTEXT,
            network_backend=TrickleBackend(self.stream),
        )
        response = await connection.handle_async_request(
            httpcore.Request(
                request.method,
                str(request.url),
                headers=request.headers.raw,
                content=request.content,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status, headers=response.headers, stream=_HttpcoreBody(response)
        )


class TotalDeadlineCancellationTests(CriticClientTestCase):
    """The total deadline CANCELS a slow exchange (real time, short deadline)."""

    def review(self, transport: httpx.AsyncBaseTransport) -> tuple[CriticDecision, float]:
        start = time.monotonic()
        decision = review_plan(
            self.request, settings=self.settings, transport=transport, deadline_seconds=SHORT_DEADLINE
        )
        return decision, time.monotonic() - start

    def recording(self, handler: Any) -> httpx.MockTransport:
        async def wrapped(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return await handler(request)

        return httpx.MockTransport(wrapped)

    def assert_cancelled(self, decision: CriticDecision, elapsed: float) -> None:
        self.assert_error(decision, CATEGORY_TIMEOUT, "timeout")
        self.assertFalse(decision.approved)
        self.assertIn("total deadline", decision.error["detail"])
        self.assertGreaterEqual(elapsed, SHORT_DEADLINE * 0.9)
        self.assertLess(elapsed, GENEROUS)
        self.assertEqual(len(self.seen), 1)  # no retry

    def test_normal_request_completes_within_the_deadline(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json=self.verdict())

        decision, elapsed = self.review(self.recording(handler))
        self.assertTrue(decision.approved)
        self.assertLess(elapsed, SHORT_DEADLINE)

    def test_slow_headers_are_cancelled_at_the_deadline(self) -> None:
        state = {"cancelled": False}

        async def handler(_request: httpx.Request) -> httpx.Response:
            try:
                await asyncio.sleep(30)  # headers that never come in time
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return httpx.Response(200, json=self.verdict())

        decision, elapsed = self.review(self.recording(handler))
        self.assert_cancelled(decision, elapsed)
        self.assertTrue(state["cancelled"])

    def test_header_trickle_cannot_keep_the_request_alive(self) -> None:
        fragments = {"sent": 0}

        async def handler(_request: httpx.Request) -> httpx.Response:
            for _ in range(1000):  # 1000 fragments, 0.05 s apart: 50 s in total
                await asyncio.sleep(0.05)  # each far inside the 10 s read timeout
                fragments["sent"] += 1
            return httpx.Response(200, json=self.verdict())

        decision, elapsed = self.review(self.recording(handler))
        self.assert_cancelled(decision, elapsed)
        self.assertLess(fragments["sent"], 1000)

    def test_header_trickle_through_the_real_http11_parser_is_cancelled(self) -> None:
        # Real httpcore/h11 parsing: the status line, then a header that grows
        # by one byte every 0.05 s and never ends. Every read returns long
        # before its 10 s read timeout; only the total deadline can stop it.
        segments = [(0.0, b"HTTP/1.1 200 OK\r\nX-Pad: ")] + [(0.05, b"a")] * 2000
        stream = TrickleNetworkStream(segments)
        transport = HttpcoreTransport(stream)
        start = time.monotonic()
        decision = review_plan(
            self.request,
            settings=CriticSettings(url="https://critic.invalid/api/critic", shared_secret=SECRET),
            transport=transport,
            deadline_seconds=SHORT_DEADLINE,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(decision.category, CATEGORY_TIMEOUT)
        self.assertIn("total deadline", decision.error["detail"])
        self.assertLess(elapsed, GENEROUS)
        self.assertGreaterEqual(stream.reads, 3)  # h11 really was parsing the headers
        self.assertLess(stream.reads, 2000)
        self.assertTrue(stream.cancelled)  # the in-progress read was cancelled
        self.assertTrue(stream.closed)  # and the connection closed
        self.assertTrue(all(t == DEFAULT_TIMEOUT_SECONDS or t == SHORT_DEADLINE for t in stream.read_timeouts))

    def test_body_trickle_through_the_real_http11_parser_is_cancelled(self) -> None:
        body = json.dumps(self.verdict()).encode()
        head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        segments = [(0.0, head.encode())] + [(0.05, body[i : i + 1]) for i in range(len(body))]
        stream = TrickleNetworkStream(segments)
        decision = review_plan(
            self.request,
            settings=CriticSettings(url="https://critic.invalid/api/critic", shared_secret=SECRET),
            transport=HttpcoreTransport(stream),
            deadline_seconds=SHORT_DEADLINE,
        )
        self.assertEqual(decision.category, CATEGORY_TIMEOUT)
        self.assertFalse(decision.approved)
        self.assertGreaterEqual(stream.reads, 3)  # headers parsed, body bytes arriving
        self.assertLess(stream.reads, len(segments))
        self.assertTrue(stream.cancelled)
        self.assertTrue(stream.closed)

    def test_slow_body_read_is_cancelled_and_the_response_closed(self) -> None:
        stream = TrackingStream([json.dumps(self.verdict()).encode()], delay=30)

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        decision, elapsed = self.review(self.recording(handler))
        self.assert_cancelled(decision, elapsed)
        self.assertTrue(stream.cancelled)
        self.assertEqual(stream.yielded, 0)
        self.assertTrue(stream.closed)

    def test_repeated_small_chunks_trigger_the_total_deadline(self) -> None:
        chunks = split(json.dumps(self.verdict(reason="x" * 150)).encode(), 1)
        stream = TrackingStream(chunks, delay=0.02)  # each chunk 0.02 s: total seconds

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        decision, elapsed = self.review(self.recording(handler))
        self.assert_cancelled(decision, elapsed)
        self.assertLess(stream.yielded, len(chunks))
        self.assertTrue(stream.closed)

    def test_refused_inside_a_running_event_loop(self) -> None:
        async def inside_a_loop() -> CriticDecision:
            return review_plan(
                self.request, settings=self.settings, transport=httpx.MockTransport(self.fail_if_called)
            )

        decision = asyncio.run(inside_a_loop())
        self.assertEqual(decision.outcome, "error")
        self.assertIn("RuntimeError", decision.error["detail"])

    def fail_if_called(self, _request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    def test_each_call_uses_and_closes_its_own_event_loop(self) -> None:
        loops: list[asyncio.AbstractEventLoop] = []
        real_new_loop = asyncio.new_event_loop

        def tracking_new_loop() -> asyncio.AbstractEventLoop:
            loop = real_new_loop()
            loops.append(loop)
            return loop

        with mock.patch.object(critic_client.asyncio, "new_event_loop", tracking_new_loop):
            self.respond(200, self.verdict())
            self.respond(200, self.verdict())
        self.assertEqual(len(loops), 2)
        self.assertTrue(all(loop.is_closed() for loop in loops))


if __name__ == "__main__":
    unittest.main()
