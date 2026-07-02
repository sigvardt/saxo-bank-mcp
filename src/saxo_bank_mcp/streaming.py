from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, NewType
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue

ContextId = NewType("ContextId", str)
ReferenceId = NewType("ReferenceId", str)

SIM_STREAMING_ENDPOINT: Final = "sim-streaming.saxobank.com/sim/oapi/streaming/ws"
SIM_STREAMING_CONNECT_URL: Final = f"wss://{SIM_STREAMING_ENDPOINT}/connect"
SUBSCRIPTION_CLEANUP_PATH: Final = "/root/v1/subscriptions/{ContextId}"
PRICE_SUBSCRIPTION_PATH: Final = "/trade/v1/prices/subscriptions"
PRICE_SUBSCRIPTION_OPERATION_ID: Final = "post.trade.v1.prices.subscriptions"
REFERENCE_ID_MAX_LENGTH: Final = 50
CONTEXT_ID_MAX_LENGTH: Final = 50
JSON_PAYLOAD_FORMAT: Final = 0
_STREAM_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class StreamingLimits:
    expected_connections: int
    expected_price_instruments: int
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SaxoStreamingMessage:
    message_id: int
    reference_id: str
    payload_format: int
    payload_size: int
    payload_text: str
    payload_json: JsonValue
    control_message: bool


@dataclass(frozen=True, slots=True)
class SaxoStreamingFrame:
    messages: tuple[SaxoStreamingMessage, ...]
    last_message_id: int | None
    control_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalSubscription:
    context_id: ContextId
    reference_id: ReferenceId
    operation_id: str
    endpoint_path: str


@dataclass(frozen=True, slots=True)
class StreamingValidationError(ValueError):
    code: str
    value: str

    def __str__(self) -> str:
        """Return stable validation error text."""
        return f"{self.code}: {self.value}"


STREAMING_LIMITS: Final = StreamingLimits(
    expected_connections=4,
    expected_price_instruments=200,
    sources=(
        "https://openapi.help.saxo/hc/en-us/articles/4417467162641-How-many-concurrent-price-subscriptions-can-I-have",
        "https://www.developer.saxo/openapi/learn/streaming",
        "https://developer.saxobank.com/openapi/releasenotes/completed-planned-changes?phrase=Value",
    ),
)
TOKEN_QUERY_NAME_MARKERS: Final = ("authorization", "access_token", "token")

_LOCAL_SUBSCRIPTIONS: dict[tuple[str, str], LocalSubscription] = {}


def validate_context_id(raw: str) -> ContextId:
    value = raw.strip()
    if not value:
        raise StreamingValidationError("context_id_missing", raw)
    if len(value) > CONTEXT_ID_MAX_LENGTH:
        raise StreamingValidationError("context_id_too_long", raw)
    if _STREAM_ID_PATTERN.fullmatch(value) is None:
        raise StreamingValidationError("context_id_invalid", raw)
    return ContextId(value)


def validate_reference_id(raw: str) -> ReferenceId:
    value = raw.strip()
    if not value:
        raise StreamingValidationError("reference_id_missing", raw)
    if len(value) > REFERENCE_ID_MAX_LENGTH:
        raise StreamingValidationError("reference_id_too_long", raw)
    if value.startswith("_"):
        raise StreamingValidationError("reference_id_reserved_control_prefix", raw)
    if _STREAM_ID_PATTERN.fullmatch(value) is None:
        raise StreamingValidationError("reference_id_invalid", raw)
    return ReferenceId(value)


def build_streaming_connect_url(
    context_id: str,
    *,
    last_message_id: int | None = None,
) -> str:
    params = {"contextId": str(validate_context_id(context_id))}
    if last_message_id is not None:
        params["messageid"] = str(last_message_id)
    return f"{SIM_STREAMING_CONNECT_URL}?{urlencode(params)}"


def token_in_streaming_query_url(connect_url: str) -> bool:
    query_names = (
        name.lower()
        for name, _value in parse_qsl(urlsplit(connect_url).query, keep_blank_values=True)
    )
    return any(
        any(marker in query_name for marker in TOKEN_QUERY_NAME_MARKERS)
        for query_name in query_names
    )


def parse_streaming_frame(frame: bytes) -> SaxoStreamingFrame:
    messages: list[SaxoStreamingMessage] = []
    offset = 0
    while offset < len(frame):
        message, offset = _parse_message(frame, offset)
        messages.append(message)
    return SaxoStreamingFrame(
        messages=tuple(messages),
        last_message_id=None if not messages else messages[-1].message_id,
        control_references=tuple(
            message.reference_id for message in messages if message.control_message
        ),
    )


