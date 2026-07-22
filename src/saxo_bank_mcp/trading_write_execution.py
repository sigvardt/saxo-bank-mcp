from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from urllib.parse import quote

import httpx2
from fastmcp.tools import ToolResult
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_json_submitted_values, redact_text
from saxo_bank_mcp.audit import AuditPathError, append_audit_event
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    SaxoEnvironment,
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_cached_token_for_tool,
    resolve_live_read_settings,
)
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.safety_models import SafetyConfig
from saxo_bank_mcp.trading_write_registry import TradingWriteSpec, trading_write_spec
from saxo_bank_mcp.trading_write_safety import trading_write_safety_errors
from saxo_bank_mcp.trading_write_state import (
    PreparedTradingWrite,
    TradingWriteRequest,
    consume_trading_write_preview,
    create_trading_write_preview,
    get_trading_write_preview,
    trading_write_preview_consumed,
)

type JsonObject = dict[str, JsonValue]
type QueryScalar = str | int | float | bool
type SaxoResponseErrorState = Literal["none", "failed", "partial_success"]

JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300
HTTP_ACCEPTED: Final = 202
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_CONFLICT: Final = 409
HTTP_RATE_LIMITED: Final = 429
MAX_PATH_PARAMETER_LENGTH: Final = 200


@dataclass(frozen=True, slots=True)
class ExecutionAccess:
    environment: SaxoEnvironment
    rest_base_url: str
    token: SaxoTokenSet


def prepare_trading_write(request: TradingWriteRequest) -> ToolResult:
    spec = trading_write_spec(request.operation_id)
    if spec is None:
        return _result(_denied(request.operation_id, "unregistered_trading_write"))
    if spec.specialized_tool is not None:
        return _result(
            {
                **_denied(request.operation_id, "specialized_order_flow_required"),
                "next_tool": "saxo_create_order_preview",
                "execution_tool": spec.specialized_tool,
            },
        )
    return prepare_registered_trading_write(request, spec)


def prepare_registered_trading_write(
    request: TradingWriteRequest,
    spec: TradingWriteSpec,
    *,
    reported_tool_name: str = "saxo_prepare_trading_write",
) -> ToolResult:
    errors = _validation_errors(request, spec.path_parameter_names, spec.query_parameter_names)
    if errors:
        return _result(
            {
                **_denied(
                    request.operation_id,
                    "invalid_request",
                    tool_name=reported_tool_name,
                ),
                "validation_errors": list(errors),
            },
        )
    resolved_path = _resolve_path(spec.path_template, request.path_parameters)
    runtime = SaxoRuntimeConfig.from_env()
    safety = SafetyConfig.from_env()
    safety_errors = trading_write_safety_errors(
        request,
        spec.risk,
        runtime.requested_environment,
        safety,
    )
    if safety_errors:
        return _result(
            {
                **_denied(
                    request.operation_id,
                    safety_errors[0],
                    tool_name=reported_tool_name,
                ),
                "denial_reasons": list(safety_errors),
            },
        )

    token, prepared = create_trading_write_preview(
        request,
        spec,
        runtime.requested_environment,
        resolved_path,
    )
    audit_path = _audit(
        safety,
        {
            "event": "trading_write_preview_created",
            "environment": prepared.environment.value,
            "operation_id": prepared.spec.operation_id,
            "request_fingerprint": prepared.request_fingerprint,
            "preview_token_fingerprint": prepared.preview_token_fingerprint,
        },
    )
    if audit_path is None:
        return _result(
            _denied(
                request.operation_id,
                "audit_write_failed",
                tool_name=reported_tool_name,
            ),
        )
    payload: JsonObject = {
        "status": "preview_created",
        "tool_name": reported_tool_name,
        "operation_id": prepared.spec.operation_id,
        "environment": prepared.environment.value,
        "risk_class": prepared.spec.risk,
        "request_fingerprint": prepared.request_fingerprint,
        "preview_token": token,
        "preview_token_fingerprint": prepared.preview_token_fingerprint,
        "preview_token_expires_at": prepared.expires_at.isoformat(),
        "preview_token_redacted_in_future_results": True,
        "approval_required": prepared.expected_approval_statement is not None,
        "approval_mode": (
            "one_exact_action_chat_approval"
            if prepared.expected_approval_statement is not None
            else "autonomous_sim"
        ),
        "network_call_made": False,
        "live_write": False,
        "audit_path_recorded": True,
        "next_action": (
            "Ask the human to send the exact approval_prompt in the agent chat, then pass that "
            "unchanged to saxo_execute_trading_write."
            if prepared.expected_approval_statement is not None
            else (
                "Call saxo_execute_trading_write with the preview token; "
                "SIM needs no human approval."
            )
        ),
    }
    if prepared.expected_approval_statement is not None:
        payload["approval_prompt"] = prepared.expected_approval_statement
        payload["approval_summary"] = {
            "method": prepared.spec.method,
            "operation_id": prepared.spec.operation_id,
            "path_parameter_names": list(prepared.spec.path_parameter_names),
            "path_parameter_values_redacted": True,
            "query_parameters": redact_json(prepared.request.query_parameters),
            "request_body": redact_json(prepared.request.request_body),
            "risk_class": prepared.spec.risk,
            "service": prepared.spec.service,
        }
    return _result(payload)


