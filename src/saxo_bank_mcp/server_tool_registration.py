# allow: SIZE_OK - static ToolRegistration catalog for all MCP function tools.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, cast

from mcp.types import AnyFunction

from saxo_bank_mcp.analytics_tool_descriptions import analytics_tool_description
from saxo_bank_mcp.fastmcp_logging_safety import SafeFastMCP
from saxo_bank_mcp.live_precheck_tool import create_live_precheck_tool
from saxo_bank_mcp.mcp_analytics_tools import (
    ANALYTICS_TOOL_FUNCTIONS,
    analytics_tool_output_schema,
)
from saxo_bank_mcp.mcp_auth_tools import (
    saxo_exchange_pkce_code,
    saxo_get_session_capabilities,
    saxo_refresh_token,
    saxo_start_pkce_login,
)
from saxo_bank_mcp.mcp_entitlement_tools import (
    ENTITLEMENTS_TOOL_DESCRIPTION,
    saxo_get_entitlements,
)
from saxo_bank_mcp.mcp_live_account_tools import (
    LIVE_ACCOUNTS_TOOL_DESCRIPTION,
    saxo_list_live_accounts,
)
from saxo_bank_mcp.mcp_order_tools import (
    ORDER_WRITE_TOOL_DESCRIPTION,
    PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION,
    saxo_cancel_multileg_order,
    saxo_cancel_multileg_sim_order,
    saxo_cancel_order,
    saxo_cancel_orders_by_instrument,
    saxo_cancel_sim_order,
    saxo_cancel_sim_orders_by_instrument,
    saxo_modify_multileg_order,
    saxo_modify_multileg_sim_order,
    saxo_modify_order,
    saxo_modify_sim_order,
    saxo_place_multileg_order,
    saxo_place_multileg_sim_order,
    saxo_place_order,
    saxo_place_sim_order,
)
from saxo_bank_mcp.mcp_portal_token_tools import (
    SIM_ACCESS_CACHE_TOOL_DESCRIPTION,
    saxo_cache_sim_access_token,
)
from saxo_bank_mcp.mcp_request_ledger_tools import (
    SAFE_REQUEST_LEDGER_TOOL_DESCRIPTION,
    saxo_get_safe_request_ledger,
)
from saxo_bank_mcp.mcp_safety_tools import (
    COMMIT_TOOL_DESCRIPTION,
    PREVIEW_TOOL_DESCRIPTION,
    SAFETY_STATUS_TOOL_DESCRIPTION,
    saxo_commit_write_preview,
    saxo_create_write_preview,
    saxo_safety_status,
)
from saxo_bank_mcp.mcp_streaming_tools import (
    STREAMING_CLEANUP_TOOL_DESCRIPTION,
    STREAMING_TOOL_DESCRIPTION,
    saxo_cleanup_streaming_subscriptions,
    saxo_create_streaming_price_subscription,
)
from saxo_bank_mcp.mcp_tool_results import (
    PKCE_EXCHANGE_TOOL_DESCRIPTION,
    PKCE_START_TOOL_DESCRIPTION,
    REFRESH_TOOL_DESCRIPTION,
    SESSION_CAPABILITIES_TOOL_DESCRIPTION,
)
from saxo_bank_mcp.mcp_trade_tools import (
    DISCLAIMER_LOOKUP_TOOL_DESCRIPTION,
    DISCLAIMER_RESPONSE_TOOL_DESCRIPTION,
    MULTILEG_DEFAULTS_TOOL_DESCRIPTION,
    ORDER_PREVIEW_TOOL_DESCRIPTION,
    saxo_create_order_preview,
    saxo_get_multileg_order_defaults,
    saxo_get_required_disclaimers,
    saxo_register_disclaimer_response,
)
from saxo_bank_mcp.mcp_trading_write_tools import (
    TRADING_WRITE_EXECUTE_DESCRIPTION,
    TRADING_WRITE_LIST_DESCRIPTION,
    TRADING_WRITE_PREPARE_DESCRIPTION,
    saxo_execute_trading_write,
    saxo_list_trading_write_operations,
    saxo_prepare_trading_write,
)
from saxo_bank_mcp.read_tools import (
    REGISTERED_CALL_TOOL_DESCRIPTION,
    saxo_call_registered_endpoint,
)
from saxo_bank_mcp.registry_list_tools import (
    READ_LIST_TOOL_DESCRIPTION,
    saxo_list_registered_endpoints,
)
from saxo_bank_mcp.server_core_tools import (
    AUTH_STATUS_TOOL_DESCRIPTION,
    HEALTH_TOOL_DESCRIPTION,
    saxo_auth_status,
    saxo_health,
)
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS, ANALYTICS_TOOL_IDS
from saxo_bank_mcp.tool_annotations import annotation_for_tool


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    tool_id: str
    description: str
    function: AnyFunction


