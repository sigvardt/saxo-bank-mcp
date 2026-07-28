from __future__ import annotations

from typing import Final

# Canonical logical tool IDs registered by the MCP server (39 total).
# Keep in sync with server_tool_registration FUNCTION tools + health/auth/precheck.
FUNCTION_TOOL_IDS: Final[tuple[str, ...]] = (
    "saxo_start_pkce_login",
    "saxo_exchange_pkce_code",
    "saxo_cache_sim_access_token",
    "saxo_refresh_token",
    "saxo_get_session_capabilities",
    "saxo_get_entitlements",
    "saxo_list_live_accounts",
    "saxo_get_safe_request_ledger",
    "saxo_list_registered_endpoints",
    "saxo_call_registered_endpoint",
    "saxo_safety_status",
    "saxo_create_write_preview",
    "saxo_commit_write_preview",
    "saxo_create_order_preview",
    "saxo_get_multileg_order_defaults",
    "saxo_get_required_disclaimers",
    "saxo_register_disclaimer_response",
    "saxo_list_trading_write_operations",
    "saxo_prepare_trading_write",
    "saxo_execute_trading_write",
    "saxo_place_order",
    "saxo_modify_order",
    "saxo_cancel_order",
    "saxo_cancel_orders_by_instrument",
    "saxo_place_multileg_order",
    "saxo_modify_multileg_order",
    "saxo_cancel_multileg_order",
    "saxo_place_sim_order",
    "saxo_modify_sim_order",
    "saxo_cancel_sim_order",
    "saxo_cancel_sim_orders_by_instrument",
    "saxo_place_multileg_sim_order",
    "saxo_modify_multileg_sim_order",
    "saxo_cancel_multileg_sim_order",
    "saxo_create_streaming_price_subscription",
    "saxo_cleanup_streaming_subscriptions",
)
CORE_TOOL_IDS: Final[tuple[str, ...]] = (
    "saxo_health",
    "saxo_auth_status",
    "saxo_precheck_live_order",
)
ALL_LOGICAL_TOOL_IDS: Final[frozenset[str]] = frozenset((*CORE_TOOL_IDS, *FUNCTION_TOOL_IDS))
EXPECTED_TOOL_COUNT: Final = 39

if len(ALL_LOGICAL_TOOL_IDS) != EXPECTED_TOOL_COUNT:  # pragma: no cover - import invariant
    message = f"tool catalog size {len(ALL_LOGICAL_TOOL_IDS)} != {EXPECTED_TOOL_COUNT}"
    raise RuntimeError(message)
