"""Stored portfolio recipes bind real source pages, proof profiles, units, and replay."""

# pyright: reportPrivateUsage=false
# ruff: noqa: PLR2004 - numeric reference results for isolated persisted fixtures

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx2
import pytest
from test_analytics_production_runtime import _checked_release
from test_analytics_runtime_portfolio import _source_rows

from saxo_bank_mcp import analytics_release
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_instrument_identity import (
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import (
    AnalysisResult,
    AnalysisStatus,
    MetricClass,
    QualityState,
    ValueUnitClass,
)
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_runtime import execute_analysis
from saxo_bank_mcp.analytics_runtime_inputs import AnalyticsExecutionError
from saxo_bank_mcp.analytics_runtime_portfolio import SUPPORTED_KINDS
from saxo_bank_mcp.analytics_source_contracts import (
    SourceJsonValue,
    SourceResponseShape,
    compare_source_schema,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    StorageDataType,
    StorageScope,
    StoredSourcePage,
)
from saxo_bank_mcp.analytics_sync import sync_price_bars
from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.mcp_analytics_tools import StoredPortfolioToolRequest

_ACCOUNT = "aa_00000000000040008000000000000012"
_CURRENT_DATASET = "ds_00000000000040008000000000000042"
_BASELINE_DATASET = "ds_00000000000040008000000000000044"
_START = datetime(2026, 2, 1, tzinfo=UTC)
_END = _START + timedelta(days=4)
_HANDLE = instrument_handle_for_saxo_identity("Stock", 123)
_PRICES = (100.0, 101.0, 99.0, 103.0, 105.0)
_EXPECTED_METRICS: dict[str, dict[str, float]] = {
    "portfolio_overview": {"account_value": 1250.0, "cash_balance": 250.0},
    "cash_and_settlement": {"settled_cash": 220.0, "unsettled_cash": -10.0},
    "portfolio_margin": {"margin_headroom": 150.0, "margin_utilization": 0.4},
    "portfolio_exposure": {"gross_exposure": 1000.0, "net_exposure": 1000.0},
    "portfolio_performance": {"time_weighted_return": 0.06, "account_value": 1060.0},
    "portfolio_risk": {
        "volatility": 0.5873694727617124,
        "maximum_drawdown": -0.03 / 1.02,
        "historical_var": 0.02357142857142857,
        "expected_shortfall": 0.03 / 1.02,
    },
    "portfolio_attribution": {"local_asset_contribution": 100.0 / 9.0},
    "portfolio_comparison": {"active_return": 0.01},
    "portfolio_time_machine": {"do_nothing_counterfactual": -50.0},
    "income_calendar": {"income_amount": 20.0, "dividend_amount": 20.0},
    "corporate_action_center": {},
    "cost_xray": {"total_cost": 2.0, "commission_cost": 2.0},
    "regulatory_cost_report": {"regulatory_ex_post_cost": 2.0},
    "trading_mirror": {"win_rate": 100.0},
    "execution_quality": {},
    "portfolio_query": {"cash_balance": 250.0},
    "tax_lot_export": {},
}


class _ChartExecutor:
    """Return documented chart fields without constructing a network transport."""

    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        assert operation.method == "GET"
        assert request_target == "/chart/v3/charts"
        assert params["Uic"] == "123"
        self.call_count += 1
        rows = [
            {
                "Time": (_START + timedelta(days=index)).isoformat(),
                "OpenBid": price,
                "HighBid": price,
                "LowBid": price,
                "CloseBid": price,
                "Volume": 10.0,
                "PriceType": "RealTime",
            }
            for index, price in enumerate(_PRICES)
        ]
        return httpx2.Response(
            200,
            content=json.dumps({"Data": rows, "DataVersion": 1}).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


def _persist_account(
    config: AnalyticsConfig,
    rows_by_contract: dict[str, list[dict[str, object]]],
    *,
    dataset_id: str = _CURRENT_DATASET,
    captured_at: datetime = _END,
) -> None:
    contracts = source_contracts_by_id()
    revision = f"fixture:portfolio:{captured_at.date().isoformat()}"
    store = AnalyticsStore.open(config)
    try:
        pages: list[StoredSourcePage] = []
        for identifier, rows in rows_by_contract.items():
            contract = contracts[identifier]
            if contract.response_shape in {
                SourceResponseShape.DATA_ARRAY,
                SourceResponseShape.TOP_LEVEL_ARRAY,
            }:
                response: Mapping[str, object] = {"Data": rows}
            else:
                response = rows[0]
            comparison = compare_source_schema(contract, response)
            assert comparison.compatible, comparison.model_dump(mode="json")
            pages.append(
                store.put_source_page(
                    source_kind=contract.source_kind,
                    page_key=f"{revision}:{identifier}",
                    source_revision=revision,
                    contract_name=identifier,
                    contract_sha256=source_contract_fingerprint(contract),
                    payload=cast(
                        "dict[str, SourceJsonValue]", {"contract_id": identifier, "rows": rows}
                    ),
                    row_count=len(rows),
                    source_timestamp=captured_at,
                    account_scope=_ACCOUNT,
                    instrument_handle=None,
                )
            )
        store.create_dataset(
            dataset_id=dataset_id,
            account_scope=_ACCOUNT,
            source_scope="saxo_openapi",
            source_revision=revision,
            source_page_ids=tuple(page.page_id for page in pages),
            created_at=captured_at,
            coverage_start=_START,
            coverage_end=captured_at,
            quality_state=QualityState.COMPLETE,
        )
        material = store.get_authenticated_dataset_material(dataset_id)
        assert {page.contract_name for page in material.pages} == set(rows_by_contract)
    finally:
        store.close()


def _persist_chart(config: AnalyticsConfig) -> tuple[str, _ChartExecutor]:
    metadata = {
        "asset_type": "Stock",
        "identifier": 123,
        "display_label": "Fixture stock",
    }
    serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    store = AnalyticsStore.open(config)
    try:
        with store.market_ingestion_transaction(0) as connection:
            put_saxo_instrument_identity(
                connection,
                asset_type="Stock",
                uic=123,
                safe_label="Fixture stock",
                source_revision="fixture:portfolio:identity",
                source_timestamp=_END,
                fingerprint_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                metadata_json=serialized,
                update_existing=False,
            )
    finally:
        store.close()
    executor = _ChartExecutor()
    result = asyncio.run(
        sync_price_bars(
            _HANDLE,
            ChartInterval.ONE_DAY,
            _START,
            _END,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _END + timedelta(days=1),
        )
    )
    assert result.source_request_count == 1
    assert result.datasets[0].row_count == len(_PRICES)
    return result.datasets[0].dataset_id, executor


def _fixture_inputs(config: AnalyticsConfig, kind: str) -> tuple[tuple[str, ...], _ChartExecutor]:
    sources = _source_rows()
    sources["costs_v1"][0].update(
        {
            "AccountCurrency": "USD",
            "AccountID": "F1",
            "Amount": 10.0,
            "AssetType": "Stock",
            "CostCalculationAssumptions": [],
            "Instrument": "Fixture stock",
            "Price": 100.0,
            "Uic": 123,
        }
    )
    dataset_ids = [_CURRENT_DATASET]
    if kind == "portfolio_time_machine":
        sources.pop("corporate_action_events_v2")
        sources.pop("corporate_action_holdings_v2")
        _persist_account(
            config,
            {identifier: sources[identifier] for identifier in ("balances_v1", "positions_v1")},
            dataset_id=_BASELINE_DATASET,
            captured_at=_START,
        )
        dataset_ids.insert(0, _BASELINE_DATASET)
    _persist_account(config, sources)
    chart_id, executor = _persist_chart(config)
    return (*dataset_ids, chart_id), executor


def _request(
    kind: str, dataset_ids: tuple[str, ...], **options: object
) -> StoredPortfolioToolRequest:
    arguments: dict[str, object] = {
        "analysis_kind": kind,
        "dataset_ids": list(dataset_ids),
        "options": options,
    }
    if kind == "portfolio_comparison":
        options["benchmark_handle"] = _HANDLE
    if kind == "portfolio_query":
        arguments["query_intent"] = {"intent": "metric", "metric": "cash_balance"}
    return StoredPortfolioToolRequest.model_validate_json(json.dumps(arguments))


def _assert_units(result: AnalysisResult) -> None:
    for metric in result.metrics:
        if metric.unit_class is ValueUnitClass.MONETARY:
            assert metric.unit == "reporting_currency"
            assert metric.currency == "USD"
        elif metric.metric_id in {"local_asset_contribution", "currency_contribution", "win_rate"}:
            assert metric.unit == "percent"
            assert metric.unit_class is ValueUnitClass.PERCENTAGE
            assert metric.currency is None
        else:
            assert metric.currency is None
    for table in result.tables:
        for row in table.rows:
            for cell in row.cells:
                if type(cell.value) in {int, float}:
                    assert cell.unit is not None


@pytest.mark.parametrize("kind", sorted(_EXPECTED_METRICS))
def test_all_portfolio_recipes_execute_and_replay_authenticated_stored_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    assert set(_EXPECTED_METRICS) == SUPPORTED_KINDS
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})
    dataset_ids, executor = _fixture_inputs(config, kind)
    _checked_release(monkeypatch)
    registry = analytics_release.load_production_registry(config)
    request = _request(kind, dataset_ids)
    store = AnalyticsStore.open(config)
    try:
        result = execute_analysis(
            tool_name="saxo_analyze_portfolio",
            request=request,
            config=config,
            store=store,
            registry=registry,
        )
        assert result.status in {AnalysisStatus.VERIFIED, AnalysisStatus.DEGRADED}
        assert result.metrics or result.tables
        actual = {metric.metric_id: metric.value for metric in result.metrics}
        for identifier, expected in _EXPECTED_METRICS[kind].items():
            assert actual[identifier] == pytest.approx(expected)
        _assert_units(result)
        assert "private-" not in result.model_dump_json()
        assert replay_analysis(result.analysis_id, config=config, registry=registry) == result
        assert (
            execute_analysis(
                tool_name="saxo_analyze_portfolio",
                request=request,
                config=config,
                store=store,
                registry=registry,
            )
            == result
        )
        assert {item.dataset_id for item in result.provenance.input_dataset_dependencies} == set(
            dataset_ids
        )
        if kind == "portfolio_time_machine":
            metric = next(
                item for item in result.metrics if item.metric_id == "do_nothing_counterfactual"
            )
            assert metric.metric_class is MetricClass.APPROXIMATION
        if kind == "tax_lot_export":
            assert result.status is AnalysisStatus.DEGRADED
            assert {"tax_cost_basis", "lot_matching", "tax_treatment"} <= set(
                result.unavailable_fields
            )
            assert result.tables[0].title == "Closed trade activity for tax reconciliation"
            assert result.tables[0].rows[0].cells[1].value == 90.0
        if kind == "corporate_action_center":
            assert not result.metrics
            assert result.tables[0].rows[0].cells[0].value == 10.0
        if kind == "execution_quality":
            assert not result.metrics
            assert result.tables[1].rows[0].cells[0].value == 90.0
    finally:
        store.close()
    assert executor.call_count == 1


