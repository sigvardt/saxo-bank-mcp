from __future__ import annotations

from typing import Final, Literal, TypedDict

import httpx2
from fastmcp.tools import ToolResult
from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_precheck_response_models import (
    contains_order_identifier,
    parse_precheck_response,
)
from saxo_bank_mcp.live_precheck_response_summary import summarize_precheck_response
from saxo_bank_mcp.live_retry import retry_after_seconds
from saxo_bank_mcp.saxo_http_error_info import validated_saxo_error_code
from saxo_bank_mcp.strict_json import StrictJsonError

LIVE_PRECHECK_TOOL_NAME: Final = "saxo_precheck_live_order"
LIVE_PRECHECK_ENDPOINT: Final = "/trade/v2/orders/precheck"
LIVE_PRECHECK_ACCESS_LEVEL: Final = "Personal:Read"
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300
HTTP_OK: Final = 200
HTTP_FORBIDDEN: Final = 403
HTTP_SERVER_ERROR_MIN: Final = 500
_HTTP_FAILURE_STATUSES: Final = {
    401: "authentication_required", HTTP_FORBIDDEN: "forbidden", 429: "rate_limited",
}
NON_PLACEMENT_TRADE_BLOCKERS: Final[tuple[str, ...]] = (
    "trade_readiness_not_assessed", "live_write_disabled", "human_approval_required",
)


class PrecheckRequestSummary(TypedDict):
    amount: float
    asset_type: str
    buy_sell: Literal["Buy", "Sell"]
    duration_type: Literal["DayOrder"]
    field_groups: list[str]
    manual_order: bool
    order_type: Literal["Market"]
    uic: int


def precheck_response_result(
    response: httpx2.Response,
    *,
    account_id: str,
    account_ref: str,
    request_summary: PrecheckRequestSummary,
) -> ToolResult:
    try:
        identifier_present = contains_order_identifier(response.content)
    except (StrictJsonError, ValidationError):
        return tool_result(
            {
                **_called_result("invalid_precheck_response"),
                "reason": "response_schema_invalid",
                "http_status": response.status_code,
                "account_id": account_id,
                "account_ref": account_ref,
                "precheck_request_accepted": False,
            },
            is_error=True,
        )

    if identifier_present:
        return tool_result(
            {
                **_called_result("unsafe_precheck_response"),
                "reason": "unexpected_order_identifier",
                "http_status": response.status_code,
                "order_identifier_present": True,
                "requires_order_readback": True,
                "account_id": account_id,
                "account_ref": account_ref,
                "precheck_request_accepted": False,
            },
            is_error=True,
        )

    try:
        parsed = parse_precheck_response(response.content)
    except (StrictJsonError, ValidationError):
        return tool_result(
            {
                **_called_result("invalid_precheck_response"),
                "reason": "response_schema_invalid",
                "http_status": response.status_code,
                "account_id": account_id,
                "account_ref": account_ref,
                "precheck_request_accepted": False,
            },
            is_error=True,
        )

    summary = summarize_precheck_response(parsed)
    accepted = (
        response.status_code == HTTP_OK
        and summary.root_result_ok
        and summary.all_results_ok
        and not summary.error_object_present
        and not summary.disclaimer_object_present
    )
    status = _precheck_status(
        accepted=accepted, error_code=summary.error_code,
        disclaimer_object_present=summary.disclaimer_object_present,
    )
    precheck_blockers = _precheck_blockers(
        accepted=accepted, error_code=summary.error_code, http_status=response.status_code,
        all_results_ok=summary.all_results_ok,
        disclaimer_object_present=summary.disclaimer_object_present,
    )
    return tool_result(
        {
            **_called_result(status),
            "http_status": response.status_code,
            "precheck_result": parsed.precheck_result,
            "error_code": summary.error_code,
            "estimated_cash_required": parsed.estimated_cash_required,
            "estimated_cash_required_currency": parsed.estimated_cash_required_currency,
            "estimated_total_cost_in_account_currency": (
                parsed.estimated_total_cost_in_account_currency
            ),
            "disclaimer_count": summary.disclaimer_count,
            "requires_disclaimer_review": summary.disclaimer_object_present,
            "root_result_explicitly_ok": summary.root_result_ok,
            "child_result_count": summary.child_result_count,
            "all_returned_results_explicitly_ok": summary.all_results_ok,
            "disclaimer_object_present": summary.disclaimer_object_present,
            "error_object_present": summary.error_object_present,
            "precheck_request_accepted": accepted,
            "precheck_blockers": precheck_blockers,
            "trade_blockers": [*precheck_blockers, *NON_PLACEMENT_TRADE_BLOCKERS],
            "account_id": account_id,
            "account_ref": account_ref,
            "request_summary": {
                "amount": request_summary["amount"],
                "asset_type": request_summary["asset_type"],
                "buy_sell": request_summary["buy_sell"],
                "duration_type": request_summary["duration_type"],
                "field_groups": request_summary["field_groups"],
                "manual_order": request_summary["manual_order"],
                "order_type": request_summary["order_type"],
                "uic": request_summary["uic"],
            },
        },
        is_error=not accepted,
    )


