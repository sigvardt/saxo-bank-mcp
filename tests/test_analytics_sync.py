# ruff: noqa: PLR2004

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import httpx2
import pytest

from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_resolver import InstrumentResolver
from saxo_bank_mcp.analytics_sync import (
    PriceBarSyncSpec,
    QuoteSyncSpec,
    SyncLimitError,
    SyncResearchRequest,
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
    dataset = get_dataset(second.datasets[0].dataset_id, 1, 500, config=config)
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
    page = get_dataset(summary.dataset_id, 1, 500, config=config)
    assert page.total_rows == 1
    row = page.rows[0]
    assert row.row_kind == "quote"
    assert row.bid_value is None
    assert row.ask_value is None
    assert row.mid_value is None


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
