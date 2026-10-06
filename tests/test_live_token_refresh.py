from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp import live_token_refresh
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.live_token_refresh import (
    keep_live_token_fresh,
    live_token_for_tool,
    refresh_live_token_if_needed,
)
from saxo_bank_mcp.oauth import OAuthRequestError, OAuthTokenResponse
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import load_token_cache, save_token_cache

TEST_ACCESS_VALUE = "fixture-access-value"
NEW_ACCESS_VALUE = "fixture-new-access-value"
REFRESH_VALUE = "fixture-refresh-value"
SAXO_REFRESH_LIFETIME_SECONDS = 2400
HTTP_UNAUTHORIZED = 401
HTTP_SERVICE_UNAVAILABLE = 503


def test_no_asyncio_or_lifespan_background_implementation_remains() -> None:
    source_paths = (
        Path("src/saxo_bank_mcp/live_token_refresh.py"),
        Path("src/saxo_bank_mcp/server.py"),
    )
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in source_paths]

    imported_modules = {
        alias.name
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    names = {
        node.id
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    function_names = {
        node.name
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert "importlib" not in imported_modules
    assert "asyncio" not in imported_modules
    assert "anyio.from_thread" not in imported_modules
    assert "Protocol" not in names
    assert "start_blocking_portal" not in names
    assert "live_token_lifespan" not in names
    assert "live_token_lifespan" not in function_names


def configure_live_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    credential_file = tmp_path / "live-credentials.json"
    credential_file.write_text(
        "{\n"
        '  "AppKey": "sim-app-key",\n'
        '  "GrantType": "PKCE",\n'
        '  "AuthorizationEndpoint": "https://live.logonvalidation.net/authorize",\n'
        '  "TokenEndpoint": "https://live.logonvalidation.net/token"\n'
        "}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "live-token.json"
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(credential_file))
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))
    monkeypatch.setenv("SAXO_MCP_LIVE_REDIRECT_URI", "http://localhost:8080/callback")
    return cache


def live_token(
    *,
    access_token: str = TEST_ACCESS_VALUE,
    refresh_value: str = REFRESH_VALUE,
    expires_at: datetime | None = None,
    refresh_expires_at: datetime | None = None,
) -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=access_token,
        refresh_token=refresh_value,
        code_verifier="v" * 43,
        environment="LIVE",
        expires_at=datetime.now(UTC) + timedelta(minutes=20) if expires_at is None else expires_at,
        refresh_expires_at=refresh_expires_at,
    )


def scripted_refresh(
    failures: list[OAuthRequestError],
    calls: list[str],
) -> object:
    async def refresh(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        calls.append("refresh")
        if failures:
            raise failures.pop(0)
        return live_token(access_token=NEW_ACCESS_VALUE)

    return refresh


async def run_keeper_briefly(seconds: float = 0.5) -> list[dict[str, object]]:
    lines: list[str] = []
    with anyio.move_on_after(seconds):
        await keep_live_token_fresh(resolve_live_oauth_settings(), log=lines.append)
    return [json.loads(line) for line in lines]


@pytest.mark.anyio
async def test_live_tool_refreshes_expired_access_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
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
        return live_token(access_token=NEW_ACCESS_VALUE)

    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        fake_refresh,
    )

    token = await live_token_for_tool(
        "saxo_get_session_capabilities",
        resolve_live_oauth_settings(),
    )

    assert isinstance(token, SaxoTokenSet)
    assert calls == 1
    assert load_token_cache(cache) == token


@pytest.mark.anyio
async def test_live_tool_reports_login_required_when_refresh_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1)))

    async def rejected_refresh(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        raise OAuthRequestError(
            "http_error",
            "Saxo token endpoint rejected the PKCE request",
            401,
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        rejected_refresh,
    )

    result = await live_token_for_tool(
        "saxo_get_session_capabilities",
        resolve_live_oauth_settings(),
    )

    assert isinstance(result, dict)
    assert result["status"] == "auth_required"
    assert result["reason"] == "token_refresh_rejected"
    assert result["network_call_made"] is True
    assert TEST_ACCESS_VALUE not in str(result)


@pytest.mark.anyio
async def test_live_read_cache_os_error_omits_private_path_from_mcp_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    private_marker = str(tmp_path / "private-live-cache-marker")

    async def fake_refresh(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        return live_token(access_token=NEW_ACCESS_VALUE)

    def refused_write(_path: Path, _token: SaxoTokenSet) -> None:
        raise OSError(f"cache write failed at {private_marker}")

    monkeypatch.setattr("saxo_bank_mcp.live_token_refresh.refresh_access_token", fake_refresh)
    monkeypatch.setattr("saxo_bank_mcp.live_token_refresh.save_token_cache", refused_write)

    with caplog.at_level(1):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_get_session_capabilities",
                {},
                raise_on_error=False,
            )

    assert result.is_error is True
    assert private_marker not in repr(result.content)
    assert private_marker not in repr(result.structured_content)
    assert private_marker not in caplog.text


@pytest.mark.anyio
async def test_fastmcp_connect_disconnect_under_live_config_has_no_lifespan_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, live_token(expires_at=datetime.now(UTC) + timedelta(seconds=30)))
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
        return live_token(access_token=NEW_ACCESS_VALUE)

    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        fake_refresh,
    )

    async with Client(mcp) as client:
        await client.call_tool("saxo_health", {})

    cached = load_token_cache(cache)
    assert cached is not None
    assert cached.access_token == TEST_ACCESS_VALUE
    assert calls == 0


