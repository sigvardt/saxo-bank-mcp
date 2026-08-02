# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, Protocol, cast

import duckdb
import pytest

import saxo_bank_mcp.analytics_migrations as migrations
import saxo_bank_mcp.analytics_store as store_module
from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    AnalyticsPaths,
    load_analytics_config,
    prepare_owner_only_path,
)
from saxo_bank_mcp.analytics_migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    migrate_store,
)
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisCalendar,
    AnalysisParameterBinding,
    AnalysisProvenance,
    AnalysisResult,
    AnalysisStatus,
    ArtifactSummary,
    DataCoverage,
    DataQuality,
    FxConversionMethod,
    FxSource,
    HandleKind,
    InstrumentAnalysisRequest,
    MarketAnalysisRequest,
    MetricClass,
    MetricValue,
    PortfolioAnalysisRequest,
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
    StoreNotFoundError,
    StoreQuotaError,
    StoreValidationError,
)

_OWNER_FILE_MODE = 0o600
_SCHEMA_SHA256 = "a" * 64
_SECOND_SCHEMA_SHA256 = "b" * 64
_EXTRA_SCHEMA_SHA256 = "c" * 64
_MULTI_CONTRACT_SHA256 = "ec085bd7249a33c0dc2f8cde734ae66755acb51a5a186398b116a5873962f762"
_CODE_COMMIT = "b" * 40
_PROOF_PROFILE = "vp_store-proof"
_SOURCE_AT = datetime(2026, 7, 30, 8, tzinfo=UTC)
_PROJECT_ROOT = Path(__file__).parents[1]
_FIXTURE = Path(__file__).parent / "fixtures" / "analytics" / "store_v0.duckdb"
_REQUIRED_TABLES = {
    "account_scope_bindings",
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


def _analysis_parameters(*, as_of: datetime) -> AnalysisParameterBinding:
    return AnalysisParameterBinding(
        start_at=_SOURCE_AT,
        end_at=_SOURCE_AT,
        as_of=as_of,
        benchmark_handle=None,
        benchmark_fingerprint_sha256=None,
        fx_method=FxConversionMethod.NOT_APPLICABLE,
        fx_source=FxSource.NOT_APPLICABLE,
        fx_timestamp=None,
        calendar=AnalysisCalendar.CALENDAR_DAYS,
        reporting_currency="DKK",
        metric_currency_bindings=(),
        model_parameters=(),
    )


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


def _config_with_alternate_store(tmp_path: Path) -> AnalyticsConfig:
    default = _config(tmp_path)
    alternate_store = prepare_owner_only_path(
        default.paths.state_root / "alternate" / "custom.duckdb",
    )
    return AnalyticsConfig(
        limits=default.limits,
        paths=AnalyticsPaths(
            state_root=default.paths.state_root,
            analytics_root=default.paths.analytics_root,
            artifacts_dir=default.paths.artifacts_dir,
            store_path=alternate_store,
        ),
    )


def _source_page(  # noqa: PLR0913
    store: AnalyticsStore,
    *,
    source_kind: str = "price_bars",
    page_key: str = "bars-page-1",
    source_revision: str = "rev-1",
    contract_sha256: str = _SCHEMA_SHA256,
    payload: dict[str, object] | None = None,
    row_count: int = 1,
    source_timestamp: datetime = _SOURCE_AT,
    account_scope: str = "aggregate",
    instrument_handle: str | None = None,
) -> store_module.StoredSourcePage:
    return store.put_source_page(
        source_kind=source_kind,
        page_key=page_key,
        source_revision=source_revision,
        contract_name=f"{source_kind}_page",
        contract_sha256=contract_sha256,
        payload=payload or {"row_count": 1, "schema": "redacted_bar_page"},
        row_count=row_count,
        source_timestamp=source_timestamp,
        account_scope=account_scope,
        instrument_handle=instrument_handle,
    )


def _dataset(
    store: AnalyticsStore,
    page_id: str,
    *,
    dataset_id: str | None = None,
    account_scope: str = "aggregate",
    source_revision: str = "rev-1",
) -> store_module.StoredDataset:
    return _dataset_from_pages(
        store,
        (page_id,),
        dataset_id=dataset_id,
        account_scope=account_scope,
        source_revision=source_revision,
    )


def _dataset_from_pages(
    store: AnalyticsStore,
    page_ids: tuple[str, ...],
    *,
    dataset_id: str | None = None,
    account_scope: str = "aggregate",
    source_revision: str = "rev-1",
) -> store_module.StoredDataset:
    return store.create_dataset(
        dataset_id=dataset_id or new_safe_handle(HandleKind.DATASET_ID),
        account_scope=account_scope,
        source_scope="saxo_openapi",
        source_revision=source_revision,
        source_page_ids=page_ids,
        created_at=_SOURCE_AT + timedelta(minutes=1),
        coverage_start=_SOURCE_AT,
        coverage_end=_SOURCE_AT,
        quality_state=QualityState.COMPLETE,
    )


def _multi_contract_dataset(store: AnalyticsStore) -> store_module.StoredDataset:
    first = _source_page(
        store,
        page_key="contract-page-a",
        contract_sha256=_SCHEMA_SHA256,
    )
    second = _source_page(
        store,
        page_key="contract-page-b",
        contract_sha256=_SECOND_SCHEMA_SHA256,
    )
    return _dataset_from_pages(store, (first.page_id, second.page_id))


def _analysis_result(  # noqa: PLR0913
    dataset_id: str,
    analysis_id: str,
    *,
    account_scope: str = "aggregate",
    source_revision: str = "rev-1",
    source_contract_sha256: str = _SCHEMA_SHA256,
    source_contract_sha256s: tuple[str, ...] = (),
    request: MarketAnalysisRequest
    | InstrumentAnalysisRequest
    | PortfolioAnalysisRequest
    | None = None,
) -> AnalysisResult:
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
        source_revision=source_revision,
        source_contract_sha256=source_contract_sha256,
        source_contract_sha256s=source_contract_sha256s,
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
        request=request
        or MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="row_count",
            dataset_id=dataset_id,
            parameters=_analysis_parameters(as_of=_SOURCE_AT + timedelta(minutes=2)),
        ),
        account_scope=account_scope,
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
            source_revision=source_revision,
            source_contract_sha256=source_contract_sha256,
            source_contract_sha256s=source_contract_sha256s,
            source_timestamp=_SOURCE_AT,
            proof_receipts=(receipt,),
            engine_name="store_test_engine",
            engine_version="1.0",
            code_commit=_CODE_COMMIT,
            analysis_input_sha256="d" * 64,
            analysis_parameters_sha256="c" * 64,
            analysis_engine_sha256="e" * 64,
            analysis_seed_sha256="f" * 64,
            random_seed=None,
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


