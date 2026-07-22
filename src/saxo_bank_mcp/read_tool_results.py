from __future__ import annotations

import json
from typing import Final

import httpx2
from fastmcp.tools import ToolResult
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_text
from saxo_bank_mcp.auth import TokenEnvironment
from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.live_mode import (
    live_read_missing_requirements_for_reason,
    live_read_next_action,
)
from saxo_bank_mcp.read_tool_types import READ_DOES_NOT_VERIFY, ReadLeaf, ReadToolResult
from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


def tool_result(result: ReadToolResult) -> ToolResult:
    return ToolResult(
        structured_content=result,
        is_error=result.get("status") != "passed",
    )


def denied(
    method: str,
    path: str,
    reason: str,
    *,
    operation: EndpointOperation | None = None,
) -> ReadToolResult:
    return {
        "status": "denied",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "denied_before_network",
        "operation_id": None if operation is None else operation.operation_id,
        "method": "GET" if method.upper() == "GET" else "<redacted>",
        "path": (
            operation.path_template
            if operation is not None
            else _redacted_denied_path(path, reason)
        ),
        "network_call_made": False,
        "denial_reason": reason,
        "denied_class": _denied_class(reason),
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
        "preview_created": False,
        "approval_requested": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "remediation_hint": "Use registered read endpoints or an approved write-preview tool.",
        "allowed_alternative": "saxo_list_registered_endpoints",
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def live_auth_required(
    operation: EndpointOperation,
    reason: str,
    *,
    network_call_made: bool = False,
    missing_requirements: list[str] | None = None,
    next_action: str | None = None,
) -> ReadToolResult:
    return {
        "status": "auth_required",
        "tool_name": "saxo_call_registered_endpoint",
        "requested_environment": "LIVE",
        "environment": "LIVE",
        "reason": reason,
        "missing_requirements": (
            live_read_missing_requirements_for_reason(reason)
            if missing_requirements is None
            else missing_requirements
        ),
        "scope_used": False,
        "network_call_made": network_call_made,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "next_action": live_read_next_action(reason) if next_action is None else next_action,
        "verifies": [],
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
        "call_class": (
            "live_read_auth_required_after_refresh"
            if network_call_made
            else "live_read_auth_required_before_network"
        ),
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
    }


def network_error(
    operation: EndpointOperation,
    environment: TokenEnvironment,
    detail: str,
) -> ReadToolResult:
    return {
        "status": "network_error",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": call_class("network_error", environment),
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "environment": environment,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "detail": detail,
        "network_error_type": detail,
        "network_call_outcome": "failed",
        "network_error_message_redacted": redact_text(detail),
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": environment == "LIVE",
        "auth_exercised": operation.auth_requirement != "none",
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def invalid_response(
    operation: EndpointOperation,
    environment: TokenEnvironment,
    http_status: int,
) -> ReadToolResult:
    return {
        "status": "invalid_response",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": call_class("invalid_response", environment),
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "environment": environment,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "http_status": http_status,
        "response": None,
        "response_visibility": "fingerprint_only",
        "response_fingerprint": None,
        "response_fingerprint_scope": None,
        "reason": "response_schema_invalid_for_safe_fingerprint",
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": environment == "LIVE",
        "auth_exercised": operation.auth_requirement != "none",
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def auth_required(reason: str) -> ReadToolResult:
    return {
        "status": "auth_required",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "auth_required_before_network",
        "environment": "SIM",
        "reason": reason,
        "remediation_hint": "Run saxo_start_pkce_login, then retry after a token is cached.",
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def response_body(response: httpx2.Response) -> ReadLeaf:
    if not response.content:
        return None
    try:
        parsed = JSON_VALUE_ADAPTER.validate_python(
            parse_json_value(response.text),
            strict=True,
        )
    except (StrictJsonError, ValidationError):
        return redact_text(response.text)
    return json.dumps(redact_json(parsed), separators=(",", ":"), sort_keys=True)


def call_class(status: str, environment: TokenEnvironment) -> str:
    prefix = "live" if environment == "LIVE" else "sim"
    if status == "passed":
        return f"{prefix}_read_succeeded"
    if status == "network_error":
        return f"{prefix}_read_attempted"
    return f"{prefix}_read_http_error"


def _denied_class(reason: str) -> str:
    if reason in {"write_operations_disabled_by_policy", "write_class_not_allowed"}:
        return "write"
    if reason == "absolute_url_rejected":
        return "host"
    if reason == "method_not_allowed":
        return "method_not_allowed"
    if reason == "unregistered_endpoint":
        return "unregistered"
    return "policy"


def _redacted_denied_path(path: str, reason: str) -> str:
    if not path:
        return "<redacted-empty-path>"
    return f"<redacted-{_denied_class(reason)}-path>"
