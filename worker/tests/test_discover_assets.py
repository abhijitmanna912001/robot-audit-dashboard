"""Unit tests for discover_cyberwave_assets. Offline.

The SDK client is replaced with a strict fake: it offers only the read-only
calls the script is allowed to make, and any other attribute access (for
example ``twin`` or ``affect``) fails the test. No network, no credentials.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

import discover_cyberwave_assets as discover
from config import CyberwaveSettings

REGISTRY_ID = "the-robot-studio/so101"
FAKE_WORKSPACE_UUID = "fake-workspace-uuid-for-tests"
FAKE_API_KEY = "fake-api-key-for-tests"

SO101 = SimpleNamespace(
    uuid="asset-uuid-1",
    name="SO-101",
    registry_id=REGISTRY_ID,
    registry_id_alias=None,
    slug="the-robot-studio/catalog/so101",
    visibility="public",
    workspace_uuid=None,
    has_universal_schema=True,
    supported_simulation_backends=["mujoco"],
    capabilities={"has_joints": True, "can_grip": True},
    metadata={"vendor": "x"},
)
OTHER = {"uuid": "asset-uuid-2", "name": "SO-100", "registry_id": "the-robot-studio/so100"}


class FakeAssetsApi:
    """Only the three read-only lookups exist. Everything is recorded."""

    def __init__(self, client: "StrictFakeClient") -> None:
        self._client = client

    def _answer(self, kind: str, key: str) -> Any:
        self._client.calls.append((kind, key, self._client.workspace_filter))
        answers = (
            self._client.scoped_answers
            if self._client.workspace_filter
            else self._client.unscoped_answers
        )
        answer = answers.get((kind, key), [] if kind != "get" else None)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def list_by_registry_id(self, registry_id: str) -> Any:
        return self._answer("list_by_registry_id", registry_id)

    def search(self, query: str) -> Any:
        return self._answer("search", query)

    def get(self, asset_id: str) -> Any:
        return self._answer("get", asset_id)


class StrictFakeClient:
    """Fake SDK client. Any attribute not defined here raises AttributeError."""

    def __init__(
        self,
        unscoped_answers: dict | None = None,
        scoped_answers: dict | None = None,
        workspace_filter: str | None = None,
    ) -> None:
        self.unscoped_answers = unscoped_answers or {}
        self.scoped_answers = scoped_answers or {}
        self.workspace_filter = workspace_filter
        self.calls: list[tuple[str, str, str | None]] = []
        self.configure_calls: list[dict[str, Any]] = []
        self.assets = FakeAssetsApi(self)
        self.workspaces = SimpleNamespace(
            list=lambda: [SimpleNamespace(uuid=FAKE_WORKSPACE_UUID)]
        )

    def configure(self, **kwargs: Any) -> None:
        self.configure_calls.append(kwargs)
        self.workspace_filter = kwargs.get("workspace_id")


class FakeRobot:
    def __init__(self, client: StrictFakeClient) -> None:
        self.sdk_client = client
        self.runtime_mode = "simulation"
        self.disconnected = False

    def connect(self) -> None:
        pass

    def disconnect(self) -> list[str]:
        self.disconnected = True
        return []


def run_script(
    client: StrictFakeClient,
    workspace_id: str | None = None,
    asset_identifier: str | None = None,
) -> tuple[int, str]:
    settings = CyberwaveSettings(
        api_key=FAKE_API_KEY,
        workspace_id=workspace_id,
        robot_asset_identifier=asset_identifier,
    )
    output = io.StringIO()
    with (
        mock.patch.object(
            discover, "load_cyberwave_settings", return_value=settings
        ) as load_settings,
        mock.patch.object(discover, "SimulatedRobot", return_value=FakeRobot(client)),
        mock.patch.object(discover, "bundled_catalog_uuid", return_value="bundled-uuid"),
        contextlib.redirect_stdout(output),
    ):
        code = discover.run(registry_id=None, search_terms=("so101",))
    # Discovery must work before the identifier is known.
    load_settings.assert_called_once_with(require_asset_identifier=False)
    return code, output.getvalue()


class QueryTests(unittest.TestCase):
    def test_query_assets_makes_only_the_read_only_lookups(self) -> None:
        client = StrictFakeClient()
        discover.query_assets(client.assets, REGISTRY_ID, ("so101", "so-101"), "bundled-uuid")
        self.assertEqual(
            [(kind, key) for kind, key, _ in client.calls],
            [
                ("list_by_registry_id", REGISTRY_ID),
                ("search", "so101"),
                ("search", "so-101"),
                ("get", "bundled-uuid"),
            ],
        )

    def test_a_failing_lookup_is_recorded_not_raised(self) -> None:
        result = discover.run_query("boom", lambda: (_ for _ in ()).throw(RuntimeError("HTTP 400")))
        self.assertEqual(result.assets, [])
        self.assertIn("RuntimeError", result.error or "")
        self.assertIn("HTTP 400", result.error or "")

    def test_single_object_and_none_are_normalised_to_lists(self) -> None:
        self.assertEqual(discover.run_query("one", lambda: SO101).assets, [SO101])
        self.assertEqual(discover.run_query("none", lambda: None).assets, [])


class AssetDescriptionTests(unittest.TestCase):
    def test_exact_match_uses_registry_id_or_alias_case_insensitively(self) -> None:
        self.assertTrue(discover.is_exact_match(SO101, REGISTRY_ID))
        self.assertTrue(discover.is_exact_match({"registry_id_alias": "The-Robot-Studio/SO101"}, REGISTRY_ID))
        self.assertFalse(discover.is_exact_match(OTHER, REGISTRY_ID))

    def test_describe_asset_reports_the_requested_fields(self) -> None:
        described = discover.describe_asset(SO101, {FAKE_WORKSPACE_UUID})
        self.assertEqual(described["name"], "SO-101")
        self.assertEqual(described["uuid"], "asset-uuid-1")
        self.assertEqual(described["registry_id"], REGISTRY_ID)
        self.assertEqual(described["slug"], "the-robot-studio/catalog/so101")
        self.assertEqual(described["capabilities"], {"has_joints": True, "can_grip": True})
        self.assertIsNone(described["owned_by_a_visible_workspace"])
        self.assertEqual(described["metadata_keys"], ["vendor"])

    def test_describe_asset_accepts_a_plain_dict(self) -> None:
        described = discover.describe_asset(
            {**OTHER, "workspace_uuid": FAKE_WORKSPACE_UUID}, {FAKE_WORKSPACE_UUID}
        )
        self.assertEqual(described["name"], "SO-100")
        self.assertTrue(described["owned_by_a_visible_workspace"])
        self.assertIsNone(described["capabilities"])

    def test_unique_assets_keeps_one_per_uuid(self) -> None:
        results = [
            discover.QueryResult("a", assets=[SO101, OTHER]),
            discover.QueryResult("b", assets=[SO101]),
        ]
        self.assertEqual(discover.unique_assets(results), [SO101, OTHER])


class RunTests(unittest.TestCase):
    def test_empty_result_is_reported_clearly(self) -> None:
        client = StrictFakeClient()
        code, output = run_script(client)
        self.assertEqual(code, 3)
        self.assertIn("NO ASSETS FOUND", output)
        self.assertIn("(none)", output)

    def test_workspace_filter_hiding_the_asset_is_called_out(self) -> None:
        client = StrictFakeClient(
            unscoped_answers={("list_by_registry_id", REGISTRY_ID): [SO101]}
        )
        code, output = run_script(client)
        self.assertEqual(code, 0)
        self.assertIn("NOT when", output)
        self.assertIn("exact registry id match", output)
        # Pass B scoped the client the way cw.twin() does, without an API call.
        self.assertEqual(client.configure_calls, [{"workspace_id": FAKE_WORKSPACE_UUID}])

    def test_found_in_both_passes(self) -> None:
        answers = {("search", "so101"): [SO101, OTHER]}
        client = StrictFakeClient(unscoped_answers=answers, scoped_answers=answers)
        code, output = run_script(client)
        self.assertEqual(code, 0)
        self.assertIn("is found", output)
        self.assertIn("SO-100", output)

    def test_lookup_errors_are_shown_and_do_not_stop_the_run(self) -> None:
        client = StrictFakeClient(
            unscoped_answers={("list_by_registry_id", REGISTRY_ID): RuntimeError("HTTP 400 workspace_required")}
        )
        code, output = run_script(client)
        self.assertEqual(code, 3)
        self.assertIn("ERROR: RuntimeError: HTTP 400 workspace_required", output)
        self.assertIn("raised an error", output)

    def test_configured_workspace_runs_a_single_pass(self) -> None:
        client = StrictFakeClient(
            scoped_answers={("list_by_registry_id", REGISTRY_ID): [SO101]},
            workspace_filter=FAKE_WORKSPACE_UUID,
        )
        code, _output = run_script(client, workspace_id=FAKE_WORKSPACE_UUID)
        self.assertEqual(code, 0)
        self.assertEqual(client.configure_calls, [])

    def test_no_credentials_or_workspace_ids_are_printed(self) -> None:
        client = StrictFakeClient(unscoped_answers={("search", "so101"): [SO101]})
        _code, output = run_script(client)
        self.assertNotIn(FAKE_API_KEY, output)
        self.assertNotIn(FAKE_WORKSPACE_UUID, output)

    def test_registry_id_is_kept_separately_from_the_configured_identifier(self) -> None:
        # The lookup uses the canonical registry id even when the configured
        # identifier is a UUID.
        self.assertEqual(discover.SO101_REGISTRY_ID, REGISTRY_ID)
        client = StrictFakeClient(
            unscoped_answers={("list_by_registry_id", REGISTRY_ID): [SO101]}
        )
        code, output = run_script(client, asset_identifier=SO101.uuid)
        self.assertEqual(code, 0)
        self.assertIn(("list_by_registry_id", REGISTRY_ID, None), client.calls)
        self.assertIn("ROBOT_ASSET_IDENTIFIER matches the uuid", output)

    def test_identifier_check_messages(self) -> None:
        self.assertIn("is not set", discover.identifier_check(None, [SO101]))
        self.assertIn("matches the uuid", discover.identifier_check("ASSET-UUID-1", [SO101]))
        self.assertIn("Prefer the uuid", discover.identifier_check(REGISTRY_ID, [SO101]))
        self.assertIn("does NOT match", discover.identifier_check("something-else", [SO101]))
        self.assertIn("does NOT match", discover.identifier_check("asset-uuid-1", []))

    def test_strict_fake_has_no_twin_or_affect(self) -> None:
        # Guards the tests above: had the script touched these, it would have
        # raised AttributeError and returned exit code 1.
        client = StrictFakeClient()
        self.assertFalse(hasattr(client, "twin"))
        self.assertFalse(hasattr(client, "affect"))
        self.assertFalse(hasattr(client.assets, "create"))


if __name__ == "__main__":
    unittest.main()
