# pyright: reportPrivateUsage=false
# ruff: noqa: SLF001 - pin the private server-owned authentication boundary

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import saxo_bank_mcp.mcp_analytics_tools as tools_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_sync import SyncError
from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.mcp_token_state import CachedTokenBlocked, cached_token_for_tool
from saxo_bank_mcp.process_scoped_selectors import (
    account_selector_for,
    inject_account_selectors,
)
from saxo_bank_mcp.token_cache import save_token_cache


def _token(
    tmp_path: Path,
    *,
    environment: TokenEnvironment | None = "LIVE",
    expired: bool = False,
) -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=f"offline-{tmp_path.name}",
        environment=environment,
        expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
    )


def _selector(
    token: SaxoTokenSet, *, client_key: str = "CK1", currency: str = "USD"
) -> str:
    payload: dict[str, JsonValue] = {
        "AccountKey": "AK1",
        "AccountId": "offline-account-id",
        "ClientKey": client_key,
        "Currency": currency,
        "AccountType": "Normal",
    }
    inject_account_selectors(payload, token)
    return account_selector_for(token, "AK1")


@pytest.fixture
def live_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache_path = tmp_path / "live-token.json"
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "offline-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache_path))

    def refuse_sim_owner(*_args: object, **_kwargs: object) -> None:
        pytest.fail("LIVE account analytics reached the SIM token owner")

    monkeypatch.setattr(tools_module, "cached_token_for_tool", refuse_sim_owner)
    return cache_path


def test_live_account_scope_uses_current_live_token_owner(live_cache: Path, tmp_path: Path) -> None:
    token = _token(tmp_path)
    selector = _selector(token)
    save_token_cache(live_cache, token)

    scope = tools_module._server_account_scope(selector)

    assert scope.account_key.get_secret_value() == "AK1"
    assert scope.client_key.get_secret_value() == "CK1"
    assert scope.alias.startswith("aa_")
    assert "AK1" not in repr(scope)
    assert isinstance(cached_token_for_tool("offline", live_cache), CachedTokenBlocked)


@pytest.mark.parametrize("environment", ["SIM", None])
def test_live_account_scope_refuses_mismatched_token_environment(
    live_cache: Path, tmp_path: Path, environment: TokenEnvironment | None
) -> None:
    token = _token(tmp_path, environment=environment)
    selector = _selector(token)
    save_token_cache(live_cache, token)

    with pytest.raises(SyncError, match="current authentication is unavailable"):
        tools_module._server_account_scope(selector)


def test_live_account_scope_refuses_expired_token(live_cache: Path, tmp_path: Path) -> None:
    token = _token(tmp_path, expired=True)
    selector = _selector(token)
    save_token_cache(live_cache, token)

    with pytest.raises(SyncError, match="current authentication is unavailable"):
        tools_module._server_account_scope(selector)


def test_live_account_scope_refuses_selector_from_previous_token_generation(
    live_cache: Path, tmp_path: Path
) -> None:
    token = _token(tmp_path)
    selector = _selector(token)
    replacement = token.model_copy(update={"access_token": "rt-b"})
    save_token_cache(live_cache, replacement)

    with pytest.raises(SyncError, match="server-owned account context is unavailable"):
        tools_module._server_account_scope(selector)


@pytest.mark.parametrize("missing_field", ["client_key", "currency"])
def test_live_account_scope_requires_observed_client_and_currency(
    live_cache: Path, tmp_path: Path, missing_field: str
) -> None:
    token = _token(tmp_path)
    selector = _selector(
        token,
        client_key="" if missing_field == "client_key" else "CK1",
        currency="" if missing_field == "currency" else "USD",
    )
    save_token_cache(live_cache, token)

    with pytest.raises(SyncError, match="server-owned account context is unavailable"):
        tools_module._server_account_scope(selector)


def test_live_account_scope_refuses_unissued_selector(live_cache: Path, tmp_path: Path) -> None:
    token = _token(tmp_path)
    save_token_cache(live_cache, token)

    with pytest.raises(SyncError, match="server-owned account context is unavailable"):
        tools_module._server_account_scope(account_selector_for(token, "unobserved-account"))
