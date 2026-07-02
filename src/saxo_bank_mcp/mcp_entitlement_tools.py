from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from saxo_bank_mcp.config import (
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.entitlements import (
    ENTITLEMENT_FIELD_SET,
    ENTITLEMENTS_PATH,
    EntitlementsRequestError,
    read_user_entitlements,
    summarize_user_entitlements,
)
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.mcp_tool_results import (
    ToolResult,
    oauth_error,
    settings_error,
)
from saxo_bank_mcp.oauth import OAuthRequestError, refresh_access_token
from saxo_bank_mcp.token_cache import save_token_cache

ENTITLEMENTS_TOOL_DESCRIPTION: Final = (
    "Reads SIM /port/v1/users/me/entitlements with EntitlementFieldSet=Default. "
    "Returns a redacted market-data entitlement summary only, not price availability "
    "for any specific instrument or trading readiness."
)
ENTITLEMENTS_VERIFIES: Final[tuple[str, ...]] = (
    "cached SIM bearer token can read current market-data entitlement summary",
)
ENTITLEMENTS_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "price availability for a specific instrument",
    "quote recency or real-time price delivery for any instrument",
    "order placement safety",
    "instrument/account suitability",
    "real-money approval",
    "live endpoint access",
)
_ENTITLEMENTS_NEXT_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "network_error": (
            "retry saxo_get_entitlements after confirming SIM connectivity and token freshness"
        ),
        "http_error": (
            "call saxo_auth_status, refresh the cached token if expired, then retry "
            "saxo_get_entitlements"
        ),
        "invalid_entitlements_response": (
            "treat entitlements as unverified; inspect the redacted response shape before "
            "using entitlement fields"
        ),
    },
)


async def saxo_get_entitlements() -> ToolResult:
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.effective_read_environment() != "SIM":
        return {
            "status": "live_not_called",
            "tool_name": "saxo_get_entitlements",
            "requested_environment": runtime.requested_environment.value,
            "live_reads": runtime.effective_read_environment() == "LIVE",
            "detail": "this Todo 2 tool only calls SIM endpoints; no live endpoint was called",
            "scope_used": False,
            "next_action": (
                "set SAXO_MCP_ENVIRONMENT=SIM to read SIM market-data entitlements; "
                "LIVE reads belong to a later approved phase"
            ),
            "verifies": [],
            "does_not_verify": list(ENTITLEMENTS_DOES_NOT_VERIFY),
        }
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return entitlement_auth_result(settings_error("saxo_get_entitlements", error))
    cache_check = cached_token_for_tool("saxo_get_entitlements", settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return entitlement_auth_result(result)
        case CachedTokenReady(token=token):
            pass
    refreshed = False
    if token.redacted_status()["is_expired"]:
        try:
            token = await refresh_access_token(settings, token)
        except OAuthRequestError as error:
            return entitlement_auth_result(oauth_error("saxo_get_entitlements", error))
        save_token_cache(settings.cache_path, token)
        refreshed = True
    try:
        entitlements = await read_user_entitlements(settings, token)
    except EntitlementsRequestError as error:
        return entitlements_error(error)
    summary = summarize_user_entitlements(entitlements)
    return {
        "status": "passed",
        "tool_name": "saxo_get_entitlements",
        "environment": "SIM",
        "endpoint_path": ENTITLEMENTS_PATH,
        "entitlement_field_set": ENTITLEMENT_FIELD_SET,
        "token_refreshed": refreshed,
        "entitlement_summary": {
            "exchange_count": summary["exchange_count"],
            "max_rows": summary["max_rows"],
            "response_count": summary["response_count"],
            "has_next_page": summary["has_next_page"],
            "possibly_truncated": summary["possibly_truncated"],
        },
        "exchange_ids": summary["exchange_ids"],
        "entitlement_bucket_counts": summary["entitlement_bucket_counts"],
        "verifies": list(ENTITLEMENTS_VERIFIES),
        "does_not_verify": list(ENTITLEMENTS_DOES_NOT_VERIFY),
    }


def entitlements_error(error: EntitlementsRequestError) -> ToolResult:
    return {
        "status": "entitlements_failed",
        "tool_name": "saxo_get_entitlements",
        "environment": "SIM",
        "endpoint_path": ENTITLEMENTS_PATH,
        "reason": error.code,
        "http_status": error.http_status,
        "detail": error.detail,
        "scope_used": False,
        "next_action": entitlements_next_action(error.code),
        "verifies": [],
        "does_not_verify": list(ENTITLEMENTS_DOES_NOT_VERIFY),
    }


def entitlement_auth_result(result: ToolResult) -> ToolResult:
    result["does_not_verify"] = list(ENTITLEMENTS_DOES_NOT_VERIFY)
    return result


def entitlements_next_action(reason: str) -> str:
    return _ENTITLEMENTS_NEXT_ACTIONS.get(
        reason,
        "call saxo_auth_status before retrying saxo_get_entitlements",
    )