FUNCTION_TOOL_REGISTRATIONS: Final[tuple[ToolRegistration, ...]] = (
    ToolRegistration("saxo_start_pkce_login", PKCE_START_TOOL_DESCRIPTION, saxo_start_pkce_login),
    ToolRegistration(
        "saxo_exchange_pkce_code",
        PKCE_EXCHANGE_TOOL_DESCRIPTION,
        saxo_exchange_pkce_code,
    ),
    ToolRegistration(
        "saxo_cache_sim_access_token",
        SIM_ACCESS_CACHE_TOOL_DESCRIPTION,
        saxo_cache_sim_access_token,
    ),
    ToolRegistration("saxo_refresh_token", REFRESH_TOOL_DESCRIPTION, saxo_refresh_token),
    ToolRegistration(
        "saxo_get_session_capabilities",
        SESSION_CAPABILITIES_TOOL_DESCRIPTION,
        saxo_get_session_capabilities,
    ),
    ToolRegistration("saxo_get_entitlements", ENTITLEMENTS_TOOL_DESCRIPTION, saxo_get_entitlements),
    ToolRegistration(
        "saxo_list_live_accounts",
        LIVE_ACCOUNTS_TOOL_DESCRIPTION,
        saxo_list_live_accounts,
    ),
    ToolRegistration(
        "saxo_get_safe_request_ledger",
        SAFE_REQUEST_LEDGER_TOOL_DESCRIPTION,
        saxo_get_safe_request_ledger,
    ),
    ToolRegistration(
        "saxo_list_registered_endpoints",
        READ_LIST_TOOL_DESCRIPTION,
        saxo_list_registered_endpoints,
    ),
    ToolRegistration(
        "saxo_call_registered_endpoint",
        REGISTERED_CALL_TOOL_DESCRIPTION,
        saxo_call_registered_endpoint,
    ),
    ToolRegistration("saxo_safety_status", SAFETY_STATUS_TOOL_DESCRIPTION, saxo_safety_status),
    ToolRegistration(
        "saxo_create_write_preview", PREVIEW_TOOL_DESCRIPTION, saxo_create_write_preview
    ),
    ToolRegistration(
        "saxo_commit_write_preview", COMMIT_TOOL_DESCRIPTION, saxo_commit_write_preview
    ),
    ToolRegistration(
        "saxo_create_order_preview",
        ORDER_PREVIEW_TOOL_DESCRIPTION,
        saxo_create_order_preview,
    ),
    ToolRegistration(
        "saxo_get_multileg_order_defaults",
        MULTILEG_DEFAULTS_TOOL_DESCRIPTION,
        saxo_get_multileg_order_defaults,
    ),
    ToolRegistration(
        "saxo_get_required_disclaimers",
        DISCLAIMER_LOOKUP_TOOL_DESCRIPTION,
        saxo_get_required_disclaimers,
    ),
    ToolRegistration(
        "saxo_register_disclaimer_response",
        DISCLAIMER_RESPONSE_TOOL_DESCRIPTION,
        saxo_register_disclaimer_response,
    ),
    ToolRegistration(
        "saxo_list_trading_write_operations",
        TRADING_WRITE_LIST_DESCRIPTION,
        saxo_list_trading_write_operations,
    ),
    ToolRegistration(
        "saxo_prepare_trading_write",
        TRADING_WRITE_PREPARE_DESCRIPTION,
        saxo_prepare_trading_write,
    ),
    ToolRegistration(
        "saxo_execute_trading_write",
        TRADING_WRITE_EXECUTE_DESCRIPTION,
        saxo_execute_trading_write,
    ),
    ToolRegistration("saxo_place_order", PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION, saxo_place_order),
    ToolRegistration(
        "saxo_modify_order", PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION, saxo_modify_order
    ),
    ToolRegistration(
        "saxo_cancel_order", PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION, saxo_cancel_order
    ),
    ToolRegistration(
        "saxo_cancel_orders_by_instrument",
        PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_cancel_orders_by_instrument,
    ),
    ToolRegistration(
        "saxo_place_multileg_order",
        PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_place_multileg_order,
    ),
    ToolRegistration(
        "saxo_modify_multileg_order",
        PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_modify_multileg_order,
    ),
    ToolRegistration(
        "saxo_cancel_multileg_order",
        PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_cancel_multileg_order,
    ),
    ToolRegistration("saxo_place_sim_order", ORDER_WRITE_TOOL_DESCRIPTION, saxo_place_sim_order),
    ToolRegistration("saxo_modify_sim_order", ORDER_WRITE_TOOL_DESCRIPTION, saxo_modify_sim_order),
    ToolRegistration("saxo_cancel_sim_order", ORDER_WRITE_TOOL_DESCRIPTION, saxo_cancel_sim_order),
    ToolRegistration(
        "saxo_cancel_sim_orders_by_instrument",
        ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_cancel_sim_orders_by_instrument,
    ),
    ToolRegistration(
        "saxo_place_multileg_sim_order",
        ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_place_multileg_sim_order,
    ),
    ToolRegistration(
        "saxo_modify_multileg_sim_order",
        ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_modify_multileg_sim_order,
    ),
    ToolRegistration(
        "saxo_cancel_multileg_sim_order",
        ORDER_WRITE_TOOL_DESCRIPTION,
        saxo_cancel_multileg_sim_order,
    ),
    ToolRegistration(
        "saxo_create_streaming_price_subscription",
        STREAMING_TOOL_DESCRIPTION,
        saxo_create_streaming_price_subscription,
    ),
    ToolRegistration(
        "saxo_cleanup_streaming_subscriptions",
        STREAMING_CLEANUP_TOOL_DESCRIPTION,
        saxo_cleanup_streaming_subscriptions,
    ),
    *(
        ToolRegistration(
            tool_id,
            analytics_tool_description(tool_id),
            cast("AnyFunction", function),
        )
        for tool_id, function in zip(
            ANALYTICS_TOOL_IDS,
            ANALYTICS_TOOL_FUNCTIONS,
            strict=True,
        )
    ),
)


