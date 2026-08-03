# allow: SIZE_OK - bounded SIM matrix orchestrates all 60 scenarios plus lifecycle coverage.
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Final, Literal

import anyio
from fastmcp import Client

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.agent_skill_matrix import LIFECYCLE_TOOLS, SCENARIO_MANIFEST, manifest_tools
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
    analytics_case_calls,
    analytics_case_contract_sha256,
    analytics_sim_contracts,
    assert_analytics_case_coverage,
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
_SAFE_HANDLE = re.compile(r"^(?P<kind>ih|ds|an|ar|jb|dp)_[0-9a-f]{32}$")
_ACTIVE_JOB_STATES: Final = frozenset({"job_queued", "job_running"})
_TERMINAL_JOB_STATES: Final = frozenset(
    {
        "job_completed",
        "job_failed",
        "job_cancelled",
        "job_expired",
        "job_interrupted_restart_required",
    },
)

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


async def _run_matrix(fixtures: MatrixFixtures) -> SimToolMatrixReceipt:
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
            async with Client(mcp) as client:
                await _run_auth_preflight(client, state)
                await _run_mcp_account_and_fixture_preflight(client, state, fixtures)
                if state.errors:
                    return _blocked(state)
                state.before = await mcp_state_fingerprint(client, state)
                await _run_read_and_refusal_phase(client, state, fixtures)
                await run_analytics_case_phase(client, state)
                await run_disclaimer_refusal_phase(client, state)
                await _run_preview_phase(client, state)
                await _run_order_mutation_phase(client, state)
                await _run_trading_write_phase(client, state)
                await _run_trailing_phase(client, state, fixtures)
                state.after = await mcp_state_fingerprint(client, state)

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
    account_ok = account.result_state == "passed" and len(selectors) == 1
    fixture_results = [
        await call_tool(client, tool, arguments) for tool, arguments in fixture_read_calls(fixtures)
    ]
    for result in fixture_results:
        _observe_auxiliary(state, result)
    fixtures_ok = fixture_values_match(fixtures) and all(
        result.result_parsed and result.result_state == "passed" for result in fixture_results
    )
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


