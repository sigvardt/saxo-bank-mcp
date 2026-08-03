from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.config import SIM_ENDPOINTS
from saxo_bank_mcp.qa_analytics_sim import (
    ANALYSIS_KIND_IDS,
    AnalyticsCaseReceipt,
    AnalyticsRuntimeResources,
    AnalyticsToolCaseEvidence,
    BrokerageStateFingerprint,
    ControlledSimLifecycleReceipt,
    analytics_case_evidence_errors,
)
from saxo_bank_mcp.server_tool_ids import ANALYTICS_TOOL_IDS, EXPECTED_TOOL_COUNT

NON_EXECUTABLE_SIM: Final = frozenset({"saxo_list_live_accounts", "saxo_precheck_live_order"})
SIM_GATEWAY_HOST: Final = urlparse(SIM_ENDPOINTS.rest_base_url).hostname or "gateway.saxobank.com"
FIXTURE_STREAM_UIC: Final = 21
FIXTURE_INSTRUMENT: Final = 211
FIXTURE_ASSET_TYPE: Final = "Stock"
FIXTURE_ORDER_AMOUNT: Final = 1
FIXTURE_LIMIT_PRICE: Final = 50
FIXTURE_MODIFIED_LIMIT_PRICE: Final = 51
MULTILEG_FIXTURE_UICS: Final = (30004846, 30004926)
MULTILEG_FIXTURE_ASSET_TYPE: Final = "StockIndexOption"
HTTP_CLIENT_ERROR_MIN: Final = 400
RATE_LIMIT_STATUSES: Final = frozenset({429, 503})
RATE_LIMIT_CODES: Final = frozenset({"RateLimitExceeded", "ServiceUnavailable"})
DISCLAIMER_RETRY_DELAYS_SECONDS: Final = (2.0, 5.0, 10.0, 20.0)


class MatrixScenarioReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    status: Literal["completed", "expected_refusal", "failed"]
    mcp_call_observed: Literal[True] = True
    result_parsed: bool
    result_state: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    mcp_is_error: bool
    skipped: Literal[False] = False
    requested_tool_covered: Literal[True] = True
    network_call_made: bool = False
    hosts: tuple[str, ...] = ()
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    call_path: Literal["fastmcp.Client.call_tool"] = "fastmcp.Client.call_tool"

    @model_validator(mode="after")
    def _validate_claim(self) -> MatrixScenarioReceipt:
        refusal_states = {
            "blocked",
            "denied",
            "invalid_arguments",
            "invalid_request",
            "refused",
            "unsupported",
        }
        failure_states = {
            "auth_required",
            "failed",
            "network_error",
            "timed_out",
            "unparsed",
        }
        if self.status == "completed" and (
            not self.result_parsed
            or self.mcp_is_error
            or self.result_state in refusal_states | failure_states
        ):
            raise ValueError("completed matrix receipt lacks a parsed successful result")
        if self.status == "expected_refusal" and (
            not self.result_parsed or self.result_state not in refusal_states
        ):
            raise ValueError("expected-refusal matrix receipt lacks a parsed refusal")
        return self


class SimToolMatrixReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "blocked"]
    environment: Literal["SIM"] = "SIM"
    reason: str = ""
    tool_receipts: tuple[MatrixScenarioReceipt, ...]
    lifecycle_calls: tuple[str, ...]
    registered_trading_write_ops: tuple[str, ...]
    disclaimer_response_made: Literal[False]
    disclaimer_refusal_observed: bool
    fixture_reference_validated: bool
    account_allowlist_resolved: bool
    auth_status_completed: bool
    session_capabilities_completed: bool
    before_state_fingerprint: BrokerageStateFingerprint
    after_state_fingerprint: BrokerageStateFingerprint
    uncleaned_resources: int
    hosts: tuple[str, ...]
    live_events: int
    live_mutation_calls: int = Field(ge=0)
    analytics_tool_receipt_count: int = Field(ge=0)
    analytics_case_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    analytics_case_receipts: tuple[AnalyticsToolCaseEvidence, ...]
    analysis_execution_receipts: tuple[AnalyticsCaseReceipt, ...] = ()
    controlled_sim_lifecycle: ControlledSimLifecycleReceipt | None = None
    mcp_only_account_fixture_state: bool
    cleanup_complete: bool
    account_state_unchanged: bool
    redacted_publication: bool
    purchase_occurred: Literal[False] = False
    errors: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_pass_claim(self) -> SimToolMatrixReceipt:
        observed_analysis_kinds = tuple(
            receipt.analysis_kind
            for receipt in self.analysis_execution_receipts
            if receipt.kind == "success"
            and receipt.result_parsed
            and receipt.analysis_kind is not None
        )
        if self.status == "passed" and (
            self.reason != ""
            or len(self.tool_receipts) != EXPECTED_TOOL_COUNT
            or len({receipt.tool for receipt in self.tool_receipts}) != EXPECTED_TOOL_COUNT
            or self.analytics_tool_receipt_count != len(ANALYTICS_TOOL_IDS)
            or self.live_events != 0
            or self.live_mutation_calls != 0
            or not self.disclaimer_refusal_observed
            or not self.fixture_reference_validated
            or not self.account_allowlist_resolved
            or not self.auth_status_completed
            or not self.session_capabilities_completed
            or self.before_state_fingerprint != self.after_state_fingerprint
            or not self.mcp_only_account_fixture_state
            or not self.cleanup_complete
            or not self.account_state_unchanged
            or not self.redacted_publication
            or self.uncleaned_resources != 0
            or any(receipt.status == "failed" for receipt in self.tool_receipts)
            or analytics_case_evidence_errors(self.analytics_case_receipts)
            or observed_analysis_kinds != ANALYSIS_KIND_IDS
            or len(observed_analysis_kinds) != len(set(observed_analysis_kinds))
            or any(receipt.state == "failed" for receipt in self.analysis_execution_receipts)
            or self.controlled_sim_lifecycle is None
            or self.controlled_sim_lifecycle.evidence_state != "passed"
            or any(
                component.observed_state != "available"
                for component in self.before_state_fingerprint.components
            )
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
    disclaimer_refusal_ok: bool = False
    mcp_only: bool = True


@dataclass(slots=True)
class MatrixRuntimeState:
    errors: list[str]
    hosts: set[str]
    live_events: int
    live_mutation_calls: int
    receipts: dict[str, MatrixScenarioReceipt]
    analytics_case_receipts: list[AnalyticsToolCaseEvidence]
    lifecycle_seen: set[str]
    registered_ops: list[str]
    uncleaned: int
    preflight: PreflightFlags
    before: BrokerageStateFingerprint | None
    after: BrokerageStateFingerprint | None
    analysis_execution_receipts: list[AnalyticsCaseReceipt] = field(default_factory=list)
    controlled_sim_lifecycle: ControlledSimLifecycleReceipt | None = None
    analytics_resources: AnalyticsRuntimeResources = field(
        default_factory=AnalyticsRuntimeResources,
    )
