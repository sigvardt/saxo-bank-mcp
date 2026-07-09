from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    SaxoEnvironment,
    SaxoRuntimeConfig,
    environment_endpoints,
)
from saxo_bank_mcp.pkce import (
    AuthorizationUrlRequest,
    PkcePair,
    build_authorization_url,
    code_challenge_s256,
    create_pkce_pair,
    create_state,
)
from saxo_bank_mcp.token_cache import (
    pending_authorization_path,
    save_token_cache,
)


def test_environment_endpoints_are_exact_official_urls() -> None:
    sim = environment_endpoints(SaxoEnvironment.SIM)
    live = environment_endpoints(SaxoEnvironment.LIVE)

    assert sim.authorization_url == "https://sim.logonvalidation.net/authorize"
    assert sim.token_url == "https://sim.logonvalidation.net/token"  # noqa: S105
    assert sim.rest_base_url == "https://gateway.saxobank.com/sim/openapi/"
    assert live.authorization_url == "https://live.logonvalidation.net/authorize"
    assert live.token_url == "https://live.logonvalidation.net/token"  # noqa: S105
    assert live.rest_base_url == "https://gateway.saxobank.com/openapi"


def test_runtime_config_fails_closed_for_live_reads_without_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_ENVIRONMENT": "LIVE",
            "SAXO_MCP_LIVE_CLIENT_ID": "client-id",
            "SAXO_MCP_LIVE_CLIENT_SECRET": "client-secret",
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["requested_environment"] == "LIVE"
    assert status["effective_read_environment"] == "LIVE_READ_DISABLED"
    assert status["live_reads"] is False
    assert status["live_writes"] is False
    assert "client-id" not in json.dumps(status)
    assert "client-secret" not in json.dumps(status)


def test_runtime_config_allows_live_reads_only_with_flag_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_ENVIRONMENT": "LIVE",
            "SAXO_MCP_ENABLE_LIVE_READS": "1",
            "SAXO_MCP_LIVE_CLIENT_ID": "client-id",
            "SAXO_MCP_LIVE_CLIENT_SECRET": "client-secret",
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["requested_environment"] == "LIVE"
    assert status["effective_read_environment"] == "LIVE"
    assert status["live_reads"] is True
    assert status["live_writes"] is False


def test_runtime_config_accepts_live_pkce_credential_file_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    credential_file = tmp_path / "live_credentials.json"
    credential_file.write_text(
        json.dumps(
            {
                "AppKey": "live-key",
                "GrantType": "PKCE",
                "AuthorizationEndpoint": "https://live.logonvalidation.net/authorize",
                "TokenEndpoint": "https://live.logonvalidation.net/token",
            },
        ),
        encoding="utf-8",
    )
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_ENVIRONMENT": "LIVE",
            "SAXO_MCP_ENABLE_LIVE_READS": "1",
            "SAXO_MCP_LIVE_CREDENTIAL_FILE": str(credential_file),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["requested_environment"] == "LIVE"
    assert status["effective_read_environment"] == "LIVE"
    assert status["live_credentials_present"] is True
    assert "live-key" not in json.dumps(status)


