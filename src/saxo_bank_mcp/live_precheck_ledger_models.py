from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SafeLedgerEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    timestamp: str = Field(min_length=1)
    phase: Literal["attempted", "completed"]
    host_role: Literal["gateway", "oauth", "other"]
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    query_names: tuple[str, ...] = Field(strict=False)
    query_present: bool
    status: int | None


class SafeLedgerReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    status: Literal["passed"]
    scope: Literal["current_mcp_session"]
    safe_fields_only: Literal[True]
    ledger_complete: bool
    events_evicted: int = Field(ge=0)
    negative_proof_available: bool
    only_precheck_gateway_non_get: bool
    unsafe_gateway_request_detected: bool | None
    order_placement_endpoint_called: bool | None
    events: tuple[SafeLedgerEvent, ...] = Field(strict=False)
