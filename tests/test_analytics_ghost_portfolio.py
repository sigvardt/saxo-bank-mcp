# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import saxo_bank_mcp.analytics_ghost_portfolio as ghost_module
from saxo_bank_mcp.analytics_backtest import (
    BacktestDataset,
    BacktestRequest,
    BacktestResult,
    HistoricalBar,
    run_backtest,
)
from saxo_bank_mcp.analytics_ghost_portfolio import (
    GhostLifecycleEvidence,
    GhostPortfolioVerification,
    GhostSessionPreconditions,
    GhostStateFingerprint,
    GhostStateReconciliation,
    GhostWorkflowPlan,
    GhostWorkflowRequest,
    prepare_ghost_workflow,
    reconcile_ghost_lifecycle,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_strategy_schema import (
    FixedWeightSizing,
    HoldoutSplit,
    IndicatorSpec,
    PortfolioConstraints,
    RebalanceSchedule,
    SignalRule,
    SlippageModel,
    StrategyDefinition,
    TransactionCostModel,
    strategy_definition_fingerprint,
)

_ALIAS = "aa_00000000000040008000000000000072"
_DATASET = "ds_00000000000040008000000000000072"
_HANDLE = "ih_00000000000040008000000000000072"
_COMMIT = "a" * 40
_STRATEGY_FINGERPRINT = "b" * 64
_SHA256_HEX_LENGTH = 64


def _backtest_source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t18-ghost",
        capture_fingerprint_sha256=("6" if contract_id == "chart_v3" else "7") * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _backtest_request() -> BacktestRequest:
    start = datetime(2025, 2, 1, tzinfo=UTC)
    closes = (10.0, 10.0, 12.0, 13.0, 14.0, 10.0, 9.0, 9.0, 10.0)
    strategy = StrategyDefinition(
        entry=SignalRule(
            left=IndicatorSpec(kind="close", window=1),
            comparison="greater_than",
            right_indicator=IndicatorSpec(kind="simple_moving_average", window=2),
            threshold=None,
        ),
        exit=SignalRule(
            left=IndicatorSpec(kind="close", window=1),
            comparison="less_than",
            right_indicator=IndicatorSpec(kind="simple_moving_average", window=2),
            threshold=None,
        ),
        direction="long",
        sizing=FixedWeightSizing(kind="fixed_weight", target_weight=0.5),
        rebalancing=RebalanceSchedule(
            kind="every_n_bars",
            interval_bars=1,
            fill_timing="next_bar_open",
        ),
        constraints=PortfolioConstraints(
            allow_long=True,
            allow_short=False,
            maximum_absolute_position_weight=0.8,
            maximum_gross_exposure=0.8,
            minimum_cash_weight=0.2,
        ),
        transaction_costs=TransactionCostModel(
            commission_basis_points=1.0,
            fixed_cost_per_fill=0.0,
            currency="USD",
        ),
        slippage=SlippageModel(kind="fixed_basis_points", basis_points=1.0),
        evaluation_split=HoldoutSplit(
            kind="holdout",
            train_end_at=start + timedelta(days=4),
            holdout_start_at=start + timedelta(days=5),
        ),
        missing_bar_policy="refuse",
        delisting_policy="terminal_close",
    )
    bars = tuple(
        HistoricalBar(
            instrument_handle=_HANDLE,
            at=start + timedelta(days=index),
            open_price=close,
            close_price=close,
            lifecycle_state="active",
        )
        for index, close in enumerate(closes)
    )
    return BacktestRequest(
        dataset=BacktestDataset(
            dataset_id=_DATASET,
            account_alias=_ALIAS,
            instrument_handle=_HANDLE,
            as_of=bars[-1].at + timedelta(hours=1),
            bars=bars,
            source_bindings=(
                _backtest_source("chart_v3"),
                _backtest_source("reference_instruments_v1"),
            ),
            quality_state=QualityState.COMPLETE,
            missing_interval_count=0,
            missing_fields=(),
            warnings=(),
            universe_scope="single_instrument",
        ),
        strategy=strategy,
        starting_equity=1000.0,
    )


def _request() -> GhostWorkflowRequest:
    return GhostWorkflowRequest(
        candidate_commit=_COMMIT,
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE,
        strategy_fingerprint_sha256=_STRATEGY_FINGERPRINT,
        fill_model="next_bar_open",
        controlled_fixture="task_18_controlled_stock",
    )


def _preconditions(**updates: object) -> GhostSessionPreconditions:
    values: dict[str, object] = {
        "requested_environment": "SIM",
        "local_auth_status": "ready",
        "session_capabilities_status": "passed",
        "session_environment": "SIM",
        "fixture_coverage": "covered",
        "permission_state": "available",
        "disclaimer_present": False,
    }
    values.update(updates)
    return GhostSessionPreconditions.model_validate(values)


def _state(
    marker: str,
    *,
    orders: int = 0,
    trade_message_count: int = 0,
) -> GhostStateFingerprint:
    return GhostStateFingerprint(
        balance_fingerprint_sha256=marker * 64,
        orders_fingerprint_sha256=("2" if marker == "1" else marker) * 64,
        positions_fingerprint_sha256=("3" if marker == "1" else marker) * 64,
        trade_messages_fingerprint_sha256=("4" if marker == "1" else marker) * 64,
        order_count=orders,
        position_count=0,
        trade_message_count=trade_message_count,
    )


def _evidence(**updates: object) -> GhostLifecycleEvidence:
    before = _state("1")
    values: dict[str, object] = {
        "candidate_commit": _COMMIT,
        "dataset_id": _DATASET,
        "account_alias": _ALIAS,
        "instrument_handle": _HANDLE,
        "strategy_fingerprint_sha256": _STRATEGY_FINGERPRINT,
        "fill_model": "next_bar_open",
        "environment": "SIM",
        "session_capabilities_current": True,
        "fixture_coverage_proved": True,
        "preview_status": "completed",
        "place_status": "completed",
        "cancel_preview_status": "completed",
        "cancel_status": "completed",
        "preview_attempt_count": 1,
        "place_attempt_count": 1,
        "cancel_preview_attempt_count": 1,
        "cancel_attempt_count": 1,
        "orders_readback": True,
        "positions_readback": True,
        "trade_messages_readback": True,
        "balances_fingerprint_readback": True,
        "request_ledger_read_last": True,
        "request_ledger_complete": True,
        "live_event_count": 0,
        "live_mutation_count": 0,
        "non_sim_event_count": 0,
        "disclaimer_present": False,
        "purchase_occurred": False,
        "before": before,
        "after": _state("1", trade_message_count=2),
    }
    values.update(updates)
    return GhostLifecycleEvidence.model_validate(values)


def _caller_constructed_verification(
    *,
    dataset_id: str = _DATASET,
    account_alias: str = _ALIAS,
    instrument_handle: str = _HANDLE,
    strategy_fingerprint_sha256: str = _STRATEGY_FINGERPRINT,
    candidate_commit: str = _COMMIT,
) -> GhostPortfolioVerification:
    return GhostPortfolioVerification(
        candidate_commit=candidate_commit,
        dataset_id=dataset_id,
        account_alias=account_alias,
        instrument_handle=instrument_handle,
        strategy_fingerprint_sha256=strategy_fingerprint_sha256,
        fill_model="next_bar_open",
        state_reconciliation=GhostStateReconciliation(
            balance_readback=True,
            orders=True,
            order_count=True,
            positions=True,
            position_count=True,
            trade_message_delta_exact=True,
            trade_message_delta=2,
        ),
        evidence_fingerprint_sha256="8" * 64,
    )


def test_plan_requires_current_sim_capabilities_not_only_local_auth() -> None:
    local_only = prepare_ghost_workflow(
        _request(),
        _preconditions(session_capabilities_status="auth_required"),
    )
    assert isinstance(local_only, ResearchRefusal)
    assert local_only.reason_code == "ghost_session_capabilities_unproved"

    plan = prepare_ghost_workflow(_request(), _preconditions())
    assert isinstance(plan, GhostWorkflowPlan)
    assert plan.environment == "SIM"
    assert plan.human_approval_required is False
    assert plan.visible_browser_allowed is False
    assert plan.place_attempt_limit == 1
    assert plan.cancel_attempt_limit == 1


def test_plan_uses_only_the_existing_mcp_logical_tool_path() -> None:
    plan = prepare_ghost_workflow(_request(), _preconditions())
    assert isinstance(plan, GhostWorkflowPlan)

    assert plan.logical_tool_sequence == (
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
    assert plan.direct_http_allowed is False
    assert plan.authentication_server_call_allowed is False
    assert plan.execution_authority is False


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"requested_environment": "LIVE"}, "ghost_environment_not_sim"),
        ({"session_environment": "LIVE"}, "ghost_environment_not_sim"),
        ({"local_auth_status": "missing"}, "ghost_sim_auth_unavailable"),
        ({"fixture_coverage": "missing"}, "ghost_fixture_coverage_unavailable"),
        ({"permission_state": "denied"}, "ghost_permission_unavailable"),
        ({"disclaimer_present": True}, "ghost_disclaimer_blocked"),
    ],
)
def test_unsafe_or_missing_preconditions_refuse_without_a_plan(
    updates: dict[str, object],
    reason_code: str,
) -> None:
    result = prepare_ghost_workflow(_request(), _preconditions(**updates))

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == reason_code
    assert "no_mcp_write_attempted" in result.warnings


