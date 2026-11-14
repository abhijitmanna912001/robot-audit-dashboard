"""Process-wide network guard for the worker test suite.

``install()`` is called before any test module is imported:

* by ``worker/conftest.py``, which pytest loads before it collects anything
  under ``worker/`` (``worker/pytest.ini`` makes ``worker/`` the rootdir for any
  path inside it, so that conftest is always in scope), and
* by ``worker/tests/__init__.py``, which runs before any ``tests.*`` module, so
  ``python -m unittest discover -s tests -t .`` and ``python -m unittest
  tests.<module>`` are covered too.

It is idempotent and is never removed for the life of the test process. Once
installed, every outbound network path below raises ``NetworkBlocked`` BEFORE
anything leaves the process:

* httpx: ``HTTPTransport.handle_request`` and
  ``AsyncHTTPTransport.handle_async_request`` (the default transports of
  ``httpx.Client`` and ``httpx.AsyncClient``)
* httpcore: ``ConnectionPool``, ``AsyncConnectionPool`` and the sync and AnyIO
  network backends' ``connect_tcp`` (in case anything bypasses httpx)
* ``http.client.HTTPConnection.connect`` (so ``urllib.request`` and urllib3)
* ``requests.adapters.HTTPAdapter.send``, if requests is installed
* sockets: ``socket.socket.connect``, ``connect_ex``, ``sendto`` (and
  ``sendmsg`` where it exists) for AF_INET / AF_INET6 sockets, and
  ``socket.create_connection``
* DNS: ``socket.getaddrinfo``, ``gethostbyname``, ``gethostbyname_ex``,
  ``gethostbyaddr``, ``getnameinfo``
* asyncio: ``create_connection``, ``create_datagram_endpoint``,
  ``getaddrinfo``, ``sock_connect`` and ``sock_sendto`` on the event loops

One local exception: ``socket.socketpair()``. On Windows it is a pure-Python
fallback that connects two sockets over loopback inside this process, and
asyncio needs it to create any event loop. Only connections made from inside
``socketpair()`` itself are allowed; it cannot reach another process or host.

``httpx.MockTransport`` is a different class and is not affected, so mocked
HTTP keeps working.

Worker configuration is also isolated: ``critic_client.load_critic_settings``
raises ``NetworkBlocked`` (a test must inject settings or a fake critic), and
``config.load_dotenv`` is a no-op, so ``worker/.env`` is never read by tests.

``NetworkBlocked`` derives from BaseException, NOT Exception, on purpose: the
worker's fail-closed ``except Exception`` handlers cannot swallow it and turn a
blocked request into an ordinary critic "error". pytest and unittest both
report it as a test failure/error.
"""

from __future__ import annotations

import asyncio
import asyncio.base_events
import http.client
import importlib.util
import socket
import sys
import threading
import unittest
from typing import Any, Callable

import httpx

__all__ = [
    "BLOCKED_ATTEMPTS",
    "NetworkBlocked",
    "block_network",
    "install",
    "is_installed",
]


class NetworkBlocked(BaseException):
    """A test tried to reach the network or load real worker configuration."""


# One short description per blocked attempt (what and where, never a payload,
# header or secret). Tests compare its length before and after an action.
BLOCKED_ATTEMPTS: list[str] = []

# Test modules already imported when the guard was installed. Must be empty:
# the guard has to be in place before any test module runs.
TEST_MODULES_IMPORTED_BEFORE_INSTALL: tuple[str, ...] = ()

_installed = False
_install_lock = threading.Lock()
_local = threading.local()
_INET_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})


def is_installed() -> bool:
    return _installed


def _refuse(what: str) -> None:
    BLOCKED_ATTEMPTS.append(what)
    raise NetworkBlocked(f"network access is blocked in tests: {what}")


def _in_socketpair() -> bool:
    return getattr(_local, "socketpair_depth", 0) > 0


