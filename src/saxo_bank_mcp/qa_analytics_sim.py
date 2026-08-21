"""Offline-safe contracts for the 21-tool analytics portion of the SIM matrix."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS, ANALYTICS_TOOL_IDS

if TYPE_CHECKING:
    from saxo_bank_mcp.qa_sim_tool_matrix_models import MatrixScenarioReceipt

type AnalyticsCaseKind = Literal[
    "success",
    "degradation",
    "refusal",
    "privacy",
    "timeout",
    "recovery",
]
type AnalyticsCaseState = Literal[
    "passed",
    "degraded",
    "refused",
    "timed_out",
    "reconciled",
    "failed",
]
type ExpectedAnalysisOutcome = Literal["persisted", "reduced", "refused", "resolved"]

ANALYTICS_CASE_KINDS: Final[tuple[AnalyticsCaseKind, ...]] = (
    "success",
    "degradation",
    "refusal",
    "privacy",
    "timeout",
    "recovery",
)
BROKERAGE_STATE_COMPONENTS: Final[tuple[str, ...]] = (
    "balances",
    "positions",
    "orders",
    "trade_messages",
    "subscriptions",
    "previews_write_state",
    "jobs",
    "caches",
    "temporary_files",
)
CONTROLLED_SIM_CASES: Final[tuple[str, ...]] = (
    "transaction_history",
    "execution_context",
    "ghost_portfolio",
    "options_entitlement",
    "cleanup",
)
CONTROLLED_SIM_MUTATION_CALL_COUNT: Final = 2
_DEGRADATION_TOOLS: Final = frozenset(
    {
        "saxo_resolve_research_universe",
        "saxo_sync_research_data",
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
    },
)
DECLARED_ANALYTICS_SUCCESS_TOOL_IDS: Final[tuple[str, ...]] = (*ANALYTICS_TOOL_IDS,)
_DECLARED_ANALYTICS_SUCCESS_TOOLS: Final = frozenset(
    DECLARED_ANALYTICS_SUCCESS_TOOL_IDS,
)
_TIMEOUT_RECOVERY_TOOLS: Final[frozenset[str]] = frozenset(
    {"saxo_manage_analysis_job"},
)
_SAFE_UUID4_PAYLOAD: Final = "00000000000040008000000000000000"
_SAFE_INSTRUMENT_HANDLE: Final = f"ih_{_SAFE_UUID4_PAYLOAD}"
_SAFE_DATASET_HANDLE: Final = f"ds_{_SAFE_UUID4_PAYLOAD}"
_SAFE_ANALYSIS_HANDLE: Final = f"an_{_SAFE_UUID4_PAYLOAD}"
_SAFE_DEGRADED_ANALYSIS_HANDLE: Final = "an_11111111111141118111111111111111"
_SAFE_JOB_HANDLE: Final = f"jb_{_SAFE_UUID4_PAYLOAD}"

ANALYSIS_KIND_TOOL_IDS: Final[tuple[tuple[str, str], ...]] = (
    ("cash_and_settlement", "saxo_analyze_portfolio"),
    ("corporate_action_center", "saxo_analyze_portfolio"),
    ("cost_xray", "saxo_analyze_portfolio"),
    ("derivatives_model", "saxo_model_derivatives"),
    ("derivatives_scenario", "saxo_model_derivatives"),
    ("execution_quality", "saxo_analyze_portfolio"),
    ("fixed_income", "saxo_analyze_instruments"),
    ("futures_curve", "saxo_model_derivatives"),
    ("fx_forward_carry", "saxo_model_derivatives"),
    ("goal_model", "saxo_manage_analysis_job"),
    ("income_calendar", "saxo_analyze_portfolio"),
    ("instrument_dossier", "saxo_analyze_instruments"),
    ("instrument_price_return", "saxo_analyze_instruments"),
    ("instrument_price_volume", "saxo_analyze_instruments"),
    ("instrument_quote", "saxo_analyze_instruments"),
    ("instrument_resolution", "saxo_resolve_research_universe"),
    ("instrument_risk", "saxo_analyze_instruments"),
    ("iv_surface", "saxo_model_derivatives"),
    ("margin_fire_drill", "saxo_run_scenario"),
    ("market_comparison", "saxo_analyze_market"),
    ("market_correlation_regime", "saxo_analyze_market"),
    ("market_microstructure", "saxo_analyze_market"),
    ("market_volatility_dispersion", "saxo_analyze_market"),
    ("monte_carlo", "saxo_manage_analysis_job"),
    ("multi_instrument_comparison", "saxo_analyze_instruments"),
    ("option_chain", "saxo_model_derivatives"),
    ("option_greeks", "saxo_model_derivatives"),
    ("option_payoff", "saxo_model_derivatives"),
    ("portfolio_attribution", "saxo_analyze_portfolio"),
    ("portfolio_comparison", "saxo_analyze_portfolio"),
    ("portfolio_exposure", "saxo_analyze_portfolio"),
    ("portfolio_margin", "saxo_analyze_portfolio"),
    ("portfolio_minimum_variance", "saxo_optimize_portfolio"),
    ("portfolio_overview", "saxo_analyze_portfolio"),
    ("portfolio_performance", "saxo_analyze_portfolio"),
    ("portfolio_risk", "saxo_analyze_portfolio"),
    ("portfolio_risk_parity", "saxo_optimize_portfolio"),
    ("portfolio_scenario", "saxo_run_scenario"),
    ("portfolio_time_machine", "saxo_analyze_portfolio"),
    ("position_sizing", "saxo_size_position"),
    ("pretrade_impact", "saxo_propose_trade_from_analysis"),
    ("regulatory_cost_report", "saxo_analyze_portfolio"),
    ("scenario_combined", "saxo_run_scenario"),
    ("scenario_currency", "saxo_run_scenario"),
    ("scenario_custom", "saxo_run_scenario"),
    ("scenario_historical", "saxo_run_scenario"),
    ("scenario_margin", "saxo_run_scenario"),
    ("scenario_rate", "saxo_run_scenario"),
    ("scenario_volatility", "saxo_run_scenario"),
    ("session_cockpit", "saxo_analyze_market"),
    ("technical_indicators", "saxo_analyze_instruments"),
    ("trading_conditions", "saxo_analyze_instruments"),
    ("trading_mirror", "saxo_analyze_portfolio"),
    ("wrapper_comparison", "saxo_analyze_market"),
)
ANALYSIS_KIND_IDS: Final = tuple(item[0] for item in ANALYSIS_KIND_TOOL_IDS)
_PERSISTED_ANALYSIS_KINDS: Final = frozenset(
    {
        "bounded_backtest",
        "derivatives_model",
        "instrument_price_return",
        "margin_fire_drill",
        "market_comparison",
        "portfolio_minimum_variance",
        "portfolio_performance",
        "portfolio_risk_parity",
        "portfolio_scenario",
        "position_sizing",
        "pretrade_impact",
        "scenario_combined",
        "scenario_currency",
        "scenario_custom",
    },
)

_SUCCESS_STATES_BY_TOOL: Final[dict[str, tuple[str, ...]]] = {
    "saxo_analytics_capabilities": ("passed",),
    "saxo_resolve_research_universe": ("resolved",),
    "saxo_manage_research_universe": ("passed",),
    "saxo_sync_research_data": ("passed",),
    "saxo_get_research_dataset": ("passed",),
    "saxo_analyze_market": ("verified",),
    "saxo_analyze_instruments": ("verified",),
    "saxo_analyze_portfolio": ("verified",),
    "saxo_size_position": ("verified",),
    "saxo_run_scenario": ("verified",),
    "saxo_optimize_portfolio": ("verified",),
    "saxo_model_derivatives": ("verified",),
    "saxo_backtest_strategy": ("verified",),
    "saxo_propose_trade_from_analysis": ("verified",),
    "saxo_render_analysis": ("inline", "resource_link"),
    "saxo_export_analysis": ("inline", "resource_link"),
    "saxo_explain_analysis": ("passed",),
    "saxo_manage_analysis_job": ("job_queued",),
    "saxo_list_analytics_storage": ("passed",),
    "saxo_preview_analytics_deletion": ("preview_ready",),
    "saxo_delete_analytics_data": ("deleted",),
}
_DEGRADATION_STATES_BY_TOOL: Final[dict[str, tuple[str, ...]]] = {
    "saxo_resolve_research_universe": ("ambiguous", "unavailable"),
    "saxo_sync_research_data": ("degraded", "refused"),
    "saxo_get_research_dataset": ("refused",),
    "saxo_analyze_market": ("degraded", "refused"),
    "saxo_analyze_instruments": ("degraded", "refused"),
    "saxo_analyze_portfolio": ("degraded", "refused"),
    "saxo_size_position": ("degraded", "refused"),
    "saxo_run_scenario": ("degraded", "refused"),
    "saxo_optimize_portfolio": ("degraded", "refused"),
    "saxo_model_derivatives": ("degraded", "refused"),
    "saxo_backtest_strategy": ("degraded", "refused"),
    "saxo_propose_trade_from_analysis": ("refused",),
    "saxo_render_analysis": ("refused",),
    "saxo_export_analysis": ("refused",),
    "saxo_explain_analysis": ("refused",),
}
_REFUSAL_STATES: Final = ("refused", "denied", "invalid_arguments", "invalid_request")
_JOB_RECONCILIATION_STATES: Final = (
    "job_queued",
    "job_running",
    "job_completed",
    "job_failed",
    "job_cancelled",
    "job_expired",
    "job_interrupted_restart_required",
)


class _StrictReceipt(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class AnalyticsCaseContract(_StrictReceipt):
    kind: AnalyticsCaseKind
    expected_states: tuple[str, ...] = Field(min_length=1)
    requirement_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    missing_data_must_reduce_or_refuse: bool = True
    entitlement_denial_must_reduce_or_refuse: bool = True
    broker_write_allowed: Literal[False] = False
    disclaimer_response_allowed: Literal[False] = False


class AnalyticsToolSimContract(_StrictReceipt):
    tool_id: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    cases: tuple[AnalyticsCaseContract, ...] = Field(min_length=1)
    runtime_source: Literal["saxo_only", "local_only"]
    background_work_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_cases(self) -> Self:
        kinds = tuple(case.kind for case in self.cases)
        if len(kinds) != len(set(kinds)):
            raise ValueError("analytics case contracts must be unique")
        if "refusal" not in kinds or "privacy" not in kinds:
            raise ValueError("analytics tools require refusal and privacy contracts")
        if "timeout" in kinds and "recovery" not in kinds:
            raise ValueError("timeout coverage requires an explicit recovery contract")
        return self


class AnalyticsCaseReceipt(_StrictReceipt):
    kind: AnalyticsCaseKind
    tool_id: str | None = Field(default=None, pattern=r"^saxo_[a-z0-9_]{1,127}$")
    analysis_kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
    returned_analysis_kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
    analysis_id: str | None = Field(default=None, pattern=r"^an_[a-f0-9]{32}$")
    expected_analysis_outcome: ExpectedAnalysisOutcome | None = None
    persisted_result_authenticated: bool = False
    source_precondition_refused: bool = False
    source_precondition_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    state: AnalyticsCaseState
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    observed_reason_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z_]{0,127}$",
    )
    mcp_call_observed: Literal[True]
    result_parsed: bool
    result_state: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    mcp_is_error: bool
    network_call_made: bool
    broker_write_made: bool
    private_values_published: bool
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reconciles_request_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    reconciliation_observation_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    blind_retry_attempted: Literal[False] = False
    call_path: Literal["fastmcp.Client.call_tool"] = "fastmcp.Client.call_tool"

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:  # noqa: C901, PLR0912
        allowed: dict[AnalyticsCaseKind, frozenset[AnalyticsCaseState]] = {
            "success": frozenset({"passed"}),
            "degradation": frozenset({"degraded", "refused"}),
            "refusal": frozenset({"refused"}),
            "privacy": frozenset({"passed"}),
            "timeout": frozenset({"timed_out"}),
            "recovery": frozenset({"reconciled"}),
        }
        analysis_observation = self.analysis_kind is not None
        if analysis_observation != (self.tool_id is not None):
            raise ValueError("analysis execution must bind its exact logical tool")
        if (
            self.state != "failed"
            and self.state not in allowed[self.kind]
            and not (
                self.kind == "success"
                and self.state in {"degraded", "refused"}
                and (analysis_observation or self.source_precondition_refused)
            )
        ):
            raise ValueError("analytics case state does not match its case kind")
        if self.state not in {"failed", "timed_out"} and not self.result_parsed:
            raise ValueError("analytics case requires a parsed FastMCP result")
        if self.state == "timed_out" and self.result_state != "timed_out":
            raise ValueError("analytics timeout evidence requires a timed-out call")
        refusal_states = {
            "refused",
            "denied",
            "invalid_arguments",
            "invalid_request",
            "job_cancelled",
            "job_expired",
            "job_failed",
            "job_interrupted_restart_required",
        }
        if self.state == "refused" and self.result_state not in refusal_states:
            raise ValueError("analytics refusal evidence requires a refused result")
        if self.source_precondition_refused and (self.kind != "success" or self.state != "refused"):
            raise ValueError("source precondition evidence must bind a refused success probe")
        if self.source_precondition_refused != (
            self.source_precondition_evidence_sha256 is not None
        ):
            raise ValueError("source precondition refusal requires exact observed evidence")
        if self.state == "reconciled" and (
            self.reconciles_request_sha256 is None or self.reconciliation_observation_sha256 is None
        ):
            raise ValueError("analytics recovery must observe the exact timed operation")
        if self.kind != "recovery" and (
            self.reconciles_request_sha256 is not None
            or self.reconciliation_observation_sha256 is not None
        ):
            raise ValueError("only recovery evidence may bind a timed operation")
        if self.state != "failed" and (self.broker_write_made or self.private_values_published):
            raise ValueError("passing analytics case evidence violates safety or privacy")
        if analysis_observation:
            if self.expected_analysis_outcome is None:
                raise ValueError("analysis execution must declare its expected honest outcome")
            expected_state: dict[ExpectedAnalysisOutcome, AnalyticsCaseState] = {
                "persisted": "passed",
                "reduced": "degraded",
                "refused": "refused",
                "resolved": "passed",
            }
            if (
                self.state != "failed"
                and self.state != expected_state[self.expected_analysis_outcome]
            ):
                raise ValueError("analysis evidence did not observe its declared honest outcome")
            if self.state == "passed":
                if self.expected_analysis_outcome == "persisted" and (
                    self.returned_analysis_kind != self.analysis_kind
                    or self.analysis_id is None
                    or not self.persisted_result_authenticated
                ):
                    raise ValueError("persisted analysis evidence is not replay-authenticated")
                if self.expected_analysis_outcome == "resolved" and self.result_state != "resolved":
                    raise ValueError("resolution evidence did not observe an exact resolved result")
            if self.state == "refused" and self.result_state not in refusal_states:
                raise ValueError("analysis refusal evidence requires a refused terminal result")
        elif any(
            (
                self.tool_id is not None,
                self.returned_analysis_kind is not None,
                self.analysis_id is not None,
                self.expected_analysis_outcome is not None,
                self.persisted_result_authenticated,
            ),
        ):
            raise ValueError("non-analysis cases cannot carry analysis-result evidence")
        return self


class AnalyticsCaseCall(_StrictReceipt):
    tool_id: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    kind: AnalyticsCaseKind
    arguments: dict[str, JsonValue]
    input_strategy: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    reconciles_kind: Literal["timeout"] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)
    analysis_kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    )
    expected_analysis_outcome: ExpectedAnalysisOutcome | None = None
    source_precondition_refused: bool = False
    source_precondition_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def _validate_recovery_strategy(self) -> Self:
        if self.kind == "recovery" and (
            self.input_strategy != "observe_exact_timed_operation"
            or self.reconciles_kind != "timeout"
        ):
            raise ValueError("analytics recovery must bind the exact timed operation")
        if self.kind != "recovery" and self.reconciles_kind is not None:
            raise ValueError("only recovery calls may name a reconciled case")
        if self.analysis_kind is not None and self.kind != "success":
            raise ValueError("analysis-kind execution applies only to success calls")
        if (self.analysis_kind is None) != (self.expected_analysis_outcome is None):
            raise ValueError("analysis-kind execution requires one honest expected outcome")
        if self.source_precondition_refused and self.kind != "success":
            raise ValueError("source precondition refusal applies only to a success probe")
        if self.source_precondition_refused != (
            self.source_precondition_evidence_sha256 is not None
        ):
            raise ValueError("source precondition refusal requires exact observed evidence")
        return self


@dataclass(frozen=True, slots=True)
class AnalyticsTimedOperation:
    tool_id: str
    request_sha256: str
    arguments: dict[str, JsonValue]


@dataclass(slots=True)
class AnalyticsRuntimeResources:
    """Server-issued handles retained only for one isolated FastMCP matrix session."""

    instrument_handles: list[str] = field(default_factory=list)
    account_selectors: list[str] = field(default_factory=list)
    degraded_instrument_handles: list[str] = field(default_factory=list)
    dataset_ids: list[str] = field(default_factory=list)
    source_dataset_ids: list[str] = field(default_factory=list)
    degraded_dataset_ids: list[str] = field(default_factory=list)
    dataset_ids_by_analysis_kind: dict[str, list[str]] = field(default_factory=dict)
    degraded_dataset_ids_by_analysis_kind: dict[str, list[str]] = field(default_factory=dict)
    analysis_input_dataset_ids_by_analysis_kind: dict[str, list[str]] = field(
        default_factory=dict,
    )
    degraded_analysis_input_dataset_ids_by_analysis_kind: dict[str, list[str]] = field(
        default_factory=dict,
    )
    analysis_input_instrument_handles_by_analysis_kind: dict[str, list[str]] = field(
        default_factory=dict,
    )
    analysis_input_refusals_by_analysis_kind: dict[str, str] = field(default_factory=dict)
    analysis_ids: list[str] = field(default_factory=list)
    degraded_analysis_ids: list[str] = field(default_factory=list)
    analysis_ids_by_kind: dict[str, list[str]] = field(default_factory=dict)
    degraded_analysis_ids_by_kind: dict[str, list[str]] = field(default_factory=dict)
    account_aliases: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    deletion_token: str | None = None
    timed_operation: AnalyticsTimedOperation | None = None
    cleanup_verified: bool = False
    source_request_count: int = 0
    source_mcp_call_count: int = 0
    source_contract_ids: set[str] = field(default_factory=set)
    option_entitlement_state: Literal["available", "denied", "unknown"] = "unknown"
    option_expiries: list[str] = field(default_factory=list)
    pretrade_proposal_price: str | None = None
    controlled_order_limit_price: str | None = None

    def remember_timeout(
        self,
        call: AnalyticsCaseCall,
        arguments: dict[str, JsonValue],
    ) -> None:
        if call.kind != "timeout":
            raise ValueError("only a timed case can establish reconciliation identity")
        self.timed_operation = AnalyticsTimedOperation(
            tool_id=call.tool_id,
            request_sha256=_request_sha256(arguments),
            arguments=dict(arguments),
        )


def _request_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode(),
    ).hexdigest()


class AnalyticsToolCaseEvidence(_StrictReceipt):
    tool_id: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    cases: tuple[AnalyticsCaseReceipt, ...]

    @model_validator(mode="after")
    def _validate_unique_cases(self) -> Self:
        kinds = tuple(case.kind for case in self.cases)
        if len(kinds) != len(set(kinds)):
            raise ValueError("analytics tool case receipts must be unique")
        return self


class BrokerageStateComponent(_StrictReceipt):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    count: int = Field(ge=0)
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_state: Literal["available", "unavailable"]
    mcp_tool_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_mcp_tools(self) -> Self:
        if len(self.mcp_tool_ids) != len(set(self.mcp_tool_ids)) or any(
            not tool_id.startswith("saxo_") for tool_id in self.mcp_tool_ids
        ):
            raise ValueError("state component MCP tools must be unique logical tool IDs")
        return self


class BrokerageStateFingerprint(_StrictReceipt):
    components: tuple[BrokerageStateComponent, ...]

    @model_validator(mode="after")
    def _validate_components(self) -> Self:
        names = tuple(component.name for component in self.components)
        if names != BROKERAGE_STATE_COMPONENTS:
            raise ValueError("brokerage state components must appear exactly once in fixed order")
        return self


def _brokerage_components(
    state: BrokerageStateFingerprint,
) -> dict[str, BrokerageStateComponent]:
    return {component.name: component for component in state.components}


def brokerage_inventory_reconciled(
    before: BrokerageStateFingerprint,
    after: BrokerageStateFingerprint,
) -> bool:
    """Prove that the controlled SIM lifecycle left no order or position change."""
    prior = _brokerage_components(before)
    current = _brokerage_components(after)
    if any(
        component.observed_state != "available"
        for component in (*before.components, *after.components)
    ):
        return False
    return bool(
        prior["orders"].count == 0
        and current["orders"].count == 0
        and prior["orders"].fingerprint_sha256 == current["orders"].fingerprint_sha256
        and prior["positions"].count == current["positions"].count
        and prior["positions"].fingerprint_sha256 == current["positions"].fingerprint_sha256
    )


def brokerage_ghost_state_reconciled(
    before: BrokerageStateFingerprint,
    after: BrokerageStateFingerprint,
) -> bool:
    """Prove broker inventory cleanup plus the exact two expected SIM audit messages."""
    if not brokerage_inventory_reconciled(before, after):
        return False
    prior = _brokerage_components(before)
    current = _brokerage_components(after)
    return bool(
        prior["balances"].count == current["balances"].count
        and current["trade_messages"].count
        == prior["trade_messages"].count + CONTROLLED_SIM_MUTATION_CALL_COUNT
    )


def brokerage_state_reconciled(
    before: BrokerageStateFingerprint,
    after: BrokerageStateFingerprint,
) -> bool:
    """Add exact task-owned local cleanup to the reconciled broker lifecycle."""
    prior = _brokerage_components(before)
    current = _brokerage_components(after)
    exact_local_components = (
        "subscriptions",
        "previews_write_state",
        "jobs",
        "caches",
        "temporary_files",
    )
    return bool(
        brokerage_ghost_state_reconciled(before, after)
        and all(prior[name] == current[name] for name in exact_local_components)
    )


class ControlledSimCaseReceipt(_StrictReceipt):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    state: Literal["passed", "degraded", "refused"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    source_request_count: int = Field(ge=0)
    mcp_call_count: int = Field(ge=0)
    sim_mutation_call_count: int = Field(ge=0)
    cleanup_complete: bool
    entitlement_state: Literal["available", "denied", "not_applicable"]
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    blind_retry_attempted: Literal[False] = False

    @model_validator(mode="after")
    def _validate_case(self) -> Self:
        if self.state == "passed" and self.reason_code != "passed":
            raise ValueError("passed controlled SIM cases must use the passed reason")
        if self.state != "passed" and self.reason_code == "passed":
            raise ValueError("reduced controlled SIM cases require a named reason")
        if self.case_id == "options_entitlement":
            if self.state == "passed" and self.entitlement_state != "available":
                raise ValueError("passed options lifecycle requires observed entitlement")
            if self.entitlement_state == "denied" and self.state == "passed":
                raise ValueError("denied options entitlement cannot pass")
        elif self.entitlement_state != "not_applicable":
            raise ValueError("entitlement state applies only to the options lifecycle")
        if not self.cleanup_complete:
            raise ValueError("controlled SIM case cleanup is incomplete")
        if self.state == "passed" and self.mcp_call_count < 1:
            raise ValueError("passed controlled SIM case requires an observed MCP call")
        if self.state == "passed" and self.case_id != "cleanup" and self.source_request_count < 1:
            raise ValueError("passed controlled SIM source case requires an observed source call")
        return self


class ControlledSimLifecycleReceipt(_StrictReceipt):
    evidence_state: Literal["passed", "reduced", "refused"] = "passed"
    environment: Literal["SIM"]
    cases: tuple[ControlledSimCaseReceipt, ...]
    before: BrokerageStateFingerprint
    after: BrokerageStateFingerprint
    live_events: int = Field(ge=0)
    live_mutation_calls: int = Field(ge=0)
    request_ledger_read_last: bool
    request_ledger_complete: bool
    request_ledger_fingerprint_sha256: str | None = Field(
        pattern=r"^[a-f0-9]{64}$",
    )
    cleanup_complete: bool
    unchanged_account_state: bool
    redacted_publication: bool
    private_values_published: bool
    purchase_occurred: bool
    disclaimer_response_made: bool

    @model_validator(mode="after")
    def _validate_pass(self) -> Self:  # noqa: C901, PLR0912
        if tuple(case.case_id for case in self.cases) != CONTROLLED_SIM_CASES:
            raise ValueError("controlled SIM lifecycle cases must appear exactly once")
        reduced_cases = tuple(case for case in self.cases if case.state != "passed")
        if self.evidence_state == "passed" and reduced_cases:
            raise ValueError("passed controlled SIM lifecycle contains reduced cases")
        if self.evidence_state == "reduced" and not reduced_cases:
            raise ValueError("reduced controlled SIM lifecycle requires a named reduced case")
        if self.evidence_state == "refused" and not any(
            case.state == "refused" for case in self.cases
        ):
            raise ValueError("refused controlled SIM lifecycle requires a refused case")
        cases_by_id = {case.case_id: case for case in self.cases}
        if self.evidence_state == "passed" and (
            cases_by_id["ghost_portfolio"].sim_mutation_call_count
            != CONTROLLED_SIM_MUTATION_CALL_COUNT
            or cases_by_id["cleanup"].sim_mutation_call_count != CONTROLLED_SIM_MUTATION_CALL_COUNT
        ):
            raise ValueError("passed controlled SIM lifecycle requires one place and one cancel")
        if self.live_events != 0 or self.live_mutation_calls != 0:
            raise ValueError("controlled analytics proof observed LIVE activity")
        if not self.cleanup_complete:
            raise ValueError("controlled analytics cleanup is incomplete")
        if (
            not brokerage_state_reconciled(self.before, self.after)
            or not self.unchanged_account_state
        ):
            raise ValueError("controlled analytics brokerage state did not reconcile")
        if self.evidence_state == "passed" and any(
            component.observed_state != "available" for component in self.before.components
        ):
            raise ValueError("passed controlled analytics state contains unavailable components")
        if not self.redacted_publication or self.private_values_published:
            raise ValueError("controlled analytics publication is not redacted")
        if self.purchase_occurred:
            raise ValueError("controlled analytics proof cannot include a purchase")
        if self.disclaimer_response_made:
            raise ValueError("analytics proof cannot answer a disclaimer")
        if self.evidence_state == "passed" and (
            not self.request_ledger_read_last
            or not self.request_ledger_complete
            or self.request_ledger_fingerprint_sha256 is None
        ):
            raise ValueError("passed controlled analytics proof requires the complete final ledger")
        return self


class PostSendTimeoutReceipt(_StrictReceipt):
    evidence_state: Literal["reconciled"] = "reconciled"
    operation_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    timeout_observed: Literal[True]
    stop_new_writes: Literal[True]
    reconciliation_attempted: bool
    reconciliation_state: Literal[
        "not_attempted",
        "no_effect_observed",
        "effect_observed",
        "unknown_state",
    ]
    matching_effect_count: int = Field(ge=0)
    blind_retry_attempted: bool
    recovery_action: Literal["refuse_retry", "reconcile_then_cleanup"]
    ledger_fingerprint_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def _validate_reconciliation(self) -> Self:
        if self.blind_retry_attempted:
            raise ValueError("post-send timeout evidence forbids a blind retry")
        if (
            not self.reconciliation_attempted
            or self.reconciliation_state == "not_attempted"
            or self.ledger_fingerprint_sha256 is None
        ):
            raise ValueError("post-send timeout requires completed reconciliation evidence")
        if self.reconciliation_state == "no_effect_observed" and self.matching_effect_count != 0:
            raise ValueError("no-effect reconciliation cannot report matching effects")
        if self.reconciliation_state == "effect_observed" and self.matching_effect_count == 0:
            raise ValueError("effect reconciliation requires a matching effect")
        return self


def analytics_sim_contracts() -> tuple[AnalyticsToolSimContract, ...]:
    """Return one immutable case contract for every analytics MCP tool."""
    contracts: list[AnalyticsToolSimContract] = []
    for tool_id in ANALYTICS_TOOL_IDS:
        cases = (
            [_case("success", _SUCCESS_STATES_BY_TOOL[tool_id])]
            if tool_id in _DECLARED_ANALYTICS_SUCCESS_TOOLS
            else []
        )
        if tool_id in _DEGRADATION_TOOLS:
            cases.append(_case("degradation", _DEGRADATION_STATES_BY_TOOL[tool_id]))
        cases.extend(
            (
                _case("refusal", _REFUSAL_STATES),
                _case("privacy", _REFUSAL_STATES),
            ),
        )
        if tool_id in _TIMEOUT_RECOVERY_TOOLS:
            cases.extend(
                (
                    _case("timeout", ("timed_out",)),
                    _case("recovery", _JOB_RECONCILIATION_STATES),
                ),
            )
        contracts.append(
            AnalyticsToolSimContract(
                tool_id=tool_id,
                cases=tuple(cases),
                runtime_source=(
                    "saxo_only"
                    if tool_id
                    in {
                        "saxo_resolve_research_universe",
                        "saxo_sync_research_data",
                    }
                    else "local_only"
                ),
            ),
        )
    return tuple(contracts)


def _case(kind: AnalyticsCaseKind, states: tuple[str, ...]) -> AnalyticsCaseContract:
    return AnalyticsCaseContract(
        kind=kind,
        expected_states=states,
        requirement_code=f"analytics_{kind}_case",
    )


def assert_analytics_case_coverage(
    contracts: Sequence[AnalyticsToolSimContract],
) -> tuple[str, ...]:
    """Return exact generated-coverage errors without weakening applicable cases."""
    tool_ids = tuple(contract.tool_id for contract in contracts)
    errors: list[str] = []
    if tool_ids != ANALYTICS_TOOL_IDS:
        errors.append("analytics_tool_contract_coverage_mismatch")
    if len(tool_ids) != len(set(tool_ids)):
        errors.append("analytics_tool_contracts_not_unique")
    by_tool = {contract.tool_id: contract for contract in contracts}
    for tool_id in ANALYTICS_TOOL_IDS:
        contract = by_tool.get(tool_id)
        if contract is None:
            continue
        kinds = {case.kind for case in contract.cases}
        if not {"refusal", "privacy"} <= kinds:
            errors.append(f"analytics_required_cases_missing:{tool_id}")
        if (tool_id in _DECLARED_ANALYTICS_SUCCESS_TOOLS) != ("success" in kinds):
            errors.append(f"analytics_success_applicability_mismatch:{tool_id}")
        if tool_id in _DEGRADATION_TOOLS and "degradation" not in kinds:
            errors.append(f"analytics_degradation_case_missing:{tool_id}")
        if tool_id in _TIMEOUT_RECOVERY_TOOLS and not {"timeout", "recovery"} <= kinds:
            errors.append(f"analytics_timeout_recovery_missing:{tool_id}")
    return tuple(errors)


def analytics_case_evidence_errors(
    receipts: Sequence[AnalyticsToolCaseEvidence],
    *,
    contracts: Sequence[AnalyticsToolSimContract] | None = None,
) -> tuple[str, ...]:
    """Require one observed receipt for every declared applicable analytics case."""
    expected = tuple(contracts or analytics_sim_contracts())
    receipt_tools = tuple(receipt.tool_id for receipt in receipts)
    expected_tools = tuple(contract.tool_id for contract in expected)
    errors: list[str] = []
    if receipt_tools != expected_tools:
        errors.append("analytics_case_receipt_tool_coverage_mismatch")
        return tuple(errors)
    for contract, receipt in zip(expected, receipts, strict=True):
        if tuple(case.kind for case in receipt.cases) != tuple(
            case.kind for case in contract.cases
        ):
            errors.append(f"analytics_case_receipt_coverage_mismatch:{contract.tool_id}")
            continue
        for case_contract, case_receipt in zip(contract.cases, receipt.cases, strict=True):
            if case_receipt.state == "failed":
                errors.append(
                    f"analytics_case_failed:{contract.tool_id}:{case_contract.kind}",
                )
                continue
            if case_receipt.result_state not in case_contract.expected_states:
                errors.append(
                    f"analytics_case_state_mismatch:{contract.tool_id}:{case_contract.kind}",
                )
    return tuple(errors)


def analytics_case_contract_sha256() -> str:
    """Fingerprint the exact checked execution-case contract without runtime values."""
    material = {
        "contracts": [contract.model_dump(mode="json") for contract in analytics_sim_contracts()],
        "calls": [call.model_dump(mode="json") for call in analytics_case_calls()],
    }
    return _json_sha256(material)


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


@contextmanager
def isolated_analytics_state(state_home: Path) -> Generator[None]:
    """Point one matrix process at a disposable owner-only analytics state home."""
    if not state_home.is_absolute() or state_home.exists():
        raise ValueError("isolated analytics state home must be a new absolute path")
    state_home.mkdir(mode=0o700, parents=False)
    previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state_home)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous


def live_mutation_calls_in(value: JsonValue) -> int:
    """Count only explicit LIVE mutation evidence in a redacted result tree."""
    if isinstance(value, list | tuple):
        return sum(live_mutation_calls_in(item) for item in value)
    if not isinstance(value, Mapping):
        return 0
    environment = str(value.get("environment", "")).upper()
    method = str(value.get("method", "GET")).upper()
    explicit = value.get("live_write_called") is True or value.get("broker_write_made") is True
    count = int(environment == "LIVE" and (method != "GET" or explicit))
    return count + sum(live_mutation_calls_in(item) for item in value.values())


def analytics_primary_calls() -> tuple[tuple[str, dict[str, JsonValue]], ...]:
    """Return schema-valid, bounded calls containing only synthetic safe handles.

    The primary calls are development and matrix routing inputs, not executable proof by
    themselves. Actual success/degradation/refusal receipts are collected separately.
    """
    strategy: dict[str, JsonValue] = {
        "entry": {
            "left": {"kind": "close", "window": 1},
            "comparison": "greater_than",
            "right_indicator": None,
            "threshold": 100.0,
        },
        "exit": {
            "left": {"kind": "close", "window": 1},
            "comparison": "less_than",
            "right_indicator": None,
            "threshold": 100.0,
        },
        "direction": "long",
        "sizing": {"kind": "fixed_weight", "target_weight": 0.5},
        "rebalancing": {
            "kind": "every_n_bars",
            "interval_bars": 5,
            "fill_timing": "next_bar_open",
        },
        "constraints": {
            "allow_long": True,
            "allow_short": False,
            "maximum_absolute_position_weight": 0.5,
            "maximum_gross_exposure": 1.0,
            "minimum_cash_weight": 0.5,
        },
        "transaction_costs": {
            "commission_basis_points": 0.0,
            "fixed_cost_per_fill": 0.0,
            "currency": "USD",
        },
        "slippage": {"kind": "none", "basis_points": 0.0},
        "evaluation_split": {
            "kind": "holdout",
            "train_end_at": "2026-01-01T00:00:00Z",
            "holdout_start_at": "2026-01-02T00:00:00Z",
        },
        "missing_bar_policy": "refuse",
        "delisting_policy": "terminal_close",
    }
    calls: dict[str, dict[str, JsonValue]] = {
        "saxo_analytics_capabilities": {},
        "saxo_resolve_research_universe": {
            "query": "AAPL",
            "asset_types": ["Stock"],
            "exchanges": ["NASDAQ"],
        },
        "saxo_manage_research_universe": {"action": "list"},
        "saxo_sync_research_data": {
            "request": {
                "items": [
                    {
                        "data_kind": "price_bars",
                        "handle": _SAFE_INSTRUMENT_HANDLE,
                        "interval": "1m",
                        "start": "2026-01-05T14:30:00Z",
                        "end": "2026-01-05T15:30:00Z",
                    },
                ],
            },
        },
        "saxo_get_research_dataset": {"dataset_id": _SAFE_DATASET_HANDLE},
        "saxo_analyze_market": {
            "request": {
                "analysis_kind": "market_comparison",
                "dataset_ids": [_SAFE_DATASET_HANDLE],
                "visibility": "private_user_result",
            },
        },
        "saxo_analyze_instruments": {
            "request": {
                "analysis_kind": "instrument_price_return",
                "dataset_ids": [_SAFE_DATASET_HANDLE],
                "instrument_handles": [_SAFE_INSTRUMENT_HANDLE],
                "visibility": "private_user_result",
            },
        },
        "saxo_analyze_portfolio": {
            "request": {
                "analysis_kind": "portfolio_performance",
                "dataset_ids": [_SAFE_DATASET_HANDLE],
                "visibility": "private_user_result",
            },
        },
        "saxo_size_position": {
            "request": {
                "dataset_id": _SAFE_DATASET_HANDLE,
                "instrument_handle": _SAFE_INSTRUMENT_HANDLE,
                "method": "stop_distance",
                "maximum_loss": "1",
                "risk_budget_confirmed": True,
                "stop_price": "99",
                "visibility": "private_user_result",
            },
        },
        "saxo_run_scenario": {
            "request": {
                "analysis_kind": "scenario_custom",
                "dataset_id": _SAFE_DATASET_HANDLE,
                "shocks": [
                    {
                        "instrument_handle": _SAFE_INSTRUMENT_HANDLE,
                        "price_shock_ratio": "-0.1",
                    },
                ],
                "numeric_shocks_echoed_by_caller": True,
                "caller_accepted_numeric_shocks": True,
                "visibility": "private_user_result",
            },
        },
        "saxo_optimize_portfolio": {
            "request": {
                "analysis_kind": "portfolio_minimum_variance",
                "dataset_id": _SAFE_DATASET_HANDLE,
                "objective": "minimum_variance",
                "objective_confirmed_by_caller": True,
                "constraints_confirmed_by_caller": True,
                "short_policy": "long_only",
                "maximum_turnover": "1",
                "maximum_transaction_cost_ratio": "0.01",
                "maximum_margin_ratio": "1",
                "visibility": "private_user_result",
            },
        },
        "saxo_model_derivatives": {
            "request": {
                "analysis_kind": "derivatives_model",
                "dataset_id": _SAFE_DATASET_HANDLE,
                "instrument_handles": [_SAFE_INSTRUMENT_HANDLE],
                "volatility_assumption": "0.2",
                "rate_assumption": "0.01",
                "visibility": "private_user_result",
            },
        },
        "saxo_backtest_strategy": {
            "request": {
                "dataset_id": _SAFE_DATASET_HANDLE,
                "instrument_handle": _SAFE_INSTRUMENT_HANDLE,
                "strategy": strategy,
                "starting_equity": 1000.0,
                "visibility": "private_user_result",
            },
        },
        "saxo_propose_trade_from_analysis": {
            "analysis_id": _SAFE_ANALYSIS_HANDLE,
            "instrument_handle": _SAFE_INSTRUMENT_HANDLE,
            "side": "buy",
            "quantity": "1",
            "proposal_price": "50",
            "maximum_loss": "1",
            "holding_period_days": 0,
            "visibility": "private_user_result",
        },
        "saxo_render_analysis": {
            "analysis_id": _SAFE_ANALYSIS_HANDLE,
            "template_id": "relative_performance",
        },
        "saxo_export_analysis": {
            "analysis_id": _SAFE_ANALYSIS_HANDLE,
            "export_kind": "table",
            "output_format": "json",
        },
        "saxo_explain_analysis": {"analysis_id": _SAFE_ANALYSIS_HANDLE},
        "saxo_manage_analysis_job": {"action": "check", "job_id": _SAFE_JOB_HANDLE},
        "saxo_list_analytics_storage": {"scope": {}},
        "saxo_preview_analytics_deletion": {"scope": {}},
        "saxo_delete_analytics_data": {},
    }
    return tuple((tool_id, calls[tool_id]) for tool_id in ANALYTICS_TOOL_IDS)


def analytics_case_calls() -> tuple[AnalyticsCaseCall, ...]:
    """Return every applicable case as a real FastMCP call contract.

    The matrix prefers handles issued earlier in the same FastMCP session. If an upstream
    analysis is refused and therefore issues no handle, its downstream local-only probes use
    these schema-valid synthetic handles to exercise the structured refusal boundary. Refusal
    and privacy cases use a guaranteed schema-extra rejection and never enter a domain service.
    """
    primary = dict(analytics_primary_calls())
    calls: list[AnalyticsCaseCall] = []
    for contract in analytics_sim_contracts():
        for case in contract.cases:
            arguments: dict[str, JsonValue]
            if case.kind in {"refusal", "privacy"}:
                arguments = {"__qa_schema_extra_rejection__": True}
            elif case.kind == "recovery" or (
                case.kind == "success"
                and contract.tool_id
                in {"saxo_preview_analytics_deletion", "saxo_delete_analytics_data"}
            ):
                arguments = {}
            else:
                arguments = dict(primary[contract.tool_id])
                if case.kind == "degradation":
                    arguments = _degradation_schema_arguments(contract.tool_id, arguments)
            calls.append(
                AnalyticsCaseCall(
                    tool_id=contract.tool_id,
                    kind=case.kind,
                    arguments=arguments,
                    input_strategy=_case_input_strategy(contract.tool_id, case.kind),
                    reconciles_kind="timeout" if case.kind == "recovery" else None,
                    timeout_seconds=0.001 if case.kind == "timeout" else None,
                ),
            )
    calls.extend(_analysis_kind_success_calls(primary))
    return tuple(calls)


def _analysis_kind_success_calls(
    primary: Mapping[str, dict[str, JsonValue]],
) -> tuple[AnalyticsCaseCall, ...]:
    """Issue one distinct logical-MCP execution contract for every frozen analysis kind."""
    calls: list[AnalyticsCaseCall] = []
    for analysis_kind, tool_id in ANALYSIS_KIND_TOOL_IDS:
        arguments = deepcopy(primary[tool_id])
        request = arguments.get("request")
        if isinstance(request, dict):
            request["analysis_kind"] = analysis_kind
            if tool_id == "saxo_optimize_portfolio":
                objective = (
                    "risk_parity"
                    if analysis_kind == "portfolio_risk_parity"
                    else "minimum_variance"
                )
                request["objective"] = objective
        calls.append(
            AnalyticsCaseCall(
                tool_id=tool_id,
                kind="success",
                arguments=arguments,
                input_strategy=f"analyze_kind_{analysis_kind}",
                analysis_kind=analysis_kind,
                expected_analysis_outcome=(
                    "resolved"
                    if analysis_kind == "instrument_resolution"
                    else "persisted"
                    if analysis_kind in _PERSISTED_ANALYSIS_KINDS
                    else "refused"
                ),
            ),
        )
    return tuple(calls)


def _case_input_strategy(tool_id: str, kind: AnalyticsCaseKind) -> str:
    if kind == "refusal":
        return "schema_refusal"
    if kind == "privacy":
        return "schema_privacy_refusal"
    if kind == "timeout":
        return "check_issued_job_with_timeout"
    if kind == "recovery":
        return "observe_exact_timed_operation"
    success: dict[str, str] = {
        "saxo_analytics_capabilities": "capabilities",
        "saxo_resolve_research_universe": "resolve_exact_fixture",
        "saxo_manage_research_universe": "list_universes",
        "saxo_sync_research_data": "sync_issued_instrument",
        "saxo_get_research_dataset": "read_issued_dataset",
        "saxo_analyze_market": "analyze_issued_dataset",
        "saxo_analyze_instruments": "analyze_issued_dataset_and_instrument",
        "saxo_analyze_portfolio": "analyze_issued_dataset",
        "saxo_size_position": "size_issued_dataset_and_instrument",
        "saxo_run_scenario": "scenario_issued_dataset_and_instrument",
        "saxo_optimize_portfolio": "optimize_issued_dataset",
        "saxo_model_derivatives": "model_issued_dataset_and_instrument",
        "saxo_backtest_strategy": "backtest_issued_dataset_and_instrument",
        "saxo_propose_trade_from_analysis": "propose_from_issued_analysis",
        "saxo_render_analysis": "render_issued_analysis",
        "saxo_export_analysis": "export_issued_analysis",
        "saxo_explain_analysis": "explain_issued_analysis",
        "saxo_manage_analysis_job": "start_job_from_issued_analysis",
        "saxo_list_analytics_storage": "list_isolated_storage",
        "saxo_preview_analytics_deletion": "preview_exact_test_closure",
        "saxo_delete_analytics_data": "consume_issued_deletion_token",
    }
    selected = success[tool_id]
    return selected if kind == "success" else f"{selected}_degraded"


def _degradation_schema_arguments(  # noqa: C901, PLR0912 - exact bounded catalog
    tool_id: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Keep declared degradation inputs distinct without supplying runtime source facts."""
    cloned: JsonValue = json.loads(json.dumps(arguments, allow_nan=False))
    if not isinstance(cloned, dict):
        return {}
    if tool_id == "saxo_resolve_research_universe":
        cloned["query"] = "controlled fixture"
        return cloned
    if tool_id == "saxo_get_research_dataset":
        cloned["page"] = 2
        return cloned
    if tool_id in {
        "saxo_propose_trade_from_analysis",
        "saxo_render_analysis",
        "saxo_export_analysis",
        "saxo_explain_analysis",
    }:
        cloned["analysis_id"] = _SAFE_DEGRADED_ANALYSIS_HANDLE
        if tool_id == "saxo_propose_trade_from_analysis":
            cloned["quantity"] = "2"
        elif tool_id == "saxo_render_analysis":
            cloned["output_format"] = "html"
        elif tool_id == "saxo_export_analysis":
            cloned["output_format"] = "csv"
        return cloned
    request = cloned.get("request")
    if not isinstance(request, dict):
        return cloned
    if tool_id == "saxo_sync_research_data":
        items = request.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            items[0]["interval"] = "1m"
            items[0]["start"] = "2026-01-01T00:00:00Z"
            items[0]["end"] = "2026-01-31T00:00:00Z"
    elif tool_id == "saxo_analyze_market":
        request["analysis_kind"] = "market_microstructure"
    elif tool_id == "saxo_analyze_instruments":
        request["analysis_kind"] = "instrument_quote"
    elif tool_id == "saxo_analyze_portfolio":
        request["analysis_kind"] = "tax_lot_export"
    elif tool_id == "saxo_size_position":
        request["maximum_loss"] = "2"
        request["risk_budget_confirmed"] = False
    elif tool_id == "saxo_run_scenario":
        request["caller_accepted_numeric_shocks"] = False
    elif tool_id == "saxo_optimize_portfolio":
        request["objective_confirmed_by_caller"] = False
    elif tool_id == "saxo_model_derivatives":
        request["analysis_kind"] = "option_payoff"
    elif tool_id == "saxo_backtest_strategy":
        request["starting_equity"] = 500.0
    return cloned


def merge_matrix_receipts(
    existing: Sequence[MatrixScenarioReceipt],
    analytics: Sequence[MatrixScenarioReceipt],
) -> tuple[MatrixScenarioReceipt, ...]:
    """Merge routing receipts and require the exact current 60-tool catalog."""
    merged = (*existing, *analytics)
    by_tool = {receipt.tool: receipt for receipt in merged}
    if len(by_tool) != len(merged):
        raise ValueError("MCP matrix receipts must be unique by tool")
    if frozenset(by_tool) != ALL_LOGICAL_TOOL_IDS:
        raise ValueError("MCP matrix receipts do not match the 60-tool catalog")
    return tuple(by_tool[tool] for tool in sorted(by_tool))
