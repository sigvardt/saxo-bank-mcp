from __future__ import annotations

import json
from pathlib import Path
from typing import Never

import pytest

from saxo_bank_mcp import sim_session_keeper
from saxo_bank_mcp.config import SimAuthSettings, SimAuthSettingsError
from saxo_bank_mcp.sim_token_refresh import SimRefreshOutcome, SimRefreshStatus

CONFIGURATION_ERROR_EXIT = 2
TEST_TOKEN_URL = "https://sim.logonvalidation.net/token"  # noqa: S105


def _settings(cache_path: Path) -> SimAuthSettings:
    return SimAuthSettings(
        app_key="fixture-app-key",
        authorization_url="https://sim.logonvalidation.net/authorize",
        token_url=TEST_TOKEN_URL,
        rest_base_url="https://gateway.saxobank.com/sim/openapi/",
        redirect_uri="",
        cache_path=cache_path,
    )


@pytest.mark.parametrize(
    ("environment", "live_reads"),
    [("LIVE", "0"), ("SIM", "1")],
)
def test_main_refuses_unsafe_runtime_before_settings_or_refresh(
    environment: str,
    live_reads: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", environment)
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", live_reads)

    def unexpected_settings(*_args: object, **_kwargs: object) -> Never:
        pytest.fail("unsafe runtime must stop before resolving auth settings")

    monkeypatch.setattr(sim_session_keeper, "resolve_sim_auth_settings", unexpected_settings)

    assert sim_session_keeper.main() == CONFIGURATION_ERROR_EXIT
    assert json.loads(capsys.readouterr().out) == {
        "environment": "SIM",
        "network_call_made": False,
        "status": "sim_environment_required",
    }


@pytest.mark.parametrize("status", ["fresh", "refreshed"])
def test_main_reports_successful_one_shot_outcome(
    status: SimRefreshStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path / "token.json")
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "0")

    def resolved_settings(**_kwargs: object) -> SimAuthSettings:
        return settings

    monkeypatch.setattr(
        sim_session_keeper,
        "resolve_sim_auth_settings",
        resolved_settings,
    )

    async def completed(_settings: SimAuthSettings) -> SimRefreshOutcome:
        return SimRefreshOutcome(
            status=status,
            network_call_made=status == "refreshed",
        )

    monkeypatch.setattr(sim_session_keeper, "refresh_sim_token_if_needed", completed)

    assert sim_session_keeper.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "environment": "SIM",
        "network_call_made": status == "refreshed",
        "status": status,
    }


def test_main_reports_rejection_as_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path / "token.json")
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "0")

    def resolved_settings(**_kwargs: object) -> SimAuthSettings:
        return settings

    monkeypatch.setattr(
        sim_session_keeper,
        "resolve_sim_auth_settings",
        resolved_settings,
    )

    async def rejected(_settings: SimAuthSettings) -> SimRefreshOutcome:
        return SimRefreshOutcome(status="refresh_rejected", network_call_made=True)

    monkeypatch.setattr(sim_session_keeper, "refresh_sim_token_if_needed", rejected)

    assert sim_session_keeper.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "environment": "SIM",
        "network_call_made": True,
        "status": "refresh_rejected",
    }


def test_main_omits_secret_from_settings_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_detail = "credential-detail-that-must-not-appear"
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "0")

    def unavailable(**_kwargs: object) -> Never:
        raise SimAuthSettingsError("sim_credentials_missing", private_detail)

    monkeypatch.setattr(sim_session_keeper, "resolve_sim_auth_settings", unavailable)

    assert sim_session_keeper.main() == CONFIGURATION_ERROR_EXIT
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "environment": "SIM",
        "network_call_made": False,
        "status": "settings_unavailable",
    }
    assert private_detail not in captured.out + captured.err


def test_main_uses_unknown_network_provenance_for_unexpected_safe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_detail = "refresh-detail-that-must-not-appear"
    settings = _settings(tmp_path / "token.json")
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "0")

    def resolved_settings(**_kwargs: object) -> SimAuthSettings:
        return settings

    monkeypatch.setattr(
        sim_session_keeper,
        "resolve_sim_auth_settings",
        resolved_settings,
    )

    async def failed(_settings: SimAuthSettings) -> Never:
        raise OSError(private_detail)

    monkeypatch.setattr(sim_session_keeper, "refresh_sim_token_if_needed", failed)

    assert sim_session_keeper.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "environment": "SIM",
        "network_call_made": None,
        "status": "keeper_failed",
    }
    assert private_detail not in captured.out + captured.err
