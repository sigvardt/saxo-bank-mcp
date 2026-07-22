from __future__ import annotations

from typing import Annotated, Final

from fastmcp.tools import ToolResult
from pydantic import Field, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.trading_write_execution import (
    execute_trading_write,
    prepare_trading_write,
)
from saxo_bank_mcp.trading_write_registry import trading_write_specs
from saxo_bank_mcp.trading_write_state import TradingWriteRequest

TRADING_WRITE_LIST_DESCRIPTION: Final = (
    "Lists every current non-GET Saxo Trading operation and whether it uses the specialized "
    "order flow or the registered write gateway. This is local metadata and makes no network call."
)
TRADING_WRITE_PREPARE_DESCRIPTION: Final = (
    "Creates a short-lived, exact-request preview for a registered Saxo Trading write. SIM "
    "previews need no human approval. LIVE previews return one exact approval statement that the "
    "human must send in the agent chat; no second person or second approval factor is required. "
    "Order mutations must use the specialized precheck flow and cannot bypass it here."
)
TRADING_WRITE_EXECUTE_DESCRIPTION: Final = (
    "Executes one prepared registered Trading write once. SIM runs autonomously. LIVE requires "
    "the exact single-use approval statement from the human in the agent chat, bound to the "
    "request fingerprint. Mutations are never transport-retried automatically."
)


def saxo_list_trading_write_operations() -> ToolResult:
    specs = trading_write_specs()
    operations: list[JsonValue] = [
        {
            "operation_id": spec.operation_id,
            "method": spec.method,
            "path_template": spec.path_template,
            "service": spec.service,
            "risk_class": spec.risk,
            "specialized_tool": spec.specialized_tool,
            "cleanup_rule": spec.cleanup_rule,
            "documentation_url": spec.documentation_url,
            "path_parameter_names": list(spec.path_parameter_names),
            "query_parameter_names": list(spec.query_parameter_names),
            "required_query_parameter_names": list(spec.required_query_parameter_names),
        }
        for spec in specs
    ]
    payload: dict[str, JsonValue] = {
        "status": "passed",
        "tool_name": "saxo_list_trading_write_operations",
        "operation_count": len(specs),
        "operations": operations,
        "unclassified_operation_ids": [],
        "live_approval_mode": "one_exact_action_chat_approval",
        "sim_human_approval_required": False,
        "network_call_made": False,
    }
    return ToolResult(structured_content=payload)


def saxo_prepare_trading_write(  # noqa: PLR0913
    operation_id: Annotated[str, Field(description="Operation id from the write-operation list")],
    path_parameters: Annotated[
        dict[str, str] | None,
        Field(description="Named route parameters; submitted values are never echoed"),
    ] = None,
    query_parameters: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Documented Saxo query parameters"),
    ] = None,
    request_body: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Exact documented Saxo request body"),
    ] = None,
    account_key: Annotated[
        str | None,
        Field(description="Target account key for safety binding; never echoed"),
    ] = None,
    instrument_uic: Annotated[
        int | None,
        Field(gt=0, description="Target instrument UIC for money-moving writes"),
    ] = None,
    quantity: Annotated[
        float | None,
        Field(gt=0, description="Quantity for money-moving safety limits"),
    ] = None,
    estimated_notional: Annotated[
        float | None,
        Field(ge=0, description="Estimated account-currency notional for safety limits"),
    ] = None,
) -> ToolResult:
    try:
        request = TradingWriteRequest(
            operation_id=operation_id,
            path_parameters={} if path_parameters is None else path_parameters,
            query_parameters={} if query_parameters is None else query_parameters,
            request_body={} if request_body is None else request_body,
            account_key=account_key,
            instrument_uic=instrument_uic,
            quantity=quantity,
            estimated_notional=estimated_notional,
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        return ToolResult(
            content="trading write preview: denied; reason=invalid_request",
            structured_content={
                "status": "denied",
                "tool_name": "saxo_prepare_trading_write",
                "operation_id": operation_id,
                "denial_reason": "invalid_request",
                "validation_errors": fields,
                "network_call_made": False,
                "mutation_may_have_occurred": False,
            },
            is_error=True,
        )
    return prepare_trading_write(request)


async def saxo_execute_trading_write(
    preview_token: Annotated[
        str,
        Field(description="Sensitive short-lived token from saxo_prepare_trading_write"),
    ],
    approval_statement: Annotated[
        str | None,
        Field(
            description=(
                "LIVE only: exact approval_prompt sent by the human in agent chat; omit in SIM"
            ),
        ),
    ] = None,
) -> ToolResult:
    return await execute_trading_write(preview_token, approval_statement)
