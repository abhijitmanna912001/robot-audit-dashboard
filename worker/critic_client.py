"""HTTP client for ``POST /api/critic``. One request, no retries, never raises.

``review_plan(request)`` sends the plan to the deployed critic and returns a
``critic_gate.CriticDecision``. Every response, whatever its status, is judged
by ``critic_gate.evaluate_critic_response``: this module only moves bytes and
never interprets a verdict itself.

Security rules
--------------
* The shared secret (``CRITIC_SHARED_SECRET``) is sent ONLY as the
  ``x-critic-secret`` request header. It never goes in the body, the URL, an
  audit payload, an exception message or console output.
* Redirects are not followed (``follow_redirects=False``). A 3xx response is an
  error, so the secret can never be forwarded to another host.
* ``trust_env=False``: no proxy, ``.netrc`` credentials or CA settings are
  picked up from the environment, so nothing outside this module decides where
  the secret goes or adds headers to the request.
* The URL must be ``https``. Plain ``http`` is accepted only for a loopback
  host (local ``next dev``). A URL with credentials, a query or a fragment is
  refused. A refused or missing configuration is an error: the critic is never
  skipped and the plan is never approved.
* Every phase has a finite timeout (``httpx.Timeout`` connect, read, write and
  pool). The value is validated: it cannot be zero, negative, infinite, NaN or
  larger than ``MAX_TIMEOUT_SECONDS``.
* A total wall-clock deadline (``TOTAL_DEADLINE_SECONDS``, 10 s) covers the
  whole network exchange. The request runs on ``httpx.AsyncClient`` inside
  ``asyncio.timeout(deadline)``, on a private event loop created and closed by
  each call (the worker stays synchronous). That scope surrounds opening the
  client, DNS, TCP and TLS, sending, receiving the response headers (however
  slowly they trickle in), reading the body with ``aiter_raw`` and closing the
  response and the client. When it expires, the read or connect in progress
  is cancelled where it stands, the response and client are closed while
  unwinding, and the result is a ``timeout`` error, never an approval or a
  reject. Each phase timeout is also capped at the deadline, and explicit
  checkpoints (headers received, every body chunk, after JSON decoding) use
  the same budget. No background thread is started.
* ``accept-encoding: identity`` is sent and only an absent or ``identity``
  Content-Encoding is accepted: nothing is decompressed. The body is read raw
  (``aiter_raw``) and reading stops as soon as more than ``MAX_RESPONSE_BYTES``
  bytes have arrived, before any JSON parsing. A Content-Length above the cap
  is refused before reading.
* No retries, for anything. The audit log is append-only, and one worker run
  makes at most one critic request for one claimed command.
* Error details stored in the audit event are fixed text plus an exception
  CLASS name, never exception text, headers or a raw response body.

What it never does: write to the database, connect to Cyberwave or move a robot.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from config import ConfigError, CriticSettings, load_critic_settings, redact
from critic_gate import (
    CriticDecision,
    CriticRequest,
    critic_failure,
    critic_network_failure,
    critic_not_sent,
    evaluate_critic_response,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "SECRET_HEADER",
    "TOTAL_DEADLINE_SECONDS",
    "build_timeout",
    "review_plan",
]

SECRET_HEADER = "x-critic-secret"
# The critic is deterministic and fast; 10 s covers a Vercel cold start.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 60.0
# Total wall-clock budget for one call: connect, headers, body and JSON decode.
TOTAL_DEADLINE_SECONDS = 10.0
# A verdict is a few hundred bytes. Anything far larger is not a verdict. A body
# of exactly this many raw bytes is accepted; one byte more is rejected.
MAX_RESPONSE_BYTES = 64 * 1024

_LOOPBACK_NAMES = frozenset({"localhost"})


def build_timeout(seconds: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.Timeout:
    """A finite ``httpx.Timeout`` with the same limit on every phase.

    Raises ValueError for a value that is not a real number in
    ``(0, MAX_TIMEOUT_SECONDS]``. ``None`` (no timeout) is refused.
    """
    value = _validated_seconds(seconds, "timeout")
    return httpx.Timeout(connect=value, read=value, write=value, pool=value)


def _validated_seconds(seconds: object, name: str) -> float:
    """A real number of seconds in ``(0, MAX_TIMEOUT_SECONDS]``, else ValueError."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError(f"{name} must be a number of seconds.")
    value = float(seconds)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"{name} must be more than 0 and at most {MAX_TIMEOUT_SECONDS} seconds."
        )
    return value


