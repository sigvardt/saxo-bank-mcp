from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx2
from fastmcp.tools import ToolResult
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_text
from saxo_bank_mcp.audit import AuditPathError, append_audit_event
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    SIM_ENDPOINTS,
    SaxoEnvironment,
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_mode import live_write_refusal_payload
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.mcp_tool_results import auth_next_action
from saxo_bank_mcp.order_mutation_guards import request_body_coherence_reasons
from saxo_bank_mcp.order_mutation_models import (
    HTTP_SUCCESS_MAX,
    HTTP_SUCCESS_MIN,
    ORDER_WRITE_SPECS,
    JsonObject,
    OrderWriteClass,
    OrderWriteOutcome,
    OrderWriteSpec,
    ParsedOrderWriteResponse,
    parse_order_mutation_response,
)
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, SafetyConfig, SafetyKernel
from saxo_bank_mcp.safety_audit import audit_mode, is_inside_repo
from saxo_bank_mcp.safety_state import get_preview

ORDER_WRITE_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "LIVE write readiness",
    "real-money approval",
    "future account suitability after readback time",
    "x-request-id response echo",
    "whether delete-by-instrument matched an existing order when Saxo returns empty success",
)
READBACK_PORT_ORDERS_PATH: Final = "/port/v1/orders"
READBACK_TRADE_MESSAGES_PATH: Final = "/trade/v1/messages"
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


async def execute_sim_order_write(  # noqa: C901, PLR0911
    write_class: OrderWriteClass,
    preview_token: str,
    approval_factor: str | None,
) -> ToolResult:
    spec = ORDER_WRITE_SPECS[write_class]
    live_refusal = _live_write_refusal_if_requested(spec)
    if live_refusal is not None:
        return _tool_result(live_refusal)
    if not preview_token.strip():
        return _tool_result(_denied(spec, "preview_token_missing"))
    if approval_factor is None or not approval_factor.strip():
        commit = SafetyKernel().commit_preview(preview_token, approval_factor=None)
        return _tool_result(_commit_denied(spec, commit))
    if not secrets.compare_digest(approval_factor, TEST_APPROVAL_FACTOR):
        commit = SafetyKernel().commit_preview(preview_token, approval_factor=approval_factor)
        return _tool_result(_commit_denied(spec, commit))

    stored = get_preview(preview_token)
    if stored is None:
        return _tool_result(_denied(spec, "preview_token_invalid"))
    if stored.request.operation_id != spec.operation_id:
        return _tool_result(_operation_mismatch(spec, stored.request.operation_id))
    coherence_reasons = request_body_coherence_reasons(spec, stored.request)
    if coherence_reasons:
        return _tool_result(_body_mismatch(spec, coherence_reasons))

    token = _cached_token(spec)
    if isinstance(token, dict):
        return _tool_result(token)

    commit = SafetyKernel().commit_preview(preview_token, approval_factor=approval_factor)
    if commit.get("status") != "approved_for_simulation":
        return _tool_result(_commit_denied(spec, commit))

    request_id = str(uuid.uuid4())
    request_body = stored.request.request_body
    route_or_denial = _resolved_route(spec, request_body)
    if isinstance(route_or_denial, dict):
        return _tool_result(route_or_denial)

    response = await _send_order_write(
        spec=spec,
        token=token,
        request_id=request_id,
        route=route_or_denial,
        request_body=request_body,
    )
    if isinstance(response, dict):
        if response.get("status") == "network_error":
            readback = await _readback(token, request_body)
            response.update(
                {
                    **readback,
                    **_indeterminate_flags(spec),
                    "mutation_may_have_occurred": True,
                    "retry_unsafe": True,
                    "committed_before_network_result": True,
                },
            )
        return _tool_result(response)

    response_body = _response_body(response)
    audit_path = _audit_raw_response(spec, response.status_code, response_body)
    parsed = parse_order_mutation_response(
        _object_payload(response_body),
        http_status=response.status_code,
    )
    readback = await _readback(token, request_body)
    status = _execution_status(spec, parsed)
    mutation_may_have_occurred = parsed.outcome in {
        "success",
        "partial_success",
        "unknown_state",
    }
    retry_unsafe = parsed.needs_readback
    raw_audit_path_inside_repo = False if audit_path is None else is_inside_repo(audit_path)
    return _tool_result(
        {
            "status": status,
            "tool_name": spec.tool_name,
            "write_class": spec.write_class,
            "operation_id": spec.operation_id,
            "endpoint_path": spec.endpoint_path,
            "environment": "SIM",
            "fastmcp_called": True,
            "network_call_made": True,
            "live_write": False,
            "preview_token_redacted": True,
            "approval_factor_mode": "test_only_sim",
            "x_request_id_present": bool(request_id),
            "x_request_id_generated": bool(request_id),
            "x_request_id_response_echo_verified": False,
            "order_result_parsed": True,
            "http_status": response.status_code,
            "parsed_response": parsed.to_json_value(),
            "port_orders_readback": readback["port_orders_readback"],
            "trade_messages_readback": readback["trade_messages_readback"],
            "readback_required": parsed.needs_readback,
            "mutation_may_have_occurred": mutation_may_have_occurred,
            "retry_unsafe": retry_unsafe,
            "raw_audit_path": str(audit_path) if audit_path is not None else "",
            "raw_audit_path_inside_repo": raw_audit_path_inside_repo,
            "audit_mode": None if audit_path is None else audit_mode(audit_path),
            "cleanup_attempted": False,
            "cleanup_status": "not_required_by_executor",
            "order_placed": _mutation_flag(spec, parsed.outcome, ("place", "multileg-place")),
            "order_modified": _mutation_flag(spec, parsed.outcome, ("modify", "multileg-modify")),
            "order_cancelled": _order_cancelled_flag(spec, parsed),
            "mutation_content_verified": _mutation_content_verified(spec, parsed),
            "does_not_verify": list(ORDER_WRITE_DOES_NOT_VERIFY),
        },
    )