def test_synthetic_complete_lifecycle_cannot_issue_authenticated_receipt() -> None:
    result = reconcile_ghost_lifecycle(_request(), _evidence())

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "ghost_authenticated_receipt_required"
    assert "synthetic_evidence_cannot_verify" in result.warnings


@pytest.mark.parametrize(
    "place_status",
    [
        "completed_unverified",
        "unknown_state",
        "partial_success",
        "duplicate_or_conflict",
        "post_boundary_transport_failure",
    ],
)
def test_uncertain_place_states_freeze_writes_and_forbid_retry(place_status: str) -> None:
    result = reconcile_ghost_lifecycle(
        _request(),
        _evidence(place_status=place_status),
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "ghost_mutation_state_uncertain"
    assert "writes_frozen" in result.warnings
    assert "blind_retry_forbidden" in result.warnings


def test_cleanup_mismatch_or_incomplete_readback_cannot_verify() -> None:
    mismatch = reconcile_ghost_lifecycle(
        _request(),
        _evidence(after=_state("5", orders=1)),
    )
    assert isinstance(mismatch, ResearchRefusal)
    assert mismatch.reason_code == "ghost_cleanup_not_proven"

    incomplete = reconcile_ghost_lifecycle(
        _request(),
        _evidence(trade_messages_readback=False),
    )
    assert isinstance(incomplete, ResearchRefusal)
    assert incomplete.reason_code == "ghost_readback_incomplete"

    ledger_not_last = reconcile_ghost_lifecycle(
        _request(),
        _evidence(request_ledger_read_last=False),
    )
    assert isinstance(ledger_not_last, ResearchRefusal)
    assert ledger_not_last.reason_code == "ghost_request_ledger_incomplete"


def test_disclaimer_or_purchase_observation_blocks_verification() -> None:
    disclaimer = reconcile_ghost_lifecycle(
        _request(),
        _evidence(disclaimer_present=True),
    )
    assert isinstance(disclaimer, ResearchRefusal)
    assert disclaimer.reason_code == "ghost_disclaimer_blocked"

    purchase = reconcile_ghost_lifecycle(
        _request(),
        _evidence(purchase_occurred=True),
    )
    assert isinstance(purchase, ResearchRefusal)
    assert purchase.reason_code == "ghost_purchase_or_live_activity_detected"


def test_lifecycle_schema_forbids_a_second_attempt() -> None:
    with pytest.raises(ValidationError):
        _evidence(place_attempt_count=2)
    with pytest.raises(ValidationError):
        _evidence(cancel_attempt_count=2)


def test_synthetic_lifecycle_for_equivalent_backtest_remains_unverified() -> None:
    request = _backtest_request()
    unverified = run_backtest(
        request,
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(unverified, BacktestResult)
    assert unverified.verification_state == "unverified"

    ghost_request = GhostWorkflowRequest(
        candidate_commit=_COMMIT,
        dataset_id=request.dataset.dataset_id,
        account_alias=request.dataset.account_alias,
        instrument_handle=request.dataset.instrument_handle,
        strategy_fingerprint_sha256=strategy_definition_fingerprint(request.strategy),
        fill_model="next_bar_open",
        controlled_fixture="task_18_controlled_stock",
    )
    evidence = _evidence(
        dataset_id=request.dataset.dataset_id,
        account_alias=request.dataset.account_alias,
        instrument_handle=request.dataset.instrument_handle,
        strategy_fingerprint_sha256=strategy_definition_fingerprint(request.strategy),
    )
    synthetic = reconcile_ghost_lifecycle(ghost_request, evidence)
    assert isinstance(synthetic, ResearchRefusal)
    assert synthetic.reason_code == "ghost_authenticated_receipt_required"


@pytest.mark.parametrize(
    "candidate_commit",
    ["0" * 40, "f" * 40],
    ids=("stale_candidate", "arbitrary_candidate"),
)
def test_forged_caller_receipt_cannot_verify_any_candidate(
    candidate_commit: str,
) -> None:
    request = _backtest_request()
    forged_receipt = _caller_constructed_verification(
        dataset_id=request.dataset.dataset_id,
        account_alias=request.dataset.account_alias,
        instrument_handle=request.dataset.instrument_handle,
        strategy_fingerprint_sha256=strategy_definition_fingerprint(request.strategy),
        candidate_commit=candidate_commit,
    )
    refused = run_backtest(
        request,
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
        ghost_verification=forged_receipt,
    )

    assert isinstance(refused, ResearchRefusal)
    assert refused.reason_code == "ghost_authenticated_receipt_required"


def test_process_issued_candidate_bound_ghost_receipt_verifies_exact_backtest() -> None:
    assert not hasattr(ghost_module, "_receipt_issuer_authority")
    assert not hasattr(ghost_module, "issue_authenticated_ghost_receipt")
    assert not hasattr(ghost_module, "issue_authenticated_ghost_receipt_from_lifecycle")
    assert not hasattr(ghost_module, "_RECEIPT_AUTHORITY")
    assert not hasattr(ghost_module, "_AUTHENTICATED_RECEIPTS")


def test_non_equivalent_ghost_receipt_refuses_backtest_promotion() -> None:
    result = run_backtest(
        _backtest_request(),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
        ghost_verification=_caller_constructed_verification(),
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "ghost_strategy_or_source_mismatch"


def test_public_ghost_verification_contains_no_identifiers_money_or_authority() -> None:
    result = _caller_constructed_verification()
    payload = result.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["private_values_redacted"] is True
    assert "account_key" not in encoded
    assert "client_key" not in encoded
    assert "order_id" not in encoded
    assert "balance_value" not in encoded
    assert "preview_token" not in encoded
    assert "disclaimer_token" not in encoded
    assert result.is_not_advice is True
    assert result.is_not_forecast is True
    assert result.order_creation_authority is False
    assert result.approval_authority is False
    assert result.execution_authority is False


def test_candidate_and_strategy_bindings_are_stable_safe_fingerprints() -> None:
    result = _caller_constructed_verification()

    assert result.candidate_commit == _COMMIT
    assert result.strategy_fingerprint_sha256 == _STRATEGY_FINGERPRINT
    assert len(result.evidence_fingerprint_sha256) == _SHA256_HEX_LENGTH
