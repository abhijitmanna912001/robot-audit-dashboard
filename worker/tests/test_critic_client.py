"""Unit tests for critic_client. Offline: every request goes to an httpx.MockTransport.

The network guard (tests/net_guard.py) is active in every test, so a request
that would reach a real transport, or a load of the real critic settings,
fails the test loudly.
"""

from __future__ import annotations

import json
import math
import unittest
from typing import Any, Callable
from unittest import mock

import httpx

import critic_client
from config import ConfigError, CriticSettings
from critic_client import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    SECRET_HEADER,
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
        with mock.patch.object(critic_client.httpx, "Client", wraps=httpx.Client) as client_cls:
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


if __name__ == "__main__":
    unittest.main()
