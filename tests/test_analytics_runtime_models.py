from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import AnalysisResult, InstrumentAnalysisRequest, QualityState
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipeRequest,
    ResearchInputs,
)
from saxo_bank_mcp.analytics_runtime_models import METRIC_IDS, REQUIRED_METRICS, execute_recipe
from saxo_bank_mcp.analytics_source_contracts import (
    SourceJsonValue,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    AuthenticatedDatasetMaterial,
    AuthenticatedSourceMaterial,
    StoredDataset,
)
from saxo_bank_mcp.analytics_sync import (
    DatasetRow,
    OptionReferenceDatasetRow,
    PriceBarDatasetRow,
    QuoteDatasetRow,
)

_AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
_DATASET = "ds_00000000000040008000000000000001"
_ACCOUNT = "aa_00000000000040008000000000000001"
_HANDLE = instrument_handle_for_saxo_identity("Stock", 1)
_SECOND_HANDLE = instrument_handle_for_saxo_identity("Stock", 2)
_OPTION_HANDLE = instrument_handle_for_saxo_identity("StockOption", 10)
_ANALYSIS = "an_00000000000040008000000000000001"
_EXPECTED_SIZING = 10.0
_EXPECTED_RISK = 100.0
_STARTING_VALUE = 1000.0
_EXPECTED_SURFACE_ROWS = 3
_CANCEL_AFTER_CHECKS = 5
_START = _AS_OF - timedelta(days=100)


def _inputs(
    tmp_path: Path,
    sources: dict[str, list[dict[str, object]]],
    rows: tuple[DatasetRow, ...] | None = None,
) -> ResearchInputs:
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path)})
    contracts = source_contracts_by_id()
    pages = tuple(
        (
            AuthenticatedSourceMaterial(
                page_id=f"sp_{index:032x}",
                contract_name=contract_id,
                contract_sha256=source_contract_fingerprint(contracts[contract_id]),
                source_kind="market",
                source_revision="fixture:models",
                source_timestamp=_AS_OF,
                fingerprint_sha256="1" * 64,
                account_scope=_ACCOUNT,
                instrument_handle=None,
                payload=cast(
                    "Mapping[str, SourceJsonValue]",
                    {
                        "rows": rows,
                        "sync_metadata": {
                            "entitlement_state": "available",
                            "delayed_by_minutes": 0,
                        },
                    },
                ),
            )
            for index, (contract_id, rows) in enumerate(sources.items(), 1)
        )
    )
    material = AuthenticatedDatasetMaterial(
        dataset=StoredDataset(
            _DATASET, "fixture:models", "2" * 64, 1, 1, _AS_OF, QualityState.COMPLETE
        ),
        account_scope=_ACCOUNT,
        coverage_start=_AS_OF,
        coverage_end=_AS_OF,
        pages=pages,
    )
    return _FixtureInputs(
        config,
        cast("AnalyticsStore", _AnalysisStore()),
        (material,),
        _rows={
            _DATASET: rows
            or (
                QuoteDatasetRow(
                    instrument_handle=_HANDLE,
                    captured_at=_AS_OF,
                    bid_value=99.0,
                    ask_value=101.0,
                    mid_value=100.0,
                    freshness="fresh",
                    warnings=(),
                ),
            )
        },
    )


def _sizing_sources() -> dict[str, list[dict[str, object]]]:
    return {
        "balances_v1": [
            {
                "Currency": "USD",
                "TotalValue": 10000.0,
                "SpendingPower": 5000.0,
                "MarginAvailableForTrading": 5000.0,
                "MarginUsedByCurrentPositions": 0.0,
            }
        ],
        "positions_v1": [
            {
                "PositionBase": {"Uic": 1, "AssetType": "Stock", "Amount": 10.0},
                "PositionView": {
                    "ExposureInBaseCurrency": 1000.0,
                    "ExposureCurrency": "USD",
                    "Exposure": 1000.0,
                },
            }
        ],
        "costs_v1": [{"Uic": 1, "AssetType": "Stock", "Cost": {"TotalCost": 1.0}}],
        "reference_instrument_details_v1": [
            {
                "Uic": 1,
                "AssetType": "Stock",
                "CurrencyCode": "USD",
                "ContractSize": 1.0,
                "LotSize": 1.0,
            }
        ],
        "info_price_v1": [{"Uic": 1, "AssetType": "Stock", "Quote": {"Mid": 100.0}}],
    }


def test_missing_source_inputs_never_become_an_option_model() -> None:
    request = RecipeRequest("derivatives_model", ("ds_00000000000040008000000000000001",))
    with pytest.raises(AnalyticsExecutionError):
        execute_recipe(request, None)


def test_sizing_enforces_risk_budget_and_delivers_all_limits(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, _sizing_sources())
    request = RecipeRequest(
        "position_sizing",
        (_DATASET,),
        (_HANDLE,),
        {
            "method": "stop_distance",
            "maximum_loss": 100.0,
            "risk_budget_confirmed": True,
            "stop_price": 90.0,
            "options": {
                "sizing": {
                    "maximum_weight": 0.5,
                    "reserved_buffer": 0.0,
                    "margin_requirement_rate": 1.0,
                }
            },
        },
    )
    result = execute_recipe(request, inputs)
    assert {metric.metric_id: metric.value for metric in result.metrics}[
        "position_size"
    ] == _EXPECTED_SIZING
    assert any(
        cell.field == "value" and cell.value == _EXPECTED_RISK
        for row in result.tables[0].rows
        for cell in row.cells
    )


