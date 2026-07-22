from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import anyio
from anyio.to_thread import run_sync

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_mode import (
    live_cached_token_for_tool,
    live_read_auth_required,
)
from saxo_bank_mcp.mcp_tool_results import ToolResult
from saxo_bank_mcp.oauth import OAuthRequestError, refresh_access_token
from saxo_bank_mcp.token_cache import inspect_token_cache, load_token_cache, save_token_cache

type RefreshStatus = Literal[
    "fresh",
    "refreshed",
    "token_missing",
    "login_required",
    "refresh_rejected",
]

REFRESH_MARGIN: Final = timedelta(minutes=2)
REFRESH_POLL_SECONDS: Final = 30.0
_REFRESH_LOCK: Final = anyio.Lock()
_LOCK_FLAGS: Final = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW


@dataclass(frozen=True, slots=True)
class LiveRefreshOutcome:
    status: RefreshStatus
    network_call_made: bool


async def refresh_live_token_if_needed(
    settings: SimAuthSettings,
    *,
    minimum_validity: timedelta = REFRESH_MARGIN,
    now: datetime | None = None,
) -> LiveRefreshOutcome:
    async with _REFRESH_LOCK:
        lock_fd = await _acquire_refresh_process_lock(settings.cache_path)
        try:
            checked_at = datetime.now(UTC) if now is None else now
            token = inspect_token_cache(settings.cache_path)["token"]
            if token is None or token.environment != "LIVE":
                return LiveRefreshOutcome("token_missing", network_call_made=False)
            if token.expires_at > checked_at + minimum_validity:
                return LiveRefreshOutcome("fresh", network_call_made=False)
            if token.refresh_material() is None:
                return LiveRefreshOutcome("login_required", network_call_made=False)
            try:
                refreshed = await refresh_access_token(settings, token)
            except OAuthRequestError:
                return LiveRefreshOutcome("refresh_rejected", network_call_made=True)
            save_token_cache(settings.cache_path, refreshed)
            return LiveRefreshOutcome("refreshed", network_call_made=True)
        finally:
            _unlock_and_close(lock_fd)


async def live_token_for_tool(
    tool_name: str,
    settings: SimAuthSettings,
) -> SaxoTokenSet | ToolResult:
    cached = live_cached_token_for_tool(tool_name, settings.cache_path)
    if not isinstance(cached, dict):
        return cached
    if cached.get("reason") != "token_cache_expired":
        return cached

    outcome = await refresh_live_token_if_needed(
        settings,
        minimum_validity=timedelta(0),
    )
    if outcome.status in {"fresh", "refreshed"}:
        current = load_token_cache(settings.cache_path)
        return current if current is not None else cached
    if outcome.status == "refresh_rejected":
        result = live_read_auth_required(tool_name, "token_refresh_rejected")
        result["network_call_made"] = True
        result["missing_requirements"] = ["fresh LIVE PKCE login"]
        result["next_action"] = "run saxo-bank-live-login, then retry the LIVE read"
        return result
    return cached


async def keep_live_token_fresh(settings: SimAuthSettings) -> None:
    rejected_cache_revision: int | None = None
    while True:
        revision = _cache_revision(settings.cache_path)
        if rejected_cache_revision is not None and revision == rejected_cache_revision:
            await anyio.sleep(REFRESH_POLL_SECONDS)
            continue
        outcome = await refresh_live_token_if_needed(settings)
        rejected_cache_revision = revision if outcome.status == "refresh_rejected" else None
        await anyio.sleep(REFRESH_POLL_SECONDS)


def _cache_revision(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


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
    return cache_path.with_name(f"{cache_path.name}.refresh.lock")


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
