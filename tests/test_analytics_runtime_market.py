"""Observable market recipe results over immutable authenticated-input fixtures."""

# ruff: noqa: PLR2004

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client
from test_analytics_production_runtime import _checked_release
from test_analytics_sync import _PayloadExecutor

import saxo_bank_mcp.mcp_analytics_tools as analytics_tools
from saxo_bank_mcp.analytics_config import AnalyticsConfig, AnalyticsLimits, AnalyticsPaths
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import AnalysisResult, MetricClass, QualityState
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_release import load_production_registry
from saxo_bank_mcp.analytics_resolver import InstrumentResolver
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipePayload,
    RecipeRequest,
    ResearchInputs,
)
from saxo_bank_mcp.analytics_runtime_market import SUPPORTED_KINDS, execute_recipe
from saxo_bank_mcp.analytics_source_contracts import SourceJsonValue
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    AuthenticatedDatasetMaterial,
    AuthenticatedSourceMaterial,
    StoredDataset,
)
from saxo_bank_mcp.analytics_sync import (
    DatasetRow,
    PriceBarDatasetRow,
    QuoteDatasetRow,
    capture_quote,
)
from saxo_bank_mcp.server import create_mcp_server

_START = datetime(2026, 9, 1, tzinfo=UTC)
_HANDLE = instrument_handle_for_saxo_identity("Stock", 1)
_DATASET = "ds_00000000000040008000000000000001"
_OTHER_HANDLE = instrument_handle_for_saxo_identity("Stock", 2)
_BOND_HANDLE = instrument_handle_for_saxo_identity("Bond", 3)


@dataclass
class _ReferenceStore:
    pages: tuple[AuthenticatedSourceMaterial, ...]

    def find_authenticated_source_materials(
        self, *, contract_name: str, instrument_handle: str
    ) -> tuple[AuthenticatedSourceMaterial, ...]:
        return tuple(
            page
            for page in self.pages
            if page.contract_name == contract_name and page.instrument_handle == instrument_handle
        )


def _inputs(tmp_path: Path) -> ResearchInputs:
    """Inject the production read-only seam after its upstream authentication boundary."""
    rows = tuple(
        PriceBarDatasetRow(
            instrument_handle=_HANDLE,
            bar_time=_START + timedelta(days=index),
            interval=ChartInterval.ONE_DAY,
            open_value=close - 1.0,
            high_value=close + 2.0,
            low_value=close - 2.0,
            close_value=close,
            volume_value=10.0 + index,
            adjusted=False,
        )
        for index, close in enumerate((100.0, 110.0, 99.0, 120.0))
    )
    payload: dict[str, SourceJsonValue] = {
        "rows": [],
        "sync_metadata": {
            "data_kind": "price_bars",
            "missing_interval_count": 0,
            "warnings": [],
        },
    }
    page = AuthenticatedSourceMaterial(
        page_id="fixture-chart",
        contract_name="chart_v3",
        contract_sha256="a" * 64,
        source_kind="price_bars",
        source_revision="fixture:chart",
        source_timestamp=_START,
        fingerprint_sha256="b" * 64,
        account_scope="aggregate",
        instrument_handle=_HANDLE,
        payload=payload,
    )
    material = AuthenticatedDatasetMaterial(
        dataset=StoredDataset(
            dataset_id=_DATASET,
            source_revision="fixture:chart",
            fingerprint_sha256="c" * 64,
            row_count=len(rows),
            byte_count=1,
            created_at=_START + timedelta(days=4),
            quality_state=QualityState.COMPLETE,
        ),
        account_scope="aggregate",
        coverage_start=_START,
        coverage_end=_START + timedelta(days=3),
        pages=(page,),
    )
    config = AnalyticsConfig(
        limits=AnalyticsLimits(),
        paths=AnalyticsPaths(
            state_root=tmp_path,
            analytics_root=tmp_path / "analytics",
            artifacts_dir=tmp_path / "analytics" / "artifacts",
            store_path=tmp_path / "analytics" / "analytics.duckdb",
        ),
    )
    return ResearchInputs(
        config=config,
        store=cast("AnalyticsStore", _ReferenceStore((page,))),
        materials=(material,),
        _rows={_DATASET: rows},
    )


