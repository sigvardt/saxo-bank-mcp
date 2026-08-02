from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from statistics import stdev
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import (
    PriceSeriesDataset,
    QuoteResearchDataset,
    ResearchRefusal,
    ResearchStatus,
    analyze_instrument_prices,
    analyze_quote,
    build_instrument_dossier,
)
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import QualityState
from saxo_bank_mcp.analytics_resolver import InstrumentState, ResolvedInstrument
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow, QuoteDatasetRow

_HANDLE = "ih_00000000000040008000000000000001"
_DATASET = "ds_00000000000040008000000000000001"
_QUOTE_DATASET = "ds_00000000000040008000000000000002"
_START = datetime(2026, 1, 2, tzinfo=UTC)


def _prices(
    closes: tuple[float, ...] = (100.0, 110.0, 99.0, 120.0),
    *,
    quality: QualityState = QualityState.COMPLETE,
    missing_intervals: int = 0,
) -> PriceSeriesDataset:
    bars = tuple(
        PriceBarDatasetRow(
            instrument_handle=_HANDLE,
            bar_time=_START + timedelta(days=index),
            interval=ChartInterval.ONE_DAY,
            open_value=close - 1.0,
            high_value=close + 2.0,
            low_value=close - 2.0,
            close_value=close,
            volume_value=1_000.0 + index,
            adjusted=False,
        )
        for index, close in enumerate(closes)
    )
    return PriceSeriesDataset(
        dataset_id=_DATASET,
        instrument_handle=_HANDLE,
        bars=bars,
        quality_state=quality,
        missing_interval_count=missing_intervals,
        return_series_label="price_return",
        adjustment_status="unadjusted",
        warnings=(),
    )


def _instrument() -> ResolvedInstrument:
    return ResolvedInstrument(
        instrument_handle=_HANDLE,
        display_label="Synthetic instrument",
        symbol="SYN",
        asset_type="Stock",
        exchange="XCSE",
        state=InstrumentState.CURRENT,
    )


def _quote(
    *,
    freshness: Literal["fresh", "stale"] = "fresh",
    delayed: int | None = 0,
) -> QuoteResearchDataset:
    return QuoteResearchDataset(
        dataset_id=_QUOTE_DATASET,
        instrument_handle=_HANDLE,
        quote=QuoteDatasetRow(
            instrument_handle=_HANDLE,
            captured_at=_START + timedelta(days=3),
            bid_value=119.0,
            ask_value=121.0,
            mid_value=120.0,
            freshness=freshness,
            warnings=(),
        ),
        quality_state=(QualityState.STALE if freshness == "stale" else QualityState.COMPLETE),
        entitlement_state="available",
        delayed_by_minutes=delayed,
        price_type="Delayed" if delayed else "RealTime",
        warnings=(),
    )


def test_golden_price_risk_drawdown_and_rolling_values_use_task11_metrics() -> None:
    result = analyze_instrument_prices(
        _prices(),
        rolling_window=2,
        periods_per_year=252.0,
        requested_return="price_return",
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.return_label == "price_return"
    assert result.price_return == pytest.approx(0.2)
    expected_returns = (0.1, -0.1, 120.0 / 99.0 - 1.0)
    assert result.period_returns == pytest.approx(expected_returns)
    assert result.annualized_volatility == pytest.approx(stdev(expected_returns) * math.sqrt(252.0))
    assert result.maximum_drawdown == pytest.approx(-0.1)
    assert tuple(point.value for point in result.rolling_returns) == pytest.approx(
        (99.0 / 100.0 - 1.0, 120.0 / 110.0 - 1.0),
    )
    assert "unadjusted_price_series" in result.warnings
    assert "total return" in result.does_not_verify


@pytest.mark.parametrize("requested", ["adjusted_price_return", "total_return"])
def test_non_price_return_requests_refuse_the_unadjusted_saxo_chart_series(
    requested: Literal["adjusted_price_return", "total_return"],
) -> None:
    result = analyze_instrument_prices(
        _prices(),
        rolling_window=2,
        periods_per_year=252.0,
        requested_return=requested,
    )

    assert isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REFUSED
    assert result.reason_code == "return_series_unavailable"
    assert result.dataset_ids == (_DATASET,)


def test_missing_prices_refuse_and_partial_coverage_stays_explicit() -> None:
    missing = analyze_instrument_prices(
        _prices((100.0,)),
        rolling_window=1,
        periods_per_year=252.0,
    )
    partial = analyze_instrument_prices(
        _prices(quality=QualityState.PARTIAL, missing_intervals=1),
        rolling_window=2,
        periods_per_year=252.0,
    )

    assert isinstance(missing, ResearchRefusal)
    assert missing.reason_code == "insufficient_price_history"
    assert not isinstance(partial, ResearchRefusal)
    assert partial.status is ResearchStatus.REDUCED
    assert "incomplete_price_coverage" in partial.warnings


@pytest.mark.parametrize(
    ("freshness", "delayed", "warning"),
    [("stale", 0, "quote_stale"), ("fresh", 15, "quote_delayed")],
)
def test_dossier_reduces_stale_or_delayed_quote_claims(
    freshness: Literal["fresh", "stale"],
    delayed: int,
    warning: str,
) -> None:
    result = build_instrument_dossier(
        _instrument(),
        _prices(),
        quote_dataset=_quote(freshness=freshness, delayed=delayed),
        rolling_window=2,
        periods_per_year=252.0,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert warning in result.warnings
    assert result.source_scope == "saxo_openapi"
    assert result.instrument_handle == _HANDLE


def test_dossier_refuses_a_quote_marked_missing_even_when_values_are_present() -> None:
    result = build_instrument_dossier(
        _instrument(),
        _prices(),
        quote_dataset=_quote().model_copy(update={"quality_state": QualityState.MISSING}),
        rolling_window=2,
        periods_per_year=252.0,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.quote is None
    assert "quote_data_unusable" in result.warnings


def test_noaccess_price_type_never_becomes_a_complete_quote() -> None:
    dataset = _quote().model_copy(
        update={
            "entitlement_state": "available",
            "price_type": "NoAccess",
            "quality_state": QualityState.COMPLETE,
        },
    )

    result = analyze_quote(dataset)

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "quote_entitlement_insufficient"


def test_raw_broker_identifier_cannot_replace_an_opaque_instrument_handle() -> None:
    with pytest.raises(ValidationError):
        PriceSeriesDataset(
            dataset_id=_DATASET,
            instrument_handle="123456",
            bars=(),
            quality_state=QualityState.MISSING,
            missing_interval_count=0,
            return_series_label="price_return",
            adjustment_status="unadjusted",
            warnings=(),
        )
