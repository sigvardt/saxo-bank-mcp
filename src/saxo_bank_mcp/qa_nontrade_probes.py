from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.nontrade_policy import (
    NO_SAFE_NONTRADE_OPERATION_REASON,
    all_nontrade_writes_are_refused,
    first_nontrade_write_operation,
    nontrade_classification_rows,
    nontrade_refusal_reason,
    nontrade_safety_class,
    nontrade_write_operations,
    safe_nontrade_write_operations,
    service_group_for_slug,
)
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.server import mcp

JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


def handle_nontrade_write(out: Path, *, safe_only: bool) -> int:
    mode = "safe_only" if safe_only else "unsafe"
    payload = anyio.run(_nontrade_write_probe, mode)
    return _write_with_secret_scan(
        out,
        payload,
        ("exercised", "skipped_no_safe_operation"),
    )


def handle_nontrade_denied(out: Path, *, service: str) -> int:
    payload = anyio.run(_nontrade_denied_probe, service)
    return _write_with_secret_scan(out, payload, ("denied", "refused"))


def handle_nontrade_denial_sweep(out: Path) -> int:
    payload = anyio.run(_nontrade_denial_sweep)
    return _write_with_secret_scan(out, payload, ("passed",))


async def _nontrade_write_probe(mode: str) -> dict[str, JsonValue]:
    first_operation = next(iter(nontrade_write_operations()), None)
    preview_denial: dict[str, JsonValue] = {}
    async with Client(mcp) as client:
        tools = await client.list_tools()
        if first_operation is not None:
            result = await client.call_tool(
                "saxo_create_write_preview",
                {"operation_id": first_operation.operation_id},
                raise_on_error=False,
            )
            preview_denial = _payload(result.structured_content)
            preview_denial["mcp_is_error"] = result.is_error
    tool_names = {tool.name for tool in tools}
    classifications = nontrade_classification_rows()
    safe_operations = safe_nontrade_write_operations()
    skipped_refused = all_nontrade_writes_are_refused()
    safe_only = mode == "safe_only"
    status = "skipped_no_safe_operation"
    detail = "No non-trading/admin write has endpoint-specific SIM-safe no-money evidence"
    if not safe_only:
        status = "failed"
        detail = "nontrade-write must be run with --safe-only"
    elif safe_operations:
        status = "failed"
        detail = "safe non-trading/admin operation exists but no execution path is implemented"
    return {
        **base_event("nontrade-write", status, detail),
        "safe_only": safe_only,
        "environment": "SIM",
        "prompted_user": False,
        "safe_operation_count": len(safe_operations),
        "preview_and_commit_exercised": False,
        "generic_preview_denial": preview_denial,
        "skipped_operations_refused": skipped_refused,
        "skipped_operation_count": len(classifications),
        "skip_reason": NO_SAFE_NONTRADE_OPERATION_REASON,
        "no_arbitrary_url_call": True,
        "live_write": False,
        "order_or_subscription_created": False,
        "fastmcp_tools": {
            "saxo_call_registered_endpoint": "saxo_call_registered_endpoint" in tool_names,
            "saxo_create_write_preview": "saxo_create_write_preview" in tool_names,
            "saxo_commit_write_preview": "saxo_commit_write_preview" in tool_names,
        },
        "classifications": classifications,
    }