class _FixtureInputs(ResearchInputs):
    def analysis(self, analysis_id: str) -> AnalysisResult:
        return cast(
            "AnalysisResult", _AnalysisStore().get_authenticated_analysis_result(analysis_id)
        )


class _AnalysisStore:
    def get_authenticated_analysis_result(self, identifier: str) -> SimpleNamespace:
        assert identifier == _ANALYSIS
        return SimpleNamespace(
            account_scope=_ACCOUNT,
            as_of=_AS_OF,
            request=InstrumentAnalysisRequest.model_validate(
                {
                    "request_kind": "instrument",
                    "analysis_kind": "derivatives_model",
                    "dataset_id": _DATASET,
                    "instrument_handles": (_OPTION_HANDLE,),
                    "parameters": {
                        "start_at": _AS_OF,
                        "end_at": _AS_OF,
                        "as_of": _AS_OF,
                        "benchmark_handle": None,
                        "benchmark_fingerprint_sha256": None,
                        "fx_method": "not_applicable",
                        "fx_source": "not_applicable",
                        "fx_timestamp": None,
                        "calendar": "calendar_days",
                        "reporting_currency": "USD",
                        "metric_currency_bindings": (),
                        "model_parameters": (),
                    },
                },
                strict=False,
            ),
            metrics=(),
        )


def _quote(handle: str, value: float) -> QuoteDatasetRow:
    return QuoteDatasetRow(
        instrument_handle=handle,
        captured_at=_AS_OF,
        bid_value=value,
        ask_value=value,
        mid_value=value,
        freshness="fresh",
        warnings=(),
    )


def _bars(handle: str, prices: tuple[float, ...]) -> tuple[PriceBarDatasetRow, ...]:
    return tuple(
        (
            PriceBarDatasetRow(
                instrument_handle=handle,
                bar_time=_START + timedelta(days=index),
                interval=ChartInterval.ONE_DAY,
                open_value=price,
                high_value=price,
                low_value=price,
                close_value=price,
                volume_value=100.0,
                adjusted=False,
            )
            for index, price in enumerate(prices)
        )
    )


def _option_choices() -> dict[str, object]:
    return {
        "pricing_model": "black_scholes",
        "exercise_style": "european",
        "payoff_style": "vanilla",
        "risk_free_rate": 0.04,
        "dividend_yield": 0.0,
        "volatility": 0.2,
        "days_per_year": 365.0,
        "expiry_time_utc": time(16),
    }


def _option_fixture() -> tuple[dict[str, list[dict[str, object]]], tuple[DatasetRow, ...]]:
    sources = _sizing_sources()
    sources["options_chain_reference_v1"] = [{"Uic": 10, "AssetType": "StockOption"}]
    sources["reference_instrument_details_v1"].extend(
        [
            {
                "Uic": 10,
                "AssetType": "StockOption",
                "CurrencyCode": "USD",
                "ContractSize": 100.0,
                "ExerciseStyle": "European",
                "PayoffStyle": "Vanilla",
            }
        ]
    )
    sources["info_price_v1"].extend(
        [{"Uic": 10, "AssetType": "StockOption", "Quote": {"Mid": 6.0}}]
    )
    return (
        sources,
        (
            _quote(_HANDLE, 100.0),
            _quote(_OPTION_HANDLE, 6.0),
            OptionReferenceDatasetRow(
                instrument_handle=_OPTION_HANDLE,
                underlying_handle=_HANDLE,
                captured_at=_AS_OF,
                expiry=date(2027, 1, 1),
                strike_value=100.0,
                currency="USD",
                put_call="call",
            ),
        ),
    )


@pytest.mark.parametrize(
    "kind", ["derivatives_model", "option_greeks", "option_chain", "option_payoff"]
)
def test_option_family_computes_values_and_signed_strategy_payoff(
    tmp_path: Path, kind: str
) -> None:
    sources, rows = _option_fixture()
    choices = _option_choices()
    choices.update(
        {
            "legs": ({"instrument_handle": _OPTION_HANDLE, "quantity": -2.0},),
            "net_premium": -1200.0,
            "eligible_costs": 2.0,
            "expiry_reference_prices": (120.0, 100.0),
        }
    )
    result = execute_recipe(
        RecipeRequest(kind, (_DATASET,), (_OPTION_HANDLE,), {"options": {"option": choices}}),
        _inputs(tmp_path, sources, rows),
    )
    metrics = {metric.metric_id: metric.value for metric in result.metrics}
    assert set(REQUIRED_METRICS[kind]) <= metrics.keys()
    assert set(metrics) <= set(METRIC_IDS[kind])
    assert result.tables
    if kind == "option_payoff":
        assert metrics["payoff_at_expiry"] == pytest.approx(-2802.0)
        assert metrics["aggregate_delta"] < 0
    else:
        value_id = "implied_volatility" if kind == "option_chain" else "delta"
        assert metrics[value_id] > 0


@pytest.mark.parametrize(
    "kind", ["derivatives_model", "option_greeks", "option_chain", "option_payoff"]
)
def test_option_family_refuses_missing_explicit_exercise_style(tmp_path: Path, kind: str) -> None:
    sources, rows = _option_fixture()
    choices = _option_choices()
    del choices["exercise_style"]
    choices["legs"] = ({"instrument_handle": _OPTION_HANDLE, "quantity": 1.0},)
    with pytest.raises(AnalyticsExecutionError, match="model_options_invalid"):
        execute_recipe(
            RecipeRequest(kind, (_DATASET,), (_OPTION_HANDLE,), {"options": {"option": choices}}),
            _inputs(tmp_path, sources, rows),
        )


