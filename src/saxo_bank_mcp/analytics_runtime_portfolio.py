"""Portfolio recipes over authenticated Saxo material, with explicit missing dimensions."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp import analytics_metrics as metrics
from saxo_bank_mcp.analytics_costs import (
    CostBooking,
    CostComponent,
    CostDataset,
    analyze_cost_xray,
)
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_instruments import ResearchRefusal
from saxo_bank_mcp.analytics_liquidity import (
    CashBalance,
    LiquidityDataset,
    analyze_cash_and_settlement,
)
from saxo_bank_mcp.analytics_models import (
    AnalysisCell,
    AnalysisRow,
    AnalysisTable,
    InstrumentHandle,
    MetricClass,
    ModelScalarUnit,
    NamedModelAssumption,
    QualityState,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipeMetric,
    RecipePayload,
    RecipeRequest,
    ResearchInputs,
    number,
    utc_timestamp,
)
from saxo_bank_mcp.analytics_trade_review import (
    ClosedTrade,
    DecisionBarReference,
    TradeFill,
    TradeReviewDataset,
    analyze_trading_mirror,
)


class PortfolioOptions(BaseModel):
    """Caller choices only; source amounts and account identifiers are never accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    start_at: datetime | None = None
    end_at: datetime | None = None
    periods_per_year: float = Field(default=252.0, gt=0, le=100_000, allow_inf_nan=False)
    confidence: float = Field(default=0.95, gt=0, lt=1, allow_inf_nan=False)
    risk_free_period_return: float | None = Field(default=None, allow_inf_nan=False)
    target_period_return: float | None = Field(default=None, allow_inf_nan=False)
    benchmark_handle: InstrumentHandle | None = None
    intervention: Literal["do_nothing"] = "do_nothing"
    cost_report_basis: Literal["ex_post", "ex_ante"] = "ex_post"
    tax_lot_export_mode: Literal["trade_activity", "authoritative_tax_lots"] = "trade_activity"

    @model_validator(mode="after")
    def validate_period(self) -> PortfolioOptions:
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("portfolio period requires both UTC boundaries")
        for value in (self.start_at, self.end_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
            ):
                raise ValueError("portfolio period must use UTC")
        if self.start_at is not None and self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("portfolio period must be ordered")
        return self


SUPPORTED_KINDS = frozenset(
    {
        "portfolio_overview",
        "cash_and_settlement",
        "portfolio_margin",
        "portfolio_exposure",
        "portfolio_performance",
        "portfolio_risk",
        "portfolio_attribution",
        "portfolio_comparison",
        "portfolio_time_machine",
        "income_calendar",
        "corporate_action_center",
        "cost_xray",
        "regulatory_cost_report",
        "trading_mirror",
        "execution_quality",
        "portfolio_query",
        "tax_lot_export",
    },
)
REQUIRED_METRICS: dict[str, tuple[str, ...]] = {
    "portfolio_overview": ("account_value", "cash_balance"),
    "cash_and_settlement": ("settled_cash",),
    "portfolio_margin": ("margin_headroom",),
    "portfolio_exposure": ("gross_exposure", "net_exposure"),
    "portfolio_performance": ("time_weighted_return",),
    "portfolio_risk": ("volatility", "maximum_drawdown"),
    "portfolio_attribution": ("local_asset_contribution",),
    "portfolio_comparison": ("active_return",),
    "portfolio_time_machine": ("do_nothing_counterfactual",),
    "income_calendar": ("income_amount",),
    "corporate_action_center": (),
    "cost_xray": ("total_cost",),
    "regulatory_cost_report": (),
    "trading_mirror": ("win_rate",),
    "execution_quality": (),
    "portfolio_query": (),
    "tax_lot_export": (),
}
SOURCE_CONTRACTS: dict[str, tuple[str, ...]] = {
    "portfolio_overview": ("balances_v1", "positions_v1", "orders_v1"),
    "cash_and_settlement": ("balances_v1",),
    "portfolio_margin": ("balances_v1",),
    "portfolio_exposure": ("positions_v1", "balances_v1"),
    "portfolio_performance": ("performance_timeseries_v4", "balances_v1"),
    "portfolio_risk": ("performance_timeseries_v4",),
    "portfolio_attribution": ("positions_v1", "balances_v1"),
    "portfolio_comparison": (
        "performance_timeseries_v4",
        "chart_v3",
        "reference_instrument_details_v1",
        "balances_v1",
    ),
    "portfolio_time_machine": ("balances_v1", "positions_v1", "chart_v3"),
    "income_calendar": ("bookings_v1", "balances_v1"),
    "corporate_action_center": ("corporate_action_events_v2", "corporate_action_holdings_v2"),
    "cost_xray": ("bookings_v1", "balances_v1"),
    "regulatory_cost_report": ("bookings_v1", "balances_v1", "costs_v1"),
    "trading_mirror": (
        "closed_positions_history_v1",
        "bookings_v1",
        "balances_v1",
        "reference_instruments_v1",
    ),
    "execution_quality": ("closed_positions_history_v1", "balances_v1", "reference_instruments_v1"),
    "portfolio_query": (),
    "tax_lot_export": ("closed_positions_history_v1", "reference_instruments_v1"),
}

METRIC_IDS: dict[str, tuple[str, ...]] = {
    "portfolio_overview": (
        "account_value",
        "cash_balance",
        "position_count",
        "working_order_count",
    ),
    "cash_and_settlement": (
        "cash_balance",
        "unsettled_cash",
        "settled_cash",
        "settlement_obligation",
        "buying_power",
        "margin_headroom",
    ),
    "portfolio_margin": ("margin_headroom", "margin_utilization"),
    "portfolio_exposure": ("long_exposure", "short_exposure", "gross_exposure", "net_exposure"),
    "portfolio_performance": ("time_weighted_return", "account_value", "xirr", "cagr"),
    "portfolio_risk": (
        "volatility",
        "maximum_drawdown",
        "historical_var",
        "expected_shortfall",
        "sharpe_ratio",
        "sortino_ratio",
        "downside_deviation",
    ),
    "portfolio_attribution": ("local_asset_contribution", "currency_contribution"),
    "portfolio_comparison": ("active_return", "tracking_error"),
    "portfolio_time_machine": ("do_nothing_counterfactual", "account_value"),
    "income_calendar": ("income_amount", "dividend_amount"),
    "corporate_action_center": (),
    "cost_xray": (
        "total_cost",
        "commission_cost",
        "spread_cost",
        "fx_conversion_cost",
        "financing_cost",
        "borrow_cost",
        "custody_cost",
        "tax_cost",
    ),
    "regulatory_cost_report": ("regulatory_ex_ante_cost", "regulatory_ex_post_cost"),
    "trading_mirror": ("win_rate",),
    "execution_quality": (),
    "tax_lot_export": (),
}
METRIC_IDS["portfolio_query"] = tuple(
    sorted({metric for values in METRIC_IDS.values() for metric in values})
)

_DATE_LENGTH = 10
_MINIMUM_POINTS = 2
_CURRENCY_LENGTH = 3

type SourceRow = Mapping[str, object]


