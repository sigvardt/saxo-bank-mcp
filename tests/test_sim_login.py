from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saxo_bank_mcp import sim_login
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import resolve_sim_auth_settings
from saxo_bank_mcp.sim_login import PreparedSimLogin
from saxo_bank_mcp.token_cache import load_token_cache, token_cache_write_lock_path

OWNER_FILE_MODE = 0o600


class CallbackServer:
    def __init__(self, callback_targets: list[str]) -> None:
        """Initialize a deterministic local callback double."""
        self.callback_targets = callback_targets
        self.timeout = 0.0
        self.closed = False

    def handle_request(self) -> None:
        self.callback_targets.append("/callback?state=fixture-state&code=fixture-code")

    def server_close(self) -> None:
        self.closed = True


def test_sim_login_callback_saves_under_owner_only_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    def prepare_login(
        *,
        app_key: str,
        redirect_uri: str,
        authorization_url: str,
    ) -> PreparedSimLogin:
        del app_key, redirect_uri, authorization_url
        return PreparedSimLogin(
            authorization_url="https://example.invalid/authorize",
            state="fixture-state",
            code_verifier="v" * 43,
        )

    monkeypatch.setattr(
        sim_login,
        "prepare_sim_login",
        prepare_login,
    )
    callback_server: CallbackServer | None = None

    def build_server(_redirect_uri: str, targets: list[str]) -> CallbackServer:
        nonlocal callback_server
        callback_server = CallbackServer(targets)
        return callback_server

    token = SaxoTokenSet(
        access_token="new-access-token",  # noqa: S106
        refresh_token="new-refresh-token",  # noqa: S106
        code_verifier="v" * 43,
        environment="SIM",
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )

    async def exchange(*_args: object, **_kwargs: object) -> SaxoTokenSet:
        return token

    def open_browser(
        _url: str,
        new: int = 0,
        autoraise: bool = True,  # noqa: FBT001, FBT002
    ) -> bool:
        del new, autoraise
        return True

    monkeypatch.setattr(sim_login, "_callback_server", build_server)
    monkeypatch.setattr(sim_login.webbrowser, "open", open_browser)
    monkeypatch.setattr(sim_login, "exchange_authorization_code", exchange)

    result = sim_login.run_sim_login(timeout_seconds=1)

    settings = resolve_sim_auth_settings(require_redirect=False)
    assert callback_server is not None
    assert callback_server.closed is True
    assert result == {
        "status": "sim_token_cached",
        "environment": "SIM",
        "is_expired": False,
        "has_refresh_token": True,
        "cache_owner_only": True,
    }
    assert load_token_cache(settings.cache_path) == token
    assert (
        stat.S_IMODE(token_cache_write_lock_path(settings.cache_path).stat().st_mode)
        == OWNER_FILE_MODE
    )
