"""Environment-based configuration for the worker.

Values are loaded from ``worker/.env`` (via python-dotenv) or from the real
process environment, which takes precedence. Nothing here prints or logs a
secret value: error messages name the missing variable only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

WORKER_DIR = Path(__file__).resolve().parent
ENV_FILE = WORKER_DIR / ".env"

# The asset to instantiate, as a UUID or a registry id. There is deliberately
# no default: which robot the worker drives must be stated in the environment.
ROBOT_ASSET_VAR = "ROBOT_ASSET_IDENTIFIER"
# Old name. It is never read as a fallback; see load_cyberwave_settings.
LEGACY_ROBOT_ASSET_VAR = "ROBOT_ASSET_SLUG"

# Variables whose values must never appear in output. See ``redact``.
SECRET_ENV_VARS = ("CYBERWAVE_API_KEY", "SUPABASE_SECRET_KEY", "CRITIC_SHARED_SECRET")


class ConfigError(Exception):
    """A required environment variable is missing or empty."""


@dataclass(frozen=True)
class CyberwaveSettings:
    api_key: str = field(repr=False)
    # Optional. Only needed when the API token is scoped to more than one
    # workspace; a single-workspace token has nothing to disambiguate.
    workspace_id: str | None
    # Asset UUID or registry id, passed to the SDK as given. None only when the
    # caller asked for it not to be required (asset discovery).
    robot_asset_identifier: str | None
    # Optional. When unset the SDK reuses or creates a "Quickstart Environment".
    environment_id: str | None = None


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


def load_cyberwave_settings(*, require_asset_identifier: bool = True) -> CyberwaveSettings:
    """Load Cyberwave settings.

    ``require_asset_identifier=False`` is for asset discovery, which runs
    before the identifier is known.
    """
    identifier = _optional(ROBOT_ASSET_VAR)
    if identifier is None and _optional(LEGACY_ROBOT_ASSET_VAR) is not None:
        # No silent fallback: the old value may be a registry id that the SDK
        # cannot resolve, and using it quietly would hide that.
        raise ConfigError(
            f"{LEGACY_ROBOT_ASSET_VAR} is set but {ROBOT_ASSET_VAR} is not. The "
            f"variable was renamed. In {ENV_FILE}, rename {LEGACY_ROBOT_ASSET_VAR} "
            f"to {ROBOT_ASSET_VAR} and set it to the asset UUID "
            "(see docs/robot-capabilities.md)."
        )

    required = ["CYBERWAVE_API_KEY"]
    if require_asset_identifier:
        required.append(ROBOT_ASSET_VAR)
    values = _require(*required)
    return CyberwaveSettings(
        api_key=values["CYBERWAVE_API_KEY"],
        workspace_id=_optional("CYBERWAVE_WORKSPACE_ID"),
        robot_asset_identifier=identifier,
        environment_id=_optional("CYBERWAVE_ENVIRONMENT_ID"),
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
