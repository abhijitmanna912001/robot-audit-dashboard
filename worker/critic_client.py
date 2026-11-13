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
* No retries, for anything. The audit log is append-only, and one worker run
  makes at most one critic request for one claimed command.
* Error details stored in the audit event are fixed text plus an exception
  CLASS name, never exception text, headers or a raw response body.

What it never does: write to the database, connect to Cyberwave or move a robot.
"""

from __future__ import annotations

import ipaddress
import json
import math
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
    "build_timeout",
    "review_plan",
]

SECRET_HEADER = "x-critic-secret"
# The critic is deterministic and fast; 10 s covers a Vercel cold start.
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 60.0
# A verdict is a few hundred bytes. Anything far larger is not a verdict.
MAX_RESPONSE_BYTES = 64 * 1024

_LOOPBACK_NAMES = frozenset({"localhost"})


def build_timeout(seconds: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.Timeout:
    """A finite ``httpx.Timeout`` with the same limit on every phase.

    Raises ValueError for a value that is not a real number in
    ``(0, MAX_TIMEOUT_SECONDS]``. ``None`` (no timeout) is refused.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("timeout must be a number of seconds.")
    value = float(seconds)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout must be more than 0 and at most {MAX_TIMEOUT_SECONDS} seconds."
        )
    return httpx.Timeout(connect=value, read=value, write=value, pool=value)


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
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> CriticDecision:
    """Send ``request`` to the critic once and judge the answer. Never raises.

    ``settings`` defaults to ``config.load_critic_settings()``. ``transport`` is
    for tests (``httpx.MockTransport``); leave it None in real use.
    """
    try:
        return _review(request, settings, transport, timeout_seconds)
    except Exception as exc:  # fail closed
        return critic_failure(
            "invalid_body", f"critic client failed unexpectedly ({type(exc).__name__})"
        )


def _review(
    request: CriticRequest,
    settings: CriticSettings | None,
    transport: httpx.BaseTransport | None,
    timeout_seconds: float,
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
        timeout = build_timeout(timeout_seconds)
    except ValueError:
        return critic_not_sent("the critic timeout is invalid")

    try:
        body = json.dumps(request.to_body(), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return critic_not_sent("the critic request is not valid JSON")

    raw = b""
    too_large = False
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                url,
                content=body.encode("utf-8"),
                headers={
                    SECRET_HEADER: secret,
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            ) as response:
                status = response.status_code
                if status == 200:
                    # Only a 200 body can be a verdict. Read at most the cap.
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            too_large = True
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
    except httpx.TimeoutException:
        return critic_failure(
            "timeout", f"no response from the critic within {float(timeout_seconds):g} s"
        )
    except httpx.HTTPError as exc:  # connection, DNS, protocol and other transport errors
        return critic_network_failure(type(exc).__name__)
    except httpx.InvalidURL:
        return critic_not_sent("CRITIC_URL is not a valid URL")

    if 300 <= status < 400:
        # Never followed: the secret must not go to another host.
        return critic_failure(
            "http_status", "redirect not followed", http_status=status
        )
    if status != 200:
        # A non-200 response is never a verdict; its body is not read.
        return evaluate_critic_response(request, status, None)

    if too_large:
        return critic_failure(
            "invalid_body", "response body is too large", http_status=200
        )
    try:
        parsed: Any = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return critic_failure(
            "invalid_body", "response body is not valid JSON", http_status=200
        )
    return evaluate_critic_response(request, 200, _redact_body(parsed, secret))


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
