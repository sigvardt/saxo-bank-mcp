from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registered_endpoint_can_call_live_read_when_live_read_gates_are_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CLIENT_ID", "live-client-id")
    monkeypatch.setenv("SAXO_MCP_LIVE_CLIENT_SECRET", "live-client-secret")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))

    def create_live_client(**_kwargs: str) -> _LiveClient:
        return _LiveClient()

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", create_live_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/root/v1/diagnostics/get"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["environment"] == "LIVE"
    assert payload["live_access"] is True
    assert payload["network_call_made"] is True
    assert payload["live_write"] is False
    assert payload["live_write_called"] is False
    assert payload["order_or_subscription_created"] is False
    assert payload["response"] == '{"Status":"Ok"}'


@pytest.mark.anyio
async def test_live_read_policy_refusal_is_an_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/port/v1/accounts/me"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert result.is_error is True
    assert payload is not None
    assert payload["status"] == "live_not_called"
    assert payload["network_call_made"] is False
    assert payload["live_write_called"] is False


@pytest.mark.anyio
async def test_live_read_reports_network_call_from_rejected_token_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "live-token.json"))

    async def rejected_refresh(
        _tool_name: str,
        _settings: SimAuthSettings,
    ) -> dict[str, str | bool | list[str]]:
        return {
            "status": "auth_required",
            "reason": "token_refresh_rejected",
            "network_call_made": True,
            "missing_requirements": ["fresh LIVE PKCE login"],
            "next_action": "run saxo-bank-live-login, then retry the LIVE read",
        }

    monkeypatch.setattr("saxo_bank_mcp.read_tools.live_token_for_tool", rejected_refresh)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/port/v1/accounts/me"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert result.is_error is True
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_refresh_rejected"
    assert payload["network_call_made"] is True
    assert payload["missing_requirements"] == ["fresh LIVE PKCE login"]
    assert payload["next_action"] == "run saxo-bank-live-login, then retry the LIVE read"


@pytest.mark.anyio
async def test_registered_endpoint_live_read_uses_token_for_authenticated_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))

    def create_live_client(**_kwargs: str) -> _AuthenticatedLiveClient:
        return _AuthenticatedLiveClient()

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", create_live_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/port/v1/accounts/me"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["environment"] == "LIVE"
    assert payload["auth_exercised"] is True
    assert payload["response"] == '{"Data":[]}'


class _LiveClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        assert path == "root/v1/diagnostics/get"
        assert params == {}
        assert headers == {"Accept": "application/json"}
        return httpx2.Response(
            200,
            text='{"Status":"Ok"}',
            request=httpx2.Request(
                "GET",
                "https://gateway.saxobank.com/openapi/root/v1/diagnostics/get",
            ),
        )


class _AuthenticatedLiveClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        assert path == "port/v1/accounts/me"
        assert params == {}
        assert headers == {
            "Accept": "application/json",
            "Authorization": "Bearer live-access-token",
        }
        return httpx2.Response(
            200,
            json={"Data": []},
            headers={"content-type": "application/json"},
            request=httpx2.Request(
                "GET",
                "https://gateway.saxobank.com/openapi/port/v1/accounts/me",
            ),
        )
