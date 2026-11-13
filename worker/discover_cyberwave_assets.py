"""Manual, read-only Cyberwave asset discovery for the SO-101.

Run from the worker directory:

    .venv\\Scripts\\python.exe discover_cyberwave_assets.py

Why this exists: ``cw.twin("the-robot-studio/so101")`` failed with "Asset not
found". The SDK's ``assets.get_by_registry_id`` swallows every API error and
returns None, so "not found" can hide a real error. This script runs the same
lookups one at a time and shows what each one returns or raises.

Every call is an HTTP GET:

    workspaces.list()
    assets.list_by_registry_id(<registry id>)   GET /api/v1/assets?registry_id=...
    assets.search(<term>)                       GET /api/v1/assets?search=...
    assets.get(<uuid>)                          GET /api/v1/assets/{uuid}

It never calls ``twin()``, ``affect()``, a create/update/delete method, or any
joint or motion command. It creates no twin and no environment.

The lookups run twice when CYBERWAVE_WORKSPACE_ID is not set: once with no
workspace filter, and once with ``workspace_uuid`` added to every request.
The second pass reproduces what ``cw.twin()`` does: after it resolves the
Quickstart Environment it stores the workspace on the client config, and from
then on the SDK appends ``workspace_uuid`` to every request.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import ConfigError, load_cyberwave_settings, redact
from cyberwave_client import RobotConnectionError, SimulatedRobot

# Canonical registry id of the SO-101 in the Cyberwave catalog. Kept here, and
# separate from ROBOT_ASSET_IDENTIFIER, because discovery looks the asset up by
# registry id while the worker instantiates it by UUID.
SO101_REGISTRY_ID = "the-robot-studio/so101"
DEFAULT_SEARCH_TERMS = ("so101", "so-101", "so 101")
MAX_WORKSPACES_TO_PROBE = 3


@dataclass
class QueryResult:
    label: str
    assets: list[Any] = field(default_factory=list)
    error: str | None = None


def _field(asset: Any, name: str) -> Any:
    """Read a field from an SDK model object or a plain dict."""
    if isinstance(asset, dict):
        return asset.get(name)
    return getattr(asset, name, None)


def run_query(label: str, call: Callable[[], Any]) -> QueryResult:
    """Run one lookup. An exception is recorded, never raised."""
    try:
        found = call()
    except Exception as exc:
        return QueryResult(label, error=f"{type(exc).__name__}: {redact(exc)}")
    if found is None:
        return QueryResult(label)
    if isinstance(found, (list, tuple)):
        return QueryResult(label, assets=list(found))
    return QueryResult(label, assets=[found])


def query_assets(
    assets_api: Any,
    registry_id: str,
    search_terms: tuple[str, ...],
    catalog_uuid: str | None,
) -> list[QueryResult]:
    """Run every read-only lookup against ``client.assets``."""
    results = [
        run_query(
            f'list_by_registry_id("{registry_id}")',
            lambda: assets_api.list_by_registry_id(registry_id),
        )
    ]
    for term in search_terms:
        results.append(
            run_query(f'search("{term}")', lambda term=term: assets_api.search(term))
        )
    if catalog_uuid:
        results.append(
            run_query(
                f'get("{catalog_uuid}")  [uuid from the SDK bundled catalog]',
                lambda: assets_api.get(catalog_uuid),
            )
        )
    return results


def is_exact_match(asset: Any, registry_id: str) -> bool:
    """True when the asset's registry id or alias equals ``registry_id``."""
    wanted = registry_id.strip().lower()
    for name in ("registry_id", "registry_id_alias"):
        value = _field(asset, name)
        if isinstance(value, str) and value.strip().lower() == wanted:
            return True
    return False


def describe_asset(asset: Any, visible_workspaces: set[str]) -> dict[str, Any]:
    """Pick the fields worth showing. No credentials are part of an asset."""
    workspace_uuid = _field(asset, "workspace_uuid")
    metadata = _field(asset, "metadata")
    return {
        "name": _field(asset, "name"),
        "uuid": _field(asset, "uuid"),
        "registry_id": _field(asset, "registry_id"),
        "registry_id_alias": _field(asset, "registry_id_alias"),
        "slug": _field(asset, "slug"),
        "visibility": _field(asset, "visibility"),
        "owned_by_a_visible_workspace": (
            str(workspace_uuid) in visible_workspaces if workspace_uuid else None
        ),
        "has_universal_schema": _field(asset, "has_universal_schema"),
        "supported_simulation_backends": _field(asset, "supported_simulation_backends"),
        "capabilities": _field(asset, "capabilities"),
        "metadata_keys": sorted(metadata) if isinstance(metadata, dict) else None,
    }


