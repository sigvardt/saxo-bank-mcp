# pyright: reportPrivateUsage=false
from __future__ import annotations

import inspect
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

import saxo_bank_mcp.qa_analytics_sim as sim_module
import saxo_bank_mcp.qa_sim_tool_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_models import AnalysisId, DatasetId, JobId
from saxo_bank_mcp.analytics_store import StorageScope
from saxo_bank_mcp.analytics_strategy_schema import parse_strategy_definition
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
from saxo_bank_mcp.qa_analytics_evidence import load_analysis_kind_catalog
from saxo_bank_mcp.qa_analytics_sim import (
    ANALYSIS_KIND_IDS,
    ANALYTICS_CASE_KINDS,
    BROKERAGE_STATE_COMPONENTS,
    CONTROLLED_SIM_CASES,
    AnalyticsCaseKind,
    AnalyticsCaseReceipt,
    AnalyticsCaseState,
    AnalyticsRuntimeResources,
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
from saxo_bank_mcp.qa_sim_tool_matrix import (
    analytics_case_receipt,
    materialize_analytics_case_arguments,
    run_analytics_cleanup_cases,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import (
    MatrixClient,
    MatrixToolObservation,
    fixture_read_calls,
    receipt_for,
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    MatrixRuntimeState,
    MatrixScenarioReceipt,
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


def _reconciled_after(state: BrokerageStateFingerprint) -> BrokerageStateFingerprint:
    return BrokerageStateFingerprint(
        components=tuple(
            component.model_copy(
                update=(
                    {"fingerprint_sha256": "b" * 64}
                    if component.name == "balances"
                    else {
                        "count": component.count + 2,
                        "fingerprint_sha256": "c" * 64,
                    }
                    if component.name == "trade_messages"
                    else {}
                ),
            )
            for component in state.components
        ),
    )


def _observed_lifecycle_state(
    *,
    balance_digest: str,
    trade_digest: str,
    trade_count: int,
    order_count: int = 0,
    position_digest: str = "3" * 64,
) -> BrokerageStateFingerprint:
    digests = {
        "balances": balance_digest,
        "positions": position_digest,
        "orders": "2" * 64,
        "trade_messages": trade_digest,
        "subscriptions": "5" * 64,
        "previews_write_state": "6" * 64,
        "jobs": "7" * 64,
        "caches": "8" * 64,
        "temporary_files": "9" * 64,
    }
    counts = {"positions": 8, "orders": order_count, "trade_messages": trade_count}
    return BrokerageStateFingerprint(
        components=tuple(
            BrokerageStateComponent(
                name=name,
                count=counts.get(name, 0),
                fingerprint_sha256=digests[name],
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
            sim_mutation_call_count=2 if case_id in {"ghost_portfolio", "cleanup"} else 0,
            cleanup_complete=True,
            entitlement_state=(
                "available" if case_id == "options_entitlement" else "not_applicable"
            ),
            evidence_sha256="f" * 64,
        )
        for case_id in CONTROLLED_SIM_CASES
    )


def _lifecycle_receipt(
    state: BrokerageStateFingerprint,
) -> ControlledSimLifecycleReceipt:
    return ControlledSimLifecycleReceipt(
        environment="SIM",
        cases=_lifecycle_cases(),
        before=state,
        after=_reconciled_after(state),
        live_events=0,
        live_mutation_calls=0,
        request_ledger_read_last=True,
        request_ledger_complete=True,
        request_ledger_fingerprint_sha256="9" * 64,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=False,
        disclaimer_response_made=False,
    )


def _analytics_case_evidence() -> tuple[AnalyticsToolCaseEvidence, ...]:
    state_by_kind: dict[AnalyticsCaseKind, AnalyticsCaseState] = {
        "success": "passed",
        "degradation": "degraded",
        "refusal": "refused",
        "privacy": "passed",
        "timeout": "timed_out",
        "recovery": "reconciled",
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
                    mcp_is_error=case.kind in {"refusal", "privacy", "timeout"},
                    network_call_made=False,
                    broker_write_made=False,
                    private_values_published=False,
                    request_sha256="b" * 64,
                    response_sha256="c" * 64,
                    evidence_sha256="d" * 64,
                    reconciles_request_sha256=("e" * 64 if case.kind == "recovery" else None),
                    reconciliation_observation_sha256=(
                        "f" * 64 if case.kind == "recovery" else None
                    ),
                )
                for case in contract.cases
            ),
        )
        for contract in analytics_sim_contracts()
    )


def _analysis_execution_receipts() -> tuple[AnalyticsCaseReceipt, ...]:
    receipts: list[AnalyticsCaseReceipt] = []
    calls = tuple(
        call
        for call in analytics_case_calls()
        if call.kind == "success" and call.analysis_kind is not None
    )
    for index, call in enumerate(calls):
        persisted = call.expected_analysis_outcome == "persisted"
        resolved = call.expected_analysis_outcome == "resolved"
        receipts.append(
            AnalyticsCaseReceipt(
                kind="success",
                tool_id=call.tool_id,
                state="passed" if persisted or resolved else "refused",
                reason_code="success_observed",
                analysis_kind=call.analysis_kind,
                returned_analysis_kind=call.analysis_kind if persisted else None,
                analysis_id=f"an_{index:032x}" if persisted else None,
                expected_analysis_outcome=call.expected_analysis_outcome,
                persisted_result_authenticated=persisted,
                mcp_call_observed=True,
                result_parsed=True,
                result_state="verified" if persisted else "resolved" if resolved else "refused",
                mcp_is_error=not (persisted or resolved),
                network_call_made=False,
                broker_write_made=False,
                private_values_published=False,
                request_sha256="1" * 64,
                response_sha256="2" * 64,
                evidence_sha256="3" * 64,
            ),
        )
    return tuple(receipts)


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


def _complete_runtime_state_for_finalize() -> MatrixRuntimeState:
    receipts = tuple(
        receipt_for(tool, {"status": "completed"}, {}) for tool in sorted(ALL_LOGICAL_TOOL_IDS)
    )
    before = _state()
    lifecycle = _lifecycle_receipt(before)
    state = _runtime_state()
    state.receipts = {receipt.tool: receipt for receipt in receipts}
    state.analytics_case_receipts = list(_analytics_case_evidence())
    state.lifecycle_seen.update(matrix_module.LIFECYCLE_TOOLS)
    state.registered_ops.extend(spec.operation_id for spec in matrix_module.trading_write_specs())
    state.preflight = PreflightFlags(
        fixtures_ok=True,
        account_ok=True,
        auth_ok=True,
        session_ok=True,
        disclaimer_refusal_ok=True,
    )
    state.before = lifecycle.before
    state.after = lifecycle.after
    state.analysis_execution_receipts = list(_analysis_execution_receipts())
    state.controlled_sim_lifecycle = lifecycle
    state.analytics_resources.cleanup_verified = True
    return state


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
        assert "refusal" in kinds
        assert "privacy" in kinds
        assert set(kinds) <= set(ANALYTICS_CASE_KINDS)
        if "timeout" in kinds:
            assert "recovery" in kinds


def test_analytics_success_contracts_cover_every_tool_with_session_issued_inputs() -> None:
    expected = {
        tool_id: sim_module._SUCCESS_STATES_BY_TOOL[tool_id]  # noqa: SLF001
        for tool_id in ANALYTICS_TOOL_IDS
    }
    observed = {
        contract.tool_id: success.expected_states
        for contract in analytics_sim_contracts()
        if (
            success := next(
                (case for case in contract.cases if case.kind == "success"),
                None,
            )
        )
        is not None
    }

    assert observed == expected
    assert tuple(observed) == ANALYTICS_TOOL_IDS


def test_controlled_ghost_source_strategy_parses_at_the_json_boundary() -> None:
    arguments = dict(analytics_primary_calls())["saxo_backtest_strategy"]
    request = arguments["request"]
    assert isinstance(request, dict)
    strategy = request["strategy"]
    assert isinstance(strategy, dict)
    split = strategy["evaluation_split"]
    assert isinstance(split, dict)
    assert isinstance(split["train_end_at"], str)
    assert isinstance(split["holdout_start_at"], str)

    parsed = parse_strategy_definition(strategy)

    assert parsed.evaluation_split.kind == "holdout"


def test_controlled_sim_order_body_matches_the_registered_place_contract() -> None:
    fixtures = matrix_module.MatrixFixtures(
        stock_uic=211,
        amount=1,
        limit_price=50,
        modified_limit_price=51,
        option_uics=(30004846, 30004926),
        stream_uic=21,
    )

    body = matrix_module._controlled_sim_order_body(  # noqa: SLF001
        "sim",
        fixtures,
        observed_limit_price="99.00",
    )

    assert body == {
        "AccountKey": "sim",
        "Uic": 211,
        "AssetType": "Stock",
        "Amount": 1,
        "BuySell": "Buy",
        "ManualOrder": False,
        "OrderType": "Limit",
        "OrderPrice": 99.0,
        "OrderDuration": {"DurationType": "DayOrder"},
        "ExternalReference": "task24-controlled-ghost",
    }


def test_controlled_sim_write_refuses_preexisting_orders_before_any_mutation(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _runtime_state()
    state.before = _observed_lifecycle_state(
        balance_digest="1" * 64,
        trade_digest="4" * 64,
        trade_count=399,
        order_count=1,
    )
    state.analytics_resources.account_selectors.append("sim")
    state.analytics_resources.controlled_order_limit_price = "99.0"
    fixtures = matrix_module.MatrixFixtures(
        stock_uic=211,
        amount=1,
        limit_price=50,
        modified_limit_price=51,
        option_uics=(30004846, 30004926),
        stream_uic=21,
    )
    primary_request = cast(
        "dict[str, JsonValue]",
        dict(analytics_primary_calls())["saxo_backtest_strategy"]["request"],
    )
    backtest_call = sim_module.AnalyticsCaseCall(
        tool_id="saxo_backtest_strategy",
        kind="success",
        arguments={
            "request": {
                **primary_request,
                "dataset_id": "ds_11111111111141118111111111111111",
                "instrument_handle": "ih_22222222222242228222222222222222",
            },
        },
        input_strategy="controlled_sim_fixture",
    )
    observed_tools: list[str] = []

    class Recorder:
        def candidate_commit(self) -> str:
            return "a" * 40

        def controlled_backtest_source_binding(
            self,
            _dataset_id: str,
            _instrument_handle: str,
            *,
            expected_uic: int,
            expected_asset_type: str,
        ) -> str:
            assert (expected_uic, expected_asset_type) == (211, "Stock")
            return "aa_00000000000040008000000000000072"

        def prepare_controlled_sim_safety(
            self,
            _account_selector: str,
            *,
            expected_uic: int,
        ) -> None:
            raise AssertionError(f"unsafe preparation for {expected_uic=}")

        def clear_controlled_sim_safety(self) -> None:
            return None

        def record_observed_ghost_lifecycle(
            self,
            _evidence: object,
            *,
            ledger_provenance_sha256: str,
        ) -> None:
            raise AssertionError(f"unexpected proof receipt {ledger_provenance_sha256}")

    async def stable_fingerprint(
        _client: object,
        _state: MatrixRuntimeState,
    ) -> BrokerageStateFingerprint:
        assert state.before is not None
        return state.before

    async def observe_tool(
        _client: object,
        tool: str,
        _arguments: dict[str, JsonValue],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed_tools.append(tool)
        if tool == "saxo_create_order_preview":
            payload: dict[str, JsonValue] = {
                "status": "preview_created",
                "preview_token": "pv",
            }
            result_state = "preview_created"
        elif tool == "saxo_place_sim_order":
            payload = {
                "status": "completed",
                "safe_cancel_by_instrument": {
                    "write_preview_arguments": {"request": "cancel"},
                },
            }
            result_state = "completed"
        elif tool == "saxo_create_write_preview":
            payload = {
                "status": "preview_created",
                "preview_token": "cv",
            }
            result_state = "preview_created"
        elif tool == "saxo_cancel_sim_orders_by_instrument":
            payload = {"status": "completed"}
            result_state = "completed"
        else:
            assert tool == "saxo_get_safe_request_ledger"
            payload = {
                "status": "passed",
                "scope": "current_mcp_session",
                "ledger_complete": True,
                "events_evicted": 0,
                "events": [],
            }
            result_state = "passed"
        return MatrixToolObservation(
            payload=payload,
            result_parsed=True,
            result_state=result_state,
            mcp_is_error=False,
        )

    async def no_wait() -> None:
        return None

    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setattr(matrix_module, "mcp_state_fingerprint", stable_fingerprint)
    monkeypatch.setattr(matrix_module, "call_tool", observe_tool)
    monkeypatch.setattr(matrix_module, "_wait_for_sim_write_rate_limit", no_wait)

    async def run_controlled_phase() -> matrix_module._ControlledGhostObservation:
        return await matrix_module._run_controlled_sim_ghost_phase(  # noqa: SLF001
            cast("MatrixClient", object()),
            state,
            backtest_call,
            fixtures,
            proof_recorder=Recorder(),  # type: ignore[arg-type]
        )

    observation = anyio.run(run_controlled_phase)

    assert observation.reason_code == "controlled_ghost_preexisting_orders"
    assert observation.sim_mutation_call_count == 0
    assert observed_tools == ["saxo_get_safe_request_ledger"]


def test_controlled_limit_price_is_derived_below_one_observed_bid() -> None:
    payload: dict[str, JsonValue] = {
        "status": "passed",
        "response": {
            "Quote": {
                "Bid": 100.0,
                "Ask": 100.2,
            },
        },
    }

    assert matrix_module._observed_controlled_limit_price(payload) == "99.0"  # noqa: SLF001
    assert (
        matrix_module._observed_controlled_limit_price(  # noqa: SLF001
            {"status": "passed", "response": {"Quote": {"Bid": 0, "Ask": 1}}},
        )
        is None
    )
    assert (
        matrix_module._observed_controlled_limit_price(  # noqa: SLF001
            {"status": "passed", "response": {"Quote": {"Bid": "NaN", "Ask": 1}}},
        )
        is None
    )
    assert (
        matrix_module._observed_controlled_limit_price(  # noqa: SLF001
            {
                "status": "passed",
                "response": [
                    {"Quote": {"Bid": 100, "Ask": 101}},
                    {"Quote": {"Bid": 200, "Ask": 201}},
                ],
            },
        )
        is None
    )
    assert (
        matrix_module._observed_controlled_limit_price(  # noqa: SLF001
            {"status": "passed", "response": {"PriceInfo": {"Bid": 100, "Ask": 101}}},
        )
        is None
    )


def test_fixture_preflight_reads_current_stock_price_through_registered_mcp() -> None:
    fixtures = matrix_module.MatrixFixtures(
        stock_uic=211,
        amount=1,
        limit_price=50,
        modified_limit_price=51,
        option_uics=(30004846, 30004926),
        stream_uic=21,
    )

    assert fixture_read_calls(fixtures)[-1] == (
        "saxo_call_registered_endpoint",
        {
            "method": "GET",
            "path": "/trade/v1/infoprices",
            "params": {"Amount": "1", "AssetType": "Stock", "Uic": "211"},
        },
    )


def test_case_plan_uses_distinct_inputs_and_exact_reconciliation() -> None:
    calls = analytics_case_calls()
    by_case = {(call.tool_id, call.kind): call for call in calls if call.analysis_kind is None}
    for contract in analytics_sim_contracts():
        kinds = {case.kind for case in contract.cases}
        if {"success", "degradation"} <= kinds:
            success = by_case[(contract.tool_id, "success")]
            degradation = by_case[(contract.tool_id, "degradation")]
            assert success.input_strategy != degradation.input_strategy
            assert success.arguments != degradation.arguments
        if "recovery" in kinds:
            recovery = by_case[(contract.tool_id, "recovery")]
            assert recovery.input_strategy == "observe_exact_timed_operation"
            assert recovery.arguments != {"__qa_schema_extra_rejection__": True}
            assert recovery.reconciles_kind == "timeout"


def test_real_success_result_states_are_not_marked_failed() -> None:
    contracts = {
        contract.tool_id: success
        for contract in analytics_sim_contracts()
        if (
            success := next(
                (case for case in contract.cases if case.kind == "success"),
                None,
            )
        )
        is not None
    }
    calls = {
        call.tool_id: call
        for call in analytics_case_calls()
        if call.kind == "success" and call.analysis_kind is None
    }
    for tool, observed_state in (
        ("saxo_resolve_research_universe", "resolved"),
        ("saxo_preview_analytics_deletion", "preview_ready"),
        ("saxo_delete_analytics_data", "deleted"),
    ):
        receipt = analytics_case_receipt(
            calls[tool],
            contracts[tool].expected_states,
            MatrixToolObservation(
                payload={"status": observed_state},
                result_parsed=True,
                result_state=observed_state,
                mcp_is_error=False,
            ),
        )
        assert receipt.state == "passed"


def test_runtime_arguments_chain_server_issued_dataset_handles() -> None:
    resources = AnalyticsRuntimeResources()
    resources.dataset_ids.append("ds_11111111111141118111111111111111")
    calls = {
        (call.tool_id, call.kind): call
        for call in analytics_case_calls()
        if call.analysis_kind is None
    }

    dataset_success = materialize_analytics_case_arguments(
        calls[("saxo_get_research_dataset", "success")], resources
    )
    assert dataset_success == {"dataset_id": resources.dataset_ids[0], "page": 1, "limit": 100}
    assert resources.timed_operation is None


def test_analysis_successes_require_their_exact_server_issued_input_kind() -> None:
    calls = {
        (call.tool_id, call.kind): call
        for call in analytics_case_calls()
        if call.analysis_kind is None
    }
    routes = {
        "saxo_analyze_market": "price_bars",
        "saxo_analyze_instruments": "price_bars",
        "saxo_analyze_portfolio": "portfolio_performance",
        "saxo_size_position": "position_sizing",
        "saxo_run_scenario": "scenario_custom",
        "saxo_optimize_portfolio": "portfolio_minimum_variance",
        "saxo_model_derivatives": "derivatives_model",
        "saxo_backtest_strategy": "bounded_backtest",
    }
    for index, (tool_id, route) in enumerate(routes.items(), start=1):
        resources = AnalyticsRuntimeResources()
        resources.instrument_handles.append("ih_22222222222242228222222222222222")
        assert (
            materialize_analytics_case_arguments(
                calls[(tool_id, "success")],
                resources,
            )
            == {}
        )
        dataset_id = f"ds_{index:032x}"
        resources.dataset_ids_by_analysis_kind[route] = [dataset_id]
        materialized = materialize_analytics_case_arguments(
            calls[(tool_id, "success")],
            resources,
        )
        request = cast("dict[str, JsonValue]", materialized["request"])
        selected = request.get("dataset_id")
        if selected is None:
            selected = cast("list[JsonValue]", request["dataset_ids"])[0]
        assert selected == dataset_id

    pretrade_resources = AnalyticsRuntimeResources()
    assert (
        materialize_analytics_case_arguments(
            calls[("saxo_propose_trade_from_analysis", "success")],
            pretrade_resources,
        )
        == dict(analytics_primary_calls())["saxo_propose_trade_from_analysis"]
    )
    pretrade_resources.instrument_handles.append("ih_22222222222242228222222222222222")
    pretrade_resources.analysis_ids_by_kind["instrument_price_return"] = [
        "an_33333333333343338333333333333333"
    ]
    pretrade_resources.pretrade_proposal_price = "100"
    materialized_pretrade = materialize_analytics_case_arguments(
        calls[("saxo_propose_trade_from_analysis", "success")],
        pretrade_resources,
    )
    assert materialized_pretrade["proposal_price"] == "100"


def test_backtest_materialization_binds_holdout_to_observed_input_coverage() -> None:
    dataset_id = "ds_88888888888848888888888888888888"
    instrument_handle = "ih_22222222222242228222222222222222"
    resources = AnalyticsRuntimeResources(instrument_handles=[instrument_handle])
    matrix_module._remember_typed_resources(  # noqa: SLF001
        resources,
        {
            "result": {
                "dataset_id": dataset_id,
                "data_kind": "analysis_input",
                "analysis_kind": "bounded_backtest",
                "instrument_handles": [instrument_handle],
                "quality_state": "complete",
                "coverage_start": "2026-01-05T14:30:00Z",
                "coverage_end": "2026-01-05T15:30:00Z",
            },
        },
        degraded=False,
    )
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_backtest_strategy"
        and item.kind == "success"
        and item.analysis_kind is None
    )

    arguments = materialize_analytics_case_arguments(call, resources)
    request = cast("dict[str, JsonValue]", arguments["request"])
    strategy = cast("dict[str, JsonValue]", request["strategy"])
    split = cast("dict[str, JsonValue]", strategy["evaluation_split"])

    assert request["dataset_id"] == dataset_id
    assert split == {
        "kind": "holdout",
        "train_end_at": "2026-01-05T14:30:00Z",
        "holdout_start_at": "2026-01-05T15:30:00Z",
    }


def test_missing_analysis_handles_still_exercise_downstream_refusal_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_WRITES", raising=False)
    tool_ids = (
        "saxo_explain_analysis",
        "saxo_export_analysis",
        "saxo_manage_analysis_job",
        "saxo_propose_trade_from_analysis",
        "saxo_render_analysis",
    )
    calls = {
        call.tool_id: call
        for call in analytics_case_calls()
        if call.kind == "success" and call.analysis_kind is None and call.tool_id in tool_ids
    }
    resources = AnalyticsRuntimeResources()

    async def exercise() -> list[tuple[str, dict[str, JsonValue], MatrixToolObservation]]:
        observations: list[tuple[str, dict[str, JsonValue], MatrixToolObservation]] = []
        async with Client(matrix_module.mcp) as client:
            for tool_id in tool_ids:
                arguments = materialize_analytics_case_arguments(calls[tool_id], resources)
                observation = await matrix_module.call_tool(client, tool_id, arguments)
                observations.append((tool_id, arguments, observation))
        return observations

    with isolated_analytics_state(tmp_path / "matrix-state"):
        observations = anyio.run(exercise)

    assert len(observations) == len(tool_ids)
    for _tool_id, arguments, observation in observations:
        assert arguments
        assert observation.result_parsed is True
        assert observation.result_state == "refused"
        assert observation.mcp_is_error is False
        assert observation.payload.get("network_call_made") is not True


def test_analysis_success_prefers_server_issued_typed_input_over_raw_source() -> None:
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_run_scenario"
        and item.kind == "success"
        and item.analysis_kind is None
    )
    raw_source_id = "ds_11111111111141118111111111111111"
    typed_input_id = "ds_22222222222242228222222222222222"
    resources = AnalyticsRuntimeResources(
        instrument_handles=["ih_33333333333343338333333333333333"],
        dataset_ids_by_analysis_kind={
            "scenario_custom": [raw_source_id, typed_input_id],
        },
        analysis_input_dataset_ids_by_analysis_kind={
            "scenario_custom": [typed_input_id],
        },
    )

    materialized = materialize_analytics_case_arguments(call, resources)
    request = cast("dict[str, JsonValue]", materialized["request"])

    assert request["dataset_id"] == typed_input_id


def test_pretrade_price_is_observed_from_one_exact_logical_mcp_quote() -> None:
    quote_row: dict[str, JsonValue] = {
        "row_kind": "quote",
        "bid_value": 99.0,
        "ask_value": 101.0,
        "mid_value": 100.0,
    }
    quote: dict[str, JsonValue] = {
        "status": "passed",
        "result": {"rows": [quote_row]},
    }

    assert matrix_module._observed_quote_midpoint(quote) == "100.0"  # noqa: SLF001
    assert matrix_module._observed_quote_midpoint({"status": "passed"}) is None  # noqa: SLF001
    assert (
        matrix_module._observed_quote_midpoint(  # noqa: SLF001
            {"result": {"rows": [quote_row, quote_row]}},
        )
        is None
    )


def test_analysis_refusal_probes_use_only_same_session_issued_dataset_handles() -> None:
    calls = {
        (call.tool_id, call.kind): call
        for call in analytics_case_calls()
        if call.analysis_kind is None
    }
    resources = AnalyticsRuntimeResources()
    dataset_id = "ds_11111111111141118111111111111111"
    instrument_handle = "ih_22222222222242228222222222222222"
    resources.dataset_ids.append(dataset_id)
    resources.dataset_ids_by_analysis_kind["price_bars"] = [dataset_id]
    resources.instrument_handles.append(instrument_handle)

    for tool_id in ("saxo_analyze_market", "saxo_analyze_instruments"):
        materialized = materialize_analytics_case_arguments(
            calls[(tool_id, "degradation")],
            resources,
        )
        request = cast("dict[str, JsonValue]", materialized["request"])
        selected = request.get("dataset_id")
        if selected is None:
            selected = cast("list[JsonValue]", request["dataset_ids"])[0]
        assert selected == dataset_id

    for tool_id in (
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
    ):
        assert (
            materialize_analytics_case_arguments(
                calls[(tool_id, "degradation")],
                resources,
            )
            == {}
        )


def test_analysis_runtime_indexes_only_typed_server_issued_handles() -> None:
    calls = {
        (call.tool_id, call.kind): call
        for call in analytics_case_calls()
        if call.analysis_kind is None
    }
    resources = AnalyticsRuntimeResources()
    dataset_id = "ds_11111111111141118111111111111111"
    untyped_dataset_id = "ds_22222222222242228222222222222222"
    analysis_id = "an_33333333333343338333333333333333"

    matrix_module._remember_analytics_handles(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        resources,
        calls[("saxo_sync_research_data", "success")],
        MatrixToolObservation(
            payload={
                "status": "passed",
                "result": {
                    "datasets": [
                        {
                            "dataset_id": dataset_id,
                            "data_kind": "portfolio_performance",
                            "contract_id": "transactions_v1",
                        },
                        {"dataset_id": untyped_dataset_id},
                    ],
                },
            },
            result_parsed=True,
            result_state="passed",
            mcp_is_error=False,
        ),
    )
    matrix_module._remember_analytics_handles(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        resources,
        calls[("saxo_analyze_portfolio", "degradation")],
        MatrixToolObservation(
            payload={
                "status": "verified",
                "analysis_id": analysis_id,
                "analysis_kind": "portfolio_performance",
            },
            result_parsed=True,
            result_state="verified",
            mcp_is_error=False,
        ),
    )

    assert resources.dataset_ids_by_analysis_kind == {
        "portfolio_performance": [dataset_id],
    }
    assert resources.degraded_analysis_ids_by_kind == {
        "portfolio_performance": [analysis_id],
    }
    assert resources.source_contract_ids == {"transactions_v1"}
    assert untyped_dataset_id in resources.dataset_ids


def test_degraded_sync_indexes_each_typed_dataset_by_its_own_quality() -> None:
    resources = AnalyticsRuntimeResources()
    complete_dataset_id = "ds_11111111111141118111111111111111"
    partial_dataset_id = "ds_22222222222242228222222222222222"
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_sync_research_data" and item.kind == "success"
    )

    matrix_module._remember_analytics_handles(  # noqa: SLF001
        resources,
        call,
        MatrixToolObservation(
            payload={
                "status": "degraded",
                "result": {
                    "datasets": [
                        {
                            "dataset_id": complete_dataset_id,
                            "data_kind": "account_analytics",
                            "quality_state": "complete",
                            "eligible_analysis_kinds": ["position_sizing"],
                        },
                        {
                            "dataset_id": partial_dataset_id,
                            "data_kind": "quote",
                            "quality_state": "partial",
                            "eligible_analysis_kinds": ["pretrade_impact"],
                        },
                    ],
                },
            },
            result_parsed=True,
            result_state="degraded",
            mcp_is_error=False,
        ),
    )

    assert resources.dataset_ids_by_analysis_kind == {
        "account_analytics": [complete_dataset_id],
        "position_sizing": [complete_dataset_id],
    }
    assert resources.degraded_dataset_ids_by_analysis_kind == {
        "quote": [partial_dataset_id],
        "pretrade_impact": [partial_dataset_id],
    }