def _optimization_fixture() -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], dict[str, object]
]:
    sources = _sizing_sources()
    sources["chart_v3"] = [{"DataVersion": 1}]
    sources["exposure_instruments_v1"] = [{"Uic": 1, "AssetType": "Stock"}]
    sources["positions_v1"][0]["PositionView"] = {
        "ExposureInBaseCurrency": 5000.0,
        "ExposureCurrency": "USD",
        "Exposure": 5000.0,
    }
    sources["positions_v1"].extend(
        [
            {
                "PositionBase": {"Uic": 2, "AssetType": "Stock", "Amount": 20.0},
                "PositionView": {
                    "ExposureInBaseCurrency": 5000.0,
                    "ExposureCurrency": "USD",
                    "Exposure": 5000.0,
                },
            }
        ]
    )
    sources["reference_instrument_details_v1"].extend(
        [
            {
                "Uic": 2,
                "AssetType": "Stock",
                "CurrencyCode": "USD",
                "ContractSize": 1.0,
                "LotSize": 1.0,
            }
        ]
    )
    sources["costs_v1"].extend([{"Uic": 2, "AssetType": "Stock", "Cost": {"TotalCost": 1.0}}])
    for cost in sources["costs_v1"]:
        cost.update({"Amount": 10.0, "Price": 100.0})
    first = tuple(100.0 + index + index % 3 for index in range(40))
    second = tuple(100.0 + index * 0.5 + index % 5 for index in range(40))
    rows = (*_bars(_HANDLE, first), *_bars(_SECOND_HANDLE, second))
    arguments: dict[str, object] = {
        "objective_confirmed_by_caller": True,
        "constraints_confirmed_by_caller": True,
        "short_policy": "long_only",
        "maximum_turnover": 2.0,
        "maximum_transaction_cost_ratio": 0.1,
        "maximum_margin_ratio": 1.0,
        "options": {
            "optimization": {
                "assets": tuple(
                    {
                        "instrument_handle": handle,
                        "lower_bound": 0.0,
                        "upper_bound": 1.0,
                        "minimum_trade_weight": 0.0,
                        "excluded": False,
                        "margin_requirement_rate": 0.0,
                    }
                    for handle in (_HANDLE, _SECOND_HANDLE)
                ),
                "covariance_perturbation_ratios": (0.01,),
            }
        },
    }
    return (sources, rows, arguments)


@pytest.mark.parametrize("kind", ["portfolio_minimum_variance", "portfolio_risk_parity"])
def test_optimization_solves_explicit_complete_portfolio(tmp_path: Path, kind: str) -> None:
    sources, rows, arguments = _optimization_fixture()
    result = execute_recipe(
        RecipeRequest(kind, (_DATASET,), (_HANDLE, _SECOND_HANDLE), arguments),
        _inputs(tmp_path, sources, rows),
    )
    metrics = {metric.metric_id: metric.value for metric in result.metrics}
    assert set(REQUIRED_METRICS[kind]) <= metrics.keys()
    target_cells = [
        cell for row in result.tables[0].rows for cell in row.cells if cell.field == "target_weight"
    ]
    assert sum(cast("float", cell.value) for cell in target_cells) == pytest.approx(1.0)
    assert all(cast("float", cell.value) >= 0 for cell in target_cells)
    assert (
        next(
            metric for metric in result.metrics if metric.metric_id == "estimated_transaction_cost"
        ).currency
        == "USD"
    )


@pytest.mark.parametrize("kind", ["portfolio_minimum_variance", "portfolio_risk_parity"])
def test_optimization_refuses_unconfirmed_constraints(tmp_path: Path, kind: str) -> None:
    sources, rows, arguments = _optimization_fixture()
    arguments["constraints_confirmed_by_caller"] = False
    with pytest.raises(AnalyticsExecutionError, match="optimizer_caller_selection_required"):
        execute_recipe(
            RecipeRequest(kind, (_DATASET,), (_HANDLE, _SECOND_HANDLE), arguments),
            _inputs(tmp_path, sources, rows),
        )


_SCENARIOS = (
    "margin_fire_drill",
    "portfolio_scenario",
    "scenario_historical",
    "scenario_custom",
    "scenario_currency",
    "scenario_volatility",
    "scenario_rate",
    "scenario_margin",
    "scenario_combined",
    "derivatives_scenario",
)


def _scenario_fixture(
    kind: str,
) -> tuple[dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], dict[str, object], str]:
    sources, option_rows = _option_fixture()
    sources["exposure_instruments_v1"] = [{"Uic": 1, "AssetType": "Stock"}]
    sources["chart_v3"] = [{"DataVersion": 1}]
    handle = _HANDLE
    rows = (*option_rows, *_bars(_HANDLE, (100.0, 90.0)))
    shock: dict[str, object] = {"instrument_handle": handle, "price_shock_ratio": -0.1}
    if kind in {"scenario_volatility", "scenario_rate", "derivatives_scenario"}:
        handle = _OPTION_HANDLE
        sources["positions_v1"] = [
            {
                "PositionBase": {"Uic": 10, "AssetType": "StockOption", "Amount": 1.0},
                "PositionView": {
                    "ExposureInBaseCurrency": 600.0,
                    "ExposureCurrency": "USD",
                    "MarketValueInBaseCurrency": 600.0,
                    "ConversionRateCurrent": 1.0,
                },
            }
        ]
        shock = {
            "instrument_handle": handle,
            "volatility_shock_points": 2.0 if kind != "scenario_rate" else 0.0,
            "rate_shock_basis_points": 100.0 if kind != "scenario_volatility" else 0.0,
            "model_analysis_id": _ANALYSIS,
        }
    scenario: dict[str, object] = {"shocks": (shock,)}
    if kind == "scenario_currency":
        shock["price_shock_ratio"] = 0.0
        scenario["currency_shocks"] = ({"currency": "USD", "shock_ratio": -0.1},)
    if kind in {"margin_fire_drill", "scenario_margin"}:
        sources["balances_v1"][0]["MarginUsedByCurrentPositions"] = 100.0
        shock.update({"price_shock_ratio": 0.0, "margin_multiplier": 2.0})
        scenario["margin_allocation"] = "proportional_gross_exposure"
    if kind == "scenario_historical":
        scenario.update(
            {"historical_start_at": _START, "historical_end_at": _START + timedelta(days=1)}
        )
    return (sources, rows, {"options": {"scenario": scenario, "option": _option_choices()}}, handle)


