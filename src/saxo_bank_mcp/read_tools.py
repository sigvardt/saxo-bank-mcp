from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import httpx2
from fastmcp.tools import ToolResult
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_text
from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.config import (
    LIVE_ENDPOINTS,
    SIM_ENDPOINTS,
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.endpoint_registry import (
    EndpointOperation,
    RegisteredEndpoint,
    find_registered_endpoint,
    path_rejection_reason,
    registered_operations_for_path,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_cached_token_for_tool,
    live_read_missing_requirements_for_reason,
    live_read_next_action,
    resolve_live_read_settings,
)
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)

type ReadLeaf = str | int | bool | None
type ReadObject = dict[str, ReadLeaf]
type ReadToolValue = (
    ReadLeaf | list[str] | ReadObject | dict[str, int] | dict[str, bool] | list[ReadObject]
)
type ReadToolResult = dict[str, ReadToolValue]
type EndpointPreflightResult = RegisteredEndpoint | ReadToolResult
type ReadExecutionResult = ReadExecutionContext | ReadToolResult

REGISTERED_CALL_TOOL_DESCRIPTION: Final = (
    "Calls registered Saxo OpenAPI GET/read operations only in SIM or explicitly enabled "
    "LIVE read mode. It denies unregistered, arbitrary-host, and write-class operations "
    "before any network call. It never performs LIVE writes."
)
READ_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "Saxo connectivity",
    "credentials/session",
    "account access",
    "catalog completeness/freshness vs live Saxo",
    "trading/order readiness",
    "instrument/account suitability",
    "real-money approval",
    "live write readiness",
)
READINESS_PREREQUISITES: Final[tuple[str, ...]] = (
    "valid Saxo session",
    "required account entitlements",
    "instrument/account suitability checks",
    "two-factor approval for write-class tools",
)
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300
JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class ReadExecutionContext:
    environment: TokenEnvironment
    rest_base_url: str
    token: SaxoTokenSet | None


async def saxo_call_registered_endpoint(
    method: str,
    path: str,
    params: Mapping[str, str] | None = None,
) -> ToolResult:
    preflight = _registered_endpoint_or_refusal(method, path)
    match preflight:
        case RegisteredEndpoint() as registered:
            operation = registered.operation
        case dict() as result:
            return _tool_result(result)
    context_or_result = _execution_context(operation)
    match context_or_result:
        case ReadExecutionContext() as context:
            headers = _read_headers(context.token)
        case dict() as result:
            return _tool_result({**result, "network_call_made": False, "live_write": False})
    try:
        async with create_async_client(base_url=context.rest_base_url) as client:
            response = await client.get(
                registered.resolved_path.lstrip("/"),
                params={} if params is None else dict(params),
                headers=headers,
            )
    except httpx2.HTTPError as error:
        return _tool_result(_network_error(operation, context.environment, type(error).__name__))
    body = _response_body(response)
    ok = HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX
    status = "passed" if ok else "http_error"
    environment = context.environment
    live_access = environment == "LIVE"
    return _tool_result(
        {
            "status": status,
            "tool_name": "saxo_call_registered_endpoint",
            "call_class": _call_class(status, environment),
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "method": operation.method,
            "path": operation.path_template,
            "resolved_path": registered.resolved_path,
            "environment": environment,
            "network_call_made": True,
            "arbitrary_url_allowed": False,
            "live_write": False,
            "live_access": live_access,
            "auth_exercised": context.token is not None,
            "trading_ready": False,
            "http_status": response.status_code,
            "response": body,
            "does_not_verify": list(READ_DOES_NOT_VERIFY),
        },
    )


def _tool_result(result: ReadToolResult) -> ToolResult:
    return ToolResult(structured_content=result, is_error=result.get("status") == "denied")


def _registered_endpoint_or_refusal(
    method: str,
    path: str,
) -> EndpointPreflightResult:
    bad_path_reason = path_rejection_reason(path)
    if bad_path_reason is not None:
        return _denied(method, path, bad_path_reason)
    registered = find_registered_endpoint(method, path)
    if registered is None:
        path_operations = registered_operations_for_path(path)
        if path_operations:
            return _denied(method, path, "method_not_allowed", operation=path_operations[0])
        return _denied(method, path, "unregistered_endpoint")
    operation = registered.operation
    if operation.status != "implemented":
        return _denied(method, path, operation.refusal_reason, operation=operation)
    if operation.method != "GET" or operation.read_write_class != "read":
        return _denied(method, path, "write_class_not_allowed", operation=operation)
    return registered


