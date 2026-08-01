from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Final
from uuid import uuid4

import duckdb

from saxo_bank_mcp.analytics_config import prepare_owner_only_path

LATEST_SCHEMA_VERSION: Final = 3
_SOURCE_PAGE_IDENTITY_SCHEMA_VERSION: Final = 2
_ACCOUNT_SCOPE_BINDING_SCHEMA_VERSION: Final = 3
_SOURCE_MIGRATIONS_DIR: Final = (
    Path(__file__).resolve().parents[2] / "data" / "analytics" / "migrations"
)
_MIGRATION_RESOURCE_DIR: Final = "_analytics_migrations"
_CONNECTION_CONFIG: Final = MappingProxyType(
    {
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "enable_external_access": "false",
    },
)
_OWNER_FILE_MODE: Final = 0o600


class MigrationError(RuntimeError):
    """Raised when a local analytics schema migration cannot complete safely."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Value-free outcome of a schema migration."""

    from_version: int
    to_version: int
    backup_created: bool


def _connect_database(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    """Open one explicit hardened DuckDB connection."""
    return duckdb.connect(
        str(path),
        read_only=read_only,
        config=dict(_CONNECTION_CONFIG),
    )


def _read_migration(version: int) -> str:
    filename = {
        1: "0001_initial.sql",
        2: "0002_source_page_identity.sql",
        3: "0003_account_scope_binding_and_snapshot_order.sql",
    }.get(version)
    if filename is None:
        msg = f"schema migration {version} is not installed"
        raise MigrationError(msg)
    resource = files("saxo_bank_mcp").joinpath(_MIGRATION_RESOURCE_DIR, filename)
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    migration_path = _SOURCE_MIGRATIONS_DIR / filename
    if not migration_path.is_file():
        msg = f"schema migration {version} is not installed"
        raise MigrationError(msg)
    return migration_path.read_text(encoding="utf-8")


def _migration_name(version: int) -> str:
    if version == 1:
        return "initial"
    if version == _SOURCE_PAGE_IDENTITY_SCHEMA_VERSION:
        return "source_page_identity"
    if version == _ACCOUNT_SCOPE_BINDING_SCHEMA_VERSION:
        return "account_scope_binding_and_snapshot_order"
    msg = f"schema migration {version} is not installed"
    raise MigrationError(msg)


def store_writer_lock_path(store_path: Path) -> Path:
    """Derive one canonical lock beside the exact database it protects."""
    resolved_store = store_path.resolve(strict=False)
    return resolved_store.with_name(f".{resolved_store.name}.write.lock")


def _create_v0_database(path: Path) -> None:
    temp_path = _migration_temp_path(path)
    try:
        temp_path.unlink()
        connection = _connect_database(temp_path, read_only=False)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_schema (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    version INTEGER NOT NULL CHECK (version >= 0)
                )
                """,
            )
            connection.execute("DELETE FROM analytics_schema")
            connection.execute(
                "INSERT INTO analytics_schema (singleton, version) VALUES (TRUE, 0)",
            )
        finally:
            connection.close()
        temp_path.chmod(_OWNER_FILE_MODE)
        _replace_database(temp_path, path)
    finally:
        _remove_temp_database(temp_path)


def _read_schema_version(path: Path) -> int:
    connection = _connect_database(path, read_only=True)
    try:
        row = connection.execute(
            "SELECT version FROM analytics_schema WHERE singleton = TRUE",
        ).fetchone()
    except duckdb.Error as error:
        raise MigrationError("analytics store has no readable schema version") from error
    finally:
        connection.close()
    if row is None or not isinstance(row[0], int):
        raise MigrationError("analytics store has no readable schema version")
    return row[0]


def _backup_path(path: Path, target_version: int) -> Path:
    return path.with_name(
        f"{path.stem}.before-v{target_version}.{uuid4().hex}.duckdb",
    )


def _copy_owner_only(source: Path, target: Path) -> None:
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        _OWNER_FILE_MODE,
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, target)
        target.chmod(_OWNER_FILE_MODE)
        with target.open("rb") as copied:
            os.fsync(copied.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _migration_temp_path(path: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".migrating",
        dir=path.parent,
    )
    os.close(descriptor)
    temp_path = Path(raw_path)
    temp_path.chmod(_OWNER_FILE_MODE)
    return temp_path


def _apply_migrations(path: Path, current_version: int, target_version: int) -> None:
    connection = _connect_database(path, read_only=False)
    try:
        connection.execute("BEGIN TRANSACTION")
        for version in range(current_version + 1, target_version + 1):
            sql = _read_migration(version)
            sql_sha256 = hashlib.sha256(sql.encode()).hexdigest()
            try:
                connection.execute(sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (
                        version,
                        name,
                        sha256,
                        applied_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        version,
                        _migration_name(version),
                        sql_sha256,
                        datetime.now(UTC),
                    ),
                )
                connection.execute(
                    "UPDATE analytics_schema SET version = ? WHERE singleton = TRUE",
                    (version,),
                )
            except duckdb.Error as error:
                msg = f"migration {version} failed"
                raise MigrationError(msg) from error
        connection.execute("COMMIT")
    except BaseException:
        with suppress(duckdb.Error):
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _replace_database(temp_path: Path, target: Path) -> None:
    temp_path.replace(target)
    target.chmod(_OWNER_FILE_MODE)
    directory_descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _remove_temp_database(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(f"{path}.wal").unlink(missing_ok=True)


def _migrate_store_locked(path: Path, target_version: int) -> MigrationResult:
    prepared_path = prepare_owner_only_path(path)
    had_database = prepared_path.stat().st_size > 0
    if not had_database:
        _create_v0_database(prepared_path)

    current_version = _read_schema_version(prepared_path)
    if target_version < current_version:
        raise MigrationError("analytics schema downgrades are not supported")
    if target_version == current_version:
        return MigrationResult(
            from_version=current_version,
            to_version=target_version,
            backup_created=False,
        )

    backup_created = False
    if had_database:
        _copy_owner_only(
            prepared_path,
            _backup_path(prepared_path, target_version),
        )
        backup_created = True

    temp_path = _migration_temp_path(prepared_path)
    try:
        shutil.copyfile(prepared_path, temp_path)
        temp_path.chmod(_OWNER_FILE_MODE)
        _apply_migrations(temp_path, current_version, target_version)
        _replace_database(temp_path, prepared_path)
    finally:
        _remove_temp_database(temp_path)

    return MigrationResult(
        from_version=current_version,
        to_version=target_version,
        backup_created=backup_created,
    )


def migrate_store(path: Path, target_version: int) -> MigrationResult:
    """Migrate one owner-only store from a fixed catalog through an atomic replacement."""
    if not 0 <= target_version <= LATEST_SCHEMA_VERSION:
        msg = f"target schema version must be between 0 and {LATEST_SCHEMA_VERSION}"
        raise MigrationError(msg)

    lock_path = prepare_owner_only_path(store_writer_lock_path(path))
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as error:
            raise MigrationError("analytics writer lock is already held") from error
        return _migrate_store_locked(path, target_version)
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


__all__ = (
    "LATEST_SCHEMA_VERSION",
    "MigrationError",
    "MigrationResult",
    "migrate_store",
    "store_writer_lock_path",
)
