from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Final, cast

import anyio
import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from pydantic import TypeAdapter, ValidationError

import saxo_bank_mcp.qa_sim_tool_matrix as matrix_module
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
    AnalyticsToolCaseEvidence,
    BrokerageStateComponent,
    BrokerageStateFingerprint,
    ControlledSimCaseReceipt,
    ControlledSimLifecycleReceipt,
    PostSendTimeoutReceipt,
    analytics_case_calls,
    analytics_case_contract_sha256,
    analytics_primary_calls,
    analytics_sim_contracts,
    assert_analytics_case_coverage,
    isolated_analytics_state,
    merge_matrix_receipts,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import (
    MatrixClient,
    MatrixToolObservation,
    receipt_for,
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    MatrixRuntimeState,
    PreflightFlags,
    SimToolMatrixReceipt,
)
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
            BrokerageStateComponent(
                name=name,
                count=0,
                fingerprint_sha256=digest,
                observed_state="available",
                mcp_tool_ids=("saxo_health",),
            )
            for name in BROKERAGE_STATE_COMPONENTS
        ),
    )


def _lifecycle_cases() -> tuple[ControlledSimCaseReceipt, ...]:
    return tuple(
        ControlledSimCaseReceipt(
            case_id=case_id,
            state="passed",
            reason_code="passed",
            source_request_count=0 if case_id == "cleanup" else 1,
            mcp_call_count=1,
            sim_mutation_call_count=0,
            cleanup_complete=True,
            entitlement_state=(
                "available" if case_id == "options_entitlement" else "not_applicable"
            ),
            evidence_sha256="f" * 64,
        )
        for case_id in CONTROLLED_SIM_CASES
    )


def _analytics_case_evidence() -> tuple[AnalyticsToolCaseEvidence, ...]:
    state_by_kind: dict[AnalyticsCaseKind, AnalyticsCaseState] = {
        "success": "passed",
        "degradation": "degraded",
        "refusal": "refused",
        "privacy": "passed",
        "timeout": "timed_out",
        "recovery": "refused",
    }
    return tuple(
        AnalyticsToolCaseEvidence(
            tool_id=contract.tool_id,
            cases=tuple(
                AnalyticsCaseReceipt(
                    kind=case.kind,
                    state=state_by_kind[case.kind],
                    reason_code=f"{case.kind}_observed",
                    mcp_call_observed=True,
                    result_parsed=case.kind != "timeout",
                    result_state=case.expected_states[0],
                    mcp_is_error=case.kind in {"refusal", "privacy", "timeout", "recovery"},
                    network_call_made=False,
                    broker_write_made=False,
                    private_values_published=False,
                    request_sha256="b" * 64,
                    response_sha256="c" * 64,
                    evidence_sha256="d" * 64,
                )
                for case in contract.cases
            ),
        )
        for contract in analytics_sim_contracts()
    )


