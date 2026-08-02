from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise
from statistics import pstdev
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import (
    PriceSeriesDataset,
    ResearchRefusal,
    ResearchStatus,
)
from saxo_bank_mcp.analytics_metrics import FinancialMetricError, log_returns, volatility
from saxo_bank_mcp.analytics_models import DatasetId, InstrumentHandle, QualityState
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow

_SOURCE_SCOPE: Final = "saxo_openapi"
_MIN_BOLLINGER_OBSERVATIONS: Final = 2


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class IndicatorParameters(_StrictModel):
    """Fixed, explicit windows for a descriptive technical snapshot."""

    moving_average_window: int = Field(ge=1)
    rsi_period: int = Field(ge=1)
    macd_fast_period: int = Field(ge=1)
    macd_slow_period: int = Field(ge=2)
    macd_signal_period: int = Field(ge=1)
    atr_period: int = Field(ge=1)
    bollinger_window: int = Field(ge=2)
    bollinger_width: float = Field(gt=0, allow_inf_nan=False)
    momentum_lookback: int = Field(ge=1)
    periods_per_year: float = Field(gt=0, allow_inf_nan=False)
    level_wing: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_macd_windows(self) -> Self:
        if self.macd_fast_period >= self.macd_slow_period:
            raise ValueError("MACD fast period must be shorter than slow period")
        return self


class MacdSeries(_StrictModel):
    line: tuple[float, ...]
    signal: tuple[float, ...]
    histogram: tuple[float, ...]


class BollingerPoint(_StrictModel):
    window_end_index: int = Field(ge=0)
    lower: float = Field(allow_inf_nan=False)
    middle: float = Field(allow_inf_nan=False)
    upper: float = Field(allow_inf_nan=False)


class IndicatorResearch(_StrictModel):
    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["technical_indicators"] = "technical_indicators"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    moving_average: float = Field(allow_inf_nan=False)
    rsi: float = Field(ge=0, le=100, allow_inf_nan=False)
    macd_line: float = Field(allow_inf_nan=False)
    macd_signal: float = Field(allow_inf_nan=False)
    macd_histogram: float = Field(allow_inf_nan=False)
    bollinger_lower: float = Field(allow_inf_nan=False)
    bollinger_middle: float = Field(allow_inf_nan=False)
    bollinger_upper: float = Field(allow_inf_nan=False)
    atr: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    momentum: float = Field(allow_inf_nan=False)
    realized_volatility: float = Field(ge=0, allow_inf_nan=False)
    volume_total: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    volume_weighted_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    trend_state: Literal["rising", "falling", "mixed"]
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    warnings: tuple[str, ...]
    descriptive_only: Literal[True] = True
    is_forecast: Literal[False] = False


def _finite_values(values: Sequence[float], *, minimum_count: int = 1) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < minimum_count:
        raise ValueError(f"at least {minimum_count} values are required")
    if not all(math.isfinite(value) for value in result):
        raise ValueError("indicator values must be finite")
    return result


def _positive_period(period: int) -> int:
    if isinstance(period, bool) or period < 1:
        raise ValueError("indicator period must be a positive integer")
    return period


def simple_moving_average(values: Sequence[float], period: int) -> tuple[float, ...]:
    """Return every complete arithmetic moving-average window."""
    window = _positive_period(period)
    series = _finite_values(values, minimum_count=window)
    return tuple(
        math.fsum(series[index - window : index]) / window
        for index in range(window, len(series) + 1)
    )


