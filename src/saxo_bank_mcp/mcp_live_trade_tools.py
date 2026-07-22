from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx2
from fastmcp.tools import ToolResult
from pydantic import ValidationError

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_account_refs import (
    LIVE_ACCOUNTS_ENDPOINT,
    LiveAccount,
    account_ref_for,
    account_summaries,
    active_live_accounts,
    parse_live_accounts,
    resolve_account_ref,
)
from saxo_bank_mcp.live_instrument_refs import (
    instrument_details_path,
    parse_live_instrument,
)
from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_read_missing_requirements_for_reason,
    live_read_next_action,
)
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.live_precheck_request import (
    LiveOrderPrecheckRequest,
    precheck_body,
    precheck_request_summary,
)
from saxo_bank_mcp.live_precheck_results import (
    HTTP_SUCCESS_MAX,
    HTTP_SUCCESS_MIN,
    LIVE_PRECHECK_ACCESS_LEVEL,
    LIVE_PRECHECK_ENDPOINT,
    LIVE_PRECHECK_TOOL_NAME,
    common_result,
    http_failure_result,
    precheck_response_result,
    tool_result,
)
from saxo_bank_mcp.live_token_refresh import live_token_for_tool
from saxo_bank_mcp.strict_json import StrictJsonError

LIVE_PRECHECK_TOOL_DESCRIPTION: Final = (
    "Selects a LIVE account by visible account ID or process-scoped reference, verifies the "
    "instrument is tradable, and runs Saxo's automatic Personal Read order precheck with "
    "ManualOrder=false. It never calls placement, change, cancellation, or disclaimer-response "
    "endpoints. Account IDs may be returned by account listing; Saxo account and client keys "
    "remain internal, and tokens are never echoed."
)


@dataclass(frozen=True, slots=True)
class AccountSelected:
    account: LiveAccount
    account_ref: str


@dataclass(frozen=True, slots=True)
class AccountSelectionFailed:
    result: ToolResult


type AccountSelection = AccountSelected | AccountSelectionFailed


async def saxo_precheck_live_order(order: LiveOrderPrecheckRequest) -> ToolResult:
    try:
        settings = resolve_live_oauth_settings()
    except LiveReadSettingsError as error:
        return tool_result(
            {
                **common_result("refused", network_call_made=False),
                "reason": error.code,
                "missing_requirements": live_read_missing_requirements_for_reason(error.code),
                "next_action": live_read_next_action(error.code),
            },
            is_error=True,
        )

    token_or_result = await live_token_for_tool(LIVE_PRECHECK_TOOL_NAME, settings)
    if isinstance(token_or_result, dict):
        payload = common_result(
            str(token_or_result["status"]),
            network_call_made=bool(token_or_result.get("network_call_made", False)),
        )
        payload["reason"] = str(token_or_result.get("reason", "token_cache_missing"))
        for name in ("missing_requirements", "next_action"):
            if name in token_or_result:
                payload[name] = token_or_result[name]
        return tool_result(payload, is_error=True)
    return await _precheck_with_token(order, settings, token_or_result)


