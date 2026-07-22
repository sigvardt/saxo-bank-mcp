from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from live_precheck_test_support import (
    AMBIGUOUS_ACCOUNT_COUNT,
    JSON_OBJECT_ADAPTER,
    LIVE_ACCOUNTS_TOOL,
    LIVE_PRECHECK_TOOL,
    accounts_body,
    configure_live,
    install_transport,
    instrument_body,
    order_payload,
)
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server import mcp


@pytest.mark.anyio
async def test_precheck_refuses_ambiguous_accounts_without_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)
    methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        return httpx2.Response(
            200,
            json=accounts_body(count=AMBIGUOUS_ACCOUNT_COUNT),
            request=request,
        )

    install_transport(monkeypatch, handler)
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order_payload()},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "account_selection_required"
    assert payload["active_account_count"] == AMBIGUOUS_ACCOUNT_COUNT
    assert payload["order_placement_endpoint_called"] is False
    assert payload["live_write_called"] is False
    assert methods == ["GET"]
    accounts = TypeAdapter(list[dict[str, JsonValue]]).validate_python(payload["accounts"])
    assert accounts[0]["account_id"] == "DISPLAY-1"
    assert "account_key" not in accounts[0]
    assert "client_key" not in accounts[0]
    assert "display_name" not in accounts[0]


@pytest.mark.parametrize(
    ("selector", "value", "expected_status"),
    [
        ("account_id", "UNKNOWN", "account_id_invalid"),
        ("account_ref", "unknown-ref", "account_ref_invalid"),
    ],
)
@pytest.mark.anyio
async def test_precheck_refuses_invalid_account_selector_without_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
    value: str,
    expected_status: str,
) -> None:
    configure_live(tmp_path, monkeypatch)
    methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        return httpx2.Response(200, json=accounts_body(), request=request)

    install_transport(monkeypatch, handler)
    order = order_payload()
    order[selector] = value
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == expected_status
    assert payload["reason"] == f"{expected_status}_for_current_login"
    assert payload["precheck_endpoint_called"] is False
    assert payload["order_placement_endpoint_called"] is False
    assert payload["live_write_called"] is False
    assert methods == ["GET"]


@pytest.mark.parametrize("duplicate_field", ["AccountId", "AccountKey"])
@pytest.mark.anyio
async def test_precheck_rejects_duplicate_account_identity_before_instrument_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_field: str,
) -> None:
    configure_live(tmp_path, monkeypatch)
    account_body = accounts_body(count=2)
    accounts = TypeAdapter(list[dict[str, JsonValue]]).validate_python(account_body["Data"])
    accounts[1][duplicate_field] = accounts[0][duplicate_field]
    account_body["Data"] = accounts
    methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        return httpx2.Response(200, json=account_body, request=request)

    install_transport(monkeypatch, handler)
    order = order_payload()
    order["account_id"] = "DISPLAY-1"
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "invalid_account_response"
    assert payload["instrument_lookup_endpoint_called"] is False
    assert payload["precheck_endpoint_called"] is False
    assert methods == ["GET"]


@pytest.mark.parametrize(
    ("instrument_body", "expected_reason"),
    [
        ({}, "instrument_response_schema_invalid"),
        (
            {"AssetType": "Stock", "IsTradable": True, "Uic": 999},
            "instrument_identity_mismatch",
        ),
        (instrument_body(tradable=False), "instrument_not_tradable"),
    ],
)
@pytest.mark.anyio
async def test_precheck_refuses_ineligible_instrument_without_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instrument_body: dict[str, JsonValue],
    expected_reason: str,
) -> None:
    configure_live(tmp_path, monkeypatch)
    methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        body = (
            instrument_body
            if request.url.path.endswith("/ref/v1/instruments/details/30031/Stock")
            else accounts_body()
        )
        return httpx2.Response(200, json=body, request=request)

    install_transport(monkeypatch, handler)
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order_payload()},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "instrument_not_eligible"
    assert payload["reason"] == expected_reason
    assert payload["instrument_lookup_endpoint_called"] is True
    assert payload["precheck_endpoint_called"] is False
    assert payload["order_placement_endpoint_called"] is False
    assert payload["live_write_called"] is False
    assert methods == ["GET", "GET"]

@pytest.mark.anyio
@pytest.mark.parametrize(
    "account_body",
    [
        {},
        {"ErrorCode": "Failure"},
        {"data": []},
        {"Data": [], "ErrorCode": "Failure"},
        {"Data": [], "__count": 1},
    ],
)
async def test_live_accounts_reject_unknown_success_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_body: dict[str, JsonValue],
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=account_body, request=request)

    install_transport(monkeypatch, handler)
    async with Client(mcp) as client:
        result = await client.call_tool(LIVE_ACCOUNTS_TOOL, {}, raise_on_error=False)

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert result.is_error is True
    assert payload["status"] == "invalid_account_response"