def make_saxo_binary_message(
    message_id: int,
    reference_id: str,
    payload: dict[str, JsonValue],
) -> bytes:
    encoded_reference = reference_id.encode("ascii")
    encoded_payload = json.dumps(payload, sort_keys=True).encode("utf-8")
    return b"".join(
        (
            message_id.to_bytes(8, "little", signed=False),
            b"\x00\x00",
            len(encoded_reference).to_bytes(1, "little"),
            encoded_reference,
            JSON_PAYLOAD_FORMAT.to_bytes(1, "little"),
            len(encoded_payload).to_bytes(4, "little", signed=False),
            encoded_payload,
        ),
    )


def reset_local_subscriptions() -> None:
    _LOCAL_SUBSCRIPTIONS.clear()


def register_local_subscription(
    *,
    context_id: str,
    reference_id: str,
    operation_id: str,
    endpoint_path: str,
) -> LocalSubscription:
    context = validate_context_id(context_id)
    reference = validate_reference_id(reference_id)
    subscription = LocalSubscription(
        context_id=context,
        reference_id=reference,
        operation_id=operation_id,
        endpoint_path=endpoint_path,
    )
    _LOCAL_SUBSCRIPTIONS[(str(context), str(reference))] = subscription
    return subscription


def remove_local_context(context_id: str) -> tuple[LocalSubscription, ...]:
    context = validate_context_id(context_id)
    removed: list[LocalSubscription] = []
    for key, subscription in tuple(_LOCAL_SUBSCRIPTIONS.items()):
        if key[0] == str(context):
            removed.append(subscription)
            del _LOCAL_SUBSCRIPTIONS[key]
    return tuple(removed)


def local_subscription_count(context_id: str | None = None) -> int:
    if context_id is None:
        return len(_LOCAL_SUBSCRIPTIONS)
    context = validate_context_id(context_id)
    return sum(1 for key in _LOCAL_SUBSCRIPTIONS if key[0] == str(context))


def streaming_docs_evidence(connect_url: str | None = None) -> dict[str, JsonValue]:
    observed_connect_url = (
        build_streaming_connect_url("docsEvidenceCtx") if connect_url is None else connect_url
    )
    return {
        "official_docs_checked": False,
        "official_docs_check_caveat": (
            "The probe does not fetch Saxo documentation at runtime; it compares "
            "requested limits against vendored limits with declared Saxo source URLs."
        ),
        "official_limits_declared": True,
        "streaming_endpoint": SIM_STREAMING_ENDPOINT,
        "streaming_connect_path": "/connect",
        "token_in_query_url": token_in_streaming_query_url(observed_connect_url),
        "token_query_url_check_source": (
            "sample_streaming_connect_url"
            if connect_url is None
            else "actual_streaming_connect_url"
        ),
        "context_id_max_length": CONTEXT_ID_MAX_LENGTH,
        "reference_id_max_length": REFERENCE_ID_MAX_LENGTH,
        "limit_expected_connections": STREAMING_LIMITS.expected_connections,
        "limit_expected_price_instruments": STREAMING_LIMITS.expected_price_instruments,
        "official_limit_sources": list(STREAMING_LIMITS.sources),
    }


def _parse_message(frame: bytes, offset: int) -> tuple[SaxoStreamingMessage, int]:
    min_header_size = 15
    if len(frame) - offset < min_header_size:
        raise StreamingValidationError("streaming_frame_truncated_header", str(offset))
    message_id = int.from_bytes(frame[offset : offset + 8], "little", signed=False)
    reference_length = frame[offset + 10]
    reference_start = offset + 11
    reference_end = reference_start + reference_length
    payload_format_offset = reference_end
    payload_size_start = payload_format_offset + 1
    payload_start = payload_size_start + 4
    if len(frame) < payload_start:
        raise StreamingValidationError("streaming_frame_truncated_reference", str(offset))
    reference_id = frame[reference_start:reference_end].decode("ascii")
    payload_format = frame[payload_format_offset]
    payload_size = int.from_bytes(
        frame[payload_size_start:payload_start],
        "little",
        signed=False,
    )
    payload_end = payload_start + payload_size
    if len(frame) < payload_end:
        raise StreamingValidationError("streaming_frame_truncated_payload", reference_id)
    payload_bytes = frame[payload_start:payload_end]
    payload_text, payload_json = _payload(payload_format, payload_bytes)
    return (
        SaxoStreamingMessage(
            message_id=message_id,
            reference_id=reference_id,
            payload_format=payload_format,
            payload_size=payload_size,
            payload_text=payload_text,
            payload_json=payload_json,
            control_message=reference_id.startswith("_"),
        ),
        payload_end,
    )


def _payload(payload_format: int, payload_bytes: bytes) -> tuple[str, JsonValue]:
    match payload_format:
        case 0:
            payload_text = payload_bytes.decode("utf-8")
            return payload_text, _JSON_VALUE_ADAPTER.validate_json(payload_text)
        case 1:
            return payload_bytes.hex(), {"binary_payload_size": len(payload_bytes)}
        case _:
            return payload_bytes.hex(), {
                "unknown_payload_format": payload_format,
                "binary_payload_size": len(payload_bytes),
            }
