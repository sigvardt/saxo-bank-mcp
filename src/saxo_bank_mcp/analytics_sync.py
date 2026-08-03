from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, cast

import duckdb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_market_data import (
    ChartInterval,
    MarketDataError,
    NormalizedOptionChain,
    NormalizedOptionReference,
    NormalizedPriceBar,
    NormalizedPriceSeries,
    NormalizedQuote,
    normalize_option_chain,
    normalize_price_series,
    normalize_quote,
)
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    DatasetId,
    HandleKind,
    InstrumentHandle,
    QualityState,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_provider import (
    SaxoAnalyticsProvider,
    SourceEntitlementError,
    SourceRequestBudget,
    SourceRequestBudgetError,
)
from saxo_bank_mcp.analytics_source_contracts import (
    SourceCaptureContext,
    SourcePage,
    build_source_capture_context,
    build_source_capture_envelope,
    source_contract_fingerprint,
    source_contracts_by_id,
    source_page_fingerprint,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreQuotaError

_CHART_CONTRACT_ID: Final = "chart_v3"
_MISSING_CURRENCY: Final = "__unavailable__"
_OPTION_SAFE_LABEL: Final = "Saxo option contract"
_NORMALIZED_ROW_ESTIMATE_BYTES: Final = 128
_DATASET_INTEGRITY_COLUMN_COUNT: Final = 9
_PRICE_BAR_INTEGRITY_COLUMN_COUNT: Final = 13
_QUOTE_INTEGRITY_COLUMN_COUNT: Final = 7
_SOURCE_PAGE_INTEGRITY_COLUMN_COUNT: Final = 19
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


type AnalysisInputKind = Literal[
    "portfolio_performance",
    "position_sizing",
    "scenario_custom",
    "portfolio_minimum_variance",
    "derivatives_model",
    "bounded_backtest",
    "pretrade_impact",
]


class AccountSnapshotDatasetSummary(_StrictModel):
    """Handle-only summary for one current server-captured Saxo account snapshot."""

    dataset_id: DatasetId
    data_kind: Literal["account_snapshot"] = "account_snapshot"
    account_alias: str = Field(pattern=r"^aa_[0-9a-f]{32}$")
    eligible_analysis_kinds: tuple[
        Literal[
            "portfolio_performance",
            "position_sizing",
            "scenario_custom",
            "portfolio_minimum_variance",
            "pretrade_impact",
        ],
        ...,
    ]
    quality_state: QualityState
    coverage_start: datetime
    coverage_end: datetime
    row_count: int = Field(ge=0)
    warnings: tuple[str, ...]
    fingerprints: IngestionFingerprints


class AnalysisInputDatasetSummary(_StrictModel):
    """Safe route to one authenticated typed execution context already held by the server."""

    dataset_id: DatasetId
    data_kind: Literal["analysis_input"] = "analysis_input"
    analysis_kind: AnalysisInputKind
    quality_state: QualityState
    coverage_start: datetime
    coverage_end: datetime
    row_count: int = Field(ge=0)
    warnings: tuple[str, ...]
    fingerprints: IngestionFingerprints


type DatasetHandleSummary = (
    PriceBarDatasetSummary
    | QuoteDatasetSummary
    | OptionChainDatasetSummary
    | AccountSnapshotDatasetSummary
    | AnalysisInputDatasetSummary
)


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


class AccountSnapshotSyncSpec(_StrictModel):
    """Request current account source material by a process-issued safe selector only."""

    data_kind: Literal["account_snapshot"] = "account_snapshot"
    safe_account_selector: str = Field(pattern=r"^proc-acct-[A-Za-z0-9_-]{20,64}$")


class AnalysisInputSyncSpec(_StrictModel):
    """Request an authenticated typed context using only existing opaque source handles."""

    data_kind: Literal["analysis_input"] = "analysis_input"
    analysis_kind: AnalysisInputKind
    source_dataset_ids: tuple[DatasetId, ...] = Field(min_length=1, max_length=25)
    origin_analysis_id: AnalysisId | None = None

    @model_validator(mode="after")
    def _validate_origin_binding(self) -> AnalysisInputSyncSpec:
        if (self.analysis_kind == "pretrade_impact") != (self.origin_analysis_id is not None):
            raise ValueError("only pretrade input requires one stored origin analysis")
        return self


type ResearchSyncSpec = (
    PriceBarSyncSpec
    | QuoteSyncSpec
    | OptionChainSyncSpec
    | AccountSnapshotSyncSpec
    | AnalysisInputSyncSpec
)


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


def _source_request_budget(
    config: AnalyticsConfig,
    budget: SourceRequestBudget | None,
) -> SourceRequestBudget:
    if budget is None:
        return SourceRequestBudget(config.limits.sync_instruments)
    if type(budget) is not SourceRequestBudget:
        raise TypeError("source request budget is invalid")
    if budget.limit != config.limits.sync_instruments:
        raise SyncLimitError("source request budget must use the fixed synchronous limit")
    return budget


def _preflight_market_capacity(
    config: AnalyticsConfig,
    projected_rows: int,
) -> None:
    incoming_bytes = max(1, projected_rows) * _NORMALIZED_ROW_ESTIMATE_BYTES
    try:
        AnalyticsStore.ensure_owner_capacity(config, incoming_bytes)
    except StoreQuotaError as error:
        raise SyncLimitError("analytics store quota refuses market ingestion") from error


async def _fetch_source_pages(
    provider: SaxoAnalyticsProvider,
    contract_id: str,
    request: Mapping[str, object],
    capture: SourceCaptureContext,
    budget: SourceRequestBudget,
) -> tuple[SourcePage, ...]:
    try:
        return tuple(
            [
                page
                async for page in provider.fetch(
                    contract_id,
                    request,
                    capture=capture,
                    budget=budget,
                )
            ],
        )
    except SourceRequestBudgetError as error:
        raise SyncLimitError("synchronous source request budget exceeded") from error


def _combine_sync_results(
    results: Sequence[SyncResult],
    *,
    source_request_count: int,
) -> SyncResult:
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
        source_request_count=source_request_count,
        datasets=tuple(dataset for result in results for dataset in result.datasets),
    )