def unique_assets(results: list[QueryResult]) -> list[Any]:
    """All assets across the results, one per uuid, in first-seen order."""
    seen: set[str] = set()
    unique: list[Any] = []
    for result in results:
        for asset in result.assets:
            key = str(_field(asset, "uuid"))
            if key not in seen:
                seen.add(key)
                unique.append(asset)
    return unique


def conclusion(
    registry_id: str,
    unscoped: list[QueryResult] | None,
    scoped: list[QueryResult],
) -> list[str]:
    """Plain-language reading of the two passes. Reports, does not guess."""
    everything = (unscoped or []) + scoped
    lines: list[str] = []

    def has_exact(results: list[QueryResult]) -> bool:
        return any(is_exact_match(a, registry_id) for a in unique_assets(results))

    errors = [r for r in everything if r.error]
    if errors:
        lines.append(
            f"{len(errors)} lookup(s) raised an error (shown above). "
            "cw.twin() hides these and reports 'Asset not found' instead."
        )

    found_any = bool(unique_assets(everything))
    exact_scoped = has_exact(scoped)
    exact_unscoped = has_exact(unscoped) if unscoped is not None else None

    if not found_any:
        lines.append(
            f"NO ASSETS FOUND. No lookup returned an asset for '{registry_id}' or "
            "the search terms."
        )
        lines.append(
            "Possible reasons: the catalog asset is not visible to this API key or "
            "workspace, its registry id has changed, or the errors above."
        )
        lines.append(
            "Next: check the asset catalog in the Cyberwave web app for the SO-101 "
            "and its exact registry id, or re-run with --query <other term>."
        )
    elif exact_unscoped and not exact_scoped:
        lines.append(
            f"'{registry_id}' IS found without a workspace filter but NOT when "
            "workspace_uuid is added to the request."
        )
        lines.append(
            "That matches the failed run: cw.twin() adds workspace_uuid to every "
            "request once it has resolved the Quickstart Environment."
        )
    elif exact_scoped:
        lines.append(f"'{registry_id}' is found. Exact registry id match confirmed.")
        if unscoped is not None:
            lines.append(
                "It is found with the workspace filter too, so the filter is not "
                "what made cw.twin() fail. Look at any errors above."
            )
    else:
        lines.append(
            f"No asset has registry id '{registry_id}', but other assets matched "
            "the search (listed above)."
        )
        lines.append(
            "If one of them is the SO-101, its uuid is the value to use for "
            "ROBOT_ASSET_IDENTIFIER."
        )
    return lines


def identifier_check(identifier: str | None, assets: list[Any]) -> str:
    """Say whether the configured ROBOT_ASSET_IDENTIFIER names a found asset."""
    if not identifier:
        return (
            "ROBOT_ASSET_IDENTIFIER is not set. Set it to the uuid of the exact "
            "match above before running the connection test."
        )
    wanted = identifier.strip().lower()
    for asset in assets:
        if str(_field(asset, "uuid")).lower() == wanted:
            return "ROBOT_ASSET_IDENTIFIER matches the uuid of an asset found above."
        if is_exact_match(asset, identifier):
            return (
                "ROBOT_ASSET_IDENTIFIER matches a registry id found above. Prefer "
                "the uuid: SDK 0.7.5 cannot resolve a registry id once the client "
                "carries a workspace filter."
            )
    return "ROBOT_ASSET_IDENTIFIER does NOT match any asset found above. Check the value."


def bundled_catalog_uuid(registry_id: str) -> str | None:
    """Asset uuid from the SDK's bundled catalog snapshot, if it lists one."""
    try:
        import cyberwave

        catalog_path = Path(cyberwave.__file__).parent / "assets_capabilities.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        uuid = catalog.get(registry_id, {}).get("uuid")
        return str(uuid) if uuid else None
    except Exception:
        return None