@pytest.mark.parametrize("kind", _SCENARIOS)
def test_all_scenario_types_compute_bound_shocks(tmp_path: Path, kind: str) -> None:
    sources, rows, arguments, handle = _scenario_fixture(kind)
    result = execute_recipe(
        RecipeRequest(kind, (_DATASET,), (handle,), arguments), _inputs(tmp_path, sources, rows)
    )
    assert {metric.metric_id for metric in result.metrics} == set(REQUIRED_METRICS[kind])
    assert result.tables[0].rows
    assert result.is_not_forecast
    if kind in {"scenario_historical", "scenario_custom", "scenario_currency"}:
        assert result.metrics[0].value == pytest.approx(-100.0)


@pytest.mark.parametrize("kind", _SCENARIOS)
def test_every_scenario_type_refuses_incomplete_shock_map(tmp_path: Path, kind: str) -> None:
    sources, rows, arguments, handle = _scenario_fixture(kind)
    options = cast("dict[str, object]", arguments["options"])
    scenario = cast("dict[str, object]", options["scenario"])
    shock = cast("tuple[dict[str, object], ...]", scenario["shocks"])[0]
    shock["instrument_handle"] = _SECOND_HANDLE
    with pytest.raises(AnalyticsExecutionError, match="scenario_shock_map_incomplete"):
        execute_recipe(
            RecipeRequest(kind, (_DATASET,), (handle,), arguments), _inputs(tmp_path, sources, rows)
        )


def _simulation_fixture() -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    sources = _sizing_sources()
    sources["performance_timeseries_v4"] = [
        {
            "TimeWeighted": {
                "Accumulated": [
                    {"Date": (_START + timedelta(days=index)).isoformat(), "Value": float(index)}
                    for index in range(40)
                ]
            }
        }
    ]
    arguments: dict[str, object] = {
        "options": {
            "simulation": {
                "random_seed": 17,
                "path_count": 100,
                "block_length": 2,
                "horizon_periods": 2,
                "horizon_years": 1.0,
                "starting_value": 1000.0,
                "explicit_goal": 1000.0,
                "explicit_ruin_threshold": 1.0,
                "cash_flows": (
                    {"period_index": 1, "contribution": Decimal(0), "withdrawal": Decimal(0)},
                    {"period_index": 2, "contribution": Decimal(0), "withdrawal": Decimal(0)},
                ),
                "annual_inflation_assumption": 0.02,
            }
        }
    }
    return (sources, arguments)


@pytest.mark.parametrize("kind", ["monte_carlo", "goal_model"])
def test_simulation_calibration_and_seed_are_reproducible(tmp_path: Path, kind: str) -> None:
    sources, arguments = _simulation_fixture()
    inputs = _inputs(tmp_path, sources)
    request = RecipeRequest(kind, (_DATASET,), (), arguments)
    result = execute_recipe(request, inputs)
    assert result == execute_recipe(request, inputs)
    assert result.model_distribution_only
    assert result.is_not_forecast
    values = {metric.metric_id: metric.value for metric in result.metrics}
    assert values["goal_probability"] == 1.0
    assert values["ruin_probability"] == 0.0
    assert values["sensitivity_upper"] > values["sensitivity_lower"] > _STARTING_VALUE


@pytest.mark.parametrize("kind", ["monte_carlo", "goal_model"])
def test_simulation_refuses_missing_cashflow_period(tmp_path: Path, kind: str) -> None:
    sources, arguments = _simulation_fixture()
    simulation = cast(
        "dict[str, object]", cast("dict[str, object]", arguments["options"])["simulation"]
    )
    simulation["cash_flows"] = (
        {"period_index": 1, "contribution": Decimal(0), "withdrawal": Decimal(0)},
    )
    with pytest.raises(AnalyticsExecutionError, match="model_inputs_invalid"):
        execute_recipe(RecipeRequest(kind, (_DATASET,), (), arguments), _inputs(tmp_path, sources))


def _futures_fixture() -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], dict[str, object]
]:
    near = instrument_handle_for_saxo_identity("FuturesContract", 20)
    later = instrument_handle_for_saxo_identity("FuturesContract", 21)
    sources = _sizing_sources()
    sources["reference_instruments_v1"] = [
        {"Uic": 20, "AssetType": "FuturesContract"},
        {"Uic": 21, "AssetType": "FuturesContract"},
    ]
    sources["reference_instrument_details_v1"].extend(
        [
            {
                "Uic": identifier,
                "AssetType": "FuturesContract",
                "CurrencyCode": "USD",
                "ContractSize": 10.0,
                "ExpiryDate": expiry,
            }
            for identifier, expiry in ((20, "2027-01-01"), (21, "2027-04-01"))
        ]
    )
    sources["info_price_v1"].extend(
        [
            {"Uic": identifier, "AssetType": "FuturesContract", "Quote": {"Mid": price}}
            for identifier, price in ((20, 102.0), (21, 104.0))
        ]
    )
    rows = (_quote(_HANDLE, 100.0), _quote(near, 102.0), _quote(later, 104.0))
    arguments: dict[str, object] = {
        "options": {
            "futures": {
                "spot_handle": _HANDLE,
                "near_handle": near,
                "later_handle": later,
                "quantity": 2.0,
                "eligible_roll_costs": 1.0,
                "days_per_year": 365.0,
                "expiry_time_utc": time(16),
            }
        }
    }
    return (sources, rows, arguments)


