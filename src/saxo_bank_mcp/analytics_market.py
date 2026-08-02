from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import combinations, pairwise
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import (
    PriceSeriesDataset,
    QuoteResearchDataset,
    ResearchRefusal,
    ResearchStatus,
    analyze_instrument_prices,
    analyze_quote,
    assess_price_quality,
)
from saxo_bank_mcp.analytics_metrics import FinancialMetricError, correlation
from saxo_bank_mcp.analytics_models import (
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    QualityState,
    UniverseId,
)

type UniverseScope = Literal["holdings", "orders", "saved_universe", "explicit"]
type DepthEntitlement = Literal["available", "partial", "denied"]
type SavedConditionKind = Literal["price_above", "return_above", "spread_below"]

_SOURCE_SCOPE: Final = "saxo_openapi"
_MAX_BOUNDED_INSTRUMENTS: Final = 25
_MIN_PAIR_OBSERVATIONS: Final = 2
_MIN_WRAPPERS: Final = 2
_LOW_CORRELATION_BOUND: Final = 0.3
_HIGH_CORRELATION_BOUND: Final = 0.7
_LOW_VOLATILITY_BOUND: Final = 0.15
_HIGH_VOLATILITY_BOUND: Final = 0.30
_UNBOUND_DEPTH_SOURCE_WARNING: Final = "market_depth_source_contract_unbound"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class BoundedResearchUniverse(_StrictModel):
    """A declared Saxo set that can never expand beyond 25 safe handles."""

    scope: UniverseScope
    universe_id: UniverseId | None = None
    series: tuple[PriceSeriesDataset, ...] = Field(
        min_length=1,
        max_length=_MAX_BOUNDED_INSTRUMENTS,
    )

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if self.scope == "saved_universe" and self.universe_id is None:
            raise ValueError("saved-universe research requires a safe universe id")
        handles = tuple(item.instrument_handle for item in self.series)
        datasets = tuple(item.dataset_id for item in self.series)
        if len(set(handles)) != len(handles):
            raise ValueError("bounded research handles must be unique")
        if len(set(datasets)) != len(datasets):
            raise ValueError("bounded research datasets must be unique")
        return self


class BoundedMover(_StrictModel):
    instrument_handle: InstrumentHandle
    dataset_id: DatasetId
    latest_price_return: float = Field(allow_inf_nan=False)


class BoundedComparison(_StrictModel):
    instrument_handle: InstrumentHandle
    dataset_id: DatasetId
    price_return: float = Field(allow_inf_nan=False)
    latest_price_return: float = Field(allow_inf_nan=False)
    annualized_volatility: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    maximum_drawdown: float = Field(ge=-1, le=0, allow_inf_nan=False)


class CorrelationPair(_StrictModel):
    left_handle: InstrumentHandle
    right_handle: InstrumentHandle
    observation_count: int = Field(ge=2)
    value: float = Field(ge=-1, le=1, allow_inf_nan=False)


