from __future__ import annotations

import fcntl
import hashlib
import os
import re
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, cast
from uuid import uuid4

import duckdb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_migrations import store_writer_lock_path
from saxo_bank_mcp.analytics_models import (
    HandleKind,
    InstrumentHandle,
    UniverseId,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreQuotaError

_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_MAX_UNIVERSE_NAME_LENGTH: Final = 100
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
_UNIVERSE_ID_ADAPTER: Final[TypeAdapter[UniverseId]] = TypeAdapter(UniverseId)


class UniverseError(RuntimeError):
    """Base error for owner-local research universes."""


class UniverseValidationError(UniverseError):
    """Raised when a universe request is invalid."""


class UniverseConflictError(UniverseError):
    """Raised when the expected universe revision is stale."""


class UniverseNotFoundError(UniverseError):
    """Raised when a requested universe does not exist."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class UniverseInstrument(_StrictModel):
    instrument_handle: InstrumentHandle
    display_label: str = Field(min_length=1, max_length=220)


class UniverseSummary(_StrictModel):
    universe_id: UniverseId
    name: str = Field(min_length=1, max_length=_MAX_UNIVERSE_NAME_LENGTH)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    instruments: tuple[UniverseInstrument, ...]
    created_at: datetime
    updated_at: datetime

    @property
    def handles(self) -> tuple[str, ...]:
        return tuple(item.instrument_handle for item in self.instruments)


def _connect(config: AnalyticsConfig) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        str(config.paths.store_path),
        read_only=False,
        config=dict(_CONNECTION_CONFIG),
    )


@contextmanager
def _writer_lock(config: AnalyticsConfig) -> Generator[None]:
    lock_path = store_writer_lock_path(config.paths.store_path)
    descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UniverseConflictError(
                "another analytics writer owns the writer lock",
            ) from error
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
        connection = _connect(config)
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


def _safe_name(value: str) -> str:
    name = " ".join(value.split()).strip()
    if (
        not name
        or len(name) > _MAX_UNIVERSE_NAME_LENGTH
        or any(not character.isprintable() for character in name)
    ):
        raise UniverseValidationError("universe name is empty or invalid")
    return name


def _validate_revision(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise UniverseValidationError("universe revision is invalid")
    return value


def _new_revision(universe_id: str, handles: Sequence[str]) -> str:
    material = f"{universe_id}:{','.join(handles)}:{uuid4().hex}"
    return hashlib.sha256(material.encode()).hexdigest()


def _normalize_handles(values: Sequence[str]) -> tuple[str, ...]:
    handles = tuple(values)
    if len(set(handles)) != len(handles):
        raise UniverseValidationError("universe instrument handles must be unique")
    try:
        return tuple(
            _INSTRUMENT_HANDLE_ADAPTER.validate_python(handle, strict=True) for handle in handles
        )
    except ValidationError as error:
        raise UniverseValidationError("universe instrument handle is invalid") from error


def _validate_universe_id(value: str) -> str:
    try:
        return _UNIVERSE_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise UniverseValidationError("universe identifier is invalid") from error


class ResearchUniverseStore:
    """Persist revision-guarded research lists in the owner-only DuckDB store."""

    def __init__(self, config: AnalyticsConfig) -> None:
        """Bind one validated owner-only analytics store."""
        self._config = config
        store = AnalyticsStore.open(config)
        store.close()

    def create_universe(
        self,
        name: str,
        handles: Sequence[str],
    ) -> UniverseSummary:
        safe_name = _safe_name(name)
        normalized_handles = _normalize_handles(handles)
        universe_id = new_safe_handle(HandleKind.UNIVERSE_ID)
        revision = _new_revision(universe_id, normalized_handles)
        now = datetime.now(UTC)
        with _write_transaction(self._config) as connection:
            self._require_unique_name(connection, safe_name)
            self._require_instruments(connection, normalized_handles)
            self._require_capacity(safe_name, normalized_handles)
            connection.execute(
                """
                INSERT INTO universes (
                    universe_id,
                    safe_name,
                    created_at,
                    updated_at,
                    fingerprint_sha256
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (universe_id, safe_name, now, now, revision),
            )
            self._replace_instruments(connection, universe_id, normalized_handles)
            self._bump_store_revision(connection)
            return self._summary(connection, universe_id)

    def update_universe(
        self,
        universe_id: str,
        additions: Sequence[str],
        removals: Sequence[str],
        expected_revision: str,
    ) -> UniverseSummary:
        validated_id = _validate_universe_id(universe_id)
        expected = _validate_revision(expected_revision)
        add = _normalize_handles(additions)
        remove = _normalize_handles(removals)
        if set(add) & set(remove):
            raise UniverseValidationError("one handle cannot be added and removed together")
        with _write_transaction(self._config) as connection:
            current = self._summary(connection, validated_id)
            self._require_expected_revision(current, expected)
            current_handles = current.handles
            self._validate_changes(current_handles, add, remove)
            self._require_instruments(connection, add)
            next_handles = (
                tuple(handle for handle in current_handles if handle not in set(remove)) + add
            )
            self._require_capacity(current.name, next_handles)
            next_revision = _new_revision(validated_id, next_handles)
            connection.execute(
                """
                UPDATE universes
                SET updated_at = ?, fingerprint_sha256 = ?
                WHERE universe_id = ?
                """,
                (datetime.now(UTC), next_revision, validated_id),
            )
            self._replace_instruments(connection, validated_id, next_handles)
            self._bump_store_revision(connection)
            return self._summary(connection, validated_id)

    def list_universes(self) -> tuple[UniverseSummary, ...]:
        connection = _connect(self._config)
        try:
            rows = cast(
                "list[tuple[object, ...]]",
                connection.execute(
                    "SELECT universe_id FROM universes ORDER BY created_at, universe_id",
                ).fetchall(),
            )
            return tuple(self._summary(connection, self._required_text(row[0])) for row in rows)
        finally:
            connection.close()

    def delete_universe(self, universe_id: str, expected_revision: str) -> None:
        validated_id = _validate_universe_id(universe_id)
        expected = _validate_revision(expected_revision)
        with _write_transaction(self._config) as connection:
            current = self._summary(connection, validated_id)
            self._require_expected_revision(current, expected)
            connection.execute(
                "DELETE FROM universe_instruments WHERE universe_id = ?",
                (validated_id,),
            )
            connection.execute(
                "DELETE FROM universes WHERE universe_id = ?",
                (validated_id,),
            )
            self._bump_store_revision(connection)

    @staticmethod
    def _require_unique_name(
        connection: duckdb.DuckDBPyConnection,
        safe_name: str,
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM universes WHERE safe_name = ?",
                (safe_name,),
            ).fetchone()
            is not None
        ):
            raise UniverseValidationError("universe name already exists")

    def _require_capacity(self, name: str, handles: Sequence[str]) -> None:
        incoming_bytes = len(name.encode()) + len(handles) * 64
        try:
            AnalyticsStore.ensure_owner_capacity(self._config, incoming_bytes)
        except StoreQuotaError as error:
            raise UniverseValidationError(
                "analytics store quota refuses the universe write",
            ) from error

    @staticmethod
    def _require_expected_revision(
        current: UniverseSummary,
        expected: str,
    ) -> None:
        if current.revision != expected:
            raise UniverseConflictError("universe revision changed")

    @staticmethod
    def _validate_changes(
        current: Sequence[str],
        additions: Sequence[str],
        removals: Sequence[str],
    ) -> None:
        if any(handle in current for handle in additions):
            raise UniverseValidationError("universe addition already exists")
        if any(handle not in current for handle in removals):
            raise UniverseValidationError("universe removal does not exist")

    @staticmethod
    def _replace_instruments(
        connection: duckdb.DuckDBPyConnection,
        universe_id: str,
        handles: Sequence[str],
    ) -> None:
        connection.execute(
            "DELETE FROM universe_instruments WHERE universe_id = ?",
            (universe_id,),
        )
        if handles:
            connection.executemany(
                """
                INSERT INTO universe_instruments (universe_id, instrument_handle, ordinal)
                VALUES (?, ?, ?)
                """,
                [
                    (universe_id, instrument_handle, ordinal)
                    for ordinal, instrument_handle in enumerate(handles)
                ],
            )

    @staticmethod
    def _require_instruments(
        connection: duckdb.DuckDBPyConnection,
        handles: Sequence[str],
    ) -> None:
        if not handles:
            return
        row = connection.execute(
            "SELECT count(*) FROM safe_instruments WHERE instrument_handle = ANY(?)",
            (list(handles),),
        ).fetchone()
        if row is None or not isinstance(row[0], int) or row[0] != len(handles):
            raise UniverseValidationError("universe instrument does not exist")

    def _summary(
        self,
        connection: duckdb.DuckDBPyConnection,
        universe_id: str,
    ) -> UniverseSummary:
        row = connection.execute(
            """
            SELECT
                universe_id,
                safe_name,
                epoch_us(created_at),
                epoch_us(updated_at),
                fingerprint_sha256
            FROM universes
            WHERE universe_id = ?
            """,
            (universe_id,),
        ).fetchone()
        if row is None:
            raise UniverseNotFoundError("research universe does not exist")
        instrument_rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT i.instrument_handle, s.safe_label
                FROM universe_instruments AS i
                JOIN safe_instruments AS s
                    ON s.instrument_handle = i.instrument_handle
                WHERE i.universe_id = ?
                ORDER BY i.ordinal
                """,
                (universe_id,),
            ).fetchall(),
        )
        instruments = tuple(
            UniverseInstrument(
                instrument_handle=self._required_text(instrument_row[0]),
                display_label=self._required_text(instrument_row[1]),
            )
            for instrument_row in instrument_rows
        )
        return UniverseSummary(
            universe_id=self._required_text(row[0]),
            name=self._required_text(row[1]),
            created_at=self._required_datetime(row[2]),
            updated_at=self._required_datetime(row[3]),
            revision=self._required_text(row[4]),
            instruments=instruments,
        )

    @staticmethod
    def _required_text(value: object) -> str:
        if not isinstance(value, str):
            raise UniverseError("stored universe text is invalid")
        return value

    @staticmethod
    def _required_datetime(value: object) -> datetime:
        if type(value) is not int:
            raise UniverseError("stored universe timestamp is invalid")
        return datetime.fromtimestamp(value / 1_000_000, UTC)

    @staticmethod
    def _bump_store_revision(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
        )


__all__ = (
    "ResearchUniverseStore",
    "UniverseConflictError",
    "UniverseError",
    "UniverseInstrument",
    "UniverseNotFoundError",
    "UniverseSummary",
    "UniverseValidationError",
)