def test_price_recipe_preserves_endpoint_return_and_period_series(tmp_path: Path) -> None:
    result = execute_recipe(
        RecipeRequest(
            "instrument_price_return",
            (_DATASET,),
            (_HANDLE,),
            {"rolling_window": 2, "periods_per_year": 252.0},
        ),
        _inputs(tmp_path),
    )
    metrics = {metric.metric_id: metric.value for metric in result.metrics}
    assert metrics["price_return"] == pytest.approx(0.2)
    assert metrics["rolling_return"] == pytest.approx(120.0 / 110.0 - 1.0)
    periods = next(table for table in result.tables if table.table_id == "price_periods")
    assert [row.cells[0].value for row in periods.rows] == pytest.approx(
        [0.1, -0.1, 120.0 / 99.0 - 1.0]
    )


def _append_source(  # noqa: PLR0913 - fixture explicitly binds source and normalized material
    inputs: ResearchInputs,
    contract_name: str,
    rows: list[dict[str, SourceJsonValue]],
    *,
    handle: str | None = _HANDLE,
    normalized: tuple[DatasetRow, ...] = (),
    metadata: dict[str, SourceJsonValue] | None = None,
) -> str:
    index = len(inputs.materials) + 1
    dataset_id = f"ds_0000000000004000800000000000{index:04x}"
    revision = f"fixture:source:{index}"
    page = AuthenticatedSourceMaterial(
        page_id=f"fixture-page-{index}",
        contract_name=contract_name,
        contract_sha256="a" * 64,
        source_kind=contract_name,
        source_revision=revision,
        source_timestamp=_START,
        fingerprint_sha256="b" * 64,
        account_scope="aggregate",
        instrument_handle=handle,
        payload={"rows": cast("list[SourceJsonValue]", rows), "sync_metadata": metadata or {}},
    )
    inputs.materials = (
        *inputs.materials,
        AuthenticatedDatasetMaterial(
            dataset=StoredDataset(
                dataset_id=dataset_id,
                source_revision=revision,
                fingerprint_sha256="c" * 64,
                row_count=len(normalized) or len(rows),
                byte_count=1,
                created_at=inputs.as_of,
                quality_state=QualityState.COMPLETE,
            ),
            account_scope="aggregate",
            coverage_start=_START,
            coverage_end=inputs.as_of,
            pages=(page,),
        ),
    )
    inputs._rows[dataset_id] = normalized  # noqa: SLF001 - fixture injects immutable read results
    reference_store = cast("_ReferenceStore", inputs.store)
    reference_store.pages = (*reference_store.pages, page)
    return dataset_id


def _seed_reference_quote(inputs: ResearchInputs, *, delayed: int = 0, bond: bool = False) -> str:
    identifier, asset_type, handle = (3, "Bond", _BOND_HANDLE) if bond else (1, "Stock", _HANDLE)
    _append_source(
        inputs,
        "reference_instruments_v1",
        [
            {
                "Identifier": identifier,
                "AssetType": asset_type,
                "CurrencyCode": "USD",
                "Description": "Synthetic stock",
                "Symbol": "SYN",
                "ExchangeId": "XNAS",
            }
        ],
        handle=handle,
    )
    quote = QuoteDatasetRow(
        instrument_handle=handle,
        captured_at=inputs.as_of,
        bid_value=99.0,
        ask_value=101.0,
        mid_value=100.0,
        freshness="fresh",
        warnings=(),
    )
    return _append_source(
        inputs,
        "info_price_v1",
        [
            {
                "Uic": identifier,
                "AssetType": asset_type,
                "Quote": {
                    "Bid": 99.0,
                    "Ask": 101.0,
                    "PriceTypeBid": "Delayed" if delayed else "RealTime",
                },
            }
        ],
        handle=handle,
        normalized=(quote,),
        metadata={
            "data_kind": "quote",
            "entitlement_state": "delayed" if delayed else "available",
            "delayed_by_minutes": delayed,
            "warnings": [],
        },
    )


def _metrics(result: RecipePayload) -> dict[str, float]:
    return {metric.metric_id: metric.value for metric in result.metrics}


