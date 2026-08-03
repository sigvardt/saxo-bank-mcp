# allow: SIZE_OK - bounded SIM matrix orchestrates all 60 scenarios plus lifecycle coverage.
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Literal

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
    per_tool: dict[str, list[AnalyticsCaseReceipt]] = {
        tool_id: [] for tool_id in ANALYTICS_TOOL_IDS
    }
    for case_call in analytics_case_calls():
        result = await call_tool(
            client,
            case_call.tool_id,
            case_call.arguments,
            timeout_seconds=case_call.timeout_seconds,
        )
        case_contract = contracts[(case_call.tool_id, case_call.kind)]
        case_receipt = _analytics_case_receipt(case_call, case_contract.expected_states, result)
        per_tool[case_call.tool_id].append(case_receipt)
        if case_call.kind == "success":
            _record(
                state,
                case_call.tool_id,
                result,
                case_call.arguments,
                status="completed",
            )
        else:
            _observe_auxiliary(state, result)
    state.analytics_case_receipts.extend(
        AnalyticsToolCaseEvidence(tool_id=tool_id, cases=tuple(per_tool[tool_id]))
        for tool_id in ANALYTICS_TOOL_IDS
    )


def _analytics_case_receipt(
    case_call: AnalyticsCaseCall,
    expected_states: tuple[str, ...],
    result: MatrixToolObservation,
) -> AnalyticsCaseReceipt:
    private_values = (
        _private_values_detected(result.payload) if case_call.kind == "privacy" else False
    )
    broker_write = _broker_write_detected(result.payload)
    matched = result.result_state in expected_states
    if case_call.kind == "timeout" and result.timed_out:
        case_state: Literal[
            "passed", "degraded", "refused", "timed_out", "reconciled", "failed"
        ] = "timed_out"
    elif not matched or broker_write or private_values:
        case_state = "failed"
    elif case_call.kind in {"success", "privacy"}:
        case_state = "passed"
    elif case_call.kind == "degradation":
        case_state = "degraded"
    elif case_call.kind == "refusal" or result.result_state in {
        "refused",
        "invalid_arguments",
        "invalid_request",
    }:
        case_state = "refused"
    else:
        case_state = "reconciled"
    evidence_material: dict[str, JsonValue] = {
        "tool_id": case_call.tool_id,
        "kind": case_call.kind,
        "result_state": result.result_state,
        "request_sha256": digest(case_call.arguments),
        "response_sha256": digest(result.payload),
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
    cleanup_complete = state.uncleaned == 0 and unchanged
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
