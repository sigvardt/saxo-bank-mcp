from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx2
from fastmcp.tools import ToolResult
from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_account_refs import (
    LIVE_ACCOUNTS_ENDPOINT,
    account_ref_for,
    account_summaries,
    active_live_accounts,
    parse_live_accounts,
)
from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_read_missing_requirements_for_reason,
    live_read_next_action,
)
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.live_token_refresh import live_token_for_tool
from saxo_bank_mcp.strict_json import StrictJsonError

LIVE_ACCOUNTS_TOOL_NAME: Final = "saxo_list_live_accounts"
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300
LIVE_ACCOUNTS_TOOL_DESCRIPTION: Final = (
    "Lists LIVE accounts with visible account IDs and process-scoped opaque references. "
    "Saxo account and client keys remain internal. It calls only GET /port/v1/accounts/me, "
    "never exposes tokens, and never performs a write."
)


async def saxo_list_live_accounts() -> ToolResult:
    try:
        settings = resolve_live_oauth_settings()
    except LiveReadSettingsError as error:
        return _tool_result(
            {
                **_common_result("refused", network_call_made=False),
                "reason": error.code,
                "missing_requirements": live_read_missing_requirements_for_reason(error.code),
                "next_action": live_read_next_action(error.code),
            },
            is_error=True,
        )

    token_or_result = await live_token_for_tool(LIVE_ACCOUNTS_TOOL_NAME, settings)
    if isinstance(token_or_result, dict):
        refresh_network_call = bool(token_or_result.get("network_call_made", False))
        return _tool_result(
            {
                **token_or_result,
                **_common_result(
                    str(token_or_result["status"]),
                    network_call_made=refresh_network_call,
                ),
                "reason": str(token_or_result.get("reason", "token_cache_missing")),
            },
            is_error=True,
        )

    try:
        async with create_async_client(base_url=settings.rest_base_url) as client:
            response = await client.get(
                LIVE_ACCOUNTS_ENDPOINT.lstrip("/"),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token_or_result.access_token}",
                },
            )
    except httpx2.HTTPError as error:
        return _tool_result(
            {
                **_common_result("network_error", network_call_made=True),
                "reason": type(error).__name__,
                "http_status": None,
            },
            is_error=True,
        )

    if not HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX:
        return _tool_result(
            {
                **_common_result("account_lookup_failed", network_call_made=True),
                "http_status": response.status_code,
            },
            is_error=True,
        )
    try:
        accounts = parse_live_accounts(response.content)
    except (StrictJsonError, ValidationError):
        return _tool_result(
            {
                **_common_result("invalid_account_response", network_call_made=True),
                "http_status": response.status_code,
            },
            is_error=True,
        )

    active_accounts = active_live_accounts(accounts)
    default_account_ref = (
        account_ref_for(token_or_result, active_accounts[0].account_key)
        if len(active_accounts) == 1
        else None
    )
    return _tool_result(
        {
            **_common_result("accounts_listed", network_call_made=True),
            "http_status": response.status_code,
            "accounts": account_summaries(token_or_result, accounts),
            "account_count": len(accounts),
            "active_account_count": len(active_accounts),
            "default_account_ref": default_account_ref,
            "selection_required": len(active_accounts) != 1,
        },
        is_error=False,
    )


def _common_result(status: str, *, network_call_made: bool) -> Mapping[str, JsonValue]:
    return {
        "status": status,
        "tool_name": LIVE_ACCOUNTS_TOOL_NAME,
        "environment": "LIVE",
        "endpoint_path": LIVE_ACCOUNTS_ENDPOINT,
        "network_call_made": network_call_made,
        "account_identifiers_exposed": True,
        "tokens_redacted": True,
        "account_refs_process_scoped": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }


def _tool_result(payload: Mapping[str, JsonValue], *, is_error: bool) -> ToolResult:
    return ToolResult(structured_content=dict(payload), is_error=is_error)
