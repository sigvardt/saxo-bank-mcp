from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import scan_secret_paths
from saxo_bank_mcp.live_mode import LIVE_WRITE_MISSING_REQUIREMENTS
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.qa_probes import call_live_write_refusal_payload
from saxo_bank_mcp.server import mcp

PUBLIC_SCAN_PATHS: Final = ("README.md", "docs", "src", "tests", ".gitignore")
RAPID_PROBE_ITERATIONS: Final = 16


def handle_prod_readiness(out: Path) -> int:
    rapid_probe = anyio.run(_run_rapid_probe)
    live_write_refusal_probe = _run_live_write_refusal_probe()
    secret_findings, secret_scan_errors = scan_secret_paths(list(PUBLIC_SCAN_PATHS))
    failed = bool(
        secret_findings
        or secret_scan_errors
        or rapid_probe["failures"]
        or live_write_refusal_probe["status"] != "refused"
        or live_write_refusal_probe["network_call_made"]
        or live_write_refusal_probe["order_or_subscription_created"]
    )
    report: dict[str, JsonValue] = {
        **base_event(
            "prod-readiness",
            "failed" if failed else "passed",
            "Saxo live-access checklist, safe rapid-call probe, and public secret scan",
        ),
        "requirement_count": len(_requirements()),
        "requirements": _requirements(),
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
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
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
    write_json(out, report)
    return 1 if failed else 0


def _run_live_write_refusal_probe() -> dict[str, JsonValue]:
    previous_environment = os.environ.get("SAXO_MCP_ENVIRONMENT")
    os.environ["SAXO_MCP_ENVIRONMENT"] = "LIVE"
    try:
        payload = anyio.run(call_live_write_refusal_payload)
    finally:
        if previous_environment is None:
            os.environ.pop("SAXO_MCP_ENVIRONMENT", None)
        else:
            os.environ["SAXO_MCP_ENVIRONMENT"] = previous_environment
    return {
        "status": str(payload.get("status", "failed")),
        "refusal_reason": str(payload.get("refusal_reason", "")),
        "missing_requirements": list(LIVE_WRITE_MISSING_REQUIREMENTS),
        "network_call_made": bool(payload.get("network_call_made", True)),
        "live_write_called": bool(payload.get("live_write_called", True)),
        "order_or_subscription_created": bool(
            payload.get("order_or_subscription_created", True),
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


def _requirements() -> list[dict[str, JsonValue]]:
    return [
        _requirement(
            "public_secret_containment",
            "implemented",
            "No AppSecret or RefreshToken belongs in public code or generated evidence.",
            ["src/saxo_bank_mcp/_redaction.py", "src/saxo_bank_mcp/token_cache.py"],
        ),
        _requirement(
            "pkce_saxo_login",
            "implemented",
            "Public login uses Saxo OAuth with PKCE and never asks agents to "
            "intercept credentials.",
            ["src/saxo_bank_mcp/oauth.py", "src/saxo_bank_mcp/mcp_auth_tools.py"],
        ),
        _requirement(
            "refresh_token_auth_server_only",
            "implemented",
            "Refresh tokens are sent only to the configured Saxo token endpoint.",
            ["src/saxo_bank_mcp/oauth.py"],
        ),
        _requirement(
            "public_secret_scan",
            "implemented",
            "The readiness command scans public paths for tokens and credentials.",
            ["src/saxo_bank_mcp/_redaction.py"],
        ),
        _requirement(
            "monkey_rapid_calls",
            "implemented",
            "The readiness command rapidly repeats a safe MCP health call with "
            "no network or trading write.",
            ["src/saxo_bank_mcp/qa_prod_readiness.py"],
        ),
        _requirement(
            "openapi_400_investigation",
            "implemented",
            "Order mutation responses preserve Saxo error codes for investigation evidence.",
            ["src/saxo_bank_mcp/order_mutation_models.py"],
        ),
        _requirement(
            "throttling_409_429",
            "implemented",
            "HTTP 429 is rate-limited, and HTTP 409 is duplicate-submit evidence.",
            ["src/saxo_bank_mcp/order_mutation_models.py", "src/saxo_bank_mcp/safety.py"],
        ),
        _requirement(
            "many_positions_orders",
            "implemented",
            "Readback code scans order collections recursively instead of assuming one row.",
            ["src/saxo_bank_mcp/order_mutation_execution.py"],
        ),
        _requirement(
            "currency_and_price_display",
            "evidence_required_live",
            "Read tools preserve Saxo response metadata; full display parity "
            "needs LIVE account evidence.",
            ["src/saxo_bank_mcp/read_tools.py", "src/saxo_bank_mcp/trade_preview.py"],
        ),
        _requirement(
            "unexpected_instruments_assets",
            "implemented",
            "Saxo-facing JSON is accepted as raw objects so added fields and "
            "new asset strings do not crash parsing.",
            ["src/saxo_bank_mcp/endpoint_registry.py", "src/saxo_bank_mcp/read_tools.py"],
        ),
        _requirement(
            "fractional_amounts",
            "implemented",
            "Order and preview paths accept JSON numeric quantities as floats "
            "and compare them with tolerance.",
            ["src/saxo_bank_mcp/order_mutation_guards.py", "src/saxo_bank_mcp/trade_preview.py"],
        ),
        _requirement(
            "all_order_mutation_shapes",
            "implemented",
            "Place, modify, cancel, related-order, and multileg write classes "
            "are registered and QA-covered.",
            ["src/saxo_bank_mcp/order_mutation_models.py", "tests/test_order_mutations.py"],
        ),
        _requirement(
            "invalid_order_prevention",
            "implemented",
            "Preview tokens, account allowlists, quantity/notional limits, and "
            "request-body checks stop invalid writes.",
            [
                "src/saxo_bank_mcp/safety.py",
                "src/saxo_bank_mcp/safety_checks.py",
                "src/saxo_bank_mcp/order_mutation_guards.py",
            ],
        ),
        _requirement(
            "automated_trading_limits",
            "implemented",
            "Automated write paths require preview plus approval factors and "
            "enforce configured size limits.",
            ["src/saxo_bank_mcp/safety.py", "src/saxo_bank_mcp/safety_models.py"],
        ),
        _requirement(
            "versioning_tolerance",
            "implemented",
            "Saxo response models avoid strict enum and extra-field rejection at the API edge.",
            ["src/saxo_bank_mcp/read_tools.py", "src/saxo_bank_mcp/endpoint_registry.py"],
        ),
        _requirement(
            "sim_before_live",
            "implemented",
            "SIM QA and final verification remain separate from LIVE read/write enablement.",
            ["src/saxo_bank_mcp/qa.py", "src/saxo_bank_mcp/final_verify_code.py"],
        ),
        _requirement(
            "live_write_refusal",
            "refused_until_live_enablement",
            "LIVE write tools refuse before network until the real-money enablement plan exists.",
            ["src/saxo_bank_mcp/live_mode.py", "src/saxo_bank_mcp/order_mutation_execution.py"],
        ),
        _requirement(
            "live_read_credentials",
            "evidence_required_live",
            "LIVE reads require approved LIVE credentials and a LIVE token cache "
            "outside the repository.",
            ["src/saxo_bank_mcp/live_mode.py", "src/saxo_bank_mcp/qa_probes.py"],
        ),
    ]


def _requirement(
    requirement_id: str,
    status: str,
    summary: str,
    evidence_refs: list[str],
) -> dict[str, JsonValue]:
    return {
        "id": requirement_id,
        "status": status,
        "summary": summary,
        "evidence_refs": evidence_refs,
    }
