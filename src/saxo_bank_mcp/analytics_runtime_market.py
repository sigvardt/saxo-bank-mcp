"""Production stock and bounded-market recipes over authenticated local inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from statistics import fmean, pstdev
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp import analytics_metrics as metrics
from saxo_bank_mcp.analytics_fixed_income import (
    FixedIncomeCashFlow,
    FixedIncomeDataset,
    analyze_fixed_income,
)
from saxo_bank_mcp.analytics_indicators import IndicatorParameters, calculate_indicators
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_instruments import (
    QuoteResearchDataset,
    ResearchRefusal,
    ReturnSeriesRequest,
    analyze_instrument_prices,
    analyze_quote,
    build_instrument_dossier,
)
from saxo_bank_mcp.analytics_market import (
    BoundedResearchUniverse,
    SavedCondition,
    analyze_bounded_market,
    check_saved_conditions,
    prepare_bounded_session,
)
from saxo_bank_mcp.analytics_models import (
    AnalysisCell,
    AnalysisRow,
    AnalysisTable,
    InstrumentHandle,
    MetricClass,
    ModelScalarUnit,
    NamedModelAssumption,
)
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipeMetric,
    RecipePayload,
    RecipeRequest,
    ResearchInputs,
    number,
)

SUPPORTED_KINDS: Final = frozenset(
    {
        "instrument_resolution",
        "instrument_dossier",
        "instrument_price_return",
        "instrument_price_volume",
        "instrument_quote",
        "instrument_risk",
        "multi_instrument_comparison",
        "technical_indicators",
        "trading_conditions",
        "fixed_income",
        "market_comparison",
        "market_correlation_regime",
        "market_microstructure",
        "market_volatility_dispersion",
        "wrapper_comparison",
        "saved_condition_checks",
        "session_cockpit",
    }
)
REQUIRED_METRICS: Final[dict[str, tuple[str, ...]]] = dict.fromkeys(SUPPORTED_KINDS, ()) | {
    "instrument_price_return": ("price_return",),
    "instrument_risk": ("maximum_drawdown",),
    "instrument_quote": ("midpoint_price", "spread"),
    "market_microstructure": ("spread",),
    "technical_indicators": ("moving_average", "rsi", "macd"),
    "instrument_price_volume": ("moving_average", "rsi", "macd"),
    "fixed_income": ("yield_to_maturity", "modified_duration", "convexity"),
    "wrapper_comparison": ("wrapper_cost_difference",),
}
SOURCE_CONTRACTS: Final[dict[str, tuple[str, ...]]] = dict.fromkeys(
    SUPPORTED_KINDS, ("chart_v3",)
) | {
    "instrument_resolution": ("reference_instruments_v1",),
    "instrument_dossier": ("reference_instruments_v1", "chart_v3"),
    "instrument_quote": ("info_price_v1",),
    "market_microstructure": ("info_price_v1",),
    "trading_conditions": ("reference_instrument_details_v1",),
    "wrapper_comparison": ("costs_v1",),
    "fixed_income": ("info_price_v1",),
}
_PRICE_METRICS: Final = (
    "price_return",
    "maximum_drawdown",
    "best_period_return",
    "worst_period_return",
    "rolling_return",
    "volatility",
)
_QUOTE_METRICS: Final = ("midpoint_price", "spread", "quote_delay")
_INDICATOR_METRICS: Final = (
    "moving_average",
    "rsi",
    "macd",
    "bollinger_lower",
    "bollinger_middle",
    "bollinger_upper",
    "momentum",
    "realized_volatility",
    "atr",
    "volume",
    "volume_weighted_price",
)
METRIC_IDS: Final[dict[str, tuple[str, ...]]] = dict.fromkeys(SUPPORTED_KINDS, ())
METRIC_IDS.update(
    {
        "instrument_price_return": _PRICE_METRICS,
        "instrument_dossier": (*_PRICE_METRICS, *_QUOTE_METRICS),
        "instrument_risk": (
            *_PRICE_METRICS,
            "historical_var",
            "expected_shortfall",
            "downside_deviation",
            "sharpe_ratio",
            "sortino_ratio",
            "correlation",
            "covariance",
            "beta",
        ),
        "instrument_quote": _QUOTE_METRICS,
        "market_microstructure": _QUOTE_METRICS,
        "technical_indicators": _INDICATOR_METRICS,
        "instrument_price_volume": _INDICATOR_METRICS,
        "trading_conditions": ("total_cost",),
        "wrapper_comparison": ("wrapper_cost_difference",),
        "fixed_income": ("yield_to_maturity", "modified_duration", "convexity"),
    }
)
_MINIMUM_PAIR_OBSERVATIONS: Final = 2


class FixedIncomeModelAssumptions(BaseModel):
    """Explicit hypothetical cashflows; these never assert observed Saxo bond terms."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    cash_flows: tuple[FixedIncomeCashFlow, ...] = Field(min_length=1, max_length=24)
    compounding_frequency: int = Field(ge=1, le=365)
    day_count_basis: str = Field(min_length=1, max_length=40)
    quote_price_scale_assumption: float = Field(gt=0, allow_inf_nan=False)
    accrued_interest_assumption: float = Field(allow_inf_nan=False)


