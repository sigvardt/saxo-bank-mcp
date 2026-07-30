from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

from fastmcp.tools import ToolResult

from saxo_bank_mcp.analytics_source_receipt import analytics_contract_receipt
from saxo_bank_mcp.endpoint_registry import (
    RegisteredEndpoint,
    find_registered_endpoint,
    path_rejection_reason,
    registered_operations_for_path,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_token_refresh import live_token_for_tool
from saxo_bank_mcp.read_fingerprints import is_balance_operation, response_fingerprint
from saxo_bank_mcp.read_tool_results import (
    call_class,
    denied,
    invalid_response,
    response_body,
    tool_result,
)
from saxo_bank_mcp.read_tool_types import (
    READ_DOES_NOT_VERIFY,
    READINESS_PREREQUISITES,
    REGISTERED_CALL_TOOL_DESCRIPTION,
    EndpointPreflightResult,
    ReadLeaf,
    ReadObject,
    ReadResponseMode,
    ReadToolResult,
    ReadToolValue,
)
from saxo_bank_mcp.registered_read_execution import (
    RegisteredReadClientFactory,
    RegisteredReadResponse,
    execute_registered_get,
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


async def saxo_call_registered_endpoint(  # noqa: PLR0911
    method: str,
    path: str,
    params: Mapping[str, str] | None = None,
    response_mode: ReadResponseMode = "redacted_body",
    analytics_contract_id: str | None = None,
) -> ToolResult:
    preflight = _registered_endpoint_or_refusal(method, path)
    if not isinstance(preflight, RegisteredEndpoint):
        return tool_result(preflight)
    registered = preflight
    operation = registered.operation
    if is_balance_operation(operation.operation_id) and response_mode not in {
        "fingerprint_only",
        "analytics_contract_receipt",
    }:
        return tool_result(
            denied(
                method,
                path,
                "sensitive_response_requires_fingerprint_only",
                operation=operation,
            ),
        )
    if response_mode == "analytics_contract_receipt":
        if analytics_contract_id is None:
            return tool_result(
                denied(
                    method,
                    path,
                    "analytics_contract_id_required",
                    operation=operation,
                ),
            )
        receipt = await analytics_contract_receipt(
            registered,
            contract_id=analytics_contract_id,
            params={} if params is None else params,
        )
        return tool_result(cast("ReadToolResult", receipt))
    if analytics_contract_id is not None:
        return tool_result(
            denied(
                method,
                path,
                "analytics_contract_mode_required",
                operation=operation,
            ),
        )
    outcome = await execute_registered_get(
        operation,
        registered.resolved_path,
        {} if params is None else params,
        client_factory=cast("RegisteredReadClientFactory", create_async_client),
        live_token_loader=live_token_for_tool,
    )
    if not isinstance(outcome, RegisteredReadResponse):
        return tool_result(outcome)
    context = outcome.context
    response = outcome.response
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
    body = (
        None
        if response_mode == "fingerprint_only"
        else response_body(response, token=context.token)
    )
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
