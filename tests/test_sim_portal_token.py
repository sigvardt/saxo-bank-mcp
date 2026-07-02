from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp import mcp_auth_tools
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettings
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.session import (
    SessionCapabilityFields,
    SessionReadSettings,
    SessionRequestError,
    read_session_capabilities,
)
from saxo_bank_mcp.token_cache import load_token_cache, pending_authorization_path, save_token_cache

PORTAL_ACCESS_FIXTURE: Final = (
    "portal-access-token-fixture-with-enough-length-for-realistic-token-shape"
)
PORTAL_SOURCE: Final = "sim_24_hour_portal_token"
HTTP_UNAUTHORIZED: Final = 401


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def sim_settings(tmp_path: Path) -> SimAuthSettings:
    return SimAuthSettings(
        app_key="sim-app-key",
        authorization_url=SIM_ENDPOINTS.authorization_url,
        token_url=SIM_ENDPOINTS.token_url,
        rest_base_url=SIM_ENDPOINTS.rest_base_url,
        redirect_uri="https://example.test/callback",
        cache_path=tmp_path / "token-cache.json",
    )


@pytest.mark.anyio
async def test_cache_sim_access_token_writes_redacted_access_only_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_cache_sim_access_token",
            {"access_token": PORTAL_ACCESS_FIXTURE, "expires_in_seconds": 300},
        )

    payload = result.structured_content
    assert payload is not None
    serialized = str(payload)
    cache_path = tmp_path / ".local/state/saxo-bank-mcp/token-cache.json"
    cached = load_token_cache(cache_path)
    assert cached is not None
    assert cached.access_token == PORTAL_ACCESS_FIXTURE
    assert cached.refresh_token is None
    assert cached.code_verifier is None
    assert cached.environment == "SIM"
    assert payload["status"] == "token_cached"
    assert payload["token"]["has_access_token"] is True
    assert payload["token"]["has_refresh_token"] is False
    assert payload["token"]["has_code_verifier"] is False
    assert payload["token"]["environment"] == "SIM"
    assert payload["token_source"] == PORTAL_SOURCE
    assert payload["expires_at_source"] == "caller_asserted"
    assert payload["replaced_refresh_capable_cache"] is False
    assert payload["pending_authorization_deleted"] is False
    assert "inline access_token arguments" in str(payload["does_not_verify"])
    assert PORTAL_ACCESS_FIXTURE not in serialized


@pytest.mark.anyio
async def test_cache_sim_access_token_blocks_unintentional_pkce_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    cache_path = tmp_path / ".local/state/saxo-bank-mcp/token-cache.json"
    save_token_cache(
        cache_path,
        SaxoTokenSet(
            access_token="existing-access-token",  # noqa: S106
            refresh_token="existing-refresh-token",  # noqa: S106
            code_verifier="existing-verifier",
            environment="SIM",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    pending_path = pending_authorization_path(cache_path)
    pending_path.write_text("{}", encoding="utf-8")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_cache_sim_access_token",
            {"access_token": PORTAL_ACCESS_FIXTURE, "expires_in_seconds": 300},
        )

    payload = result.structured_content
    cached = load_token_cache(cache_path)
    assert payload is not None
    assert cached is not None
    assert payload["status"] == "cache_replace_blocked"
    assert payload["existing_refresh_capable_cache"] is True
    assert payload["pending_authorization_present"] is True
    assert "replace_existing_cache=true" in str(payload["next_action"])
    assert cached.access_token == "existing-access-token"  # noqa: S105
    assert cached.refresh_token == "existing-refresh-token"  # noqa: S105
    assert pending_path.exists()
    assert PORTAL_ACCESS_FIXTURE not in str(payload)


@pytest.mark.anyio
async def test_refresh_token_refuses_access_only_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    async with Client(mcp) as client:
        await client.call_tool(
            "saxo_cache_sim_access_token",
            {"access_token": PORTAL_ACCESS_FIXTURE, "expires_in_seconds": 300},
        )
        result = await client.call_tool("saxo_refresh_token", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_not_refreshable"
    assert payload["network_call_made"] is False
    assert PORTAL_ACCESS_FIXTURE not in str(payload)


@pytest.mark.anyio
async def test_session_capabilities_accepts_access_only_token(tmp_path: Path) -> None:
    settings = sim_settings(tmp_path)
    seen_authorization_headers: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_authorization_headers.append(request.headers["Authorization"])
        return httpx2.Response(
            200,
            json={
                "AuthenticationLevel": "Strong",
                "DataLevel": "Full",
                "TradeLevel": "None",
            },
            request=request,
        )

    capabilities = await read_session_capabilities(
        settings,
        SaxoTokenSet(
            access_token=PORTAL_ACCESS_FIXTURE,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        transport=httpx2.MockTransport(handler),
    )

    assert seen_authorization_headers == [f"Bearer {PORTAL_ACCESS_FIXTURE}"]
    assert capabilities["AuthenticationLevel"] == "Strong"


@pytest.mark.anyio
async def test_session_capabilities_recaches_when_fresh_portal_token_gets_401(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async def reject_session(
        _settings: SessionReadSettings,
        _token: SaxoTokenSet,
    ) -> SessionCapabilityFields:
        raise SessionRequestError("http_error", "rejected", 401)

    monkeypatch.setattr(mcp_auth_tools, "read_session_capabilities", reject_session)
    async with Client(mcp) as client:
        await client.call_tool(
            "saxo_cache_sim_access_token",
            {"access_token": PORTAL_ACCESS_FIXTURE, "expires_in_seconds": 300},
        )
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "session_capabilities_failed"
    assert payload["reason"] == "http_error"
    assert payload["http_status"] == HTTP_UNAUTHORIZED
    assert payload["token_refresh_supported"] is False
    assert "saxo_cache_sim_access_token" in str(payload["next_action"])
    assert PORTAL_ACCESS_FIXTURE not in str(payload)
