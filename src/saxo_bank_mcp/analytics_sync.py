from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, cast

import duckdb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_market_data import (
    ChartInterval,
    NormalizedOptionChain,
    NormalizedOptionReference,
    NormalizedPriceSeries,
    NormalizedQuote,
    normalize_option_chain,
    normalize_price_series,
    normalize_quote,
)
from saxo_bank_mcp.analytics_migrations import store_writer_lock_path
from saxo_bank_mcp.analytics_models import (
    DatasetId,
    HandleKind,
    InstrumentHandle,
    QualityState,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_provider import (
    SaxoAnalyticsProvider,
    SourceEntitlementError,
)
from saxo_bank_mcp.analytics_source_contracts import (
    SourcePage,
    build_source_capture_context,
    build_source_capture_envelope,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreQuotaError

_CHART_CONTRACT_ID: Final = "chart_v3"
_MISSING_CURRENCY: Final = "__unavailable__"
_CONNECTION_CONFIG: Final = MappingProxyType(
    {
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "enable_external_access": "false",
    },
)
_INSTRUMENT_HANDLE_ADAPTER: Final[TypeAdapter[InstrumentHandle]] = TypeAdapter(
    InstrumentHandle,
)
_DATASET_ID_ADAPTER: Final[TypeAdapter[DatasetId]] = TypeAdapter(DatasetId)

type Clock = Callable[[], datetime]


class SyncError(RuntimeError):
    """Base error for on-demand Saxo research synchronization."""


class SyncValidationError(SyncError):
    """Raised before source access when a sync request is invalid."""


class SyncLimitError(SyncValidationError):
    """Raised before source access when a fixed request limit is exceeded."""


class DatasetNotFoundError(SyncError):
    """Raised when a safe dataset handle does not exist."""


class SyncStatus(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    REFUSED = "refused"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class IngestionFingerprints(_StrictModel):
    """Value-free fingerprints retained for every market ingestion."""

    raw_pages_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalized_rows_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    entitlements_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    correction_state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _DatasetHandleSummary(_StrictModel):
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    quality_state: QualityState
    coverage_start: datetime
    coverage_end: datetime
    row_count: int = Field(ge=0)
    warnings: tuple[str, ...]
    fingerprints: IngestionFingerprints


class PriceBarDatasetSummary(_DatasetHandleSummary):
    """Small handle-only summary for normalized chart bars."""

    data_kind: Literal["price_bars"]
    missing_interval_count: int = Field(ge=0)
    return_series_label: Literal["price_return"]
    adjustment_status: Literal["unadjusted"]


class QuoteDatasetSummary(_DatasetHandleSummary):
    """Small handle-only summary for one quality-bound quote."""

    data_kind: Literal["quote"]
    freshness: Literal["fresh", "stale"]
    delayed_by_minutes: int | None = Field(ge=0)


class OptionChainDatasetSummary(_DatasetHandleSummary):
    """Small handle-only summary for an entitled or refused option chain."""

    data_kind: Literal["option_chain"]
    expiries: tuple[date, ...]
    entitlement_state: Literal["available", "denied"]
    entitlement_error_code: str | None = Field(max_length=128)


type DatasetHandleSummary = PriceBarDatasetSummary | QuoteDatasetSummary | OptionChainDatasetSummary


class SyncResult(_StrictModel):
    """Bounded synchronization result containing handles instead of candles."""

    status: SyncStatus
    source_request_count: int = Field(ge=0)
    datasets: tuple[DatasetHandleSummary, ...]


class PriceBarSyncSpec(_StrictModel):
    """One bounded price-bar item in an on-demand research sync."""

    data_kind: Literal["price_bars"] = "price_bars"
    handle: InstrumentHandle
    interval: ChartInterval
    start: datetime
    end: datetime


class QuoteSyncSpec(_StrictModel):
    """One quote item in an on-demand research sync."""

    data_kind: Literal["quote"] = "quote"
    handle: InstrumentHandle
    max_age: timedelta = timedelta(minutes=5)


class OptionChainSyncSpec(_StrictModel):
    """One bounded option-chain item in an on-demand research sync."""

    data_kind: Literal["option_chain"] = "option_chain"
    handle: InstrumentHandle
    expiries: tuple[date, ...]


type ResearchSyncSpec = PriceBarSyncSpec | QuoteSyncSpec | OptionChainSyncSpec


class SyncResearchRequest(_StrictModel):
    """Explicit on-demand work only; it does not describe a collector."""

    items: tuple[ResearchSyncSpec, ...] = Field(min_length=1)


class PriceBarDatasetRow(_StrictModel):
    """One explicitly requested bounded dataset row."""

    row_kind: Literal["price_bar"] = "price_bar"
    instrument_handle: InstrumentHandle
    bar_time: datetime
    interval: ChartInterval
    open_value: float | None = Field(allow_inf_nan=False)
    high_value: float | None = Field(allow_inf_nan=False)
    low_value: float | None = Field(allow_inf_nan=False)
    close_value: float = Field(allow_inf_nan=False)
    volume_value: float | None = Field(allow_inf_nan=False)
    adjusted: Literal[False]


class QuoteDatasetRow(_StrictModel):
    """One explicitly requested bounded quote row."""

    row_kind: Literal["quote"] = "quote"
    instrument_handle: InstrumentHandle
    captured_at: datetime
    bid_value: float | None = Field(allow_inf_nan=False)
    ask_value: float | None = Field(allow_inf_nan=False)
    mid_value: float | None = Field(allow_inf_nan=False)
    freshness: Literal["fresh", "stale"]
    warnings: tuple[str, ...]


class OptionReferenceDatasetRow(_StrictModel):
    """One bounded option reference with no raw Saxo identifier."""

    row_kind: Literal["option_reference"] = "option_reference"
    instrument_handle: InstrumentHandle
    underlying_handle: InstrumentHandle
    captured_at: datetime
    expiry: date
    strike_value: float = Field(allow_inf_nan=False)
    currency: str | None = Field(max_length=16)
    put_call: Literal["call", "put"]


type DatasetRow = PriceBarDatasetRow | QuoteDatasetRow | OptionReferenceDatasetRow


class DatasetPage(_StrictModel):
    """One deterministic page capped by the fixed direct-response limit."""

    dataset_id: DatasetId
    page: int = Field(ge=1)
    limit: int = Field(ge=1, le=500)
    total_rows: int = Field(ge=0)
    rows: tuple[DatasetRow, ...]
    next_page: int | None = Field(ge=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def sync_price_bars(  # noqa: PLR0913
    handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
) -> SyncResult:
    """Fetch one bounded chart window from Saxo and persist it on demand."""
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("market sync requires SaxoAnalyticsProvider")
    instrument_handle = _validate_instrument_handle(handle)
    _require_utc_range(start, end)
    requested_rows = _row_bound(start, end, interval)
    if requested_rows > config.limits.sync_rows:
        raise SyncLimitError("synchronous market data row limit exceeded")
    selector = _instrument_selector(config, instrument_handle)
    prior = _existing_bar_state(config, instrument_handle, interval, start, end)
    prior_page_ids = _latest_bar_page_ids(
        config,
        instrument_handle,
        interval,
        start,
        end,
    )
    refresh_start = start if prior is None else max(start, prior.coverage_end - interval.delta)
    refresh_rows = _row_bound(refresh_start, end, interval)
    capture_time = _require_utc_clock(clock())
    request: dict[str, object] = {
        "AssetType": selector.asset_type,
        "Count": refresh_rows,
        "Horizon": interval.minutes,
        "Mode": "From",
        "Time": refresh_start.isoformat(),
        "Uic": selector.identifier,
    }
    capture = build_source_capture_context(
        {_CHART_CONTRACT_ID: request},
        captured_at=capture_time,
    )
    pages = tuple(
        [
            page
            async for page in provider.fetch(
                _CHART_CONTRACT_ID,
                request,
                capture=capture,
            )
        ],
    )
    envelope = build_source_capture_envelope(capture, pages)
    series = normalize_price_series(
        rows=tuple(row for page in envelope.pages for row in page.rows),
        instrument_handle=instrument_handle,
        interval=interval,
        start=refresh_start,
        end=end,
    )
    correction_state = {
        "prior_coverage": (
            None
            if prior is None
            else {
                "end": prior.coverage_end.isoformat(),
                "start": prior.coverage_start.isoformat(),
            }
        ),
        "refresh_start": refresh_start.isoformat(),
        "requested_end": end.isoformat(),
        "requested_start": start.isoformat(),
        "source_native_revisions": sorted(
            {page.source_revision for page in envelope.pages},
        ),
    }
    fingerprints = _capture_fingerprints(
        envelope.pages,
        series.fingerprint_sha256,
        correction_state,
    )
    quality_state = (
        QualityState.MISSING
        if not series.bars
        else QualityState.PARTIAL
        if series.warnings
        else QualityState.COMPLETE
    )
    stored_pages, dataset_id = _persist_chart_capture(
        config=config,
        pages=envelope.pages,
        instrument_handle=instrument_handle,
        interval=interval,
        start=start,
        end=end,
        quality_state=quality_state,
        fingerprints=fingerprints,
        correction_state=correction_state,
        prior_page_ids=prior_page_ids,
    )
    _persist_normalized_bars(
        config=config,
        pages=envelope.pages,
        stored_page_ids=stored_pages,
        series=series,
    )
    summary = PriceBarDatasetSummary(
        dataset_id=dataset_id,
        data_kind="price_bars",
        instrument_handle=instrument_handle,
        quality_state=quality_state,
        coverage_start=start,
        coverage_end=end,
        row_count=len(series.bars),
        missing_interval_count=series.missing_interval_count,
        return_series_label=series.return_series_label,
        adjustment_status=series.adjustment_status,
        warnings=series.warnings,
        fingerprints=fingerprints,
    )
    return SyncResult(
        status=(
            SyncStatus.COMPLETE if quality_state is QualityState.COMPLETE else SyncStatus.DEGRADED
        ),
        source_request_count=1,
        datasets=(summary,),
    )


async def capture_quote(
    handle: str,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
    max_age: timedelta = timedelta(minutes=5),
) -> SyncResult:
    """Capture one quality-bound Saxo quote and return only its dataset handle."""
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("market sync requires SaxoAnalyticsProvider")
    instrument_handle = _validate_instrument_handle(handle)
    selector = _instrument_selector(config, instrument_handle)
    captured_at = _require_utc_clock(clock())
    request: dict[str, object] = {
        "AssetType": selector.asset_type,
        "Uic": selector.identifier,
    }
    capture = build_source_capture_context(
        {"info_price_v1": request},
        captured_at=captured_at,
    )
    pages = tuple(
        [
            page
            async for page in provider.fetch(
                "info_price_v1",
                request,
                capture=capture,
            )
        ],
    )
    envelope = build_source_capture_envelope(capture, pages)
    rows = tuple(row for page in envelope.pages for row in page.rows)
    if len(rows) != 1:
        raise SyncError("quote capture did not return exactly one source row")
    quote = normalize_quote(
        row=rows[0],
        instrument_handle=instrument_handle,
        captured_at=captured_at,
        evaluated_at=_require_utc_clock(clock()),
        max_age=max_age,
    )
    correction_state = {
        "capture_kind": "point",
        "captured_at": captured_at.isoformat(),
        "source_native_revisions": sorted(
            {page.source_revision for page in envelope.pages},
        ),
    }
    fingerprints = _capture_fingerprints(
        envelope.pages,
        quote.fingerprint_sha256,
        correction_state,
    )
    source_limited = any(page.source_quality.state == "limited" for page in envelope.pages)
    has_price = any(
        value is not None for value in (quote.bid_value, quote.ask_value, quote.mid_value)
    )
    quality_state = (
        QualityState.MISSING
        if not has_price
        else QualityState.STALE
        if quote.freshness == "stale"
        else QualityState.PARTIAL
        if quote.warnings or source_limited
        else QualityState.COMPLETE
    )
    entitlement_state = (
        "delayed"
        if "quote_delayed" in quote.warnings
        else "limited"
        if source_limited
        else "available"
    )
    sync_metadata = {
        "capture_revision": capture.capture_revision,
        "captured_at": captured_at.isoformat(),
        "correction_state": correction_state,
        "data_kind": "quote",
        "delayed_by_minutes": quote.delayed_by_minutes,
        "entitlement_state": entitlement_state,
        "fingerprints": fingerprints.model_dump(mode="json"),
        "freshness": quote.freshness,
        "instrument_handle": instrument_handle,
        "warnings": list(quote.warnings),
    }
    stored_pages, dataset_id = _persist_market_capture(
        config=config,
        pages=envelope.pages,
        instrument_handle=instrument_handle,
        coverage_start=captured_at,
        coverage_end=captured_at,
        quality_state=quality_state,
        sync_metadata=sync_metadata,
    )
    _persist_normalized_quote(
        config=config,
        pages=envelope.pages,
        stored_page_ids=stored_pages,
        quote=quote,
    )
    summary = QuoteDatasetSummary(
        dataset_id=dataset_id,
        data_kind="quote",
        instrument_handle=instrument_handle,
        quality_state=quality_state,
        coverage_start=captured_at,
        coverage_end=captured_at,
        row_count=1,
        freshness=quote.freshness,
        delayed_by_minutes=quote.delayed_by_minutes,
        warnings=quote.warnings,
        fingerprints=fingerprints,
    )
    return SyncResult(
        status=(
            SyncStatus.COMPLETE if quality_state is QualityState.COMPLETE else SyncStatus.DEGRADED
        ),
        source_request_count=1,
        datasets=(summary,),
    )


async def capture_option_chain(
    handle: str,
    expiries: Sequence[date],
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
) -> SyncResult:
    """Capture one bounded Saxo option-chain request with entitlement proof."""
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("market sync requires SaxoAnalyticsProvider")
    instrument_handle = _validate_instrument_handle(handle)
    requested_expiries = _validate_expiries(expiries, config)
    selector = _instrument_selector(config, instrument_handle)
    captured_at = _require_utc_clock(clock())
    request: dict[str, object] = {
        "ExpiryDates": [expiry.isoformat() for expiry in requested_expiries],
        "OptionRootId": selector.identifier,
    }
    capture = build_source_capture_context(
        {"options_chain_reference_v1": request},
        captured_at=captured_at,
    )
    try:
        pages = tuple(
            [
                page
                async for page in provider.fetch(
                    "options_chain_reference_v1",
                    request,
                    capture=capture,
                )
            ],
        )
    except SourceEntitlementError as error:
        correction_state = {
            "capture_kind": "point",
            "captured_at": captured_at.isoformat(),
            "requested_expiries": [expiry.isoformat() for expiry in requested_expiries],
        }
        entitlement = {
            "error_code": error.error_code,
            "http_status": error.http_status,
            "state": "denied",
        }
        contract = source_contracts_by_id()["options_chain_reference_v1"]
        contract_sha256 = source_contract_fingerprint(contract)
        fingerprints = IngestionFingerprints(
            raw_pages_sha256=_fingerprint(entitlement),
            normalized_rows_sha256=_fingerprint([]),
            source_contract_sha256=_fingerprint([contract_sha256]),
            entitlements_sha256=_fingerprint(entitlement),
            correction_state_sha256=_fingerprint(correction_state),
        )
        dataset_id = _persist_entitlement_refusal(
            config=config,
            capture_revision=capture.capture_revision,
            captured_at=captured_at,
            instrument_handle=instrument_handle,
            instrument_scope_sha256=capture.instrument_scope_sha256,
            request_fingerprint_sha256=capture.request_fingerprints["options_chain_reference_v1"],
            entitlement=entitlement,
            correction_state=correction_state,
            fingerprints=fingerprints,
        )
        summary = OptionChainDatasetSummary(
            dataset_id=dataset_id,
            data_kind="option_chain",
            instrument_handle=instrument_handle,
            quality_state=QualityState.MISSING,
            coverage_start=captured_at,
            coverage_end=captured_at,
            row_count=0,
            expiries=requested_expiries,
            entitlement_state="denied",
            entitlement_error_code=error.error_code,
            warnings=("option_entitlement_denied",),
            fingerprints=fingerprints,
        )
        return SyncResult(
            status=SyncStatus.REFUSED,
            source_request_count=1,
            datasets=(summary,),
        )
    envelope = build_source_capture_envelope(capture, pages)
    return _persist_available_option_chain(
        config=config,
        envelope_pages=envelope.pages,
        instrument_handle=instrument_handle,
        requested_expiries=requested_expiries,
        captured_at=captured_at,
    )


async def sync_research_data(
    request: SyncResearchRequest,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
) -> SyncResult:
    """Run one bounded batch immediately and return dataset handles only."""
    if type(request) is not SyncResearchRequest:
        raise TypeError("research sync requires SyncResearchRequest")
    if len(request.items) > config.limits.sync_instruments:
        raise SyncLimitError("synchronous research instrument limit exceeded")
    handles = {item.handle for item in request.items}
    if len(handles) > config.limits.sync_instruments:
        raise SyncLimitError("synchronous research instrument limit exceeded")
    projected_rows = 0
    for item in request.items:
        if isinstance(item, PriceBarSyncSpec):
            _require_utc_range(item.start, item.end)
            projected_rows += _row_bound(item.start, item.end, item.interval)
    if projected_rows > config.limits.sync_rows:
        raise SyncLimitError("synchronous market data row limit exceeded")
    results: list[SyncResult] = []
    for item in request.items:
        if isinstance(item, PriceBarSyncSpec):
            result = await sync_price_bars(
                item.handle,
                item.interval,
                item.start,
                item.end,
                provider=provider,
                config=config,
                clock=clock,
            )
        elif isinstance(item, QuoteSyncSpec):
            result = await capture_quote(
                item.handle,
                provider=provider,
                config=config,
                clock=clock,
                max_age=item.max_age,
            )
        else:
            result = await capture_option_chain(
                item.handle,
                item.expiries,
                provider=provider,
                config=config,
                clock=clock,
            )
        results.append(result)
    statuses = {result.status for result in results}
    status = (
        SyncStatus.COMPLETE
        if statuses == {SyncStatus.COMPLETE}
        else SyncStatus.REFUSED
        if statuses == {SyncStatus.REFUSED}
        else SyncStatus.DEGRADED
    )
    return SyncResult(
        status=status,
        source_request_count=sum(result.source_request_count for result in results),
        datasets=tuple(dataset for result in results for dataset in result.datasets),
    )


def get_dataset(
    dataset_id: str,
    page: int,
    limit: int,
    *,
    config: AnalyticsConfig,
) -> DatasetPage:
    """Return one fixed, bounded page from a safe market dataset handle."""
    validated_id = _validate_dataset_id(dataset_id)
    if page < 1:
        raise SyncValidationError("dataset page must be at least one")
    if not 1 <= limit <= config.limits.response_rows:
        raise SyncLimitError("dataset response row limit exceeded")
    metadata = _dataset_sync_metadata(config, validated_id)
    if metadata.get("data_kind") == "option_chain":
        if metadata.get("entitlement_state") == "denied":
            return DatasetPage(
                dataset_id=validated_id,
                page=page,
                limit=limit,
                total_rows=0,
                rows=(),
                next_page=None,
            )
        return _option_dataset_page(validated_id, page, limit, config)
    if metadata.get("data_kind") == "quote":
        return _quote_dataset_page(
            validated_id,
            page,
            limit,
            metadata,
            config,
        )
    if metadata.get("data_kind") != "price_bars":
        raise SyncValidationError("dataset kind is not supported")
    instrument_handle = _validate_instrument_handle(metadata.get("instrument_handle"))
    interval = ChartInterval(_required_text(metadata.get("interval"), "dataset interval"))
    start = _required_utc_text(metadata.get("requested_start"), "dataset coverage")
    end = _required_utc_text(metadata.get("requested_end"), "dataset coverage")
    capture_revision = _required_text(
        metadata.get("capture_revision"),
        "dataset capture revision",
    )
    prior_page_ids = _stored_text_tuple(
        metadata.get("prior_page_ids"),
        "dataset prior source pages",
    )
    offset = (page - 1) * limit
    connection = _connect(config, read_only=True)
    try:
        total_row = connection.execute(
            _PRICE_BAR_COUNT_SQL,
            (
                capture_revision,
                instrument_handle,
                interval.value,
                start,
                end,
                capture_revision,
                list(prior_page_ids),
            ),
        ).fetchone()
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                _PRICE_BAR_PAGE_SQL,
                (
                    capture_revision,
                    instrument_handle,
                    interval.value,
                    start,
                    end,
                    capture_revision,
                    list(prior_page_ids),
                    limit,
                    offset,
                ),
            ).fetchall(),
        )
    finally:
        connection.close()
    if total_row is None or not isinstance(total_row[0], int):
        raise SyncError("dataset row count is invalid")
    total_rows = total_row[0]
    parsed_rows = tuple(
        PriceBarDatasetRow(
            instrument_handle=_required_text(row[0], "stored instrument handle"),
            bar_time=_epoch_us(row[1]),
            interval=interval,
            open_value=_optional_float(row[2]),
            high_value=_optional_float(row[3]),
            low_value=_optional_float(row[4]),
            close_value=_required_float(row[5]),
            volume_value=_optional_float(row[6]),
            adjusted=False,
        )
        for row in rows
    )
    return DatasetPage(
        dataset_id=validated_id,
        page=page,
        limit=limit,
        total_rows=total_rows,
        rows=parsed_rows,
        next_page=page + 1 if offset + len(parsed_rows) < total_rows else None,
    )


def _quote_dataset_page(
    dataset_id: str,
    page: int,
    limit: int,
    metadata: Mapping[str, object],
    config: AnalyticsConfig,
) -> DatasetPage:
    instrument_handle = _validate_instrument_handle(metadata.get("instrument_handle"))
    freshness_value = metadata.get("freshness")
    if freshness_value not in {"fresh", "stale"}:
        raise SyncError("stored quote freshness is invalid")
    freshness = cast("Literal['fresh', 'stale']", freshness_value)
    warnings = _warning_tuple(metadata.get("warnings"))
    offset = (page - 1) * limit
    connection = _connect(config, read_only=True)
    try:
        total_row = connection.execute(
            """
            SELECT count(*)
            FROM quotes AS q
            JOIN dataset_source_pages AS dsp ON dsp.page_id = q.page_id
            WHERE dsp.dataset_id = ? AND q.instrument_handle = ?
            """,
            (dataset_id, instrument_handle),
        ).fetchone()
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    q.instrument_handle,
                    epoch_us(q.captured_at),
                    q.bid_value,
                    q.ask_value,
                    q.mid_value
                FROM quotes AS q
                JOIN dataset_source_pages AS dsp ON dsp.page_id = q.page_id
                WHERE dsp.dataset_id = ? AND q.instrument_handle = ?
                ORDER BY q.captured_at, q.quote_id
                LIMIT ? OFFSET ?
                """,
                (dataset_id, instrument_handle, limit, offset),
            ).fetchall(),
        )
    finally:
        connection.close()
    if total_row is None or not isinstance(total_row[0], int):
        raise SyncError("dataset row count is invalid")
    total_rows = total_row[0]
    parsed_rows = tuple(
        QuoteDatasetRow(
            instrument_handle=_required_text(row[0], "stored instrument handle"),
            captured_at=_epoch_us(row[1]),
            bid_value=_optional_float(row[2]),
            ask_value=_optional_float(row[3]),
            mid_value=_optional_float(row[4]),
            freshness=freshness,
            warnings=warnings,
        )
        for row in rows
    )
    return DatasetPage(
        dataset_id=dataset_id,
        page=page,
        limit=limit,
        total_rows=total_rows,
        rows=parsed_rows,
        next_page=page + 1 if offset + len(parsed_rows) < total_rows else None,
    )


def _option_dataset_page(
    dataset_id: str,
    page: int,
    limit: int,
    config: AnalyticsConfig,
) -> DatasetPage:
    offset = (page - 1) * limit
    connection = _connect(config, read_only=True)
    try:
        total_row = connection.execute(
            """
            SELECT count(*)
            FROM option_snapshots AS o
            JOIN dataset_source_pages AS dsp ON dsp.page_id = o.page_id
            WHERE dsp.dataset_id = ?
            """,
            (dataset_id,),
        ).fetchone()
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    o.instrument_handle,
                    o.underlying_handle,
                    epoch_us(o.captured_at),
                    o.expiry_date,
                    o.strike_value,
                    o.currency,
                    o.put_call
                FROM option_snapshots AS o
                JOIN dataset_source_pages AS dsp ON dsp.page_id = o.page_id
                WHERE dsp.dataset_id = ?
                ORDER BY o.expiry_date, o.strike_value, o.put_call, o.instrument_handle
                LIMIT ? OFFSET ?
                """,
                (dataset_id, limit, offset),
            ).fetchall(),
        )
    finally:
        connection.close()
    if total_row is None or not isinstance(total_row[0], int):
        raise SyncError("dataset row count is invalid")
    total_rows = total_row[0]
    parsed_rows = tuple(_option_dataset_row(row) for row in rows)
    return DatasetPage(
        dataset_id=dataset_id,
        page=page,
        limit=limit,
        total_rows=total_rows,
        rows=parsed_rows,
        next_page=page + 1 if offset + len(parsed_rows) < total_rows else None,
    )


