from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx2
from fastmcp.tools import ToolResult

from saxo_bank_mcp.endpoint_registry import (
    RegisteredEndpoint,
    find_registered_endpoint,
    path_rejection_reason,
    registered_operations_for_path,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_token_refresh import live_token_for_tool
from saxo_bank_mcp.read_fingerprints import is_balance_operation, response_fingerprint
from saxo_bank_mcp.read_tool_execution import execution_context, read_headers
from saxo_bank_mcp.read_tool_results import (
    call_class,
    denied,
    invalid_response,
    network_error,
    response_body,
    tool_result,
)
from saxo_bank_mcp.read_tool_types import (
    READ_DOES_NOT_VERIFY,
    READINESS_PREREQUISITES,
    REGISTERED_CALL_TOOL_DESCRIPTION,
    EndpointPreflightResult,
    ReadExecutionContext,
    ReadLeaf,
    ReadObject,
    ReadResponseMode,
    ReadToolResult,
    ReadToolValue,
)

__all__ = [
    "READINESS_PREREQUISITES",
    "READ_DOES_NOT_VERIFY",
    "REGISTERED_CALL_TOOL_DESCRIPTION",
    "ReadLeaf",
    "ReadObject",
    "ReadToolResult",
    "ReadToolValue",
    "saxo_call_registered_endpoint",
]

HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300


async def saxo_call_registered_endpoint(
    method: str,
    path: str,
    params: Mapping[str, str] | None = None,
    response_mode: ReadResponseMode = "redacted_body",
) -> ToolResult:
    preflight = _registered_endpoint_or_refusal(method, path)
    if not isinstance(preflight, RegisteredEndpoint):
        return tool_result(preflight)
    registered = preflight
    operation = registered.operation
    if is_balance_operation(operation.operation_id) and response_mode != "fingerprint_only":
        return tool_result(
            denied(
                method,
                path,
                "sensitive_response_requires_fingerprint_only",
                operation=operation,
            ),
        )
    context_or_result = await execution_context(
        operation,
        live_token_loader=live_token_for_tool,
    )
    if not isinstance(context_or_result, ReadExecutionContext):
        return tool_result({**context_or_result, "live_write": False})
    context = context_or_result
    headers = read_headers(context.token)
    try:
        async with create_async_client(base_url=context.rest_base_url) as client:
            response = await client.get(
                registered.resolved_path.lstrip("/"),
                params={} if params is None else dict(params),
                headers=headers,
            )
    except httpx2.HTTPError as error:
        return tool_result(network_error(operation, context.environment, type(error).__name__))
    ok = HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX
    try:
        fingerprint, fingerprint_scope = response_fingerprint(
            operation.operation_id,
            response.content,
        )
    except ValueError:
        if ok:
            return tool_result(
                invalid_response(operation, context.environment, response.status_code),
            )
        fingerprint = None
        fingerprint_scope = None
    body = None if response_mode == "fingerprint_only" else response_body(response)
    status = "passed" if ok else "http_error"
    environment = context.environment
    live_access = environment == "LIVE"
    return tool_result(
        {
            "status": status,
            "tool_name": "saxo_call_registered_endpoint",
            "call_class": call_class(status, environment),
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "method": operation.method,
            "path": operation.path_template,
            "environment": environment,
            "network_call_made": True,
            "live_write_called": False,
            "order_or_subscription_created": False,
            "arbitrary_url_allowed": False,
            "live_write": False,
            "live_access": live_access,
            "auth_exercised": context.token is not None,
            "trading_ready": False,
            "http_status": response.status_code,
            "response": body,
            "response_visibility": response_mode,
            "response_fingerprint": fingerprint,
            "response_fingerprint_scope": fingerprint_scope,
            "does_not_verify": list(READ_DOES_NOT_VERIFY),
        },
    )


def _registered_endpoint_or_refusal(
    method: str,
    path: str,
) -> EndpointPreflightResult:
    bad_path_reason = path_rejection_reason(path)
    if bad_path_reason is not None:
        return denied(method, path, bad_path_reason)
    registered = find_registered_endpoint(method, path)
    if registered is None:
        path_operations = registered_operations_for_path(path)
        if path_operations:
            return denied(method, path, "method_not_allowed", operation=path_operations[0])
        return denied(method, path, "unregistered_endpoint")
    operation = registered.operation
    if operation.status != "implemented":
        return denied(method, path, operation.refusal_reason, operation=operation)
    if operation.method != "GET" or operation.read_write_class != "read":
        return denied(method, path, "write_class_not_allowed", operation=operation)
    return registered
