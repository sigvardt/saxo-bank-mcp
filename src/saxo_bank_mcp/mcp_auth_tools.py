from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final

from pydantic import Field

from saxo_bank_mcp.auth import SaxoPendingAuthorization
from saxo_bank_mcp.config import (
    SaxoEnvironment,
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.mcp_pkce_state import (
    PendingAuthorizationBlocked,
    PendingAuthorizationReady,
    pending_authorization_for_exchange,
)
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.mcp_tool_results import (
    AUTH_DOES_NOT_VERIFY,
    AUTH_VERIFIES,
    EXCHANGED_DOES_NOT_VERIFY,
    SESSION_DOES_NOT_VERIFY,
    SESSION_VERIFIES,
    ToolResult,
    oauth_error,
    redacted_authorization_url,
    session_capabilities,
    session_error,
    settings_error,
    token_status,
)
from saxo_bank_mcp.oauth import (
    OAuthRequestError,
    exchange_authorization_code,
    refresh_access_token,
)
from saxo_bank_mcp.pkce import (
    AuthorizationUrlRequest,
    build_authorization_url,
    create_pkce_pair,
    create_state,
)
from saxo_bank_mcp.session import (
    SESSION_CAPABILITIES_PATH,
    SessionRequestError,
    read_session_capabilities,
)
from saxo_bank_mcp.token_cache import (
    delete_pending_authorization,
    pending_authorization_path,
    save_pending_authorization,
    save_token_cache,
)

AUTHORIZATION_URL_SENSITIVITY: Final = (
    "sensitive SIM login URL for the human completing PKCE login; do not log or share"
)


def saxo_start_pkce_login(*, reveal_authorization_url: bool = False) -> ToolResult:
    try:
        settings = resolve_sim_auth_settings()
    except SimAuthSettingsError as error:
        return settings_error("saxo_start_pkce_login", error)
    pkce = create_pkce_pair()
    state = create_state()
    authorization_url = build_authorization_url(
        AuthorizationUrlRequest(
            environment=SaxoEnvironment.SIM,
            client_id=settings.app_key,
            redirect_uri=settings.redirect_uri,
            pkce=pkce,
            state=state,
            authorization_url=settings.authorization_url,
        ),
    )
    save_pending_authorization(
        pending_authorization_path(settings.cache_path),
        SaxoPendingAuthorization(
            state=state,
            code_verifier=pkce.verifier,
            redirect_uri=settings.redirect_uri,
            created_at=datetime.now(UTC),
        ),
    )
    result: ToolResult = {
        "status": "authorization_url_ready",
        "tool_name": "saxo_start_pkce_login",
        "environment": "SIM",
        "scope_used": False,
        "authorization_url_redacted": redacted_authorization_url(authorization_url),
        "authorization_url_revealed": reveal_authorization_url,
        "authorization_url_sensitivity": AUTHORIZATION_URL_SENSITIVITY,
        "redirect_uri_present": True,
        "redirect_uri_note": "must match Saxo app registration; Saxo may reject mismatches",
        "verifies": list(AUTH_VERIFIES),
        "does_not_verify": list(AUTH_DOES_NOT_VERIFY),
        "next_action": "complete Saxo login, then call saxo_exchange_pkce_code with code and state",
    }
    if reveal_authorization_url:
        result["authorization_url"] = authorization_url
    return result


async def saxo_exchange_pkce_code(
    code: Annotated[
        str,
        Field(
            description=(
                "The authorization code returned from the Saxo login redirect query parameters"
            ),
        ),
    ],
    state: Annotated[
        str,
        Field(
            description=(
                "The state value returned from the Saxo login redirect, which must "
                "match the pending login state"
            ),
        ),
    ],
) -> ToolResult:
    try:
        settings = resolve_sim_auth_settings()
    except SimAuthSettingsError as error:
        return settings_error("saxo_exchange_pkce_code", error)
    readiness = pending_authorization_for_exchange(settings, state)
    match readiness:
        case PendingAuthorizationBlocked(result=result):
            return result
        case PendingAuthorizationReady(pending=pending):
            pass
    try:
        token = await exchange_authorization_code(
            settings,
            code=code,
            code_verifier=pending.code_verifier,
        )
    except OAuthRequestError as error:
        return oauth_error("saxo_exchange_pkce_code", error)
    save_token_cache(settings.cache_path, token)
    delete_pending_authorization(pending_authorization_path(settings.cache_path))
    return {
        "status": "token_cached",
        "tool_name": "saxo_exchange_pkce_code",
        "environment": "SIM",
        "scope_used": False,
        "token": token_status(token),
        "verifies": ["authorization code exchanged and tokens cached owner-only"],
        "does_not_verify": list(EXCHANGED_DOES_NOT_VERIFY),
    }


async def saxo_refresh_token() -> ToolResult:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return settings_error("saxo_refresh_token", error)
    cache_check = cached_token_for_tool("saxo_refresh_token", settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return result
        case CachedTokenReady(token=token):
            pass
    try:
        refreshed = await refresh_access_token(settings, token)
    except OAuthRequestError as error:
        return oauth_error("saxo_refresh_token", error)
    save_token_cache(settings.cache_path, refreshed)
    return {
        "status": "token_refreshed",
        "tool_name": "saxo_refresh_token",
        "environment": "SIM",
        "scope_used": False,
        "token": token_status(refreshed),
        "verifies": ["cached SIM access token refreshed"],
        "does_not_verify": list(EXCHANGED_DOES_NOT_VERIFY),
    }


async def saxo_get_session_capabilities() -> ToolResult:
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.effective_read_environment() != "SIM":
        return {
            "status": "live_not_called",
            "tool_name": "saxo_get_session_capabilities",
            "requested_environment": runtime.requested_environment.value,
            "live_reads": runtime.effective_read_environment() == "LIVE",
            "detail": "this Todo 2 tool only calls SIM endpoints; no live endpoint was called",
            "scope_used": False,
            "next_action": (
                "set SAXO_MCP_ENVIRONMENT=SIM to read SIM session capability fields; "
                "LIVE reads belong to a later approved phase"
            ),
            "verifies": [],
            "does_not_verify": list(SESSION_DOES_NOT_VERIFY),
        }
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return settings_error("saxo_get_session_capabilities", error)
    cache_check = cached_token_for_tool("saxo_get_session_capabilities", settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return result
        case CachedTokenReady(token=token):
            pass
    refreshed = False
    if token.redacted_status()["is_expired"]:
        try:
            token = await refresh_access_token(settings, token)
        except OAuthRequestError as error:
            return oauth_error("saxo_get_session_capabilities", error)
        save_token_cache(settings.cache_path, token)
        refreshed = True
    try:
        capabilities = await read_session_capabilities(settings, token)
    except SessionRequestError as error:
        return session_error("saxo_get_session_capabilities", error)
    return {
        "status": "passed",
        "tool_name": "saxo_get_session_capabilities",
        "environment": "SIM",
        "endpoint_path": SESSION_CAPABILITIES_PATH,
        "token_refreshed": refreshed,
        "capabilities": session_capabilities(capabilities),
        "verifies": list(SESSION_VERIFIES),
        "does_not_verify": list(SESSION_DOES_NOT_VERIFY),
    }
