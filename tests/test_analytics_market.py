from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import (
    PriceSeriesDataset,
    QuoteResearchDataset,
    ResearchRefusal,
    ResearchStatus,
)
from saxo_bank_mcp.analytics_market import (
    BoundedResearchUniverse,
    DepthLevel,
    MarketDepthDataset,
    SavedCondition,
    WrapperCostDataset,
    analyze_bounded_market,
    analyze_entitled_depth,
    check_saved_conditions,
    compare_wrappers,
    prepare_bounded_session,
)
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import QualityState
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow, QuoteDatasetRow

_START = datetime(2026, 3, 2, tzinfo=UTC)
_UNIVERSE_ID = "un_00000000000040008000000000000001"
_PROPERTY_SETTINGS = settings(max_examples=40, deadline=None, derandomize=True)


def _handle(index: int) -> str:
    return f"ih_0000000000004000800000000000{index:04x}"


def _dataset_id(index: int) -> str:
    return f"ds_0000000000004000800000000000{index:04x}"


def _series(
    index: int,
    closes: tuple[float, ...],
    *,
    day_offsets: tuple[int, ...] | None = None,
) -> PriceSeriesDataset:
    handle = _handle(index)
    offsets = day_offsets if day_offsets is not None else tuple(range(len(closes)))
    if len(offsets) != len(closes):
        raise ValueError("test price offsets must match closes")
    return PriceSeriesDataset(
        dataset_id=_dataset_id(index),
        instrument_handle=handle,
        bars=tuple(
            PriceBarDatasetRow(
                instrument_handle=handle,
                bar_time=_START + timedelta(days=offset),
                interval=ChartInterval.ONE_DAY,
                open_value=close,
                high_value=close + 1.0,
                low_value=close - 1.0,
                close_value=close,
                volume_value=1_000.0,
                adjusted=False,
            )
            for offset, close in zip(offsets, closes, strict=True)
        ),
        quality_state=QualityState.COMPLETE,
        missing_interval_count=0,
        return_series_label="price_return",
        adjustment_status="unadjusted",
        warnings=(),
    )


def _quote(index: int, bid: float, ask: float) -> QuoteResearchDataset:
    handle = _handle(index)
    return QuoteResearchDataset(
        dataset_id=_dataset_id(100 + index),
        instrument_handle=handle,
        quote=QuoteDatasetRow(
            instrument_handle=handle,
            captured_at=_START + timedelta(days=3),
            bid_value=bid,
            ask_value=ask,
            mid_value=(bid + ask) / 2.0,
            freshness="fresh",
            warnings=(),
        ),
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
        delayed_by_minutes=0,
        price_type="RealTime",
        warnings=(),
    )


def _universe(series: tuple[PriceSeriesDataset, ...]) -> BoundedResearchUniverse:
    return BoundedResearchUniverse(
        scope="saved_universe",
        universe_id=_UNIVERSE_ID,
        series=series,
    )