def test_futures_curve_has_exact_quantity_multiplier_roll(tmp_path: Path) -> None:
    sources, rows, arguments = _futures_fixture()
    result = execute_recipe(
        RecipeRequest("futures_curve", (_DATASET,), (), arguments), _inputs(tmp_path, sources, rows)
    )
    values = {metric.metric_id: metric.value for metric in result.metrics}
    assert values["futures_basis"] == pytest.approx(2.0)
    assert abs(values["futures_roll"]) == pytest.approx(41.0)


def test_futures_curve_refuses_missing_multiplier(tmp_path: Path) -> None:
    sources, rows, arguments = _futures_fixture()
    del sources["reference_instrument_details_v1"][1]["ContractSize"]
    with pytest.raises(AnalyticsExecutionError, match="futures_contract_basis_mismatch"):
        execute_recipe(
            RecipeRequest("futures_curve", (_DATASET,), (), arguments),
            _inputs(tmp_path, sources, rows),
        )


def _fx_fixture() -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], dict[str, object], str
]:
    handle = instrument_handle_for_saxo_identity("FxSpot", 30)
    sources = _sizing_sources()
    sources["reference_instruments_v1"] = [{"Uic": 30, "AssetType": "FxSpot"}]
    sources["info_price_v1"] = [
        {
            "Uic": 30,
            "AssetType": "FxSpot",
            "DisplayAndFormat": {"Symbol": "EURUSD"},
            "Quote": {"Mid": 1.1},
        }
    ]
    arguments: dict[str, object] = {
        "options": {
            "fx": {
                "base_currency": "EUR",
                "quote_currency": "USD",
                "base_rate": 0.02,
                "quote_rate": 0.04,
                "forward_date": _AS_OF + timedelta(days=365),
                "days_per_year": 365.0,
            }
        }
    }
    return (sources, (_quote(handle, 1.1),), arguments, handle)


def test_fx_forward_uses_explicit_pair_rates_and_horizon(tmp_path: Path) -> None:
    sources, rows, arguments, handle = _fx_fixture()
    result = execute_recipe(
        RecipeRequest("fx_forward_carry", (_DATASET,), (handle,), arguments),
        _inputs(tmp_path, sources, rows),
    )
    values = {metric.metric_id: metric.value for metric in result.metrics}
    assert values["fx_forward"] == pytest.approx(1.1 * 1.04 / 1.02)


def test_fx_forward_refuses_reversed_pair(tmp_path: Path) -> None:
    sources, rows, arguments, handle = _fx_fixture()
    sources["info_price_v1"][0]["DisplayAndFormat"] = {"Symbol": "USDEUR"}
    with pytest.raises(AnalyticsExecutionError, match="fx_currency_pair_mismatch"):
        execute_recipe(
            RecipeRequest("fx_forward_carry", (_DATASET,), (handle,), arguments),
            _inputs(tmp_path, sources, rows),
        )


def _backtest_fixture() -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], dict[str, object]
]:
    sources: dict[str, list[dict[str, object]]] = {
        "chart_v3": [{"DataVersion": 1}],
        "reference_instruments_v1": [
            {"Uic": 1, "AssetType": "Stock", "TradingStatus": "Tradable", "CurrencyCode": "USD"}
        ],
    }
    sources["reference_instrument_details_v1"] = [
        {"Uic": 1, "AssetType": "Stock", "TradingStatus": "Tradable", "CurrencyCode": "USD"}
    ]
    rows = _bars(_HANDLE, (10.0, 10.0, 12.0, 13.0, 14.0, 10.0, 9.0, 9.0, 10.0))
    close = {"kind": "close", "window": 1}
    average = {"kind": "simple_moving_average", "window": 2}
    strategy = {
        "entry": {
            "left": close,
            "comparison": "greater_than",
            "right_indicator": average,
            "threshold": None,
        },
        "exit": {
            "left": close,
            "comparison": "less_than",
            "right_indicator": average,
            "threshold": None,
        },
        "direction": "long",
        "sizing": {"kind": "fixed_weight", "target_weight": 0.5},
        "rebalancing": {"kind": "every_n_bars", "interval_bars": 1, "fill_timing": "next_bar_open"},
        "constraints": {
            "allow_long": True,
            "allow_short": False,
            "maximum_absolute_position_weight": 0.5,
            "maximum_gross_exposure": 1.0,
            "minimum_cash_weight": 0.0,
        },
        "transaction_costs": {
            "commission_basis_points": 10.0,
            "fixed_cost_per_fill": 1.0,
            "currency": "USD",
        },
        "slippage": {"kind": "fixed_basis_points", "basis_points": 5.0},
        "evaluation_split": {
            "kind": "holdout",
            "train_end_at": _START + timedelta(days=4),
            "holdout_start_at": _START + timedelta(days=5),
        },
        "missing_bar_policy": "refuse",
        "delisting_policy": "terminal_close",
    }
    return (
        sources,
        rows,
        {"options": {"backtest": {"strategy": strategy, "starting_equity": 1000.0}}},
    )