class MarketOptions(BaseModel):
    """Bounded mathematical choices; callers cannot supply broker or source facts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    indicator_parameters: IndicatorParameters | None = None
    confidence: float = Field(default=0.95, gt=0, lt=1, allow_inf_nan=False)
    risk_free_period_return_assumption: float | None = Field(default=None, allow_inf_nan=False)
    downside_target_period_return_assumption: float = Field(default=0.0, allow_inf_nan=False)
    benchmark_handle: InstrumentHandle | None = None
    fixed_income_model: FixedIncomeModelAssumptions | None = None


def _options(request: RecipeRequest) -> MarketOptions:
    value = request.arguments.get("options")
    if value is None:
        return MarketOptions()
    try:
        return MarketOptions.model_validate(value, strict=True)
    except ValidationError as error:
        raise AnalyticsExecutionError("market_options_invalid") from error


def _cell(
    field: str,
    value: str | float | bool | None,  # noqa: FBT001 - bool is a table scalar, not a flag
    *,
    unit: str | None = None,
    currency: str | None = None,
) -> AnalysisCell:
    return AnalysisCell(field=field, value=value, unit=unit, currency=currency)


def _table(table_id: str, title: str, rows: Sequence[AnalysisRow]) -> AnalysisTable:
    return AnalysisTable(table_id=table_id, title=title, rows=tuple(rows))


def _assumption(name: str, value: float, unit: ModelScalarUnit) -> NamedModelAssumption:
    return NamedModelAssumption(name=name, value=value, unit=unit)


def _checked[ResultT](value: ResultT | ResearchRefusal) -> ResultT:
    if isinstance(value, ResearchRefusal):
        raise AnalyticsExecutionError(
            value.reason_code, tuple(field for field in value.missing_fields if field)
        )
    return value


def _handles(request: RecipeRequest, inputs: ResearchInputs) -> tuple[str, ...]:
    if request.instrument_handles:
        handles = request.instrument_handles
    else:
        handles = tuple(dict.fromkeys(row.instrument_handle for row in inputs.all_rows()))
        if not handles:
            handles = tuple(
                dict.fromkeys(
                    page.instrument_handle
                    for page in inputs.pages
                    if page.instrument_handle is not None
                )
            )
    if not handles or len(handles) > inputs.config.limits.sync_instruments:
        raise AnalyticsExecutionError("bounded_instrument_scope_required")
    if len(handles) != len(set(handles)):
        raise AnalyticsExecutionError("duplicate_instrument_scope")
    return handles


def _single_handle(request: RecipeRequest, inputs: ResearchInputs) -> str:
    handles = _handles(request, inputs)
    if len(handles) != 1:
        raise AnalyticsExecutionError("single_instrument_scope_required")
    return handles[0]


def _periods(request: RecipeRequest) -> float:
    result = number(request.arguments.get("periods_per_year", 252.0), field_name="periods_per_year")
    if result <= 0:
        raise AnalyticsExecutionError("periods_per_year_invalid")
    return result


def _currency(inputs: ResearchInputs, handle: str) -> str | None:
    currency = _reference_currency(inputs, handle)
    if currency is not None:
        return currency
    try:
        inputs.reference(handle)
    except AnalyticsExecutionError as error:
        if error.reason_code != "instrument_reference_unavailable":
            raise
        return None
    return _reference_currency(inputs, handle)


def _reference_currency(inputs: ResearchInputs, handle: str) -> str | None:
    """Read the instrument's price currency independently of account reporting currency."""
    for row in inputs.source_rows("reference_instruments_v1"):
        identifier, asset_type = row.get("Identifier"), row.get("AssetType")
        if isinstance(identifier, int) and isinstance(asset_type, str):
            if instrument_handle_for_saxo_identity(asset_type, identifier) != handle:
                continue
            currency = row.get("CurrencyCode")
            if isinstance(currency, str):
                return currency
    for row in inputs.source_rows("reference_instrument_details_v1"):
        identifier, asset_type = row.get("Uic"), row.get("AssetType")
        if isinstance(identifier, int) and isinstance(asset_type, str):
            if instrument_handle_for_saxo_identity(asset_type, identifier) != handle:
                continue
            currency = row.get("CurrencyCode")
            if isinstance(currency, str):
                return currency
    return None


