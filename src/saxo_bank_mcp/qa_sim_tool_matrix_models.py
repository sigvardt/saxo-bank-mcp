from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SIM_ENDPOINTS

NON_EXECUTABLE_SIM: Final = frozenset({"saxo_list_live_accounts", "saxo_precheck_live_order"})
SIM_GATEWAY_HOST: Final = urlparse(SIM_ENDPOINTS.rest_base_url).hostname or "gateway.saxobank.com"
FIXTURE_STREAM_UIC: Final = 21
HTTP_CLIENT_ERROR_MIN: Final = 400
RATE_LIMIT_STATUSES: Final = frozenset({429, 503})
RATE_LIMIT_CODES: Final = frozenset({"RateLimitExceeded", "ServiceUnavailable"})
DISCLAIMER_RETRY_DELAYS_SECONDS: Final = (2.0, 5.0, 10.0, 20.0)


class MatrixScenarioReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    status: Literal["completed", "expected_refusal"]
    mcp_call_observed: Literal[True] = True
    result_parsed: Literal[True] = True
    skipped: Literal[False] = False
    requested_tool_covered: Literal[True] = True
    network_call_made: bool = False
    hosts: tuple[str, ...] = ()
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    call_path: Literal["fastmcp.Client.call_tool"] = "fastmcp.Client.call_tool"


class SimToolMatrixReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "blocked"]
    environment: Literal["SIM"] = "SIM"
    reason: str = ""
    tool_receipts: tuple[MatrixScenarioReceipt, ...]
    lifecycle_calls: tuple[str, ...]
    registered_trading_write_ops: tuple[str, ...]
    disclaimer_response_completed: bool
    fixture_reference_validated: bool
    account_allowlist_resolved: bool
    auth_status_completed: bool
    session_capabilities_completed: bool
    before_state_fingerprint: dict[str, JsonValue]
    after_state_fingerprint: dict[str, JsonValue]
    uncleaned_resources: int
    hosts: tuple[str, ...]
    live_events: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatrixFixtures:
    stock_uic: int
    amount: float
    limit_price: float
    modified_limit_price: float
    option_uics: tuple[int, ...]
    stream_uic: int


@dataclass(frozen=True, slots=True)
class MatrixCliFixtures:
    stock_uic: str
    amount: str
    limit_price: str
    modified_limit_price: str
    option_uics: str
    stream_uic: str


@dataclass(frozen=True, slots=True)
class PreflightFlags:
    fixtures_ok: bool
    account_ok: bool
    auth_ok: bool
    session_ok: bool
    disclaimer_ok: bool = False


@dataclass(slots=True)
class MatrixRuntimeState:
    errors: list[str]
    hosts: set[str]
    live_events: int
    receipts: dict[str, MatrixScenarioReceipt]
    lifecycle_seen: set[str]
    registered_ops: list[str]
    uncleaned: int
    preflight: PreflightFlags
    before: dict[str, JsonValue]
    after: dict[str, JsonValue]
