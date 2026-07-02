from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from saxo_bank_mcp.auth import SaxoPendingAuthorization
from saxo_bank_mcp.config import SaxoRuntimeConfig, resolve_sim_auth_settings
from saxo_bank_mcp.token_cache import pending_authorization_path, save_pending_authorization


def test_runtime_config_uses_pending_pkce_redirect_when_env_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = tmp_path / "state" / "token-cache.json"
    save_pending_authorization(
        pending_authorization_path(cache),
        _pending("https://example.test/pending-callback"),
    )

    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_TOKEN_CACHE_PATH": str(cache),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()
    assert status["sim_redirect_uri_present"] is True
    assert status["blocking_reasons"] == [
        "pending_pkce_authorization_present",
        "token_cache_missing",
    ]


def test_runtime_config_prefers_env_redirect_over_pending_pkce_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = tmp_path / "state" / "token-cache.json"
    save_pending_authorization(
        pending_authorization_path(cache),
        _pending("https://example.test/pending-callback"),
    )

    settings = resolve_sim_auth_settings(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_SIM_REDIRECT_URI": "https://example.test/env-callback",
            "SAXO_MCP_TOKEN_CACHE_PATH": str(cache),
        },
        repo_root=tmp_path / "repo",
    )

    assert settings.redirect_uri == "https://example.test/env-callback"


def _pending(redirect_uri: str) -> SaxoPendingAuthorization:
    return SaxoPendingAuthorization(
        state="state",
        code_verifier="verifier",
        redirect_uri=redirect_uri,
        created_at=datetime.now(UTC),
    )
