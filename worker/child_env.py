"""The environment a spawned twin child is allowed to inherit.

``multiprocessing`` spawn gives a child the parent's ``os.environ`` as it is
while ``Process.start()`` runs. The twin child (the code that talks to
Cyberwave) must not be able to read the worker's Supabase or critic
credentials, whether they came from ``worker/.env`` or were exported in the
shell. ``child_environment()`` removes them from ``os.environ`` for the
duration of ``start()`` only and puts them back exactly afterwards.

Thread safety. ``ENV_LOCK`` is held for the whole scrub/start/restore window,
so two sessions starting at once cannot interleave their scrub and restore.
``config`` takes the same lock when it reads ``os.environ``, so the worker's
own readers never see a half-scrubbed environment. Third-party code that reads
other variables (proxy, SSL) is unaffected; only the names below are removed.

This is protection against accidental inheritance, not a sandbox: a child
running as the same user can still open ``worker/.env`` as a file.

Standard library only, and nothing here runs at import time.
"""

from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from typing import Iterator

__all__ = [
    "ENV_LOCK",
    "SCRUBBED_PREFIXES",
    "SECRET_ENV_VARS",
    "child_environment",
    "is_scrubbed",
    "looks_secret_key",
    "looks_secret_value",
]

# Variables whose values must never appear in output (see ``config.redact``)
# and that a twin child must never inherit.
SECRET_ENV_VARS = ("CYBERWAVE_API_KEY", "SUPABASE_SECRET_KEY", "CRITIC_SHARED_SECRET")

# Every variable with one of these prefixes belongs to the worker, not the twin.
SCRUBBED_PREFIXES = ("SUPABASE_", "CRITIC_", "WORKER_")

# What a credential looks like, so none can travel to a twin child inside
# ``TwinSpec.config`` (see ``simulation_supervisor._is_plain``). They live here,
# not in a simulation module, because those modules must contain no credential
# patterns at all.
_SECRET_KEY_WORDS = ("key", "secret", "token", "password")
_SECRET_VALUE_PREFIXES = ("sb_secret_", "sk-", "eyJ")
_LONG_HEX = re.compile(r"[0-9a-fA-F]{32,}")


def looks_secret_key(name: str) -> bool:
    """True if a config key names a secret (case-insensitive substring)."""
    lowered = name.lower()
    return any(word in lowered for word in _SECRET_KEY_WORDS)


def looks_secret_value(text: str) -> bool:
    """True if a string is shaped like a credential (Supabase secret, ``sk-``
    key, JWT, or a run of 32 or more hex characters)."""
    return text.startswith(_SECRET_VALUE_PREFIXES) or _LONG_HEX.search(text) is not None

# Re-entrant so a caller that already holds it can still read the config.
ENV_LOCK = threading.RLock()

_SECRET_NAMES_UPPER = frozenset(name.upper() for name in SECRET_ENV_VARS)


def is_scrubbed(name: str) -> bool:
    """True if ``name`` must not reach a twin child (case-insensitive)."""
    upper = name.upper()
    return upper in _SECRET_NAMES_UPPER or upper.startswith(SCRUBBED_PREFIXES)


@contextmanager
def child_environment() -> Iterator[None]:
    """Hide worker credentials from a process started inside this block.

    Wrap ONLY ``Process.start()``. The removed variables are restored exactly
    (same names, same values) on every exit path, including exceptions.
    """
    with ENV_LOCK:
        removed = {name: value for name, value in os.environ.items() if is_scrubbed(name)}
        try:
            for name in removed:
                del os.environ[name]
            yield
        finally:
            for name, value in removed.items():
                os.environ[name] = value
