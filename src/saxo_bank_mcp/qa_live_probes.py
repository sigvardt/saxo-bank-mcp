from __future__ import annotations

import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

import anyio

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SaxoRuntimeConfig
from saxo_bank_mcp.http_client import NetworkTransportForbiddenError, forbid_network_transport
from saxo_bank_mcp.live_mode import live_read_missing_requirements
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.qa_live_evidence import sanitize_live_read_payloads
from saxo_bank_mcp.qa_live_publication import write_secret_scanned_event
from saxo_bank_mcp.qa_live_read_contract import (
    LIVE_READ_SCENARIO_STATUSES,
    live_read_transport_passed,
)
from saxo_bank_mcp.qa_mcp_probe_calls import (
    call_live_read_payloads,
    call_live_read_refusal_payload,
    call_live_write_refusal_payload,
    call_tool_inventory_payload,
)


def handle_live_read(out: Path, skip_out: Path) -> int:
    live_environ = _live_probe_environ()
    runtime = SaxoRuntimeConfig.from_env(live_environ)
    if runtime.effective_read_environment() != "LIVE":
        event = {
            **base_event(
                "live-read",
                "skipped_no_live_credentials",
                "LIVE read env vars or credentials are absent",
            ),
            "requested_environment": "LIVE",
            "effective_read_environment": runtime.effective_read_environment(),
            "live_reads_enabled": runtime.live_reads_enabled,
            "live_credentials_present": runtime.live_credentials_present,
            "network_call_made": False,
            "live_write_called": False,
            "order_or_subscription_created": False,
            "prompted_user": False,
            "private_identifiers_present": False,
            "missing_requirements": live_read_missing_requirements(runtime),
        }
        return write_secret_scanned_event(out, event, mirrors=(skip_out,))

    with _temporary_env({"SAXO_MCP_ENVIRONMENT": "LIVE"}):
        payloads = anyio.run(call_live_read_payloads)
    tool_statuses = {
        tool_name: str(payload.get("status", "failed")) for tool_name, payload in payloads.items()
    }
    status = (
        "passed"
        if (
            _live_read_suite_passed(tool_statuses)
            and _live_read_safety_passed(payloads)
            and live_read_transport_passed(payloads)
        )
        else "failed"
    )
    event: dict[str, JsonValue] = {
        **base_event("live-read", status, "FastMCP live read-only probe suite returned"),
        "requested_environment": "LIVE",
        "read_scenarios_exercised": list(payloads),
        "read_tools_exercised": sorted(
            {
                str(payload.get("tool_name", scenario_name))
                for scenario_name, payload in payloads.items()
            },
        ),
        "tool_statuses": tool_statuses,
        "tool_results": sanitize_live_read_payloads(payloads),
        "authenticated_registered_read_passed": (
            tool_statuses.get("saxo_call_registered_endpoint_authenticated_account") == "passed"
            and payloads["saxo_call_registered_endpoint_authenticated_account"].get(
                "auth_exercised",
            )
            is True
        ),
        "live_read_coverage": _live_read_coverage(tool_statuses),
        "network_call_made": any(
            bool(payload.get("network_call_made", False)) for payload in payloads.values()
        ),
        "network_read_count": sum(
            1 for payload in payloads.values() if payload.get("network_call_made") is True
        ),
        "live_write_called": any(
            payload.get("live_write_called") is not False for payload in payloads.values()
        ),
        "order_or_subscription_created": any(
            payload.get("order_or_subscription_created") is not False
            for payload in payloads.values()
        ),
        "prompted_user": False,
        "private_identifiers_redacted": True,
        "private_financial_data_omitted": True,
    }
    exit_code = write_secret_scanned_event(out, event)
    return 1 if status != "passed" else exit_code


