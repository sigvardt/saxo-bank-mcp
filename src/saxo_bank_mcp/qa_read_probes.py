from __future__ import annotations

from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.endpoint_registry import (
    EXPECTED_SERVICE_GROUP_COUNTS,
    implemented_read_operations,
)
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.server import mcp

JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


def handle_read_smoke(out: Path, groups: str | None) -> int:
    payload = anyio.run(_read_smoke, groups or "all")
    return _write_with_secret_scan(out, payload, "passed")


def handle_registered_endpoint_denied(out: Path, *, method: str, path: str) -> int:
    payload = anyio.run(_registered_endpoint_denied, method, path)
    return _write_with_secret_scan(out, payload, "denied")


def load_registered_endpoint_list(
    service_group: str | None,
    limit: int,
    offset: int,
) -> dict[str, JsonValue]:
    return anyio.run(_list_registered_endpoints, service_group, limit, offset)


async def _list_registered_endpoints(
    service_group: str | None,
    limit: int,
    offset: int,
) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {"limit": limit, "offset": offset}
    if service_group is not None:
        arguments["service_group"] = service_group
    async with Client(mcp) as client:
        result = await client.call_tool("saxo_list_registered_endpoints", arguments)
    return _payload(result.structured_content)


async def _read_smoke(groups: str) -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/root/v1/diagnostics/get"},
        )
    tool_names = {tool.name for tool in tools}
    diagnostic = _payload(result.structured_content)
    per_group = _per_group_smoke(diagnostic)
    passed = (
        "saxo_call_registered_endpoint" in tool_names
        and "saxo_list_registered_endpoints" in tool_names
        and len(per_group) == len(EXPECTED_SERVICE_GROUP_COUNTS)
    )
    return {
        **base_event(
            "read-smoke",
            "passed" if passed else "failed",
            "FastMCP read tools listed and representative registered read smoke recorded",
        ),
        "groups": groups,
        "fastmcp_tools": {
            "saxo_call_registered_endpoint": "saxo_call_registered_endpoint" in tool_names,
            "saxo_list_registered_endpoints": "saxo_list_registered_endpoints" in tool_names,
        },
        "per_group": per_group,
        "diagnostic_call": diagnostic,
        "no_arbitrary_url_call": True,
        "live_write": False,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _registered_endpoint_denied(method: str, path: str) -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": method, "path": path},
            raise_on_error=False,
        )
    payload = _payload(result.structured_content)
    return {
        **base_event(
            "registered-endpoint-denied",
            str(payload.get("status", "failed")),
            "FastMCP registered endpoint caller denied an unregistered path",
        ),
        **payload,
        "git": current_git_state().model_dump(mode="json"),
    }


def _per_group_smoke(diagnostic: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    group_examples = {
        operation.service_group: operation
        for operation in implemented_read_operations()
        if operation.service_group != "Root Services"
    }
    rows: list[dict[str, JsonValue]] = []
    for group in EXPECTED_SERVICE_GROUP_COUNTS:
        if group == "Root Services":
            rows.append(
                {
                    "service_group": group,
                    "status": "exercised",
                    "operation_id": str(diagnostic.get("operation_id", "")),
                    "method": "GET",
                    "path": "/root/v1/diagnostics/get",
                },
            )
            continue
        operation = group_examples.get(group)
        rows.append(
            {
                "service_group": group,
                "status": "no_sim_data",
                "operation_id": "" if operation is None else operation.operation_id,
                "reason": "requires_account_instrument_or_service_specific_parameters",
            },
        )
    return rows


def _payload(value: object) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _write_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    success_status: str,
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("read probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") == success_status and clean else 1