async def _nontrade_denied_probe(service: str) -> dict[str, JsonValue]:
    service_group = service_group_for_slug(service)
    if service_group is None:
        return _unknown_service_denial(service)
    operation = first_nontrade_write_operation(service_group)
    if operation is None:
        return _unknown_service_denial(service)
    path = _sample_path(operation.path_template)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": operation.method, "path": path},
            raise_on_error=False,
        )
    call_payload = _payload(result.structured_content)
    call_payload["mcp_is_error"] = result.is_error
    denied = (
        call_payload.get("status") == "denied"
        and call_payload.get("network_call_made") is False
        and call_payload.get("mcp_is_error") is True
    )
    return {
        **base_event(
            "nontrade-denied",
            "denied" if denied else "failed",
            "FastMCP registered endpoint caller refused risky non-trading/admin write",
        ),
        "service": service,
        "service_group": service_group,
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": path,
        "safety_class": nontrade_safety_class(operation),
        "registry_status": operation.status,
        "registered_refusal_reason": operation.refusal_reason,
        "refusal_reason": nontrade_refusal_reason(operation),
        "preview_created": False,
        "approval_requested": False,
        "network_call_made": bool(call_payload.get("network_call_made", False)),
        "live_write": False,
        "order_or_subscription_created": False,
        "registered_call": call_payload,
    }


async def _nontrade_denial_sweep() -> dict[str, JsonValue]:
    operations = nontrade_write_operations()
    rows: list[dict[str, JsonValue]] = []
    async with Client(mcp) as client:
        for operation in operations:
            path = _sample_path(operation.path_template)
            result = await client.call_tool(
                "saxo_call_registered_endpoint",
                {"method": operation.method, "path": path},
                raise_on_error=False,
            )
            call_payload = _payload(result.structured_content)
            call_payload["mcp_is_error"] = result.is_error
            rows.append(
                {
                    "operation_id": operation.operation_id,
                    "service_group": operation.service_group,
                    "method": operation.method,
                    "path": path,
                    "status": str(call_payload.get("status", "")),
                    "mcp_is_error": bool(call_payload.get("mcp_is_error", False)),
                    "denial_reason": str(call_payload.get("denial_reason", "")),
                    "network_call_made": bool(
                        call_payload.get("network_call_made", False),
                    ),
                    "preview_created": bool(call_payload.get("preview_created", False)),
                    "approval_requested": bool(
                        call_payload.get("approval_requested", False),
                    ),
                    "order_or_subscription_created": bool(
                        call_payload.get("order_or_subscription_created", False),
                    ),
                },
            )
    passed = all(
        row["status"] == "denied"
        and row["mcp_is_error"] is True
        and row["network_call_made"] is False
        and row["preview_created"] is False
        and row["approval_requested"] is False
        and row["order_or_subscription_created"] is False
        for row in rows
    )
    return {
        **base_event(
            "nontrade-denial-sweep",
            "passed" if passed else "failed",
            "FastMCP denial sweep over all registered non-trading/admin writes",
        ),
        "operation_count": len(operations),
        "denied_count": sum(1 for row in rows if row["status"] == "denied"),
        "mcp_error_count": sum(1 for row in rows if row["mcp_is_error"] is True),
        "network_call_made": any(bool(row["network_call_made"]) for row in rows),
        "preview_created": any(bool(row["preview_created"]) for row in rows),
        "approval_requested": any(bool(row["approval_requested"]) for row in rows),
        "order_or_subscription_created": any(
            bool(row["order_or_subscription_created"]) for row in rows
        ),
        "results": rows,
    }


def _unknown_service_denial(service: str) -> dict[str, JsonValue]:
    return {
        **base_event(
            "nontrade-denied",
            "denied",
            "Unknown non-trading/admin write service refused before preview",
        ),
        "service": service,
        "service_group": "",
        "operation_id": "",
        "method": "",
        "path": "",
        "safety_class": "unclassified_write_fail_closed",
        "registry_status": "unregistered",
        "registered_refusal_reason": "",
        "refusal_reason": "unclassified_nontrade_write_service",
        "preview_created": False,
        "approval_requested": False,
        "network_call_made": False,
        "live_write": False,
        "order_or_subscription_created": False,
        "registered_call": {},
    }


def _sample_path(path_template: str) -> str:
    return re.sub(r"\{[^/{}]+\}", "sample-id", path_template)


def _payload(value: object) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _write_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    success_statuses: tuple[str, ...],
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("nontrade probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") in success_statuses and clean else 1