async def execute_trading_write(
    preview_token: str,
    approval_statement: str | None,
) -> ToolResult:
    if not preview_token.strip():
        return _result(_denied("", "preview_token_missing"))
    prepared = get_trading_write_preview(preview_token)
    if prepared is None:
        return _result(_denied("", "preview_token_invalid"))
    denial = _execution_denial(prepared, approval_statement)
    if denial is not None:
        return _result(_denied(prepared.spec.operation_id, denial))

    access_or_result = _execution_access(prepared)
    if isinstance(access_or_result, ToolResult):
        return access_or_result
    consume_trading_write_preview(prepared)
    request_id = str(uuid.uuid4())
    try:
        async with create_async_client(
            base_url=access_or_result.rest_base_url,
            retries=0,
        ) as client:
            response = await client.request(
                prepared.spec.method,
                prepared.resolved_path.lstrip("/"),
                params=_query_params(prepared.request.query_parameters),
                json=prepared.request.request_body or None,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_or_result.token.access_token}",
                    "x-request-id": request_id,
                },
            )
    except httpx2.HTTPError as error:
        return _result(
            {
                **_base_execution_payload(prepared),
                "status": "unknown_state",
                "reason": "network_result_unknown",
                "network_call_made": True,
                "mutation_may_have_occurred": True,
                "retry_unsafe": True,
                "error_detail": redact_text(str(error)),
                "x_request_id_present": True,
            },
        )

    payload = _response_payload(prepared, response, request_id)
    _audit(
        SafetyConfig.from_env(),
        {
            "event": "trading_write_executed",
            "environment": prepared.environment.value,
            "operation_id": prepared.spec.operation_id,
            "request_fingerprint": prepared.request_fingerprint,
            "http_status": response.status_code,
            "status": str(payload["status"]),
        },
    )
    return _result(payload)


def _execution_denial(
    prepared: PreparedTradingWrite,
    approval_statement: str | None,
) -> str | None:
    reasons: list[str] = []
    if prepared.expires_at <= datetime.now(UTC):
        reasons.append("preview_token_expired")
    if trading_write_preview_consumed(prepared):
        reasons.append("preview_already_consumed")
    current_environment = SaxoRuntimeConfig.from_env().requested_environment
    if current_environment != prepared.environment:
        reasons.append("preview_environment_changed")
    reasons.extend(
        trading_write_safety_errors(
            prepared.request,
            prepared.spec.risk,
            current_environment,
            SafetyConfig.from_env(),
        ),
    )
    expected = prepared.expected_approval_statement
    if expected is not None:
        if approval_statement is None or not approval_statement.strip():
            reasons.append("chat_approval_missing")
        elif not secrets.compare_digest(approval_statement, expected):
            reasons.append("chat_approval_mismatch")
    return reasons[0] if reasons else None


