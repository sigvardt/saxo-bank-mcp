from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import pstdev, stdev

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from saxo_bank_mcp.analytics_indicators import (
    IndicatorParameters,
    ResearchRefusal,
    ResearchStatus,
    average_true_range,
    bollinger_bands,
    calculate_indicators,
    exponential_moving_average,
    moving_average_convergence_divergence,
    relative_strength_index,
    simple_moving_average,
)
from saxo_bank_mcp.analytics_instruments import PriceSeriesDataset
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import QualityState
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow

_HANDLE = "ih_00000000000040008000000000000003"
_DATASET = "ds_00000000000040008000000000000003"
_START = datetime(2026, 2, 2, tzinfo=UTC)
_PROPERTY_SETTINGS = settings(max_examples=40, deadline=None, derandomize=True)


def _dataset(
    closes: tuple[float, ...] = (10.0, 11.0, 12.0, 11.0, 13.0, 14.0),
    *,
    missing_ohlc: bool = False,
    missing_volume: bool = False,
) -> PriceSeriesDataset:
    bars = tuple(
        PriceBarDatasetRow(
            instrument_handle=_HANDLE,
            bar_time=_START + timedelta(days=index),
            interval=ChartInterval.ONE_DAY,
            open_value=None if missing_ohlc else close - 0.5,
            high_value=None if missing_ohlc else close + 1.0,
            low_value=None if missing_ohlc else close - 1.0,
            close_value=close,
            volume_value=None if missing_volume else float(100 + index * 10),
            adjusted=False,
        )
        for index, close in enumerate(closes)
    )
    return PriceSeriesDataset(
        dataset_id=_DATASET,
        instrument_handle=_HANDLE,
        bars=bars,
        quality_state=QualityState.COMPLETE,
        missing_interval_count=0,
        return_series_label="price_return",
        adjustment_status="unadjusted",
        warnings=(),
    )


def _reference_ema(values: tuple[float, ...], period: int) -> tuple[float, ...]:
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return tuple(result)


