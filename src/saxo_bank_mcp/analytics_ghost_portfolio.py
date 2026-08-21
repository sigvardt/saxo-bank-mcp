from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    CodeCommit,
    DatasetId,
    InstrumentHandle,
    SafeAccountScope,
    Sha256Fingerprint,
)

type GhostPlaceStatus = Literal[
    "completed",
    "failed",
    "completed_unverified",
    "unknown_state",
    "partial_success",
    "duplicate_or_conflict",
    "post_boundary_transport_failure",
    "not_run",
]
type GhostStepStatus = Literal["completed", "failed", "refused", "not_run"]

_LOGICAL_TOOL_SEQUENCE: Final = (
    "saxo_auth_status",
    "saxo_get_session_capabilities",
    "saxo_call_registered_endpoint",
    "saxo_create_order_preview",
    "saxo_place_order",
    "saxo_create_write_preview",
    "saxo_cancel_sim_orders_by_instrument",
    "saxo_call_registered_endpoint",
    "saxo_get_safe_request_ledger",
)
_UNCERTAIN_PLACE_STATES: Final = frozenset(
    {
        "completed_unverified",
        "unknown_state",
        "partial_success",
        "duplicate_or_conflict",
        "post_boundary_transport_failure",
    }
)
_EXPECTED_TRADE_MESSAGE_DELTA: Final = 2


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class GhostWorkflowRequest(_StrictModel):
    """Value-free binding for one frozen candidate and controlled SIM fixture."""

    candidate_commit: CodeCommit
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    strategy_fingerprint_sha256: Sha256Fingerprint
    fill_model: Literal["next_bar_open"]
    controlled_fixture: Literal["task_18_controlled_stock"]


class GhostSessionPreconditions(_StrictModel):
    """Pre-write state; local auth alone never satisfies the session requirement."""

    requested_environment: Literal["SIM", "LIVE", "UNKNOWN"]
    local_auth_status: Literal["ready", "missing", "expired", "invalid"]
    session_capabilities_status: Literal["passed", "auth_required", "failed", "unknown"]
    session_environment: Literal["SIM", "LIVE", "UNKNOWN"]
    fixture_coverage: Literal["covered", "missing", "unknown"]
    permission_state: Literal["available", "denied", "unknown"]
    disclaimer_present: bool


class GhostWorkflowPlan(_StrictModel):
    """A plan only; the analytics module has no MCP execution authority."""

    status: Literal[ResearchStatus.REDUCED] = ResearchStatus.REDUCED
    analysis_kind: Literal["ghost_portfolio"] = "ghost_portfolio"
    candidate_commit: CodeCommit
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    strategy_fingerprint_sha256: Sha256Fingerprint
    environment: Literal["SIM"] = "SIM"
    logical_tool_sequence: tuple[str, ...] = _LOGICAL_TOOL_SEQUENCE
    place_attempt_limit: Literal[1] = 1
    cancel_attempt_limit: Literal[1] = 1
    human_approval_required: Literal[False] = False
    visible_browser_allowed: Literal[False] = False
    direct_http_allowed: Literal[False] = False
    authentication_server_call_allowed: Literal[False] = False
    order_creation_authority: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    verification_state: Literal["unverified"] = "unverified"
    warnings: tuple[Literal["ghost_workflow_not_executed"], ...] = ("ghost_workflow_not_executed",)


class GhostStateFingerprint(_StrictModel):
    """Fingerprint-only brokerage state with safe aggregate counts."""

    balance_fingerprint_sha256: Sha256Fingerprint
    orders_fingerprint_sha256: Sha256Fingerprint
    positions_fingerprint_sha256: Sha256Fingerprint
    trade_messages_fingerprint_sha256: Sha256Fingerprint
    order_count: int = Field(ge=0)
    position_count: int = Field(ge=0)
    trade_message_count: int = Field(ge=0)