def test_market_capture_keeps_quote_when_option_chain_capture_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = AnalyticsRuntimeResources(
        instrument_handles=["ih_11111111111141118111111111111111"],
        option_expiries=["2026-09-18"],
    )
    state = _runtime_state()
    state.analytics_resources = resources
    quote_dataset_id = "ds_22222222222242228222222222222222"
    sync_items: list[list[JsonValue]] = []

    async def capture(
        _client: object,
        tool: str,
        arguments: dict[str, JsonValue],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        if tool == "saxo_get_research_dataset":
            return MatrixToolObservation(
                payload={
                    "status": "passed",
                    "result": {
                        "rows": [
                            {
                                "row_kind": "quote",
                                "bid_value": 99,
                                "ask_value": 101,
                            },
                        ],
                    },
                },
                result_parsed=True,
                result_state="passed",
                mcp_is_error=False,
            )
        request = cast("dict[str, JsonValue]", arguments["request"])
        items = cast("list[JsonValue]", request["items"])
        sync_items.append(items)
        item = cast("dict[str, JsonValue]", items[0])
        if item["data_kind"] == "quote":
            return MatrixToolObservation(
                payload={
                    "status": "passed",
                    "result": {
                        "datasets": [
                            {
                                "dataset_id": quote_dataset_id,
                                "data_kind": "quote",
                                "quality_state": "complete",
                            },
                        ],
                    },
                },
                result_parsed=True,
                result_state="passed",
                mcp_is_error=False,
            )
        return MatrixToolObservation(
            payload={"status": "refused", "reason_code": "source_http_error"},
            result_parsed=True,
            result_state="refused",
            mcp_is_error=True,
        )

    monkeypatch.setattr(matrix_module, "call_tool", capture)

    anyio.run(
        matrix_module._prepare_server_owned_analysis_inputs,  # noqa: SLF001
        cast("MatrixClient", object()),
        state,
    )

    assert [cast("dict[str, JsonValue]", items[0])["data_kind"] for items in sync_items[:2]] == [
        "quote",
        "option_chain",
    ]
    assert all(len(items) == 1 for items in sync_items[:2])
    assert resources.dataset_ids_by_analysis_kind["quote"] == [quote_dataset_id]
    assert resources.pretrade_proposal_price == "100"


def test_scenario_arguments_use_every_server_issued_context_instrument() -> None:
    first_handle = "ih_11111111111141118111111111111111"
    second_handle = "ih_22222222222242228222222222222222"
    dataset_id = "ds_33333333333343338333333333333333"
    resources = AnalyticsRuntimeResources(
        analysis_input_dataset_ids_by_analysis_kind={"scenario_custom": [dataset_id]},
        analysis_input_instrument_handles_by_analysis_kind={
            "scenario_custom": [first_handle, second_handle],
        },
    )
    call = next(item for item in analytics_case_calls() if item.analysis_kind == "scenario_custom")

    materialized = materialize_analytics_case_arguments(call, resources)
    request = cast("dict[str, JsonValue]", materialized["request"])
    shocks = cast("list[JsonValue]", request["shocks"])

    assert request["dataset_id"] == dataset_id
    assert [cast("dict[str, JsonValue]", shock)["instrument_handle"] for shock in shocks] == [
        first_handle,
        second_handle,
    ]
    assert all(
        cast("dict[str, JsonValue]", shock)["price_shock_ratio"] == "-0.1" for shock in shocks
    )


@pytest.mark.parametrize("analysis_kind", ["margin_fire_drill", "scenario_currency"])
def test_linear_context_uses_zero_component_shocks_for_non_price_scenarios(
    analysis_kind: str,
) -> None:
    instrument_handle = "ih_11111111111141118111111111111111"
    dataset_id = "ds_22222222222242228222222222222222"
    resources = AnalyticsRuntimeResources(
        analysis_input_dataset_ids_by_analysis_kind={"scenario_custom": [dataset_id]},
        analysis_input_instrument_handles_by_analysis_kind={
            "scenario_custom": [instrument_handle],
        },
    )
    call = next(item for item in analytics_case_calls() if item.analysis_kind == analysis_kind)

    materialized = materialize_analytics_case_arguments(call, resources)
    request = cast("dict[str, JsonValue]", materialized["request"])
    shock = cast("dict[str, JsonValue]", cast("list[JsonValue]", request["shocks"])[0])

    assert shock == {
        "instrument_handle": instrument_handle,
        "price_shock_ratio": "0",
    }


@pytest.mark.parametrize("analysis_kind", ["scenario_rate", "scenario_volatility"])
def test_non_linear_scenarios_are_declared_as_honest_current_source_refusals(
    analysis_kind: str,
) -> None:
    call = next(item for item in analytics_case_calls() if item.analysis_kind == analysis_kind)

    assert call.expected_analysis_outcome == "refused"


def test_render_success_uses_html_when_pixel_qa_cannot_be_prevalidated() -> None:
    analysis_id = "an_11111111111141118111111111111111"
    resources = AnalyticsRuntimeResources(analysis_ids=[analysis_id])
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_render_analysis" and item.kind == "success"
    )

    materialized = materialize_analytics_case_arguments(call, resources)

    assert materialized == {
        "analysis_id": analysis_id,
        "template_id": "relative_performance",
        "output_format": "html",
    }