async def sync_price_bars(  # noqa: PLR0913
    handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Fetch one bounded chart window from Saxo and persist it on demand."""
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("market sync requires SaxoAnalyticsProvider")
    budget = _source_request_budget(config, request_budget)
    request_count_start = budget.used
    instrument_handle = _validate_instrument_handle(handle)
    _require_utc_range(start, end)
    requested_rows = _row_bound(start, end, interval)
    if requested_rows > config.limits.sync_rows:
        raise SyncLimitError("synchronous market data row limit exceeded")
    _preflight_market_capacity(config, requested_rows)
    selector = _instrument_selector(config, instrument_handle)
    prior_bar_lineage = _latest_visible_bar_lineage(
        config,
        instrument_handle,
        interval,
        start,
        end,
    )
    prior = _existing_bar_state(prior_bar_lineage, start, end)
    prior_material_sha256 = _visible_price_series_fingerprint(
        prior_bar_lineage,
        instrument_handle,
        interval,
        start,
        end,
    )
    refresh_start = (
        start if prior is None or prior.coverage_start > start else max(start, prior.refresh_start)
    )
    retained_bar_lineage = {
        bar_time: reference
        for bar_time, reference in prior_bar_lineage.items()
        if start <= bar_time < refresh_start
    }
    prior_page_ids = tuple(
        sorted({reference.page_id for reference in prior_bar_lineage.values()}),
    )
    retained_page_ids = tuple(
        sorted({reference.page_id for reference in retained_bar_lineage.values()}),
    )
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
    pages = await _fetch_source_pages(
        provider,
        _CHART_CONTRACT_ID,
        request,
        capture,
        budget,
    )
    envelope = build_source_capture_envelope(capture, pages)
    source_rows = tuple(row for page in envelope.pages for row in page.rows)
    refreshed_series = normalize_price_series(
        rows=source_rows,
        instrument_handle=instrument_handle,
        interval=interval,
        start=refresh_start,
        end=end,
    )
    series = _merged_price_series(
        config,
        instrument_handle,
        interval,
        start,
        end,
        refreshed_series,
        source_rows,
        retained_bar_lineage,
    )
    visible_bar_revisions = {
        bar_time: reference.capture_revision for bar_time, reference in retained_bar_lineage.items()
    }
    visible_bar_revisions.update(
        {bar.bar_time: pages[0].capture_revision for bar in refreshed_series.bars},
    )
    if set(visible_bar_revisions) != {bar.bar_time for bar in series.bars}:
        raise SyncError("visible price-bar lineage does not match normalized rows")
    coverage = _price_series_coverage(series, start, end)
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
        retained_page_ids=prior_page_ids,
    )
    quality_state = (
        QualityState.MISSING
        if not series.bars
        else QualityState.PARTIAL
        if series.warnings
        else QualityState.COMPLETE
    )
    _stored_pages, dataset_id = _persist_chart_capture(
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
        retained_page_ids=retained_page_ids,
        normalized_series=refreshed_series,
        coverage=coverage,
        visible_bar_revisions=visible_bar_revisions,
        invalidate_prior_analyses=(
            prior_material_sha256 is not None and prior_material_sha256 != series.fingerprint_sha256
        ),
    )
    summary = PriceBarDatasetSummary(
        dataset_id=dataset_id,
        data_kind="price_bars",
        instrument_handle=instrument_handle,
        quality_state=quality_state,
        coverage_start=coverage[0],
        coverage_end=coverage[1],
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
        source_request_count=budget.used - request_count_start,
        datasets=(summary,),
    )


async def capture_quote(  # noqa: PLR0913
    handle: str,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
    max_age: timedelta = timedelta(minutes=5),
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Capture one quality-bound Saxo quote and return only its dataset handle."""
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("market sync requires SaxoAnalyticsProvider")
    budget = _source_request_budget(config, request_budget)
    request_count_start = budget.used
    instrument_handle = _validate_instrument_handle(handle)
    _validate_max_age(max_age)
    _preflight_market_capacity(config, 1)
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
    pages = await _fetch_source_pages(
        provider,
        "info_price_v1",
        request,
        capture,
        budget,
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
    source_limited = any(page.source_quality.state == "limited" for page in envelope.pages)
    entitlement_limited = any(
        page.source_quality.entitlement_limited_fields for page in envelope.pages
    )
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
    warnings = set(quote.warnings)
    if entitlement_limited:
        warnings.add("quote_entitlement_limited")
    if not has_price:
        warnings.add("quote_values_missing")
    sorted_warnings = tuple(sorted(warnings))
    row_fingerprint_sha256 = _quote_row_fingerprint(
        quote,
        quality_state=quality_state,
        entitlement_state=entitlement_state,
        warnings=sorted_warnings,
    )
    fingerprints = _capture_fingerprints(
        envelope.pages,
        row_fingerprint_sha256,
        correction_state,
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
        "quality_state": quality_state.value,
        "warnings": list(sorted_warnings),
    }
    _stored_pages, dataset_id = _persist_market_capture(
        config=config,
        pages=envelope.pages,
        instrument_handle=instrument_handle,
        coverage_start=captured_at,
        coverage_end=captured_at,
        quality_state=quality_state,
        sync_metadata=sync_metadata,
        normalized_bytes=_NORMALIZED_ROW_ESTIMATE_BYTES,
        persist_normalized=lambda connection, stored_page_ids: _persist_normalized_quote(
            connection=connection,
            pages=envelope.pages,
            stored_page_ids=stored_page_ids,
            quote=quote,
            row_fingerprint_sha256=row_fingerprint_sha256,
        ),
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
        warnings=sorted_warnings,
        fingerprints=fingerprints,
    )
    return SyncResult(
        status=(
            SyncStatus.COMPLETE if quality_state is QualityState.COMPLETE else SyncStatus.DEGRADED
        ),
        source_request_count=budget.used - request_count_start,
        datasets=(summary,),
    )


async def capture_option_chain(  # noqa: PLR0913
    handle: str,
    expiries: Sequence[date],
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    clock: Clock = _utc_now,
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Capture bounded single-expiry option-chain requests with entitlement proof."""
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("market sync requires SaxoAnalyticsProvider")
    budget = _source_request_budget(config, request_budget)
    request_count_start = budget.used
    instrument_handle = _validate_instrument_handle(handle)
    requested_expiries = _validate_expiries(expiries, config)
    _preflight_market_capacity(config, len(requested_expiries))
    selector = _instrument_selector(config, instrument_handle)
    captured_at = _require_utc_clock(clock())
    results: list[SyncResult] = []
    for expiry in requested_expiries:
        request: dict[str, object] = {
            "ExpiryDates": [expiry.isoformat()],
            "OptionRootId": selector.identifier,
        }
        capture = build_source_capture_context(
            {"options_chain_reference_v1": request},
            captured_at=captured_at,
        )
        try:
            pages = await _fetch_source_pages(
                provider,
                "options_chain_reference_v1",
                request,
                capture,
                budget,
            )
        except SourceEntitlementError as error:
            correction_state = {
                "capture_kind": "point",
                "captured_at": captured_at.isoformat(),
                "requested_expiries": [expiry.isoformat()],
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
                request_fingerprint_sha256=(
                    capture.request_fingerprints["options_chain_reference_v1"]
                ),
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
                expiries=(expiry,),
                entitlement_state="denied",
                entitlement_error_code=error.error_code,
                warnings=("option_entitlement_denied",),
                fingerprints=fingerprints,
            )
            results.append(
                SyncResult(
                    status=SyncStatus.REFUSED,
                    source_request_count=0,
                    datasets=(summary,),
                ),
            )
            continue
        envelope = build_source_capture_envelope(capture, pages)
        results.append(
            _persist_available_option_chain(
                config=config,
                envelope_pages=envelope.pages,
                instrument_handle=instrument_handle,
                expected_root_id=selector.identifier,
                requested_expiry=expiry,
                captured_at=captured_at,
            ),
        )
    return _combine_sync_results(
        results,
        source_request_count=budget.used - request_count_start,
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
    _preflight_research_request(request, config)
    budget = SourceRequestBudget(config.limits.sync_instruments)
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
                request_budget=budget,
            )
        elif isinstance(item, QuoteSyncSpec):
            result = await capture_quote(
                item.handle,
                provider=provider,
                config=config,
                clock=clock,
                max_age=item.max_age,
                request_budget=budget,
            )
        elif isinstance(item, OptionChainSyncSpec):
            result = await capture_option_chain(
                item.handle,
                item.expiries,
                provider=provider,
                config=config,
                clock=clock,
                request_budget=budget,
            )
        else:
            raise SyncValidationError("server-owned analysis input handler is required")
        results.append(result)
    return _combine_sync_results(
        results,
        source_request_count=budget.used,
    )


def _preflight_research_request(  # noqa: C901
    request: SyncResearchRequest,
    config: AnalyticsConfig,
) -> None:
    if len(request.items) > config.limits.sync_instruments:
        raise SyncLimitError("synchronous research instrument limit exceeded")
    handles = {
        item.handle
        for item in request.items
        if isinstance(item, PriceBarSyncSpec | QuoteSyncSpec | OptionChainSyncSpec)
    }
    if len(handles) > config.limits.sync_instruments:
        raise SyncLimitError("synchronous research instrument limit exceeded")
    projected_rows = 0
    projected_source_requests = 0
    projected_storage_rows = 0
    for item in request.items:
        if isinstance(item, PriceBarSyncSpec):
            _require_utc_range(item.start, item.end)
            item_rows = _row_bound(item.start, item.end, item.interval)
            projected_rows += item_rows
            projected_storage_rows += item_rows
            projected_source_requests += 1
        elif isinstance(item, QuoteSyncSpec):
            _validate_max_age(item.max_age)
            projected_storage_rows += 1
            projected_source_requests += 1
        elif isinstance(item, OptionChainSyncSpec):
            item_expiries = _validate_expiries(item.expiries, config)
            projected_storage_rows += len(item_expiries)
            projected_source_requests += len(item_expiries)
        elif isinstance(item, AnalysisInputSyncSpec):
            if len(item.source_dataset_ids) != len(set(item.source_dataset_ids)):
                raise SyncValidationError("analysis input source handles must be unique")
    if projected_rows > config.limits.sync_rows:
        raise SyncLimitError("synchronous market data row limit exceeded")
    if projected_source_requests > config.limits.sync_instruments:
        raise SyncLimitError("synchronous source request budget exceeded")
    _preflight_market_capacity(config, projected_storage_rows)


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
    connection = _connect(config, read_only=True)
    try:
        metadata, source_pages, source_revision = _authenticated_dataset_metadata(
            connection,
            validated_id,
        )
        if metadata.get("data_kind") == "price_bars":
            return _price_bar_dataset_page(
                connection,
                validated_id,
                metadata,
                source_pages,
                source_revision,
                page,
                limit,
            )
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
            return _option_dataset_page(
                connection,
                validated_id,
                page,
                limit,
                metadata,
            )
        if metadata.get("data_kind") == "quote":
            return _quote_dataset_page(
                connection,
                validated_id,
                page,
                limit,
                metadata,
                source_pages,
            )
    finally:
        connection.close()
    raise SyncValidationError("dataset kind is not supported")


def _price_bar_dataset_page(  # noqa: PLR0913
    connection: duckdb.DuckDBPyConnection,
    dataset_id: str,
    metadata: Mapping[str, object],
    source_pages: Sequence[_AuthenticatedSourcePage],
    source_revision: str,
    page: int,
    limit: int,
) -> DatasetPage:
    instrument_handle = _validate_instrument_handle(metadata.get("instrument_handle"))
    interval = ChartInterval(_required_text(metadata.get("interval"), "dataset interval"))
    visible_bar_lineage = _authenticated_chart_lineage(
        connection,
        dataset_id,
        instrument_handle,
        interval,
        metadata,
        tuple(page for page in source_pages if page.contract_name == _CHART_CONTRACT_ID),
        source_revision,
    )
    bar_times_us, page_ids = _bar_page_query_values(visible_bar_lineage)
    offset = (page - 1) * limit
    total_row = connection.execute(
        _PRICE_BAR_COUNT_SQL,
        (
            bar_times_us,
            page_ids,
            instrument_handle,
            interval.value,
        ),
    ).fetchone()
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            _PRICE_BAR_PAGE_SQL,
            (
                bar_times_us,
                page_ids,
                instrument_handle,
                interval.value,
                limit,
                offset,
            ),
        ).fetchall(),
    )
    if total_row is None or not isinstance(total_row[0], int):
        raise SyncError("dataset row count is invalid")
    total_rows = total_row[0]
    if total_rows != len(visible_bar_lineage):
        raise SyncError("stored visible price-bar lineage is invalid")
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
        dataset_id=dataset_id,
        page=page,
        limit=limit,
        total_rows=total_rows,
        rows=parsed_rows,
        next_page=page + 1 if offset + len(parsed_rows) < total_rows else None,
    )


def _quote_dataset_page(  # noqa: PLR0913
    connection: duckdb.DuckDBPyConnection,
    dataset_id: str,
    page: int,
    limit: int,
    metadata: Mapping[str, object],
    source_pages: Sequence[_AuthenticatedSourcePage],
) -> DatasetPage:
    instrument_handle = _validate_instrument_handle(metadata.get("instrument_handle"))
    freshness_value = metadata.get("freshness")
    if freshness_value not in {"fresh", "stale"}:
        raise SyncError("stored quote freshness is invalid")
    freshness = cast("Literal['fresh', 'stale']", freshness_value)
    warnings = _warning_tuple(metadata.get("warnings"))
    offset = (page - 1) * limit
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
                    q.mid_value,
                    q.page_id,
                    q.fingerprint_sha256
                FROM quotes AS q
                JOIN dataset_source_pages AS dsp ON dsp.page_id = q.page_id
                WHERE dsp.dataset_id = ? AND q.instrument_handle = ?
                ORDER BY q.captured_at, q.quote_id
                """,
            (dataset_id, instrument_handle),
        ).fetchall(),
    )
    if total_row is None or not isinstance(total_row[0], int):
        raise SyncError("dataset row count is invalid")
    total_rows = total_row[0]
    if total_rows != len(rows):
        raise SyncError("stored quote normalized-row integrity check failed")
    _validate_stored_quotes(
        rows,
        instrument_handle,
        metadata,
        source_pages,
    )
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
        for row in rows[offset : offset + limit]
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
    connection: duckdb.DuckDBPyConnection,
    dataset_id: str,
    page: int,
    limit: int,
    metadata: Mapping[str, object],
) -> DatasetPage:
    offset = (page - 1) * limit
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
                    o.put_call,
                    o.fingerprint_sha256,
                    o.payload_json
                FROM option_snapshots AS o
                JOIN dataset_source_pages AS dsp ON dsp.page_id = o.page_id
                WHERE dsp.dataset_id = ?
                ORDER BY o.expiry_date, o.strike_value, o.put_call, o.instrument_handle
                """,
            (dataset_id,),
        ).fetchall(),
    )
    if total_row is None or not isinstance(total_row[0], int):
        raise SyncError("dataset row count is invalid")
    total_rows = total_row[0]
    if total_rows != len(rows):
        raise SyncError("stored option normalized-row integrity check failed")
    _validate_stored_options(connection, rows, metadata)
    parsed_rows = tuple(_option_dataset_row(row) for row in rows[offset : offset + limit])
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


def _validate_stored_quotes(
    rows: Sequence[tuple[object, ...]],
    instrument_handle: str,
    metadata: Mapping[str, object],
    source_pages: Sequence[_AuthenticatedSourcePage],
) -> None:
    if len(rows) != 1:
        raise SyncError("stored quote normalized-row integrity check failed")
    pages_by_id = {page.page_id: page for page in source_pages}
    row = rows[0]
    if len(row) != _QUOTE_INTEGRITY_COLUMN_COUNT:
        raise SyncError("stored quote normalized-row integrity check failed")
    try:
        stored_handle = _validate_instrument_handle(row[0])
        captured_at = _epoch_us(row[1])
        bid_value = _optional_float(row[2])
        ask_value = _optional_float(row[3])
        mid_value = _optional_float(row[4])
        page_id = _required_text(row[5], "stored quote page ID")
        fingerprint_sha256 = _required_text(row[6], "stored quote fingerprint")
    except (IndexError, SyncError) as error:
        raise SyncError("stored quote normalized-row integrity check failed") from error
    source_page = pages_by_id.get(page_id)
    if source_page is None:
        raise SyncError("stored quote normalized-row integrity check failed")
    raw_rows = source_page.payload.get("rows")
    if not isinstance(raw_rows, list):
        raise SyncError("stored quote normalized-row integrity check failed")
    source_rows = cast("list[object]", raw_rows)
    if len(source_rows) != 1 or not isinstance(source_rows[0], dict):
        raise SyncError("stored quote normalized-row integrity check failed")
    freshness = metadata.get("freshness")
    delayed_by_minutes = metadata.get("delayed_by_minutes")
    entitlement_state = metadata.get("entitlement_state")
    quality_state_value = metadata.get("quality_state")
    if (
        freshness not in {"fresh", "stale"}
        or (
            delayed_by_minutes is not None
            and (type(delayed_by_minutes) is not int or delayed_by_minutes < 0)
        )
        or entitlement_state not in {"available", "delayed", "limited"}
        or not isinstance(quality_state_value, str)
    ):
        raise SyncError("stored quote normalized-row integrity check failed")
    try:
        quality_state = QualityState(quality_state_value)
        canonical = normalize_quote(
            row=cast("dict[str, object]", source_rows[0]),
            instrument_handle=instrument_handle,
            captured_at=captured_at,
            evaluated_at=(
                captured_at + timedelta(microseconds=2) if freshness == "stale" else captured_at
            ),
            max_age=timedelta(microseconds=1),
        )
    except (MarketDataError, ValueError) as error:
        raise SyncError("stored quote normalized-row integrity check failed") from error
    warnings = _warning_tuple(metadata.get("warnings"))
    validated_entitlement_state = cast("str", entitlement_state)
    canonical_row_fingerprint = _quote_row_fingerprint(
        canonical,
        quality_state=quality_state,
        entitlement_state=validated_entitlement_state,
        warnings=warnings,
    )
    if (
        stored_handle != canonical.instrument_handle
        or bid_value != canonical.bid_value
        or ask_value != canonical.ask_value
        or mid_value != canonical.mid_value
        or delayed_by_minutes != canonical.delayed_by_minutes
        or fingerprint_sha256 != canonical_row_fingerprint
    ):
        raise SyncError("stored quote normalized-row integrity check failed")
    _validate_normalized_rows_fingerprint(metadata, canonical_row_fingerprint)


def _validate_stored_options(  # noqa: C901
    connection: duckdb.DuckDBPyConnection,
    rows: Sequence[tuple[object, ...]],
    metadata: Mapping[str, object],
) -> None:
    raw_normalized_rows = metadata.get("normalized_rows")
    if not isinstance(raw_normalized_rows, list):
        raise SyncError("stored option normalized-row integrity check failed")
    try:
        metadata_options = tuple(
            NormalizedOptionReference.model_validate_json(
                _canonical_stored_json(value),
                strict=True,
            )
            for value in cast("list[object]", raw_normalized_rows)
        )
    except ValidationError as error:
        raise SyncError("stored option normalized-row integrity check failed") from error
    canonical_metadata_options: dict[int, NormalizedOptionReference] = {}
    for option in metadata_options:
        expected_fingerprint = _fingerprint(
            {
                "expiry": option.expiry.isoformat(),
                "put_call": option.put_call,
                "source_identifier": option.source_identifier,
                "strike_value": option.strike_value,
            },
        )
        if (
            option.fingerprint_sha256 != expected_fingerprint
            or option.source_identifier in canonical_metadata_options
        ):
            raise SyncError("stored option normalized-row integrity check failed")
        canonical_metadata_options[option.source_identifier] = option
    option_handles = _validated_option_handles(connection)
    identifiers_by_handle = {handle: identifier for identifier, handle in option_handles.items()}
    try:
        expected_underlying_handle = _validate_instrument_handle(
            metadata.get("instrument_handle"),
        )
        expected_captured_at = _required_utc_text(
            metadata.get("captured_at"),
            "stored option capture time",
        )
    except SyncError as error:
        raise SyncError("stored option normalized-row integrity check failed") from error
    canonical_options: list[NormalizedOptionReference] = []
    seen_identifiers: set[int] = set()
    for row in rows:
        try:
            option_handle = _validate_instrument_handle(row[0])
            underlying_handle = _validate_instrument_handle(row[1])
            captured_at = _epoch_us(row[2])
            expiry = row[3]
            strike_value = _required_float(row[4])
            currency = _required_text(row[5], "stored option currency")
            put_call_value = _required_text(row[6], "stored option put-call value")
            fingerprint_sha256 = _required_text(row[7], "stored option fingerprint")
            payload_json = _required_text(row[8], "stored option payload")
        except (IndexError, SyncError) as error:
            raise SyncError("stored option normalized-row integrity check failed") from error
        if (
            isinstance(expiry, datetime)
            or not isinstance(expiry, date)
            or put_call_value not in {"call", "put"}
        ):
            raise SyncError("stored option normalized-row integrity check failed")
        put_call = cast("Literal['call', 'put']", put_call_value)
        source_identifier = identifiers_by_handle.get(option_handle)
        metadata_option = (
            canonical_metadata_options.get(source_identifier)
            if source_identifier is not None
            else None
        )
        if (
            source_identifier is None
            or metadata_option is None
            or source_identifier in seen_identifiers
            or currency != _MISSING_CURRENCY
            or underlying_handle != expected_underlying_handle
            or captured_at != expected_captured_at
            or expiry != metadata_option.expiry
            or strike_value != metadata_option.strike_value
            or put_call != metadata_option.put_call
        ):
            raise SyncError("stored option normalized-row integrity check failed")
        expected_payload = _canonical_stored_json(
            {
                "currency": None,
                "currency_state": "unavailable",
                "option_reference_sha256": metadata_option.fingerprint_sha256,
            },
        )
        expected_row_fingerprint = _option_row_fingerprint(
            option_handle=option_handle,
            underlying_handle=underlying_handle,
            captured_at=captured_at,
            option=metadata_option,
        )
        if payload_json != expected_payload or fingerprint_sha256 != expected_row_fingerprint:
            raise SyncError("stored option normalized-row integrity check failed")
        seen_identifiers.add(source_identifier)
        canonical_options.append(metadata_option)
    if seen_identifiers != set(canonical_metadata_options):
        raise SyncError("stored option normalized-row integrity check failed")
    ordered_options = sorted(
        canonical_options,
        key=lambda option: (option.strike_value, option.put_call, option.source_identifier),
    )
    _validate_normalized_rows_fingerprint(
        metadata,
        _fingerprint([option.model_dump(mode="json") for option in ordered_options]),
    )


class _InstrumentSelector(_StrictModel):
    identifier: int = Field(ge=0)
    asset_type: str = Field(min_length=1, max_length=64)


class _ExistingBarState(_StrictModel):
    coverage_start: datetime
    coverage_end: datetime
    refresh_start: datetime


@dataclass(frozen=True, slots=True)
class _AuthenticatedSourcePage:
    page_id: str
    source_revision: str
    source_native_revision: str
    source_timestamp: datetime
    account_scope: str | None
    instrument_handle: str | None
    contract_name: str
    contract_sha256: str
    fingerprint_sha256: str
    row_count: int
    byte_count: int
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _VisibleBarReference:
    page_id: str
    capture_revision: str
    source_row: dict[str, object]


type _VisibleBarLineage = dict[datetime, _VisibleBarReference]


@dataclass(frozen=True, slots=True)
class _StoredPriceBar:
    page_id: str
    instrument_handle: str
    source_native_revision: str
    bar_time: datetime
    interval: ChartInterval
    open_value: float | None
    high_value: float | None
    low_value: float | None
    close_value: float
    volume_value: float | None


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


def _validate_max_age(max_age: timedelta) -> None:
    if type(max_age) is not timedelta or max_age <= timedelta(0):
        raise SyncValidationError("quote maximum age must be positive")


def _instrument_selector(
    config: AnalyticsConfig,
    instrument_handle: str,
) -> _InstrumentSelector:
    connection = _connect(config, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT
                instrument_handle,
                asset_type,
                safe_label,
                fingerprint_sha256,
                metadata_json
            FROM safe_instruments
            WHERE instrument_handle = ?
            """,
            (instrument_handle,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SyncValidationError("instrument handle is not resolved")
    try:
        stored_handle = _validate_instrument_handle(row[0])
        stored_asset_type = _required_text(row[1], "stored instrument asset type")
        stored_label = _required_text(row[2], "stored instrument label")
        stored_fingerprint = _required_text(row[3], "stored instrument fingerprint")
        metadata_json = _required_text(row[4], "stored instrument metadata")
        loaded_payload = json.loads(metadata_json)
    except (IndexError, SyncError, TypeError, ValueError) as error:
        raise SyncError("stored instrument selector integrity check failed") from error
    if not isinstance(loaded_payload, dict):
        raise SyncError("stored instrument selector integrity check failed")
    payload = cast("dict[str, object]", loaded_payload)
    identifier = payload.get("identifier")
    asset_type = payload.get("asset_type")
    if (
        stored_handle != instrument_handle
        or hashlib.sha256(metadata_json.encode()).hexdigest() != stored_fingerprint
        or asset_type != stored_asset_type
        or payload.get("display_label") != stored_label
    ):
        raise SyncError("stored instrument selector integrity check failed")
    try:
        return _InstrumentSelector.model_validate(
            {"identifier": identifier, "asset_type": asset_type},
            strict=True,
        )
    except ValidationError as error:
        raise SyncError("stored instrument selector integrity check failed") from error


def _canonical_stored_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise SyncError("stored source page integrity check failed") from error


def _authenticated_source_page(row: tuple[object, ...]) -> _AuthenticatedSourcePage:
    if len(row) != _SOURCE_PAGE_INTEGRITY_COLUMN_COUNT:
        raise SyncError("stored source page integrity check failed")
    if type(row[9]) is not int or type(row[10]) is not int:
        raise SyncError("stored source page integrity check failed")
    try:
        page_id = _required_text(row[0], "stored source page ID")
        source_kind = _required_text(row[1], "stored source kind")
        page_key = _required_text(row[2], "stored source page key")
        source_revision = _required_text(row[3], "stored source revision")
        source_native_revision = _required_text(row[4], "stored source native revision")
        account_scope = None if row[5] is None else _required_text(row[5], "account scope")
        instrument_handle = None if row[6] is None else _validate_instrument_handle(row[6])
        instrument_scope_sha256 = (
            None
            if row[7] is None
            else _required_text(row[7], "stored instrument scope fingerprint")
        )
        source_timestamp = _epoch_us(row[8])
        row_count = row[9]
        byte_count = row[10]
        fingerprint_sha256 = _required_text(row[11], "stored source page fingerprint")
        payload_sha256 = _required_text(row[12], "stored source payload fingerprint")
        payload_json = _required_text(row[13], "stored source payload")
        contract_id = _required_text(row[14], "stored source contract ID")
        contract_name = _required_text(row[15], "stored source contract name")
        contract_sha256 = _required_text(row[16], "stored source contract fingerprint")
        source_scope = _required_text(row[17], "stored source scope")
        logical_key_sha256 = _required_text(row[18], "stored source logical key")
        loaded_payload = json.loads(payload_json)
    except (SyncError, TypeError, ValueError) as error:
        raise SyncError("stored source page integrity check failed") from error
    if not isinstance(loaded_payload, dict):
        raise SyncError("stored source page integrity check failed")
    payload = cast("dict[str, object]", loaded_payload)
    canonical_payload = _canonical_stored_json(payload)
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise SyncError("stored source page integrity check failed")
    source_rows = cast("list[object]", raw_rows)
    contract = source_contracts_by_id().get(contract_name)
    if (
        source_scope != "saxo_openapi"
        or contract is None
        or contract.contract_id != contract_name
        or contract.source_kind != source_kind
        or source_contract_fingerprint(contract) != contract_sha256
        or contract_id
        != "sc_" + hashlib.sha256(f"{contract_name}:{contract_sha256}".encode()).hexdigest()
        or payload.get("contract_id") != contract_name
        or any(not isinstance(source_row, dict) for source_row in source_rows)
        or len(source_rows) != row_count
        or len(canonical_payload.encode()) != byte_count
        or hashlib.sha256(canonical_payload.encode()).hexdigest() != payload_sha256
        or canonical_payload != payload_json
    ):
        raise SyncError("stored source page integrity check failed")
    if (
        "source_native_revision" in payload
        and payload.get("source_native_revision") != source_native_revision
    ):
        raise SyncError("stored source page integrity check failed")
    raw_page_fingerprint = payload.get("page_fingerprint_sha256")
    if (raw_page_fingerprint is not None or contract_name == _CHART_CONTRACT_ID) and (
        not isinstance(raw_page_fingerprint, str)
        or source_page_fingerprint(
            cast("list[Mapping[str, object]]", source_rows),
        )
        != raw_page_fingerprint
    ):
        raise SyncError("stored source page integrity check failed")
    material_json = _canonical_stored_json(
        {
            "account_scope": account_scope,
            "contract_name": contract_name,
            "contract_sha256": contract_sha256,
            "instrument_handle": instrument_handle,
            "instrument_scope_sha256": instrument_scope_sha256,
            "page_key": page_key,
            "payload_sha256": payload_sha256,
            "row_count": row_count,
            "source_kind": source_kind,
            "source_revision": source_revision,
            "source_native_revision": source_native_revision,
            "source_timestamp": source_timestamp.isoformat(),
        },
    )
    logical_key_json = _canonical_stored_json(
        {
            "account_scope": account_scope,
            "instrument_handle": instrument_handle,
            "instrument_scope_sha256": instrument_scope_sha256,
            "page_key": page_key,
            "source_kind": source_kind,
            "source_revision": source_revision,
        },
    )
    reconstructed_fingerprint = hashlib.sha256(material_json.encode()).hexdigest()
    if (
        fingerprint_sha256 != reconstructed_fingerprint
        or page_id != f"sp_{reconstructed_fingerprint}"
        or logical_key_sha256 != hashlib.sha256(logical_key_json.encode()).hexdigest()
    ):
        raise SyncError("stored source page integrity check failed")
    return _AuthenticatedSourcePage(
        page_id=page_id,
        source_revision=source_revision,
        source_native_revision=source_native_revision,
        source_timestamp=source_timestamp,
        account_scope=account_scope,
        instrument_handle=instrument_handle,
        contract_name=contract_name,
        contract_sha256=contract_sha256,
        fingerprint_sha256=fingerprint_sha256,
        row_count=row_count,
        byte_count=byte_count,
        payload=payload,
    )


def _authenticated_dataset_metadata(  # noqa: C901
    connection: duckdb.DuckDBPyConnection,
    dataset_id: str,
) -> tuple[dict[str, object], tuple[_AuthenticatedSourcePage, ...], str]:
    dataset_row = connection.execute(
        """
        SELECT
            account_scope,
            source_scope,
            source_revision,
            epoch_us(coverage_start),
            epoch_us(coverage_end),
            quality_state,
            row_count,
            byte_count,
            fingerprint_sha256
        FROM datasets
        WHERE dataset_id = ?
        """,
        (dataset_id,),
    ).fetchone()
    if dataset_row is None:
        raise DatasetNotFoundError("research dataset does not exist")
    if (
        len(dataset_row) != _DATASET_INTEGRITY_COLUMN_COUNT
        or type(dataset_row[6]) is not int
        or type(dataset_row[7]) is not int
    ):
        raise SyncError("stored dataset lineage integrity check failed")
    try:
        account_scope = _required_text(dataset_row[0], "stored dataset account scope")
        source_scope = _required_text(dataset_row[1], "stored dataset source scope")
        source_revision = _required_text(dataset_row[2], "stored dataset source revision")
        coverage_start = _epoch_us(dataset_row[3])
        coverage_end = _epoch_us(dataset_row[4])
        quality_state = _required_text(dataset_row[5], "stored dataset quality state")
        row_count = dataset_row[6]
        byte_count = dataset_row[7]
        dataset_fingerprint = _required_text(
            dataset_row[8],
            "stored dataset fingerprint",
        )
    except (IndexError, SyncError) as error:
        raise SyncError("stored dataset lineage integrity check failed") from error
    raw_page_rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            """
            SELECT
                p.page_id,
                p.source_kind,
                p.page_key,
                p.source_revision,
                p.source_native_revision,
                p.account_scope,
                p.instrument_handle,
                p.instrument_scope_sha256,
                epoch_us(p.source_timestamp),
                p.row_count,
                p.byte_count,
                p.fingerprint_sha256,
                p.payload_sha256,
                p.payload_json,
                p.contract_id,
                c.contract_name,
                c.contract_sha256,
                c.source_scope,
                p.logical_key_sha256
            FROM dataset_source_pages AS dsp
            JOIN source_pages AS p ON p.page_id = dsp.page_id
            JOIN source_contracts AS c ON c.contract_id = p.contract_id
            WHERE dsp.dataset_id = ?
            ORDER BY p.page_id
            """,
            (dataset_id,),
        ).fetchall(),
    )
    if not raw_page_rows:
        raise SyncError("stored dataset lineage integrity check failed")
    try:
        source_pages = tuple(_authenticated_source_page(row) for row in raw_page_rows)
    except SyncError as error:
        raise SyncError("stored dataset lineage integrity check failed") from error
    current_pages = tuple(page for page in source_pages if page.source_revision == source_revision)
    if (
        source_scope != "saxo_openapi"
        or not current_pages
        or any(
            page.account_scope is not None and page.account_scope != account_scope
            for page in source_pages
        )
        or row_count != sum(page.row_count for page in source_pages)
        or byte_count != sum(page.byte_count for page in source_pages)
    ):
        raise SyncError("stored dataset lineage integrity check failed")
    dataset_material = _canonical_stored_json(
        {
            "account_scope": account_scope,
            "coverage_end": coverage_end.isoformat(),
            "coverage_start": coverage_start.isoformat(),
            "pages": [
                {
                    "page_id": page.page_id,
                    "page_fingerprint_sha256": page.fingerprint_sha256,
                    "source_contract_sha256": page.contract_sha256,
                }
                for page in source_pages
            ],
            "quality_state": quality_state,
            "source_revision": source_revision,
            "source_scope": source_scope,
        },
    )
    if hashlib.sha256(dataset_material.encode()).hexdigest() != dataset_fingerprint:
        raise SyncError("stored dataset lineage integrity check failed")
    try:
        metadata_values = tuple(
            _stored_sync_metadata(_canonical_stored_json(page.payload)) for page in current_pages
        )
    except SyncError as error:
        raise SyncError("stored dataset lineage integrity check failed") from error
    canonical_metadata = {_canonical_stored_json(metadata) for metadata in metadata_values}
    if len(canonical_metadata) != 1:
        raise SyncError("stored dataset lineage integrity check failed")
    metadata = metadata_values[0]
    if metadata.get("capture_revision") != source_revision:
        raise SyncError("stored dataset lineage integrity check failed")
    return metadata, source_pages, source_revision