def _describe_address(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return ":".join(str(part) for part in address[:2])
    return type(address).__name__


def _request_host(request: Any) -> str:
    try:
        return f"{request.url.scheme}://{request.url.host}"
    except Exception:
        return "unknown host"


# --------------------------------------------------------------------------
# Patches
# --------------------------------------------------------------------------


def _patch_sockets() -> None:
    real_socketpair = socket.socketpair

    def guarded_socketpair(*args: Any, **kwargs: Any) -> Any:
        _local.socketpair_depth = getattr(_local, "socketpair_depth", 0) + 1
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            _local.socketpair_depth -= 1

    socket.socketpair = guarded_socketpair  # type: ignore[assignment]

    def guard_method(name: str) -> None:
        original = getattr(socket.socket, name)

        def guarded(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
            if self.family in _INET_FAMILIES and not _in_socketpair():
                target = args[-1] if args else kwargs.get("address")
                _refuse(f"socket.{name} to {_describe_address(target)}")
            return original(self, *args, **kwargs)

        guarded.__name__ = name
        setattr(socket.socket, name, guarded)

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            guard_method(name)

    def guard_function(name: str) -> None:
        original = getattr(socket, name)

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if not _in_socketpair():
                target = args[0] if args else kwargs.get("host", "?")
                _refuse(f"socket.{name}({_describe_address(target) if isinstance(target, tuple) else target})")
            return original(*args, **kwargs)

        guarded.__name__ = name
        setattr(socket, name, guarded)

    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        guard_function(name)


def _patch_http_libraries() -> None:
    def sync_transport(self: Any, request: Any) -> Any:
        _refuse(f"httpx.HTTPTransport request to {_request_host(request)}")

    def async_transport(self: Any, request: Any) -> Any:
        _refuse(f"httpx.AsyncHTTPTransport request to {_request_host(request)}")

    httpx.HTTPTransport.handle_request = sync_transport  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = async_transport  # type: ignore[method-assign]

    import httpcore

    def refuse_as(what: str) -> Callable[..., Any]:
        def refused(*_args: Any, **_kwargs: Any) -> Any:
            _refuse(what)

        return refused

    httpcore.ConnectionPool.handle_request = refuse_as("httpcore.ConnectionPool")  # type: ignore[method-assign]
    httpcore.AsyncConnectionPool.handle_async_request = refuse_as(  # type: ignore[method-assign]
        "httpcore.AsyncConnectionPool"
    )
    for module_name, class_name in (
        ("httpcore._backends.sync", "SyncBackend"),
        ("httpcore._backends.anyio", "AnyIOBackend"),
        ("httpcore._backends.auto", "AutoBackend"),
    ):
        try:
            module = __import__(module_name, fromlist=[class_name])
            backend = getattr(module, class_name)
        except Exception:  # private modules: absent in some versions
            continue
        for method in ("connect_tcp", "connect_unix_socket"):
            if hasattr(backend, method):
                setattr(backend, method, refuse_as(f"httpcore {class_name}.{method}"))

    def http_client_connect(self: Any) -> None:
        _refuse(f"http.client connection to {getattr(self, 'host', '?')}:{getattr(self, 'port', '?')}")

    http.client.HTTPConnection.connect = http_client_connect  # type: ignore[method-assign]

    if importlib.util.find_spec("requests") is not None:
        import requests.adapters

        requests.adapters.HTTPAdapter.send = refuse_as("requests.HTTPAdapter.send")  # type: ignore[method-assign]


def _patch_asyncio() -> None:
    import asyncio.proactor_events
    import asyncio.selector_events

    def refuse_as(what: str) -> Callable[..., Any]:
        def refused(*_args: Any, **_kwargs: Any) -> Any:
            _refuse(what)

        return refused

    base = asyncio.base_events.BaseEventLoop
    for name in ("create_connection", "create_datagram_endpoint", "getaddrinfo", "getnameinfo"):
        if hasattr(base, name):
            setattr(base, name, refuse_as(f"asyncio loop.{name}"))
    for loop_class in (
        asyncio.proactor_events.BaseProactorEventLoop,
        asyncio.selector_events.BaseSelectorEventLoop,
    ):
        for name in ("sock_connect", "sock_sendto"):
            if name in vars(loop_class):
                setattr(loop_class, name, refuse_as(f"asyncio loop.{name}"))


def _isolate_worker_configuration() -> None:
    """No real critic settings and no worker/.env in any test."""
    import dotenv

    import config
    import critic_client

    def no_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False  # worker/.env is never read in tests

    config.load_dotenv = no_dotenv  # type: ignore[assignment]
    dotenv.load_dotenv = no_dotenv  # type: ignore[assignment]

    def refuse_settings(*_args: Any, **_kwargs: Any) -> Any:
        _refuse("critic_client.load_critic_settings (inject settings or a fake critic)")

    critic_client.load_critic_settings = refuse_settings  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def install() -> None:
    """Block all outbound network access for this process. Idempotent."""
    global _installed, TEST_MODULES_IMPORTED_BEFORE_INSTALL
    with _install_lock:
        if _installed:
            return
        TEST_MODULES_IMPORTED_BEFORE_INSTALL = tuple(
            sorted(
                name
                for name in sys.modules
                if name.startswith("tests.test_") or name.startswith("test_")
            )
        )
        # Network first, so importing worker modules below is already guarded.
        _patch_sockets()
        _patch_http_libraries()
        _patch_asyncio()
        _isolate_worker_configuration()
        _installed = True


def block_network(test: unittest.TestCase) -> None:
    """Kept for existing call sites: asserts the global guard is in place.

    The guard is process-wide, so there is nothing to patch per test. A test
    calling this fails at once if the guard was somehow not installed.
    """
    install()
    test.assertTrue(is_installed(), "network guard is not installed")