def _runtime_state() -> MatrixRuntimeState:
    return MatrixRuntimeState(
        errors=[],
        hosts=set(),
        live_events=0,
        live_mutation_calls=0,
        receipts={},
        analytics_case_receipts=[],
        lifecycle_seen=set(),
        registered_ops=[],
        uncleaned=0,
        preflight=PreflightFlags(
            fixtures_ok=True,
            account_ok=True,
            auth_ok=True,
            session_ok=True,
        ),
        before=None,
        after=None,
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
    state = _state()
    case_evidence = _analytics_case_evidence()
    receipt = SimToolMatrixReceipt(
        status="passed",
        tool_receipts=receipts,
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_made=False,
        disclaimer_refusal_observed=True,
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
        analytics_case_receipts=case_evidence,
        mcp_only_account_fixture_state=True,
        cleanup_complete=True,
        account_state_unchanged=True,
        redacted_publication=True,
        errors=(),
    )
    assert receipt.status == "passed"

    with pytest.raises(ValidationError, match="60-tool"):
        SimToolMatrixReceipt.model_validate(
            {**receipt.model_dump(mode="python"), "live_mutation_calls": 1},
        )
    with pytest.raises(ValidationError, match="60-tool"):
        SimToolMatrixReceipt.model_validate(
            {
                **receipt.model_dump(mode="python"),
                "analytics_case_receipts": case_evidence[:-1],
            },
        )
    failed = receipt_for(
        receipts[0].tool,
        {"status": "failed", "result_parsed": False},
        {},
        status="failed",
    )
    with pytest.raises(ValidationError, match="60-tool"):
        SimToolMatrixReceipt.model_validate(
            {
                **receipt.model_dump(mode="python"),
                "tool_receipts": (failed, *receipts[1:]),
            },
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
        "recovery": "refused",
    }
    receipts = tuple(
        AnalyticsCaseReceipt(
            kind=kind,
            state=states[kind],
            reason_code=f"{kind}_observed",
            mcp_call_observed=True,
            result_parsed=kind != "timeout",
            result_state={
                "success": "passed",
                "degradation": "degraded",
                "refusal": "refused",
                "privacy": "refused",
                "timeout": "timed_out",
                "recovery": "refused",
            }[kind],
            mcp_is_error=kind in {"refusal", "privacy", "timeout", "recovery"},
            network_call_made=False,
            broker_write_made=False,
            private_values_published=False,
            request_sha256="b" * 64,
            response_sha256="c" * 64,
            evidence_sha256="d" * 64,
        )
        for kind in ANALYTICS_CASE_KINDS
    )
    assert tuple(item.kind for item in receipts) == ANALYTICS_CASE_KINDS


def test_matrix_never_discovers_or_answers_a_disclaimer() -> None:
    matrix_source = (
        Path(__file__).resolve().parents[1] / "src/saxo_bank_mcp/qa_sim_tool_matrix.py"
    ).read_text(encoding="utf-8")

    assert "discover_disclaimer" not in matrix_source
    assert '"Accepted"' not in matrix_source
    assert "disclaimer_response_made" in SimToolMatrixReceipt.model_fields
    assert "disclaimer_refusal_observed" in SimToolMatrixReceipt.model_fields
    assert "disclaimer_response_completed" not in SimToolMatrixReceipt.model_fields


def test_disclaimer_phase_calls_only_safe_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def refuse(
        _client: object,
        tool: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        calls.append((tool, arguments))
        return MatrixToolObservation(
            payload={"status": "refused", "network_call_made": False},
            result_parsed=True,
            result_state="refused",
            mcp_is_error=True,
        )

    monkeypatch.setattr(matrix_module, "call_tool", refuse)
    state = _runtime_state()
    anyio.run(
        matrix_module.run_disclaimer_refusal_phase,
        cast("MatrixClient", object()),
        state,
    )

    assert calls == [
        ("saxo_get_required_disclaimers", {}),
        ("saxo_register_disclaimer_response", {}),
    ]
    assert "Accepted" not in repr(calls)
    assert state.preflight.disclaimer_refusal_ok is True
    assert state.errors == []


def test_disclaimer_matrix_inputs_refuse_in_fastmcp_before_tool_execution() -> None:
    async def exercise() -> tuple[object, object]:
        async with Client(matrix_module.mcp) as client:
            lookup = await client.call_tool(
                "saxo_get_required_disclaimers",
                {},
                raise_on_error=False,
            )
            response = await client.call_tool(
                "saxo_register_disclaimer_response",
                {},
                raise_on_error=False,
            )
        return lookup, response

    lookup, response = anyio.run(exercise)
    for result in (lookup, response):
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert result.structured_content == {
            "status": "invalid_arguments",
            "message": "Tool input validation failed.",
        }


def test_matrix_execution_path_contains_no_direct_saxo_or_token_helpers() -> None:
    root = Path(__file__).resolve().parents[1]
    matrix_source = (root / "src/saxo_bank_mcp/qa_sim_tool_matrix.py").read_text(
        encoding="utf-8",
    )
    helper_source = (root / "src/saxo_bank_mcp/qa_sim_tool_matrix_helpers.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "resolve_sim_account_key",
        "cached_token_for_tool",
        "create_async_client",
        "raw_open_orders_for_matrix",
        "discover_pretrade_disclaimer_input",
    ):
        assert forbidden not in matrix_source
        assert forbidden not in helper_source


def test_failed_or_unparsed_payload_cannot_be_a_completed_receipt() -> None:
    with pytest.raises(ValueError, match="completed"):
        receipt_for(
            "saxo_analytics_capabilities",
            {"status": "failed", "result_parsed": False},
            {},
            status="completed",
        )


def test_passed_matrix_requires_every_applicable_analytics_case_receipt() -> None:
    calls = analytics_case_calls()
    expected = tuple(
        (contract.tool_id, case.kind)
        for contract in analytics_sim_contracts()
        for case in contract.cases
    )

    assert tuple((call.tool_id, call.kind) for call in calls) == expected


def test_analytics_phase_executes_and_checks_every_applicable_fastmcp_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_calls = analytics_case_calls()
    contracts = {
        (contract.tool_id, case.kind): case
        for contract in analytics_sim_contracts()
        for case in contract.cases
    }
    observed: list[tuple[str, dict[str, object], float | None]] = []

    async def result_for_case(
        _client: object,
        tool: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> MatrixToolObservation:
        call = expected_calls[len(observed)]
        assert (tool, arguments, timeout_seconds) == (
            call.tool_id,
            call.arguments,
            call.timeout_seconds,
        )
        observed.append((tool, arguments, timeout_seconds))
        contract = contracts[(call.tool_id, call.kind)]
        state = contract.expected_states[0]
        return MatrixToolObservation(
            payload={"status": state},
            result_parsed=call.kind != "timeout",
            result_state=state,
            mcp_is_error=call.kind in {"refusal", "privacy", "timeout", "recovery"},
            timed_out=call.kind == "timeout",
        )

    monkeypatch.setattr(matrix_module, "call_tool", result_for_case)
    state = _runtime_state()
    anyio.run(
        matrix_module.run_analytics_case_phase,
        cast("MatrixClient", object()),
        state,
    )

    assert len(observed) == len(expected_calls)
    assert len(state.analytics_case_receipts) == len(ANALYTICS_TOOL_IDS)
    assert set(state.receipts) == set(ANALYTICS_TOOL_IDS)
    assert state.errors == []


def test_state_fingerprint_uses_only_logical_mcp_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_tools: list[str] = []

    async def state_result(
        _client: object,
        tool: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed_tools.append(tool)
        if tool == "saxo_call_registered_endpoint":
            if arguments.get("response_mode") == "fingerprint_only":
                payload: dict[str, JsonValue] = {
                    "status": "passed",
                    "response": None,
                    "response_fingerprint": "f" * 64,
                }
            else:
                payload = {"status": "passed", "response": {"Data": []}}
        elif tool == "saxo_safety_status":
            payload = {
                "status": "passed",
                "local_subscription_count": 0,
                "pending_preview_count": 0,
                "committed_fingerprint_count": 0,
            }
        else:
            payload = {
                "status": "passed",
                "result": {
                    "runtime_state": {
                        "job_count": 0,
                        "cache_entry_count": 0,
                        "temporary_entry_count": 0,
                        "fingerprint_sha256": "e" * 64,
                    },
                },
            }
        return MatrixToolObservation(
            payload=payload,
            result_parsed=True,
            result_state="passed",
            mcp_is_error=False,
        )

    monkeypatch.setattr(matrix_module, "call_tool", state_result)
    state = _runtime_state()
    fingerprint = anyio.run(
        matrix_module.mcp_state_fingerprint,
        cast("MatrixClient", object()),
        state,
    )

    assert tuple(component.name for component in fingerprint.components) == (
        BROKERAGE_STATE_COMPONENTS
    )
    assert all(component.observed_state == "available" for component in fingerprint.components)
    assert set(observed_tools) == {
        "saxo_call_registered_endpoint",
        "saxo_safety_status",
        "saxo_list_analytics_storage",
    }


def test_controlled_lifecycle_requires_observed_mcp_and_source_calls() -> None:
    assert "mcp_call_count" in ControlledSimCaseReceipt.model_fields
    with pytest.raises(ValidationError, match="source"):
        ControlledSimCaseReceipt(
            case_id="transaction_history",
            state="passed",
            reason_code="passed",
            source_request_count=0,
            mcp_call_count=1,
            sim_mutation_call_count=0,
            cleanup_complete=True,
            entitlement_state="not_applicable",
            evidence_sha256="f" * 64,
        )


def test_state_fingerprint_covers_every_required_mcp_component() -> None:
    assert BROKERAGE_STATE_COMPONENTS == (
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
    assert "mcp_tool_ids" in BrokerageStateComponent.model_fields
