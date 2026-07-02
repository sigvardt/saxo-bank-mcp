from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

import saxo_bank_mcp.order_mutation_execution as order_execution
from saxo_bank_mcp import qa
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.order_mutation_models import (
    OrderWriteSpec,
    parse_order_mutation_response,
)
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, reset_safety_state
from saxo_bank_mcp.server import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_order_mutation_response_parser_covers_success_and_partial_states() -> None:
    success = parse_order_mutation_response(
        {"OrderId": "67762872", "Orders": [{"OrderId": "67762872"}]},
        http_status=200,
    )
    partial = parse_order_mutation_response(
        {
            "Orders": [
                {"OrderId": "67762872"},
                {"ErrorInfo": {"ErrorCode": "TradeNotCompleted"}},
            ],
        },
        http_status=200,
    )
    duplicate = parse_order_mutation_response(
        {"ErrorInfo": {"ErrorCode": "DuplicateRequest"}},
        http_status=400,
    )
    rate_limited = parse_order_mutation_response({}, http_status=429)

    assert success.outcome == "success"
    assert success.order_ids == ("67762872",)
    assert partial.outcome == "unknown_state"
    assert partial.partial_success is True
    assert partial.trade_not_completed is True
    assert partial.needs_readback is True
    assert duplicate.duplicate_request is True
    assert duplicate.outcome == "failed"
    assert rate_limited.rate_limited is True
    assert rate_limited.outcome == "rate_limited"


@pytest.mark.anyio
async def test_order_write_tools_are_registered_with_safety_descriptions() -> None:
    expected = {
        "saxo_place_sim_order",
        "saxo_modify_sim_order",
        "saxo_cancel_sim_order",
        "saxo_cancel_sim_orders_by_instrument",
        "saxo_place_multileg_sim_order",
        "saxo_modify_multileg_sim_order",
        "saxo_cancel_multileg_sim_order",
    }
    async with Client(mcp) as client:
        tools = await client.list_tools()

    descriptions = {tool.name: tool.description or "" for tool in tools}
    assert expected <= set(descriptions)
    for tool_name in expected:
        assert "SIM-only" in descriptions[tool_name]
        assert "approval" in descriptions[tool_name]
        assert "never calls LIVE" in descriptions[tool_name]
        assert "missing approval is denied before network" in descriptions[tool_name]


