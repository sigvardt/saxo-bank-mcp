from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final, cast

import httpx2
from fastmcp.tools import ToolResult
from pydantic import Field

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_text
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    SIM_ENDPOINTS,
    SaxoEnvironment,
    SaxoRuntimeConfig,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_cached_token_for_tool,
    live_read_auth_required,
    resolve_live_read_settings,
)
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.order_mutation_guards import multileg_body_safety_reasons
from saxo_bank_mcp.safety import SafetyKernel, WritePreviewRequest
from saxo_bank_mcp.safety_models import SafetyConfig
from saxo_bank_mcp.trade_preview import (
    DISCLAIMER_RESPONSE_ENDPOINT_PATH,
    TRADE_DOES_NOT_VERIFY,
    DisclaimerState,
    JsonObject,
    OrderKind,
    account_currency,
    account_currency_risk,
    disclaimer_blockers,
    disclaimer_context,
    disclaimer_tokens,
    operation_id_for_order_kind,
    order_account_key,
    order_instrument_uic,
    order_quantity,
    precheck_endpoint_for_order_kind,
)
from saxo_bank_mcp.trading_write_execution import prepare_registered_trading_write
from saxo_bank_mcp.trading_write_registry import TradingWriteSpec
from saxo_bank_mcp.trading_write_state import TradingWriteRequest

ORDER_PREVIEW_TOOL_DESCRIPTION: Final = (
    "Runs a Saxo trade pre-check in the configured environment, or evaluates a supplied redacted "
    "SIM fixture, then "
    "creates a local preview token only when account-currency risk and disclaimer state are known. "
    "LIVE fixtures are refused. A LIVE preview returns the one exact-action approval statement "
    "that must be sent by the human in agent chat. It does not place, modify, or cancel orders."
)
MULTILEG_DEFAULTS_TOOL_DESCRIPTION: Final = (
    "Fetches SIM multi-leg order defaults from Saxo when a token cache is available. It does not "
    "create orders or prove order readiness. It refuses before network when configured for LIVE."
)
DISCLAIMER_LOOKUP_TOOL_DESCRIPTION: Final = (
    "Fetches SIM disclaimer details for tokens returned by order pre-check. It does not accept or "
    "submit disclaimer responses. It refuses before network when configured for LIVE."
)
DISCLAIMER_RESPONSE_TOOL_DESCRIPTION: Final = (
    "Submits a SIM disclaimer response without human approval. In LIVE it creates a short-lived "
    "exact-request preview; one human must send the returned approval statement in agent chat, "
    "then the agent calls saxo_execute_trading_write. It never places an order by itself."
)
DISCLAIMER_WRITE_SPEC: Final = TradingWriteSpec(
    operation_id="post.dm.v2.disclaimers",
    method="POST",
    path_template=DISCLAIMER_RESPONSE_ENDPOINT_PATH,
    service="Disclaimer Management",
    documentation_url=(
        "https://www.developer.saxo/openapi/referencedocs/dm/v2/disclaimermanagement"
    ),
    cleanup_rule=None,
    risk="state_change",
    path_parameter_names=(),
    query_parameter_names=(),
    required_query_parameter_names=(),
    specialized_tool="saxo_register_disclaimer_response",
)
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300


@dataclass(frozen=True, slots=True)
class TradePrecheckAccess:
    environment: SaxoEnvironment
    rest_base_url: str
    token: SaxoTokenSet


