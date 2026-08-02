from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock, get_ident
from types import MappingProxyType
from typing import Final, Self, cast
from uuid import RFC_4122, UUID, uuid4

import duckdb
from pydantic import ValidationError

from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    prepare_owner_only_path,
)
from saxo_bank_mcp.analytics_migrations import (
    LATEST_SCHEMA_VERSION,
    migrate_store,
    store_writer_lock_path,
)
from saxo_bank_mcp.analytics_models import (
    AnalysisResult,
    ArtifactSummary,
    HandleKind,
    InstrumentAnalysisRequest,
    PortfolioAnalysisRequest,
    QualityState,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_source_contracts import (
    SourceCaptureEnvelope,
    SourceJsonValue,
    SourceQualityProof,
    compare_source_schema,
    source_contract_fingerprint,
    source_contracts_by_id,
    source_page_fingerprint,
    source_quality_proof,
)

_OWNER_FILE_MODE: Final = 0o600
_OPAQUE_UUID_VERSION: Final = 4
_CONNECTION_CONFIG: Final = MappingProxyType(
    {
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "enable_external_access": "false",
    },
)
_DELETION_TOKEN_TTL: Final = timedelta(minutes=5)
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_SAFE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SOURCE_REVISION_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
)
_PAGE_KEY_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$",
)
_ACCOUNT_ALIAS_PATTERN: Final = re.compile(
    r"^aa_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$",
)
_SAFE_ACCOUNT_SCOPES: Final = frozenset({"aggregate", "selected SIM account"})
_INFO_PRICE_CONTRACT_ID: Final = "info_price_v1"
_SOURCE_KINDS: Final = frozenset(
    {
        "account_snapshots",
        "bookings",
        "closed_positions",
        "costs",
        "balances",
        "chart",
        "corporate_actions",
        "exposure",
        "info_prices",
        "option_snapshots",
        "options_chain",
        "orders",
        "performance",
        "positions",
        "price_bars",
        "quotes",
        "reference_instruments",
        "transactions",
    },
)


def supported_source_kinds() -> frozenset[str]:
    """Return the immutable source kinds accepted by the store bridge."""
    return _SOURCE_KINDS


_DELETION_COLUMNS: Final = {
    "account_snapshots": "snapshot_id",
    "analyses": "analysis_id",
    "artifacts": "artifact_id",
    "bookings": "page_id",
    "closed_positions": "page_id",
    "costs": "page_id",
    "dataset_source_pages": "dataset_id",
    "datasets": "dataset_id",
    "jobs": "job_id",
    "metrics": "analysis_id",
    "option_snapshots": "page_id",
    "price_bars": "page_id",
    "proof_receipts": "analysis_id",
    "quotes": "page_id",
    "source_pages": "page_id",
    "transactions": "page_id",
}
_BYTE_SUM_QUERIES: Final = {
    "account_snapshots": (
        "SELECT coalesce(sum(byte_count), 0) FROM account_snapshots WHERE snapshot_id = ANY(?)"
    ),
    "analyses": ("SELECT coalesce(sum(byte_count), 0) FROM analyses WHERE analysis_id = ANY(?)"),
    "artifacts": ("SELECT coalesce(sum(byte_count), 0) FROM artifacts WHERE artifact_id = ANY(?)"),
    "datasets": ("SELECT coalesce(sum(byte_count), 0) FROM datasets WHERE dataset_id = ANY(?)"),
    "source_pages": (
        "SELECT coalesce(sum(byte_count), 0) FROM source_pages WHERE page_id = ANY(?)"
    ),
}
_DELETION_ORDER: Final = (
    "artifacts",
    "jobs",
    "metrics",
    "proof_receipts",
    "analyses",
    "account_snapshots",
    "dataset_source_pages",
    "datasets",
    "price_bars",
    "quotes",
    "option_snapshots",
    "transactions",
    "bookings",
    "closed_positions",
    "costs",
    "source_pages",
)
type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class StoreError(RuntimeError):
    """Base error for local analytics storage failures."""


class StoreValidationError(StoreError):
    """Raised when a typed store input is invalid."""


class StoreConflictError(StoreError):
    """Raised when an immutable handle is reused for different content."""


class StoreNotFoundError(StoreError):
    """Raised when a referenced local analytics object does not exist."""


class StoreBusyError(StoreError):
    """Raised when another process owns the single writer lock."""


class StoreQuotaError(StoreError):
    """Raised when a write would exceed the configured local quota."""


class DeletionTokenError(StoreError):
    """Raised when a deletion preview token cannot authorize deletion."""


class StorageDataType(StrEnum):
    """Typed data classes accepted by storage listing and deletion scopes."""

    SOURCE_PAGES = "source_pages"
    PRICE_BARS = "price_bars"
    QUOTES = "quotes"
    OPTION_SNAPSHOTS = "option_snapshots"
    ACCOUNT_SNAPSHOTS = "account_snapshots"
    TRANSACTIONS = "transactions"
    BOOKINGS = "bookings"
    CLOSED_POSITIONS = "closed_positions"
    COSTS = "costs"
    DATASETS = "datasets"
    ANALYSES = "analyses"
    ARTIFACTS = "artifacts"
    JOBS = "jobs"
    DELETION_RECEIPTS = "deletion_receipts"


_SOURCE_PAGE_BACKED_DATA_TYPES: Final = frozenset(
    {
        StorageDataType.SOURCE_PAGES,
        StorageDataType.PRICE_BARS,
        StorageDataType.QUOTES,
        StorageDataType.OPTION_SNAPSHOTS,
        StorageDataType.TRANSACTIONS,
        StorageDataType.BOOKINGS,
        StorageDataType.CLOSED_POSITIONS,
        StorageDataType.COSTS,
    },
)
_NORMALIZED_PAGE_QUERIES: Final[Mapping[StorageDataType, str]] = MappingProxyType(
    {
        StorageDataType.PRICE_BARS: """
            SELECT
                p.page_id,
                p.account_scope,
                b.instrument_handle,
                b.source_revision,
                epoch_us(min(b.bar_time)),
                epoch_us(max(b.bar_time)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM price_bars AS b
            JOIN source_pages AS p ON p.page_id = b.page_id
            GROUP BY ALL
        """,
        StorageDataType.QUOTES: """
            SELECT
                p.page_id,
                p.account_scope,
                q.instrument_handle,
                q.source_revision,
                epoch_us(min(q.captured_at)),
                epoch_us(max(q.captured_at)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM quotes AS q
            JOIN source_pages AS p ON p.page_id = q.page_id
            GROUP BY ALL
        """,
        StorageDataType.OPTION_SNAPSHOTS: """
            SELECT
                p.page_id,
                p.account_scope,
                o.instrument_handle,
                o.source_revision,
                epoch_us(min(o.captured_at)),
                epoch_us(max(o.captured_at)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM option_snapshots AS o
            JOIN source_pages AS p ON p.page_id = o.page_id
            GROUP BY ALL
        """,
        StorageDataType.TRANSACTIONS: """
            SELECT
                p.page_id,
                t.account_scope,
                t.instrument_handle,
                t.source_revision,
                epoch_us(min(t.effective_at)),
                epoch_us(max(t.effective_at)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM transactions AS t
            JOIN source_pages AS p ON p.page_id = t.page_id
            GROUP BY ALL
        """,
        StorageDataType.BOOKINGS: """
            SELECT
                p.page_id,
                b.account_scope,
                b.instrument_handle,
                b.source_revision,
                epoch_us(min(b.booked_at)),
                epoch_us(max(b.booked_at)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM bookings AS b
            JOIN source_pages AS p ON p.page_id = b.page_id
            GROUP BY ALL
        """,
        StorageDataType.CLOSED_POSITIONS: """
            SELECT
                p.page_id,
                c.account_scope,
                c.instrument_handle,
                c.source_revision,
                epoch_us(min(c.closed_at)),
                epoch_us(max(c.closed_at)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM closed_positions AS c
            JOIN source_pages AS p ON p.page_id = c.page_id
            GROUP BY ALL
        """,
        StorageDataType.COSTS: """
            SELECT
                p.page_id,
                c.account_scope,
                c.instrument_handle,
                c.source_revision,
                epoch_us(min(c.effective_at)),
                epoch_us(max(c.effective_at)),
                count(*),
                p.byte_count,
                p.fingerprint_sha256
            FROM costs AS c
            JOIN source_pages AS p ON p.page_id = c.page_id
            GROUP BY ALL
        """,
    },
)


@dataclass(frozen=True, slots=True)
class StorageScope:
    """Safe typed filters for owner-only storage operations."""

    account_scope: str | None = None
    data_types: tuple[StorageDataType, ...] = ()
    instrument_handles: tuple[str, ...] = ()
    from_at: datetime | None = None
    to_at: datetime | None = None
    dataset_ids: tuple[str, ...] = ()
    analysis_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalize equivalent scopes before they are fingerprinted."""
        account_scope = self.account_scope
        if account_scope is not None:
            account_scope = account_scope.strip()
            _validate_account_scope(account_scope)
        object.__setattr__(self, "account_scope", account_scope)
        object.__setattr__(
            self,
            "data_types",
            tuple(sorted(set(self.data_types), key=lambda item: item.value)),
        )
        object.__setattr__(
            self,
            "instrument_handles",
            _normalize_handles(self.instrument_handles, "ih"),
        )
        object.__setattr__(
            self,
            "dataset_ids",
            _normalize_handles(self.dataset_ids, "ds"),
        )
        object.__setattr__(
            self,
            "analysis_ids",
            _normalize_handles(self.analysis_ids, "an"),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            _normalize_handles(self.artifact_ids, "ar"),
        )
        if self.from_at is not None:
            _validate_utc(self.from_at)
        if self.to_at is not None:
            _validate_utc(self.to_at)
        if self.from_at is not None and self.to_at is not None and self.to_at < self.from_at:
            raise StoreValidationError("storage scope end precedes its start")


@dataclass(frozen=True, slots=True)
class StoredSourcePage:
    """Safe metadata for one immutable source page revision."""

    page_id: str
    source_kind: str
    page_key: str
    source_revision: str
    source_native_revision: str
    instrument_scope_sha256: str | None
    fingerprint_sha256: str
    row_count: int
    byte_count: int
    source_timestamp: datetime
    ingested_at: datetime


@dataclass(frozen=True, slots=True)
class StoredDataset:
    """Safe metadata for one immutable dataset."""

    dataset_id: str
    source_revision: str
    fingerprint_sha256: str
    row_count: int
    byte_count: int
    created_at: datetime
    quality_state: QualityState


@dataclass(frozen=True, slots=True)
class StoredSourceCapture:
    """Stored pages and deterministic dataset for one provider capture."""

    pages: tuple[StoredSourcePage, ...]
    dataset: StoredDataset


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """Safe metadata for one immutable account snapshot."""

    snapshot_id: str
    dataset_id: str
    fingerprint_sha256: str
    byte_count: int
    as_of: datetime


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    """Safe metadata for one stored typed analysis."""

    analysis_id: str
    dataset_id: str
    fingerprint_sha256: str
    byte_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Safe metadata for one stored artifact."""

    artifact_id: str
    analysis_id: str
    sha256: str
    byte_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StorageEntry:
    """Value-free storage metadata returned to owner-only listing tools."""

    data_type: StorageDataType
    object_id: str
    account_scope: str | None
    instrument_handle: str | None
    source_revision: str | None
    start_at: datetime | None
    end_at: datetime | None
    row_count: int
    byte_count: int
    fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class TableCount:
    """Exact count of rows affected in one normalized table."""

    table: str
    rows: int


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    """Value-free deletion closure plus its short-lived owner token."""

    token: str
    scope_fingerprint: str
    store_revision: int
    expires_at: datetime
    table_counts: tuple[TableCount, ...]
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """Persistent value-free record of one completed local deletion."""

    receipt_id: str
    scope_fingerprint: str
    deleted_at: datetime
    table_counts: tuple[TableCount, ...]
    estimated_bytes: int
    store_revision_before: int
    store_revision_after: int


