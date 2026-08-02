# pyright: reportPrivateUsage=false
# ruff: noqa: PLR2004, SLF001

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

import saxo_bank_mcp.analytics_store as store_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_models import HandleKind, new_safe_handle
from saxo_bank_mcp.analytics_storage_tools import (
    StorageBoundaryError,
    delete_analytics_data,
    list_storage,
    preview_deletion,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    DeletionTokenError,
    StorageDataType,
    StorageScope,
    StoreBusyError,
    StoreValidationError,
    TableCount,
)
from saxo_bank_mcp.request_ledger import capture_request_ledger, record_request_attempt

_NOW = datetime(2026, 8, 2, 11, tzinfo=UTC)


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


def _seed_dependency_chain(store: AnalyticsStore) -> tuple[str, str, str]:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    artifact_id = new_safe_handle(HandleKind.ARTIFACT_ID)
    job_id = new_safe_handle(HandleKind.JOB_ID)
    with store._write_connection() as connection:
        connection.execute(
            """
            INSERT INTO datasets VALUES (?, 'aggregate', 'saxo_openapi', 'rev-synthetic', ?, ?, ?,
                'complete', 1, 32, ?)
            """,
            (dataset_id, _NOW, _NOW, _NOW, "a" * 64),
        )
        connection.execute(
            """
            INSERT INTO analyses VALUES (?, ?, 'aggregate', 'synthetic_analysis', 'verified',
                'rev-synthetic', ?, ?, 64, ?, '{}')
            """,
            (analysis_id, dataset_id, _NOW, _NOW, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO artifacts VALUES (?, ?, 'image/png', 128, ?, ?, 'Synthetic artifact',
                'private_user_result')
            """,
            (artifact_id, analysis_id, "c" * 64, _NOW),
        )
        connection.execute(
            """
            INSERT INTO jobs VALUES (?, 'completed', ?, ?, ?, ?, '{}', '{}')
            """,
            (job_id, "d" * 64, _NOW, _NOW, analysis_id),
        )
        store._bump_revision(connection)
    return analysis_id, artifact_id, job_id


def _insert_unrelated_job(store: AnalyticsStore) -> None:
    with store._write_connection() as connection:
        connection.execute(
            """
            INSERT INTO jobs VALUES (?, 'queued', ?, ?, ?, NULL, '{}', '{}')
            """,
            (new_safe_handle(HandleKind.JOB_ID), "e" * 64, _NOW, _NOW),
        )
        store._bump_revision(connection)


def test_storage_listing_uses_normalized_store_scope_and_safe_local_evidence(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        analysis_id, artifact_id, job_id = _seed_dependency_chain(store)
        result = list_storage(
            StorageScope(
                data_types=(
                    StorageDataType.JOBS,
                    StorageDataType.ARTIFACTS,
                    StorageDataType.ANALYSES,
                    StorageDataType.JOBS,
                ),
            ),
            store=store,
        )

        assert {entry.object_id for entry in result.entries} == {
            analysis_id,
            artifact_id,
            job_id,
        }
        assert result.visibility == "owner_only"
        assert result.evidence.request_event_count == 0
        assert result.evidence.saxo_network_calls == 0
        assert result.evidence.broker_write_calls == 0
        assert all("path" not in repr(entry).lower() for entry in result.entries)
    finally:
        store.close()


def test_deletion_preview_reports_exact_dependency_closure_rows_and_bytes(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        analysis_id, artifact_id, job_id = _seed_dependency_chain(store)

        result = preview_deletion(
            StorageScope(analysis_ids=(analysis_id, analysis_id)),
            store=store,
        )

        assert result.preview.table_counts == (
            TableCount(table="analyses", rows=1),
            TableCount(table="artifacts", rows=1),
            TableCount(table="jobs", rows=1),
        )
        assert result.preview.estimated_bytes == 192
        assert {entry.object_id for entry in store.list_storage(StorageScope())} >= {
            analysis_id,
            artifact_id,
            job_id,
        }
        assert result.evidence.request_event_count == 0
    finally:
        store.close()


def test_deletion_token_is_single_use_and_receipt_is_value_free(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        preview = preview_deletion(
            StorageScope(analysis_ids=(analysis_id,)),
            store=store,
        )

        result = delete_analytics_data(preview.preview.token, store=store)

        assert result.receipt.table_counts == preview.preview.table_counts
        assert result.receipt.estimated_bytes == preview.preview.estimated_bytes
        assert result.evidence.saxo_network_calls == 0
        assert result.evidence.broker_write_calls == 0
        dumped = repr(result).lower()
        assert "token=" not in dumped
        assert "payload" not in dumped
        assert "money" not in dumped
        with pytest.raises(DeletionTokenError, match="used"):
            delete_analytics_data(preview.preview.token, store=store)
    finally:
        store.close()


def test_expired_deletion_token_refuses_without_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _NOW
    monkeypatch.setattr(store_module, "_utc_now", lambda: now)
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        preview = preview_deletion(
            StorageScope(analysis_ids=(analysis_id,)),
            store=store,
        )
        now += timedelta(minutes=6)

        with pytest.raises(DeletionTokenError, match="expired"):
            delete_analytics_data(preview.preview.token, store=store)

        assert store.list_storage(StorageScope(analysis_ids=(analysis_id,)))
    finally:
        store.close()


def test_revision_change_invalidates_deletion_token(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        preview = preview_deletion(
            StorageScope(analysis_ids=(analysis_id,)),
            store=store,
        )
        _insert_unrelated_job(store)

        with pytest.raises(DeletionTokenError, match="revision"):
            delete_analytics_data(preview.preview.token, store=store)
    finally:
        store.close()


def test_deletion_token_race_yields_one_local_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = AnalyticsStore.open(config)
    second = AnalyticsStore.open(config)
    try:
        analysis_id, _, _ = _seed_dependency_chain(first)
        preview = preview_deletion(
            StorageScope(analysis_ids=(analysis_id,)),
            store=first,
        )

        def delete(store: AnalyticsStore) -> object:
            try:
                return delete_analytics_data(preview.preview.token, store=store)
            except (DeletionTokenError, StoreBusyError) as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(delete, (first, second)))

        assert sum(hasattr(result, "receipt") for result in results) == 1
        assert sum(isinstance(result, Exception) for result in results) == 1
        receipts = list_storage(
            StorageScope(data_types=(StorageDataType.DELETION_RECEIPTS,)),
            store=first,
        )
        assert len(receipts.entries) == 1
    finally:
        first.close()
        second.close()


def test_storage_tools_make_zero_network_calls_and_zero_broker_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(httpx2.Client, "send", forbidden_network)
    monkeypatch.setattr(httpx2.AsyncClient, "send", forbidden_network)
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        with capture_request_ledger() as ledger:
            listing = list_storage(StorageScope(), store=store)
            preview = preview_deletion(
                StorageScope(analysis_ids=(analysis_id,)),
                store=store,
            )
            deletion = delete_analytics_data(preview.preview.token, store=store)

        assert ledger.events == []
        for evidence in (listing.evidence, preview.evidence, deletion.evidence):
            assert evidence.request_event_count == 0
            assert evidence.saxo_network_calls == 0
            assert evidence.broker_write_calls == 0
            assert evidence.local_only is True
    finally:
        store.close()


def test_local_boundary_failure_cannot_be_hidden_by_an_operation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsafe_listing(_store: AnalyticsStore, _scope: StorageScope) -> object:
        record_request_attempt(
            httpx2.Request("GET", "https://gateway.saxobank.com/openapi/port/v1/accounts"),
        )
        raise RuntimeError("synthetic operation failure")

    store = AnalyticsStore.open(_config(tmp_path))
    monkeypatch.setattr(AnalyticsStore, "list_storage", unsafe_listing)
    try:
        with pytest.raises(StorageBoundaryError, match="request boundary"):
            list_storage(StorageScope(), store=store)
    finally:
        store.close()


def test_deletion_requires_an_explicit_normalized_scope(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        _seed_dependency_chain(store)
        with pytest.raises(StoreValidationError, match="explicit scope"):
            preview_deletion(StorageScope(), store=store)
    finally:
        store.close()