def _tool_result(payload: Mapping[str, JsonValue]) -> ToolResult:
    return ToolResult(
        content=_diagnostic_text(payload),
        structured_content=dict(payload),
        is_error=payload.get("status") != "completed",
    )


def _diagnostic_text(payload: Mapping[str, JsonValue]) -> str:
    status = str(payload.get("status", "unknown"))
    tool_name = str(payload.get("tool_name", "saxo_order_tool"))
    reason = payload.get("denial_reason", payload.get("reason", ""))
    if reason:
        return f"{tool_name}: {status}; reason={reason}; no completed mutation claimed"
    return f"{tool_name}: {status}; completed mutation requires real SIM proof"


def _cached_token(spec: OrderWriteSpec) -> SaxoTokenSet | JsonObject:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return _auth_required(spec, error.code)
    cache_check = cached_token_for_tool(spec.tool_name, settings.cache_path)
    match cache_check:
        case CachedTokenReady(token=token):
            return token
        case CachedTokenBlocked(result=result):
            reason = result.get("reason", "token_cache_missing")
            return _auth_required(spec, str(reason))


def _auth_required(spec: OrderWriteSpec, reason: str) -> JsonObject:
    return {
        "status": "auth_required",
        "tool_name": spec.tool_name,
        "write_class": spec.write_class,
        "write_class_status": "incomplete",
        "environment": "SIM",
        "reason": reason,
        "next_action": auth_next_action(reason),
        "fastmcp_called": True,
        "network_call_made": False,
        "live_write": False,
        "preview_token_redacted": True,
        "approval_factor_mode": "test_only_sim",
        "order_placed": False,
        "order_modified": False,
        "order_cancelled": False,
        "order_or_subscription_created": False,
        "port_orders_readback": False,
        "trade_messages_readback": False,
        "mutation_may_have_occurred": False,
        "retry_unsafe": False,
    }