class BoundedMarketResearch(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["bounded_market_research"] = "bounded_market_research"
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    whole_market: Literal[False] = False
    scope: UniverseScope
    universe_id: UniverseId | None
    scope_statement: str = Field(min_length=1, max_length=220)
    instrument_count: int = Field(ge=1, le=_MAX_BOUNDED_INSTRUMENTS)
    movers: tuple[BoundedMover, ...]
    comparisons: tuple[BoundedComparison, ...]
    advancers: int = Field(ge=0)
    decliners: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    breadth: float = Field(ge=-1, le=1, allow_inf_nan=False)
    correlations: tuple[CorrelationPair, ...]
    average_absolute_correlation: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    correlation_regime: Literal["low", "mixed", "high"] | None
    volatility_regime: Literal["low", "moderate", "high"] | None
    warnings: tuple[str, ...]


def _scope_statement(universe: BoundedResearchUniverse) -> str:
    count = len(universe.series)
    if universe.scope == "saved_universe":
        return f"within the selected saved Saxo universe of {count} instruments"
    if universe.scope == "explicit":
        return f"within the explicit Saxo set of {count} instruments"
    if universe.scope == "holdings":
        return f"within the selected Saxo holdings set of {count} instruments"
    return f"within the selected Saxo orders set of {count} instruments"


def _universe_refusal(
    universe: BoundedResearchUniverse,
    *,
    reason_code: str,
    reason: str,
    warnings: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="bounded_market_research",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=tuple(item.dataset_id for item in universe.series),
        instrument_handles=tuple(item.instrument_handle for item in universe.series),
        warnings=tuple(sorted(set(warnings))),
    )


def _aligned_period_returns(
    left: PriceSeriesDataset,
    right: PriceSeriesDataset,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    left_by_period = {
        (previous.bar_time, bar.bar_time): bar.close_value / previous.close_value - 1.0
        for previous, bar in pairwise(left.bars)
    }
    right_by_period = {
        (previous.bar_time, bar.bar_time): bar.close_value / previous.close_value - 1.0
        for previous, bar in pairwise(right.bars)
    }
    common = tuple(sorted(set(left_by_period) & set(right_by_period)))
    return (
        tuple(left_by_period[period] for period in common),
        tuple(right_by_period[period] for period in common),
    )


def analyze_bounded_market(  # noqa: C901, PLR0912, PLR0915
    universe: BoundedResearchUniverse,
    *,
    periods_per_year: float,
) -> BoundedMarketResearch | ResearchRefusal:
    """Compare only the explicitly supplied bounded Saxo set."""
    if not math.isfinite(periods_per_year) or periods_per_year <= 0.0:
        raise ValueError("periods per year must be positive and finite")
    comparisons: list[BoundedComparison] = []
    movers: list[BoundedMover] = []
    warnings: set[str] = set()
    volatility_values: list[float] = []
    for dataset in universe.series:
        result = analyze_instrument_prices(
            dataset,
            rolling_window=1,
            periods_per_year=periods_per_year,
        )
        if isinstance(result, ResearchRefusal):
            return _universe_refusal(
                universe,
                reason_code="bounded_series_unusable",
                reason="one or more bounded Saxo price series cannot support comparison",
                warnings=(*warnings, result.reason_code, *result.warnings),
            )
        latest_return = result.period_returns[-1]
        comparisons.append(
            BoundedComparison(
                instrument_handle=result.instrument_handle,
                dataset_id=result.dataset_id,
                price_return=result.price_return,
                latest_price_return=latest_return,
                annualized_volatility=result.annualized_volatility,
                maximum_drawdown=result.maximum_drawdown,
            ),
        )
        movers.append(
            BoundedMover(
                instrument_handle=result.instrument_handle,
                dataset_id=result.dataset_id,
                latest_price_return=latest_return,
            ),
        )
        if result.annualized_volatility is not None:
            volatility_values.append(result.annualized_volatility)
        warnings.update(result.warnings)

    latest_values = tuple(item.latest_price_return for item in movers)
    advancers = sum(value > 0.0 for value in latest_values)
    decliners = sum(value < 0.0 for value in latest_values)
    unchanged = len(latest_values) - advancers - decliners
    breadth = (advancers - decliners) / len(latest_values)

    correlations: list[CorrelationPair] = []
    for left, right in combinations(universe.series, 2):
        left_returns, right_returns = _aligned_period_returns(left, right)
        if len(left_returns) < _MIN_PAIR_OBSERVATIONS:
            warnings.add("correlation_alignment_insufficient")
            continue
        try:
            value = correlation(left_returns, right_returns)
        except FinancialMetricError:
            warnings.add("correlation_undefined_for_some_pairs")
            continue
        correlations.append(
            CorrelationPair(
                left_handle=left.instrument_handle,
                right_handle=right.instrument_handle,
                observation_count=len(left_returns),
                value=value,
            ),
        )

    average_absolute_correlation = (
        math.fsum(abs(item.value) for item in correlations) / len(correlations)
        if correlations
        else None
    )
    if average_absolute_correlation is None:
        correlation_regime = None
        warnings.add("correlation_regime_unavailable")
    elif average_absolute_correlation < _LOW_CORRELATION_BOUND:
        correlation_regime = "low"
    elif average_absolute_correlation < _HIGH_CORRELATION_BOUND:
        correlation_regime = "mixed"
    else:
        correlation_regime = "high"
    average_volatility = (
        math.fsum(volatility_values) / len(volatility_values) if volatility_values else None
    )
    if average_volatility is None:
        volatility_regime = None
        warnings.add("volatility_regime_unavailable")
    elif average_volatility < _LOW_VOLATILITY_BOUND:
        volatility_regime = "low"
    elif average_volatility < _HIGH_VOLATILITY_BOUND:
        volatility_regime = "moderate"
    else:
        volatility_regime = "high"

    return BoundedMarketResearch(
        status=ResearchStatus.REDUCED,
        scope=universe.scope,
        universe_id=universe.universe_id,
        scope_statement=_scope_statement(universe),
        instrument_count=len(universe.series),
        movers=tuple(
            sorted(
                movers,
                key=lambda item: (-item.latest_price_return, item.instrument_handle),
            ),
        ),
        comparisons=tuple(comparisons),
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        breadth=breadth,
        correlations=tuple(correlations),
        average_absolute_correlation=average_absolute_correlation,
        correlation_regime=correlation_regime,
        volatility_regime=volatility_regime,
        warnings=tuple(sorted(warnings)),
    )


class DepthLevel(_StrictModel):
    price: float = Field(gt=0, allow_inf_nan=False)
    size: float = Field(gt=0, allow_inf_nan=False)


class MarketDepthDataset(_StrictModel):
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    captured_at: datetime
    quality_state: QualityState
    entitlement_state: DepthEntitlement
    delayed_by_minutes: int | None = Field(default=None, ge=0)
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_depth(self) -> Self:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() != timedelta(0):
            raise ValueError("depth timestamp must use UTC")
        if tuple(level.price for level in self.bids) != tuple(
            sorted((level.price for level in self.bids), reverse=True),
        ):
            raise ValueError("bid levels must be best-price first")
        if tuple(level.price for level in self.asks) != tuple(
            sorted(level.price for level in self.asks),
        ):
            raise ValueError("ask levels must be best-price first")
        return self


class MarketDepthResearch(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["entitled_market_depth"] = "entitled_market_depth"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] | None = None
    captured_at: datetime
    spread: float = Field(ge=0, allow_inf_nan=False)
    spread_basis_points: float = Field(ge=0, allow_inf_nan=False)
    bid_depth: float = Field(gt=0, allow_inf_nan=False)
    ask_depth: float = Field(gt=0, allow_inf_nan=False)
    depth_imbalance: float = Field(ge=-100, le=100, allow_inf_nan=False)
    delayed_by_minutes: int | None = Field(default=None, ge=0)
    warnings: tuple[str, ...]


def analyze_entitled_depth(
    dataset: MarketDepthDataset,
) -> MarketDepthResearch | ResearchRefusal:
    """Calculate spread and displayed depth without asserting unbound provenance."""
    if dataset.entitlement_state != "available":
        return ResearchRefusal(
            analysis_kind="entitled_market_depth",
            reason_code="market_depth_entitlement_insufficient",
            reason="declared depth entitlement is not complete",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(dataset.instrument_handle,),
            missing_fields=("bids", "asks"),
            warnings=dataset.warnings,
            source_scope=None,
        )
    if dataset.quality_state in {QualityState.MISSING, QualityState.INVALID, QualityState.STALE}:
        return ResearchRefusal(
            analysis_kind="entitled_market_depth",
            reason_code="market_depth_unusable",
            reason="the supplied depth dataset is missing, invalid, or stale",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(dataset.instrument_handle,),
            warnings=dataset.warnings,
            source_scope=None,
        )
    if not dataset.bids or not dataset.asks:
        return ResearchRefusal(
            analysis_kind="entitled_market_depth",
            reason_code="market_depth_fields_missing",
            reason="the supplied depth dataset has no complete bid and ask levels",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(dataset.instrument_handle,),
            missing_fields=("bids", "asks"),
            warnings=dataset.warnings,
            source_scope=None,
        )
    best_bid = dataset.bids[0].price
    best_ask = dataset.asks[0].price
    if best_ask < best_bid:
        return ResearchRefusal(
            analysis_kind="entitled_market_depth",
            reason_code="market_depth_crossed",
            reason="the supplied depth dataset contains a crossed book",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(dataset.instrument_handle,),
            warnings=dataset.warnings,
            source_scope=None,
        )
    bid_depth = math.fsum(level.size for level in dataset.bids)
    ask_depth = math.fsum(level.size for level in dataset.asks)
    total_depth = bid_depth + ask_depth
    midpoint = (best_bid + best_ask) / 2.0
    warnings = set(dataset.warnings)
    warnings.add(_UNBOUND_DEPTH_SOURCE_WARNING)
    if (dataset.delayed_by_minutes or 0) > 0:
        warnings.add("depth_delayed")
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("depth_quality_partial")
    return MarketDepthResearch(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        instrument_handle=dataset.instrument_handle,
        captured_at=dataset.captured_at.astimezone(UTC),
        spread=best_ask - best_bid,
        spread_basis_points=(best_ask - best_bid) / midpoint * 10_000.0,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        depth_imbalance=100.0 * (bid_depth - ask_depth) / total_depth,
        delayed_by_minutes=dataset.delayed_by_minutes,
        warnings=tuple(sorted(warnings)),
    )


class WrapperCostDataset(_StrictModel):
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    wrapper_label: str = Field(min_length=1, max_length=80)
    exposure_amount: float = Field(gt=0, allow_inf_nan=False)
    currency: IsoCurrencyCode
    horizon_days: int = Field(ge=1)
    opening_cost: float = Field(ge=0, allow_inf_nan=False)
    holding_cost: float = Field(ge=0, allow_inf_nan=False)
    closing_cost: float = Field(ge=0, allow_inf_nan=False)


class WrapperCostItem(_StrictModel):
    instrument_handle: InstrumentHandle
    dataset_id: DatasetId
    wrapper_label: str
    total_cost: float = Field(ge=0, allow_inf_nan=False)
    cost_basis_points: float = Field(ge=0, allow_inf_nan=False)


class WrapperComparison(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE] = ResearchStatus.COMPLETE
    analysis_kind: Literal["wrapper_cost_comparison"] = "wrapper_cost_comparison"
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    exposure_amount: float = Field(gt=0, allow_inf_nan=False)
    currency: IsoCurrencyCode
    horizon_days: int = Field(ge=1)
    wrappers: tuple[WrapperCostItem, ...] = Field(min_length=2)
    cost_difference: float = Field(ge=0, allow_inf_nan=False)
    limitations: tuple[str, ...] = ("cost_inputs_only",)


def compare_wrappers(
    datasets: Sequence[WrapperCostDataset],
) -> WrapperComparison | ResearchRefusal:
    """Compare declared Saxo wrapper costs for one matching exposure and horizon."""
    values = tuple(datasets)
    if len(values) < _MIN_WRAPPERS:
        return ResearchRefusal(
            analysis_kind="wrapper_cost_comparison",
            reason_code="wrapper_comparison_insufficient",
            reason="at least two Saxo wrapper cost datasets are required",
            dataset_ids=tuple(item.dataset_id for item in values),
            instrument_handles=tuple(item.instrument_handle for item in values),
            missing_fields=("comparison_wrapper",),
        )
    first = values[0]
    if any(
        item.exposure_amount != first.exposure_amount
        or item.currency != first.currency
        or item.horizon_days != first.horizon_days
        for item in values[1:]
    ):
        return ResearchRefusal(
            analysis_kind="wrapper_cost_comparison",
            reason_code="wrapper_basis_mismatch",
            reason="Saxo wrapper costs must use the same exposure, currency, and horizon",
            dataset_ids=tuple(item.dataset_id for item in values),
            instrument_handles=tuple(item.instrument_handle for item in values),
        )
    items = tuple(
        WrapperCostItem(
            instrument_handle=item.instrument_handle,
            dataset_id=item.dataset_id,
            wrapper_label=item.wrapper_label,
            total_cost=item.opening_cost + item.holding_cost + item.closing_cost,
            cost_basis_points=(item.opening_cost + item.holding_cost + item.closing_cost)
            / item.exposure_amount
            * 10_000.0,
        )
        for item in values
    )
    totals = tuple(item.total_cost for item in items)
    return WrapperComparison(
        exposure_amount=first.exposure_amount,
        currency=first.currency,
        horizon_days=first.horizon_days,
        wrappers=items,
        cost_difference=max(totals) - min(totals),
    )


class SavedCondition(_StrictModel):
    condition_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    instrument_handle: InstrumentHandle
    dataset_id: DatasetId
    kind: SavedConditionKind
    threshold: float = Field(allow_inf_nan=False)


class SavedConditionCheck(_StrictModel):
    condition_id: str
    instrument_handle: InstrumentHandle
    kind: SavedConditionKind
    observed_value: float = Field(allow_inf_nan=False)
    threshold: float = Field(allow_inf_nan=False)
    matched: bool


class SavedConditionResult(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["saved_condition_checks"] = "saved_condition_checks"
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    checks: tuple[SavedConditionCheck, ...]
    warnings: tuple[str, ...]


def check_saved_conditions(  # noqa: C901, PLR0911, PLR0912
    conditions: Sequence[SavedCondition],
    universe: BoundedResearchUniverse,
    *,
    quotes: Sequence[QuoteResearchDataset] = (),
) -> SavedConditionResult | ResearchRefusal:
    """Evaluate only the fixed condition catalog over supplied Saxo datasets."""
    values = tuple(conditions)
    if not values:
        return ResearchRefusal(
            analysis_kind="saved_condition_checks",
            reason_code="saved_conditions_missing",
            reason="at least one fixed saved condition is required",
            dataset_ids=(),
            instrument_handles=(),
            missing_fields=("saved_conditions",),
        )
    price_by_dataset = {item.dataset_id: item for item in universe.series}
    quote_by_dataset = {item.dataset_id: item for item in quotes}
    warnings: set[str] = set()
    checks: list[SavedConditionCheck] = []
    for condition in values:
        observed: float
        if condition.kind == "spread_below":
            quote_dataset = quote_by_dataset.get(condition.dataset_id)
            if (
                quote_dataset is None
                or quote_dataset.instrument_handle != condition.instrument_handle
            ):
                return ResearchRefusal(
                    analysis_kind="saved_condition_checks",
                    reason_code="saved_condition_dataset_missing",
                    reason="the saved spread condition has no matching bounded Saxo quote",
                    dataset_ids=(condition.dataset_id,),
                    instrument_handles=(condition.instrument_handle,),
                    missing_fields=("quote_dataset",),
                )
            if (
                quote_dataset.quality_state is QualityState.STALE
                or quote_dataset.quote.freshness == "stale"
            ):
                quote_warnings = set(quote_dataset.warnings) | set(
                    quote_dataset.quote.warnings,
                )
                quote_warnings.add("quote_stale")
                return ResearchRefusal(
                    analysis_kind="saved_condition_checks",
                    reason_code="quote_data_unusable",
                    reason="stale quote data cannot be evaluated for a saved condition",
                    dataset_ids=(condition.dataset_id,),
                    instrument_handles=(condition.instrument_handle,),
                    warnings=tuple(sorted(quote_warnings)),
                )
            quote_result = analyze_quote(quote_dataset)
            if isinstance(quote_result, ResearchRefusal):
                return quote_result
            observed = quote_result.spread
            matched = observed < condition.threshold
            warnings.update(quote_result.warnings)
        else:
            price_dataset = price_by_dataset.get(condition.dataset_id)
            if (
                price_dataset is None
                or price_dataset.instrument_handle != condition.instrument_handle
            ):
                return ResearchRefusal(
                    analysis_kind="saved_condition_checks",
                    reason_code="saved_condition_dataset_missing",
                    reason="the saved condition has no matching bounded Saxo price dataset",
                    dataset_ids=(condition.dataset_id,),
                    instrument_handles=(condition.instrument_handle,),
                    missing_fields=("price_dataset",),
                )
            price_quality = assess_price_quality(
                price_dataset,
                analysis_kind="saved_condition_checks",
            )
            if isinstance(price_quality, ResearchRefusal):
                return price_quality
            warnings.update(price_quality)
            if not price_dataset.bars:
                return ResearchRefusal(
                    analysis_kind="saved_condition_checks",
                    reason_code="saved_condition_price_missing",
                    reason="the matching bounded Saxo price dataset is empty",
                    dataset_ids=(condition.dataset_id,),
                    instrument_handles=(condition.instrument_handle,),
                    missing_fields=("price_history",),
                )
            if condition.kind == "price_above":
                observed = price_dataset.bars[-1].close_value
            else:
                if len(price_dataset.bars) < _MIN_PAIR_OBSERVATIONS:
                    return ResearchRefusal(
                        analysis_kind="saved_condition_checks",
                        reason_code="saved_condition_return_missing",
                        reason="the bounded Saxo series cannot support a price return",
                        dataset_ids=(condition.dataset_id,),
                        instrument_handles=(condition.instrument_handle,),
                        missing_fields=("price_history",),
                    )
                observed = (
                    price_dataset.bars[-1].close_value / price_dataset.bars[0].close_value - 1.0
                )
                warnings.add("unadjusted_price_series")
            matched = observed > condition.threshold
        checks.append(
            SavedConditionCheck(
                condition_id=condition.condition_id,
                instrument_handle=condition.instrument_handle,
                kind=condition.kind,
                observed_value=observed,
                threshold=condition.threshold,
                matched=matched,
            ),
        )
    return SavedConditionResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        checks=tuple(checks),
        warnings=tuple(sorted(warnings)),
    )


class SessionPreparationItem(_StrictModel):
    instrument_handle: InstrumentHandle
    dataset_id: DatasetId
    latest_price: float = Field(gt=0, allow_inf_nan=False)
    latest_price_return: float = Field(allow_inf_nan=False)
    spread: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    quote_freshness: Literal["fresh", "stale"] | None = None
    warnings: tuple[str, ...]


class BoundedSessionPreparation(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["bounded_session_preparation"] = "bounded_session_preparation"
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    whole_market: Literal[False] = False
    scope_statement: str
    items: tuple[SessionPreparationItem, ...]
    warnings: tuple[str, ...]


def prepare_bounded_session(
    universe: BoundedResearchUniverse,
    *,
    quotes: Sequence[QuoteResearchDataset] = (),
    periods_per_year: float = 252.0,
) -> BoundedSessionPreparation | ResearchRefusal:
    """Prepare a quote-quality-aware view of only the supplied bounded Saxo set."""
    bounded_result = analyze_bounded_market(
        universe,
        periods_per_year=periods_per_year,
    )
    if isinstance(bounded_result, ResearchRefusal):
        return bounded_result
    quotes_by_handle = {item.instrument_handle: item for item in quotes}
    warnings = set(bounded_result.warnings)
    items: list[SessionPreparationItem] = []
    for dataset in universe.series:
        quote_dataset = quotes_by_handle.get(dataset.instrument_handle)
        spread: float | None = None
        freshness: Literal["fresh", "stale"] | None = None
        item_warnings: set[str] = set()
        if quote_dataset is None:
            item_warnings.add("quote_not_supplied")
        else:
            quote_result = analyze_quote(quote_dataset)
            if isinstance(quote_result, ResearchRefusal):
                item_warnings.add(quote_result.reason_code)
                item_warnings.update(quote_result.warnings)
            else:
                spread = quote_result.spread
                freshness = quote_result.freshness
                item_warnings.update(quote_result.warnings)
        latest_return = dataset.bars[-1].close_value / dataset.bars[-2].close_value - 1.0
        warnings.update(item_warnings)
        items.append(
            SessionPreparationItem(
                instrument_handle=dataset.instrument_handle,
                dataset_id=dataset.dataset_id,
                latest_price=dataset.bars[-1].close_value,
                latest_price_return=latest_return,
                spread=spread,
                quote_freshness=freshness,
                warnings=tuple(sorted(item_warnings)),
            ),
        )
    return BoundedSessionPreparation(
        status=ResearchStatus.REDUCED,
        scope_statement=bounded_result.scope_statement,
        items=tuple(items),
        warnings=tuple(sorted(warnings)),
    )


__all__ = (
    "BoundedMarketResearch",
    "BoundedResearchUniverse",
    "BoundedSessionPreparation",
    "CorrelationPair",
    "DepthLevel",
    "MarketDepthDataset",
    "MarketDepthResearch",
    "SavedCondition",
    "SavedConditionResult",
    "WrapperComparison",
    "WrapperCostDataset",
    "analyze_bounded_market",
    "analyze_entitled_depth",
    "check_saved_conditions",
    "compare_wrappers",
    "prepare_bounded_session",
)
