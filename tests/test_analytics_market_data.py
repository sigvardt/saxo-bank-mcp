# ruff: noqa: PLR2004

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from saxo_bank_mcp.analytics_market_data import (
    ChartInterval,
    MarketDataValidationError,
    normalize_option_chain,
    normalize_price_series,
    normalize_quote,
)
from saxo_bank_mcp.analytics_models import HandleKind, new_safe_handle


def test_chart_time_uses_its_exchange_offset_and_labels_price_return() -> None:
    handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    series = normalize_price_series(
        rows=(
            {
                "CloseBid": 101.0,
                "HighBid": 102.0,
                "LowBid": 99.0,
                "OpenBid": 100.0,
                "PriceType": "RealTime",
                "Time": "2026-03-30T09:00:00+02:00",
                "Volume": 12,
            },
        ),
        instrument_handle=handle,
        interval=ChartInterval.ONE_MINUTE,
        start=datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        end=datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
    )

    assert series.bars[0].bar_time == datetime(2026, 3, 30, 7, 0, tzinfo=UTC)
    assert series.return_series_label == "price_return"
    assert series.adjustment_status == "unadjusted"


def test_chart_gap_and_missing_volume_remain_explicit() -> None:
    handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    series = normalize_price_series(
        rows=(
            {
                "CloseBid": 101.0,
                "Time": "2026-03-30T09:00:00+02:00",
                "Volume": 12,
            },
            {
                "CloseBid": 103.0,
                "Time": "2026-03-30T09:02:00+02:00",
            },
        ),
        instrument_handle=handle,
        interval=ChartInterval.ONE_MINUTE,
        start=datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        end=datetime(2026, 3, 30, 7, 2, tzinfo=UTC),
    )

    assert series.missing_interval_count == 1
    assert series.bars[1].volume_value is None
    assert set(series.warnings) == {"observed_interval_gap", "volume_missing"}


def test_chart_time_off_the_requested_interval_boundary_is_refused() -> None:
    handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    with pytest.raises(MarketDataValidationError, match="interval boundary"):
        normalize_price_series(
            rows=(
                {
                    "CloseBid": 101.0,
                    "Time": "2026-03-30T09:01:00+02:00",
                },
            ),
            instrument_handle=handle,
            interval=ChartInterval.FIVE_MINUTES,
            start=datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
            end=datetime(2026, 3, 30, 7, 5, tzinfo=UTC),
        )


def test_duplicate_bar_inside_one_source_capture_is_refused() -> None:
    handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    duplicate = {
        "CloseBid": 101.0,
        "Time": "2026-03-30T09:00:00+02:00",
    }

    with pytest.raises(MarketDataValidationError, match="duplicate bar timestamp"):
        normalize_price_series(
            rows=(duplicate, duplicate),
            instrument_handle=handle,
            interval=ChartInterval.ONE_MINUTE,
            start=datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
            end=datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        )


def test_quote_preserves_delayed_and_stale_quality() -> None:
    handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    quote = normalize_quote(
        row={
            "AssetType": "Stock",
            "PriceTypeAsk": "Delayed",
            "PriceTypeBid": "Delayed",
            "Quote": {
                "Ask": 102.0,
                "Bid": 100.0,
                "DelayedByMinutes": 15,
                "Mid": 101.0,
                "PriceType": "Delayed",
            },
            "Uic": 1001,
        },
        instrument_handle=handle,
        captured_at=datetime(2026, 3, 30, 12, tzinfo=UTC),
        evaluated_at=datetime(2026, 3, 30, 12, 6, tzinfo=UTC),
        max_age=timedelta(minutes=5),
    )

    assert quote.freshness == "stale"
    assert quote.delayed_by_minutes == 15
    assert quote.bid_value == 100.0
    assert quote.ask_value == 102.0
    assert quote.mid_value == 101.0
    assert set(quote.warnings) == {"quote_delayed", "quote_stale"}


def test_option_chain_keeps_only_complete_references_and_marks_missing_currency() -> None:
    chain = normalize_option_chain(
        row={
            "ExpiryDates": ["2026-09-18"],
            "OptionRootId": 17,
            "SpecificOptions": [
                {"PutCall": "Call", "Strike": 100.0, "Uic": 1001},
                {"Uic": 1002},
            ],
        },
        expected_root_id=17,
        expiry=date(2026, 9, 18),
    )

    assert len(chain.options) == 1
    assert chain.options[0].source_identifier == 1001
    assert chain.options[0].put_call == "call"
    assert chain.options[0].strike_value == 100.0
    assert set(chain.warnings) == {
        "option_currency_missing",
        "option_reference_incomplete",
    }
