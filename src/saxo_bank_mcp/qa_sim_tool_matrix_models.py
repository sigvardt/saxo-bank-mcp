from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SIM_ENDPOINTS
from saxo_bank_mcp.server_tool_ids import ANALYTICS_TOOL_IDS, EXPECTED_TOOL_COUNT

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
    # Optional here for compatibility with the pre-Task-23 matrix producer
    # receipt. The final analytics proof bundle requires every field below.
    live_mutation_calls: int | None = Field(default=None, ge=0)
    analytics_tool_receipt_count: int | None = Field(default=None, ge=0)
    analytics_case_contract_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    cleanup_complete: bool | None = None
    account_state_unchanged: bool | None = None
    redacted_publication: bool | None = None
    purchase_occurred: Literal[False] = False
    errors: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_pass_claim(self) -> SimToolMatrixReceipt:
        if self.status == "passed" and (
            self.reason != ""
            or len(self.tool_receipts) != EXPECTED_TOOL_COUNT
            or len({receipt.tool for receipt in self.tool_receipts}) != EXPECTED_TOOL_COUNT
            or (
                self.analytics_tool_receipt_count is not None
                and self.analytics_tool_receipt_count != len(ANALYTICS_TOOL_IDS)
            )
            or self.live_events != 0
            or self.live_mutation_calls not in {None, 0}
            or not self.disclaimer_response_completed
            or not self.fixture_reference_validated
            or not self.account_allowlist_resolved
            or not self.auth_status_completed
            or not self.session_capabilities_completed
            or self.before_state_fingerprint != self.after_state_fingerprint
            or self.cleanup_complete is False
            or self.account_state_unchanged is False
            or self.redacted_publication is False
            or self.uncleaned_resources != 0
            or self.errors
        ):
            raise ValueError("passed SIM matrix lacks complete safe 60-tool evidence")
        return self


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
    live_mutation_calls: int
    receipts: dict[str, MatrixScenarioReceipt]
    lifecycle_seen: set[str]
    registered_ops: list[str]
    uncleaned: int
    preflight: PreflightFlags
    before: dict[str, JsonValue]
    after: dict[str, JsonValue]
