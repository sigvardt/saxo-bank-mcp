from __future__ import annotations

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.server import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registered_endpoint_network_error_keeps_safety_caveats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("blocked", request=request)

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
            raise_on_error=False,
        )

    payload = result.structured_content
    assert payload is not None
    assert result.is_error is True
    assert payload["status"] == "network_error"
    assert payload["call_class"] == "sim_read_attempted"
    assert payload["environment"] == "SIM"
    assert payload["network_call_made"] is True
    assert payload["live_write"] is False
    assert "trading/order readiness" in payload["does_not_verify"]
