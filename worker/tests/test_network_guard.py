"""Proof that the process-wide network guard (tests/net_guard.py) works.

Every target here is local or can never resolve: the reserved ``.invalid``
domain (RFC 6761) and loopback on the discard port 9. No test sends anything
to an external host. Where a test names the production critic URL, two further
independent blocks sit underneath the layer under test, so nothing can leave
the process even if that layer were broken.
"""

from __future__ import annotations

import asyncio
import http.client
import importlib.util
import socket
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

import httpcore
import httpx

import audit_store
import command_processor
import config
import critic_client
from config import ConfigError, CriticSettings
from critic_gate import build_critic_request
from tests import net_guard
from tests.net_guard import BLOCKED_ATTEMPTS, NetworkBlocked
from tests.test_audit_store import FakeClient
from tests.test_command_processor import COMMAND_ID, WORKER_ID, command_row

WORKER_DIR = Path(__file__).resolve().parents[1]
INVALID_HOST = "guard-test.invalid"
LOOPBACK_URL = "http://127.0.0.1:9/"
INVALID_URL = f"https://{INVALID_HOST}/api/critic"
PRODUCTION_CRITIC_URL = "https://robot-audit-dashboard.vercel.app/api/critic"
TEST_SECRET = "test-secret-not-real"


class GuardTestCase(unittest.TestCase):
    def assert_blocked(self, action: Any, contains: str) -> None:
        before = len(BLOCKED_ATTEMPTS)
        with self.assertRaises(NetworkBlocked):
            action()
        self.assertGreater(len(BLOCKED_ATTEMPTS), before, "nothing was recorded as blocked")
        self.assertIn(contains, BLOCKED_ATTEMPTS[before])


class InstallationTests(GuardTestCase):
    def test_installed_before_any_test_module(self) -> None:
        self.assertTrue(net_guard.is_installed())
        self.assertEqual(net_guard.TEST_MODULES_IMPORTED_BEFORE_INSTALL, ())

    def test_blocked_attempt_is_not_an_ordinary_exception(self) -> None:
        # Fail-closed `except Exception` handlers cannot swallow it.
        self.assertFalse(issubclass(NetworkBlocked, Exception))
        try:
            try:
                socket.getaddrinfo(INVALID_HOST, 80)
            except Exception:  # noqa: BLE001 - what application code does
                self.fail("NetworkBlocked was caught by `except Exception`")
        except NetworkBlocked:
            pass

    def test_install_is_idempotent(self) -> None:
        connect = socket.socket.connect
        net_guard.install()
        self.assertIs(socket.socket.connect, connect)


class HttpxTests(GuardTestCase):
    def test_sync_client_default_transport_is_blocked(self) -> None:
        for url in (INVALID_URL, LOOPBACK_URL):
            with self.subTest(url=url), httpx.Client() as client:
                self.assert_blocked(lambda: client.get(url), "httpx.HTTPTransport")

    def test_sync_block_happens_before_httpcore_or_a_socket(self) -> None:
        with mock.patch.object(
            httpcore.ConnectionPool, "handle_request", side_effect=AssertionError("reached httpcore")
        ) as pool, mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("reached a socket")
        ) as connect, httpx.Client() as client:
            self.assert_blocked(lambda: client.post(LOOPBACK_URL, json={}), "httpx.HTTPTransport")
        pool.assert_not_called()
        connect.assert_not_called()

    def test_async_client_default_transport_is_blocked(self) -> None:
        async def fetch(url: str) -> None:
            async with httpx.AsyncClient() as client:
                await client.get(url)

        for url in (INVALID_URL, LOOPBACK_URL):
            with self.subTest(url=url):
                self.assert_blocked(lambda: asyncio.run(fetch(url)), "httpx.AsyncHTTPTransport")

    def test_async_block_happens_before_httpcore(self) -> None:
        async def fetch() -> None:
            async with httpx.AsyncClient() as client:
                await client.post(LOOPBACK_URL, json={})

        with mock.patch.object(
            httpcore.AsyncConnectionPool,
            "handle_async_request",
            side_effect=AssertionError("reached httpcore"),
        ) as pool:
            self.assert_blocked(lambda: asyncio.run(fetch()), "httpx.AsyncHTTPTransport")
        pool.assert_not_called()

    def test_httpcore_used_directly_is_blocked(self) -> None:
        with httpcore.ConnectionPool() as pool:
            self.assert_blocked(lambda: pool.request("GET", LOOPBACK_URL), "httpcore")

    def test_mock_transport_still_works_sync_and_async(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"host": request.url.host})

        before = len(BLOCKED_ATTEMPTS)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            response = client.get(INVALID_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"host": INVALID_HOST})

        async def fetch() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await client.get(INVALID_URL)

        self.assertEqual(asyncio.run(fetch()).json(), {"host": INVALID_HOST})
        self.assertEqual(len(BLOCKED_ATTEMPTS), before)


