from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Annotated, Final, cast

import httpx2
from fastmcp.tools import ToolResult
from pydantic import Field

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, redact_text
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    SIM_ENDPOINTS,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, SafetyKernel, WritePreviewRequest
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

ORDER_PREVIEW_TOOL_DESCRIPTION: Final = (
    "Runs a Saxo trade pre-check or evaluates a supplied redacted pre-check fixture, then creates "
    "a local preview token only when account-currency risk and disclaimer state are known. It "
    "does not place, modify, or cancel orders."
)
MULTILEG_DEFAULTS_TOOL_DESCRIPTION: Final = (
    "Fetches SIM multi-leg order defaults from Saxo when a token cache is available. It does not "
    "create orders or prove order readiness."
)
DISCLAIMER_LOOKUP_TOOL_DESCRIPTION: Final = (
    "Fetches SIM disclaimer details for tokens returned by order pre-check. It does not accept or "
    "submit disclaimer responses."
)
DISCLAIMER_RESPONSE_TOOL_DESCRIPTION: Final = (
    "Submits a SIM disclaimer response only after an explicit approval factor. It never places an "
    "order and LIVE writes remain disabled."
)
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300


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
    if source_precheck is None:
        token_or_result = _cached_token("saxo_create_order_preview")
        if isinstance(token_or_result, ToolResult):
            return token_or_result
        fetched = await _post_saxo_json(
            endpoint,
            order_body,
            token_or_result,
            tool_name="saxo_create_order_preview",
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
        Field(description="Separate approval factor; SIM tests use a test-only factor"),
    ] = None,
) -> ToolResult:
    missing = [
        name
        for name, value in (
            ("disclaimer_context", disclaimer_context),
            ("disclaimer_token", disclaimer_token),
            ("response_type", response_type),
            ("approval_factor", approval_factor),
        )
        if value is None or not value.strip()
    ]
    if missing:
        return _tool_result(
            _denied(
                "saxo_register_disclaimer_response",
                [f"{name}_missing" for name in missing],
            ),
        )
    if approval_factor is None or not secrets.compare_digest(
        approval_factor,
        TEST_APPROVAL_FACTOR,
    ):
        return _tool_result(
            _denied("saxo_register_disclaimer_response", ["approval_factor_invalid"]),
        )
    token_or_result = _cached_token("saxo_register_disclaimer_response")
    if isinstance(token_or_result, ToolResult):
        return token_or_result
    body: dict[str, JsonValue] = {
        "DisclaimerContext": disclaimer_context,
        "DisclaimerToken": disclaimer_token,
        "ResponseType": response_type,
    }
    if user_input is not None:
        body["UserInput"] = user_input
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
) -> JsonObject | ToolResult:
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
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