def _authenticated_chart_lineage(  # noqa: PLR0913
    connection: duckdb.DuckDBPyConnection,
    dataset_id: str,
    instrument_handle: str,
    interval: ChartInterval,
    metadata: Mapping[str, object],
    source_pages: Sequence[_AuthenticatedSourcePage],
    source_revision: str,
) -> _VisibleBarLineage:
    if (
        metadata.get("data_kind") != "price_bars"
        or metadata.get("instrument_handle") != instrument_handle
        or metadata.get("interval") != interval.value
    ):
        raise SyncError("stored chart lineage integrity check failed")
    try:
        visible_revisions = _stored_visible_bar_revisions(
            metadata.get("visible_bar_revisions"),
        )
    except SyncError as error:
        raise SyncError("stored chart lineage integrity check failed") from error
    normalized_rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            """
            SELECT
                epoch_us(b.bar_time),
                b.page_id,
                p.source_revision,
                b.instrument_handle,
                b.source_revision,
                b.duration,
                b.open_value,
                b.high_value,
                b.low_value,
                b.close_value,
                b.volume_value,
                b.currency,
                b.adjusted
            FROM price_bars AS b
            JOIN dataset_source_pages AS dsp
                ON dsp.dataset_id = ? AND dsp.page_id = b.page_id
            JOIN source_pages AS p ON p.page_id = b.page_id
            WHERE b.instrument_handle = ? AND b.duration = ?
            ORDER BY b.bar_time, b.page_id
            """,
            (dataset_id, instrument_handle, interval.value),
        ).fetchall(),
    )
    rows_by_lineage: dict[tuple[datetime, str], list[_StoredPriceBar]] = {}
    for row in normalized_rows:
        stored_bar = _stored_price_bar(row)
        capture_revision = _required_text(row[2], "stored price-bar capture revision")
        rows_by_lineage.setdefault((stored_bar.bar_time, capture_revision), []).append(stored_bar)
    pages_by_id = {page.page_id: page for page in source_pages}
    lineage: _VisibleBarLineage = {}
    canonical_bars: list[NormalizedPriceBar] = []
    for bar_time, capture_revision in visible_revisions.items():
        stored_bars = rows_by_lineage.get((bar_time, capture_revision), [])
        if len(stored_bars) != 1:
            raise SyncError("stored chart lineage integrity check failed")
        stored_bar = stored_bars[0]
        source_page = pages_by_id.get(stored_bar.page_id)
        if source_page is None:
            raise SyncError("stored chart lineage integrity check failed")
        source_row = _authenticated_source_row(source_page, bar_time)
        canonical_bar = _canonical_price_bar(
            source_row,
            instrument_handle,
            interval,
            bar_time,
        )
        if (
            stored_bar.instrument_handle != canonical_bar.instrument_handle
            or stored_bar.source_native_revision != source_page.source_native_revision
            or stored_bar.interval is not canonical_bar.interval
            or stored_bar.open_value != canonical_bar.open_value
            or stored_bar.high_value != canonical_bar.high_value
            or stored_bar.low_value != canonical_bar.low_value
            or stored_bar.close_value != canonical_bar.close_value
            or stored_bar.volume_value != canonical_bar.volume_value
        ):
            raise SyncError("stored chart normalized-row integrity check failed")
        canonical_bars.append(canonical_bar)
        lineage[bar_time] = _VisibleBarReference(
            page_id=source_page.page_id,
            capture_revision=capture_revision,
            source_row=source_row,
        )
    current_page_ids = {
        page.page_id for page in source_pages if page.source_revision == source_revision
    }
    visible_page_ids = {reference.page_id for reference in lineage.values()}
    if set(pages_by_id) != current_page_ids | visible_page_ids:
        raise SyncError("stored chart lineage integrity check failed")
    _validate_normalized_rows_fingerprint(
        metadata,
        _fingerprint([bar.model_dump(mode="json") for bar in canonical_bars]),
    )
    return lineage


