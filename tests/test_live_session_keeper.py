from __future__ import annotations

from pathlib import Path
from typing import Never

import anyio
import pytest
from anyio.lowlevel import checkpoint

from saxo_bank_mcp import live_session_keeper
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_mode import LiveReadSettingsError


def _live_settings(cache_path: Path) -> SimAuthSettings:
    oauth_endpoint = "https://example.test/oauth"
    return SimAuthSettings(
        app_key="app-key",
        authorization_url="https://example.test/authorize",
        token_url=oauth_endpoint,
        rest_base_url="https://example.test/openapi",
        redirect_uri="http://localhost:8080/callback",
        cache_path=cache_path,
    )


def test_main_returns_clear_error_without_exposing_settings_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = "client-value-that-must-stay-private"

    def missing_settings() -> Never:
        raise LiveReadSettingsError(
            "live_credentials_missing",
            f"missing credential {sensitive_value}",
        )

    monkeypatch.setattr(
        live_session_keeper,
        "resolve_live_oauth_settings",
        missing_settings,
    )

    result = live_session_keeper.main()

    captured = capsys.readouterr()
    assert result != 0
    assert "live_credentials_missing" in captured.err
    assert sensitive_value not in captured.out + captured.err


def test_main_delegates_resolved_settings_to_existing_keepalive_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _live_settings(tmp_path / "live-token.json")
    received: list[SimAuthSettings] = []

    async def keepalive(resolved: SimAuthSettings) -> None:
        await checkpoint()
        received.append(resolved)

    monkeypatch.setattr(
        live_session_keeper,
        "resolve_live_oauth_settings",
        lambda: settings,
    )
    monkeypatch.setattr(live_session_keeper, "keep_live_token_fresh", keepalive)

    result = live_session_keeper.main()

    assert result == 0
    assert received == [settings]


def test_main_stops_cleanly_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _live_settings(tmp_path / "live-token.json")

    async def interrupted(_settings: SimAuthSettings) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        live_session_keeper,
        "resolve_live_oauth_settings",
        lambda: settings,
    )
    monkeypatch.setattr(live_session_keeper, "keep_live_token_fresh", interrupted)

    assert live_session_keeper.main() == 0


def test_main_stops_cleanly_on_anyio_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _live_settings(tmp_path / "live-token.json")

    async def cancelled(_settings: SimAuthSettings) -> None:
        raise anyio.get_cancelled_exc_class()

    monkeypatch.setattr(
        live_session_keeper,
        "resolve_live_oauth_settings",
        lambda: settings,
    )
    monkeypatch.setattr(live_session_keeper, "keep_live_token_fresh", cancelled)

    assert live_session_keeper.main() == 0
