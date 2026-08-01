# ruff: noqa: PLR2004

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import duckdb
import httpx2
import pytest

import saxo_bank_mcp.analytics_resolver as resolver_module
import saxo_bank_mcp.analytics_source_contracts as source_contracts_module
import saxo_bank_mcp.analytics_sync as analytics_sync_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import HandleKind
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_resolver import InstrumentResolver
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreQuotaError
from saxo_bank_mcp.analytics_sync import (
    OptionChainSyncSpec,
    PriceBarDatasetSummary,
    PriceBarSyncSpec,
    QuoteDatasetRow,
    QuoteSyncSpec,
    SyncLimitError,
    SyncResearchRequest,
    SyncValidationError,
    capture_option_chain,
    capture_quote,
    get_dataset,
    sync_price_bars,
    sync_research_data,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CAPTURED_AT = datetime(2026, 3, 30, 12, tzinfo=UTC)


class _PayloadExecutor:
    def __init__(self, payloads: Sequence[Mapping[str, object]]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        self.calls.append((operation.operation_id, request_target, dict(params)))
        payload = self._payloads.pop(0)
        return httpx2.Response(
            200,
            content=json.dumps(payload).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


class _EntitlementExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        self.calls.append((operation.operation_id, request_target, dict(params)))
        return httpx2.Response(
            403,
            content=json.dumps(
                {
                    "ErrorCode": "NoAccess",
                    "Message": "private broker explanation",
                },
            ).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


class _ResponseExecutor:
    def __init__(
        self,
        responses: Sequence[tuple[int, Mapping[str, object]]],
    ) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        self.calls.append((operation.operation_id, request_target, dict(params)))
        status, payload = self._responses.pop(0)
        return httpx2.Response(
            status,
            content=json.dumps(payload).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


async def _no_sleep(_delay: float) -> None:
    return None


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


async def _resolved_handle(config: AnalyticsConfig) -> str:
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "Identifier": 1001,
                        "AssetType": "Stock",
                        "Description": "Fixture instrument",
                        "Symbol": "FIX",
                        "ExchangeId": "XNAS",
                    },
                ],
            },
        ),
    )
    resolver = InstrumentResolver(
        SaxoAnalyticsProvider(request_executor=executor),
        config,
    )
    result = await resolver.resolve_instruments("FIX", (), ())
    return result.matches[0].instrument_handle


@pytest.mark.anyio
async def test_chart_sync_persists_raw_and_normalized_data_but_returns_a_handle(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "PriceType": "RealTime",
                        "Time": "2026-03-30T09:00:00+02:00",
                        "Volume": 12,
                    },
                    {
                        "CloseBid": 102.0,
                        "PriceType": "RealTime",
                        "Time": "2026-03-30T09:01:00+02:00",
                        "Volume": 13,
                    },
                ],
                "DataVersion": 1,
            },
        ),
    )

    result = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 1, tzinfo=UTC),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "complete"
    assert result.source_request_count == 1
    assert len(result.datasets) == 1
    summary = result.datasets[0]
    assert summary.data_kind == "price_bars"
    assert summary.instrument_handle == handle
    assert summary.row_count == 2
    assert summary.return_series_label == "price_return"
    assert summary.adjustment_status == "unadjusted"
    assert not hasattr(result, "rows")
    assert all(_SHA256.fullmatch(value) for value in summary.fingerprints.model_dump().values())

    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 2
    assert [row.close_value for row in page.rows if row.row_kind == "price_bar"] == [101.0, 102.0]
    with pytest.raises(SyncLimitError, match="response row limit"):
        get_dataset(summary.dataset_id, 1, 501, config=config)

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM price_bars),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
        payload_row = connection.execute(
            "SELECT payload_json FROM source_pages ORDER BY ingested_at DESC LIMIT 1",
        ).fetchone()
    finally:
        connection.close()
    assert counts == (1, 2, 1)
    assert payload_row is not None
    sync_metadata = json.loads(str(payload_row[0]))["sync_metadata"]
    assert sync_metadata["fingerprints"] == summary.fingerprints.model_dump()


