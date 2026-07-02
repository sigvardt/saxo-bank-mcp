from __future__ import annotations

from pathlib import Path

import anyio
from fastmcp import Client

from saxo_bank_mcp._evidence import write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.qa_streaming_payloads import (
    JsonObject,
    StreamCleanupProbeOptions,
    StreamProbeOptions,
    cleanup_event_payload,
    parse_tool_payload,
    stream_event_payload,
    stream_limit_check,
)
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.streaming import (
    PRICE_SUBSCRIPTION_OPERATION_ID,
    PRICE_SUBSCRIPTION_PATH,
    register_local_subscription,
    reset_local_subscriptions,
)


def handle_stream(
    out: Path,
    *,
    require_frame: bool,
    expect_connections: int | None,
    expect_price_instruments: int | None,
) -> int:
    payload = anyio.run(
        stream_probe,
        StreamProbeOptions(
            require_frame=require_frame,
            expect_connections=expect_connections,
            expect_price_instruments=expect_price_instruments,
        ),
    )
    return write_redacted_with_secret_scan(
        out,
        payload,
        ("passed", "incomplete_auth_required"),
    )


def handle_stream_cleanup(out: Path, *, simulate_leak: bool) -> int:
    payload = anyio.run(
        stream_cleanup_probe,
        StreamCleanupProbeOptions(simulate_leak=simulate_leak),
    )
    return write_redacted_with_secret_scan(out, payload, ("passed", "incomplete_auth_required"))


async def stream_probe(options: StreamProbeOptions) -> JsonObject:
    reset_local_subscriptions()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_streaming_price_subscription",
            {
                "context_id": "task8ctx",
                "reference_id": "task8prices",
                "uics": [21],
                "asset_type": "Stock",
                "wait_seconds": 2.0,
            },
            raise_on_error=False,
        )
    tool_payload = parse_tool_payload(result.structured_content)
    limit_check = stream_limit_check(options, tool_payload)
    return stream_event_payload(options, tool_payload, limit_check)


async def stream_cleanup_probe(options: StreamCleanupProbeOptions) -> JsonObject:
    reset_local_subscriptions()
    if options.simulate_leak:
        register_local_subscription(
            context_id="task8leak",
            reference_id="Price_QA",
            operation_id=PRICE_SUBSCRIPTION_OPERATION_ID,
            endpoint_path=PRICE_SUBSCRIPTION_PATH,
        )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_cleanup_streaming_subscriptions",
            {"context_id": "task8leak"},
            raise_on_error=False,
        )
    tool_payload = parse_tool_payload(result.structured_content)
    return cleanup_event_payload(options, tool_payload)


def write_redacted_with_secret_scan(
    out: Path,
    payload_value: JsonObject,
    success_statuses: tuple[str, ...],
) -> int:
    redacted = redact_json(payload_value)
    if not isinstance(redacted, dict):
        raise TypeError("streaming probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") in success_statuses and clean else 1
