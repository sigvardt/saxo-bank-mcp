from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

from saxo_bank_mcp import mcp_auth_tools
from saxo_bank_mcp.auth import SaxoPendingAuthorization, SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings, resolve_sim_auth_settings
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import (
    load_token_cache,
    pending_authorization_path,
    save_pending_authorization,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass(frozen=True, slots=True)
class PendingSpec:
    state: str = "state-123"
    redirect_uri: str = "https://example.test/callback"
    created_at: datetime | None = None

    def authorization(self) -> SaxoPendingAuthorization:
        return SaxoPendingAuthorization(
            state=self.state,
            code_verifier="verifier-abc",
            redirect_uri=self.redirect_uri,
            created_at=datetime.now(UTC) if self.created_at is None else self.created_at,
        )


@dataclass(frozen=True, slots=True)
class ExchangeCall:
    code: str
    code_verifier: str
    token_url: str


class TokenExchangeRecorder:
    def __init__(self) -> None:  # noqa: D107
        self.calls: list[ExchangeCall] = []

    async def exchange(
        self,
        settings: SimAuthSettings,
        *,
        code: str,
        code_verifier: str,
    ) -> SaxoTokenSet:
        self.calls.append(
            ExchangeCall(code=code, code_verifier=code_verifier, token_url=settings.token_url),
        )
        return SaxoTokenSet(
            access_token="mocked-access-token",  # noqa: S106
            refresh_token="mocked-refresh-token",  # noqa: S106
            code_verifier=code_verifier,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def configure_sim_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimAuthSettings:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("SAXO_MCP_SIM_REDIRECT_URI", "https://example.test/callback")
    return resolve_sim_auth_settings()


def save_pending(settings: SimAuthSettings, spec: PendingSpec) -> Path:
    path = pending_authorization_path(settings.cache_path)
    save_pending_authorization(path, spec.authorization())
    return path


def install_exchange_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> TokenExchangeRecorder:
    recorder = TokenExchangeRecorder()
    monkeypatch.setattr(mcp_auth_tools, "exchange_authorization_code", recorder.exchange)
    return recorder


@pytest.mark.anyio
async def test_exchange_requires_pending_pkce_before_token_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sim_auth(tmp_path, monkeypatch)
    recorder = install_exchange_recorder(monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_exchange_pkce_code",
            {"code": "auth-code-789", "state": "state-123"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "pending_pkce_state_missing"
    assert "saxo_start_pkce_login" in str(payload["next_action"])
    assert recorder.calls == []


@pytest.mark.anyio
async def test_exchange_rejects_mismatched_state_before_token_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configure_sim_auth(tmp_path, monkeypatch)
    save_pending(settings, PendingSpec())
    recorder = install_exchange_recorder(monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_exchange_pkce_code",
            {"code": "auth-code-789", "state": "wrong-state"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "pending_pkce_state_mismatch"
    assert "most recent Saxo login redirect" in str(payload["next_action"])
    assert recorder.calls == []


@pytest.mark.anyio
async def test_exchange_rejects_redirect_uri_change_before_token_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configure_sim_auth(tmp_path, monkeypatch)
    save_pending(
        settings,
        replace(PendingSpec(), redirect_uri="https://different.example.test/callback"),
    )
    recorder = install_exchange_recorder(monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_exchange_pkce_code",
            {"code": "auth-code-789", "state": "state-123"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "redirect_uri_changed_since_login_start"
    assert "SAXO_MCP_SIM_REDIRECT_URI" in str(payload["next_action"])
    assert recorder.calls == []


@pytest.mark.anyio
async def test_exchange_clears_expired_pending_pkce_before_token_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configure_sim_auth(tmp_path, monkeypatch)
    stale = datetime.now(UTC) - timedelta(minutes=20)
    pending_path = save_pending(settings, replace(PendingSpec(), created_at=stale))
    recorder = install_exchange_recorder(monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_exchange_pkce_code",
            {"code": "auth-code-789", "state": "state-123"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "pending_pkce_expired"
    assert "expired" in str(payload["next_action"])
    assert recorder.calls == []
    assert not pending_path.exists()


@pytest.mark.anyio
async def test_exchange_caches_token_and_clears_pending_pkce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configure_sim_auth(tmp_path, monkeypatch)
    pending_path = save_pending(settings, PendingSpec())
    recorder = install_exchange_recorder(monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_exchange_pkce_code",
            {"code": "auth-code-789", "state": "state-123"},
        )

    payload = result.structured_content
    cached = load_token_cache(settings.cache_path)
    assert payload is not None
    assert cached is not None
    assert payload["status"] == "token_cached"
    assert payload["token"] == cached.redacted_status()
    assert payload["verifies"] == ["authorization code exchanged and tokens cached owner-only"]
    assert "Saxo login completed" not in payload["does_not_verify"]
    assert recorder.calls == [
        ExchangeCall(
            code="auth-code-789",
            code_verifier="verifier-abc",
            token_url=settings.token_url,
        ),
    ]
    assert not pending_path.exists()


@pytest.mark.anyio
async def test_exchange_treats_corrupt_pending_pkce_as_auth_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configure_sim_auth(tmp_path, monkeypatch)
    pending_path = pending_authorization_path(settings.cache_path)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text("corrupted-{bad-json", encoding="utf-8")
    recorder = install_exchange_recorder(monkeypatch)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_exchange_pkce_code",
            {"code": "auth-code-789", "state": "state-123"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "pending_pkce_unreadable"
    assert "unreadable pending PKCE" in str(payload["next_action"])
    assert recorder.calls == []


@pytest.mark.anyio
async def test_refresh_treats_corrupt_token_cache_as_auth_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configure_sim_auth(tmp_path, monkeypatch)
    settings.cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.cache_path.write_text("corrupted-token-cache", encoding="utf-8")

    async with Client(mcp) as client:
        result = await client.call_tool("saxo_refresh_token", {})

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "auth_required"
    assert payload["reason"] == "token_cache_unreadable"