def _price(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    handle = _single_handle(request, inputs)
    series = inputs.series(handle)
    window = request.arguments.get("rolling_window", 20)
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise AnalyticsExecutionError("rolling_window_invalid")
    requested_return = request.arguments.get("requested_return", "price_return")
    if requested_return not in {"price_return", "adjusted_price_return", "total_return"}:
        raise AnalyticsExecutionError("return_choice_invalid")
    result = _checked(
        analyze_instrument_prices(
            series,
            rolling_window=window,
            periods_per_year=_periods(request),
            requested_return=cast("ReturnSeriesRequest", requested_return),
        )
    )
    claims = [
        RecipeMetric("price_return", result.price_return),
        RecipeMetric("maximum_drawdown", result.maximum_drawdown),
        RecipeMetric("best_period_return", max(result.period_returns)),
        RecipeMetric("worst_period_return", min(result.period_returns)),
    ]
    unavailable: list[str] = []
    if result.rolling_returns:
        claims.append(RecipeMetric("rolling_return", result.rolling_returns[-1].value))
    else:
        unavailable.append("rolling_return")
    if result.annualized_volatility is not None:
        claims.append(RecipeMetric("volatility", result.annualized_volatility))
    else:
        unavailable.append("volatility")
    periods = _table(
        "price_periods",
        "Returns between observed Saxo bars",
        [
            AnalysisRow(
                label="Observed bar return",
                instrument_handle=handle,
                at=bar.bar_time,
                cells=(_cell("price_return", value, unit="ratio"),),
            )
            for bar, value in zip(series.bars[1:], result.period_returns, strict=True)
        ],
    )
    rolling = _table(
        "rolling_returns",
        "Returns over the exact observation window",
        [
            AnalysisRow(
                label="Rolling return",
                instrument_handle=handle,
                at=item.at,
                cells=(_cell("rolling_return", item.value, unit="ratio"),),
            )
            for item in result.rolling_returns
        ],
    )
    return RecipePayload(
        metrics=tuple(claims),
        tables=(periods, rolling),
        warnings=result.warnings,
        unavailable_fields=tuple(unavailable),
        assumptions=(
            _assumption("periods_per_year", _periods(request), ModelScalarUnit.COUNT),
            _assumption("rolling_window", float(window), ModelScalarUnit.COUNT),
        ),
        verifies=result.verifies,
        does_not_verify=result.does_not_verify,
    )


def _risk(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    payload = _price(request, inputs)
    options = _options(request)
    series = inputs.series(_single_handle(request, inputs))
    returns = metrics.simple_returns([bar.close_value for bar in series.bars])
    claims = list(payload.metrics)
    assumptions = [
        *payload.assumptions,
        _assumption("confidence", options.confidence, ModelScalarUnit.RATIO),
        _assumption(
            "downside_target_period_return_assumption",
            options.downside_target_period_return_assumption,
            ModelScalarUnit.RATE,
        ),
    ]
    claims.extend(
        (
            RecipeMetric("historical_var", metrics.historical_var(returns, options.confidence)),
            RecipeMetric(
                "downside_deviation",
                metrics.downside_deviation(
                    returns, options.downside_target_period_return_assumption, _periods(request)
                ),
            ),
        )
    )
    unavailable = list(payload.unavailable_fields)
    try:
        claims.append(
            RecipeMetric(
                "expected_shortfall", metrics.expected_shortfall(returns, options.confidence)
            )
        )
    except metrics.FinancialMetricError:
        unavailable.append("expected_shortfall")
    if options.risk_free_period_return_assumption is None:
        unavailable.extend(("sharpe_ratio", "sortino_ratio"))
    else:
        rate = options.risk_free_period_return_assumption
        assumptions.append(
            _assumption("risk_free_period_return_assumption", rate, ModelScalarUnit.RATE)
        )
        for metric_id, calculator in (
            ("sharpe_ratio", metrics.sharpe_ratio),
            ("sortino_ratio", metrics.sortino_ratio),
        ):
            try:
                claims.append(RecipeMetric(metric_id, calculator(returns, rate, _periods(request))))
            except metrics.FinancialMetricError:
                unavailable.append(metric_id)
    tables = list(payload.tables)
    if options.benchmark_handle is not None:
        other = inputs.series(options.benchmark_handle)
        left = {
            (a.bar_time, b.bar_time): b.close_value / a.close_value - 1
            for a, b in zip(series.bars[:-1], series.bars[1:], strict=True)
        }
        right = {
            (a.bar_time, b.bar_time): b.close_value / a.close_value - 1
            for a, b in zip(other.bars[:-1], other.bars[1:], strict=True)
        }
        shared = tuple(sorted(left.keys() & right.keys()))
        if len(shared) < _MINIMUM_PAIR_OBSERVATIONS:
            unavailable.extend(("correlation", "covariance", "beta"))
        else:
            subject, benchmark = [left[key] for key in shared], [right[key] for key in shared]
            for metric_id, calculator in (
                ("correlation", metrics.correlation),
                ("covariance", metrics.covariance),
                ("beta", metrics.beta),
            ):
                try:
                    claims.append(RecipeMetric(metric_id, calculator(subject, benchmark)))
                except metrics.FinancialMetricError:
                    unavailable.append(metric_id)
            tables.append(
                _table(
                    "benchmark_alignment",
                    "Exact shared return periods",
                    [
                        AnalysisRow(
                            label="Shared period",
                            at=key[1],
                            cells=(
                                _cell("subject_return", left[key], unit="ratio"),
                                _cell("benchmark_return", right[key], unit="ratio"),
                            ),
                        )
                        for key in shared
                    ],
                )
            )
    return replace(
        payload,
        metrics=tuple(claims),
        tables=tuple(tables),
        assumptions=tuple(assumptions),
        unavailable_fields=tuple(unavailable),
    )


def _quote(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    handle = _single_handle(request, inputs)
    result = _checked(analyze_quote(inputs.quote(handle)))
    currency = _currency(inputs, handle)
    claims = [
        RecipeMetric("midpoint_price", result.midpoint, currency),
        RecipeMetric("spread", result.spread, currency),
    ]
    unavailable = [] if currency else ["quote_currency"]
    if result.delayed_by_minutes is not None:
        claims.append(RecipeMetric("quote_delay", float(result.delayed_by_minutes)))
    else:
        unavailable.append("quote_delay")
    table = _table(
        "quote",
        "Observed Saxo quote and its quality",
        [
            AnalysisRow(
                label="Quote",
                instrument_handle=handle,
                at=result.captured_at,
                cells=(
                    _cell("bid", result.bid, currency=currency),
                    _cell("ask", result.ask, currency=currency),
                    _cell("midpoint", result.midpoint, currency=currency),
                    _cell("spread_basis_points", result.spread_basis_points, unit="basis_points"),
                    _cell("freshness", result.freshness),
                    _cell("delayed_by_minutes", result.delayed_by_minutes, unit="minutes"),
                ),
            ),
        ],
    )
    return RecipePayload(
        metrics=tuple(claims),
        tables=(table,),
        warnings=result.warnings,
        unavailable_fields=tuple(unavailable),
        verifies=("The supplied entitled Saxo bid and ask at their capture time.",),
    )


def _reference(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    rows: list[AnalysisRow] = []
    for handle in _handles(request, inputs):
        reference = inputs.reference(handle)
        rows.append(
            AnalysisRow(
                label=reference.display_label,
                instrument_handle=handle,
                cells=(
                    _cell("symbol", reference.symbol),
                    _cell("asset_type", reference.asset_type),
                    _cell("exchange", reference.exchange),
                    _cell("state", reference.state.value),
                ),
            )
        )
    return RecipePayload(
        tables=(_table("instrument_reference", "Saxo instrument identity", rows),),
        verifies=("The authenticated reference identity for each opaque handle.",),
    )


def _dossier(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    handle = _single_handle(request, inputs)
    options = request.arguments.get("rolling_window", 20)
    if not isinstance(options, int) or isinstance(options, bool) or options < 1:
        raise AnalyticsExecutionError("rolling_window_invalid")
    quote = None
    try:
        quote = inputs.quote(handle)
    except AnalyticsExecutionError as error:
        if error.reason_code != "quote_missing_or_ambiguous":
            raise
    result = _checked(
        build_instrument_dossier(
            inputs.reference(handle),
            inputs.series(handle),
            quote_dataset=quote,
            rolling_window=options,
            periods_per_year=_periods(request),
        )
    )
    reference_payload = _reference(request, inputs)
    price_payload = _price(request, inputs)
    tables = [*reference_payload.tables, *price_payload.tables]
    unavailable = list(price_payload.unavailable_fields)
    claims = list(price_payload.metrics)
    warnings = set(result.warnings)
    if quote is not None and result.quote is not None:
        quoted = _quote(request, inputs)
        tables.extend(quoted.tables)
        claims.extend(quoted.metrics)
        unavailable.extend(quoted.unavailable_fields)
    else:
        unavailable.append("quote")
    return replace(
        price_payload,
        metrics=tuple(claims),
        tables=tuple(tables),
        unavailable_fields=tuple(unavailable),
        warnings=tuple(sorted(warnings)),
    )


def _indicators(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    options = _options(request)
    parameters = options.indicator_parameters or IndicatorParameters(
        moving_average_window=20,
        rsi_period=14,
        macd_fast_period=12,
        macd_slow_period=26,
        macd_signal_period=9,
        atr_period=14,
        bollinger_window=20,
        bollinger_width=2.0,
        momentum_lookback=10,
        periods_per_year=_periods(request),
        level_wing=2,
    )
    handle = _single_handle(request, inputs)
    series = inputs.series(handle)
    result = _checked(calculate_indicators(series, parameters))
    currency = _currency(inputs, handle)
    mapped = {
        "moving_average": result.moving_average,
        "rsi": result.rsi,
        "macd": result.macd_line,
        "bollinger_lower": result.bollinger_lower,
        "bollinger_middle": result.bollinger_middle,
        "bollinger_upper": result.bollinger_upper,
        "momentum": result.momentum,
        "realized_volatility": result.realized_volatility,
        "atr": result.atr,
        "volume": result.volume_total,
        "volume_weighted_price": result.volume_weighted_price,
    }
    priced = {
        "moving_average",
        "macd",
        "bollinger_lower",
        "bollinger_middle",
        "bollinger_upper",
        "atr",
        "volume_weighted_price",
    }
    claims = tuple(
        RecipeMetric(metric_id, value, currency if metric_id in priced else None)
        for metric_id, value in mapped.items()
        if value is not None
    )
    state = AnalysisRow(
        label="Descriptive indicator snapshot",
        instrument_handle=handle,
        at=series.bars[-1].bar_time,
        cells=(
            _cell("trend_state", result.trend_state),
            _cell("macd_signal", result.macd_signal),
            _cell("macd_histogram", result.macd_histogram),
        ),
    )
    levels = [
        AnalysisRow(
            label=kind, instrument_handle=handle, cells=(_cell("price", value, currency=currency),)
        )
        for kind, values in (
            ("support", result.support_levels),
            ("resistance", result.resistance_levels),
        )
        for value in values
    ]
    assumptions = tuple(
        _assumption(
            name,
            float(value),
            ModelScalarUnit.DIMENSIONLESS if name == "bollinger_width" else ModelScalarUnit.COUNT,
        )
        for name, value in parameters.model_dump().items()
    )
    return RecipePayload(
        metrics=claims,
        tables=(
            _table("indicator_state", "Descriptive indicators", (state,)),
            _table("price_levels", "Observed local support and resistance", levels),
        ),
        warnings=result.warnings,
        assumptions=assumptions,
        unavailable_fields=tuple(metric_id for metric_id, value in mapped.items() if value is None),
        verifies=("Descriptive formulas over the exact declared observation windows.",),
    )


def _universe(request: RecipeRequest, inputs: ResearchInputs) -> BoundedResearchUniverse:
    return BoundedResearchUniverse(
        scope="explicit",
        series=tuple(inputs.series(handle) for handle in _handles(request, inputs)),
    )


def _market(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    universe = _universe(request, inputs)
    result = _checked(analyze_bounded_market(universe, periods_per_year=_periods(request)))
    comparisons = [
        AnalysisRow(
            label="Selected instrument",
            instrument_handle=item.instrument_handle,
            cells=(
                _cell("price_return", item.price_return, unit="ratio"),
                _cell("latest_price_return", item.latest_price_return, unit="ratio"),
                _cell("annualized_volatility", item.annualized_volatility, unit="ratio"),
                _cell("maximum_drawdown", item.maximum_drawdown, unit="ratio"),
            ),
        )
        for item in result.comparisons
    ]
    pairs = [
        AnalysisRow(
            label="Shared-period pair",
            instrument_handle=item.left_handle,
            cells=(
                _cell("other_instrument_handle", item.right_handle),
                _cell("observation_count", item.observation_count, unit="count"),
                _cell("correlation", item.value, unit="ratio"),
            ),
        )
        for item in result.correlations
    ]
    scope = AnalysisRow(
        label=result.scope_statement,
        cells=(
            _cell("advancers", result.advancers, unit="count"),
            _cell("decliners", result.decliners, unit="count"),
            _cell("unchanged", result.unchanged, unit="count"),
            _cell("breadth", result.breadth, unit="ratio"),
            _cell(
                "average_absolute_correlation", result.average_absolute_correlation, unit="ratio"
            ),
            _cell("correlation_regime", result.correlation_regime),
            _cell("volatility_regime", result.volatility_regime),
        ),
    )
    unavailable: list[str] = []
    if result.average_absolute_correlation is None:
        unavailable.append("market_correlation_regime")
    volatility_values = [
        item.annualized_volatility
        for item in result.comparisons
        if item.annualized_volatility is not None
    ]
    tables = [
        _table("market_comparisons", "Selected Saxo instrument comparisons", comparisons),
        _table("market_correlations", "Exactly aligned return correlations", pairs),
        _table("market_scope", "Bounded universe breadth and regimes", (scope,)),
    ]
    if request.analysis_kind == "market_volatility_dispersion":
        if (
            len(volatility_values) != len(result.comparisons)
            or len(volatility_values) < _MINIMUM_PAIR_OBSERVATIONS
        ):
            unavailable.append("volatility_dispersion")
        else:
            tables.append(
                _table(
                    "volatility_dispersion",
                    "Cross-sectional annualized volatility",
                    (
                        AnalysisRow(
                            label="Selected universe",
                            cells=(
                                _cell("mean_volatility", fmean(volatility_values), unit="ratio"),
                                _cell(
                                    "population_standard_deviation",
                                    pstdev(volatility_values),
                                    unit="ratio",
                                ),
                                _cell("minimum_volatility", min(volatility_values), unit="ratio"),
                                _cell("maximum_volatility", max(volatility_values), unit="ratio"),
                            ),
                        ),
                    ),
                )
            )
    return RecipePayload(
        tables=tuple(tables),
        warnings=tuple(
            sorted(
                {
                    "bounded_universe_only",
                    *result.warnings,
                }
            )
        ),
        unavailable_fields=tuple(unavailable),
        assumptions=(_assumption("periods_per_year", _periods(request), ModelScalarUnit.COUNT),),
        verifies=("Only the explicit selected Saxo universe and shared return intervals.",),
        does_not_verify=("Whole-market breadth, forecasts or recommendations.",),
    )


def _session(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    universe = _universe(request, inputs)
    quotes: list[QuoteResearchDataset] = []
    for series in universe.series:
        try:
            quotes.append(inputs.quote(series.instrument_handle))
        except AnalyticsExecutionError as error:
            if error.reason_code != "quote_missing_or_ambiguous":
                raise
    result = _checked(
        prepare_bounded_session(universe, quotes=quotes, periods_per_year=_periods(request))
    )
    rows = [
        AnalysisRow(
            label="Session instrument",
            instrument_handle=item.instrument_handle,
            cells=(
                _cell("latest_price", item.latest_price),
                _cell("latest_price_return", item.latest_price_return, unit="ratio"),
                _cell("spread", item.spread),
                _cell("quote_freshness", item.quote_freshness),
            ),
        )
        for item in result.items
    ]
    unavailable = tuple("session_quote" for item in result.items if item.spread is None)
    return RecipePayload(
        tables=(_table("session_preparation", result.scope_statement, rows),),
        warnings=result.warnings,
        unavailable_fields=tuple(set(unavailable)),
        verifies=("A bounded preparation view; it creates no orders.",),
    )


def _conditions(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    material = request.arguments.get("conditions", ())
    if not isinstance(material, tuple | list):
        raise AnalyticsExecutionError("saved_conditions_invalid")
    try:
        conditions = tuple(
            SavedCondition.model_validate(value, strict=True)
            for value in cast("Sequence[object]", material)
        )
    except ValidationError as error:
        raise AnalyticsExecutionError("saved_conditions_invalid") from error
    universe = _universe(request, inputs)
    quotes = tuple(
        inputs.quote(condition.instrument_handle)
        for condition in conditions
        if condition.kind == "spread_below"
    )
    result = _checked(check_saved_conditions(conditions, universe, quotes=quotes))
    return RecipePayload(
        tables=(
            _table(
                "saved_conditions",
                "Fixed saved-condition evaluations",
                [
                    AnalysisRow(
                        label=check.condition_id,
                        instrument_handle=check.instrument_handle,
                        cells=(
                            _cell("kind", check.kind),
                            _cell("observed_value", check.observed_value),
                            _cell("threshold", check.threshold),
                            _cell("matched", check.matched),
                        ),
                    )
                    for check in result.checks
                ],
            ),
        ),
        warnings=result.warnings,
        verifies=("Only the caller-selected fixed condition catalog at the observed cutoff.",),
    )


def _microstructure(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    result = _quote(request, inputs)
    return replace(
        result,
        warnings=tuple(sorted({*result.warnings, "displayed_depth_source_unavailable"})),
        unavailable_fields=(
            *result.unavailable_fields,
            "market_depth",
            "depth_imbalance",
            "liquidity_score",
        ),
        does_not_verify=(
            "Displayed market depth, fill probability or liquidity away from the quote.",
        ),
    )


def _source_matches(row: Mapping[str, object], handle: str) -> bool:
    identity = row.get("Uic")
    asset_type = row.get("AssetType")
    return (
        isinstance(identity, int)
        and not isinstance(identity, bool)
        and isinstance(asset_type, str)
        and instrument_handle_for_saxo_identity(asset_type, identity) == handle
    )


def _cost_rows(inputs: ResearchInputs, handles: Sequence[str]) -> tuple[Mapping[str, object], ...]:
    values = inputs.source_rows("costs_v1")
    matched: list[Mapping[str, object]] = []
    for handle in handles:
        rows = [row for row in values if _source_matches(row, handle)]
        if len(rows) != 1:
            raise AnalyticsExecutionError("cost_scope_missing_or_ambiguous")
        matched.append(cast("Mapping[str, object]", rows[0]))
    return tuple(matched)


def _cost_values(row: Mapping[str, object]) -> tuple[float, str]:
    cost = row.get("Cost")
    if not isinstance(cost, Mapping):
        raise AnalyticsExecutionError("cost_values_unavailable", ("total_cost",))
    cost_mapping = cast("Mapping[str, object]", cost)
    side = cost_mapping.get("Long", cost_mapping)
    if not isinstance(side, Mapping):
        raise AnalyticsExecutionError("cost_values_unavailable", ("total_cost",))
    side_mapping = cast("Mapping[str, object]", side)
    total = number(side_mapping.get("TotalCost"), field_name="total_cost")
    currency = side_mapping.get("Currency", row.get("Currency", row.get("AccountCurrency")))
    if total < 0 or not isinstance(currency, str):
        raise AnalyticsExecutionError("cost_values_unavailable", ("total_cost", "cost_currency"))
    return total, currency


def _wrappers(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    handles = _handles(request, inputs)
    if len(handles) < _MINIMUM_PAIR_OBSERVATIONS:
        raise AnalyticsExecutionError("wrapper_comparison_insufficient")
    costs = _cost_rows(inputs, handles)
    basis: list[tuple[float, str, float]] = []
    totals: list[float] = []
    rows: list[AnalysisRow] = []
    for handle, cost in zip(handles, costs, strict=True):
        if cost.get("AssetType") != "Stock":
            raise AnalyticsExecutionError("wrapper_contract_multiplier_unavailable")
        amount = number(cost.get("Amount"), field_name="wrapper_amount")
        price = number(cost.get("Price"), field_name="wrapper_price")
        horizon = number(cost.get("HoldingPeriodInDays"), field_name="wrapper_horizon")
        if amount <= 0 or price <= 0 or horizon < 0:
            raise AnalyticsExecutionError("wrapper_basis_invalid")
        total, currency = _cost_values(cost)
        exposure = amount * price
        basis.append((exposure, currency, horizon))
        totals.append(total)
        rows.append(
            AnalysisRow(
                label="Saxo cost illustration",
                instrument_handle=handle,
                cells=(
                    _cell("total_cost", total, currency=currency),
                    _cell("cost_basis_points", total / exposure * 10_000, unit="basis_points"),
                    _cell("exposure", exposure, currency=currency),
                    _cell("horizon_days", horizon, unit="days"),
                ),
            )
        )
    if any(item != basis[0] for item in basis[1:]):
        raise AnalyticsExecutionError("wrapper_basis_mismatch")
    return RecipePayload(
        metrics=(RecipeMetric("wrapper_cost_difference", max(totals) - min(totals), basis[0][1]),),
        tables=(_table("wrapper_costs", "Equal-exposure Saxo aggregate cost illustrations", rows),),
        warnings=("cost_inputs_only", "wrapper_economic_exposure_equivalence_unverified"),
        unavailable_fields=(
            "wrapper_open_hold_close_cost_split",
            "wrapper_economic_exposure_equivalence",
        ),
        verifies=("The total illustrated cost for equal stock notional, currency and horizon.",),
        does_not_verify=("Economic equivalence of different instruments or wrappers.",),
    )


def _trading_conditions(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    handle = _single_handle(request, inputs)
    rows = [
        row
        for row in inputs.source_rows("reference_instrument_details_v1")
        if _source_matches(row, handle)
    ]
    if len(rows) != 1:
        raise AnalyticsExecutionError("instrument_details_missing_or_ambiguous")
    row = rows[0]
    exchange = row.get("Exchange")
    exchange_name = exchange.get("Name") if isinstance(exchange, Mapping) else None
    currencies = row.get("CurrencyCode")
    table = _table(
        "trading_conditions",
        "Authenticated Saxo instrument details",
        (
            AnalysisRow(
                label="Instrument details",
                instrument_handle=handle,
                cells=(
                    _cell("asset_type", cast("str", row["AssetType"])),
                    _cell("currency", cast("str | None", currencies)),
                    _cell("exchange_name", cast("str | None", exchange_name)),
                ),
            ),
        ),
    )
    claims: tuple[RecipeMetric, ...] = ()
    unavailable = ["minimum_trade_size", "tick_size", "margin_requirement"]
    if inputs.source_rows("costs_v1"):
        total, currency = _cost_values(_cost_rows(inputs, (handle,))[0])
        claims = (RecipeMetric("total_cost", total, currency),)
    else:
        unavailable.append("cost_illustration")
    return RecipePayload(
        metrics=claims,
        tables=(table,),
        unavailable_fields=tuple(unavailable),
        verifies=("Only fields actually present in authenticated reference details.",),
    )


def _fixed_income(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    options = _options(request)
    model = options.fixed_income_model
    if model is None:
        raise AnalyticsExecutionError("fixed_income_cashflow_model_required", ("cashflow_model",))
    handle = _single_handle(request, inputs)
    quoted = _checked(analyze_quote(inputs.quote(handle)))
    dirty_price = (
        quoted.midpoint * model.quote_price_scale_assumption + model.accrued_interest_assumption
    )
    result = _checked(
        analyze_fixed_income(
            FixedIncomeDataset(
                dataset_id=inputs.quote(handle).dataset_id,
                instrument_handle=handle,
                as_of=inputs.as_of,
                entitlement_state="available",
                dirty_price=dirty_price,
                cash_flows=model.cash_flows,
                compounding_frequency=model.compounding_frequency,
                day_count_basis=model.day_count_basis,
                settlement_at=quoted.captured_at,
            )
        )
    )
    assumptions = [
        _assumption(
            "quote_price_scale_assumption",
            model.quote_price_scale_assumption,
            ModelScalarUnit.DIMENSIONLESS,
        ),
        _assumption(
            "accrued_interest_assumption",
            model.accrued_interest_assumption,
            ModelScalarUnit.DIMENSIONLESS,
        ),
        _assumption(
            "compounding_frequency", float(model.compounding_frequency), ModelScalarUnit.COUNT
        ),
    ]
    for index, flow in enumerate(model.cash_flows):
        assumptions.extend(
            (
                _assumption(
                    f"cashflow_{index}_years_assumption",
                    flow.years_from_settlement,
                    ModelScalarUnit.YEARS,
                ),
                _assumption(
                    f"cashflow_{index}_amount_assumption",
                    flow.amount,
                    ModelScalarUnit.DIMENSIONLESS,
                ),
            )
        )
    table = _table(
        "fixed_income_model",
        "Explicit hypothetical cashflow model",
        [
            AnalysisRow(
                label="Assumed cashflow",
                cells=(
                    _cell("years_from_settlement", flow.years_from_settlement, unit="years"),
                    _cell("amount_assumption", flow.amount),
                    _cell("day_count_basis", model.day_count_basis),
                ),
            )
            for flow in model.cash_flows
        ],
    )
    return RecipePayload(
        metrics=tuple(
            RecipeMetric(metric_id, value, metric_class=MetricClass.MODEL_OUTPUT)
            for metric_id, value in (
                ("yield_to_maturity", result.yield_to_maturity),
                ("modified_duration", result.modified_duration),
                ("convexity", result.convexity),
            )
        ),
        tables=(table,),
        assumptions=tuple(assumptions),
        warnings=tuple(
            sorted(
                {*quoted.warnings, *result.warnings, "hypothetical_cashflows_not_saxo_bond_terms"}
            )
        ),
        unavailable_fields=(
            "observed_bond_cashflows",
            "bond_carry",
            "roll_down",
            "curve_shock_effect",
        ),
        verifies=(
            "Bond mathematics under explicit hypothetical cashflows and the observed quote.",
        ),
        does_not_verify=("Observed contractual bond cashflows, broker yield or future returns.",),
    )


def execute_recipe(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    """Execute the closed market family; no recipe has a broker or network capability."""
    _options(request)
    dispatch = {
        "instrument_resolution": _reference,
        "instrument_dossier": _dossier,
        "instrument_price_return": _price,
        "instrument_risk": _risk,
        "instrument_quote": _quote,
        "technical_indicators": _indicators,
        "instrument_price_volume": _indicators,
        "multi_instrument_comparison": _market,
        "market_comparison": _market,
        "market_correlation_regime": _market,
        "market_volatility_dispersion": _market,
        "session_cockpit": _session,
        "saved_condition_checks": _conditions,
        "market_microstructure": _microstructure,
        "wrapper_comparison": _wrappers,
        "trading_conditions": _trading_conditions,
        "fixed_income": _fixed_income,
    }
    executor = dispatch.get(request.analysis_kind)
    if executor is None:
        raise AnalyticsExecutionError("market_analysis_kind_unsupported")
    try:
        return executor(request, inputs)
    except (metrics.FinancialMetricError, ArithmeticError) as error:
        raise AnalyticsExecutionError("market_measure_undefined") from error
