from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Final

import pytest
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_models import AnalysisId, DatasetId, DeletionPreviewToken, JobId
from saxo_bank_mcp.analytics_store import StorageScope
from saxo_bank_mcp.analytics_sync import SyncResearchRequest
from saxo_bank_mcp.mcp_analytics_tools import (
    StoredBacktestToolRequest,
    StoredDerivativesToolRequest,
    StoredInstrumentToolRequest,
    StoredMarketToolRequest,
    StoredOptimizationToolRequest,
    StoredPortfolioToolRequest,
    StoredPositionSizingToolRequest,
    StoredScenarioToolRequest,
)
from saxo_bank_mcp.qa_analytics_sim import (
    ANALYTICS_CASE_KINDS,
    BROKERAGE_STATE_COMPONENTS,
    CONTROLLED_SIM_CASES,
    AnalyticsCaseKind,
    AnalyticsCaseReceipt,
    AnalyticsCaseState,
    BrokerageStateComponent,
    BrokerageStateFingerprint,
    ControlledSimCaseReceipt,
    ControlledSimLifecycleReceipt,
    PostSendTimeoutReceipt,
    analytics_case_contract_sha256,
    analytics_primary_calls,
    analytics_sim_contracts,
    assert_analytics_case_coverage,
    isolated_analytics_state,
    merge_matrix_receipts,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import receipt_for
from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt
from saxo_bank_mcp.server_tool_ids import (
    ALL_LOGICAL_TOOL_IDS,
    ANALYTICS_TOOL_IDS,
    EXPECTED_TOOL_COUNT,
)

EXPECTED_ANALYTICS_TOOL_COUNT: Final = len(ANALYTICS_TOOL_IDS)
OWNER_DIRECTORY_MODE: Final = 0o700


def _state(digest: str = "a" * 64) -> BrokerageStateFingerprint:
    return BrokerageStateFingerprint(
        components=tuple(
            BrokerageStateComponent(name=name, count=0, fingerprint_sha256=digest)
            for name in BROKERAGE_STATE_COMPONENTS
        ),
    )


def _lifecycle_cases() -> tuple[ControlledSimCaseReceipt, ...]:
    return tuple(
        ControlledSimCaseReceipt(
            case_id=case_id,
            state="passed",
            reason_code="passed",
            source_request_count=0,
            sim_mutation_call_count=0,
            cleanup_complete=True,
            entitlement_state=(
                "available" if case_id == "options_entitlement" else "not_applicable"
            ),
            evidence_sha256="f" * 64,
        )
        for case_id in CONTROLLED_SIM_CASES
    )


def test_analytics_sim_contracts_cover_21_tools_and_all_applicable_cases_once() -> None:
    contracts = analytics_sim_contracts()

    assert tuple(contract.tool_id for contract in contracts) == ANALYTICS_TOOL_IDS
    assert (
        len(contracts)
        == len({contract.tool_id for contract in contracts})
        == EXPECTED_ANALYTICS_TOOL_COUNT
    )
    assert assert_analytics_case_coverage(contracts) == ()
    for contract in contracts:
        kinds = tuple(case.kind for case in contract.cases)
        assert len(kinds) == len(set(kinds))
        assert "success" in kinds
        assert "refusal" in kinds
        assert "privacy" in kinds
        assert set(kinds) <= set(ANALYTICS_CASE_KINDS)
        if "timeout" in kinds:
            assert "recovery" in kinds


def test_primary_calls_are_value_free_and_cover_all_analytics_tools() -> None:
    calls = analytics_primary_calls()

    assert tuple(tool for tool, _arguments in calls) == ANALYTICS_TOOL_IDS
    assert len(calls) == EXPECTED_ANALYTICS_TOOL_COUNT
    serialized = repr(calls).lower()
    for forbidden in (
        "accountkey",
        "clientkey",
        "displayname",
        "access_token",
        "refresh_token",
        "http://",
        "https://",
        "file://",
    ):
        assert forbidden not in serialized


def test_primary_calls_satisfy_the_typed_adapter_input_contracts() -> None:
    calls = dict(analytics_primary_calls())

    SyncResearchRequest.model_validate_json(
        json.dumps(calls["saxo_sync_research_data"]["request"]),
    )
    TypeAdapter(DatasetId).validate_python(calls["saxo_get_research_dataset"]["dataset_id"])
    for tool, model in (
        ("saxo_analyze_market", StoredMarketToolRequest),
        ("saxo_analyze_instruments", StoredInstrumentToolRequest),
        ("saxo_analyze_portfolio", StoredPortfolioToolRequest),
        ("saxo_size_position", StoredPositionSizingToolRequest),
        ("saxo_run_scenario", StoredScenarioToolRequest),
        ("saxo_optimize_portfolio", StoredOptimizationToolRequest),
        ("saxo_model_derivatives", StoredDerivativesToolRequest),
        ("saxo_backtest_strategy", StoredBacktestToolRequest),
    ):
        model.model_validate(calls[tool]["request"])
    TypeAdapter(AnalysisId).validate_python(calls["saxo_explain_analysis"]["analysis_id"])
    TypeAdapter(JobId).validate_python(calls["saxo_manage_analysis_job"]["job_id"])
    assert calls["saxo_list_analytics_storage"]["scope"] == {}
    StorageScope()
    TypeAdapter(DeletionPreviewToken).validate_python(calls["saxo_delete_analytics_data"]["token"])


def test_matrix_merge_produces_exact_60_unique_receipts() -> None:
    existing_tools = tuple(sorted(ALL_LOGICAL_TOOL_IDS - set(ANALYTICS_TOOL_IDS)))
    old_receipts = tuple(receipt_for(tool, {"status": "completed"}, {}) for tool in existing_tools)
    analytics_receipts = tuple(
        receipt_for(tool, {"status": "refused"}, {}, status="expected_refusal")
        for tool in ANALYTICS_TOOL_IDS
    )

    merged = merge_matrix_receipts(old_receipts, analytics_receipts)

    assert len(merged) == len({receipt.tool for receipt in merged}) == EXPECTED_TOOL_COUNT
    assert {receipt.tool for receipt in merged} == ALL_LOGICAL_TOOL_IDS


def test_matrix_analytics_state_is_disposable_owner_only_and_restores_env(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "matrix-state"
    previous = os.environ.get("XDG_STATE_HOME")

    with isolated_analytics_state(state_home):
        assert os.environ["XDG_STATE_HOME"] == str(state_home)
        assert stat.S_IMODE(state_home.stat().st_mode) == OWNER_DIRECTORY_MODE

    assert os.environ.get("XDG_STATE_HOME") == previous


def test_passed_matrix_receipt_requires_exact_safe_60_tool_state() -> None:
    receipts = tuple(
        receipt_for(tool, {"status": "completed"}, {}) for tool in sorted(ALL_LOGICAL_TOOL_IDS)
    )
    state: dict[str, JsonValue] = {"state": {"count": 0, "fingerprint": "a" * 64}}
    receipt = SimToolMatrixReceipt(
        status="passed",
        tool_receipts=receipts,
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_completed=True,
        fixture_reference_validated=True,
        account_allowlist_resolved=True,
        auth_status_completed=True,
        session_capabilities_completed=True,
        before_state_fingerprint=state,
        after_state_fingerprint=state,
        uncleaned_resources=0,
        hosts=("gateway.saxobank.com",),
        live_events=0,
        live_mutation_calls=0,
        analytics_tool_receipt_count=len(ANALYTICS_TOOL_IDS),
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        cleanup_complete=True,
        account_state_unchanged=True,
        redacted_publication=True,
        errors=(),
    )
    assert receipt.status == "passed"

    with pytest.raises(ValidationError, match="60-tool"):
        SimToolMatrixReceipt.model_validate(
            {**receipt.model_dump(mode="json"), "live_mutation_calls": 1},
        )


def test_controlled_lifecycle_requires_sim_zero_live_cleanup_and_state_equality() -> None:
    before = _state()
    receipt = ControlledSimLifecycleReceipt(
        environment="SIM",
        cases=_lifecycle_cases(),
        before=before,
        after=before,
        live_events=0,
        live_mutation_calls=0,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=False,
        disclaimer_response_made=False,
    )
    assert receipt.evidence_state == "passed"

    unsafe_updates: tuple[dict[str, object], ...] = (
        {"live_events": 1},
        {"live_mutation_calls": 1},
        {"cleanup_complete": False},
        {"unchanged_account_state": False},
        {"redacted_publication": False},
        {"private_values_published": True},
        {"purchase_occurred": True},
        {"disclaimer_response_made": True},
        {"after": _state("b" * 64)},
    )
    for update in unsafe_updates:
        with pytest.raises(ValidationError):
            ControlledSimLifecycleReceipt.model_validate(
                {**receipt.model_dump(mode="json"), **update},
            )


def test_brokerage_state_requires_each_fingerprint_and_count_exactly_once() -> None:
    state = _state()
    assert tuple(item.name for item in state.components) == BROKERAGE_STATE_COMPONENTS
    with pytest.raises(ValidationError, match="components"):
        BrokerageStateFingerprint(components=(*state.components, state.components[0]))


def test_controlled_options_entitlement_can_reduce_without_fabricating_success() -> None:
    cases = list(_lifecycle_cases())
    option_index = CONTROLLED_SIM_CASES.index("options_entitlement")
    cases[option_index] = cases[option_index].model_copy(
        update={
            "state": "degraded",
            "reason_code": "options_entitlement_denied",
            "entitlement_state": "denied",
        },
    )
    state = _state()

    receipt = ControlledSimLifecycleReceipt(
        evidence_state="reduced",
        environment="SIM",
        cases=tuple(cases),
        before=state,
        after=state,
        live_events=0,
        live_mutation_calls=0,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=False,
        disclaimer_response_made=False,
    )

    assert receipt.evidence_state == "reduced"


def test_post_send_timeout_forbids_blind_retry_and_requires_reconciliation() -> None:
    receipt = PostSendTimeoutReceipt(
        operation_kind="controlled_sim_fixture",
        timeout_observed=True,
        stop_new_writes=True,
        reconciliation_attempted=True,
        reconciliation_state="no_effect_observed",
        matching_effect_count=0,
        blind_retry_attempted=False,
        recovery_action="refuse_retry",
        ledger_fingerprint_sha256="c" * 64,
    )
    assert receipt.evidence_state == "reconciled"

    with pytest.raises(ValidationError, match="blind retry"):
        PostSendTimeoutReceipt.model_validate(
            {**receipt.model_dump(mode="json"), "blind_retry_attempted": True},
        )
    with pytest.raises(ValidationError, match="reconciliation"):
        PostSendTimeoutReceipt.model_validate(
            {
                **receipt.model_dump(mode="json"),
                "reconciliation_attempted": False,
                "reconciliation_state": "not_attempted",
                "ledger_fingerprint_sha256": None,
            },
        )


def test_case_receipts_preserve_reduced_refusal_timeout_and_recovery_states() -> None:
    states: dict[AnalyticsCaseKind, AnalyticsCaseState] = {
        "success": "passed",
        "degradation": "degraded",
        "refusal": "refused",
        "privacy": "passed",
        "timeout": "timed_out",
        "recovery": "reconciled",
    }
    receipts = tuple(
        AnalyticsCaseReceipt(
            kind=kind,
            state=states[kind],
            reason_code=f"{kind}_observed",
            network_call_made=False,
            broker_write_made=False,
            private_values_published=False,
            evidence_sha256="d" * 64,
        )
        for kind in ANALYTICS_CASE_KINDS
    )
    assert tuple(item.kind for item in receipts) == ANALYTICS_CASE_KINDS
