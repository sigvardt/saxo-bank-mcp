from __future__ import annotations

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.order_mutation_models import (
    ORDER_WRITE_CLASSES,
    ORDER_WRITE_SPECS,
    OrderWriteClass,
    OrderWriteSpec,
)
from saxo_bank_mcp.qa_account import resolve_sim_account_key
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, reset_safety_state
from saxo_bank_mcp.server import mcp

FIXTURE_ACCOUNT: Final = "SIM-ACCOUNT-1"
FIXTURE_INSTRUMENT: Final = 211
FIXTURE_ASSET_TYPE: Final = "Stock"
FIXTURE_ORDER_AMOUNT: Final = 1
FIXTURE_ORDER_NOTIONAL: Final = 100
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
ORDER_WRITE_CLASS_BY_NAME: Final[dict[str, OrderWriteClass]] = {
    "place": "place",
    "modify": "modify",
    "cancel": "cancel",
    "cancel-by-instrument": "cancel-by-instrument",
    "multileg-place": "multileg-place",
    "multileg-modify": "multileg-modify",
    "multileg-cancel": "multileg-cancel",
}


def handle_sim_order_mutation(out: Path, classes: str | None) -> int:
    requested = _requested_classes(classes)
    payload = anyio.run(_sim_order_mutation, requested)
    return _write_redacted_with_secret_scan(
        out,
        payload,
        ("passed", "exercised", "incomplete_auth_required"),
    )


def handle_trade_write_denied(out: Path, missing: str) -> int:
    payload = anyio.run(_trade_write_denied, missing)
    return _write_redacted_with_secret_scan(out, payload, ("denied",))