def test_backtest_retains_fill_equity_and_split_evidence_without_ghost_claim(
    tmp_path: Path,
) -> None:
    sources, rows, arguments = _backtest_fixture()
    result = execute_recipe(
        RecipeRequest("bounded_backtest", (_DATASET,), (_HANDLE,), arguments),
        _inputs(tmp_path, sources, rows),
    )
    assert set(REQUIRED_METRICS["bounded_backtest"]) <= {
        metric.metric_id for metric in result.metrics
    }
    assert result.tables[1].rows
    assert result.tables[2].rows
    assert "broker_execution_validation_not_run" in result.warnings
    assert "equivalent_broker_execution_validation" in result.unavailable_fields


def test_backtest_refuses_forming_bar(tmp_path: Path) -> None:
    sources, rows, arguments = _backtest_fixture()
    rows = (*rows, rows[-1].model_copy(update={"bar_time": _AS_OF}))
    with pytest.raises(AnalyticsExecutionError, match="backtest_incomplete_bar"):
        execute_recipe(
            RecipeRequest("bounded_backtest", (_DATASET,), (_HANDLE,), arguments),
            _inputs(tmp_path, sources, rows),
        )


def _pretrade_fixture() -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    sources = _sizing_sources()
    sources["info_price_v1"][0]["Quote"] = {"Mid": 100.0, "PriceTypeBid": "Tradable"}
    sources["costs_v1"][0].update({"Amount": 1.0, "Price": 100.0})
    costs = {
        field: Decimal(0)
        for field in ("spread", "fx_conversion", "financing", "borrow", "custody", "tax")
    }
    costs.update({"commission": Decimal(1), "total_cost": Decimal(1), "turnover": Decimal(100)})
    return (
        sources,
        {
            "options": {
                "pretrade": {
                    "origin_analysis_id": _ANALYSIS,
                    "side": "buy",
                    "quantity": 1.0,
                    "holding_period_days": 0,
                    "margin_requirement_rate": 1.0,
                    "modeled_cost_components": costs,
                }
            }
        },
    )


def test_pretrade_impact_uses_source_total_cost_and_current_exposure(tmp_path: Path) -> None:
    sources, arguments = _pretrade_fixture()
    result = execute_recipe(
        RecipeRequest("pretrade_impact", (_DATASET,), (_HANDLE,), arguments),
        _inputs(tmp_path, sources, (_quote(_HANDLE, 100.0),)),
    )
    values = {metric.metric_id: metric.value for metric in result.metrics}
    assert values["instrument_exposure"] == pytest.approx(1100.0)
    assert values["estimated_transaction_cost"] == 1.0


def test_pretrade_refuses_unreconciled_total_cost(tmp_path: Path) -> None:
    sources, arguments = _pretrade_fixture()
    sources["costs_v1"][0]["Cost"] = {"TotalCost": 2.0}
    with pytest.raises(AnalyticsExecutionError, match="pretrade_cost_model_source_mismatch"):
        execute_recipe(
            RecipeRequest("pretrade_impact", (_DATASET,), (_HANDLE,), arguments),
            _inputs(tmp_path, sources, (_quote(_HANDLE, 100.0),)),
        )


def _surface_fixture() -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], dict[str, object]
]:
    sources, first_rows = _option_fixture()
    rows = list(first_rows)
    for identifier, expiry, strike, price in (
        (11, date(2027, 1, 1), 90.0, 12.0),
        (12, date(2027, 4, 1), 100.0, 9.0),
    ):
        handle = instrument_handle_for_saxo_identity("StockOption", identifier)
        sources["reference_instrument_details_v1"].extend(
            [
                {
                    "Uic": identifier,
                    "AssetType": "StockOption",
                    "CurrencyCode": "USD",
                    "ContractSize": 100.0,
                    "ExerciseStyle": "European",
                    "PayoffStyle": "Vanilla",
                }
            ]
        )
        sources["info_price_v1"].extend(
            [{"Uic": identifier, "AssetType": "StockOption", "Quote": {"Mid": price}}]
        )
        rows.extend(
            [
                _quote(handle, price),
                OptionReferenceDatasetRow(
                    instrument_handle=handle,
                    underlying_handle=_HANDLE,
                    captured_at=_AS_OF,
                    expiry=expiry,
                    strike_value=strike,
                    currency="USD",
                    put_call="call",
                ),
            ]
        )
    choices = _option_choices()
    choices.update({"smile_expiry": date(2027, 1, 1), "term_selector_value": 1.0})
    return (sources, tuple(rows), {"options": {"option": choices}})


def test_iv_surface_solves_observed_prices_and_exact_term_selector(tmp_path: Path) -> None:
    sources, rows, arguments = _surface_fixture()
    result = execute_recipe(
        RecipeRequest("iv_surface", (_DATASET,), (_HANDLE,), arguments),
        _inputs(tmp_path, sources, rows),
    )
    assert {metric.metric_id for metric in result.metrics} == {"iv_skew"}
    assert len(result.tables[0].rows) == _EXPECTED_SURFACE_ROWS


def test_iv_surface_refuses_unavailable_exact_selector(tmp_path: Path) -> None:
    sources, rows, arguments = _surface_fixture()
    choices = cast("dict[str, object]", cast("dict[str, object]", arguments["options"])["option"])
    choices["term_selector_value"] = 0.5
    with pytest.raises(AnalyticsExecutionError, match="iv_surface_exact_points_unavailable"):
        execute_recipe(
            RecipeRequest("iv_surface", (_DATASET,), (_HANDLE,), arguments),
            _inputs(tmp_path, sources, rows),
        )