def _stored_price_bar(row: tuple[object, ...]) -> _StoredPriceBar:
    if len(row) != _PRICE_BAR_INTEGRITY_COLUMN_COUNT or row[11] is not None or row[12] is not False:
        raise SyncError("stored chart normalized-row integrity check failed")
    try:
        return _StoredPriceBar(
            page_id=_required_text(row[1], "stored price-bar page ID"),
            instrument_handle=_validate_instrument_handle(row[3]),
            source_native_revision=_required_text(
                row[4],
                "stored price-bar source revision",
            ),
            bar_time=_epoch_us(row[0]),
            interval=ChartInterval(_required_text(row[5], "stored price-bar interval")),
            open_value=_optional_float(row[6]),
            high_value=_optional_float(row[7]),
            low_value=_optional_float(row[8]),
            close_value=_required_float(row[9]),
            volume_value=_optional_float(row[10]),
        )
    except (IndexError, SyncError, ValueError) as error:
        raise SyncError("stored chart normalized-row integrity check failed") from error


def _canonical_price_bar(
    source_row: Mapping[str, object],
    instrument_handle: str,
    interval: ChartInterval,
    bar_time: datetime,
) -> NormalizedPriceBar:
    try:
        series = normalize_price_series(
            rows=(source_row,),
            instrument_handle=instrument_handle,
            interval=interval,
            start=bar_time,
            end=bar_time,
        )
    except (MarketDataError, TypeError, ValueError) as error:
        raise SyncError("stored chart normalized-row integrity check failed") from error
    if len(series.bars) != 1 or series.bars[0].bar_time != bar_time:
        raise SyncError("stored chart normalized-row integrity check failed")
    return series.bars[0]