def _seed_normalized_scope(
    store: AnalyticsStore,
    data_type: StorageDataType,
) -> str:
    if data_type is StorageDataType.JOBS:
        job_id = new_safe_handle(HandleKind.JOB_ID)
        with store._write_connection() as connection:  # noqa: SLF001
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    state,
                    request_fingerprint,
                    created_at,
                    updated_at,
                    analysis_id,
                    message,
                    request_json
                )
                VALUES (?, 'cancelled', ?, ?, ?, NULL, NULL, '{}')
                """,
                (job_id, "f" * 64, _SOURCE_AT, _SOURCE_AT),
            )
            store._bump_revision(connection)  # noqa: SLF001
        return job_id

    instrument_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    with store._write_connection() as connection:  # noqa: SLF001
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
            VALUES (?, 'Stock', 'Synthetic instrument', 'rev-1', ?, ?, '{}')
            """,
            (instrument_handle, _SOURCE_AT, "e" * 64),
        )
        store._bump_revision(connection)  # noqa: SLF001
    page = _source_page(
        store,
        source_kind=data_type.value,
        page_key=f"{data_type.value}-page-1",
        instrument_handle=instrument_handle,
    )
    with store._write_connection() as connection:  # noqa: SLF001
        if data_type is StorageDataType.PRICE_BARS:
            connection.execute(
                """
                INSERT INTO price_bars
                VALUES (?, ?, 'rev-1', ?, '1m', 1, 2, 0.5, 1.5, 10, 'DKK', FALSE)
                """,
                (page.page_id, instrument_handle, _SOURCE_AT),
            )
        elif data_type is StorageDataType.QUOTES:
            connection.execute(
                """
                INSERT INTO quotes
                VALUES ('quote-1', ?, ?, 'rev-1', ?, 1, 2, 1.5, 'DKK', ?)
                """,
                (page.page_id, instrument_handle, _SOURCE_AT, "f" * 64),
            )
        elif data_type is StorageDataType.OPTION_SNAPSHOTS:
            connection.execute(
                """
                INSERT INTO option_snapshots
                VALUES (
                    'option-1', ?, ?, ?, 'rev-1', ?, DATE '2026-12-31',
                    100, 'DKK', 'call', ?, '{}'
                )
                """,
                (
                    page.page_id,
                    instrument_handle,
                    instrument_handle,
                    _SOURCE_AT,
                    "f" * 64,
                ),
            )
        elif data_type is StorageDataType.TRANSACTIONS:
            connection.execute(
                """
                INSERT INTO transactions
                VALUES (
                    'transaction-1', ?, 'aggregate', ?, 'rev-1', ?,
                    10, 'DKK', ?, '{}'
                )
                """,
                (page.page_id, instrument_handle, _SOURCE_AT, "f" * 64),
            )
        elif data_type is StorageDataType.BOOKINGS:
            connection.execute(
                """
                INSERT INTO bookings
                VALUES (
                    'booking-1', ?, 'aggregate', ?, 'rev-1', ?,
                    10, 'DKK', ?, '{}'
                )
                """,
                (page.page_id, instrument_handle, _SOURCE_AT, "f" * 64),
            )
        elif data_type is StorageDataType.CLOSED_POSITIONS:
            connection.execute(
                """
                INSERT INTO closed_positions
                VALUES (
                    'closed-position-1', ?, 'aggregate', ?, 'rev-1', ?,
                    10, 'DKK', ?, '{}'
                )
                """,
                (page.page_id, instrument_handle, _SOURCE_AT, "f" * 64),
            )
        elif data_type is StorageDataType.COSTS:
            connection.execute(
                """
                INSERT INTO costs
                VALUES (
                    'cost-1', ?, 'aggregate', ?, 'rev-1', ?,
                    10, 'DKK', ?, '{}'
                )
                """,
                (page.page_id, instrument_handle, _SOURCE_AT, "f" * 64),
            )
        else:
            raise AssertionError(f"unsupported normalized test scope: {data_type}")
        store._bump_revision(connection)  # noqa: SLF001
    return page.page_id


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