def http_failure_result(
    response: httpx2.Response,
    *,
    account_lookup_endpoint_called: bool,
    instrument_lookup_endpoint_called: bool = False,
    precheck_endpoint_called: bool,
) -> ToolResult:
    failure_stage = (
        "precheck"
        if precheck_endpoint_called
        else "instrument_lookup"
        if instrument_lookup_endpoint_called
        else "account_lookup"
    )
    if response.status_code >= HTTP_SERVER_ERROR_MIN:
        status = f"{failure_stage}_unavailable"
    else:
        status = _HTTP_FAILURE_STATUSES.get(response.status_code, "http_error")
    retry_seconds = retry_after_seconds(response.headers)
    return tool_result(
        {
            **common_result(status, network_call_made=True),
            "failure_stage": failure_stage,
            "http_status": response.status_code,
            "saxo_error_code": validated_saxo_error_code(response.content),
            "retry_after_seconds": retry_seconds,
            "retry_known": retry_seconds is not None,
            "automatic_retry_allowed": False,
            "automatic_relogin_allowed": False,
            "forbidden_reason": (
                "permission_or_security_block" if response.status_code == HTTP_FORBIDDEN else None
            ),
            "account_lookup_endpoint_called": account_lookup_endpoint_called,
            "instrument_lookup_endpoint_called": instrument_lookup_endpoint_called,
            "instrument_tradable": precheck_endpoint_called,
            "precheck_endpoint_called": precheck_endpoint_called,
            "precheck_request_accepted": False,
        },
        is_error=True,
    )


def common_result(status: str, *, network_call_made: bool) -> dict[str, JsonValue]:
    return {
        "status": status,
        "tool_name": LIVE_PRECHECK_TOOL_NAME,
        "environment": "LIVE",
        "endpoint_path": LIVE_PRECHECK_ENDPOINT,
        "access_level": LIVE_PRECHECK_ACCESS_LEVEL,
        "network_call_made": network_call_made,
        "trade_readiness": "not_assessed",
        "account_key_redacted": True,
        "account_refs_process_scoped": True,
        "account_lookup_endpoint_called": False,
        "instrument_lookup_endpoint_called": False,
        "instrument_tradable": False,
        "precheck_endpoint_called": False,
        "order_placement_endpoint_called": False,
        "order_change_endpoint_called": False,
        "order_cancel_endpoint_called": False,
        "disclaimer_response_endpoint_called": False,
        "order_identifier_present": False,
        "requires_order_readback": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "does_not_verify": [
            "order placement",
            "order cancellation",
            "real-money write readiness",
        ],
    }


def tool_result(payload: dict[str, JsonValue], *, is_error: bool) -> ToolResult:
    return ToolResult(structured_content=payload, is_error=is_error)


def _called_result(status: str) -> dict[str, JsonValue]:
    return {
        **common_result(status, network_call_made=True),
        "account_lookup_endpoint_called": True,
        "instrument_lookup_endpoint_called": True,
        "instrument_tradable": True,
        "precheck_endpoint_called": True,
    }


def _precheck_status(
    *,
    accepted: bool,
    error_code: str,
    disclaimer_object_present: bool,
) -> str:
    if error_code:
        return "precheck_rejected"
    if disclaimer_object_present:
        return "disclaimer_required"
    return "precheck_accepted" if accepted else "precheck_rejected"


def _precheck_blockers(
    *,
    accepted: bool,
    error_code: str,
    http_status: int,
    all_results_ok: bool,
    disclaimer_object_present: bool,
) -> list[str]:
    blockers: list[str] = []
    if not accepted:
        blockers.append("precheck_not_accepted")
    if error_code:
        blockers.append(f"saxo_error:{error_code}")
    if http_status != HTTP_OK:
        blockers.append("http_status_not_200")
    if not all_results_ok:
        blockers.append("returned_precheck_result_not_ok")
    if disclaimer_object_present:
        blockers.append("pretrade_disclaimer")
    return blockers
