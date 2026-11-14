"""Environment-based configuration for the worker.

Values are loaded from ``worker/.env`` (via python-dotenv) or from the real
process environment, which takes precedence. Nothing here prints or logs a
secret value: error messages name the missing variable only.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

WORKER_DIR = Path(__file__).resolve().parent
ENV_FILE = WORKER_DIR / ".env"

# The SO-101 asset. There is deliberately no default: which robot the worker
# uses must be stated in the environment.
#
# Two readers, two rules:
# * load_cyberwave_twin_settings (anything that loads the twin): a canonical
#   UUID only. It is compared with the twin's asset UUID, and a registry id
#   ("vendor/model") is a different kind of identifier that must never be
#   compared with a UUID.
# * load_cyberwave_settings (asset discovery only): free-form, shown for
#   diagnosis, never compared with a twin.
ROBOT_ASSET_VAR = "ROBOT_ASSET_IDENTIFIER"
# Old name. It is never read as a fallback; see _refuse_legacy_asset_variable.
LEGACY_ROBOT_ASSET_VAR = "ROBOT_ASSET_SLUG"
# The EXISTING environment and the EXISTING twin in it. Both are required to
# load the twin. Nothing is created when either is missing.
ENVIRONMENT_ID_VAR = "CYBERWAVE_ENVIRONMENT_ID"
TWIN_ID_VAR = "CYBERWAVE_TWIN_ID"

# Variables whose values must never appear in output. See ``redact``.
SECRET_ENV_VARS = ("CYBERWAVE_API_KEY", "SUPABASE_SECRET_KEY", "CRITIC_SHARED_SECRET")


class ConfigError(Exception):
    """A required environment variable is missing or empty."""


@dataclass(frozen=True)
class CyberwaveSettings:
    """Settings for asset discovery. Not enough to load a twin."""

    api_key: str = field(repr=False)
    # Optional. Only needed when the API token is scoped to more than one
    # workspace; a single-workspace token has nothing to disambiguate.
    workspace_id: str | None
    # Free-form (UUID or registry id), for diagnosis only. None when the caller
    # asked for it not to be required (asset discovery).
    robot_asset_identifier: str | None
    # Optional here. Never used to create anything.
    environment_id: str | None = None


@dataclass(frozen=True)
class CyberwaveTwinSettings:
    """Settings for loading the EXISTING SO-101 twin. Every id is a canonical UUID.

    Lower case, 8-4-4-4-12. All three are required: there is no default and no
    fallback that creates a twin or an environment.
    """

    api_key: str = field(repr=False)
    workspace_id: str | None
    environment_id: str
    twin_id: str
    asset_uuid: str


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class CriticSettings:
    url: str
    shared_secret: str = field(repr=False)


def _load_env_file() -> None:
    # override=False: a variable already set in the shell wins over the file.
    load_dotenv(ENV_FILE, override=False)


def _require(*names: str) -> dict[str, str]:
    """Return the named variables, or raise listing every one that is missing."""
    _load_env_file()
    values = {name: (os.getenv(name) or "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + f". Set them in {ENV_FILE} (see .env.example)."
        )
    return values


def _optional(name: str) -> str | None:
    _load_env_file()
    return (os.getenv(name) or "").strip() or None


def canonical_uuid(value: object) -> str | None:
    """``value`` as a lower-case canonical UUID, or None if it is not one.

    Only the 8-4-4-4-12 form is accepted. ``uuid.UUID`` alone would also take
    braces, a ``urn:uuid:`` prefix and 32 bare hex digits; those are refused so
    that two ids are only ever compared in one form.
    """
    if type(value) is not str:
        return None
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed if parsed == value.lower() else None


def _refuse_legacy_asset_variable(identifier: str | None) -> None:
    if identifier is None and _optional(LEGACY_ROBOT_ASSET_VAR) is not None:
        # No silent fallback: the old value may be a registry id that the SDK
        # cannot resolve, and using it quietly would hide that.
        raise ConfigError(
            f"{LEGACY_ROBOT_ASSET_VAR} is set but {ROBOT_ASSET_VAR} is not. The "
            f"variable was renamed. In {ENV_FILE}, rename {LEGACY_ROBOT_ASSET_VAR} "
            f"to {ROBOT_ASSET_VAR} and set it to the asset UUID "
            "(see docs/robot-capabilities.md)."
        )


def load_cyberwave_twin_settings() -> CyberwaveTwinSettings:
    """Settings for loading the existing SO-101 twin. Fails closed.

    Requires ``CYBERWAVE_API_KEY``, ``CYBERWAVE_ENVIRONMENT_ID``,
    ``CYBERWAVE_TWIN_ID`` and ``ROBOT_ASSET_IDENTIFIER``. The three ids must be
    canonical UUIDs. A missing, blank or malformed value raises ``ConfigError``
    naming the variable (never its value). There is no default and nothing is
    looked up or created to fill a gap: a registry id is refused, not resolved.
    """
    _refuse_legacy_asset_variable(_optional(ROBOT_ASSET_VAR))
    id_vars = (ENVIRONMENT_ID_VAR, TWIN_ID_VAR, ROBOT_ASSET_VAR)
    values = _require("CYBERWAVE_API_KEY", *id_vars)
    ids: dict[str, str] = {}
    malformed: list[str] = []
    for name in id_vars:
        parsed = canonical_uuid(values[name])
        if parsed is None:
            malformed.append(name)
        else:
            ids[name] = parsed
    if malformed:
        raise ConfigError(
            "Not a canonical UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx): "
            + ", ".join(malformed)
            + f". Set them in {ENV_FILE}. {ROBOT_ASSET_VAR} must be the asset UUID, "
            "not a registry id."
        )
    return CyberwaveTwinSettings(
        api_key=values["CYBERWAVE_API_KEY"],
        workspace_id=_optional("CYBERWAVE_WORKSPACE_ID"),
        environment_id=ids[ENVIRONMENT_ID_VAR],
        twin_id=ids[TWIN_ID_VAR],
        asset_uuid=ids[ROBOT_ASSET_VAR],
    )


def load_cyberwave_settings(*, require_asset_identifier: bool = True) -> CyberwaveSettings:
    """Load Cyberwave settings for asset discovery.

    Not enough to load a twin: use ``load_cyberwave_twin_settings`` for that.
    ``require_asset_identifier=False`` is for discovery, which runs before the
    identifier is known. The identifier is free-form here and is only shown.
    """
    identifier = _optional(ROBOT_ASSET_VAR)
    _refuse_legacy_asset_variable(identifier)

    required = ["CYBERWAVE_API_KEY"]
    if require_asset_identifier:
        required.append(ROBOT_ASSET_VAR)
    values = _require(*required)
    return CyberwaveSettings(
        api_key=values["CYBERWAVE_API_KEY"],
        workspace_id=_optional("CYBERWAVE_WORKSPACE_ID"),
        robot_asset_identifier=identifier,
        environment_id=_optional(ENVIRONMENT_ID_VAR),
    )


def load_supabase_settings() -> SupabaseSettings:
    values = _require("SUPABASE_URL", "SUPABASE_SECRET_KEY")
    return SupabaseSettings(
        url=values["SUPABASE_URL"], secret_key=values["SUPABASE_SECRET_KEY"]
    )


def load_critic_settings() -> CriticSettings:
    values = _require("CRITIC_URL", "CRITIC_SHARED_SECRET")
    return CriticSettings(
        url=values["CRITIC_URL"], shared_secret=values["CRITIC_SHARED_SECRET"]
    )


def redact(text: object) -> str:
    """Replace any configured secret value found in ``text`` with ``***``.

    Use this on third-party error messages before printing them, in case a
    library echoes a credential back.
    """
    result = str(text)
    for name in SECRET_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            result = result.replace(value, "***")
    return result