async def saxo_create_order_preview(
    order_body: Annotated[
        dict[str, JsonValue],
        Field(description="Exact Saxo order body; account identifiers are never echoed raw"),
    ],
    order_kind: Annotated[OrderKind, Field(description="single or multileg order pre-check")] = (
        "single"
    ),
    precheck_response: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Optional redacted pre-check response fixture for no-secret testing"),
    ] = None,
    disclaimer_details: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Optional redacted dm/v2/disclaimers response"),
    ] = None,
    disclaimer_response_state: Annotated[
        DisclaimerState,
        Field(description="Current response state for required disclaimers"),
    ] = "unknown",
) -> ToolResult:
    endpoint = precheck_endpoint_for_order_kind(order_kind)
    network_call_made = False
    source_precheck = precheck_response
    environment = SaxoRuntimeConfig.from_env().requested_environment
    if environment == SaxoEnvironment.LIVE and source_precheck is not None:
        return _tool_result(
            _denied(
                "saxo_create_order_preview",
                ["live_precheck_fixture_forbidden"],
                order_kind=order_kind,
                precheck_endpoint=endpoint,
            ),
        )
    if environment == SaxoEnvironment.LIVE and order_body.get("ManualOrder") is not True:
        return _tool_result(
            _denied(
                "saxo_create_order_preview",
                ["live_manual_order_confirmation_required"],
                order_kind=order_kind,
                precheck_endpoint=endpoint,
            ),
        )
    if source_precheck is None:
        access_or_result = _precheck_access("saxo_create_order_preview", environment)
        if isinstance(access_or_result, ToolResult):
            return access_or_result
        fetched = await _post_saxo_json(
            endpoint,
            _precheck_body(order_body, environment),
            access_or_result.token,
            tool_name="saxo_create_order_preview",
            base_url=access_or_result.rest_base_url,
        )
        if isinstance(fetched, ToolResult):
            return fetched
        source_precheck = fetched
        network_call_made = True
    return _preview_result(
        order_body=order_body,
        order_kind=order_kind,
        precheck_response=source_precheck,
        disclaimer_details=disclaimer_details,
        disclaimer_response_state=disclaimer_response_state,
        network_call_made=network_call_made,
    )


async def saxo_get_multileg_order_defaults(
    account_key: Annotated[str, Field(description="Saxo account key; never echoed raw")],
    option_root_id: Annotated[int, Field(gt=0, description="Option root identifier")],
    options_strategy_type: Annotated[str, Field(description="Saxo options strategy type")],
) -> ToolResult:
    token_or_result = _cached_token("saxo_get_multileg_order_defaults")
    if isinstance(token_or_result, ToolResult):
        return token_or_result
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            response = await client.get(
                "trade/v2/orders/multileg/defaults",
                params={
                    "AccountKey": account_key,
                    "OptionRootId": option_root_id,
                    "OptionsStrategyType": options_strategy_type,
                },
                headers=_headers(token_or_result),
            )
    except httpx2.HTTPError as error:
        return _error_result("saxo_get_multileg_order_defaults", "network_error", str(error))
    payload = _http_payload("saxo_get_multileg_order_defaults", response)
    payload["account_key_redacted"] = True
    payload["order_placed"] = False
    return _tool_result(payload)


async def saxo_get_required_disclaimers(
    disclaimer_tokens: Annotated[
        list[str],
        Field(description="Disclaimer tokens returned by Saxo order pre-check"),
    ],
) -> ToolResult:
    tokens = [token for token in disclaimer_tokens if token.strip()]
    if not tokens:
        return _tool_result(_denied("saxo_get_required_disclaimers", ["disclaimer_tokens_missing"]))
    token_or_result = _cached_token("saxo_get_required_disclaimers")
    if isinstance(token_or_result, ToolResult):
        return token_or_result
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            response = await client.get(
                "dm/v2/disclaimers",
                params={"DisclaimerTokens": ",".join(tokens)},
                headers=_headers(token_or_result),
            )
    except httpx2.HTTPError as error:
        return _error_result("saxo_get_required_disclaimers", "network_error", str(error))
    payload = _http_payload("saxo_get_required_disclaimers", response)
    payload["disclaimer_tokens_count"] = len(tokens)
    payload["order_placed"] = False
    return _tool_result(payload)