def exponential_moving_average(values: Sequence[float], period: int) -> tuple[float, ...]:
    """Return an EMA seeded with the first supplied observation."""
    window = _positive_period(period)
    series = _finite_values(values)
    alpha = 2.0 / (window + 1.0)
    result = [series[0]]
    for value in series[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return tuple(result)


def relative_strength_index(values: Sequence[float], period: int) -> tuple[float, ...]:
    """Return Wilder RSI values after the exact declared warm-up."""
    window = _positive_period(period)
    series = _finite_values(values, minimum_count=window + 1)
    changes = tuple(right - left for left, right in pairwise(series))
    gains = tuple(max(change, 0.0) for change in changes)
    losses = tuple(max(-change, 0.0) for change in changes)
    average_gain = math.fsum(gains[:window]) / window
    average_loss = math.fsum(losses[:window]) / window

    def _score() -> float:
        if average_loss == 0.0:
            return 50.0 if average_gain == 0.0 else 100.0
        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    result = [_score()]
    for gain, loss in zip(gains[window:], losses[window:], strict=True):
        average_gain = (average_gain * (window - 1) + gain) / window
        average_loss = (average_loss * (window - 1) + loss) / window
        result.append(_score())
    return tuple(result)


def moving_average_convergence_divergence(
    values: Sequence[float],
    *,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> MacdSeries:
    """Return a same-length EMA-seeded MACD series and signal."""
    fast = _positive_period(fast_period)
    slow = _positive_period(slow_period)
    signal = _positive_period(signal_period)
    if fast >= slow:
        raise ValueError("MACD fast period must be shorter than slow period")
    series = _finite_values(values, minimum_count=slow)
    fast_values = exponential_moving_average(series, fast)
    slow_values = exponential_moving_average(series, slow)
    line = tuple(left - right for left, right in zip(fast_values, slow_values, strict=True))
    signal_values = exponential_moving_average(line, signal)
    return MacdSeries(
        line=line,
        signal=signal_values,
        histogram=tuple(
            value - signal_value for value, signal_value in zip(line, signal_values, strict=True)
        ),
    )


def bollinger_bands(
    values: Sequence[float],
    *,
    window: int,
    width: float,
) -> tuple[BollingerPoint, ...]:
    """Return population-standard-deviation Bollinger bands."""
    period = _positive_period(window)
    if period < _MIN_BOLLINGER_OBSERVATIONS:
        raise ValueError("Bollinger window must contain at least two observations")
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError("Bollinger width must be positive and finite")
    series = _finite_values(values, minimum_count=period)
    result: list[BollingerPoint] = []
    for index in range(period, len(series) + 1):
        sample = series[index - period : index]
        middle = math.fsum(sample) / period
        distance = width * pstdev(sample)
        result.append(
            BollingerPoint(
                window_end_index=index - 1,
                lower=middle - distance,
                middle=middle,
                upper=middle + distance,
            ),
        )
    return tuple(result)


def average_true_range(
    bars: Sequence[PriceBarDatasetRow],
    *,
    period: int,
) -> tuple[float, ...]:
    """Return Wilder ATR when every requested Saxo OHLC field is present."""
    window = _positive_period(period)
    if len(bars) < window:
        raise ValueError("ATR warm-up is incomplete")
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        high = bar.high_value
        low = bar.low_value
        if high is None or low is None:
            raise ValueError("ATR requires high and low fields")
        if not math.isfinite(high) or not math.isfinite(low) or high < low:
            raise ValueError("ATR high and low fields are invalid")
        if index == 0:
            true_ranges.append(high - low)
        else:
            previous_close = bars[index - 1].close_value
            true_ranges.append(
                max(high - low, abs(high - previous_close), abs(low - previous_close)),
            )
    result = [math.fsum(true_ranges[:window]) / window]
    for true_range in true_ranges[window:]:
        result.append((result[-1] * (window - 1) + true_range) / window)
    return tuple(result)


def _price_levels(
    bars: Sequence[PriceBarDatasetRow],
    wing: int,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    supports: list[float] = []
    resistances: list[float] = []
    for index in range(wing, len(bars) - wing):
        center = bars[index].close_value
        neighbours = tuple(
            bars[other].close_value
            for other in range(index - wing, index + wing + 1)
            if other != index
        )
        if all(center < value for value in neighbours):
            supports.append(center)
        if all(center > value for value in neighbours):
            resistances.append(center)
    return tuple(supports), tuple(resistances)


def _refusal(dataset: PriceSeriesDataset, reason_code: str, reason: str) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="technical_indicators",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(dataset.instrument_handle,),
        warnings=dataset.warnings,
    )


def calculate_indicators(
    dataset: PriceSeriesDataset,
    parameters: IndicatorParameters,
) -> IndicatorResearch | ResearchRefusal:
    """Calculate descriptive indicators from one bounded Saxo chart dataset."""
    if dataset.quality_state in {
        QualityState.MISSING,
        QualityState.INVALID,
        QualityState.STALE,
    }:
        return _refusal(
            dataset,
            "indicator_dataset_unusable",
            "the bounded Saxo price dataset is missing, invalid, or stale",
        )
    required = max(
        parameters.moving_average_window,
        parameters.rsi_period + 1,
        parameters.macd_slow_period,
        parameters.atr_period,
        parameters.bollinger_window,
        parameters.momentum_lookback + 1,
        parameters.level_wing * 2 + 1,
        3,
    )
    if len(dataset.bars) < required:
        return ResearchRefusal(
            analysis_kind="technical_indicators",
            reason_code="indicator_warmup_incomplete",
            reason="the exact declared indicator windows are not fully available",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(dataset.instrument_handle,),
            missing_fields=("indicator_warmup",),
            warnings=dataset.warnings,
        )

    closes = tuple(bar.close_value for bar in dataset.bars)
    moving_averages = list(simple_moving_average(closes, parameters.moving_average_window))
    rsi_values = relative_strength_index(closes, parameters.rsi_period)
    macd = moving_average_convergence_divergence(
        closes,
        fast_period=parameters.macd_fast_period,
        slow_period=parameters.macd_slow_period,
        signal_period=parameters.macd_signal_period,
    )
    bands = bollinger_bands(
        closes,
        window=parameters.bollinger_window,
        width=parameters.bollinger_width,
    )
    try:
        realized_volatility = volatility(
            log_returns(closes),
            parameters.periods_per_year,
        )
    except FinancialMetricError:
        return _refusal(
            dataset,
            "indicator_volatility_undefined",
            "the bounded Saxo closes do not support realized volatility",
        )

    warnings = set(dataset.warnings)
    warnings.add("unadjusted_price_series")
    if dataset.quality_state is QualityState.PARTIAL or dataset.missing_interval_count:
        warnings.add("incomplete_price_coverage")
    try:
        atr = average_true_range(dataset.bars, period=parameters.atr_period)[-1]
    except ValueError:
        atr = None
        warnings.add("ohlc_missing")

    volumes = tuple(bar.volume_value for bar in dataset.bars)
    volume_total: float | None = None
    volume_weighted_price: float | None = None
    if all(value is not None and value >= 0.0 for value in volumes):
        complete_volumes = tuple(float(value) for value in volumes if value is not None)
        volume_total = math.fsum(complete_volumes)
        if volume_total > 0.0:
            volume_weighted_price = (
                math.fsum(
                    close * volume for close, volume in zip(closes, complete_volumes, strict=True)
                )
                / volume_total
            )
        else:
            warnings.add("volume_zero")
    else:
        warnings.add("volume_missing")

    support_levels, resistance_levels = _price_levels(dataset.bars, parameters.level_wing)
    latest_average = moving_averages[-1]
    previous_average = moving_averages[-2] if len(moving_averages) > 1 else latest_average
    if closes[-1] > latest_average > previous_average:
        trend_state = "rising"
    elif closes[-1] < latest_average < previous_average:
        trend_state = "falling"
    else:
        trend_state = "mixed"

    latest_band = bands[-1]
    return IndicatorResearch(
        status=ResearchStatus.REDUCED,
        dataset_id=dataset.dataset_id,
        instrument_handle=dataset.instrument_handle,
        moving_average=latest_average,
        rsi=rsi_values[-1],
        macd_line=macd.line[-1],
        macd_signal=macd.signal[-1],
        macd_histogram=macd.histogram[-1],
        bollinger_lower=latest_band.lower,
        bollinger_middle=latest_band.middle,
        bollinger_upper=latest_band.upper,
        atr=atr,
        momentum=closes[-1] / closes[-1 - parameters.momentum_lookback] - 1.0,
        realized_volatility=realized_volatility,
        volume_total=volume_total,
        volume_weighted_price=volume_weighted_price,
        trend_state=trend_state,
        support_levels=support_levels,
        resistance_levels=resistance_levels,
        warnings=tuple(sorted(warnings)),
    )


__all__ = (
    "BollingerPoint",
    "IndicatorParameters",
    "IndicatorResearch",
    "MacdSeries",
    "ResearchRefusal",
    "ResearchStatus",
    "average_true_range",
    "bollinger_bands",
    "calculate_indicators",
    "exponential_moving_average",
    "moving_average_convergence_divergence",
    "relative_strength_index",
    "simple_moving_average",
)
