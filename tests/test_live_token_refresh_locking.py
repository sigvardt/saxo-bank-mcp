from __future__ import annotations

import errno
import fcntl
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import anyio
import httpx2
import pytest
from anyio.lowlevel import checkpoint

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.live_token_refresh import (
    LiveRefreshOutcome,
    live_token_for_tool,
    refresh_live_token_if_needed,
)
from saxo_bank_mcp.token_cache import save_token_cache

TEST_ACCESS_TOKEN: Final = "access-token-value"  # noqa: S105
TEST_REFRESH_TOKEN: Final = "refresh-token-value"  # noqa: S105
NEW_ACCESS_TOKEN: Final = "new-access-token"  # noqa: S105
NEW_REFRESH_TOKEN: Final = "new-refresh-token"  # noqa: S105


def _configure_live_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    credential_file = tmp_path / "live-credentials.json"
    credential_file.write_text(
        """{
  "AppKey": "sim-app-key",
  "GrantType": "PKCE",
  "AuthorizationEndpoint": "https://live.logonvalidation.net/authorize",
  "TokenEndpoint": "https://live.logonvalidation.net/token"
}
""",
        encoding="utf-8",
    )
    cache = tmp_path / "live-token.json"
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(credential_file))
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))
    monkeypatch.setenv("SAXO_MCP_LIVE_REDIRECT_URI", "http://localhost:8080/callback")
    return cache


def _live_token(
    *,
    access_token: str = TEST_ACCESS_TOKEN,
    refresh_token: str = TEST_REFRESH_TOKEN,
    expires_at: datetime | None = None,
) -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        code_verifier="v" * 43,
        environment="LIVE",
        expires_at=datetime.now(UTC) + timedelta(minutes=20) if expires_at is None else expires_at,
    )


@pytest.mark.anyio
async def test_concurrent_refresh_attempts_call_saxo_once_after_cache_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, _live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    calls = 0

    async def fake_refresh(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        nonlocal calls
        calls += 1
        await checkpoint()
        return _live_token(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)

    monkeypatch.setattr("saxo_bank_mcp.live_token_refresh.refresh_access_token", fake_refresh)
    settings = resolve_live_oauth_settings()
    outcomes: list[LiveRefreshOutcome] = []

    async def refresh_once() -> None:
        outcomes.append(await refresh_live_token_if_needed(settings))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(refresh_once)
        task_group.start_soon(refresh_once)

    assert {outcome.status for outcome in outcomes} == {"fresh", "refreshed"}
    assert calls == 1


@pytest.mark.anyio
async def test_concurrent_live_tools_both_return_the_current_cached_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, _live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    refresh_started = anyio.Event()
    release_refresh = anyio.Event()
    calls = 0

    async def fake_refresh(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        nonlocal calls
        calls += 1
        refresh_started.set()
        await release_refresh.wait()
        return _live_token(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)

    monkeypatch.setattr("saxo_bank_mcp.live_token_refresh.refresh_access_token", fake_refresh)
    settings = resolve_live_oauth_settings()
    returned_access_tokens: list[str] = []

    async def token_for_tool() -> None:
        token = await live_token_for_tool("saxo_get_session_capabilities", settings)
        assert isinstance(token, SaxoTokenSet)
        returned_access_tokens.append(token.access_token)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(token_for_tool)
        with anyio.fail_after(1):
            await refresh_started.wait()
        task_group.start_soon(token_for_tool)
        await checkpoint()
        release_refresh.set()

    assert returned_access_tokens == [NEW_ACCESS_TOKEN, NEW_ACCESS_TOKEN]
    assert calls == 1


@pytest.mark.anyio
async def test_refresh_holds_kernel_lock_while_calling_saxo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, _live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    refresh_started = anyio.Event()
    release_refresh = anyio.Event()

    async def fake_refresh(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        refresh_started.set()
        await release_refresh.wait()
        return _live_token(access_token=NEW_ACCESS_TOKEN, refresh_token=NEW_REFRESH_TOKEN)

    monkeypatch.setattr("saxo_bank_mcp.live_token_refresh.refresh_access_token", fake_refresh)
    settings = resolve_live_oauth_settings()
    lock_path = cache.with_name(f"{cache.name}.refresh.lock")
    outcomes: list[LiveRefreshOutcome] = []

    async def refresh_once() -> None:
        outcomes.append(await refresh_live_token_if_needed(settings))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(refresh_once)
        with anyio.fail_after(1):
            await refresh_started.wait()
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError) as error:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert error.value.errno in {errno.EACCES, errno.EAGAIN}
        finally:
            os.close(lock_fd)
            release_refresh.set()

    assert [outcome.status for outcome in outcomes] == ["refreshed"]