class _CancelledError(RuntimeError):
    pass


@pytest.mark.parametrize(
    "kind", ["monte_carlo", "bounded_backtest", "portfolio_minimum_variance", "option_payoff"]
)
def test_heavy_recipes_stop_at_work_boundaries_and_report_progress(
    tmp_path: Path, kind: str
) -> None:
    if kind == "monte_carlo":
        sources, arguments = _simulation_fixture()
        rows = None
        handles = ()
    elif kind == "bounded_backtest":
        sources, rows, arguments = _backtest_fixture()
        handles = (_HANDLE,)
    elif kind == "portfolio_minimum_variance":
        sources, rows, arguments = _optimization_fixture()
        handles = (_HANDLE, _SECOND_HANDLE)
    else:
        sources, rows = _option_fixture()
        choices = _option_choices()
        choices.update(
            {
                "legs": ({"instrument_handle": _OPTION_HANDLE, "quantity": 1.0},),
                "net_premium": 0.0,
                "eligible_costs": 0.0,
                "expiry_reference_prices": tuple(float(index) for index in range(1, 1001)),
            }
        )
        arguments = {"options": {"option": choices}}
        handles = (_OPTION_HANDLE,)
    progress: list[tuple[int, int]] = []
    checks = 0

    def cancel() -> None:
        nonlocal checks
        checks += 1
        if checks > _CANCEL_AFTER_CHECKS:
            raise _CancelledError

    inputs = replace(
        _inputs(tmp_path, sources, rows),
        cancellation_check=cancel,
        progress=_collect_progress(progress),
    )
    with pytest.raises(_CancelledError):
        execute_recipe(RecipeRequest(kind, (_DATASET,), handles, arguments), inputs)
    assert checks > _CANCEL_AFTER_CHECKS
    if kind in {"monte_carlo", "option_payoff"}:
        assert progress
        assert all(0 <= completed <= total for completed, total in progress)


@pytest.mark.parametrize("kind", ["monte_carlo", "bounded_backtest", "portfolio_minimum_variance"])
def test_progress_callbacks_preserve_the_exact_model_result(tmp_path: Path, kind: str) -> None:
    if kind == "monte_carlo":
        sources, arguments = _simulation_fixture()
        rows = None
        handles = ()
    elif kind == "bounded_backtest":
        sources, rows, arguments = _backtest_fixture()
        handles = (_HANDLE,)
    else:
        sources, rows, arguments = _optimization_fixture()
        handles = (_HANDLE, _SECOND_HANDLE)
    request = RecipeRequest(kind, (_DATASET,), handles, arguments)
    inputs = _inputs(tmp_path, sources, rows)
    progress: list[tuple[int, int]] = []
    with_progress = replace(inputs, progress=_collect_progress(progress))
    assert execute_recipe(request, inputs) == execute_recipe(request, with_progress)
    assert progress[-1][0] == progress[-1][1]


def test_chain_references_and_quotes_are_useful_without_pricing_assumptions(tmp_path: Path) -> None:
    sources, rows = _option_fixture()
    result = execute_recipe(
        RecipeRequest("option_chain", (_DATASET,), (_HANDLE,)), _inputs(tmp_path, sources, rows)
    )
    assert not result.metrics
    assert result.tables[0].rows[0].instrument_handle == _OPTION_HANDLE
    assert "model_implied_volatility" in result.unavailable_fields


def test_public_decimal_volatility_is_reconciled_and_conflicts_refused(tmp_path: Path) -> None:
    sources, rows = _option_fixture()
    arguments: dict[str, object] = {
        "options": {"option": _option_choices()},
        "volatility_assumption": Decimal("0.2"),
        "rate_assumption": Decimal("0.04"),
    }
    request = RecipeRequest("option_greeks", (_DATASET,), (_OPTION_HANDLE,), arguments)
    assert execute_recipe(request, _inputs(tmp_path, sources, rows)).metrics
    arguments["volatility_assumption"] = Decimal("0.3")
    with pytest.raises(AnalyticsExecutionError, match="model_choice_conflict"):
        execute_recipe(request, _inputs(tmp_path, sources, rows))


def test_public_backtest_starting_equity_conflict_is_not_ignored(tmp_path: Path) -> None:
    sources, rows, arguments = _backtest_fixture()
    arguments["starting_equity"] = 2000.0
    with pytest.raises(AnalyticsExecutionError, match="model_choice_conflict"):
        execute_recipe(
            RecipeRequest("bounded_backtest", (_DATASET,), (_HANDLE,), arguments),
            _inputs(tmp_path, sources, rows),
        )


def test_public_scenario_map_and_confirmation_are_honored(tmp_path: Path) -> None:
    sources, rows, arguments, handle = _scenario_fixture("scenario_custom")
    arguments["shocks"] = ({"instrument_handle": handle, "price_shock_ratio": Decimal("-0.2")},)
    request = RecipeRequest("scenario_custom", (_DATASET,), (handle,), arguments)
    with pytest.raises(AnalyticsExecutionError, match="model_choice_conflict"):
        execute_recipe(request, _inputs(tmp_path, sources, rows))
    del arguments["shocks"]
    arguments["caller_accepted_numeric_shocks"] = False
    with pytest.raises(AnalyticsExecutionError, match="scenario_numeric_confirmation_required"):
        execute_recipe(request, _inputs(tmp_path, sources, rows))


