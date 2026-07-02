from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.streaming import make_saxo_binary_message
from saxo_bank_mcp.streaming_execution import (
    StreamingSubscriptionInput,
    execute_streaming_price_subscription,
)
from saxo_bank_mcp.streaming_transport import websocket_payload

type JsonObject = dict[str, JsonValue]
TK: Final = "tok-a"
RF: Final = "tok-r"
RESET_MESSAGE_ID: Final = 101
RECONNECT_MESSAGE_ID: Final = 42


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def token_set() -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=TK,
        refresh_token=RF,
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_control_only_binary_frame_reports_no_data() -> None:
    payload = websocket_payload(
        make_saxo_binary_message(
            RESET_MESSAGE_ID,
            "_resetsubscriptions",
            {"TargetReferenceIds": ["prices1"]},
        ),
    )

    assert payload["websocket_frame_recorded"] is True
    assert payload["data_message_observed"] is False
    assert payload["control_message_observed"] is True
    assert payload["control_only_no_data"] is True


@pytest.mark.anyio
async def test_control_only_frame_does_not_complete_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = token_set()

    def fake_cached_token(_tool_name: str) -> SaxoTokenSet | JsonObject:
        return token

    async def fake_create_price_subscriptions(
        _token: SaxoTokenSet,
        _context_id: str,
        _reference_id: str,
        uics: list[int],
        _asset_type: str,
    ) -> JsonObject:
        return snapshot_payload(uics)

    async def fake_receive_stream_frame(
        _token: SaxoTokenSet,
        _context_id: str,
        _wait_seconds: float,
        *,
        last_message_id: int | None = None,
    ) -> JsonObject:
        assert last_message_id is None
        return frame_payload(data_message_observed=False)

    monkeypatch.setattr("saxo_bank_mcp.streaming_execution.cached_token", fake_cached_token)
    monkeypatch.setattr(
        "saxo_bank_mcp.streaming_execution.create_price_subscriptions",
        fake_create_price_subscriptions,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.streaming_execution.receive_stream_frame",
        fake_receive_stream_frame,
    )

    result = await execute_streaming_price_subscription(
        StreamingSubscriptionInput(
            context_id="ctx1",
            reference_id="prices1",
            uics=[21],
            asset_type="Stock",
            wait_seconds=0.1,
        ),
    )

    payload = result.structured_content
    assert payload is not None
    assert result.is_error is True
    assert payload["status"] == "control_only_no_data"
    assert payload["streaming_completion_claim_allowed"] is False


@pytest.mark.anyio
async def test_data_frame_completes_subscription_and_passes_reconnect_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = token_set()
    captured_last_message_ids: list[int | None] = []

    def fake_cached_token(_tool_name: str) -> SaxoTokenSet | JsonObject:
        return token

    async def fake_create_price_subscriptions(
        _token: SaxoTokenSet,
        _context_id: str,
        _reference_id: str,
        uics: list[int],
        _asset_type: str,
    ) -> JsonObject:
        return snapshot_payload(uics)

    async def fake_receive_stream_frame(
        _token: SaxoTokenSet,
        _context_id: str,
        _wait_seconds: float,
        *,
        last_message_id: int | None = None,
    ) -> JsonObject:
        captured_last_message_ids.append(last_message_id)
        return frame_payload(data_message_observed=True)

    monkeypatch.setattr("saxo_bank_mcp.streaming_execution.cached_token", fake_cached_token)
    monkeypatch.setattr(
        "saxo_bank_mcp.streaming_execution.create_price_subscriptions",
        fake_create_price_subscriptions,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.streaming_execution.receive_stream_frame",
        fake_receive_stream_frame,
    )

    result = await execute_streaming_price_subscription(
        StreamingSubscriptionInput(
            context_id="ctx1",
            reference_id="prices1",
            uics=[21],
            asset_type="Stock",
            wait_seconds=0.1,
            last_message_id=RECONNECT_MESSAGE_ID,
        ),
    )

    payload = result.structured_content
    assert payload is not None
    assert result.is_error is False
    assert payload["status"] == "completed"
    assert payload["subscription_snapshot_recorded"] is True
    assert payload["data_message_observed"] is True
    assert payload["streaming_completion_claim_allowed"] is True
    assert payload["remote_subscription_may_remain"] is True
    assert payload["cleanup_required"] is True
    assert payload["cleanup_tool_name"] == "saxo_cleanup_streaming_subscriptions"
    assert captured_last_message_ids == [RECONNECT_MESSAGE_ID]


def snapshot_payload(uics: list[int]) -> JsonObject:
    return {
        "status": "snapshot_recorded",
        "subscription_snapshots": [],
        "requested_price_instruments_count": len(uics),
        "order_or_subscription_created": True,
    }


def frame_payload(*, data_message_observed: bool) -> JsonObject:
    return {
        "websocket_frame_recorded": True,
        "websocket_frame_kind": "binary",
        "data_message_observed": data_message_observed,
        "control_message_observed": not data_message_observed,
        "control_only_no_data": not data_message_observed,
    }
