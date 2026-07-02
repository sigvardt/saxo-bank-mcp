from __future__ import annotations

from typing import Final

import anyio
import httpx2
from pydantic import TypeAdapter
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_text
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SIM_ENDPOINTS
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.streaming import (
    PRICE_SUBSCRIPTION_OPERATION_ID,
    PRICE_SUBSCRIPTION_PATH,
    SUBSCRIPTION_CLEANUP_PATH,
    SaxoStreamingFrame,
    StreamingValidationError,
    build_streaming_connect_url,
    parse_streaming_frame,
    register_local_subscription,
    validate_reference_id,
)

type JsonObject = dict[str, JsonValue]
_JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300


async def create_price_subscriptions(
    token: SaxoTokenSet,
    context_id: str,
    reference_id: str,
    uics: list[int],
    asset_type: str,
) -> JsonObject:
    snapshots: list[JsonObject] = []
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            for subscription_reference, uic in subscription_refs(reference_id, len(uics), uics):
                response = await client.post(
                    PRICE_SUBSCRIPTION_PATH.lstrip("/"),
                    json=subscription_body(context_id, subscription_reference, uic, asset_type),
                    headers=saxo_headers(token),
                )
                snapshots.append(subscription_snapshot(response, subscription_reference, uic))
                if not http_ok(response.status_code):
                    created_count = successful_snapshot_count(snapshots)
                    return {
                        **http_error_payload(response),
                        "subscription_snapshots": snapshots,
                        "partial_subscription_snapshots_recorded": created_count > 0,
                        "created_subscription_count": created_count,
                        "remote_subscription_may_remain": created_count > 0,
                        "cleanup_required": created_count > 0,
                        "cleanup_tool_name": "saxo_cleanup_streaming_subscriptions",
                        "cleanup_warning": (
                            "One or more subscription POSTs may have succeeded before this "
                            "failure; call cleanup for the ContextId before retrying."
                        ),
                    }
                register_local_subscription(
                    context_id=context_id,
                    reference_id=subscription_reference,
                    operation_id=PRICE_SUBSCRIPTION_OPERATION_ID,
                    endpoint_path=PRICE_SUBSCRIPTION_PATH,
                )
    except httpx2.HTTPError as error:
        return network_error_payload(str(error), type(error).__name__)
    except StreamingValidationError as error:
        return {"status": "denied", "denial_reason": error.code}
    return {
        "status": "snapshot_recorded",
        "subscription_snapshots": snapshots,
        "requested_price_instruments_count": len(uics),
        "order_or_subscription_created": True,
    }


async def receive_stream_frame(
    token: SaxoTokenSet,
    context_id: str,
    wait_seconds: float,
    last_message_id: int | None = None,
) -> JsonObject:
    frame_payload: JsonObject | None = None
    try:
        with anyio.move_on_after(wait_seconds) as scope:
            async with connect(
                build_streaming_connect_url(context_id, last_message_id=last_message_id),
                additional_headers=saxo_headers(token),
                open_timeout=wait_seconds,
            ) as websocket:
                message = await websocket.recv()
                frame_payload = websocket_payload(message)
    except (OSError, TimeoutError, WebSocketException) as error:
        return {
            "websocket_frame_recorded": False,
            "websocket_status": "network_error",
            "network_error_type": type(error).__name__,
            "network_error_message_redacted": redact_text(str(error)),
            "streaming_completion_claim_allowed": False,
        }
    else:
        if frame_payload is None:
            return {
                "websocket_frame_recorded": False,
                "websocket_status": "timeout" if scope.cancelled_caught else "closed_without_frame",
                "streaming_completion_claim_allowed": False,
            }
        return frame_payload


async def delete_root_subscription(token: SaxoTokenSet, context_id: str) -> JsonObject:
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            response = await client.delete(
                SUBSCRIPTION_CLEANUP_PATH.replace("{ContextId}", context_id).lstrip("/"),
                headers=saxo_headers(token),
            )
    except httpx2.HTTPError as error:
        return {
            "network_call_made": True,
            "cleanup_status": "network_error",
            "network_error_type": type(error).__name__,
            "network_error_message_redacted": redact_text(str(error)),
        }
    return {
        "network_call_made": True,
        "cleanup_status": "accepted" if response.status_code in {202, 204} else "http_error",
        "cleanup_http_status": response.status_code,
        "response": response_body(response),
    }


