from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettings, resolve_sim_auth_settings
from saxo_bank_mcp.entitlements import (
    ENTITLEMENTS_PATH,
    UserEntitlementsFields,
    read_user_entitlements,
    summarize_user_entitlements,
)
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache

REALTIME_TOP_OF_BOOK_COUNT = 2
DOCUMENTED_RESPONSE_COUNT = 2


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


def configure_cached_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    settings = resolve_sim_auth_settings(require_redirect=False)
    save_token_cache(settings.cache_path, token())


@pytest.mark.anyio
async def test_user_entitlements_reads_documented_default_endpoint(tmp_path: Path) -> None:
    settings = sim_settings(tmp_path)
    seen_urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer access-token-value"
        return httpx2.Response(
            200,
            json={
                "Data": [
                    {
                        "ExchangeId": "XNAS",
                        "Entitlements": [
                            {
                                "RealTimeTopOfBook": ["Stock"],
                                "DelayedFullBook": ["CfdOnFund"],
                            },
                        ],
                    },
                ],
                "MaxRows": 99,
                "__count": 2,
                "__next": "/next-page",
            },
            request=request,
        )

    entitlements = await read_user_entitlements(
        settings,
        token(),
        transport=httpx2.MockTransport(handler),
    )

    assert seen_urls == [
        "https://gateway.saxobank.com/sim/openapi/port/v1/users/me/entitlements"
        "?EntitlementFieldSet=Default",
    ]
    assert ENTITLEMENTS_PATH == "/port/v1/users/me/entitlements"
    assert entitlements["Data"][0]["ExchangeId"] == "XNAS"
    assert entitlements["Data"][0]["Entitlements"][0]["RealTimeTopOfBook"] == ["Stock"]
    assert entitlements["Count"] == DOCUMENTED_RESPONSE_COUNT
    assert entitlements["HasNextPage"] is True


@pytest.mark.anyio
async def test_get_entitlements_success_summary_is_scoped_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_cached_token(tmp_path, monkeypatch)

    async def read_entitlements(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
    ) -> UserEntitlementsFields:
        return {
            "Data": [
                {
                    "ExchangeId": "XNAS",
                    "Entitlements": [
                        {
                            "DelayedFullBook": ["CfdOnFund"],
                            "DelayedGreeks": [],
                            "Greeks": [],
                            "RealTimeFullBook": ["CfdOnEtc"],
                            "RealTimeTopOfBook": ["Stock", "CfdOnEtf"],
                        },
                    ],
                },
            ],
            "MaxRows": 99,
            "Count": 1,
            "HasNextPage": False,
        }

    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_entitlement_tools.read_user_entitlements",
        read_entitlements,
    )

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_entitlements", {})

    payload = result.structured_content
    assert payload is not None
    serialized = str(payload)
    assert payload["status"] == "passed"
    assert payload["entitlement_summary"] == {
        "exchange_count": 1,
        "max_rows": 99,
        "response_count": 1,
        "has_next_page": False,
        "possibly_truncated": False,
    }
    assert payload["exchange_ids"] == ["XNAS"]
    assert payload["entitlement_bucket_counts"]["RealTimeTopOfBook"] == REALTIME_TOP_OF_BOOK_COUNT
    assert "price availability for a specific instrument" in payload["does_not_verify"]
    assert "access-token-value" not in serialized
    assert "refresh-token-value" not in serialized


@pytest.mark.anyio
async def test_get_entitlements_auth_required_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_entitlements", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_cache_missing"
    assert "price availability for a specific instrument" in payload["does_not_verify"]


def test_entitlements_summary_signals_possible_truncation() -> None:
    summary = summarize_user_entitlements(
        {
            "Data": [
                {
                    "ExchangeId": "XNAS",
                    "Entitlements": [],
                },
            ],
            "MaxRows": 1,
            "Count": 2,
            "HasNextPage": True,
        },
    )

    assert summary["possibly_truncated"] is True


@pytest.mark.anyio
async def test_get_entitlements_live_refusal_has_agent_next_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_entitlements", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "live_not_called"
    assert "SAXO_MCP_ENVIRONMENT=SIM" in str(payload["next_action"])