class GhostLifecycleEvidence(_StrictModel):
    """Sanitized receipts from one externally driven MCP logical-tool lifecycle."""

    candidate_commit: CodeCommit
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    strategy_fingerprint_sha256: Sha256Fingerprint
    fill_model: Literal["next_bar_open"]
    environment: Literal["SIM", "LIVE", "UNKNOWN"]
    session_capabilities_current: bool
    fixture_coverage_proved: bool
    preview_status: GhostStepStatus
    place_status: GhostPlaceStatus
    cancel_preview_status: GhostStepStatus
    cancel_status: GhostStepStatus
    preview_attempt_count: int = Field(ge=0, le=1)
    place_attempt_count: int = Field(ge=0, le=1)
    cancel_preview_attempt_count: int = Field(ge=0, le=1)
    cancel_attempt_count: int = Field(ge=0, le=1)
    orders_readback: bool
    positions_readback: bool
    trade_messages_readback: bool
    balances_fingerprint_readback: bool
    request_ledger_read_last: bool
    request_ledger_complete: bool
    live_event_count: int = Field(ge=0)
    live_mutation_count: int = Field(ge=0)
    non_sim_event_count: int = Field(ge=0)
    disclaimer_present: bool
    purchase_occurred: bool
    before: GhostStateFingerprint
    after: GhostStateFingerprint


class GhostStateReconciliation(_StrictModel):
    balance_readback: Literal[True]
    orders: Literal[True]
    order_count: Literal[True]
    positions: Literal[True]
    position_count: Literal[True]
    trade_message_delta_exact: Literal[True]
    trade_message_delta: Literal[2]


class GhostPortfolioVerification(_StrictModel):
    """Public-safe proof that one equivalent controlled SIM lifecycle reconciled."""

    status: Literal[ResearchStatus.COMPLETE] = ResearchStatus.COMPLETE
    analysis_kind: Literal["ghost_portfolio"] = "ghost_portfolio"
    candidate_commit: CodeCommit
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    strategy_fingerprint_sha256: Sha256Fingerprint
    fill_model: Literal["next_bar_open"]
    environment: Literal["SIM"] = "SIM"
    verification_state: Literal["verified"] = "verified"
    preview_count: Literal[1] = 1
    place_attempt_count: Literal[1] = 1
    cancel_attempt_count: Literal[1] = 1
    preview_status: Literal["completed"] = "completed"
    place_status: Literal["completed"] = "completed"
    cancel_status: Literal["completed"] = "completed"
    cleanup_state: Literal["proved_reconciled"] = "proved_reconciled"
    state_reconciliation: GhostStateReconciliation
    request_ledger_state: Literal["complete_and_last"] = "complete_and_last"
    evidence_fingerprint_sha256: Sha256Fingerprint
    private_values_redacted: Literal[True] = True
    is_not_advice: Literal[True] = True
    is_not_forecast: Literal[True] = True
    prediction_claim: Literal[False] = False
    recommendation_authority: Literal[False] = False
    order_creation_authority: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False


