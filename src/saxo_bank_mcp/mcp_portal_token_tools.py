from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from pydantic import Field

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettingsError, resolve_sim_auth_settings
from saxo_bank_mcp.mcp_tool_results import (
    EXCHANGED_DOES_NOT_VERIFY,
    ToolResult,
    settings_error,
    token_status,
)
from saxo_bank_mcp.token_cache import (
    delete_pending_authorization,
    load_token_cache,
    pending_authorization_path,
    save_token_cache,
)

SIM_ACCESS_CACHE_TOOL_DESCRIPTION: Final = (
    "Caches a Saxo developer portal 24-hour SIM OpenAPI access token owner-only "
    "outside the repository. The token is write-only from the tool response, is not "
    "refreshable, and does not prove account access until session capabilities pass."
)


def saxo_cache_sim_access_token(
    access_token: Annotated[
        str,
        Field(
            description=(
                "Saxo developer portal 24-hour SIM OpenAPI access token. "
                "Sensitive: this value is cached owner-only and never echoed."
            ),
            min_length=64,
        ),
    ],
    expires_in_seconds: Annotated[
        int,
        Field(
            description="Seconds until this portal token should be treated as expired.",
            gt=0,
            le=86_400,
        ),
    ] = 86_400,
    *,
    replace_existing_cache: Annotated[
        bool,
        Field(
            description=(
                "Set true only when intentionally replacing an existing refresh-capable "
                "SIM PKCE cache or pending PKCE login."
            ),
        ),
    ] = False,
) -> ToolResult:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return settings_error("saxo_cache_sim_access_token", error)
    token = SaxoTokenSet(
        access_token=access_token,
        environment="SIM",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
    )
    existing = load_token_cache(settings.cache_path)
    pending_path = pending_authorization_path(settings.cache_path)
    pending_deleted = pending_path.exists()
    existing_refresh_capable = (
        existing.refresh_material() is not None if existing is not None else False
    )
    if not replace_existing_cache and (existing_refresh_capable or pending_deleted):
        return {
            "status": "cache_replace_blocked",
            "tool_name": "saxo_cache_sim_access_token",
            "environment": "SIM",
            "scope_used": False,
            "token_source": "sim_24_hour_portal_token",
            "existing_refresh_capable_cache": existing_refresh_capable,
            "pending_authorization_present": pending_deleted,
            "network_call_made": False,
            "next_action": (
                "call saxo_cache_sim_access_token with replace_existing_cache=true only "
                "after deciding to discard the existing SIM PKCE cache or pending login"
            ),
            "verifies": [
                "existing token cache and pending PKCE state were left unchanged",
                "raw access token was not echoed in the tool result",
            ],
            "does_not_verify": [
                *EXCHANGED_DOES_NOT_VERIFY,
                "inline access_token arguments may pass through agent context and MCP transcripts",
                "portal token expiry is caller-asserted and Saxo may reject it earlier",
            ],
        }
    save_token_cache(settings.cache_path, token)
    delete_pending_authorization(pending_path)
    return {
        "status": "token_cached",
        "tool_name": "saxo_cache_sim_access_token",
        "environment": "SIM",
        "scope_used": False,
        "token_source": "sim_24_hour_portal_token",
        "expires_at_source": "caller_asserted",
        "refresh_supported": False,
        "replaced_refresh_capable_cache": (
            existing.refresh_material() is not None if existing is not None else False
        ),
        "pending_authorization_deleted": pending_deleted,
        "network_call_made": False,
        "token": token_status(token),
        "next_action": (
            "call saxo_get_session_capabilities to verify the current SIM session "
            "capability fields"
        ),
        "verifies": [
            "Saxo developer portal SIM access token cached owner-only outside the repository",
            "raw access token was not echoed in the tool result",
        ],
        "does_not_verify": [
            *EXCHANGED_DOES_NOT_VERIFY,
            "inline access_token arguments may pass through agent context and MCP transcripts",
            "portal token expiry is caller-asserted and Saxo may reject it earlier",
        ],
    }