@pytest.mark.anyio
async def test_chart_sync_refreshes_a_trailing_window_and_keeps_corrections(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": value,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                    }
                    for minute, value in enumerate((100.0, 101.0, 102.0))
                ],
                "DataVersion": 1,
            },
            {
                "Data": [
                    {
                        "CloseBid": value,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                        "Volume": 1,
                    }
                    for minute, value in ((1, 111.0), (2, 102.0), (3, 103.0))
                ],
                "DataVersion": 2,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)
    capture_times = iter(
        (
            datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
            datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        ),
    )

    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 2, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: next(capture_times),
    )
    second = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 3, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: next(capture_times),
    )

    second_params = executor.calls[1][2]
    assert second_params["Time"] == "2026-03-30T07:01:00+00:00"
    assert second_params["Count"] == "3"
    assert second.status == "degraded"
    summary = second.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    assert summary.row_count == 4
    assert summary.missing_interval_count == 0
    assert summary.warnings == ("volume_missing",)
    dataset = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert [row.close_value for row in dataset.rows if row.row_kind == "price_bar"] == [
        100.0,
        111.0,
        102.0,
        103.0,
    ]

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM price_bars),
                (
                    SELECT count(*)
                    FROM dataset_source_pages
                    WHERE dataset_id = ?
                )
            """,
            (second.datasets[0].dataset_id,),
        ).fetchone()
        metadata_row = connection.execute(
            """
            SELECT p.payload_json
            FROM dataset_source_pages AS dsp
            JOIN source_pages AS p ON p.page_id = dsp.page_id
            WHERE dsp.dataset_id = ?
            ORDER BY p.source_timestamp DESC
            LIMIT 1
            """,
            (second.datasets[0].dataset_id,),
        ).fetchone()
    finally:
        connection.close()
    assert counts == (2, 6, 1)
    assert metadata_row is not None
    correction_state = json.loads(str(metadata_row[0]))["sync_metadata"]["correction_state"]
    assert correction_state == {
        "prior_coverage": {
            "end": "2026-03-30T07:02:00+00:00",
            "start": "2026-03-30T07:00:00+00:00",
        },
        "refresh_start": "2026-03-30T07:01:00+00:00",
        "requested_end": "2026-03-30T07:03:00+00:00",
        "requested_start": "2026-03-30T07:00:00+00:00",
        "source_native_revisions": ["data_version:2"],
    }


@pytest.mark.anyio
async def test_chart_refresh_recomputes_gaps_over_the_full_visible_dataset(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 100.0 + minute,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                    }
                    for minute in range(3)
                ],
                "DataVersion": 1,
            },
            {
                "Data": [
                    {
                        "CloseBid": 104.0,
                        "Time": "2026-03-30T09:04:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 2,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)

    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 2, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    result = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 4, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    summary = result.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    assert result.status == "degraded"
    assert summary.row_count == 2
    assert summary.missing_interval_count == 3
    assert set(summary.warnings) == {"observed_interval_gap", "volume_missing"}
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert [row.close_value for row in page.rows if row.row_kind == "price_bar"] == [
        100.0,
        104.0,
    ]


@pytest.mark.anyio
async def test_chart_request_backfills_missing_leading_history_and_reports_actual_coverage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": float(hour),
                        "Time": f"2026-03-30T{hour + 2:02d}:00:00+02:00",
                        "Volume": 1,
                    }
                    for hour in range(10, 13)
                ],
                "DataVersion": 1,
            },
            {
                "Data": [
                    {
                        "CloseBid": float(hour),
                        "Time": f"2026-03-30T{hour + 2:02d}:00:00+02:00",
                        "Volume": 1,
                    }
                    for hour in range(2, 14)
                ],
                "DataVersion": 2,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)

    await sync_price_bars(
        handle,
        ChartInterval.ONE_HOUR,
        datetime(2026, 3, 30, 10, tzinfo=UTC),
        datetime(2026, 3, 30, 12, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    result = await sync_price_bars(
        handle,
        ChartInterval.ONE_HOUR,
        datetime(2026, 3, 30, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 13, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert executor.calls[1][2]["Time"] == "2026-03-30T00:00:00+00:00"
    assert executor.calls[1][2]["Count"] == "14"
    summary = result.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    assert summary.coverage_start == datetime(2026, 3, 30, 2, tzinfo=UTC)
    assert summary.coverage_end == datetime(2026, 3, 30, 13, tzinfo=UTC)
    assert summary.row_count == 12
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 12
    bars = [row for row in page.rows if row.row_kind == "price_bar"]
    assert bars[0].bar_time == datetime(2026, 3, 30, 2, tzinfo=UTC)
    assert bars[-1].bar_time == datetime(2026, 3, 30, 13, tzinfo=UTC)

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        stored_coverage = connection.execute(
            """
            SELECT epoch_us(coverage_start), epoch_us(coverage_end)
            FROM datasets
            WHERE dataset_id = ?
            """,
            (summary.dataset_id,),
        ).fetchone()
    finally:
        connection.close()
    assert stored_coverage == (
        1_774_836_000_000_000,
        1_774_875_600_000_000,
    )


@pytest.mark.parametrize(
    ("source_hours", "expected_coverage", "expected_warning"),
    [
        pytest.param(
            tuple(range(2, 14)),
            (
                datetime(2026, 3, 30, 2, tzinfo=UTC),
                datetime(2026, 3, 30, 13, tzinfo=UTC),
            ),
            "leading_coverage_missing",
            id="leading",
        ),
        pytest.param(
            tuple(range(12)),
            (
                datetime(2026, 3, 30, 0, tzinfo=UTC),
                datetime(2026, 3, 30, 11, tzinfo=UTC),
            ),
            "trailing_coverage_missing",
            id="trailing",
        ),
    ],
)
@pytest.mark.anyio
async def test_chart_edge_coverage_is_partial_without_internal_gaps(
    tmp_path: Path,
    source_hours: tuple[int, ...],
    expected_coverage: tuple[datetime, datetime],
    expected_warning: str,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": float(hour),
                        "Time": f"2026-03-30T{hour + 2:02d}:00:00+02:00",
                        "Volume": 1,
                    }
                    for hour in source_hours
                ],
                "DataVersion": 1,
            },
        ),
    )

    result = await sync_price_bars(
        handle,
        ChartInterval.ONE_HOUR,
        datetime(2026, 3, 30, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 13, tzinfo=UTC),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "degraded"
    summary = result.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    assert summary.quality_state == "partial"
    assert (summary.coverage_start, summary.coverage_end) == expected_coverage
    assert summary.missing_interval_count == 0
    assert summary.warnings == (expected_warning,)


@pytest.mark.anyio
async def test_complete_chart_refresh_replaces_its_window_and_exposes_removed_bar_gap(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 100.0 + minute,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                        "Volume": 1,
                    }
                    for minute in range(3)
                ],
                "DataVersion": 1,
            },
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-03-30T09:01:00+02:00",
                        "Volume": 1,
                    },
                    {
                        "CloseBid": 103.0,
                        "Time": "2026-03-30T09:03:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 2,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)

    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 2, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    result = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 7, 3, tzinfo=UTC),
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    summary = result.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    assert summary.row_count == 3
    assert summary.missing_interval_count == 1
    assert "observed_interval_gap" in summary.warnings
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert [row.close_value for row in page.rows if row.row_kind == "price_bar"] == [
        100.0,
        101.0,
        103.0,
    ]


@pytest.mark.anyio
async def test_daily_refresh_rebuilds_retained_rows_with_original_exchange_midnights(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": close,
                        "Time": source_time,
                        "Volume": 1,
                    }
                    for close, source_time in (
                        (100.0, "2026-03-27T00:00:00+01:00"),
                        (101.0, "2026-03-28T00:00:00+01:00"),
                        (102.0, "2026-03-29T00:00:00+01:00"),
                        (103.0, "2026-03-30T00:00:00+02:00"),
                    )
                ],
                "DataVersion": 1,
            },
            {
                "Data": [
                    {
                        "CloseBid": 102.0,
                        "Time": "2026-03-29T00:00:00+01:00",
                        "Volume": 1,
                    },
                    {
                        "CloseBid": 103.0,
                        "Time": "2026-03-30T00:00:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 2,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)
    start = datetime(2026, 3, 26, 23, tzinfo=UTC)
    end = datetime(2026, 3, 29, 22, tzinfo=UTC)

    await sync_price_bars(
        handle,
        ChartInterval.ONE_DAY,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    result = await sync_price_bars(
        handle,
        ChartInterval.ONE_DAY,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert executor.calls[1][2]["Time"] == "2026-03-28T23:00:00+00:00"
    assert executor.calls[1][2]["Count"] == "2"
    summary = result.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    assert summary.row_count == 4
    assert summary.missing_interval_count == 0
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert [row.bar_time for row in page.rows if row.row_kind == "price_bar"] == [
        datetime(2026, 3, 26, 23, tzinfo=UTC),
        datetime(2026, 3, 27, 23, tzinfo=UTC),
        datetime(2026, 3, 28, 23, tzinfo=UTC),
        datetime(2026, 3, 29, 22, tzinfo=UTC),
    ]


@pytest.mark.anyio
async def test_later_chart_refresh_anchors_to_latest_visible_dataset_after_removals(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 100.0 + minute,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                        "Volume": 1,
                    }
                    for minute in range(5)
                ],
                "DataVersion": 1,
            },
            {"Data": [], "DataVersion": 2},
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-03-30T09:01:00+02:00",
                        "Volume": 1,
                    },
                    {
                        "CloseBid": 102.0,
                        "Time": "2026-03-30T09:02:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 3,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)
    start = datetime(2026, 3, 30, 7, 0, tzinfo=UTC)
    end = datetime(2026, 3, 30, 7, 4, tzinfo=UTC)

    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    removed = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    removed_summary = removed.datasets[0]
    assert isinstance(removed_summary, PriceBarDatasetSummary)
    assert removed_summary.coverage_end == datetime(2026, 3, 30, 7, 2, tzinfo=UTC)

    latest = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert executor.calls[2][2]["Time"] == "2026-03-30T07:01:00+00:00"
    assert executor.calls[2][2]["Count"] == "4"
    latest_summary = latest.datasets[0]
    assert isinstance(latest_summary, PriceBarDatasetSummary)
    assert latest_summary.row_count == 3
    assert latest_summary.coverage_end == datetime(2026, 3, 30, 7, 2, tzinfo=UTC)
    page = get_dataset(latest_summary.dataset_id, 1, 500, config=config)
    assert [row.close_value for row in page.rows if row.row_kind == "price_bar"] == [
        100.0,
        101.0,
        102.0,
    ]

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        historical_rows = connection.execute("SELECT count(*) FROM price_bars").fetchone()
    finally:
        connection.close()
    assert historical_rows == (7,)


@pytest.mark.anyio
async def test_chart_refresh_lineage_never_resurrects_a_removed_nontrailing_bar(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 100.0 + minute,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                        "Volume": 1,
                    }
                    for minute in (0, 5, 9)
                ],
                "DataVersion": 1,
            },
            {
                "Data": [
                    {
                        "CloseBid": 200.0 + minute,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                        "Volume": 2,
                    }
                    for minute in (7, 8, 9)
                ],
                "DataVersion": 2,
            },
            {
                "Data": [
                    {
                        "CloseBid": 300.0 + minute,
                        "Time": f"2026-03-30T09:0{minute}:00+02:00",
                        "Volume": 3,
                    }
                    for minute in (8, 9)
                ],
                "DataVersion": 3,
            },
        ),
    )
    provider = SaxoAnalyticsProvider(request_executor=executor)
    start = datetime(2026, 3, 30, 7, 0, tzinfo=UTC)
    end = datetime(2026, 3, 30, 7, 9, tzinfo=UTC)

    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    latest = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        start,
        end,
        provider=provider,
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert executor.calls[1][2]["Time"] == "2026-03-30T07:05:00+00:00"
    assert executor.calls[2][2]["Time"] == "2026-03-30T07:08:00+00:00"
    summary = latest.datasets[0]
    assert isinstance(summary, PriceBarDatasetSummary)
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    bars = [row for row in page.rows if row.row_kind == "price_bar"]
    assert [bar.bar_time.minute for bar in bars] == [0, 7, 8, 9]
    assert [bar.close_value for bar in bars] == [100.0, 207.0, 308.0, 309.0]

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        historical_rows = connection.execute("SELECT count(*) FROM price_bars").fetchone()
        removed_rows = connection.execute(
            "SELECT count(*) FROM price_bars WHERE minute(bar_time) = 5",
        ).fetchone()
    finally:
        connection.close()
    assert historical_rows == (8,)
    assert removed_rows == (1,)


@pytest.mark.anyio
async def test_every_chart_fingerprint_binds_retained_and_refreshed_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_handle = "ih_00000000000040008000000000000001"

    def fixed_instrument_handle(_kind: HandleKind) -> str:
        return fixed_handle

    async def scenario(root: Path, leading_close: float) -> dict[str, str]:
        uuids = iter(
            (
                UUID("00000000-0000-4000-8000-000000000011"),
                UUID("00000000-0000-4000-8000-000000000012"),
                UUID("00000000-0000-4000-8000-000000000013"),
            ),
        )
        monkeypatch.setattr(source_contracts_module, "uuid4", lambda: next(uuids))
        monkeypatch.setattr(
            resolver_module,
            "new_safe_handle",
            fixed_instrument_handle,
        )
        config = _config(root)
        handle = await _resolved_handle(config)
        executor = _PayloadExecutor(
            (
                {
                    "Data": [
                        {
                            "CloseBid": leading_close,
                            "Time": "2026-03-30T09:00:00+02:00",
                            "Volume": 1,
                        },
                        {
                            "CloseBid": 101.0,
                            "Time": "2026-03-30T09:01:00+02:00",
                            "Volume": 1,
                        },
                    ],
                    "DataVersion": 1,
                },
                {
                    "Data": [
                        {
                            "CloseBid": 101.0,
                            "Time": "2026-03-30T09:01:00+02:00",
                            "Volume": 1,
                        },
                        {
                            "CloseBid": 102.0,
                            "Time": "2026-03-30T09:02:00+02:00",
                            "Volume": 1,
                        },
                    ],
                    "DataVersion": 2,
                },
            ),
        )
        provider = SaxoAnalyticsProvider(request_executor=executor)
        await sync_price_bars(
            handle,
            ChartInterval.ONE_MINUTE,
            datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
            datetime(2026, 3, 30, 7, 1, tzinfo=UTC),
            provider=provider,
            config=config,
            clock=lambda: _CAPTURED_AT,
        )
        result = await sync_price_bars(
            handle,
            ChartInterval.ONE_MINUTE,
            datetime(2026, 3, 30, 7, 0, tzinfo=UTC),
            datetime(2026, 3, 30, 7, 2, tzinfo=UTC),
            provider=provider,
            config=config,
            clock=lambda: _CAPTURED_AT,
        )
        return result.datasets[0].fingerprints.model_dump()

    baseline = await scenario(tmp_path / "baseline", 100.0)
    changed = await scenario(tmp_path / "changed", 999.0)

    assert baseline.keys() == changed.keys()
    unchanged = {name for name in baseline if baseline[name] == changed[name]}
    assert not unchanged, unchanged


@pytest.mark.anyio
async def test_quote_capture_persists_delay_and_stale_quality(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
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
        ),
    )
    times = iter(
        (
            datetime(2026, 3, 30, 12, tzinfo=UTC),
            datetime(2026, 3, 30, 12, 6, tzinfo=UTC),
        ),
    )

    result = await capture_quote(
        handle,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: next(times),
        max_age=timedelta(minutes=5),
    )

    assert result.status == "degraded"
    summary = result.datasets[0]
    assert summary.data_kind == "quote"
    assert summary.quality_state == "stale"
    assert summary.freshness == "stale"
    assert set(summary.warnings) == {"quote_delayed", "quote_stale"}
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 1
    assert page.rows[0].row_kind == "quote"
    assert page.rows[0].bid_value == 100.0
    assert page.rows[0].ask_value == 102.0

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM quotes),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
        payload_row = connection.execute(
            "SELECT payload_json FROM source_pages LIMIT 1",
        ).fetchone()
    finally:
        connection.close()
    assert counts == (1, 1, 1)
    assert payload_row is not None
    payload = json.loads(str(payload_row[0]))
    assert payload["source_quality"]["state"] == "limited"
    assert payload["sync_metadata"]["entitlement_state"] == "delayed"


@pytest.mark.anyio
async def test_quote_capture_keeps_a_missing_value_row_without_inventing_prices(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "AssetType": "Stock",
                "PriceTypeAsk": "NoAccess",
                "PriceTypeBid": "NoAccess",
                "Quote": None,
                "Uic": 1001,
            },
        ),
    )

    result = await capture_quote(
        handle,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    summary = result.datasets[0]
    assert summary.data_kind == "quote"
    assert summary.quality_state == "missing"
    assert summary.row_count == 1
    assert summary.warnings == ("quote_entitlement_limited", "quote_values_missing")
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 1
    row = page.rows[0]
    assert row.row_kind == "quote"
    assert row.bid_value is None
    assert row.ask_value is None
    assert row.mid_value is None

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        payload_row = connection.execute(
            "SELECT payload_json FROM source_pages LIMIT 1",
        ).fetchone()
    finally:
        connection.close()
    assert payload_row is not None
    payload = json.loads(str(payload_row[0]))
    assert payload["sync_metadata"]["entitlement_state"] == "limited"
    assert payload["sync_metadata"]["warnings"] == [
        "quote_entitlement_limited",
        "quote_values_missing",
    ]


@pytest.mark.anyio
async def test_partial_noaccess_quote_preserves_a_safe_entitlement_warning(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "AssetType": "Stock",
                "PriceTypeAsk": "NoAccess",
                "PriceTypeBid": "RealTime",
                "Quote": {
                    "Ask": None,
                    "Bid": 100.0,
                    "DelayedByMinutes": 0,
                    "Mid": 100.0,
                    "PriceType": "RealTime",
                },
                "Uic": 1001,
            },
        ),
    )

    result = await capture_quote(
        handle,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    summary = result.datasets[0]
    assert summary.quality_state == "partial"
    assert summary.warnings == ("quote_entitlement_limited",)
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    row = page.rows[0]
    assert isinstance(row, QuoteDatasetRow)
    assert row.bid_value == 100.0
    assert row.ask_value is None


@pytest.mark.anyio
async def test_market_quota_refusal_happens_before_chart_source_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-03-30T09:00:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 1,
            },
        ),
    )

    def refuse_capacity(
        _store_type: type[AnalyticsStore],
        _config_value: AnalyticsConfig,
        _incoming_bytes: int,
    ) -> None:
        raise StoreQuotaError("fixture quota refusal")

    monkeypatch.setattr(
        AnalyticsStore,
        "ensure_owner_capacity",
        classmethod(refuse_capacity),
    )
    with pytest.raises(SyncLimitError, match="store quota"):
        await sync_price_bars(
            handle,
            ChartInterval.ONE_MINUTE,
            datetime(2026, 3, 30, 7, tzinfo=UTC),
            datetime(2026, 3, 30, 7, tzinfo=UTC),
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    assert executor.calls == []
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM price_bars),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 0, 0)


@pytest.mark.anyio
async def test_quote_rejects_zero_maximum_age_before_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(())

    with pytest.raises(SyncValidationError, match="maximum age"):
        await capture_quote(
            handle,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
            max_age=timedelta(0),
        )

    assert executor.calls == []


@pytest.mark.anyio
async def test_quote_source_request_count_includes_retry_attempts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _ResponseExecutor(
        (
            (503, {"ErrorCode": "ServiceUnavailable"}),
            (
                200,
                {
                    "AssetType": "Stock",
                    "PriceTypeAsk": "RealTime",
                    "PriceTypeBid": "RealTime",
                    "Quote": {
                        "Ask": 102.0,
                        "Bid": 100.0,
                        "DelayedByMinutes": 0,
                        "Mid": 101.0,
                        "PriceType": "RealTime",
                    },
                    "Uic": 1001,
                },
            ),
        ),
    )

    result = await capture_quote(
        handle,
        provider=SaxoAnalyticsProvider(
            request_executor=executor,
            retry_attempts=2,
            sleep=_no_sleep,
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.source_request_count == 2
    assert len(executor.calls) == 2


@pytest.mark.anyio
async def test_option_chain_entitlement_refusal_is_sanitized_and_persisted(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _EntitlementExecutor()

    result = await capture_option_chain(
        handle,
        (date(2026, 9, 18),),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "refused"
    assert result.source_request_count == 1
    summary = result.datasets[0]
    assert summary.data_kind == "option_chain"
    assert summary.entitlement_state == "denied"
    assert summary.entitlement_error_code == "NoAccess"
    assert summary.row_count == 0
    assert summary.warnings == ("option_entitlement_denied",)
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 0
    assert page.rows == ()

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM option_snapshots),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
        payload_row = connection.execute(
            "SELECT payload_json FROM source_pages LIMIT 1",
        ).fetchone()
    finally:
        connection.close()
    assert counts == (1, 0, 1)
    assert payload_row is not None
    payload_text = str(payload_row[0])
    assert "private broker explanation" not in payload_text
    payload = json.loads(payload_text)
    assert payload["entitlement"]["error_code"] == "NoAccess"
    assert payload["sync_metadata"]["correction_state"]["capture_kind"] == "point"


@pytest.mark.anyio
async def test_entitled_option_chain_persists_safe_normalized_references(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "ExpiryDates": ["2026-09-18"],
                "OptionRootId": 1001,
                "SpecificOptions": [
                    {"PutCall": "Call", "Strike": 100.0, "Uic": 2001},
                ],
            },
        ),
    )

    result = await capture_option_chain(
        handle,
        (date(2026, 9, 18),),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "degraded"
    summary = result.datasets[0]
    assert summary.data_kind == "option_chain"
    assert summary.entitlement_state == "available"
    assert summary.row_count == 1
    assert summary.warnings == ("option_currency_missing",)
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 1
    option = page.rows[0]
    assert option.row_kind == "option_reference"
    assert option.instrument_handle.startswith("ih_")
    assert option.instrument_handle != handle
    assert option.underlying_handle == handle
    assert option.expiry == date(2026, 9, 18)
    assert option.strike_value == 100.0
    assert option.currency is None

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM option_snapshots),
                (SELECT count(*) FROM safe_instruments),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (1, 1, 2, 1)


@pytest.mark.anyio
async def test_multiple_option_expiries_retain_every_entitled_matching_option(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    expiries = (date(2026, 9, 18), date(2026, 12, 18))
    executor = _PayloadExecutor(
        tuple(
            {
                "ExpiryDates": [expiry.isoformat()],
                "OptionRootId": 1001,
                "SpecificOptions": [
                    {
                        "PutCall": "Call",
                        "Strike": strike,
                        "Uic": identifier,
                    },
                ],
            }
            for expiry, strike, identifier in (
                (expiries[0], 100.0, 2001),
                (expiries[1], 110.0, 2002),
            )
        ),
    )

    result = await capture_option_chain(
        handle,
        expiries,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.source_request_count == 2
    assert len(result.datasets) == 2
    assert [call[2]["ExpiryDates"] for call in executor.calls] == [
        "2026-09-18",
        "2026-12-18",
    ]
    pages = [get_dataset(summary.dataset_id, 1, 500, config=config) for summary in result.datasets]
    options = [row for page in pages for row in page.rows]
    assert len(options) == 2
    assert {option.expiry for option in options if option.row_kind == "option_reference"} == set(
        expiries,
    )


@pytest.mark.anyio
async def test_market_capture_rolls_back_raw_dataset_and_rows_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-03-30T09:00:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 1,
            },
        ),
    )

    def injected_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected normalized failure")

    monkeypatch.setattr(
        analytics_sync_module,
        "_persist_normalized_bars",
        injected_failure,
    )
    with pytest.raises(RuntimeError, match="injected normalized failure"):
        await sync_price_bars(
            handle,
            ChartInterval.ONE_MINUTE,
            datetime(2026, 3, 30, 7, tzinfo=UTC),
            datetime(2026, 3, 30, 7, tzinfo=UTC),
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM price_bars),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 0, 0)


@pytest.mark.anyio
async def test_market_capture_quota_reserves_raw_and_normalized_bytes_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    observed: list[int] = []

    def observe_capacity(
        _store_type: type[AnalyticsStore],
        _config_value: AnalyticsConfig,
        incoming_bytes: int,
    ) -> None:
        observed.append(incoming_bytes)

    monkeypatch.setattr(
        AnalyticsStore,
        "ensure_owner_capacity",
        classmethod(observe_capacity),
    )
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-03-30T09:00:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 1,
            },
        ),
    )

    await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        datetime(2026, 3, 30, 7, tzinfo=UTC),
        datetime(2026, 3, 30, 7, tzinfo=UTC),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        raw_byte_count = connection.execute(
            "SELECT byte_count FROM source_pages",
        ).fetchone()
    finally:
        connection.close()
    assert raw_byte_count is not None
    assert any(value >= int(raw_byte_count[0]) + 128 for value in observed)


@pytest.mark.anyio
async def test_sync_request_limits_are_refused_before_market_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(())
    provider = SaxoAnalyticsProvider(request_executor=executor)
    too_many_items = SyncResearchRequest(
        items=tuple(QuoteSyncSpec(handle=handle) for _ in range(26)),
    )

    with pytest.raises(SyncLimitError, match="instrument limit"):
        await sync_research_data(
            too_many_items,
            provider=provider,
            config=config,
            clock=lambda: _CAPTURED_AT,
        )
    with pytest.raises(SyncLimitError, match="row limit"):
        await sync_price_bars(
            handle,
            ChartInterval.ONE_MINUTE,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=50_000),
            provider=provider,
            config=config,
            clock=lambda: _CAPTURED_AT,
        )
    assert executor.calls == []


@pytest.mark.anyio
async def test_batch_validates_every_quote_age_before_any_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(())
    request = SyncResearchRequest(
        items=(
            PriceBarSyncSpec(
                handle=handle,
                interval=ChartInterval.ONE_MINUTE,
                start=datetime(2026, 3, 30, 7, tzinfo=UTC),
                end=datetime(2026, 3, 30, 7, tzinfo=UTC),
            ),
            QuoteSyncSpec(handle=handle, max_age=timedelta(0)),
        ),
    )

    with pytest.raises(SyncValidationError, match="maximum age"):
        await sync_research_data(
            request,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    assert executor.calls == []


@pytest.mark.anyio
async def test_batch_shares_one_fixed_budget_across_requests_and_retries(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    quote_payload = {
        "AssetType": "Stock",
        "PriceTypeAsk": "RealTime",
        "PriceTypeBid": "RealTime",
        "Quote": {
            "Ask": 102.0,
            "Bid": 100.0,
            "DelayedByMinutes": 0,
            "Mid": 101.0,
            "PriceType": "RealTime",
        },
        "Uic": 1001,
    }
    responses: list[tuple[int, Mapping[str, object]]] = []
    for _index in range(13):
        responses.extend(
            (
                (503, {"ErrorCode": "ServiceUnavailable"}),
                (200, quote_payload),
            ),
        )
    executor = _ResponseExecutor(tuple(responses))
    request = SyncResearchRequest(
        items=tuple(QuoteSyncSpec(handle=handle) for _index in range(13)),
    )

    with pytest.raises(SyncLimitError, match="source request budget"):
        await sync_research_data(
            request,
            provider=SaxoAnalyticsProvider(
                request_executor=executor,
                retry_attempts=2,
                sleep=_no_sleep,
            ),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    assert len(executor.calls) == config.limits.sync_instruments


@pytest.mark.anyio
async def test_batch_preflights_expanded_option_requests_before_calls_or_writes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    expiries = tuple(date(2026, 9, 1) + timedelta(days=index) for index in range(25))
    executor = _PayloadExecutor(
        tuple(
            {
                "ExpiryDates": [expiry.isoformat()],
                "OptionRootId": 1001,
                "SpecificOptions": [],
            }
            for expiry in expiries
        ),
    )
    request = SyncResearchRequest(
        items=(
            OptionChainSyncSpec(handle=handle, expiries=expiries),
            QuoteSyncSpec(handle=handle),
        ),
    )

    with pytest.raises(SyncLimitError, match="source request budget"):
        await sync_research_data(
            request,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    assert executor.calls == []
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM option_snapshots),
                (SELECT count(*) FROM quotes),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 0, 0, 0)


@pytest.mark.anyio
async def test_sync_research_data_batches_on_demand_and_returns_only_handles(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    handle = await _resolved_handle(config)
    executor = _PayloadExecutor(
        (
            {
                "Data": [
                    {
                        "CloseBid": 101.0,
                        "Time": "2026-03-30T09:00:00+02:00",
                        "Volume": 1,
                    },
                ],
                "DataVersion": 1,
            },
            {
                "AssetType": "Stock",
                "PriceTypeAsk": "RealTime",
                "PriceTypeBid": "RealTime",
                "Quote": {
                    "Ask": 102.0,
                    "Bid": 100.0,
                    "DelayedByMinutes": 0,
                    "Mid": 101.0,
                    "PriceType": "RealTime",
                },
                "Uic": 1001,
            },
        ),
    )
    times = iter((_CAPTURED_AT, _CAPTURED_AT, _CAPTURED_AT))
    request = SyncResearchRequest(
        items=(
            PriceBarSyncSpec(
                handle=handle,
                interval=ChartInterval.ONE_MINUTE,
                start=datetime(2026, 3, 30, 7, tzinfo=UTC),
                end=datetime(2026, 3, 30, 7, tzinfo=UTC),
            ),
            QuoteSyncSpec(handle=handle),
        ),
    )

    result = await sync_research_data(
        request,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: next(times),
    )

    assert result.status == "complete"
    assert result.source_request_count == 2
    assert [dataset.data_kind for dataset in result.datasets] == [
        "price_bars",
        "quote",
    ]
    assert not hasattr(result, "rows")
    assert len(executor.calls) == 2
