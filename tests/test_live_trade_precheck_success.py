from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from live_precheck_test_support import (
    FIXTURE_ACCOUNT_KEY,
    JSON_OBJECT_ADAPTER,
    LIVE_ACCOUNTS_TOOL,
    LIVE_PRECHECK_TOOL,
    configure_live,
    install_transport,
    order_payload,
    read_body,
)
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server import mcp


@pytest.mark.anyio
async def test_agent_can_chain_opaque_account_ref_into_clean_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)
    requests: list[tuple[str, str, JsonValue | None]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = (
            None if request.method == "GET" else JSON_OBJECT_ADAPTER.validate_json(request.content)
        )
        requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(
            200,
            json={
                "PreCheckResult": "Ok",
                "EstimatedCashRequired": 1,
                "EstimatedCashRequiredCurrency": "EUR",
            },
            request=request,
        )

    install_transport(monkeypatch, handler)
    async with Client(mcp) as client:
        accounts_result = await client.call_tool(LIVE_ACCOUNTS_TOOL, {})
        accounts = JSON_OBJECT_ADAPTER.validate_python(accounts_result.structured_content)
        account_rows = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
            accounts["accounts"],
        )
        account_ref = str(account_rows[0]["account_ref"])
        precheck_result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order_payload(account_ref)},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(precheck_result.structured_content)
    assert precheck_result.is_error is False
    assert payload["status"] == "precheck_accepted"
    assert payload["root_result_explicitly_ok"] is True
    assert payload["child_result_count"] == 0
    assert payload["all_returned_results_explicitly_ok"] is True
    assert payload["disclaimer_object_present"] is False
    assert payload["error_object_present"] is False
    assert payload["trade_readiness"] == "not_assessed"
    assert payload["precheck_request_accepted"] is True
    assert payload["account_ref"] == account_ref
    assert payload["account_id"] == "DISPLAY-1"
    assert payload["instrument_tradable"] is True
    assert payload["account_key_redacted"] is True
    assert payload["order_placement_endpoint_called"] is False
    assert payload["live_write_called"] is False
    assert account_rows[0]["account_id"] == "DISPLAY-1"
    assert "account_key" not in account_rows[0]
    assert "client_key" not in account_rows[0]
    assert "display_name" not in account_rows[0]
    assert FIXTURE_ACCOUNT_KEY not in str(payload)
    assert [method for method, _, _ in requests] == ["GET", "GET", "GET", "POST"]
    posted = TypeAdapter(dict[str, JsonValue]).validate_python(requests[-1][2])
    assert posted == {
        "AccountKey": f"{FIXTURE_ACCOUNT_KEY}_1",
        "Amount": 1.0,
        "AssetType": "Stock",
        "BuySell": "Buy",
        "FieldGroups": ["Costs", "MarginImpactBuySell"],
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "DayOrder"},
        "OrderType": "Market",
        "Uic": 30031,
    }
