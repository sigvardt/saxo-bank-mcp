from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from saxo_bank_mcp.auth_status import SimCredentialSource
from saxo_bank_mcp.credentials import CredentialFileError, parse_sim_pkce_credentials_file

DEFAULT_SIM_CREDENTIAL_FILE: Final = (
    Path.home() / "Desktop" / "saxo_bank_mcp_DEMO_credentials.txt"
)
DEFAULT_LIVE_CREDENTIAL_FILE: Final = (
    Path.home() / "Desktop" / "saxo_bank_mcp_LIVE_credentials.txt"
)


def env_sim_app_key(environ: Mapping[str, str]) -> str | None:
    return _first_nonempty(environ, "SAXO_MCP_SIM_APP_KEY", "SAXO_MCP_SIM_CLIENT_ID")


def live_credentials_present(environ: Mapping[str, str]) -> bool:
    if _first_nonempty(environ, "SAXO_MCP_LIVE_APP_KEY", "SAXO_MCP_LIVE_CLIENT_ID"):
        return True
    credential_file = Path(
        environ.get("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(DEFAULT_LIVE_CREDENTIAL_FILE)),
    )
    if not credential_file.exists():
        return False
    try:
        parse_sim_pkce_credentials_file(credential_file)
    except CredentialFileError:
        return False
    return True


def sim_credential_source(environ: Mapping[str, str]) -> SimCredentialSource:
    if env_sim_app_key(environ) is not None:
        return "env"
    credential_file = Path(
        environ.get("SAXO_MCP_SIM_CREDENTIAL_FILE", str(DEFAULT_SIM_CREDENTIAL_FILE)),
    )
    if not credential_file.exists():
        return "missing"
    try:
        parse_sim_pkce_credentials_file(credential_file)
    except CredentialFileError:
        return "missing"
    return "file"


def _first_nonempty(environ: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = environ.get(key, "").strip()
        if value:
            return value
    return None