def test_auth_status_uses_live_token_cache_path_in_live_mode(tmp_path: Path) -> None:
    live_cache = tmp_path / "state" / "live-token.json"
    token = SaxoTokenSet(
        access_token="live-access-token",  # noqa: S106
        environment="LIVE",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    save_token_cache(
        live_cache,
        token,
    )
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_ENVIRONMENT": "LIVE",
            "SAXO_MCP_ENABLE_LIVE_READS": "1",
            "SAXO_MCP_LIVE_APP_KEY": "live-key",
            "SAXO_MCP_LIVE_TOKEN_CACHE_PATH": str(live_cache),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["effective_read_environment"] == "LIVE"
    assert status["token_cache_path"] == str(live_cache)
    assert status["token_cache_environment"] == token.environment
    assert status["token_cache_expired"] is False
    assert status["blocking_reasons"] == []
    assert "SIM token" not in status["next_action"]


def test_runtime_config_accepts_sim_pkce_app_key_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_SIM_CREDENTIAL_FILE": str(tmp_path / "missing.txt"),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["sim_credentials_present"] is True
    assert status["sim_credential_source"] == "env"
    assert status["scope_used"] is False
    assert "sim-app-key" not in json.dumps(status)


def test_auth_status_reports_local_only_blockers_and_non_verifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_SIM_CREDENTIAL_FILE": str(tmp_path / "missing.txt"),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()
    serialized = json.dumps(status)

    assert status["verifies"] == [
        "local Saxo environment selection",
        "local credential-source presence without exposing credentials",
        "local token-cache path, presence, readability, and expiry metadata",
    ]
    assert "Saxo login/server-side authentication" in status["does_not_verify"]
    assert "trading/order readiness" in status["does_not_verify"]
    assert status["blocking_reasons"] == [
        "sim_redirect_uri_missing",
        "token_cache_missing",
    ]
    assert status["next_action"].startswith("set SAXO_MCP_SIM_REDIRECT_URI")
    assert status["token_cache_readable"] is False
    assert "sim-app-key" not in serialized


def test_auth_status_reports_corrupt_token_cache_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = tmp_path / "state" / "token-cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("not json", encoding="utf-8")
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_SIM_REDIRECT_URI": "https://example.test/callback",
            "SAXO_MCP_TOKEN_CACHE_PATH": str(cache),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["token_cache_present"] is True
    assert status["token_cache_readable"] is False
    assert status["token_cache_expired"] is None
    assert status["blocking_reasons"] == ["token_cache_unreadable"]
    assert status["next_action"].startswith("remove or replace the unreadable token cache")


def test_auth_status_routes_pending_pkce_without_restarting_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache = tmp_path / "state" / "token-cache.json"
    pending = pending_authorization_path(cache)
    pending.parent.mkdir(parents=True)
    pending.write_text("{}", encoding="utf-8")
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_SIM_REDIRECT_URI": "https://example.test/callback",
            "SAXO_MCP_TOKEN_CACHE_PATH": str(cache),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["pending_pkce_authorization_present"] is True
    assert status["blocking_reasons"] == [
        "pending_pkce_authorization_present",
        "token_cache_missing",
    ]
    assert status["next_action"].startswith("complete the Saxo login already started")


def test_runtime_config_accepts_sim_pkce_credential_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    credential_file = tmp_path / "demo_credentials.txt"
    credential_file.write_text(
        """
Saxo Bank MCP DEMO credentials
App Key: sim-app-key
Access Control: local
Grant Type: PKCE
Auth endpoint: https://sim.logonvalidation.net/authorize
Token endpoint: https://sim.logonvalidation.net/token
""",
        encoding="utf-8",
    )

    config = SaxoRuntimeConfig.from_env(
        {"SAXO_MCP_SIM_CREDENTIAL_FILE": str(credential_file)},
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["sim_credentials_present"] is True
    assert status["sim_credential_source"] == "file"
    assert status["scope_used"] is False
    assert "sim-app-key" not in json.dumps(status)


def test_pkce_challenge_uses_s256_base64url_without_padding() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    assert code_challenge_s256(verifier) == expected
    assert code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_pair_and_authorization_url_have_no_scope() -> None:
    pkce = create_pkce_pair()
    state = create_state()
    url = build_authorization_url(
        AuthorizationUrlRequest(
            environment=SaxoEnvironment.SIM,
            client_id="client-id",
            redirect_uri="http://127.0.0.1/callback",
            pkce=pkce,
            state=state,
        ),
    )

    assert re.fullmatch(r"[A-Za-z0-9_-]{43,128}", pkce.verifier)
    assert pkce.challenge == code_challenge_s256(pkce.verifier)
    assert state
    assert "code_challenge_method=S256" in url
    assert "scope=" not in url


def test_token_model_redacted_status_excludes_token_values() -> None:
    token = SaxoTokenSet(
        access_token="access-token-value",  # noqa: S106
        refresh_token="refresh-token-value",  # noqa: S106
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    status = token.redacted_status(now=datetime.now(UTC))
    serialized = json.dumps(status)

    assert status["has_access_token"] is True
    assert status["has_refresh_token"] is True
    assert status["has_code_verifier"] is True
    assert "access-token-value" not in serialized
    assert "refresh-token-value" not in serialized
    assert "verifier-value" not in serialized


def test_token_model_rejects_empty_tokens_and_naive_expiry() -> None:
    with pytest.raises(ValidationError):
        SaxoTokenSet(
            access_token="",
            refresh_token="refresh",  # noqa: S106
            code_verifier="verifier",
            expires_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        SaxoTokenSet(
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            code_verifier="verifier",
            expires_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None),
        )


def test_pkce_pair_constructor_rejects_wrong_challenge() -> None:
    with pytest.raises(ValidationError):
        PkcePair(verifier="A" * 43, challenge="wrong")
