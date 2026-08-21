# pyright: reportPrivateUsage=false
# allow: SIZE_OK - bounded SIM matrix orchestrates all 60 scenarios plus lifecycle coverage.
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Literal, Protocol, cast

import anyio
from anyio.lowlevel import checkpoint
from fastmcp import Client, FastMCP

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.agent_skill_matrix import LIFECYCLE_TOOLS, SCENARIO_MANIFEST, manifest_tools
from saxo_bank_mcp.analytics_ghost_portfolio import (
    GhostLifecycleEvidence,
    GhostPlaceStatus,
    GhostStateFingerprint,
    GhostStepStatus,
)
from saxo_bank_mcp.analytics_strategy_schema import (
    parse_strategy_definition,
    strategy_definition_fingerprint,
)
from saxo_bank_mcp.config import SaxoEnvironment, SaxoRuntimeConfig
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.order_mutation_models import (
    ORDER_WRITE_CLASSES,
    ORDER_WRITE_SPECS,
    PRODUCTION_ORDER_TOOL_NAMES,
)
from saxo_bank_mcp.qa_analytics_sim import (
    BROKERAGE_STATE_COMPONENTS,
    AnalyticsCaseCall,
    AnalyticsCaseReceipt,
    AnalyticsRuntimeResources,
    AnalyticsToolCaseEvidence,
    BrokerageStateComponent,
    BrokerageStateFingerprint,
    ControlledSimCaseReceipt,
    ControlledSimLifecycleReceipt,
    analytics_case_calls,
    analytics_case_contract_sha256,
    analytics_sim_contracts,
    assert_analytics_case_coverage,
    brokerage_ghost_state_reconciled,
    brokerage_inventory_reconciled,
    brokerage_state_reconciled,
    isolated_analytics_state,
    live_mutation_calls_in,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import (
    MatrixClient,
    MatrixToolObservation,
    account_discovery_call,
    auth_lifecycle_calls,
    call_tool,
    digest,
    fixture_read_calls,
    fixture_values_match,
    hosts_of,
    is_blocker,
    live_transport_events,
    local_and_read_calls,
    needs_recovery,
    receipt_for,
    refusal_arguments,
    safe_account_selectors,
    state_read_calls,
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    FIXTURE_ASSET_TYPE,
    NON_EXECUTABLE_SIM,
    SIM_GATEWAY_HOST,
    MatrixCliFixtures,
    MatrixFixtures,
    MatrixRuntimeState,
    MatrixScenarioReceipt,
    PreflightFlags,
    SimToolMatrixReceipt,
)
from saxo_bank_mcp.secret_scan import scan_secret_text
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.server_tool_ids import ANALYTICS_TOOL_IDS
from saxo_bank_mcp.trading_write_registry import trading_write_specs

_SHA256_HEX_LENGTH = 64
_MINIMUM_FIXTURE_READ_RESULTS = 4
_CONTROLLED_GHOST_EXTERNAL_REFERENCE: Final = "task24-controlled-ghost"
_SIM_WRITE_RATE_LIMIT_WAIT_SECONDS: Final = 1.05
_SAFE_HANDLE = re.compile(r"^(?P<kind>ih|ds|an|ar|jb|dp)_[0-9a-f]{32}$")
_ACTIVE_JOB_STATES: Final = frozenset({"job_queued", "job_running"})
_SAFE_OBSERVED_REASON_CODES: Final = frozenset(
    {
        "analysis_input_account_scope_mismatch",
        "analysis_input_kind_unsupported",
        "analysis_input_source_scope_invalid",
        "analysis_input_source_scope_mismatch",
        "analysis_parameter_binding_failed",
        "analysis_source_context_invalid",
        "analysis_source_context_unavailable",
        "analysis_source_contracts_incomplete",
        "analysis_source_field_invalid",
        "analysis_source_field_missing",
        "analysis_source_rows_invalid",
        "analysis_source_scope_ambiguous",
        "analytics_contract_mismatch",
        "analytics_measure_undefined",
        "analytics_request_invalid",
        "backtest_chart_scope_ambiguous",
        "backtest_reference_scope_mismatch",
        "backtest_sim_proof_unavailable",
        "derivatives_expiry_unavailable",
        "derivatives_quote_scope_mismatch",
        "derivatives_reference_price_unavailable",
        "derivatives_source_scope_ambiguous",
        "explicit_derivatives_assumptions_required",
        "explicit_pretrade_inputs_required",
        "future_source_observation",
        "historical_replay_observations_unbound",
        "instrument_dataset_scope_mismatch",
        "market_comparison_result_invalid",
        "missing_proof_profile",
        "multi_dataset_proof_binding_unavailable",
        "multi_instrument_executor_unavailable",
        "optimization_cost_scope_incomplete",
        "optimization_minimum_asset_count_unavailable",
        "optimization_minimum_sample_count_unavailable",
        "optimization_price_history_incomplete",
        "portfolio_boundary_valuations_unavailable",
        "portfolio_flow_boundary_valuations_unavailable",
        "portfolio_performance_reconciliation_unavailable",
        "position_sizing_asset_constraints_unavailable",
        "position_sizing_currency_mismatch",
        "position_sizing_instrument_scope_mismatch",
        "position_sizing_source_scope_ambiguous",
        "pretrade_context_ambiguous",
        "pretrade_context_unavailable",
        "pretrade_cost_reconciliation_unavailable",
        "pretrade_cost_scope_ambiguous",
        "pretrade_currency_mismatch",
        "pretrade_origin_account_scope_mismatch",
        "pretrade_origin_analysis_required",
        "pretrade_origin_lineage_mismatch",
        "pretrade_origin_scope_ambiguous",
        "pretrade_quote_unavailable",
        "pretrade_verified_basis_unavailable",
        "private_result_required",
        "proof_engine_executor_unavailable",
        "proof_expired",
        "proof_metric_executor_unavailable",
        "proof_source_contract_missing",
        "proposal_context_mismatch",
        "saxo_greek_reconciliation_unavailable",
        "scenario_exposure_scope_incomplete",
        "scenario_kind_unsupported",
        "scenario_metric_binding_unavailable",
        "scenario_shock_map_incomplete",
        "source_binding_ambiguous",
        "source_binding_invalid",
        "source_contract_missing",
        "stored_backtest_context_mismatch",
        "stored_derivatives_context_mismatch",
        "stored_execution_context_invalid",
        "stored_optimization_context_mismatch",
        "stored_portfolio_context_mismatch",
        "stored_price_dataset_invalid",
        "stored_scenario_context_mismatch",
        "stored_sizing_context_mismatch",
        "supporting_dataset_not_proof_bound",
        "supporting_dataset_scope_invalid",
        "twr_boundary_valuations_unavailable",
        "verified_coverage_unavailable",
    }
)
_TERMINAL_JOB_STATES: Final = frozenset(
    {
        "job_completed",
        "job_failed",
        "job_cancelled",
        "job_expired",
        "job_interrupted_restart_required",
    },
)
_ANALYSIS_INPUT_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "portfolio_performance": ("portfolio_performance",),
    "position_sizing": ("position_sizing",),
    "scenario_custom": (
        "margin_fire_drill",
        "portfolio_scenario",
        "scenario_combined",
        "scenario_currency",
        "scenario_custom",
        "scenario_rate",
        "scenario_volatility",
    ),
    "portfolio_minimum_variance": (
        "portfolio_minimum_variance",
        "portfolio_risk_parity",
    ),
    "derivatives_model": ("derivatives_model",),
    "bounded_backtest": ("bounded_backtest",),
    "pretrade_impact": ("pretrade_impact",),
}
_ANALYSIS_INPUT_ROUTE_BY_KIND: Final = {
    analysis_kind: route
    for route, analysis_kinds in _ANALYSIS_INPUT_ALIASES.items()
    for analysis_kind in analysis_kinds
}


@dataclass(frozen=True, slots=True)
class _ControlledGhostObservation:
    reason_code: str
    evidence: GhostLifecycleEvidence | None
    ledger_fingerprint_sha256: str | None
    receipt_bound: bool
    mcp_call_count: int
    sim_mutation_call_count: int


class _InstalledProofRecorder(Protocol):
    def candidate_commit(self) -> str: ...

    def controlled_backtest_source_binding(
        self,
        dataset_id: str,
        instrument_handle: str,
        *,
        expected_uic: int,
        expected_asset_type: str,
    ) -> str: ...

    def prepare_controlled_sim_safety(
        self,
        account_selector: str,
        *,
        expected_uic: int,
    ) -> None: ...

    def clear_controlled_sim_safety(self) -> None: ...

    def record_observed_ghost_lifecycle(
        self,
        evidence: GhostLifecycleEvidence,
        *,
        ledger_provenance_sha256: str,
    ) -> None: ...


# Re-export models for producer imports.
__all__ = [
    "MatrixCliFixtures",
    "MatrixFixtures",
    "MatrixScenarioReceipt",
    "SimToolMatrixReceipt",
    "handle_sim_tool_matrix",
]


def handle_sim_tool_matrix(out: Path, fixtures: MatrixCliFixtures) -> int:
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.requested_environment is not SaxoEnvironment.SIM:
        return _fail(out, "environment_not_sim")
    try:
        parsed = MatrixFixtures(
            stock_uic=int(fixtures.stock_uic),
            amount=float(fixtures.amount),
            limit_price=float(fixtures.limit_price),
            modified_limit_price=float(fixtures.modified_limit_price),
            option_uics=tuple(
                int(part) for part in fixtures.option_uics.split(",") if part.strip()
            ),
            stream_uic=int(fixtures.stream_uic),
        )
    except ValueError:
        return _fail(out, "invalid_fixture_values")
    receipt = anyio.run(_run_matrix, parsed)
    redacted = redact_json(receipt.model_dump(mode="json"))
    if not isinstance(redacted, dict):
        return 1
    ok = write_scanned_json(out, redacted)
    return 0 if ok and receipt.status == "passed" else 1


async def _run_matrix(
    fixtures: MatrixFixtures,
    *,
    proof_recorder: _InstalledProofRecorder | None = None,
    matrix_server: FastMCP | None = None,
) -> SimToolMatrixReceipt:
    state = MatrixRuntimeState(
        errors=[],
        hosts={SIM_GATEWAY_HOST},
        live_events=0,
        live_mutation_calls=0,
        receipts={},
        analytics_case_receipts=[],
        lifecycle_seen=set(),
        registered_ops=[],
        uncleaned=0,
        preflight=PreflightFlags(
            fixtures_ok=False,
            account_ok=False,
            auth_ok=False,
            session_ok=False,
        ),
        before=None,
        after=None,
    )
    with tempfile.TemporaryDirectory(prefix="saxo-mcp-task23-") as runtime_dir:
        runtime_root = Path(runtime_dir)
        with isolated_analytics_state(runtime_root / "state"):
            async with Client(mcp if matrix_server is None else matrix_server) as client:
                await _run_auth_preflight(client, state)
                await _run_mcp_account_and_fixture_preflight(client, state, fixtures)
                if state.errors:
                    return _blocked(state)
                state.before = await mcp_state_fingerprint(client, state)
                await _run_read_and_refusal_phase(client, state, fixtures)
                await run_disclaimer_refusal_phase(client, state)
                await _run_preview_phase(client, state)
                await _run_order_mutation_phase(client, state)
                await _run_trading_write_phase(client, state)
                await _run_trailing_phase(client, state, fixtures)
                await run_analytics_case_phase(
                    client,
                    state,
                    controlled_fixtures=fixtures,
                    proof_recorder=proof_recorder,
                )

    return _finalize(state)