def _execution_access(prepared: PreparedTradingWrite) -> ExecutionAccess | ToolResult:
    match prepared.environment:
        case SaxoEnvironment.SIM:
            try:
                settings = resolve_sim_auth_settings(require_redirect=False)
            except SimAuthSettingsError as error:
                return _auth_required(prepared, error.code)
            cached = cached_token_for_tool(
                "saxo_execute_trading_write",
                settings.cache_path,
            )
            match cached:
                case CachedTokenReady(token=token):
                    return ExecutionAccess(prepared.environment, settings.rest_base_url, token)
                case CachedTokenBlocked(result=result):
                    reason = str(result.get("reason", "token_cache_missing"))
                    return _auth_required(prepared, reason)
        case SaxoEnvironment.LIVE:
            try:
                settings = resolve_live_read_settings()
            except LiveReadSettingsError as error:
                return _auth_required(prepared, error.code)
            token_or_result = live_cached_token_for_tool(
                "saxo_execute_trading_write",
                settings.cache_path,
            )
            if isinstance(token_or_result, dict):
                reason = str(token_or_result.get("reason", "token_cache_missing"))
                return _auth_required(prepared, reason)
            return ExecutionAccess(prepared.environment, settings.rest_base_url, token_or_result)


def _validation_errors(
    request: TradingWriteRequest,
    expected_path_names: tuple[str, ...],
    allowed_query_names: tuple[str, ...],
) -> tuple[str, ...]:
    errors = [
        *_path_validation_errors(request.path_parameters, expected_path_names),
        *_query_validation_errors(request, allowed_query_names),
    ]
    return tuple(dict.fromkeys(errors))


