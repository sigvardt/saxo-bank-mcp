from __future__ import annotations

from typing import Annotated, Final

from fastmcp.tools import ToolResult
from pydantic import Field

from saxo_bank_mcp.streaming_execution import (
    StreamingSubscriptionInput,
    execute_streaming_cleanup,
    execute_streaming_price_subscription,
)

STREAMING_TOOL_DESCRIPTION: Final = (
    "Creates a SIM Saxo price streaming subscription only with a cached SIM token, then opens "
    "the new sim-streaming.saxobank.com/sim/oapi/streaming/ws Saxo WebSocket endpoint using "
    "an Authorization header. It surfaces 4 simultaneous streaming connections as Saxo's "
    "current expected connection limit and 200 price instruments as the current expected "
    "price limit. "
    "It never puts tokens in URLs and does not claim completion unless a REST snapshot and "
    "a non-control WebSocket data frame are both observed; control-only frames stay incomplete. "
    "Agents may pass last_message_id to reconnect with Saxo's messageid cursor."
)
STREAMING_CLEANUP_TOOL_DESCRIPTION: Final = (
    "Deletes local streaming registry entries for a ContextId and, when a cached SIM token "
    "exists, calls Saxo SIM root subscription cleanup for subscriptions opened through "
    "sim-streaming.saxobank.com/sim/oapi/streaming/ws. It uses an Authorization header, keeps "
    "Saxo's 4 simultaneous streaming connections and 200 price instruments limits visible to "
    "agents, reports local registry state separately from remote Saxo cleanup confirmation, "
    "never calls LIVE endpoints, and never puts bearer tokens in URLs or evidence."
)


async def saxo_create_streaming_price_subscription(  # noqa: PLR0913
    context_id: Annotated[str, Field(description="Saxo streaming ContextId, max 50 chars")],
    reference_id: Annotated[str, Field(description="Saxo data ReferenceId, max 50 chars")],
    uics: Annotated[list[int], Field(description="Instrument UICs, up to 200")],
    asset_type: Annotated[str, Field(description="Saxo AssetType, for example Stock")],
    wait_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0,
    last_message_id: Annotated[
        int | None,
        Field(ge=0, description="Optional Saxo streaming messageid reconnect cursor"),
    ] = None,
) -> ToolResult:
    return await execute_streaming_price_subscription(
        StreamingSubscriptionInput(
            context_id=context_id,
            reference_id=reference_id,
            uics=uics,
            asset_type=asset_type,
            wait_seconds=wait_seconds,
            last_message_id=last_message_id,
        ),
    )


async def saxo_cleanup_streaming_subscriptions(
    context_id: Annotated[str, Field(description="Saxo streaming ContextId to delete")],
) -> ToolResult:
    return await execute_streaming_cleanup(context_id)