async def _sim_order_mutation(classes: tuple[OrderWriteClass, ...]) -> dict[str, JsonValue]:
    reset_safety_state()
    per_class: list[dict[str, JsonValue]] = []
    account = await resolve_sim_account_key(
        default_account_key=FIXTURE_ACCOUNT,
        tool_name="saxo_sim_order_mutation_qa",
    )
    with _safety_env(account.account_key):
        async with Client(mcp) as client:
            for write_class in classes:
                spec = ORDER_WRITE_SPECS[write_class]
                preview = await _create_preview(client, spec, account.account_key)
                tool_payload = await _call_order_tool(client, spec, preview)
                per_class.append(class_report_for_qa(spec, preview, tool_payload))
    completed = [
        str(row["write_class"])
        for row in per_class
        if row.get("status") == "completed" and row.get("real_mutation_proven") is True
    ]
    auth_required = [
        str(row["write_class"]) for row in per_class if row.get("tool_status") == "auth_required"
    ]
    has_failed = any(row.get("status") == "failed" for row in per_class)
    was_exercised = any(row.get("network_call_made") is True for row in per_class)
    status = (
        "failed"
        if has_failed
        else "passed"
        if completed
        else "exercised"
        if was_exercised
        else "incomplete_auth_required"
    )
    all_classes_complete = len(completed) == len(classes)
    return {
        **base_event(
            "sim-order-mutation",
            status,
            "FastMCP SIM order mutation tools exercised through safety gates",
        ),
        "environment": "SIM",
        "classes_requested": list(classes),
        "completed_classes": completed,
        "auth_required_classes": auth_required,
        "per_class": per_class,
        "real_mutation_proven": all_classes_complete,
        "completion_claim_allowed": all_classes_complete,
        "fastmcp_called": True,
        "network_call_made": any(row.get("network_call_made") is True for row in per_class),
        "account_resolution": account.to_safe_json(),
        "live_write": any(row.get("live_write") is True for row in per_class),
        "order_or_subscription_created": any(
            row.get("order_or_subscription_created") is True for row in per_class
        ),
        "account_key_redacted": True,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_write_denied(missing: str) -> dict[str, JsonValue]:
    reset_safety_state()
    spec = ORDER_WRITE_SPECS["place"]
    account = await resolve_sim_account_key(
        default_account_key=FIXTURE_ACCOUNT,
        tool_name="saxo_trade_write_denied_qa",
    )
    with _safety_env(account.account_key):
        async with Client(mcp) as client:
            preview = await _create_preview(client, spec, account.account_key)
            token = str(preview.get("preview_token", ""))
            result = await client.call_tool(
                spec.tool_name,
                {"preview_token": token},
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    same_fingerprint = payload.get("request_fingerprint") == preview.get("request_fingerprint")
    denied = (
        missing == "approval-factor"
        and payload.get("status") == "denied"
        and payload.get("denial_reason") == "approval_factor_missing"
        and payload.get("network_call_made") is False
    )
    return {
        **base_event(
            "trade-write-denied",
            "denied" if denied else "failed",
            "FastMCP order write refused missing approval factor before network",
        ),
        "tool_name": spec.tool_name,
        "fastmcp_called": True,
        "missing": missing,
        "denial_reason": str(payload.get("denial_reason", "")),
        "same_request_fingerprint": same_fingerprint,
        "preview_token_redacted": True,
        "account_resolution": account.to_safe_json(),
        "audit_path_inside_repo": payload.get("audit_path_inside_repo") is True,
        "network_call_made": payload.get("network_call_made") is True,
        "order_placed": payload.get("order_placed") is True,
        "order_modified": payload.get("order_modified") is True,
        "order_cancelled": payload.get("order_cancelled") is True,
        "live_write": payload.get("live_write") is True,
        "order_or_subscription_created": (payload.get("order_or_subscription_created") is True),
        "write_result": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _create_preview(
    client: Client[FastMCPTransport],
    spec: OrderWriteSpec,
    account_key: str,
) -> dict[str, JsonValue]:
    result = await client.call_tool(
        "saxo_create_write_preview", probe_preview_request(spec, account_key)
    )
    return _payload(result.structured_content)


async def _call_order_tool(
    client: Client[FastMCPTransport],
    spec: OrderWriteSpec,
    preview: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    token = str(preview.get("preview_token", ""))
    result = await client.call_tool(
        spec.tool_name,
        {"preview_token": token, "approval_factor": TEST_APPROVAL_FACTOR},
        raise_on_error=False,
    )
    return _payload(result.structured_content)


def class_report_for_qa(
    spec: OrderWriteSpec,
    preview: dict[str, JsonValue],
    tool_payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    completed = _completion_requirements_met(spec, tool_payload)
    status = _class_status(tool_payload, completed=completed)
    reason = "" if completed else _agent_reason(tool_payload)
    return {
        "write_class": spec.write_class,
        "tool_name": spec.tool_name,
        "operation_id": spec.operation_id,
        "status": status,
        "preview_status": str(preview.get("status", "")),
        "tool_status": str(tool_payload.get("status", "")),
        "write_class_status": str(tool_payload.get("write_class_status", status)),
        "real_mutation_proven": completed,
        "completion_oracle": _completion_oracle(spec),
        "completion_not_claimed_reason": _completion_not_claimed_reason(
            spec,
            tool_payload,
            completed=completed,
        ),
        "fastmcp_called": tool_payload.get("fastmcp_called") is True,
        "preview_token_redacted": _preview_token_redacted(preview),
        "approval_factor_mode": str(tool_payload.get("approval_factor_mode", "test_only_sim")),
        "x_request_id_present": tool_payload.get("x_request_id_present") is True,
        "x_request_id_response_echo_verified": (
            tool_payload.get("x_request_id_response_echo_verified") is True
        ),
        "order_result_parsed": tool_payload.get("order_result_parsed") is True,
        "port_orders_readback": tool_payload.get("port_orders_readback") is True,
        "trade_messages_readback": tool_payload.get("trade_messages_readback") is True,
        "open_order_readback_matched_response_order": (
            tool_payload.get("open_order_readback_matched_response_order") is True
        ),
        "open_order_readback_confirmed_absent": (
            tool_payload.get("open_order_readback_confirmed_absent") is True
        ),
        "cleanup_attempted": tool_payload.get("cleanup_attempted") is True,
        "cleanup_status": str(tool_payload.get("cleanup_status", "not_run")),
        "raw_audit_path_inside_repo": tool_payload.get("raw_audit_path_inside_repo") is True,
        "account_key_redacted": _account_key_redacted(tool_payload),
        "mutation_may_have_occurred": tool_payload.get("mutation_may_have_occurred") is True,
        "mutation_content_verified": tool_payload.get("mutation_content_verified") is True,
        "retry_unsafe": tool_payload.get("retry_unsafe") is True,
        "committed_before_network_result": (
            tool_payload.get("committed_before_network_result") is True
        ),
        "order_placed": tool_payload.get("order_placed"),
        "order_modified": tool_payload.get("order_modified"),
        "order_cancelled": tool_payload.get("order_cancelled"),
        "network_call_made": tool_payload.get("network_call_made") is True,
        "live_write": tool_payload.get("live_write") is True,
        "order_or_subscription_created": (
            tool_payload.get("order_or_subscription_created") is True
        ),
        "denial_reason": str(tool_payload.get("denial_reason") or ""),
        "reason": reason,
        "next_action": str(tool_payload.get("next_action", "")),
        "does_not_verify": _does_not_verify(tool_payload),
    }


def _completion_requirements_met(
    spec: OrderWriteSpec,
    tool_payload: dict[str, JsonValue],
) -> bool:
    common_requirements_met = (
        tool_payload.get("status") == "completed"
        and tool_payload.get("network_call_made") is True
        and tool_payload.get("order_result_parsed") is True
        and tool_payload.get("x_request_id_present") is True
        and tool_payload.get("retry_unsafe") is not True
    )
    if not common_requirements_met:
        return False
    if spec.write_class == "cancel-by-instrument":
        return (
            tool_payload.get("mutation_content_verified") is True
            and tool_payload.get("order_cancelled") is True
            and tool_payload.get("trade_messages_readback") is True
        )
    if spec.write_class in {"place", "multileg-place"}:
        return (
            tool_payload.get("mutation_content_verified") is True
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
            and tool_payload.get("cleanup_status") == "verified_no_open_order"
        )
    return (
        tool_payload.get("mutation_content_verified") is True
        and tool_payload.get("port_orders_readback") is True
        and tool_payload.get("trade_messages_readback") is True
    )


def _completion_oracle(spec: OrderWriteSpec) -> str:
    if spec.write_class == "cancel-by-instrument":
        return (
            "To claim completion, the output must show a parsed completed response, a generated "
            "x-request-id, retry_unsafe=false, order_cancelled=true, mutation content proving a "
            "matched order, and trade-message readback; portfolio order-list readback alone is "
            "not sufficient for delete-by-instrument"
        )
    if spec.write_class in {"place", "multileg-place"}:
        return (
            "completed response parsed, x-request-id present, retry safe, mutation content "
            "verified, portfolio order-list readback, trade messages readback, and cleanup_status "
            "verified_no_open_order"
        )
    return (
        "completed response parsed, x-request-id present, retry safe, mutation content "
        "verified, portfolio order-list readback, and trade messages readback"
    )


def _completion_not_claimed_reason(
    spec: OrderWriteSpec,
    tool_payload: dict[str, JsonValue],
    *,
    completed: bool,
) -> str:
    if completed:
        return ""
    if (
        spec.write_class == "cancel-by-instrument"
        and tool_payload.get("status") == "completed_unverified"
    ):
        return (
            "empty-success delete-by-instrument did not prove any order matched the "
            "cancel filter"
        )
    if tool_payload.get("status") == "completed":
        return "completed response lacked the class-specific proof required by the oracle"
    return _agent_reason(tool_payload)


def _agent_reason(tool_payload: dict[str, JsonValue]) -> str:
    for key in ("reason", "denial_reason"):
        value = tool_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parsed = tool_payload.get("parsed_response")
    if isinstance(parsed, dict):
        status = str(tool_payload.get("status", "unknown"))
        http_status = str(tool_payload.get("http_status", "unknown"))
        error_codes = _error_codes(parsed)
        codes = ",".join(error_codes) if error_codes else "none"
        return (
            "saxo_order_write_not_completed "
            f"status={status} http_status={http_status} error_codes={codes}"
        )
    return ""


def _error_codes(parsed_response: dict[str, JsonValue]) -> list[str]:
    raw = parsed_response.get("error_codes")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _does_not_verify(tool_payload: dict[str, JsonValue]) -> list[str]:
    raw = tool_payload.get("does_not_verify")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _class_status(tool_payload: dict[str, JsonValue], *, completed: bool) -> str:
    if completed:
        return "completed"
    status = tool_payload.get("status")
    if status == "auth_required":
        return "incomplete"
    if status == "denied":
        return "refused"
    if (
        status in {"completed", "completed_unverified"}
        and tool_payload.get("network_call_made") is True
    ):
        return "exercised"
    if _safely_rejected_by_saxo(tool_payload):
        return "incomplete"
    return "failed"


def _safely_rejected_by_saxo(tool_payload: dict[str, JsonValue]) -> bool:
    return (
        tool_payload.get("status") == "failed"
        and tool_payload.get("network_call_made") is True
        and tool_payload.get("order_or_subscription_created") is not True
        and tool_payload.get("mutation_may_have_occurred") is not True
        and tool_payload.get("retry_unsafe") is not True
        and tool_payload.get("order_result_parsed") is True
    )


def _preview_token_redacted(preview: dict[str, JsonValue]) -> bool:
    return isinstance(preview.get("preview_token"), str)


def _account_key_redacted(tool_payload: dict[str, JsonValue]) -> bool:
    return "AccountKey" not in json.dumps(tool_payload, sort_keys=True)


def probe_preview_request(spec: OrderWriteSpec, account_key: str) -> dict[str, JsonValue]:
    return {
        "operation_id": spec.operation_id,
        "account_key": account_key,
        "instrument_uic": FIXTURE_INSTRUMENT,
        "quantity": FIXTURE_ORDER_AMOUNT,
        "estimated_notional": FIXTURE_ORDER_NOTIONAL,
        "account_currency": "USD",
        "risk": {
            "cost": FIXTURE_ORDER_NOTIONAL,
            "cash_required": FIXTURE_ORDER_NOTIONAL,
            "margin_impact": 1,
            "contract_multiplier": 1,
            "conversion_known": True,
        },
        "request_body": _request_body(spec, account_key),
    }


def _request_body(spec: OrderWriteSpec, account_key: str) -> dict[str, JsonValue]:
    common: dict[str, JsonValue] = {"AccountKey": account_key}
    match spec.write_class:
        case "cancel":
            return {**common, "OrderIds": "fixture-order-id"}
        case "cancel-by-instrument":
            return {**common, "AssetType": FIXTURE_ASSET_TYPE, "Uic": FIXTURE_INSTRUMENT}
        case "multileg-cancel":
            return {**common, "MultiLegOrderId": "fixture-multileg-order-id"}
        case "multileg-place" | "multileg-modify":
            return {
                **common,
                "OrderType": "Limit",
                "OrderDuration": {"DurationType": "DayOrder"},
                "Legs": [
                    {
                        "Uic": FIXTURE_INSTRUMENT,
                        "Amount": FIXTURE_ORDER_AMOUNT,
                        "BuySell": "Buy",
                    },
                ],
            }
        case _:
            return {
                **common,
                "Uic": FIXTURE_INSTRUMENT,
                "AssetType": FIXTURE_ASSET_TYPE,
                "Amount": FIXTURE_ORDER_AMOUNT,
                "BuySell": "Buy",
                "ManualOrder": False,
                "OrderType": "Market",
                "OrderDuration": {"DurationType": "DayOrder"},
            }


def _requested_classes(classes: str | None) -> tuple[OrderWriteClass, ...]:
    if classes is None or not classes.strip():
        return ORDER_WRITE_CLASSES
    requested: list[OrderWriteClass] = []
    for raw in classes.split(","):
        parsed = _parse_order_write_class(raw)
        if parsed is not None:
            requested.append(parsed)
    return tuple(requested) if requested else ORDER_WRITE_CLASSES


def _parse_order_write_class(raw: str) -> OrderWriteClass | None:
    return ORDER_WRITE_CLASS_BY_NAME.get(raw.strip())


def _payload(value: object) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _write_redacted_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    success_statuses: tuple[str, ...],
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("order probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") in success_statuses and clean else 1


@contextmanager
def _safety_env(account_key: str) -> Generator[None]:
    previous = {key: os.environ.get(key) for key in _SAFETY_ENV_DEFAULTS}
    try:
        for key, value in _SAFETY_ENV_DEFAULTS.items():
            os.environ[key] = value
        os.environ["SAXO_MCP_ACCOUNT_ALLOWLIST"] = account_key
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
