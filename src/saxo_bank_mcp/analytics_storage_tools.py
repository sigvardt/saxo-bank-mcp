"""Local-only analytics storage listing and explicit deletion controls."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    DeletionPreview,
    DeletionReceipt,
    StorageDataType,
    StorageEntry,
    StorageScope,
)
from saxo_bank_mcp.request_ledger import (
    RequestLedgerEvent,
    capture_scoped_request_ledger_delta,
)

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
    runtime_state: LocalAnalyticsRuntimeState
    evidence: LocalStorageEvidence


@dataclass(frozen=True, slots=True)
class LocalAnalyticsRuntimeState:
    """Value-free counts for jobs, persistent caches, and temporary owner state."""

    job_count: int
    cache_entry_count: int
    temporary_entry_count: int
    fingerprint_sha256: str


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
    analytics_root: Path | None = None,
) -> StorageListing:
    """List retained local data through the existing typed store operation."""
    local = _run_local_only("list_storage", lambda: store.list_storage(scope))
    return StorageListing(
        visibility="owner_only",
        entries=local.value,
        runtime_state=_runtime_state(local.value, analytics_root=analytics_root),
        evidence=local.evidence,
    )


def _runtime_state(
    entries: tuple[StorageEntry, ...],
    *,
    analytics_root: Path | None,
) -> LocalAnalyticsRuntimeState:
    job_count = sum(entry.data_type is StorageDataType.JOBS for entry in entries)
    cache_types = {
        StorageDataType.SAFE_INSTRUMENTS,
        StorageDataType.SOURCE_PAGES,
        StorageDataType.DATASETS,
        StorageDataType.ACCOUNT_SNAPSHOTS,
        StorageDataType.ANALYSES,
        StorageDataType.ARTIFACTS,
    }
    cache_entries = tuple(entry for entry in entries if entry.data_type in cache_types)
    temporary_entry_count = 0 if analytics_root is None else _temporary_entry_count(analytics_root)
    material = {
        "jobs": sorted(
            entry.fingerprint_sha256 for entry in entries if entry.data_type is StorageDataType.JOBS
        ),
        "caches": sorted(entry.fingerprint_sha256 for entry in cache_entries),
        "temporary_entry_count": temporary_entry_count,
    }
    fingerprint = hashlib.sha256(
        json.dumps(material, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()
    return LocalAnalyticsRuntimeState(
        job_count=job_count,
        cache_entry_count=len(cache_entries),
        temporary_entry_count=temporary_entry_count,
        fingerprint_sha256=fingerprint,
    )


def _temporary_entry_count(analytics_root: Path) -> int:
    if not analytics_root.is_absolute() or analytics_root.is_symlink():
        raise StorageBoundaryError("analytics runtime root is not owner-contained")
    try:
        root = analytics_root.resolve(strict=True)
    except OSError as error:
        raise StorageBoundaryError("analytics runtime root is unavailable") from error
    count = 0
    for directory in (root / "job-workspaces", root / "artifacts"):
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise StorageBoundaryError("analytics temporary directory is unsafe")
        try:
            with os.scandir(directory) as children:
                for child in children:
                    if child.is_symlink():
                        raise StorageBoundaryError("analytics temporary entry is unsafe")
                    if directory.name == "job-workspaces" or child.name.startswith("."):
                        count += 1
        except OSError as error:
            raise StorageBoundaryError("analytics temporary state is unavailable") from error
    return count


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
    with capture_scoped_request_ledger_delta() as ledger:
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