def register_saxo_tools(
    mcp: SafeFastMCP,
    *,
    allowed_tools: frozenset[str] | None = None,
) -> None:
    """Register health/auth/function/live-precheck tools, optionally filtered.

    When allowed_tools is None every catalog tool is registered (production: 60).
    When set, only the exact logical subset is registered via public FastMCP APIs.
    """
    allowed = ALL_LOGICAL_TOOL_IDS if allowed_tools is None else allowed_tools
    _register_core_tools(mcp, allowed)
    for registration in FUNCTION_TOOL_REGISTRATIONS:
        if registration.tool_id not in allowed:
            continue
        output_schema = analytics_tool_output_schema(registration.tool_id)
        if output_schema is None:
            decorator = mcp.tool(
                description=registration.description,
                annotations=annotation_for_tool(registration.tool_id),
            )
        else:
            decorator = mcp.tool(
                description=registration.description,
                annotations=annotation_for_tool(registration.tool_id),
                output_schema=cast("dict[str, Any]", output_schema),
            )
        decorator(registration.function)
    if "saxo_precheck_live_order" in allowed:
        mcp.add_tool(create_live_precheck_tool(annotation_for_tool("saxo_precheck_live_order")))


def _register_core_tools(mcp: SafeFastMCP, allowed: frozenset[str]) -> None:
    if "saxo_health" in allowed:
        mcp.tool(
            description=HEALTH_TOOL_DESCRIPTION,
            annotations=annotation_for_tool("saxo_health"),
        )(saxo_health)
    if "saxo_auth_status" in allowed:
        mcp.tool(
            description=AUTH_STATUS_TOOL_DESCRIPTION,
            annotations=annotation_for_tool("saxo_auth_status"),
        )(saxo_auth_status)
