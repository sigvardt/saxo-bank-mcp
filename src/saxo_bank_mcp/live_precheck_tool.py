from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from fastmcp.tools import Tool, ToolResult
from pydantic import BaseModel, ConfigDict, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.fastmcp_logging_safety import generic_tool_error_result
from saxo_bank_mcp.live_precheck_request import LiveOrderPrecheckRequest
from saxo_bank_mcp.live_precheck_results import (
    LIVE_PRECHECK_TOOL_NAME,
    common_result,
    tool_result,
)
from saxo_bank_mcp.mcp_live_trade_tools import (
    LIVE_PRECHECK_TOOL_DESCRIPTION,
    saxo_precheck_live_order,
)

_TOOL_EXECUTION_ERRORS: Final[tuple[type[Exception], ...]] = (Exception,)
_SAFE_VALIDATION_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "account_selector_conflict": "Provide either account_id or account_ref, not both.",
        "extra_forbidden": "Remove this unrecognized field.",
        "finite_number": "Use a finite number.",
        "float_type": "Use a finite number.",
        "greater_than": "Use a value greater than zero.",
        "int_type": "Use an integer.",
        "literal_error": "Use one of the values allowed by the schema.",
        "missing": "Add this required field.",
        "model_type": "Use an object matching the advertised order schema.",
        "string_pattern_mismatch": "Use the format allowed by the schema.",
        "string_too_short": "Use a non-empty string.",
        "string_type": "Use a string.",
    },
)


class LivePrecheckArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    order: LiveOrderPrecheckRequest


LIVE_PRECHECK_INPUT_SCHEMA: Final = LivePrecheckArguments.model_json_schema()


class LivePrecheckTool(Tool):
    async def run(self, arguments: dict[str, JsonValue]) -> ToolResult:
        try:
            parsed = LivePrecheckArguments.model_validate(arguments)
        except ValidationError as validation_error:
            return tool_result(
                {
                    **common_result("invalid_request", network_call_made=False),
                    "reason": "request_schema_invalid",
                    "validation_errors": _safe_validation_errors(validation_error),
                    "next_action": (
                        "Correct the order object to match the advertised schema, then retry."
                    ),
                    "precheck_request_accepted": False,
                },
                is_error=True,
            )
        try:
            return await saxo_precheck_live_order(parsed.order)
        except _TOOL_EXECUTION_ERRORS:
            return generic_tool_error_result()


def create_live_precheck_tool() -> Tool:
    return LivePrecheckTool(
        name=LIVE_PRECHECK_TOOL_NAME,
        description=LIVE_PRECHECK_TOOL_DESCRIPTION,
        parameters=LIVE_PRECHECK_INPUT_SCHEMA,
    )


def _safe_validation_errors(error: ValidationError) -> list[JsonValue]:
    return [
        {
            "location": _safe_location(
                validation_error["loc"],
                validation_error["type"],
            ),
            "type": validation_error["type"],
            "message": _safe_validation_message(validation_error["type"]),
        }
        for validation_error in error.errors(
            include_input=False,
            include_context=False,
            include_url=False,
        )
    ]


def _safe_location(location: tuple[int | str, ...], error_type: str) -> list[str | int]:
    safe_location = list(location)
    if error_type == "extra_forbidden" and safe_location:
        safe_location[-1] = "<extra>"
    return safe_location


def _safe_validation_message(error_type: str) -> str:
    return _SAFE_VALIDATION_MESSAGES.get(
        error_type,
        "Correct this field to match the advertised schema.",
    )