def prepare_ghost_workflow(  # noqa: PLR0911
    request: GhostWorkflowRequest,
    preconditions: GhostSessionPreconditions,
) -> GhostWorkflowPlan | ResearchRefusal:
    """Return an inert typed plan only after every pre-write SIM gate is proved."""
    if preconditions.requested_environment != "SIM" or preconditions.session_environment != "SIM":
        return _refusal(
            request,
            "ghost_environment_not_sim",
            "the controlled ghost workflow requires a proven current SIM environment",
            warnings=("no_mcp_write_attempted",),
        )
    if preconditions.local_auth_status != "ready":
        return _refusal(
            request,
            "ghost_sim_auth_unavailable",
            "ready isolated SIM authentication is unavailable",
            warnings=("no_mcp_write_attempted",),
        )
    if preconditions.session_capabilities_status != "passed":
        return _refusal(
            request,
            "ghost_session_capabilities_unproved",
            "local auth status does not prove current SIM session capabilities",
            warnings=("no_mcp_write_attempted",),
        )
    if preconditions.fixture_coverage != "covered":
        return _refusal(
            request,
            "ghost_fixture_coverage_unavailable",
            "the controlled named SIM fixture is not currently proved",
            warnings=("no_mcp_write_attempted",),
        )
    if preconditions.permission_state != "available":
        return _refusal(
            request,
            "ghost_permission_unavailable",
            "the current SIM session does not prove required lifecycle permissions",
            warnings=("no_mcp_write_attempted",),
        )
    if preconditions.disclaimer_present:
        return _refusal(
            request,
            "ghost_disclaimer_blocked",
            "a Saxo disclaimer blocks the workflow and must not be answered",
            warnings=("no_mcp_write_attempted",),
        )
    return GhostWorkflowPlan(
        candidate_commit=request.candidate_commit,
        dataset_id=request.dataset_id,
        account_alias=request.account_alias,
        instrument_handle=request.instrument_handle,
        strategy_fingerprint_sha256=request.strategy_fingerprint_sha256,
    )


def reconcile_ghost_lifecycle(
    request: GhostWorkflowRequest,
    evidence: GhostLifecycleEvidence,
) -> GhostPortfolioVerification | ResearchRefusal:
    """Refuse caller-authored evidence even when its claimed lifecycle is internally valid."""
    validated = _validated_ghost_lifecycle(request, evidence)
    if isinstance(validated, ResearchRefusal):
        return validated
    return _refusal(
        request,
        "ghost_authenticated_receipt_required",
        "caller-supplied lifecycle evidence cannot issue an authenticated MCP ledger receipt",
        warnings=(
            "writes_frozen",
            "blind_retry_forbidden",
            "synthetic_evidence_cannot_verify",
        ),
    )


