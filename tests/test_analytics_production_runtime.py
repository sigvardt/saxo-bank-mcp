"""Normal MCP calculation, replay and export over authenticated offline Saxo fixtures."""

# pyright: reportPrivateUsage=false

# ruff: noqa: SLF001, PLR2004 - isolated fixture setup and numeric reference results

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from test_mcp_analytics_tools import _state_env, _synced_chart_fixture

import saxo_bank_mcp.analytics_release as release
import saxo_bank_mcp.mcp_analytics_tools as analytics_tools
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_export import StoredTableExportRequest, export_analysis
from saxo_bank_mcp.analytics_models import (
    AnalysisResult,
    HandleKind,
    QualityState,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_provenance import AnalysisReplayRefused, replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_render import ArtifactBindingRegistry, ArtifactRefusal
from saxo_bank_mcp.analytics_runtime import PROOF_CHECKS, RECIPE_KINDS, execute_analysis
from saxo_bank_mcp.analytics_runtime_inputs import AnalyticsExecutionError
from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    StorageDataType,
    StorageScope,
    StoreValidationError,
)
from saxo_bank_mcp.analytics_sync import ReferenceDetailsSyncSpec, SyncResearchRequest
from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.server import create_mcp_server


def _checked_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the release receipt is injected; dispatcher, calculation and replay remain real."""
    code_hash = release.runtime_code_sha256()
    monkeypatch.setattr(release, "runtime_code_sha256", lambda: code_hash)
    receipt = {
        "code_sha256": code_hash,
        "definition_catalog_sha256": release.load_metric_definition_catalog().fingerprint_sha256,
        "source_catalog_sha256": source_contract_catalog_sha256(),
        "recipe_kinds": list(RECIPE_KINDS),
        "checks_passed": list(PROOF_CHECKS),
        "suite_passed": True,
    }
    monkeypatch.setattr(release, "_release_document", lambda: receipt)


@pytest.mark.parametrize("kind", ["instrument_price_return", "instrument_dossier"])
def test_normal_live_mcp_calculates_replays_and_exports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = analytics_tools._analytics_config()
    handle, dataset_id, _, executor = asyncio.run(_synced_chart_fixture(config))
    _checked_release(monkeypatch)

    async def call() -> AnalysisResult:
        async with Client(create_mcp_server()) as client:
            response = await client.call_tool(
                "saxo_analyze_instruments",
                {
                    "request": {
                        "analysis_kind": kind,
                        "dataset_ids": [dataset_id],
                        "instrument_handles": [handle],
                        "rolling_window": 2,
                    }
                },
            )
        assert not response.is_error
        assert response.structured_content is not None
        body = response.structured_content
        assert body["status"] in {"verified", "degraded"}, body.get("reason_code")
        return AnalysisResult.model_validate_json(json.dumps(body["result"]))

    result = asyncio.run(call())
    assert asyncio.run(call()) == result
    metrics = {metric.metric_id: metric.value for metric in result.metrics}
    assert metrics["price_return"] == pytest.approx(0.1)
    assert result.tables
    registry = release.load_production_registry(config)
    replayed = replay_analysis(result.analysis_id, config=config, registry=registry)
    assert replayed == result
    store = AnalyticsStore.open(config)
    try:
        material = store.get_authenticated_dataset_material(result.provenance.dataset_id)
        assert {page.contract_name for page in material.pages} >= {"chart_v3"}
        if kind == "instrument_dossier":
            assert "reference_instruments_v1" in {page.contract_name for page in material.pages}
        bindings = ArtifactBindingRegistry(config=config, proof_registry=registry)
        issued = bindings.issue(result.analysis_id)
        exported = export_analysis(
            StoredTableExportRequest(
                binding_id=issued.binding_id,
                output_format="json",
                table_id=result.tables[0].table_id,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
        assert not isinstance(exported, ArtifactRefusal)
    finally:
        store.close()
    assert executor.call_count == 1  # All calculation, replay and export are local.


def test_fingerprint_transport_still_computes_an_owner_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    config = analytics_tools._analytics_config()
    handle, dataset_id, _, _ = asyncio.run(_synced_chart_fixture(config))
    _checked_release(monkeypatch)
    response = analytics_tools.saxo_analyze_instruments(
        analytics_tools.StoredInstrumentToolRequest(
            analysis_kind="instrument_price_return",
            dataset_ids=(dataset_id,),
            instrument_handles=(handle,),
            rolling_window=2,
            visibility=VisibilityMode.FINGERPRINT_ONLY,
        )
    )
    assert response.status in {"verified", "degraded"}
    assert response.result is None
    assert response.analysis_id is not None
    replayed = replay_analysis(
        response.analysis_id, config=config, registry=release.load_production_registry(config)
    )
    assert next(
        metric.value for metric in replayed.metrics if metric.metric_id == "price_return"
    ) == pytest.approx(0.1)


def test_unverified_or_changed_release_never_activates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path)})
    monkeypatch.setattr(release, "_release_document", lambda: {"suite_passed": True})
    registry = release.load_production_registry(config)
    assert all(
        profile.activation_state.value == "quarantined" for profile in registry.catalog.profiles
    )
    _checked_release(monkeypatch)
    monkeypatch.setattr(release, "runtime_code_sha256", lambda: "f" * 64)
    changed = release.load_production_registry(config)
    assert all(
        profile.activation_state.value == "quarantined" for profile in changed.catalog.profiles
    )


def test_live_dossier_uses_current_reference_contract_and_preserves_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = analytics_tools._analytics_config()
    handle, dataset_id, _, _ = asyncio.run(_synced_chart_fixture(config))
    store = AnalyticsStore.open(config)
    try:
        current = store.find_authenticated_source_materials(
            contract_name="reference_instruments_v1", instrument_handle=handle
        )
        obsolete = store.put_source_page(
            source_kind="reference_instruments",
            page_key="fixture:obsolete-reference",
            source_revision="capture:obsolete-reference",
            contract_name="reference_instruments_v1",
            contract_sha256="a" * 64,
            payload=current[0].payload,
            row_count=1,
            source_timestamp=current[0].source_timestamp + timedelta(days=1),
            account_scope="aggregate",
            instrument_handle=handle,
        )
    finally:
        store.close()
    _checked_release(monkeypatch)

    async def call() -> AnalysisResult:
        async with Client(create_mcp_server()) as client:
            response = await client.call_tool(
                "saxo_analyze_instruments",
                {
                    "request": {
                        "analysis_kind": "instrument_dossier",
                        "dataset_ids": [dataset_id],
                        "instrument_handles": [handle],
                        "rolling_window": 2,
                    }
                },
            )
        body = response.structured_content
        assert body is not None
        assert body["status"] in {"verified", "degraded"}, body.get("reason_code")
        return AnalysisResult.model_validate_json(json.dumps(body["result"]))

    result = asyncio.run(call())
    assert {metric.metric_id: metric.value for metric in result.metrics}["price_return"] == (
        pytest.approx(0.1)
    )
    assert (
        replay_analysis(
            result.analysis_id, config=config, registry=release.load_production_registry(config)
        )
        == result
    )
    store = AnalyticsStore.open(config)
    try:
        assert (
            store.find_authenticated_source_materials(
                contract_name="reference_instruments_v1", instrument_handle=handle
            )
            == current
        )
        entries = store.list_storage(StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)))
        retained = next(entry for entry in entries if entry.object_id == obsolete.page_id)
        assert retained.fingerprint_sha256 == obsolete.fingerprint_sha256
        assert retained.source_revision == "capture:obsolete-reference"
        with pytest.raises(StoreValidationError, match="persisted source contract metadata"):
            store.create_dataset(
                dataset_id=new_safe_handle(HandleKind.DATASET_ID),
                account_scope="aggregate",
                source_scope="saxo_openapi",
                source_revision=obsolete.source_revision,
                source_page_ids=(obsolete.page_id,),
                created_at=current[0].source_timestamp,
                coverage_start=current[0].source_timestamp,
                coverage_end=current[0].source_timestamp,
                quality_state=QualityState.COMPLETE,
            )
        with store._write_connection() as connection:
            connection.execute(
                "UPDATE source_pages SET payload_json = '{}' WHERE page_id = ?",
                (current[0].page_id,),
            )
        with pytest.raises(StoreValidationError, match="source page integrity"):
            store.find_authenticated_source_materials(
                contract_name="reference_instruments_v1", instrument_handle=handle
            )
        with pytest.raises(AnalysisReplayRefused) as raised:
            replay_analysis(
                result.analysis_id, config=config, registry=release.load_production_registry(config)
            )
        assert raised.value.reason_code == "dataset_integrity_changed"
    finally:
        store.close()


def test_cancelled_calculation_never_persists_a_conclusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    config = analytics_tools._analytics_config()
    handle, dataset_id, _, _ = asyncio.run(_synced_chart_fixture(config))
    _checked_release(monkeypatch)
    store = AnalyticsStore.open(config)
    calls = 0

    def cancel_after_math() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AnalyticsExecutionError("job_cancelled")

    try:
        with pytest.raises(AnalyticsExecutionError, match="job_cancelled"):
            execute_analysis(
                tool_name="saxo_analyze_instruments",
                request=analytics_tools.StoredInstrumentToolRequest(
                    analysis_kind="instrument_price_return",
                    dataset_ids=(dataset_id,),
                    instrument_handles=(handle,),
                    rolling_window=2,
                ),
                config=config,
                store=store,
                registry=release.load_production_registry(config),
                cancellation_check=cancel_after_math,
            )
        assert not store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,)))
    finally:
        store.close()


def test_normal_live_mcp_captures_exact_details_and_replays_conditions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = analytics_tools._analytics_config()
    handle, _, _, _ = asyncio.run(_synced_chart_fixture(config))
    calls: list[str] = []

    async def get_details(
        operation: EndpointOperation, request_target: str, params: Mapping[str, str]
    ) -> httpx2.Response:
        assert operation.method == "GET"
        assert request_target == "/ref/v1/instruments/details"
        assert params["Uics"] == "1"
        assert params["AssetTypes"] == "Stock"
        calls.append(request_target)
        return httpx2.Response(
            200,
            content=json.dumps(
                {
                    "Data": [
                        {
                            "Uic": 1,
                            "AssetType": "Stock",
                            "CurrencyCode": "EUR",
                            "LotSize": 1.0,
                            "TradingStatus": "Tradable",
                            "Format": {"Decimals": 2, "OrderDecimals": 2},
                            "Exchange": {"ExchangeId": "SYNTHETIC", "Name": "Fixture"},
                        }
                    ]
                }
            ).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )

    provider = SaxoAnalyticsProvider(request_executor=get_details)
    monkeypatch.setattr(analytics_tools, "SaxoAnalyticsProvider", lambda: provider)
    _checked_release(monkeypatch)
    sync = asyncio.run(
        analytics_tools._sync_research_request(
            SyncResearchRequest(items=(ReferenceDetailsSyncSpec(instrument_handle=handle),))
        )
    )
    response = analytics_tools.saxo_analyze_instruments(
        analytics_tools.StoredInstrumentToolRequest(
            analysis_kind="trading_conditions",
            dataset_ids=(sync.datasets[0].dataset_id,),
            instrument_handles=(handle,),
        )
    )
    assert response.status in {"verified", "degraded"}
    assert response.result is not None
    assert isinstance(response.result, AnalysisResult)
    assert response.result.tables[0].rows[0].cells[1].value == "EUR"
    assert (
        replay_analysis(
            response.result.analysis_id,
            config=config,
            registry=release.load_production_registry(config),
        )
        == response.result
    )
    assert calls == ["/ref/v1/instruments/details"]