def execute_recipe(request: RecipeRequest, inputs: ResearchInputs) -> RecipePayload:
    """Execute one allowlisted portfolio recipe without network access or source replacement."""
    if request.analysis_kind not in SUPPORTED_KINDS:
        raise AnalyticsExecutionError("portfolio_recipe_unavailable")
    try:
        options = PortfolioOptions.model_validate(request.arguments.get("options") or {})
    except ValidationError as error:
        raise AnalyticsExecutionError("portfolio_options_invalid") from error
    if any(
        material.dataset.quality_state
        in {QualityState.MISSING, QualityState.INVALID, QualityState.STALE}
        for material in inputs.materials
    ):
        raise AnalyticsExecutionError("portfolio_source_data_unusable")
    if inputs.account_scope == "aggregate":
        raise AnalyticsExecutionError("portfolio_account_scope_required")
    handlers: dict[
        str, Callable[[RecipeRequest, ResearchInputs, PortfolioOptions], RecipePayload]
    ] = {
        "portfolio_overview": _overview,
        "cash_and_settlement": _cash,
        "portfolio_margin": _margin,
        "portfolio_exposure": _exposure,
        "portfolio_performance": _performance,
        "portfolio_risk": _risk,
        "portfolio_attribution": _attribution,
        "portfolio_comparison": _comparison,
        "portfolio_time_machine": _time_machine,
        "income_calendar": _income,
        "corporate_action_center": _corporate_actions,
        "cost_xray": _costs,
        "regulatory_cost_report": _regulatory_costs,
        "trading_mirror": _mirror,
        "execution_quality": _execution_quality,
        "portfolio_query": _query,
        "tax_lot_export": _tax_lots,
    }
    try:
        return handlers[request.analysis_kind](request, inputs, options)
    except metrics.FinancialMetricError as error:
        raise AnalyticsExecutionError("portfolio_metric_undefined") from error


def _decimal(row: SourceRow, field: str) -> Decimal:
    value = row.get(field)
    number(value, field_name=field)
    return Decimal(str(value))


def _optional(row: SourceRow, field: str) -> Decimal | None:
    return None if row.get(field) is None else _decimal(row, field)


def _object(row: SourceRow, field: str) -> SourceRow:
    value = row.get(field)
    if not isinstance(value, Mapping):
        raise AnalyticsExecutionError("source_object_field_unavailable", (field,))
    return cast("SourceRow", value)


