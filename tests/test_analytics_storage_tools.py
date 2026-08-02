# pyright: reportPrivateUsage=false
# ruff: noqa: PLR2004, SLF001

from __future__ import annotations

import stat
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
    StoreError,
    StoreQuotaError,
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


def test_resource_artifacts_use_random_registered_handles_and_exact_file_deletion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        analysis_id, legacy_artifact_id, _ = _seed_dependency_chain(store)
        content = b"synthetic owner artifact"
        first = store.put_owned_artifact(
            analysis_id=analysis_id,
            media_type="text/plain",
            extension="txt",
            content=content,
            description="Synthetic owner artifact",
        )
        second = store.put_owned_artifact(
            analysis_id=analysis_id,
            media_type="text/plain",
            extension="txt",
            content=content,
            description="Synthetic owner artifact",
        )

        assert first.artifact_id != second.artifact_id
        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.ARTIFACTS,)),
        )
        assert {first.artifact_id, second.artifact_id} <= {entry.object_id for entry in entries}
        files_before = tuple(config.paths.artifacts_dir.iterdir())
        assert len(files_before) == 2
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files_before)

        preview = preview_deletion(
            StorageScope(artifact_ids=(first.artifact_id,)),
            store=store,
        )
        delete_analytics_data(preview.preview.token, store=store)

        files_after = tuple(config.paths.artifacts_dir.iterdir())
        assert len(files_after) == 1
        assert second.artifact_id in files_after[0].name
        remaining = store.list_storage(
            StorageScope(data_types=(StorageDataType.ARTIFACTS,)),
        )
        assert {entry.object_id for entry in remaining} == {
            legacy_artifact_id,
            second.artifact_id,
        }
    finally:
        store.close()


def test_resource_artifact_refuses_when_store_quota_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        content = b"bounded synthetic artifact"

        def refuse_capacity(
            _store: AnalyticsStore,
            _config: AnalyticsConfig,
            _incoming_bytes: int,
        ) -> None:
            raise StoreQuotaError("synthetic quota refusal")

        monkeypatch.setattr(AnalyticsStore, "ensure_owner_capacity", refuse_capacity)
        with pytest.raises(StoreQuotaError, match="quota refusal"):
            store.put_owned_artifact(
                analysis_id=analysis_id,
                media_type="text/plain",
                extension="txt",
                content=content,
                description="Synthetic bounded artifact",
            )
        assert tuple(config.paths.artifacts_dir.iterdir()) == ()
    finally:
        store.close()


def test_owned_artifact_write_rolls_back_physical_and_metadata_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        analysis_id, legacy_artifact_id, _ = _seed_dependency_chain(store)

        def write_then_rollback() -> None:
            with store.transaction():
                store.put_owned_artifact(
                    analysis_id=analysis_id,
                    media_type="text/plain",
                    extension="txt",
                    content=b"synthetic rollback artifact",
                    description="Synthetic rollback artifact",
                )
                raise RuntimeError("synthetic rollback")

        with pytest.raises(RuntimeError, match="synthetic rollback"):
            write_then_rollback()

        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.ARTIFACTS,)),
        )
        assert {entry.object_id for entry in entries} == {legacy_artifact_id}
        assert tuple(config.paths.artifacts_dir.iterdir()) == ()
    finally:
        store.close()


