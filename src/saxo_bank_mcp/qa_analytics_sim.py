"""Offline-safe contracts for the 21-tool analytics portion of the SIM matrix."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
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
_DEGRADATION_TOOLS: Final = frozenset(
    {
        "saxo_resolve_research_universe",
        "saxo_sync_research_data",
        "saxo_get_research_dataset",
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
        "saxo_propose_trade_from_analysis",
        "saxo_render_analysis",
        "saxo_export_analysis",
        "saxo_explain_analysis",
    },
)
_TIMEOUT_RECOVERY_TOOLS: Final = frozenset(
    {
        "saxo_resolve_research_universe",
        "saxo_sync_research_data",
        "saxo_manage_analysis_job",
        "saxo_render_analysis",
        "saxo_export_analysis",
        "saxo_delete_analytics_data",
    },
)
_SAFE_UUID4_PAYLOAD: Final = "00000000000040008000000000000000"
_SAFE_INSTRUMENT_HANDLE: Final = f"ih_{_SAFE_UUID4_PAYLOAD}"
_SAFE_DATASET_HANDLE: Final = f"ds_{_SAFE_UUID4_PAYLOAD}"
_SAFE_ANALYSIS_HANDLE: Final = f"an_{_SAFE_UUID4_PAYLOAD}"
_SAFE_JOB_HANDLE: Final = f"jb_{_SAFE_UUID4_PAYLOAD}"
_SAFE_DELETION_TOKEN: Final = f"dp_{_SAFE_UUID4_PAYLOAD}"


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
        if "success" not in kinds or "refusal" not in kinds or "privacy" not in kinds:
            raise ValueError("analytics tools require success, refusal, and privacy contracts")
        if "timeout" in kinds and "recovery" not in kinds:
            raise ValueError("timeout coverage requires an explicit recovery contract")
        return self


class AnalyticsCaseReceipt(_StrictReceipt):
    kind: AnalyticsCaseKind
    state: AnalyticsCaseState
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
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
    call_path: Literal["fastmcp.Client.call_tool"] = "fastmcp.Client.call_tool"

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        allowed: dict[AnalyticsCaseKind, frozenset[AnalyticsCaseState]] = {
            "success": frozenset({"passed"}),
            "degradation": frozenset({"degraded"}),
            "refusal": frozenset({"refused"}),
            "privacy": frozenset({"passed"}),
            "timeout": frozenset({"timed_out"}),
            "recovery": frozenset({"reconciled", "refused"}),
        }
        if self.state != "failed" and self.state not in allowed[self.kind]:
            raise ValueError("analytics case state does not match its case kind")
        if self.state not in {"failed", "timed_out"} and not self.result_parsed:
            raise ValueError("analytics case requires a parsed FastMCP result")
        if self.state == "timed_out" and self.result_state != "timed_out":
            raise ValueError("analytics timeout evidence requires a timed-out call")
        refusal_states = {"refused", "denied", "invalid_arguments", "invalid_request"}
        if self.state == "refused" and self.result_state not in refusal_states:
            raise ValueError("analytics refusal evidence requires a refused result")
        if self.state == "reconciled" and self.result_state != "reconciled":
            raise ValueError("analytics recovery evidence requires a reconciled result")
        if self.state != "failed" and (self.broker_write_made or self.private_values_published):
            raise ValueError("passing analytics case evidence violates safety or privacy")
        return self


class AnalyticsCaseCall(_StrictReceipt):
    tool_id: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    kind: AnalyticsCaseKind
    arguments: dict[str, JsonValue]
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)


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
    cleanup_complete: bool
    unchanged_account_state: bool
    redacted_publication: bool
    private_values_published: bool
    purchase_occurred: bool
    disclaimer_response_made: bool

    @model_validator(mode="after")
    def _validate_pass(self) -> Self:  # noqa: C901
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
        if self.live_events != 0 or self.live_mutation_calls != 0:
            raise ValueError("controlled analytics proof observed LIVE activity")
        if not self.cleanup_complete:
            raise ValueError("controlled analytics cleanup is incomplete")
        if self.before != self.after or not self.unchanged_account_state:
            raise ValueError("controlled analytics brokerage state changed")
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
        cases = [
            _case("success", ("passed", "verified", "completed", "inline", "resource_link")),
        ]
        if tool_id in _DEGRADATION_TOOLS:
            cases.append(
                _case(
                    "degradation",
                    ("degraded", "reduced", "ambiguous", "unavailable"),
                ),
            )
        cases.extend(
            (
                _case(
                    "refusal",
                    ("refused", "denied", "invalid_arguments", "invalid_request"),
                ),
                _case(
                    "privacy",
                    ("refused", "denied", "invalid_arguments", "invalid_request"),
                ),
            ),
        )
        if tool_id in _TIMEOUT_RECOVERY_TOOLS:
            cases.extend(
                (
                    _case("timeout", ("timed_out", "unknown_state")),
                    _case("recovery", ("refused", "invalid_arguments", "invalid_request")),
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
        if not {"success", "refusal", "privacy"} <= kinds:
            errors.append(f"analytics_required_cases_missing:{tool_id}")
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
    return hashlib.sha256(
        json.dumps(material, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
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
        "saxo_resolve_research_universe": {"query": "controlled stock fixture"},
        "saxo_manage_research_universe": {"action": "list"},
        "saxo_sync_research_data": {
            "request": {
                "items": [
                    {
                        "data_kind": "price_bars",
                        "handle": _SAFE_INSTRUMENT_HANDLE,
                        "interval": "1d",
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-01-31T00:00:00Z",
                    },
                ],
            },
        },
        "saxo_get_research_dataset": {"dataset_id": _SAFE_DATASET_HANDLE},
        "saxo_analyze_market": {
            "request": {
                "analysis_kind": "market_comparison",
                "dataset_ids": [_SAFE_DATASET_HANDLE],
            },
        },
        "saxo_analyze_instruments": {
            "request": {
                "analysis_kind": "instrument_price_return",
                "dataset_ids": [_SAFE_DATASET_HANDLE],
                "instrument_handles": [_SAFE_INSTRUMENT_HANDLE],
            },
        },
        "saxo_analyze_portfolio": {
            "request": {
                "analysis_kind": "portfolio_performance",
                "dataset_ids": [_SAFE_DATASET_HANDLE],
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
            },
        },
        "saxo_model_derivatives": {
            "request": {
                "analysis_kind": "derivatives_model",
                "dataset_id": _SAFE_DATASET_HANDLE,
                "instrument_handles": [_SAFE_INSTRUMENT_HANDLE],
            },
        },
        "saxo_backtest_strategy": {
            "request": {
                "dataset_id": _SAFE_DATASET_HANDLE,
                "instrument_handle": _SAFE_INSTRUMENT_HANDLE,
                "strategy": strategy,
                "starting_equity": 1000.0,
            },
        },
        "saxo_propose_trade_from_analysis": {
            "analysis_id": _SAFE_ANALYSIS_HANDLE,
            "instrument_handle": _SAFE_INSTRUMENT_HANDLE,
            "side": "buy",
            "quantity": "1",
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
        "saxo_delete_analytics_data": {"token": _SAFE_DELETION_TOKEN},
    }
    return tuple((tool_id, calls[tool_id]) for tool_id in ANALYTICS_TOOL_IDS)


def analytics_case_calls() -> tuple[AnalyticsCaseCall, ...]:
    """Return every applicable case as a real FastMCP call contract.

    Success, degradation, timeout, and recovery use the bounded typed request. Refusal and
    privacy use a guaranteed schema-extra rejection, so the privacy probe cannot enter a domain
    service or publish owner values. A runtime result must still match its declared case contract.
    """
    primary = dict(analytics_primary_calls())
    calls: list[AnalyticsCaseCall] = []
    for contract in analytics_sim_contracts():
        for case in contract.cases:
            arguments: dict[str, JsonValue]
            if case.kind in {"refusal", "privacy", "recovery"}:
                arguments = {"__qa_schema_extra_rejection__": True}
            else:
                arguments = dict(primary[contract.tool_id])
            calls.append(
                AnalyticsCaseCall(
                    tool_id=contract.tool_id,
                    kind=case.kind,
                    arguments=arguments,
                    timeout_seconds=0.001 if case.kind == "timeout" else None,
                ),
            )
    return tuple(calls)


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