def test_risk_keeps_defined_results_when_positive_history_has_no_loss_tail(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    rows = inputs.all_rows()
    inputs._rows[_DATASET] = tuple(  # noqa: SLF001 - control one return path
        row.model_copy(update={"close_value": value})
        for row, value in zip(rows, (100.0, 105.0, 110.0, 120.0), strict=True)
    )
    result = execute_recipe(
        RecipeRequest(
            "instrument_risk",
            (_DATASET,),
            (_HANDLE,),
            {"rolling_window": 2},
        ),
        inputs,
    )
    assert _metrics(result)["maximum_drawdown"] == 0.0
    assert _metrics(result)["historical_var"] == 0.0
    assert "expected_shortfall" in result.unavailable_fields
    assert "sharpe_ratio" in result.unavailable_fields


def _complete_inputs(tmp_path: Path) -> ResearchInputs:
    inputs = _inputs(tmp_path)
    _seed_reference_quote(inputs)
    _seed_reference_quote(inputs, bond=True)
    other_rows = tuple(
        row.model_copy(update={"instrument_handle": _OTHER_HANDLE, "close_value": value})
        for row, value in zip(
            inputs.dataset_rows(inputs.materials[0]), (200.0, 190.0, 220.0, 210.0), strict=True
        )
    )
    _append_source(
        inputs,
        "chart_v3",
        [],
        handle=_OTHER_HANDLE,
        normalized=other_rows,
        metadata={
            "data_kind": "price_bars",
            "missing_interval_count": 0,
            "warnings": [],
        },
    )
    _append_source(
        inputs,
        "reference_instrument_details_v1",
        [
            {
                "Uic": 1,
                "AssetType": "Stock",
                "CurrencyCode": "USD",
                "Exchange": {"Name": "Synthetic exchange"},
            }
        ],
    )
    _append_source(
        inputs,
        "costs_v1",
        [
            {
                "Uic": 1,
                "AssetType": "Stock",
                "Amount": 1.0,
                "Price": 100.0,
                "HoldingPeriodInDays": 30,
                "Currency": "USD",
                "Cost": {"Long": {"Currency": "USD", "TotalCost": 3.0}},
            },
            {
                "Uic": 2,
                "AssetType": "Stock",
                "Amount": 2.0,
                "Price": 50.0,
                "HoldingPeriodInDays": 30,
                "Currency": "USD",
                "Cost": {"Long": {"Currency": "USD", "TotalCost": 5.0}},
            },
        ],
        handle=None,
    )
    return inputs


def _table_values(result: RecipePayload, table_id: str) -> list[dict[str, object]]:
    table = next(table for table in result.tables if table.table_id == table_id)
    return [{cell.field: cell.value for cell in row.cells} for row in table.rows]


@pytest.mark.parametrize("kind", sorted(SUPPORTED_KINDS))
def test_every_market_route_produces_observed_or_explicit_model_results(  # noqa: C901, PLR0912, PLR0915 - one catalog gate with route-specific values
    tmp_path: Path, kind: str
) -> None:
    inputs = _complete_inputs(tmp_path)
    comparison_kinds = {
        "market_comparison",
        "multi_instrument_comparison",
        "market_correlation_regime",
        "market_volatility_dispersion",
        "wrapper_comparison",
    }
    handles = (_HANDLE, _OTHER_HANDLE) if kind in comparison_kinds else (_HANDLE,)
    options: dict[str, object] = {
        "risk_free_period_return_assumption": 0.0,
        "indicator_parameters": {
            "moving_average_window": 2,
            "rsi_period": 2,
            "macd_fast_period": 2,
            "macd_slow_period": 3,
            "macd_signal_period": 2,
            "atr_period": 2,
            "bollinger_window": 2,
            "bollinger_width": 2.0,
            "momentum_lookback": 1,
            "periods_per_year": 252.0,
            "level_wing": 1,
        },
    }
    if kind == "fixed_income":
        handles = (_BOND_HANDLE,)
        options["fixed_income_model"] = {
            "cash_flows": ({"years_from_settlement": 1.0, "amount": 110.0},),
            "compounding_frequency": 1,
            "day_count_basis": "ACT/365",
            "quote_price_scale_assumption": 1.0,
            "accrued_interest_assumption": 0.0,
        }
    arguments: dict[str, object] = {"rolling_window": 2, "options": options}
    if kind == "saved_condition_checks":
        arguments["conditions"] = (
            {
                "condition_id": "close_above_110",
                "instrument_handle": _HANDLE,
                "dataset_id": _DATASET,
                "kind": "price_above",
                "threshold": 110.0,
            },
        )
    result = execute_recipe(
        RecipeRequest(
            kind,
            tuple(material.dataset.dataset_id for material in inputs.materials),
            handles,
            arguments,
        ),
        inputs,
    )
    observed = _metrics(result)
    if kind in {"instrument_price_return", "instrument_risk", "instrument_dossier"}:
        assert observed["price_return"] == pytest.approx(0.2)
        assert observed["maximum_drawdown"] == pytest.approx(-0.1)
    elif kind in {"instrument_quote", "market_microstructure"}:
        assert observed["spread"] == 2.0
        assert observed["midpoint_price"] == 100.0
    elif kind == "instrument_resolution":
        assert _table_values(result, "instrument_reference")[0]["symbol"] == "SYN"
        assert _table_values(result, "instrument_reference")[0]["asset_type"] == "Stock"
    elif kind in {"technical_indicators", "instrument_price_volume"}:
        assert observed["moving_average"] == pytest.approx(109.5)
        assert observed["volume"] == 46.0
        assert observed["volume_weighted_price"] == pytest.approx(
            (100 * 10 + 110 * 11 + 99 * 12 + 120 * 13) / 46
        )
    elif kind in comparison_kinds - {"wrapper_comparison"}:
        comparisons = _table_values(result, "market_comparisons")
        assert comparisons[0]["price_return"] == pytest.approx(0.2)
        assert comparisons[1]["price_return"] == pytest.approx(0.05)
        assert _table_values(result, "market_scope")[0]["advancers"] == 1
        assert _table_values(result, "market_scope")[0]["decliners"] == 1
        if kind == "market_volatility_dispersion":
            values = _table_values(result, "volatility_dispersion")[0]
            assert cast("float", values["population_standard_deviation"]) > 0
    elif kind == "wrapper_comparison":
        assert observed["wrapper_cost_difference"] == 2.0
        assert [row["total_cost"] for row in _table_values(result, "wrapper_costs")] == [3.0, 5.0]
    elif kind == "session_cockpit":
        assert _table_values(result, "session_preparation")[0]["latest_price"] == 120.0
        assert _table_values(result, "session_preparation")[0]["spread"] == 2.0
    elif kind == "saved_condition_checks":
        assert _table_values(result, "saved_conditions")[0]["matched"] is True
        assert _table_values(result, "saved_conditions")[0]["observed_value"] == 120.0
    elif kind == "trading_conditions":
        assert _table_values(result, "trading_conditions")[0]["currency"] == "USD"
        assert observed["total_cost"] == 3.0
    elif kind == "fixed_income":
        assert observed["yield_to_maturity"] == pytest.approx(0.1)
        assert observed["modified_duration"] == pytest.approx(1.0 / 1.1)
        assert all(metric.metric_class is MetricClass.MODEL_OUTPUT for metric in result.metrics)
        assert "observed_bond_cashflows" in result.unavailable_fields
    else:
        pytest.fail(f"Route lacks a substantive numerical or source assertion: {kind}")


@pytest.mark.parametrize("kind", ["instrument_quote", "market_microstructure"])
def test_quote_delay_and_depth_limits_remain_visible(tmp_path: Path, kind: str) -> None:
    inputs = _inputs(tmp_path)
    _seed_reference_quote(inputs, delayed=15)
    result = execute_recipe(
        RecipeRequest(
            kind,
            tuple(material.dataset.dataset_id for material in inputs.materials),
            (_HANDLE,),
        ),
        inputs,
    )
    assert _metrics(result)["spread"] == 2.0
    assert _metrics(result)["quote_delay"] == 15.0
    assert "quote_delayed" in result.warnings
    if kind == "market_microstructure":
        assert {"market_depth", "depth_imbalance", "liquidity_score"} <= set(
            result.unavailable_fields
        )
        assert "depth_imbalance" not in _metrics(result)


def test_quote_requires_the_matching_authenticated_source(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    with pytest.raises(AnalyticsExecutionError, match="quote_missing_or_ambiguous"):
        execute_recipe(RecipeRequest("instrument_quote", (_DATASET,), (_HANDLE,)), inputs)


def test_unadjusted_series_cannot_become_total_return(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    with pytest.raises(AnalyticsExecutionError, match="return_series_unavailable"):
        execute_recipe(
            RecipeRequest(
                "instrument_price_return",
                (_DATASET,),
                (_HANDLE,),
                {"rolling_window": 2, "requested_return": "total_return"},
            ),
            inputs,
        )


def test_optional_volume_missingness_does_not_erase_core_indicators(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs._rows[_DATASET] = tuple(  # noqa: SLF001 - control source missingness
        row.model_copy(update={"volume_value": None}) for row in inputs.all_rows()
    )
    result = execute_recipe(
        RecipeRequest(
            "technical_indicators",
            (_DATASET,),
            (_HANDLE,),
            {
                "options": {
                    "indicator_parameters": {
                        "moving_average_window": 2,
                        "rsi_period": 2,
                        "macd_fast_period": 2,
                        "macd_slow_period": 3,
                        "macd_signal_period": 2,
                        "atr_period": 2,
                        "bollinger_window": 2,
                        "bollinger_width": 2.0,
                        "momentum_lookback": 1,
                        "periods_per_year": 252.0,
                        "level_wing": 1,
                    },
                }
            },
        ),
        inputs,
    )
    assert _metrics(result)["moving_average"] == 109.5
    assert {"volume", "volume_weighted_price"} <= set(result.unavailable_fields)
    assert "volume" not in _metrics(result)


def test_partial_price_metadata_survives_into_the_recipe(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    original = inputs.materials[0]
    page = replace(
        original.pages[0],
        payload={
            "rows": [],
            "sync_metadata": {
                "data_kind": "price_bars",
                "missing_interval_count": 1,
                "warnings": ["observed_interval_gap"],
            },
        },
    )
    inputs.materials = (
        replace(
            original,
            dataset=replace(
                original.dataset,
                quality_state=QualityState.PARTIAL,
            ),
            pages=(page,),
        ),
    )
    result = execute_recipe(
        RecipeRequest(
            "instrument_price_return",
            (_DATASET,),
            (_HANDLE,),
            {"rolling_window": 2},
        ),
        inputs,
    )
    assert _metrics(result)["price_return"] == pytest.approx(0.2)
    assert "observed_interval_gap" in result.warnings
    assert "incomplete_price_coverage" in result.warnings


def test_correlation_table_aligns_both_period_endpoints(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    other = tuple(
        row.model_copy(
            update={
                "instrument_handle": _OTHER_HANDLE,
                "bar_time": row.bar_time + timedelta(days=1),
            }
        )
        for row in inputs.all_rows()
        if isinstance(row, PriceBarDatasetRow)
    )
    _append_source(
        inputs,
        "chart_v3",
        [],
        handle=_OTHER_HANDLE,
        normalized=other,
        metadata={"data_kind": "price_bars", "missing_interval_count": 0, "warnings": []},
    )
    result = execute_recipe(
        RecipeRequest(
            "market_correlation_regime",
            tuple(material.dataset.dataset_id for material in inputs.materials),
            (_HANDLE, _OTHER_HANDLE),
        ),
        inputs,
    )
    pair = _table_values(result, "market_correlations")[0]
    assert pair["observation_count"] == 2
    assert pair["correlation"] == pytest.approx(-1.0)


def test_wrapper_costs_refuse_mismatched_exposure_basis(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    _append_source(
        inputs,
        "costs_v1",
        [
            {
                "Uic": 1,
                "AssetType": "Stock",
                "Amount": 1.0,
                "Price": 100.0,
                "HoldingPeriodInDays": 30,
                "Currency": "USD",
                "Cost": {"TotalCost": 3.0},
            },
            {
                "Uic": 2,
                "AssetType": "Stock",
                "Amount": 1.0,
                "Price": 50.0,
                "HoldingPeriodInDays": 30,
                "Currency": "USD",
                "Cost": {"TotalCost": 5.0},
            },
        ],
        handle=None,
    )
    with pytest.raises(AnalyticsExecutionError, match="wrapper_basis_mismatch"):
        execute_recipe(
            RecipeRequest("wrapper_comparison", (_DATASET,), (_HANDLE, _OTHER_HANDLE)), inputs
        )


def test_model_options_cannot_supply_quote_or_bond_source_facts(tmp_path: Path) -> None:
    with pytest.raises(AnalyticsExecutionError, match="market_options_invalid"):
        execute_recipe(
            RecipeRequest(
                "instrument_quote",
                (_DATASET,),
                (_HANDLE,),
                {
                    "options": {"bid": 99.0, "ask": 101.0, "observed_coupon": 5.0},
                },
            ),
            _inputs(tmp_path),
        )


def test_fixed_income_refuses_without_an_explicit_cashflow_model(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    _seed_reference_quote(inputs, bond=True)
    with pytest.raises(AnalyticsExecutionError, match="fixed_income_cashflow_model_required"):
        execute_recipe(RecipeRequest("fixed_income", (_DATASET,), (_BOND_HANDLE,)), inputs)


@pytest.mark.parametrize("kind", ["instrument_quote", "market_microstructure"])
def test_normal_quote_capture_resolves_and_pins_instrument_currency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = analytics_tools._analytics_config()  # noqa: SLF001 - ordinary server configuration
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "Identifier": 1,
                        "AssetType": "Stock",
                        "Description": "Synthetic EUR instrument",
                        "CurrencyCode": "EUR",
                        "Symbol": "FIX",
                        "ExchangeId": "XPAR",
                    }
                ]
            },
            {
                "AssetType": "Stock",
                "Uic": 1,
                "PriceTypeAsk": "RealTime",
                "PriceTypeBid": "RealTime",
                "Quote": {
                    "Ask": 102.0,
                    "Bid": 100.0,
                    "Mid": 101.0,
                    "DelayedByMinutes": 0,
                    "PriceType": "RealTime",
                },
            },
        )
    )
    _checked_release(monkeypatch)

    async def call() -> AnalysisResult:
        provider = SaxoAnalyticsProvider(request_executor=executor)
        resolved = await InstrumentResolver(provider, config).resolve_instruments("FIX", (), ())
        handle = resolved.matches[0].instrument_handle
        captured = await capture_quote(handle, provider=provider, config=config)
        dataset_id = captured.datasets[0].dataset_id
        request: dict[str, object] = {"analysis_kind": kind, "dataset_ids": [dataset_id]}
        tool = "saxo_analyze_market"
        if kind == "instrument_quote":
            request["instrument_handles"] = [handle]
            tool = "saxo_analyze_instruments"
        async with Client(create_mcp_server()) as client:
            response = await client.call_tool(tool, {"request": request})
        assert not response.is_error
        assert response.structured_content is not None
        body = response.structured_content
        assert body["status"] in {"verified", "degraded"}, body
        return AnalysisResult.model_validate_json(json.dumps(body["result"]))

    result = asyncio.run(call())
    metrics = {metric.metric_id: metric for metric in result.metrics}
    assert metrics["midpoint_price"].value == 101.0
    assert metrics["spread"].value == 2.0
    assert metrics["midpoint_price"].currency == metrics["spread"].currency == "EUR"
    assert "quote_currency" not in result.unavailable_fields
    assert (
        replay_analysis(
            result.analysis_id, config=config, registry=load_production_registry(config)
        )
        == result
    )
    store = AnalyticsStore.open(config)
    try:
        bound = store.get_authenticated_dataset_material(result.provenance.dataset_id)
        assert {page.contract_name for page in bound.pages} == {
            "info_price_v1",
            "reference_instruments_v1",
        }
    finally:
        store.close()
    assert len(executor.calls) == 2


def test_quote_currency_is_not_inferred_from_account_reporting_currency(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    _seed_reference_quote(inputs)
    references = {
        page.page_id: replace(page, payload={"rows": [{"Identifier": 1, "AssetType": "Stock"}]})
        if page.contract_name == "reference_instruments_v1"
        else page
        for page in inputs.pages
    }
    inputs.materials = tuple(
        replace(material, pages=tuple(references[page.page_id] for page in material.pages))
        for material in inputs.materials
    )
    cast("_ReferenceStore", inputs.store).pages = tuple(references.values())
    _append_source(inputs, "balances_v1", [{"Currency": "USD"}], handle=None)
    result = execute_recipe(RecipeRequest("instrument_quote", (_DATASET,), (_HANDLE,)), inputs)
    assert "quote_currency" in result.unavailable_fields
    assert all(metric.currency is None for metric in result.metrics)