def test_owned_artifact_delete_restores_file_when_outer_transaction_rolls_back(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        owned = store.put_owned_artifact(
            analysis_id=analysis_id,
            media_type="text/plain",
            extension="txt",
            content=b"synthetic retained artifact",
            description="Synthetic retained artifact",
        )
        preview = preview_deletion(
            StorageScope(artifact_ids=(owned.artifact_id,)),
            store=store,
        )

        def delete_then_rollback() -> None:
            with store.transaction():
                delete_analytics_data(preview.preview.token, store=store)
                raise RuntimeError("synthetic rollback")

        with pytest.raises(RuntimeError, match="synthetic rollback"):
            delete_then_rollback()

        entries = store.list_storage(StorageScope(artifact_ids=(owned.artifact_id,)))
        assert tuple(entry.object_id for entry in entries) == (owned.artifact_id,)
        files = tuple(config.paths.artifacts_dir.iterdir())
        assert len(files) == 1
        assert owned.artifact_id in files[0].name
    finally:
        store.close()


def test_startup_finishes_owned_artifact_publish_interrupted_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    analysis_id, legacy_artifact_id, _ = _seed_dependency_chain(store)
    publish = getattr(store_module, "_publish_staged_artifact")  # noqa: B009

    def interrupt_publish(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(store_module, "_publish_staged_artifact", interrupt_publish)
    try:
        with pytest.raises(KeyboardInterrupt):
            store.put_owned_artifact(
                analysis_id=analysis_id,
                media_type="text/plain",
                extension="txt",
                content=b"synthetic committed artifact",
                description="Synthetic committed artifact",
            )
    finally:
        store.close()
    monkeypatch.setattr(store_module, "_publish_staged_artifact", publish)

    recovered = AnalyticsStore.open(config)
    try:
        entries = recovered.list_storage(
            StorageScope(data_types=(StorageDataType.ARTIFACTS,)),
        )
        assert len(entries) == 2
        assert legacy_artifact_id in {entry.object_id for entry in entries}
        files = tuple(config.paths.artifacts_dir.iterdir())
        assert len(files) == 1
        assert not files[0].name.startswith(".")
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    finally:
        recovered.close()


def test_startup_finishes_owned_artifact_delete_interrupted_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    analysis_id, legacy_artifact_id, _ = _seed_dependency_chain(store)
    owned = store.put_owned_artifact(
        analysis_id=analysis_id,
        media_type="text/plain",
        extension="txt",
        content=b"synthetic deleted artifact",
        description="Synthetic deleted artifact",
    )
    preview = preview_deletion(
        StorageScope(artifact_ids=(owned.artifact_id,)),
        store=store,
    )

    def interrupt_cleanup(_staged: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(store_module, "_remove_staged_artifact_files", interrupt_cleanup)
    try:
        with pytest.raises(KeyboardInterrupt):
            delete_analytics_data(preview.preview.token, store=store)
    finally:
        store.close()
    monkeypatch.undo()

    recovered = AnalyticsStore.open(config)
    try:
        entries = recovered.list_storage(
            StorageScope(data_types=(StorageDataType.ARTIFACTS,)),
        )
        assert {entry.object_id for entry in entries} == {legacy_artifact_id}
        assert tuple(config.paths.artifacts_dir.iterdir()) == ()
    finally:
        recovered.close()


def test_startup_reconciles_owned_artifact_precommit_interruption_markers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    analysis_id, _, _ = _seed_dependency_chain(store)
    owned = store.put_owned_artifact(
        analysis_id=analysis_id,
        media_type="text/plain",
        extension="txt",
        content=b"synthetic precommit retained artifact",
        description="Synthetic precommit retained artifact",
    )
    canonical = next(
        path for path in config.paths.artifacts_dir.iterdir() if owned.artifact_id in path.name
    )
    delete_pending = canonical.with_name(
        f".artifact-delete-{owned.artifact_id}-{'2' * 32}.txt.pending",
    )
    canonical.replace(delete_pending)
    orphan_id = new_safe_handle(HandleKind.ARTIFACT_ID)
    write_pending = config.paths.artifacts_dir / (
        f".artifact-write-{orphan_id}-{'1' * 32}.txt.pending"
    )
    write_pending.write_bytes(b"synthetic uncommitted artifact")
    write_pending.chmod(0o600)
    store.close()

    recovered = AnalyticsStore.open(config)
    try:
        files = tuple(config.paths.artifacts_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == canonical.name
        assert files[0].read_bytes() == b"synthetic precommit retained artifact"
        assert tuple(
            entry.object_id
            for entry in recovered.list_storage(StorageScope(artifact_ids=(owned.artifact_id,)))
        ) == (owned.artifact_id,)
    finally:
        recovered.close()


def test_startup_quarantines_unreferenced_owner_artifact_without_deleting_it(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initialized = AnalyticsStore.open(config)
    initialized.close()
    artifact_id = new_safe_handle(HandleKind.ARTIFACT_ID)
    orphan = config.paths.artifacts_dir / f"{artifact_id}.txt"
    content = b"synthetic owner data without committed metadata"
    orphan.write_bytes(content)
    orphan.chmod(0o600)

    recovered = AnalyticsStore.open(config)
    try:
        retained = tuple(config.paths.artifacts_dir.iterdir())
        assert len(retained) == 1
        assert retained[0].name.startswith(f".artifact-orphan-{artifact_id}-")
        assert retained[0].read_bytes() == content
        assert stat.S_IMODE(retained[0].stat().st_mode) == 0o600
    finally:
        recovered.close()


def test_owned_artifact_publication_and_deletion_fsync_the_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    fsync_directory = getattr(store_module, "_fsync_artifact_directory")  # noqa: B009
    calls = 0

    def record_fsync(path: Path) -> None:
        nonlocal calls
        calls += 1
        fsync_directory(path)

    monkeypatch.setattr(store_module, "_fsync_artifact_directory", record_fsync)
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        owned = store.put_owned_artifact(
            analysis_id=analysis_id,
            media_type="text/plain",
            extension="txt",
            content=b"synthetic durable artifact",
            description="Synthetic durable artifact",
        )
        preview = preview_deletion(
            StorageScope(artifact_ids=(owned.artifact_id,)),
            store=store,
        )
        delete_analytics_data(preview.preview.token, store=store)
        assert calls >= 4
    finally:
        store.close()


def test_postcommit_publish_actions_drain_then_require_safe_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    publish = store_module._publish_staged_artifact
    attempted: list[str] = []

    def fail_first_publish(
        destination: Path,
        staged: Path,
        expected_sha256: str,
    ) -> None:
        attempted.append(destination.name)
        if len(attempted) == 1:
            raise OSError("synthetic postcommit publish interruption")
        publish(destination, staged, expected_sha256)

    monkeypatch.setattr(store_module, "_publish_staged_artifact", fail_first_publish)
    owned_ids: list[str] = []

    def publish_two() -> None:
        with store.transaction():
            for ordinal in range(2):
                owned = store.put_owned_artifact(
                    analysis_id=analysis_id,
                    media_type="text/plain",
                    extension="txt",
                    content=f"synthetic committed artifact {ordinal}".encode(),
                    description="Synthetic committed artifact",
                )
                owned_ids.append(owned.artifact_id)

    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        with pytest.raises(OSError, match="publish interruption"):
            publish_two()

        assert len(attempted) == 2
        with pytest.raises(StoreError, match="reopen"):
            store.list_storage(StorageScope())
    finally:
        store.close()

    monkeypatch.setattr(store_module, "_publish_staged_artifact", publish)
    recovered = AnalyticsStore.open(config)
    try:
        files = tuple(config.paths.artifacts_dir.iterdir())
        assert len(files) == 2
        assert all(any(artifact_id in path.name for path in files) for artifact_id in owned_ids)
        assert all(not path.name.startswith(".") for path in files)
    finally:
        recovered.close()


def test_postcommit_delete_actions_drain_then_require_safe_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    original_unlink = Path.unlink
    attempted: list[str] = []
    try:
        analysis_id, _, _ = _seed_dependency_chain(store)
        owned = tuple(
            store.put_owned_artifact(
                analysis_id=analysis_id,
                media_type="text/plain",
                extension="txt",
                content=f"synthetic deleted artifact {ordinal}".encode(),
                description="Synthetic deleted artifact",
            )
            for ordinal in range(2)
        )
        preview = preview_deletion(
            StorageScope(artifact_ids=tuple(item.artifact_id for item in owned)),
            store=store,
        )

        def fail_first_pending_unlink(path: Path, *, missing_ok: bool = False) -> None:
            if path.name.startswith(".artifact-delete-"):
                attempted.append(path.name)
                if len(attempted) == 1:
                    raise OSError("synthetic postcommit deletion interruption")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_first_pending_unlink)
        with (
            pytest.raises(StoreError, match="physical deletion cleanup failed"),
            store.transaction(),
        ):
            delete_analytics_data(preview.preview.token, store=store)

        assert len(attempted) == 2
        with pytest.raises(StoreError, match="reopen"):
            store.list_storage(StorageScope())
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)
        store.close()

    recovered = AnalyticsStore.open(config)
    try:
        assert tuple(config.paths.artifacts_dir.iterdir()) == ()
    finally:
        recovered.close()


def test_startup_refuses_swapped_artifact_directory_without_touching_sibling(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initialized = AnalyticsStore.open(config)
    initialized.close()
    sibling = config.paths.analytics_root / "synthetic-sibling"
    sibling.mkdir(mode=0o700)
    artifact_id = new_safe_handle(HandleKind.ARTIFACT_ID)
    external = sibling / f"{artifact_id}.txt"
    content = b"synthetic sibling owner content"
    external.write_bytes(content)
    external.chmod(0o600)
    config.paths.artifacts_dir.rmdir()
    config.paths.artifacts_dir.symlink_to(sibling, target_is_directory=True)

    opened: AnalyticsStore | None = None
    try:
        with pytest.raises(StoreValidationError, match="artifact directory"):
            opened = AnalyticsStore.open(config)
        assert external.exists()
        assert external.read_bytes() == content
        assert stat.S_IMODE(external.stat().st_mode) == 0o600
        assert tuple(sibling.iterdir()) == (external,)
    finally:
        if opened is not None:
            opened.close()


def test_startup_refuses_hardlinked_artifact_without_mutating_external_inode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initialized = AnalyticsStore.open(config)
    initialized.close()
    external = config.paths.analytics_root / "synthetic-external-owner-file"
    content = b"synthetic multiply linked owner content"
    external.write_bytes(content)
    external.chmod(0o640)
    original_mode = stat.S_IMODE(external.stat().st_mode)
    artifact_id = new_safe_handle(HandleKind.ARTIFACT_ID)
    linked = config.paths.artifacts_dir / f"{artifact_id}.txt"
    linked.hardlink_to(external)

    opened: AnalyticsStore | None = None
    try:
        with pytest.raises(StoreError, match="artifact"):
            opened = AnalyticsStore.open(config)
        assert external.exists()
        assert external.read_bytes() == content
        assert stat.S_IMODE(external.stat().st_mode) == original_mode
        assert external.stat().st_nlink == 2
        assert linked.exists()
    finally:
        if opened is not None:
            opened.close()


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