def _text(row: SourceRow, field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise AnalyticsExecutionError("source_text_field_unavailable", (field,))
    return value


def _at(value: object, field: str) -> datetime:
    if isinstance(value, str) and len(value) == _DATE_LENGTH:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        except ValueError as error:
            raise AnalyticsExecutionError("source_date_unavailable", (field,)) from error
    if (
        isinstance(value, str)
        and len(value) >= _DATE_LENGTH
        and value[_DATE_LENGTH:]
        in {
            "T00:00:00",
            "T00:00:00.000",
        }
        and field in {"TradeDateOpen", "TradeDateClose", "Date"}
    ):
        return _at(value[:_DATE_LENGTH], field)
    return utc_timestamp(value, field_name=field)


def _single(inputs: ResearchInputs, contract: str) -> SourceRow:
    rows = inputs.source_rows(contract)
    if len(rows) != 1:
        raise AnalyticsExecutionError("portfolio_source_scope_ambiguous", (contract,))
    return rows[0]


def _currency(inputs: ResearchInputs) -> str:
    rows = inputs.source_rows("balances_v1")
    currencies = {_text(row, "Currency") for row in rows}
    if len(currencies) != 1:
        raise AnalyticsExecutionError("portfolio_reporting_currency_unavailable", ("Currency",))
    currency = next(iter(currencies))
    if len(currency) != _CURRENCY_LENGTH or currency == "XXX":
        raise AnalyticsExecutionError("portfolio_reporting_currency_unavailable", ("Currency",))
    return currency


def _metric(metric_id: str, value: Decimal | float, currency: str | None = None) -> RecipeMetric:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise AnalyticsExecutionError("portfolio_numeric_result_unrepresentable")
    return RecipeMetric(metric_id, numeric, currency)


def _row(
    label: str,
    values: Mapping[str, str | float | int | bool | None],
    *,
    handle: str | None = None,
    at: datetime | None = None,
    currency: str | None = None,
) -> AnalysisRow:
    dimensions = {
        "quantity": "quantity",
        "holding_quantity": "quantity",
        "holding_calendar_days": "days",
        "accumulated_return": "ratio",
        "return": "ratio",
        "portfolio": "ratio",
        "proxy": "ratio",
        "opening_price": "price",
        "weighted_opening_price": "price",
        "entry_vwap": "price",
        "exit_vwap": "price",
    }
    return AnalysisRow(
        label=label,
        instrument_handle=handle,
        at=at,
        cells=tuple(
            AnalysisCell(
                field=field,
                value=value,
                unit=dimensions.get(field, "currency" if currency is not None else None),
                currency=None
                if field in {"quantity", "holding_quantity", "holding_calendar_days"}
                else currency,
            )
            for field, value in values.items()
        ),
    )


def _table(table_id: str, title: str, rows: Sequence[AnalysisRow]) -> AnalysisTable:
    return AnalysisTable(table_id=table_id, title=title, rows=tuple(rows))


def _handle(row: SourceRow) -> str:
    identifier = row.get("Uic")
    if type(identifier) is not int:
        raise AnalyticsExecutionError("portfolio_instrument_identity_unavailable", ("Uic",))
    return instrument_handle_for_saxo_identity(_text(row, "AssetType"), identifier)


def _fingerprint(scope: str, key: str) -> str:
    return hashlib.sha256((scope + "\0" + key).encode()).hexdigest()


def _latest(rows: Sequence[SourceRow], key: str) -> tuple[SourceRow, ...]:
    """Deduplicate identical source rows; conflicting revisions need an explicit source revision."""
    by_key: dict[str, SourceRow] = {}
    for row in rows:
        identity = _text(row, key)
        if identity in by_key and by_key[identity] != row:
            raise AnalyticsExecutionError("portfolio_source_revision_ambiguous", (key,))
        by_key[identity] = row
    return tuple(by_key.values())


def _overview(
    _request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions
) -> RecipePayload:
    balance = _single(inputs, "balances_v1")
    currency = _currency(inputs)
    positions = _latest(inputs.source_rows("positions_v1"), "PositionId")
    orders = _latest(inputs.source_rows("orders_v1"), "OrderId")
    rows: list[AnalysisRow] = []
    missing: set[str] = set()
    for position in positions:
        base = _object(position, "PositionBase")
        view = _object(position, "PositionView")
        values: dict[str, float | None] = {}
        for field in (
            "MarketValueInBaseCurrency",
            "ProfitLossOnTradeInBaseCurrency",
            "ExposureInBaseCurrency",
        ):
            value = _optional(view, field)
            if value is None:
                missing.add(field)
            values[field.lower()] = float(value) if value is not None else None
        rows.append(_row("Position", values, handle=_handle(base), currency=currency))
    return RecipePayload(
        metrics=(
            _metric("account_value", _decimal(balance, "TotalValue"), currency),
            _metric("cash_balance", _decimal(balance, "CashBalance"), currency),
            _metric("position_count", len(positions)),
            _metric(
                "working_order_count",
                sum(order.get("Status") in {"Working", "Pending"} for order in orders),
            ),
        ),
        tables=(_table("portfolio_positions", "Current positions", rows),),
        unavailable_fields=tuple(sorted(missing)),
        warnings=("position_dimensions_incomplete",) if missing else (),
        verifies=("Current account value and source-reported position dimensions.",),
    )


def _cash(_request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions) -> RecipePayload:
    balance = _single(inputs, "balances_v1")
    currency = _currency(inputs)
    missing = tuple(
        field
        for field in ("FundsAvailableForSettlement", "SpendingPower", "MarginAvailableForTrading")
        if balance.get(field) is None
    )
    dataset = LiquidityDataset(
        dataset_id=inputs.materials[0].dataset.dataset_id,
        account_alias=inputs.account_scope,
        as_of=inputs.as_of,
        reporting_currency=currency,
        balances=(
            CashBalance(
                account_alias=inputs.account_scope,
                currency=currency,
                cash_balance=_decimal(balance, "CashBalance"),
                transactions_not_booked=_decimal(balance, "TransactionsNotBooked"),
                funds_reserved_for_settlement=_decimal(balance, "FundsReservedForSettlement"),
                funds_available_for_settlement=_optional(balance, "FundsAvailableForSettlement"),
                spending_power=_optional(balance, "SpendingPower"),
                margin_available_for_trading=_optional(balance, "MarginAvailableForTrading"),
            ),
        ),
        fx_quotes=(),
        source_bindings=inputs.bindings(),
        quality_state=QualityState.PARTIAL if missing else QualityState.COMPLETE,
        missing_fields=missing,
        warnings=(),
    )
    result = analyze_cash_and_settlement(
        dataset, visibility=VisibilityMode.PRIVATE_USER_RESULT, trusted_local_host=True
    )
    if isinstance(result, ResearchRefusal):
        raise AnalyticsExecutionError(result.reason_code, result.missing_fields)
    values = result.private_values
    assert values is not None  # noqa: S101 - domain private delivery invariant
    output = [
        _metric("cash_balance", values.cash_balance, currency),
        _metric("unsettled_cash", values.transactions_not_booked, currency),
        _metric("settled_cash", values.projected_settled_cash, currency),
        _metric("settlement_obligation", values.funds_reserved_for_settlement, currency),
    ]
    for metric_id, value in (
        ("buying_power", values.spending_power),
        ("margin_headroom", values.margin_available_for_trading),
    ):
        if value is not None:
            output.append(_metric(metric_id, value, currency))
    return RecipePayload(
        metrics=tuple(output),
        warnings=result.warnings,
        unavailable_fields=(*missing, "liquidity_score"),
        verifies=("Cash settlement identity in the account currency.",),
    )


def _margin(_request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions) -> RecipePayload:
    balance = _single(inputs, "balances_v1")
    if balance.get("CalculationReliability") not in {None, "Ok"}:
        raise AnalyticsExecutionError("portfolio_margin_calculation_unreliable")
    currency = _currency(inputs)
    output = [_metric("margin_headroom", _decimal(balance, "MarginAvailableForTrading"), currency)]
    utilization = _optional(balance, "MarginUtilizationPct")
    if utilization is not None:
        output.append(_metric("margin_utilization", utilization / 100))
    rows = [
        _row(
            "Current margin",
            {
                "used": float(_decimal(balance, "MarginUsedByCurrentPositions")),
                "equity": float(_decimal(balance, "NetEquityForMargin")),
            },
            currency=currency,
        )
    ]
    initial = balance.get("InitialMargin")
    if isinstance(initial, Mapping):
        initial = cast("SourceRow", initial)
        rows.append(
            _row(
                "Initial margin",
                {
                    field.lower(): float(_decimal(initial, field))
                    for field in (
                        "MarginAvailable",
                        "MarginUsedByCurrentPositions",
                        "NetEquityForMargin",
                    )
                    if initial.get(field) is not None
                },
                currency=currency,
            )
        )
    missing = ["distance_to_liquidation_approximation"]
    if utilization is None:
        missing.append("MarginUtilizationPct")
    return RecipePayload(
        metrics=tuple(output),
        tables=(_table("margin", "Broker margin", rows),),
        warnings=("liquidation_threshold_unavailable",),
        unavailable_fields=tuple(missing),
        verifies=("Broker-reported current margin headroom and utilization.",),
    )


def _exposure(
    _request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions
) -> RecipePayload:
    positions = _latest(inputs.source_rows("positions_v1"), "PositionId")
    if not positions:
        raise AnalyticsExecutionError("portfolio_positions_unavailable")
    currency = _currency(inputs)
    signed: list[Decimal] = []
    rows: list[AnalysisRow] = []
    for position in positions:
        base = _object(position, "PositionBase")
        view = _object(position, "PositionView")
        value = _decimal(view, "ExposureInBaseCurrency")
        signed.append(value)
        rows.append(
            _row(
                "Position exposure",
                {"exposure": float(value)},
                handle=_handle(base),
                currency=currency,
            )
        )
    long = sum((value for value in signed if value > 0), Decimal(0))
    short = -sum((value for value in signed if value < 0), Decimal(0))
    return RecipePayload(
        metrics=tuple(
            _metric(name, value, currency)
            for name, value in (
                ("long_exposure", long),
                ("short_exposure", short),
                ("gross_exposure", long + short),
                ("net_exposure", long - short),
            )
        ),
        tables=(_table("exposure", "Broker-reported exposure", rows),),
        warnings=("broker_reported_exposure_not_delta_recalculated",),
        unavailable_fields=("delta_equivalent_options_exposure",),
        verifies=("Signed broker exposure aggregation in the account currency.",),
    )


def _points(
    row: SourceRow, field: str, options: PortfolioOptions
) -> tuple[tuple[datetime, float], ...]:
    raw = row.get(field)
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise AnalyticsExecutionError("portfolio_series_unavailable", (field,))
    points: dict[datetime, float] = {}
    for untyped in cast("Sequence[object]", raw):
        if not isinstance(untyped, Mapping):
            raise AnalyticsExecutionError("portfolio_series_invalid", (field,))
        value = cast("SourceRow", untyped)
        at = _at(value.get("Date"), "Date")
        if (
            options.start_at is not None
            and options.end_at is not None
            and (at < options.start_at or at > options.end_at)
        ):
            continue
        observed = number(value.get("Value"), field_name="Value")
        if at in points and points[at] != observed:
            raise AnalyticsExecutionError("portfolio_series_revision_ambiguous", (field,))
        points[at] = observed
    return tuple(sorted(points.items()))


def _reject_cashflow_basis(reason_code: str) -> None:
    raise AnalyticsExecutionError(reason_code)


def _return_series(
    inputs: ResearchInputs, options: PortfolioOptions
) -> tuple[tuple[datetime, float], ...]:
    source = _single(inputs, "performance_timeseries_v4")
    points = _points(_object(source, "TimeWeighted"), "Accumulated", options)
    if len(points) < _MINIMUM_POINTS or any(at > inputs.as_of for at, _ in points):
        raise AnalyticsExecutionError(
            "portfolio_return_series_incomplete", ("TimeWeighted.Accumulated",)
        )
    if (
        options.start_at is not None
        and options.end_at is not None
        and (
            points[0][0].date() != options.start_at.date()
            or points[-1][0].date() != options.end_at.date()
        )
    ):
        raise AnalyticsExecutionError("portfolio_requested_period_incomplete")
    return points


def _index_returns(growth: Sequence[float]) -> list[float]:
    """Use the domain's return-index calculation, including a terminal total loss."""
    return [metrics.time_weighted_return((left, right), (0.0,)) for left, right in pairwise(growth)]


def _performance(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    points = _return_series(inputs, options)
    growth = [1 + value for _, value in points]
    returns = _index_returns(growth)
    total = metrics.cumulative_return(returns)
    output = [_metric("time_weighted_return", total)]
    rows = [
        _row("Broker cumulative return", {"accumulated_return": value}, at=at)
        for at, value in points
    ]
    unavailable: list[str] = []
    source = _single(inputs, "performance_timeseries_v4")
    balance = _object(source, "Balance")
    values = _points(balance, "AccountValue", options)
    if len(values) < _MINIMUM_POINTS or tuple(at for at, _ in values) != tuple(
        at for at, _ in points
    ):
        unavailable.extend(("money_weighted_return", "xirr", "account_value"))
    else:
        currency = _currency(inputs)
        output.append(_metric("account_value", values[-1][1], currency))
        try:
            transfers = _points(balance, "CashTransfer", options)
            securities = (
                _points(balance, "SecurityTransfer", options)
                if balance.get("SecurityTransfer")
                else ()
            )
            if securities:
                _reject_cashflow_basis("security_transfer_cashflow_basis_unavailable")
            start, end = values[0][0], values[-1][0]
            if any(at <= start or at > end for at, _ in transfers):
                _reject_cashflow_basis("portfolio_cashflow_boundary_ambiguous")
            flow_values = [-values[0][1], *(-value for _, value in transfers), values[-1][1]]
            flow_dates = [start.date(), *(at.date() for at, _ in transfers), end.date()]
            xirr = metrics.xirr(flow_values, flow_dates)
            output.append(_metric("xirr", xirr))
        except (AnalyticsExecutionError, metrics.FinancialMetricError):
            unavailable.extend(("money_weighted_return", "xirr"))
    elapsed_years = (points[-1][0] - points[0][0]).total_seconds() / (365 * 24 * 3600)
    try:
        output.append(_metric("cagr", metrics.cagr(growth[0], growth[-1], elapsed_years)))
    except metrics.FinancialMetricError:
        unavailable.append("cagr")
    return RecipePayload(
        metrics=tuple(output),
        tables=(_table("portfolio_returns", "Broker performance", rows),),
        warnings=("performance_dimensions_unavailable",) if unavailable else (),
        unavailable_fields=tuple(sorted(set(unavailable))),
        verifies=("Broker time-weighted growth over the exact stored period.",),
        does_not_verify=(
            "Independent reconstruction of broker performance accounting.",
            "Future returns.",
        ),
    )


def _risk(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    points = _return_series(inputs, options)
    growth = [1 + value for _, value in points]
    returns = _index_returns(growth)
    output = [
        _metric("volatility", metrics.volatility(returns, options.periods_per_year)),
        _metric("maximum_drawdown", metrics.maximum_drawdown(growth)),
        _metric("historical_var", metrics.historical_var(returns, options.confidence)),
    ]
    missing = ["component_risk_contribution", "marginal_risk_contribution"]
    try:
        output.append(
            _metric("expected_shortfall", metrics.expected_shortfall(returns, options.confidence))
        )
    except metrics.FinancialMetricError:
        missing.append("expected_shortfall")
    if options.risk_free_period_return is None:
        missing.append("sharpe_ratio")
    else:
        try:
            output.append(
                _metric(
                    "sharpe_ratio",
                    metrics.sharpe_ratio(
                        returns, options.risk_free_period_return, options.periods_per_year
                    ),
                )
            )
        except metrics.FinancialMetricError:
            missing.append("sharpe_ratio")
    if options.target_period_return is None:
        missing.extend(("sortino_ratio", "downside_deviation"))
    else:
        output.append(
            _metric(
                "downside_deviation",
                metrics.downside_deviation(
                    returns, options.target_period_return, options.periods_per_year
                ),
            )
        )
        try:
            output.append(
                _metric(
                    "sortino_ratio",
                    metrics.sortino_ratio(
                        returns, options.target_period_return, options.periods_per_year
                    ),
                )
            )
        except metrics.FinancialMetricError:
            missing.append("sortino_ratio")
    return RecipePayload(
        metrics=tuple(output),
        tables=(
            _table(
                "risk_returns",
                "Eligible periodic portfolio returns",
                [
                    _row("Periodic return", {"return": float(value)}, at=point[0])
                    for point, value in zip(points[1:], returns, strict=True)
                ],
            ),
        ),
        unavailable_fields=tuple(sorted(set(missing))),
        warnings=("risk_dimensions_unavailable",),
        assumptions=(
            NamedModelAssumption(
                name="periods_per_year", value=options.periods_per_year, unit=ModelScalarUnit.RATIO
            ),
            NamedModelAssumption(
                name="confidence", value=options.confidence, unit=ModelScalarUnit.RATIO
            ),
        ),
        verifies=("Historical statistics of broker flow-neutral portfolio returns.",),
    )


def _attribution(
    _request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions
) -> RecipePayload:
    """Decompose source-reported open-position P&L without asserting period attribution."""
    currency = _currency(inputs)
    positions = _latest(inputs.source_rows("positions_v1"), "PositionId")
    if not positions:
        raise AnalyticsExecutionError("portfolio_positions_unavailable")
    local = Decimal(0)
    fx = Decimal(0)
    opening = Decimal(0)
    rows: list[AnalysisRow] = []
    for position in positions:
        base = _object(position, "PositionBase")
        view = _object(position, "PositionView")
        trade = _decimal(view, "ProfitLossOnTradeInBaseCurrency")
        conversion = _decimal(view, "ProfitLossCurrencyConversion")
        basis = _decimal(view, "MarketValueOpenInBaseCurrency")
        local += trade
        fx += conversion
        opening += abs(basis)
        rows.append(
            _row(
                "Open-position P&L",
                {"local_market_pnl": float(trade), "currency_pnl": float(conversion)},
                handle=_handle(base),
                currency=currency,
            )
        )
    if opening <= 0:
        raise AnalyticsExecutionError("portfolio_attribution_opening_basis_unavailable")
    return RecipePayload(
        metrics=(
            _metric("local_asset_contribution", 100 * local / opening),
            _metric("currency_contribution", 100 * fx / opening),
        ),
        tables=(_table("open_position_attribution", "Open-position P&L decomposition", rows),),
        warnings=("open_position_lifetime_attribution_only",),
        unavailable_fields=(
            "position_contribution",
            "income_contribution",
            "cost_drag",
            "allocation_attribution",
            "selection_attribution",
        ),
        verifies=(
            "Source-reported local and currency P&L relative to gross opening position value.",
        ),
        does_not_verify=("Whole-period portfolio return attribution.", "Future returns."),
    )


def _comparison(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    if options.benchmark_handle is None:
        raise AnalyticsExecutionError("portfolio_benchmark_choice_required", ("benchmark_handle",))
    points = _return_series(inputs, options)
    series = inputs.series(options.benchmark_handle)
    reference = [
        row
        for row in inputs.source_rows("reference_instrument_details_v1")
        if _handle(row) == options.benchmark_handle
    ]
    if len(reference) != 1 or reference[0].get("CurrencyCode") != _currency(inputs):
        raise AnalyticsExecutionError("portfolio_benchmark_currency_basis_unavailable")
    bars = {bar.bar_time.date(): bar for bar in series.bars}
    prices: list[float] = []
    for at, _ in points:
        bar = bars.get(at.date())
        if bar is None or bar.bar_time > inputs.as_of:
            raise AnalyticsExecutionError("portfolio_benchmark_calendar_mismatch")
        prices.append(number(bar.close_value, field_name="close"))
    portfolio_returns = _index_returns([1 + value for _, value in points])
    benchmark_returns = metrics.simple_returns(prices)
    portfolio_total = metrics.cumulative_return(portfolio_returns)
    benchmark_total = metrics.cumulative_return(benchmark_returns)
    return RecipePayload(
        metrics=(
            _metric("active_return", metrics.active_return(portfolio_total, benchmark_total)),
            _metric(
                "tracking_error",
                metrics.tracking_error(
                    portfolio_returns, benchmark_returns, options.periods_per_year
                ),
            ),
        ),
        tables=(
            _table(
                "comparison",
                "Aligned portfolio and proxy returns",
                [
                    _row(
                        "Aligned return",
                        {"portfolio": float(left), "proxy": float(right)},
                        at=point[0],
                    )
                    for point, left, right in zip(
                        points[1:], portfolio_returns, benchmark_returns, strict=True
                    )
                ],
            ),
        ),
        warnings=("benchmark_proxy_price_return_not_official",),
        unavailable_fields=("benchmark_total_return",),
        verifies=("Aligned broker portfolio return against the selected price-return proxy.",),
    )


def _time_machine(  # noqa: C901 - exact product, currency and date cutoffs
    _request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions
) -> RecipePayload:
    snapshots: list[tuple[datetime, SourceRow, list[SourceRow]]] = []
    for material in inputs.materials:
        balances = [
            row
            for page in material.pages
            if page.contract_name == "balances_v1"
            for row in cast("Sequence[SourceRow]", page.payload.get("rows", ()))
        ]
        positions = [
            row
            for page in material.pages
            if page.contract_name == "positions_v1"
            for row in cast("Sequence[SourceRow]", page.payload.get("rows", ()))
        ]
        if len(balances) == 1:
            snapshots.append((material.dataset.created_at, balances[0], positions))
    snapshots.sort(key=lambda item: item[0])
    if len(snapshots) < _MINIMUM_POINTS or snapshots[0][0] == snapshots[-1][0]:
        raise AnalyticsExecutionError("portfolio_time_machine_snapshots_unavailable")
    start, balance, positions = snapshots[0]
    end, actual, _ = snapshots[-1]
    currency = _text(balance, "Currency")
    if _text(actual, "Currency") != currency:
        raise AnalyticsExecutionError("portfolio_time_machine_currency_mismatch")
    value = _decimal(balance, "TotalValue")
    rows: list[AnalysisRow] = []
    for position in positions:
        base = _object(position, "PositionBase")
        view = _object(position, "PositionView")
        if _text(base, "AssetType") != "Stock":
            raise AnalyticsExecutionError("portfolio_time_machine_product_basis_unavailable")
        if view.get("ExposureCurrency") != currency:
            raise AnalyticsExecutionError("portfolio_time_machine_historical_fx_unavailable")
        handle = _handle(base)
        bars = inputs.series(handle).bars
        initial = [bar for bar in bars if bar.bar_time <= start]
        terminal = [bar for bar in bars if bar.bar_time <= end]
        if not initial or not terminal:
            raise AnalyticsExecutionError("portfolio_time_machine_price_coverage_unavailable")
        first = max(initial, key=lambda bar: bar.bar_time)
        last = max(terminal, key=lambda bar: bar.bar_time)
        if first.bar_time.date() != start.date() or last.bar_time.date() != end.date():
            raise AnalyticsExecutionError("portfolio_time_machine_price_cutoff_mismatch")
        quantity = _decimal(base, "Amount")
        start_price = _decimal(view, "CurrentPrice")
        if start_price != Decimal(str(first.close_value)):
            raise AnalyticsExecutionError("portfolio_time_machine_initial_price_mismatch")
        change = quantity * (Decimal(str(last.close_value)) - start_price)
        value += change
        rows.append(
            _row(
                "Held baseline position",
                {"quantity": float(quantity), "price_change_pnl": float(change)},
                handle=handle,
                currency=currency,
            )
        )
    if inputs.source_rows("corporate_action_events_v2"):
        raise AnalyticsExecutionError("portfolio_time_machine_corporate_action_basis_unavailable")
    return RecipePayload(
        metrics=(
            RecipeMetric(
                "do_nothing_counterfactual",
                float(_decimal(actual, "TotalValue") - value),
                currency,
                MetricClass.APPROXIMATION,
            ),
            _metric("account_value", _decimal(actual, "TotalValue"), currency),
        ),
        tables=(
            _table("time_machine", "Held-baseline counterfactual", rows),
            _table(
                "counterfactual_ending_value",
                "Frozen-path ending value",
                [
                    _row(
                        "Frozen holdings and cash",
                        {"ending_value": float(value)},
                        currency=currency,
                    )
                ],
            ),
        ),
        warnings=("counterfactual_constant_cash_no_income_or_financing",),
        unavailable_fields=(
            "counterfactual_income",
            "counterfactual_financing",
            "counterfactual_fx",
        ),
        verifies=("Same-currency stock repricing with baseline cash held constant.",),
        does_not_verify=("An investable total-return counterfactual.", "Future returns."),
    )


@dataclass(frozen=True, slots=True)
class _Booking:
    key: str
    at: datetime
    amount: Decimal
    category: str
    source: SourceRow


def _category(row: SourceRow) -> str:
    labels = " ".join(
        str(row.get(field) or "")
        for field in (
            "BkAmountType",
            "AmountClass",
            "AmountSubClass",
            "CostClass",
            "CostSubClass",
        )
    ).lower()
    aliases = (
        ("withholding", "withholding_tax"),
        ("dividend", "dividend"),
        ("coupon", "coupon"),
        ("commission", "commission"),
        ("spread", "spread"),
        ("conversion", "fx_conversion"),
        ("financing", "financing"),
        ("interest", "interest"),
        ("borrow", "borrow"),
        ("custody", "custody"),
        ("tax", "tax"),
        ("stamp", "tax"),
        ("fee", "fee"),
        ("cashtransfer", "external_flow"),
        ("cash transfer", "external_flow"),
    )
    return next((category for word, category in aliases if word in labels), "unclassified")


def _bookings(inputs: ResearchInputs, options: PortfolioOptions) -> tuple[_Booking, ...]:
    currency = _currency(inputs)
    result: list[_Booking] = []
    for row in _latest(inputs.source_rows("bookings_v1"), "BkAmountId"):
        at = _at(row.get("Date"), "Date")
        if at > inputs.as_of:
            raise AnalyticsExecutionError("portfolio_booking_after_cutoff")
        if (
            options.start_at is not None
            and options.end_at is not None
            and (at < options.start_at or at > options.end_at)
        ):
            continue
        if row.get("AmountAccountCurrency") is not None and row.get("AccountCurrency") == currency:
            amount = _decimal(row, "AmountAccountCurrency")
        elif row.get("Currency") == currency:
            amount = _decimal(row, "Amount")
        else:
            raise AnalyticsExecutionError(
                "portfolio_booking_currency_basis_unavailable", ("Currency",)
            )
        result.append(
            _Booking(
                _fingerprint(inputs.account_scope, _text(row, "BkAmountId")),
                at,
                amount,
                _category(row),
                row,
            )
        )
    return tuple(result)


def _income(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    bookings = _bookings(inputs, options)
    income = [item for item in bookings if item.category in {"dividend", "coupon", "interest"}]
    if not income:
        raise AnalyticsExecutionError("portfolio_income_entries_unavailable")
    currency = _currency(inputs)
    gross = sum((item.amount for item in income), Decimal(0))
    dividends = sum((item.amount for item in income if item.category == "dividend"), Decimal(0))
    withheld = -sum(
        (item.amount for item in bookings if item.category == "withholding_tax"), Decimal(0)
    )
    rows = [
        _row(item.category, {"booked_income": float(item.amount)}, at=item.at, currency=currency)
        for item in income
    ]
    missing = ("future_income_entitlement",)
    if any(item.category == "unclassified" for item in bookings):
        missing = (*missing, "unclassified_bookings")
    return RecipePayload(
        metrics=(
            _metric("income_amount", gross, currency),
            _metric("dividend_amount", dividends, currency),
        ),
        tables=(
            _table("booked_income", "Booked income", rows),
            _table(
                "income_totals",
                "Booked income reconciliation",
                [
                    _row(
                        "Income",
                        {
                            "gross": float(gross),
                            "withheld_tax": float(withheld),
                            "net": float(gross - withheld),
                        },
                        currency=currency,
                    )
                ],
            ),
        ),
        warnings=("booked_income_only_future_entitlement_unavailable",),
        unavailable_fields=missing,
        verifies=("Classified booked income and withholding in the account currency.",),
    )


def _corporate_actions(
    _request: RecipeRequest, inputs: ResearchInputs, __: PortfolioOptions
) -> RecipePayload:
    required = {"corporate_action_events_v2", "corporate_action_holdings_v2"}
    if required - {page.contract_name for page in inputs.pages}:
        raise AnalyticsExecutionError("corporate_action_capture_required", tuple(sorted(required)))
    events = _latest(inputs.source_rows("corporate_action_events_v2"), "EventId")
    holdings = inputs.source_rows("corporate_action_holdings_v2")
    by_holding: dict[tuple[str, str], SourceRow] = {}
    for holding in holdings:
        key = (_text(holding, "EventId"), _text(holding, "AccountId"))
        if key in by_holding and by_holding[key] != holding:
            raise AnalyticsExecutionError(
                "portfolio_source_revision_ambiguous", ("EventId", "AccountId")
            )
        by_holding[key] = holding
    rows: list[AnalysisRow] = []
    payment_rows: list[AnalysisRow] = []
    missing = {"cash_entitlement", "election_authority", "historical_event_completeness"}
    for event in events:
        event_type = _object(event, "EventType")
        label = _text(event_type, "Name" if event_type.get("Name") else "Code")
        ex = event.get("Ex")
        ex_date = cast("SourceRow", ex).get("Date") if isinstance(ex, Mapping) else None
        handle = (
            _handle(event)
            if event.get("Uic") is not None and event.get("AssetType") is not None
            else None
        )
        if handle is None:
            missing.add("instrument_identity")
        matched = [
            holding for holding in by_holding.values() if holding.get("EventId") == event["EventId"]
        ]
        if not matched:
            missing.add("holding_quantity")
        for holding in matched or [None]:
            # Holdings Amount is a quantity, never an income cash amount.
            rows.append(  # noqa: PERF401 - retain holding quantity semantics explicitly
                _row(
                    label,
                    {
                        "holding_quantity": None
                        if holding is None
                        else float(_decimal(holding, "Amount")),
                        "ex_date": cast("str | None", ex_date),
                    },
                    handle=handle,
                    at=None if ex_date is None else _at(ex_date, "Date"),
                )
            )
        for index, option in enumerate(
            cast("Sequence[SourceRow]", event.get("Options") or ()), start=1
        ):
            payment = option.get("Payment")
            payment_date = (
                cast("SourceRow", payment).get("Date") if isinstance(payment, Mapping) else None
            )
            payment_rows.append(
                _row(
                    f"{label}: option {index}",
                    {
                        "pay_date": cast("str | None", payment_date),
                        "is_default": cast("bool | None", option.get("IsDefault")),
                    },
                    handle=handle,
                    at=None if payment_date is None else _at(payment_date, "Date"),
                )
            )
    return RecipePayload(
        tables=(
            _table("corporate_actions", "Source corporate actions and quantities", rows),
            *(
                ()
                if not payment_rows
                else (
                    _table(
                        "corporate_action_payments", "Source option payment dates", payment_rows
                    ),
                )
            ),
        ),
        warnings=(
            "corporate_action_cash_entitlement_unavailable",
            "corporate_action_list_scope_limited",
        ),
        unavailable_fields=tuple(sorted(missing)),
        verifies=(
            "Bound event types, calendar dates and holding quantities "
            "within the captured Saxo list scope."
            if events
            else "The captured Saxo event list returned no matching events.",
        ),
    )


def _costs(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    currency = _currency(inputs)
    bookings = _bookings(inputs, options)
    cost_categories = {
        component.value: component
        for component in CostComponent
        if component is not CostComponent.TURNOVER
    }
    recognized = [item for item in bookings if item.category in cost_categories]
    if not recognized:
        raise AnalyticsExecutionError("portfolio_cost_bookings_unavailable")
    for item in recognized:
        labels = " ".join(
            str(value) for value in item.source.values() if isinstance(value, str)
        ).lower()
        if "refund" in labels or "reversal" in labels:
            raise AnalyticsExecutionError("portfolio_cost_refund_semantics_unavailable")
    unclassified = any(item.category in {"fee", "unclassified"} for item in bookings)
    missing = ("unclassified_cost_bookings",) if unclassified else ()
    dataset = CostDataset(
        dataset_id=inputs.materials[0].dataset.dataset_id,
        account_alias=inputs.account_scope,
        as_of=inputs.as_of,
        reporting_currency=currency,
        bookings=tuple(
            CostBooking(
                account_alias=inputs.account_scope,
                event_key_sha256=item.key,
                revision=1,
                occurred_at=item.at,
                component=cost_categories[item.category],
                source_amount=item.amount,
                currency=currency,
                fill_key_sha256=(
                    _fingerprint(inputs.account_scope, cast("str", item.source["RelatedTradeId"]))
                    if isinstance(item.source.get("RelatedTradeId"), str)
                    else None
                ),
            )
            for item in recognized
        ),
        fx_quotes=(),
        source_bindings=inputs.bindings(),
        quality_state=QualityState.PARTIAL if missing else QualityState.COMPLETE,
        missing_fields=missing,
        warnings=(),
        saxo_illustration=None,
        named_difference=None,
    )
    result = analyze_cost_xray(
        dataset, visibility=VisibilityMode.PRIVATE_USER_RESULT, trusted_local_host=True
    )
    if isinstance(result, ResearchRefusal):
        raise AnalyticsExecutionError(result.reason_code, result.missing_fields)
    values = result.private_values
    assert values is not None  # noqa: S101 - domain private delivery invariant
    components = values.components
    output = [_metric("total_cost", components.total_cost, currency)]
    present = {item.category for item in recognized}
    for field, metric_id in (
        ("commission", "commission_cost"),
        ("spread", "spread_cost"),
        ("fx_conversion", "fx_conversion_cost"),
        ("financing", "financing_cost"),
        ("borrow", "borrow_cost"),
        ("custody", "custody_cost"),
        ("tax", "tax_cost"),
    ):
        if field in present:
            output.append(_metric(metric_id, getattr(components, field), currency))
    return RecipePayload(
        metrics=tuple(output),
        tables=(
            _table(
                "realized_costs",
                "Classified booked costs",
                [
                    _row(
                        item.category,
                        {"source_amount": float(item.amount)},
                        at=item.at,
                        currency=currency,
                    )
                    for item in recognized
                ],
            ),
        ),
        warnings=(*result.warnings, "realized_bookings_not_cost_illustration"),
        unavailable_fields=(
            *missing,
            "spread_cost_unbooked",
            "turnover",
            "cost_to_open",
            "cost_to_close",
        ),
        verifies=(
            "Cost components from classified booked charges without illustration double counting.",
        ),
    )


def _regulatory_costs(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    if options.cost_report_basis == "ex_ante":
        rows = inputs.source_rows("costs_v1")
        if not rows:
            raise AnalyticsExecutionError("regulatory_ex_ante_cost_basis_unavailable")
        currency = _currency(inputs)
        output_rows: list[AnalysisRow] = []
        total = Decimal(0)
        for row in rows:
            cost = _object(row, "Cost")
            side = _object(cost, "Long")
            if side.get("Currency") != currency:
                raise AnalyticsExecutionError("regulatory_cost_currency_basis_unavailable")
            value = _decimal(side, "TotalCost")
            total += value
            output_rows.append(
                _row(
                    "Ex-ante illustration",
                    {
                        "cost": float(value),
                        "holding_days": float(_decimal(row, "HoldingPeriodInDays")),
                    },
                    currency=currency,
                )
            )
        metric_id = "regulatory_ex_ante_cost"
    else:
        bookings = _bookings(inputs, options)
        cost_categories = {
            "commission",
            "spread",
            "fx_conversion",
            "financing",
            "borrow",
            "custody",
            "tax",
            "fee",
        }
        recognized = [item for item in bookings if item.category in cost_categories]
        if not recognized:
            raise AnalyticsExecutionError("regulatory_ex_post_cost_basis_unavailable")
        currency = _currency(inputs)
        total = -sum((item.amount for item in recognized), Decimal(0))
        output_rows = [
            _row(
                item.category, {"signed_charge": float(-item.amount)}, at=item.at, currency=currency
            )
            for item in recognized
        ]
        metric_id = "regulatory_ex_post_cost"
    return RecipePayload(
        metrics=(_metric(metric_id, total, currency),),
        tables=(
            _table("regulatory_cost_components", "Source cost report components", output_rows),
        ),
        warnings=("regulatory_compliance_authority_unavailable",),
        unavailable_fields=("regulatory_compliance_certification",),
        verifies=(
            "Bound source cost components on the explicitly selected ex-ante or ex-post basis.",
        ),
        does_not_verify=("Regulatory completeness or legal compliance.", "Future returns."),
    )


def _closed_rows(inputs: ResearchInputs, options: PortfolioOptions) -> tuple[SourceRow, ...]:
    rows = _latest(inputs.source_rows("closed_positions_history_v1"), "ClosePositionId")
    if not rows:
        raise AnalyticsExecutionError("closed_trade_history_unavailable")
    selected: list[SourceRow] = []
    for row in rows:
        opened = _at(row.get("TradeDateOpen"), "TradeDateOpen")
        closed = _at(row.get("TradeDateClose"), "TradeDateClose")
        if opened > closed or closed > inputs.as_of:
            raise AnalyticsExecutionError("closed_trade_economic_order_invalid")
        if (
            options.start_at is None
            or options.end_at is None
            or options.start_at <= closed <= options.end_at
        ):
            selected.append(row)
    if not selected:
        raise AnalyticsExecutionError("closed_trade_history_unavailable_in_period")
    return tuple(selected)


def _closed_identity(row: SourceRow, inputs: ResearchInputs) -> str:
    symbol = row.get("InstrumentSymbol")
    matches: list[str] = []
    for page in inputs.pages:
        if page.contract_name != "reference_instruments_v1":
            continue
        for reference in cast("Sequence[SourceRow]", page.payload.get("rows", ())):
            if (
                symbol
                and reference.get("Symbol") == symbol
                and reference.get("AssetType") == row.get("AssetType")
            ):
                identifier = reference.get("Identifier")
                if type(identifier) is not int:
                    raise AnalyticsExecutionError(
                        "closed_trade_instrument_identity_unavailable", ("Identifier",)
                    )
                matches.append(
                    instrument_handle_for_saxo_identity(_text(reference, "AssetType"), identifier)
                )
    if len(set(matches)) != 1:
        raise AnalyticsExecutionError("closed_trade_instrument_identity_unavailable")
    return matches[0]


def _bar_reference(
    inputs: ResearchInputs, handle: str, decision_at: datetime
) -> DecisionBarReference | None:
    try:
        series = inputs.series(handle)
    except AnalyticsExecutionError:
        return None
    bars = [bar for bar in series.bars if bar.bar_time == decision_at]
    if len(bars) != 1:
        return None
    bindings = {binding.contract_id: binding for binding in inputs.bindings()}
    binding = bindings.get("chart_v3")
    if binding is None:
        return None
    return DecisionBarReference(
        dataset_id=series.dataset_id,
        instrument_handle=handle,
        decision_at=decision_at,
        bar_start_at=decision_at,
        bar_end_at=decision_at,
        close_price=Decimal(str(bars[0].close_value)),
        source_binding=binding,
    )


def _mirror(  # noqa: C901 - inventory, cost, currency and broker reconciliation
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    currency = _currency(inputs)
    trades: list[ClosedTrade] = []
    # A selected closed trade keeps its full captured lifecycle costs, including entry costs.
    bookings = _bookings(inputs, PortfolioOptions())
    for row in _closed_rows(inputs, options):
        if row.get("InstrumentCurrency") != currency:
            raise AnalyticsExecutionError(
                "closed_trade_price_currency_unavailable", ("InstrumentCurrency",)
            )
        handle = _closed_identity(row, inputs)
        opened = _at(row.get("TradeDateOpen"), "TradeDateOpen")
        closed = _at(row.get("TradeDateClose"), "TradeDateClose")
        if opened > closed or closed > inputs.as_of:
            raise AnalyticsExecutionError("closed_trade_economic_order_invalid")
        if row.get("AssetType") != "Stock":
            raise AnalyticsExecutionError("closed_trade_contract_multiplier_unavailable")
        amount_open = _decimal(row, "AmountOpen")
        amount_close = _decimal(row, "AmountClose")
        if not amount_open or abs(amount_open) != abs(amount_close):
            raise AnalyticsExecutionError("closed_trade_inventory_incomplete")
        key = _text(row, "ClosePositionId")
        position_keys = {row.get("OpenPositionId"), row.get("ClosePositionId")} - {None}
        linked = [
            item for item in bookings if item.source.get("RelatedPositionId") in position_keys
        ]
        charges = [
            item
            for item in linked
            if item.category
            in {
                "commission",
                "spread",
                "fx_conversion",
                "financing",
                "borrow",
                "custody",
                "tax",
                "fee",
            }
        ]
        if any(item.category == "unclassified" for item in linked):
            raise AnalyticsExecutionError("closed_trade_cost_semantics_unavailable")
        costs = -sum((item.amount for item in charges), Decimal(0))
        if costs < 0:
            raise AnalyticsExecutionError("closed_trade_cost_semantics_unavailable")
        long = amount_open > 0
        gross = abs(amount_open) * (_decimal(row, "ClosePrice") - _decimal(row, "OpenPrice"))
        if not long:
            gross = -gross
        if abs(gross - costs - _decimal(row, "PnLAccountCurrency")) > Decimal("0.01"):
            raise AnalyticsExecutionError("closed_trade_pnl_reconciliation_failed")
        fills = (
            TradeFill(
                account_alias=inputs.account_scope,
                instrument_handle=handle,
                event_key_sha256=_fingerprint(inputs.account_scope, key + ":open"),
                revision=1,
                occurred_at=opened,
                role="entry",
                side="buy" if long else "sell",
                quantity=abs(amount_open),
                price=_decimal(row, "OpenPrice"),
                currency=currency,
            ),
            TradeFill(
                account_alias=inputs.account_scope,
                instrument_handle=handle,
                event_key_sha256=_fingerprint(inputs.account_scope, key + ":close"),
                revision=1,
                occurred_at=closed,
                role="exit",
                side="sell" if long else "buy",
                quantity=abs(amount_close),
                price=_decimal(row, "ClosePrice"),
                currency=currency,
            ),
        )
        trades.append(
            ClosedTrade(
                trade_key_sha256=_fingerprint(inputs.account_scope, key),
                account_alias=inputs.account_scope,
                instrument_handle=handle,
                decision_at=opened,
                fills=fills,
                costs=costs,
                cost_currency=currency,
                counterfactual_at=closed,
                counterfactual_price=_decimal(row, "ClosePrice"),
                decision_quote=None,
                decision_bar=_bar_reference(inputs, handle, opened),
            )
        )
    dataset = TradeReviewDataset(
        dataset_id=inputs.materials[0].dataset.dataset_id,
        account_alias=inputs.account_scope,
        start_at=min(trade.decision_at for trade in trades),
        end_at=inputs.as_of,
        reporting_currency=currency,
        trades=tuple(trades),
        fx_quotes=(),
        source_bindings=inputs.bindings(),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=("closed_leg_aggregates_not_individual_fills",),
    )
    result = analyze_trading_mirror(
        dataset, visibility=VisibilityMode.PRIVATE_USER_RESULT, trusted_local_host=True
    )
    if isinstance(result, ResearchRefusal):
        raise AnalyticsExecutionError(result.reason_code, result.missing_fields)
    private = result.private_values
    assert private is not None  # noqa: S101 - domain private delivery invariant
    rows = [
        _row(
            "Closed trade",
            {
                "gross_pnl": float(trade.gross_profit_loss),
                "net_pnl": float(trade.net_profit_loss),
                "holding_calendar_days": int(trade.holding_hours / 24),
                "entry_vwap": float(trade.entry_vwap),
                "exit_vwap": float(trade.exit_vwap),
                "gross_traded_notional": float(trade.turnover),
            },
            handle=trade.instrument_handle,
            currency=currency,
        )
        for trade in private.trades
    ]
    return RecipePayload(
        metrics=(_metric("win_rate", private.report_card.win_rate_percentage),),
        tables=(_table("trading_mirror", "Closed-trade descriptive review", rows),),
        warnings=(*result.warnings, "counterfactual_same_terminal_price_not_independent"),
        unavailable_fields=(
            "individual_fills",
            "averaging_down_rate",
            "arrival_price",
            "slippage",
            "independent_do_nothing_counterfactual",
            "turnover",
            "average_declared_capital_base",
        ),
        verifies=("Closed-leg aggregate accounting and descriptive holding duration.",),
        does_not_verify=(
            "Trade-decision causality or precise intraday behavior.",
            "Future returns.",
        ),
    )


def _execution_quality(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    rows: list[AnalysisRow] = []
    weighted = Decimal(0)
    quantities = Decimal(0)
    for row in _closed_rows(inputs, options):
        quantity = abs(_decimal(row, "AmountOpen"))
        if quantity == 0:
            raise AnalyticsExecutionError("execution_quantity_invalid")
        price = _decimal(row, "OpenPrice")
        handle = _closed_identity(row, inputs)
        currency = _text(row, "InstrumentCurrency")
        if currency != _currency(inputs):
            raise AnalyticsExecutionError("execution_price_currency_mismatch")
        quantities += quantity
        weighted += quantity * price
        rows.append(
            _row(
                "Reported opening-leg average",
                {"opening_price": float(price), "quantity": float(quantity)},
                handle=handle,
                currency=currency,
            )
        )
    # An average across distinct instruments is not an execution price.
    if len({row.instrument_handle for row in rows}) != 1:
        raise AnalyticsExecutionError("execution_price_instrument_scope_ambiguous")
    return RecipePayload(
        tables=(
            _table("execution_prices", "Source opening-leg prices", rows),
            _table(
                "opening_leg_weighted_price",
                "Quantity-weighted opening-leg price",
                [
                    _row(
                        "Closed-leg aggregate price",
                        {"weighted_opening_price": float(weighted / quantities)},
                        handle=rows[0].instrument_handle,
                        currency=_currency(inputs),
                    ),
                ],
            ),
        ),
        warnings=("closed_leg_average_not_individual_fill_vwap", "decision_quote_unavailable"),
        unavailable_fields=(
            "vwap",
            "individual_fills",
            "arrival_price",
            "midpoint_price",
            "slippage",
            "spread",
        ),
        verifies=("Quantity-weighted broker opening-leg average for one instrument.",),
        does_not_verify=(
            "Arrival-price or exact decision-point execution quality.",
            "Future returns.",
        ),
    )


def _query(
    request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    intent = request.arguments.get("query_intent")
    if not isinstance(intent, Mapping):
        raise AnalyticsExecutionError("portfolio_query_intent_required")
    intent = cast("SourceRow", intent)
    kind = intent.get("intent")
    routes = {
        "portfolio_value": "portfolio_overview",
        "cash_balance": "portfolio_overview",
        "time_weighted_return": "portfolio_performance",
        "volatility": "portfolio_risk",
        "maximum_drawdown": "portfolio_risk",
        "gross_exposure": "portfolio_exposure",
        "net_exposure": "portfolio_exposure",
        "income": "income_calendar",
        "total_cost": "cost_xray",
        "margin_available": "portfolio_margin",
        "settlement_cash": "cash_and_settlement",
    }
    if kind == "metric":
        analysis_kind = routes.get(cast("str", intent.get("metric")))
    elif kind == "analysis":
        analysis_kind = cast("str | None", intent.get("requested_analysis"))
    elif kind == "capability" and intent.get("capability") == "tax_lot_export":
        return _tax_lots(request, inputs, options)
    else:
        raise AnalyticsExecutionError("portfolio_query_dimension_unavailable")
    if analysis_kind not in SUPPORTED_KINDS or analysis_kind == "portfolio_query":
        raise AnalyticsExecutionError("portfolio_query_route_unavailable")
    return execute_recipe(
        RecipeRequest(
            analysis_kind,
            request.dataset_ids,
            request.instrument_handles,
            request.arguments,
        ),
        inputs,
    )


def _tax_lots(
    _request: RecipeRequest, inputs: ResearchInputs, options: PortfolioOptions
) -> RecipePayload:
    if options.tax_lot_export_mode == "authoritative_tax_lots":
        raise AnalyticsExecutionError(
            "authoritative_tax_lot_basis_unavailable",
            ("tax_cost_basis", "lot_matching", "tax_treatment"),
        )
    output: list[AnalysisRow] = []
    for source in _closed_rows(inputs, options):
        opened = _at(source.get("TradeDateOpen"), "TradeDateOpen")
        closed = _at(source.get("TradeDateClose"), "TradeDateClose")
        if opened > closed or closed > inputs.as_of:
            raise AnalyticsExecutionError("closed_trade_economic_order_invalid")
        if (
            options.start_at is not None
            and options.end_at is not None
            and not options.start_at <= closed <= options.end_at
        ):
            continue
        currency = _text(source, "InstrumentCurrency")
        if len(currency) != _CURRENCY_LENGTH or currency == "XXX":
            raise AnalyticsExecutionError(
                "closed_trade_price_currency_unavailable", ("InstrumentCurrency",)
            )
        handle = _closed_identity(source, inputs)
        for label, at, quantity_field, price_field in (
            ("Opening leg", opened, "AmountOpen", "OpenPrice"),
            ("Closing leg", closed, "AmountClose", "ClosePrice"),
        ):
            quantity = _decimal(source, quantity_field)
            if quantity == 0:
                raise AnalyticsExecutionError(
                    "closed_trade_inventory_incomplete", (quantity_field,)
                )
            output.append(
                AnalysisRow(
                    label=label,
                    instrument_handle=handle,
                    at=at,
                    cells=(
                        AnalysisCell(field="quantity", value=float(quantity), unit="quantity"),
                        AnalysisCell(
                            field="price",
                            value=float(_decimal(source, price_field)),
                            unit="price",
                            currency=currency,
                        ),
                    ),
                ),
            )
    if not output:
        raise AnalyticsExecutionError("closed_trade_history_unavailable_in_period")
    return RecipePayload(
        tables=(
            _table("closed_trade_activity", "Closed trade activity for tax reconciliation", output),
        ),
        warnings=(
            "trade_activity_is_not_authoritative_tax_lots",
            "trade_dates_do_not_prove_intraday_times",
        ),
        unavailable_fields=("tax_cost_basis", "lot_matching", "tax_treatment"),
        verifies=(
            "Authenticated closed-leg dates, source quantities, prices and instrument currencies.",
        ),
        does_not_verify=(
            "Tax cost basis, tax-lot matching or jurisdictional treatment.",
            "Exhaustive account activity.",
            "Future returns.",
        ),
    )