def _url_problem(url: str) -> str | None:
    """Why ``url`` must not receive the secret, or None if it may."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "CRITIC_URL is not a valid URL"
    if not host:
        return "CRITIC_URL has no host"
    if parts.username is not None or parts.password is not None:
        return "CRITIC_URL must not contain credentials"
    if parts.query or parts.fragment:
        return "CRITIC_URL must not contain a query or fragment"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and _is_loopback(host):
        return None
    return "CRITIC_URL must use https (http only for a loopback host)"


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def review_plan(
    request: CriticRequest,
    *,
    settings: CriticSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    deadline_seconds: float = TOTAL_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> CriticDecision:
    """Send ``request`` to the critic once and judge the answer. Never raises.

    Synchronous: the exchange runs on a private event loop for the duration of
    the call (see ``_run_exchange``). Must not be called from inside a running
    event loop; that is refused and reported as a critic error.

    ``settings`` defaults to ``config.load_critic_settings()``. ``transport`` and
    ``clock`` are for tests (``httpx.MockTransport``, a fake monotonic clock);
    leave them at their defaults in real use. ``deadline_seconds`` is the total
    wall-clock budget for the whole call (see the module docstring).
    """
    try:
        return _review(request, settings, transport, timeout_seconds, deadline_seconds, clock)
    except Exception as exc:  # fail closed
        return critic_failure(
            "invalid_body", f"critic client failed unexpectedly ({type(exc).__name__})"
        )


def _review(
    request: CriticRequest,
    settings: CriticSettings | None,
    transport: httpx.AsyncBaseTransport | None,
    timeout_seconds: float,
    deadline_seconds: float,
    clock: Callable[[], float],
) -> CriticDecision:
    if not isinstance(request, CriticRequest):
        return critic_not_sent("no valid critic request")

    if settings is None:
        try:
            settings = load_critic_settings()
        except ConfigError:
            # The ConfigError text names the variables only, but it is not
            # needed: the fixed text below says the same without any risk.
            return critic_not_sent("CRITIC_URL or CRITIC_SHARED_SECRET is not set")
    if not isinstance(settings, CriticSettings):
        return critic_not_sent("no critic settings")
    url = settings.url.strip() if isinstance(settings.url, str) else ""
    secret = settings.shared_secret if isinstance(settings.shared_secret, str) else ""
    if not url or not secret.strip():
        return critic_not_sent("CRITIC_URL or CRITIC_SHARED_SECRET is not set")
    problem = _url_problem(url)
    if problem is not None:
        return critic_not_sent(problem)

    try:
        build_timeout(timeout_seconds)  # validates the configured phase timeout
        total = _validated_seconds(deadline_seconds, "deadline")
    except ValueError:
        return critic_not_sent("the critic timeout or deadline is invalid")
    if not callable(clock):
        return critic_not_sent("no usable clock")
    # No single phase may wait longer than the whole deadline.
    phase = min(float(timeout_seconds), total)
    timeout = build_timeout(phase)

    try:
        body = json.dumps(request.to_body(), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return critic_not_sent("the critic request is not valid JSON")

    deadline = _Deadline(clock, total)
    try:
        status, outcome, raw = _run_exchange(
            _exchange(url, secret, body.encode("utf-8"), transport, timeout, deadline)
        )
    except httpx.TimeoutException:
        return critic_failure(
            "timeout", f"no response from the critic within {phase:g} s"
        )
    except httpx.HTTPError as exc:  # connection, DNS, protocol and other transport errors
        return critic_network_failure(type(exc).__name__)
    except httpx.InvalidURL:
        return critic_not_sent("CRITIC_URL is not a valid URL")

    if outcome is not None:
        return outcome
    if 300 <= status < 400:
        # Never followed: the secret must not go to another host.
        return critic_failure(
            "http_status", "redirect not followed", http_status=status
        )
    if status != 200:
        # A non-200 response is never a verdict; its body is not read.
        return evaluate_critic_response(request, status, None)

    try:
        parsed: Any = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return critic_failure(
            "invalid_body", "response body is not valid JSON", http_status=200
        )
    if deadline.expired():
        return deadline.error()
    return evaluate_critic_response(request, 200, _redact_body(parsed, secret))


def _run_exchange(coro: Any) -> tuple[int, CriticDecision | None, bytes]:
    """Run the async exchange to completion on a private event loop.

    The worker is synchronous, so each critic call gets its own new loop,
    which is closed afterwards. Calling this from inside a running event loop
    is refused (RuntimeError, which ``review_plan`` turns into a critic error).

    Not ``asyncio.run``: on shutdown it waits up to five minutes for the
    default executor's threads, and asyncio resolves host names in such a
    thread. A DNS lookup that is still blocked when the deadline cancels the
    call would then hold the worker. ``loop.close()`` does not wait for them.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError("review_plan cannot run inside a running event loop")

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


