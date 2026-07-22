from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import scan_secret_paths
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.http_client import NetworkTransportForbiddenError, forbid_network_transport
from saxo_bank_mcp.live_mode import LIVE_WRITE_MISSING_REQUIREMENTS
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.qa_probes import call_live_write_refusal_payload
from saxo_bank_mcp.qa_prod_requirements import prod_readiness_requirements
from saxo_bank_mcp.server import mcp

PUBLIC_SCAN_PATHS: Final = (
    "README.md",
    "docs",
    "src",
    "tests",
    "pyproject.toml",
    ".gitignore",
)
RAPID_PROBE_ITERATIONS: Final = 16


def handle_prod_readiness(out: Path) -> int:
    rapid_probe = anyio.run(_run_rapid_probe)
    live_write_refusal_probe = _run_live_write_refusal_probe()
    requirements = prod_readiness_requirements()
    secret_findings, secret_scan_errors = scan_secret_paths(list(PUBLIC_SCAN_PATHS))
    network_call_made = live_write_refusal_probe.get("network_call_made") is not False
    live_write_called = live_write_refusal_probe.get("live_write_called") is not False
    order_or_subscription_created = (
        live_write_refusal_probe.get("order_or_subscription_created") is not False
    )
    transport_constructed = live_write_refusal_probe.get("transport_constructed") is not False
    failed = bool(
        secret_findings
        or secret_scan_errors
        or rapid_probe["failures"]
        or live_write_refusal_probe.get("status") != "refused"
        or live_write_refusal_probe.get("refusal_reason") != "missing_live_write_enablement"
        or transport_constructed
        or network_call_made
        or live_write_called
        or order_or_subscription_created
    )
    report: dict[str, JsonValue] = {
        **base_event(
            "prod-readiness",
            "failed" if failed else "passed",
            "Saxo code safety checks, safe rapid-call probe, and public secret scan",
        ),
        "status_scope": "code_safety_checks_only",
        "code_safety_checks_passed": not failed,
        "production_ready": False,
        "requirement_count": len(requirements),
        "requirements": requirements,
        "rapid_call_probe": rapid_probe,
        "live_write_refusal_probe": live_write_refusal_probe,
        "secret_scan": {
            "paths": list(PUBLIC_SCAN_PATHS),
            "findings": secret_findings,
            "scan_errors": secret_scan_errors,
        },
        "live_read_ready": False,
        "live_read_ready_scope": "not_evaluated_by_prod_readiness",
        "live_read_evidence_required": "use the separate live-read probe artifact",
        "live_write_ready": False,
        "live_write_missing_requirements": live_write_refusal_probe["missing_requirements"],
        "transport_constructed": transport_constructed,
        "network_call_made": network_call_made,
        "live_write_called": live_write_called,
        "order_or_subscription_created": order_or_subscription_created,
        "verifies": [
            "public MCP code does not expose AppSecret or RefreshToken values",
            "safe MCP tools tolerate rapid repeated calls without hanging",
            "LIVE writes are refused before network until every real-money gate exists",
        ],
        "does_not_verify": [
            "PKCE browser login completes",
            "LIVE read credentials are valid; this command does not consume live-read evidence",
            "LIVE order placement works",
            "Saxo platform visual parity for every instrument",
            "exchange-specific legal approval",
        ],
        "next_action": (
            "use the live-read artifact for read proof, then create the separate "
            "live-write enablement gate before any real-money write"
        ),
    }
    published = write_scanned_json(out, report)
    return 1 if failed or not published else 0


def _run_live_write_refusal_probe() -> dict[str, JsonValue]:
    previous_environment = os.environ.get("SAXO_MCP_ENVIRONMENT")
    os.environ["SAXO_MCP_ENVIRONMENT"] = "LIVE"
    payload: dict[str, JsonValue] = {}
    transport_constructed = False
    try:
        with forbid_network_transport() as sentinel:
            payload = anyio.run(call_live_write_refusal_payload)
            transport_constructed = sentinel.constructed
    except NetworkTransportForbiddenError:
        transport_constructed = True
    finally:
        if previous_environment is None:
            os.environ.pop("SAXO_MCP_ENVIRONMENT", None)
        else:
            os.environ["SAXO_MCP_ENVIRONMENT"] = previous_environment
    return {
        "status": str(payload.get("status", "failed")),
        "refusal_reason": str(payload.get("refusal_reason", "")),
        "missing_requirements": list(LIVE_WRITE_MISSING_REQUIREMENTS),
        "transport_constructed": transport_constructed,
        "network_call_made": payload.get("network_call_made") is not False,
        "live_write_called": payload.get("live_write_called") is not False,
        "order_or_subscription_created": (
            payload.get("order_or_subscription_created") is not False
        ),
    }


async def _run_rapid_probe() -> dict[str, JsonValue]:
    failures: list[dict[str, JsonValue]] = []
    async with Client(mcp) as client:
        for index in range(RAPID_PROBE_ITERATIONS):
            result = await client.call_tool("saxo_health", {})
            payload = result.structured_content
            if not isinstance(payload, dict):
                failures.append({"iteration": index, "reason": "non_object_payload"})
                continue
            if payload.get("status") != "passed":
                failures.append(
                    {
                        "iteration": index,
                        "reason": "unexpected_status",
                        "status": str(payload.get("status")),
                    },
                )
    return {
        "status": "passed" if not failures else "failed",
        "tool_name": "saxo_health",
        "iterations": RAPID_PROBE_ITERATIONS,
        "network_call_made": False,
        "order_or_subscription_created": False,
        "failures": failures,
    }
