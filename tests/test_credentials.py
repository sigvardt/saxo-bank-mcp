from __future__ import annotations

import pytest

from saxo_bank_mcp.credentials import CredentialFileError, parse_sim_pkce_credentials_text


def test_parse_sim_pkce_credentials_file_shape_ignores_headers() -> None:
    text = """
Saxo Bank MCP DEMO credentials
Saxo Bank MCP DEMO credentials

App Key: fixture-app-key
Access Control: local
Grant Type: PKCE
Auth endpoint: https://sim.logonvalidation.net/authorize
Token endpoint: https://sim.logonvalidation.net/token
"""

    credentials = parse_sim_pkce_credentials_text(text)

    assert credentials.app_key == "fixture-app-key"
    assert credentials.grant_type == "PKCE"
    assert credentials.auth_endpoint == "https://sim.logonvalidation.net/authorize"
    assert credentials.token_endpoint == "https://sim.logonvalidation.net/token"  # noqa: S105


def test_parse_sim_pkce_credentials_rejects_missing_token_endpoint() -> None:
    text = """
App Key: fixture-app-key
Grant Type: PKCE
Auth endpoint: https://sim.logonvalidation.net/authorize
"""

    with pytest.raises(CredentialFileError):
        parse_sim_pkce_credentials_text(text)
