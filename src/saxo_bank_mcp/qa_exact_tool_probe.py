from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SaxoEnvironment, SaxoRuntimeConfig
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.server import mcp

PROBE_ARGUMENT_NAME: Final = "__qa_exact_tool_probe__"
RESULT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
type ProbeResultStatus = Literal["invalid_arguments", "invalid_request", "refused"]


class ExactToolProbeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    logical_tool: str = Field(min_length=1, pattern=r"^saxo_")
    fastmcp_called: Literal[True]
    call_path: Literal["SafeFastMCP.call_tool"]
    fastmcp_result_status: ProbeResultStatus
    fastmcp_is_error: Literal[True]
    input_rejected_before_execution: Literal[True]
    input_source: Literal["schema_extra_rejection", "typed_live_precheck_fixture"]
    environment: Literal["SIM"]
    client_used: Literal[False]
    mcp_transport_used: Literal[False]
    network_call_made: Literal[False]
    mutation_attempted: Literal[False]
    order_or_subscription_created: Literal[False]


def handle_exact_tool_probe(out: Path, tool: str) -> int:
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.requested_environment is not SaxoEnvironment.SIM:
        return _write_failure(out, "environment_not_sim")
    receipt = anyio.run(_call_exact_tool, tool)
    if receipt is None:
        return _write_failure(out, "exact_tool_call_not_proven")
    return 0 if write_scanned_json(out, receipt.model_dump(mode="json")) else 1


async def _call_exact_tool(tool: str) -> ExactToolProbeReceipt | None:
    registered = await mcp.get_tool(tool)
    if registered is None or registered.parameters.get("additionalProperties") is not False:
        return None
    logical_tool = registered.name
    input_source, arguments = _probe_arguments(logical_tool)
    result = await mcp.call_tool(logical_tool, arguments)
    payload = RESULT_ADAPTER.validate_python(result.structured_content)
    result_status = _probe_result_status(payload.get("status"))
    if result_status is None:
        return None
    if result.is_error is not True or payload.get("network_call_made") is True:
        return None
    return ExactToolProbeReceipt(
        status="passed",
        logical_tool=logical_tool,
        fastmcp_called=True,
        call_path="SafeFastMCP.call_tool",
        fastmcp_result_status=result_status,
        fastmcp_is_error=True,
        input_rejected_before_execution=True,
        input_source=input_source,
        environment="SIM",
        client_used=False,
        mcp_transport_used=False,
        network_call_made=False,
        mutation_attempted=False,
        order_or_subscription_created=False,
    )


def _probe_arguments(
    tool: str,
) -> tuple[Literal["schema_extra_rejection", "typed_live_precheck_fixture"], dict[str, JsonValue]]:
    if tool == "saxo_precheck_live_order":
        return (
            "typed_live_precheck_fixture",
            {
                "order": {
                    "uic": 211,
                    "asset_type": "Stock",
                    "amount": 1,
                    "buy_sell": "Buy",
                },
            },
        )
    return "schema_extra_rejection", {PROBE_ARGUMENT_NAME: True}


def _probe_result_status(value: JsonValue | None) -> ProbeResultStatus | None:
    match value:
        case "invalid_arguments":
            return "invalid_arguments"
        case "invalid_request":
            return "invalid_request"
        case "refused":
            return "refused"
        case _:
            return None


def _write_failure(out: Path, reason: str) -> int:
    write_scanned_json(out, {"status": "failed", "reason": reason})
    return 1
