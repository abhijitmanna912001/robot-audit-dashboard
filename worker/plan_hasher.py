"""Plan hashing: RFC 8785 canonical JSON of the actions array, then SHA-256.

Contract (must match the TypeScript implementation byte for byte):

* input: the plan's ``actions`` array only, nothing else from the plan
* canonicalization: RFC 8785 (JCS), via the ``rfc8785`` library
* encoding: UTF-8 (what RFC 8785 produces)
* digest: SHA-256, lowercase hex
* output: ``"sha256:" + hex``

Array order is significant and preserved; object key order is not. Non-finite
numbers (NaN, +/-Infinity) are rejected, as is anything else JSON cannot
represent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import rfc8785

from schemas import JsonValue

HASH_PREFIX = "sha256:"


class PlanHashError(ValueError):
    """The actions value cannot be canonicalized and hashed."""


def canonicalize_actions(actions: Sequence[JsonValue]) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of the actions array."""
    # str is a Sequence too, so check for list/tuple explicitly.
    if not isinstance(actions, (list, tuple)):
        raise PlanHashError(
            f"actions must be an array (list), got {type(actions).__name__}."
        )
    try:
        return rfc8785.dumps(list(actions))
    except rfc8785.CanonicalizationError as exc:
        # Covers NaN/Infinity, integers outside the IEEE-754 safe range,
        # non-string object keys, non-JSON types and invalid Unicode.
        raise PlanHashError(f"actions cannot be canonicalized: {exc}") from exc
    except RecursionError as exc:
        # The library recurses, so extreme nesting would otherwise escape as a
        # RecursionError that callers expecting PlanHashError do not handle.
        raise PlanHashError("actions cannot be canonicalized: nesting is too deep") from exc


def hash_actions(actions: Sequence[JsonValue]) -> str:
    """Return ``sha256:<lowercase hex>`` for the given actions array."""
    return HASH_PREFIX + hashlib.sha256(canonicalize_actions(actions)).hexdigest()