@pytest.mark.anyio
async def test_live_tool_reports_temporary_refresh_failure_without_requiring_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    expired = live_token(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    save_token_cache(cache, expired)
    failure = OAuthRequestError("network_error", "ConnectTimeout")
    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        scripted_refresh([failure], []),
    )

    result = await live_token_for_tool(
        "saxo_get_session_capabilities",
        resolve_live_oauth_settings(),
    )

    assert isinstance(result, dict)
    assert result["reason"] == "token_refresh_temporarily_failed"
    assert result["missing_requirements"] == []
    assert result["network_call_made"] is True
    assert load_token_cache(cache) == expired


@pytest.mark.anyio
async def test_keeper_retries_temporary_failures_until_the_refresh_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an access token inside the refresh margin and two temporary token-endpoint failures.
    cache = configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, live_token(expires_at=datetime.now(UTC) + timedelta(minutes=1)))
    calls: list[str] = []
    failures = [
        OAuthRequestError("network_error", "ReadTimeout"),
        OAuthRequestError("http_error", "Saxo token endpoint rejected the PKCE request", 503),
    ]
    monkeypatch.setattr(live_token_refresh, "REFRESH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        scripted_refresh(failures, calls),
    )

    # When: the keeper runs.
    events = await run_keeper_briefly()

    # Then: it keeps trying, stores the new token, and logs status without token values.
    assert calls == ["refresh", "refresh", "refresh"]
    assert [event["status"] for event in events] == [
        "refresh_retryable",
        "refresh_retryable",
        "refreshed",
    ]
    assert events[1]["http_status"] == HTTP_SERVICE_UNAVAILABLE
    assert events[0]["error"] == "network_error"
    cached = load_token_cache(cache)
    assert cached is not None
    assert cached.access_token == NEW_ACCESS_VALUE
    logged = json.dumps(events)
    assert TEST_ACCESS_VALUE not in logged
    assert NEW_ACCESS_VALUE not in logged
    assert REFRESH_VALUE not in logged


@pytest.mark.anyio
async def test_keeper_stops_after_a_definitive_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    save_token_cache(cache, live_token(expires_at=datetime.now(UTC) + timedelta(minutes=1)))
    calls: list[str] = []
    rejection = OAuthRequestError(
        "http_error", "Saxo token endpoint rejected the PKCE request", 401
    )
    monkeypatch.setattr(live_token_refresh, "REFRESH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        scripted_refresh([rejection], calls),
    )

    events = await run_keeper_briefly(0.3)

    assert calls == ["refresh"]
    assert [event["status"] for event in events] == ["refresh_rejected"]
    assert events[0]["http_status"] == HTTP_UNAUTHORIZED


@pytest.mark.anyio
async def test_refresh_makes_no_call_after_the_refresh_token_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = configure_live_refresh(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    save_token_cache(
        cache,
        live_token(
            expires_at=now - timedelta(minutes=30),
            refresh_expires_at=now - timedelta(minutes=1),
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "saxo_bank_mcp.live_token_refresh.refresh_access_token",
        scripted_refresh([], calls),
    )

    outcome = await refresh_live_token_if_needed(resolve_live_oauth_settings())
    result = await live_token_for_tool("saxo_list_live_accounts", resolve_live_oauth_settings())

    assert outcome.status == "refresh_expired"
    assert outcome.network_call_made is False
    assert calls == []
    assert isinstance(result, dict)
    assert result["reason"] == "refresh_token_expired"
    assert result["missing_requirements"] == ["fresh LIVE PKCE login"]


def test_token_response_records_the_refresh_token_deadline() -> None:
    received_at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    with_deadline = OAuthTokenResponse.model_validate(
        {
            "access_token": TEST_ACCESS_VALUE,
            "refresh_token": REFRESH_VALUE,
            "expires_in": 1200,
            "refresh_token_expires_in": SAXO_REFRESH_LIFETIME_SECONDS,
        },
    )
    without_deadline = OAuthTokenResponse.model_validate(
        {"access_token": TEST_ACCESS_VALUE, "refresh_token": REFRESH_VALUE, "expires_in": 1200},
    )

    token = with_deadline.to_token_set(code_verifier="v" * 43, received_at=received_at)
    legacy = without_deadline.to_token_set(code_verifier="v" * 43, received_at=received_at)

    assert token.refresh_expires_at == received_at + timedelta(
        seconds=SAXO_REFRESH_LIFETIME_SECONDS,
    )
    assert legacy.refresh_expires_at is None
