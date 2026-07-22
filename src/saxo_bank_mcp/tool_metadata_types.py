from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

type ToolEnvironment = Literal["SIM", "LIVE_READ", "LIVE_WRITE", "LOCAL"]
type WriteEffect = Literal[
    "none",
    "local_state",
    "sim_network",
    "sim_streaming",
    "live_network",
]


class ToolMetadata(TypedDict):
    tool_class: str
    environment_support: list[ToolEnvironment]
    write_effect: WriteEffect
    state_changing: bool
    safe_in_live_read_mode: bool
    agent_hint: str
    endpoint_operation_id: NotRequired[str]
    endpoint_inventory_class: NotRequired[str]
