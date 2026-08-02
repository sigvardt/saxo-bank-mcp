"""Local-only analytics storage listing and explicit deletion controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    DeletionPreview,
    DeletionReceipt,
    StorageEntry,
    StorageScope,
)
from saxo_bank_mcp.request_ledger import RequestLedgerEvent, capture_scoped_request_ledger

type StorageOperation = Literal["list_storage", "preview_deletion", "delete_analytics_data"]


class StorageBoundaryError(RuntimeError):
    """Raised if a local-only storage operation emits a request-ledger event."""


@dataclass(frozen=True, slots=True)
class LocalStorageEvidence:
    """Value-free evidence that one operation remained below the broker boundary."""

    operation: StorageOperation
    local_only: Literal[True]
    request_event_count: Literal[0]
    saxo_network_calls: Literal[0]
    broker_write_calls: Literal[0]


@dataclass(frozen=True, slots=True)
class StorageListing:
    """Owner-only safe handles, counts, timestamps, and fingerprints."""

    visibility: Literal["owner_only"]
    entries: tuple[StorageEntry, ...]
    evidence: LocalStorageEvidence


@dataclass(frozen=True, slots=True)
class DeletionPreviewResult:
    """Owner-only exact preview carrying the store-issued short-lived token."""

    visibility: Literal["owner_only"]
    preview: DeletionPreview
    evidence: LocalStorageEvidence


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """Value-free local audit receipt with no deletion token or deleted values."""

    visibility: Literal["owner_only"]
    receipt: DeletionReceipt
    evidence: LocalStorageEvidence


@dataclass(frozen=True, slots=True)
class _LocalResult[T]:
    value: T
    evidence: LocalStorageEvidence


def list_storage(
    scope: StorageScope,
    *,
    store: AnalyticsStore,
) -> StorageListing:
    """List retained local data through the existing typed store operation."""
    local = _run_local_only("list_storage", lambda: store.list_storage(scope))
    return StorageListing(
        visibility="owner_only",
        entries=local.value,
        evidence=local.evidence,
    )


def preview_deletion(
    scope: StorageScope,
    *,
    store: AnalyticsStore,
) -> DeletionPreviewResult:
    """Preview the store's exact dependency closure without deleting data."""
    local = _run_local_only("preview_deletion", lambda: store.preview_delete(scope))
    return DeletionPreviewResult(
        visibility="owner_only",
        preview=local.value,
        evidence=local.evidence,
    )


def delete_analytics_data(
    token: str,
    *,
    store: AnalyticsStore,
) -> DeletionResult:
    """Consume one store-issued revision-bound token for a local deletion."""
    local = _run_local_only(
        "delete_analytics_data",
        lambda: store.delete_previewed(token),
    )
    return DeletionResult(
        visibility="owner_only",
        receipt=local.value,
        evidence=local.evidence,
    )


def _run_local_only[T](
    operation: StorageOperation,
    action: Callable[[], T],
) -> _LocalResult[T]:
    with capture_scoped_request_ledger() as ledger:
        try:
            value = action()
        except BaseException:
            _require_no_requests(ledger.events)
            raise
    _require_no_requests(ledger.events)
    return _LocalResult(
        value=value,
        evidence=LocalStorageEvidence(
            operation=operation,
            local_only=True,
            request_event_count=0,
            saxo_network_calls=0,
            broker_write_calls=0,
        ),
    )


def _require_no_requests(events: list[RequestLedgerEvent]) -> None:
    if events:
        raise StorageBoundaryError(
            "local analytics storage operation crossed the request boundary",
        )


__all__ = [
    "DeletionPreviewResult",
    "DeletionResult",
    "LocalStorageEvidence",
    "StorageBoundaryError",
    "StorageListing",
    "delete_analytics_data",
    "list_storage",
    "preview_deletion",
]
