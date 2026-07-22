from __future__ import annotations

import pytest
from fastmcp import Client

from saxo_bank_mcp.server import mcp

EXPECTED_OPERATION_COUNT = 294
TRADING_IMPLEMENTED_READS = 7
TRADING_OPERATION_COUNT = 45
TRADING_PAGE_LIMIT = 25
TRADING_REFUSED_OPERATIONS = 0
TRADING_REGISTERED_WRITES = 38


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_read_tools_are_registered_with_agent_safe_descriptions() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    descriptions = {tool.name: tool.description or "" for tool in tools}
    assert "saxo_list_registered_endpoints" in descriptions
    assert "saxo_call_registered_endpoint" in descriptions
    assert (
        "registered Saxo OpenAPI GET/read operations only"
        in descriptions["saxo_call_registered_endpoint"]
    )
    assert "explicitly enabled LIVE read mode" in descriptions["saxo_call_registered_endpoint"]
    assert "safe SIM GET read operations" not in descriptions["saxo_call_registered_endpoint"]
    assert (
        "denies unregistered, arbitrary-host, and write-class operations before any network call"
        in descriptions["saxo_call_registered_endpoint"]
    )


@pytest.mark.anyio
async def test_list_registered_endpoints_uses_registry_safe_status_and_counts() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_list_registered_endpoints",
            {"service_group": "Trading", "limit": TRADING_PAGE_LIMIT},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "metadata_only_not_ready_for_trading"
    assert payload["registry_load_state"] == "loaded"
    assert payload["inventory_validation_status"] == "self_consistent"
    assert payload["total_operation_count"] == EXPECTED_OPERATION_COUNT
    assert payload["matched_count"] == TRADING_OPERATION_COUNT
    assert payload["returned_count"] == TRADING_PAGE_LIMIT
    assert payload["next_offset"] == TRADING_PAGE_LIMIT
    assert payload["truncated"] is True
    assert payload["live_source_fetched"] is False
    assert payload["network_call_made"] is False
    assert payload["live_write_called"] is False
    assert payload["order_or_subscription_created"] is False
    assert payload["registry_only"] is True
    assert payload["execution_readiness_verified"] is False
    assert payload["live_access"] is False
    assert payload["account_access_verified"] is False
    assert payload["trading_ready"] is False
    assert payload["official_group_completeness_verified"] is False
    assert payload["official_group_source_url"] == (
        "https://www.developer.saxo/openapi/referencedocs"
    )
    assert payload["official_group_reconciliation"] == "not_run"
    assert payload["coverage_basis"] == "curated_snapshot_not_reconciled_with_live"
    assert payload["warning_notice"].startswith("Registry metadata only")
    assert isinstance(payload["snapshot_age_days"], int)
    assert isinstance(payload["snapshot_stale"], bool)
    assert payload["snapshot_completeness_scope"] == (
        "statically_checked_in_not_reconciled_with_live"
    )
    assert payload["unknown_service_group"] is False
    assert "snapshot_date" in payload
    assert "retrieved_at" not in payload
    for missing_check in (
        "Saxo connectivity",
        "credentials/session",
        "account access",
        "catalog completeness/freshness vs live Saxo",
    ):
        assert missing_check in payload["does_not_verify"]
    support = {item["service_group"]: item for item in payload["service_group_support"]}
    assert support["Trading"]["registered_read_definitions"] == TRADING_IMPLEMENTED_READS
    assert support["Trading"]["registered_write_definitions"] == TRADING_REGISTERED_WRITES
    assert support["Trading"]["refused_operations"] == TRADING_REFUSED_OPERATIONS
    assert all(
        "implemented" not in key and "ready" not in key and "available" not in key
        for item in payload["service_group_support"]
        for key in item
    )
    assert all("status" not in operation for operation in payload["operations"])
    assert all("mcp_support_status" not in operation for operation in payload["operations"])
    assert all("mcp_support_policy" in operation for operation in payload["operations"])
    assert all(
        "available" not in str(operation["mcp_support_policy"])
        and "ready" not in str(operation["mcp_support_policy"])
        for operation in payload["operations"]
    )
    assert all(
        "todo" not in str(operation["refusal_reason"]) for operation in payload["operations"]
    )
    assert all(
        operation["live_reachability_verified"] is False for operation in payload["operations"]
    )


@pytest.mark.anyio
async def test_list_registered_endpoints_flags_unknown_service_group() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_list_registered_endpoints",
            {"service_group": "Not A Saxo Group", "limit": 5},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "metadata_only_not_ready_for_trading"
    assert payload["registry_load_state"] == "loaded"
    assert payload["unknown_service_group"] is True
    assert payload["service_group_error"] == "unknown_service_group"
    assert isinstance(payload["service_group_suggestions"], list)
    assert payload["matched_count"] == 0
    assert payload["returned_count"] == 0
    assert "Trading" in payload["valid_service_groups"]
