# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import multiprocessing
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, Protocol, cast

import duckdb
import pytest

import saxo_bank_mcp.analytics_migrations as migrations
import saxo_bank_mcp.analytics_store as store_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    migrate_store,
)
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisProvenance,
    AnalysisResult,
    AnalysisStatus,
    ArtifactSummary,
    DataCoverage,
    DataQuality,
    HandleKind,
    MarketAnalysisRequest,
    MetricClass,
    MetricValue,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    ValueUnitClass,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    DeletionReceipt,
    DeletionTokenError,
    StorageDataType,
    StorageScope,
    StoreBusyError,
    StoreConflictError,
    StoreQuotaError,
)

_OWNER_FILE_MODE = 0o600
_SCHEMA_SHA256 = "a" * 64
_CODE_COMMIT = "b" * 40
_PROOF_PROFILE = "vp_store-proof"
_SOURCE_AT = datetime(2026, 7, 30, 8, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures" / "analytics" / "store_v0.duckdb"
_REQUIRED_TABLES = {
    "account_snapshots",
    "analyses",
    "artifacts",
    "bookings",
    "closed_positions",
    "costs",
    "datasets",
    "deletion_receipts",
    "jobs",
    "metrics",
    "option_snapshots",
    "price_bars",
    "proof_receipts",
    "quotes",
    "safe_instruments",
    "source_contracts",
    "source_pages",
    "transactions",
    "universes",
}


class _ProcessEvent(Protocol):
    def set(self) -> None:
        """Set the shared process event."""
        ...

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the shared process event."""
        ...


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


def _source_page(
    store: AnalyticsStore,
    *,
    page_key: str = "bars-page-1",
    source_revision: str = "rev-1",
    payload: dict[str, object] | None = None,
) -> store_module.StoredSourcePage:
    return store.put_source_page(
        source_kind="price_bars",
        page_key=page_key,
        source_revision=source_revision,
        contract_name="price_bars_page",
        contract_sha256=_SCHEMA_SHA256,
        payload=payload or {"row_count": 1, "schema": "redacted_bar_page"},
        row_count=1,
        source_timestamp=_SOURCE_AT,
        account_scope="aggregate",
        instrument_handle=None,
    )


def _dataset(
    store: AnalyticsStore,
    page_id: str,
    *,
    dataset_id: str | None = None,
) -> store_module.StoredDataset:
    return store.create_dataset(
        dataset_id=dataset_id or new_safe_handle(HandleKind.DATASET_ID),
        account_scope="aggregate",
        source_scope="saxo_openapi",
        source_revision="rev-1",
        source_page_ids=(page_id,),
        created_at=_SOURCE_AT + timedelta(minutes=1),
        coverage_start=_SOURCE_AT,
        coverage_end=_SOURCE_AT,
        quality_state=QualityState.COMPLETE,
    )


def _analysis_result(dataset_id: str, analysis_id: str) -> AnalysisResult:
    coverage = DataCoverage(
        state=QualityState.COMPLETE,
        start_at=_SOURCE_AT,
        end_at=_SOURCE_AT,
        row_count=1,
        expected_row_count=1,
        missing_row_count=0,
    )
    source_binding = ProofSourceBinding(
        source_scope="saxo_openapi",
        source_revision="rev-1",
        source_contract_sha256=_SCHEMA_SHA256,
    )
    engine_binding = ProofEngineBinding(
        engine_name="store_test_engine",
        engine_version="1.0",
        code_commit=_CODE_COMMIT,
    )
    receipt = ActiveProofReceipt(
        state="active",
        analysis_kind="row_count",
        schema_version="1",
        source_binding=source_binding,
        engine_binding=engine_binding,
        proof_profile_id=_PROOF_PROFILE,
        checks_passed=(
            "golden",
            "property",
            "independent_reference",
            "saxo_reconciliation",
            "sim_end_to_end",
        ),
    )
    return AnalysisResult(
        status=AnalysisStatus.VERIFIED,
        tool_name="saxo_analyze_market",
        analysis_id=analysis_id,
        analysis_kind="row_count",
        request=MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="row_count",
            dataset_id=dataset_id,
        ),
        account_scope="aggregate",
        as_of=_SOURCE_AT + timedelta(minutes=2),
        valid_until=_SOURCE_AT + timedelta(minutes=7),
        metrics=(
            MetricValue(
                metric_id="observed_rows",
                value=1.0,
                unit="count",
                unit_class=ValueUnitClass.COUNT,
                currency=None,
                metric_class=MetricClass.CALCULATED_VERIFIED,
                source_timestamp=_SOURCE_AT,
                proof_profile_id=_PROOF_PROFILE,
            ),
        ),
        warnings=(),
        data_quality=DataQuality(
            state=QualityState.COMPLETE,
            coverage=coverage,
            checked_at=_SOURCE_AT + timedelta(minutes=1),
            warnings=(),
        ),
        provenance=AnalysisProvenance(
            dataset_id=dataset_id,
            source_scope="saxo_openapi",
            source_revision="rev-1",
            source_contract_sha256=_SCHEMA_SHA256,
            source_timestamp=_SOURCE_AT,
            proof_receipts=(receipt,),
            engine_name="store_test_engine",
            engine_version="1.0",
            code_commit=_CODE_COMMIT,
        ),
        assumptions=(),
        is_not_advice=True,
        is_not_forecast=True,
        model_distribution_only=False,
        verifies=("row count",),
        does_not_verify=(),
        replayable=True,
        next_actions=(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )


def _artifact(analysis_id: str, artifact_id: str) -> ArtifactSummary:
    return ArtifactSummary(
        artifact_id=artifact_id,
        analysis_id=analysis_id,
        media_type="image/png",
        byte_count=128,
        sha256="c" * 64,
        created_at=_SOURCE_AT + timedelta(minutes=3),
        description="Redacted deterministic chart",
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )


def _seed_dependency_chain(
    store: AnalyticsStore,
) -> tuple[
    store_module.StoredSourcePage,
    store_module.StoredDataset,
    AnalysisResult,
    ArtifactSummary,
]:
    page = _source_page(store)
    dataset = _dataset(store, page.page_id)
    analysis = _analysis_result(
        dataset.dataset_id,
        new_safe_handle(HandleKind.ANALYSIS_ID),
    )
    artifact = _artifact(
        analysis.analysis_id,
        new_safe_handle(HandleKind.ARTIFACT_ID),
    )
    store.put_analysis(analysis)
    store.put_artifact(artifact)
    return page, dataset, analysis, artifact


def _all_local_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _hold_writer(
    config_json: str,
    ready: _ProcessEvent,
    release: _ProcessEvent,
) -> None:
    config = AnalyticsConfig.model_validate_json(config_json)
    store = AnalyticsStore.open(config)
    try:
        with store.transaction():
            ready.set()
            release.wait(10)
    finally:
        store.close()


def _abort_transaction(store: AnalyticsStore) -> None:
    with store.transaction():
        _source_page(store)
        raise RuntimeError("abort")


def test_open_creates_the_normalized_schema_and_owner_only_database(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    store.close()

    with duckdb.connect(str(config.paths.store_path), read_only=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'",
            ).fetchall()
        }
        version = connection.execute(
            "SELECT version FROM analytics_schema WHERE singleton = TRUE",
        ).fetchone()

    assert names >= _REQUIRED_TABLES
    assert version == (LATEST_SCHEMA_VERSION,)
    assert _mode(config.paths.store_path) == _OWNER_FILE_MODE


def test_v0_fixture_contains_only_schema_metadata_and_no_private_rows() -> None:
    with duckdb.connect(str(_FIXTURE), read_only=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'",
            ).fetchall()
        }
        rows = connection.execute("SELECT * FROM analytics_schema").fetchall()

    assert names == {"analytics_schema"}
    assert rows == [(True, 0)]


def test_migration_from_v0_creates_an_owner_only_backup(tmp_path: Path) -> None:
    target = tmp_path / "store.duckdb"
    shutil.copyfile(_FIXTURE, target)

    result = migrate_store(target, LATEST_SCHEMA_VERSION)
    backups = tuple(tmp_path.glob("store.before-v1.*.duckdb"))

    assert result.from_version == 0
    assert result.to_version == LATEST_SCHEMA_VERSION
    assert result.backup_created
    assert len(backups) == 1
    assert _mode(target) == _OWNER_FILE_MODE
    assert _mode(backups[0]) == _OWNER_FILE_MODE
    with duckdb.connect(str(backups[0]), read_only=True) as connection:
        assert connection.execute("SELECT version FROM analytics_schema").fetchone() == (0,)


def test_failed_migration_keeps_v0_byte_identical_and_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "store.duckdb"
    shutil.copyfile(_FIXTURE, target)
    before = _sha256(target)
    original_reader = migrations._read_migration  # noqa: SLF001

    def broken_migration(version: int) -> str:
        sql = original_reader(version)
        return f"{sql}\nCREATE TABLE migration_must_rollback(value INTEGER);\nSELECT * FROM absent;"

    monkeypatch.setattr(migrations, "_read_migration", broken_migration)

    with pytest.raises(MigrationError, match="migration 1 failed"):
        migrate_store(target, LATEST_SCHEMA_VERSION)

    assert _sha256(target) == before
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute("SELECT version FROM analytics_schema").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'migration_must_rollback'",
            ).fetchone()
            == (0,)
        )


def test_migration_rejects_unknown_target_without_changing_fixture(tmp_path: Path) -> None:
    target = tmp_path / "store.duckdb"
    shutil.copyfile(_FIXTURE, target)
    before = _sha256(target)

    with pytest.raises(MigrationError, match="target schema version"):
        migrate_store(target, LATEST_SCHEMA_VERSION + 1)

    assert _sha256(target) == before


def test_transaction_rolls_back_every_domain_write(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        with pytest.raises(RuntimeError, match="abort"):
            _abort_transaction(store)

        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        assert entries == ()
    finally:
        store.close()


def test_process_lock_allows_only_one_writer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_writer,
        args=(config.model_dump_json(), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(StoreBusyError, match="writer"):
            _source_page(store)
    finally:
        release.set()
        process.join(10)
        store.close()

    assert process.exitcode == 0


def test_migration_cannot_bypass_the_store_writer_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        with store.transaction(), pytest.raises(MigrationError, match="writer lock"):
            migrate_store(config.paths.store_path, LATEST_SCHEMA_VERSION)
    finally:
        store.close()


def test_reads_use_a_connection_separate_from_the_uncommitted_writer(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        with store.transaction():
            _source_page(store)
            assert (
                store.list_storage(
                    StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
                )
                == ()
            )

        assert len(
            store.list_storage(
                StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
            ),
        ) == 1
    finally:
        store.close()


def test_page_and_dataset_fingerprints_are_deterministic(tmp_path: Path) -> None:
    first = AnalyticsStore.open(_config(tmp_path / "first"))
    second = AnalyticsStore.open(_config(tmp_path / "second"))
    try:
        first_page = _source_page(
            first,
            payload={"schema": "redacted_bar_page", "row_count": 1},
        )
        second_page = _source_page(
            second,
            payload={"row_count": 1, "schema": "redacted_bar_page"},
        )
        first_dataset = _dataset(first, first_page.page_id)
        second_dataset = _dataset(second, second_page.page_id)

        assert first_page.page_id == second_page.page_id
        assert first_page.fingerprint_sha256 == second_page.fingerprint_sha256
        assert first_dataset.fingerprint_sha256 == second_dataset.fingerprint_sha256
    finally:
        first.close()
        second.close()


def test_duplicate_page_retry_is_idempotent_without_losing_revisions(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        original = _source_page(store)
        retried = _source_page(store)
        revised = _source_page(store, source_revision="rev-2")

        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        assert retried == original
        assert revised.page_id != original.page_id
        assert {entry.source_revision for entry in entries} == {"rev-1", "rev-2"}
    finally:
        store.close()


def test_snapshot_retry_is_idempotent_and_conflicting_reuse_is_rejected(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(store)
        dataset = _dataset(store, page.page_id)
        snapshot_id = new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID)
        original = store.create_snapshot(
            snapshot_id=snapshot_id,
            dataset_id=dataset.dataset_id,
            snapshot_kind="portfolio",
            account_scope="aggregate",
            source_revision="rev-1",
            as_of=_SOURCE_AT,
            payload={"row_count": 1, "schema": "redacted_snapshot"},
        )
        retried = store.create_snapshot(
            snapshot_id=snapshot_id,
            dataset_id=dataset.dataset_id,
            snapshot_kind="portfolio",
            account_scope="aggregate",
            source_revision="rev-1",
            as_of=_SOURCE_AT,
            payload={"schema": "redacted_snapshot", "row_count": 1},
        )

        assert retried == original
        with pytest.raises(StoreConflictError, match="snapshot"):
            store.create_snapshot(
                snapshot_id=snapshot_id,
                dataset_id=dataset.dataset_id,
                snapshot_kind="portfolio",
                account_scope="aggregate",
                source_revision="rev-2",
                as_of=_SOURCE_AT,
                payload={"schema": "redacted_snapshot", "row_count": 2},
            )
    finally:
        store.close()


def test_quota_refusal_preserves_all_existing_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        existing = _source_page(store)
        sparse = config.paths.artifacts_dir / "reserved.bin"
        sparse.touch(mode=_OWNER_FILE_MODE)
        current_bytes = _all_local_bytes(config.paths.analytics_root)
        with sparse.open("r+b") as reserved:
            reserved.truncate(
                config.limits.store_quota_bytes - current_bytes - 1,
            )
        sparse.chmod(_OWNER_FILE_MODE)

        with pytest.raises(StoreQuotaError, match="quota"):
            _source_page(store, page_key="bars-page-2")

        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        assert tuple(entry.object_id for entry in entries) == (existing.page_id,)
        assert sparse.exists()
    finally:
        store.close()


def test_storage_listing_contains_only_safe_metadata(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        _source_page(store)

        entry = store.list_storage(StorageScope())[0]
        dumped = repr(entry)

        assert entry.account_scope == "aggregate"
        assert entry.data_type is StorageDataType.SOURCE_PAGES
        assert "payload" not in dumped
        assert "path" not in dumped
    finally:
        store.close()


def test_delete_preview_is_exact_and_does_not_delete(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page, dataset, analysis, artifact = _seed_dependency_chain(store)

        preview = store.preview_delete(
            StorageScope(
                account_scope="aggregate",
                data_types=(StorageDataType.SOURCE_PAGES,),
            ),
        )

        assert preview.table_counts == (
            store_module.TableCount("analyses", 1),
            store_module.TableCount("artifacts", 1),
            store_module.TableCount("dataset_source_pages", 1),
            store_module.TableCount("datasets", 1),
            store_module.TableCount("metrics", 1),
            store_module.TableCount("proof_receipts", 1),
            store_module.TableCount("source_pages", 1),
        )
        assert preview.estimated_bytes > 0
        assert {
            entry.object_id
            for entry in store.list_storage(StorageScope())
        } >= {
            page.page_id,
            dataset.dataset_id,
            analysis.analysis_id,
            artifact.artifact_id,
        }
    finally:
        store.close()


def test_scope_normalization_is_stable(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        _seed_dependency_chain(store)
        first = store.preview_delete(
            StorageScope(
                data_types=(
                    StorageDataType.ARTIFACTS,
                    StorageDataType.SOURCE_PAGES,
                    StorageDataType.ARTIFACTS,
                ),
                account_scope=" aggregate ",
            ),
        )
        second = store.preview_delete(
            StorageScope(
                account_scope="aggregate",
                data_types=(
                    StorageDataType.SOURCE_PAGES,
                    StorageDataType.ARTIFACTS,
                ),
            ),
        )

        assert first.scope_fingerprint == second.scope_fingerprint
        assert first.table_counts == second.table_counts
    finally:
        store.close()


def test_delete_token_is_single_use_and_receipt_is_value_free(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        _seed_dependency_chain(store)
        preview = store.preview_delete(
            StorageScope(
                account_scope="aggregate",
                data_types=(StorageDataType.SOURCE_PAGES,),
            ),
        )

        receipt = store.delete_previewed(preview.token)

        assert isinstance(receipt, DeletionReceipt)
        assert receipt.table_counts == preview.table_counts
        assert receipt.scope_fingerprint == preview.scope_fingerprint
        assert not hasattr(receipt, "token")
        assert not hasattr(receipt, "values")
        assert store.list_storage(StorageScope()) == ()
        with pytest.raises(DeletionTokenError, match="used"):
            store.delete_previewed(preview.token)
    finally:
        store.close()


def test_delete_token_expires_without_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _SOURCE_AT
    monkeypatch.setattr(store_module, "_utc_now", lambda: now)
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(store)
        preview = store.preview_delete(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        now += timedelta(minutes=6)

        with pytest.raises(DeletionTokenError, match="expired"):
            store.delete_previewed(preview.token)

        assert tuple(
            entry.object_id
            for entry in store.list_storage(
                StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
            )
        ) == (page.page_id,)
    finally:
        store.close()


def test_store_revision_change_invalidates_delete_token(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        original = _source_page(store)
        preview = store.preview_delete(
            StorageScope(
                data_types=(StorageDataType.SOURCE_PAGES,),
                account_scope="aggregate",
            ),
        )
        _source_page(store, page_key="bars-page-2")

        with pytest.raises(DeletionTokenError, match="revision"):
            store.delete_previewed(preview.token)

        assert original.page_id in {
            entry.object_id
            for entry in store.list_storage(
                StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
            )
        }
    finally:
        store.close()


def test_delete_token_race_produces_one_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = AnalyticsStore.open(config)
    second = AnalyticsStore.open(config)
    try:
        _source_page(first)
        preview = first.preview_delete(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        barrier = Barrier(2)

        def delete(store: AnalyticsStore) -> DeletionReceipt | Exception:
            barrier.wait()
            try:
                return store.delete_previewed(preview.token)
            except (DeletionTokenError, StoreBusyError) as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(delete, (first, second)))

        assert sum(isinstance(result, DeletionReceipt) for result in results) == 1
        assert sum(isinstance(result, Exception) for result in results) == 1
        receipt_entries = first.list_storage(
            StorageScope(data_types=(StorageDataType.DELETION_RECEIPTS,)),
        )
        assert len(receipt_entries) == 1
    finally:
        first.close()
        second.close()


def test_hardened_connections_block_extensions_network_and_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    store.close()
    connection = store_module._connect_store_database(  # noqa: SLF001
        config.paths.store_path,
        read_only=False,
    )
    forbidden_output = tmp_path / "forbidden.csv"
    try:
        with pytest.raises(duckdb.Error):
            connection.execute("INSTALL httpfs")
        with pytest.raises(duckdb.Error):
            connection.execute(
                "SELECT * FROM read_csv_auto('https://analytics.invalid/source.csv')",
            )
        connection.execute("CREATE TEMP TABLE export_guard(value INTEGER)")
        with pytest.raises(duckdb.Error):
            connection.execute(
                f"COPY export_guard TO '{forbidden_output.as_posix()}' (FORMAT CSV)",
            )
    finally:
        connection.close()

    assert not forbidden_output.exists()


def test_public_store_api_rejects_caller_sql_and_file_paths(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    target = tmp_path / "caller-selected.bin"
    unsafe_store = cast("Any", store)
    try:
        with pytest.raises(TypeError):
            unsafe_store.list_storage(StorageScope(), sql="SELECT 1")
        with pytest.raises(TypeError):
            unsafe_store.put_artifact(
                _artifact(
                    new_safe_handle(HandleKind.ANALYSIS_ID),
                    new_safe_handle(HandleKind.ARTIFACT_ID),
                ),
                file_path=target,
            )
    finally:
        store.close()

    assert not target.exists()
