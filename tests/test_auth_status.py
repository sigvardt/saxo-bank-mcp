from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.auth_status import SaxoAuthStatus
from saxo_bank_mcp.config import SaxoRuntimeConfig
from saxo_bank_mcp.token_cache import save_token_cache


def auth_status_for_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: SaxoTokenSet,
    *,
    redirect_uri: str | None = None,
) -> SaxoAuthStatus:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = tmp_path / "state" / "token-cache.json"
    save_token_cache(cache, token)
    environ = {
        "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
        "SAXO_MCP_TOKEN_CACHE_PATH": str(cache),
    }
    if redirect_uri is not None:
        environ["SAXO_MCP_SIM_REDIRECT_URI"] = redirect_uri
    return SaxoRuntimeConfig.from_env(environ, repo_root=tmp_path / "repo").redacted_status()


def test_auth_status_accepts_usable_token_cache_without_redirect_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = auth_status_for_cache(
        tmp_path,
        monkeypatch,
        SaxoTokenSet(
            access_token="access-token-value",  # noqa: S106
            environment="SIM",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )

    assert status["sim_redirect_uri_present"] is False
    assert status["token_cache_present"] is True
    assert status["token_cache_readable"] is True
    assert status["token_cache_expired"] is False
    assert status["token_cache_refresh_supported"] is False
    assert status["token_cache_environment"] == "SIM"  # noqa: S105
    assert status["blocking_reasons"] == []
    assert status["next_action"].startswith("call saxo_get_session_capabilities")
    assert "access-token-value" not in json.dumps(status)


@pytest.mark.parametrize("redirect_uri", [None, "https://example.test/callback"])
def test_expired_portal_token_routes_to_recaching_not_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redirect_uri: str | None,
) -> None:
    status = auth_status_for_cache(
        tmp_path,
        monkeypatch,
        SaxoTokenSet(
            access_token="expired-access-token",  # noqa: S106
            environment="SIM",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        ),
        redirect_uri=redirect_uri,
    )

    assert status["token_cache_expired"] is True
    assert status["token_cache_refresh_supported"] is False
    assert "saxo_cache_sim_access_token" in str(status["next_action"])
    assert "saxo_refresh_token" not in str(status["next_action"])
