from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastmcp.tools import ToolResult

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    SaxoEnvironment,
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.mcp_tool_results import auth_next_action
from saxo_bank_mcp.streaming import (
    STREAMING_LIMITS,
    ContextId,
    ReferenceId,
    StreamingValidationError,
    streaming_docs_evidence,
    validate_context_id,
    validate_reference_id,
)

type JsonObject = dict[str, JsonValue]
ERROR_STATUSES: Final = frozenset(
    {
        "auth_required",
        "denied",
        "http_error",
        "network_error",
        "incomplete_no_frame",
        "control_only_no_data",
        "cleanup_remote_failed",
    },
)


@dataclass(frozen=True, slots=True)
class PriceStreamingRequest:
    context: ContextId
    reference: ReferenceId
    uics: tuple[int, ...]
    asset_type: str


def validate_price_subscription_request(
    context_id: str,
    reference_id: str,
    uics: list[int],
    asset_type: str,
) -> PriceStreamingRequest | JsonObject:
    try:
        context = validate_context_id(context_id)
    except StreamingValidationError as error:
        return denied(
            "saxo_create_streaming_price_subscription",
            error.code,
            context_id_validated=False,
            reference_id_validated=False,
        )
    try:
        reference = validate_reference_id(reference_id)
    except StreamingValidationError as error:
        return denied(
            "saxo_create_streaming_price_subscription",
            error.code,
            reference_id_validated=False,
        )
    if not uics:
        return denied("saxo_create_streaming_price_subscription", "uics_missing")
    if len(uics) > STREAMING_LIMITS.expected_price_instruments:
        return denied(
            "saxo_create_streaming_price_subscription",
            "price_instrument_limit_exceeded",
        )
    return PriceStreamingRequest(context, reference, tuple(uics), asset_type)


def tool_result(payload: JsonObject) -> ToolResult:
    status = str(payload.get("status", "unknown"))
    return ToolResult(
        content=f"{payload.get('tool_name', 'saxo_streaming_tool')}: {status}",
        structured_content=payload,
        is_error=status in ERROR_STATUSES,
    )


def cached_token(tool_name: str) -> SaxoTokenSet | JsonObject:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return auth_required(tool_name, error.code)
    cache_check = cached_token_for_tool(tool_name, settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return auth_required(tool_name, str(result.get("reason", "token_cache_missing")))
        case CachedTokenReady(token=token):
            return token


def auth_required(tool_name: str, reason: str) -> JsonObject:
    return {
        **base_payload(tool_name, "auth_required"),
        "reason": reason,
        "next_action": auth_next_action(reason),
        "network_call_made": False,
        "authorization_header_used": False,
        "subscription_snapshot_recorded": False,
        "websocket_frame_recorded": False,
        "streaming_completion_claim_allowed": False,
    }


def live_streaming_denial(tool_name: str) -> JsonObject | None:
    match SaxoRuntimeConfig.from_env().requested_environment:
        case SaxoEnvironment.SIM:
            return None
        case SaxoEnvironment.LIVE:
            return denied(
                tool_name,
                "streaming_sim_only",
                environment=SaxoEnvironment.LIVE,
                context_id_validated=False,
                reference_id_validated=False,
            )


def base_payload(
    tool_name: str,
    status: str,
    *,
    environment: SaxoEnvironment = SaxoEnvironment.SIM,
    context_id_validated: bool = True,
    reference_id_validated: bool = True,
) -> JsonObject:
    return {
        **streaming_docs_evidence(),
        "status": status,
        "tool_name": tool_name,
        "environment": environment.value,
        "fastmcp_called": True,
        "context_id_validated": context_id_validated,
        "reference_id_validated": reference_id_validated,
        "bearer_token_in_logs": False,
        "live_write": False,
        "order_or_subscription_created": False,
    }


def denied(
    tool_name: str,
    reason: str,
    *,
    environment: SaxoEnvironment = SaxoEnvironment.SIM,
    context_id_validated: bool = True,
    reference_id_validated: bool = True,
) -> JsonObject:
    return {
        **base_payload(
            tool_name,
            "denied",
            environment=environment,
            context_id_validated=context_id_validated,
            reference_id_validated=reference_id_validated,
        ),
        "denial_reason": reason,
        "network_call_made": False,
        "authorization_header_used": False,
        "subscription_snapshot_recorded": False,
        "websocket_frame_recorded": False,
        "streaming_completion_claim_allowed": False,
    }
