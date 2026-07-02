from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from saxo_bank_mcp.config import resolve_sim_auth_settings
from saxo_bank_mcp.server import mcp


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