async def _exchange(
    url: str,
    secret: str,
    content: bytes,
    transport: httpx.AsyncBaseTransport | None,
    timeout: httpx.Timeout,
    deadline: _Deadline,
) -> tuple[int, CriticDecision | None, bytes]:
    """(status, error or None, raw body) for ONE request, under the total deadline.

    ``asyncio.timeout`` surrounds everything that touches the network: opening
    the client, connecting (DNS, TCP, TLS), sending, waiting for and parsing
    the response headers however slowly they trickle in, reading the body, and
    closing the response and the client. When the deadline passes, the
    operation in progress is cancelled where it stands and the ``async with``
    blocks close the response and the client while unwinding.
    """
    try:
        async with asyncio.timeout(deadline.seconds):
            async with httpx.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    content=content,
                    headers={
                        SECRET_HEADER: secret,
                        "content-type": "application/json",
                        "accept": "application/json",
                        # Never a compressed body: the size cap is on received bytes.
                        "accept-encoding": "identity",
                    },
                ) as response:
                    status = response.status_code
                    if deadline.expired():
                        return status, deadline.error(), b""
                    if status != 200:
                        return status, None, b""  # a non-200 body is never read
                    # Leaving these blocks closes the response and the client,
                    # whatever _read_body decided.
                    outcome, raw = await _read_body(response, deadline)
                    return status, outcome, raw
    except TimeoutError:
        # The total deadline cancelled the exchange.
        return 0, deadline.error(), b""


class _Deadline:
    """A total wall-clock budget for one critic call, on a monotonic clock.

    ``asyncio.timeout`` (the event loop's monotonic clock) is what cancels a
    late exchange. ``expired()`` adds explicit checkpoints on the given clock,
    for the step after the exchange (JSON decoding) and for deterministic tests.
    """

    def __init__(self, clock: Callable[[], float], seconds: float) -> None:
        self._clock = clock
        self.seconds = seconds
        self._end = clock() + seconds

    def expired(self) -> bool:
        return self._clock() > self._end

    def error(self) -> CriticDecision:
        return critic_failure(
            "timeout", f"critic response not complete within the {self.seconds:g} s total deadline"
        )


async def _read_body(
    response: httpx.Response, deadline: _Deadline
) -> tuple[CriticDecision | None, bytes]:
    """(error or None, raw body). Reads the RAW bytes, at most MAX_RESPONSE_BYTES.

    No decompression is ever done: only an absent or ``identity``
    Content-Encoding is accepted, and the body is read with ``aiter_raw``, so
    the cap applies to the bytes received. Reading stops at the first chunk
    that crosses the cap or a deadline checkpoint; nothing more is read. A read
    that is still waiting when the total deadline passes is cancelled by the
    caller's ``asyncio.timeout``.
    """
    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        return critic_failure(
            "invalid_body", "unsupported response content-encoding", http_status=200
        ), b""

    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared.strip())
        except ValueError:
            return critic_failure("invalid_body", "invalid content-length", http_status=200), b""
        if declared_size > MAX_RESPONSE_BYTES:
            return _too_large(), b""

    if response.is_stream_consumed:
        # Only an in-memory response (httpx.Response(content=...), as built by
        # a MockTransport handler) arrives already read; a network response
        # never does. Its content is already in memory and, with the identity
        # encoding checked above, equals the raw bytes. The cap still applies.
        received = [response.content]
    else:
        received = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                return _too_large(), b""
            received.append(chunk)
            if deadline.expired():
                return deadline.error(), b""
    chunks: list[bytes] = []
    size = 0
    for chunk in received:
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            return _too_large(), b""
        chunks.append(chunk)
    if deadline.expired():
        return deadline.error(), b""
    return None, b"".join(chunks)


def _too_large() -> CriticDecision:
    return critic_failure("invalid_body", "response body is too large", http_status=200)


def _redact_body(body: Any, secret: str) -> Any:
    """Scrub secret values from the strings that can reach the audit payload.

    Only top-level strings and top-level lists of strings are kept by
    ``critic_gate.sanitize_response``, so only those are scrubbed. This runs
    BEFORE the gate caps lengths, so a secret cut by a cap cannot survive as a
    fragment. A scrubbed id or hash no longer matches the request, which makes
    the response an error (fail closed).
    """
    if not isinstance(body, dict):
        return body

    def clean(text: str) -> str:
        return redact(text).replace(secret, "***")

    scrubbed: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, str):
            scrubbed[key] = clean(value)
        elif isinstance(value, list):
            scrubbed[key] = [clean(v) if isinstance(v, str) else v for v in value]
        else:
            scrubbed[key] = value
    return scrubbed


def _reject_constant(name: str) -> Any:
    # NaN, Infinity and -Infinity are not JSON.
    raise ValueError(f"non-standard JSON constant {name}")
