# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import httpx2
import pytest
from fastmcp import Client

import saxo_bank_mcp.mcp_analytics_tools as tools_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instrument_identity import (
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_jobs import (
    AnalyticsJobManager,
    JobProgress,
    JobRequest,
    JobStatus,
)
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_models import (
    HandleKind,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    ArtifactOwnerBinding,
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofRegistry,
    SourceContractProofBinding,
)
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_resolver import (
    InstrumentState,
    ResolutionError,
    ResolutionIssue,
    ResolutionIssueCode,
    ResolutionResult,
    ResolutionStatus,
    ResolvedInstrument,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_sync import DatasetPage, sync_price_bars
from saxo_bank_mcp.analytics_tool_descriptions import ANALYTICS_TOOL_DESCRIPTIONS
from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.server import create_mcp_server
from saxo_bank_mcp.server_tool_ids import (
    ALL_LOGICAL_TOOL_IDS,
    ANALYTICS_TOOL_IDS,
    EXPECTED_TOOL_COUNT,
)

EXACT_ANALYTICS_TOOL_IDS: Final[tuple[str, ...]] = (
    "saxo_analytics_capabilities",
    "saxo_resolve_research_universe",
    "saxo_manage_research_universe",
    "saxo_sync_research_data",
    "saxo_get_research_dataset",
    "saxo_analyze_market",
    "saxo_analyze_instruments",
    "saxo_analyze_portfolio",
    "saxo_size_position",
    "saxo_run_scenario",
    "saxo_optimize_portfolio",
    "saxo_model_derivatives",
    "saxo_backtest_strategy",
    "saxo_propose_trade_from_analysis",
    "saxo_render_analysis",
    "saxo_export_analysis",
    "saxo_explain_analysis",
    "saxo_manage_analysis_job",
    "saxo_list_analytics_storage",
    "saxo_preview_analytics_deletion",
    "saxo_delete_analytics_data",
)
_EXPECTED_TOOL_COUNT: Final = 60
_NOW = datetime(2026, 8, 3, 10, tzinfo=UTC)


class _ChartFixtureExecutor:
    """Return one local synthetic chart response without opening a socket."""

    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        del operation, request_target, params
        self.call_count += 1
        return httpx2.Response(
            200,
            content=json.dumps(
                {
                    "Data": [
                        {
                            "CloseBid": 100.0,
                            "PriceType": "RealTime",
                            "Time": "2026-08-03T09:00:00Z",
                            "Volume": 10,
                        },
                        {
                            "CloseBid": 110.0,
                            "PriceType": "RealTime",
                            "Time": "2026-08-03T09:01:00Z",
                            "Volume": 11,
                        },
                    ],
                    "DataVersion": 1,
                }
            ).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


def _seed_chart_instrument(config: AnalyticsConfig) -> str:
    handle = instrument_handle_for_saxo_identity("Stock", 1)
    metadata: dict[str, object] = {
        "aliases": [],
        "asset_type": "Stock",
        "display_label": "Synthetic instrument",
        "exchange": None,
        "identifier": 1,
        "symbol": None,
    }
    metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    store = AnalyticsStore.open(config)
    try:
        with store.market_ingestion_transaction(0) as connection:
            write = put_saxo_instrument_identity(
                connection,
                asset_type="Stock",
                uic=1,
                safe_label="Synthetic instrument",
                source_revision="fixture:instrument",
                source_timestamp=_NOW,
                fingerprint_sha256=hashlib.sha256(metadata_json.encode()).hexdigest(),
                metadata_json=metadata_json,
                update_existing=False,
            )
    finally:
        store.close()
    assert write.instrument_handle == handle
    return handle


def _active_market_registry(
    config: AnalyticsConfig,
    *,
    source_revision: str,
) -> ProofRegistry:
    definitions = load_metric_definition_catalog()
    definition = definitions.by_id()["price_return"]
    contract = source_contracts_by_id()["chart_v3"]
    source = SourceContractProofBinding(
        contract_id="chart_v3",
        contract_sha256=source_contract_fingerprint(contract),
        field_paths=("CloseBid", "Time"),
    )
    profile = ProofProfile(
        proof_profile_id="vp_market_comparison_test_v1",
        profile_version="1",
        activation_state=ProfileActivationState.ACTIVE,
        quarantine_reason=None,
        analysis_kind="market_comparison",
        schema_version="1",
        metric_definitions=(
            MetricDefinitionBinding(
                metric_id=definition.metric_id,
                definition_version=definition.definition_version,
            ),
        ),
        source_contracts=(source,),
        source_revision=source_revision,
        engines=(
            EngineProofBinding(
                engine_name="saxo_analytics",
                engine_version="1",
                code_commit="abcdef0",
            ),
        ),
        artifact_template_ids=("relative_performance",),
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        valid_until=datetime(2099, 1, 1, tzinfo=UTC),
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version="area-f-test-1",
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        production_metric_ids=("price_return",),
        production_analysis_kinds=("market_comparison",),
        production_artifact_template_ids=("relative_performance",),
        artifact_owners=(
            ArtifactOwnerBinding(
                template_id="relative_performance",
                analysis_kind="market_comparison",
            ),
        ),
        source_field_coverage=(source,),
        profiles=(profile,),
    )
    return ProofRegistry(definitions=definitions, catalog=catalog, config=config)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB", "1")


def test_exact_analytics_catalog_moves_server_from_39_to_60() -> None:
    assert ANALYTICS_TOOL_IDS == EXACT_ANALYTICS_TOOL_IDS
    assert len(ANALYTICS_TOOL_IDS) == len(set(ANALYTICS_TOOL_IDS)) == len(EXACT_ANALYTICS_TOOL_IDS)
    assert EXPECTED_TOOL_COUNT == _EXPECTED_TOOL_COUNT
    assert len(ALL_LOGICAL_TOOL_IDS) == EXPECTED_TOOL_COUNT
    assert set(ANALYTICS_TOOL_DESCRIPTIONS) == set(ANALYTICS_TOOL_IDS)


@pytest.mark.anyio
async def test_all_analytics_tools_register_once_with_schema_and_description() -> None:
    server = create_mcp_server()
    async with Client(server) as client:
        tools = list(await client.list_tools())

    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)) == EXPECTED_TOOL_COUNT
    assert set(names) == ALL_LOGICAL_TOOL_IDS
    for tool_id in ANALYTICS_TOOL_IDS:
        tool = next(item for item in tools if item.name == tool_id)
        assert tool.description == ANALYTICS_TOOL_DESCRIPTIONS[tool_id]
        assert tool.inputSchema["type"] == "object"
        assert tool.annotations is not None
    render = next(item for item in tools if item.name == "saxo_render_analysis")
    assert render.inputSchema["properties"]["output_format"]["enum"] == ["png", "html"]


@pytest.mark.anyio
async def test_analytics_schemas_expose_no_paths_raw_broker_ids_or_trust_switches() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        tools = list(await client.list_tools())

    forbidden = {
        "account_id",
        "account_key",
        "client_id",
        "client_key",
        "disclaimer_response",
        "path",
        "raw_account_id",
        "raw_client_id",
        "trusted_local_host",
        "uic",
        "url",
    }
    property_names = {name for tool in tools for name in _schema_property_names(tool.inputSchema)}
    assert forbidden.isdisjoint(property_names)


@pytest.mark.anyio
async def test_analysis_schemas_accept_only_handles_and_user_parameters() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    source_fact_fields = {
        "account_alias",
        "bars",
        "buying_power_available",
        "components",
        "cost_estimate",
        "current_position_exposure",
        "current_position_quantity",
        "dataset",
        "datasets",
        "decision_bar",
        "decision_quote",
        "holdings",
        "margin_available",
        "portfolio_value",
        "quote",
        "quotes",
        "saxo_greeks",
        "source_bindings",
    }
    analysis_tools = (
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
    )
    for tool_id in analysis_tools:
        fields = set(_schema_property_names(listed[tool_id].inputSchema))
        assert source_fact_fields.isdisjoint(fields), tool_id
        assert "dataset_id" in fields or "dataset_ids" in fields

    proposal_fields = set(
        _schema_property_names(listed["saxo_propose_trade_from_analysis"].inputSchema)
    )
    assert "proposal" not in proposal_fields
    assert {
        "analysis_id",
        "side",
        "quantity",
        "instrument_handle",
    } <= proposal_fields
    assert source_fact_fields.isdisjoint(proposal_fields)


@pytest.mark.anyio
async def test_analysis_and_artifact_tools_publish_discriminated_output_schemas() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    for tool_id in (
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
        "saxo_propose_trade_from_analysis",
    ):
        schema = listed[tool_id].outputSchema
        assert schema is not None, tool_id
        assert {"verified", "degraded", "refused"} <= set(_schema_literal_values(schema))
        assert not _contains_open_object_schema(schema), tool_id
        for field in (
            "network_call_made",
            "local_state_changed",
            "broker_write_made",
            "next_action",
        ):
            assert field in _schema_property_names(schema), (tool_id, field)

    for tool_id in ("saxo_render_analysis", "saxo_export_analysis"):
        schema = listed[tool_id].outputSchema
        assert schema is not None, tool_id
        assert {"inline", "resource_link", "refused"} <= set(_schema_literal_values(schema))
        assert not _contains_open_object_schema(schema), tool_id
        assert "broker_write_made" in _schema_property_names(schema)


@pytest.mark.anyio
async def test_all_analytics_tools_publish_explicit_state_discriminated_output_schemas() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    expected_states = {
        "saxo_analytics_capabilities": {"passed", "refused"},
        "saxo_resolve_research_universe": {
            "resolved",
            "ambiguous",
            "unavailable",
            "refused",
        },
        "saxo_manage_research_universe": {"passed", "refused"},
        "saxo_sync_research_data": {"passed", "degraded", "refused"},
        "saxo_get_research_dataset": {"passed", "refused"},
        "saxo_explain_analysis": {"passed", "refused"},
        "saxo_manage_analysis_job": {
            "job_queued",
            "job_running",
            "job_completed",
            "job_failed",
            "job_cancelled",
            "job_expired",
            "job_interrupted_restart_required",
            "refused",
        },
        "saxo_list_analytics_storage": {"passed", "refused"},
        "saxo_preview_analytics_deletion": {"preview_ready", "refused"},
        "saxo_delete_analytics_data": {"deleted", "refused"},
    }
    for tool_id, states in expected_states.items():
        schema = listed[tool_id].outputSchema
        assert schema is not None, tool_id
        assert states <= set(_schema_literal_values(schema)), tool_id
        assert "pattern" not in _status_schema_keywords(schema), tool_id
        assert not _contains_open_object_schema(schema), tool_id
        for field in (
            "network_call_made",
            "local_state_changed",
            "broker_write_made",
            "approval_authority",
            "execution_authority",
            "disclaimer_response_available",
            "next_action",
        ):
            assert field in _schema_property_names(schema), (tool_id, field)


@pytest.mark.anyio
async def test_analysis_fastmcp_request_accepts_safe_handles_and_returns_canonical_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    server = create_mcp_server(allowed_tools=frozenset({"saxo_size_position"}))

    async with Client(server) as client:
        response = await client.call_tool(
            "saxo_size_position",
            {
                "request": {
                    "dataset_id": new_safe_handle(HandleKind.DATASET_ID),
                    "instrument_handle": new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                    "method": "stop_distance",
                    "maximum_loss": "10",
                    "risk_budget_confirmed": True,
                    "stop_price": "99",
                    "visibility": "fingerprint_only",
                }
            },
        )

    assert response.structured_content is not None
    assert response.structured_content["status"] == "refused"
    assert response.structured_content["analysis_id"] is None
    assert response.structured_content["broker_write_made"] is False


@pytest.mark.anyio
async def test_ambiguous_resolution_returns_exact_recovery_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    matches = tuple(
        ResolvedInstrument(
            instrument_handle=new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
            display_label=f"Synthetic listing {ordinal}",
            symbol="SYN",
            asset_type="Stock",
            exchange=f"EX{ordinal}",
            state=InstrumentState.CURRENT,
        )
        for ordinal in range(2)
    )

    async def resolve(
        _self: object,
        query: str,
        asset_types: tuple[str, ...],
        exchanges: tuple[str, ...],
    ) -> ResolutionResult:
        assert query == "Synthetic"
        assert asset_types == ()
        assert exchanges == ()
        return ResolutionResult(
            query=query,
            status=ResolutionStatus.AMBIGUOUS,
            matches=matches,
            issues=(
                ResolutionIssue(
                    code=ResolutionIssueCode.MULTIPLE_LISTINGS,
                    message="Several materially different Saxo instruments match the query.",
                ),
            ),
        )

    monkeypatch.setattr(tools_module.InstrumentResolver, "resolve_instruments", resolve)
    response = await tools_module.saxo_resolve_research_universe(
        query="Synthetic",
        asset_types=(),
        exchanges=(),
    )

    assert response.status == "ambiguous"
    assert response.next_tool == "saxo_resolve_research_universe"
    assert "asset type or exchange" in (response.next_action or "")
    assert response.broker_write_made is False


@pytest.mark.anyio
async def test_known_failure_is_value_free_and_names_the_next_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    private_marker = "private-value-must-not-escape"

    async def fail(*_args: object, **_kwargs: object) -> ResolutionResult:
        raise ResolutionError(f"{private_marker} at a private local path")

    monkeypatch.setattr(tools_module.InstrumentResolver, "resolve_instruments", fail)
    response = await tools_module.saxo_resolve_research_universe(
        query="Synthetic",
        asset_types=(),
        exchanges=(),
    )

    assert response.status == "refused"
    assert response.reason_code == "instrument_resolution_failed"
    assert response.next_tool == "saxo_analytics_capabilities"
    assert private_marker not in repr(response)
    assert "path" not in repr(response).lower()


def test_dataset_adapter_calls_existing_service_and_returns_exact_next_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    calls: list[tuple[str, int, int]] = []

    def get_page(
        selected_dataset_id: str,
        page: int,
        limit: int,
        *,
        config: object,
    ) -> DatasetPage:
        del config
        calls.append((selected_dataset_id, page, limit))
        return DatasetPage(
            dataset_id=dataset_id,
            page=1,
            limit=100,
            total_rows=0,
            rows=(),
            next_page=None,
        )

    monkeypatch.setattr(tools_module, "get_dataset", get_page)
    response = tools_module.saxo_get_research_dataset(dataset_id, page=1, limit=100)

    assert calls == [(dataset_id, 1, 100)]
    assert response.status == "passed"
    assert response.next_tool == "saxo_analyze_instruments"
    assert response.broker_write_made is False


def test_private_delivery_is_derived_from_server_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    request = tools_module.StoredPositionSizingToolRequest(
        dataset_id=new_safe_handle(HandleKind.DATASET_ID),
        instrument_handle=new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
        method="stop_distance",
        maximum_loss=Decimal(10),
        risk_budget_confirmed=True,
        stop_price=Decimal(99),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    sim = tools_module.saxo_size_position(request)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    live = tools_module.saxo_size_position(request)

    assert sim.status == "refused"
    assert sim.reason_code == "analytics_object_not_found"
    assert live.status == "refused"
    assert live.reason_code == "inline_private_not_enabled"
    assert live.next_tool == "saxo_export_analysis"


def test_analysis_authenticates_stored_handle_and_honors_frozen_proof_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    instrument_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    authenticated: list[str] = []
    closed: list[bool] = []

    class _Store:
        def get_authenticated_dataset(self, selected: str) -> object:
            authenticated.append(selected)
            return object()

        def close(self) -> None:
            closed.append(True)

    class _Registry:
        def profile(self, analysis_kind: str) -> object:
            assert analysis_kind == "position_sizing"
            return SimpleNamespace(
                activation_state=SimpleNamespace(value="quarantined"),
                quarantine_reason="implementation_pending",
            )

    def open_store(_config: AnalyticsConfig) -> _Store:
        return _Store()

    def proof_registry(_config: AnalyticsConfig) -> _Registry:
        return _Registry()

    monkeypatch.setattr(
        tools_module.AnalyticsStore,
        "open",
        staticmethod(open_store),
    )
    monkeypatch.setattr(tools_module, "_proof_registry", proof_registry)

    response = tools_module.saxo_size_position(
        tools_module.StoredPositionSizingToolRequest(
            dataset_id=dataset_id,
            instrument_handle=instrument_handle,
            method="stop_distance",
            maximum_loss=Decimal(10),
            risk_budget_confirmed=True,
            stop_price=Decimal(99),
        )
    )

    assert authenticated == [dataset_id]
    assert closed == [True]
    assert isinstance(response, tools_module.RefusedAnalysisToolResponse)
    assert response.status == "refused"
    assert response.analysis_id is None
    assert response.reason_code == "implementation_pending"
    assert response.local_state_changed is False


@pytest.mark.anyio
async def test_active_proof_market_adapter_persists_replays_renders_and_explains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    handle = _seed_chart_instrument(config)
    executor = _ChartFixtureExecutor()
    sync = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        _NOW - timedelta(hours=1),
        _NOW - timedelta(minutes=59),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _NOW,
    )
    dataset_id = sync.datasets[0].dataset_id
    store = AnalyticsStore.open(config)
    try:
        dataset = store.get_authenticated_dataset(dataset_id)
    finally:
        store.close()
    registry = _active_market_registry(
        config,
        source_revision=dataset.source_revision,
    )

    def active_registry(_config: AnalyticsConfig) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)

    calculated = tools_module.saxo_analyze_market(
        tools_module.StoredMarketToolRequest(
            analysis_kind="market_comparison",
            dataset_ids=(dataset_id,),
            periods_per_year=252,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
    )

    assert executor.call_count == 1
    assert isinstance(calculated, tools_module.VerifiedAnalysisToolResponse)
    assert calculated.status == "verified"
    assert calculated.analysis_id == calculated.result.analysis_id
    assert calculated.result.metrics[0].metric_id == "price_return"
    assert calculated.result.metrics[0].value == pytest.approx(0.1)
    assert calculated.result.provenance.dataset_id == dataset_id
    replayed = replay_analysis(
        calculated.analysis_id,
        config=config,
        registry=registry,
    )
    assert replayed == calculated.result

    explained = tools_module.saxo_explain_analysis(calculated.analysis_id)
    assert explained.status == "passed"
    assert explained.result is not None
    rendered = tools_module.saxo_render_analysis(
        calculated.analysis_id,
        "relative_performance",
        output_format="html",
        width=1200,
        height=675,
    )
    assert rendered.structured_content is not None
    assert rendered.structured_content["status"] == "inline", rendered.structured_content
    assert rendered.structured_content["broker_write_made"] is False

    started = await tools_module.saxo_manage_analysis_job(
        action="start",
        request=tools_module.AnalyticsJobToolRequest(
            job_kind="report_generation",
            analysis_ids=(calculated.analysis_id,),
            parameters=(
                tools_module.AnalyticsJobParameter(
                    name="template_id",
                    value="relative_performance",
                ),
                tools_module.AnalyticsJobParameter(
                    name="output_format",
                    value="html",
                ),
                tools_module.AnalyticsJobParameter(
                    name="viewport_width",
                    value=800,
                ),
            ),
            total_work_units=1,
        ),
    )
    checked = started
    try:
        for _ in range(100):
            if checked.status not in {"job_queued", "job_running"}:
                break
            await asyncio.sleep(0)
            checked = await tools_module.saxo_manage_analysis_job(
                action="check",
                job_id=checked.result.job_id if checked.result is not None else None,
            )
        assert checked.status == "job_completed"
        assert checked.result is not None
        assert checked.result.conclusion_available is True
        assert len(checked.result.artifact_ids) == 1
    finally:
        await tools_module.shutdown_analytics_runtime()


@pytest.mark.anyio
async def test_job_adapter_preserves_bounded_transitions_and_no_partial_conclusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    job_id = new_safe_handle(HandleKind.JOB_ID)
    status = JobStatus(
        job_id=job_id,
        state="running",
        status_code="job_running",
        request_fingerprint="a" * 64,
        created_at=_NOW,
        updated_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
        progress=JobProgress(completed_units=1, total_units=4, remaining_units=3),
        analysis_id=None,
        artifact_ids=(),
        conclusion_available=False,
        restart_allowed=False,
    )

    class _Manager:
        async def get_job(self, selected_job_id: str) -> JobStatus:
            assert selected_job_id == job_id
            return status

    def manager_for_config(_config: AnalyticsConfig) -> AnalyticsJobManager:
        return cast("AnalyticsJobManager", _Manager())

    manager_factory = cast(
        "Callable[[AnalyticsConfig], AnalyticsJobManager]",
        manager_for_config,
    )
    monkeypatch.setattr(tools_module, "_job_manager_for_config", manager_factory)
    response = await tools_module.saxo_manage_analysis_job(action="check", job_id=job_id)
    missing = await tools_module.saxo_manage_analysis_job(action="cancel")

    assert response.status == "job_running"
    assert response.result is not None
    assert response.result.analysis_id is None
    assert response.result.artifact_ids == ()
    assert response.next_tool == "saxo_manage_analysis_job"
    assert missing.status == "refused"
    assert missing.reason_code == "job_id_required"
    assert missing.next_tool == "saxo_manage_analysis_job"


@pytest.mark.anyio
async def test_process_job_runtime_registers_real_handler_and_closes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    manager = tools_module._job_manager_for_config(  # noqa: SLF001
        tools_module._analytics_config()  # noqa: SLF001
    )

    assert set(manager._handlers) == {  # noqa: SLF001
        "monte_carlo",
        "optimization",
        "backtest",
        "report_generation",
    }
    request = JobRequest(
        job_kind="report_generation",
        analysis_ids=(new_safe_handle(HandleKind.ANALYSIS_ID),),
        total_work_units=1,
    )
    status = await manager.start_job(request)
    assert status.state in {"queued", "running"}
    cancelled = await manager.cancel_job(status.job_id)
    checked = await manager.get_job(status.job_id)
    assert cancelled.state == "cancelled"
    assert checked.state == "cancelled"
    assert checked.conclusion_available is False

    await tools_module.shutdown_analytics_runtime()
    assert tools_module._job_runtime is None  # noqa: SLF001


@pytest.mark.anyio
async def test_fastmcp_lifespan_owns_job_runtime_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    server = create_mcp_server(allowed_tools=frozenset({"saxo_manage_analysis_job"}))

    async with Client(server) as client:
        response = await client.call_tool(
            "saxo_manage_analysis_job",
            {
                "action": "start",
                "request": {
                    "job_kind": "report_generation",
                    "dataset_ids": [],
                    "analysis_ids": [new_safe_handle(HandleKind.ANALYSIS_ID)],
                    "instrument_handles": [],
                    "parameters": [],
                    "total_work_units": 1,
                    "restart_interrupted": False,
                },
            },
        )
        assert response.structured_content is not None
        assert response.structured_content["status"] in {"job_queued", "job_running"}
        assert tools_module._job_runtime is not None  # noqa: SLF001

    assert tools_module._job_runtime is None  # noqa: SLF001


@pytest.mark.anyio
async def test_runtime_shutdown_finishes_cleanup_when_transport_is_cancelled() -> None:
    started = asyncio.Event()
    manager_stopped: list[bool] = []
    store_closed: list[bool] = []

    class _Manager:
        async def shutdown(self) -> None:
            started.set()
            await asyncio.sleep(0.05)
            manager_stopped.append(True)

    class _Store:
        def close(self) -> None:
            store_closed.append(True)

    runtime = cast(
        "tuple[str, AnalyticsStore, AnalyticsJobManager]",
        ("fixture-runtime", _Store(), _Manager()),
    )
    tools_module._job_runtime = runtime  # noqa: SLF001
    shutdown = asyncio.create_task(tools_module.shutdown_analytics_runtime())
    await started.wait()

    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert manager_stopped == [True]
    assert store_closed == [True]
    assert tools_module._job_runtime is None  # noqa: SLF001


@pytest.mark.anyio
async def test_trade_proposal_replays_then_refuses_unbound_context_without_preview_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    server = create_mcp_server(allowed_tools=frozenset({"saxo_propose_trade_from_analysis"}))
    async with Client(server) as client:
        response = await client.call_tool(
            "saxo_propose_trade_from_analysis",
            {
                "analysis_id": new_safe_handle(HandleKind.ANALYSIS_ID),
                "instrument_handle": new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                "side": "buy",
                "quantity": "1",
                "visibility": "fingerprint_only",
            },
        )

    assert response.structured_content is not None
    assert response.structured_content["status"] == "refused"
    assert response.structured_content["next_tool"] != "saxo_create_order_preview"
    assert response.structured_content["broker_write_made"] is False


def test_trade_proposal_binds_instrument_and_authenticates_analysis_dataset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    bound_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    other_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    authenticated: list[str] = []

    class _Store:
        def get_authenticated_dataset(self, selected: str) -> object:
            authenticated.append(selected)
            return object()

        def close(self) -> None:
            return None

    def replay(
        _analysis_id: str,
        *,
        config: AnalyticsConfig,
        registry: object,
    ) -> object:
        del config, registry
        return SimpleNamespace(
            provenance=SimpleNamespace(dataset_id=dataset_id),
            request=SimpleNamespace(instrument_handles=(bound_handle,)),
        )

    def open_store(_config: AnalyticsConfig) -> _Store:
        return _Store()

    def proof_registry(_config: AnalyticsConfig) -> object:
        return object()

    monkeypatch.setattr(
        tools_module,
        "replay_analysis",
        replay,
    )
    monkeypatch.setattr(
        tools_module.AnalyticsStore,
        "open",
        staticmethod(open_store),
    )
    monkeypatch.setattr(tools_module, "_proof_registry", proof_registry)

    mismatch = tools_module.saxo_propose_trade_from_analysis(
        analysis_id,
        other_handle,
        "buy",
        Decimal(1),
    )
    bound = tools_module.saxo_propose_trade_from_analysis(
        analysis_id,
        bound_handle,
        "buy",
        Decimal(1),
    )

    assert authenticated == [dataset_id, dataset_id]
    assert isinstance(mismatch, tools_module.RefusedAnalysisToolResponse)
    assert isinstance(bound, tools_module.RefusedAnalysisToolResponse)
    assert mismatch.reason_code == "proposal_context_mismatch"
    assert bound.reason_code == "pretrade_context_unavailable"
    assert mismatch.next_tool != "saxo_create_order_preview"
    assert bound.next_tool != "saxo_create_order_preview"


def _schema_property_names(schema: object) -> tuple[str, ...]:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        properties = mapping.get("properties", {})
        property_mapping = (
            cast("dict[object, object]", properties) if isinstance(properties, dict) else {}
        )
        names = tuple(str(name).casefold() for name in property_mapping)
        return names + tuple(
            nested for value in mapping.values() for nested in _schema_property_names(value)
        )
    if isinstance(schema, list):
        values = cast("list[object]", schema)
        return tuple(nested for value in values for nested in _schema_property_names(value))
    return ()


def _schema_literal_values(schema: object) -> tuple[str, ...]:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        values: list[str] = []
        const = mapping.get("const")
        if isinstance(const, str):
            values.append(const)
        enum = mapping.get("enum")
        if isinstance(enum, list):
            values.extend(item for item in cast("list[object]", enum) if isinstance(item, str))
        return tuple(values) + tuple(
            nested for value in mapping.values() for nested in _schema_literal_values(value)
        )
    if isinstance(schema, list):
        return tuple(
            nested
            for value in cast("list[object]", schema)
            for nested in _schema_literal_values(value)
        )
    return ()


def _status_schema_keywords(schema: object) -> tuple[str, ...]:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        keywords: list[str] = []
        properties = mapping.get("properties")
        if isinstance(properties, dict):
            status = cast("dict[object, object]", properties).get("status")
            if isinstance(status, dict):
                keywords.extend(str(key) for key in cast("dict[object, object]", status))
        return tuple(keywords) + tuple(
            nested for value in mapping.values() for nested in _status_schema_keywords(value)
        )
    if isinstance(schema, list):
        return tuple(
            nested
            for value in cast("list[object]", schema)
            for nested in _status_schema_keywords(value)
        )
    return ()


def _contains_open_object_schema(schema: object) -> bool:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        if mapping.get("additionalProperties") is True:
            return True
        return any(_contains_open_object_schema(value) for value in mapping.values())
    if isinstance(schema, list):
        return any(_contains_open_object_schema(value) for value in cast("list[object]", schema))
    return False
