from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_metrics import (
    FinancialMetricError,
    cumulative_return,
    maximum_drawdown,
    simple_returns,
    volatility,
)
from saxo_bank_mcp.analytics_models import DatasetId, InstrumentHandle, QualityState
from saxo_bank_mcp.analytics_resolver import ResolvedInstrument
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow, QuoteDatasetRow

type ReturnSeriesRequest = Literal[
    "price_return",
    "adjusted_price_return",
    "total_return",
]
type EntitlementState = Literal["available", "partial", "denied"]

_SOURCE_SCOPE: Final = "saxo_openapi"
_MIN_PRICE_OBSERVATIONS: Final = 2
_MIN_RISK_RETURNS: Final = 2


class ResearchStatus(StrEnum):
    """Data completeness before a later proof profile can verify the analysis."""

    COMPLETE = "complete"
    REDUCED = "reduced"
    REFUSED = "refused"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ResearchRefusal(_StrictModel):
    """Value-bounded refusal over opaque Saxo analytics handles."""

    status: Literal[ResearchStatus.REFUSED] = ResearchStatus.REFUSED
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    reason: str = Field(min_length=1, max_length=500)
    dataset_ids: tuple[DatasetId, ...]
    instrument_handles: tuple[InstrumentHandle, ...]
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_scope: Literal["saxo_openapi"] | None = _SOURCE_SCOPE


class PriceSeriesDataset(_StrictModel):
    """One bounded, handle-only Saxo chart dataset used by research engines."""

    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    bars: tuple[PriceBarDatasetRow, ...]
    quality_state: QualityState
    missing_interval_count: int = Field(ge=0)
    return_series_label: Literal["price_return"]
    adjustment_status: Literal["unadjusted"]
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_bars(self) -> Self:
        timestamps: list[datetime] = []
        intervals: set[str] = set()
        for bar in self.bars:
            if bar.instrument_handle != self.instrument_handle:
                raise ValueError("price rows must match the dataset instrument handle")
            if bar.bar_time.tzinfo is None or bar.bar_time.utcoffset() != timedelta(0):
                raise ValueError("price rows must use UTC")
            if bar.close_value <= 0.0:
                raise ValueError("price closes must be positive")
            timestamps.append(bar.bar_time.astimezone(UTC))
            intervals.add(bar.interval.value)
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("price rows must be unique and strictly ordered")
        if len(intervals) > 1:
            raise ValueError("price rows must use one interval")
        return self


class QuoteResearchDataset(_StrictModel):
    """One quote dataset with explicit Saxo entitlement, delay, and freshness."""

    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    quote: QuoteDatasetRow
    quality_state: QualityState
    entitlement_state: EntitlementState
    delayed_by_minutes: int | None = Field(default=None, ge=0)
    price_type: str | None = Field(default=None, min_length=1, max_length=64)
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_quote(self) -> Self:
        if self.quote.instrument_handle != self.instrument_handle:
            raise ValueError("quote row must match the dataset instrument handle")
        if self.quote.captured_at.tzinfo is None or self.quote.captured_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("quote timestamp must use UTC")
        return self


class TimedValue(_StrictModel):
    at: datetime
    value: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_time(self) -> Self:
        if self.at.tzinfo is None or self.at.utcoffset() != timedelta(0):
            raise ValueError("research value timestamp must use UTC")
        return self


