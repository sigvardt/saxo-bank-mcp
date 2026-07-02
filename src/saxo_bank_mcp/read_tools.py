from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx2
from fastmcp.tools import ToolResult

from saxo_bank_mcp._redaction import redact_text
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
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

REGISTERED_CALL_TOOL_DESCRIPTION: Final = (
    "Calls registered Saxo OpenAPI operations only. In this phase it allows safe SIM GET "
    "read operations and denies unregistered or write-class operations before any network call. "
    "It never accepts arbitrary hosts or LIVE writes."
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
    token_or_result = _token_for_operation(operation)
    match token_or_result:
        case SaxoTokenSet() as token:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token.access_token}",
            }
        case None:
            headers = {"Accept": "application/json"}
        case dict() as result:
            return _tool_result({**result, "network_call_made": False, "live_write": False})
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            response = await client.get(
                registered.resolved_path.lstrip("/"),
                params={} if params is None else dict(params),
                headers=headers,
            )
    except httpx2.HTTPError as error:
        return _tool_result(_network_error(operation, type(error).__name__))
    body = _response_body(response)
    ok = HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX
    status = "passed" if ok else "http_error"
    return _tool_result(
        {
            "status": status,
            "tool_name": "saxo_call_registered_endpoint",
            "call_class": ("sim_read_succeeded" if status == "passed" else "sim_read_http_error"),
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "method": operation.method,
            "path": operation.path_template,
            "resolved_path": registered.resolved_path,
            "environment": "SIM",
            "network_call_made": True,
            "arbitrary_url_allowed": False,
            "live_write": False,
            "live_access": False,
            "auth_exercised": operation.auth_requirement != "none",
            "trading_ready": False,
            "http_status": response.status_code,
            "response": body,
            "does_not_verify": list(READ_DOES_NOT_VERIFY),
        },
    )


def _tool_result(result: ReadToolResult) -> ToolResult:
    return ToolResult(structured_content=result, is_error=result.get("status") == "denied")


def _registered_endpoint_or_refusal(  # noqa: PLR0911
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
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.effective_read_environment() != "SIM":
        return _live_refusal(operation)
    return registered


def _token_for_operation(operation: EndpointOperation) -> SaxoTokenSet | ReadToolResult | None:
    if operation.auth_requirement == "none":
        return None
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return _auth_required(error.code)
    cache_check = cached_token_for_tool("saxo_call_registered_endpoint", settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return _auth_required(str(result.get("reason", "token_missing")))
        case CachedTokenReady(token=token):
            return token


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


def _live_refusal(operation: EndpointOperation) -> ReadToolResult:
    return {
        "status": "live_not_called",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "live_read_disabled",
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "network_call_made": False,
        "reason": "missing_live_read_enablement",
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def _network_error(operation: EndpointOperation, detail: str) -> ReadToolResult:
    return {
        "status": "network_error",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_attempted",
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "environment": "SIM",
        "network_call_made": True,
        "detail": detail,
        "network_error_type": detail,
        "network_call_outcome": "failed",
        "network_error_message_redacted": redact_text(detail),
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
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
    return redact_text(response.text)


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
