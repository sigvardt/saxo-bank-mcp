from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.tool_metadata_live import LIVE_TOOL_METADATA
from saxo_bank_mcp.tool_metadata_types import ToolEnvironment, ToolMetadata, WriteEffect

__all__ = (
    "ToolEnvironment",
    "ToolMetadata",
    "WriteEffect",
    "metadata_for_tool",
    "tool_metadata",
)


_TOOLS: Mapping[str, ToolMetadata] = MappingProxyType(
    {
        "saxo_health": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Use for MCP process liveness only, then call saxo_auth_status.",
        },
        "saxo_auth_status": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Use before network reads to check local config and token-cache state.",
        },
        "saxo_start_pkce_login": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM PKCE login helper. Do not use for LIVE read validation.",
        },
        "saxo_exchange_pkce_code": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM PKCE token-cache helper. Do not use for LIVE read validation.",
        },
        "saxo_cache_sim_access_token": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Caches a SIM portal token only.",
        },
        "saxo_refresh_token": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Refreshes a SIM token cache only.",
        },
        "saxo_get_session_capabilities": {
            "tool_class": "network_read",
            "environment_support": ["SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Safe live-read probe when LIVE reads and a LIVE token cache are configured."
            ),
        },
        "saxo_get_entitlements": {
            "tool_class": "network_read",
            "environment_support": ["SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Reads entitlement summary without placing orders.",
        },
        "saxo_list_registered_endpoints": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Registry metadata only. It does not prove Saxo connectivity.",
        },
        "saxo_call_registered_endpoint": {
            "tool_class": "network_read",
            "environment_support": ["SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Use only for registered GET/read operations. It denies writes before network."
            ),
        },
        "saxo_safety_status": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Reports local write-safety config and tool metadata. It does not call Saxo."
            ),
        },
        "saxo_create_write_preview": {
            "tool_class": "local_write_preview",
            "environment_support": ["LOCAL", "SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Creates only a local simulation preview token. It does not call Saxo.",
        },
        "saxo_commit_write_preview": {
            "tool_class": "local_write_preview",
            "environment_support": ["LOCAL", "SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Approves only local simulation. It does not call Saxo.",
        },
        "saxo_create_order_preview": {
            "tool_class": "sim_trade_precheck",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": False,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM-only trade pre-check. It refuses before network when configured for LIVE."
            ),
        },
        "saxo_get_multileg_order_defaults": {
            "tool_class": "sim_only_network_read",
            "environment_support": ["SIM"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM-only GET helper. Use saxo_call_registered_endpoint for LIVE read checks."
            ),
        },
        "saxo_get_required_disclaimers": {
            "tool_class": "sim_only_network_read",
            "environment_support": ["SIM"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM-only GET helper. Use saxo_call_registered_endpoint for LIVE read checks."
            ),
        },
        "saxo_register_disclaimer_response": {
            "tool_class": "sim_trade_state_change",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM-only disclaimer response. Do not use for LIVE read validation.",
        },
        "saxo_place_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_modify_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_cancel_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_cancel_sim_orders_by_instrument": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_place_multileg_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_modify_multileg_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_cancel_multileg_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_create_streaming_price_subscription": {
            "tool_class": "sim_streaming_state",
            "environment_support": ["SIM"],
            "write_effect": "sim_streaming",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM streaming subscription tool. Do not use for LIVE read validation.",
        },
        "saxo_cleanup_streaming_subscriptions": {
            "tool_class": "sim_streaming_cleanup",
            "environment_support": ["SIM"],
            "write_effect": "sim_streaming",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "Cleans SIM streaming subscriptions. Do not use for LIVE read validation."
            ),
        },
        **LIVE_TOOL_METADATA,
    },
)


def tool_metadata() -> dict[str, dict[str, JsonValue]]:
    return {name: cast("dict[str, JsonValue]", dict(metadata)) for name, metadata in _TOOLS.items()}


def metadata_for_tool(tool_name: str) -> ToolMetadata | None:
    return _TOOLS.get(tool_name)
