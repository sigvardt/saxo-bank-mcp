from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client

import saxo_bank_mcp.analytics_source_receipt as receipt_module
import saxo_bank_mcp.read_tools as read_tools_module
from saxo_bank_mcp.analytics_source_receipt import analytics_contract_receipt
from saxo_bank_mcp.endpoint_registry import find_registered_endpoint
from saxo_bank_mcp.read_tool_types import ReadExecutionContext
from saxo_bank_mcp.registered_read_execution import RegisteredReadResponse
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.server_tool_ids import EXPECTED_TOOL_COUNT

_FIXTURE_ROOT = Path(__file__).parent / "fixtures/analytics/saxo_pages"
_EXPECTED_PAGE_COUNT = 2
_EXPECTED_RETRY_COUNT = 3


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_server_receipt_validates_raw_pages_but_returns_only_safe_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = [
        (_FIXTURE_ROOT / "chart_page_1.json").read_bytes(),
        (_FIXTURE_ROOT / "chart_page_2.json").read_bytes(),
    ]

    async def execute(
        _operation: object,
        request_target: str,
        params: Mapping[str, str],
        **_kwargs: object,
    ) -> RegisteredReadResponse:
        assert request_target == "/chart/v3/charts"
        if len(payloads) == 1:
            assert params["$skiptoken"] == "synthetic-page-2"
        response = httpx2.Response(
            200,
            content=payloads.pop(0),
            request=httpx2.Request("GET", "https://registered.invalid"),
        )
        return RegisteredReadResponse(
            context=ReadExecutionContext(
                environment="SIM",
                rest_base_url="https://registered.invalid",
                token=None,
            ),
            response=response,
        )

    monkeypatch.setattr(receipt_module, "execute_registered_get", execute)
    registered = find_registered_endpoint("GET", "/chart/v3/charts")
    assert registered is not None
    receipt = await analytics_contract_receipt(
        registered,
        contract_id="chart_v3",
        params={"AssetType": "Stock", "Count": "2", "Uic": "1001"},
    )
    serialized = json.dumps(receipt, sort_keys=True)

    assert receipt["status"] == "passed"
    assert receipt["response"] is None
    assert receipt["response_visibility"] == "analytics_contract_receipt"
    assert receipt["response_fingerprint_scope"] == "analytics_contract_receipt"
    assert receipt["page_count"] == _EXPECTED_PAGE_COUNT
    assert receipt["row_count"] == _EXPECTED_PAGE_COUNT
    assert receipt["network_call_count"] == _EXPECTED_PAGE_COUNT
    for private in (
        "synthetic-page-2",
        "2026-07-29T08:00:00Z",
        "2026-07-29T08:01:00Z",
        "101.0",
        "102.0",
        "__next",
        "DataVersion",
    ):
        assert private not in serialized


@pytest.mark.anyio
async def test_fastmcp_exposes_and_executes_receipt_on_existing_read_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def safe_receipt(
        _registered: object,
        *,
        contract_id: str,
        params: Mapping[str, str],
    ) -> dict[str, object]:
        assert contract_id == "chart_v3"
        assert params == {"AssetType": "Stock", "Count": "2", "Uic": "1001"}
        return {
            "status": "passed",
            "analytics_contract_id": contract_id,
            "response": None,
            "response_visibility": "analytics_contract_receipt",
            "response_fingerprint_scope": "analytics_contract_receipt",
            "network_call_made": True,
            "live_write_called": False,
        }

    monkeypatch.setattr(read_tools_module, "analytics_contract_receipt", safe_receipt)
    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/chart/v3/charts",
                "params": {"AssetType": "Stock", "Count": "2", "Uic": "1001"},
                "response_mode": "analytics_contract_receipt",
                "analytics_contract_id": "chart_v3",
            },
        )

    assert len(tools) == EXPECTED_TOOL_COUNT
    read_tool = next(tool for tool in tools if tool.name == "saxo_call_registered_endpoint")
    response_modes = read_tool.inputSchema["properties"]["response_mode"]["enum"]
    assert "analytics_contract_receipt" in response_modes
    assert "analytics_contract_id" in read_tool.inputSchema["properties"]
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["response"] is None
    assert payload["response_visibility"] == "analytics_contract_receipt"


@pytest.mark.anyio
async def test_pre_execution_refusal_does_not_invent_a_sim_environment() -> None:
    registered = find_registered_endpoint("GET", "/chart/v3/charts")
    assert registered is not None

    receipt = await analytics_contract_receipt(
        registered,
        contract_id="transactions_v1",
        params={},
    )

    assert receipt["status"] == "refused"
    assert receipt["environment"] == "UNKNOWN"
    assert receipt["network_call_made"] is False
    assert receipt["network_call_count"] == 0


@pytest.mark.anyio
async def test_failed_network_attempt_is_counted_without_exposing_response_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(
        _operation: object,
        _request_target: str,
        _params: Mapping[str, str],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "status": "network_error",
            "reason": "registered_read_failed",
            "environment": "SIM",
            "network_call_made": True,
        }

    monkeypatch.setattr(receipt_module, "execute_registered_get", execute)
    registered = find_registered_endpoint("GET", "/chart/v3/charts")
    assert registered is not None
    receipt = await analytics_contract_receipt(
        registered,
        contract_id="chart_v3",
        params={"AssetType": "Stock", "Count": "2", "Uic": "1001"},
    )

    assert receipt["status"] == "refused"
    assert receipt["environment"] == "SIM"
    assert receipt["network_call_made"] is True
    assert receipt["network_call_count"] == _EXPECTED_RETRY_COUNT
    assert receipt["response"] is None