async def saxo_register_disclaimer_response(
    disclaimer_context: Annotated[str, Field(description="DisclaimerContext from pre-check")],
    disclaimer_token: Annotated[str, Field(description="DisclaimerToken from pre-check")],
    response_type: Annotated[str, Field(description="Saxo disclaimer ResponseType")],
    user_input: Annotated[str | None, Field(description="UserInput if required")] = None,
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    _ = approval_factor
    missing = [
        name
        for name, value in (
            ("disclaimer_context", disclaimer_context),
            ("disclaimer_token", disclaimer_token),
            ("response_type", response_type),
        )
        if not value.strip()
    ]
    if missing:
        return _tool_result(
            _denied(
                "saxo_register_disclaimer_response",
                [f"{name}_missing" for name in missing],
            ),
        )
    body: dict[str, JsonValue] = {
        "DisclaimerContext": disclaimer_context,
        "DisclaimerToken": disclaimer_token,
        "ResponseType": response_type,
    }
    if user_input is not None:
        body["UserInput"] = user_input
    if SaxoRuntimeConfig.from_env().requested_environment == SaxoEnvironment.LIVE:
        prepared = prepare_registered_trading_write(
            TradingWriteRequest(
                operation_id=DISCLAIMER_WRITE_SPEC.operation_id,
                request_body=body,
            ),
            DISCLAIMER_WRITE_SPEC,
            reported_tool_name="saxo_register_disclaimer_response",
        )
        payload = dict(prepared.structured_content or {})
        payload.update(
            {
                "disclaimer_response_submitted": False,
                "execution_tool": "saxo_execute_trading_write",
                "order_placed": False,
                "live_write": False,
            },
        )
        return _tool_result(payload)
    token_or_result = _cached_token("saxo_register_disclaimer_response")
    if isinstance(token_or_result, ToolResult):
        return token_or_result
    fetched = await _post_saxo_json(
        DISCLAIMER_RESPONSE_ENDPOINT_PATH,
        body,
        token_or_result,
        tool_name="saxo_register_disclaimer_response",
    )
    if isinstance(fetched, ToolResult):
        return fetched
    payload: dict[str, JsonValue] = {
        "status": "passed",
        "tool_name": "saxo_register_disclaimer_response",
        "response_endpoint_path": DISCLAIMER_RESPONSE_ENDPOINT_PATH,
        "network_call_made": True,
        "disclaimer_response_submitted": True,
        "order_placed": False,
        "live_write": False,
        "response": fetched,
        "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
    }
    return _tool_result(payload)


def _preview_result(  # noqa: PLR0913
    *,
    order_body: dict[str, JsonValue],
    order_kind: OrderKind,
    precheck_response: dict[str, JsonValue],
    disclaimer_details: dict[str, JsonValue] | None,
    disclaimer_response_state: DisclaimerState,
    network_call_made: bool,
) -> ToolResult:
    endpoint = precheck_endpoint_for_order_kind(order_kind)
    risk, risk_reasons, estimated_notional = account_currency_risk(precheck_response, order_body)
    disclaimer_reasons, details = disclaimer_blockers(
        precheck_response,
        disclaimer_details,
        disclaimer_response_state,
    )
    reasons = [
        *_order_input_reasons(order_body, precheck_response),
        *(
            multileg_body_safety_reasons(order_body, SafetyConfig.from_env())
            if order_kind == "multileg"
            else ()
        ),
        *risk_reasons,
        *disclaimer_reasons,
    ]
    if reasons:
        return _tool_result(
            _denied(
                "saxo_create_order_preview",
                reasons,
                order_kind=order_kind,
                precheck_endpoint=endpoint,
                network_call_made=network_call_made,
                precheck_response=precheck_response,
                disclaimer_details=details,
            ),
        )
    account_key = cast("str", order_account_key(order_body))
    instrument_uic = cast("int", order_instrument_uic(order_body))
    quantity = cast("float", order_quantity(order_body))
    currency = cast("str", account_currency(precheck_response))

    tokens = disclaimer_tokens(precheck_response)
    final_disclaimer_state = disclaimer_response_state
    if not tokens and final_disclaimer_state == "unknown":
        final_disclaimer_state = "none"

    preview = dict(
        SafetyKernel().create_preview(
            WritePreviewRequest(
                operation_id=operation_id_for_order_kind(order_kind),
                account_key=account_key,
                instrument_uic=instrument_uic,
                quantity=quantity,
                estimated_notional=estimated_notional,
                account_currency=currency,
                risk=risk,
                request_body=order_body,
            ),
        ),
    )
    preview.update(
        {
            "tool_name": "saxo_create_order_preview",
            "order_kind": order_kind,
            "precheck_endpoint": endpoint,
            "response_endpoint_path": DISCLAIMER_RESPONSE_ENDPOINT_PATH,
            "network_call_made": network_call_made,
            "fastmcp_called": True,
            "preview_created": preview.get("status") == "preview_created",
            "account_key_redacted": True,
            "order_placed": False,
            "order_modified": False,
            "order_cancelled": False,
            "live_write": False,
            "precheck_result": precheck_response.get("PreCheckResult", "unknown"),
            "disclaimer_tokens_count": len(tokens),
            "disclaimer_response_state": final_disclaimer_state,
            "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
        },
    )
    return _tool_result(cast("dict[str, JsonValue]", preview))


async def _post_saxo_json(
    path: str,
    body: dict[str, JsonValue],
    token: SaxoTokenSet,
    *,
    tool_name: str,
    base_url: str = SIM_ENDPOINTS.rest_base_url,
) -> JsonObject | ToolResult:
    try:
        async with create_async_client(base_url=base_url, retries=0) as client:
            response = await client.post(path.lstrip("/"), json=body, headers=_headers(token))
    except httpx2.HTTPError as error:
        return _error_result(tool_name, "network_error", str(error))
    payload = _http_payload(tool_name, response)
    if payload["status"] != "passed":
        return _tool_result(payload)
    raw = payload.get("response")
    return raw if isinstance(raw, dict) else {"raw_response": raw}


def _order_input_reasons(
    order_body: dict[str, JsonValue],
    precheck_response: dict[str, JsonValue],
) -> list[str]:
    reasons: list[str] = []
    if order_account_key(order_body) is None:
        reasons.append("account_key_missing")
    if order_instrument_uic(order_body) is None:
        reasons.append("instrument_uic_missing")
    if order_quantity(order_body) is None:
        reasons.append("quantity_missing")
    if account_currency(precheck_response) is None:
        reasons.append("account_currency_unknown")

    precheck_result = precheck_response.get("PreCheckResult")
    if precheck_result is None:
        reasons.append("precheck_result_unknown")
    elif precheck_result != "Ok":
        reasons.append("precheck_not_ok")

    return reasons


def _cached_token(tool_name: str) -> SaxoTokenSet | ToolResult:
    if SaxoRuntimeConfig.from_env().requested_environment == SaxoEnvironment.LIVE:
        return _sim_only_refusal(tool_name)
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return _auth_required(tool_name, error.code)
    cache_check = cached_token_for_tool(tool_name, settings.cache_path)
    match cache_check:
        case CachedTokenBlocked(result=result):
            return _auth_required(tool_name, str(result.get("reason", "token_missing")))
        case CachedTokenReady(token=token):
            return token
    raise AssertionError("unreachable cached token result")


def _precheck_access(
    tool_name: str,
    environment: SaxoEnvironment,
) -> TradePrecheckAccess | ToolResult:
    match environment:
        case SaxoEnvironment.SIM:
            token_or_result = _cached_token(tool_name)
            if isinstance(token_or_result, ToolResult):
                return token_or_result
            return TradePrecheckAccess(environment, SIM_ENDPOINTS.rest_base_url, token_or_result)
        case SaxoEnvironment.LIVE:
            try:
                settings = resolve_live_read_settings()
            except LiveReadSettingsError as error:
                return ToolResult(
                    structured_content=live_read_auth_required(tool_name, error.code),
                    is_error=True,
                )
            token_or_result = live_cached_token_for_tool(tool_name, settings.cache_path)
            if isinstance(token_or_result, dict):
                return ToolResult(
                    structured_content=token_or_result,
                    is_error=True,
                )
            return TradePrecheckAccess(environment, settings.rest_base_url, token_or_result)


def _precheck_body(
    order_body: dict[str, JsonValue],
    environment: SaxoEnvironment,
) -> dict[str, JsonValue]:
    if environment == SaxoEnvironment.SIM:
        return order_body
    body = dict(order_body)
    body["ManualOrder"] = False
    orders = body.get("Orders")
    if isinstance(orders, list):
        body["Orders"] = [
            {**row, "ManualOrder": False} if isinstance(row, dict) else row
            for row in orders
        ]
    return body


def _sim_only_refusal(tool_name: str) -> ToolResult:
    return _tool_result(
        {
            "status": "denied",
            "tool_name": tool_name,
            "environment": "SIM",
            "requested_environment": "LIVE",
            "reason": "sim_only_tool_in_live_environment",
            "network_call_made": False,
            "preview_created": False,
            "disclaimer_response_submitted": False,
            "order_placed": False,
            "order_modified": False,
            "order_cancelled": False,
            "live_write": False,
            "next_action": (
                "Use saxo_call_registered_endpoint for LIVE read checks, or switch "
                "SAXO_MCP_ENVIRONMENT back to SIM before using this SIM-only helper."
            ),
            "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
        },
    )


def _headers(token: SaxoTokenSet) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {token.access_token}"}


def _http_payload(tool_name: str, response: httpx2.Response) -> dict[str, JsonValue]:
    ok = HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX
    return {
        "status": "passed" if ok else "http_error",
        "tool_name": tool_name,
        "environment": "SIM",
        "http_status": response.status_code,
        "network_call_made": True,
        "order_placed": False,
        "order_modified": False,
        "order_cancelled": False,
        "live_write": False,
        "response": _response_body(response),
        "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
    }


def _response_body(response: httpx2.Response) -> JsonValue:
    if not response.content:
        return None
    if not _is_json_response(response):
        return redact_text(response.text)
    try:
        parsed = cast("JsonValue", response.json())
    except ValueError:
        return redact_text(response.text)
    return redact_json(parsed)


def _is_json_response(response: httpx2.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")


def _auth_required(tool_name: str, reason: str) -> ToolResult:
    return _tool_result(
        {
            "status": "auth_required",
            "tool_name": tool_name,
            "environment": "SIM",
            "reason": reason,
            "network_call_made": False,
            "preview_created": False,
            "disclaimer_response_submitted": False,
            "order_placed": False,
            "order_modified": False,
            "order_cancelled": False,
            "live_write": False,
            "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
        },
    )


def _error_result(tool_name: str, status: str, detail: str) -> ToolResult:
    return _tool_result(
        {
            "status": status,
            "tool_name": tool_name,
            "detail": redact_text(detail),
            "network_call_made": status == "network_error",
            "order_placed": False,
            "live_write": False,
            "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
        },
    )


def _denied(  # noqa: PLR0913
    tool_name: str,
    reasons: list[str],
    *,
    order_kind: OrderKind | None = None,
    precheck_endpoint: str | None = None,
    network_call_made: bool = False,
    precheck_response: Mapping[str, JsonValue] | None = None,
    disclaimer_details: list[JsonObject] | None = None,
) -> dict[str, JsonValue]:
    return {
        "status": "denied",
        "tool_name": tool_name,
        "order_kind": "" if order_kind is None else order_kind,
        "denial_reasons": reasons,
        "precheck_endpoint": "" if precheck_endpoint is None else precheck_endpoint,
        "response_endpoint_path": DISCLAIMER_RESPONSE_ENDPOINT_PATH,
        "network_call_made": network_call_made,
        "fastmcp_called": True,
        "preview_created": False,
        "account_key_redacted": True,
        "disclaimer_context_present": _has_disclaimer_context(precheck_response),
        "disclaimer_tokens_count": (
            0 if precheck_response is None else len(disclaimer_tokens(precheck_response))
        ),
        "disclaimer_details_sanitized": disclaimer_details is not None,
        "exact_disclaimer_content_present": bool(disclaimer_details),
        "disclaimer_details": [] if disclaimer_details is None else disclaimer_details,
        "disclaimer_response_submitted": False,
        "order_placed": False,
        "order_modified": False,
        "order_cancelled": False,
        "live_write": False,
        "does_not_verify": list(TRADE_DOES_NOT_VERIFY),
    }


def _has_disclaimer_context(precheck_response: Mapping[str, JsonValue] | None) -> bool:
    return False if precheck_response is None else disclaimer_context(precheck_response) is not None


def _tool_result(payload: dict[str, JsonValue]) -> ToolResult:
    status = payload.get("status")
    return ToolResult(
        structured_content=payload,
        is_error=status in {"auth_required", "denied", "http_error", "network_error"},
    )