def _pearson(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    denominator = math.sqrt(sum((value - left_mean) ** 2 for value in left)) * math.sqrt(
        sum((value - right_mean) ** 2 for value in right),
    )
    return numerator / denominator


def test_bounded_movers_breadth_comparison_and_correlation_never_claim_the_market() -> None:
    first = _series(1, (100.0, 102.0, 101.0, 105.0))
    second = _series(2, (100.0, 99.0, 101.0, 100.0))
    third = _series(3, (100.0, 100.0, 100.0, 100.0))

    result = analyze_bounded_market(_universe((first, second, third)), periods_per_year=252.0)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.whole_market is False
    assert result.scope_statement == "within the selected saved Saxo universe of 3 instruments"
    assert tuple(item.instrument_handle for item in result.movers) == (
        first.instrument_handle,
        third.instrument_handle,
        second.instrument_handle,
    )
    assert result.advancers == 1
    assert result.decliners == 1
    assert result.unchanged == 1
    assert result.breadth == pytest.approx(0.0)

    first_returns = tuple(
        right / left - 1.0
        for left, right in zip((100.0, 102.0, 101.0), (102.0, 101.0, 105.0), strict=True)
    )
    second_returns = tuple(
        right / left - 1.0
        for left, right in zip((100.0, 99.0, 101.0), (99.0, 101.0, 100.0), strict=True)
    )
    pair = next(
        value
        for value in result.correlations
        if {value.left_handle, value.right_handle}
        == {first.instrument_handle, second.instrument_handle}
    )
    assert pair.value == pytest.approx(_pearson(first_returns, second_returns))
    assert result.correlation_regime in {"low", "mixed", "high"}
    assert result.volatility_regime in {"low", "moderate", "high"}


def test_universe_hard_limit_refuses_more_than_25_safe_instruments() -> None:
    with pytest.raises(ValidationError):
        _universe(tuple(_series(index, (100.0, 101.0, 102.0)) for index in range(1, 27)))


def test_correlation_aligns_both_period_endpoints_before_enforcing_minimum() -> None:
    left = _series(1, (100.0, 101.0, 102.0, 103.0))
    right = _series(
        2,
        (200.0, 202.0, 204.0),
        day_offsets=(0, 2, 3),
    )

    result = analyze_bounded_market(_universe((left, right)), periods_per_year=252.0)

    assert not isinstance(result, ResearchRefusal)
    assert result.correlations == ()
    assert "correlation_alignment_insufficient" in result.warnings


def test_missing_regime_inputs_are_unavailable_instead_of_low() -> None:
    result = analyze_bounded_market(
        _universe((_series(1, (100.0, 101.0)),)),
        periods_per_year=252.0,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.correlation_regime is None
    assert result.volatility_regime is None
    assert set(result.warnings) >= {
        "correlation_regime_unavailable",
        "volatility_regime_unavailable",
    }


def test_entitled_depth_computes_spread_depth_and_imbalance() -> None:
    dataset = MarketDepthDataset(
        dataset_id=_dataset_id(50),
        instrument_handle=_handle(1),
        captured_at=_START,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
        delayed_by_minutes=0,
        bids=(DepthLevel(price=99.0, size=10.0), DepthLevel(price=98.0, size=5.0)),
        asks=(DepthLevel(price=101.0, size=4.0), DepthLevel(price=102.0, size=6.0)),
    )

    result = analyze_entitled_depth(dataset)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.source_scope is None
    assert "market_depth_source_contract_unbound" in result.warnings
    assert result.spread == pytest.approx(2.0)
    assert result.bid_depth == pytest.approx(15.0)
    assert result.ask_depth == pytest.approx(10.0)
    assert result.depth_imbalance == pytest.approx(20.0)


def test_caller_availability_cannot_claim_unbound_depth_saxo_provenance() -> None:
    dataset = MarketDepthDataset(
        dataset_id=_dataset_id(53),
        instrument_handle=_handle(1),
        captured_at=_START,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
        delayed_by_minutes=0,
        bids=(DepthLevel(price=99.0, size=1.0),),
        asks=(DepthLevel(price=101.0, size=1.0),),
    )

    result = analyze_entitled_depth(dataset)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.source_scope is None
    assert "market_depth_source_contract_unbound" in result.warnings


@pytest.mark.parametrize("entitlement", ["denied", "partial"])
def test_depth_refuses_when_entitlement_is_not_complete(
    entitlement: Literal["denied", "partial"],
) -> None:
    dataset = MarketDepthDataset(
        dataset_id=_dataset_id(51),
        instrument_handle=_handle(1),
        captured_at=_START,
        quality_state=QualityState.MISSING,
        entitlement_state=entitlement,
        delayed_by_minutes=None,
        bids=(),
        asks=(),
    )

    result = analyze_entitled_depth(dataset)

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "market_depth_entitlement_insufficient"
    assert result.source_scope is None
    assert "saxo" not in result.reason.casefold()


def test_delayed_depth_is_reduced_without_hiding_the_delay() -> None:
    dataset = MarketDepthDataset(
        dataset_id=_dataset_id(52),
        instrument_handle=_handle(1),
        captured_at=_START,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
        delayed_by_minutes=15,
        bids=(DepthLevel(price=99.0, size=1.0),),
        asks=(DepthLevel(price=101.0, size=1.0),),
    )

    result = analyze_entitled_depth(dataset)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert "depth_delayed" in result.warnings


def test_wrapper_comparison_uses_only_same_exposure_horizon_and_currency() -> None:
    wrappers = (
        WrapperCostDataset(
            dataset_id=_dataset_id(60),
            instrument_handle=_handle(1),
            wrapper_label="cash_equity",
            exposure_amount=10_000.0,
            currency="DKK",
            horizon_days=30,
            opening_cost=20.0,
            holding_cost=0.0,
            closing_cost=20.0,
        ),
        WrapperCostDataset(
            dataset_id=_dataset_id(61),
            instrument_handle=_handle(2),
            wrapper_label="cfd",
            exposure_amount=10_000.0,
            currency="DKK",
            horizon_days=30,
            opening_cost=10.0,
            holding_cost=60.0,
            closing_cost=10.0,
        ),
    )

    result = compare_wrappers(wrappers)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    assert tuple(item.total_cost for item in result.wrappers) == pytest.approx((40.0, 80.0))
    assert result.cost_difference == pytest.approx(40.0)


def test_saved_condition_checks_use_a_fixed_catalog_instead_of_expressions() -> None:
    series = _series(1, (100.0, 102.0, 105.0, 110.0))
    quote = _quote(1, 109.0, 111.0)
    conditions = (
        SavedCondition(
            condition_id="price_gate",
            instrument_handle=series.instrument_handle,
            dataset_id=series.dataset_id,
            kind="price_above",
            threshold=108.0,
        ),
        SavedCondition(
            condition_id="return_gate",
            instrument_handle=series.instrument_handle,
            dataset_id=series.dataset_id,
            kind="return_above",
            threshold=0.05,
        ),
        SavedCondition(
            condition_id="spread_gate",
            instrument_handle=series.instrument_handle,
            dataset_id=quote.dataset_id,
            kind="spread_below",
            threshold=3.0,
        ),
    )

    result = check_saved_conditions(conditions, _universe((series,)), quotes=(quote,))

    assert not isinstance(result, ResearchRefusal)
    assert all(item.matched for item in result.checks)
    with pytest.raises(ValidationError):
        SavedCondition.model_validate(
            {
                "condition_id": "unsafe",
                "instrument_handle": series.instrument_handle,
                "dataset_id": series.dataset_id,
                "kind": "python_expression",
                "threshold": 0.0,
            },
        )


@pytest.mark.parametrize("quality", [QualityState.STALE, QualityState.INVALID])
def test_saved_price_conditions_refuse_unusable_price_quality(
    quality: QualityState,
) -> None:
    series = _series(1, (100.0, 110.0)).model_copy(update={"quality_state": quality})
    condition = SavedCondition(
        condition_id="unsafe_price_gate",
        instrument_handle=series.instrument_handle,
        dataset_id=series.dataset_id,
        kind="price_above",
        threshold=105.0,
    )

    result = check_saved_conditions((condition,), _universe((series,)))

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "price_data_unusable"


def test_saved_price_conditions_propagate_gaps_and_source_warnings() -> None:
    series = _series(1, (100.0, 110.0)).model_copy(
        update={
            "quality_state": QualityState.PARTIAL,
            "missing_interval_count": 1,
            "warnings": ("observed_interval_gap", "source_quality_limited"),
        },
    )
    condition = SavedCondition(
        condition_id="partial_price_gate",
        instrument_handle=series.instrument_handle,
        dataset_id=series.dataset_id,
        kind="price_above",
        threshold=105.0,
    )

    result = check_saved_conditions((condition,), _universe((series,)))

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.checks[0].matched is True
    assert set(result.warnings) >= {
        "incomplete_price_coverage",
        "observed_interval_gap",
        "source_quality_limited",
    }


def test_session_preparation_is_bounded_and_reports_quote_quality() -> None:
    result = prepare_bounded_session(
        _universe((_series(1, (100.0, 101.0, 102.0)),)),
        quotes=(_quote(1, 101.0, 103.0),),
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.whole_market is False
    assert len(result.items) == 1
    assert result.items[0].spread == pytest.approx(2.0)


@seed(2026080202)
@_PROPERTY_SETTINGS
@given(
    latest_returns=st.lists(
        st.floats(min_value=-0.5, max_value=0.5, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=25,
    ),
)
def test_property_breadth_is_bounded_for_every_allowed_universe(
    latest_returns: list[float],
) -> None:
    series = tuple(
        _series(index + 1, (100.0, 101.0, 101.0 * (1.0 + latest_return)))
        for index, latest_return in enumerate(latest_returns)
    )

    result = analyze_bounded_market(_universe(series), periods_per_year=252.0)

    assert not isinstance(result, ResearchRefusal)
    assert -1.0 <= result.breadth <= 1.0
    assert result.advancers + result.decliners + result.unchanged == len(latest_returns)
