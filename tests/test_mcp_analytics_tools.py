from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, cast

import pytest
from fastmcp import Client

import saxo_bank_mcp.mcp_analytics_tools as tools_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instruments import ResearchRefusal
from saxo_bank_mcp.analytics_jobs import AnalyticsJobManager, JobProgress, JobStatus
from saxo_bank_mcp.analytics_models import (
    HandleKind,
    QualityState,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_position_sizing import PositionSizingRequest
from saxo_bank_mcp.analytics_resolver import (
    InstrumentState,
    ResolutionError,
    ResolutionIssue,
    ResolutionIssueCode,
    ResolutionResult,
    ResolutionStatus,
    ResolvedInstrument,
)
from saxo_bank_mcp.analytics_sync import DatasetPage
from saxo_bank_mcp.analytics_tool_descriptions import ANALYTICS_TOOL_DESCRIPTIONS
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
) -> None:
    request = PositionSizingRequest(
        dataset_id=new_safe_handle(HandleKind.DATASET_ID),
        account_alias="aa_00000000000040008000000000000051",
        instrument_handle=new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
        reporting_currency="USD",
        method="stop_distance",
        maximum_loss=Decimal(10),
        risk_budget_confirmed=True,
        entry_price=Decimal(100),
        stop_price=Decimal(99),
        value_per_price_unit=Decimal(1),
        lot_size=Decimal(1),
        portfolio_value=Decimal(1000),
        maximum_weight=Decimal("0.2"),
        buying_power=Decimal(500),
        reserved_buffer=Decimal(0),
        estimated_transaction_cost=Decimal(1),
        margin_headroom=Decimal(500),
        margin_requirement_per_money_unit=Decimal(1),
        source_bindings=(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )
    observed: list[tuple[VisibilityMode, bool]] = []

    def sizing(
        _request: PositionSizingRequest,
        *,
        visibility: VisibilityMode,
        trusted_local_host: bool,
    ) -> ResearchRefusal:
        observed.append((visibility, trusted_local_host))
        return ResearchRefusal(
            analysis_kind="position_sizing",
            reason_code="source_binding_required",
            reason="A current bound source is required.",
            dataset_ids=(_request.dataset_id,),
            instrument_handles=(_request.instrument_handle,),
            warnings=("source_quality_reduced",),
            source_scope=None,
        )

    monkeypatch.setattr(tools_module, "size_position", sizing)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    sim = tools_module.saxo_size_position(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    live = tools_module.saxo_size_position(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )

    assert observed == [(VisibilityMode.PRIVATE_USER_RESULT, True)]
    assert sim.warnings == ("source_quality_reduced",)
    assert live.status == "refused"
    assert live.reason_code == "inline_private_not_enabled"
    assert live.next_tool == "saxo_export_analysis"


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

    assert response.status == "running"
    assert response.result is not None
    assert response.result["analysis_id"] is None
    assert response.result["artifact_ids"] == []
    assert response.next_tool == "saxo_manage_analysis_job"
    assert missing.status == "refused"
    assert missing.reason_code == "job_id_required"
    assert missing.next_tool == "saxo_manage_analysis_job"


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
