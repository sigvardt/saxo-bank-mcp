from __future__ import annotations

import fcntl
import os
import tempfile
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
    "refresh_rejected_unchanged",
]

SIM_REFRESH_MARGIN: Final = timedelta(minutes=5)
_LOCK_FLAGS: Final = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
_REFRESH_LOCK: Final = anyio.Lock()


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
            marker_path = _rejection_marker_path(settings.cache_path)
            if revision is not None and _read_rejected_revision(marker_path) == revision:
                return SimRefreshOutcome(
                    "refresh_rejected_unchanged",
                    network_call_made=False,
                )

            try:
                refreshed = await refresh_access_token(settings, token, transport=transport)
            except OAuthRequestError:
                if revision is not None:
                    _write_rejected_revision(marker_path, revision)
                return SimRefreshOutcome("refresh_rejected", network_call_made=True)

            save_token_cache(settings.cache_path, refreshed)
            marker_path.unlink(missing_ok=True)
            return SimRefreshOutcome("refreshed", network_call_made=True)
        finally:
            _unlock_and_close(lock_fd)


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


def _cache_revision(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _rejection_marker_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}.sim-refresh-rejected")


def _read_rejected_revision(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return None


def _write_rejected_revision(path: Path, revision: int) -> None:
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
        temporary_path.replace(path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


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
