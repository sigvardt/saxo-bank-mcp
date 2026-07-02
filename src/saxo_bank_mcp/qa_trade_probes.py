from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.safety import reset_safety_state
from saxo_bank_mcp.server import mcp

FIXTURE_ACCOUNT: Final = "SIM-ACCOUNT-1"
FIXTURE_INSTRUMENT: Final = 21
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


def handle_trade_precheck(out: Path) -> int:
    payload = anyio.run(_trade_precheck)
    return _write_redacted_with_secret_scan(out, payload, "passed")


def handle_trade_disclaimer_blocked(out: Path) -> int:
    payload = anyio.run(_trade_disclaimer_blocked)
    return _write_redacted_with_secret_scan(out, payload, "denied")


async def _trade_precheck() -> dict[str, JsonValue]:
    reset_safety_state()
    with _safety_env():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_create_order_preview",
                {
                    "order_body": _order_body(),
                    "precheck_response": _precheck_response(),
                    "disclaimer_response_state": "none",
                },
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    passed = payload.get("status") == "preview_created" and result.is_error is False
    return {
        **base_event(
            "trade-precheck",
            "passed" if passed else "failed",
            "FastMCP trade preview path exercised with no-secret pre-check fixture",
        ),
        "tool_name": "saxo_create_order_preview",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "environment": "SIM",
        "precheck_endpoint": "/trade/v2/orders/precheck",
        "preview_created": payload.get("preview_created") is True,
        "preview_status": str(payload.get("status", "")),
        "account_key_redacted": True,
        "network_call_made": payload.get("network_call_made") is True,
        "fixture_precheck_used": True,
        "order_placed": payload.get("order_placed") is True,
        "order_modified": payload.get("order_modified") is True,
        "order_cancelled": payload.get("order_cancelled") is True,
        "live_write": payload.get("live_write") is True,
        "preview": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_disclaimer_blocked() -> dict[str, JsonValue]:
    reset_safety_state()
    with _safety_env():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_create_order_preview",
                {
                    "order_body": _order_body(),
                    "precheck_response": {
                        **_precheck_response(),
                        "PreTradeDisclaimers": {
                            "DisclaimerContext": "fixture-context",
                            "DisclaimerTokens": ["fixture-token"],
                        },
                    },
                    "disclaimer_details": _blocking_disclaimer_details(),
                    "disclaimer_response_state": "missing",
                },
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    raw_reasons = payload.get("denial_reasons", [])
    reasons = [str(value) for value in raw_reasons] if isinstance(raw_reasons, list) else []
    denied = payload.get("status") == "denied" and "blocking_disclaimer" in reasons
    return {
        **base_event(
            "trade-disclaimer-blocked",
            "denied" if denied else "failed",
            "FastMCP trade preview refused required or blocking disclaimer",
        ),
        "tool_name": "saxo_create_order_preview",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "denial_reasons": reasons,
        "disclaimer_context_present": payload.get("disclaimer_context_present") is True,
        "disclaimer_tokens_count": payload.get("disclaimer_tokens_count", 0),
        "disclaimer_details_sanitized": payload.get("disclaimer_details_sanitized") is True,
        "exact_disclaimer_content_present": (
            payload.get("exact_disclaimer_content_present") is True
        ),
        "response_endpoint_path": payload.get("response_endpoint_path", ""),
        "network_call_made": payload.get("network_call_made") is True,
        "disclaimer_response_submitted": payload.get("disclaimer_response_submitted") is True,
        "preview_created": payload.get("preview_created") is True,
        "order_placed": payload.get("order_placed") is True,
        "order_modified": payload.get("order_modified") is True,
        "order_cancelled": payload.get("order_cancelled") is True,
        "live_write": payload.get("live_write") is True,
        "preview": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


def _order_body() -> dict[str, JsonValue]:
    return {
        "AccountKey": FIXTURE_ACCOUNT,
        "Uic": FIXTURE_INSTRUMENT,
        "AssetType": "Stock",
        "Amount": 10,
        "BuySell": "Buy",
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ContractMultiplier": 1,
    }


def _precheck_response() -> dict[str, JsonValue]:
    return {
        "PreCheckResult": "Ok",
        "EstimatedCashRequired": 500,
        "EstimatedCashRequiredCurrency": "USD",
        "EstimatedTotalCostInAccountCurrency": 500,
        "InstrumentToAccountConversionRate": 1,
        "CostInAccountCurrency": {"Amount": 500},
        "MarginImpactBuySell": {"MarginImpact": 20},
    }


def _blocking_disclaimer_details() -> dict[str, JsonValue]:
    return {
        "Data": [
            {
                "Body": "Trading this instrument requires exchange rules acceptance.",
                "Conditions": [{"Type": "Checkbox", "Label": "I understand"}],
                "DisclaimerToken": "fixture-token",
                "IsBlocking": True,
                "ResponseOptions": [{"ResponseType": "Accepted", "Label": "I accept"}],
            },
        ],
    }


def _payload(value: object) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _write_redacted_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    success_status: str,
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("trade probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") == success_status and clean else 1


@contextmanager
def _safety_env() -> Generator[None]:
    previous = {key: os.environ.get(key) for key in _SAFETY_ENV_DEFAULTS}
    try:
        for key, value in _SAFETY_ENV_DEFAULTS.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_SAFETY_ENV_DEFAULTS: Final = {
    "SAXO_MCP_ENVIRONMENT": "SIM",
    "SAXO_MCP_ACCOUNT_ALLOWLIST": FIXTURE_ACCOUNT,
    "SAXO_MCP_INSTRUMENT_ALLOWLIST": str(FIXTURE_INSTRUMENT),
}
