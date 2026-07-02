from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.streaming import (
    SIM_STREAMING_ENDPOINT,
    StreamingValidationError,
    validate_context_id,
    validate_reference_id,
)

type JsonObject = dict[str, JsonValue]
_JSON_OBJECT_ADAPTER: Final[TypeAdapter[JsonObject]] = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class StreamProbeOptions:
    require_frame: bool
    expect_connections: int | None
    expect_price_instruments: int | None


@dataclass(frozen=True, slots=True)
class StreamCleanupProbeOptions:
    simulate_leak: bool


@dataclass(frozen=True, slots=True)
class StreamLimitCheck:
    official_connections: int
    official_price_instruments: int
    connections_match: bool
    price_instruments_match: bool
    limits_match_official: bool


def parse_tool_payload(value: JsonValue) -> JsonObject:
    return _JSON_OBJECT_ADAPTER.validate_python(value)


def stream_limit_check(
    options: StreamProbeOptions,
    tool_payload: JsonObject,
) -> StreamLimitCheck:
    official_connections = int_field(tool_payload, "limit_expected_connections")
    official_price_instruments = int_field(tool_payload, "limit_expected_price_instruments")
    connections_match = expected_limit_matches(
        options.expect_connections,
        official_connections,
    )
    price_instruments_match = expected_limit_matches(
        options.expect_price_instruments,
        official_price_instruments,
    )
    return StreamLimitCheck(
        official_connections=official_connections,
        official_price_instruments=official_price_instruments,
        connections_match=connections_match,
        price_instruments_match=price_instruments_match,
        limits_match_official=connections_match and price_instruments_match,
    )


def stream_event_payload(
    options: StreamProbeOptions,
    tool_payload: JsonObject,
    limit_check: StreamLimitCheck,
) -> JsonObject:
    status = qa_stream_status(
        tool_payload,
        require_frame=options.require_frame,
        limits_match_official=limit_check.limits_match_official,
    )
    return {
        **base_event("stream", status, "FastMCP streaming tool exercised in SIM mode"),
        "command": "stream",
        "tool_name": str(tool_payload.get("tool_name", "")),
        "environment": "SIM",
        "fastmcp_called": tool_payload.get("fastmcp_called") is True,
        "official_docs_checked": tool_payload.get("official_docs_checked") is True,
        "streaming_endpoint": SIM_STREAMING_ENDPOINT,
        "token_in_query_url": tool_payload.get("token_in_query_url") is True,
        "token_query_url_check_source": str(
            tool_payload.get("token_query_url_check_source", ""),
        ),
        "authorization_header_used": tool_payload.get("authorization_header_used") is True,
        "context_id_validated": tool_payload.get("context_id_validated") is True,
        "reference_id_validated": tool_payload.get("reference_id_validated") is True,
        "limit_expected_connections": limit_check.official_connections,
        "limit_expected_price_instruments": limit_check.official_price_instruments,
        "requested_expect_connections": options.expect_connections,
        "requested_expect_price_instruments": options.expect_price_instruments,
        "expect_connections_match": limit_check.connections_match,
        "expect_price_instruments_match": limit_check.price_instruments_match,
        "limits_match_official": limit_check.limits_match_official,
        "subscription_snapshot_recorded": (
            tool_payload.get("subscription_snapshot_recorded") is True
        ),
        "websocket_frame_recorded": tool_payload.get("websocket_frame_recorded") is True,
        "data_message_observed": tool_payload.get("data_message_observed") is True,
        "control_message_observed": tool_payload.get("control_message_observed") is True,
        "control_only_no_data": tool_payload.get("control_only_no_data") is True,
        "streaming_completion_claim_allowed": (
            tool_payload.get("streaming_completion_claim_allowed") is True
        ),
        "stream_live_verified": (
            status == "passed"
            and tool_payload.get("streaming_completion_claim_allowed") is True
        ),
        "qa_status_is_authoritative": True,
        "probe_exit_code_caveat": (
            "QA exit 0 means the probe observed the expected status; agents must read "
            "status and streaming_completion_claim_allowed before treating a stream as live; "
            "incomplete_no_frame always exits nonzero."
        ),
        "network_call_made": tool_payload.get("network_call_made") is True,
        "live_write": tool_payload.get("live_write") is True,
        "tool_result": tool_payload,
        "git": current_git_state().model_dump(mode="json"),
    }


