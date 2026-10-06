"""Production decision models over authenticated Saxo inputs and explicit choices.

The recipes have no network or trading client. Source facts are read only from
ResearchInputs; caller options describe model assumptions and decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import fmean
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.analytics_backtest import (
    BacktestDataset,
    BacktestRequest,
    HistoricalBar,
    run_backtest,
)
from saxo_bank_mcp.analytics_costs import CostComponents
from saxo_bank_mcp.analytics_derivatives import (
    DerivativeDataset,
    FuturesContractPoint,
    FuturesCurveRequest,
    FuturesCurveValues,
    FxForwardRequest,
    FxForwardValues,
    IvSurfacePoint,
    IvSurfaceRequest,
    IvSurfaceValues,
    OptionAnalyticsValues,
    analyze_futures_curve,
    analyze_fx_forward,
    analyze_iv_surface,
    analyze_option_model,
)
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_instruments import ResearchRefusal
from saxo_bank_mcp.analytics_metrics import covariance, simple_returns
from saxo_bank_mcp.analytics_models import (
    AnalysisCell,
    AnalysisId,
    AnalysisResult,
    AnalysisRow,
    AnalysisTable,
    InstrumentAnalysisRequest,
    InstrumentHandle,
    MetricClass,
    ModelScalarUnit,
    NamedModelAssumption,
    QualityState,
    RecipeAnalysisRequest,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_monte_carlo import (
    BootstrapGoalRequest,
    CashFlowPeriod,
    ReturnCalibrationDataset,
    ReturnObservation,
    run_bootstrap_goal_model,
)
from saxo_bank_mcp.analytics_optimization import (
    AssetClassConstraint,
    CovariancePerturbation,
    CurrencyConstraint,
    OptimizationAsset,
    OptimizationDataset,
    OptimizationRequest,
    SolverSettings,
    optimize_portfolio,
)
from saxo_bank_mcp.analytics_options import (
    ImpliedVolatilityRequest,
    OptionContract,
    OptionModelInput,
    OptionStrategyLeg,
    OptionStrategyRequest,
    StrategyPayoffPoint,
    aggregate_strategy_greeks,
    model_option,
    solve_implied_volatility,
    strategy_payoff,
)
from saxo_bank_mcp.analytics_portfolio import assess_source_bindings
from saxo_bank_mcp.analytics_position_sizing import PositionSizingRequest, size_position
from saxo_bank_mcp.analytics_pretrade import TradeProposal, build_pretrade_impact
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipeMetric,
    RecipePayload,
    RecipeRequest,
    ResearchInputs,
    number,
    utc_timestamp,
)
from saxo_bank_mcp.analytics_scenarios import (
    CurrencyShock,
    PortfolioScenarioRequest,
    ScenarioComponent,
    ScenarioShock,
    ScenarioType,
    run_portfolio_scenario,
)
from saxo_bank_mcp.analytics_strategy_schema import StrategyDefinition
from saxo_bank_mcp.analytics_sync import OptionReferenceDatasetRow
from saxo_bank_mcp.analytics_trade_review import DecisionPointQuote


class _Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SizingOptions(_Choice):
    maximum_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    reserved_buffer: float = Field(ge=0, allow_inf_nan=False)
    margin_requirement_rate: float = Field(gt=0, allow_inf_nan=False)
    volatility_window: int | None = Field(default=None, ge=2, le=1000)


class AssetConstraintChoice(_Choice):
    instrument_handle: InstrumentHandle
    lower_bound: float = Field(allow_inf_nan=False)
    upper_bound: float = Field(allow_inf_nan=False)
    minimum_trade_weight: float = Field(ge=0, allow_inf_nan=False)
    excluded: bool
    margin_requirement_rate: float = Field(ge=0, allow_inf_nan=False)


class OptimizationOptions(_Choice):
    assets: tuple[AssetConstraintChoice, ...] = Field(min_length=2, max_length=12)
    asset_class_constraints: tuple[AssetClassConstraint, ...] = ()
    currency_constraints: tuple[CurrencyConstraint, ...] = ()
    covariance_perturbation_ratios: tuple[float, ...] = Field(min_length=1, max_length=8)
    estimation_start_at: datetime | None = None
    estimation_end_at: datetime | None = None


class OptionLegChoice(_Choice):
    instrument_handle: InstrumentHandle
    quantity: float = Field(allow_inf_nan=False)
    volatility: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class OptionOptions(_Choice):
    pricing_model: Literal["black_scholes", "black_76"]
    exercise_style: Literal["european", "american"]
    payoff_style: Literal["vanilla", "path_dependent"]
    risk_free_rate: float = Field(allow_inf_nan=False)
    dividend_yield: float | None = Field(default=None, allow_inf_nan=False)
    volatility: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    days_per_year: float = Field(gt=0, allow_inf_nan=False)
    expiry_time_utc: time | None = None
    legs: tuple[OptionLegChoice, ...] = Field(default=(), max_length=20)
    net_premium: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="Signed total strategy premium assumption in the native contract currency.",
    )
    eligible_costs: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="Total modeled strategy cost assumption in the native contract currency.",
    )
    expiry_reference_prices: tuple[float, ...] = Field(default=(), max_length=50_000)
    iv_sigma_min: float = Field(default=0.0001, gt=0, allow_inf_nan=False)
    iv_sigma_max: float = Field(default=5, gt=0, allow_inf_nan=False)
    iv_price_tolerance: float = Field(default=1e-8, gt=0, allow_inf_nan=False)
    iv_sigma_tolerance: float = Field(default=1e-8, gt=0, allow_inf_nan=False)
    iv_maximum_iterations: int = Field(default=256, ge=1, le=10_000)
    smile_axis: Literal["strike", "delta"] = "strike"
    smile_expiry: date | None = None
    term_selector: Literal["moneyness", "delta"] = "moneyness"
    term_selector_value: float | None = Field(default=None, allow_inf_nan=False)


class ScenarioChoice(_Choice):
    instrument_handle: InstrumentHandle
    price_shock_ratio: float = Field(default=0, ge=-1, allow_inf_nan=False)
    volatility_shock_points: float = Field(default=0, allow_inf_nan=False)
    rate_shock_basis_points: float = Field(default=0, allow_inf_nan=False)
    cash_flow_shock: float = Field(default=0, allow_inf_nan=False)
    margin_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    model_analysis_id: AnalysisId | None = None


class CurrencyShockChoice(_Choice):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    shock_ratio: float = Field(gt=-1, allow_inf_nan=False)


class ScenarioOptions(_Choice):
    shocks: tuple[ScenarioChoice, ...] = Field(min_length=1, max_length=100)
    currency_shocks: tuple[CurrencyShockChoice, ...] = ()
    historical_start_at: datetime | None = None
    historical_end_at: datetime | None = None
    margin_allocation: Literal["proportional_gross_exposure"] | None = None


class SimulationOptions(_Choice):
    random_seed: int = Field(ge=0, lt=2**64)
    path_count: int = Field(ge=1, le=100_000)
    block_length: int = Field(ge=1)
    horizon_periods: int = Field(ge=1, le=10_000)
    horizon_years: float = Field(gt=0, allow_inf_nan=False)
    starting_value: float = Field(gt=0, allow_inf_nan=False)
    explicit_goal: float = Field(ge=0, allow_inf_nan=False)
    explicit_ruin_threshold: float = Field(ge=0, allow_inf_nan=False)
    cash_flows: tuple[CashFlowPeriod, ...] = Field(min_length=1, max_length=10_000)
    annual_inflation_assumption: float | None = Field(default=None, gt=-1, allow_inf_nan=False)


class FuturesOptions(_Choice):
    spot_handle: InstrumentHandle
    near_handle: InstrumentHandle
    later_handle: InstrumentHandle
    quantity: float = Field(allow_inf_nan=False)
    eligible_roll_costs: float = Field(
        ge=0,
        allow_inf_nan=False,
        description="Modeled total roll-cost assumption in the native futures contract currency.",
    )
    days_per_year: float = Field(gt=0, allow_inf_nan=False)
    expiry_time_utc: time | None = None


class FxOptions(_Choice):
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    quote_currency: str = Field(pattern=r"^[A-Z]{3}$")
    base_rate: float = Field(allow_inf_nan=False)
    quote_rate: float = Field(allow_inf_nan=False)
    forward_date: datetime
    days_per_year: float = Field(gt=0, allow_inf_nan=False)


class PretradeOptions(_Choice):
    origin_analysis_id: AnalysisId
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, allow_inf_nan=False)
    holding_period_days: int = Field(ge=0, le=36_500)
    margin_requirement_rate: float = Field(ge=0, allow_inf_nan=False)
    modeled_cost_components: CostComponents
    proposal_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    maximum_loss: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    stop_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class BacktestOptions(_Choice):
    strategy: StrategyDefinition
    starting_equity: float = Field(gt=0, allow_inf_nan=False)


class ModelOptions(_Choice):
    """Explicit model choices accepted by the public MCP request options field."""

    sizing: SizingOptions | None = None
    optimization: OptimizationOptions | None = None
    option: OptionOptions | None = None
    scenario: ScenarioOptions | None = None
    simulation: SimulationOptions | None = None
    futures: FuturesOptions | None = None
    fx: FxOptions | None = None
    pretrade: PretradeOptions | None = None
    backtest: BacktestOptions | None = None


_MINIMUM_PRICE_OBSERVATIONS = 31
_MINIMUM_SURFACE_POINTS = 2

_SCENARIO_TYPES: dict[str, ScenarioType] = {
    "margin_fire_drill": "margin",
    "portfolio_scenario": "combined",
    "scenario_historical": "historical",
    "scenario_custom": "equity",
    "scenario_currency": "currency",
    "scenario_volatility": "volatility",
    "scenario_rate": "rate",
    "scenario_margin": "margin",
    "scenario_combined": "combined",
    "derivatives_scenario": "combined",
}
_SCENARIO_METRICS = {
    "margin_fire_drill": "margin_stress_effect",
    "portfolio_scenario": "combined_scenario_effect",
    "scenario_historical": "historical_scenario_effect",
    "scenario_custom": "custom_shock_effect",
    "scenario_currency": "currency_shock_effect",
    "scenario_volatility": "volatility_shock_effect",
    "scenario_rate": "rate_shock_effect",
    "scenario_margin": "margin_stress_effect",
    "scenario_combined": "combined_scenario_effect",
    "derivatives_scenario": "combined_scenario_effect",
}
REQUIRED_METRICS: dict[str, tuple[str, ...]] = {
    "position_sizing": ("position_size", "maximum_loss"),
    "pretrade_impact": ("instrument_exposure", "estimated_transaction_cost"),
    "portfolio_minimum_variance": ("minimum_variance_objective", "optimizer_turnover"),
    "portfolio_risk_parity": ("risk_parity_contribution", "optimizer_turnover"),
    "derivatives_model": ("theoretical_option_value", "delta"),
    "option_greeks": ("delta", "gamma", "theta", "vega", "rho"),
    "option_chain": (),
    "option_payoff": ("payoff_at_expiry", "aggregate_delta"),
    "iv_surface": ("iv_skew",),
    "futures_curve": ("futures_basis", "futures_carry", "futures_roll"),
    "fx_forward_carry": ("fx_forward", "fx_carry"),
    "monte_carlo": ("goal_probability", "ruin_probability"),
    "goal_model": ("goal_probability", "ruin_probability", "sequence_of_returns_risk"),
    "bounded_backtest": ("total_return", "maximum_drawdown", "turnover"),
    **{kind: (metric,) for kind, metric in _SCENARIO_METRICS.items()},
}
SUPPORTED_KINDS = frozenset(REQUIRED_METRICS)
SOURCE_CONTRACTS: dict[str, tuple[str, ...]] = {
    "position_sizing": (
        "balances_v1",
        "positions_v1",
        "costs_v1",
        "reference_instrument_details_v1",
        "info_price_v1",
    ),
    "pretrade_impact": (
        "balances_v1",
        "positions_v1",
        "costs_v1",
        "info_price_v1",
        "reference_instrument_details_v1",
    ),
    "portfolio_minimum_variance": (
        "chart_v3",
        "positions_v1",
        "exposure_instruments_v1",
        "balances_v1",
        "costs_v1",
        "reference_instrument_details_v1",
    ),
    "portfolio_risk_parity": (
        "chart_v3",
        "positions_v1",
        "exposure_instruments_v1",
        "balances_v1",
        "costs_v1",
        "reference_instrument_details_v1",
    ),
    "futures_curve": (
        "reference_instruments_v1",
        "reference_instrument_details_v1",
        "info_price_v1",
    ),
    "fx_forward_carry": ("reference_instruments_v1", "info_price_v1"),
    "monte_carlo": ("performance_timeseries_v4", "balances_v1"),
    "goal_model": ("performance_timeseries_v4", "balances_v1"),
    "bounded_backtest": ("chart_v3", "reference_instruments_v1", "reference_instrument_details_v1"),
    **dict.fromkeys(
        ("derivatives_model", "option_greeks", "option_chain", "option_payoff", "iv_surface"),
        ("options_chain_reference_v1", "reference_instrument_details_v1", "info_price_v1"),
    ),
    "option_chain": ("options_chain_reference_v1", "info_price_v1"),
    **dict.fromkeys(_SCENARIO_TYPES, ("balances_v1", "positions_v1", "exposure_instruments_v1")),
    "scenario_historical": ("balances_v1", "positions_v1", "exposure_instruments_v1", "chart_v3"),
}
METRIC_IDS: dict[str, tuple[str, ...]] = {
    **REQUIRED_METRICS,
    "pretrade_impact": (*REQUIRED_METRICS["pretrade_impact"], "maximum_loss"),
    "position_sizing": (
        *REQUIRED_METRICS["position_sizing"],
        "concentration_limit",
        "margin_constraint",
    ),
    "portfolio_minimum_variance": (
        *REQUIRED_METRICS["portfolio_minimum_variance"],
        "estimated_transaction_cost",
        "optimizer_stability",
    ),
    "portfolio_risk_parity": (
        *REQUIRED_METRICS["portfolio_risk_parity"],
        "estimated_transaction_cost",
        "optimizer_stability",
    ),
    "derivatives_model": ("theoretical_option_value", "delta", "gamma", "theta", "vega", "rho"),
    "option_greeks": ("theoretical_option_value", *REQUIRED_METRICS["option_greeks"]),
    "option_chain": ("implied_volatility",),
    "option_payoff": (
        "payoff_at_expiry",
        "aggregate_delta",
        "aggregate_gamma",
        "aggregate_theta",
        "aggregate_vega",
        "aggregate_rho",
    ),
    "futures_curve": (*REQUIRED_METRICS["futures_curve"], "futures_term_structure"),
    "monte_carlo": (
        "goal_probability",
        "ruin_probability",
        "sequence_of_returns_risk",
        "sensitivity_lower",
        "sensitivity_upper",
    ),
    "goal_model": (
        "goal_probability",
        "ruin_probability",
        "sequence_of_returns_risk",
        "sensitivity_lower",
        "sensitivity_upper",
    ),
}


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return Decimal(str(number(value, field_name=field_name)))


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnalyticsExecutionError("source_object_unavailable", (field_name,))
    return cast("Mapping[str, object]", value)


def _text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise AnalyticsExecutionError("source_field_unavailable", (field,))
    return value


def _identity(row: Mapping[str, object]) -> str:
    identifier = row.get("Uic", row.get("Identifier"))
    if not isinstance(identifier, int) or isinstance(identifier, bool):
        raise AnalyticsExecutionError("instrument_identity_unavailable", ("Uic",))
    return instrument_handle_for_saxo_identity(_text(row, "AssetType"), identifier)


def _one(inputs: ResearchInputs, contract: str) -> Mapping[str, object]:
    rows = inputs.source_rows(contract)
    if len(rows) != 1:
        raise AnalyticsExecutionError("source_scope_missing_or_ambiguous", (contract,))
    return rows[0]


def _source_for(inputs: ResearchInputs, contract: str, handle: str) -> Mapping[str, object]:
    rows = tuple(row for row in inputs.source_rows(contract) if _identity(row) == handle)
    if len(rows) != 1:
        raise AnalyticsExecutionError("instrument_source_missing_or_ambiguous", (contract,))
    return rows[0]


def _currency(inputs: ResearchInputs) -> str:
    currency = inputs.currency()
    if currency == "XXX":
        raise AnalyticsExecutionError("reporting_currency_unavailable", ("Currency",))
    return currency


def _require_sources(request: RecipeRequest, inputs: ResearchInputs) -> None:
    present = {page.contract_name for page in inputs.pages}
    missing = tuple(sorted(set(SOURCE_CONTRACTS[request.analysis_kind]) - present))
    if missing:
        raise AnalyticsExecutionError("source_contract_missing", missing)
    if any(
        material.dataset.quality_state is not QualityState.COMPLETE for material in inputs.materials
    ):
        raise AnalyticsExecutionError("model_source_incomplete")
    warnings = _checked(
        assess_source_bindings(
            inputs.bindings(),
            required_contract_ids=SOURCE_CONTRACTS[request.analysis_kind],
            analysis_kind=request.analysis_kind,
            dataset_id=request.dataset_ids[0],
            instrument_handles=request.instrument_handles,
        )
    )
    if warnings:
        raise AnalyticsExecutionError("model_source_incomplete", warnings)


def _options(request: RecipeRequest) -> ModelOptions:
    raw: object = request.arguments.get("options") or {}
    try:
        options = (
            raw if isinstance(raw, ModelOptions) else ModelOptions.model_validate(raw, strict=False)
        )
        if options.option is not None:
            updates: dict[str, float] = {}
            for public_name, option_name in (
                ("volatility_assumption", "volatility"),
                ("rate_assumption", "risk_free_rate"),
            ):
                supplied = request.arguments.get(public_name)
                if supplied is None:
                    continue
                value = float(_decimal(supplied, public_name))
                chosen = getattr(options.option, option_name)
                if chosen is not None and chosen != value:
                    raise AnalyticsExecutionError("model_choice_conflict", (public_name,))
                updates[option_name] = value
            if updates:
                options = options.model_copy(
                    update={"option": options.option.model_copy(update=updates)}
                )
        elif (
            request.arguments.get("volatility_assumption") is not None
            or request.arguments.get("rate_assumption") is not None
        ):
            raise AnalyticsExecutionError("model_choice_not_supported_for_kind")
    except ValidationError as error:
        raise AnalyticsExecutionError("model_options_invalid") from error
    return options


def _required[T](value: T | None, field_name: str) -> T:
    if value is None:
        raise AnalyticsExecutionError("explicit_model_choice_required", (field_name,))
    return value


def _checked[T](result: T | ResearchRefusal) -> T:
    if isinstance(result, ResearchRefusal):
        raise AnalyticsExecutionError(result.reason_code, result.missing_fields)
    return result


def _scalar(value: object) -> str | float | int | bool | None:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, str | float | int | bool):
        return value
    raise AnalyticsExecutionError("model_table_value_invalid")


def _table(
    table_id: str,
    title: str,
    rows: Sequence[Mapping[str, object]],
    *,
    units: Mapping[str, tuple[str, str | None]] | None = None,
) -> AnalysisTable:
    declared = units or {}
    return AnalysisTable(
        table_id=table_id,
        title=title,
        rows=tuple(
            AnalysisRow(
                label=str(index + 1),
                instrument_handle=cast("str | None", row.get("instrument_handle")),
                at=cast("datetime | None", row.get("at")),
                cells=tuple(
                    AnalysisCell(
                        field=field,
                        value=_scalar(value),
                        unit=declared.get(field, (None, None))[0],
                        currency=declared.get(field, (None, None))[1],
                    )
                    for field, value in row.items()
                    if field not in {"instrument_handle", "at"}
                ),
            )
            for index, row in enumerate(rows)
        ),
    )


def _model_table(
    table_id: str,
    title: str,
    values: BaseModel,
    *,
    value_unit: tuple[str, str | None] | None = None,
) -> AnalysisTable:
    rows = [
        {"measure": name, "value": value}
        for name, value in values.model_dump(mode="python").items()
        if value is None or isinstance(value, str | float | int | bool | Decimal)
    ]
    return _table(
        table_id, title, rows, units=None if value_unit is None else {"value": value_unit}
    )


def _greek_table(table_id: str, title: str, values: BaseModel, currency: str) -> AnalysisTable:
    return _table(
        table_id,
        title,
        [values.model_dump(mode="python")],
        units={
            "delta": ("ratio", None),
            "gamma": ("inverse_price_currency", currency),
            "theta_per_day": ("price_currency_per_day", currency),
            "vega_per_volatility_point": ("price_currency_per_volatility_point", currency),
            "rho_per_rate_point": ("price_currency_per_rate_point", currency),
        },
    )


def _assumptions(**values: float | None) -> tuple[NamedModelAssumption, ...]:
    units = {
        "days_per_year": ModelScalarUnit.DAYS,
        "block_length": ModelScalarUnit.COUNT,
        "path_count": ModelScalarUnit.COUNT,
        "covariance_sample_count": ModelScalarUnit.COUNT,
        "risk_free_rate": ModelScalarUnit.RATE,
        "dividend_yield": ModelScalarUnit.RATE,
        "base_rate": ModelScalarUnit.RATE,
        "quote_rate": ModelScalarUnit.RATE,
    }
    return tuple(
        NamedModelAssumption(name=name, value=value, unit=units.get(name, ModelScalarUnit.RATIO))
        for name, value in sorted(values.items())
        if value is not None
    )


def _metric(metric_id: str, value: float | Decimal, currency: str | None = None) -> RecipeMetric:
    return RecipeMetric(metric_id, float(value), currency, MetricClass.MODEL_OUTPUT)


def _handle(request: RecipeRequest) -> str:
    handle = request.arguments.get("instrument_handle")
    if isinstance(handle, str):
        return handle
    if len(request.instrument_handles) != 1:
        raise AnalyticsExecutionError("single_instrument_required")
    return request.instrument_handles[0]


def _price(inputs: ResearchInputs, handle: str) -> float:
    dataset = inputs.quote(handle)
    quote = dataset.quote
    if (
        quote.freshness != "fresh"
        or dataset.entitlement_state != "available"
        or not timedelta(0) <= inputs.as_of - quote.captured_at <= timedelta(minutes=5)
    ):
        raise AnalyticsExecutionError("current_quote_unavailable")
    if quote.mid_value is not None:
        return quote.mid_value
    if quote.bid_value is not None and quote.ask_value is not None:
        return (quote.bid_value + quote.ask_value) / 2
    raise AnalyticsExecutionError("quote_mid_unavailable", ("Quote.Mid",))


def _details(inputs: ResearchInputs, handle: str) -> Mapping[str, object]:
    return _source_for(inputs, "reference_instrument_details_v1", handle)


def _contract_multiplier(details: Mapping[str, object]) -> Decimal:
    if details.get("PriceCurrency") is not None and details["PriceCurrency"] != details.get(
        "CurrencyCode"
    ):
        raise AnalyticsExecutionError("instrument_price_currency_conversion_required")
    if _text(details, "AssetType") == "Stock":
        supplied = details.get("ContractSize")
        if supplied is not None and _decimal(supplied, "ContractSize") != 1:
            raise AnalyticsExecutionError("stock_share_multiplier_invalid")
        return Decimal(1)
    return _decimal(details.get("ContractSize"), "ContractSize")


def _positions(
    inputs: ResearchInputs,
) -> dict[str, tuple[Mapping[str, object], Mapping[str, object]]]:
    result: dict[str, tuple[Mapping[str, object], Mapping[str, object]]] = {}
    for row in inputs.source_rows("positions_v1"):
        base = _mapping(row.get("PositionBase"), "PositionBase")
        view = _mapping(row.get("PositionView"), "PositionView")
        handle = _identity(base)
        if handle in result:
            previous_base, previous_view = result[handle]
            if previous_view.get("ExposureCurrency") != view.get(
                "ExposureCurrency"
            ) or previous_view.get("ConversionRateCurrent") != view.get("ConversionRateCurrent"):
                raise AnalyticsExecutionError("position_scope_ambiguous")
            merged_base = dict(previous_base)
            merged_base["Amount"] = _decimal(previous_base.get("Amount"), "Amount") + _decimal(
                base.get("Amount"), "Amount"
            )
            merged_view = dict(previous_view)
            for field in ("Exposure", "ExposureInBaseCurrency", "MarketValueInBaseCurrency"):
                if field in previous_view or field in view:
                    merged_view[field] = _decimal(previous_view.get(field), field) + _decimal(
                        view.get(field), field
                    )
            result[handle] = (merged_base, merged_view)
            continue
        result[handle] = (base, view)
    return result


def execute_recipe(request: RecipeRequest, inputs: ResearchInputs | None) -> RecipePayload:
    """Execute one typed production recipe without acquiring data or broker authority."""
    if request.analysis_kind not in SUPPORTED_KINDS:
        raise AnalyticsExecutionError("analysis_kind_unsupported")
    if inputs is None or not request.dataset_ids:
        raise AnalyticsExecutionError("authenticated_inputs_required")
    inputs.check_cancellation()
    _require_sources(request, inputs)
    choices = _options(request)
    allowed_options = {
        "position_sizing": {"sizing"},
        "pretrade_impact": {"pretrade"},
        "futures_curve": {"futures"},
        "fx_forward_carry": {"fx"},
        "monte_carlo": {"simulation"},
        "goal_model": {"simulation"},
        "bounded_backtest": {"backtest"},
        **dict.fromkeys(_SCENARIO_TYPES, frozenset(("scenario", "option"))),
        **dict.fromkeys(
            ("portfolio_minimum_variance", "portfolio_risk_parity"), frozenset(("optimization",))
        ),
        **dict.fromkeys(
            ("derivatives_model", "option_greeks", "option_chain", "option_payoff", "iv_surface"),
            frozenset(("option",)),
        ),
    }
    unsupported = tuple(
        f"options.{name}"
        for name in sorted(ModelOptions.model_fields)
        if getattr(choices, name) is not None and name not in allowed_options[request.analysis_kind]
    )
    if unsupported:
        raise AnalyticsExecutionError("model_choice_not_supported_for_kind", unsupported)
    handlers = {
        "position_sizing": _sizing,
        "pretrade_impact": _pretrade,
        "futures_curve": _futures,
        "fx_forward_carry": _fx,
        "monte_carlo": _simulation,
        "goal_model": _simulation,
        "bounded_backtest": _backtest,
        **dict.fromkeys(_SCENARIO_TYPES, _scenario),
        **dict.fromkeys(("portfolio_minimum_variance", "portfolio_risk_parity"), _optimization),
        **dict.fromkeys(
            ("derivatives_model", "option_greeks", "option_chain", "option_payoff", "iv_surface"),
            _options_recipe,
        ),
    }
    try:
        return handlers[request.analysis_kind](request, inputs, choices)
    except (ValidationError, ArithmeticError) as error:
        raise AnalyticsExecutionError("model_inputs_invalid") from error


def _cost_total(inputs: ResearchInputs, handle: str) -> Decimal:
    source = _source_for(inputs, "costs_v1", handle)
    cost = _mapping(source.get("Cost"), "Cost")
    if cost.get("TotalCost") is not None:
        return _decimal(cost["TotalCost"], "Cost.TotalCost")
    long = _mapping(cost.get("Long"), "Cost.Long")
    return _decimal(long.get("TotalCost"), "Cost.Long.TotalCost")


def _sizing(request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions) -> RecipePayload:
    choices = _required(options.sizing, "options.sizing")
    handle = _handle(request)
    details = _details(inputs, handle)
    if _text(details, "CurrencyCode") != _currency(inputs):
        raise AnalyticsExecutionError("sizing_fx_conversion_source_required")
    balance = _one(inputs, "balances_v1")
    volatility = None
    if request.arguments.get("method") == "volatility":
        window = _required(choices.volatility_window, "options.sizing.volatility_window")
        closes = [bar.close_value for bar in inputs.series(handle).bars]
        if len(closes) < window + 1:
            raise AnalyticsExecutionError("volatility_window_incomplete")
        returns = simple_returns(closes[-window - 1 :])
        volatility = Decimal(str(float(returns.std(ddof=1)) * closes[-1]))
    domain_request = PositionSizingRequest(
        dataset_id=request.dataset_ids[0],
        account_alias=inputs.account_scope,
        instrument_handle=handle,
        reporting_currency=_currency(inputs),
        method=cast("Literal['stop_distance', 'volatility']", request.arguments.get("method")),
        maximum_loss=_decimal(request.arguments.get("maximum_loss"), "maximum_loss"),
        risk_budget_confirmed=request.arguments.get("risk_budget_confirmed") is True,
        entry_price=Decimal(str(_price(inputs, handle))),
        stop_price=None
        if request.arguments.get("stop_price") is None
        else _decimal(request.arguments["stop_price"], "stop_price"),
        volatility_measure=volatility,
        volatility_multiplier=None
        if request.arguments.get("volatility_multiple") is None
        else _decimal(request.arguments["volatility_multiple"], "volatility_multiple"),
        value_per_price_unit=_contract_multiplier(details),
        lot_size=_decimal(details.get("LotSize"), "LotSize"),
        portfolio_value=_decimal(balance.get("TotalValue"), "TotalValue"),
        maximum_weight=Decimal(str(choices.maximum_weight)),
        buying_power=_decimal(balance.get("SpendingPower"), "SpendingPower"),
        reserved_buffer=Decimal(str(choices.reserved_buffer)),
        estimated_transaction_cost=_cost_total(inputs, handle),
        margin_headroom=_decimal(
            balance.get("MarginAvailableForTrading"), "MarginAvailableForTrading"
        ),
        margin_requirement_per_money_unit=Decimal(str(choices.margin_requirement_rate)),
        source_bindings=inputs.bindings(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )
    result = _checked(
        size_position(
            domain_request, visibility=VisibilityMode.PRIVATE_USER_RESULT, trusted_local_host=True
        )
    )
    values = _required(result.private_values, "private_values")
    return RecipePayload(
        metrics=(
            _metric("position_size", values.quantity),
            _metric("maximum_loss", values.maximum_loss, _currency(inputs)),
            _metric("concentration_limit", values.concentration_limit, _currency(inputs)),
            _metric("margin_constraint", values.margin_constraint, _currency(inputs)),
        ),
        tables=(_model_table("position_sizing", "Position size and binding constraints", values),),
        warnings=result.warnings,
        assumptions=_assumptions(
            margin_requirement_model_ratio=choices.margin_requirement_rate,
            maximum_weight=choices.maximum_weight,
        ),
        verifies=("Lot-aligned size under the explicit loss budget and observed account limits.",),
        does_not_verify=("Broker acceptance of a future order or its actual margin charge.",),
    )


def _pretrade_numeric_choice(
    request: RecipeRequest,
    choices: PretradeOptions,
    field: Literal["proposal_price", "maximum_loss"],
) -> float | None:
    chosen = choices.proposal_price if field == "proposal_price" else choices.maximum_loss
    public = request.arguments.get(field)
    if public is None:
        return chosen
    supplied = float(_decimal(public, field))
    if chosen is not None and chosen != supplied:
        raise AnalyticsExecutionError("model_choice_conflict", (field,))
    return supplied


def _proposal_risk(
    request: RecipeRequest,
    choices: PretradeOptions,
    details: Mapping[str, object],
    price: Decimal,
) -> tuple[float | None, list[Mapping[str, object]]]:
    selected_price = _pretrade_numeric_choice(request, choices, "proposal_price")
    maximum_loss = _pretrade_numeric_choice(request, choices, "maximum_loss")
    if selected_price is not None and Decimal(str(selected_price)) != price:
        raise AnalyticsExecutionError("proposal_price_quote_mismatch")
    if maximum_loss is None:
        if choices.stop_price is not None:
            raise AnalyticsExecutionError("proposal_risk_budget_required")
        return None, []
    rows: list[Mapping[str, object]] = [
        {"measure": "caller_declared_maximum_loss", "value": maximum_loss}
    ]
    if choices.stop_price is None:
        return maximum_loss, rows
    if _text(details, "AssetType") != "Stock":
        raise AnalyticsExecutionError("proposal_stop_loss_model_unsupported")
    if (choices.side == "buy" and choices.stop_price >= price) or (
        choices.side == "sell" and choices.stop_price <= price
    ):
        raise AnalyticsExecutionError("proposal_invalidation_side_mismatch")
    loss = (
        abs(price - Decimal(str(choices.stop_price)))
        * Decimal(str(choices.quantity))
        * _contract_multiplier(details)
    )
    if loss > Decimal(str(maximum_loss)):
        raise AnalyticsExecutionError("proposal_maximum_loss_exceeded")
    rows.append({"measure": "modeled_loss_at_declared_invalidation", "value": loss})
    return maximum_loss, rows


def _pretrade(
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    choices = _required(options.pretrade, "options.pretrade")
    handle = _handle(request)
    original = inputs.analysis(choices.origin_analysis_id)
    if original.account_scope != inputs.account_scope or original.as_of > inputs.as_of:
        raise AnalyticsExecutionError("origin_analysis_scope_mismatch")
    balance = _one(inputs, "balances_v1")
    details = _details(inputs, handle)
    if _text(details, "CurrencyCode") != _currency(inputs):
        raise AnalyticsExecutionError("pretrade_fx_conversion_source_required")
    price = Decimal(str(_price(inputs, handle)))
    multiplier = _contract_multiplier(details)
    quantity = Decimal(str(choices.quantity))
    maximum_loss, risk_rows = _proposal_risk(request, choices, details, price)
    notional = quantity * price * multiplier
    base, view = _positions(inputs).get(handle, ({}, {}))
    positions = _positions(inputs)
    currency_exposure = sum(
        (
            _decimal(position_view.get("ExposureInBaseCurrency"), "ExposureInBaseCurrency")
            for _, position_view in positions.values()
            if position_view.get("ExposureCurrency") == _currency(inputs)
        ),
        Decimal(0),
    )
    bindings = inputs.bindings()
    quote_dataset = inputs.quote(handle)
    quote = quote_dataset.quote
    quote_binding = next(binding for binding in bindings if binding.contract_id == "info_price_v1")
    decision_quote = DecisionPointQuote(
        dataset_id=request.dataset_ids[0],
        instrument_handle=handle,
        captured_at=quote.captured_at,
        bid=_decimal(quote.bid_value, "Quote.Bid"),
        ask=_decimal(quote.ask_value, "Quote.Ask"),
        price_type=_required(quote_dataset.price_type, "Quote.PriceTypeBid"),
        delayed_by_minutes=quote_dataset.delayed_by_minutes,
        quality_state=quote_dataset.quality_state,
        entitlement_state=quote_dataset.entitlement_state,
        source_binding=quote_binding,
        captured_by_mcp=True,
        warnings=quote_dataset.warnings,
    )
    costs = choices.modeled_cost_components
    if costs.turnover != notional:
        raise AnalyticsExecutionError("pretrade_turnover_mismatch")
    if costs.total_cost != _cost_total(inputs, handle):
        raise AnalyticsExecutionError("pretrade_cost_model_source_mismatch")
    cost_source = _source_for(inputs, "costs_v1", handle)
    if (
        _decimal(cost_source.get("Amount"), "cost.Amount") != quantity
        or _decimal(cost_source.get("Price"), "cost.Price") != price
    ):
        raise AnalyticsExecutionError("pretrade_cost_source_proposal_mismatch")
    proposal = TradeProposal(
        origin_analysis_id=choices.origin_analysis_id,
        dataset_id=request.dataset_ids[0],
        account_alias=inputs.account_scope,
        instrument_handle=handle,
        decision_at=quote.captured_at,
        side=choices.side,
        quantity=quantity,
        proposal_price=price,
        contract_multiplier=multiplier,
        instrument_currency=_currency(inputs),
        reporting_currency=_currency(inputs),
        current_position_quantity=_decimal(base.get("Amount", 0.0), "Amount"),
        current_position_exposure=_decimal(
            view.get("ExposureInBaseCurrency", 0.0), "ExposureInBaseCurrency"
        ),
        portfolio_value=_decimal(balance.get("TotalValue"), "TotalValue"),
        current_currency_exposure=currency_exposure,
        buying_power_available=_decimal(balance.get("SpendingPower"), "SpendingPower"),
        estimated_cash_required=notional if choices.side == "buy" else Decimal(0),
        margin_available=_decimal(
            balance.get("MarginAvailableForTrading"), "MarginAvailableForTrading"
        ),
        estimated_margin_required=notional * Decimal(str(choices.margin_requirement_rate)),
        holding_period_days=choices.holding_period_days,
        cost_estimate=costs,
        saxo_illustration=None,
        named_cost_difference=None,
        decision_quote=decision_quote,
        decision_bar=None,
        source_bindings=bindings,
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=("cost_component_allocation_model",),
    )
    result = _checked(
        build_pretrade_impact(
            choices.origin_analysis_id,
            proposal,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        )
    )
    values = _required(result.private_values, "private_values")
    return RecipePayload(
        metrics=(
            _metric("instrument_exposure", values.projected_instrument_exposure, _currency(inputs)),
            _metric("estimated_transaction_cost", values.costs.total_cost, _currency(inputs)),
            *(
                ()
                if maximum_loss is None
                else (_metric("maximum_loss", maximum_loss, _currency(inputs)),)
            ),
        ),
        tables=(
            _model_table("pretrade_impact", "Modeled proposal impact", values),
            _model_table("pretrade_costs", "Explicit modeled cost allocation", values.costs),
            _model_table("pretrade_quote", "Decision quote evidence", values.quote_evidence),
            *(
                ()
                if not risk_rows
                else (
                    _table(
                        "proposal_risk_budget",
                        "Explicit caller risk budget and invalidation",
                        risk_rows,
                    ),
                )
            ),
        ),
        warnings=tuple(
            sorted(
                {
                    *result.warnings,
                    *(() if maximum_loss is None else ("maximum_loss_is_caller_declared_budget",)),
                }
            )
        ),
        unavailable_fields=()
        if maximum_loss is None or choices.stop_price is not None
        else ("proposal_loss_validation",),
        assumptions=_assumptions(margin_requirement_model_ratio=choices.margin_requirement_rate),
        verifies=(
            "Proposal arithmetic using the exact quote, account limits and reconciled total cost.",
        ),
        does_not_verify=(
            "Trade approval, order creation, broker acceptance or actual future margin.",
        ),
    )


def _snapshot(inputs: ResearchInputs) -> str:
    digest = hashlib.sha256(
        "".join(material.dataset.fingerprint_sha256 for material in inputs.materials).encode()
    ).hexdigest()
    return "ps_" + UUID(hex=digest[:32], version=4).hex


def _optimization(
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    choices = _required(options.optimization, "options.optimization")
    by_handle = {choice.instrument_handle: choice for choice in choices.assets}
    if len(by_handle) != len(choices.assets):
        raise AnalyticsExecutionError("optimization_asset_constraints_duplicate")
    positions = _positions(inputs)
    if set(positions) - set(by_handle):
        raise AnalyticsExecutionError("optimization_current_holdings_omitted")
    handles = tuple(sorted(by_handle))
    series = {handle: inputs.series(handle) for handle in handles}
    price_rows = {
        handle: {bar.bar_time: bar.close_value for bar in series[handle].bars} for handle in handles
    }
    common = sorted(
        set(price_rows[handles[0]]).intersection(*(set(rows) for rows in price_rows.values()))
    )
    if choices.estimation_start_at is not None:
        common = [at for at in common if at >= choices.estimation_start_at]
    if choices.estimation_end_at is not None:
        common = [at for at in common if at <= choices.estimation_end_at]
    if (
        len(common) < _MINIMUM_PRICE_OBSERVATIONS
        or common[-1] > inputs.as_of
        or any(series[handle].missing_interval_count for handle in handles)
    ):
        raise AnalyticsExecutionError("optimization_price_history_incomplete")
    returns = {
        handle: simple_returns([price_rows[handle][at] for at in common]) for handle in handles
    }
    matrix = tuple(
        tuple(Decimal(str(covariance(returns[left], returns[right]))) for right in handles)
        for left in handles
    )
    exposures = {
        handle: _decimal(view.get("ExposureInBaseCurrency"), "ExposureInBaseCurrency")
        for handle, (_, view) in positions.items()
    }
    total = sum(exposures.values(), Decimal(0))
    if total <= 0:
        raise AnalyticsExecutionError("optimization_current_budget_undefined")
    weights = {handle: exposures.get(handle, Decimal(0)) / total for handle in handles}
    weights[handles[-1]] += Decimal(1) - sum(weights.values(), Decimal(0))
    assets: list[OptimizationAsset] = []
    for handle in handles:
        choice = by_handle[handle]
        details = _details(inputs, handle)
        cost = _source_for(inputs, "costs_v1", handle)
        cost_notional = (
            _decimal(cost.get("Amount"), "cost.Amount")
            * _decimal(cost.get("Price"), "cost.Price")
            * _contract_multiplier(details)
        )
        if cost_notional <= 0:
            raise AnalyticsExecutionError("optimization_cost_basis_unavailable")
        assets.append(
            OptimizationAsset(
                account_alias=inputs.account_scope,
                instrument_handle=handle,
                asset_class=_text(details, "AssetType").lower(),
                currency=_text(details, "CurrencyCode"),
                current_weight=weights[handle],
                expected_return=Decimal(str(fmean(returns[handle]))),
                lower_bound=Decimal(str(choice.lower_bound)),
                upper_bound=Decimal(str(choice.upper_bound)),
                transaction_cost_rate=_cost_total(inputs, handle) / cost_notional,
                margin_requirement_rate=Decimal(str(choice.margin_requirement_rate)),
                minimum_trade_weight=Decimal(str(choice.minimum_trade_weight)),
                excluded=choice.excluded,
            )
        )
    dataset = OptimizationDataset(
        dataset_id=request.dataset_ids[0],
        snapshot_id=_snapshot(inputs),
        account_alias=inputs.account_scope,
        estimation_start_at=common[0],
        estimation_end_at=common[-1],
        as_of=inputs.as_of,
        reporting_currency=_currency(inputs),
        assets=tuple(assets),
        covariance_matrix=matrix,
        sample_count=len(common) - 1,
        return_model="historical_arithmetic",
        covariance_model="sample_covariance",
        source_bindings=inputs.bindings(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )
    objective = (
        "risk_parity" if request.analysis_kind == "portfolio_risk_parity" else "minimum_variance"
    )
    if request.arguments.get("objective", objective) != objective:
        raise AnalyticsExecutionError("optimization_objective_kind_mismatch")
    domain_request = OptimizationRequest(
        dataset=dataset,
        objective=objective,
        objective_confirmed_by_caller=request.arguments.get("objective_confirmed_by_caller")
        is True,
        constraints_confirmed_by_caller=request.arguments.get("constraints_confirmed_by_caller")
        is True,
        short_policy=cast(
            "Literal['long_only', 'bounded_short']", request.arguments.get("short_policy")
        ),
        asset_class_constraints=choices.asset_class_constraints,
        currency_constraints=choices.currency_constraints,
        maximum_turnover=_decimal(request.arguments.get("maximum_turnover"), "maximum_turnover"),
        maximum_transaction_cost_ratio=_decimal(
            request.arguments.get("maximum_transaction_cost_ratio"),
            "maximum_transaction_cost_ratio",
        ),
        maximum_margin_ratio=_decimal(
            request.arguments.get("maximum_margin_ratio"), "maximum_margin_ratio"
        ),
        perturbations=tuple(
            CovariancePerturbation(
                perturbation_id=f"scale_{index}",
                covariance_matrix=tuple(
                    tuple(value * (Decimal(1) + Decimal(str(ratio))) for value in row)
                    for row in matrix
                ),
            )
            for index, ratio in enumerate(choices.covariance_perturbation_ratios)
        ),
        solver_settings=SolverSettings(
            method="SLSQP",
            maximum_iterations=1000,
            objective_tolerance=Decimal("1e-8"),
            feasibility_tolerance=Decimal("1e-8"),
            kkt_tolerance=Decimal("1e-5"),
        ),
        lexicographic_tie_break_rule="asset_order_within_objective_tolerance",
        concentration_warning_threshold=Decimal("0.95"),
        condition_number_warning_threshold=Decimal(1000000),
        stability_warning_threshold=Decimal("0.05"),
        stability_refusal_threshold=Decimal("0.25"),
    )
    result = _checked(
        optimize_portfolio(
            domain_request,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
            cancellation_check=inputs.check_cancellation,
            progress=inputs.report_work,
        )
    )
    values = _required(result.private_values, "private_values")
    metric_id = REQUIRED_METRICS[request.analysis_kind][0]
    objective_value = (
        values.objective_value
        if objective == "minimum_variance"
        else _required(
            next(
                target.normalized_risk_contribution
                for target in values.targets
                if target.normalized_risk_contribution is not None
            ),
            "risk_contributions",
        )
    )
    return RecipePayload(
        metrics=(
            _metric(metric_id, objective_value),
            _metric("optimizer_turnover", values.turnover),
            _metric(
                "estimated_transaction_cost",
                values.estimated_transaction_cost_ratio
                * _decimal(_one(inputs, "balances_v1").get("TotalValue"), "TotalValue"),
                _currency(inputs),
            ),
            _metric("optimizer_stability", values.diagnostics.maximum_perturbation_weight_change),
        ),
        tables=(
            _table(
                "optimization_targets",
                "Current and target weights",
                [target.model_dump(mode="python") for target in values.targets],
                units=dict.fromkeys(
                    (
                        "current_weight",
                        "target_weight",
                        "current_to_target_delta",
                        "normalized_risk_contribution",
                    ),
                    ("ratio", None),
                ),
            ),
            _model_table(
                "optimization_diagnostics", "Feasibility and solver diagnostics", values.diagnostics
            ),
            _table(
                "optimization_covariance",
                "Frozen covariance estimate",
                [
                    {
                        "instrument_handle": left,
                        **{
                            f"asset_{index}": float(matrix[row_index][index])
                            for index in range(len(handles))
                        },
                    }
                    for row_index, left in enumerate(handles)
                ],
            ),
        ),
        warnings=tuple(
            sorted(
                {
                    *result.warnings,
                    *(
                        ("risk_contribution_scalar_first_eligible_asset",)
                        if objective == "risk_parity"
                        else ()
                    ),
                }
            )
        ),
        assumptions=_assumptions(covariance_sample_count=float(len(common) - 1)),
        verifies=(
            "Constrained objective, feasibility, target weights and covariance stability tests.",
        ),
        does_not_verify=("A preferred objective, investment recommendation or broker execution.",),
    )


def _expiry(details: Mapping[str, object], expiry_date: date, expiry_time: time | None) -> datetime:
    timestamp = details.get("ExpiryDateTime")
    if timestamp is not None:
        return utc_timestamp(timestamp, field_name="ExpiryDateTime")
    selected_time = _required(expiry_time, "options.expiry_time_utc")
    return datetime.combine(expiry_date, selected_time, tzinfo=UTC)


def _option_references(inputs: ResearchInputs) -> tuple[OptionReferenceDatasetRow, ...]:
    references = tuple(
        row for row in inputs.all_rows() if isinstance(row, OptionReferenceDatasetRow)
    )
    if not references:
        raise AnalyticsExecutionError("option_reference_unavailable")
    if len({row.instrument_handle for row in references}) != len(references):
        raise AnalyticsExecutionError("option_reference_scope_ambiguous")
    return references


def _model_input(
    inputs: ResearchInputs, handle: str, choices: OptionOptions, volatility: float | None = None
) -> OptionModelInput:
    matching = tuple(row for row in _option_references(inputs) if row.instrument_handle == handle)
    if len(matching) != 1:
        raise AnalyticsExecutionError("option_reference_scope_mismatch")
    reference = matching[0]
    details = _details(inputs, handle)
    underlying = _source_for(inputs, "info_price_v1", reference.underlying_handle)
    if choices.pricing_model == "black_76" and _text(underlying, "AssetType") not in {
        "FuturesContract",
        "CfdOnFutures",
        "FxForwards",
    }:
        raise AnalyticsExecutionError("option_reference_kind_mismatch")
    expiry = _expiry(details, reference.expiry, choices.expiry_time_utc)
    if expiry <= inputs.as_of:
        raise AnalyticsExecutionError("option_expiry_unavailable")
    contract = OptionContract(
        dataset_id=inputs.materials[0].dataset.dataset_id,
        option_handle=handle,
        underlying_handle=reference.underlying_handle,
        contract_currency=_text(details, "CurrencyCode"),
        pricing_model=choices.pricing_model,
        reference_kind="spot" if choices.pricing_model == "black_scholes" else "forward_or_futures",
        option_type=reference.put_call,
        exercise_style=choices.exercise_style,
        payoff_style=choices.payoff_style,
        rate_model="constant",
        reference_price=_price(inputs, reference.underlying_handle),
        strike=reference.strike_value,
        time_to_expiry_years=(expiry - inputs.as_of).total_seconds()
        / (choices.days_per_year * 86400),
        risk_free_rate=choices.risk_free_rate,
        dividend_yield=choices.dividend_yield,
        days_per_year=choices.days_per_year,
    )
    if (
        details.get("PriceCurrency") is not None
        and details["PriceCurrency"] != contract.contract_currency
    ):
        raise AnalyticsExecutionError("option_price_currency_conversion_unbound")
    return OptionModelInput(
        contract=contract,
        volatility=_required(
            volatility if volatility is not None else choices.volatility,
            "options.option.volatility",
        ),
    )


def _derivative_dataset(
    request: RecipeRequest, inputs: ResearchInputs, handles: tuple[str, ...]
) -> DerivativeDataset:
    return DerivativeDataset(
        dataset_id=request.dataset_ids[0],
        account_alias=inputs.account_scope,
        as_of=inputs.as_of,
        instrument_handles=handles,
        source_bindings=inputs.bindings(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )


def _iv(
    inputs: ResearchInputs, reference: OptionReferenceDatasetRow, choices: OptionOptions
) -> tuple[OptionModelInput, float]:
    model = _model_input(inputs, reference.instrument_handle, choices, choices.volatility or 0.2)
    result = _checked(
        solve_implied_volatility(
            ImpliedVolatilityRequest(
                contract=model.contract,
                observed_price=_price(inputs, reference.instrument_handle),
                sigma_min=choices.iv_sigma_min,
                sigma_max=choices.iv_sigma_max,
                price_tolerance=choices.iv_price_tolerance,
                sigma_tolerance=choices.iv_sigma_tolerance,
                maximum_iterations=choices.iv_maximum_iterations,
            )
        )
    )
    return model.model_copy(update={"volatility": result.volatility}), result.volatility


def _options_recipe(  # noqa: C901, PLR0912, PLR0915
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    references = _option_references(inputs)
    if request.instrument_handles:
        selected = tuple(
            reference
            for reference in references
            if reference.instrument_handle in request.instrument_handles
            or reference.underlying_handle in request.instrument_handles
        )
        if not selected:
            raise AnalyticsExecutionError("option_reference_scope_mismatch")
        references = selected
    if request.analysis_kind == "option_chain" and options.option is None:
        return RecipePayload(
            tables=(
                _table(
                    "option_chain",
                    "Bound option references and observed quotes",
                    [
                        {
                            "instrument_handle": reference.instrument_handle,
                            "underlying_handle": reference.underlying_handle,
                            "expiry": reference.expiry,
                            "strike": reference.strike_value,
                            "put_call": reference.put_call,
                            "observed_mid": _price(inputs, reference.instrument_handle),
                        }
                        for reference in references
                    ],
                ),
            ),
            unavailable_fields=("model_implied_volatility",),
            verifies=("Declared bounded chain references and observed quote scope.",),
            does_not_verify=("Market-wide option coverage, model volatility or future value.",),
        )
    choices = _required(options.option, "options.option")
    handles = tuple(
        dict.fromkeys(
            handle
            for reference in references
            for handle in (reference.instrument_handle, reference.underlying_handle)
        )
    )
    dataset = _derivative_dataset(request, inputs, handles)
    assumptions = _assumptions(
        risk_free_rate=choices.risk_free_rate,
        dividend_yield=choices.dividend_yield,
        days_per_year=choices.days_per_year,
        european_exercise_model=1.0 if choices.exercise_style == "european" else 0.0,
        vanilla_payoff_model=1.0 if choices.payoff_style == "vanilla" else 0.0,
        volatility=choices.volatility,
    )
    if request.analysis_kind in {"derivatives_model", "option_greeks"}:
        if len(references) != 1:
            raise AnalyticsExecutionError("single_option_required")
        modeled = _model_input(
            inputs,
            references[0].instrument_handle,
            choices,
            cast("float | None", request.arguments.get("volatility_assumption"))
            or choices.volatility,
        )
        result = _checked(
            analyze_option_model(
                dataset,
                modeled,
                visibility=VisibilityMode.PRIVATE_USER_RESULT,
                trusted_local_host=True,
            )
        )
        values = _required(result.private_values, "private_values")
        if not isinstance(values, OptionAnalyticsValues):
            raise AnalyticsExecutionError("option_model_output_invalid")
        model = values.model
        greeks = _required(model.greeks, "option_greeks")
        return RecipePayload(
            metrics=(
                _metric(
                    "theoretical_option_value", model.value, modeled.contract.contract_currency
                ),
                _metric("delta", greeks.delta),
                _metric("gamma", greeks.gamma),
                _metric("theta", greeks.theta_per_day, modeled.contract.contract_currency),
                _metric(
                    "vega", greeks.vega_per_volatility_point, modeled.contract.contract_currency
                ),
                _metric("rho", greeks.rho_per_rate_point, modeled.contract.contract_currency),
            ),
            tables=(
                _table(
                    "option_model",
                    "Option model value",
                    [model.model_dump(mode="python", exclude={"greeks", "warnings"})],
                    units={"value": ("price_currency", modeled.contract.contract_currency)},
                ),
                _greek_table(
                    "option_greeks",
                    "Local model Greeks",
                    greeks,
                    modeled.contract.contract_currency,
                ),
            ),
            warnings=tuple(
                sorted({*result.warnings, "exercise_and_payoff_styles_model_assumptions"})
            ),
            assumptions=assumptions,
            verifies=("The declared supported pricing model and frozen Greek units.",),
            does_not_verify=(
                "Broker Greek agreement, American exercise valuation or future option value.",
            ),
        )
    if request.analysis_kind == "option_payoff":
        if not choices.legs:
            raise AnalyticsExecutionError("option_strategy_legs_required")
        strategy = OptionStrategyRequest(
            dataset_id=request.dataset_ids[0],
            account_alias=inputs.account_scope,
            strategy_id="caller_defined_strategy",
            legs=tuple(
                OptionStrategyLeg(
                    model_input=_model_input(
                        inputs, leg.instrument_handle, choices, leg.volatility
                    ),
                    quantity=leg.quantity,
                    contract_multiplier=number(
                        _details(inputs, leg.instrument_handle).get("ContractSize"),
                        field_name="ContractSize",
                    ),
                )
                for leg in choices.legs
            ),
            net_premium=_required(choices.net_premium, "options.option.net_premium"),
            eligible_costs=_required(choices.eligible_costs, "options.option.eligible_costs"),
        )
        # The domain direct path caps output at 500; jobs use the same exact leg mathematics.
        grid = choices.expiry_reference_prices
        if not grid:
            raise AnalyticsExecutionError("option_payoff_grid_empty")
        aggregate = _checked(aggregate_strategy_greeks(strategy))
        payoff_points: list[StrategyPayoffPoint] = []
        for index, price in enumerate(grid):
            inputs.report_work(index, len(grid))
            payoff_points.append(_checked(strategy_payoff(strategy, expiry_reference_price=price)))
        inputs.report_work(len(grid), len(grid))
        points = tuple(payoff_points)
        currency = strategy.legs[0].model_input.contract.contract_currency
        return RecipePayload(
            metrics=(
                _metric(
                    "payoff_at_expiry",
                    points[0].total_payoff,
                    strategy.legs[0].model_input.contract.contract_currency,
                ),
                _metric("aggregate_delta", aggregate.delta),
                _metric("aggregate_gamma", aggregate.gamma),
                _metric("aggregate_theta", aggregate.theta_per_day, currency),
                _metric("aggregate_vega", aggregate.vega_per_volatility_point, currency),
                _metric("aggregate_rho", aggregate.rho_per_rate_point, currency),
            ),
            tables=(
                _table(
                    "option_payoff",
                    "Expiry payoff by declared reference price",
                    [
                        {
                            "expiry_reference_price": point.expiry_reference_price,
                            "total_payoff": point.total_payoff,
                            **{
                                f"leg_{index}_payoff": payoff
                                for index, payoff in enumerate(point.leg_payoffs)
                            },
                        }
                        for point in points
                    ],
                    units={
                        "expiry_reference_price": (
                            "price_currency",
                            strategy.legs[0].model_input.contract.contract_currency,
                        ),
                        "total_payoff": (
                            "price_currency",
                            strategy.legs[0].model_input.contract.contract_currency,
                        ),
                        **{
                            f"leg_{index}_payoff": (
                                "price_currency",
                                strategy.legs[0].model_input.contract.contract_currency,
                            )
                            for index in range(len(strategy.legs))
                        },
                    },
                ),
                _greek_table(
                    "strategy_greeks",
                    "Quantity and multiplier weighted Greeks",
                    aggregate,
                    currency,
                ),
                _table(
                    "option_strategy_assumptions",
                    "Caller premium and cost assumptions in the native contract currency",
                    [
                        {
                            "native_currency": currency,
                            "net_premium": strategy.net_premium,
                            "eligible_costs": strategy.eligible_costs,
                        }
                    ],
                    units={
                        "net_premium": ("price_currency", currency),
                        "eligible_costs": ("price_currency", currency),
                    },
                ),
            ),
            assumptions=assumptions,
            verifies=(
                "Exact signed leg payoff sum, declared premium/cost and aggregate local Greeks.",
            ),
            does_not_verify=("Future option prices, assignment or broker execution.",),
        )
    modeled_points: list[IvSurfacePoint] = []
    rows: list[Mapping[str, object]] = []
    for index, reference in enumerate(references):
        inputs.report_work(index, len(references))
        modeled, implied = _iv(inputs, reference, choices)
        values = _checked(model_option(modeled))
        greeks = _required(values.greeks, "option_greeks")
        expiry = _expiry(
            _details(inputs, reference.instrument_handle), reference.expiry, choices.expiry_time_utc
        )
        point = IvSurfacePoint(
            instrument_handle=reference.instrument_handle,
            expiry_at=expiry,
            time_to_expiry_years=modeled.contract.time_to_expiry_years,
            strike=reference.strike_value,
            signed_delta=greeks.delta,
            moneyness=reference.strike_value / modeled.contract.reference_price,
            implied_volatility=implied,
        )
        modeled_points.append(point)
        rows.append(
            {
                "instrument_handle": reference.instrument_handle,
                "expiry": expiry,
                "strike": reference.strike_value,
                "put_call": reference.put_call,
                "observed_mid": _price(inputs, reference.instrument_handle),
                "implied_volatility": implied,
                "delta": greeks.delta,
            }
        )
    if request.analysis_kind == "option_chain":
        return RecipePayload(
            metrics=()
            if len(modeled_points) != 1
            else (
                _metric(
                    "implied_volatility",
                    modeled_points[0].implied_volatility,
                ),
            ),
            tables=(
                _table(
                    "option_chain",
                    "Bound option references, quotes and model implied volatility",
                    rows,
                    units={
                        "strike": ("price_currency", references[0].currency),
                        "observed_mid": ("price_currency", references[0].currency),
                        "implied_volatility": ("ratio", None),
                        "delta": ("ratio", None),
                    },
                ),
            ),
            assumptions=assumptions,
            warnings=("exercise_and_payoff_styles_model_assumptions",),
            verifies=("Explicit chain scope and quote-implied volatility under the chosen model.",),
            does_not_verify=("Complete market-wide option-chain coverage or future value.",),
        )
    smile_expiry = _required(choices.smile_expiry, "options.option.smile_expiry")
    selector = _required(choices.term_selector_value, "options.option.term_selector_value")
    smile = tuple(point for point in modeled_points if point.expiry_at.date() == smile_expiry)
    terms = tuple(
        point
        for point in modeled_points
        if (point.moneyness if choices.term_selector == "moneyness" else point.signed_delta)
        == selector
    )
    if len(smile) < _MINIMUM_SURFACE_POINTS or len(terms) < _MINIMUM_SURFACE_POINTS:
        raise AnalyticsExecutionError("iv_surface_exact_points_unavailable")
    result = _checked(
        analyze_iv_surface(
            dataset,
            IvSurfaceRequest(
                smile_axis=choices.smile_axis,
                smile_expiry_at=smile[0].expiry_at,
                smile_points=smile,
                term_selector=choices.term_selector,
                term_selector_value=selector,
                term_interpolation_rule="exact_match",
                term_points=terms,
            ),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        )
    )
    values = _required(result.private_values, "private_values")
    if not isinstance(values, IvSurfaceValues):
        raise AnalyticsExecutionError("iv_surface_output_invalid")
    return RecipePayload(
        metrics=(_metric("iv_skew", values.skew),),
        tables=(
            _table(
                "iv_surface",
                "Quote-implied smile and exact term points",
                rows,
                units={
                    "strike": ("price_currency", references[0].currency),
                    "observed_mid": ("price_currency", references[0].currency),
                    "implied_volatility": ("ratio", None),
                    "delta": ("ratio", None),
                },
            ),
        ),
        warnings=result.warnings,
        assumptions=assumptions,
        verifies=("Deterministic smile and exact selector-matched term structure.",),
        does_not_verify=("Interpolated missing strikes, missing maturities or broker forecasts.",),
    )


def _futures(
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    choices = _required(options.futures, "options.futures")
    handles = (choices.spot_handle, choices.near_handle, choices.later_handle)
    points: list[FuturesContractPoint] = []
    for handle in handles[1:]:
        details = _details(inputs, handle)
        expiry = _expiry(
            details, date.fromisoformat(_text(details, "ExpiryDate")[:10]), choices.expiry_time_utc
        )
        points.append(
            FuturesContractPoint(
                instrument_handle=handle,
                expiry_at=expiry,
                time_to_expiry_years=(expiry - inputs.as_of).total_seconds()
                / (choices.days_per_year * 86400),
                price=_price(inputs, handle),
            )
        )
    near_details = _details(inputs, choices.near_handle)
    later_details = _details(inputs, choices.later_handle)
    if near_details.get("CurrencyCode") != later_details.get("CurrencyCode") or near_details.get(
        "ContractSize"
    ) != later_details.get("ContractSize"):
        raise AnalyticsExecutionError("futures_contract_basis_mismatch")
    if _details(inputs, choices.spot_handle).get("CurrencyCode") != near_details.get(
        "CurrencyCode"
    ):
        raise AnalyticsExecutionError("futures_spot_currency_basis_mismatch")
    result = _checked(
        analyze_futures_curve(
            _derivative_dataset(request, inputs, handles),
            FuturesCurveRequest(
                spot_instrument_handle=choices.spot_handle,
                spot_price=_price(inputs, choices.spot_handle),
                near_contract=points[0],
                later_contract=points[1],
                quantity=choices.quantity,
                contract_multiplier=number(
                    near_details.get("ContractSize"), field_name="ContractSize"
                ),
                eligible_roll_costs=choices.eligible_roll_costs,
                reporting_currency=_text(near_details, "CurrencyCode"),
            ),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        )
    )
    values = _required(result.private_values, "private_values")
    if not isinstance(values, FuturesCurveValues):
        raise AnalyticsExecutionError("futures_model_output_invalid")
    return RecipePayload(
        metrics=(
            _metric("futures_basis", values.basis, values.reporting_currency),
            _metric("futures_carry", values.annualized_carry),
            _metric("futures_term_structure", values.term_structure),
            _metric("futures_roll", values.modeled_roll, values.reporting_currency),
        ),
        tables=(
            _table(
                "futures_curve",
                "Futures basis, carry and modeled roll",
                [values.model_dump(mode="python")],
                units={
                    "basis": ("price_currency", values.reporting_currency),
                    "annualized_carry": ("ratio", None),
                    "term_structure": ("ratio", None),
                    "modeled_roll": ("price_currency", values.reporting_currency),
                },
            ),
            _table(
                "futures_contracts",
                "Observed near and later contracts",
                [point.model_dump(mode="python") for point in points],
                units={
                    "price": ("price_currency", values.reporting_currency),
                    "time_to_expiry_years": ("years", None),
                },
            ),
            _table(
                "futures_roll_assumptions",
                "Caller roll-cost assumption in the native contract currency",
                [
                    {
                        "native_currency": values.reporting_currency,
                        "eligible_roll_costs": choices.eligible_roll_costs,
                    }
                ],
                units={"eligible_roll_costs": ("price_currency", values.reporting_currency)},
            ),
        ),
        warnings=result.warnings,
        assumptions=_assumptions(days_per_year=choices.days_per_year),
        verifies=("Observed contract curve and caller-specified roll-cost arithmetic.",),
    )


def _fx(request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions) -> RecipePayload:
    choices = _required(options.fx, "options.fx")
    handle = _handle(request)
    if choices.forward_date.tzinfo is None or choices.forward_date <= inputs.as_of:
        raise AnalyticsExecutionError("fx_forward_date_invalid")
    source = _source_for(inputs, "info_price_v1", handle)
    format_data = _mapping(source.get("DisplayAndFormat"), "DisplayAndFormat")
    symbol = _text(format_data, "Symbol").replace("/", "")
    if symbol != choices.base_currency + choices.quote_currency:
        raise AnalyticsExecutionError("fx_currency_pair_mismatch")
    result = _checked(
        analyze_fx_forward(
            _derivative_dataset(request, inputs, (handle,)),
            FxForwardRequest(
                spot_instrument_handle=handle,
                base_currency=choices.base_currency,
                quote_currency=choices.quote_currency,
                spot=_price(inputs, handle),
                time_to_expiry_years=(choices.forward_date - inputs.as_of).total_seconds()
                / (choices.days_per_year * 86400),
                base_rate=choices.base_rate,
                quote_rate=choices.quote_rate,
                rate_model="simple",
            ),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
        )
    )
    values = _required(result.private_values, "private_values")
    if not isinstance(values, FxForwardValues):
        raise AnalyticsExecutionError("fx_model_output_invalid")
    return RecipePayload(
        metrics=(
            _metric("fx_forward", values.forward, choices.quote_currency),
            _metric("fx_carry", values.annualized_carry),
        ),
        tables=(_model_table("fx_forward", "Simple-rate modeled forward and carry", values),),
        warnings=result.warnings,
        assumptions=_assumptions(
            base_rate=choices.base_rate,
            quote_rate=choices.quote_rate,
            days_per_year=choices.days_per_year,
        ),
        verifies=("Covered-interest-parity model under the explicit rates and horizon.",),
        does_not_verify=("Broker executable forward quotes or rate-curve valuation.",),
    )


def _calibration(inputs: ResearchInputs, dataset_id: str) -> ReturnCalibrationDataset:
    source = _one(inputs, "performance_timeseries_v4")
    weighted = _mapping(source.get("TimeWeighted"), "TimeWeighted")
    accumulated = weighted.get("Accumulated")
    if not isinstance(accumulated, Sequence) or isinstance(accumulated, str):
        raise AnalyticsExecutionError(
            "calibration_returns_unavailable", ("TimeWeighted.Accumulated",)
        )
    observations = sorted(
        (
            _mapping(row, "TimeWeighted.Accumulated")
            for row in cast("Sequence[object]", accumulated)
        ),
        key=lambda row: utc_timestamp(row.get("Date"), field_name="Date"),
    )
    returns: list[ReturnObservation] = []
    for previous, current in pairwise(observations):
        before = _decimal(previous.get("Value"), "TimeWeighted.Accumulated.Value") / Decimal(100)
        after = _decimal(current.get("Value"), "TimeWeighted.Accumulated.Value") / Decimal(100)
        if before <= -1:
            raise AnalyticsExecutionError("calibration_growth_undefined")
        returns.append(
            ReturnObservation(
                at=utc_timestamp(current.get("Date"), field_name="Date"),
                return_ratio=(Decimal(1) + after) / (Decimal(1) + before) - Decimal(1),
            )
        )
    return ReturnCalibrationDataset(
        dataset_id=dataset_id,
        account_alias=inputs.account_scope,
        as_of=inputs.as_of,
        reporting_currency=_currency(inputs),
        observations=tuple(returns),
        source_bindings=inputs.bindings(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )


def _simulation(
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    choices = _required(options.simulation, "options.simulation")
    result = _checked(
        run_bootstrap_goal_model(
            BootstrapGoalRequest(
                calibration=_calibration(inputs, request.dataset_ids[0]),
                starting_value=Decimal(str(choices.starting_value)),
                explicit_goal=Decimal(str(choices.explicit_goal)),
                explicit_ruin_threshold=Decimal(str(choices.explicit_ruin_threshold)),
                horizon_periods=choices.horizon_periods,
                horizon_years=Decimal(str(choices.horizon_years)),
                path_count=choices.path_count,
                block_length=choices.block_length,
                random_seed=choices.random_seed,
                cash_flow_timing="start_of_period",
                cash_flows=choices.cash_flows,
                include_inflation_adjustment=choices.annual_inflation_assumption is not None,
                annual_inflation_assumption=None
                if choices.annual_inflation_assumption is None
                else Decimal(str(choices.annual_inflation_assumption)),
            ),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
            cancellation_check=inputs.check_cancellation,
            progress=inputs.report_work,
        )
    )
    values = _required(result.private_values, "private_values")
    tables = [
        _model_table(
            "simulation_probabilities", "Seeded model probabilities and sequence risk", values
        ),
        _model_table(
            "simulation_distribution",
            "Ending-value model distribution",
            values.ending_values,
            value_unit=("reporting_currency", values.reporting_currency),
        ),
    ]
    if values.inflation_adjusted_ending_values is not None:
        tables.append(
            _model_table(
                "simulation_real_distribution",
                "Explicit inflation-adjusted model distribution",
                values.inflation_adjusted_ending_values,
                value_unit=("reporting_currency", values.reporting_currency),
            )
        )
    return RecipePayload(
        metrics=(
            _metric("goal_probability", values.goal_probability),
            _metric("ruin_probability", values.ruin_probability),
            _metric("sequence_of_returns_risk", values.sequence_of_returns_risk),
            _metric("sensitivity_lower", values.ending_values.lower, _currency(inputs)),
            _metric("sensitivity_upper", values.ending_values.upper, _currency(inputs)),
        ),
        tables=tuple(tables),
        warnings=result.warnings,
        assumptions=_assumptions(
            annual_inflation_assumption=choices.annual_inflation_assumption,
            block_length=float(choices.block_length),
            path_count=float(choices.path_count),
        ),
        verifies=(
            "Reproducible bootstrap distribution under explicit calibration and cash flows.",
        ),
        does_not_verify=("Future goal attainment or future returns.",),
        model_distribution_only=True,
    )


def _backtest(
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    choices = options.backtest
    if choices is None:
        strategy = request.arguments.get("strategy")
        if not isinstance(strategy, StrategyDefinition):
            strategy = StrategyDefinition.model_validate(strategy)
        choices = BacktestOptions(
            strategy=strategy,
            starting_equity=number(
                request.arguments.get("starting_equity"), field_name="starting_equity"
            ),
        )
    if (
        request.arguments.get("strategy") is not None
        and StrategyDefinition.model_validate(request.arguments["strategy"], strict=False)
        != choices.strategy
    ):
        raise AnalyticsExecutionError("model_choice_conflict", ("strategy",))
    if (
        request.arguments.get("starting_equity") is not None
        and number(request.arguments["starting_equity"], field_name="starting_equity")
        != choices.starting_equity
    ):
        raise AnalyticsExecutionError("model_choice_conflict", ("starting_equity",))
    handle = _handle(request)
    series = inputs.series(handle)
    details = _details(inputs, handle)
    state = _text(details, "TradingStatus")
    if _text(details, "CurrencyCode") != choices.strategy.transaction_costs.currency:
        raise AnalyticsExecutionError("backtest_currency_basis_mismatch")
    if state not in {"Tradable", "NotTradable", "Delisted"}:
        raise AnalyticsExecutionError("backtest_lifecycle_state_unavailable")
    bars = tuple(
        HistoricalBar(
            instrument_handle=handle,
            at=bar.bar_time,
            open_price=bar.open_value,
            close_price=bar.close_value,
            lifecycle_state="delisted"
            if index == len(series.bars) - 1 and state == "Delisted"
            else "active",
        )
        for index, bar in enumerate(series.bars)
        if bar.bar_time + bar.interval.delta <= inputs.as_of
    )
    if len(bars) != len(series.bars):
        raise AnalyticsExecutionError("backtest_incomplete_bar")
    domain_request = BacktestRequest(
        dataset=BacktestDataset(
            dataset_id=request.dataset_ids[0],
            account_alias=inputs.account_scope,
            instrument_handle=handle,
            as_of=inputs.as_of,
            bars=bars,
            source_bindings=inputs.bindings(),
            quality_state=QualityState.COMPLETE,
            missing_interval_count=series.missing_interval_count,
            missing_fields=(),
            warnings=series.warnings,
            universe_scope="single_instrument",
        ),
        strategy=choices.strategy,
        starting_equity=choices.starting_equity,
    )
    result = _checked(
        run_backtest(
            domain_request,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=True,
            cancellation_check=inputs.check_cancellation,
            progress=inputs.report_work,
        )
    )
    values = _required(result.private_values, "private_values")
    return RecipePayload(
        metrics=(
            _metric("total_return", values.total_return_ratio),
            _metric("maximum_drawdown", values.maximum_drawdown),
            _metric("turnover", values.total_turnover),
        ),
        tables=(
            _table(
                "backtest_equity",
                "Historical modeled equity",
                [point.model_dump(mode="python") for point in values.equity_curve],
                units={
                    "equity": ("reporting_currency", choices.strategy.transaction_costs.currency)
                },
            ),
            _table(
                "backtest_fills",
                "Next-bar-open modeled fills",
                [fill.model_dump(mode="python") for fill in values.fills],
                units={
                    "target_weight": ("ratio", None),
                    "turnover": ("ratio", None),
                    **dict.fromkeys(
                        ("commission", "fixed_fee", "slippage", "total_cost"),
                        ("reporting_currency", choices.strategy.transaction_costs.currency),
                    ),
                },
            ),
            _table(
                "backtest_splits",
                "Training and evaluation windows",
                [window.model_dump(mode="python") for window in values.split_windows],
            ),
            _model_table(
                "backtest_costs",
                "Modeled costs",
                values.costs,
                value_unit=("reporting_currency", choices.strategy.transaction_costs.currency),
            ),
            _model_table(
                "backtest_cost_sensitivity",
                "Zero, modeled and doubled costs",
                values.cost_sensitivity,
                value_unit=("reporting_currency", choices.strategy.transaction_costs.currency),
            ),
        ),
        warnings=tuple(sorted({*result.warnings, "broker_execution_validation_not_run"})),
        unavailable_fields=("equivalent_broker_execution_validation",),
        verifies=("Historical accounting under the bounded strategy and next-bar-open model.",),
        does_not_verify=(
            "Broker execution, SIM lifecycle, historical market universe or future returns.",
        ),
    )


def _scenario_options(request: RecipeRequest, options: ModelOptions) -> ScenarioOptions:
    if (
        "numeric_shocks_echoed_by_caller" in request.arguments
        and request.arguments["numeric_shocks_echoed_by_caller"] is not True
    ) or (
        "caller_accepted_numeric_shocks" in request.arguments
        and request.arguments["caller_accepted_numeric_shocks"] is not True
    ):
        raise AnalyticsExecutionError("scenario_numeric_confirmation_required")
    if options.scenario is not None:
        public = request.arguments.get("shocks")
        if public is not None:
            supplied = ScenarioOptions.model_validate({"shocks": public}, strict=False)
            fields = {
                "instrument_handle",
                "price_shock_ratio",
                "volatility_shock_points",
                "rate_shock_basis_points",
            }
            expected = tuple(shock.model_dump(include=fields) for shock in options.scenario.shocks)
            actual = tuple(shock.model_dump(include=fields) for shock in supplied.shocks)
            if expected != actual:
                raise AnalyticsExecutionError("model_choice_conflict", ("shocks",))
        return options.scenario
    shocks = request.arguments.get("shocks")
    if shocks is None:
        raise AnalyticsExecutionError("scenario_shock_map_required")
    return ScenarioOptions.model_validate({"shocks": shocks})


def _historical_shock(inputs: ResearchInputs, handle: str, choices: ScenarioOptions) -> float:
    start = _required(choices.historical_start_at, "options.scenario.historical_start_at")
    end = _required(choices.historical_end_at, "options.scenario.historical_end_at")
    if start.tzinfo is None or end.tzinfo is None or end < start or end > inputs.as_of:
        raise AnalyticsExecutionError("historical_scenario_interval_invalid")
    series = inputs.series(handle)
    if series.missing_interval_count:
        raise AnalyticsExecutionError("historical_replay_coverage_incomplete")
    rows = {
        bar.bar_time: bar.close_value
        for bar in series.bars
        if bar.bar_time + bar.interval.delta <= inputs.as_of
    }
    if start not in rows or end not in rows:
        raise AnalyticsExecutionError("historical_replay_observations_unbound")
    return rows[end] / rows[start] - 1


def _model_analysis(inputs: ResearchInputs, analysis_id: str | None, handle: str) -> AnalysisResult:
    identifier = _required(analysis_id, "scenario.model_analysis_id")
    analysis = inputs.analysis(identifier)
    if analysis.account_scope != inputs.account_scope or analysis.as_of > inputs.as_of:
        raise AnalyticsExecutionError("scenario_model_scope_mismatch")
    if isinstance(analysis.request, InstrumentAnalysisRequest):
        model_handles = analysis.request.instrument_handles
    elif isinstance(analysis.request, RecipeAnalysisRequest):
        arguments = _mapping(json.loads(analysis.request.recipe_arguments_json), "model_request")
        model_handles = cast("Sequence[str]", arguments.get("instrument_handles", ()))
    else:
        model_handles = ()
    if handle not in model_handles:
        raise AnalyticsExecutionError("scenario_model_instrument_mismatch")
    return analysis


def _reprice_option(
    inputs: ResearchInputs, handle: str, choices: OptionOptions, shock: ScenarioChoice
) -> Decimal:
    model = _model_input(inputs, handle, choices)
    updated_contract = model.contract.model_copy(
        update={
            "reference_price": model.contract.reference_price * (1 + shock.price_shock_ratio),
            "risk_free_rate": model.contract.risk_free_rate
            + shock.rate_shock_basis_points / 10_000,
        }
    )
    result = _checked(
        model_option(
            OptionModelInput(
                contract=updated_contract,
                volatility=model.volatility + shock.volatility_shock_points / 100,
            )
        )
    )
    return Decimal(str(result.value))


def _reprice_bond(
    inputs: ResearchInputs, handle: str, current: Decimal, shock: ScenarioChoice
) -> Decimal:
    analysis = _model_analysis(inputs, shock.model_analysis_id, handle)
    metrics = {metric.metric_id: metric.value for metric in analysis.metrics}
    duration = _decimal(metrics.get("modified_duration"), "model.modified_duration")
    convexity = _decimal(metrics.get("convexity"), "model.convexity")
    yield_change = Decimal(str(shock.rate_shock_basis_points)) / Decimal(10_000)
    return current * (
        Decimal(1)
        - duration * yield_change
        + Decimal("0.5") * convexity * yield_change * yield_change
    )


def _scenario(  # noqa: PLR0915
    request: RecipeRequest, inputs: ResearchInputs, options: ModelOptions
) -> RecipePayload:
    choices = _scenario_options(request, options)
    positions = _positions(inputs)
    shocks = {shock.instrument_handle: shock for shock in choices.shocks}
    if len(shocks) != len(choices.shocks) or set(shocks) != set(positions):
        raise AnalyticsExecutionError("scenario_shock_map_incomplete")
    balance = _one(inputs, "balances_v1")
    used_margin = _decimal(
        balance.get("MarginUsedByCurrentPositions"), "MarginUsedByCurrentPositions"
    )
    if used_margin != 0 and choices.margin_allocation is None:
        raise AnalyticsExecutionError("position_margin_allocation_required")
    gross = sum(
        (
            abs(_decimal(view.get("ExposureInBaseCurrency"), "ExposureInBaseCurrency"))
            for _, view in positions.values()
        ),
        Decimal(0),
    )
    if gross == 0:
        raise AnalyticsExecutionError("scenario_exposure_undefined")
    components: list[ScenarioComponent] = []
    component_shocks: list[ScenarioShock] = []
    warnings: list[str] = []
    for handle in sorted(positions):
        base, view = positions[handle]
        shock = shocks[handle]
        asset_type = _text(base, "AssetType").lower()
        currency = _text(view, "ExposureCurrency")
        exposure = _decimal(view.get("ExposureInBaseCurrency"), "ExposureInBaseCurrency")
        current = exposure
        margin = used_margin * abs(exposure) / gross
        branch: Literal["linear", "option", "fixed_income"] = "linear"
        repriced = None
        if "option" in asset_type:
            branch = "option"
            _model_analysis(inputs, shock.model_analysis_id, handle)
            model_choices = _required(options.option, "options.option")
            current = _decimal(view.get("MarketValueInBaseCurrency"), "MarketValueInBaseCurrency")
            multiplier = _decimal(_details(inputs, handle).get("ContractSize"), "ContractSize")
            quantity = _decimal(base.get("Amount"), "Amount")
            conversion = _decimal(view.get("ConversionRateCurrent"), "ConversionRateCurrent")
            repriced = (
                _reprice_option(inputs, handle, model_choices, shock)
                * multiplier
                * quantity
                * conversion
            )
        elif asset_type == "bond":
            branch = "fixed_income"
            current = _decimal(view.get("MarketValueInBaseCurrency"), "MarketValueInBaseCurrency")
            repriced = _reprice_bond(inputs, handle, current, shock)
            warnings.append("duration_convexity_repricing_approximation")
        price_shock = (
            _historical_shock(inputs, handle, choices)
            if request.analysis_kind == "scenario_historical"
            else shock.price_shock_ratio
        )
        components.append(
            ScenarioComponent(
                account_alias=inputs.account_scope,
                instrument_handle=handle,
                branch_id=branch,
                current_value=current,
                currency=currency,
                current_margin_requirement=margin,
                model_analysis_id=shock.model_analysis_id if branch != "linear" else None,
            )
        )
        component_shocks.append(
            ScenarioShock(
                instrument_handle=handle,
                price_shock_ratio=Decimal(str(price_shock)),
                volatility_shock_points=Decimal(str(shock.volatility_shock_points)),
                rate_shock_basis_points=Decimal(str(shock.rate_shock_basis_points)),
                cash_flow_shock=Decimal(str(shock.cash_flow_shock)),
                repriced_value_at_base_fx=repriced,
                stressed_margin_requirement=margin * Decimal(str(shock.margin_multiplier)),
            )
        )
    currencies = {component.currency for component in components}
    currency_choices = {shock.currency: shock for shock in choices.currency_shocks}
    if currency_choices and set(currency_choices) != currencies:
        raise AnalyticsExecutionError("scenario_currency_shock_map_incomplete")
    currency_shocks = tuple(
        CurrencyShock(
            currency=currency,
            shock_ratio=Decimal(str(currency_choices[currency].shock_ratio))
            if currency_choices
            else Decimal(0),
        )
        for currency in sorted(currencies)
    )
    if request.analysis_kind == "scenario_currency" and not currency_choices:
        raise AnalyticsExecutionError("scenario_currency_shock_map_required")
    domain_request = PortfolioScenarioRequest(
        dataset_id=request.dataset_ids[0],
        snapshot_id=_snapshot(inputs),
        account_alias=inputs.account_scope,
        as_of=inputs.as_of,
        reporting_currency=_currency(inputs),
        scenario_type="equity"
        if request.analysis_kind == "scenario_historical"
        else _SCENARIO_TYPES[request.analysis_kind],
        input_mode="numeric",
        narrative_fingerprint_sha256=None,
        numeric_shocks_echoed_by_caller=False,
        caller_accepted_numeric_shocks=False,
        echoed_shock_map_sha256=None,
        accepted_shock_map_sha256=None,
        historical_start_at=None,
        historical_end_at=None,
        components=tuple(components),
        component_shocks=tuple(component_shocks),
        currency_shocks=currency_shocks,
        current_margin_headroom=_decimal(
            balance.get("MarginAvailableForTrading"), "MarginAvailableForTrading"
        ),
        source_bindings=inputs.bindings(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=tuple(sorted(set(warnings))),
    )
    result = _checked(
        run_portfolio_scenario(
            domain_request, visibility=VisibilityMode.PRIVATE_USER_RESULT, trusted_local_host=True
        )
    )
    values = _required(result.private_values, "private_values")
    effect = (
        values.margin_stress_effect
        if _SCENARIO_TYPES[request.analysis_kind] == "margin"
        else values.total_effect
    )
    return RecipePayload(
        metrics=(_metric(_SCENARIO_METRICS[request.analysis_kind], effect, _currency(inputs)),),
        tables=(
            _table(
                "scenario_contributions",
                "Simultaneous scenario contributions",
                [contribution.model_dump(mode="python") for contribution in values.contributions],
                units={
                    field: ("reporting_currency", _currency(inputs))
                    for field in ("current_value", "stressed_value", "effect")
                },
            ),
            _model_table("scenario_totals", "Scenario effect and margin headroom", values),
        ),
        warnings=result.warnings,
        assumptions=_assumptions(
            proportional_gross_margin_allocation=1.0 if used_margin != 0 else None
        ),
        verifies=(
            "Arithmetic of the explicit shock map against the current portfolio and bound models.",
        ),
        does_not_verify=(
            "Scenario likelihood, broker stressed-margin acceptance or future outcomes.",
        ),
    )
