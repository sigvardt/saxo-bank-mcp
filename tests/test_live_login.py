from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from saxo_bank_mcp.live_login import (
    LiveLoginCallbackError,
    parse_live_login_callback,
    prepare_live_login,
)
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings


def _configure_live_oauth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credential_file = tmp_path / "live-credentials.json"
    credential_file.write_text(
        """{
  "AppKey": "sim-app-key",
  "GrantType": "PKCE",
  "AuthorizationEndpoint": "https://live.logonvalidation.net/authorize",
  "TokenEndpoint": "https://live.logonvalidation.net/token"
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(credential_file))
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "live-token.json"))
    monkeypatch.setenv("SAXO_MCP_LIVE_REDIRECT_URI", "http://localhost:8080/callback")


def test_live_login_prepares_saxo_pkce_url_without_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_live_oauth(tmp_path, monkeypatch)

    pending = prepare_live_login(resolve_live_oauth_settings())
    parsed = urlparse(pending.authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == "live.logonvalidation.net"
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:8080/callback"]
    assert "scope" not in query
    assert pending.code_verifier not in pending.authorization_url


def test_live_login_callback_requires_matching_state() -> None:
    with pytest.raises(LiveLoginCallbackError, match="callback_state_mismatch"):
        parse_live_login_callback(
            "/callback?code=authorization-code&state=wrong-state",
            expected_state="expected-state",
            expected_path="/callback",
        )


def test_live_login_callback_returns_code_without_echoing_it() -> None:
    result = parse_live_login_callback(
        "/callback?code=authorization-code&state=expected-state",
        expected_state="expected-state",
        expected_path="/callback",
    )

    assert result == "authorization-code"


def test_live_login_callback_accepts_configured_path() -> None:
    result = parse_live_login_callback(
        "/saxo?code=authorization-code&state=expected-state",
        expected_state="expected-state",
        expected_path="/saxo",
    )

    assert result == "authorization-code"
