from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from live_precheck_test_support import (
    JSON_OBJECT_ADAPTER,
    LIVE_ACCOUNTS_TOOL,
    LIVE_PRECHECK_TOOL,
    accounts_body,
    capture_fastmcp_debug,
    configure_live,
    install_transport,
    instrument_body,
    order_payload,
)
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.endpoint_registry import load_inventory
from saxo_bank_mcp.live_precheck_tool import create_live_precheck_tool
from saxo_bank_mcp.mcp_live_trade_tools import LiveOrderPrecheckRequest
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.tool_metadata import tool_metadata


@pytest.mark.anyio
async def test_precheck_validation_errors_request_pydantic_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def redacted_errors(
        _self: ValidationError,
        **kwargs: bool,
    ) -> list[dict[str, JsonValue]]:
        assert kwargs == {"include_input": False, "include_context": False, "include_url": False}
        return [
            {
                "loc": ["order", "buy_sell"],
                "type": "literal_error",
                "msg": "unsafe fallback",
                "input": "BAD_INPUT",
                "ctx": {"expected": "BAD_CTX"},
                "url": "https://errors.pydantic.dev/test",
            },
        ]

    monkeypatch.setattr(ValidationError, "errors", redacted_errors)
    order = order_payload()
    order["buy_sell"] = "BAD_INPUT"
    result = await create_live_precheck_tool().run({"order": order})

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)

    assert payload["validation_errors"] == [
        {
            "location": ["order", "buy_sell"],
            "type": "literal_error",
            "message": "Use one of the values allowed by the schema.",
        },
    ]
    for marker in ("BAD_INPUT", "BAD_CTX", "errors.pydantic.dev"):
        assert marker not in json.dumps(payload)


@pytest.mark.anyio
async def test_live_account_and_precheck_tools_are_registered_as_live_safe() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert LIVE_ACCOUNTS_TOOL in tools
    assert LIVE_PRECHECK_TOOL in tools
    input_schema = tools[LIVE_PRECHECK_TOOL].inputSchema
    assert input_schema["additionalProperties"] is False
    assert input_schema["required"] == ["order"]
    order_schema = input_schema["properties"]["order"]
    assert order_schema["additionalProperties"] is False
    assert order_schema["required"] == ["uic", "asset_type", "amount", "buy_sell"]
    metadata = tool_metadata()
    for tool_name in (LIVE_ACCOUNTS_TOOL, LIVE_PRECHECK_TOOL):
        assert metadata[tool_name]["environment_support"] == ["LIVE_READ"]
        assert metadata[tool_name]["write_effect"] == "none"
        assert metadata[tool_name]["state_changing"] is False
    assert metadata[LIVE_PRECHECK_TOOL]["tool_class"] == "live_precheck"
    assert metadata[LIVE_PRECHECK_TOOL]["endpoint_operation_id"] == "post.trade.v2.orders.precheck"
    assert metadata[LIVE_PRECHECK_TOOL]["endpoint_inventory_class"] == ("write_or_subscription")
    account_hint = metadata[LIVE_ACCOUNTS_TOOL]["agent_hint"]
    assert isinstance(account_hint, str)
    assert "process-scoped opaque references" in account_hint
    assert "account/client keys remain internal" in account_hint
    assert "navigation keys" not in account_hint
    accounts_description = tools[LIVE_ACCOUNTS_TOOL].description
    precheck_description = tools[LIVE_PRECHECK_TOOL].description
    assert isinstance(accounts_description, str)
    assert isinstance(precheck_description, str)
    assert "keys remain internal" in accounts_description
    assert "keys remain internal" in precheck_description
    assert "IDs and keys may be returned" not in precheck_description
    inventory_operation = next(
        operation
        for operation in load_inventory().operations
        if operation.operation_id == metadata[LIVE_PRECHECK_TOOL]["endpoint_operation_id"]
    )
    assert metadata[LIVE_PRECHECK_TOOL]["endpoint_inventory_class"] == (
        inventory_operation.read_write_class
    )


@pytest.mark.anyio
async def test_precheck_rejects_infinite_amount_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=accounts_body(), request=request)

    install_transport(monkeypatch, handler)
    order = order_payload()
    order["amount"] = float("inf")
    with pytest.raises(ValidationError):
        LiveOrderPrecheckRequest.model_validate(order)
    async with Client(mcp) as client:
        result = await client.call_tool(LIVE_PRECHECK_TOOL, {"order": order}, raise_on_error=False)

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "invalid_request"
    assert payload["reason"] == "request_schema_invalid"
    assert payload["validation_errors"] == [
        {
            "location": ["order", "amount"],
            "type": "float_type",
            "message": "Use a finite number.",
        },
    ]
    assert payload["network_call_made"] is False
    assert payload["precheck_request_accepted"] is False
    assert payload["order_placement_endpoint_called"] is False
    assert requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("uic", "30031"), ("amount", True)],
)
@pytest.mark.anyio
async def test_precheck_rejects_coercible_scalars_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: JsonValue,
) -> None:
    configure_live(tmp_path, monkeypatch)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=accounts_body(), request=request)

    install_transport(monkeypatch, handler)
    order = order_payload()
    order[field] = value
    with pytest.raises(ValidationError):
        LiveOrderPrecheckRequest.model_validate(order)
    async with Client(mcp) as client:
        result = await client.call_tool(LIVE_PRECHECK_TOOL, {"order": order}, raise_on_error=False)

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "invalid_request"
    assert payload["reason"] == "request_schema_invalid"
    assert payload["network_call_made"] is False
    assert payload["precheck_request_accepted"] is False
    assert payload["live_write_called"] is False
    assert requests == []


@pytest.mark.anyio
async def test_precheck_redacts_valid_arguments_before_fastmcp_debug_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)
    sensitive_marker = "DEBUG-ACCOUNT-" + ("x" * 48)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(200, json={"PreCheckResult": "Ok"}, request=request)
        if request.url.path.endswith("/ref/v1/instruments/details/30031/Stock"):
            return httpx2.Response(200, json=instrument_body(), request=request)
        accounts = accounts_body()
        account_rows = TypeAdapter(list[dict[str, JsonValue]]).validate_python(accounts["Data"])
        account_rows[0]["AccountId"] = sensitive_marker
        accounts["Data"] = account_rows
        return httpx2.Response(200, json=accounts, request=request)

    install_transport(monkeypatch, handler)
    order = order_payload()
    order["account_id"] = sensitive_marker
    with capture_fastmcp_debug() as captured:
        async with Client(mcp) as client:
            result = await client.call_tool(
                LIVE_PRECHECK_TOOL,
                {"order": order},
                raise_on_error=False,
            )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is False
    assert payload["status"] == "precheck_accepted"
    assert sensitive_marker not in captured()
