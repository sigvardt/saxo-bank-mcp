from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettings
from saxo_bank_mcp.oauth import (
    authorization_code_form,
    exchange_authorization_code,
    refresh_access_token,
    refresh_token_form,
)
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.session import SESSION_CAPABILITIES_PATH, read_session_capabilities


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


def token() -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token="access-token-value",  # noqa: S106
        refresh_token="refresh-token-value",  # noqa: S106
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_oauth_forms_match_saxo_pkce_docs_without_scope(tmp_path: Path) -> None:
    settings = sim_settings(tmp_path)

    exchange_form = authorization_code_form(settings, code="auth-code", code_verifier="verifier")
    refresh_form = refresh_token_form(token())

    assert exchange_form == {
        "grant_type": "authorization_code",
        "client_id": "sim-app-key",
        "code": "auth-code",
        "redirect_uri": "https://example.test/callback",
        "code_verifier": "verifier",
    }
    assert refresh_form == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-token-value",
        "code_verifier": "verifier-value",
    }
    assert "scope" not in exchange_form
    assert "scope" not in refresh_form
    assert "client_id" not in refresh_form


@pytest.mark.anyio
async def test_token_exchange_and_refresh_use_documented_forms(tmp_path: Path) -> None:
    settings = sim_settings(tmp_path)
    seen_forms: list[dict[str, list[str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_forms.append(parse_qs(request.content.decode("utf-8")))
        return httpx2.Response(
            201,
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 60,
            },
            request=request,
        )

    transport = httpx2.MockTransport(handler)

    exchanged = await exchange_authorization_code(
        settings,
        code="auth-code",
        code_verifier="exchange-verifier",
        transport=transport,
    )
    refreshed = await refresh_access_token(settings, exchanged, transport=transport)

    assert seen_forms[0]["grant_type"] == ["authorization_code"]
    assert seen_forms[0]["client_id"] == ["sim-app-key"]
    assert seen_forms[0]["code"] == ["auth-code"]
    assert seen_forms[0]["redirect_uri"] == ["https://example.test/callback"]
    assert seen_forms[0]["code_verifier"] == ["exchange-verifier"]
    assert "scope" not in seen_forms[0]
    assert seen_forms[1]["grant_type"] == ["refresh_token"]
    assert seen_forms[1]["refresh_token"] == ["new-refresh-token"]
    assert seen_forms[1]["code_verifier"] == ["exchange-verifier"]
    assert "client_id" not in seen_forms[1]
    assert "scope" not in seen_forms[1]
    assert refreshed.redacted_status()["has_code_verifier"] is True


@pytest.mark.anyio
async def test_session_capabilities_reads_documented_sim_path(tmp_path: Path) -> None:
    settings = sim_settings(tmp_path)
    seen_urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer access-token-value"
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
        token(),
        transport=httpx2.MockTransport(handler),
    )

    assert seen_urls == [
        "https://gateway.saxobank.com/sim/openapi/root/v1/sessions/capabilities",
    ]
    assert SESSION_CAPABILITIES_PATH == "/root/v1/sessions/capabilities"
    assert capabilities == {
        "AuthenticationLevel": "Strong",
        "DataLevel": "Full",
        "TradeLevel": "None",
    }


@pytest.mark.anyio
async def test_start_pkce_login_redacts_authorization_url_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("SAXO_MCP_SIM_REDIRECT_URI", "https://example.test/callback")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_start_pkce_login", {})

    payload = result.structured_content
    assert payload is not None
    serialized = str(payload)
    assert payload["status"] == "authorization_url_ready"
    assert payload["scope_used"] is False
    assert payload["authorization_url_revealed"] is False
    assert "do not log or share" in str(payload["authorization_url_sensitivity"])
    assert "authorization_url" not in payload
    assert "sim-app-key" not in serialized
    assert "scope=" not in serialized
    assert "trading readiness/order safety" in serialized


@pytest.mark.anyio
async def test_start_pkce_login_marks_revealed_authorization_url_sensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("SAXO_MCP_SIM_REDIRECT_URI", "https://example.test/callback")

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_start_pkce_login",
            {"reveal_authorization_url": True},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["authorization_url_revealed"] is True
    assert "authorization_url" in payload
    assert "sim-app-key" in str(payload["authorization_url"])
    assert "do not log or share" in str(payload["authorization_url_sensitivity"])
    assert "code_verifier" not in str(payload["authorization_url"])


@pytest.mark.anyio
async def test_start_pkce_login_reports_missing_redirect_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.delenv("SAXO_MCP_SIM_REDIRECT_URI", raising=False)

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_start_pkce_login", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "sim_redirect_uri_missing"
    assert "SAXO_MCP_SIM_REDIRECT_URI" in str(payload)


@pytest.mark.anyio
async def test_session_capabilities_auth_required_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_cache_missing"
    assert "trading readiness/order safety" in str(payload)


@pytest.mark.anyio
async def test_session_capabilities_does_not_call_live_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "refused"
    assert payload["requested_environment"] == "LIVE"