def _write_two_quota_pages_in_one_transaction(store: AnalyticsStore) -> None:
    with store.transaction():
        _source_page(
            store,
            page_key="bars-page-2",
            payload={"data": "x" * 8_000},
        )
        _source_page(
            store,
            page_key="bars-page-3",
            payload={"data": "y" * 8_000},
        )


def _write_one_quota_page_then_abort(store: AnalyticsStore) -> None:
    with store.transaction():
        _source_page(
            store,
            page_key="bars-page-2",
            payload={"data": "x" * 8_000},
        )
        raise RuntimeError("abort quota probe")


def test_open_creates_the_normalized_schema_and_owner_only_database(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    store.close()

    with duckdb.connect(str(config.paths.store_path), read_only=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'",
            ).fetchall()
        }
        version = connection.execute(
            "SELECT version FROM analytics_schema WHERE singleton = TRUE",
        ).fetchone()

    assert names >= _REQUIRED_TABLES
    assert version == (LATEST_SCHEMA_VERSION,)
    assert _mode(config.paths.store_path) == _OWNER_FILE_MODE


def test_analysis_provenance_accepts_an_explicit_contract_set_binding() -> None:
    result = _analysis_result(
        new_safe_handle(HandleKind.DATASET_ID),
        new_safe_handle(HandleKind.ANALYSIS_ID),
        source_contract_sha256=_MULTI_CONTRACT_SHA256,
    )
    payload = result.model_dump(mode="python")
    provenance = cast("dict[str, Any]", payload["provenance"])
    provenance["source_contract_sha256s"] = (
        _SCHEMA_SHA256,
        _SECOND_SCHEMA_SHA256,
    )
    receipts = cast("tuple[dict[str, Any], ...]", provenance["proof_receipts"])
    source_binding = cast("dict[str, Any]", receipts[0]["source_binding"])
    source_binding["source_contract_sha256s"] = (
        _SCHEMA_SHA256,
        _SECOND_SCHEMA_SHA256,
    )

    rebound = AnalysisResult.model_validate(payload)

    assert rebound.provenance.source_contract_sha256s == (
        _SCHEMA_SHA256,
        _SECOND_SCHEMA_SHA256,
    )