def handle_live_write_refusal(out: Path) -> int:
    payload: dict[str, JsonValue] = {}
    transport_constructed = False
    try:
        with (
            _temporary_env({"SAXO_MCP_ENVIRONMENT": "LIVE"}),
            forbid_network_transport() as sentinel,
        ):
            payload = anyio.run(call_live_write_refusal_payload)
            transport_constructed = sentinel.constructed
    except NetworkTransportForbiddenError:
        transport_constructed = True
    tool_status = str(payload.get("status", "failed"))
    refused = (
        not transport_constructed
        and tool_status == "refused"
        and payload.get("refusal_reason") == "missing_live_write_enablement"
        and payload.get("network_call_made") is False
        and payload.get("live_write_called") is False
        and payload.get("order_or_subscription_created") is False
    )
    status = "refused" if refused else "failed"
    event: dict[str, JsonValue] = {
        **payload,
        **base_event("live-write-refusal", status, "FastMCP order tool refused LIVE write"),
        "tool_status": tool_status,
        "transport_constructed": transport_constructed,
        "network_call_made": bool(payload.get("network_call_made", False)),
        "live_write_called": bool(payload.get("live_write_called", False)),
        "order_or_subscription_created": bool(payload.get("order_or_subscription_created", False)),
    }
    publication = write_secret_scanned_event(out, event)
    return 0 if refused and publication == 0 else 1


def handle_tool_inventory(out: Path) -> int:
    payload = anyio.run(call_tool_inventory_payload)
    payload["git"] = current_git_state().model_dump(mode="json")
    exit_code = write_secret_scanned_event(out, payload)
    return 0 if payload["status"] == "passed" and exit_code == 0 else 1


def handle_live_read_refusal(out: Path) -> int:
    enabled = os.environ.get("SAXO_MCP_ENABLE_LIVE_READS") == "1"
    payload: dict[str, JsonValue] = {}
    fastmcp_called = False
    transport_constructed = False
    if not enabled:
        try:
            with (
                _temporary_env(
                    {
                        "SAXO_MCP_ENVIRONMENT": "LIVE",
                        "SAXO_MCP_ENABLE_LIVE_READS": None,
                    },
                ),
                forbid_network_transport() as sentinel,
            ):
                fastmcp_called = True
                payload = anyio.run(call_live_read_refusal_payload)
                transport_constructed = sentinel.constructed
        except NetworkTransportForbiddenError:
            transport_constructed = True
    refused = (
        not enabled
        and fastmcp_called
        and not transport_constructed
        and payload.get("status") == "live_not_called"
        and payload.get("reason") == "missing_live_read_enablement"
        and payload.get("network_call_made") is False
        and payload.get("live_write_called") is False
        and payload.get("order_or_subscription_created") is False
    )
    status = "refused" if refused else "failed"
    event: dict[str, JsonValue] = {
        **base_event(
            "live-read-refusal",
            status,
            "FastMCP LIVE read refused before transport construction",
        ),
        "environment": "LIVE",
        "live_reads": enabled,
        "live_writes": False,
        "scope_used": False,
        "fastmcp_tool_called": fastmcp_called,
        "transport_constructed": transport_constructed,
        "network_call_made": bool(payload.get("network_call_made", False)),
        "live_write_called": payload.get("live_write_called") is not False,
        "order_or_subscription_created": (
            payload.get("order_or_subscription_created") is not False
        ),
        "reason": str(payload.get("reason", "live_reads_enabled")),
    }
    publication = write_secret_scanned_event(out, event)
    return 0 if refused and publication == 0 else 1


def _live_probe_environ() -> dict[str, str]:
    source = dict(os.environ)
    source["SAXO_MCP_ENVIRONMENT"] = "LIVE"
    return source


def _live_read_suite_passed(statuses: Mapping[str, str]) -> bool:
    if statuses.keys() != LIVE_READ_SCENARIO_STATUSES.keys():
        return False
    return all(
        statuses.get(name) == expected
        for name, expected in LIVE_READ_SCENARIO_STATUSES.items()
    )


def _live_read_safety_passed(payloads: Mapping[str, Mapping[str, JsonValue]]) -> bool:
    return all(
        payload.get("live_write_called") is False
        and payload.get("order_or_subscription_created") is False
        for payload in payloads.values()
    )


def _live_read_coverage(statuses: Mapping[str, str]) -> dict[str, JsonValue]:
    prefix = "saxo_call_registered_endpoint_"
    return {
        "accounts": statuses.get(f"{prefix}authenticated_account") == "passed",
        "balances": statuses.get(f"{prefix}balances") == "passed",
        "positions": statuses.get(f"{prefix}positions") == "passed",
        "orders": statuses.get(f"{prefix}orders") == "passed",
        "prices": statuses.get(f"{prefix}prices") == "passed",
        "streaming": "not_applicable_to_read_only_get_tools",
    }


@contextmanager
def _temporary_env(updates: Mapping[str, str | None]) -> Generator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