def _path_validation_errors(
    path_parameters: Mapping[str, str],
    expected_names: tuple[str, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    supplied_names = set(path_parameters)
    errors.extend(
        f"path_parameters.{name}"
        for name in expected_names
        if name not in supplied_names
    )
    errors.extend(
        f"path_parameters.{name}"
        for name in sorted(supplied_names.difference(expected_names))
    )
    for name, value in path_parameters.items():
        if not _safe_path_value(value):
            errors.append(f"path_parameters.{name}")
    return tuple(errors)


def _query_validation_errors(
    request: TradingWriteRequest,
    allowed_names: tuple[str, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    for name, value in request.query_parameters.items():
        if name not in allowed_names or not isinstance(value, str | int | float | bool):
            errors.append(f"query_parameters.{name}")
    spec = trading_write_spec(request.operation_id)
    if spec is not None:
        errors.extend(
            f"query_parameters.{name}"
            for name in spec.required_query_parameter_names
            if name not in request.query_parameters
        )
    return tuple(errors)


def _resolve_path(template: str, values: Mapping[str, str]) -> str:
    resolved = template
    for name, value in values.items():
        resolved = resolved.replace("{" + name + "}", quote(value, safe=",-_.~="))
    return resolved


def _safe_path_value(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "%" not in value
        and len(value) <= MAX_PATH_PARAMETER_LENGTH
        and all(character.isprintable() for character in value)
    )


def _query_params(values: Mapping[str, JsonValue]) -> dict[str, QueryScalar] | None:
    params = {
        key: value
        for key, value in values.items()
        if isinstance(value, str | int | float | bool)
    }
    return params or None


def _response_payload(
    prepared: PreparedTradingWrite,
    response: httpx2.Response,
    request_id: str,
) -> JsonObject:
    response_body = _response_body(response)
    error_state = _saxo_error_state(response_body)
    status = _response_status(response.status_code, error_state)
    mutation_may_have_occurred = status in {
        "completed",
        "partial_success",
        "unknown_state",
    }
    retry_unsafe = response.status_code == HTTP_ACCEPTED or (
        prepared.spec.risk == "money_moving"
        and response.status_code not in {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN, HTTP_RATE_LIMITED}
    )
    return {
        **_base_execution_payload(prepared),
        "status": status,
        "http_status": response.status_code,
        "network_call_made": True,
        "mutation_may_have_occurred": mutation_may_have_occurred,
        "retry_unsafe": retry_unsafe,
        "saxo_error_present": error_state != "none",
        "x_request_id_present": bool(request_id),
        "response": redact_json_submitted_values(
            response_body,
            _submitted_string_values(prepared.request),
        ),
    }


def _base_execution_payload(prepared: PreparedTradingWrite) -> JsonObject:
    return {
        "tool_name": "saxo_execute_trading_write",
        "operation_id": prepared.spec.operation_id,
        "environment": prepared.environment.value,
        "risk_class": prepared.spec.risk,
        "request_fingerprint": prepared.request_fingerprint,
        "preview_token_redacted": True,
        "approval_mode": (
            "one_exact_action_chat_approval"
            if prepared.environment == SaxoEnvironment.LIVE
            else "autonomous_sim"
        ),
        "live_write": prepared.environment == SaxoEnvironment.LIVE,
        "live_write_called": prepared.environment == SaxoEnvironment.LIVE,
        "cleanup_rule": prepared.spec.cleanup_rule,
    }


def _response_status(
    http_status: int,
    error_state: SaxoResponseErrorState,
) -> str:
    if http_status == HTTP_ACCEPTED:
        status = "unknown_state"
    elif HTTP_SUCCESS_MIN <= http_status < HTTP_SUCCESS_MAX:
        if error_state == "failed":
            status = "failed"
        elif error_state == "partial_success":
            status = "partial_success"
        else:
            status = "completed"
    elif http_status == HTTP_RATE_LIMITED:
        status = "rate_limited"
    elif http_status == HTTP_CONFLICT:
        status = "duplicate_or_conflict"
    else:
        status = "failed"
    return status


def _saxo_error_state(value: JsonValue) -> SaxoResponseErrorState:
    if isinstance(value, list):
        child_states = [
            _saxo_error_state(child)
            for child in value
            if isinstance(child, Mapping | list)
        ]
        if "partial_success" in child_states:
            state: SaxoResponseErrorState = "partial_success"
        elif "failed" in child_states and "none" in child_states:
            state = "partial_success"
        else:
            state = "failed" if "failed" in child_states else "none"
    elif isinstance(value, Mapping):
        error_info = value.get("ErrorInfo")
        child_states = [
            _saxo_error_state(child)
            for key, child in value.items()
            if key != "ErrorInfo" and isinstance(child, Mapping | list)
        ]
        if _nonempty_error_info(error_info):
            state = "failed"
        elif "partial_success" in child_states:
            state = "partial_success"
        else:
            state = "failed" if "failed" in child_states else "none"
    else:
        state = "none"
    return state


def _nonempty_error_info(value: JsonValue | None) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | list):
        return bool(value)
    return True


def _response_body(response: httpx2.Response) -> JsonValue:
    if not response.content:
        return None
    try:
        return JSON_VALUE_ADAPTER.validate_python(response.json())
    except (ValueError, ValidationError):
        return redact_text(response.text)


def _submitted_string_values(request: TradingWriteRequest) -> tuple[str, ...]:
    values = [
        *request.path_parameters.values(),
        *(_strings_in_json(request.query_parameters)),
        *(_strings_in_json(request.request_body)),
    ]
    if request.account_key is not None:
        values.append(request.account_key)
    return tuple(dict.fromkeys(values))


def _strings_in_json(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(item for child in value.values() for item in _strings_in_json(child))
    if isinstance(value, list):
        return tuple(item for child in value for item in _strings_in_json(child))
    return ()


def _auth_required(prepared: PreparedTradingWrite, reason: str) -> ToolResult:
    return _result(
        {
            **_base_execution_payload(prepared),
            "status": "auth_required",
            "reason": reason,
            "network_call_made": False,
            "mutation_may_have_occurred": False,
            "retry_unsafe": False,
        },
    )


def _denied(
    operation_id: str,
    reason: str,
    *,
    tool_name: str = "saxo_prepare_trading_write",
) -> JsonObject:
    return {
        "status": "denied",
        "tool_name": tool_name,
        "operation_id": operation_id,
        "denial_reason": reason,
        "network_call_made": False,
        "live_write": False,
        "live_write_called": False,
        "mutation_may_have_occurred": False,
        "retry_unsafe": False,
    }


def _audit(config: SafetyConfig, event: Mapping[str, JsonValue]) -> str | None:
    try:
        return str(append_audit_event(config.audit_dir, dict(event)))
    except (AuditPathError, OSError):
        return None


def _result(payload: Mapping[str, JsonValue]) -> ToolResult:
    status = str(payload.get("status", "failed"))
    operation = str(payload.get("operation_id", "registered trading write"))
    reason = str(payload.get("denial_reason", payload.get("reason", "")))
    text = f"{operation}: {status}"
    if reason:
        text = f"{text}; reason={reason}"
    return ToolResult(
        content=text,
        structured_content=dict(payload),
        is_error=status not in {"passed", "preview_created", "completed"},
    )