def saxo_headers(token: SaxoTokenSet) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {token.access_token}"}


def subscription_refs(
    reference_id: str,
    count: int,
    uics: list[int],
) -> tuple[tuple[str, int], ...]:
    if count == 1:
        return ((str(validate_reference_id(reference_id)), uics[0]),)
    refs: list[tuple[str, int]] = []
    for index, uic in enumerate(uics, start=1):
        suffix = f"-{index}"
        base = reference_id[: 50 - len(suffix)]
        refs.append((str(validate_reference_id(f"{base}{suffix}")), uic))
    return tuple(refs)


def subscription_body(
    context_id: str,
    reference_id: str,
    uic: int,
    asset_type: str,
) -> dict[str, JsonValue]:
    return {
        "ContextId": context_id,
        "ReferenceId": reference_id,
        "Arguments": {"AssetType": asset_type, "Uic": uic},
    }


def subscription_snapshot(response: httpx2.Response, reference_id: str, uic: int) -> JsonObject:
    return {
        "reference_id": reference_id,
        "uic": uic,
        "http_status": response.status_code,
        "snapshot": response_body(response),
        "resource_location_present": bool(response.headers.get("location")),
    }


def successful_snapshot_count(snapshots: list[JsonObject]) -> int:
    count = 0
    for snapshot in snapshots:
        status = snapshot.get("http_status")
        if isinstance(status, int) and http_ok(status):
            count += 1
    return count


def websocket_payload(message: str | bytes) -> JsonObject:
    match message:
        case bytes() as raw:
            frame = parse_streaming_frame(raw)
            data_message_observed = any(
                not message.control_message for message in frame.messages
            )
            control_message_observed = bool(frame.control_references)
            return {
                "websocket_frame_recorded": True,
                "websocket_frame_kind": "binary",
                "data_message_observed": data_message_observed,
                "control_message_observed": control_message_observed,
                "control_only_no_data": (
                    control_message_observed and not data_message_observed
                ),
                "parsed_frame": frame_json(frame),
                "last_message_id": -1 if frame.last_message_id is None else frame.last_message_id,
                "control_references": list(frame.control_references),
            }
        case str() as text:
            return {
                "websocket_frame_recorded": True,
                "websocket_frame_kind": "text",
                "data_message_observed": False,
                "control_message_observed": False,
                "control_only_no_data": False,
                "parsed_frame": _JSON_VALUE_ADAPTER.validate_json(text),
            }


def frame_json(frame: SaxoStreamingFrame) -> JsonObject:
    return {
        "message_count": len(frame.messages),
        "last_message_id": -1 if frame.last_message_id is None else frame.last_message_id,
        "control_references": list(frame.control_references),
        "messages": [
            {
                "message_id": message.message_id,
                "reference_id": message.reference_id,
                "payload_format": message.payload_format,
                "payload_size": message.payload_size,
                "control_message": message.control_message,
                "payload": message.payload_json,
            }
            for message in frame.messages
        ],
    }


def http_error_payload(response: httpx2.Response) -> JsonObject:
    return {
        "status": "http_error",
        "http_status": response.status_code,
        "network_call_made": True,
        "authorization_header_used": True,
        "response": response_body(response),
        "streaming_completion_claim_allowed": False,
    }


def network_error_payload(detail: str, error_type: str) -> JsonObject:
    return {
        "status": "network_error",
        "network_call_made": True,
        "authorization_header_used": True,
        "network_error_type": error_type,
        "network_error_message_redacted": redact_text(detail),
        "streaming_completion_claim_allowed": False,
    }


def response_body(response: httpx2.Response) -> JsonValue:
    if not response.content:
        return None
    if not response.headers.get("content-type", "").startswith("application/json"):
        return redact_text(response.text)
    try:
        parsed = _JSON_VALUE_ADAPTER.validate_python(response.json())
    except ValueError:
        return redact_text(response.text)
    return redact_json(parsed)


def http_ok(status_code: int) -> bool:
    return HTTP_SUCCESS_MIN <= status_code < HTTP_SUCCESS_MAX