class InstrumentPriceResearch(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["instrument_price_return"] = "instrument_price_return"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    return_label: Literal["price_return"]
    adjustment_status: Literal["unadjusted"]
    start_at: datetime
    end_at: datetime
    start_price: float = Field(gt=0, allow_inf_nan=False)
    end_price: float = Field(gt=0, allow_inf_nan=False)
    price_return: float = Field(allow_inf_nan=False)
    period_returns: tuple[float, ...]
    annualized_volatility: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    maximum_drawdown: float = Field(ge=-1, le=0, allow_inf_nan=False)
    rolling_window: int = Field(ge=1)
    rolling_returns: tuple[TimedValue, ...]
    warnings: tuple[str, ...]
    verifies: tuple[str, ...]
    does_not_verify: tuple[str, ...]


class InstrumentQuoteResearch(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["instrument_quote"] = "instrument_quote"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    captured_at: datetime
    bid: float = Field(gt=0, allow_inf_nan=False)
    ask: float = Field(gt=0, allow_inf_nan=False)
    midpoint: float = Field(gt=0, allow_inf_nan=False)
    spread: float = Field(ge=0, allow_inf_nan=False)
    spread_basis_points: float = Field(ge=0, allow_inf_nan=False)
    freshness: Literal["fresh", "stale"]
    delayed_by_minutes: int | None = Field(default=None, ge=0)
    warnings: tuple[str, ...]


class InstrumentDossier(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["instrument_dossier"] = "instrument_dossier"
    instrument_handle: InstrumentHandle
    dataset_ids: tuple[DatasetId, ...]
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    display_label: str = Field(min_length=1, max_length=220)
    symbol: str | None = Field(default=None, max_length=64)
    asset_type: str = Field(min_length=1, max_length=64)
    exchange: str | None = Field(default=None, max_length=64)
    prices: InstrumentPriceResearch
    quote: InstrumentQuoteResearch | None
    warnings: tuple[str, ...]
    bounded: Literal[True] = True


def _refusal(  # noqa: PLR0913
    *,
    analysis_kind: str,
    reason_code: str,
    reason: str,
    datasets: Sequence[str],
    handles: Sequence[str],
    missing_fields: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=analysis_kind,
        reason_code=reason_code,
        reason=reason,
        dataset_ids=tuple(datasets),
        instrument_handles=tuple(handles),
        missing_fields=tuple(sorted({field for field in missing_fields if field})),
        warnings=tuple(sorted(set(warnings))),
    )


def assess_price_quality(
    dataset: PriceSeriesDataset,
    *,
    analysis_kind: str,
) -> tuple[str, ...] | ResearchRefusal:
    """Apply one price-quality gate and preserve every upstream limitation."""
    if dataset.quality_state in {
        QualityState.MISSING,
        QualityState.INVALID,
        QualityState.STALE,
    }:
        return _refusal(
            analysis_kind=analysis_kind,
            reason_code="price_data_unusable",
            reason="the bounded Saxo price dataset is missing, invalid, or stale",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            warnings=dataset.warnings,
        )
    warnings = set(dataset.warnings)
    if dataset.quality_state is QualityState.PARTIAL or dataset.missing_interval_count:
        warnings.add("incomplete_price_coverage")
    return tuple(sorted(warnings))


def analyze_instrument_prices(
    dataset: PriceSeriesDataset,
    *,
    rolling_window: int,
    periods_per_year: float,
    requested_return: ReturnSeriesRequest = "price_return",
) -> InstrumentPriceResearch | ResearchRefusal:
    """Calculate bounded price-return and risk measures from one safe Saxo dataset."""
    if isinstance(rolling_window, bool) or rolling_window < 1:
        raise ValueError("rolling window must be a positive integer")
    if not math.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("periods per year must be positive and finite")
    if requested_return != "price_return":
        return _refusal(
            analysis_kind="instrument_price_return",
            reason_code="return_series_unavailable",
            reason=("the bounded Saxo chart dataset is unadjusted and proves price return only"),
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            missing_fields=(requested_return,),
            warnings=("unadjusted_price_series",),
        )
    quality = assess_price_quality(dataset, analysis_kind="instrument_price_return")
    if isinstance(quality, ResearchRefusal):
        return quality
    if len(dataset.bars) < _MIN_PRICE_OBSERVATIONS:
        return _refusal(
            analysis_kind="instrument_price_return",
            reason_code="insufficient_price_history",
            reason="at least two complete Saxo closes are required",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            missing_fields=("price_history",),
            warnings=dataset.warnings,
        )
    if len(dataset.bars) <= rolling_window:
        return _refusal(
            analysis_kind="instrument_price_return",
            reason_code="rolling_window_incomplete",
            reason="the exact rolling window endpoints are unavailable",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            missing_fields=("rolling_window_endpoint",),
            warnings=dataset.warnings,
        )

    closes = tuple(bar.close_value for bar in dataset.bars)
    try:
        period_values = tuple(float(value) for value in simple_returns(closes))
        total = cumulative_return(period_values)
        drawdown = maximum_drawdown(closes)
        annualized_volatility = (
            volatility(period_values, periods_per_year)
            if len(period_values) >= _MIN_RISK_RETURNS
            else None
        )
    except FinancialMetricError as error:
        return _refusal(
            analysis_kind="instrument_price_return",
            reason_code="price_metric_undefined",
            reason="the bounded Saxo price series cannot support the requested metric",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            warnings=(*dataset.warnings, type(error).__name__),
        )
    rolling = tuple(
        TimedValue(
            at=dataset.bars[index].bar_time,
            value=closes[index] / closes[index - rolling_window] - 1.0,
        )
        for index in range(rolling_window, len(closes))
    )
    warnings = set(quality)
    warnings.add("unadjusted_price_series")
    if annualized_volatility is None:
        warnings.add("risk_sample_insufficient")
    return InstrumentPriceResearch(
        status=ResearchStatus.REDUCED,
        dataset_id=dataset.dataset_id,
        instrument_handle=dataset.instrument_handle,
        return_label="price_return",
        adjustment_status="unadjusted",
        start_at=dataset.bars[0].bar_time,
        end_at=dataset.bars[-1].bar_time,
        start_price=closes[0],
        end_price=closes[-1],
        price_return=total,
        period_returns=period_values,
        annualized_volatility=annualized_volatility,
        maximum_drawdown=drawdown,
        rolling_window=rolling_window,
        rolling_returns=rolling,
        warnings=tuple(sorted(warnings)),
        verifies=("unadjusted Saxo price return over the exact bounded dataset",),
        does_not_verify=("total return", "adjusted price return", "future performance"),
    )


def analyze_quote(  # noqa: C901
    dataset: QuoteResearchDataset,
) -> InstrumentQuoteResearch | ResearchRefusal:
    """Calculate quote quality only when Saxo supplied an entitled bid and ask."""
    warnings = set(dataset.warnings) | set(dataset.quote.warnings)
    normalized_price_type = (
        None
        if dataset.price_type is None
        else dataset.price_type.replace(" ", "").casefold()
    )
    if (
        dataset.entitlement_state != "available"
        or normalized_price_type == "noaccess"
        or "quote_entitlement_limited" in warnings
    ):
        warnings.add("quote_entitlement_limited")
        return _refusal(
            analysis_kind="instrument_quote",
            reason_code="quote_entitlement_insufficient",
            reason="Saxo quote entitlement is insufficient for bid and ask analysis",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            missing_fields=("bid", "ask"),
            warnings=tuple(warnings),
        )
    if dataset.quality_state in {QualityState.MISSING, QualityState.INVALID}:
        return _refusal(
            analysis_kind="instrument_quote",
            reason_code="quote_data_unusable",
            reason="the entitled Saxo quote dataset is marked missing or invalid",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            warnings=tuple(warnings),
        )
    bid = dataset.quote.bid_value
    ask = dataset.quote.ask_value
    if bid is None or ask is None:
        return _refusal(
            analysis_kind="instrument_quote",
            reason_code="quote_fields_missing",
            reason="the entitled Saxo quote does not contain both bid and ask",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            missing_fields=("bid" if bid is None else "", "ask" if ask is None else ""),
            warnings=tuple(warnings),
        )
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        return _refusal(
            analysis_kind="instrument_quote",
            reason_code="quote_fields_invalid",
            reason="the Saxo bid and ask do not form a valid quote",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            warnings=tuple(warnings),
        )
    midpoint = dataset.quote.mid_value
    if midpoint is None:
        midpoint = (bid + ask) / 2.0
    if midpoint <= 0.0:
        return _refusal(
            analysis_kind="instrument_quote",
            reason_code="quote_midpoint_invalid",
            reason="the Saxo quote midpoint is invalid",
            datasets=(dataset.dataset_id,),
            handles=(dataset.instrument_handle,),
            warnings=tuple(warnings),
        )
    spread = ask - bid
    if dataset.quote.freshness == "stale" or dataset.quality_state is QualityState.STALE:
        warnings.add("quote_stale")
    if (dataset.delayed_by_minutes or 0) > 0 or normalized_price_type == "delayed":
        warnings.add("quote_delayed")
    if normalized_price_type == "indicative":
        warnings.add("quote_indicative")
    if normalized_price_type is None:
        warnings.add("quote_price_type_missing")
    elif normalized_price_type not in {"delayed", "indicative", "realtime"}:
        warnings.add("quote_price_type_unrecognized")
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("quote_quality_partial")
    status = ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE
    return InstrumentQuoteResearch(
        status=status,
        dataset_id=dataset.dataset_id,
        instrument_handle=dataset.instrument_handle,
        captured_at=dataset.quote.captured_at,
        bid=bid,
        ask=ask,
        midpoint=midpoint,
        spread=spread,
        spread_basis_points=spread / midpoint * 10_000.0,
        freshness=dataset.quote.freshness,
        delayed_by_minutes=dataset.delayed_by_minutes,
        warnings=tuple(sorted(warnings)),
    )


def build_instrument_dossier(
    instrument: ResolvedInstrument,
    price_dataset: PriceSeriesDataset,
    *,
    quote_dataset: QuoteResearchDataset | None = None,
    rolling_window: int,
    periods_per_year: float,
) -> InstrumentDossier | ResearchRefusal:
    """Build one compact dossier without accepting raw Saxo identifiers or payloads."""
    if instrument.instrument_handle != price_dataset.instrument_handle:
        return _refusal(
            analysis_kind="instrument_dossier",
            reason_code="instrument_handle_mismatch",
            reason="the resolved instrument and price dataset handles differ",
            datasets=(price_dataset.dataset_id,),
            handles=(instrument.instrument_handle, price_dataset.instrument_handle),
        )
    prices = analyze_instrument_prices(
        price_dataset,
        rolling_window=rolling_window,
        periods_per_year=periods_per_year,
    )
    if isinstance(prices, ResearchRefusal):
        return prices
    quote: InstrumentQuoteResearch | None = None
    warnings = set(prices.warnings)
    dataset_ids = [price_dataset.dataset_id]
    if quote_dataset is None:
        warnings.add("quote_not_supplied")
    elif quote_dataset.instrument_handle != instrument.instrument_handle:
        warnings.add("quote_handle_mismatch")
    else:
        dataset_ids.append(quote_dataset.dataset_id)
        quote_result = analyze_quote(quote_dataset)
        if isinstance(quote_result, ResearchRefusal):
            warnings.add(quote_result.reason_code)
            warnings.update(quote_result.warnings)
        else:
            quote = quote_result
            warnings.update(quote.warnings)
    return InstrumentDossier(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        instrument_handle=instrument.instrument_handle,
        dataset_ids=tuple(dataset_ids),
        display_label=instrument.display_label,
        symbol=instrument.symbol,
        asset_type=instrument.asset_type,
        exchange=instrument.exchange,
        prices=prices,
        quote=quote,
        warnings=tuple(sorted(warnings)),
    )


__all__ = (
    "EntitlementState",
    "InstrumentDossier",
    "InstrumentPriceResearch",
    "InstrumentQuoteResearch",
    "PriceSeriesDataset",
    "QuoteResearchDataset",
    "ResearchRefusal",
    "ResearchStatus",
    "ReturnSeriesRequest",
    "TimedValue",
    "analyze_instrument_prices",
    "analyze_quote",
    "assess_price_quality",
    "build_instrument_dossier",
)
