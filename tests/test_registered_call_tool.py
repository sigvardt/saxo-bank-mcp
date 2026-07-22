from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.endpoint_registry import EndpointOperation, RegisteredEndpoint
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registered_endpoint_denies_unknown_path_before_network() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/not-a-registered-saxo-path"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["call_class"] == "denied_before_network"
    assert payload["denial_reason"] == "unregistered_endpoint"
    assert payload["denied_class"] == "unregistered"
    assert payload["network_call_made"] is False


@pytest.mark.anyio
async def test_registered_endpoint_denies_wrong_method_before_network() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "POST", "path": "/root/v1/diagnostics/get"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "method_not_allowed"
    assert payload["denied_class"] == "method_not_allowed"
    assert payload["network_call_made"] is False


@pytest.mark.anyio
async def test_registered_endpoint_denies_absolute_url_with_specific_reason() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "https://evil.example/root/v1/diagnostics/get"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "absolute_url_rejected"
    assert payload["denied_class"] == "host"
    assert payload["network_call_made"] is False


@pytest.mark.anyio
async def test_registered_endpoint_denies_refused_operation_with_operation_id() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "POST", "path": "/root/v1/diagnostics/post"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["operation_id"] == "post.root.v1.diagnostics.post"
    assert payload["denial_reason"] == "write_operations_disabled_by_policy"
    assert payload["denied_class"] == "write"
    assert payload["network_call_made"] is False


@pytest.mark.parametrize(
    ("method", "path", "reason"),
    [
        ("GET", "/not-a-registered-saxo-path", "unregistered_endpoint"),
        ("GET", "https://evil.example/root/v1/diagnostics/get", "absolute_url_rejected"),
        ("POST", "/root/v1/diagnostics/get", "method_not_allowed"),
    ],
)
@pytest.mark.anyio
async def test_registered_endpoint_denials_do_not_construct_transport(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    reason: str,
) -> None:
    attempts = 0

    def fail_client(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        nonlocal attempts
        attempts += 1
        _ = (base_url, transport)
        raise AssertionError("denied calls must not construct an HTTP client")

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", fail_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": method, "path": path},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == reason
    assert payload["network_call_made"] is False
    assert attempts == 0


@pytest.mark.anyio
async def test_registered_endpoint_calls_public_diagnostic_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(str(request.url))
        return httpx2.Response(
            200,
            json={"ClientKey": "fake-key", "ok": True},
            request=request,
        )

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", client_factory)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/root/v1/diagnostics/get"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["call_class"] == "sim_read_succeeded"
    assert payload["network_call_made"] is True
    assert payload["auth_exercised"] is False
    assert payload["trading_ready"] is False
    assert payload["http_status"] == httpx2.codes.OK
    assert "fake-key" not in str(payload["response"])
    assert payload["operation_id"] == "get.root.v1.diagnostics.get"
    assert seen_urls == ["https://gateway.saxobank.com/sim/openapi/root/v1/diagnostics/get"]


@pytest.mark.anyio
async def test_registered_endpoint_uses_resolved_path_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen_urls: list[str] = []
    cache = tmp_path / "sim-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="sim-access-token",  # noqa: S106
            environment="SIM",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(str(request.url))
        return httpx2.Response(200, json={"ok": True}, request=request)

    def client_factory(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=base_url,
            transport=httpx2.MockTransport(handler) if transport is None else transport,
        )

    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(cache))
    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", client_factory)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/hist/v3/accountvalues/client-123"},
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["call_class"] == "sim_read_succeeded"
    assert payload["operation_id"] == "get.hist.v3.accountvalues.clientkey"
    assert "resolved_path" not in payload
    assert seen_urls == [
        "https://gateway.saxobank.com/sim/openapi/hist/v3/accountvalues/client-123",
    ]


@pytest.mark.anyio
async def test_registered_endpoint_future_write_guard_denies_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = EndpointOperation(
        operation_id="post.test.implemented.write",
        service_group="Trading",
        service="Test",
        method="POST",
        path_template="/trade/v2/test",
        query_template="",
        documentation_url="https://www.developer.saxo/openapi/referencedocs",
        read_write_class="write_or_subscription",
        risk_class="write",
        auth_requirement="none",
        request_model="",
        response_model="",
        rate_rule="",
        cleanup_rule=None,
        status="implemented",
        refusal_reason="",
    )
    registered = RegisteredEndpoint(operation=operation, resolved_path="/trade/v2/test")

    def registered_endpoint(_method: str, _path: str) -> RegisteredEndpoint:
        return registered

    monkeypatch.setattr(
        "saxo_bank_mcp.read_tools.find_registered_endpoint",
        registered_endpoint,
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "POST", "path": "/trade/v2/test"},
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "write_class_not_allowed"
    assert payload["operation_id"] == "post.test.implemented.write"
    assert payload["network_call_made"] is False
