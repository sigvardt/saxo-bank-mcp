from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings, resolve_sim_auth_settings
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.session import (
    SessionCapabilityFields,
    SessionRequestError,
)
from saxo_bank_mcp.token_cache import save_token_cache


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def configure_cached_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    settings = resolve_sim_auth_settings(require_redirect=False)
    save_token_cache(
        settings.cache_path,
        SaxoTokenSet(
            access_token="access-token-value",  # noqa: S106
            refresh_token="refresh-token-value",  # noqa: S106
            code_verifier="verifier-value",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )


@pytest.mark.anyio
async def test_session_capabilities_success_path_is_scoped_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_cached_token(tmp_path, monkeypatch)

    async def read_capabilities(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
    ) -> SessionCapabilityFields:
        return {
            "AuthenticationLevel": "Strong",
            "DataLevel": "Full",
            "TradeLevel": "None",
        }

    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_auth_tools.read_session_capabilities",
        read_capabilities,
    )

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    serialized = str(payload)
    assert payload["status"] == "passed"
    assert payload["token_refreshed"] is False
    assert payload["capabilities"] == {
        "AuthenticationLevel": "Strong",
        "DataLevel": "Full",
        "TradeLevel": "None",
    }
    assert payload["verifies"] == [
        "cached SIM bearer token can read current session capability fields",
    ]
    assert "order placement safety" in payload["does_not_verify"]
    assert "real-money approval" in payload["does_not_verify"]
    assert "access-token-value" not in serialized
    assert "refresh-token-value" not in serialized


@pytest.mark.anyio
async def test_session_capabilities_failure_has_next_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_cached_token(tmp_path, monkeypatch)

    async def fail_capabilities(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
    ) -> SessionCapabilityFields:
        raise SessionRequestError(
            "invalid_capabilities_response",
            "Saxo session capabilities response did not match documented fields",
            200,
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_auth_tools.read_session_capabilities",
        fail_capabilities,
    )

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "session_capabilities_failed"
    assert payload["reason"] == "invalid_capabilities_response"
    assert payload["scope_used"] is False
    assert "redacted response shape" in str(payload["next_action"])


@pytest.mark.anyio
async def test_session_capabilities_live_refusal_has_agent_next_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "refused"
    assert payload["scope_used"] is False
    assert payload["verifies"] == []
    assert "provide LIVE read credentials" in str(payload["next_action"])