def _live_write_refusal_if_requested(spec: OrderWriteSpec) -> JsonObject | None:
    match SaxoRuntimeConfig.from_env().requested_environment:
        case SaxoEnvironment.LIVE:
            return live_write_refusal_payload(
                tool_name=spec.tool_name,
                write_class=spec.write_class,
                operation_id=spec.operation_id,
            )
        case SaxoEnvironment.SIM:
            return None


def _commit_denied(spec: OrderWriteSpec, commit: Mapping[str, object]) -> JsonObject:
    return {
        **_denied(spec, str(commit.get("denial_reason", "commit_denied"))),
        "request_fingerprint": str(commit.get("request_fingerprint", "")),
        "audit_path": str(commit.get("audit_path", "")),
        "audit_path_inside_repo": bool(commit.get("audit_path_inside_repo", False)),
        "approval_factor_mode": str(commit.get("approval_factor_mode", "missing")),
    }


def _operation_mismatch(spec: OrderWriteSpec, actual_operation_id: str) -> JsonObject:
    return {
        **_denied(spec, "preview_operation_mismatch"),
        "expected_operation_id": spec.operation_id,
        "actual_operation_id": actual_operation_id,
    }


def _body_mismatch(spec: OrderWriteSpec, reasons: tuple[str, ...]) -> JsonObject:
    return {
        **_denied(spec, "request_body_preview_mismatch"),
        "denial_reasons": list(reasons),
    }


def _denied(spec: OrderWriteSpec, reason: str) -> JsonObject:
    return {
        "status": "denied",
        "tool_name": spec.tool_name,
        "write_class": spec.write_class,
        "operation_id": spec.operation_id,
        "environment": "SIM",
        "denial_reason": reason,
        "fastmcp_called": True,
        "network_call_made": False,
        "live_write": False,
        "preview_token_redacted": True,
        "order_placed": False,
        "order_modified": False,
        "order_cancelled": False,
        "order_or_subscription_created": False,
        "port_orders_readback": False,
        "trade_messages_readback": False,
        "mutation_may_have_occurred": False,
        "retry_unsafe": False,
    }


def _mutation_flag(
    spec: OrderWriteSpec,
    outcome: OrderWriteOutcome,
    matching_classes: tuple[OrderWriteClass, ...],
) -> bool | None:
    if spec.write_class not in matching_classes:
        return False
    match outcome:
        case "success":
            return True
        case "partial_success" | "unknown_state":
            return None
        case _:
            return False


def _execution_status(spec: OrderWriteSpec, parsed: ParsedOrderWriteResponse) -> str:
    if (
        spec.write_class == "cancel-by-instrument"
        and parsed.outcome == "success"
        and not parsed.order_ids
    ):
        return "completed_unverified"
    return "completed" if parsed.outcome == "success" else parsed.outcome


def _order_cancelled_flag(
    spec: OrderWriteSpec,
    parsed: ParsedOrderWriteResponse,
) -> bool | None:
    if spec.write_class not in {"cancel", "cancel-by-instrument", "multileg-cancel"}:
        return False
    outcome = parsed.outcome
    if outcome in {"partial_success", "unknown_state"}:
        return None
    if outcome != "success":
        return False
    if spec.write_class == "cancel-by-instrument" and not parsed.order_ids:
        return None
    return True


def _mutation_content_verified(
    spec: OrderWriteSpec,
    parsed: ParsedOrderWriteResponse,
) -> bool:
    if parsed.outcome != "success":
        return False
    if spec.write_class == "cancel-by-instrument":
        return bool(parsed.order_ids)
    return True