def _validated_ghost_lifecycle(  # noqa: C901, PLR0911
    request: GhostWorkflowRequest,
    evidence: GhostLifecycleEvidence,
) -> GhostPortfolioVerification | ResearchRefusal:
    """Validate observed evidence without exposing receipt authority to public callers."""
    if not _evidence_matches_request(request, evidence):
        return _refusal(
            request,
            "ghost_evidence_binding_mismatch",
            "ghost lifecycle evidence does not match the frozen candidate and strategy",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if evidence.environment != "SIM" or not evidence.session_capabilities_current:
        return _refusal(
            request,
            "ghost_environment_not_sim",
            "the lifecycle lacks current SIM environment proof",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if evidence.disclaimer_present:
        return _refusal(
            request,
            "ghost_disclaimer_blocked",
            "a disclaimer was observed and no response is permitted",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if (
        evidence.purchase_occurred
        or evidence.live_event_count
        or evidence.live_mutation_count
        or evidence.non_sim_event_count
    ):
        return _refusal(
            request,
            "ghost_purchase_or_live_activity_detected",
            "purchase or non-SIM activity prevents ghost verification",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if evidence.place_status in _UNCERTAIN_PLACE_STATES:
        return _refusal(
            request,
            "ghost_mutation_state_uncertain",
            "the placement boundary is uncertain and must be reconciled before any retry",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if not evidence.fixture_coverage_proved:
        return _refusal(
            request,
            "ghost_fixture_coverage_unavailable",
            "the lifecycle did not prove the controlled fixture",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    attempts_and_statuses = (
        evidence.preview_attempt_count == 1
        and evidence.place_attempt_count == 1
        and evidence.cancel_preview_attempt_count == 1
        and evidence.cancel_attempt_count == 1
        and evidence.preview_status == "completed"
        and evidence.place_status == "completed"
        and evidence.cancel_preview_status == "completed"
        and evidence.cancel_status == "completed"
    )
    if not attempts_and_statuses:
        return _refusal(
            request,
            "ghost_lifecycle_incomplete",
            "the exact one-preview, one-place, one-cancel lifecycle did not complete",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if not all(
        (
            evidence.orders_readback,
            evidence.positions_readback,
            evidence.trade_messages_readback,
            evidence.balances_fingerprint_readback,
        )
    ):
        return _refusal(
            request,
            "ghost_readback_incomplete",
            "orders, positions, trade messages, and balance fingerprint readback are required",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    if not evidence.request_ledger_read_last or not evidence.request_ledger_complete:
        return _refusal(
            request,
            "ghost_request_ledger_incomplete",
            "the complete safe request ledger must be read last",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    reconciliation = _state_reconciliation(evidence.before, evidence.after)
    if not all(
        (
            reconciliation["balance_readback"],
            reconciliation["orders"],
            reconciliation["order_count"],
            reconciliation["positions"],
            reconciliation["position_count"],
            reconciliation["trade_message_delta_exact"],
            reconciliation["trade_message_delta"] == _EXPECTED_TRADE_MESSAGE_DELTA,
        ),
    ):
        return _refusal(
            request,
            "ghost_cleanup_not_proven",
            "controlled brokerage inventory did not reconcile with two SIM audit messages",
            warnings=("writes_frozen", "blind_retry_forbidden"),
        )
    return GhostPortfolioVerification(
        candidate_commit=request.candidate_commit,
        dataset_id=request.dataset_id,
        account_alias=request.account_alias,
        instrument_handle=request.instrument_handle,
        strategy_fingerprint_sha256=request.strategy_fingerprint_sha256,
        fill_model=request.fill_model,
        state_reconciliation=GhostStateReconciliation.model_validate(reconciliation),
        evidence_fingerprint_sha256=hashlib.sha256(
            evidence.model_dump_json().encode(),
        ).hexdigest(),
    )


def _evidence_matches_request(
    request: GhostWorkflowRequest,
    evidence: GhostLifecycleEvidence,
) -> bool:
    return (
        request.candidate_commit == evidence.candidate_commit
        and request.dataset_id == evidence.dataset_id
        and request.account_alias == evidence.account_alias
        and request.instrument_handle == evidence.instrument_handle
        and request.strategy_fingerprint_sha256 == evidence.strategy_fingerprint_sha256
        and request.fill_model == evidence.fill_model
    )


def _state_reconciliation(
    before: GhostStateFingerprint,
    after: GhostStateFingerprint,
) -> dict[str, bool | int]:
    return {
        "balance_readback": True,
        "orders": before.orders_fingerprint_sha256 == after.orders_fingerprint_sha256,
        "order_count": before.order_count == after.order_count == 0,
        "positions": before.positions_fingerprint_sha256 == after.positions_fingerprint_sha256,
        "position_count": before.position_count == after.position_count,
        "trade_message_delta_exact": after.trade_message_count
        == before.trade_message_count + _EXPECTED_TRADE_MESSAGE_DELTA,
        "trade_message_delta": after.trade_message_count - before.trade_message_count,
    }


def ghost_state_reconciled(
    before: GhostStateFingerprint,
    after: GhostStateFingerprint,
) -> bool:
    """Return whether the lifecycle reconciled inventory plus its two audit events."""
    observed = _state_reconciliation(before, after)
    return bool(
        observed["balance_readback"]
        and observed["orders"]
        and observed["order_count"]
        and observed["positions"]
        and observed["position_count"]
        and observed["trade_message_delta_exact"]
        and observed["trade_message_delta"] == _EXPECTED_TRADE_MESSAGE_DELTA
    )


def _refusal(
    request: GhostWorkflowRequest,
    reason_code: str,
    reason: str,
    *,
    warnings: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="ghost_portfolio",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(request.dataset_id,),
        instrument_handles=(request.instrument_handle,),
        warnings=tuple(sorted(set(warnings))),
        source_scope=None,
    )
