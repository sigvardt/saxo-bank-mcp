from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings, resolve_sim_auth_settings
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def configure_sim_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")


@pytest.mark.anyio
async def test_session_capabilities_reports_unreadable_token_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sim_auth(tmp_path, monkeypatch)
    settings = resolve_sim_auth_settings(require_redirect=False)
    settings.cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.cache_path.write_text("corrupted-token-cache", encoding="utf-8")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_cache_unreadable"
    assert "unreadable token cache" in str(payload["next_action"])


@pytest.mark.anyio
async def test_session_capabilities_reports_refused_token_cache_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sim_auth(tmp_path, monkeypatch)
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(Path.cwd() / ".unsafe-token.json"))

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_get_session_capabilities", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_cache_path_refused"
    assert "outside the repository" in str(payload["next_action"])


@pytest.mark.anyio
async def test_refresh_cache_os_error_omits_private_path_from_mcp_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_sim_auth(tmp_path, monkeypatch)
    settings = resolve_sim_auth_settings(require_redirect=False)
    token = SaxoTokenSet(
        access_token="mocked-access-token",  # noqa: S106
        refresh_token="mocked-refresh-token",  # noqa: S106
        code_verifier="v" * 43,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    save_token_cache(settings.cache_path, token)
    private_marker = str(tmp_path / "private-cache-marker")

    async def refreshed_token(
        _settings: SimAuthSettings,
        _token: SaxoTokenSet,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> SaxoTokenSet:
        del transport
        return token

    def refused_write(_path: Path, _token: SaxoTokenSet) -> None:
        raise OSError(f"cache write failed at {private_marker}")

    monkeypatch.setattr("saxo_bank_mcp.mcp_auth_tools.refresh_access_token", refreshed_token)
    monkeypatch.setattr("saxo_bank_mcp.mcp_auth_tools.save_token_cache", refused_write)

    with caplog.at_level(1):
        async with Client(mcp) as client:
            result = await client.call_tool("saxo_refresh_token", {}, raise_on_error=False)

    assert result.is_error is True
    assert private_marker not in repr(result.content)
    assert private_marker not in repr(result.structured_content)
    assert private_marker not in caplog.text
