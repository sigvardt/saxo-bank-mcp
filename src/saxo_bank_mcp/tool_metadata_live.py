from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from saxo_bank_mcp.tool_metadata_types import ToolMetadata

LIVE_TOOL_METADATA: Final[Mapping[str, ToolMetadata]] = MappingProxyType(
    {
        "saxo_list_live_accounts": {
            "tool_class": "network_read",
            "environment_support": ["LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Returns visible account IDs and process-scoped opaque references for LIVE tools. "
                "Saxo account/client keys remain internal; the tool never performs a write."
            ),
        },
        "saxo_precheck_live_order": {
            "tool_class": "live_precheck",
            "environment_support": ["LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "endpoint_operation_id": "post.trade.v2.orders.precheck",
            "endpoint_inventory_class": "write_or_subscription",
            "agent_hint": (
                "Calls Saxo's POST precheck endpoint under Personal Read permission. The inventory "
                "classifies POST as write_or_subscription, but this dedicated tool cannot place, "
                "change, or cancel an order. Verify the safe request ledger and account state."
            ),
        },
        "saxo_get_safe_request_ledger": {
            "tool_class": "local_safety_evidence",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Clear it before a task, then read it afterward to inspect safe HTTP methods and "
                "paths for the current MCP session. It never exposes request or response bodies."
            ),
        },
    },
)
