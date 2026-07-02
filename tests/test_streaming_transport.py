from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx2
import pytest

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.streaming import (
    local_subscription_count,
    make_saxo_binary_message,
    reset_local_subscriptions,
)
from saxo_bank_mcp.streaming_transport import (
    create_price_subscriptions,
    receive_stream_frame,
)

type JsonObject = dict[str, JsonValue]
TK: Final = "tok-a"
RF: Final = "tok-r"
RECONNECT_MESSAGE_ID: Final = 42
POST_ATTEMPT_COUNT: Final = 2
LOCAL_SUBSCRIPTION_COUNT_AFTER_PARTIAL_FAILURE: Final = 1


@dataclass(frozen=True, slots=True)
class FakeWebSocket:
    message: bytes

    async def recv(self) -> bytes:
        return self.message


@dataclass(frozen=True, slots=True)
class FakeSubscriptionClient:
    responses: list[httpx2.Response]
    posted_paths: list[str]
    posted_headers: list[dict[str, str]]

    async def post(
        self,
        path: str,
        *,
        json: dict[str, JsonValue],
        headers: dict[str, str],
    ) -> httpx2.Response:
        _ = json
        self.posted_paths.append(path)
        self.posted_headers.append(headers)
        return self.responses.pop(0)


@asynccontextmanager
async def fake_websocket_context(message: bytes) -> AsyncGenerator[FakeWebSocket]:
    yield FakeWebSocket(message)


@asynccontextmanager
async def fake_client_context(
    client: FakeSubscriptionClient,
) -> AsyncGenerator[FakeSubscriptionClient]:
    yield client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def local_streaming_registry() -> Iterator[None]:
    reset_local_subscriptions()
    yield
    reset_local_subscriptions()


def token_set() -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=TK,
        refresh_token=RF,
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.anyio
async def test_receive_stream_frame_sends_token_in_header_not_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_urls: list[str] = []
    captured_headers: list[dict[str, str]] = []

    def fake_connect(
        uri: str,
        *,
        additional_headers: dict[str, str],
        open_timeout: float,
    ) -> AbstractAsyncContextManager[FakeWebSocket]:
        _ = open_timeout
        captured_urls.append(uri)
        captured_headers.append(additional_headers)
        return fake_websocket_context(
            make_saxo_binary_message(
                7,
                "prices1",
                {"Quote": {"Bid": 123.45}},
            ),
        )

    monkeypatch.setattr("saxo_bank_mcp.streaming_transport.connect", fake_connect)

    payload = await receive_stream_frame(
        token_set(),
        "ctx1",
        0.1,
        last_message_id=RECONNECT_MESSAGE_ID,
    )

    assert payload["websocket_frame_recorded"] is True
    assert payload["data_message_observed"] is True
    assert captured_urls == [
        (
            "wss://sim-streaming.saxobank.com/sim/oapi/streaming/ws/connect"
            "?contextId=ctx1&messageid=42"
        ),
    ]
    assert TK not in captured_urls[0]
    assert captured_headers == [
        {"Accept": "application/json", "Authorization": f"Bearer {TK}"},
    ]


@pytest.mark.anyio
async def test_partial_price_subscription_failure_requires_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeSubscriptionClient(
        responses=[
            httpx2.Response(
                201,
                json={"Snapshot": {"Quote": {"Bid": 1.0}}},
                headers={"location": "/root/v1/subscriptions/ctx1/prices-1"},
            ),
            httpx2.Response(500, json={"ErrorInfo": {"Code": "InternalError"}}),
        ],
        posted_paths=[],
        posted_headers=[],
    )

    def fake_create_async_client(
        *,
        base_url: str,
    ) -> AbstractAsyncContextManager[FakeSubscriptionClient]:
        _ = base_url
        return fake_client_context(fake_client)

    monkeypatch.setattr(
        "saxo_bank_mcp.streaming_transport.create_async_client",
        fake_create_async_client,
    )

    payload = await create_price_subscriptions(
        token_set(),
        "ctx1",
        "prices",
        [21, 22],
        "Stock",
    )

    assert payload["status"] == "http_error"
    assert payload["partial_subscription_snapshots_recorded"] is True
    assert payload["created_subscription_count"] == 1
    assert payload["remote_subscription_may_remain"] is True
    assert payload["cleanup_required"] is True
    assert payload["cleanup_tool_name"] == "saxo_cleanup_streaming_subscriptions"
    assert local_subscription_count("ctx1") == LOCAL_SUBSCRIPTION_COUNT_AFTER_PARTIAL_FAILURE
    assert len(fake_client.posted_paths) == POST_ATTEMPT_COUNT
    assert fake_client.posted_headers == [
        {"Accept": "application/json", "Authorization": f"Bearer {TK}"},
        {"Accept": "application/json", "Authorization": f"Bearer {TK}"},
    ]
