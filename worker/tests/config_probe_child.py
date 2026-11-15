"""TEST ONLY: runs the worker's configuration loaders INSIDE a spawned child.

Top level is the standard library only; ``config`` is imported inside ``main``,
in the child. ``config.dotenv_values`` is replaced first by a recording stub that
returns fake secrets, so the real ``.env`` can never be read here, even if the
child guard in ``config.load_config`` regressed. The stub also tells the test
whether the loader reached the file at all.
"""

from __future__ import annotations

from typing import Any

FAKE_FILE = {
    "SUPABASE_URL": "https://file.invalid",
    "SUPABASE_SECRET_KEY": "only-in-the-file",
    "CRITIC_URL": "https://critic-file.invalid",
    "CRITIC_SHARED_SECRET": "critic-only-in-the-file",
}


def main(conn: Any) -> None:
    import multiprocessing

    import config

    reads: list[str] = []

    def stub(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        reads.append("read")
        return dict(FAKE_FILE)

    config.dotenv_values = stub
    outcome: dict[str, Any] = {"is_child": multiprocessing.parent_process() is not None}
    for name in ("load_config", "load_supabase_settings", "load_critic_settings"):
        try:
            getattr(config, name)()
            outcome[name] = ("returned", "")
        except config.ConfigError as exc:
            outcome[name] = ("ConfigError", str(exc))
        except BaseException as exc:  # anything else is also a finding
            outcome[name] = (type(exc).__name__, "")
    outcome["redact"] = config.redact("x only-in-the-file y")
    outcome["file_reads"] = len(reads)
    conn.send(outcome)
    conn.close()
