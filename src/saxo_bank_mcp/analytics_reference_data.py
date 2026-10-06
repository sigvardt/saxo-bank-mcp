"""Bounded instrument details captured through registered Saxo GET operations."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import HandleKind, QualityState, new_safe_handle
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider, SourceRequestBudget
from saxo_bank_mcp.analytics_source_contracts import build_source_capture_context
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_sync import (
    IngestionFingerprints,
    InstrumentDetailsDatasetSummary,
    SyncError,
    SyncResult,
    SyncStatus,
    _fetch_source_pages,
    _instrument_selector,
)


async def capture_instrument_details(
    instrument_handle: str,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Resolve the private selector from an existing authenticated instrument handle."""
    selector = _instrument_selector(config, instrument_handle)
    contract_id = "reference_instrument_details_v1"
    request: dict[str, object] = {
        "AssetTypes": selector.asset_type,
        "Uics": selector.identifier,
    }
    captured_at = datetime.now(UTC)
    capture = build_source_capture_context({contract_id: request}, captured_at=captured_at)
    budget = request_budget or SourceRequestBudget(config.limits.sync_instruments)
    before = budget.used
    pages = await _fetch_source_pages(provider, contract_id, request, capture, budget)
    rows = tuple(row for page in pages for row in page.rows)
    serialized_rows = [row for page in pages for row in page.model_dump(mode="json")["rows"]]
    if (
        len(rows) != 1
        or rows[0].get("Uic") != selector.identifier
        or rows[0].get("AssetType") != selector.asset_type
    ):
        raise SyncError("instrument details do not match the requested exact identity")
    limited = any(page.source_quality.state == "limited" for page in pages)
    store = AnalyticsStore.open(config)
    try:
        with store.transaction():
            page_ids = tuple(
                store.put_source_page(
                    source_kind=page.source_kind,
                    page_key=f"{page.contract_id}:{page.page_number}:{capture.capture_revision}:{instrument_handle}",
                    source_revision=page.capture_revision,
                    source_native_revision=page.source_revision,
                    contract_name=page.contract_id,
                    contract_sha256=page.contract_sha256,
                    payload={
                        "contract_id": page.contract_id,
                        "rows": page.model_dump(mode="json")["rows"],
                        "source_quality": page.source_quality.model_dump(mode="json"),
                        "request_fingerprint_sha256": page.request_fingerprint_sha256,
                        "source_native_revision": page.source_revision,
                    },
                    row_count=page.row_count,
                    source_timestamp=page.source_timestamp,
                    account_scope="aggregate",
                    instrument_handle=instrument_handle,
                    instrument_scope_sha256=page.instrument_scope_sha256,
                ).page_id
                for page in pages
            )
            dataset = store.create_dataset(
                dataset_id=new_safe_handle(HandleKind.DATASET_ID),
                account_scope="aggregate",
                source_scope="saxo_openapi",
                source_revision=capture.capture_revision,
                source_page_ids=page_ids,
                created_at=captured_at,
                coverage_start=captured_at,
                coverage_end=captured_at,
                quality_state=QualityState.PARTIAL if limited else QualityState.COMPLETE,
            )
    finally:
        store.close()

    def fingerprint(values: object) -> str:
        return hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    return SyncResult(
        status=SyncStatus.DEGRADED if limited else SyncStatus.COMPLETE,
        source_request_count=budget.used - before,
        datasets=(
            InstrumentDetailsDatasetSummary(
                dataset_id=dataset.dataset_id,
                instrument_handle=instrument_handle,
                quality_state=dataset.quality_state,
                coverage_start=captured_at,
                coverage_end=captured_at,
                row_count=dataset.row_count,
                warnings=("source_quality_limited",) if limited else (),
                fingerprints=IngestionFingerprints(
                    raw_pages_sha256=fingerprint([page.page_fingerprint_sha256 for page in pages]),
                    normalized_rows_sha256=fingerprint(serialized_rows),
                    source_contract_sha256=pages[0].contract_sha256,
                    entitlements_sha256=fingerprint(
                        [page.source_quality.model_dump(mode="json") for page in pages]
                    ),
                    correction_state_sha256=fingerprint(capture.capture_revision),
                ),
            ),
        ),
    )