def cleanup_event_payload(
    options: StreamCleanupProbeOptions,
    tool_payload: JsonObject,
) -> JsonObject:
    status = qa_cleanup_status(tool_payload)
    return {
        **base_event("stream-cleanup", status, "FastMCP streaming cleanup removed local leak"),
        "command": "stream-cleanup",
        "tool_name": str(tool_payload.get("tool_name", "")),
        "simulate_leak": options.simulate_leak,
        "simulated_leak": options.simulate_leak,
        "fastmcp_called": tool_payload.get("fastmcp_called") is True,
        "environment": "SIM",
        "malformed_context_denied": malformed_context_denied(),
        "malformed_reference_denied": malformed_reference_denied(),
        "local_registry_before_count": int_field(tool_payload, "local_registry_before_count"),
        "local_registry_after_count": int_field(tool_payload, "local_registry_after_count"),
        "local_removed_reference_ids": tool_payload.get("local_removed_reference_ids", []),
        "local_open_records_after": tool_payload.get("local_open_records_after", []),
        "local_open_records_left": tool_payload.get("local_open_records_left") is True,
        "cleanup_endpoint": str(tool_payload.get("cleanup_endpoint", "")),
        "cleanup_attempted": tool_payload.get("cleanup_attempted") is True,
        "cleanup_status": str(tool_payload.get("cleanup_status", "")),
        "remote_cleanup_confirmed": tool_payload.get("remote_cleanup_confirmed") is True,
        "remote_cleanup_status_known": (
            tool_payload.get("remote_cleanup_status_known") is True
        ),
        "remote_subscription_may_remain": (
            tool_payload.get("remote_subscription_may_remain") is True
        ),
        "remote_cleanup_claim_allowed": (
            tool_payload.get("remote_cleanup_claim_allowed") is True
        ),
        "cleanup_remote_verified": (
            status == "passed" and tool_payload.get("remote_cleanup_confirmed") is True
        ),
        "qa_status_is_authoritative": True,
        "any_subscription_may_remain": (
            tool_payload.get("any_subscription_may_remain") is True
        ),
        "token_in_query_url": tool_payload.get("token_in_query_url") is True,
        "bearer_token_in_logs": tool_payload.get("bearer_token_in_logs") is True,
        "network_call_made": tool_payload.get("network_call_made") is True,
        "live_write": tool_payload.get("live_write") is True,
        "open_subscription_left": tool_payload.get("open_subscription_left") is True,
        "open_subscription_left_scope": str(
            tool_payload.get("open_subscription_left_scope", "local_registry_only"),
        ),
        "tool_result": tool_payload,
        "git": current_git_state().model_dump(mode="json"),
    }


def qa_stream_status(
    tool_payload: JsonObject,
    *,
    require_frame: bool,
    limits_match_official: bool,
) -> str:
    if not limits_match_official:
        return "failed"
    match str(tool_payload.get("status", "failed")):
        case "completed":
            if require_frame and tool_payload.get("websocket_frame_recorded") is not True:
                return "incomplete_no_frame"
            return "passed"
        case "auth_required":
            return "incomplete_auth_required"
        case "incomplete_no_frame" | "control_only_no_data":
            return "incomplete_no_frame"
        case _:
            return "failed"


def qa_cleanup_status(tool_payload: JsonObject) -> str:
    match str(tool_payload.get("status", "failed")):
        case "completed":
            return "passed"
        case "auth_required":
            return "incomplete_auth_required"
        case "cleanup_remote_failed":
            return "cleanup_remote_failed"
        case "denied":
            return "denied"
        case _:
            return "failed"


def expected_limit_matches(requested: int | None, official: int) -> bool:
    return requested is None or requested == official


def malformed_context_denied() -> bool:
    try:
        validate_context_id("bad context")
    except StreamingValidationError:
        return True
    return False


def malformed_reference_denied() -> bool:
    try:
        validate_reference_id("_heartbeat")
    except StreamingValidationError:
        return True
    return False


def int_field(source: JsonObject, key: str) -> int:
    value = source.get(key)
    if isinstance(value, int):
        return value
    return 0