def test_sizing_refuses_missing_choices_and_unbound_fx(tmp_path: Path) -> None:
    sources = _sizing_sources()
    with pytest.raises(AnalyticsExecutionError, match="explicit_model_choice_required"):
        execute_recipe(
            RecipeRequest("position_sizing", (_DATASET,), (_HANDLE,)), _inputs(tmp_path, sources)
        )
    sources["reference_instrument_details_v1"][0]["CurrencyCode"] = "EUR"
    request = RecipeRequest(
        "position_sizing",
        (_DATASET,),
        (_HANDLE,),
        {
            "options": {
                "sizing": {
                    "maximum_weight": 0.5,
                    "reserved_buffer": 0.0,
                    "margin_requirement_rate": 1.0,
                }
            }
        },
    )
    with pytest.raises(AnalyticsExecutionError, match="sizing_fx_conversion_source_required"):
        execute_recipe(request, _inputs(tmp_path, sources))


def test_scenario_aggregates_multiple_lots_for_one_instrument(tmp_path: Path) -> None:
    sources, rows, arguments, handle = _scenario_fixture("scenario_custom")
    sources["positions_v1"].append(sources["positions_v1"][0])
    result = execute_recipe(
        RecipeRequest("scenario_custom", (_DATASET,), (handle,), arguments),
        _inputs(tmp_path, sources, rows),
    )
    assert result.metrics[0].value == pytest.approx(-200.0)


def _collect_progress(progress: list[tuple[int, int]]) -> Callable[[int, int], None]:
    def collect(completed: int, total: int) -> None:
        progress.append((completed, total))

    return collect


def test_pretrade_honors_price_and_maximum_loss_as_a_declared_budget(tmp_path: Path) -> None:
    sources, arguments = _pretrade_fixture()
    arguments.update({"proposal_price": Decimal(100), "maximum_loss": Decimal(20)})
    request = RecipeRequest("pretrade_impact", (_DATASET,), (_HANDLE,), arguments)
    result = execute_recipe(request, _inputs(tmp_path, sources, (_quote(_HANDLE, 100.0),)))
    assert "maximum_loss_is_caller_declared_budget" in result.warnings
    assert "proposal_loss_validation" in result.unavailable_fields
    choice = cast("dict[str, object]", cast("dict[str, object]", arguments["options"])["pretrade"])
    choice["stop_price"] = 90.0
    validated = execute_recipe(request, _inputs(tmp_path, sources, (_quote(_HANDLE, 100.0),)))
    assert not validated.unavailable_fields
    arguments["maximum_loss"] = Decimal(5)
    with pytest.raises(AnalyticsExecutionError, match="proposal_maximum_loss_exceeded"):
        execute_recipe(request, _inputs(tmp_path, sources, (_quote(_HANDLE, 100.0),)))
    arguments["proposal_price"] = Decimal(99)
    with pytest.raises(AnalyticsExecutionError, match="proposal_price_quote_mismatch"):
        execute_recipe(request, _inputs(tmp_path, sources, (_quote(_HANDLE, 100.0),)))


def test_stock_sizing_uses_one_share_unit_when_contract_size_is_absent(tmp_path: Path) -> None:
    sources = _sizing_sources()
    del sources["reference_instrument_details_v1"][0]["ContractSize"]
    arguments: dict[str, object] = {
        "method": "stop_distance",
        "maximum_loss": 100.0,
        "risk_budget_confirmed": True,
        "stop_price": 90.0,
        "options": {
            "sizing": {
                "maximum_weight": 0.5,
                "reserved_buffer": 0.0,
                "margin_requirement_rate": 1.0,
            }
        },
    }
    result = execute_recipe(
        RecipeRequest("position_sizing", (_DATASET,), (_HANDLE,), arguments),
        _inputs(tmp_path, sources),
    )
    assert result.metrics[0].value == _EXPECTED_SIZING


def test_chartable_tables_use_declared_units_and_currencies(tmp_path: Path) -> None:
    sources, rows, arguments = _backtest_fixture()
    result = execute_recipe(
        RecipeRequest("bounded_backtest", (_DATASET,), (_HANDLE,), arguments),
        _inputs(tmp_path, sources, rows),
    )
    cells = [cell for row in result.tables[0].rows for cell in row.cells if cell.field == "equity"]
    assert cells
    assert all(cell.unit == "reporting_currency" and cell.currency == "USD" for cell in cells)
    sources, rows, arguments = _optimization_fixture()
    targets = execute_recipe(
        RecipeRequest("portfolio_risk_parity", (_DATASET,), (_HANDLE, _SECOND_HANDLE), arguments),
        _inputs(tmp_path, sources, rows),
    )
    weights = [
        cell
        for row in targets.tables[0].rows
        for cell in row.cells
        if cell.field == "target_weight"
    ]
    assert all(cell.unit == "ratio" and cell.currency is None for cell in weights)
    assert next(
        metric for metric in targets.metrics if metric.metric_id == "risk_parity_contribution"
    ).value == pytest.approx(0.5, abs=1e-6)


def test_model_choices_for_another_recipe_are_refused(tmp_path: Path) -> None:
    sources, arguments = _simulation_fixture()
    options = cast("dict[str, object]", arguments["options"])
    options["sizing"] = {
        "maximum_weight": 0.5,
        "reserved_buffer": 0.0,
        "margin_requirement_rate": 1.0,
    }
    with pytest.raises(AnalyticsExecutionError) as refused:
        execute_recipe(
            RecipeRequest("monte_carlo", (_DATASET,), (), arguments), _inputs(tmp_path, sources)
        )
    assert refused.value.reason_code == "model_choice_not_supported_for_kind"
    assert refused.value.missing_fields == ("options.sizing",)