@dataclass(frozen=True, slots=True)
class _DeletionPlan:
    targets: dict[str, tuple[str, ...]]
    table_counts: tuple[TableCount, ...]
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class _DatasetBinding:
    account_scope: str
    source_scope: str
    source_revision: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _connect_store_database(
    path: Path,
    *,
    read_only: bool,
) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        str(path),
        read_only=read_only,
        config=dict(_CONNECTION_CONFIG),
    )


def _validate_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StoreValidationError("analytics timestamps must use UTC")


def _validate_account_scope(value: str) -> None:
    if value in _SAFE_ACCOUNT_SCOPES:
        return
    if _ACCOUNT_ALIAS_PATTERN.fullmatch(value) is None:
        raise StoreValidationError("account scope must use a safe alias or aggregate")
    opaque_uuid = UUID(hex=value.removeprefix("aa_"))
    if opaque_uuid.version != _OPAQUE_UUID_VERSION or opaque_uuid.variant != RFC_4122:
        raise StoreValidationError("account scope must use a safe alias or aggregate")


def _validate_handle(value: str, prefix: str) -> None:
    expected = f"{prefix}_"
    if not value.startswith(expected):
        raise StoreValidationError("analytics handle has the wrong kind")
    payload = value.removeprefix(expected)
    if re.fullmatch(r"[0-9a-f]{32}", payload) is None:
        raise StoreValidationError("analytics handle must be opaque")
    opaque_uuid = UUID(hex=payload)
    if opaque_uuid.version != _OPAQUE_UUID_VERSION or opaque_uuid.variant != RFC_4122:
        raise StoreValidationError("analytics handle must be opaque")


def _normalize_handles(values: Sequence[str], prefix: str) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    for value in normalized:
        _validate_handle(value, prefix)
    return normalized