class SocketAndDnsTests(GuardTestCase):
    def test_tcp_connect_is_blocked(self) -> None:
        for family, address in ((socket.AF_INET, ("127.0.0.1", 9)), (socket.AF_INET6, ("::1", 9))):
            for method in ("connect", "connect_ex"):
                with self.subTest(family=family, method=method):
                    try:
                        sock = socket.socket(family, socket.SOCK_STREAM)
                    except OSError:
                        self.skipTest("address family not available")
                    with sock:
                        self.assert_blocked(lambda: getattr(sock, method)(address), f"socket.{method}")

    def test_create_connection_is_blocked(self) -> None:
        self.assert_blocked(
            lambda: socket.create_connection((INVALID_HOST, 443), timeout=1), "socket.create_connection"
        )

    def test_udp_send_is_blocked(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            self.assert_blocked(lambda: sock.sendto(b"x", ("127.0.0.1", 9)), "socket.sendto")
            self.assert_blocked(lambda: sock.connect(("127.0.0.1", 9)), "socket.connect")

    def test_dns_resolution_is_blocked(self) -> None:
        calls = {
            "getaddrinfo": lambda: socket.getaddrinfo(INVALID_HOST, 443),
            "gethostbyname": lambda: socket.gethostbyname(INVALID_HOST),
            "gethostbyname_ex": lambda: socket.gethostbyname_ex(INVALID_HOST),
            "gethostbyaddr": lambda: socket.gethostbyaddr("127.0.0.1"),
            "getnameinfo": lambda: socket.getnameinfo(("127.0.0.1", 9), 0),
        }
        for name, call in calls.items():
            with self.subTest(name=name):
                self.assert_blocked(call, f"socket.{name}")

    def test_asyncio_network_paths_are_blocked(self) -> None:
        async def attempt(kind: str) -> None:
            loop = asyncio.get_running_loop()
            if kind == "create_connection":
                await loop.create_connection(asyncio.Protocol, "127.0.0.1", 9)
            elif kind == "getaddrinfo":
                await loop.getaddrinfo(INVALID_HOST, 443)
            elif kind == "datagram":
                await loop.create_datagram_endpoint(
                    asyncio.DatagramProtocol, remote_addr=("127.0.0.1", 9)
                )
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(False)
                try:
                    await loop.sock_connect(sock, ("127.0.0.1", 9))
                finally:
                    sock.close()

        for kind in ("create_connection", "getaddrinfo", "datagram", "sock_connect"):
            with self.subTest(kind=kind):
                self.assert_blocked(lambda: asyncio.run(attempt(kind)), "asyncio loop.")

    def test_event_loops_and_socketpair_still_work(self) -> None:
        # asyncio needs socket.socketpair (a loopback pair inside this process on Windows).
        self.assertEqual(asyncio.run(asyncio.sleep(0, result="ok")), "ok")
        left, right = socket.socketpair()
        with left, right:
            left.sendall(b"ping")
            self.assertEqual(right.recv(4), b"ping")


class StandardLibraryHttpTests(GuardTestCase):
    def test_urllib_is_blocked(self) -> None:
        for url in (f"http://{INVALID_HOST}/", LOOPBACK_URL):
            with self.subTest(url=url):
                self.assert_blocked(lambda: urllib.request.urlopen(url, timeout=1), "http.client")

    def test_http_client_is_blocked(self) -> None:
        for connection in (
            http.client.HTTPConnection("127.0.0.1", 9, timeout=1),
            http.client.HTTPSConnection(INVALID_HOST, 443, timeout=1),
        ):
            with self.subTest(connection=type(connection).__name__):
                self.assert_blocked(lambda: connection.request("GET", "/"), "http.client")

    def test_urllib3_is_blocked(self) -> None:
        import urllib3

        with urllib3.PoolManager() as manager:
            with self.assertRaises(NetworkBlocked):
                manager.request("GET", LOOPBACK_URL, retries=False, timeout=1)

    @unittest.skipUnless(importlib.util.find_spec("requests"), "requests is not installed")
    def test_requests_is_blocked(self) -> None:  # pragma: no cover - not installed here
        import requests

        self.assert_blocked(lambda: requests.get(LOOPBACK_URL, timeout=1), "requests")


class WorkerIsolationTests(GuardTestCase):
    def setUp(self) -> None:
        guard = mock.patch.object(
            audit_store, "_default_client", side_effect=AssertionError("real client built")
        )
        guard.start()
        self.addCleanup(guard.stop)

    def test_production_critic_cannot_be_contacted(self) -> None:
        request = build_critic_request(
            COMMAND_ID,
            "9d2e4a10-0000-4000-8000-000000000002",
            [{"actionId": "a1", "type": "set_joint", "target": "_1", "value": 0.1, "unit": "rad", "reason": "r"}],
        )
        settings = CriticSettings(url=PRODUCTION_CRITIC_URL, shared_secret=TEST_SECRET)
        # Two independent safety nets UNDER the layer being tested.
        with mock.patch.object(
            httpcore.ConnectionPool, "handle_request", side_effect=AssertionError("reached httpcore")
        ) as pool, mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("reached a socket")
        ) as connect:
            self.assert_blocked(
                lambda: critic_client.review_plan(request, settings=settings),
                "httpx.HTTPTransport request to https://robot-audit-dashboard.vercel.app",
            )
        pool.assert_not_called()
        connect.assert_not_called()
        self.assertNotIn(TEST_SECRET, " ".join(BLOCKED_ATTEMPTS))

    def test_real_critic_settings_cannot_be_loaded(self) -> None:
        request = build_critic_request(COMMAND_ID, "p1", [])
        self.assert_blocked(
            lambda: critic_client.review_plan(request), "critic_client.load_critic_settings"
        )

    def test_worker_env_file_is_never_read(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError):
                config.load_critic_settings()

    def test_processor_cannot_fall_through_to_the_real_critic(self) -> None:
        client = FakeClient(
            commands=[command_row("set joint _1 to 0.1 rad", claimed_by=None, claimed_at=None)]
        )
        # No critic injected and review_plan not patched: the default path.
        self.assert_blocked(
            lambda: command_processor.process_one_command(WORKER_ID, client=client),
            "critic_client.load_critic_settings",
        )
        # Fails loudly: no result event, no status change.
        self.assertEqual(
            [e["event_type"] for e in client.tables["audit_events"]], ["plan_generated"]
        )
        self.assertEqual(client.tables["commands"][0]["status"], "received")

    def test_processor_with_real_settings_still_cannot_send(self) -> None:
        # Even if settings were available, the transport is blocked.
        client = FakeClient(
            commands=[command_row("set joint _1 to 0.1 rad", claimed_by=None, claimed_at=None)]
        )
        settings = CriticSettings(url=PRODUCTION_CRITIC_URL, shared_secret=TEST_SECRET)
        with mock.patch.object(critic_client, "load_critic_settings", return_value=settings):
            self.assert_blocked(
                lambda: command_processor.process_one_command(WORKER_ID, client=client),
                "httpx.HTTPTransport",
            )
        self.assertEqual(client.tables["commands"][0]["status"], "received")

    def test_fake_supabase_client_still_works(self) -> None:
        client = FakeClient(commands=[command_row(claimed_by=None, claimed_at=None)])
        before = len(BLOCKED_ATTEMPTS)
        result = command_processor.process_one_command(WORKER_ID, client=client)
        self.assertEqual(result.outcome, "planner_failed")
        self.assertEqual(client.tables["commands"][0]["status"], "failed")
        self.assertEqual(len(BLOCKED_ATTEMPTS), before)

    def test_main_without_confirm_write_sends_nothing(self) -> None:
        before = len(BLOCKED_ATTEMPTS)
        with mock.patch.object(command_processor.audit_store, "claim_next_command") as claim:
            with mock.patch("sys.stdout"):
                code = command_processor.main([])
        self.assertEqual(code, 2)
        claim.assert_not_called()
        self.assertEqual(len(BLOCKED_ATTEMPTS), before)

    def test_importing_worker_modules_sends_nothing(self) -> None:
        # A fresh interpreter with the guard installed first: any network
        # attempt during import would raise NetworkBlocked and exit non-zero.
        code = (
            "from tests import net_guard; "
            "import critic_client, command_processor, plan_snapshot, audit_store, config; "
            "print(len(net_guard.BLOCKED_ATTEMPTS))"
        )
        out = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=WORKER_DIR,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertEqual(out.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
