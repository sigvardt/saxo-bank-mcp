from __future__ import annotations

from typing import Annotated, Final

from fastmcp.tools import ToolResult
from pydantic import Field

from saxo_bank_mcp.order_mutation_execution import execute_order_write, execute_sim_order_write

ORDER_WRITE_TOOL_DESCRIPTION: Final = (
    "Executes a SIM-only Saxo order write from a local preview token without human approval. "
    "It never calls LIVE endpoints. It must not be used unless the preview was created "
    "from the matching pre-check or current-order preview. A missing SIM token is reported "
    "without consuming the preview. Future LIVE tools use one exact-action approval statement "
    "sent by the human in the agent chat."
)
PRODUCTION_ORDER_WRITE_TOOL_DESCRIPTION: Final = (
    "Executes an exact previewed Saxo order write in the configured environment. Place requests "
    "must use saxo_create_order_preview for Saxo precheck. Modify and cancel requests use "
    "saxo_create_write_preview to bind the exact current action. SIM needs no "
    "human approval. LIVE requires the exact single-use approval_prompt sent by the human in "
    "agent chat and bound to the preview fingerprint. Place and modify requests must use "
    "ManualOrder=true after that human confirmation. Unknown-state results must never be retried "
    "without order and trade-message readback."
)


async def saxo_place_order(
    preview_token: Annotated[str, Field(description="Sensitive token from order precheck")],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "place",
        preview_token,
        approval_statement,
        "saxo_place_order",
    )


async def saxo_modify_order(
    preview_token: Annotated[str, Field(description="Sensitive token from order precheck")],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "modify",
        preview_token,
        approval_statement,
        "saxo_modify_order",
    )


async def saxo_cancel_order(
    preview_token: Annotated[
        str,
        Field(description="Sensitive token from current-order cancel preview"),
    ],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "cancel",
        preview_token,
        approval_statement,
        "saxo_cancel_order",
    )


async def saxo_cancel_orders_by_instrument(
    preview_token: Annotated[
        str,
        Field(description="Sensitive token from current-order cancel preview"),
    ],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "cancel-by-instrument",
        preview_token,
        approval_statement,
        "saxo_cancel_orders_by_instrument",
    )


async def saxo_place_multileg_order(
    preview_token: Annotated[str, Field(description="Sensitive token from multileg precheck")],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "multileg-place",
        preview_token,
        approval_statement,
        "saxo_place_multileg_order",
    )


async def saxo_modify_multileg_order(
    preview_token: Annotated[str, Field(description="Sensitive token from multileg precheck")],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "multileg-modify",
        preview_token,
        approval_statement,
        "saxo_modify_multileg_order",
    )


async def saxo_cancel_multileg_order(
    preview_token: Annotated[
        str,
        Field(description="Sensitive token from current multileg-order cancel preview"),
    ],
    approval_statement: Annotated[
        str | None,
        Field(description="LIVE only: exact human approval_prompt; omit in SIM"),
    ] = None,
) -> ToolResult:
    return await execute_order_write(
        "multileg-cancel",
        preview_token,
        approval_statement,
        "saxo_cancel_multileg_order",
    )


async def saxo_place_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("place", preview_token, approval_factor)


async def saxo_modify_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("modify", preview_token, approval_factor)


async def saxo_cancel_sim_order(
    preview_token: Annotated[
        str,
        Field(description="Sensitive preview token from current-order cancel preview"),
    ],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("cancel", preview_token, approval_factor)


async def saxo_cancel_sim_orders_by_instrument(
    preview_token: Annotated[
        str,
        Field(description="Sensitive preview token from current-order cancel preview"),
    ],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("cancel-by-instrument", preview_token, approval_factor)


async def saxo_place_multileg_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("multileg-place", preview_token, approval_factor)


async def saxo_modify_multileg_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("multileg-modify", preview_token, approval_factor)


async def saxo_cancel_multileg_sim_order(
    preview_token: Annotated[
        str,
        Field(description="Sensitive preview token from current-order cancel preview"),
    ],
    approval_factor: Annotated[
        str | None,
        Field(description="Deprecated compatibility value; SIM does not require approval"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("multileg-cancel", preview_token, approval_factor)