def test_v0_fixture_contains_only_schema_metadata_and_no_private_rows() -> None:
    with duckdb.connect(str(_FIXTURE), read_only=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'",
            ).fetchall()
        }
        rows = connection.execute("SELECT * FROM analytics_schema").fetchall()

    assert names == {"analytics_schema"}
    assert rows == [(True, 0)]


def test_clean_wheel_contains_and_loads_the_fixed_migration_catalog(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    uv_path = shutil.which("uv")
    assert uv_path is not None
    build = subprocess.run(
        [
            uv_path,
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=_PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "saxo_bank_mcp/_analytics_migrations/0001_initial.sql" in archive.namelist()
        assert (
            "saxo_bank_mcp/_analytics_migrations/0002_source_page_identity.sql"
            in archive.namelist()
        )
        assert (
            "saxo_bank_mcp/_analytics_migrations/"
            "0003_account_scope_binding_and_snapshot_order.sql" in archive.namelist()
        )

    isolated_state = tmp_path / "isolated-state"
    script = """
import sys
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_store import AnalyticsStore

config = load_analytics_config({"XDG_STATE_HOME": sys.argv[1]})
store = AnalyticsStore.open(config)
store.close()
print(config.paths.store_path.is_file())
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(wheel)
    opened = subprocess.run(
        [sys.executable, "-c", script, str(isolated_state)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert opened.returncode == 0, opened.stderr
    assert opened.stdout.strip() == "True"


def test_migration_from_v0_creates_an_owner_only_backup(tmp_path: Path) -> None:
    target = tmp_path / "store.duckdb"
    shutil.copyfile(_FIXTURE, target)

    result = migrate_store(target, LATEST_SCHEMA_VERSION)
    backups = tuple(tmp_path.glob(f"store.before-v{LATEST_SCHEMA_VERSION}.*.duckdb"))

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
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'migration_must_rollback'",
        ).fetchone() == (0,)


def test_failed_v2_migration_keeps_historical_v1_byte_identical_and_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "store.duckdb"
    connection = duckdb.connect(str(target))
    try:
        connection.execute(
            Path("data/analytics/migrations/0001_initial.sql").read_text(
                encoding="utf-8",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'initial', ?, current_timestamp)",
            ("0" * 64,),
        )
        connection.execute("INSERT INTO analytics_schema VALUES (TRUE, 1)")
    finally:
        connection.close()
    target.chmod(_OWNER_FILE_MODE)
    before = _sha256(target)
    original_reader = migrations._read_migration  # noqa: SLF001

    def broken_migration(version: int) -> str:
        sql = original_reader(version)
        return f"{sql}\nSELECT * FROM absent;"

    monkeypatch.setattr(migrations, "_read_migration", broken_migration)

    with pytest.raises(MigrationError, match="migration 2 failed"):
        migrate_store(target, LATEST_SCHEMA_VERSION)

    assert _sha256(target) == before
    with duckdb.connect(str(target), read_only=True) as readable:
        assert readable.execute("SELECT version FROM analytics_schema").fetchone() == (1,)
        columns = {
            str(row[1]) for row in readable.execute("PRAGMA table_info('source_pages')").fetchall()
        }
    assert "logical_key_sha256" not in columns
    assert "fingerprint_sha256" not in columns


def test_v3_migration_orders_existing_snapshots_and_adds_selector_bindings(
    tmp_path: Path,
) -> None:
    target = tmp_path / "store.duckdb"
    connection = duckdb.connect(str(target))
    try:
        connection.execute(
            Path("data/analytics/migrations/0001_initial.sql").read_text(
                encoding="utf-8",
            ),
        )
        connection.execute(
            Path("data/analytics/migrations/0002_source_page_identity.sql").read_text(
                encoding="utf-8",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'initial', ?, current_timestamp)",
            ("0" * 64,),
        )
        connection.execute(
            """
            INSERT INTO schema_migrations
            VALUES (2, 'source_page_identity', ?, current_timestamp)
            """,
            ("1" * 64,),
        )
        connection.execute("INSERT INTO analytics_schema VALUES (TRUE, 2)")
        connection.execute(
            """
            INSERT INTO account_snapshots (
                snapshot_id, dataset_id, page_id, snapshot_kind, account_scope,
                source_revision, as_of, byte_count, fingerprint_sha256, payload_json
            )
            VALUES (?, ?, NULL, 'portfolio', ?, ?, ?, 2, ?, '{}')
            """,
            (
                "ps_00000000000040008000000000000000",
                "ds_00000000000040008000000000000000",
                "aa_00000000000040008000000000000000",
                f"capture:{'0' * 64}",
                _SOURCE_AT,
                "2" * 64,
            ),
        )
    finally:
        connection.close()
    target.chmod(_OWNER_FILE_MODE)

    result = migrate_store(target, 3)

    with duckdb.connect(str(target), read_only=True) as migrated:
        version = migrated.execute("SELECT version FROM analytics_schema").fetchone()
        created_order = migrated.execute(
            "SELECT created_order FROM account_snapshots",
        ).fetchone()
        binding_count = migrated.execute(
            "SELECT count(*) FROM account_scope_bindings",
        ).fetchone()
    assert result.to_version == LATEST_SCHEMA_VERSION
    assert version == (LATEST_SCHEMA_VERSION,)
    assert created_order == (1,)
    assert binding_count == (0,)


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


def test_noncanonical_contained_store_path_uses_the_same_writer_lock(
    tmp_path: Path,
) -> None:
    default = _config(tmp_path)
    alternate_store = prepare_owner_only_path(
        default.paths.state_root / "alternate" / "custom.duckdb",
    )
    config = AnalyticsConfig(
        limits=default.limits,
        paths=AnalyticsPaths(
            state_root=default.paths.state_root,
            analytics_root=default.paths.analytics_root,
            artifacts_dir=default.paths.artifacts_dir,
            store_path=alternate_store,
        ),
    )
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

        assert (
            len(
                store.list_storage(
                    StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
                ),
            )
            == 1
        )
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


def test_identical_payloads_in_distinct_account_scopes_do_not_collapse(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        aggregate = _source_page(store, account_scope="aggregate")
        selected = _source_page(store, account_scope="selected SIM account")

        assert aggregate.page_id != selected.page_id
        assert aggregate.fingerprint_sha256 != selected.fingerprint_sha256
        assert {
            entry.account_scope
            for entry in store.list_storage(
                StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
            )
        } == {"aggregate", "selected SIM account"}
    finally:
        store.close()


@pytest.mark.parametrize(
    ("payload", "row_count", "source_timestamp"),
    [
        ({"schema": "changed"}, 1, _SOURCE_AT),
        ({"schema": "redacted_bar_page"}, 2, _SOURCE_AT),
        ({"schema": "redacted_bar_page"}, 1, _SOURCE_AT + timedelta(seconds=1)),
    ],
)
def test_source_page_logical_revision_rejects_conflicting_material(
    tmp_path: Path,
    payload: dict[str, object],
    row_count: int,
    source_timestamp: datetime,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        _source_page(
            store,
            payload={"schema": "redacted_bar_page"},
        )

        with pytest.raises(StoreConflictError, match="source page"):
            _source_page(
                store,
                payload=payload,
                row_count=row_count,
                source_timestamp=source_timestamp,
            )
    finally:
        store.close()


def test_source_page_rejects_a_missing_instrument_reference(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        with pytest.raises(StoreNotFoundError, match="instrument"):
            _source_page(
                store,
                instrument_handle=new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
            )

        assert store.list_storage(StorageScope()) == ()
    finally:
        store.close()


def test_dataset_rejects_a_missing_source_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        page = _source_page(store)
        with store_module._connect_store_database(  # noqa: SLF001
            config.paths.store_path,
            read_only=False,
        ) as connection:
            connection.execute("DELETE FROM source_contracts")

        with pytest.raises(StoreNotFoundError, match="source contract"):
            _dataset(store, page.page_id)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("page_account_scope", "page_revision", "dataset_account_scope", "dataset_revision"),
    [
        ("selected SIM account", "rev-1", "aggregate", "rev-1"),
        ("aggregate", "rev-1", "aggregate", "rev-2"),
    ],
)
def test_dataset_rejects_source_page_binding_mismatches(
    tmp_path: Path,
    page_account_scope: str,
    page_revision: str,
    dataset_account_scope: str,
    dataset_revision: str,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(
            store,
            account_scope=page_account_scope,
            source_revision=page_revision,
        )

        with pytest.raises(StoreValidationError, match="dataset"):
            _dataset(
                store,
                page.page_id,
                account_scope=dataset_account_scope,
                source_revision=dataset_revision,
            )
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


@pytest.mark.parametrize(
    ("account_scope", "source_revision"),
    [
        ("selected SIM account", "rev-1"),
        ("aggregate", "rev-2"),
    ],
)
def test_snapshot_rejects_dataset_binding_mismatches(
    tmp_path: Path,
    account_scope: str,
    source_revision: str,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(store)
        dataset = _dataset(store, page.page_id)

        with pytest.raises(StoreValidationError, match="snapshot"):
            store.create_snapshot(
                snapshot_id=new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID),
                dataset_id=dataset.dataset_id,
                snapshot_kind="portfolio",
                account_scope=account_scope,
                source_revision=source_revision,
                as_of=_SOURCE_AT,
                payload={"schema": "redacted_snapshot"},
            )
    finally:
        store.close()


def test_analysis_rejects_a_missing_dataset_reference(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        result = _analysis_result(
            new_safe_handle(HandleKind.DATASET_ID),
            new_safe_handle(HandleKind.ANALYSIS_ID),
        )

        with pytest.raises(StoreNotFoundError, match="dataset"):
            store.put_analysis(result)
    finally:
        store.close()


def test_instrument_analysis_rejects_a_missing_instrument_reference(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(store)
        dataset = _dataset(store, page.page_id)
        request = InstrumentAnalysisRequest(
            request_kind="instrument",
            analysis_kind="row_count",
            dataset_id=dataset.dataset_id,
            parameters=_analysis_parameters(as_of=_SOURCE_AT + timedelta(minutes=2)),
            instrument_handles=(new_safe_handle(HandleKind.INSTRUMENT_HANDLE),),
        )
        result = _analysis_result(
            dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            request=request,
        )

        with pytest.raises(StoreNotFoundError, match="instrument"):
            store.put_analysis(result)
    finally:
        store.close()


def test_portfolio_analysis_rejects_a_missing_snapshot_reference(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(store)
        dataset = _dataset(store, page.page_id)
        request = PortfolioAnalysisRequest(
            request_kind="portfolio",
            analysis_kind="row_count",
            dataset_id=dataset.dataset_id,
            parameters=_analysis_parameters(as_of=_SOURCE_AT + timedelta(minutes=2)),
            portfolio_snapshot_id=new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID),
        )
        result = _analysis_result(
            dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            request=request,
        )

        with pytest.raises(StoreNotFoundError, match="snapshot"):
            store.put_analysis(result)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("account_scope", "source_revision", "source_contract_sha256"),
    [
        ("selected SIM account", "rev-1", _SCHEMA_SHA256),
        ("aggregate", "rev-2", _SCHEMA_SHA256),
        ("aggregate", "rev-1", "d" * 64),
    ],
)
def test_analysis_rejects_dataset_binding_mismatches(
    tmp_path: Path,
    account_scope: str,
    source_revision: str,
    source_contract_sha256: str,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _source_page(store)
        dataset = _dataset(store, page.page_id)
        result = _analysis_result(
            dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            account_scope=account_scope,
            source_revision=source_revision,
            source_contract_sha256=source_contract_sha256,
        )

        with pytest.raises(StoreValidationError, match="analysis"):
            store.put_analysis(result)
    finally:
        store.close()


def test_multi_contract_analysis_accepts_the_complete_sorted_set_and_fingerprint(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        dataset = _multi_contract_dataset(store)
        result = _analysis_result(
            dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            source_contract_sha256=_MULTI_CONTRACT_SHA256,
            source_contract_sha256s=(
                _SCHEMA_SHA256,
                _SECOND_SCHEMA_SHA256,
            ),
        )

        stored = store.put_analysis(result)

        assert stored.analysis_id == result.analysis_id
    finally:
        store.close()


@pytest.mark.parametrize(
    ("source_contract_sha256", "source_contract_sha256s"),
    [
        pytest.param(_SCHEMA_SHA256, (), id="missing"),
        pytest.param(_SCHEMA_SHA256, (_SCHEMA_SHA256,), id="partial"),
        pytest.param(
            _SCHEMA_SHA256,
            (
                _SCHEMA_SHA256,
                _SECOND_SCHEMA_SHA256,
                _EXTRA_SCHEMA_SHA256,
            ),
            id="extra",
        ),
        pytest.param(
            _SCHEMA_SHA256,
            (
                _SCHEMA_SHA256,
                _SCHEMA_SHA256,
                _SECOND_SCHEMA_SHA256,
            ),
            id="duplicate",
        ),
        pytest.param(
            _MULTI_CONTRACT_SHA256,
            (_SECOND_SCHEMA_SHA256, _SCHEMA_SHA256),
            id="reordered",
        ),
        pytest.param(
            _SCHEMA_SHA256,
            (_SCHEMA_SHA256, _SECOND_SCHEMA_SHA256),
            id="wrong-fingerprint",
        ),
    ],
)
def test_multi_contract_analysis_rejects_incomplete_or_inconsistent_bindings(
    tmp_path: Path,
    source_contract_sha256: str,
    source_contract_sha256s: tuple[str, ...],
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        dataset = _multi_contract_dataset(store)
        result = _analysis_result(
            dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            source_contract_sha256=source_contract_sha256,
            source_contract_sha256s=source_contract_sha256s,
        )

        with pytest.raises(StoreValidationError, match="contract set"):
            store.put_analysis(result)
    finally:
        store.close()


def test_multi_contract_analysis_rejects_a_missing_dataset_contract_row(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        dataset = _multi_contract_dataset(store)
        with store_module._connect_store_database(  # noqa: SLF001
            config.paths.store_path,
            read_only=False,
        ) as connection:
            connection.execute(
                "DELETE FROM source_contracts WHERE contract_sha256 = ?",
                (_SECOND_SCHEMA_SHA256,),
            )
        result = _analysis_result(
            dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            source_contract_sha256=_SCHEMA_SHA256,
            source_contract_sha256s=(_SCHEMA_SHA256,),
        )

        with pytest.raises(StoreNotFoundError, match="source contract"):
            store.put_analysis(result)
    finally:
        store.close()


def test_portfolio_analysis_rejects_a_snapshot_from_another_dataset(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        first_page = _source_page(store)
        second_page = _source_page(store, page_key="bars-page-2")
        first_dataset = _dataset(store, first_page.page_id)
        second_dataset = _dataset(store, second_page.page_id)
        snapshot_id = new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID)
        store.create_snapshot(
            snapshot_id=snapshot_id,
            dataset_id=first_dataset.dataset_id,
            snapshot_kind="portfolio",
            account_scope="aggregate",
            source_revision="rev-1",
            as_of=_SOURCE_AT,
            payload={"schema": "redacted_snapshot"},
        )
        request = PortfolioAnalysisRequest(
            request_kind="portfolio",
            analysis_kind="row_count",
            dataset_id=second_dataset.dataset_id,
            parameters=_analysis_parameters(as_of=_SOURCE_AT + timedelta(minutes=2)),
            portfolio_snapshot_id=snapshot_id,
        )
        result = _analysis_result(
            second_dataset.dataset_id,
            new_safe_handle(HandleKind.ANALYSIS_ID),
            request=request,
        )

        with pytest.raises(StoreValidationError, match="snapshot"):
            store.put_analysis(result)
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


def test_transaction_reserves_cumulative_quota_and_rolls_back_all_writes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        existing = _source_page(store)
        sparse = config.paths.artifacts_dir / "reserved.bin"
        sparse.touch(mode=_OWNER_FILE_MODE)
        current_bytes = _all_local_bytes(config.paths.analytics_root)
        with sparse.open("r+b") as reserved:
            reserved.truncate(config.limits.store_quota_bytes - current_bytes - 12_000)
        sparse.chmod(_OWNER_FILE_MODE)

        with pytest.raises(StoreQuotaError, match="quota"):
            _write_two_quota_pages_in_one_transaction(store)

        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        assert tuple(entry.object_id for entry in entries) == (existing.page_id,)
        assert sparse.exists()
    finally:
        store.close()


def test_alternate_store_resources_count_toward_transaction_quota(
    tmp_path: Path,
) -> None:
    config = _config_with_alternate_store(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        existing = _source_page(store)
        store_path = config.paths.store_path
        lock_path = store_path.with_name(f".{store_path.name}.write.lock")
        backup = store_path.with_name(
            f"{store_path.stem}.before-v1.{'f' * 32}.duckdb",
        )
        migration_temp = store_path.with_name(
            f".{store_path.name}.fixture.migrating",
        )
        migration_wal = Path(f"{migration_temp}.wal")
        for path, size in (
            (backup, 2_048),
            (migration_temp, 2_048),
            (migration_wal, 2_048),
        ):
            path.touch(mode=_OWNER_FILE_MODE)
            with path.open("r+b") as managed:
                managed.truncate(size)
            path.chmod(_OWNER_FILE_MODE)
        unrelated = store_path.parent / "unrelated-parent-file.bin"
        unrelated.touch(mode=_OWNER_FILE_MODE)
        with unrelated.open("r+b") as ignored:
            ignored.truncate(config.limits.store_quota_bytes)
        unrelated.chmod(_OWNER_FILE_MODE)
        unrelated_lookalike = store_path.with_name(
            f"{store_path.stem}.before-vacation.notes.duckdb",
        )
        unrelated_lookalike.touch(mode=_OWNER_FILE_MODE)
        with unrelated_lookalike.open("r+b") as ignored:
            ignored.truncate(config.limits.store_quota_bytes)
        unrelated_lookalike.chmod(_OWNER_FILE_MODE)
        managed_external = (store_path, lock_path, backup, migration_temp, migration_wal)
        sparse = config.paths.artifacts_dir / "reserved.bin"
        sparse.touch(mode=_OWNER_FILE_MODE)

        def reset_headroom() -> None:
            with sparse.open("r+b") as reserved:
                reserved.truncate(0)
            analytics_bytes = _all_local_bytes(config.paths.analytics_root)
            external_bytes = sum(path.stat().st_size for path in managed_external)
            with sparse.open("r+b") as reserved:
                reserved.truncate(
                    config.limits.store_quota_bytes - analytics_bytes - external_bytes - 12_000,
                )
            sparse.chmod(_OWNER_FILE_MODE)

        reset_headroom()
        with pytest.raises(RuntimeError, match="quota probe"):
            _write_one_quota_page_then_abort(store)

        reset_headroom()
        with pytest.raises(StoreQuotaError, match="quota"):
            _write_two_quota_pages_in_one_transaction(store)

        entries = store.list_storage(
            StorageScope(data_types=(StorageDataType.SOURCE_PAGES,)),
        )
        assert tuple(entry.object_id for entry in entries) == (existing.page_id,)
        assert unrelated.exists()
        assert unrelated_lookalike.exists()
        assert all(path.exists() for path in managed_external)
    finally:
        store.close()


@pytest.mark.parametrize(
    "data_type",
    [
        StorageDataType.PRICE_BARS,
        StorageDataType.QUOTES,
        StorageDataType.OPTION_SNAPSHOTS,
        StorageDataType.TRANSACTIONS,
        StorageDataType.BOOKINGS,
        StorageDataType.CLOSED_POSITIONS,
        StorageDataType.COSTS,
        StorageDataType.JOBS,
    ],
)
def test_normalized_storage_scopes_list_value_free_summaries(
    tmp_path: Path,
    data_type: StorageDataType,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        object_id = _seed_normalized_scope(store, data_type)

        entries = store.list_storage(StorageScope(data_types=(data_type,)))

        assert len(entries) == 1
        assert entries[0].data_type is data_type
        assert entries[0].object_id == object_id
        assert entries[0].row_count == 1
        dumped = repr(entries[0])
        assert "payload" not in dumped
        assert "request_json" not in dumped
        assert "amount_value" not in dumped
        assert "path" not in dumped
    finally:
        store.close()


@pytest.mark.parametrize(
    "data_type",
    [
        StorageDataType.PRICE_BARS,
        StorageDataType.QUOTES,
        StorageDataType.OPTION_SNAPSHOTS,
        StorageDataType.TRANSACTIONS,
        StorageDataType.BOOKINGS,
        StorageDataType.CLOSED_POSITIONS,
        StorageDataType.COSTS,
        StorageDataType.JOBS,
    ],
)
def test_normalized_storage_scopes_preview_and_delete_the_direct_closure(
    tmp_path: Path,
    data_type: StorageDataType,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        _seed_normalized_scope(store, data_type)

        preview = store.preview_delete(StorageScope(data_types=(data_type,)))

        expected_tables = (
            {data_type.value}
            if data_type is StorageDataType.JOBS
            else {data_type.value, "source_pages"}
        )
        assert {item.table for item in preview.table_counts} == expected_tables
        assert all(item.rows == 1 for item in preview.table_counts)
        receipt = store.delete_previewed(preview.token)
        assert receipt.table_counts == preview.table_counts
        assert store.list_storage(StorageScope(data_types=(data_type,))) == ()
        dumped = repr(receipt)
        assert "token" not in dumped
        assert "payload" not in dumped
        assert "value" not in dumped
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
        assert {entry.object_id for entry in store.list_storage(StorageScope())} >= {
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