def test_degradation_analysis_uses_primary_typed_context_for_a_valid_refusal() -> None:
    dataset_id = "ds_11111111111141118111111111111111"
    instrument_handle = "ih_22222222222242228222222222222222"
    resources = AnalyticsRuntimeResources(
        instrument_handles=[instrument_handle],
        analysis_input_dataset_ids_by_analysis_kind={"scenario_custom": [dataset_id]},
        analysis_input_instrument_handles_by_analysis_kind={
            "scenario_custom": [instrument_handle],
        },
    )
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_run_scenario" and item.kind == "degradation"
    )

    materialized = materialize_analytics_case_arguments(call, resources)
    request = cast("dict[str, JsonValue]", materialized["request"])

    assert request["dataset_id"] == dataset_id
    assert request["caller_accepted_numeric_shocks"] is False


def test_base_instrument_result_prepares_pretrade_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis_id = "an_11111111111141118111111111111111"
    prepared = 0

    async def prepare(_client: object, _state: MatrixRuntimeState) -> None:
        nonlocal prepared
        prepared += 1

    monkeypatch.setattr(matrix_module, "_prepare_server_owned_pretrade_input", prepare)
    state = _runtime_state()
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_analyze_instruments"
        and item.kind == "success"
        and item.analysis_kind is None
    )
    result = MatrixToolObservation(
        payload={
            "status": "verified",
            "analysis_kind": "instrument_price_return",
            "analysis_id": analysis_id,
        },
        result_parsed=True,
        result_state="verified",
        mcp_is_error=False,
    )
    receipt = analytics_case_receipt(call, ("verified",), result)

    anyio.run(
        matrix_module._remember_analysis_case_outputs,  # noqa: SLF001
        cast("MatrixClient", object()),
        state,
        call,
        result,
        receipt,
    )

    assert prepared == 1


