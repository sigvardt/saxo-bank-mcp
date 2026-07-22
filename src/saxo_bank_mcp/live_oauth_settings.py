from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from saxo_bank_mcp.config import (
    LIVE_ENDPOINTS,
    SimAuthSettings,
)
from saxo_bank_mcp.config_credentials import DEFAULT_LIVE_CREDENTIAL_FILE
from saxo_bank_mcp.credentials import CredentialFileError, parse_sim_pkce_credentials_file
from saxo_bank_mcp.live_mode import LiveReadSettingsError, resolve_live_read_settings

DEFAULT_LIVE_REDIRECT_URI: Final = "http://localhost:8080/callback"


def resolve_live_oauth_settings(
    environ: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> SimAuthSettings:
    source = os.environ if environ is None else environ
    read_settings = resolve_live_read_settings(source, repo_root=repo_root)
    app_key = _live_app_key(source)
    if app_key is None:
        credential_file = Path(
            source.get(
                "SAXO_MCP_LIVE_CREDENTIAL_FILE",
                str(DEFAULT_LIVE_CREDENTIAL_FILE),
            ),
        )
        try:
            credentials = parse_sim_pkce_credentials_file(credential_file)
        except CredentialFileError as error:
            raise LiveReadSettingsError(
                "live_credentials_missing",
                "LIVE PKCE credentials are required",
            ) from error
        if (
            credentials.auth_endpoint != LIVE_ENDPOINTS.authorization_url
            or credentials.token_endpoint != LIVE_ENDPOINTS.token_url
        ):
            raise LiveReadSettingsError(
                "live_credentials_missing",
                "LIVE PKCE credential endpoints do not match Saxo LIVE",
            )
        app_key = credentials.app_key

    redirect_uri = source.get(
        "SAXO_MCP_LIVE_REDIRECT_URI",
        DEFAULT_LIVE_REDIRECT_URI,
    ).strip()
    if not redirect_uri:
        redirect_uri = DEFAULT_LIVE_REDIRECT_URI
    return SimAuthSettings(
        app_key=app_key,
        authorization_url=LIVE_ENDPOINTS.authorization_url,
        token_url=LIVE_ENDPOINTS.token_url,
        rest_base_url=LIVE_ENDPOINTS.rest_base_url,
        redirect_uri=redirect_uri,
        cache_path=read_settings.cache_path,
    )


def _live_app_key(environ: Mapping[str, str]) -> str | None:
    for key in ("SAXO_MCP_LIVE_APP_KEY", "SAXO_MCP_LIVE_CLIENT_ID"):
        value = environ.get(key, "").strip()
        if value:
            return value
    return None
