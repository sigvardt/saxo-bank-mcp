from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.tool_metadata import tool_metadata

TOOL_PAYLOAD_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


async def call_tool_payload(
    name: str,
    arguments: dict[str, JsonValue],
    *,
    raise_on_error: bool = True,
) -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=raise_on_error)
    return TOOL_PAYLOAD_ADAPTER.validate_python(result.structured_content)


async def call_live_session_capabilities_payload() -> dict[str, JsonValue]:
    return await call_tool_payload("saxo_get_session_capabilities", {})


async def call_live_read_payloads() -> dict[str, dict[str, JsonValue]]:
    registered_read = "saxo_call_registered_endpoint"
    return {
        "saxo_get_session_capabilities": await call_live_session_capabilities_payload(),
        "saxo_get_entitlements": await call_tool_payload("saxo_get_entitlements", {}),
        "saxo_list_registered_endpoints": await call_tool_payload(
            "saxo_list_registered_endpoints",
            {"limit": 1},
        ),
        "saxo_call_registered_endpoint_public_diagnostics": await call_tool_payload(
            registered_read,
            {"method": "GET", "path": "/root/v1/diagnostics/get"},
            raise_on_error=False,
        ),
        "saxo_call_registered_endpoint_authenticated_account": await call_tool_payload(
            registered_read,
            {"method": "GET", "path": "/port/v1/accounts/me"},
            raise_on_error=False,
        ),
        "saxo_call_registered_endpoint_balances": await call_tool_payload(
            registered_read,
            {
                "method": "GET",
                "path": "/port/v1/balances/me",
                "response_mode": "fingerprint_only",
            },
            raise_on_error=False,
        ),
        "saxo_call_registered_endpoint_positions": await call_tool_payload(
            registered_read,
            {
                "method": "GET",
                "path": "/port/v1/positions/me",
                "params": {"$top": "1"},
            },
            raise_on_error=False,
        ),
        "saxo_call_registered_endpoint_orders": await call_tool_payload(
            registered_read,
            {
                "method": "GET",
                "path": "/port/v1/orders/me",
                "params": {"$top": "1"},
            },
            raise_on_error=False,
        ),
        "saxo_call_registered_endpoint_prices": await call_tool_payload(
            registered_read,
            {
                "method": "GET",
                "path": "/trade/v1/infoprices",
                "params": {"Uic": "21", "AssetType": "FxSpot"},
            },
            raise_on_error=False,
        ),
    }


async def call_live_write_refusal_payload() -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {"preview_token": "LIVE-WRITE-REFUSAL-PROBE"}
    return await call_tool_payload("saxo_place_sim_order", arguments, raise_on_error=False)


async def call_live_read_refusal_payload() -> dict[str, JsonValue]:
    return await call_tool_payload(
        "saxo_call_registered_endpoint",
        {"method": "GET", "path": "/root/v1/diagnostics/get"},
        raise_on_error=False,
    )


async def call_tool_inventory_payload() -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    all_tools = sorted(tool.name for tool in tools)
    metadata = tool_metadata()
    missing_metadata = [name for name in all_tools if name not in metadata]
    unregistered_metadata = [name for name in sorted(metadata) if name not in all_tools]
    live_network_reads = [
        name
        for name, item in metadata.items()
        if item["tool_class"] == "network_read" and item["safe_in_live_read_mode"] is True
    ]
    state_changing_or_write = [
        name
        for name, item in metadata.items()
        if item["state_changing"] is True or item["write_effect"] != "none"
    ]
    return {
        "status": "passed" if not missing_metadata and not unregistered_metadata else "failed",
        "tool_count": len(all_tools),
        "all_tools": all_tools,
        "tool_metadata": metadata,
        "local_metadata_read_tools": _tools_by_class(metadata, ("local_metadata_read",)),
        "live_network_read_tools": live_network_reads,
        "sim_only_network_read_tools": _tools_by_class(metadata, ("sim_only_network_read",)),
        "write_or_state_changing_tools": state_changing_or_write,
        "metadata_missing_tools": missing_metadata,
        "metadata_unregistered_tools": unregistered_metadata,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }


def _tools_by_class(
    metadata: Mapping[str, Mapping[str, JsonValue]],
    tool_classes: tuple[str, ...],
) -> list[str]:
    return sorted(name for name, item in metadata.items() if item.get("tool_class") in tool_classes)
