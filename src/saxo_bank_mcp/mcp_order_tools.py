from __future__ import annotations

from typing import Annotated, Final

from fastmcp.tools import ToolResult
from pydantic import Field

from saxo_bank_mcp.order_mutation_execution import execute_sim_order_write

ORDER_WRITE_TOOL_DESCRIPTION: Final = (
    "Executes a SIM-only Saxo order write from a local preview token after a separate approval "
    "factor. It never calls LIVE endpoints. It must not be used unless the preview was created "
    "from the matching pre-check or current-order preview; missing approval is denied before "
    "network, and missing SIM token is reported without consuming the preview."
)


async def saxo_place_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("place", preview_token, approval_factor)


async def saxo_modify_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
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
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
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
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("cancel-by-instrument", preview_token, approval_factor)


async def saxo_place_multileg_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("multileg-place", preview_token, approval_factor)


async def saxo_modify_multileg_sim_order(
    preview_token: Annotated[str, Field(description="Sensitive preview token from pre-check")],
    approval_factor: Annotated[
        str | None,
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
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
        Field(description="Separate approval factor; SIM tests use the test-only factor"),
    ] = None,
) -> ToolResult:
    return await execute_sim_order_write("multileg-cancel", preview_token, approval_factor)
