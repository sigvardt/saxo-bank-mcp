from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from saxo_bank_mcp.auth import TokenEnvironment
from saxo_bank_mcp.config import (
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.entitlements import (
    ENTITLEMENT_FIELD_SET,
    ENTITLEMENTS_PATH,
    EntitlementsRequestError,
    EntitlementsSummary,
    read_user_entitlements,
    summarize_user_entitlements,
)
from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_cached_token_for_tool,
    live_read_auth_required,
    resolve_live_read_settings,
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
    "Reads /port/v1/users/me/entitlements with EntitlementFieldSet=Default in SIM, "
    "or in LIVE only when explicit live-read gates and a LIVE token cache are present. "
    "Returns a redacted market-data entitlement summary only, not price availability "
    "for any specific instrument, trading readiness, or live-write permission."
)
ENTITLEMENTS_VERIFIES: Final[tuple[str, ...]] = (
    "cached SIM bearer token can read current market-data entitlement summary",
)
LIVE_ENTITLEMENTS_VERIFIES: Final[tuple[str, ...]] = (
    "cached LIVE bearer token can read current market-data entitlement summary",
)
ENTITLEMENTS_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "price availability for a specific instrument",
    "quote recency or real-time price delivery for any instrument",
    "order placement safety",
    "instrument/account suitability",
    "real-money approval",
    "live endpoint access",
)
LIVE_ENTITLEMENTS_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "price availability for a specific instrument",
    "quote recency or real-time price delivery for any instrument",
    "order placement safety",
    "instrument/account suitability",
    "real-money approval",
    "live-write permission",
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
    match runtime.effective_read_environment():
        case "SIM":
            return await _read_sim_entitlements()
        case "LIVE_READ_DISABLED":
            return _live_entitlements_refused(runtime)
        case "LIVE":
            return await _read_live_entitlements()


async def _read_sim_entitlements() -> ToolResult:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return entitlement_auth_result(
            settings_error("saxo_get_entitlements", error),
            does_not_verify=ENTITLEMENTS_DOES_NOT_VERIFY,
        )
    cache_check = cached_token_for_tool("saxo_get_entitlements", settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return entitlement_auth_result(
                result,
                does_not_verify=ENTITLEMENTS_DOES_NOT_VERIFY,
            )
        case CachedTokenReady(token=token):
            pass
    refreshed = False
    if token.redacted_status()["is_expired"]:
        try:
            token = await refresh_access_token(settings, token)
        except OAuthRequestError as error:
            return entitlement_auth_result(
                oauth_error("saxo_get_entitlements", error),
                does_not_verify=ENTITLEMENTS_DOES_NOT_VERIFY,
            )
        save_token_cache(settings.cache_path, token)
        refreshed = True
    try:
        entitlements = await read_user_entitlements(settings, token)
    except EntitlementsRequestError as error:
        return entitlements_error(
            error,
            environment="SIM",
            does_not_verify=ENTITLEMENTS_DOES_NOT_VERIFY,
        )
    summary = summarize_user_entitlements(entitlements)
    return entitlements_success(
        environment="SIM",
        token_refreshed=refreshed,
        summary=summary,
        verifies=ENTITLEMENTS_VERIFIES,
        does_not_verify=ENTITLEMENTS_DOES_NOT_VERIFY,
    )


async def _read_live_entitlements() -> ToolResult:
    try:
        settings = resolve_live_read_settings()
    except LiveReadSettingsError as error:
        return entitlement_auth_result(
            live_read_auth_required("saxo_get_entitlements", error.code),
            does_not_verify=LIVE_ENTITLEMENTS_DOES_NOT_VERIFY,
        )
    token_or_result = live_cached_token_for_tool("saxo_get_entitlements", settings.cache_path)
    if isinstance(token_or_result, dict):
        return entitlement_auth_result(
            token_or_result,
            does_not_verify=LIVE_ENTITLEMENTS_DOES_NOT_VERIFY,
        )
    try:
        entitlements = await read_user_entitlements(settings, token_or_result)
    except EntitlementsRequestError as error:
        return entitlements_error(
            error,
            environment="LIVE",
            does_not_verify=LIVE_ENTITLEMENTS_DOES_NOT_VERIFY,
        )
    summary = summarize_user_entitlements(entitlements)
    return entitlements_success(
        environment="LIVE",
        token_refreshed=False,
        summary=summary,
        verifies=LIVE_ENTITLEMENTS_VERIFIES,
        does_not_verify=LIVE_ENTITLEMENTS_DOES_NOT_VERIFY,
    )


def _live_entitlements_refused(runtime: SaxoRuntimeConfig) -> ToolResult:
    return {
        "status": "live_not_called",
        "tool_name": "saxo_get_entitlements",
        "requested_environment": runtime.requested_environment.value,
        "live_reads": False,
        "detail": "LIVE read gates are absent; no live endpoint was called",
        "scope_used": False,
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "next_action": (
            "configure LIVE read credentials, SAXO_MCP_ENABLE_LIVE_READS=1, "
            "and SAXO_MCP_LIVE_TOKEN_CACHE_PATH before retrying"
        ),
        "verifies": [],
        "does_not_verify": list(LIVE_ENTITLEMENTS_DOES_NOT_VERIFY),
    }


def entitlements_success(
    *,
    environment: TokenEnvironment,
    token_refreshed: bool,
    summary: EntitlementsSummary,
    verifies: tuple[str, ...],
    does_not_verify: tuple[str, ...],
) -> ToolResult:
    return {
        "status": "passed",
        "tool_name": "saxo_get_entitlements",
        "environment": environment,
        "endpoint_path": ENTITLEMENTS_PATH,
        "entitlement_field_set": ENTITLEMENT_FIELD_SET,
        "token_refreshed": token_refreshed,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "entitlement_summary": {
            "exchange_count": summary["exchange_count"],
            "max_rows": summary["max_rows"],
            "response_count": summary["response_count"],
            "has_next_page": summary["has_next_page"],
            "possibly_truncated": summary["possibly_truncated"],
        },
        "exchange_ids": summary["exchange_ids"],
        "entitlement_bucket_counts": summary["entitlement_bucket_counts"],
        "verifies": list(verifies),
        "does_not_verify": list(does_not_verify),
    }


def entitlements_error(
    error: EntitlementsRequestError,
    *,
    environment: TokenEnvironment,
    does_not_verify: tuple[str, ...],
) -> ToolResult:
    return {
        "status": "entitlements_failed",
        "tool_name": "saxo_get_entitlements",
        "environment": environment,
        "endpoint_path": ENTITLEMENTS_PATH,
        "reason": error.code,
        "http_status": error.http_status,
        "detail": error.detail,
        "scope_used": False,
        "network_call_made": error.code in {"http_error", "invalid_entitlements_response"},
        "live_write_called": False,
        "order_or_subscription_created": False,
        "next_action": entitlements_next_action(error.code),
        "verifies": [],
        "does_not_verify": list(does_not_verify),
    }


def entitlement_auth_result(
    result: ToolResult,
    *,
    does_not_verify: tuple[str, ...],
) -> ToolResult:
    return {
        **result,
        "does_not_verify": list(does_not_verify),
    }


def entitlements_next_action(reason: str) -> str:
    return _ENTITLEMENTS_NEXT_ACTIONS.get(
        reason,
        "call saxo_auth_status before retrying saxo_get_entitlements",
    )