def _print_results(results: list[QueryResult]) -> None:
    for result in results:
        if result.error:
            print(f"    {result.label}\n        ERROR: {result.error}")
        else:
            print(f"    {result.label}\n        {len(result.assets)} asset(s) returned")


def _print_assets(assets: list[Any], registry_id: str, visible_workspaces: set[str]) -> None:
    if not assets:
        print("    (none)")
        return
    for index, asset in enumerate(assets, start=1):
        marker = "  <-- exact registry id match" if is_exact_match(asset, registry_id) else ""
        print(f"\n    Asset {index}{marker}")
        for key, value in describe_asset(asset, visible_workspaces).items():
            shown = (
                json.dumps(value, sort_keys=True, default=str)
                if isinstance(value, (dict, list))
                else value
            )
            print(f"      {key}: {shown}")


def run(registry_id: str | None, search_terms: tuple[str, ...]) -> int:
    try:
        # Discovery is how the identifier is found, so it is not required here.
        settings = load_cyberwave_settings(require_asset_identifier=False)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2
    registry_id = registry_id or SO101_REGISTRY_ID
    catalog_uuid = bundled_catalog_uuid(registry_id)

    robot = SimulatedRobot(settings)
    try:
        print("[1] Create client in simulation mode (no simulation is started)")
        robot.connect()
        client = robot.sdk_client
        print(f"    runtime mode: {robot.runtime_mode}")

        print("\n[2] Authenticate and list workspaces (read-only)")
        workspaces = client.workspaces.list()
        visible = [str(_field(w, "uuid")) for w in workspaces if _field(w, "uuid")]
        print(f"    OK. Workspaces visible to this key: {len(visible)}")

        unscoped: list[QueryResult] | None = None
        scoped: list[QueryResult] = []

        if settings.workspace_id:
            print("\n[3] Lookups with the configured workspace (CYBERWAVE_WORKSPACE_ID is set)")
            scoped = query_assets(client.assets, registry_id, search_terms, catalog_uuid)
            _print_results(scoped)
        else:
            print("\n[3] Pass A: lookups with no workspace filter")
            unscoped = query_assets(client.assets, registry_id, search_terms, catalog_uuid)
            _print_results(unscoped)

            for number, workspace_uuid in enumerate(visible[:MAX_WORKSPACES_TO_PROBE], start=1):
                print(
                    f"\n[4] Pass B: same lookups with workspace_uuid added "
                    f"(visible workspace #{number}), as cw.twin() does"
                )
                # Local client setting only; this is not an API call.
                client.configure(workspace_id=workspace_uuid)
                pass_results = query_assets(
                    client.assets, registry_id, search_terms, catalog_uuid
                )
                _print_results(pass_results)
                scoped.extend(pass_results)
            if not visible:
                print("\n[4] Pass B skipped: no workspace is visible to this key.")

        found = unique_assets((unscoped or []) + scoped)
        print("\n[5] Assets found (each shown once)")
        _print_assets(found, registry_id, set(visible))

        print("\n[6] Reading of the results")
        for line in conclusion(registry_id, unscoped, scoped):
            print(f"    {line}")
        print(f"    {identifier_check(settings.robot_asset_identifier, found)}")

        found_exact = any(is_exact_match(a, registry_id) for a in found)
        return 0 if found_exact else 3
    except RobotConnectionError as exc:
        print(f"\nFAILED: {redact(exc)}")
        return 1
    except Exception as exc:
        print(f"\nFAILED (unexpected {type(exc).__name__}): {redact(exc)}")
        return 1
    finally:
        cleanup_errors = robot.disconnect()
        if cleanup_errors:
            print("Disconnect finished with errors: " + redact("; ".join(cleanup_errors)))
        else:
            print("\nDisconnected cleanly. No twin or environment was created.")


def main() -> int:
    # Asset names can contain characters the Windows console cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--registry-id",
        default=None,
        help=f"Registry id to look up (default: {SO101_REGISTRY_ID}).",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=None,
        help="Search term. Repeat for several. Default: so101, so-101, 'so 101'.",
    )
    args = parser.parse_args()
    terms = tuple(args.query) if args.query else DEFAULT_SEARCH_TERMS
    return run(registry_id=args.registry_id, search_terms=terms)


if __name__ == "__main__":
    sys.exit(main())