def _authenticated_source_row(
    source_page: _AuthenticatedSourcePage,
    bar_time: datetime,
) -> dict[str, object]:
    raw_rows = source_page.payload.get("rows")
    if not isinstance(raw_rows, list):
        raise SyncError("stored chart lineage integrity check failed")
    matching_rows: list[dict[str, object]] = []
    try:
        for raw_row in cast("list[object]", raw_rows):
            if not isinstance(raw_row, dict):
                continue
            row = cast("dict[str, object]", raw_row)
            raw_time = row.get("Time")
            if isinstance(raw_time, str) and _parse_source_time(raw_time) == bar_time:
                matching_rows.append(row)
    except (SyncError, TypeError, ValueError) as error:
        raise SyncError("stored chart lineage integrity check failed") from error
    if len(matching_rows) != 1:
        raise SyncError("stored chart lineage integrity check failed")
    return dict(matching_rows[0])


def _latest_visible_bar_lineage(
    config: AnalyticsConfig,
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
) -> _VisibleBarLineage:
    connection = _connect(config, read_only=True)
    try:
        candidates = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT d.dataset_id
                FROM datasets AS d
                JOIN dataset_source_pages AS dsp ON dsp.dataset_id = d.dataset_id
                JOIN source_pages AS p ON p.page_id = dsp.page_id
                JOIN source_contracts AS c ON c.contract_id = p.contract_id
                WHERE
                    p.instrument_handle = ?
                    AND c.contract_name = ?
                    AND p.source_revision = d.source_revision
                    AND d.coverage_start <= ?
                    AND d.coverage_end >= ?
                GROUP BY d.dataset_id
                ORDER BY
                    max(p.ingested_at) DESC,
                    max(p.source_timestamp) DESC,
                    d.dataset_id DESC
                """,
                (instrument_handle, _CHART_CONTRACT_ID, end, start),
            ).fetchall(),
        )
        for row in candidates:
            dataset_id = _required_text(row[0], "stored dataset ID")
            candidate, source_pages, source_revision = _authenticated_dataset_metadata(
                connection,
                dataset_id,
            )
            if (
                candidate.get("data_kind") == "price_bars"
                and candidate.get("instrument_handle") == instrument_handle
                and candidate.get("interval") == interval.value
            ):
                return _authenticated_chart_lineage(
                    connection,
                    dataset_id,
                    instrument_handle,
                    interval,
                    candidate,
                    source_pages,
                    source_revision,
                )
    finally:
        connection.close()
    return {}


def _existing_bar_state(
    visible_bar_lineage: Mapping[datetime, object],
    start: datetime,
    end: datetime,
) -> _ExistingBarState | None:
    visible_times = sorted(bar_time for bar_time in visible_bar_lineage if start <= bar_time <= end)
    if not visible_times:
        return None
    return _ExistingBarState(
        coverage_start=visible_times[0],
        coverage_end=visible_times[-1],
        refresh_start=(visible_times[-2] if len(visible_times) > 1 else visible_times[-1]),
    )


def _visible_price_series_fingerprint(
    visible_bar_lineage: Mapping[datetime, _VisibleBarReference],
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
) -> str | None:
    if not visible_bar_lineage:
        return None
    prior = normalize_price_series(
        rows=tuple(
            reference.source_row for _bar_time, reference in sorted(visible_bar_lineage.items())
        ),
        instrument_handle=instrument_handle,
        interval=interval,
        start=start,
        end=end,
    )
    return prior.fingerprint_sha256


def _stored_sync_metadata(payload_json: object) -> dict[str, object]:
    if not isinstance(payload_json, str):
        raise SyncError("stored dataset metadata is invalid")
    try:
        loaded_payload = json.loads(payload_json)
    except (TypeError, ValueError) as error:
        raise SyncError("stored dataset metadata is invalid") from error
    if not isinstance(loaded_payload, dict):
        raise SyncError("stored dataset metadata is invalid")
    payload = cast("dict[str, object]", loaded_payload)
    sync_metadata = payload.get("sync_metadata")
    if not isinstance(sync_metadata, dict):
        raise SyncError("stored dataset metadata is invalid")
    return cast("dict[str, object]", sync_metadata)


def _stored_visible_bar_revisions(value: object) -> dict[datetime, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SyncError("stored visible price-bar lineage is invalid")
    revisions: dict[datetime, str] = {}
    for raw_time, raw_revision in cast("dict[str, object]", value).items():
        bar_time = _required_utc_text(raw_time, "stored visible price-bar time")
        if bar_time in revisions:
            raise SyncError("stored visible price-bar lineage is invalid")
        revisions[bar_time] = _required_text(
            raw_revision,
            "stored visible price-bar revision",
        )
    return revisions


def _validate_normalized_rows_fingerprint(
    metadata: Mapping[str, object],
    canonical_rows_sha256: str,
) -> None:
    raw_prior_page_ids = metadata.get("prior_page_ids", [])
    if not isinstance(raw_prior_page_ids, list):
        raise SyncError("stored normalized-row fingerprint integrity check failed")
    raw_page_ids = cast("list[object]", raw_prior_page_ids)
    if any(not isinstance(page_id, str) for page_id in raw_page_ids):
        raise SyncError("stored normalized-row fingerprint integrity check failed")
    prior_page_ids = cast("list[str]", raw_page_ids)
    if prior_page_ids != sorted(set(prior_page_ids)):
        raise SyncError("stored normalized-row fingerprint integrity check failed")
    try:
        fingerprints = IngestionFingerprints.model_validate(
            metadata.get("fingerprints"),
            strict=True,
        )
    except ValidationError as error:
        raise SyncError("stored normalized-row fingerprint integrity check failed") from error
    visible_dataset_sha256 = _fingerprint(
        {
            "normalized_rows_sha256": canonical_rows_sha256,
            "retained_page_ids": prior_page_ids,
        },
    )
    expected = _fingerprint(
        {
            "normalized_rows_sha256": canonical_rows_sha256,
            "visible_dataset_sha256": visible_dataset_sha256,
        },
    )
    if fingerprints.normalized_rows_sha256 != expected:
        raise SyncError("stored normalized-row fingerprint integrity check failed")


def _merged_price_series(  # noqa: PLR0913
    _config: AnalyticsConfig,
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
    refreshed: NormalizedPriceSeries,
    refreshed_rows: Sequence[Mapping[str, object]],
    retained_bar_lineage: Mapping[datetime, _VisibleBarReference],
) -> NormalizedPriceSeries:
    retained_rows = tuple(
        reference.source_row for _bar_time, reference in sorted(retained_bar_lineage.items())
    )
    if not retained_rows:
        return refreshed
    rows_by_time: dict[datetime, Mapping[str, object]] = {}
    for row in retained_rows:
        bar_time = _parse_source_time(
            _required_text(row.get("Time"), "stored price-bar time"),
        )
        rows_by_time[bar_time] = row
    for row in refreshed_rows:
        bar_time = _parse_source_time(
            _required_text(row.get("Time"), "refreshed price-bar time"),
        )
        rows_by_time[bar_time] = row
    return normalize_price_series(
        rows=tuple(rows_by_time[key] for key in sorted(rows_by_time)),
        instrument_handle=instrument_handle,
        interval=interval,
        start=start,
        end=end,
    )


def _price_series_coverage(
    series: NormalizedPriceSeries,
    requested_start: datetime,
    requested_end: datetime,
) -> tuple[datetime, datetime]:
    if not series.bars:
        return requested_start, requested_end
    return series.bars[0].bar_time, series.bars[-1].bar_time


def _capture_fingerprints(
    pages: Sequence[SourcePage],
    normalized_rows_sha256: str,
    correction_state: Mapping[str, object],
    *,
    retained_page_ids: Sequence[str] = (),
) -> IngestionFingerprints:
    visible_dataset_sha256 = _fingerprint(
        {
            "normalized_rows_sha256": normalized_rows_sha256,
            "retained_page_ids": sorted(set(retained_page_ids)),
        },
    )
    return IngestionFingerprints(
        raw_pages_sha256=_fingerprint(
            {
                "current_pages": [page.page_fingerprint_sha256 for page in pages],
                "visible_dataset_sha256": visible_dataset_sha256,
            },
        ),
        normalized_rows_sha256=_fingerprint(
            {
                "normalized_rows_sha256": normalized_rows_sha256,
                "visible_dataset_sha256": visible_dataset_sha256,
            },
        ),
        source_contract_sha256=_fingerprint(
            {
                "contracts": sorted({page.contract_sha256 for page in pages}),
                "visible_dataset_sha256": visible_dataset_sha256,
            },
        ),
        entitlements_sha256=_fingerprint(
            {
                "source_quality": [page.source_quality.model_dump(mode="json") for page in pages],
                "visible_dataset_sha256": visible_dataset_sha256,
            },
        ),
        correction_state_sha256=_fingerprint(
            {
                "correction_state": dict(correction_state),
                "visible_dataset_sha256": visible_dataset_sha256,
            },
        ),
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
    retained_page_ids: Sequence[str],
    normalized_series: NormalizedPriceSeries,
    coverage: tuple[datetime, datetime],
    visible_bar_revisions: Mapping[datetime, str],
    invalidate_prior_analyses: bool,
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
        "refresh_start": correction_state.get("refresh_start"),
        "requested_end": end.isoformat(),
        "requested_start": start.isoformat(),
        "return_series_label": "price_return",
        "visible_bar_revisions": {
            bar_time.isoformat(): revision
            for bar_time, revision in sorted(visible_bar_revisions.items())
        },
    }

    def invalidate_dependent_analyses(connection: duckdb.DuckDBPyConnection) -> int:
        return _invalidate_chart_dependent_analyses(
            connection,
            instrument_handle=instrument_handle,
            interval=interval,
            start=start,
            end=end,
        )

    return _persist_market_capture(
        config=config,
        pages=pages,
        instrument_handle=instrument_handle,
        coverage_start=coverage[0],
        coverage_end=coverage[1],
        quality_state=quality_state,
        sync_metadata=sync_metadata,
        normalized_bytes=(len(normalized_series.bars) * _NORMALIZED_ROW_ESTIMATE_BYTES),
        lineage_page_ids=retained_page_ids,
        persist_normalized=lambda connection, stored_page_ids: _persist_normalized_bars(
            connection=connection,
            pages=pages,
            stored_page_ids=stored_page_ids,
            series=normalized_series,
        ),
        invalidate_dependent_analyses=(
            invalidate_dependent_analyses if invalidate_prior_analyses else None
        ),
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
    normalized_bytes: int,
    lineage_page_ids: Sequence[str] = (),
    persist_normalized: Callable[
        [duckdb.DuckDBPyConnection, Mapping[int, str]],
        None,
    ],
    invalidate_dependent_analyses: Callable[[duckdb.DuckDBPyConnection], int] | None = None,
) -> tuple[dict[int, str], str]:
    if not pages:
        raise SyncError("market capture contains no source page")
    store = AnalyticsStore.open(config)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    stored_page_ids: dict[int, str] = {}
    try:
        try:
            with store.market_ingestion_transaction(normalized_bytes) as connection:
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
                persist_normalized(connection, stored_page_ids)
                if invalidate_dependent_analyses is not None:
                    invalidate_dependent_analyses(connection)
                store.create_dataset(
                    dataset_id=dataset_id,
                    account_scope=pages[0].account_scope,
                    source_scope="saxo_openapi",
                    source_revision=pages[0].capture_revision,
                    source_page_ids=tuple(stored_page_ids.values()),
                    lineage_source_page_ids=lineage_page_ids,
                    created_at=pages[0].source_timestamp,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    quality_state=quality_state,
                )
        except StoreQuotaError as error:
            raise SyncLimitError("analytics store quota refuses market ingestion") from error
    finally:
        store.close()
    return stored_page_ids, dataset_id


def _invalidate_chart_dependent_analyses(
    connection: duckdb.DuckDBPyConnection,
    *,
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
) -> int:
    dependent = tuple(source_contracts_by_id()[_CHART_CONTRACT_ID].dependent_analysis_kinds)
    if not dependent:
        return 0
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            """
            SELECT DISTINCT a.analysis_id, a.dataset_id
            FROM analyses AS a
            JOIN datasets AS d ON d.dataset_id = a.dataset_id
            JOIN dataset_source_pages AS dsp ON dsp.dataset_id = d.dataset_id
            JOIN source_pages AS p ON p.page_id = dsp.page_id
            JOIN source_contracts AS c ON c.contract_id = p.contract_id
            WHERE a.analysis_kind = ANY(?)
              AND a.status != 'invalidated'
              AND c.contract_name = ?
              AND p.instrument_handle = ?
              AND d.coverage_start <= ?
              AND d.coverage_end >= ?
            """,
            (list(dependent), _CHART_CONTRACT_ID, instrument_handle, end, start),
        ).fetchall(),
    )
    analysis_ids: list[str] = []
    validated_datasets: dict[str, bool] = {}
    for raw_analysis_id, raw_dataset_id in rows:
        analysis_id = _required_text(raw_analysis_id, "stored analysis ID")
        dataset_id = _required_text(raw_dataset_id, "stored analysis dataset ID")
        matches_scope = validated_datasets.get(dataset_id)
        if matches_scope is None:
            metadata, _source_pages, _source_revision = _authenticated_dataset_metadata(
                connection,
                dataset_id,
            )
            matches_scope = (
                metadata.get("data_kind") == "price_bars"
                and metadata.get("instrument_handle") == instrument_handle
                and metadata.get("interval") == interval.value
            )
            validated_datasets[dataset_id] = matches_scope
        if matches_scope:
            analysis_ids.append(analysis_id)
    unique_analysis_ids = sorted(set(analysis_ids))
    if unique_analysis_ids:
        connection.execute(
            "UPDATE analyses SET status = 'invalidated' WHERE analysis_id = ANY(?)",
            (unique_analysis_ids,),
        )
    return len(unique_analysis_ids)


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


def _persist_available_option_chain(  # noqa: PLR0913
    *,
    config: AnalyticsConfig,
    envelope_pages: Sequence[SourcePage],
    instrument_handle: str,
    expected_root_id: int,
    requested_expiry: date,
    captured_at: datetime,
) -> SyncResult:
    rows = tuple(row for page in envelope_pages for row in page.rows)
    if len(rows) != 1:
        raise SyncError("option-chain capture did not return exactly one source row")
    chain = normalize_option_chain(
        row=rows[0],
        expected_root_id=expected_root_id,
        expiry=requested_expiry,
    )
    correction_state = {
        "capture_kind": "point",
        "captured_at": captured_at.isoformat(),
        "requested_expiries": [requested_expiry.isoformat()],
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
        "expiries": [requested_expiry.isoformat()],
        "fingerprints": fingerprints.model_dump(mode="json"),
        "instrument_handle": instrument_handle,
        "normalized_rows": [option.model_dump(mode="json") for option in chain.options],
        "warnings": list(sorted_warnings),
    }
    _stored_pages, dataset_id = _persist_market_capture(
        config=config,
        pages=envelope_pages,
        instrument_handle=instrument_handle,
        coverage_start=captured_at,
        coverage_end=captured_at,
        quality_state=quality_state,
        sync_metadata=sync_metadata,
        normalized_bytes=(len(chain.options) * _NORMALIZED_ROW_ESTIMATE_BYTES),
        persist_normalized=lambda connection, stored_page_ids: _persist_normalized_options(
            connection=connection,
            pages=envelope_pages,
            stored_page_ids=stored_page_ids,
            underlying_handle=instrument_handle,
            chain=chain,
        ),
    )
    summary = OptionChainDatasetSummary(
        dataset_id=dataset_id,
        data_kind="option_chain",
        instrument_handle=instrument_handle,
        quality_state=quality_state,
        coverage_start=captured_at,
        coverage_end=captured_at,
        row_count=len(chain.options),
        expiries=(requested_expiry,),
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
    connection: duckdb.DuckDBPyConnection,
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


def _quote_row_fingerprint(
    quote: NormalizedQuote,
    *,
    quality_state: QualityState,
    entitlement_state: str,
    warnings: Sequence[str],
) -> str:
    return _fingerprint(
        {
            "ask_value": quote.ask_value,
            "bid_value": quote.bid_value,
            "captured_at": quote.captured_at.isoformat(),
            "delayed_by_minutes": quote.delayed_by_minutes,
            "entitlement_state": entitlement_state,
            "freshness": quote.freshness,
            "instrument_handle": quote.instrument_handle,
            "mid_value": quote.mid_value,
            "price_type": quote.price_type,
            "quality_state": quality_state.value,
            "warnings": list(warnings),
        },
    )


def _persist_normalized_quote(
    *,
    connection: duckdb.DuckDBPyConnection,
    pages: Sequence[SourcePage],
    stored_page_ids: Mapping[int, str],
    quote: NormalizedQuote,
    row_fingerprint_sha256: str,
) -> None:
    if len(pages) != 1:
        raise SyncError("quote capture contains an invalid source-page count")
    source_page = pages[0]
    quote_id = "quote:" + _fingerprint(
        {
            "capture_revision": source_page.capture_revision,
            "instrument_handle": quote.instrument_handle,
            "quote_fingerprint": row_fingerprint_sha256,
        },
    )
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
            row_fingerprint_sha256,
        ),
    )
    connection.execute(
        "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
    )


def _option_row_fingerprint(
    *,
    option_handle: str,
    underlying_handle: str,
    captured_at: datetime,
    option: NormalizedOptionReference,
) -> str:
    return _fingerprint(
        {
            "captured_at": captured_at.isoformat(),
            "currency": option.currency,
            "expiry": option.expiry.isoformat(),
            "instrument_handle": option_handle,
            "put_call": option.put_call,
            "row_kind": "option_reference",
            "source_identifier": option.source_identifier,
            "strike_value": option.strike_value,
            "underlying_handle": underlying_handle,
        },
    )


def _persist_normalized_options(
    *,
    connection: duckdb.DuckDBPyConnection,
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
    option_handles = _option_handles(
        connection,
        chain.options,
        source_page,
    )
    for option in chain.options:
        option_handle = option_handles[option.source_identifier]
        row_fingerprint_sha256 = _option_row_fingerprint(
            option_handle=option_handle,
            underlying_handle=underlying_handle,
            captured_at=source_page.source_timestamp,
            option=option,
        )
        snapshot_id = "option:" + _fingerprint(
            {
                "capture_revision": source_page.capture_revision,
                "option_fingerprint": row_fingerprint_sha256,
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
                row_fingerprint_sha256,
                payload,
            ),
        )
    connection.execute(
        "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
    )


def _validated_option_handles(
    connection: duckdb.DuckDBPyConnection,
) -> dict[int, str]:
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            """
            SELECT
                s.instrument_handle,
                s.asset_type,
                s.safe_label,
                s.fingerprint_sha256,
                s.metadata_json
            FROM safe_instruments AS s
            WHERE s.asset_type = 'ContractOption'
                OR EXISTS (
                    SELECT 1
                    FROM option_snapshots AS o
                    WHERE o.instrument_handle = s.instrument_handle
                )
            """,
        ).fetchall(),
    )
    handles: dict[int, str] = {}
    for row in rows:
        try:
            handle = _validate_instrument_handle(row[0])
            asset_type = _required_text(row[1], "stored option asset type")
            safe_label = _required_text(row[2], "stored option label")
            fingerprint_sha256 = _required_text(row[3], "stored option fingerprint")
            metadata_json = _required_text(row[4], "stored option metadata")
            loaded_metadata = json.loads(metadata_json)
        except (IndexError, SyncError, TypeError, ValueError) as error:
            raise SyncError("stored option instrument integrity check failed") from error
        if not isinstance(loaded_metadata, dict):
            raise SyncError("stored option instrument integrity check failed")
        metadata = cast("dict[str, object]", loaded_metadata)
        identifier = metadata.get("identifier")
        if (
            asset_type != "ContractOption"
            or safe_label != _OPTION_SAFE_LABEL
            or hashlib.sha256(metadata_json.encode()).hexdigest() != fingerprint_sha256
            or metadata.get("asset_type") != asset_type
            or metadata.get("display_label") != safe_label
            or type(identifier) is not int
            or identifier < 0
            or identifier in handles
        ):
            raise SyncError("stored option instrument integrity check failed")
        handles[identifier] = handle
    return handles


def _option_handles(
    connection: duckdb.DuckDBPyConnection,
    options: Sequence[NormalizedOptionReference],
    source_page: SourcePage,
) -> dict[int, str]:
    handles = _validated_option_handles(connection)
    for option in options:
        if option.source_identifier in handles:
            continue
        option_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
        metadata: dict[str, object] = {
            "aliases": list[str](),
            "asset_type": "ContractOption",
            "display_label": _OPTION_SAFE_LABEL,
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
                _OPTION_SAFE_LABEL,
                source_page.source_revision,
                source_page.source_timestamp,
                hashlib.sha256(metadata_json.encode()).hexdigest(),
                metadata_json,
            ),
        )
        handles[option.source_identifier] = option_handle
    return handles


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
    if interval is ChartInterval.ONE_DAY:
        return (end.date() - start.date()).days + 1
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


def _epoch_us(value: object) -> datetime:
    if type(value) is not int:
        raise SyncError("stored dataset timestamp is invalid")
    return datetime.fromtimestamp(value / 1_000_000, UTC)


def _datetime_epoch_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SyncError("visible price-bar timestamp is invalid")
    elapsed = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _bar_page_query_values(
    visible_bar_lineage: Mapping[datetime, _VisibleBarReference],
) -> tuple[list[int], list[str]]:
    entries = sorted(visible_bar_lineage.items())
    return (
        [_datetime_epoch_us(bar_time) for bar_time, _reference in entries],
        [reference.page_id for _bar_time, reference in entries],
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _required_float(value)


def _required_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SyncError("stored dataset number is invalid")
    return float(value)


_VISIBLE_PRICE_BAR_SQL: Final = """
    WITH lineage AS (
        SELECT
            unnest(?::BIGINT[]) AS bar_time_us,
            unnest(?::VARCHAR[]) AS page_id
    ),
    visible AS (
        SELECT
            b.instrument_handle,
            lineage.bar_time_us,
            b.open_value,
            b.high_value,
            b.low_value,
            b.close_value,
            b.volume_value,
            b.page_id
        FROM lineage
        JOIN price_bars AS b ON
            epoch_us(b.bar_time) = lineage.bar_time_us
            AND b.page_id = lineage.page_id
        WHERE
            b.instrument_handle = ?
            AND b.duration = ?
    )
"""
_PRICE_BAR_COUNT_SQL: Final = (
    _VISIBLE_PRICE_BAR_SQL  # noqa: S608
    + "SELECT count(*) FROM visible"
)
_PRICE_BAR_PAGE_SQL: Final = (
    _VISIBLE_PRICE_BAR_SQL
    + """
        SELECT
            instrument_handle,
            bar_time_us,
            open_value,
            high_value,
            low_value,
            close_value,
            volume_value
        FROM visible
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