def test_analytics_cleanup_consumes_issued_token_and_verifies_empty_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _runtime_state()
    state.analytics_resources.dataset_ids.append("ds_11111111111141118111111111111111")
    state.analytics_resources.job_ids.append("jb_22222222222242228222222222222222")
    issued_deletion_handle = "dp_33333333333343338333333333333333"
    observed: list[tuple[str, dict[str, object]]] = []

    async def local_result(
        _client: object,
        tool: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed.append((tool, arguments))
        if tool == "saxo_manage_analysis_job":
            return MatrixToolObservation(
                payload={"status": "job_completed"},
                result_parsed=True,
                result_state="job_completed",
                mcp_is_error=False,
            )
        if tool == "saxo_list_analytics_storage":
            already_deleted = any(name == "saxo_delete_analytics_data" for name, _ in observed)
            entries: list[JsonValue] = []
            if not already_deleted:
                entries = [
                    {
                        "data_type": "safe_instruments",
                        "object_id": "ih_55555555555545558555555555555555",
                    },
                    {
                        "data_type": "datasets",
                        "object_id": state.analytics_resources.dataset_ids[0],
                    },
                    {
                        "data_type": "jobs",
                        "object_id": state.analytics_resources.job_ids[0],
                    },
                ]
            payload: dict[str, JsonValue] = {
                "status": "passed",
                "result": {
                    "entries": entries,
                    "runtime_state": {
                        "job_count": len(entries),
                        "cache_entry_count": len(entries),
                        "temporary_entry_count": 0,
                        "fingerprint_sha256": "a" * 64,
                    },
                },
            }
            return MatrixToolObservation(
                payload=payload,
                result_parsed=True,
                result_state="passed",
                mcp_is_error=False,
            )
        if tool == "saxo_preview_analytics_deletion":
            assert arguments == {
                "scope": {"data_types": ["datasets", "jobs", "safe_instruments"]},
            }
            return MatrixToolObservation(
                payload={
                    "status": "preview_ready",
                    "result": {"preview": {"token": issued_deletion_handle}},
                },
                result_parsed=True,
                result_state="preview_ready",
                mcp_is_error=False,
            )
        assert tool == "saxo_delete_analytics_data"
        assert arguments == {"token": issued_deletion_handle}
        return MatrixToolObservation(
            payload={"status": "deleted"},
            result_parsed=True,
            result_state="deleted",
            mcp_is_error=False,
        )

    monkeypatch.setattr(matrix_module, "call_tool", local_result)
    receipts = anyio.run(
        run_analytics_cleanup_cases,
        cast("MatrixClient", object()),
        state,
    )

    assert tuple(receipt.kind for receipt in receipts) == ("success", "success")
    assert all(receipt.state == "passed" for receipt in receipts)
    assert state.analytics_resources.cleanup_verified is True
    assert state.uncleaned == 0
    assert any(tool == "saxo_delete_analytics_data" for tool, _ in observed)


def test_analytics_cleanup_previews_an_explicit_empty_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _runtime_state()
    issued_deletion_handle = "dp_33333333333343338333333333333333"
    observed: list[tuple[str, dict[str, object]]] = []

    async def empty_storage(
        _client: object,
        tool: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed.append((tool, arguments))
        if tool == "saxo_list_analytics_storage":
            return MatrixToolObservation(
                payload={
                    "status": "passed",
                    "result": {
                        "entries": [],
                        "runtime_state": {
                            "job_count": 0,
                            "cache_entry_count": 0,
                            "temporary_entry_count": 0,
                            "fingerprint_sha256": "a" * 64,
                        },
                    },
                },
                result_parsed=True,
                result_state="passed",
                mcp_is_error=False,
            )
        if tool == "saxo_preview_analytics_deletion":
            assert arguments == {"scope": {"data_types": []}}
            return MatrixToolObservation(
                payload={
                    "status": "preview_ready",
                    "result": {"preview": {"token": issued_deletion_handle}},
                },
                result_parsed=True,
                result_state="preview_ready",
                mcp_is_error=False,
            )
        assert tool == "saxo_delete_analytics_data"
        assert arguments == {"token": issued_deletion_handle}
        return MatrixToolObservation(
            payload={"status": "deleted"},
            result_parsed=True,
            result_state="deleted",
            mcp_is_error=False,
        )

    monkeypatch.setattr(matrix_module, "call_tool", empty_storage)
    receipts = anyio.run(
        run_analytics_cleanup_cases,
        cast("MatrixClient", object()),
        state,
    )

    assert all(receipt.state == "passed" for receipt in receipts)
    assert state.analytics_resources.cleanup_verified is True
    assert state.uncleaned == 0
    assert observed[1] == (
        "saxo_preview_analytics_deletion",
        {"scope": {"data_types": []}},
    )


def test_position_state_fingerprint_uses_inventory_not_live_marks() -> None:
    def observation(
        *, amount: int, current_price: int, source_digest: str
    ) -> MatrixToolObservation:
        return MatrixToolObservation(
            payload={
                "status": "passed",
                "response": {
                    "Data": [
                        {
                            "PositionBase": {
                                "AssetType": "Stock",
                                "Amount": amount,
                                "Uic": 9001,
                            },
                            "PositionView": {
                                "CurrentPrice": current_price,
                                "ProfitLossOnTrade": current_price - 100,
                            },
                        },
                    ],
                },
                "response_fingerprint": source_digest,
            },
            result_parsed=True,
            result_state="passed",
            mcp_is_error=False,
        )

    first = matrix_module._state_component_values(  # noqa: SLF001
        "positions",
        observation(amount=1, current_price=100, source_digest="a" * 64),
    )
    remarked = matrix_module._state_component_values(  # noqa: SLF001
        "positions",
        observation(amount=1, current_price=101, source_digest="b" * 64),
    )
    resized = matrix_module._state_component_values(  # noqa: SLF001
        "positions",
        observation(amount=2, current_price=101, source_digest="c" * 64),
    )

    assert first[0] == remarked[0] == resized[0] == 1
    assert first[2] is remarked[2] is resized[2] is True
    assert first[1] == remarked[1]
    assert first[1] != resized[1]


def test_analytics_cleanup_never_deletes_while_an_issued_job_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _runtime_state()
    state.analytics_resources.job_ids.append("jb_22222222222242228222222222222222")
    observed: list[tuple[str, dict[str, object]]] = []

    async def active_job(
        _client: object,
        tool: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed.append((tool, arguments))
        if tool == "saxo_manage_analysis_job":
            return MatrixToolObservation(
                payload={"status": "job_running"},
                result_parsed=True,
                result_state="job_running",
                mcp_is_error=False,
            )
        if tool == "saxo_list_analytics_storage":
            return MatrixToolObservation(
                payload={
                    "status": "passed",
                    "result": {
                        "entries": [
                            {
                                "data_type": "jobs",
                                "object_id": state.analytics_resources.job_ids[0],
                            },
                        ],
                        "runtime_state": {
                            "job_count": 1,
                            "cache_entry_count": 0,
                            "temporary_entry_count": 1,
                            "fingerprint_sha256": "a" * 64,
                        },
                    },
                },
                result_parsed=True,
                result_state="passed",
                mcp_is_error=False,
            )
        assert arguments == {}
        return MatrixToolObservation(
            payload={"status": "invalid_arguments"},
            result_parsed=True,
            result_state="invalid_arguments",
            mcp_is_error=True,
        )

    monkeypatch.setattr(matrix_module, "call_tool", active_job)
    receipts = anyio.run(
        matrix_module.run_analytics_cleanup_cases,
        cast("MatrixClient", object()),
        state,
    )

    assert all(receipt.state == "failed" for receipt in receipts)
    assert state.uncleaned == 1
    assert state.analytics_resources.cleanup_verified is False
    assert not any(
        tool == "saxo_preview_analytics_deletion" and arguments.get("scope")
        for tool, arguments in observed
    )
    assert not any(
        tool == "saxo_delete_analytics_data" and arguments.get("token")
        for tool, arguments in observed
    )


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


def test_matrix_resolves_the_exact_controlled_stock_fixture_query() -> None:
    primary = dict(analytics_primary_calls())["saxo_resolve_research_universe"]
    success = next(
        call
        for call in analytics_case_calls()
        if call.tool_id == "saxo_resolve_research_universe" and call.kind == "success"
    )

    assert primary == {
        "query": "AAPL",
        "asset_types": ["Stock"],
        "exchanges": ["NASDAQ"],
    }
    assert (
        materialize_analytics_case_arguments(
            success,
            AnalyticsRuntimeResources(),
        )
        == primary
    )


def test_matrix_uses_a_continuous_bounded_intraday_chart_window() -> None:
    primary = dict(analytics_primary_calls())["saxo_sync_research_data"]
    success = next(
        call
        for call in analytics_case_calls()
        if call.tool_id == "saxo_sync_research_data" and call.kind == "success"
    )
    resources = AnalyticsRuntimeResources(
        instrument_handles=["ih_00000000000040008000000000000000"],
    )

    expected = {
        "request": {
            "items": [
                {
                    "data_kind": "price_bars",
                    "handle": "ih_00000000000040008000000000000000",
                    "interval": "1m",
                    "start": "2026-01-05T14:30:00Z",
                    "end": "2026-01-05T15:30:00Z",
                },
            ],
        },
    }
    primary_request = cast("dict[str, JsonValue]", primary["request"])
    primary_items = cast("list[JsonValue]", primary_request["items"])
    primary_item = cast("dict[str, JsonValue]", primary_items[0])
    expected_request = cast("dict[str, JsonValue]", expected["request"])
    expected_items = cast("list[JsonValue]", expected_request["items"])
    expected_item = cast("dict[str, JsonValue]", expected_items[0])
    assert primary_item == {
        **expected_item,
        "handle": "ih_00000000000040008000000000000000",
    }
    assert materialize_analytics_case_arguments(success, resources) == expected


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
    assert calls["saxo_delete_analytics_data"] == {}


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


def test_matrix_executes_one_distinct_success_call_for_every_analysis_kind() -> None:
    catalog = load_analysis_kind_catalog()
    success_calls = tuple(
        call
        for call in analytics_case_calls()
        if call.kind == "success" and call.input_strategy.startswith("analyze_kind_")
    )
    observed = tuple(call.analysis_kind for call in success_calls)

    assert len(success_calls) == len(catalog.analysis_kinds)
    assert set(observed) == set(catalog.analysis_kinds)
    assert len(observed) == len(set(observed))


def test_analysis_kind_contracts_name_honest_expected_outcomes() -> None:
    calls = {
        call.analysis_kind: call
        for call in analytics_case_calls()
        if call.analysis_kind is not None
    }

    assert calls["scenario_historical"].expected_analysis_outcome == "refused"
    assert calls["fixed_income"].expected_analysis_outcome == "refused"
    assert calls["instrument_price_return"].expected_analysis_outcome == "persisted"
    assert calls["monte_carlo"].expected_analysis_outcome == "refused"


def test_margin_fire_drill_preserves_its_exact_analysis_kind_in_runtime_arguments() -> None:
    call = next(
        item for item in analytics_case_calls() if item.analysis_kind == "margin_fire_drill"
    )

    request = cast("dict[str, JsonValue]", call.arguments["request"])
    assert request["analysis_kind"] == "margin_fire_drill"
    resources = AnalyticsRuntimeResources(
        instrument_handles=["ih_00000000000040008000000000000000"],
        dataset_ids_by_analysis_kind={
            "scenario_custom": ["ds_00000000000040008000000000000000"],
        },
    )
    materialized = materialize_analytics_case_arguments(call, resources)
    materialized_request = cast("dict[str, JsonValue]", materialized["request"])
    assert materialized_request["analysis_kind"] == "margin_fire_drill"


@pytest.mark.parametrize(
    "analysis_kind",
    [
        "margin_fire_drill",
        "portfolio_scenario",
        "scenario_combined",
        "scenario_currency",
        "scenario_custom",
        "scenario_rate",
        "scenario_volatility",
    ],
)
def test_scenario_family_uses_one_exact_server_issued_scenario_context(
    analysis_kind: str,
) -> None:
    call = next(item for item in analytics_case_calls() if item.analysis_kind == analysis_kind)
    typed_input_id = "ds_44444444444444448444444444444444"
    resources = AnalyticsRuntimeResources(
        instrument_handles=["ih_55555555555545558555555555555555"],
        analysis_input_dataset_ids_by_analysis_kind={
            "scenario_custom": [typed_input_id],
        },
    )

    materialized = materialize_analytics_case_arguments(call, resources)
    request = cast("dict[str, JsonValue]", materialized["request"])

    assert request["analysis_kind"] == analysis_kind
    assert request["dataset_id"] == typed_input_id


def test_server_observed_input_refusal_binds_an_honest_terminal_refusal() -> None:
    call = next(item for item in analytics_case_calls() if item.analysis_kind == "position_sizing")
    resources = AnalyticsRuntimeResources(
        analysis_input_refusals_by_analysis_kind={
            "position_sizing": "a" * 64,
        },
    )

    observed = matrix_module._bind_observed_source_precondition(  # noqa: SLF001
        call,
        resources,
    )
    receipt = analytics_case_receipt(
        observed,
        ("verified",),
        MatrixToolObservation(
            payload={"status": "refused", "reason_code": "analytics_request_invalid"},
            result_parsed=True,
            result_state="refused",
            mcp_is_error=True,
        ),
    )

    assert observed.expected_analysis_outcome == "refused"
    assert observed.source_precondition_refused is True
    assert receipt.state == "refused"
    assert receipt.source_precondition_refused is True
    assert receipt.source_precondition_evidence_sha256 == "a" * 64


def test_failed_analysis_receipt_retains_only_a_safe_observed_reason_code() -> None:
    call = next(
        item for item in analytics_case_calls() if item.analysis_kind == "instrument_price_return"
    )

    safe = analytics_case_receipt(
        call,
        ("verified",),
        MatrixToolObservation(
            payload={
                "status": "refused",
                "reason_code": "verified_coverage_unavailable",
            },
            result_parsed=True,
            result_state="refused",
            mcp_is_error=False,
        ),
    )
    assert safe.observed_reason_code == "verified_coverage_unavailable"
    for unsafe_reason in (
        "account_123456",
        "private_account_name",
        "joakim_sigvardt",
        "client_secret",
    ):
        unsafe = analytics_case_receipt(
            call,
            ("verified",),
            MatrixToolObservation(
                payload={
                    "status": "refused",
                    "reason_code": unsafe_reason,
                },
                result_parsed=True,
                result_state="refused",
                mcp_is_error=False,
            ),
        )
        assert unsafe.observed_reason_code is None


def test_pretrade_context_uses_the_just_observed_instrument_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis_id = "an_33333333333343338333333333333333"
    typed_input = "ds_44444444444444448444444444444444"
    calls: list[dict[str, JsonValue]] = []

    async def route_pretrade(
        _client: object,
        tool: str,
        arguments: dict[str, JsonValue],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        assert tool == "saxo_sync_research_data"
        calls.append(arguments)
        return MatrixToolObservation(
            payload={
                "status": "passed",
                "result": {
                    "datasets": [
                        {
                            "dataset_id": typed_input,
                            "data_kind": "analysis_input",
                            "analysis_kind": "pretrade_impact",
                        },
                    ],
                },
            },
            result_parsed=True,
            result_state="passed",
            mcp_is_error=False,
        )

    monkeypatch.setattr(matrix_module, "call_tool", route_pretrade)
    resources = AnalyticsRuntimeResources(
        dataset_ids_by_analysis_kind={
            "price_bars": ["ds_11111111111141118111111111111111"],
            "quote": ["ds_22222222222242228222222222222222"],
        },
    )
    state = _runtime_state()
    state.analytics_resources = resources
    call = next(
        item for item in analytics_case_calls() if item.analysis_kind == "instrument_price_return"
    )
    result = MatrixToolObservation(
        payload={
            "status": "verified",
            "analysis_kind": "instrument_price_return",
            "analysis_id": analysis_id,
        },
        result_parsed=True,
        result_state="verified",
        mcp_is_error=False,
    )
    receipt = analytics_case_receipt(
        call,
        ("verified",),
        result,
        returned_analysis_kind="instrument_price_return",
        analysis_id=analysis_id,
        persisted_result_authenticated=True,
    )

    anyio.run(
        matrix_module._remember_analysis_case_outputs,  # noqa: SLF001
        cast("MatrixClient", object()),
        state,
        call,
        result,
        receipt,
    )

    item = cast(
        "dict[str, JsonValue]",
        cast(
            "list[JsonValue]",
            cast("dict[str, JsonValue]", calls[0]["request"])["items"],
        )[0],
    )
    assert item["origin_analysis_id"] == analysis_id
    assert resources.analysis_input_dataset_ids_by_analysis_kind["pretrade_impact"] == [
        typed_input,
    ]


def test_failed_replay_does_not_seed_pretrade_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis_id = "an_33333333333343338333333333333333"
    calls: list[dict[str, JsonValue]] = []

    async def reject_pretrade(
        _client: object,
        _tool: str,
        arguments: dict[str, JsonValue],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        calls.append(arguments)
        raise AssertionError("failed replay must not seed pretrade context")

    monkeypatch.setattr(matrix_module, "call_tool", reject_pretrade)
    state = _runtime_state()
    state.analytics_resources = AnalyticsRuntimeResources(
        dataset_ids_by_analysis_kind={
            "price_bars": ["ds_11111111111141118111111111111111"],
            "quote": ["ds_22222222222242228222222222222222"],
        },
    )
    call = next(
        item for item in analytics_case_calls() if item.analysis_kind == "instrument_price_return"
    )
    result = MatrixToolObservation(
        payload={
            "status": "verified",
            "analysis_kind": "instrument_price_return",
            "analysis_id": analysis_id,
        },
        result_parsed=True,
        result_state="verified",
        mcp_is_error=False,
    )
    failed_receipt = analytics_case_receipt(
        call,
        ("verified",),
        result,
        returned_analysis_kind="instrument_price_return",
        analysis_id=analysis_id,
        persisted_result_authenticated=False,
    )
    assert failed_receipt.state == "failed"

    anyio.run(
        matrix_module._remember_analysis_case_outputs,  # noqa: SLF001
        cast("MatrixClient", object()),
        state,
        call,
        result,
        failed_receipt,
    )

    assert calls == []
    assert state.analytics_resources.analysis_ids_by_kind["instrument_price_return"] == [
        analysis_id,
    ]
    assert "pretrade_impact" not in (
        state.analytics_resources.analysis_input_dataset_ids_by_analysis_kind
    )


def test_tool_availability_probe_accepts_only_an_observed_source_refusal() -> None:
    call = next(
        item
        for item in analytics_case_calls()
        if item.tool_id == "saxo_size_position"
        and item.kind == "success"
        and item.analysis_kind is None
    )
    refused = MatrixToolObservation(
        payload={"status": "refused", "reason_code": "analytics_request_invalid"},
        result_parsed=True,
        result_state="refused",
        mcp_is_error=True,
    )

    unbound = analytics_case_receipt(call, ("verified",), refused)
    resources = AnalyticsRuntimeResources(
        analysis_input_refusals_by_analysis_kind={"position_sizing": "b" * 64},
    )
    observed = matrix_module._bind_observed_source_precondition(  # noqa: SLF001
        call,
        resources,
    )
    bound = analytics_case_receipt(observed, ("verified",), refused)

    assert unbound.state == "failed"
    assert bound.state == "refused"
    assert bound.source_precondition_evidence_sha256 == "b" * 64


def test_controlled_context_coverage_requires_typed_input_or_observed_refusal() -> None:
    exact_contexts = {
        "portfolio_performance",
        "position_sizing",
        "scenario_custom",
        "portfolio_minimum_variance",
        "derivatives_model",
        "bounded_backtest",
        "pretrade_impact",
    }
    raw_only = AnalyticsRuntimeResources(
        dataset_ids_by_analysis_kind={
            kind: [f"ds_{index:032x}"] for index, kind in enumerate(exact_contexts)
        },
    )
    observed = AnalyticsRuntimeResources(
        analysis_input_dataset_ids_by_analysis_kind={
            "scenario_custom": ["ds_66666666666646668666666666666666"],
        },
        analysis_input_refusals_by_analysis_kind={
            kind: f"{index + 1:064x}"
            for index, kind in enumerate(exact_contexts - {"scenario_custom"})
        },
    )

    assert matrix_module._controlled_context_coverage(raw_only) == (False, 0)  # noqa: SLF001
    assert matrix_module._controlled_context_coverage(observed) == (True, 7)  # noqa: SLF001


def test_queued_long_job_is_not_a_completed_analysis_receipt() -> None:
    call = sim_module.AnalyticsCaseCall(
        tool_id="saxo_manage_analysis_job",
        kind="success",
        arguments={},
        input_strategy="analyze_kind_monte_carlo",
        analysis_kind="monte_carlo",
        expected_analysis_outcome="refused",
    )
    queued = MatrixToolObservation(
        payload={"status": "job_queued", "result": {"job_id": "jb_0" * 0}},
        result_parsed=True,
        result_state="job_queued",
        mcp_is_error=False,
    )

    receipt = analytics_case_receipt(call, ("job_queued",), queued)

    assert receipt.state == "failed"
    assert receipt.analysis_id is None
    assert receipt.persisted_result_authenticated is False


def test_verified_analysis_receipt_requires_exact_kind_and_persisted_handle() -> None:
    call = sim_module.AnalyticsCaseCall(
        tool_id="saxo_analyze_instruments",
        kind="success",
        arguments={},
        input_strategy="analyze_kind_instrument_price_return",
        analysis_kind="instrument_price_return",
        expected_analysis_outcome="persisted",
    )
    mismatched = MatrixToolObservation(
        payload={
            "status": "verified",
            "analysis_kind": "instrument_quote",
            "analysis_id": "an_00000000000040008000000000000000",
        },
        result_parsed=True,
        result_state="verified",
        mcp_is_error=False,
    )

    receipt = analytics_case_receipt(call, ("verified",), mismatched)

    assert receipt.state == "failed"
    assert receipt.persisted_result_authenticated is False


def test_passed_matrix_requires_one_distinct_success_receipt_per_analysis_kind() -> None:
    source = inspect.getsource(SimToolMatrixReceipt)
    assert "analysis_execution_receipts" in source
    assert "analysis_kinds" in source
    assert "analysis_execution_receipt" in source
    assert load_analysis_kind_catalog().analysis_kinds == ANALYSIS_KIND_IDS


def test_passed_matrix_receipt_requires_exact_safe_60_tool_state() -> None:
    receipts = tuple(
        receipt_for(tool, {"status": "completed"}, {}) for tool in sorted(ALL_LOGICAL_TOOL_IDS)
    )
    state = _state()
    lifecycle_receipt = _lifecycle_receipt(state)
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
        before_state_fingerprint=lifecycle_receipt.before,
        after_state_fingerprint=lifecycle_receipt.after,
        uncleaned_resources=0,
        hosts=("gateway.saxobank.com",),
        live_events=0,
        live_mutation_calls=0,
        analytics_tool_receipt_count=len(ANALYTICS_TOOL_IDS),
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        analytics_case_receipts=case_evidence,
        analysis_execution_receipts=_analysis_execution_receipts(),
        controlled_sim_lifecycle=lifecycle_receipt,
        mcp_only_account_fixture_state=True,
        cleanup_complete=True,
        account_state_unchanged=True,
        redacted_publication=True,
        errors=(),
    )
    assert receipt.status == "passed"

    lifecycle = receipt.controlled_sim_lifecycle
    assert lifecycle is not None
    reduced_cases = list(lifecycle.cases)
    option_index = CONTROLLED_SIM_CASES.index("options_entitlement")
    reduced_cases[option_index] = reduced_cases[option_index].model_copy(
        update={
            "state": "degraded",
            "reason_code": "options_entitlement_denied",
            "entitlement_state": "denied",
        },
    )
    reduced_lifecycle = lifecycle.model_copy(
        update={"evidence_state": "reduced", "cases": tuple(reduced_cases)},
    )
    reduced_receipt = SimToolMatrixReceipt.model_validate(
        {
            **receipt.model_dump(mode="python"),
            "controlled_sim_lifecycle": reduced_lifecycle,
        },
    )
    assert reduced_receipt.status == "passed"

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


def test_finalize_reports_exact_failed_analytics_case_instead_of_pydantic_collapse() -> None:
    state = _complete_runtime_state_for_finalize()
    tool_index = next(
        index
        for index, evidence in enumerate(state.analytics_case_receipts)
        if evidence.tool_id == "saxo_backtest_strategy"
    )
    evidence = state.analytics_case_receipts[tool_index]
    cases = list(evidence.cases)
    case_index = next(index for index, case in enumerate(cases) if case.kind == "success")
    cases[case_index] = cases[case_index].model_copy(update={"state": "failed"})
    state.analytics_case_receipts[tool_index] = evidence.model_copy(update={"cases": tuple(cases)})

    receipt = matrix_module._finalize(state)  # noqa: SLF001

    assert receipt.status == "failed"
    assert receipt.reason == "analytics_case_failed:saxo_backtest_strategy:success"
    assert receipt.errors[0] == receipt.reason


def test_finalize_reports_incomplete_analysis_execution_coverage() -> None:
    state = _complete_runtime_state_for_finalize()
    state.analysis_execution_receipts.pop()

    receipt = matrix_module._finalize(state)  # noqa: SLF001

    assert receipt.status == "failed"
    assert receipt.reason == "analysis_execution_coverage_incomplete"
    assert receipt.errors[0] == receipt.reason


def test_controlled_lifecycle_requires_sim_zero_live_cleanup_and_state_equality() -> None:
    before = _state()
    receipt = ControlledSimLifecycleReceipt(
        environment="SIM",
        cases=_lifecycle_cases(),
        before=before,
        after=_reconciled_after(before),
        live_events=0,
        live_mutation_calls=0,
        request_ledger_read_last=True,
        request_ledger_complete=True,
        request_ledger_fingerprint_sha256="9" * 64,
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


def test_controlled_lifecycle_reconciles_material_state_with_two_expected_audit_events() -> None:
    before = _observed_lifecycle_state(
        balance_digest="1" * 64,
        trade_digest="4" * 64,
        trade_count=399,
    )
    after = _observed_lifecycle_state(
        balance_digest="a" * 64,
        trade_digest="b" * 64,
        trade_count=401,
    )

    assert sim_module.brokerage_state_reconciled(before, after) is True
    receipt = ControlledSimLifecycleReceipt(
        environment="SIM",
        cases=_lifecycle_cases(),
        before=before,
        after=after,
        live_events=0,
        live_mutation_calls=0,
        request_ledger_read_last=True,
        request_ledger_complete=True,
        request_ledger_fingerprint_sha256="9" * 64,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=False,
        disclaimer_response_made=False,
    )

    assert receipt.evidence_state == "passed"
    storage_active = BrokerageStateFingerprint(
        components=tuple(
            component.model_copy(
                update={"count": 1, "fingerprint_sha256": "d" * 64}
                if component.name == "jobs"
                else {},
            )
            for component in after.components
        ),
    )
    assert sim_module.brokerage_ghost_state_reconciled(before, storage_active) is True
    assert sim_module.brokerage_state_reconciled(before, storage_active) is False
    assert (
        sim_module.brokerage_state_reconciled(
            before,
            _observed_lifecycle_state(
                balance_digest="a" * 64,
                trade_digest="b" * 64,
                trade_count=401,
                order_count=1,
            ),
        )
        is False
    )
    assert (
        sim_module.brokerage_state_reconciled(
            before,
            _observed_lifecycle_state(
                balance_digest="a" * 64,
                trade_digest="b" * 64,
                trade_count=401,
                position_digest="c" * 64,
            ),
        )
        is False
    )


def test_reconciled_matrix_receipt_accepts_only_one_cleanup_pending_success() -> None:
    reconciled = MatrixScenarioReceipt(
        tool="saxo_place_sim_order",
        status="reconciled",
        result_parsed=True,
        result_state="completed_unverified",
        mcp_is_error=True,
        request_digest="1" * 64,
        response_digest="2" * 64,
    )

    assert reconciled.status == "reconciled"
    with pytest.raises(ValidationError):
        MatrixScenarioReceipt(
            tool="saxo_place_sim_order",
            status="reconciled",
            result_parsed=True,
            result_state="failed",
            mcp_is_error=True,
            request_digest="1" * 64,
            response_digest="2" * 64,
        )


def test_controlled_sim_waits_past_the_process_write_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []

    async def observe_sleep(seconds: float) -> None:
        observed.append(seconds)

    monkeypatch.setattr(matrix_module.anyio, "sleep", observe_sleep)
    anyio.run(matrix_module._wait_for_sim_write_rate_limit)  # noqa: SLF001

    assert len(observed) == 1
    assert observed[0] > 1.0


def test_controlled_lifecycle_pass_requires_last_complete_request_ledger() -> None:
    receipt = _lifecycle_receipt(_state())

    assert receipt.request_ledger_read_last is True
    assert receipt.request_ledger_complete is True
    assert receipt.request_ledger_fingerprint_sha256 == "9" * 64
    with pytest.raises(ValidationError):
        ControlledSimLifecycleReceipt.model_validate(
            {
                **receipt.model_dump(mode="json"),
                "request_ledger_read_last": False,
            },
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
        after=_reconciled_after(state),
        live_events=0,
        live_mutation_calls=0,
        request_ledger_read_last=True,
        request_ledger_complete=True,
        request_ledger_fingerprint_sha256="9" * 64,
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
            mcp_call_observed=True,
            result_parsed=kind != "timeout",
            result_state={
                "success": "passed",
                "degradation": "degraded",
                "refusal": "refused",
                "privacy": "refused",
                "timeout": "timed_out",
                "recovery": "job_completed",
            }[kind],
            mcp_is_error=kind in {"refusal", "privacy", "timeout"},
            network_call_made=False,
            broker_write_made=False,
            private_values_published=False,
            request_sha256="b" * 64,
            response_sha256="c" * 64,
            evidence_sha256="d" * 64,
            reconciles_request_sha256="e" * 64 if kind == "recovery" else None,
            reconciliation_observation_sha256="f" * 64 if kind == "recovery" else None,
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

    assert (
        tuple((call.tool_id, call.kind) for call in calls if call.analysis_kind is None) == expected
    )
    assert len(tuple(call for call in calls if call.analysis_kind is not None)) == len(
        ANALYSIS_KIND_IDS
    )


def test_analytics_phase_executes_and_checks_every_applicable_fastmcp_case(  # noqa: C901, PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_calls = tuple(
        call
        for call in analytics_case_calls()
        if not (
            call.kind == "success"
            and call.tool_id in {"saxo_preview_analytics_deletion", "saxo_delete_analytics_data"}
        )
    )
    contracts = {
        (contract.tool_id, case.kind): case
        for contract in analytics_sim_contracts()
        for case in contract.cases
    }
    observed: list[tuple[str, dict[str, object], float | None]] = []
    case_index = 0
    replay_kind: str | None = None
    dataset_id = "ds_11111111111141118111111111111111"
    degraded_dataset_id = "ds_22222222222242228222222222222222"
    analysis_id = "an_33333333333343338333333333333333"
    degraded_analysis_id = "an_44444444444444448444444444444444"
    instrument_handle = "ih_55555555555545558555555555555555"
    degraded_instrument_handle = "ih_66666666666646668666666666666666"
    job_id = "jb_77777777777747778777777777777777"
    artifact_id = "ar_88888888888848888888888888888888"
    deletion_handle = "dp_99999999999949998999999999999999"

    async def result_for_case(  # noqa: C901, PLR0912
        _client: object,
        tool: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> MatrixToolObservation:
        nonlocal case_index, replay_kind
        observed.append((tool, arguments, timeout_seconds))
        if tool == "saxo_explain_analysis" and (
            case_index >= len(expected_calls)
            or expected_calls[case_index].tool_id != "saxo_explain_analysis"
        ):
            assert replay_kind is not None
            return MatrixToolObservation(
                payload={
                    "status": "passed",
                    "result": {
                        "analysis": {
                            "analysis_kind": replay_kind,
                            "analysis_id": analysis_id,
                        },
                    },
                },
                result_parsed=True,
                result_state="passed",
                mcp_is_error=False,
            )
        if case_index >= len(expected_calls):
            if tool == "saxo_manage_analysis_job":
                return MatrixToolObservation(
                    payload={"status": "job_completed"},
                    result_parsed=True,
                    result_state="job_completed",
                    mcp_is_error=False,
                )
            if tool == "saxo_list_analytics_storage":
                deleted = any(item[0] == "saxo_delete_analytics_data" for item in observed)
                entries = (
                    []
                    if deleted
                    else [
                        {"data_type": "datasets", "object_id": dataset_id},
                        {"data_type": "jobs", "object_id": job_id},
                    ]
                )
                return MatrixToolObservation(
                    payload={
                        "status": "passed",
                        "result": {
                            "entries": entries,
                            "runtime_state": {
                                "job_count": 0 if deleted else 1,
                                "cache_entry_count": 0 if deleted else 1,
                                "temporary_entry_count": 0,
                                "fingerprint_sha256": "1" * 64,
                            },
                        },
                    },
                    result_parsed=True,
                    result_state="passed",
                    mcp_is_error=False,
                )
            if tool == "saxo_preview_analytics_deletion":
                return MatrixToolObservation(
                    payload={
                        "status": "preview_ready",
                        "result": {"preview": {"token": deletion_handle}},
                    },
                    result_parsed=True,
                    result_state="preview_ready",
                    mcp_is_error=False,
                )
            assert tool == "saxo_delete_analytics_data"
            assert arguments == {"token": deletion_handle}
            return MatrixToolObservation(
                payload={"status": "deleted"},
                result_parsed=True,
                result_state="deleted",
                mcp_is_error=False,
            )
        call = expected_calls[case_index]
        case_index += 1
        assert (tool, timeout_seconds) == (call.tool_id, call.timeout_seconds)
        contract = contracts[(call.tool_id, call.kind)]
        state = contract.expected_states[0]
        if call.analysis_kind is not None:
            state = (
                "verified"
                if call.expected_analysis_outcome == "persisted"
                else "resolved"
                if call.expected_analysis_outcome == "resolved"
                else "refused"
            )
        payload: dict[str, JsonValue] = {"status": state}
        if call.analysis_kind is not None:
            payload["analysis_kind"] = call.analysis_kind
            if call.expected_analysis_outcome == "persisted":
                payload["analysis_id"] = analysis_id
                replay_kind = call.analysis_kind
        if call.tool_id == "saxo_resolve_research_universe":
            payload["result"] = {
                "instrument_handle": (
                    instrument_handle if call.kind == "success" else degraded_instrument_handle
                ),
            }
        elif call.tool_id == "saxo_sync_research_data" and call.kind in {
            "success",
            "degradation",
        }:
            payload["result"] = {
                "datasets": [
                    {
                        "dataset_id": (
                            dataset_id if call.kind == "success" else degraded_dataset_id
                        ),
                    },
                ],
            }
        elif (
            call.tool_id.startswith("saxo_analyze_")
            and call.kind
            in {
                "success",
                "degradation",
            }
            and call.analysis_kind is None
        ):
            payload["analysis_id"] = analysis_id if call.kind == "success" else degraded_analysis_id
        elif call.tool_id in {"saxo_render_analysis", "saxo_export_analysis"} and (
            call.kind == "success"
        ):
            payload["artifact_id"] = artifact_id
        elif (
            call.tool_id == "saxo_manage_analysis_job"
            and call.kind == "success"
            and call.analysis_kind is None
        ):
            payload["result"] = {"job_id": job_id}
        return MatrixToolObservation(
            payload=payload,
            result_parsed=call.kind != "timeout",
            result_state=state,
            mcp_is_error=call.kind in {"refusal", "privacy", "timeout"},
            timed_out=call.kind == "timeout",
        )

    monkeypatch.setattr(matrix_module, "call_tool", result_for_case)

    async def execute_every_declared_case(
        client: object,
        call: sim_module.AnalyticsCaseCall,
        _cache: dict[tuple[str, str, float | None], MatrixToolObservation],
    ) -> MatrixToolObservation:
        return await result_for_case(
            client,
            call.tool_id,
            cast("dict[str, object]", call.arguments),
            timeout_seconds=call.timeout_seconds,
        )

    monkeypatch.setattr(
        matrix_module,
        "_call_analytics_case_once",
        execute_every_declared_case,
    )
    state = _runtime_state()
    anyio.run(
        matrix_module.run_analytics_case_phase,
        cast("MatrixClient", object()),
        state,
    )

    assert case_index == len(expected_calls)
    assert len(state.analytics_case_receipts) == len(ANALYTICS_TOOL_IDS)
    assert set(state.receipts) == set(ANALYTICS_TOOL_IDS)
    assert state.analytics_resources.cleanup_verified is True
    assert state.errors == []


def test_exact_duplicate_analytics_request_reuses_one_mcp_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "_call_analytics_case_once" in inspect.getsource(
        matrix_module.run_analytics_case_phase,
    )
    observed: list[tuple[str, dict[str, JsonValue]]] = []
    result = MatrixToolObservation(
        payload={
            "status": "verified",
            "analysis_kind": "market_comparison",
            "analysis_id": "an_11111111111141118111111111111111",
        },
        result_parsed=True,
        result_state="verified",
        mcp_is_error=False,
    )

    async def execute_once(
        _client: object,
        tool: str,
        arguments: dict[str, JsonValue],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed.append((tool, arguments))
        return result

    monkeypatch.setattr(matrix_module, "call_tool", execute_once)
    call = sim_module.AnalyticsCaseCall(
        tool_id="saxo_analyze_market",
        kind="success",
        arguments={"request": {"analysis_kind": "market_comparison"}},
        input_strategy="exact_duplicate",
        analysis_kind="market_comparison",
        expected_analysis_outcome="persisted",
    )
    cache: dict[tuple[str, str, float | None], MatrixToolObservation] = {}

    first = anyio.run(
        matrix_module._call_analytics_case_once,  # noqa: SLF001
        cast("MatrixClient", object()),
        call,
        cache,
    )
    second = anyio.run(
        matrix_module._call_analytics_case_once,  # noqa: SLF001
        cast("MatrixClient", object()),
        call.model_copy(update={"input_strategy": "exact_kind_receipt"}),
        cache,
    )

    assert first is second is result
    assert observed == [(call.tool_id, call.arguments)]


def test_timeout_and_recovery_with_identical_arguments_make_distinct_mcp_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float | None] = []

    async def execute_each(
        _client: object,
        _tool: str,
        _arguments: dict[str, JsonValue],
        *,
        timeout_seconds: float | None = None,
    ) -> MatrixToolObservation:
        observed_timeouts.append(timeout_seconds)
        return MatrixToolObservation(
            payload={"status": "timed_out" if timeout_seconds is not None else "reconciled"},
            result_parsed=timeout_seconds is None,
            result_state="timed_out" if timeout_seconds is not None else "reconciled",
            mcp_is_error=timeout_seconds is not None,
            timed_out=timeout_seconds is not None,
        )

    monkeypatch.setattr(matrix_module, "call_tool", execute_each)
    arguments: dict[str, JsonValue] = {"action": "check", "job_id": "jb_1"}
    timed = sim_module.AnalyticsCaseCall(
        tool_id="saxo_manage_analysis_job",
        kind="timeout",
        arguments=arguments,
        input_strategy="bounded_timeout",
        timeout_seconds=0.001,
    )
    recovery = timed.model_copy(
        update={
            "kind": "recovery",
            "input_strategy": "observe_exact_timed_operation",
            "reconciles_kind": "timeout",
            "timeout_seconds": None,
        },
    )
    cache: dict[tuple[str, str, float | None], MatrixToolObservation] = {}

    timed_result = anyio.run(
        matrix_module._call_analytics_case_once,  # noqa: SLF001
        cast("MatrixClient", object()),
        timed,
        cache,
    )
    recovery_result = anyio.run(
        matrix_module._call_analytics_case_once,  # noqa: SLF001
        cast("MatrixClient", object()),
        recovery,
        cache,
    )

    assert timed_result is not recovery_result
    assert observed_timeouts == [0.001, None]


def test_analytics_request_cache_never_reuses_different_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, JsonValue]] = []

    async def execute_each(
        _client: object,
        _tool: str,
        arguments: dict[str, JsonValue],
        **_kwargs: object,
    ) -> MatrixToolObservation:
        observed.append(arguments)
        return MatrixToolObservation(
            payload={"status": "verified"},
            result_parsed=True,
            result_state="verified",
            mcp_is_error=False,
        )

    monkeypatch.setattr(matrix_module, "call_tool", execute_each)
    cache: dict[tuple[str, str, float | None], MatrixToolObservation] = {}
    first = sim_module.AnalyticsCaseCall(
        tool_id="saxo_analyze_market",
        kind="success",
        arguments={"request": {"analysis_kind": "market_comparison", "window": 20}},
        input_strategy="first",
        analysis_kind="market_comparison",
        expected_analysis_outcome="persisted",
    )
    second = first.model_copy(
        update={
            "arguments": {
                "request": {"analysis_kind": "market_comparison", "window": 21},
            },
        },
    )

    anyio.run(
        matrix_module._call_analytics_case_once,  # noqa: SLF001
        cast("MatrixClient", object()),
        first,
        cache,
    )
    anyio.run(
        matrix_module._call_analytics_case_once,  # noqa: SLF001
        cast("MatrixClient", object()),
        second,
        cache,
    )

    assert observed == [first.arguments, second.arguments]


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
