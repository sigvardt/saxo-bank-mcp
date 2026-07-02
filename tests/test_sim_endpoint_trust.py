from __future__ import annotations

from pathlib import Path

import pytest

from saxo_bank_mcp.config import SimAuthSettingsError, resolve_sim_auth_settings


def test_sim_token_endpoint_override_must_use_saxo_sim_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(SimAuthSettingsError) as error:
        resolve_sim_auth_settings(
            {
                "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
                "SAXO_MCP_SIM_TOKEN_URL": "https://example.invalid/token",
            },
            require_redirect=False,
        )

    assert error.value.code == "sim_endpoint_untrusted"


def test_sim_auth_endpoint_override_must_use_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(SimAuthSettingsError) as error:
        resolve_sim_auth_settings(
            {
                "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
                "SAXO_MCP_SIM_AUTH_URL": "http://sim.logonvalidation.net/authorize",
            },
            require_redirect=False,
        )

    assert error.value.code == "sim_endpoint_untrusted"


def test_sim_endpoint_credential_file_override_must_use_saxo_sim_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    cred_file = tmp_path / "sim-credentials.txt"
    cred_file.write_text(
        """
App Key: fixture-app-key
Grant Type: PKCE
Auth endpoint: https://example.invalid/authorize
Token endpoint: https://sim.logonvalidation.net/token
""",
        encoding="utf-8",
    )

    with pytest.raises(SimAuthSettingsError) as error:
        resolve_sim_auth_settings(
            {
                "SAXO_MCP_SIM_CREDENTIAL_FILE": str(cred_file),
            },
            require_redirect=False,
        )

    assert error.value.code == "sim_endpoint_untrusted"