def _option_dataset_row(row: tuple[object, ...]) -> OptionReferenceDatasetRow:
    expiry = row[3]
    if isinstance(expiry, datetime) or not isinstance(expiry, date):
        raise SyncError("stored option expiry is invalid")
    currency_value = _required_text(row[5], "stored option currency")
    put_call_value = _required_text(row[6], "stored option put-call value")
    if put_call_value not in {"call", "put"}:
        raise SyncError("stored option put-call value is invalid")
    put_call = cast("Literal['call', 'put']", put_call_value)
    return OptionReferenceDatasetRow(
        instrument_handle=_required_text(row[0], "stored option handle"),
        underlying_handle=_required_text(row[1], "stored underlying handle"),
        captured_at=_epoch_us(row[2]),
        expiry=expiry,
        strike_value=_required_float(row[4]),
        currency=None if currency_value == _MISSING_CURRENCY else currency_value,
        put_call=put_call,
    )


class _InstrumentSelector(_StrictModel):
    identifier: int = Field(ge=0)
    asset_type: str = Field(min_length=1, max_length=64)


class _ExistingBarState(_StrictModel):
    coverage_start: datetime
    coverage_end: datetime


def _validate_expiries(
    expiries: Sequence[date],
    config: AnalyticsConfig,
) -> tuple[date, ...]:
    values = tuple(expiries)
    if not values:
        raise SyncValidationError("option-chain expiries cannot be empty")
    if len(values) > config.limits.sync_instruments:
        raise SyncLimitError("option-chain expiry limit exceeded")
    if any(isinstance(value, datetime) or type(value) is not date for value in values):
        raise SyncValidationError("option-chain expiry is invalid")
    if values != tuple(sorted(set(values))):
        raise SyncValidationError("option-chain expiries must be sorted and unique")
    return values