def test_stored_regulatory_report_retains_illustration_basis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})
    dataset_ids, executor = _fixture_inputs(config, "regulatory_cost_report")
    _checked_release(monkeypatch)
    registry = analytics_release.load_production_registry(config)
    store = AnalyticsStore.open(config)
    try:
        result = execute_analysis(
            tool_name="saxo_analyze_portfolio",
            request=_request("regulatory_cost_report", dataset_ids, cost_report_basis="ex_ante"),
            config=config,
            store=store,
            registry=registry,
        )
        assert {metric.metric_id: metric.value for metric in result.metrics} == {
            "regulatory_ex_ante_cost": 9.0
        }
        assert replay_analysis(result.analysis_id, config=config, registry=registry) == result
        _assert_units(result)
    finally:
        store.close()
    assert executor.call_count == 1


def test_stored_tax_activity_cannot_claim_authoritative_lot_basis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})
    dataset_ids, executor = _fixture_inputs(config, "tax_lot_export")
    _checked_release(monkeypatch)
    registry = analytics_release.load_production_registry(config)
    store = AnalyticsStore.open(config)
    try:
        with pytest.raises(
            AnalyticsExecutionError, match="authoritative_tax_lot_basis_unavailable"
        ) as error:
            execute_analysis(
                tool_name="saxo_analyze_portfolio",
                request=_request(
                    "tax_lot_export", dataset_ids, tax_lot_export_mode="authoritative_tax_lots"
                ),
                config=config,
                store=store,
                registry=registry,
            )
        assert error.value.missing_fields == ("tax_cost_basis", "lot_matching", "tax_treatment")
        assert not store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,)))
    finally:
        store.close()
    assert executor.call_count == 1
