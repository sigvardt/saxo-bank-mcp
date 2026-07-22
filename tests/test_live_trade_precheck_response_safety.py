"""LIVE precheck response-safety regression matrix. # noqa: SIZE_OK."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from live_precheck_test_support import (
    DECODER_LIMIT_PAYLOADS,
    JSON_OBJECT_ADAPTER,
    LIVE_PRECHECK_TOOL,
    NESTED_QUALIFIER_RESPONSES,
    configure_live,
    install_transport,
    order_payload,
    read_body,
)
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server import mcp


@pytest.mark.anyio
async def test_live_precheck_rejects_nested_numeric_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(
            200,
            content=b'{"PreCheckResult":"Ok","Cost":{"Overflow":1e999}}',
            headers={"Content-Type": "application/json"},
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
    assert payload["status"] == "invalid_precheck_response"
    assert payload["precheck_request_accepted"] is False


@pytest.mark.parametrize("payload_factory", DECODER_LIMIT_PAYLOADS)
@pytest.mark.anyio
async def test_live_precheck_normalizes_decoder_limit_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_factory: Callable[[], bytes],
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json=read_body(request), request=request)
        return httpx2.Response(
            200,
            content=payload_factory(),
            headers={"Content-Type": "application/json"},
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
    assert payload["status"] == "invalid_precheck_response"
    assert payload["precheck_request_accepted"] is False

@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response_body", "expected_status", "expected_blocker"),
    [
        (
            {"PreCheckResult": "Ok"},
            "precheck_accepted",
            None,
        ),
        (
            {"PreCheckResult": "Ok", "ErrorInfo": {"ErrorCode": "IllegalRequest"}},
            "precheck_rejected",
            "saxo_error:IllegalRequest",
        ),
        (
            {
                "PreCheckResult": "Ok",
                "PreTradeDisclaimers": {"DisclaimerTokens": ["hidden-token"]},
            },
            "disclaimer_required",
            "pretrade_disclaimer",
        ),
    ],
)
async def test_precheck_qualifiers_are_machine_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_body: dict[str, JsonValue],
    expected_status: str,
    expected_blocker: str | None,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = read_body(request) if request.method == "GET" else response_body
        return httpx2.Response(200, json=body, request=request)

    install_transport(monkeypatch, handler)
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order_payload()},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    blockers = TypeAdapter(list[str]).validate_python(payload["trade_blockers"])
    precheck_blockers = TypeAdapter(list[str]).validate_python(payload["precheck_blockers"])
    assert result.is_error is (expected_blocker is not None)
    assert payload["status"] == expected_status
    assert payload["precheck_request_accepted"] is (expected_blocker is None)
    assert payload["trade_readiness"] == "not_assessed"
    assert {
        "trade_readiness_not_assessed",
        "live_write_disabled",
        "human_approval_required",
    }.issubset(blockers)
    if expected_blocker is None:
        assert precheck_blockers == []
    else:
        assert expected_blocker in precheck_blockers
        assert expected_blocker in blockers
    assert "hidden-token" not in str(payload)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response_body",
    NESTED_QUALIFIER_RESPONSES,
)
async def test_precheck_fails_closed_on_nested_or_empty_disclaimer_qualifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_body: dict[str, JsonValue],
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = read_body(request) if request.method == "GET" else response_body
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
    assert payload["precheck_request_accepted"] is False


@pytest.mark.anyio
async def test_live_precheck_fails_closed_on_unexpected_order_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = (
            read_body(request)
            if request.method == "GET"
            else {"PreCheckResult": "Ok", "OrderId": "unexpected-order-id"}
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
    assert payload["status"] == "unsafe_precheck_response"
    assert payload["order_identifier_present"] is True
    assert payload["requires_order_readback"] is True
    assert payload["order_placement_endpoint_called"] is False
    assert "unexpected-order-id" not in str(payload)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response_body", "expected_status", "unsafe_value"),
    [
        (
            {"PreCheckResult": "Ok", "OrderIds": ["unexpected"]},
            "unsafe_precheck_response",
            None,
        ),
        (
            {"PreCheckResult": "Ok", "MultiLegOrderId": "unexpected"},
            "unsafe_precheck_response",
            None,
        ),
        (
            {"PreCheckResult": "Ok", "ErrorInfo": {"Code": "Unexpected"}},
            "invalid_precheck_response",
            None,
        ),
        (
            {"PreCheckResult": "Ok", "PreTradeDisclaimers": {"Unknown": True}},
            "invalid_precheck_response",
            None,
        ),
        (
            {"PreCheckResult": "Ok", "EstimatedCashRequired": 10**4199},
            "invalid_precheck_response",
            None,
        ),
        (
            {"PreCheckResult": "Ok", "ErrorInfo": {"ErrorCode": "Invalid Request SECRET"}},
            "invalid_precheck_response",
            "Invalid Request SECRET",
        ),
        (
            {"PreCheckResult": "Ok", "ErrorInfo": {"ErrorCode": "S" * 129}},
            "invalid_precheck_response",
            "S" * 129,
        ),
        (
            {"PreCheckResult": "Ok", "EstimatedCashRequiredCurrency": "EURO"},
            "invalid_precheck_response",
            "EURO",
        ),
        (
            {"PreCheckResult": "Rejected"},
            "invalid_precheck_response",
            "Rejected",
        ),
    ],
)
async def test_live_precheck_rejects_unknown_success_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_body: dict[str, JsonValue],
    expected_status: str,
    unsafe_value: str | None,
) -> None:
    configure_live(tmp_path, monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = read_body(request) if request.method == "GET" else response_body
        return httpx2.Response(200, json=body, request=request)

    install_transport(monkeypatch, handler)
    async with Client(mcp) as client:
        result = await client.call_tool(
            LIVE_PRECHECK_TOOL,
            {"order": order_payload()},
            raise_on_error=False,
        )

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    assert payload["status"] == expected_status
    assert payload["precheck_request_accepted"] is False
    if expected_status == "invalid_precheck_response":
        assert payload["reason"] == "response_schema_invalid"
    if unsafe_value is not None:
        assert unsafe_value not in str(payload)