async def run_analytics_case_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
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
        observed_call = case_call.model_copy(update={"arguments": arguments})
        result = await call_tool(
            client,
            observed_call.tool_id,
            observed_call.arguments,
            timeout_seconds=observed_call.timeout_seconds,
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
        case_receipt = analytics_case_receipt(
            observed_call,
            case_contract.expected_states,
            result,
            reconciles_request_sha256=reconciles_request,
        )
        per_tool[observed_call.tool_id][observed_call.kind] = case_receipt
        _remember_analytics_handles(state.analytics_resources, observed_call, result)
        if observed_call.kind == "success":
            _record(
                state,
                observed_call.tool_id,
                result,
                observed_call.arguments,
                status="completed",
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
    state.analytics_case_receipts.extend(
        AnalyticsToolCaseEvidence(
            tool_id=contract.tool_id,
            cases=tuple(per_tool[contract.tool_id][case.kind] for case in contract.cases),
        )
        for contract in analytics_sim_contracts()
    )


def analytics_case_receipt(
    case_call: AnalyticsCaseCall,
    expected_states: tuple[str, ...],
    result: MatrixToolObservation,
    *,
    reconciles_request_sha256: str | None = None,
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
    }
    if case_call.kind == "timeout" and result.timed_out:
        case_state: Literal[
            "passed", "degraded", "refused", "timed_out", "reconciled", "failed"
        ] = "timed_out"
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
    evidence_material: dict[str, JsonValue] = {
        "tool_id": case_call.tool_id,
        "kind": case_call.kind,
        "result_state": result.result_state,
        "request_sha256": digest(case_call.arguments),
        "response_sha256": digest(result.payload),
        "reconciles_request_sha256": reconciles_request_sha256,
    }
    return AnalyticsCaseReceipt(
        kind=case_call.kind,
        state=case_state,
        reason_code="observed" if case_state != "failed" else "unexpected_case_result",
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


def materialize_analytics_case_arguments(  # noqa: C901, PLR0911, PLR0912 - bounded dispatch
    case_call: AnalyticsCaseCall,
    resources: AnalyticsRuntimeResources,
) -> dict[str, JsonValue]:
    """Bind one case to handles issued earlier in the same FastMCP session."""
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
            "query": (
                "controlled stock fixture" if case_call.kind == "success" else "controlled fixture"
            ),
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
            "interval": "1d" if case_call.kind == "success" else "1m",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-31T00:00:00Z",
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
        if case_call.kind in {"timeout", "recovery"}:
            job_id = resources.job_ids[0] if resources.job_ids else None
            return {"action": "check", "job_id": job_id} if job_id is not None else {}
        analysis_id = resources.analysis_ids[0] if resources.analysis_ids else None
        if analysis_id is None:
            return {}
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


def _materialize_analysis_arguments(  # noqa: C901, PLR0912 - bounded typed adapters
    case_call: AnalyticsCaseCall,
    resources: AnalyticsRuntimeResources,
) -> dict[str, JsonValue]:
    degraded = case_call.kind == "degradation"
    route = {
        "saxo_analyze_market": "price_bars",
        "saxo_analyze_instruments": "price_bars",
        "saxo_analyze_portfolio": "portfolio_performance",
        "saxo_size_position": "position_sizing",
        "saxo_run_scenario": "scenario_custom",
        "saxo_optimize_portfolio": "portfolio_minimum_variance",
        "saxo_model_derivatives": "derivatives_model",
        "saxo_backtest_strategy": "bounded_backtest",
    }[case_call.tool_id]
    routed = (
        resources.degraded_dataset_ids_by_analysis_kind
        if degraded
        else resources.dataset_ids_by_analysis_kind
    )
    dataset_ids = routed.get(route, [])
    if degraded and not dataset_ids:
        dataset_ids = resources.dataset_ids_by_analysis_kind.get("price_bars", [])
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
        if analysis_id is None or instrument is None:
            return {}
        return {
            "analysis_id": analysis_id,
            "instrument_handle": instrument,
            "side": "buy",
            "quantity": "1" if not degraded else "2",
            "proposal_price": "50",
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
        return {}
    if case_call.tool_id == "saxo_render_analysis":
        return {
            "analysis_id": analysis_id,
            "template_id": "relative_performance",
            "output_format": "png" if not degraded else "html",
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


def _remember_typed_resources(
    resources: AnalyticsRuntimeResources,
    payload: dict[str, JsonValue],
    *,
    degraded: bool,
) -> None:
    """Index only server-issued handles that name their exact stored input kind."""
    dataset_routes = (
        resources.degraded_dataset_ids_by_analysis_kind
        if degraded
        else resources.dataset_ids_by_analysis_kind
    )
    analysis_routes = (
        resources.degraded_analysis_ids_by_kind if degraded else resources.analysis_ids_by_kind
    )

    def visit(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        dataset_id = value.get("dataset_id")
        data_kind = value.get("data_kind")
        analysis_kind = value.get("analysis_kind")
        if isinstance(dataset_id, str):
            route = (
                analysis_kind
                if isinstance(analysis_kind, str)
                else data_kind
                if isinstance(data_kind, str)
                else None
            )
            if route is not None:
                _extend_unique(dataset_routes.setdefault(route, []), [dataset_id])
        analysis_id = value.get("analysis_id")
        if isinstance(analysis_id, str) and isinstance(analysis_kind, str):
            _extend_unique(analysis_routes.setdefault(analysis_kind, []), [analysis_id])
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
        {"scope": {"data_types": data_type_values}} if jobs_stopped and data_type_values else {}
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
    status: Literal["completed", "expected_refusal", "failed"] = "completed",
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


def _finalize(state: MatrixRuntimeState) -> SimToolMatrixReceipt:
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
    if before != after:
        state.errors.append("state_fingerprint_mismatch")
        state.uncleaned = max(state.uncleaned, 1)
    if not state.analytics_resources.cleanup_verified:
        state.errors.append("analytics_cleanup_unverified")
        state.uncleaned = max(state.uncleaned, 1)
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
    unchanged = before == after
    cleanup_complete = (
        state.uncleaned == 0 and unchanged and state.analytics_resources.cleanup_verified
    )
    status: Literal["passed", "failed", "blocked"] = (
        "blocked"
        if is_blocker(state.errors)
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