async def _run_auth_preflight(client: MatrixClient, state: MatrixRuntimeState) -> None:
    auth_result = await call_tool(client, "saxo_auth_status", {})
    auth_payload = auth_result.payload
    state.preflight = PreflightFlags(
        fixtures_ok=state.preflight.fixtures_ok,
        account_ok=state.preflight.account_ok,
        auth_ok=True,
        session_ok=state.preflight.session_ok,
    )
    _record(state, "saxo_auth_status", auth_result, {})
    refresh = await call_tool(client, "saxo_refresh_token", {})
    _record(state, "saxo_refresh_token", refresh, {})
    if needs_recovery(auth_payload) or refresh.result_state in {
        "completed",
        "token_refreshed",
        "passed",
    }:
        auth_result = await call_tool(client, "saxo_auth_status", {})
        auth_payload = auth_result.payload
        _record(state, "saxo_auth_status", auth_result, {})
    session = await call_tool(client, "saxo_get_session_capabilities", {})
    session_ok = session.result_state in {"completed", "passed"}
    if session.result_state == "auth_required":
        state.errors.append("sim_session_auth_required")
    state.preflight = PreflightFlags(
        fixtures_ok=state.preflight.fixtures_ok,
        account_ok=state.preflight.account_ok,
        auth_ok=state.preflight.auth_ok,
        session_ok=session_ok,
    )
    _record(state, "saxo_get_session_capabilities", session, {})


async def _run_mcp_account_and_fixture_preflight(
    client: MatrixClient,
    state: MatrixRuntimeState,
    fixtures: MatrixFixtures,
) -> None:
    account_tool, account_arguments = account_discovery_call()
    account = await call_tool(client, account_tool, account_arguments)
    _record(state, account_tool, account, account_arguments)
    selectors = safe_account_selectors(account.payload)
    _extend_unique(state.analytics_resources.account_selectors, list(selectors))
    account_ok = account.result_state == "passed" and len(selectors) == 1
    fixture_results = [
        await call_tool(client, tool, arguments) for tool, arguments in fixture_read_calls(fixtures)
    ]
    for result in fixture_results:
        _observe_auxiliary(state, result)
    if len(fixture_results) >= _MINIMUM_FIXTURE_READ_RESULTS:
        _extend_unique(
            state.analytics_resources.option_expiries,
            list(_observed_option_expiries(fixture_results[1].payload)),
        )
        state.analytics_resources.controlled_order_limit_price = _observed_controlled_limit_price(
            fixture_results[-1].payload
        )
    fixtures_ok = fixture_values_match(fixtures) and all(
        result.result_parsed and result.result_state == "passed" for result in fixture_results
    )
    fixtures_ok = fixtures_ok and state.analytics_resources.controlled_order_limit_price is not None
    if not account_ok:
        state.errors.append("account_allowlist_unresolved")
    if not fixtures_ok:
        state.errors.append("fixture_reference_invalid")
    state.preflight = PreflightFlags(
        fixtures_ok=fixtures_ok,
        account_ok=account_ok,
        auth_ok=state.preflight.auth_ok,
        session_ok=state.preflight.session_ok,
        disclaimer_refusal_ok=state.preflight.disclaimer_refusal_ok,
        mcp_only=True,
    )


