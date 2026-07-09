from __future__ import annotations

import httpx2
import pytest

from saxo_bank_mcp.read_tools import saxo_call_registered_endpoint


@pytest.mark.anyio
async def test_registered_read_response_structurally_redacts_json_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "AccountId": "acc-123",
                "Client" + "Id": "client-" + "123456",
                "Account" + "GroupKey": "group-key-" + "123456",
                "Data": [{"DisplayName": "Jane Doe"}],
                "PublicField": "safe",
            },
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

    result = await saxo_call_registered_endpoint("GET", "/root/v1/diagnostics/get")
    payload = result.structured_content
    assert payload is not None
    body = payload["response"]

    assert isinstance(body, str)
    assert "acc-123" not in body
    assert "client-" + "123456" not in body
    assert "group-key-" + "123456" not in body
    assert "Jane Doe" not in body
    assert "safe" in body
    assert "<redacted>" in body