def _indeterminate_flags(spec: OrderWriteSpec) -> JsonObject:
    return {
        "order_placed": _mutation_flag(spec, "unknown_state", ("place", "multileg-place")),
        "order_modified": _mutation_flag(spec, "unknown_state", ("modify", "multileg-modify")),
        "order_cancelled": _mutation_flag(
            spec,
            "unknown_state",
            ("cancel", "cancel-by-instrument", "multileg-cancel"),
        ),
    }


def _resolved_route(
    spec: OrderWriteSpec,
    request_body: Mapping[str, JsonValue],
) -> str | JsonObject:
    if spec.route_key is None:
        return spec.endpoint_path.lstrip("/")
    route_value = _string_or_int(request_body.get(spec.route_key))
    if route_value is None:
        return _denied(spec, f"{spec.route_key.lower()}_missing")
    return spec.endpoint_path.replace(f"{{{spec.route_key}}}", route_value).lstrip("/")


async def _send_order_write(
    *,
    spec: OrderWriteSpec,
    token: SaxoTokenSet,
    request_id: str,
    route: str,
    request_body: Mapping[str, JsonValue],
) -> httpx2.Response | JsonObject:
    params = _query_params(spec, request_body)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.access_token}",
        "Content-Type": "application/json",
        "x-request-id": request_id,
    }
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            match spec.method:
                case "POST":
                    return await client.post(route, json=dict(request_body), headers=headers)
                case "PATCH":
                    return await client.patch(route, json=dict(request_body), headers=headers)
                case "DELETE":
                    return await client.delete(route, params=params, headers=headers)
    except httpx2.HTTPError as error:
        return _network_error(spec, type(error).__name__)


async def _readback(token: SaxoTokenSet, request_body: Mapping[str, JsonValue]) -> JsonObject:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token.access_token}"}
    account_key = _string_or_int(request_body.get("AccountKey"))
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            port = await client.get(
                READBACK_PORT_ORDERS_PATH.lstrip("/"),
                params={} if account_key is None else {"AccountKey": account_key},
                headers=headers,
            )
            messages = await client.get(READBACK_TRADE_MESSAGES_PATH.lstrip("/"), headers=headers)
    except httpx2.HTTPError:
        return {"port_orders_readback": False, "trade_messages_readback": False}
    return {
        "port_orders_readback": HTTP_SUCCESS_MIN <= port.status_code < HTTP_SUCCESS_MAX,
        "trade_messages_readback": (
            HTTP_SUCCESS_MIN <= messages.status_code < HTTP_SUCCESS_MAX
        ),
    }


def _network_error(spec: OrderWriteSpec, detail: str) -> JsonObject:
    return {
        **_denied(spec, "network_error"),
        "status": "network_error",
        "network_call_made": True,
        "network_error_type": detail,
        "network_error_message_redacted": redact_text(detail),
    }


def _query_params(
    spec: OrderWriteSpec,
    request_body: Mapping[str, JsonValue],
) -> dict[str, str]:
    params: dict[str, str] = {}
    for key in spec.query_keys:
        value = _string_or_int(request_body.get(key))
        if value is not None:
            params[key] = value
    return params


def _response_body(response: httpx2.Response) -> JsonValue:
    if not response.content:
        return {}
    try:
        redacted = redact_json(response.json())
    except ValueError:
        return redact_text(response.text)
    return redacted


def _object_payload(value: JsonValue) -> JsonObject:
    if isinstance(value, Mapping):
        return JSON_OBJECT_ADAPTER.validate_python(value)
    return {}


def _audit_raw_response(
    spec: OrderWriteSpec,
    http_status: int,
    response_body: JsonValue,
) -> Path | None:
    try:
        return append_audit_event(
            SafetyConfig.from_env().audit_dir,
            {
                "event": "order_write_raw_response",
                "tool_name": spec.tool_name,
                "write_class": spec.write_class,
                "http_status": http_status,
                "response_body": response_body,
            },
        )
    except (AuditPathError, OSError):
        return None


def _string_or_int(value: JsonValue | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None