async def mcp_state_fingerprint(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> BrokerageStateFingerprint:
    observed: dict[tuple[str, str], MatrixToolObservation] = {}
    components: list[BrokerageStateComponent] = []
    for name, tool, arguments in state_read_calls():
        key = (tool, digest(arguments))
        result = observed.get(key)
        if result is None:
            result = await call_tool(client, tool, arguments)
            observed[key] = result
            _observe_auxiliary(state, result)
        count, fingerprint, available = _state_component_values(name, result)
        components.append(
            BrokerageStateComponent(
                name=name,
                count=count,
                fingerprint_sha256=fingerprint,
                observed_state="available" if available else "unavailable",
                mcp_tool_ids=(tool,),
            ),
        )
    return BrokerageStateFingerprint(components=tuple(components))


def _state_component_values(  # noqa: PLR0911
    name: str,
    result: MatrixToolObservation,
) -> tuple[int, str, bool]:
    payload = result.payload
    available = result.result_parsed and result.result_state == "passed"
    if name == "balances":
        source_fingerprint = payload.get("response_fingerprint")
        if isinstance(source_fingerprint, str) and len(source_fingerprint) == _SHA256_HEX_LENGTH:
            return 1, source_fingerprint, available
        return 0, digest({"state": "unavailable"}), False
    if name in {"positions", "orders", "trade_messages"}:
        response = _safe_response_value(payload.get("response"))
        count = _safe_response_count(response)
        if name == "positions":
            fingerprint = _position_inventory_fingerprint(response)
            return count, fingerprint, available and response is not None
        source_fingerprint = payload.get("response_fingerprint")
        fingerprint = (
            source_fingerprint
            if isinstance(source_fingerprint, str) and len(source_fingerprint) == _SHA256_HEX_LENGTH
            else digest(response)
        )
        return count, fingerprint, available and response is not None
    if name == "subscriptions":
        count = _safe_int(payload.get("local_subscription_count"))
        return count, digest({"local_subscription_count": count}), available
    if name == "previews_write_state":
        pending = _safe_int(payload.get("pending_preview_count"))
        committed = _safe_int(payload.get("committed_fingerprint_count"))
        return pending + committed, digest({"pending": pending, "committed": committed}), available
    runtime = payload.get("result")
    runtime_state = runtime.get("runtime_state") if isinstance(runtime, dict) else None
    if not isinstance(runtime_state, dict):
        return 0, digest({"state": "unavailable"}), False
    field = {
        "jobs": "job_count",
        "caches": "cache_entry_count",
        "temporary_files": "temporary_entry_count",
    }[name]
    count = _safe_int(runtime_state.get(field))
    runtime_fingerprint = runtime_state.get("fingerprint_sha256")
    fingerprint = (
        runtime_fingerprint
        if isinstance(runtime_fingerprint, str) and len(runtime_fingerprint) == _SHA256_HEX_LENGTH
        else digest(runtime_state)
    )
    return count, fingerprint, available


_POSITION_INVENTORY_FIELDS: Final = frozenset(
    {
        "AccountId",
        "Amount",
        "AssetType",
        "ClientId",
        "ExecutionTimeOpen",
        "IsForceOpen",
        "OpenPrice",
        "PositionId",
        "Uic",
        "ValueDate",
    },
)


def _position_inventory_fingerprint(response: JsonValue | None) -> str:
    """Hash stable position inventory while excluding live marks and P&L."""
    rows: JsonValue = response
    if isinstance(response, dict) and isinstance(response.get("Data"), list):
        rows = response["Data"]
    if not isinstance(rows, list):
        return digest({"positions": []})
    inventory: list[dict[str, JsonValue]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        base = raw.get("PositionBase")
        source = base if isinstance(base, dict) else raw
        stable = {key: source[key] for key in sorted(_POSITION_INVENTORY_FIELDS) if key in source}
        top_position_id = raw.get("PositionId")
        if "PositionId" not in stable and top_position_id is not None:
            stable["PositionId"] = top_position_id
        inventory.append(stable)
    inventory.sort(key=digest)
    return digest({"positions": inventory})


def _safe_response_value(value: JsonValue | None) -> JsonValue | None:
    if not isinstance(value, str):
        return value
    try:
        parsed: JsonValue = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed


def _safe_response_count(value: JsonValue | None) -> int:
    if isinstance(value, list):
        return len(value)
    if not isinstance(value, dict):
        return int(value is not None)
    data = value.get("Data")
    if isinstance(data, list):
        return len(data)
    return 1


def _safe_int(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def _run_read_and_refusal_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
    fixtures: MatrixFixtures,
) -> None:
    for tool, arguments in local_and_read_calls(fixtures):
        if tool in state.receipts:
            continue
        payload = await call_tool(client, tool, arguments)
        _record(state, tool, payload, arguments)
    for tool in sorted(NON_EXECUTABLE_SIM):
        arguments = refusal_arguments(tool)
        payload = await call_tool(client, tool, arguments)
        _record(state, tool, payload, arguments, status="expected_refusal")


async def run_disclaimer_refusal_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    lookup_args: dict[str, JsonValue] = {}
    required = await call_tool(client, "saxo_get_required_disclaimers", lookup_args)
    required_receipt = _record(
        state,
        "saxo_get_required_disclaimers",
        required,
        lookup_args,
        status="expected_refusal",
    )
    response_args: dict[str, JsonValue] = {}
    response = await call_tool(client, "saxo_register_disclaimer_response", response_args)
    response_receipt = _record(
        state,
        "saxo_register_disclaimer_response",
        response,
        response_args,
        status="expected_refusal",
    )
    refusal_ok = (
        required_receipt.status == "expected_refusal"
        and response_receipt.status == "expected_refusal"
        and response.payload.get("network_call_made") is not True
    )
    if not refusal_ok:
        state.errors.append("disclaimer_safe_refusal_missing")
    state.preflight = PreflightFlags(
        fixtures_ok=state.preflight.fixtures_ok,
        account_ok=state.preflight.account_ok,
        auth_ok=state.preflight.auth_ok,
        session_ok=state.preflight.session_ok,
        disclaimer_refusal_ok=refusal_ok,
        mcp_only=state.preflight.mcp_only,
    )
    state.lifecycle_seen.add("saxo_register_disclaimer_response")


async def run_analytics_case_phase(  # noqa: C901, PLR0912
    client: MatrixClient,
    state: MatrixRuntimeState,
    *,
    controlled_fixtures: MatrixFixtures | None = None,
    proof_recorder: _InstalledProofRecorder | None = None,
) -> None:
    """Exercise every applicable analytics case through the actual FastMCP call path."""
    coverage_errors = assert_analytics_case_coverage(analytics_sim_contracts())
    state.errors.extend(coverage_errors)
    contracts = {
        (contract.tool_id, case.kind): case
        for contract in analytics_sim_contracts()
        for case in contract.cases
    }
    per_tool: dict[str, dict[str, AnalyticsCaseReceipt]] = {
        tool_id: {} for tool_id in ANALYTICS_TOOL_IDS
    }
    observed_requests: dict[tuple[str, str, float | None], MatrixToolObservation] = {}
    ghost_observation: _ControlledGhostObservation | None = None
    for case_call in analytics_case_calls():
        if case_call.kind == "success" and case_call.tool_id in {
            "saxo_preview_analytics_deletion",
            "saxo_delete_analytics_data",
        }:
            continue
        arguments = materialize_analytics_case_arguments(
            case_call,
            state.analytics_resources,
        )
        observed_call = _bind_observed_source_precondition(
            case_call.model_copy(update={"arguments": arguments}),
            state.analytics_resources,
        )
        if (
            controlled_fixtures is not None
            and ghost_observation is None
            and observed_call.tool_id == "saxo_backtest_strategy"
            and observed_call.kind == "success"
        ):
            ghost_observation = await _run_controlled_sim_ghost_phase(
                client,
                state,
                observed_call,
                controlled_fixtures,
                proof_recorder=proof_recorder,
            )
        result = await _call_analytics_case_once(
            client,
            observed_call,
            observed_requests,
        )
        if observed_call.analysis_kind is not None:
            result = await _settle_analysis_observation(
                client,
                state,
                observed_call,
                result,
            )
        if observed_call.kind == "timeout" and result.timed_out:
            state.analytics_resources.remember_timeout(observed_call, arguments)
        timed_operation = state.analytics_resources.timed_operation
        reconciles_request = (
            timed_operation.request_sha256
            if observed_call.kind == "recovery"
            and timed_operation is not None
            and timed_operation.tool_id == observed_call.tool_id
            else None
        )
        case_contract = contracts[(observed_call.tool_id, observed_call.kind)]
        returned_kind, analysis_id, replay_authenticated = await _analysis_result_binding(
            client,
            state,
            observed_call,
            result,
        )
        case_receipt = analytics_case_receipt(
            observed_call,
            case_contract.expected_states,
            result,
            reconciles_request_sha256=reconciles_request,
            returned_analysis_kind=returned_kind,
            analysis_id=analysis_id,
            persisted_result_authenticated=replay_authenticated,
        )
        if case_receipt.kind == "success" and case_receipt.analysis_kind is not None:
            state.analysis_execution_receipts.append(case_receipt)
        else:
            per_tool[observed_call.tool_id][observed_call.kind] = case_receipt
        await _remember_analysis_case_outputs(
            client,
            state,
            observed_call,
            result,
            case_receipt,
        )
        if (
            observed_call.tool_id == "saxo_sync_research_data"
            and observed_call.kind == "success"
            and result.result_parsed
            and state.analytics_resources.dataset_ids_by_analysis_kind.get("price_bars")
        ):
            await _prepare_server_owned_analysis_inputs(client, state)
        if observed_call.kind == "success" and observed_call.analysis_kind is None:
            _record(
                state,
                observed_call.tool_id,
                result,
                observed_call.arguments,
                status=("expected_refusal" if case_receipt.state == "refused" else "completed"),
            )
        elif observed_call.tool_id not in state.receipts and case_receipt.state in {
            "degraded",
            "refused",
        }:
            _record(
                state,
                observed_call.tool_id,
                result,
                observed_call.arguments,
                status=("completed" if case_receipt.state == "degraded" else "expected_refusal"),
            )
        else:
            _observe_auxiliary(state, result)
    preview_receipt, delete_receipt = await run_analytics_cleanup_cases(client, state)
    per_tool["saxo_preview_analytics_deletion"]["success"] = preview_receipt
    per_tool["saxo_delete_analytics_data"]["success"] = delete_receipt
    if controlled_fixtures is not None:
        if ghost_observation is None:
            ghost_observation = await _run_controlled_sim_ghost_phase(
                client,
                state,
                None,
                controlled_fixtures,
                proof_recorder=proof_recorder,
            )
        await _refresh_post_cleanup_local_state(client, state)
        state.controlled_sim_lifecycle = _controlled_sim_lifecycle_receipt(
            state,
            per_tool,
            ghost_observation,
        )
    state.analytics_case_receipts.extend(
        AnalyticsToolCaseEvidence(
            tool_id=contract.tool_id,
            cases=tuple(per_tool[contract.tool_id][case.kind] for case in contract.cases),
        )
        for contract in analytics_sim_contracts()
    )


async def _call_analytics_case_once(
    client: MatrixClient,
    call: AnalyticsCaseCall,
    observed_requests: dict[tuple[str, str, float | None], MatrixToolObservation],
) -> MatrixToolObservation:
    """Reuse only one exact tool plus canonical-request observation in this phase."""
    key = (call.tool_id, digest(call.arguments), call.timeout_seconds)
    observed = observed_requests.get(key)
    if observed is not None:
        return observed
    observed = await call_tool(
        client,
        call.tool_id,
        call.arguments,
        timeout_seconds=call.timeout_seconds,
    )
    observed_requests[key] = observed
    return observed


async def _settle_analysis_observation(
    client: MatrixClient,
    state: MatrixRuntimeState,
    call: AnalyticsCaseCall,
    result: MatrixToolObservation,
) -> MatrixToolObservation:
    """Require a terminal long-job observation; queued work is never a conclusion."""
    if call.tool_id != "saxo_manage_analysis_job" or result.result_state not in _ACTIVE_JOB_STATES:
        return result
    job_id = _first_safe_handle(result.payload, "jb")
    if job_id is None:
        return result
    _extend_unique(state.analytics_resources.job_ids, [job_id])
    observed = result
    for _ in range(64):
        await checkpoint()
        observed = await call_tool(
            client,
            "saxo_manage_analysis_job",
            {"action": "check", "job_id": job_id},
        )
        _observe_auxiliary(state, observed)
        if observed.result_state in _TERMINAL_JOB_STATES:
            return observed
    return observed


async def _analysis_result_binding(
    client: MatrixClient,
    state: MatrixRuntimeState,
    call: AnalyticsCaseCall,
    result: MatrixToolObservation,
) -> tuple[str | None, str | None, bool]:
    """Replay the exact persisted result before one analysis receipt may pass."""
    if call.analysis_kind is None:
        return None, None, False
    returned_kind = _first_string_field(result.payload, "analysis_kind")
    analysis_id = _first_safe_handle(result.payload, "an")
    if result.result_state not in {"verified", "degraded", "job_completed"}:
        return returned_kind, analysis_id, False
    if analysis_id is None:
        return returned_kind, None, False
    replay = await call_tool(
        client,
        "saxo_explain_analysis",
        {"analysis_id": analysis_id},
    )
    _observe_auxiliary(state, replay)
    replay_kind = _first_string_field(replay.payload, "analysis_kind")
    replay_id = _first_safe_handle(replay.payload, "an")
    authenticated = (
        replay.result_parsed
        and replay.result_state == "passed"
        and replay_kind == call.analysis_kind
        and replay_id == analysis_id
    )
    return replay_kind or returned_kind, analysis_id, authenticated


async def _prepare_server_owned_analysis_inputs(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    """Capture and route only current server-owned inputs; missing coverage stays absent."""
    resources = state.analytics_resources
    if resources.account_selectors:
        account_capture = await call_tool(
            client,
            "saxo_sync_research_data",
            {
                "request": {
                    "items": [
                        {
                            "data_kind": "account_analytics",
                            "safe_account_selector": resources.account_selectors[0],
                            "analysis_kinds": [
                                "portfolio_performance",
                                "position_sizing",
                                "scenario_custom",
                                "portfolio_minimum_variance",
                                "derivatives_model",
                                "pretrade_impact",
                            ],
                            "instrument_handles": list(resources.instrument_handles[:25]),
                        },
                    ],
                },
            },
        )
        _observe_auxiliary(state, account_capture)
        auxiliary_call = AnalyticsCaseCall(
            tool_id="saxo_sync_research_data",
            kind="success",
            arguments={},
            input_strategy="sync_issued_instrument",
        )
        _remember_analytics_handles(resources, auxiliary_call, account_capture)
    if resources.instrument_handles:
        market_items: list[dict[str, JsonValue]] = [
            {
                "data_kind": "quote",
                "handle": resources.instrument_handles[0],
            },
        ]
        if resources.option_expiries:
            market_items.append(
                {
                    "data_kind": "option_chain",
                    "handle": resources.instrument_handles[0],
                    "expiries": list(resources.option_expiries),
                },
            )
        for market_item in market_items:
            market_capture = await call_tool(
                client,
                "saxo_sync_research_data",
                {"request": {"items": [market_item]}},
            )
            _observe_auxiliary(state, market_capture)
            _remember_analytics_handles(
                resources,
                AnalyticsCaseCall(
                    tool_id="saxo_sync_research_data",
                    kind="success",
                    arguments={},
                    input_strategy="sync_issued_instrument",
                ),
                market_capture,
            )
        quote_dataset_ids = resources.dataset_ids_by_analysis_kind.get("quote", [])
        if quote_dataset_ids:
            quote_read = await call_tool(
                client,
                "saxo_get_research_dataset",
                {"dataset_id": quote_dataset_ids[-1], "page": 1, "limit": 1},
            )
            _observe_auxiliary(state, quote_read)
            resources.pretrade_proposal_price = _observed_quote_midpoint(
                quote_read.payload,
            )
    for analysis_kind in (
        "portfolio_performance",
        "position_sizing",
        "scenario_custom",
        "portfolio_minimum_variance",
        "derivatives_model",
        "bounded_backtest",
    ):
        source_dataset_ids = _source_datasets_for_execution(resources, analysis_kind)
        if not source_dataset_ids:
            _remember_analysis_input_refusal(resources, analysis_kind)
            continue
        routed = await call_tool(
            client,
            "saxo_sync_research_data",
            {
                "request": {
                    "items": [
                        {
                            "data_kind": "analysis_input",
                            "analysis_kind": analysis_kind,
                            "source_dataset_ids": source_dataset_ids,
                        },
                    ],
                },
            },
        )
        _observe_auxiliary(state, routed)
        auxiliary_call = AnalyticsCaseCall(
            tool_id="saxo_sync_research_data",
            kind="success",
            arguments={},
            input_strategy="sync_issued_instrument",
        )
        _remember_analytics_handles(resources, auxiliary_call, routed)
        if not resources.analysis_input_dataset_ids_by_analysis_kind.get(analysis_kind):
            _remember_analysis_input_refusal(resources, analysis_kind, routed)


async def _prepare_server_owned_pretrade_input(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    """Bind a proposal context to one replayable result and the exact current source set."""
    resources = state.analytics_resources
    if resources.analysis_input_dataset_ids_by_analysis_kind.get("pretrade_impact"):
        return
    origins = resources.analysis_ids_by_kind.get("instrument_price_return", [])
    source_dataset_ids = _source_datasets_for_execution(resources, "pretrade_impact")
    if len(origins) != 1 or not source_dataset_ids:
        _remember_analysis_input_refusal(resources, "pretrade_impact")
        return
    routed = await call_tool(
        client,
        "saxo_sync_research_data",
        {
            "request": {
                "items": [
                    {
                        "data_kind": "analysis_input",
                        "analysis_kind": "pretrade_impact",
                        "source_dataset_ids": source_dataset_ids,
                        "origin_analysis_id": origins[0],
                    },
                ],
            },
        },
    )
    _observe_auxiliary(state, routed)
    _remember_analytics_handles(
        resources,
        AnalyticsCaseCall(
            tool_id="saxo_sync_research_data",
            kind="success",
            arguments={},
            input_strategy="sync_issued_instrument",
        ),
        routed,
    )
    if not resources.analysis_input_dataset_ids_by_analysis_kind.get("pretrade_impact"):
        _remember_analysis_input_refusal(resources, "pretrade_impact", routed)


def _source_datasets_for_execution(
    resources: AnalyticsRuntimeResources,
    analysis_kind: str,
) -> list[str]:
    """Select only the exact server-issued source families needed by one typed context."""
    routes = {
        "portfolio_performance": ("portfolio_performance",),
        "position_sizing": ("position_sizing", "price_bars"),
        "scenario_custom": ("scenario_custom",),
        "portfolio_minimum_variance": (
            "portfolio_minimum_variance",
            "price_bars",
        ),
        "derivatives_model": ("derivatives_model", "option_chain", "quote"),
        "bounded_backtest": ("price_bars",),
        "pretrade_impact": ("pretrade_impact", "price_bars", "quote"),
    }
    selected: list[str] = []
    for route in routes.get(analysis_kind, (analysis_kind,)):
        _extend_unique(
            selected,
            resources.dataset_ids_by_analysis_kind.get(route, []),
        )
    return selected[:25]


def _remember_analysis_input_refusal(
    resources: AnalyticsRuntimeResources,
    analysis_kind: str,
    observation: MatrixToolObservation | None = None,
) -> None:
    """Record only the server-observed absence of one typed source precondition."""
    evidence_sha256 = digest(
        {
            "analysis_kind": analysis_kind,
            "result_state": (
                observation.result_state if observation is not None else "source_scope_missing"
            ),
            "response": (redact_json(observation.payload) if observation is not None else None),
        },
    )
    for exact_kind in _ANALYSIS_INPUT_ALIASES.get(analysis_kind, (analysis_kind,)):
        resources.analysis_input_refusals_by_analysis_kind[exact_kind] = evidence_sha256


def _bind_observed_source_precondition(
    case_call: AnalyticsCaseCall,
    resources: AnalyticsRuntimeResources,
) -> AnalyticsCaseCall:
    """Bind a current source refusal without changing the production capability claim."""
    if case_call.kind != "success":
        return case_call
    route = _ANALYSIS_INPUT_ROUTE_BY_KIND.get(
        case_call.analysis_kind or "",
        {
            "saxo_analyze_portfolio": "portfolio_performance",
            "saxo_size_position": "position_sizing",
            "saxo_run_scenario": "scenario_custom",
            "saxo_optimize_portfolio": "portfolio_minimum_variance",
            "saxo_model_derivatives": "derivatives_model",
            "saxo_backtest_strategy": "bounded_backtest",
            "saxo_propose_trade_from_analysis": "pretrade_impact",
        }.get(case_call.tool_id, ""),
    )
    exact_kind = case_call.analysis_kind or route
    evidence_sha256 = resources.analysis_input_refusals_by_analysis_kind.get(
        exact_kind,
        resources.analysis_input_refusals_by_analysis_kind.get(route),
    )
    if evidence_sha256 is None:
        return case_call
    updates: dict[str, object] = {
        "source_precondition_refused": True,
        "source_precondition_evidence_sha256": evidence_sha256,
    }
    if case_call.expected_analysis_outcome == "persisted":
        updates["expected_analysis_outcome"] = "refused"
    return case_call.model_copy(update=updates)


def _observed_option_expiries(payload: JsonValue) -> tuple[str, ...]:  # noqa: C901
    """Read only ISO expiry dates observed through the logical fixture MCP response."""
    found: set[str] = set()

    def visit(value: JsonValue) -> None:
        if isinstance(value, str):
            try:
                decoded: JsonValue = json.loads(value)
            except json.JSONDecodeError:
                return
            visit(decoded)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key.casefold() in {"expiry", "expirydate"} and isinstance(item, str):
                try:
                    parsed = date.fromisoformat(item[:10])
                except ValueError:
                    pass
                else:
                    found.add(parsed.isoformat())
            else:
                visit(item)

    visit(payload)
    return tuple(sorted(found))


def _observed_quote_midpoint(payload: JsonValue) -> str | None:  # noqa: C901
    """Read one exact normalized midpoint observed through the logical MCP path."""
    found: list[Decimal] = []

    def decimal_value(value: JsonValue | None) -> Decimal | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() and parsed > 0 else None

    def visit(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("row_kind") == "quote":
            midpoint = decimal_value(value.get("mid_value"))
            if midpoint is None:
                bid = decimal_value(value.get("bid_value"))
                ask = decimal_value(value.get("ask_value"))
                if bid is not None and ask is not None and ask >= bid:
                    midpoint = (bid + ask) / Decimal(2)
            if midpoint is not None:
                found.append(midpoint)
        for item in value.values():
            visit(item)

    visit(payload)
    if len(found) != 1:
        return None
    return format(found[0], "f")


async def _run_controlled_sim_ghost_phase(  # noqa: C901, PLR0912, PLR0915
    client: MatrixClient,
    state: MatrixRuntimeState,
    backtest_call: AnalyticsCaseCall | None,
    fixtures: MatrixFixtures,
    *,
    proof_recorder: _InstalledProofRecorder | None,
) -> _ControlledGhostObservation:
    """Run at most one exact SIM fixture lifecycle, then reconcile before issuing proof."""
    reason = "controlled_ghost_input_unavailable"
    candidate: str | None = None
    account_alias: str | None = None
    dataset_id: str | None = None
    instrument_handle: str | None = None
    strategy: dict[str, JsonValue] | None = None
    request = backtest_call.arguments.get("request") if backtest_call is not None else None
    if isinstance(request, dict):
        raw_dataset_id = request.get("dataset_id")
        raw_instrument_handle = request.get("instrument_handle")
        raw_strategy = request.get("strategy")
        if (
            isinstance(raw_dataset_id, str)
            and isinstance(raw_instrument_handle, str)
            and isinstance(raw_strategy, dict)
            and len(state.analytics_resources.account_selectors) == 1
            and state.before is not None
            and state.preflight.fixtures_ok
            and state.preflight.account_ok
            and state.preflight.auth_ok
            and state.preflight.session_ok
            and SaxoRuntimeConfig.from_env().requested_environment is SaxoEnvironment.SIM
        ):
            if proof_recorder is None:
                reason = "controlled_ghost_source_binding_unavailable"
            else:
                try:
                    candidate = proof_recorder.candidate_commit()
                    account_alias = proof_recorder.controlled_backtest_source_binding(
                        raw_dataset_id,
                        raw_instrument_handle,
                        expected_uic=fixtures.stock_uic,
                        expected_asset_type=FIXTURE_ASSET_TYPE,
                    )
                except (OSError, ValueError):
                    reason = "controlled_ghost_source_binding_unavailable"
                else:
                    dataset_id = raw_dataset_id
                    instrument_handle = raw_instrument_handle
                    strategy = raw_strategy

    calls = 0
    preview_state = "not_run"
    place_state = "not_run"
    cancel_preview_state = "not_run"
    cancel_state = "not_run"
    disclaimer_present = False
    place_attempted = False
    cancel_attempted = False
    place_observation: MatrixToolObservation | None = None
    place_arguments: dict[str, JsonValue] = {}
    cancel_observation: MatrixToolObservation | None = None
    cancel_arguments: dict[str, JsonValue] = {}
    selector = (
        state.analytics_resources.account_selectors[0]
        if len(state.analytics_resources.account_selectors) == 1
        else None
    )
    observed_limit_price = state.analytics_resources.controlled_order_limit_price
    controlled_safety_prepared = False
    if all(
        value is not None
        for value in (
            candidate,
            account_alias,
            dataset_id,
            instrument_handle,
            strategy,
            selector,
            observed_limit_price,
        )
    ):
        if not _controlled_sim_orders_clear(state.before):
            reason = "controlled_ghost_preexisting_orders"
        elif proof_recorder is None:
            reason = "controlled_ghost_safety_binding_unavailable"
        else:
            try:
                proof_recorder.prepare_controlled_sim_safety(
                    cast("str", selector),
                    expected_uic=fixtures.stock_uic,
                )
            except (OSError, TypeError, ValueError):
                reason = "controlled_ghost_safety_binding_unavailable"
            else:
                controlled_safety_prepared = True
    if controlled_safety_prepared:
        preview_arguments: dict[str, JsonValue] = {
            "order_body": _controlled_sim_order_body(
                cast("str", selector),
                fixtures,
                observed_limit_price=cast("str", observed_limit_price),
            ),
        }
        preview = await call_tool(client, "saxo_create_order_preview", preview_arguments)
        calls += 1
        _record(state, "saxo_create_order_preview", preview, preview_arguments)
        state.lifecycle_seen.add("saxo_create_order_preview")
        disclaimer_present = _disclaimer_detected(preview.payload)
        preview_state = "completed" if preview.result_state == "preview_created" else "refused"
        preview_token = preview.payload.get("preview_token")
        if disclaimer_present:
            reason = "controlled_ghost_disclaimer_blocked"
        elif preview_state != "completed" or not isinstance(preview_token, str):
            reason = "controlled_ghost_preview_unavailable"
        else:
            place_arguments = {"preview_token": preview_token}
            place = await call_tool(client, "saxo_place_sim_order", place_arguments)
            place_observation = place
            calls += 1
            place_attempted = True
            state.lifecycle_seen.add("saxo_place_sim_order")
            place_state = (
                place.result_state
                if place.result_state
                in {
                    "completed",
                    "completed_unverified",
                    "unknown_state",
                    "partial_success",
                    "duplicate_or_conflict",
                    "post_boundary_transport_failure",
                }
                else "failed"
            )
            cancel_scope = place.payload.get("safe_cancel_by_instrument")
            write_arguments = (
                cancel_scope.get("write_preview_arguments")
                if isinstance(cancel_scope, dict)
                else None
            )
            if isinstance(write_arguments, dict):
                cancel_preview = await call_tool(
                    client,
                    "saxo_create_write_preview",
                    write_arguments,
                )
                calls += 1
                _record(
                    state,
                    "saxo_create_write_preview",
                    cancel_preview,
                    write_arguments,
                )
                state.lifecycle_seen.add("saxo_create_write_preview")
                cancel_preview_state = (
                    "completed" if cancel_preview.result_state == "preview_created" else "failed"
                )
                cancel_token = cancel_preview.payload.get("preview_token")
                if cancel_preview_state == "completed" and isinstance(cancel_token, str):
                    await _wait_for_sim_write_rate_limit()
                    cancel_arguments = {"preview_token": cancel_token}
                    cancel = await call_tool(
                        client,
                        "saxo_cancel_sim_orders_by_instrument",
                        cancel_arguments,
                    )
                    cancel_observation = cancel
                    calls += 1
                    cancel_attempted = True
                    state.lifecycle_seen.add("saxo_cancel_sim_orders_by_instrument")
                    cancel_state = (
                        cancel.result_state
                        if cancel.result_state in {"completed", "completed_unverified"}
                        else "failed"
                    )
                else:
                    reason = "controlled_ghost_cancel_preview_failed"
            else:
                reason = "controlled_ghost_cancel_scope_unavailable"

    if controlled_safety_prepared and proof_recorder is not None:
        proof_recorder.clear_controlled_sim_safety()
        controlled_safety_prepared = False
    after = await mcp_state_fingerprint(client, state)
    state.after = after
    ledger_arguments: dict[str, JsonValue] = {}
    ledger = await call_tool(client, "saxo_get_safe_request_ledger", ledger_arguments)
    calls += 1
    _record(state, "saxo_get_safe_request_ledger", ledger, ledger_arguments)
    ledger_complete = _complete_sim_request_ledger(ledger)
    ledger_sha256 = digest(ledger.payload) if ledger_complete else None
    if reason == "controlled_ghost_preexisting_orders":
        return _ControlledGhostObservation(
            reason_code=reason,
            evidence=None,
            ledger_fingerprint_sha256=ledger_sha256,
            receipt_bound=False,
            mcp_call_count=calls,
            sim_mutation_call_count=0,
        )
    before = state.before
    if (
        before is None
        or candidate is None
        or account_alias is None
        or dataset_id is None
        or instrument_handle is None
        or strategy is None
    ):
        if controlled_safety_prepared and proof_recorder is not None:
            proof_recorder.clear_controlled_sim_safety()
        return _ControlledGhostObservation(
            reason_code=reason,
            evidence=None,
            ledger_fingerprint_sha256=ledger_sha256,
            receipt_bound=False,
            mcp_call_count=calls,
            sim_mutation_call_count=int(place_attempted) + int(cancel_attempted),
        )
    before_ghost = _ghost_state_fingerprint(before)
    after_ghost = _ghost_state_fingerprint(after)
    state_reconciled = brokerage_ghost_state_reconciled(before, after)
    inventory_reconciled = brokerage_inventory_reconciled(before, after)
    reconciled_place = place_state in {"completed", "completed_unverified"}
    reconciled_cancel = cancel_state in {"completed", "completed_unverified"}
    if reconciled_place and reconciled_cancel and state_reconciled:
        place_state = "completed"
        cancel_state = "completed"
    if place_observation is not None:
        _record(
            state,
            "saxo_place_sim_order",
            place_observation,
            place_arguments,
            status="reconciled" if place_state == "completed" else "failed",
        )
    if cancel_observation is not None:
        _record(
            state,
            "saxo_cancel_sim_orders_by_instrument",
            cancel_observation,
            cancel_arguments,
            status="reconciled" if cancel_state == "completed" else "failed",
        )
    evidence = GhostLifecycleEvidence(
        candidate_commit=candidate,
        dataset_id=dataset_id,
        account_alias=account_alias,
        instrument_handle=instrument_handle,
        strategy_fingerprint_sha256=strategy_definition_fingerprint(
            parse_strategy_definition(strategy),
        ),
        fill_model="next_bar_open",
        environment="SIM",
        session_capabilities_current=state.preflight.session_ok,
        fixture_coverage_proved=state.preflight.fixtures_ok,
        preview_status=cast("GhostStepStatus", preview_state),
        place_status=cast("GhostPlaceStatus", place_state),
        cancel_preview_status=cast("GhostStepStatus", cancel_preview_state),
        cancel_status=cast("GhostStepStatus", cancel_state),
        preview_attempt_count=int(preview_state != "not_run"),
        place_attempt_count=int(place_attempted),
        cancel_preview_attempt_count=int(cancel_preview_state != "not_run"),
        cancel_attempt_count=int(cancel_attempted),
        orders_readback=_component_available(after, "orders"),
        positions_readback=_component_available(after, "positions"),
        trade_messages_readback=_component_available(after, "trade_messages"),
        balances_fingerprint_readback=_component_available(after, "balances"),
        request_ledger_read_last=ledger_complete,
        request_ledger_complete=ledger_complete,
        live_event_count=live_transport_events(ledger.payload),
        live_mutation_count=live_mutation_calls_in(ledger.payload),
        non_sim_event_count=_non_sim_ledger_event_count(ledger.payload),
        disclaimer_present=disclaimer_present,
        purchase_occurred=not inventory_reconciled,
        before=before_ghost,
        after=after_ghost,
    )
    bound = False
    if (
        ledger_sha256 is not None
        and place_state == "completed"
        and cancel_preview_state == "completed"
        and cancel_state == "completed"
        and state_reconciled
        and not disclaimer_present
    ):
        if proof_recorder is None:
            reason = "controlled_ghost_receipt_binding_refused"
        else:
            try:
                proof_recorder.record_observed_ghost_lifecycle(
                    evidence,
                    ledger_provenance_sha256=ledger_sha256,
                )
            except (OSError, ValueError):
                reason = "controlled_ghost_receipt_binding_refused"
            else:
                bound = True
                reason = "passed"
    elif not ledger_complete:
        reason = "controlled_ghost_request_ledger_incomplete"
    elif not state_reconciled:
        reason = "controlled_ghost_cleanup_not_equal"
    elif place_state != "completed":
        reason = "controlled_ghost_place_not_reconciled"
    if controlled_safety_prepared and proof_recorder is not None:
        proof_recorder.clear_controlled_sim_safety()
    return _ControlledGhostObservation(
        reason_code=reason,
        evidence=evidence,
        ledger_fingerprint_sha256=ledger_sha256,
        receipt_bound=bound,
        mcp_call_count=calls,
        sim_mutation_call_count=int(place_attempted) + int(cancel_attempted),
    )


def _controlled_sim_order_body(
    account_selector: str,
    fixtures: MatrixFixtures,
    *,
    observed_limit_price: str,
) -> dict[str, JsonValue]:
    """Build the exact registered SIM place body from one process-scoped selector."""
    return {
        "AccountKey": account_selector,
        "Uic": fixtures.stock_uic,
        "AssetType": FIXTURE_ASSET_TYPE,
        "Amount": fixtures.amount,
        "BuySell": "Buy",
        "ManualOrder": False,
        "OrderType": "Limit",
        "OrderPrice": float(Decimal(observed_limit_price)),
        "OrderDuration": {"DurationType": "DayOrder"},
        "ExternalReference": _CONTROLLED_GHOST_EXTERNAL_REFERENCE,
    }


def _controlled_sim_orders_clear(before: BrokerageStateFingerprint | None) -> bool:
    """Require one available zero-order readback before any controlled SIM write."""
    if before is None:
        return False
    return any(
        component.name == "orders"
        and component.observed_state == "available"
        and component.count == 0
        for component in before.components
    )


async def _wait_for_sim_write_rate_limit() -> None:
    """Keep the controlled cancel beyond the process-local one-write-per-second guard."""
    await anyio.sleep(_SIM_WRITE_RATE_LIMIT_WAIT_SECONDS)


def _observed_controlled_limit_price(payload: JsonValue) -> str | None:  # noqa: C901
    """Derive one tick-shaped resting bid one percent below one observed SIM quote."""
    source = payload
    response = source.get("response") if isinstance(source, dict) else None
    if isinstance(response, str):
        try:
            source = cast("JsonValue", json.loads(response))
        except json.JSONDecodeError:
            return None
    pairs: list[tuple[Decimal, Decimal]] = []

    def visit(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        quote = value.get("Quote")
        if isinstance(quote, dict):
            bid = quote.get("Bid")
            ask = quote.get("Ask")
            if (
                isinstance(bid, (int, float, str))
                and not isinstance(bid, bool)
                and isinstance(ask, (int, float, str))
                and not isinstance(ask, bool)
            ):
                try:
                    parsed_bid = Decimal(str(bid))
                    parsed_ask = Decimal(str(ask))
                except InvalidOperation:
                    pass
                else:
                    if (
                        parsed_bid.is_finite()
                        and parsed_ask.is_finite()
                        and parsed_bid > 0
                        and parsed_ask >= parsed_bid
                    ):
                        pairs.append((parsed_bid, parsed_ask))
        for item in value.values():
            visit(item)

    visit(source)
    if len(pairs) != 1:
        return None
    bid, _ask = pairs[0]
    quantum = Decimal(1).scaleb(cast("int", bid.as_tuple().exponent))
    resting = (bid * Decimal("0.99")).quantize(quantum, rounding=ROUND_DOWN)
    return format(resting, "f") if resting > 0 and resting < bid else None


def _ghost_state_fingerprint(state: BrokerageStateFingerprint) -> GhostStateFingerprint:
    components = {component.name: component for component in state.components}
    return GhostStateFingerprint(
        balance_fingerprint_sha256=components["balances"].fingerprint_sha256,
        orders_fingerprint_sha256=components["orders"].fingerprint_sha256,
        positions_fingerprint_sha256=components["positions"].fingerprint_sha256,
        trade_messages_fingerprint_sha256=components["trade_messages"].fingerprint_sha256,
        order_count=components["orders"].count,
        position_count=components["positions"].count,
        trade_message_count=components["trade_messages"].count,
    )


def _component_available(state: BrokerageStateFingerprint, name: str) -> bool:
    return next(
        component.observed_state == "available"
        for component in state.components
        if component.name == name
    )


def _disclaimer_detected(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(_disclaimer_detected(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("exact_disclaimer_content_present") is True:
        return True
    reasons = value.get("denial_reasons")
    if isinstance(reasons, list) and any(
        reason in {"blocking_disclaimer", "disclaimer_response_required"} for reason in reasons
    ):
        return True
    return any(_disclaimer_detected(item) for item in value.values())


def _complete_sim_request_ledger(result: MatrixToolObservation) -> bool:
    payload = result.payload
    events = payload.get("events")
    return bool(
        result.result_parsed
        and result.result_state == "passed"
        and payload.get("scope") == "current_mcp_session"
        and payload.get("ledger_complete") is True
        and payload.get("events_evicted") == 0
        and isinstance(events, list)
        and all(
            isinstance(event, dict) and str(event.get("environment", "")).upper() == "SIM"
            for event in events
        )
    )


def _non_sim_ledger_event_count(payload: dict[str, JsonValue]) -> int:
    events = payload.get("events")
    if not isinstance(events, list):
        return 1
    return sum(
        1
        for event in events
        if not isinstance(event, dict) or str(event.get("environment", "")).upper() != "SIM"
    )


async def _refresh_post_cleanup_local_state(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    if state.after is None:
        return
    components = list(state.after.components[:4])
    observed: dict[tuple[str, str], MatrixToolObservation] = {}
    for name, tool, arguments in state_read_calls()[4:]:
        key = (tool, digest(arguments))
        result = observed.get(key)
        if result is None:
            result = await call_tool(client, tool, arguments)
            observed[key] = result
            _observe_auxiliary(state, result)
        count, fingerprint, available = _state_component_values(name, result)
        components.append(
            BrokerageStateComponent(
                name=name,
                count=count,
                fingerprint_sha256=fingerprint,
                observed_state="available" if available else "unavailable",
                mcp_tool_ids=(tool,),
            ),
        )
    state.after = BrokerageStateFingerprint(components=tuple(components))


def _controlled_sim_lifecycle_receipt(
    state: MatrixRuntimeState,
    per_tool: dict[str, dict[str, AnalyticsCaseReceipt]],
    ghost: _ControlledGhostObservation,
) -> ControlledSimLifecycleReceipt | None:
    resources = state.analytics_resources
    before = state.before or _unavailable_state_fingerprint()
    after = state.after or _unavailable_state_fingerprint()
    successful = {tool: cases.get("success") for tool, cases in per_tool.items()}

    def passed(tool: str) -> bool:
        receipt = successful.get(tool)
        return receipt is not None and receipt.state == "passed" and receipt.result_parsed

    transaction_ok = {"transactions_v1", "bookings_v1"} <= resources.source_contract_ids
    context_ok, context_observation_count = _controlled_context_coverage(resources)
    options_ok = (
        passed("saxo_model_derivatives") and resources.option_entitlement_state == "available"
    )
    cleanup_ok = resources.cleanup_verified and brokerage_state_reconciled(before, after)
    if not cleanup_ok:
        state.errors.append("controlled_sim_cleanup_unverified")
        return None
    case_specs = (
        (
            "transaction_history",
            transaction_ok,
            "transaction_history_source_unavailable",
            resources.source_request_count,
            resources.source_mcp_call_count,
            0,
            "not_applicable",
        ),
        (
            "execution_context",
            context_ok,
            "typed_execution_context_incomplete",
            resources.source_request_count,
            context_observation_count,
            0,
            "not_applicable",
        ),
        (
            "ghost_portfolio",
            ghost.receipt_bound,
            ghost.reason_code,
            1 if ghost.evidence is not None else 0,
            ghost.mcp_call_count,
            ghost.sim_mutation_call_count,
            "not_applicable",
        ),
        (
            "options_entitlement",
            options_ok,
            "options_entitlement_unavailable",
            resources.source_request_count,
            resources.source_mcp_call_count,
            0,
            "available" if options_ok else "denied",
        ),
        (
            "cleanup",
            cleanup_ok,
            "controlled_cleanup_unverified",
            0,
            1,
            ghost.sim_mutation_call_count,
            "not_applicable",
        ),
    )
    case_receipts: list[ControlledSimCaseReceipt] = []
    analysis_tool_by_case = {
        "transaction_history": "saxo_analyze_portfolio",
        "options_entitlement": "saxo_model_derivatives",
    }
    for (
        case_id,
        ok,
        reason,
        source_count,
        mcp_count,
        mutation_count,
        entitlement,
    ) in case_specs:
        analysis_tool = analysis_tool_by_case.get(case_id)
        observed_analysis = successful.get(analysis_tool) if analysis_tool is not None else None
        case_state: Literal["passed", "degraded", "refused"] = (
            "passed"
            if ok
            else "degraded"
            if case_id == "options_entitlement" and entitlement == "denied"
            else "refused"
        )
        case_receipts.append(
            ControlledSimCaseReceipt(
                case_id=case_id,
                state=case_state,
                reason_code="passed" if ok else reason,
                source_request_count=source_count,
                mcp_call_count=mcp_count,
                sim_mutation_call_count=mutation_count,
                cleanup_complete=True,
                entitlement_state=cast(
                    "Literal['available', 'denied', 'not_applicable']",
                    entitlement,
                ),
                evidence_sha256=digest(
                    {
                        "case_id": case_id,
                        "ghost_ledger": ghost.ledger_fingerprint_sha256,
                        "observed_analysis": (
                            observed_analysis.evidence_sha256
                            if observed_analysis is not None
                            else None
                        ),
                        "reason": "passed" if ok else reason,
                        "source_request_count": source_count,
                    },
                ),
            ),
        )
    cases = tuple(case_receipts)
    state_value: Literal["passed", "reduced", "refused"] = (
        "refused"
        if any(case.state == "refused" for case in cases)
        else "reduced"
        if any(case.state == "degraded" for case in cases)
        else "passed"
    )
    return ControlledSimLifecycleReceipt(
        evidence_state=state_value,
        environment="SIM",
        cases=cases,
        before=before,
        after=after,
        live_events=state.live_events,
        live_mutation_calls=state.live_mutation_calls,
        request_ledger_read_last=ghost.ledger_fingerprint_sha256 is not None,
        request_ledger_complete=ghost.ledger_fingerprint_sha256 is not None,
        request_ledger_fingerprint_sha256=ghost.ledger_fingerprint_sha256,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=(
            ghost.evidence.purchase_occurred if ghost.evidence is not None else False
        ),
        disclaimer_response_made=False,
    )


def _controlled_context_coverage(
    resources: AnalyticsRuntimeResources,
) -> tuple[bool, int]:
    required = set(_ANALYSIS_INPUT_ALIASES)
    observed = (
        set(resources.analysis_input_dataset_ids_by_analysis_kind)
        | set(resources.analysis_input_refusals_by_analysis_kind)
    ) & required
    return required <= observed, len(observed)


def analytics_case_receipt(  # noqa: C901, PLR0912, PLR0913
    case_call: AnalyticsCaseCall,
    expected_states: tuple[str, ...],
    result: MatrixToolObservation,
    *,
    reconciles_request_sha256: str | None = None,
    returned_analysis_kind: str | None = None,
    analysis_id: str | None = None,
    persisted_result_authenticated: bool = False,
) -> AnalyticsCaseReceipt:
    private_values = (
        _private_values_detected(result.payload) if case_call.kind == "privacy" else False
    )
    broker_write = _broker_write_detected(result.payload)
    matched = result.result_state in expected_states
    refusal_state = result.result_state in {
        "refused",
        "denied",
        "invalid_arguments",
        "invalid_request",
        "job_cancelled",
        "job_expired",
        "job_failed",
        "job_interrupted_restart_required",
    }
    expected_outcome = case_call.expected_analysis_outcome
    if case_call.analysis_kind is not None:
        if (
            expected_outcome == "persisted"
            and result.result_state in {"verified", "degraded", "job_completed"}
            and returned_analysis_kind == case_call.analysis_kind
            and analysis_id is not None
            and persisted_result_authenticated
        ):
            case_state: Literal[
                "passed", "degraded", "refused", "timed_out", "reconciled", "failed"
            ] = "passed"
        elif expected_outcome == "reduced" and result.result_state in {"degraded", "reduced"}:
            case_state = "degraded"
        elif expected_outcome == "refused" and refusal_state:
            case_state = "refused"
        elif expected_outcome == "resolved" and result.result_state == "resolved":
            case_state = "passed"
        else:
            case_state = "failed"
    elif case_call.source_precondition_refused and refusal_state:
        case_state = "refused"
    elif case_call.kind == "timeout" and result.timed_out:
        case_state = cast(
            "Literal['passed', 'degraded', 'refused', 'timed_out', 'reconciled', 'failed']",
            "timed_out",
        )
    elif (
        not matched
        or broker_write
        or private_values
        or (case_call.kind == "recovery" and reconciles_request_sha256 is None)
    ):
        case_state = "failed"
    elif case_call.kind in {"success", "privacy"}:
        case_state = "passed"
    elif case_call.kind == "degradation":
        case_state = "refused" if refusal_state else "degraded"
    elif case_call.kind == "refusal":
        case_state = "refused"
    else:
        case_state = "reconciled"
    source_precondition_refused = case_call.source_precondition_refused and case_state == "refused"
    source_precondition_evidence_sha256 = (
        case_call.source_precondition_evidence_sha256 if source_precondition_refused else None
    )
    observed_reason_code = _observed_reason_code(result.payload)
    evidence_material: dict[str, JsonValue] = {
        "tool_id": case_call.tool_id,
        "kind": case_call.kind,
        "result_state": result.result_state,
        "observed_reason_code": observed_reason_code,
        "request_sha256": digest(case_call.arguments),
        "response_sha256": digest(result.payload),
        "reconciles_request_sha256": reconciles_request_sha256,
        "source_precondition_refused": source_precondition_refused,
        "source_precondition_evidence_sha256": source_precondition_evidence_sha256,
    }
    return AnalyticsCaseReceipt(
        kind=case_call.kind,
        tool_id=case_call.tool_id if case_call.analysis_kind is not None else None,
        analysis_kind=_analysis_kind_from_case_call(case_call),
        returned_analysis_kind=returned_analysis_kind,
        analysis_id=analysis_id,
        expected_analysis_outcome=expected_outcome,
        persisted_result_authenticated=persisted_result_authenticated,
        source_precondition_refused=source_precondition_refused,
        source_precondition_evidence_sha256=source_precondition_evidence_sha256,
        state=case_state,
        reason_code="observed" if case_state != "failed" else "unexpected_case_result",
        observed_reason_code=observed_reason_code,
        mcp_call_observed=True,
        result_parsed=result.result_parsed,
        result_state=result.result_state,
        mcp_is_error=result.mcp_is_error,
        network_call_made=result.payload.get("network_call_made") is True,
        broker_write_made=broker_write,
        private_values_published=private_values,
        request_sha256=digest(case_call.arguments),
        response_sha256=digest(result.payload),
        evidence_sha256=digest(evidence_material),
        reconciles_request_sha256=(
            reconciles_request_sha256 if case_state == "reconciled" else None
        ),
        reconciliation_observation_sha256=(
            digest(result.payload) if case_state == "reconciled" else None
        ),
    )


def _observed_reason_code(payload: dict[str, JsonValue]) -> str | None:
    """Retain one source-controlled reason token without private text or values."""
    value = payload.get("reason_code")
    if isinstance(value, str) and value in _SAFE_OBSERVED_REASON_CODES:
        return value
    return None


def _analysis_kind_from_case_call(case_call: AnalyticsCaseCall) -> str | None:
    return case_call.analysis_kind if case_call.kind == "success" else None


def materialize_analytics_case_arguments(  # noqa: C901, PLR0911, PLR0912 - bounded dispatch
    case_call: AnalyticsCaseCall,
    resources: AnalyticsRuntimeResources,
) -> dict[str, JsonValue]:
    """Bind one case to issued handles or a schema-valid local refusal fallback."""
    if case_call.kind in {"refusal", "privacy"}:
        return dict(case_call.arguments)
    if case_call.kind == "recovery":
        timed = resources.timed_operation
        if timed is None or timed.tool_id != case_call.tool_id:
            return {}
        return dict(timed.arguments)
    if case_call.tool_id == "saxo_analytics_capabilities":
        return {}
    if case_call.tool_id == "saxo_resolve_research_universe":
        return {
            "query": "AAPL" if case_call.kind == "success" else "controlled fixture",
            **({"asset_types": ["Stock"]} if case_call.kind == "success" else {}),
            **({"exchanges": ["NASDAQ"]} if case_call.kind == "success" else {}),
        }
    if case_call.tool_id == "saxo_manage_research_universe":
        return {"action": "list"}
    if case_call.tool_id == "saxo_sync_research_data":
        handle = _selected_handle(
            resources.instrument_handles,
            resources.degraded_instrument_handles,
            degraded=case_call.kind == "degradation",
        )
        if handle is None:
            return {}
        item: dict[str, JsonValue] = {
            "data_kind": "price_bars",
            "handle": handle,
            "interval": "1m",
            "start": (
                "2026-01-05T14:30:00Z" if case_call.kind == "success" else "2026-01-01T00:00:00Z"
            ),
            "end": (
                "2026-01-05T15:30:00Z" if case_call.kind == "success" else "2026-01-31T00:00:00Z"
            ),
        }
        return {"request": {"items": [item]}}
    if case_call.tool_id == "saxo_get_research_dataset":
        dataset_id = _selected_handle(
            resources.dataset_ids,
            resources.degraded_dataset_ids,
            degraded=case_call.kind == "degradation",
            require_degraded=case_call.kind == "degradation",
        )
        return {"dataset_id": dataset_id, "page": 1, "limit": 100} if dataset_id is not None else {}
    if case_call.tool_id in {
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
    }:
        return _materialize_analysis_arguments(case_call, resources)
    if case_call.tool_id in {
        "saxo_propose_trade_from_analysis",
        "saxo_render_analysis",
        "saxo_export_analysis",
        "saxo_explain_analysis",
    }:
        return _materialize_analysis_consumer_arguments(case_call, resources)
    if case_call.tool_id == "saxo_manage_analysis_job":
        if case_call.analysis_kind in {"goal_model", "monte_carlo"}:
            dataset_ids = resources.dataset_ids_by_analysis_kind.get(
                "portfolio_performance",
                resources.dataset_ids_by_analysis_kind.get("price_bars", []),
            )
            if not dataset_ids:
                return {}
            return {
                "action": "start",
                "request": {
                    "job_kind": "monte_carlo",
                    "dataset_ids": [dataset_ids[0]],
                    "parameters": [
                        {"name": "analysis_kind", "value": case_call.analysis_kind},
                    ],
                    "total_work_units": 1,
                },
            }
        if case_call.kind in {"timeout", "recovery"}:
            job_id = resources.job_ids[0] if resources.job_ids else None
            return {"action": "check", "job_id": job_id} if job_id is not None else {}
        analysis_id = resources.analysis_ids[0] if resources.analysis_ids else None
        if analysis_id is None:
            return _clone_arguments(case_call.arguments)
        return {
            "action": "start",
            "request": {
                "job_kind": "report_generation",
                "analysis_ids": [analysis_id],
                "parameters": [
                    {"name": "template_id", "value": "relative_performance"},
                    {"name": "output_format", "value": "html"},
                ],
                "total_work_units": 1,
            },
        }
    if case_call.tool_id == "saxo_list_analytics_storage":
        return {"scope": {}}
    return {}


async def _remember_analysis_case_outputs(
    client: MatrixClient,
    state: MatrixRuntimeState,
    observed_call: AnalyticsCaseCall,
    result: MatrixToolObservation,
    case_receipt: AnalyticsCaseReceipt,
) -> None:
    """Index one result before deriving any context that depends on its issued handle."""
    _remember_analytics_handles(state.analytics_resources, observed_call, result)
    returned_kind = _first_string_field(result.payload, "analysis_kind")
    if (
        case_receipt.state == "passed"
        and observed_call.kind == "success"
        and returned_kind == "instrument_price_return"
    ):
        await _prepare_server_owned_pretrade_input(client, state)


def _materialize_analysis_arguments(  # noqa: C901, PLR0912, PLR0915 - bounded adapters
    case_call: AnalyticsCaseCall,
    resources: AnalyticsRuntimeResources,
) -> dict[str, JsonValue]:
    degraded = case_call.kind == "degradation"
    default_route = {
        "saxo_analyze_market": "price_bars",
        "saxo_analyze_instruments": "price_bars",
        "saxo_analyze_portfolio": "portfolio_performance",
        "saxo_size_position": "position_sizing",
        "saxo_run_scenario": "scenario_custom",
        "saxo_optimize_portfolio": "portfolio_minimum_variance",
        "saxo_model_derivatives": "derivatives_model",
        "saxo_backtest_strategy": "bounded_backtest",
    }[case_call.tool_id]
    route = _ANALYSIS_INPUT_ROUTE_BY_KIND.get(
        case_call.analysis_kind or "",
        case_call.analysis_kind or default_route,
    )
    if case_call.tool_id in {"saxo_analyze_market", "saxo_analyze_instruments"}:
        route = "price_bars"
    elif case_call.tool_id == "saxo_analyze_portfolio":
        route = "portfolio_performance"
    elif case_call.tool_id == "saxo_model_derivatives":
        route = "derivatives_model"
    routed = (
        resources.degraded_dataset_ids_by_analysis_kind
        if degraded
        else resources.dataset_ids_by_analysis_kind
    )
    analysis_inputs = (
        resources.degraded_analysis_input_dataset_ids_by_analysis_kind
        if degraded
        else resources.analysis_input_dataset_ids_by_analysis_kind
    )
    dataset_ids = analysis_inputs.get(route, routed.get(route, []))
    if degraded and not dataset_ids:
        degraded_route = "price_bars" if route == "bounded_backtest" else route
        dataset_ids = resources.degraded_dataset_ids_by_analysis_kind.get(
            degraded_route,
            [],
        )
    if degraded and not dataset_ids:
        dataset_ids = resources.analysis_input_dataset_ids_by_analysis_kind.get(
            route,
            resources.dataset_ids_by_analysis_kind.get(route, []),
        )
    dataset_id = dataset_ids[0] if dataset_ids else None
    if dataset_id is None:
        return {}
    instrument_handle = _selected_handle(
        resources.instrument_handles,
        resources.degraded_instrument_handles,
        degraded=degraded,
    )
    arguments = _clone_arguments(case_call.arguments)
    request = arguments.get("request")
    if not isinstance(request, dict):
        return {}
    if "dataset_ids" in request:
        request["dataset_ids"] = [dataset_id]
    if "dataset_id" in request:
        request["dataset_id"] = dataset_id
    if "instrument_handles" in request:
        if instrument_handle is None:
            return {}
        request["instrument_handles"] = [instrument_handle]
    if "instrument_handle" in request:
        if instrument_handle is None:
            return {}
        request["instrument_handle"] = instrument_handle
    if case_call.analysis_kind is not None:
        request["analysis_kind"] = case_call.analysis_kind
        if case_call.tool_id == "saxo_optimize_portfolio":
            request["objective"] = (
                "risk_parity"
                if case_call.analysis_kind == "portfolio_risk_parity"
                else "minimum_variance"
            )
    if degraded:
        if case_call.tool_id == "saxo_size_position":
            request["maximum_loss"] = "2"
        elif case_call.tool_id == "saxo_run_scenario":
            shocks = request.get("shocks")
            if isinstance(shocks, list) and shocks and isinstance(shocks[0], dict):
                shocks[0]["price_shock_ratio"] = "-0.2"
        elif case_call.tool_id == "saxo_optimize_portfolio":
            request["maximum_turnover"] = "0"
        elif case_call.tool_id == "saxo_backtest_strategy":
            request["starting_equity"] = 500.0
    if case_call.tool_id == "saxo_run_scenario":
        context_handles = resources.analysis_input_instrument_handles_by_analysis_kind.get(
            route,
            [],
        )
        if context_handles:
            requested_kind = request.get("analysis_kind")
            ratio = (
                "0"
                if requested_kind in {"margin_fire_drill", "scenario_currency"}
                else "-0.2"
                if degraded
                else "-0.1"
            )
            request["shocks"] = [
                {
                    "instrument_handle": handle,
                    "price_shock_ratio": ratio,
                }
                for handle in context_handles
            ]
    return arguments


def _materialize_analysis_consumer_arguments(
    case_call: AnalyticsCaseCall,
    resources: AnalyticsRuntimeResources,
) -> dict[str, JsonValue]:
    degraded = case_call.kind == "degradation"
    if case_call.tool_id == "saxo_propose_trade_from_analysis":
        analysis_ids = (
            resources.degraded_analysis_ids_by_kind if degraded else resources.analysis_ids_by_kind
        ).get("instrument_price_return", [])
        analysis_id = analysis_ids[0] if analysis_ids else None
        instrument = resources.instrument_handles[0] if resources.instrument_handles else None
        if analysis_id is None or instrument is None or resources.pretrade_proposal_price is None:
            return _clone_arguments(case_call.arguments)
        return {
            "analysis_id": analysis_id,
            "instrument_handle": instrument,
            "side": "buy",
            "quantity": "1" if not degraded else "2",
            "proposal_price": resources.pretrade_proposal_price,
            "maximum_loss": "1",
            "holding_period_days": 0,
            "visibility": "private_user_result",
        }
    analysis_id = _selected_handle(
        resources.analysis_ids,
        resources.degraded_analysis_ids,
        degraded=degraded,
        require_degraded=degraded,
    )
    if analysis_id is None:
        return _clone_arguments(case_call.arguments)
    if case_call.tool_id == "saxo_render_analysis":
        return {
            "analysis_id": analysis_id,
            "template_id": "relative_performance",
            "output_format": "html",
        }
    if case_call.tool_id == "saxo_export_analysis":
        return {
            "analysis_id": analysis_id,
            "export_kind": "table",
            "output_format": "json" if not degraded else "csv",
        }
    return {"analysis_id": analysis_id}


def _clone_arguments(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    cloned: JsonValue = json.loads(json.dumps(arguments, allow_nan=False))
    return cloned if isinstance(cloned, dict) else {}


def _selected_handle(
    primary: list[str],
    degraded_handles: list[str],
    *,
    degraded: bool,
    require_degraded: bool = False,
) -> str | None:
    if degraded and degraded_handles:
        return degraded_handles[0]
    if degraded and require_degraded:
        return None
    return primary[0] if primary else None


def _remember_analytics_handles(
    resources: AnalyticsRuntimeResources,
    case_call: AnalyticsCaseCall,
    result: MatrixToolObservation,
) -> None:
    if not result.result_parsed or result.mcp_is_error:
        return
    if case_call.tool_id == "saxo_sync_research_data":
        sync_result = result.payload.get("result")
        if isinstance(sync_result, dict):
            source_count = sync_result.get("source_request_count")
            if isinstance(source_count, int) and not isinstance(source_count, bool):
                resources.source_request_count += max(0, source_count)
                resources.source_mcp_call_count += int(source_count > 0)
    found: dict[str, list[str]] = {kind: [] for kind in ("ih", "ds", "an", "ar", "jb", "dp")}
    _collect_safe_handles(result.payload, found)
    degraded = case_call.kind == "degradation" or result.result_state in {
        "ambiguous",
        "degraded",
        "reduced",
        "unavailable",
    }
    _extend_unique(
        resources.degraded_instrument_handles if degraded else resources.instrument_handles,
        found["ih"],
    )
    _extend_unique(
        resources.degraded_dataset_ids if degraded else resources.dataset_ids,
        found["ds"],
    )
    _extend_unique(
        resources.degraded_analysis_ids if degraded else resources.analysis_ids,
        found["an"],
    )
    _extend_unique(resources.artifact_ids, found["ar"])
    _extend_unique(resources.job_ids, found["jb"])
    _remember_typed_resources(resources, result.payload, degraded=degraded)
    if case_call.tool_id == "saxo_preview_analytics_deletion" and found["dp"]:
        resources.deletion_token = found["dp"][0]


def _remember_typed_resources(  # noqa: C901
    resources: AnalyticsRuntimeResources,
    payload: dict[str, JsonValue],
    *,
    degraded: bool,
) -> None:
    """Index only server-issued handles that name their exact stored input kind."""
    analysis_routes = (
        resources.degraded_analysis_ids_by_kind if degraded else resources.analysis_ids_by_kind
    )

    def visit(value: JsonValue) -> None:  # noqa: C901, PLR0912
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        dataset_id = value.get("dataset_id")
        data_kind = value.get("data_kind")
        analysis_kind = value.get("analysis_kind")
        contract_id = value.get("contract_id")
        if isinstance(contract_id, str):
            resources.source_contract_ids.add(contract_id)
        if isinstance(dataset_id, str):
            quality_state = value.get("quality_state")
            dataset_degraded = (
                quality_state != "complete"
                if quality_state in {"complete", "partial", "stale", "missing", "invalid"}
                else degraded
            )
            dataset_routes = (
                resources.degraded_dataset_ids_by_analysis_kind
                if dataset_degraded
                else resources.dataset_ids_by_analysis_kind
            )
            analysis_input_routes = (
                resources.degraded_analysis_input_dataset_ids_by_analysis_kind
                if dataset_degraded
                else resources.analysis_input_dataset_ids_by_analysis_kind
            )
            if data_kind == "analysis_input" and isinstance(analysis_kind, str):
                _extend_unique(
                    analysis_input_routes.setdefault(analysis_kind, []),
                    [dataset_id],
                )
                instrument_handles = value.get("instrument_handles")
                if isinstance(instrument_handles, list):
                    safe_handles = [
                        handle
                        for handle in instrument_handles
                        if isinstance(handle, str) and _SAFE_HANDLE.fullmatch(handle)
                    ]
                    _extend_unique(
                        resources.analysis_input_instrument_handles_by_analysis_kind.setdefault(
                            analysis_kind,
                            [],
                        ),
                        safe_handles,
                    )
                for exact_kind in _ANALYSIS_INPUT_ALIASES.get(
                    analysis_kind,
                    (analysis_kind,),
                ):
                    resources.analysis_input_refusals_by_analysis_kind.pop(
                        exact_kind,
                        None,
                    )
            else:
                _extend_unique(resources.source_dataset_ids, [dataset_id])
            route = (
                analysis_kind
                if isinstance(analysis_kind, str)
                else data_kind
                if isinstance(data_kind, str)
                else None
            )
            if route is not None:
                _extend_unique(dataset_routes.setdefault(route, []), [dataset_id])
            eligible = value.get("eligible_analysis_kinds")
            if isinstance(eligible, list):
                for eligible_kind in eligible:
                    if isinstance(eligible_kind, str):
                        _extend_unique(
                            dataset_routes.setdefault(eligible_kind, []),
                            [dataset_id],
                        )
        analysis_id = value.get("analysis_id")
        if isinstance(analysis_id, str) and isinstance(analysis_kind, str):
            _extend_unique(analysis_routes.setdefault(analysis_kind, []), [analysis_id])
        account_alias = value.get("account_alias")
        if isinstance(account_alias, str) and account_alias.startswith("aa_"):
            _extend_unique(resources.account_aliases, [account_alias])
        if data_kind == "option_chain":
            entitlement = value.get("entitlement_state")
            if entitlement in {"available", "denied"}:
                resources.option_entitlement_state = cast(
                    "Literal['available', 'denied', 'unknown']",
                    entitlement,
                )
        for item in value.values():
            visit(item)

    visit(payload)


def _collect_safe_handles(value: JsonValue, found: dict[str, list[str]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_safe_handles(item, found)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_safe_handles(item, found)
        return
    if not isinstance(value, str):
        return
    match = _SAFE_HANDLE.fullmatch(value)
    if match is not None:
        found[match.group("kind")].append(value)


def _first_safe_handle(value: JsonValue, kind: str) -> str | None:
    found: dict[str, list[str]] = {item: [] for item in ("ih", "ds", "an", "ar", "jb", "dp")}
    _collect_safe_handles(value, found)
    values = found.get(kind, [])
    return values[0] if values else None


def _first_string_field(value: JsonValue, name: str) -> str | None:
    if isinstance(value, list):
        for item in value:
            found = _first_string_field(item, name)
            if found is not None:
                return found
        return None
    if not isinstance(value, dict):
        return None
    direct = value.get(name)
    if isinstance(direct, str):
        return direct
    for item in value.values():
        found = _first_string_field(item, name)
        if found is not None:
            return found
    return None


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


async def run_analytics_cleanup_cases(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> tuple[AnalyticsCaseReceipt, AnalyticsCaseReceipt]:
    """Stop matrix-owned jobs, delete the exact isolated closure, and prove it empty."""
    jobs_stopped = await _stop_analytics_jobs(client, state)
    listing = await call_tool(client, "saxo_list_analytics_storage", {"scope": {}})
    _observe_auxiliary(state, listing)
    data_types = _listed_storage_types(listing)
    preview_call, delete_call = (
        call
        for call in analytics_case_calls()
        if call.kind == "success"
        and call.tool_id in {"saxo_preview_analytics_deletion", "saxo_delete_analytics_data"}
    )
    data_type_values: list[JsonValue] = list(data_types)
    preview_arguments: dict[str, JsonValue] = (
        {"scope": {"data_types": data_type_values}} if jobs_stopped else {}
    )
    preview_observation = await call_tool(
        client,
        preview_call.tool_id,
        preview_arguments,
    )
    observed_preview = preview_call.model_copy(update={"arguments": preview_arguments})
    _remember_analytics_handles(
        state.analytics_resources,
        observed_preview,
        preview_observation,
    )
    preview_contract = next(
        case
        for contract in analytics_sim_contracts()
        if contract.tool_id == preview_call.tool_id
        for case in contract.cases
        if case.kind == "success"
    )
    preview_receipt = analytics_case_receipt(
        observed_preview,
        preview_contract.expected_states,
        preview_observation,
    )
    _record(
        state,
        observed_preview.tool_id,
        preview_observation,
        preview_arguments,
        status="completed",
    )

    token = state.analytics_resources.deletion_token if jobs_stopped else None
    delete_arguments: dict[str, JsonValue] = {"token": token} if token is not None else {}
    delete_observation = await call_tool(
        client,
        delete_call.tool_id,
        delete_arguments,
    )
    observed_delete = delete_call.model_copy(update={"arguments": delete_arguments})
    delete_contract = next(
        case
        for contract in analytics_sim_contracts()
        if contract.tool_id == delete_call.tool_id
        for case in contract.cases
        if case.kind == "success"
    )
    delete_receipt = analytics_case_receipt(
        observed_delete,
        delete_contract.expected_states,
        delete_observation,
    )
    _record(
        state,
        observed_delete.tool_id,
        delete_observation,
        delete_arguments,
        status="completed",
    )
    after = await call_tool(client, "saxo_list_analytics_storage", {"scope": {}})
    _observe_auxiliary(state, after)
    clean = _storage_is_empty(after)
    state.analytics_resources.cleanup_verified = clean
    if not clean:
        state.errors.append("analytics_cleanup_incomplete")
        state.uncleaned = max(state.uncleaned, 1)
    return preview_receipt, delete_receipt


async def _stop_analytics_jobs(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> bool:
    stopped = True
    for job_id in tuple(state.analytics_resources.job_ids):
        arguments: dict[str, JsonValue] = {"action": "check", "job_id": job_id}
        observation = await call_tool(client, "saxo_manage_analysis_job", arguments)
        _observe_auxiliary(state, observation)
        if observation.result_state in _ACTIVE_JOB_STATES:
            cancel_arguments: dict[str, JsonValue] = {"action": "cancel", "job_id": job_id}
            observation = await call_tool(
                client,
                "saxo_manage_analysis_job",
                cancel_arguments,
            )
            _observe_auxiliary(state, observation)
        for _ in range(16):
            if observation.result_state in _TERMINAL_JOB_STATES:
                break
            await anyio.sleep(0)  # noqa: ASYNC115 - yield to the bounded in-process job
            observation = await call_tool(client, "saxo_manage_analysis_job", arguments)
            _observe_auxiliary(state, observation)
        if observation.result_state not in _TERMINAL_JOB_STATES:
            state.errors.append("analytics_job_cleanup_incomplete")
            state.uncleaned = max(state.uncleaned, 1)
            stopped = False
    return stopped


def _listed_storage_types(result: MatrixToolObservation) -> list[str]:
    if not result.result_parsed or result.result_state != "passed":
        return []
    payload = result.payload.get("result")
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    found: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        data_type = item.get("data_type")
        if isinstance(data_type, str):
            found.add(data_type)
    found.discard("deletion_receipts")
    return sorted(found)


def _storage_is_empty(result: MatrixToolObservation) -> bool:
    if not result.result_parsed or result.result_state != "passed":
        return False
    payload = result.payload.get("result")
    if not isinstance(payload, dict) or payload.get("entries") != []:
        return False
    runtime = payload.get("runtime_state")
    return isinstance(runtime, dict) and all(
        runtime.get(field) == 0
        for field in ("job_count", "cache_entry_count", "temporary_entry_count")
    )


def _private_values_detected(payload: dict[str, JsonValue]) -> bool:
    findings, errors = scan_secret_text(
        "analytics-case-result",
        json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True),
    )
    return bool(findings or errors)


def _broker_write_detected(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(_broker_write_detected(item) for item in value)
    if not isinstance(value, dict):
        return False
    if any(
        value.get(key) is True
        for key in (
            "broker_write_made",
            "live_write_called",
            "order_placed",
            "order_modified",
            "order_cancelled",
            "purchase_occurred",
        )
    ):
        return True
    return any(_broker_write_detected(item) for item in value.values())


async def _run_preview_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    create_args: dict[str, JsonValue] = {}
    create_preview = await call_tool(client, "saxo_create_write_preview", create_args)
    _record(
        state,
        "saxo_create_write_preview",
        create_preview,
        create_args,
        status="expected_refusal",
    )
    state.lifecycle_seen.add("saxo_create_write_preview")
    commit_args: dict[str, JsonValue] = {}
    commit = await call_tool(client, "saxo_commit_write_preview", commit_args)
    _record(
        state,
        "saxo_commit_write_preview",
        commit,
        commit_args,
        status="expected_refusal",
    )
    state.lifecycle_seen.add("saxo_commit_write_preview")


async def _run_order_mutation_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    for production in (True, False):
        for write_class in ORDER_WRITE_CLASSES:
            base = ORDER_WRITE_SPECS[write_class]
            tool_name = PRODUCTION_ORDER_TOOL_NAMES[write_class] if production else base.tool_name
            arguments: dict[str, JsonValue] = {}
            result = await call_tool(client, tool_name, arguments)
            _record(
                state,
                tool_name,
                result,
                arguments,
                status="expected_refusal",
            )
            state.lifecycle_seen.add(tool_name)


async def _run_trading_write_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
) -> None:
    for spec in trading_write_specs():
        state.registered_ops.append(spec.operation_id)
    for tool_name in ("saxo_prepare_trading_write", "saxo_execute_trading_write"):
        arguments: dict[str, JsonValue] = {}
        result = await call_tool(client, tool_name, arguments)
        _record(
            state,
            tool_name,
            result,
            arguments,
            status="expected_refusal",
        )
        state.lifecycle_seen.add(tool_name)


async def _run_trailing_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
    fixtures: MatrixFixtures,
) -> None:
    preview_args: dict[str, JsonValue] = {}
    order_preview = await call_tool(client, "saxo_create_order_preview", preview_args)
    _record(
        state,
        "saxo_create_order_preview",
        order_preview,
        preview_args,
        status="expected_refusal",
    )
    state.lifecycle_seen.add("saxo_create_order_preview")

    multileg_args: dict[str, JsonValue] = {}
    defaults = await call_tool(client, "saxo_get_multileg_order_defaults", multileg_args)
    _record(
        state,
        "saxo_get_multileg_order_defaults",
        defaults,
        multileg_args,
        status="expected_refusal",
    )

    _ = fixtures
    stream_args: dict[str, JsonValue] = {}
    stream = await call_tool(client, "saxo_create_streaming_price_subscription", stream_args)
    _record(
        state,
        "saxo_create_streaming_price_subscription",
        stream,
        stream_args,
        status="expected_refusal",
    )
    state.lifecycle_seen.add("saxo_create_streaming_price_subscription")
    cleanup_args: dict[str, JsonValue] = {}
    cleanup = await call_tool(client, "saxo_cleanup_streaming_subscriptions", cleanup_args)
    _record(
        state,
        "saxo_cleanup_streaming_subscriptions",
        cleanup,
        cleanup_args,
        status="expected_refusal",
    )
    state.lifecycle_seen.add("saxo_cleanup_streaming_subscriptions")

    for tool, arguments in auth_lifecycle_calls():
        if tool in state.receipts:
            continue
        payload = await call_tool(client, tool, arguments)
        _record(state, tool, payload, arguments, status="expected_refusal")


def _record(
    state: MatrixRuntimeState,
    tool: str,
    result: MatrixToolObservation,
    arguments: dict[str, JsonValue],
    *,
    status: Literal["completed", "expected_refusal", "reconciled", "failed"] = "completed",
) -> MatrixScenarioReceipt:
    try:
        receipt = receipt_for(tool, result, arguments, status=status)
    except ValueError:
        receipt = receipt_for(tool, result, arguments, status="failed")
        state.errors.append(f"tool_result_state_mismatch:{tool}")
    state.receipts[tool] = receipt
    _observe_auxiliary(state, result)
    return receipt


def _observe_auxiliary(state: MatrixRuntimeState, result: MatrixToolObservation) -> None:
    state.hosts.update(hosts_of(result.payload))
    state.live_events += live_transport_events(result.payload)
    state.live_mutation_calls += live_mutation_calls_in(result.payload)


def _finalize(state: MatrixRuntimeState) -> SimToolMatrixReceipt:  # noqa: C901
    expected, _ = manifest_tools(SCENARIO_MANIFEST)
    missing = sorted(expected - set(state.receipts))
    for tool in missing:
        state.errors.append(f"missing_tool_receipt:{tool}")
    ordered_lifecycle = tuple(tool for tool in LIFECYCLE_TOOLS if tool in state.lifecycle_seen)
    if ordered_lifecycle != LIFECYCLE_TOOLS:
        missing_life = [tool for tool in LIFECYCLE_TOOLS if tool not in state.lifecycle_seen]
        state.errors.append("lifecycle_incomplete:" + ",".join(missing_life))
    before = state.before or _unavailable_state_fingerprint()
    after = state.after or _unavailable_state_fingerprint()
    if state.before is None or state.after is None:
        state.errors.append("state_fingerprint_missing")
    if not brokerage_state_reconciled(before, after):
        state.errors.append("state_fingerprint_mismatch")
        state.uncleaned = max(state.uncleaned, 1)
    if not state.analytics_resources.cleanup_verified:
        state.errors.append("analytics_cleanup_unverified")
        state.uncleaned = max(state.uncleaned, 1)
    if (
        state.controlled_sim_lifecycle is None
        or state.controlled_sim_lifecycle.evidence_state == "refused"
    ):
        state.errors.append("controlled_sim_lifecycle_unverified")
    if state.live_events:
        state.errors.append("live_transport_or_ledger_event")
    if state.live_mutation_calls:
        state.errors.append("live_mutation_call")
    expected_ops = {spec.operation_id for spec in trading_write_specs()}
    if set(state.registered_ops) != expected_ops:
        state.errors.append("registered_trading_write_coverage_incomplete")
    analytics_count = len(set(state.receipts) & set(ANALYTICS_TOOL_IDS))
    if analytics_count != len(ANALYTICS_TOOL_IDS):
        state.errors.append("analytics_tool_coverage_incomplete")
    unchanged = brokerage_state_reconciled(before, after)
    cleanup_complete = (
        state.uncleaned == 0 and unchanged and state.analytics_resources.cleanup_verified
    )
    status: Literal["passed", "failed", "blocked"] = (
        "blocked"
        if is_blocker(state.errors) or "controlled_sim_lifecycle_unverified" in state.errors
        else "failed"
        if state.errors or state.uncleaned
        else "passed"
    )
    return SimToolMatrixReceipt(
        status=status,
        reason=state.errors[0] if state.errors else "",
        tool_receipts=tuple(state.receipts[tool] for tool in sorted(state.receipts)),
        lifecycle_calls=ordered_lifecycle,
        registered_trading_write_ops=tuple(sorted(set(state.registered_ops))),
        disclaimer_response_made=False,
        disclaimer_refusal_observed=state.preflight.disclaimer_refusal_ok,
        fixture_reference_validated=state.preflight.fixtures_ok,
        account_allowlist_resolved=state.preflight.account_ok,
        auth_status_completed=state.preflight.auth_ok,
        session_capabilities_completed=state.preflight.session_ok,
        before_state_fingerprint=before,
        after_state_fingerprint=after,
        uncleaned_resources=state.uncleaned,
        hosts=tuple(sorted(state.hosts)),
        live_events=state.live_events,
        live_mutation_calls=state.live_mutation_calls,
        analytics_tool_receipt_count=analytics_count,
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        analytics_case_receipts=tuple(state.analytics_case_receipts),
        analysis_execution_receipts=tuple(state.analysis_execution_receipts),
        controlled_sim_lifecycle=state.controlled_sim_lifecycle,
        mcp_only_account_fixture_state=state.preflight.mcp_only,
        cleanup_complete=cleanup_complete,
        account_state_unchanged=unchanged,
        redacted_publication=True,
        errors=tuple(state.errors),
    )


def _blocked(state: MatrixRuntimeState) -> SimToolMatrixReceipt:
    unavailable = _unavailable_state_fingerprint()
    return SimToolMatrixReceipt(
        status="blocked",
        reason=state.errors[0],
        tool_receipts=tuple(state.receipts[tool] for tool in sorted(state.receipts)),
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_made=False,
        disclaimer_refusal_observed=state.preflight.disclaimer_refusal_ok,
        fixture_reference_validated=state.preflight.fixtures_ok,
        account_allowlist_resolved=state.preflight.account_ok,
        auth_status_completed=state.preflight.auth_ok,
        session_capabilities_completed=state.preflight.session_ok,
        before_state_fingerprint=unavailable,
        after_state_fingerprint=unavailable,
        uncleaned_resources=0,
        hosts=tuple(sorted(state.hosts)),
        live_events=state.live_events,
        live_mutation_calls=state.live_mutation_calls,
        analytics_tool_receipt_count=len(set(state.receipts) & set(ANALYTICS_TOOL_IDS)),
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        analytics_case_receipts=tuple(state.analytics_case_receipts),
        analysis_execution_receipts=tuple(state.analysis_execution_receipts),
        controlled_sim_lifecycle=state.controlled_sim_lifecycle,
        mcp_only_account_fixture_state=state.preflight.mcp_only,
        cleanup_complete=False,
        account_state_unchanged=False,
        redacted_publication=True,
        errors=tuple(state.errors),
    )


def _unavailable_state_fingerprint() -> BrokerageStateFingerprint:
    return BrokerageStateFingerprint(
        components=tuple(
            BrokerageStateComponent(
                name=name,
                count=0,
                fingerprint_sha256=digest({"state": "unavailable", "component": name}),
                observed_state="unavailable",
                mcp_tool_ids=("saxo_health",),
            )
            for name in BROKERAGE_STATE_COMPONENTS
        ),
    )


def _fail(out: Path, reason: str) -> int:
    write_scanned_json(out, {"status": "failed", "reason": reason, "environment": "SIM"})
    return 1