def _execution_context(operation: EndpointOperation) -> ReadExecutionResult:
    runtime = SaxoRuntimeConfig.from_env()
    match runtime.effective_read_environment():
        case "SIM":
            return _sim_execution_context(operation)
        case "LIVE_READ_DISABLED":
            return _live_refusal(operation, runtime)
        case "LIVE":
            return _live_execution_context(operation)


def _sim_execution_context(operation: EndpointOperation) -> ReadExecutionResult:
    if operation.auth_requirement == "none":
        return ReadExecutionContext(
            environment="SIM",
            rest_base_url=SIM_ENDPOINTS.rest_base_url,
            token=None,
        )
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return _auth_required(error.code)
    cache_check = cached_token_for_tool("saxo_call_registered_endpoint", settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return _auth_required(str(result.get("reason", "token_missing")))
        case CachedTokenReady(token=token):
            return ReadExecutionContext(
                environment="SIM",
                rest_base_url=SIM_ENDPOINTS.rest_base_url,
                token=token,
            )


def _live_execution_context(operation: EndpointOperation) -> ReadExecutionResult:
    try:
        settings = resolve_live_read_settings()
    except LiveReadSettingsError as error:
        return _live_auth_required(operation, error.code)
    if operation.auth_requirement == "none":
        return ReadExecutionContext(
            environment="LIVE",
            rest_base_url=LIVE_ENDPOINTS.rest_base_url,
            token=None,
        )
    token_or_result = live_cached_token_for_tool(
        "saxo_call_registered_endpoint",
        settings.cache_path,
    )
    if isinstance(token_or_result, dict):
        return _live_auth_required(
            operation,
            str(token_or_result.get("reason", "token_cache_missing")),
        )
    return ReadExecutionContext(
        environment="LIVE",
        rest_base_url=settings.rest_base_url,
        token=token_or_result,
    )


def _read_headers(token: SaxoTokenSet | None) -> dict[str, str]:
    if token is None:
        return {"Accept": "application/json"}
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.access_token}",
    }


def _denied(
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
        "method": method.upper(),
        "path": path,
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
        "order_or_subscription_created": False,
        "remediation_hint": "Use registered read endpoints or an approved write-preview tool.",
        "allowed_alternative": "saxo_list_registered_endpoints",
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def _live_refusal(operation: EndpointOperation, runtime: SaxoRuntimeConfig) -> ReadToolResult:
    return {
        "status": "live_not_called",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "live_read_disabled",
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "requested_environment": runtime.requested_environment.value,
        "effective_read_environment": runtime.effective_read_environment(),
        "network_call_made": False,
        "reason": "missing_live_read_enablement",
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def _live_auth_required(operation: EndpointOperation, reason: str) -> ReadToolResult:
    return {
        "status": "auth_required",
        "tool_name": "saxo_call_registered_endpoint",
        "requested_environment": "LIVE",
        "environment": "LIVE",
        "reason": reason,
        "missing_requirements": live_read_missing_requirements_for_reason(reason),
        "scope_used": False,
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "next_action": live_read_next_action(reason),
        "verifies": [],
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
        "call_class": "live_read_auth_required_before_network",
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
    }


def _network_error(
    operation: EndpointOperation,
    environment: TokenEnvironment,
    detail: str,
) -> ReadToolResult:
    return {
        "status": "network_error",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": _call_class("network_error", environment),
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "environment": environment,
        "network_call_made": True,
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


def _auth_required(reason: str) -> ReadToolResult:
    return {
        "status": "auth_required",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "auth_required_before_network",
        "environment": "SIM",
        "reason": reason,
        "remediation_hint": "Run saxo_start_pkce_login, then retry after a token is cached.",
        "network_call_made": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def _response_body(response: httpx2.Response) -> ReadLeaf:
    if not response.content:
        return None
    try:
        parsed = JSON_VALUE_ADAPTER.validate_json(response.text)
    except ValidationError:
        return redact_text(response.text)
    return json.dumps(redact_json(parsed), separators=(",", ":"), sort_keys=True)


def _call_class(status: str, environment: TokenEnvironment) -> str:
    prefix = "live" if environment == "LIVE" else "sim"
    match status:
        case "passed":
            return f"{prefix}_read_succeeded"
        case "network_error":
            return f"{prefix}_read_attempted"
        case _:
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