def _reference_rsi(values: tuple[float, ...], period: int) -> tuple[float, ...]:
    changes = [right - left for left, right in pairwise(values)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def score() -> float:
        if average_loss == 0.0:
            return 50.0 if average_gain == 0.0 else 100.0
        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    result = [score()]
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        result.append(score())
    return tuple(result)


def test_golden_indicator_formulas_match_independent_scalar_calculations() -> None:
    values = (10.0, 11.0, 12.0, 11.0, 13.0, 14.0)

    assert simple_moving_average(values, 3) == pytest.approx((11.0, 34.0 / 3.0, 12.0, 38.0 / 3.0))
    assert exponential_moving_average(values, 3) == pytest.approx(_reference_ema(values, 3))
    assert relative_strength_index(values, 3) == pytest.approx(_reference_rsi(values, 3))

    macd = moving_average_convergence_divergence(
        values,
        fast_period=2,
        slow_period=3,
        signal_period=2,
    )
    expected_fast = _reference_ema(values, 2)
    expected_slow = _reference_ema(values, 3)
    expected_line = tuple(
        left - right for left, right in zip(expected_fast, expected_slow, strict=True)
    )
    expected_signal = _reference_ema(expected_line, 2)
    assert macd.line == pytest.approx(expected_line)
    assert macd.signal == pytest.approx(expected_signal)
    assert macd.histogram == pytest.approx(
        tuple(left - right for left, right in zip(expected_line, expected_signal, strict=True)),
    )

    bands = bollinger_bands(values, window=3, width=2.0)
    expected_middle = values[-3:]
    middle = sum(expected_middle) / 3.0
    width = 2.0 * pstdev(expected_middle)
    assert bands[-1].middle == pytest.approx(middle)
    assert bands[-1].lower == pytest.approx(middle - width)
    assert bands[-1].upper == pytest.approx(middle + width)


def test_snapshot_covers_trend_momentum_volume_and_realized_volatility() -> None:
    dataset = _dataset()
    parameters = IndicatorParameters(
        moving_average_window=3,
        rsi_period=3,
        macd_fast_period=2,
        macd_slow_period=3,
        macd_signal_period=2,
        atr_period=3,
        bollinger_window=3,
        bollinger_width=2.0,
        momentum_lookback=2,
        periods_per_year=252.0,
        level_wing=1,
    )

    result = calculate_indicators(dataset, parameters)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.descriptive_only is True
    assert result.is_forecast is False
    assert result.momentum == pytest.approx(14.0 / 11.0 - 1.0)
    log_returns = [
        math.log(right / left)
        for left, right in zip((10, 11, 12, 11, 13), (11, 12, 11, 13, 14), strict=True)
    ]
    assert result.realized_volatility == pytest.approx(stdev(log_returns) * math.sqrt(252.0))
    assert result.volume_total == pytest.approx(sum(100 + index * 10 for index in range(6)))
    assert result.volume_total is not None
    expected_vwap = (
        sum(close * (100 + index * 10) for index, close in enumerate((10, 11, 12, 11, 13, 14)))
        / result.volume_total
    )
    assert result.volume_weighted_price == pytest.approx(expected_vwap)
    assert "unadjusted_price_series" in result.warnings


def test_missing_ohlc_or_volume_reduces_only_the_dependent_indicators() -> None:
    parameters = IndicatorParameters(
        moving_average_window=3,
        rsi_period=3,
        macd_fast_period=2,
        macd_slow_period=3,
        macd_signal_period=2,
        atr_period=3,
        bollinger_window=3,
        bollinger_width=2.0,
        momentum_lookback=2,
        periods_per_year=252.0,
        level_wing=1,
    )

    no_ohlc = calculate_indicators(_dataset(missing_ohlc=True), parameters)
    no_volume = calculate_indicators(_dataset(missing_volume=True), parameters)

    assert not isinstance(no_ohlc, ResearchRefusal)
    assert no_ohlc.atr is None
    assert "ohlc_missing" in no_ohlc.warnings
    assert not isinstance(no_volume, ResearchRefusal)
    assert no_volume.volume_weighted_price is None
    assert "volume_missing" in no_volume.warnings


def test_insufficient_indicator_warmup_refuses_instead_of_shortening_windows() -> None:
    result = calculate_indicators(
        _dataset((10.0, 11.0)),
        IndicatorParameters(
            moving_average_window=3,
            rsi_period=3,
            macd_fast_period=2,
            macd_slow_period=3,
            macd_signal_period=2,
            atr_period=3,
            bollinger_window=3,
            bollinger_width=2.0,
            momentum_lookback=2,
            periods_per_year=252.0,
            level_wing=1,
        ),
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "indicator_warmup_incomplete"


@seed(2026080201)
@_PROPERTY_SETTINGS
@given(
    values=st.lists(
        st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
        min_size=4,
        max_size=20,
    ),
    scale=st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
)
def test_property_price_scaling_scales_moving_averages_and_leaves_rsi_unchanged(
    values: list[float],
    scale: float,
) -> None:
    scaled = [value * scale for value in values]

    assert simple_moving_average(scaled, 3) == pytest.approx(
        tuple(value * scale for value in simple_moving_average(values, 3)),
    )
    assert relative_strength_index(scaled, 3) == pytest.approx(relative_strength_index(values, 3))


def test_atr_matches_an_independent_true_range_wilder_loop() -> None:
    bars = _dataset().bars
    actual = average_true_range(bars, period=3)
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        assert bar.high_value is not None
        assert bar.low_value is not None
        if index == 0:
            true_ranges.append(bar.high_value - bar.low_value)
        else:
            previous_close = bars[index - 1].close_value
            true_ranges.append(
                max(
                    bar.high_value - bar.low_value,
                    abs(bar.high_value - previous_close),
                    abs(bar.low_value - previous_close),
                ),
            )
    expected = [sum(true_ranges[:3]) / 3.0]
    for value in true_ranges[3:]:
        expected.append((expected[-1] * 2.0 + value) / 3.0)
    assert actual == pytest.approx(expected)