def _instrument_selector(
    config: AnalyticsConfig,
    instrument_handle: str,
) -> _InstrumentSelector:
    connection = _connect(config, read_only=True)
    try:
        row = connection.execute(
            "SELECT metadata_json FROM safe_instruments WHERE instrument_handle = ?",
            (instrument_handle,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], str):
        raise SyncValidationError("instrument handle is not resolved")
    try:
        loaded_payload = json.loads(row[0])
    except (TypeError, ValueError) as error:
        raise SyncError("stored instrument selector is invalid") from error
    if not isinstance(loaded_payload, dict):
        raise SyncError("stored instrument selector is invalid")
    payload = cast("dict[str, object]", loaded_payload)
    identifier = payload.get("identifier")
    asset_type = payload.get("asset_type")
    try:
        return _InstrumentSelector.model_validate(
            {"identifier": identifier, "asset_type": asset_type},
            strict=True,
        )
    except ValidationError as error:
        raise SyncError("stored instrument selector is invalid") from error


def _existing_bar_state(
    config: AnalyticsConfig,
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
) -> _ExistingBarState | None:
    connection = _connect(config, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT epoch_us(min(bar_time)), epoch_us(max(bar_time))
            FROM price_bars
            WHERE
                instrument_handle = ?
                AND duration = ?
                AND bar_time BETWEEN ? AND ?
            """,
            (instrument_handle, interval.value, start, end),
        ).fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None or row[1] is None:
        return None
    return _ExistingBarState(
        coverage_start=_epoch_us(row[0]),
        coverage_end=_epoch_us(row[1]),
    )


def _latest_bar_page_ids(
    config: AnalyticsConfig,
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
) -> tuple[str, ...]:
    connection = _connect(config, read_only=True)
    try:
        rows = connection.execute(
            """
            WITH ranked AS (
                SELECT
                    b.page_id,
                    row_number() OVER (
                        PARTITION BY b.instrument_handle, b.bar_time, b.duration
                        ORDER BY
                            p.source_timestamp DESC,
                            p.ingested_at DESC,
                            b.page_id DESC
                    ) AS revision_rank
                FROM price_bars AS b
                JOIN source_pages AS p ON p.page_id = b.page_id
                WHERE
                    b.instrument_handle = ?
                    AND b.duration = ?
                    AND b.bar_time BETWEEN ? AND ?
            )
            SELECT DISTINCT page_id
            FROM ranked
            WHERE revision_rank = 1
            ORDER BY page_id
            """,
            (instrument_handle, interval.value, start, end),
        ).fetchall()
    finally:
        connection.close()
    return tuple(_required_text(row[0], "stored source page ID") for row in rows)


def _capture_fingerprints(
    pages: Sequence[SourcePage],
    normalized_rows_sha256: str,
    correction_state: Mapping[str, object],
) -> IngestionFingerprints:
    return IngestionFingerprints(
        raw_pages_sha256=_fingerprint(
            [page.page_fingerprint_sha256 for page in pages],
        ),
        normalized_rows_sha256=normalized_rows_sha256,
        source_contract_sha256=_fingerprint(
            sorted({page.contract_sha256 for page in pages}),
        ),
        entitlements_sha256=_fingerprint(
            [page.source_quality.model_dump(mode="json") for page in pages],
        ),
        correction_state_sha256=_fingerprint(dict(correction_state)),
    )


def _persist_chart_capture(  # noqa: PLR0913
    *,
    config: AnalyticsConfig,
    pages: Sequence[SourcePage],
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
    quality_state: QualityState,
    fingerprints: IngestionFingerprints,
    correction_state: Mapping[str, object],
    prior_page_ids: Sequence[str],
) -> tuple[dict[int, str], str]:
    if not pages:
        raise SyncError("chart capture contains no source page")
    sync_metadata: dict[str, object] = {
        "adjustment_status": "unadjusted",
        "capture_revision": pages[0].capture_revision,
        "captured_at": pages[0].source_timestamp.isoformat(),
        "correction_state": dict(correction_state),
        "data_kind": "price_bars",
        "fingerprints": fingerprints.model_dump(mode="json"),
        "instrument_handle": instrument_handle,
        "interval": interval.value,
        "prior_page_ids": list(prior_page_ids),
        "requested_end": end.isoformat(),
        "requested_start": start.isoformat(),
        "return_series_label": "price_return",
    }
    return _persist_market_capture(
        config=config,
        pages=pages,
        instrument_handle=instrument_handle,
        coverage_start=start,
        coverage_end=end,
        quality_state=quality_state,
        sync_metadata=sync_metadata,
    )


def _persist_market_capture(  # noqa: PLR0913
    *,
    config: AnalyticsConfig,
    pages: Sequence[SourcePage],
    instrument_handle: str,
    coverage_start: datetime,
    coverage_end: datetime,
    quality_state: QualityState,
    sync_metadata: Mapping[str, object],
) -> tuple[dict[int, str], str]:
    if not pages:
        raise SyncError("market capture contains no source page")
    normalized_bytes = sum(page.row_count for page in pages) * 128
    try:
        AnalyticsStore.ensure_owner_capacity(config, normalized_bytes)
    except StoreQuotaError as error:
        raise SyncLimitError("analytics store quota refuses market ingestion") from error
    store = AnalyticsStore.open(config)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    stored_page_ids: dict[int, str] = {}
    try:
        with store.transaction():
            for source_page in pages:
                serialized = source_page.model_dump(mode="json")
                rows = serialized["rows"]
                stored = store.put_source_page(
                    source_kind=source_page.source_kind,
                    page_key=(
                        f"{source_page.contract_id}:{source_page.page_number}:"
                        f"{source_page.capture_revision.removeprefix('capture:')}"
                    ),
                    source_revision=source_page.capture_revision,
                    source_native_revision=source_page.source_revision,
                    contract_name=source_page.contract_id,
                    contract_sha256=source_page.contract_sha256,
                    payload={
                        "contract_id": source_page.contract_id,
                        "data_version": source_page.data_version,
                        "page_fingerprint_sha256": source_page.page_fingerprint_sha256,
                        "page_number": source_page.page_number,
                        "request_fingerprint_sha256": (source_page.request_fingerprint_sha256),
                        "rows": rows,
                        "source_native_revision": source_page.source_revision,
                        "source_quality": source_page.source_quality.model_dump(mode="json"),
                        "sync_metadata": dict(sync_metadata),
                    },
                    row_count=source_page.row_count,
                    source_timestamp=source_page.source_timestamp,
                    account_scope=source_page.account_scope,
                    instrument_handle=instrument_handle,
                    instrument_scope_sha256=source_page.instrument_scope_sha256,
                )
                stored_page_ids[source_page.page_number] = stored.page_id
            store.create_dataset(
                dataset_id=dataset_id,
                account_scope=pages[0].account_scope,
                source_scope="saxo_openapi",
                source_revision=pages[0].capture_revision,
                source_page_ids=tuple(stored_page_ids.values()),
                created_at=pages[0].source_timestamp,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                quality_state=quality_state,
            )
    finally:
        store.close()
    return stored_page_ids, dataset_id


def _persist_entitlement_refusal(  # noqa: PLR0913
    *,
    config: AnalyticsConfig,
    capture_revision: str,
    captured_at: datetime,
    instrument_handle: str,
    instrument_scope_sha256: str,
    request_fingerprint_sha256: str,
    entitlement: Mapping[str, object],
    correction_state: Mapping[str, object],
    fingerprints: IngestionFingerprints,
) -> str:
    contract = source_contracts_by_id()["options_chain_reference_v1"]
    contract_sha256 = source_contract_fingerprint(contract)
    sync_metadata = {
        "capture_revision": capture_revision,
        "captured_at": captured_at.isoformat(),
        "correction_state": dict(correction_state),
        "data_kind": "option_chain",
        "entitlement_error_code": entitlement.get("error_code"),
        "entitlement_state": "denied",
        "expiries": correction_state.get("requested_expiries"),
        "fingerprints": fingerprints.model_dump(mode="json"),
        "instrument_handle": instrument_handle,
        "warnings": ["option_entitlement_denied"],
    }
    payload: dict[str, object] = {
        "contract_id": contract.contract_id,
        "entitlement": dict(entitlement),
        "request_fingerprint_sha256": request_fingerprint_sha256,
        "rows": list[object](),
        "sync_metadata": sync_metadata,
    }
    AnalyticsStore.ensure_owner_capacity(
        config,
        len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()),
    )
    store = AnalyticsStore.open(config)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    try:
        with store.transaction():
            stored = store.put_source_page(
                source_kind=contract.source_kind,
                page_key=(
                    f"{contract.contract_id}:entitlement:"
                    f"{capture_revision.removeprefix('capture:')}"
                ),
                source_revision=capture_revision,
                source_native_revision=(
                    "entitlement:"
                    + _required_text(
                        entitlement.get("error_code") or "Denied",
                        "entitlement error code",
                    )
                ),
                contract_name=contract.contract_id,
                contract_sha256=contract_sha256,
                payload=payload,
                row_count=0,
                source_timestamp=captured_at,
                account_scope="aggregate",
                instrument_handle=instrument_handle,
                instrument_scope_sha256=instrument_scope_sha256,
            )
            store.create_dataset(
                dataset_id=dataset_id,
                account_scope="aggregate",
                source_scope="saxo_openapi",
                source_revision=capture_revision,
                source_page_ids=(stored.page_id,),
                created_at=captured_at,
                coverage_start=captured_at,
                coverage_end=captured_at,
                quality_state=QualityState.MISSING,
            )
    finally:
        store.close()
    return dataset_id


def _persist_available_option_chain(
    *,
    config: AnalyticsConfig,
    envelope_pages: Sequence[SourcePage],
    instrument_handle: str,
    requested_expiries: tuple[date, ...],
    captured_at: datetime,
) -> SyncResult:
    rows = tuple(row for page in envelope_pages for row in page.rows)
    if len(rows) != 1:
        raise SyncError("option-chain capture did not return exactly one source row")
    if len(requested_expiries) == 1:
        selector = _instrument_selector(config, instrument_handle)
        chain = normalize_option_chain(
            row=rows[0],
            expected_root_id=selector.identifier,
            expiry=requested_expiries[0],
        )
    else:
        chain = NormalizedOptionChain(
            option_root_id=_instrument_selector(config, instrument_handle).identifier,
            expiry=requested_expiries[0],
            options=(),
            warnings=("option_expiry_mapping_unavailable",),
            fingerprint_sha256=_fingerprint([]),
        )
    correction_state = {
        "capture_kind": "point",
        "captured_at": captured_at.isoformat(),
        "requested_expiries": [expiry.isoformat() for expiry in requested_expiries],
        "source_native_revisions": sorted(
            {page.source_revision for page in envelope_pages},
        ),
    }
    fingerprints = _capture_fingerprints(
        envelope_pages,
        chain.fingerprint_sha256,
        correction_state,
    )
    source_limited = any(page.source_quality.state == "limited" for page in envelope_pages)
    warnings = set(chain.warnings)
    if source_limited:
        warnings.add("option_source_quality_limited")
    sorted_warnings = tuple(sorted(warnings))
    quality_state = (
        QualityState.MISSING
        if not chain.options
        else QualityState.PARTIAL
        if sorted_warnings
        else QualityState.COMPLETE
    )
    sync_metadata = {
        "capture_revision": envelope_pages[0].capture_revision,
        "captured_at": captured_at.isoformat(),
        "correction_state": correction_state,
        "data_kind": "option_chain",
        "entitlement_error_code": None,
        "entitlement_state": "available",
        "expiries": [expiry.isoformat() for expiry in requested_expiries],
        "fingerprints": fingerprints.model_dump(mode="json"),
        "instrument_handle": instrument_handle,
        "normalized_rows": [option.model_dump(mode="json") for option in chain.options],
        "warnings": list(sorted_warnings),
    }
    stored_pages, dataset_id = _persist_market_capture(
        config=config,
        pages=envelope_pages,
        instrument_handle=instrument_handle,
        coverage_start=captured_at,
        coverage_end=captured_at,
        quality_state=quality_state,
        sync_metadata=sync_metadata,
    )
    _persist_normalized_options(
        config=config,
        pages=envelope_pages,
        stored_page_ids=stored_pages,
        underlying_handle=instrument_handle,
        chain=chain,
    )
    summary = OptionChainDatasetSummary(
        dataset_id=dataset_id,
        data_kind="option_chain",
        instrument_handle=instrument_handle,
        quality_state=quality_state,
        coverage_start=captured_at,
        coverage_end=captured_at,
        row_count=len(chain.options),
        expiries=requested_expiries,
        entitlement_state="available",
        entitlement_error_code=None,
        warnings=sorted_warnings,
        fingerprints=fingerprints,
    )
    return SyncResult(
        status=(
            SyncStatus.COMPLETE if quality_state is QualityState.COMPLETE else SyncStatus.DEGRADED
        ),
        source_request_count=1,
        datasets=(summary,),
    )


def _persist_normalized_bars(
    *,
    config: AnalyticsConfig,
    pages: Sequence[SourcePage],
    stored_page_ids: Mapping[int, str],
    series: NormalizedPriceSeries,
) -> None:
    source_page_by_time: dict[datetime, SourcePage] = {}
    for source_page in pages:
        for row in source_page.rows:
            raw_time = row.get("Time")
            if not isinstance(raw_time, str):
                raise SyncError("validated chart row lost its timestamp")
            source_page_by_time[_parse_source_time(raw_time)] = source_page
    values: list[tuple[object, ...]] = []
    for bar in series.bars:
        source_page = source_page_by_time.get(bar.bar_time)
        if source_page is None:
            raise SyncError("normalized chart bar lost its source page")
        values.append(
            (
                stored_page_ids[source_page.page_number],
                bar.instrument_handle,
                source_page.source_revision,
                bar.bar_time,
                bar.interval.value,
                bar.open_value,
                bar.high_value,
                bar.low_value,
                bar.close_value,
                bar.volume_value,
                None,
                False,
            ),
        )
    if not values:
        return
    with _write_transaction(config) as connection:
        connection.executemany(
            """
            INSERT INTO price_bars (
                page_id,
                instrument_handle,
                source_revision,
                bar_time,
                duration,
                open_value,
                high_value,
                low_value,
                close_value,
                volume_value,
                currency,
                adjusted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            values,
        )
        connection.execute(
            "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
        )


def _persist_normalized_quote(
    *,
    config: AnalyticsConfig,
    pages: Sequence[SourcePage],
    stored_page_ids: Mapping[int, str],
    quote: NormalizedQuote,
) -> None:
    if len(pages) != 1:
        raise SyncError("quote capture contains an invalid source-page count")
    source_page = pages[0]
    quote_id = "quote:" + _fingerprint(
        {
            "capture_revision": source_page.capture_revision,
            "instrument_handle": quote.instrument_handle,
            "quote_fingerprint": quote.fingerprint_sha256,
        },
    )
    with _write_transaction(config) as connection:
        connection.execute(
            """
            INSERT INTO quotes (
                quote_id,
                page_id,
                instrument_handle,
                source_revision,
                captured_at,
                bid_value,
                ask_value,
                mid_value,
                currency,
                fingerprint_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                quote_id,
                stored_page_ids[source_page.page_number],
                quote.instrument_handle,
                source_page.source_revision,
                quote.captured_at,
                quote.bid_value,
                quote.ask_value,
                quote.mid_value,
                None,
                quote.fingerprint_sha256,
            ),
        )
        connection.execute(
            "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
        )


def _persist_normalized_options(
    *,
    config: AnalyticsConfig,
    pages: Sequence[SourcePage],
    stored_page_ids: Mapping[int, str],
    underlying_handle: str,
    chain: NormalizedOptionChain,
) -> None:
    if not chain.options:
        return
    if len(pages) != 1:
        raise SyncError("option-chain capture contains an invalid source-page count")
    source_page = pages[0]
    with _write_transaction(config) as connection:
        option_handles = _option_handles(
            connection,
            chain.options,
            source_page,
        )
        for option in chain.options:
            option_handle = option_handles[option.source_identifier]
            snapshot_id = "option:" + _fingerprint(
                {
                    "capture_revision": source_page.capture_revision,
                    "option_fingerprint": option.fingerprint_sha256,
                    "underlying_handle": underlying_handle,
                },
            )
            payload = json.dumps(
                {
                    "currency": None,
                    "currency_state": "unavailable",
                    "option_reference_sha256": option.fingerprint_sha256,
                },
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT INTO option_snapshots (
                    option_snapshot_id,
                    page_id,
                    instrument_handle,
                    underlying_handle,
                    source_revision,
                    captured_at,
                    expiry_date,
                    strike_value,
                    currency,
                    put_call,
                    fingerprint_sha256,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    snapshot_id,
                    stored_page_ids[source_page.page_number],
                    option_handle,
                    underlying_handle,
                    source_page.source_revision,
                    source_page.source_timestamp,
                    option.expiry,
                    option.strike_value,
                    _MISSING_CURRENCY,
                    option.put_call,
                    option.fingerprint_sha256,
                    payload,
                ),
            )
        connection.execute(
            "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
        )


def _option_handles(
    connection: duckdb.DuckDBPyConnection,
    options: Sequence[NormalizedOptionReference],
    source_page: SourcePage,
) -> dict[int, str]:
    rows = connection.execute(
        """
        SELECT instrument_handle, metadata_json
        FROM safe_instruments
        WHERE asset_type = 'ContractOption'
        """,
    ).fetchall()
    handles: dict[int, str] = {}
    for raw_handle, raw_metadata in rows:
        if not isinstance(raw_handle, str) or not isinstance(raw_metadata, str):
            raise SyncError("stored option instrument is invalid")
        try:
            loaded_metadata = json.loads(raw_metadata)
        except (TypeError, ValueError) as error:
            raise SyncError("stored option instrument is invalid") from error
        if not isinstance(loaded_metadata, dict):
            raise SyncError("stored option instrument is invalid")
        metadata = cast("dict[str, object]", loaded_metadata)
        identifier = metadata.get("identifier")
        if type(identifier) is int:
            handles[identifier] = _validate_instrument_handle(raw_handle)
    for option in options:
        if option.source_identifier in handles:
            continue
        option_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
        metadata: dict[str, object] = {
            "aliases": list[str](),
            "asset_type": "ContractOption",
            "display_label": "Saxo option contract",
            "exchange": None,
            "identifier": option.source_identifier,
            "symbol": None,
        }
        metadata_json = json.dumps(
            metadata,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO safe_instruments (
                instrument_handle,
                asset_type,
                safe_label,
                source_revision,
                source_timestamp,
                fingerprint_sha256,
                metadata_json
            )
            VALUES (?, 'ContractOption', ?, ?, ?, ?, ?)
            """,
            (
                option_handle,
                "Saxo option contract",
                source_page.source_revision,
                source_page.source_timestamp,
                hashlib.sha256(metadata_json.encode()).hexdigest(),
                metadata_json,
            ),
        )
        handles[option.source_identifier] = option_handle
    return handles


def _dataset_sync_metadata(
    config: AnalyticsConfig,
    dataset_id: str,
) -> dict[str, object]:
    connection = _connect(config, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT p.payload_json
            FROM datasets AS d
            JOIN dataset_source_pages AS dsp ON dsp.dataset_id = d.dataset_id
            JOIN source_pages AS p ON p.page_id = dsp.page_id
            WHERE d.dataset_id = ?
            ORDER BY p.source_timestamp DESC, p.ingested_at DESC, p.page_key DESC
            LIMIT 1
            """,
            (dataset_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise DatasetNotFoundError("research dataset does not exist")
    if not isinstance(row[0], str):
        raise SyncError("stored dataset metadata is invalid")
    try:
        loaded_payload = json.loads(row[0])
    except (TypeError, ValueError) as error:
        raise SyncError("stored dataset metadata is invalid") from error
    if not isinstance(loaded_payload, dict):
        raise SyncError("stored dataset metadata is invalid")
    payload = cast("dict[str, object]", loaded_payload)
    sync_metadata = payload.get("sync_metadata")
    if not isinstance(sync_metadata, dict):
        raise SyncError("stored dataset metadata is invalid")
    return cast("dict[str, object]", sync_metadata)


@contextmanager
def _writer_lock(config: AnalyticsConfig) -> Generator[None]:
    lock_path = store_writer_lock_path(config.paths.store_path)
    descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SyncError("another analytics writer owns the writer lock") from error
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _write_transaction(
    config: AnalyticsConfig,
) -> Generator[duckdb.DuckDBPyConnection]:
    with _writer_lock(config):
        connection = _connect(config, read_only=False)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                yield connection
            except BaseException:
                with suppress(duckdb.Error):
                    connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()


def _connect(
    config: AnalyticsConfig,
    *,
    read_only: bool,
) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        str(config.paths.store_path),
        read_only=read_only,
        config=dict(_CONNECTION_CONFIG),
    )


def _validate_instrument_handle(value: object) -> str:
    try:
        return _INSTRUMENT_HANDLE_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise SyncValidationError("instrument handle is invalid") from error


def _validate_dataset_id(value: object) -> str:
    try:
        return _DATASET_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise SyncValidationError("dataset handle is invalid") from error


def _require_utc_range(start: datetime, end: datetime) -> None:
    if (
        start.tzinfo is None
        or start.utcoffset() != timedelta(0)
        or end.tzinfo is None
        or end.utcoffset() != timedelta(0)
    ):
        raise SyncValidationError("market sync range must use UTC")
    if end < start:
        raise SyncValidationError("market sync range end precedes its start")


def _require_utc_clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SyncValidationError("market sync clock must use UTC")
    return value


def _row_bound(start: datetime, end: datetime, interval: ChartInterval) -> int:
    return int((end - start) / interval.delta) + 1


def _parse_source_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SyncError("stored source time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SyncError("stored source time is invalid")
    return parsed.astimezone(UTC)


def _fingerprint(value: object) -> str:
    material = json.dumps(
        value,
        allow_nan=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SyncError(f"{label} is invalid")
    return value


def _required_utc_text(value: object, label: str) -> datetime:
    parsed = _parse_source_time(_required_text(value, label))
    if parsed.utcoffset() != timedelta(0):
        raise SyncError(f"{label} is invalid")
    return parsed


def _warning_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SyncError("stored dataset warnings are invalid")
    items = cast("list[object]", value)
    if any(not isinstance(item, str) or not item for item in items):
        raise SyncError("stored dataset warnings are invalid")
    warnings = cast("list[str]", items)
    if warnings != sorted(set(warnings)):
        raise SyncError("stored dataset warnings are invalid")
    return tuple(warnings)


def _stored_text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SyncError(f"{label} is invalid")
    items = cast("list[object]", value)
    if any(not isinstance(item, str) or not item for item in items):
        raise SyncError(f"{label} is invalid")
    return tuple(cast("list[str]", items))


def _epoch_us(value: object) -> datetime:
    if type(value) is not int:
        raise SyncError("stored dataset timestamp is invalid")
    return datetime.fromtimestamp(value / 1_000_000, UTC)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _required_float(value)


def _required_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SyncError("stored dataset number is invalid")
    return float(value)


_PRICE_BAR_RANKED_SQL: Final = """
    WITH ranked AS (
        SELECT
            b.instrument_handle,
            epoch_us(b.bar_time) AS bar_time_us,
            b.open_value,
            b.high_value,
            b.low_value,
            b.close_value,
            b.volume_value,
            row_number() OVER (
                PARTITION BY b.instrument_handle, b.bar_time, b.duration
                ORDER BY
                    (p.source_revision = ?) DESC,
                    p.source_timestamp DESC,
                    p.ingested_at DESC,
                    b.page_id DESC
            ) AS revision_rank
        FROM price_bars AS b
        JOIN source_pages AS p ON p.page_id = b.page_id
        WHERE
            b.instrument_handle = ?
            AND b.duration = ?
            AND b.bar_time BETWEEN ? AND ?
            AND (
                p.source_revision = ?
                OR p.page_id = ANY(?)
            )
    )
"""
_PRICE_BAR_COUNT_SQL: Final = (
    _PRICE_BAR_RANKED_SQL  # noqa: S608
    + "SELECT count(*) FROM ranked WHERE revision_rank = 1"
)
_PRICE_BAR_PAGE_SQL: Final = (
    _PRICE_BAR_RANKED_SQL
    + """
        SELECT
            instrument_handle,
            bar_time_us,
            open_value,
            high_value,
            low_value,
            close_value,
            volume_value
        FROM ranked
        WHERE revision_rank = 1
        ORDER BY bar_time_us
        LIMIT ? OFFSET ?
    """
)


__all__ = (
    "DatasetHandleSummary",
    "DatasetNotFoundError",
    "DatasetPage",
    "IngestionFingerprints",
    "OptionChainDatasetSummary",
    "OptionChainSyncSpec",
    "OptionReferenceDatasetRow",
    "PriceBarDatasetRow",
    "PriceBarDatasetSummary",
    "PriceBarSyncSpec",
    "QuoteDatasetRow",
    "QuoteDatasetSummary",
    "QuoteSyncSpec",
    "SyncError",
    "SyncLimitError",
    "SyncResearchRequest",
    "SyncResult",
    "SyncStatus",
    "SyncValidationError",
    "capture_option_chain",
    "capture_quote",
    "get_dataset",
    "sync_price_bars",
    "sync_research_data",
)