@pytest.mark.anyio
async def test_order_write_denies_missing_approval_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    _configure_safety(monkeypatch, tmp_path)

    def fail_client(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        _ = (base_url, transport)
        raise AssertionError("missing approval must not construct an HTTP client")

    monkeypatch.setattr(order_execution, "create_async_client", fail_client)
    async with Client(mcp) as client:
        preview = await _create_preview(client, "post.trade.v2.orders")
        result = await client.call_tool(
            "saxo_place_sim_order",
            {"preview_token": str(preview["preview_token"])},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert result.is_error is True
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "approval_factor_missing"
    assert payload["network_call_made"] is False
    assert payload["request_fingerprint"] == preview["request_fingerprint"]
    assert payload["audit_path_inside_repo"] is False


@pytest.mark.anyio
async def test_approved_order_write_without_token_is_incomplete_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    _configure_safety(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")

    async with Client(mcp) as client:
        preview = await _create_preview(client, "post.trade.v2.orders")
        result = await client.call_tool(
            "saxo_place_sim_order",
            {
                "preview_token": str(preview["preview_token"]),
                "approval_factor": TEST_APPROVAL_FACTOR,
            },
            raise_on_error=False,
        )
        safety = await client.call_tool("saxo_safety_status", {})

    payload = result.structured_content
    state = safety.structured_content
    assert payload is not None
    assert state is not None
    assert payload["status"] == "auth_required"
    assert payload["write_class_status"] == "incomplete"
    assert payload["network_call_made"] is False
    assert payload["order_placed"] is False
    assert state["pending_preview_count"] == 1
    assert state["committed_fingerprint_count"] == 0


@pytest.mark.anyio
async def test_order_write_denies_request_body_preview_mismatch_before_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    _configure_safety(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    request = _preview_request("post.trade.v2.orders")
    request["request_body"] = {
        "AccountKey": "SIM-ACCOUNT-1",
        "Uic": 22,
        "AssetType": "Stock",
        "Amount": 1,
    }

    async with Client(mcp) as client:
        preview_result = await client.call_tool("saxo_create_write_preview", request)
        preview = preview_result.structured_content
        assert isinstance(preview, dict)
        result = await client.call_tool(
            "saxo_place_sim_order",
            {
                "preview_token": str(preview["preview_token"]),
                "approval_factor": TEST_APPROVAL_FACTOR,
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "request_body_preview_mismatch"
    assert "request_body_instrument_uic_mismatch" in payload["denial_reasons"]
    assert payload["network_call_made"] is False
    assert payload["mutation_may_have_occurred"] is False
    assert payload["retry_unsafe"] is False


@pytest.mark.anyio
async def test_place_order_executes_mocked_sim_write_with_readbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    _configure_safety(monkeypatch, tmp_path)
    seen: list[tuple[str, str]] = []

    def ready_token(_spec: OrderWriteSpec) -> SaxoTokenSet:
        return SaxoTokenSet(
            access_token="access-token-value",  # noqa: S106
            refresh_token="refresh-token-value",  # noqa: S106
            code_verifier="code-verifier-value",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx2.Response(
                200,
                json={"OrderId": "67762872", "Orders": [{"OrderId": "67762872"}]},
                request=request,
            )
        return httpx2.Response(200, json={"Data": []}, request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(order_execution, "_cached_token", ready_token)
    monkeypatch.setattr(order_execution, "create_async_client", client_factory)

    async with Client(mcp) as client:
        preview = await _create_preview(client, "post.trade.v2.orders")
        result = await client.call_tool(
            "saxo_place_sim_order",
            {
                "preview_token": str(preview["preview_token"]),
                "approval_factor": TEST_APPROVAL_FACTOR,
            },
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["network_call_made"] is True
    assert payload["order_result_parsed"] is True
    assert payload["x_request_id_present"] is True
    assert payload["port_orders_readback"] is True
    assert payload["trade_messages_readback"] is True
    assert payload["order_placed"] is True
    assert payload["raw_audit_path_inside_repo"] is False
    assert seen == [
        ("POST", "/sim/openapi/trade/v2/orders"),
        ("GET", "/sim/openapi/port/v1/orders"),
        ("GET", "/sim/openapi/trade/v1/messages"),
    ]


@pytest.mark.anyio
async def test_unknown_order_response_is_retry_unsafe_and_tri_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    _configure_safety(monkeypatch, tmp_path)

    def ready_token(_spec: OrderWriteSpec) -> SaxoTokenSet:
        return SaxoTokenSet(
            access_token="access-token-value",  # noqa: S106
            refresh_token="refresh-token-value",  # noqa: S106
            code_verifier="code-verifier-value",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(
                200,
                json={
                    "Orders": [
                        {"OrderId": "67762872"},
                        {"ErrorInfo": {"ErrorCode": "TradeNotCompleted"}},
                    ],
                },
                request=request,
            )
        return httpx2.Response(200, json={"Data": []}, request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(order_execution, "_cached_token", ready_token)
    monkeypatch.setattr(order_execution, "create_async_client", client_factory)

    async with Client(mcp) as client:
        preview = await _create_preview(client, "post.trade.v2.orders")
        result = await client.call_tool(
            "saxo_place_sim_order",
            {
                "preview_token": str(preview["preview_token"]),
                "approval_factor": TEST_APPROVAL_FACTOR,
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "unknown_state"
    assert payload["order_placed"] is None
    assert payload["mutation_may_have_occurred"] is True
    assert payload["retry_unsafe"] is True
    assert payload["port_orders_readback"] is True
    assert payload["trade_messages_readback"] is True


@pytest.mark.anyio
async def test_network_error_after_commit_is_retry_unsafe_and_tri_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    _configure_safety(monkeypatch, tmp_path)

    def ready_token(_spec: OrderWriteSpec) -> SaxoTokenSet:
        return SaxoTokenSet(
            access_token="access-token-value",  # noqa: S106
            refresh_token="refresh-token-value",  # noqa: S106
            code_verifier="code-verifier-value",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("simulated timeout after commit", request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr(order_execution, "_cached_token", ready_token)
    monkeypatch.setattr(order_execution, "create_async_client", client_factory)

    async with Client(mcp) as client:
        preview = await _create_preview(client, "post.trade.v2.orders")
        result = await client.call_tool(
            "saxo_place_sim_order",
            {
                "preview_token": str(preview["preview_token"]),
                "approval_factor": TEST_APPROVAL_FACTOR,
            },
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "network_error"
    assert payload["order_placed"] is None
    assert payload["mutation_may_have_occurred"] is True
    assert payload["retry_unsafe"] is True
    assert payload["committed_before_network_result"] is True
    assert payload["port_orders_readback"] is False
    assert payload["trade_messages_readback"] is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/trade/v2/orders"),
        ("PATCH", "/trade/v2/orders"),
        ("DELETE", "/trade/v2/orders/fixture-order-id"),
        ("POST", "/trade/v2/orders/multileg"),
    ],
)
@pytest.mark.anyio
async def test_generic_registered_endpoint_denies_order_writes_before_network(
    method: str,
    path: str,
) -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": method, "path": path},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denied_class"] == "write"
    assert payload["network_call_made"] is False


def test_sim_order_mutation_qa_reports_incomplete_auth_not_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_safety(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    out = tmp_path / "sim-order.json"

    result = qa.main(
        [
            "sim-order-mutation",
            "--classes",
            "place,modify,cancel,cancel-by-instrument,multileg-place,multileg-modify,multileg-cancel",
            "--out",
            str(out),
        ],
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "incomplete_auth_required"
    assert report["completed_classes"] == []
    assert report["auth_required_classes"] == [
        "place",
        "modify",
        "cancel",
        "cancel-by-instrument",
        "multileg-place",
        "multileg-modify",
        "multileg-cancel",
    ]
    assert report["real_mutation_proven"] is False
    assert report["completion_claim_allowed"] is False
    assert {row["status"] for row in report["per_class"]} == {"incomplete"}
    assert {row["write_class_status"] for row in report["per_class"]} == {"incomplete"}
    assert all(row["next_action"] for row in report["per_class"])
    assert all(row["mutation_may_have_occurred"] is False for row in report["per_class"])
    assert all(row["retry_unsafe"] is False for row in report["per_class"])
    assert report["network_call_made"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_trade_write_denied_qa_probe_names_missing_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_safety(monkeypatch, tmp_path)
    out = tmp_path / "write-denied.json"

    result = qa.main(["trade-write-denied", "--missing", "approval-factor", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "denied"
    assert report["tool_name"] == "saxo_place_sim_order"
    assert report["denial_reason"] == "approval_factor_missing"
    assert report["same_request_fingerprint"] is True
    assert report["network_call_made"] is False
    assert report["order_placed"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


async def _create_preview(
    client: Client[FastMCPTransport],
    operation_id: str,
) -> dict[str, JsonValue]:
    result = await client.call_tool("saxo_create_write_preview", _preview_request(operation_id))
    payload = result.structured_content
    assert isinstance(payload, dict)
    return payload


def _preview_request(operation_id: str) -> dict[str, JsonValue]:
    return {
        "operation_id": operation_id,
        "account_key": "SIM-ACCOUNT-1",
        "instrument_uic": 21,
        "quantity": 1,
        "estimated_notional": 10,
        "account_currency": "USD",
        "risk": {
            "cost": 10,
            "cash_required": 10,
            "margin_impact": 1,
            "contract_multiplier": 1,
            "conversion_known": True,
        },
        "request_body": {
            "AccountKey": "SIM-ACCOUNT-1",
            "Uic": 21,
            "AssetType": "Stock",
            "Amount": 1,
            "BuySell": "Buy",
            "OrderType": "Limit",
            "OrderDuration": {"DurationType": "DayOrder"},
        },
    }


def _configure_safety(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", "SIM-ACCOUNT-1")
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", "21")
