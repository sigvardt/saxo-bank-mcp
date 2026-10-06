from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Callable
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
    "refresh_expired",
    "refresh_retryable",
    "refresh_rejected",
]

REFRESH_MARGIN: Final = timedelta(minutes=5)
REFRESH_POLL_SECONDS: Final = 30.0
HTTP_REQUEST_TIMEOUT: Final = 408
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR_MIN: Final = 500
_KEEPER_STOP_STATUSES: Final[frozenset[RefreshStatus]] = frozenset(
    {"refresh_rejected", "refresh_expired"},
)
_REFRESH_LOCK: Final = anyio.Lock()
_LOCK_FLAGS: Final = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW


@dataclass(frozen=True, slots=True)
class LiveRefreshOutcome:
    status: RefreshStatus
    network_call_made: bool
    http_status: int | None = None
    error_code: str | None = None
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None


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
                return _outcome("fresh", token, network_call_made=False)
            if token.refresh_material() is None:
                return _outcome("login_required", token, network_call_made=False)
            if token.refresh_expires_at is not None and token.refresh_expires_at <= checked_at:
                return _outcome("refresh_expired", token, network_call_made=False)
            try:
                refreshed = await refresh_access_token(settings, token)
            except OAuthRequestError as error:
                status: RefreshStatus = (
                    "refresh_retryable" if _is_transient(error) else "refresh_rejected"
                )
                return _outcome(status, token, network_call_made=True, error=error)
            save_token_cache(settings.cache_path, refreshed)
            return _outcome("refreshed", refreshed, network_call_made=True)
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
    if outcome.status == "refresh_retryable":
        result = live_read_auth_required(tool_name, "token_refresh_temporarily_failed")
        result["network_call_made"] = True
        result["missing_requirements"] = []
        result["next_action"] = (
            "retry the LIVE read shortly; the Saxo token endpoint did not answer and the "
            "session may still be valid"
        )
        return result
    if outcome.status in _KEEPER_STOP_STATUSES:
        reason = (
            "token_refresh_rejected"
            if outcome.status == "refresh_rejected"
            else "refresh_token_expired"
        )
        result = live_read_auth_required(tool_name, reason)
        result["network_call_made"] = outcome.network_call_made
        result["missing_requirements"] = ["fresh LIVE PKCE login"]
        result["next_action"] = "run saxo-bank-live-login, then retry the LIVE read"
        return result
    return cached


def refresh_log_line(outcome: LiveRefreshOutcome, *, now: datetime | None = None) -> str:
    logged_at = datetime.now(UTC) if now is None else now
    return json.dumps(
        {
            "at": logged_at.isoformat(timespec="seconds"),
            "event": "live_token_refresh",
            "status": outcome.status,
            "network_call_made": outcome.network_call_made,
            "http_status": outcome.http_status,
            "error": outcome.error_code,
            "access_expires_at": _isoformat(outcome.access_expires_at),
            "refresh_expires_at": _isoformat(outcome.refresh_expires_at),
        },
        sort_keys=True,
    )


def _log_to_stderr(line: str) -> None:
    sys.stderr.write(f"{line}\n")
    sys.stderr.flush()


async def keep_live_token_fresh(
    settings: SimAuthSettings,
    *,
    log: Callable[[str], None] = _log_to_stderr,
) -> None:
    stopped_cache_revision: int | None = None
    last_status: RefreshStatus | None = None
    while True:
        revision = _cache_revision(settings.cache_path)
        if stopped_cache_revision is not None and revision == stopped_cache_revision:
            await anyio.sleep(REFRESH_POLL_SECONDS)
            continue
        outcome = await refresh_live_token_if_needed(settings)
        if outcome.network_call_made or outcome.status != last_status:
            log(refresh_log_line(outcome))
        last_status = "fresh" if outcome.status == "refreshed" else outcome.status
        stopped_cache_revision = revision if outcome.status in _KEEPER_STOP_STATUSES else None
        await anyio.sleep(REFRESH_POLL_SECONDS)


def _outcome(
    status: RefreshStatus,
    token: SaxoTokenSet,
    *,
    network_call_made: bool,
    error: OAuthRequestError | None = None,
) -> LiveRefreshOutcome:
    return LiveRefreshOutcome(
        status=status,
        network_call_made=network_call_made,
        http_status=None if error is None else error.http_status,
        error_code=None if error is None else error.code,
        access_expires_at=token.expires_at,
        refresh_expires_at=token.refresh_expires_at,
    )


def _is_transient(error: OAuthRequestError) -> bool:
    if error.code == "network_error":
        return True
    status = error.http_status
    return (
        error.code == "http_error"
        and status is not None
        and (
            status >= HTTP_SERVER_ERROR_MIN
            or status in {HTTP_REQUEST_TIMEOUT, HTTP_TOO_MANY_REQUESTS}
        )
    )


def _isoformat(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat(timespec="seconds")


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