def _validate_sha256(value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise StoreValidationError("fingerprint must be lowercase SHA-256")


def _validate_source_revision(value: str) -> None:
    if _SOURCE_REVISION_PATTERN.fullmatch(value) is None:
        raise StoreValidationError("source revision is invalid")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError("store payload must be finite JSON") from error


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _capture_dataset_id(
    capture_revision: str,
    page_ids: Sequence[str],
) -> str:
    material = _canonical_json(
        {
            "capture_revision": capture_revision,
            "page_ids": sorted(page_ids),
        },
    )
    raw = bytearray(hashlib.sha256(material.encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return f"ds_{UUID(bytes=bytes(raw)).hex}"


def _require_datetime(value: object) -> datetime:
    if type(value) is not int:
        raise StoreError("analytics store returned an invalid timestamp")
    return datetime.fromtimestamp(value / 1_000_000, UTC)


def _require_int(value: object) -> int:
    if not isinstance(value, int):
        raise StoreError("analytics store returned an invalid count")
    return value


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise StoreError("analytics store returned invalid text")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _require_str(value)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _require_datetime(value)


class AnalyticsStore:
    """Owner-only DuckDB store with typed operations and one process-safe writer."""

    def __init__(
        self,
        config: AnalyticsConfig,
        lock_descriptor: int,
    ) -> None:
        """Retain only the validated config and owner-only lock descriptor."""
        self._config = config
        self._lock_descriptor = lock_descriptor
        self._thread_lock = RLock()
        self._active_writer: duckdb.DuckDBPyConnection | None = None
        self._transaction_owner: int | None = None
        self._transaction_depth = 0
        self._transaction_reserved_bytes = 0
        self._closed = False

    @classmethod
    def open(cls, config: AnalyticsConfig) -> Self:
        """Open the configured owner-only store at the latest fixed schema."""
        validated = AnalyticsConfig.model_validate(config)
        migrate_store(validated.paths.store_path, LATEST_SCHEMA_VERSION)
        validated.paths.store_path.chmod(_OWNER_FILE_MODE)
        lock_path = prepare_owner_only_path(
            store_writer_lock_path(validated.paths.store_path),
        )
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags)
        try:
            return cls(validated, descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        """Close explicit database and lock resources."""
        with self._thread_lock:
            if self._closed:
                return
            if self._transaction_owner is not None:
                raise StoreError("cannot close an active analytics transaction")
            os.close(self._lock_descriptor)
            self._closed = True

    @contextmanager
    def transaction(self) -> Generator[AnalyticsStore]:
        """Group typed store writes into one atomic writer transaction."""
        self._require_open()
        owner = get_ident()
        if self._transaction_owner == owner:
            self._transaction_depth += 1
            try:
                yield self
            finally:
                self._transaction_depth -= 1
            return

        with self._writer_lock():
            writer = _connect_store_database(
                self._config.paths.store_path,
                read_only=False,
            )
            try:
                writer.execute("BEGIN TRANSACTION")
                self._active_writer = writer
                self._transaction_owner = owner
                self._transaction_depth = 1
                self._transaction_reserved_bytes = 0
                try:
                    yield self
                except BaseException:
                    with suppress(duckdb.Error):
                        writer.execute("ROLLBACK")
                    raise
                else:
                    writer.execute("COMMIT")
                finally:
                    self._active_writer = None
                    self._transaction_owner = None
                    self._transaction_depth = 0
                    self._transaction_reserved_bytes = 0
            finally:
                writer.close()

    @contextmanager
    def market_ingestion_transaction(
        self,
        normalized_bytes: int,
    ) -> Generator[duckdb.DuckDBPyConnection]:
        """Atomically reserve and persist one raw-plus-normalized market capture."""
        if type(normalized_bytes) is not int or normalized_bytes < 0:
            raise StoreValidationError("normalized market byte estimate is invalid")
        with self.transaction():
            self._ensure_capacity(normalized_bytes)
            if self._active_writer is None:
                raise StoreError("analytics writer transaction is missing")
            yield self._active_writer

    @contextmanager
    def _writer_lock(self) -> Generator[None]:
        with self._thread_lock:
            self._require_open()
            try:
                fcntl.flock(
                    self._lock_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as error:
                raise StoreBusyError(
                    "another analytics writer owns the writer lock",
                ) from error
            try:
                yield
            finally:
                fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)

    @contextmanager
    def _write_connection(self) -> Generator[duckdb.DuckDBPyConnection]:
        owner = get_ident()
        if self._transaction_owner == owner:
            if self._active_writer is None:
                raise StoreError("analytics writer transaction is missing")
            yield self._active_writer
            return
        with self._writer_lock():
            writer = _connect_store_database(
                self._config.paths.store_path,
                read_only=False,
            )
            try:
                writer.execute("BEGIN TRANSACTION")
                try:
                    yield writer
                except BaseException:
                    with suppress(duckdb.Error):
                        writer.execute("ROLLBACK")
                    raise
                else:
                    writer.execute("COMMIT")
            finally:
                writer.close()

    @contextmanager
    def _read_connection(self) -> Generator[duckdb.DuckDBPyConnection]:
        self._require_open()
        connection = _connect_store_database(
            self._config.paths.store_path,
            read_only=False,
        )
        try:
            yield connection
        finally:
            connection.close()

    def _require_open(self) -> None:
        if self._closed:
            raise StoreError("analytics store is closed")

    @classmethod
    def ensure_owner_capacity(
        cls,
        config: AnalyticsConfig,
        incoming_bytes: int,
    ) -> None:
        """Refuse a write when all owner-managed analytics files exceed quota."""
        validated = AnalyticsConfig.model_validate(config)
        current_bytes = sum(path.stat().st_size for path in cls._owner_managed_files_for(validated))
        if not validated.limits.can_accept_ingestion(current_bytes, incoming_bytes):
            raise StoreQuotaError(
                "analytics store quota refuses the write without deleting data",
            )

    @classmethod
    def _owner_managed_files_for(cls, config: AnalyticsConfig) -> set[Path]:
        paths: set[Path] = set()
        for root in {
            config.paths.analytics_root,
            config.paths.artifacts_dir,
        }:
            paths.update(
                path.resolve(strict=True)
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )

        store_path = config.paths.store_path
        candidates = {
            store_path,
            Path(f"{store_path}.wal"),
            store_writer_lock_path(store_path),
            *cls._migration_resource_files(store_path),
        }
        paths.update(
            path.resolve(strict=True)
            for path in candidates
            if path.is_file() and not path.is_symlink()
        )
        return paths

    @staticmethod
    def _migration_resource_files(store_path: Path) -> set[Path]:
        backup_pattern = re.compile(
            rf"^{re.escape(store_path.stem)}\.before-v[0-9]+\.[0-9a-f]{{32}}"
            r"\.duckdb$",
        )
        temporary_pattern = re.compile(
            rf"^\.{re.escape(store_path.name)}\.[A-Za-z0-9_-]+"
            r"\.migrating(?:\.wal)?$",
        )
        candidates = {
            *store_path.parent.glob(
                f"{store_path.stem}.before-v*.*.duckdb",
            ),
            *store_path.parent.glob(
                f".{store_path.name}.*.migrating*",
            ),
        }
        return {
            path
            for path in candidates
            if backup_pattern.fullmatch(path.name) is not None
            or temporary_pattern.fullmatch(path.name) is not None
        }

    def _ensure_capacity(self, incoming_bytes: int) -> None:
        reserved_bytes = (
            self._transaction_reserved_bytes if self._transaction_owner == get_ident() else 0
        )
        self.ensure_owner_capacity(
            self._config,
            reserved_bytes + incoming_bytes,
        )
        if self._transaction_owner == get_ident():
            self._transaction_reserved_bytes += incoming_bytes

    @staticmethod
    def _revision(connection: duckdb.DuckDBPyConnection) -> int:
        row = connection.execute(
            "SELECT revision FROM store_metadata WHERE singleton = TRUE",
        ).fetchone()
        if row is None:
            raise StoreError("analytics store revision is missing")
        return _require_int(row[0])

    @classmethod
    def _bump_revision(cls, connection: duckdb.DuckDBPyConnection) -> int:
        connection.execute(
            "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
        )
        return cls._revision(connection)

    def put_source_page(  # noqa: C901, PLR0913
        self,
        *,
        source_kind: str,
        page_key: str,
        source_revision: str,
        contract_name: str,
        contract_sha256: str,
        payload: Mapping[str, object],
        row_count: int,
        source_timestamp: datetime,
        account_scope: str | None,
        instrument_handle: str | None,
        source_native_revision: str | None = None,
        instrument_scope_sha256: str | None = None,
    ) -> StoredSourcePage:
        """Store one immutable Saxo page and make duplicate retries a no-op."""
        if source_kind not in _SOURCE_KINDS:
            raise StoreValidationError("source kind is not supported")
        if _PAGE_KEY_PATTERN.fullmatch(page_key) is None:
            raise StoreValidationError("source page key is invalid")
        _validate_source_revision(source_revision)
        native_revision = (
            source_revision if source_native_revision is None else source_native_revision
        )
        _validate_source_revision(native_revision)
        if _SAFE_NAME_PATTERN.fullmatch(contract_name) is None:
            raise StoreValidationError("source contract name is invalid")
        _validate_sha256(contract_sha256)
        if row_count < 0:
            raise StoreValidationError("source page row count cannot be negative")
        _validate_utc(source_timestamp)
        if account_scope is not None:
            _validate_account_scope(account_scope)
        if instrument_handle is not None:
            _validate_handle(instrument_handle, "ih")
        if instrument_scope_sha256 is not None:
            _validate_sha256(instrument_scope_sha256)

        payload_json = _canonical_json(dict(payload))
        payload_sha256 = _fingerprint(payload_json)
        logical_key_sha256 = _fingerprint(
            _canonical_json(
                {
                    "account_scope": account_scope,
                    "instrument_handle": instrument_handle,
                    "instrument_scope_sha256": instrument_scope_sha256,
                    "page_key": page_key,
                    "source_kind": source_kind,
                    "source_revision": source_revision,
                },
            ),
        )
        material_json = _canonical_json(
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
                "source_native_revision": native_revision,
                "source_timestamp": source_timestamp.isoformat(),
            },
        )
        fingerprint_sha256 = _fingerprint(material_json)
        page_id = f"sp_{fingerprint_sha256}"
        contract_id = f"sc_{_fingerprint(f'{contract_name}:{contract_sha256}')}"
        byte_count = len(payload_json.encode())

        with self._write_connection() as connection:
            self._require_instrument_reference(connection, instrument_handle)
            existing = self._source_page_by_logical_key(
                connection,
                logical_key_sha256,
            )
            if existing is not None:
                if existing.fingerprint_sha256 != fingerprint_sha256:
                    raise StoreConflictError(
                        "source page logical revision identifies different material",
                    )
                return existing
            self._ensure_capacity(byte_count)
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO source_contracts (
                    contract_id,
                    source_scope,
                    contract_name,
                    contract_sha256,
                    first_seen_at
                )
                VALUES (?, 'saxo_openapi', ?, ?, ?)
                ON CONFLICT (contract_id) DO NOTHING
                """,
                (contract_id, contract_name, contract_sha256, now),
            )
            connection.execute(
                """
                INSERT INTO source_pages (
                    page_id,
                    source_kind,
                    page_key,
                    source_revision,
                    source_native_revision,
                    contract_id,
                    account_scope,
                    instrument_handle,
                    instrument_scope_sha256,
                    source_timestamp,
                    ingested_at,
                    row_count,
                    byte_count,
                    logical_key_sha256,
                    fingerprint_sha256,
                    payload_sha256,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page_id,
                    source_kind,
                    page_key,
                    source_revision,
                    native_revision,
                    contract_id,
                    account_scope,
                    instrument_handle,
                    instrument_scope_sha256,
                    source_timestamp,
                    now,
                    row_count,
                    byte_count,
                    logical_key_sha256,
                    fingerprint_sha256,
                    payload_sha256,
                    payload_json,
                ),
            )
            self._bump_revision(connection)
            stored = self._source_page_by_id(connection, page_id)
            if stored is None:
                raise StoreError("stored source page cannot be read back")
            return stored

    def ingest_source_capture(  # noqa: C901
        self,
        envelope: SourceCaptureEnvelope,
    ) -> StoredSourceCapture:
        """Persist one exact, validated provider capture and derive its dataset."""
        try:
            authenticated = SourceCaptureEnvelope.model_validate_json(
                envelope.model_dump_json(),
                strict=True,
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as error:
            raise StoreValidationError(
                "source capture envelope authentication failed",
            ) from error
        source_pages = authenticated.pages
        capture_revisions = {page.capture_revision for page in source_pages}
        account_scopes = {page.account_scope for page in source_pages}
        instrument_scopes = {page.instrument_scope_sha256 for page in source_pages}
        source_timestamps = {page.source_timestamp for page in source_pages}
        if len(capture_revisions) != 1:
            raise StoreValidationError("source capture mixes capture revisions")
        if len(account_scopes) != 1:
            raise StoreValidationError("source capture mixes account scopes")
        if len(instrument_scopes) != 1:
            raise StoreValidationError("source capture mixes instrument scopes")
        if len(source_timestamps) != 1:
            raise StoreValidationError("source capture mixes capture timestamps")

        page_keys = {(page.contract_id, page.page_number) for page in source_pages}
        if len(page_keys) != len(source_pages):
            raise StoreValidationError("source capture repeats a contract page")
        for contract_id in {page.contract_id for page in source_pages}:
            contract_pages = sorted(
                (page for page in source_pages if page.contract_id == contract_id),
                key=lambda page: page.page_number,
            )
            if [page.page_number for page in contract_pages] != list(
                range(1, len(contract_pages) + 1),
            ):
                raise StoreValidationError("source capture page numbering is not contiguous")
            if len({page.source_revision for page in contract_pages}) != 1:
                raise StoreValidationError("source contract changes revision within a capture")

        stored_pages: list[StoredSourcePage] = []
        with self.transaction():
            for page in sorted(
                source_pages,
                key=lambda item: (item.contract_id, item.page_number),
            ):
                contract = source_contracts_by_id().get(page.contract_id)
                serialized = page.model_dump(mode="json")
                rows = serialized.get("rows")
                if (
                    contract is None
                    or page.operation_id != contract.operation_id
                    or page.source_kind != contract.source_kind
                    or page.contract_sha256 != source_contract_fingerprint(contract)
                    or not page.schema_comparison.compatible
                    or not isinstance(rows, list)
                    or source_page_fingerprint(cast("list[Mapping[str, object]]", rows))
                    != page.page_fingerprint_sha256
                ):
                    raise StoreValidationError("source page does not match validated provider data")
                payload: dict[str, SourceJsonValue] = {
                    "contract_id": page.contract_id,
                    "data_version": page.data_version,
                    "page_fingerprint_sha256": page.page_fingerprint_sha256,
                    "page_number": page.page_number,
                    "request_fingerprint_sha256": page.request_fingerprint_sha256,
                    "rows": cast("list[SourceJsonValue]", rows),
                    "source_native_revision": page.source_revision,
                    "source_quality": cast(
                        "dict[str, SourceJsonValue]",
                        page.source_quality.model_dump(mode="json"),
                    ),
                }
                provider_identity_sha256 = _fingerprint(
                    _canonical_json(
                        {
                            "account_scope": page.account_scope,
                            "capture_revision": page.capture_revision,
                            "contract_id": page.contract_id,
                            "contract_sha256": page.contract_sha256,
                            "instrument_scope_sha256": page.instrument_scope_sha256,
                            "page_number": page.page_number,
                            "request_fingerprint_sha256": (page.request_fingerprint_sha256),
                            "source_native_revision": page.source_revision,
                        },
                    ),
                )
                stored_pages.append(
                    self.put_source_page(
                        source_kind=page.source_kind,
                        page_key=f"{page.contract_id}:{page.page_number}:{provider_identity_sha256}",
                        source_revision=page.capture_revision,
                        source_native_revision=page.source_revision,
                        contract_name=page.contract_id,
                        contract_sha256=page.contract_sha256,
                        payload=payload,
                        row_count=page.row_count,
                        source_timestamp=page.source_timestamp,
                        account_scope=page.account_scope,
                        instrument_handle=None,
                        instrument_scope_sha256=page.instrument_scope_sha256,
                    ),
                )
            capture_revision = next(iter(capture_revisions))
            account_scope = next(iter(account_scopes))
            captured_at = next(iter(source_timestamps))
            dataset_id = _capture_dataset_id(
                capture_revision,
                tuple(page.page_id for page in stored_pages),
            )
            dataset = self.create_dataset(
                dataset_id=dataset_id,
                account_scope=account_scope,
                source_scope="saxo_openapi",
                source_revision=capture_revision,
                source_page_ids=tuple(page.page_id for page in stored_pages),
                created_at=captured_at,
                coverage_start=captured_at,
                coverage_end=captured_at,
                quality_state=(
                    QualityState.PARTIAL
                    if any(page.source_quality.state == "limited" for page in source_pages)
                    else QualityState.COMPLETE
                ),
            )
        return StoredSourceCapture(pages=tuple(stored_pages), dataset=dataset)

    @staticmethod
    def _require_instrument_reference(
        connection: duckdb.DuckDBPyConnection,
        instrument_handle: str | None,
    ) -> None:
        if instrument_handle is None:
            return
        row = connection.execute(
            "SELECT count(*) FROM safe_instruments WHERE instrument_handle = ?",
            (instrument_handle,),
        ).fetchone()
        if row is None:
            raise StoreError("instrument reference count is missing")
        if _require_int(row[0]) != 1:
            raise StoreNotFoundError("source page instrument does not exist")

    @staticmethod
    def _source_page_by_logical_key(
        connection: duckdb.DuckDBPyConnection,
        logical_key_sha256: str,
    ) -> StoredSourcePage | None:
        row = connection.execute(
            "SELECT page_id FROM source_pages WHERE logical_key_sha256 = ?",
            (logical_key_sha256,),
        ).fetchone()
        if row is None:
            return None
        return AnalyticsStore._source_page_by_id(
            connection,
            _require_str(row[0]),
        )

    @staticmethod
    def _source_page_by_id(
        connection: duckdb.DuckDBPyConnection,
        page_id: str,
    ) -> StoredSourcePage | None:
        row = connection.execute(
            """
            SELECT
                page_id,
                source_kind,
                page_key,
                source_revision,
                source_native_revision,
                instrument_scope_sha256,
                fingerprint_sha256,
                row_count,
                byte_count,
                epoch_us(source_timestamp),
                epoch_us(ingested_at)
            FROM source_pages
            WHERE page_id = ?
            """,
            (page_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredSourcePage(
            page_id=_require_str(row[0]),
            source_kind=_require_str(row[1]),
            page_key=_require_str(row[2]),
            source_revision=_require_str(row[3]),
            source_native_revision=_require_str(row[4]),
            instrument_scope_sha256=_optional_str(row[5]),
            fingerprint_sha256=_require_str(row[6]),
            row_count=_require_int(row[7]),
            byte_count=_require_int(row[8]),
            source_timestamp=_require_datetime(row[9]),
            ingested_at=_require_datetime(row[10]),
        )

    @staticmethod
    def _validated_dataset_source_rows(
        connection: duckdb.DuckDBPyConnection,
        page_ids: Sequence[str],
        account_scope: str,
        source_revision: str | None,
    ) -> list[tuple[object, ...]]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    p.page_id,
                    p.fingerprint_sha256,
                    p.row_count,
                    p.byte_count,
                    p.account_scope,
                    p.source_revision,
                    c.contract_sha256,
                    p.instrument_handle,
                    i.instrument_handle,
                    p.payload_json,
                    c.contract_name,
                    p.source_kind,
                    p.page_key,
                    p.source_native_revision,
                    p.instrument_scope_sha256,
                    epoch_us(p.source_timestamp),
                    p.payload_sha256,
                    p.contract_id,
                    c.source_scope,
                    p.logical_key_sha256
                FROM source_pages AS p
                LEFT JOIN source_contracts AS c ON c.contract_id = p.contract_id
                LEFT JOIN safe_instruments AS i
                    ON i.instrument_handle = p.instrument_handle
                WHERE p.page_id = ANY(?)
                ORDER BY p.page_id
                """,
                (list(page_ids),),
            ).fetchall(),
        )
        if len(rows) != len(page_ids):
            raise StoreNotFoundError("dataset references an absent source page")
        if any(row[6] is None for row in rows):
            raise StoreNotFoundError("dataset source contract does not exist")
        if any(row[7] is not None and row[8] is None for row in rows):
            raise StoreNotFoundError("dataset instrument does not exist")
        if any(
            (page_scope := _optional_str(row[4])) is not None and page_scope != account_scope
            for row in rows
        ):
            raise StoreValidationError(
                "dataset account scope does not match its source pages",
            )
        if source_revision is not None and any(
            _require_str(row[5]) != source_revision for row in rows
        ):
            raise StoreValidationError(
                "dataset source revision does not match its source pages",
            )
        for row in rows:
            AnalyticsStore._validate_bound_source_page_identity(row)
        return rows

    @staticmethod
    def _validate_bound_source_page_identity(row: tuple[object, ...]) -> None:
        try:
            page_id = _require_str(row[0])
            fingerprint_sha256 = _require_str(row[1])
            row_count = _require_int(row[2])
            byte_count = _require_int(row[3])
            account_scope = _optional_str(row[4])
            source_revision = _require_str(row[5])
            contract_sha256 = _require_str(row[6])
            instrument_handle = _optional_str(row[7])
            payload_json = _require_str(row[9])
            contract_name = _require_str(row[10])
            source_kind = _require_str(row[11])
            page_key = _require_str(row[12])
            source_native_revision = _require_str(row[13])
            instrument_scope_sha256 = _optional_str(row[14])
            source_timestamp = _require_datetime(row[15])
            payload_sha256 = _require_str(row[16])
            contract_id = _require_str(row[17])
            source_scope = _require_str(row[18])
            logical_key_sha256 = _require_str(row[19])
            loaded_payload = json.loads(payload_json)
        except (IndexError, StoreError, TypeError, ValueError) as error:
            raise StoreValidationError(
                "dataset source page integrity check failed",
            ) from error
        if not isinstance(loaded_payload, dict):
            raise StoreValidationError("dataset source page integrity check failed")
        payload = cast("dict[str, object]", loaded_payload)
        canonical_payload = _canonical_json(payload)
        raw_rows = payload.get("rows")
        if raw_rows is not None and not isinstance(raw_rows, list):
            raise StoreValidationError("dataset source page integrity check failed")
        source_rows = [] if raw_rows is None else cast("list[object]", raw_rows)
        contract = source_contracts_by_id().get(contract_name)
        if contract is not None and (
            contract.contract_id != contract_name
            or contract.source_kind != source_kind
            or source_contract_fingerprint(contract) != contract_sha256
        ):
            raise StoreValidationError(
                "persisted source contract metadata is invalid",
            )
        if (
            source_scope != "saxo_openapi"
            or contract_id != f"sc_{_fingerprint(f'{contract_name}:{contract_sha256}')}"
            or ("contract_id" in payload and payload.get("contract_id") != contract_name)
            or any(not isinstance(source_row, dict) for source_row in source_rows)
            or (raw_rows is not None and len(source_rows) != row_count)
            or canonical_payload != payload_json
            or len(payload_json.encode()) != byte_count
            or _fingerprint(payload_json) != payload_sha256
        ):
            raise StoreValidationError("dataset source page integrity check failed")
        if (
            "source_native_revision" in payload
            and payload.get("source_native_revision") != source_native_revision
        ):
            raise StoreValidationError("dataset source page integrity check failed")
        raw_page_fingerprint = payload.get("page_fingerprint_sha256")
        if raw_page_fingerprint is not None and (
            raw_rows is None
            or not isinstance(raw_page_fingerprint, str)
            or source_page_fingerprint(
                cast("list[Mapping[str, object]]", source_rows),
            )
            != raw_page_fingerprint
        ):
            raise StoreValidationError("dataset source page integrity check failed")
        material_json = _canonical_json(
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
        logical_key_json = _canonical_json(
            {
                "account_scope": account_scope,
                "instrument_handle": instrument_handle,
                "instrument_scope_sha256": instrument_scope_sha256,
                "page_key": page_key,
                "source_kind": source_kind,
                "source_revision": source_revision,
            },
        )
        reconstructed_fingerprint = _fingerprint(material_json)
        if (
            fingerprint_sha256 != reconstructed_fingerprint
            or page_id != f"sp_{reconstructed_fingerprint}"
            or logical_key_sha256 != _fingerprint(logical_key_json)
        ):
            raise StoreValidationError("dataset source page integrity check failed")

    @staticmethod
    def _bound_dataset_quality_state(
        rows: Sequence[tuple[object, ...]],
        requested: QualityState,
    ) -> QualityState:
        has_limited_source = False
        for row in rows:
            is_info_price = AnalyticsStore._is_exact_info_price_contract(row)
            try:
                raw_payload = json.loads(_require_str(row[9]))
            except (TypeError, ValueError) as error:
                raise StoreValidationError(
                    "persisted source quality metadata is invalid",
                ) from error
            if not isinstance(raw_payload, dict):
                raise StoreValidationError(
                    "persisted source quality metadata is invalid",
                )
            payload = cast("dict[str, object]", raw_payload)
            source_quality = payload.get("source_quality")
            if source_quality is None:
                if is_info_price:
                    raise StoreValidationError(
                        "persisted source quality metadata is invalid",
                    )
                continue
            try:
                proof = SourceQualityProof.model_validate(source_quality, strict=True)
            except ValidationError as error:
                raise StoreValidationError(
                    "persisted source quality metadata is invalid",
                ) from error
            if is_info_price and proof != AnalyticsStore._canonical_persisted_info_price_quality(
                row, payload
            ):
                raise StoreValidationError(
                    "persisted source quality metadata is invalid",
                )
            has_limited_source = has_limited_source or proof.state == "limited"
        if has_limited_source and requested is QualityState.COMPLETE:
            return QualityState.PARTIAL
        return requested

    @staticmethod
    def _is_exact_info_price_contract(row: tuple[object, ...]) -> bool:
        contract = source_contracts_by_id()[_INFO_PRICE_CONTRACT_ID]
        name_matches = _require_str(row[10]) == contract.contract_id
        sha_matches = _require_str(row[6]) == source_contract_fingerprint(contract)
        if name_matches != sha_matches:
            raise StoreValidationError(
                "persisted source contract metadata is invalid",
            )
        return name_matches

    @staticmethod
    def _canonical_persisted_info_price_quality(
        row: tuple[object, ...],
        payload: Mapping[str, object],
    ) -> SourceQualityProof:
        contract = source_contracts_by_id()[_INFO_PRICE_CONTRACT_ID]
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            raise StoreValidationError(
                "persisted source quality metadata is invalid",
            )
        stored_rows = cast("list[object]", raw_rows)
        if (
            _require_str(row[6]) != source_contract_fingerprint(contract)
            or len(stored_rows) != _require_int(row[2])
            or len(stored_rows) != 1
            or not isinstance(stored_rows[0], dict)
        ):
            raise StoreValidationError(
                "persisted source quality metadata is invalid",
            )
        quote_row = cast("dict[str, object]", stored_rows[0])
        comparison = compare_source_schema(contract, quote_row)
        if not comparison.compatible:
            raise StoreValidationError(
                "persisted source quality metadata is invalid",
            )
        return source_quality_proof(contract, (quote_row,), comparison)

    def create_dataset(  # noqa: PLR0913
        self,
        *,
        dataset_id: str,
        account_scope: str,
        source_scope: str,
        source_revision: str,
        source_page_ids: Sequence[str],
        lineage_source_page_ids: Sequence[str] = (),
        created_at: datetime,
        coverage_start: datetime,
        coverage_end: datetime,
        quality_state: QualityState,
    ) -> StoredDataset:
        """Create one immutable dataset from a deterministic source-page set."""
        _validate_handle(dataset_id, "ds")
        _validate_account_scope(account_scope)
        if source_scope != "saxo_openapi":
            raise StoreValidationError("Saxo OpenAPI is the only source scope")
        _validate_source_revision(source_revision)
        _validate_utc(created_at)
        _validate_utc(coverage_start)
        _validate_utc(coverage_end)
        if coverage_end < coverage_start:
            raise StoreValidationError("dataset coverage end precedes its start")
        current_page_ids = tuple(sorted(set(source_page_ids)))
        lineage_page_ids = tuple(
            sorted(set(lineage_source_page_ids).difference(current_page_ids)),
        )
        page_ids = tuple(sorted((*current_page_ids, *lineage_page_ids)))
        if not current_page_ids:
            raise StoreValidationError("dataset requires at least one source page")
        if any(re.fullmatch(r"sp_[a-f0-9]{64}", page_id) is None for page_id in page_ids):
            raise StoreValidationError("dataset source page identifier is invalid")

        with self._write_connection() as connection:
            current_rows = self._validated_dataset_source_rows(
                connection,
                current_page_ids,
                account_scope,
                source_revision,
            )
            lineage_rows = self._validated_dataset_source_rows(
                connection,
                lineage_page_ids,
                account_scope,
                None,
            )
            rows = sorted((*current_rows, *lineage_rows), key=lambda row: _require_str(row[0]))
            bound_quality_state = self._bound_dataset_quality_state(rows, quality_state)
            row_count = sum(_require_int(row[2]) for row in rows)
            byte_count = sum(_require_int(row[3]) for row in rows)
            material_json = _canonical_json(
                {
                    "account_scope": account_scope,
                    "coverage_end": coverage_end.isoformat(),
                    "coverage_start": coverage_start.isoformat(),
                    "pages": [
                        {
                            "page_id": _require_str(row[0]),
                            "page_fingerprint_sha256": _require_str(row[1]),
                            "source_contract_sha256": _require_str(row[6]),
                        }
                        for row in rows
                    ],
                    "quality_state": bound_quality_state.value,
                    "source_revision": source_revision,
                    "source_scope": source_scope,
                },
            )
            fingerprint_sha256 = _fingerprint(material_json)
            existing = self._dataset_by_id(connection, dataset_id)
            if existing is not None:
                if existing.fingerprint_sha256 != fingerprint_sha256:
                    raise StoreConflictError(
                        "dataset handle already identifies different content",
                    )
                return existing
            connection.execute(
                """
                INSERT INTO datasets (
                    dataset_id,
                    account_scope,
                    source_scope,
                    source_revision,
                    created_at,
                    coverage_start,
                    coverage_end,
                    quality_state,
                    row_count,
                    byte_count,
                    fingerprint_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_id,
                    account_scope,
                    source_scope,
                    source_revision,
                    created_at,
                    coverage_start,
                    coverage_end,
                    bound_quality_state.value,
                    row_count,
                    byte_count,
                    fingerprint_sha256,
                ),
            )
            connection.executemany(
                """
                INSERT INTO dataset_source_pages (dataset_id, page_id)
                VALUES (?, ?)
                """,
                [(dataset_id, page_id) for page_id in page_ids],
            )
            self._bump_revision(connection)
            stored = self._dataset_by_id(connection, dataset_id)
            if stored is None:
                raise StoreError("stored dataset cannot be read back")
            return stored

    @staticmethod
    def authenticate_dataset(
        connection: duckdb.DuckDBPyConnection,
        dataset_id: str,
    ) -> StoredDataset:
        """Recompute one stored dataset and every page bound to it."""
        _validate_handle(dataset_id, "ds")
        row = connection.execute(
            """
            SELECT dataset_id, account_scope, source_scope, source_revision,
                   epoch_us(created_at), epoch_us(coverage_start),
                   epoch_us(coverage_end), quality_state, row_count,
                   byte_count, fingerprint_sha256
            FROM datasets
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if row is None:
            raise StoreNotFoundError("dataset does not exist")
        try:
            stored_id = _require_str(row[0])
            account_scope = _require_str(row[1])
            source_scope = _require_str(row[2])
            source_revision = _require_str(row[3])
            created_at = _require_datetime(row[4])
            coverage_start = _require_datetime(row[5])
            coverage_end = _require_datetime(row[6])
            quality_state = QualityState(_require_str(row[7]))
            row_count = _require_int(row[8])
            byte_count = _require_int(row[9])
            fingerprint_sha256 = _require_str(row[10])
        except (IndexError, StoreError, ValueError) as error:
            raise StoreValidationError("dataset integrity check failed") from error
        try:
            _validate_account_scope(account_scope)
            _validate_source_revision(source_revision)
            _validate_utc(created_at)
            _validate_utc(coverage_start)
            _validate_utc(coverage_end)
            _validate_sha256(fingerprint_sha256)
        except StoreValidationError as error:
            raise StoreValidationError("dataset integrity check failed") from error
        if (
            stored_id != dataset_id
            or source_scope != "saxo_openapi"
            or coverage_end < coverage_start
            or row_count < 0
            or byte_count < 0
        ):
            raise StoreValidationError("dataset integrity check failed")
        raw_page_ids = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT page_id
                FROM dataset_source_pages
                WHERE dataset_id = ?
                ORDER BY page_id
                """,
                (dataset_id,),
            ).fetchall(),
        )
        page_ids = tuple(_require_str(page[0]) for page in raw_page_ids)
        if not page_ids or page_ids != tuple(sorted(set(page_ids))):
            raise StoreValidationError("dataset integrity check failed")
        rows = AnalyticsStore._validated_dataset_source_rows(
            connection,
            page_ids,
            account_scope,
            None,
        )
        if not any(_require_str(page[5]) == source_revision for page in rows):
            raise StoreValidationError("dataset integrity check failed")
        bound_quality = AnalyticsStore._bound_dataset_quality_state(rows, quality_state)
        recomputed_row_count = sum(_require_int(page[2]) for page in rows)
        recomputed_byte_count = sum(_require_int(page[3]) for page in rows)
        material_json = _canonical_json(
            {
                "account_scope": account_scope,
                "coverage_end": coverage_end.isoformat(),
                "coverage_start": coverage_start.isoformat(),
                "pages": [
                    {
                        "page_id": _require_str(page[0]),
                        "page_fingerprint_sha256": _require_str(page[1]),
                        "source_contract_sha256": _require_str(page[6]),
                    }
                    for page in rows
                ],
                "quality_state": bound_quality.value,
                "source_revision": source_revision,
                "source_scope": source_scope,
            },
        )
        if (
            bound_quality is not quality_state
            or row_count != recomputed_row_count
            or byte_count != recomputed_byte_count
        ):
            raise StoreValidationError("dataset integrity check failed")
        if fingerprint_sha256 != _fingerprint(material_json):
            raise StoreConflictError("dataset fingerprint does not match bound pages")
        return StoredDataset(
            dataset_id=stored_id,
            source_revision=source_revision,
            fingerprint_sha256=fingerprint_sha256,
            row_count=row_count,
            byte_count=byte_count,
            created_at=created_at,
            quality_state=quality_state,
        )

    @staticmethod
    def _dataset_by_id(
        connection: duckdb.DuckDBPyConnection,
        dataset_id: str,
    ) -> StoredDataset | None:
        row = connection.execute(
            """
            SELECT
                dataset_id,
                source_revision,
                fingerprint_sha256,
                row_count,
                byte_count,
                epoch_us(created_at),
                quality_state
            FROM datasets
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredDataset(
            dataset_id=_require_str(row[0]),
            source_revision=_require_str(row[1]),
            fingerprint_sha256=_require_str(row[2]),
            row_count=_require_int(row[3]),
            byte_count=_require_int(row[4]),
            created_at=_require_datetime(row[5]),
            quality_state=QualityState(_require_str(row[6])),
        )

    @staticmethod
    def _dataset_binding(
        connection: duckdb.DuckDBPyConnection,
        dataset_id: str,
    ) -> _DatasetBinding | None:
        row = connection.execute(
            """
            SELECT account_scope, source_scope, source_revision
            FROM datasets
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if row is None:
            return None
        return _DatasetBinding(
            account_scope=_require_str(row[0]),
            source_scope=_require_str(row[1]),
            source_revision=_require_str(row[2]),
        )

    @staticmethod
    def _dataset_contracts(
        connection: duckdb.DuckDBPyConnection,
        dataset_id: str,
    ) -> frozenset[str]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT DISTINCT c.contract_sha256
                FROM dataset_source_pages AS d
                LEFT JOIN source_pages AS p ON p.page_id = d.page_id
                LEFT JOIN source_contracts AS c ON c.contract_id = p.contract_id
                WHERE d.dataset_id = ?
                """,
                (dataset_id,),
            ).fetchall(),
        )
        if any(row[0] is None for row in rows):
            raise StoreNotFoundError(
                "analysis dataset source contract does not exist",
            )
        return frozenset(_require_str(row[0]) for row in rows)

    def create_snapshot(  # noqa: PLR0913
        self,
        *,
        snapshot_id: str,
        dataset_id: str,
        snapshot_kind: str,
        account_scope: str,
        source_revision: str,
        as_of: datetime,
        payload: Mapping[str, object],
    ) -> StoredSnapshot:
        """Store one immutable account snapshot linked to its dataset."""
        _validate_handle(snapshot_id, "ps")
        _validate_handle(dataset_id, "ds")
        if _SAFE_NAME_PATTERN.fullmatch(snapshot_kind) is None:
            raise StoreValidationError("snapshot kind is invalid")
        _validate_account_scope(account_scope)
        _validate_source_revision(source_revision)
        _validate_utc(as_of)
        payload_json = _canonical_json(dict(payload))
        byte_count = len(payload_json.encode())
        fingerprint_sha256 = _fingerprint(
            _canonical_json(
                {
                    "account_scope": account_scope,
                    "as_of": as_of.isoformat(),
                    "dataset_id": dataset_id,
                    "payload_sha256": _fingerprint(payload_json),
                    "snapshot_kind": snapshot_kind,
                    "source_revision": source_revision,
                },
            ),
        )
        with self._write_connection() as connection:
            existing = self._snapshot_by_id(connection, snapshot_id)
            if existing is not None:
                if existing.fingerprint_sha256 != fingerprint_sha256:
                    raise StoreConflictError(
                        "snapshot handle already identifies different content",
                    )
                return existing
            dataset_binding = self._dataset_binding(connection, dataset_id)
            if dataset_binding is None:
                raise StoreNotFoundError("snapshot dataset does not exist")
            if dataset_binding.account_scope != account_scope:
                raise StoreValidationError(
                    "snapshot account scope does not match its dataset",
                )
            if dataset_binding.source_revision != source_revision:
                raise StoreValidationError(
                    "snapshot source revision does not match its dataset",
                )
            self._ensure_capacity(byte_count)
            created_order = self._revision(connection) + 1
            connection.execute(
                """
                INSERT INTO account_snapshots (
                    snapshot_id,
                    dataset_id,
                    page_id,
                    snapshot_kind,
                    account_scope,
                    source_revision,
                    as_of,
                    created_order,
                    byte_count,
                    fingerprint_sha256,
                    payload_json
                )
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    dataset_id,
                    snapshot_kind,
                    account_scope,
                    source_revision,
                    as_of,
                    created_order,
                    byte_count,
                    fingerprint_sha256,
                    payload_json,
                ),
            )
            self._bump_revision(connection)
            stored = self._snapshot_by_id(connection, snapshot_id)
            if stored is None:
                raise StoreError("stored snapshot cannot be read back")
            return stored

    @staticmethod
    def _snapshot_by_id(
        connection: duckdb.DuckDBPyConnection,
        snapshot_id: str,
    ) -> StoredSnapshot | None:
        row = connection.execute(
            """
            SELECT
                snapshot_id,
                dataset_id,
                fingerprint_sha256,
                byte_count,
                epoch_us(as_of)
            FROM account_snapshots
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredSnapshot(
            snapshot_id=_require_str(row[0]),
            dataset_id=_require_str(row[1]),
            fingerprint_sha256=_require_str(row[2]),
            byte_count=_require_int(row[3]),
            as_of=_require_datetime(row[4]),
        )

    def put_analysis(self, result: AnalysisResult) -> StoredAnalysis:
        """Persist one strict analysis plus normalized metrics and proof receipts."""
        result = AnalysisResult.model_validate(result)
        result_json = _canonical_json(result.model_dump(mode="json"))
        fingerprint_sha256 = _fingerprint(result_json)
        byte_count = len(result_json.encode())
        created_at = _utc_now()
        with self._write_connection() as connection:
            self._validate_analysis_references(connection, result)
            existing = self._analysis_by_id(connection, result.analysis_id)
            if existing is not None:
                if existing.fingerprint_sha256 != fingerprint_sha256:
                    raise StoreConflictError(
                        "analysis handle already identifies different content",
                    )
                return existing
            self._ensure_capacity(byte_count)
            connection.execute(
                """
                INSERT INTO analyses (
                    analysis_id,
                    dataset_id,
                    account_scope,
                    analysis_kind,
                    status,
                    source_revision,
                    as_of,
                    created_at,
                    byte_count,
                    fingerprint_sha256,
                    result_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.analysis_id,
                    result.provenance.dataset_id,
                    result.account_scope,
                    result.analysis_kind,
                    result.status.value,
                    result.provenance.source_revision,
                    result.as_of,
                    created_at,
                    byte_count,
                    fingerprint_sha256,
                    result_json,
                ),
            )
            connection.executemany(
                """
                INSERT INTO metrics (
                    analysis_id,
                    metric_id,
                    value,
                    unit,
                    unit_class,
                    currency,
                    metric_class,
                    source_timestamp,
                    proof_profile_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.analysis_id,
                        metric.metric_id,
                        metric.value,
                        metric.unit,
                        metric.unit_class.value,
                        metric.currency,
                        metric.metric_class.value,
                        metric.source_timestamp,
                        metric.proof_profile_id,
                    )
                    for metric in result.metrics
                ],
            )
            connection.executemany(
                """
                INSERT INTO proof_receipts (
                    analysis_id,
                    proof_profile_id,
                    analysis_kind,
                    source_binding_json,
                    engine_binding_json,
                    checks_passed_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.analysis_id,
                        receipt.proof_profile_id,
                        receipt.analysis_kind,
                        _canonical_json(receipt.source_binding.model_dump(mode="json")),
                        _canonical_json(receipt.engine_binding.model_dump(mode="json")),
                        _canonical_json(receipt.checks_passed),
                    )
                    for receipt in result.provenance.proof_receipts
                ],
            )
            self._bump_revision(connection)
            stored = self._analysis_by_id(connection, result.analysis_id)
            if stored is None:
                raise StoreError("stored analysis cannot be read back")
            return stored

    def _validate_analysis_references(
        self,
        connection: duckdb.DuckDBPyConnection,
        result: AnalysisResult,
    ) -> None:
        dataset_id = result.provenance.dataset_id
        dataset_binding = self._dataset_binding(connection, dataset_id)
        if dataset_binding is None:
            raise StoreNotFoundError("analysis dataset does not exist")
        if (
            dataset_binding.account_scope != result.account_scope
            or dataset_binding.source_scope != result.provenance.source_scope
            or dataset_binding.source_revision != result.provenance.source_revision
        ):
            raise StoreValidationError(
                "analysis provenance does not match its dataset",
            )
        contracts = self._dataset_contracts(connection, dataset_id)
        if not contracts:
            raise StoreNotFoundError("analysis dataset source contract does not exist")
        self._validate_analysis_contract_binding(result, contracts)
        request = result.request
        if isinstance(request, InstrumentAnalysisRequest):
            self._validate_analysis_instruments(
                connection,
                dataset_id,
                request.instrument_handles,
            )
        elif isinstance(request, PortfolioAnalysisRequest):
            snapshot = self._snapshot_by_id(
                connection,
                request.portfolio_snapshot_id,
            )
            if snapshot is None:
                raise StoreNotFoundError("analysis snapshot does not exist")
            if snapshot.dataset_id != dataset_id:
                raise StoreValidationError(
                    "analysis snapshot does not belong to its dataset",
                )

    @staticmethod
    def _validate_analysis_contract_binding(
        result: AnalysisResult,
        contracts: frozenset[str],
    ) -> None:
        expected = tuple(sorted(contracts))
        supplied = result.provenance.source_contract_sha256s
        if len(expected) == 1:
            if supplied not in {(), expected}:
                raise StoreValidationError(
                    "analysis source contract set does not match its dataset",
                )
            if result.provenance.source_contract_sha256 != expected[0]:
                raise StoreValidationError(
                    "analysis source contract set fingerprint is invalid",
                )
            return
        if supplied != expected:
            raise StoreValidationError(
                "analysis source contract set does not match its dataset",
            )
        expected_fingerprint = _fingerprint(
            _canonical_json(
                {
                    "source_contract_sha256s": list(expected),
                },
            )
        )
        if result.provenance.source_contract_sha256 != expected_fingerprint:
            raise StoreValidationError(
                "analysis source contract set fingerprint is invalid",
            )

    @staticmethod
    def _validate_analysis_instruments(
        connection: duckdb.DuckDBPyConnection,
        dataset_id: str,
        instrument_handles: Sequence[str],
    ) -> None:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT instrument_handle
                FROM safe_instruments
                WHERE instrument_handle = ANY(?)
                """,
                (list(instrument_handles),),
            ).fetchall(),
        )
        existing = {_require_str(row[0]) for row in rows}
        requested = set(instrument_handles)
        if existing != requested:
            raise StoreNotFoundError("analysis instrument does not exist")
        dataset_rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT DISTINCT p.instrument_handle
                FROM dataset_source_pages AS d
                JOIN source_pages AS p ON p.page_id = d.page_id
                WHERE d.dataset_id = ? AND p.instrument_handle IS NOT NULL
                """,
                (dataset_id,),
            ).fetchall(),
        )
        dataset_instruments = {_require_str(row[0]) for row in dataset_rows}
        if not requested.issubset(dataset_instruments):
            raise StoreValidationError(
                "analysis instruments do not belong to its dataset",
            )

    @staticmethod
    def _analysis_by_id(
        connection: duckdb.DuckDBPyConnection,
        analysis_id: str,
    ) -> StoredAnalysis | None:
        row = connection.execute(
            """
            SELECT
                analysis_id,
                dataset_id,
                fingerprint_sha256,
                byte_count,
                epoch_us(created_at)
            FROM analyses
            WHERE analysis_id = ?
            """,
            (analysis_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredAnalysis(
            analysis_id=_require_str(row[0]),
            dataset_id=_require_str(row[1]),
            fingerprint_sha256=_require_str(row[2]),
            byte_count=_require_int(row[3]),
            created_at=_require_datetime(row[4]),
        )

    def put_artifact(self, artifact: ArtifactSummary) -> StoredArtifact:
        """Persist typed artifact metadata without accepting a caller path."""
        with self._write_connection() as connection:
            existing = self._artifact_by_id(connection, artifact.artifact_id)
            if existing is not None:
                if (
                    existing.sha256 != artifact.sha256
                    or existing.byte_count != artifact.byte_count
                    or existing.analysis_id != artifact.analysis_id
                ):
                    raise StoreConflictError(
                        "artifact handle already identifies different content",
                    )
                return existing
            if self._analysis_by_id(connection, artifact.analysis_id) is None:
                raise StoreNotFoundError("artifact analysis does not exist")
            self._ensure_capacity(artifact.byte_count)
            connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id,
                    analysis_id,
                    media_type,
                    byte_count,
                    sha256,
                    created_at,
                    description,
                    visibility
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.analysis_id,
                    artifact.media_type,
                    artifact.byte_count,
                    artifact.sha256,
                    artifact.created_at,
                    artifact.description,
                    artifact.visibility.value,
                ),
            )
            self._bump_revision(connection)
            stored = self._artifact_by_id(connection, artifact.artifact_id)
            if stored is None:
                raise StoreError("stored artifact cannot be read back")
            return stored

    @staticmethod
    def _artifact_by_id(
        connection: duckdb.DuckDBPyConnection,
        artifact_id: str,
    ) -> StoredArtifact | None:
        row = connection.execute(
            """
            SELECT
                artifact_id,
                analysis_id,
                sha256,
                byte_count,
                epoch_us(created_at)
            FROM artifacts
            WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredArtifact(
            artifact_id=_require_str(row[0]),
            analysis_id=_require_str(row[1]),
            sha256=_require_str(row[2]),
            byte_count=_require_int(row[3]),
            created_at=_require_datetime(row[4]),
        )

    def list_storage(self, scope: StorageScope) -> tuple[StorageEntry, ...]:
        """List only safe handles, counts, timestamps, and fingerprints."""
        with self._read_connection() as connection:
            return self._storage_entries(connection, scope)

    def _storage_entries(
        self,
        connection: duckdb.DuckDBPyConnection,
        scope: StorageScope,
    ) -> tuple[StorageEntry, ...]:
        entries = [
            *self._source_page_entries(connection),
            *self._normalized_page_entries(connection),
            *self._dataset_entries(connection),
            *self._snapshot_entries(connection),
            *self._analysis_entries(connection),
            *self._artifact_entries(connection),
            *self._job_entries(connection),
        ]
        if StorageDataType.DELETION_RECEIPTS in scope.data_types:
            entries.extend(self._deletion_receipt_entries(connection))
        return tuple(
            sorted(
                (entry for entry in entries if self._entry_matches_scope(entry, scope)),
                key=lambda entry: (entry.data_type.value, entry.object_id),
            ),
        )

    @staticmethod
    def _source_page_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    page_id,
                    account_scope,
                    instrument_handle,
                    source_revision,
                    epoch_us(source_timestamp),
                    row_count,
                    byte_count,
                    fingerprint_sha256
                FROM source_pages
                """,
            ).fetchall(),
        )
        return [
            StorageEntry(
                data_type=StorageDataType.SOURCE_PAGES,
                object_id=_require_str(row[0]),
                account_scope=_optional_str(row[1]),
                instrument_handle=_optional_str(row[2]),
                source_revision=_require_str(row[3]),
                start_at=_require_datetime(row[4]),
                end_at=_require_datetime(row[4]),
                row_count=_require_int(row[5]),
                byte_count=_require_int(row[6]),
                fingerprint_sha256=_require_str(row[7]),
            )
            for row in rows
        ]

    @staticmethod
    def _normalized_page_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        entries: list[StorageEntry] = []
        for data_type, query in _NORMALIZED_PAGE_QUERIES.items():
            rows = cast(
                "list[tuple[object, ...]]",
                connection.execute(query).fetchall(),
            )
            entries.extend(
                StorageEntry(
                    data_type=data_type,
                    object_id=_require_str(row[0]),
                    account_scope=_optional_str(row[1]),
                    instrument_handle=_optional_str(row[2]),
                    source_revision=_require_str(row[3]),
                    start_at=_require_datetime(row[4]),
                    end_at=_require_datetime(row[5]),
                    row_count=_require_int(row[6]),
                    byte_count=_require_int(row[7]),
                    fingerprint_sha256=_require_str(row[8]),
                )
                for row in rows
            )
        return entries

    @staticmethod
    def _dataset_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    dataset_id,
                    account_scope,
                    source_revision,
                    epoch_us(coverage_start),
                    epoch_us(coverage_end),
                    row_count,
                    byte_count,
                    fingerprint_sha256
                FROM datasets
                """,
            ).fetchall(),
        )
        return [
            StorageEntry(
                data_type=StorageDataType.DATASETS,
                object_id=_require_str(row[0]),
                account_scope=_require_str(row[1]),
                instrument_handle=None,
                source_revision=_require_str(row[2]),
                start_at=_require_datetime(row[3]),
                end_at=_require_datetime(row[4]),
                row_count=_require_int(row[5]),
                byte_count=_require_int(row[6]),
                fingerprint_sha256=_require_str(row[7]),
            )
            for row in rows
        ]

    @staticmethod
    def _snapshot_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    snapshot_id,
                    account_scope,
                    source_revision,
                    epoch_us(as_of),
                    byte_count,
                    fingerprint_sha256
                FROM account_snapshots
                """,
            ).fetchall(),
        )
        return [
            StorageEntry(
                data_type=StorageDataType.ACCOUNT_SNAPSHOTS,
                object_id=_require_str(row[0]),
                account_scope=_require_str(row[1]),
                instrument_handle=None,
                source_revision=_require_str(row[2]),
                start_at=_require_datetime(row[3]),
                end_at=_require_datetime(row[3]),
                row_count=1,
                byte_count=_require_int(row[4]),
                fingerprint_sha256=_require_str(row[5]),
            )
            for row in rows
        ]

    @staticmethod
    def _analysis_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    a.analysis_id,
                    a.account_scope,
                    a.source_revision,
                    epoch_us(a.as_of),
                    count(m.metric_id),
                    a.byte_count,
                    a.fingerprint_sha256
                FROM analyses AS a
                LEFT JOIN metrics AS m ON m.analysis_id = a.analysis_id
                GROUP BY ALL
                """,
            ).fetchall(),
        )
        return [
            StorageEntry(
                data_type=StorageDataType.ANALYSES,
                object_id=_require_str(row[0]),
                account_scope=_require_str(row[1]),
                instrument_handle=None,
                source_revision=_require_str(row[2]),
                start_at=_require_datetime(row[3]),
                end_at=_require_datetime(row[3]),
                row_count=_require_int(row[4]),
                byte_count=_require_int(row[5]),
                fingerprint_sha256=_require_str(row[6]),
            )
            for row in rows
        ]

    @staticmethod
    def _artifact_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    r.artifact_id,
                    a.account_scope,
                    a.source_revision,
                    epoch_us(r.created_at),
                    r.byte_count,
                    r.sha256
                FROM artifacts AS r
                JOIN analyses AS a ON a.analysis_id = r.analysis_id
                """,
            ).fetchall(),
        )
        return [
            StorageEntry(
                data_type=StorageDataType.ARTIFACTS,
                object_id=_require_str(row[0]),
                account_scope=_require_str(row[1]),
                instrument_handle=None,
                source_revision=_require_str(row[2]),
                start_at=_require_datetime(row[3]),
                end_at=_require_datetime(row[3]),
                row_count=1,
                byte_count=_require_int(row[4]),
                fingerprint_sha256=_require_str(row[5]),
            )
            for row in rows
        ]

    @staticmethod
    def _job_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    job_id,
                    epoch_us(created_at),
                    epoch_us(updated_at),
                    request_fingerprint
                FROM jobs
                """,
            ).fetchall(),
        )
        return [
            StorageEntry(
                data_type=StorageDataType.JOBS,
                object_id=_require_str(row[0]),
                account_scope=None,
                instrument_handle=None,
                source_revision=None,
                start_at=_require_datetime(row[1]),
                end_at=_require_datetime(row[2]),
                row_count=1,
                byte_count=0,
                fingerprint_sha256=_require_str(row[3]),
            )
            for row in rows
        ]

    @staticmethod
    def _deletion_receipt_entries(
        connection: duckdb.DuckDBPyConnection,
    ) -> list[StorageEntry]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT
                    receipt_id,
                    scope_fingerprint,
                    epoch_us(deleted_at),
                    table_counts_json,
                    estimated_bytes
                FROM deletion_receipts
                """,
            ).fetchall(),
        )
        entries: list[StorageEntry] = []
        for row in rows:
            counts = _table_counts_from_json(_require_str(row[3]))
            entries.append(
                StorageEntry(
                    data_type=StorageDataType.DELETION_RECEIPTS,
                    object_id=_require_str(row[0]),
                    account_scope=None,
                    instrument_handle=None,
                    source_revision=None,
                    start_at=_require_datetime(row[2]),
                    end_at=_require_datetime(row[2]),
                    row_count=sum(item.rows for item in counts),
                    byte_count=_require_int(row[4]),
                    fingerprint_sha256=_require_str(row[1]),
                ),
            )
        return entries

    @staticmethod
    def _entry_matches_scope(entry: StorageEntry, scope: StorageScope) -> bool:
        type_matches = (
            entry.data_type in scope.data_types
            if scope.data_types
            else entry.data_type is not StorageDataType.DELETION_RECEIPTS
        )
        account_matches = scope.account_scope is None or entry.account_scope == scope.account_scope
        instrument_matches = (
            not scope.instrument_handles or entry.instrument_handle in scope.instrument_handles
        )
        start_matches = (
            scope.from_at is None or entry.end_at is None or entry.end_at >= scope.from_at
        )
        end_matches = scope.to_at is None or entry.start_at is None or entry.start_at <= scope.to_at
        return (
            type_matches
            and account_matches
            and instrument_matches
            and start_matches
            and end_matches
            and AnalyticsStore._entry_matches_object_filters(entry, scope)
        )

    @staticmethod
    def _entry_matches_object_filters(
        entry: StorageEntry,
        scope: StorageScope,
    ) -> bool:
        """Match optional safe object handles without widening other data types."""
        has_object_filter = bool(scope.dataset_ids or scope.analysis_ids or scope.artifact_ids)
        if not has_object_filter:
            return True
        if entry.data_type is StorageDataType.DATASETS:
            return entry.object_id in scope.dataset_ids
        if entry.data_type is StorageDataType.ANALYSES:
            return entry.object_id in scope.analysis_ids
        if entry.data_type is StorageDataType.ARTIFACTS:
            return entry.object_id in scope.artifact_ids
        return False

    def preview_delete(self, scope: StorageScope) -> DeletionPreview:
        """Preview the exact dependency closure and issue one short-lived token."""
        if not _scope_has_selector(scope):
            raise StoreValidationError("deletion preview requires an explicit scope")
        if scope.data_types == (StorageDataType.DELETION_RECEIPTS,):
            raise StoreValidationError("deletion receipts are retained as value-free audit data")
        normalized_scope_json = _scope_json(scope)
        scope_fingerprint = _fingerprint(normalized_scope_json)
        with self._write_connection() as connection:
            plan = self._build_deletion_plan(connection, scope)
            if not plan.table_counts:
                raise StoreNotFoundError("deletion scope matches no stored data")
            store_revision = self._revision(connection)
            created_at = _utc_now()
            expires_at = created_at + _DELETION_TOKEN_TTL
            token = new_safe_handle(HandleKind.DELETION_PREVIEW_TOKEN)
            token_sha256 = _fingerprint(token)
            connection.execute(
                """
                INSERT INTO deletion_tokens (
                    token_sha256,
                    scope_fingerprint,
                    normalized_scope_json,
                    deletion_plan_json,
                    store_revision,
                    created_at,
                    expires_at,
                    used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    token_sha256,
                    scope_fingerprint,
                    normalized_scope_json,
                    _plan_json(plan),
                    store_revision,
                    created_at,
                    expires_at,
                ),
            )
            return DeletionPreview(
                token=token,
                scope_fingerprint=scope_fingerprint,
                store_revision=store_revision,
                expires_at=expires_at,
                table_counts=plan.table_counts,
                estimated_bytes=plan.estimated_bytes,
            )

    def _build_deletion_plan(
        self,
        connection: duckdb.DuckDBPyConnection,
        scope: StorageScope,
    ) -> _DeletionPlan:
        entries = self._storage_entries(connection, scope)
        source_page_ids = {
            entry.object_id
            for entry in entries
            if entry.data_type in _SOURCE_PAGE_BACKED_DATA_TYPES
        }
        dataset_ids = {
            entry.object_id for entry in entries if entry.data_type is StorageDataType.DATASETS
        }
        snapshot_ids = {
            entry.object_id
            for entry in entries
            if entry.data_type is StorageDataType.ACCOUNT_SNAPSHOTS
        }
        analysis_ids = {
            entry.object_id for entry in entries if entry.data_type is StorageDataType.ANALYSES
        }
        artifact_ids = {
            entry.object_id for entry in entries if entry.data_type is StorageDataType.ARTIFACTS
        }

        dataset_ids.update(
            _select_strings(
                connection,
                "SELECT DISTINCT dataset_id FROM dataset_source_pages",
                "page_id",
                source_page_ids,
            ),
        )
        snapshot_ids.update(
            _select_strings(
                connection,
                "SELECT snapshot_id FROM account_snapshots",
                "dataset_id",
                dataset_ids,
            ),
        )
        analysis_ids.update(
            _select_strings(
                connection,
                "SELECT analysis_id FROM analyses",
                "dataset_id",
                dataset_ids,
            ),
        )
        artifact_ids.update(
            _select_strings(
                connection,
                "SELECT artifact_id FROM artifacts",
                "analysis_id",
                analysis_ids,
            ),
        )
        job_ids = {entry.object_id for entry in entries if entry.data_type is StorageDataType.JOBS}
        job_ids.update(
            _select_strings(
                connection,
                "SELECT job_id FROM jobs",
                "analysis_id",
                analysis_ids,
            ),
        )

        raw_targets: dict[str, set[str]] = {
            "account_snapshots": snapshot_ids,
            "analyses": analysis_ids,
            "artifacts": artifact_ids,
            "bookings": source_page_ids,
            "closed_positions": source_page_ids,
            "costs": source_page_ids,
            "dataset_source_pages": dataset_ids,
            "datasets": dataset_ids,
            "jobs": job_ids,
            "metrics": analysis_ids,
            "option_snapshots": source_page_ids,
            "price_bars": source_page_ids,
            "proof_receipts": analysis_ids,
            "quotes": source_page_ids,
            "source_pages": source_page_ids,
            "transactions": source_page_ids,
        }
        targets: dict[str, tuple[str, ...]] = {}
        table_counts: list[TableCount] = []
        for table, ids in raw_targets.items():
            ordered_ids = tuple(sorted(ids))
            if not ordered_ids:
                continue
            count = _count_targets(
                connection,
                table,
                _DELETION_COLUMNS[table],
                ordered_ids,
            )
            if count == 0:
                continue
            targets[table] = ordered_ids
            table_counts.append(TableCount(table=table, rows=count))

        estimated_bytes = sum(
            _sum_bytes(
                connection,
                table,
                _DELETION_COLUMNS[table],
                targets.get(table, ()),
            )
            for table in (
                "account_snapshots",
                "analyses",
                "artifacts",
                "datasets",
                "source_pages",
            )
        )
        return _DeletionPlan(
            targets=targets,
            table_counts=tuple(sorted(table_counts, key=lambda item: item.table)),
            estimated_bytes=estimated_bytes,
        )

    def delete_previewed(self, token: str) -> DeletionReceipt:
        """Delete only a valid previewed closure and retain a value-free receipt."""
        try:
            _validate_handle(token, "dp")
        except StoreValidationError as error:
            raise DeletionTokenError("deletion token is invalid") from error
        token_sha256 = _fingerprint(token)
        with self._write_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    scope_fingerprint,
                    normalized_scope_json,
                    deletion_plan_json,
                    store_revision,
                    epoch_us(expires_at),
                    epoch_us(used_at)
                FROM deletion_tokens
                WHERE token_sha256 = ?
                """,
                (token_sha256,),
            ).fetchone()
            if row is None:
                raise DeletionTokenError("deletion token is invalid")
            used_at = _optional_datetime(row[5])
            if used_at is not None:
                raise DeletionTokenError("deletion token was already used")
            now = _utc_now()
            expires_at = _require_datetime(row[4])
            if now >= expires_at:
                raise DeletionTokenError("deletion token expired")
            expected_revision = _require_int(row[3])
            current_revision = self._revision(connection)
            if expected_revision != current_revision:
                raise DeletionTokenError("store revision changed after deletion preview")
            normalized_scope_json = _require_str(row[1])
            scope_fingerprint = _require_str(row[0])
            if _fingerprint(normalized_scope_json) != scope_fingerprint:
                raise DeletionTokenError("deletion token scope is invalid")
            plan = _plan_from_json(_require_str(row[2]))
            for table in _DELETION_ORDER:
                ids = plan.targets.get(table, ())
                if ids:
                    _delete_targets(
                        connection,
                        table,
                        _DELETION_COLUMNS[table],
                        ids,
                    )
            connection.execute(
                "UPDATE deletion_tokens SET used_at = ? WHERE token_sha256 = ?",
                (now, token_sha256),
            )
            next_revision = self._bump_revision(connection)
            receipt_id = f"dr_{uuid4().hex}"
            counts_json = _table_counts_json(plan.table_counts)
            connection.execute(
                """
                INSERT INTO deletion_receipts (
                    receipt_id,
                    scope_fingerprint,
                    deleted_at,
                    table_counts_json,
                    estimated_bytes,
                    store_revision_before,
                    store_revision_after
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    scope_fingerprint,
                    now,
                    counts_json,
                    plan.estimated_bytes,
                    current_revision,
                    next_revision,
                ),
            )
            return DeletionReceipt(
                receipt_id=receipt_id,
                scope_fingerprint=scope_fingerprint,
                deleted_at=now,
                table_counts=plan.table_counts,
                estimated_bytes=plan.estimated_bytes,
                store_revision_before=current_revision,
                store_revision_after=next_revision,
            )


def _scope_has_selector(scope: StorageScope) -> bool:
    return bool(
        scope.account_scope
        or scope.data_types
        or scope.instrument_handles
        or scope.from_at
        or scope.to_at
        or scope.dataset_ids
        or scope.analysis_ids
        or scope.artifact_ids
    )


def _scope_json(scope: StorageScope) -> str:
    return _canonical_json(
        {
            "account_scope": scope.account_scope,
            "analysis_ids": list(scope.analysis_ids),
            "artifact_ids": list(scope.artifact_ids),
            "data_types": [item.value for item in scope.data_types],
            "dataset_ids": list(scope.dataset_ids),
            "from_at": scope.from_at.isoformat() if scope.from_at else None,
            "instrument_handles": list(scope.instrument_handles),
            "to_at": scope.to_at.isoformat() if scope.to_at else None,
        },
    )


def _select_strings(
    connection: duckdb.DuckDBPyConnection,
    base_sql: str,
    filter_column: str,
    values: set[str],
) -> set[str]:
    if not values:
        return set()
    ordered = tuple(sorted(values))
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            f"{base_sql} WHERE {filter_column} = ANY(?)",
            (list(ordered),),
        ).fetchall(),
    )
    return {_require_str(row[0]) for row in rows}


def _count_targets(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    values: Sequence[str],
) -> int:
    if _DELETION_COLUMNS.get(table) != column:
        raise StoreError("analytics deletion table is not allowed")
    row = connection.execute(
        f"SELECT count(*) FROM {table} WHERE {column} = ANY(?)",  # noqa: S608
        (list(values),),
    ).fetchone()
    if row is None:
        raise StoreError("analytics deletion count is missing")
    return _require_int(row[0])


def _sum_bytes(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    values: Sequence[str],
) -> int:
    if not values:
        return 0
    if _DELETION_COLUMNS.get(table) != column:
        raise StoreError("analytics deletion table is not allowed")
    query = _BYTE_SUM_QUERIES.get(table)
    if query is None:
        raise StoreError("analytics deletion byte table is not allowed")
    row = connection.execute(
        query,
        (list(values),),
    ).fetchone()
    if row is None:
        raise StoreError("analytics deletion byte estimate is missing")
    return _require_int(row[0])


def _delete_targets(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    values: Sequence[str],
) -> None:
    if _DELETION_COLUMNS.get(table) != column:
        raise StoreError("analytics deletion table is not allowed")
    connection.execute(
        f"DELETE FROM {table} WHERE {column} = ANY(?)",  # noqa: S608
        (list(values),),
    )


def _table_counts_json(counts: Sequence[TableCount]) -> str:
    return _canonical_json(
        [{"rows": item.rows, "table": item.table} for item in counts],
    )


def _table_counts_from_json(value: str) -> tuple[TableCount, ...]:
    loaded = cast("JsonValue", json.loads(value))
    if not isinstance(loaded, list):
        raise StoreError("deletion receipt counts are invalid")
    counts: list[TableCount] = []
    for item in loaded:
        if not isinstance(item, dict):
            raise StoreError("deletion receipt counts are invalid")
        table = item.get("table")
        rows = item.get("rows")
        if not isinstance(table, str) or not isinstance(rows, int):
            raise StoreError("deletion receipt counts are invalid")
        counts.append(TableCount(table=table, rows=rows))
    return tuple(counts)


def _plan_json(plan: _DeletionPlan) -> str:
    return _canonical_json(
        {
            "estimated_bytes": plan.estimated_bytes,
            "table_counts": [
                {"rows": item.rows, "table": item.table} for item in plan.table_counts
            ],
            "targets": {table: list(values) for table, values in sorted(plan.targets.items())},
        },
    )


def _plan_from_json(value: str) -> _DeletionPlan:
    loaded = cast("JsonValue", json.loads(value))
    if not isinstance(loaded, dict):
        raise DeletionTokenError("deletion token plan is invalid")
    raw_targets = loaded.get("targets")
    raw_estimated_bytes = loaded.get("estimated_bytes")
    raw_counts = loaded.get("table_counts")
    if (
        not isinstance(raw_targets, dict)
        or not isinstance(raw_estimated_bytes, int)
        or not isinstance(raw_counts, list)
    ):
        raise DeletionTokenError("deletion token plan is invalid")
    targets: dict[str, tuple[str, ...]] = {}
    for raw_table, raw_values in raw_targets.items():
        if (
            raw_table not in _DELETION_COLUMNS
            or not isinstance(raw_values, list)
            or any(not isinstance(item, str) for item in raw_values)
        ):
            raise DeletionTokenError("deletion token plan is invalid")
        targets[raw_table] = tuple(cast("list[str]", raw_values))
    counts = _table_counts_from_json(_canonical_json(raw_counts))
    expected_counts = tuple(sorted(counts, key=lambda item: item.table))
    if counts != expected_counts:
        raise DeletionTokenError("deletion token plan is invalid")
    return _DeletionPlan(
        targets=targets,
        table_counts=counts,
        estimated_bytes=raw_estimated_bytes,
    )


__all__ = (
    "AnalyticsStore",
    "DeletionPreview",
    "DeletionReceipt",
    "DeletionTokenError",
    "StorageDataType",
    "StorageEntry",
    "StorageScope",
    "StoreBusyError",
    "StoreConflictError",
    "StoreError",
    "StoreNotFoundError",
    "StoreQuotaError",
    "StoreValidationError",
    "StoredAnalysis",
    "StoredArtifact",
    "StoredDataset",
    "StoredSnapshot",
    "StoredSourceCapture",
    "StoredSourcePage",
    "TableCount",
    "supported_source_kinds",
)
