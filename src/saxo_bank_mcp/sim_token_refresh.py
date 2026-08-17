from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import anyio
import httpx2
from anyio.to_thread import run_sync

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.oauth import OAuthRequestError, refresh_access_token
from saxo_bank_mcp.token_cache import inspect_token_cache, save_token_cache

type SimRefreshStatus = Literal[
    "fresh",
    "refreshed",
    "token_missing",
    "wrong_environment",
    "login_required",
    "refresh_rejected",
    "refresh_attempt_suppressed",
    "attempt_marker_failed",
    "cache_save_failed",
    "cache_changed",
    "marker_clear_failed",
]
type CacheRevision = str

SIM_REFRESH_MARGIN: Final = timedelta(minutes=5)
_LOCK_FLAGS: Final = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
_REFRESH_LOCK: Final = anyio.Lock()
_CACHE_REVISION_PARTS: Final = 4


@dataclass(frozen=True, slots=True)
class SimRefreshOutcome:
    status: SimRefreshStatus
    network_call_made: bool


async def refresh_sim_token_if_needed(
    settings: SimAuthSettings,
    *,
    minimum_validity: timedelta = SIM_REFRESH_MARGIN,
    now: datetime | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> SimRefreshOutcome:
    async with _REFRESH_LOCK:
        lock_fd = await _acquire_refresh_process_lock(settings.cache_path)
        try:
            checked_at = datetime.now(UTC) if now is None else now
            inspection = inspect_token_cache(settings.cache_path)
            token = inspection["token"]
            if token is None:
                return SimRefreshOutcome("token_missing", network_call_made=False)
            no_network_status = _cached_token_no_network_status(
                token,
                checked_at=checked_at,
                minimum_validity=minimum_validity,
            )
            if no_network_status is not None:
                return SimRefreshOutcome(no_network_status, network_call_made=False)

            revision = _cache_revision(settings.cache_path)
            if revision is None:
                return SimRefreshOutcome("attempt_marker_failed", network_call_made=False)
            marker_path = _attempt_marker_path(settings.cache_path)
            if _read_attempt_revision(marker_path) == revision:
                return SimRefreshOutcome(
                    "refresh_attempt_suppressed",
                    network_call_made=False,
                )
            try:
                _write_attempt_revision(marker_path, revision)
            except OSError:
                return SimRefreshOutcome("attempt_marker_failed", network_call_made=False)
            return await _refresh_after_attempt_marker(
                settings,
                token=token,
                original_revision=revision,
                marker_path=marker_path,
                transport=transport,
            )
        finally:
            _unlock_and_close(lock_fd)


async def _refresh_after_attempt_marker(
    settings: SimAuthSettings,
    *,
    token: SaxoTokenSet,
    original_revision: CacheRevision,
    marker_path: Path,
    transport: httpx2.AsyncBaseTransport | None,
) -> SimRefreshOutcome:
    try:
        refreshed = await refresh_access_token(settings, token, transport=transport)
    except OAuthRequestError:
        return SimRefreshOutcome("refresh_rejected", network_call_made=True)

    if _cache_revision(settings.cache_path) != original_revision:
        return SimRefreshOutcome("cache_changed", network_call_made=True)
    save_failure = _save_refreshed_token(settings.cache_path, refreshed)
    if save_failure is not None:
        return SimRefreshOutcome(save_failure, network_call_made=True)
    try:
        _clear_attempt_marker(marker_path)
    except OSError:
        return SimRefreshOutcome("marker_clear_failed", network_call_made=True)
    return SimRefreshOutcome("refreshed", network_call_made=True)


def _save_refreshed_token(
    cache_path: Path,
    refreshed: SaxoTokenSet,
) -> Literal["cache_save_failed", "cache_changed"] | None:
    try:
        save_token_cache(cache_path, refreshed)
        _sync_file_and_parent(cache_path)
    except OSError:
        return "cache_save_failed"
    if inspect_token_cache(cache_path)["token"] != refreshed:
        return "cache_changed"
    return None


def _cached_token_no_network_status(
    token: SaxoTokenSet,
    *,
    checked_at: datetime,
    minimum_validity: timedelta,
) -> SimRefreshStatus | None:
    if token.environment != "SIM":
        return "wrong_environment"
    if token.expires_at > checked_at + minimum_validity:
        return "fresh"
    if token.refresh_material() is None:
        return "login_required"
    return None


def _cache_revision(path: Path) -> CacheRevision | None:
    try:
        metadata = path.stat()
    except OSError:
        return None
    return ":".join(
        str(value)
        for value in (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
    )


def _attempt_marker_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}.sim-refresh-attempt")


def _read_attempt_revision(path: Path) -> CacheRevision | None:
    try:
        revision = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    parts = revision.split(":")
    if len(parts) != _CACHE_REVISION_PARTS or any(not part.isdigit() for part in parts):
        return None
    return revision


def _write_attempt_revision(path: Path, revision: CacheRevision) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as file:
            temporary_path = Path(file.name)
            temporary_path.chmod(0o600)
            file.write(str(revision))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary_path.replace(path)
        _sync_directory(path.parent)
    except OSError:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def _sync_file_and_parent(path: Path) -> None:
    file_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(file_fd)
    finally:
        os.close(file_fd)
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _clear_attempt_marker(path: Path) -> None:
    path.unlink()
    _sync_directory(path.parent)


async def _acquire_refresh_process_lock(cache_path: Path) -> int:
    lock_fd = _open_refresh_lock(cache_path)
    lock_acquired = False
    try:
        await run_sync(_lock_exclusive, lock_fd)
        lock_acquired = True
    finally:
        if not lock_acquired:
            os.close(lock_fd)
    return lock_fd


def _refresh_lock_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}.sim-refresh.lock")


def _open_refresh_lock(cache_path: Path) -> int:
    lock_path = _refresh_lock_path(cache_path)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, _LOCK_FLAGS, 0o600)
    os.fchmod(lock_fd, 0o600)
    return lock_fd


def _lock_exclusive(lock_fd: int) -> None:
    fcntl.flock(lock_fd, fcntl.LOCK_EX)


def _unlock_and_close(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