async def _precheck_with_token(  # noqa: PLR0911
    order: LiveOrderPrecheckRequest,
    settings: SimAuthSettings,
    token: SaxoTokenSet,
) -> ToolResult:
    instrument_called = False
    precheck_called = False
    try:
        async with create_async_client(base_url=settings.rest_base_url) as client:
            accounts_response = await client.get(
                LIVE_ACCOUNTS_ENDPOINT.lstrip("/"),
                headers=_headers(token),
            )
            if not _is_success(accounts_response):
                return http_failure_result(
                    accounts_response,
                    account_lookup_endpoint_called=True,
                    precheck_endpoint_called=False,
                )
            try:
                accounts = parse_live_accounts(accounts_response.content)
            except (StrictJsonError, ValidationError):
                return _invalid_account_result(accounts_response.status_code)
            selection = _select_account(order, token, accounts)
            if isinstance(selection, AccountSelectionFailed):
                return selection.result

            instrument_called = True
            instrument_response = await client.get(
                instrument_details_path(order.uic, order.asset_type).lstrip("/"),
                headers=_headers(token),
            )
            if not _is_success(instrument_response):
                return http_failure_result(
                    instrument_response,
                    account_lookup_endpoint_called=True,
                    instrument_lookup_endpoint_called=True,
                    precheck_endpoint_called=False,
                )
            instrument_failure = _validate_instrument(order, instrument_response)
            if instrument_failure is not None:
                return instrument_failure

            precheck_called = True
            response = await client.post(
                LIVE_PRECHECK_ENDPOINT.lstrip("/"),
                json=precheck_body(order, selection.account),
                headers={**_headers(token), "Content-Type": "application/json"},
            )
    except httpx2.HTTPError as error:
        failure_stage = (
            "precheck"
            if precheck_called
            else "instrument_lookup"
            if instrument_called
            else "account_lookup"
        )
        return tool_result(
            {
                **common_result("network_error", network_call_made=True),
                "reason": type(error).__name__,
                "failure_stage": failure_stage,
                "http_status": None,
                "account_lookup_endpoint_called": True,
                "instrument_lookup_endpoint_called": instrument_called,
                "instrument_tradable": precheck_called,
                "precheck_endpoint_called": precheck_called,
                "precheck_request_accepted": False,
            },
            is_error=True,
        )

    if not _is_success(response):
        return http_failure_result(
            response,
            account_lookup_endpoint_called=True,
            instrument_lookup_endpoint_called=True,
            precheck_endpoint_called=True,
        )
    return precheck_response_result(
        response,
        account_id=selection.account.account_id,
        account_ref=selection.account_ref,
        request_summary=precheck_request_summary(order),
    )


def _select_account(
    order: LiveOrderPrecheckRequest,
    token: SaxoTokenSet,
    accounts: tuple[LiveAccount, ...],
) -> AccountSelection:
    active_accounts = active_live_accounts(accounts)
    selected: LiveAccount | None = None
    invalid_status = "account_selection_required"
    if order.account_id is not None:
        matches = tuple(
            account for account in active_accounts if account.account_id == order.account_id
        )
        selected = matches[0] if len(matches) == 1 else None
        invalid_status = "account_id_invalid"
    elif order.account_ref is not None:
        selected = resolve_account_ref(token, active_accounts, order.account_ref)
        invalid_status = "account_ref_invalid"
    elif len(active_accounts) == 1:
        selected = active_accounts[0]

    if selected is not None:
        return AccountSelected(selected, account_ref_for(token, selected.account_key))
    payload = {
        **common_result(invalid_status, network_call_made=True),
        "accounts": account_summaries(token, accounts),
        "active_account_count": len(active_accounts),
        "account_lookup_endpoint_called": True,
    }
    if invalid_status != "account_selection_required":
        payload["reason"] = f"{invalid_status}_for_current_login"
    return AccountSelectionFailed(tool_result(payload, is_error=True))


def _validate_instrument(
    order: LiveOrderPrecheckRequest,
    response: httpx2.Response,
) -> ToolResult | None:
    try:
        instrument = parse_live_instrument(response.content)
    except (StrictJsonError, ValidationError):
        reason = "instrument_response_schema_invalid"
    else:
        if instrument.uic != order.uic or instrument.asset_type != order.asset_type:
            reason = "instrument_identity_mismatch"
        elif not instrument.is_tradable:
            reason = "instrument_not_tradable"
        else:
            return None
    return tool_result(
        {
            **common_result("instrument_not_eligible", network_call_made=True),
            "reason": reason,
            "http_status": response.status_code,
            "account_lookup_endpoint_called": True,
            "instrument_lookup_endpoint_called": True,
            "precheck_request_accepted": False,
        },
        is_error=True,
    )


def _invalid_account_result(http_status: int) -> ToolResult:
    return tool_result(
        {
            **common_result("invalid_account_response", network_call_made=True),
            "http_status": http_status,
            "account_lookup_endpoint_called": True,
            "precheck_request_accepted": False,
        },
        is_error=True,
    )


def _is_success(response: httpx2.Response) -> bool:
    return HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX


def _headers(token: SaxoTokenSet) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.access_token}",
    }


__all__ = (
    "LIVE_PRECHECK_ACCESS_LEVEL",
    "LIVE_PRECHECK_ENDPOINT",
    "LIVE_PRECHECK_TOOL_DESCRIPTION",
    "LIVE_PRECHECK_TOOL_NAME",
    "LiveOrderPrecheckRequest",
    "saxo_precheck_live_order",
)
