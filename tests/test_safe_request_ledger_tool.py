from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client, FastMCP
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.mcp_request_ledger_tools import (
    SAFE_REQUEST_LEDGER_MIDDLEWARE,
    saxo_get_safe_request_ledger,
)
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache

JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
JSON_ROWS_ADAPTER = TypeAdapter(list[dict[str, JsonValue]])
LEDGER_TOOL = "saxo_get_safe_request_ledger"
PRECHECK_TOOL = "saxo_precheck_live_order"
EXPECTED_RETRIES = 3
EXPECTED_REQUEST_COUNT = 3
EXPECTED_EVENT_COUNT = 6
EXPECTED_EVICTED_EVENTS = 2


def _configure_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "live-token.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="ledger-fixture-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))


def _install_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            if "instruments/details" in request.url.path:
                return httpx2.Response(
                    200,
                    json={"AssetType": "Stock", "IsTradable": True, "Uic": 30031},
                    request=request,
                )
            return httpx2.Response(
                200,
                json={
                    "Data": [
                        {
                            "AccountId": "display",
                            "AccountKey": "fixture",
                            "ClientKey": "fixture-client",
                            "AccountType": "Normal",
                            "Active": True,
                            "Currency": "EUR",
                        },
                    ],
                },
                request=request,
            )
        return httpx2.Response(
            200,
            json={"PreCheckResult": "Ok"},
            request=request,
        )

    transport = httpx2.MockTransport(handler)

    def transport_factory(
        *,
        http2: bool,
        retries: int,
        limits: httpx2.Limits,
        socket_options: list[tuple[int, int, int]],
    ) -> httpx2.AsyncBaseTransport:
        assert http2 is True
        assert retries == EXPECTED_RETRIES
        assert limits.max_connections is not None
        assert limits.max_connections > 0
        assert socket_options
        return transport

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport_factory)


@pytest.mark.anyio
async def test_safe_request_ledger_proves_precheck_without_exposing_request_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_live(tmp_path, monkeypatch)
    _install_transport(monkeypatch)

    async with Client(mcp) as client:
        await client.call_tool(LEDGER_TOOL, {"clear": True})
        precheck = await client.call_tool(
            PRECHECK_TOOL,
            {
                "order": {
                    "uic": 30031,
                    "asset_type": "Stock",
                    "amount": 1,
                    "buy_sell": "Buy",
                },
            },
            raise_on_error=False,
        )
        ledger = await client.call_tool(LEDGER_TOOL, {})
        await client.call_tool(LEDGER_TOOL, {"clear": True})
        empty = await client.call_tool(LEDGER_TOOL, {})

    assert precheck.is_error is False
    payload = JSON_OBJECT_ADAPTER.validate_python(ledger.structured_content)
    events = JSON_ROWS_ADAPTER.validate_python(payload["events"])
    assert payload["status"] == "passed"
    assert payload["scope"] == "current_mcp_session"
    assert payload["request_count"] == EXPECTED_REQUEST_COUNT
    assert payload["non_get_request_count"] == 1
    assert payload["gateway_post_paths"] == ["/openapi/trade/v2/orders/precheck"]
    assert payload["order_placement_endpoint_called"] is False
    assert payload["ledger_complete"] is True
    assert payload["events_evicted"] == 0
    assert payload["negative_proof_available"] is True
    assert len(events) == EXPECTED_EVENT_COUNT
    assert set(events[0]) == {
        "host_role",
        "method",
        "path",
        "phase",
        "query_names",
        "query_present",
        "status",
        "timestamp",
    }
    serialized = str(payload)
    assert "fixture" not in serialized
    assert "ledger-fixture-token" not in serialized
    assert "30031" not in serialized
    empty_payload = JSON_OBJECT_ADAPTER.validate_python(empty.structured_content)
    assert empty_payload["request_count"] == 0
    assert empty_payload["events"] == []


def _ledger_test_server() -> FastMCP:
    server = FastMCP("ledger-test")
    server.add_middleware(SAFE_REQUEST_LEDGER_MIDDLEWARE)
    server.tool()(saxo_get_safe_request_ledger)
    return server


@pytest.mark.anyio
async def test_safe_request_ledger_keeps_events_when_tool_raises() -> None:
    server = _ledger_test_server()

    async def failing_after_request() -> None:
        async def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200)

        async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
            await client.post("https://gateway.saxobank.com/openapi/trade/v2/orders/precheck")
        msg = "fixture failure after HTTP"
        raise RuntimeError(msg)

    server.tool()(failing_after_request)

    async with Client(server) as client:
        await client.call_tool(LEDGER_TOOL, {"clear": True})
        failed = await client.call_tool(
            "failing_after_request",
            {},
            raise_on_error=False,
        )
        ledger = await client.call_tool(LEDGER_TOOL, {})

    payload = JSON_OBJECT_ADAPTER.validate_python(ledger.structured_content)
    assert failed.is_error is True
    assert payload["ledger_complete"] is True
    assert payload["gateway_post_paths"] == ["/openapi/trade/v2/orders/precheck"]
    assert payload["request_count"] == 1


@pytest.mark.anyio
async def test_safe_request_ledger_detects_multileg_order_placement() -> None:
    server = _ledger_test_server()

    async def place_multileg_order() -> None:
        async def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200)

        async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
            await client.post("https://gateway.saxobank.com/openapi/trade/v2/orders/multileg")

    server.tool()(place_multileg_order)

    async with Client(server) as client:
        await client.call_tool(LEDGER_TOOL, {"clear": True})
        await client.call_tool("place_multileg_order", {})
        ledger = await client.call_tool(LEDGER_TOOL, {})

    payload = JSON_OBJECT_ADAPTER.validate_python(ledger.structured_content)
    assert payload["unsafe_gateway_request_detected"] is True
    assert payload["order_placement_endpoint_called"] is True
    assert payload["negative_proof_available"] is True


@pytest.mark.anyio
async def test_safe_request_ledger_marks_negative_proof_unknown_after_eviction() -> None:
    server = _ledger_test_server()

    async def overflow_ledger() -> None:
        async def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200)

        async with create_async_client(transport=httpx2.MockTransport(handler)) as client:
            await client.post("https://gateway.saxobank.com/openapi/trade/v2/orders")
            for _ in range(250):
                await client.get(
                    "https://gateway.saxobank.com/openapi/root/v1/sessions/capabilities",
                )

    server.tool()(overflow_ledger)

    async with Client(server) as client:
        await client.call_tool(LEDGER_TOOL, {"clear": True})
        await client.call_tool("overflow_ledger", {})
        ledger = await client.call_tool(LEDGER_TOOL, {})

    payload = JSON_OBJECT_ADAPTER.validate_python(ledger.structured_content)
    assert payload["ledger_complete"] is False
    assert payload["events_evicted"] == EXPECTED_EVICTED_EVENTS
    assert payload["negative_proof_available"] is False
    assert payload["order_placement_endpoint_called"] is None
