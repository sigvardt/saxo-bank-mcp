# allow: SIZE_OK - bounded SIM matrix orchestrates all 60 scenarios plus lifecycle coverage.
from __future__ import annotations

import tempfile
from dataclasses import replace
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
from saxo_bank_mcp.qa_account import resolve_sim_account_key
from saxo_bank_mcp.qa_analytics_sim import (
    analytics_case_contract_sha256,
    analytics_primary_calls,
    analytics_sim_contracts,
    assert_analytics_case_coverage,
    isolated_analytics_state,
    live_mutation_calls_in,
)
from saxo_bank_mcp.qa_order_probes import (
    call_order_tool_for_matrix,
    create_order_preview_for_matrix,
    post_tool_cleanup_for_matrix,
    safety_env_for_matrix,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import (
    MatrixClient,
    auth_lifecycle_calls,
    call_tool,
    discover_disclaimer_with_retry,
    hosts_of,
    is_blocker,
    live_transport_events,
    local_and_read_calls,
    needs_recovery,
    order_preview_args,
    receipt_for,
    refusal_arguments,
    state_fingerprint,
    validate_fixtures,
    write_preview_args,
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
from saxo_bank_mcp.qa_trade_probes import (
    disclaimer_response_completion_claim_allowed,
    disclaimer_response_status,
)
from saxo_bank_mcp.qa_trading_write_probes import (
    exercise_trading_write_spec,
    safety_environment_for_matrix,
)
from saxo_bank_mcp.safety import reset_safety_state
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.server_tool_ids import ANALYTICS_TOOL_IDS
from saxo_bank_mcp.streaming import reset_local_subscriptions
from saxo_bank_mcp.trading_write_registry import trading_write_specs

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
        lifecycle_seen=set(),
        registered_ops=[],
        uncleaned=0,
        preflight=PreflightFlags(
            fixtures_ok=False,
            account_ok=False,
            auth_ok=False,
            session_ok=False,
        ),
        before={},
        after={},
    )
    reset_safety_state()
    reset_local_subscriptions()
    with tempfile.TemporaryDirectory(prefix="saxo-mcp-task23-") as runtime_dir:
        runtime_root = Path(runtime_dir)
        with isolated_analytics_state(runtime_root / "state"):
            async with Client(mcp) as client:
                await _run_auth_preflight(client, state)
                account = await resolve_sim_account_key(
                    default_account_key="SIM-ACCOUNT-1",
                    tool_name="saxo_sim_tool_matrix",
                )
                account_ok = account.discovered and account.source == "sim_accounts_me"
                fixtures_ok = await validate_fixtures(fixtures)
                if not account_ok:
                    state.errors.append("account_allowlist_unresolved")
                if not fixtures_ok:
                    state.errors.append("fixture_reference_invalid")
                state.preflight = PreflightFlags(
                    fixtures_ok=fixtures_ok,
                    account_ok=account_ok,
                    auth_ok=state.preflight.auth_ok,
                    session_ok=state.preflight.session_ok,
                )
                if state.errors:
                    return _blocked(state)

                with (
                    safety_env_for_matrix(account.account_key),
                    safety_environment_for_matrix(account.account_key, runtime_root / "audit"),
                ):
                    state.before = await state_fingerprint()
                    await _run_read_and_refusal_phase(client, state, fixtures)
                    await _run_analytics_phase(client, state)
                    await _run_disclaimer_phase(client, state)
                    await _run_preview_phase(client, state, account.account_key, fixtures)
                    await _run_order_mutation_phase(client, state, account.account_key)
                    await _run_trading_write_phase(client, state, account.account_key)
                    await _run_trailing_phase(client, state, account.account_key, fixtures)
                    state.after = await state_fingerprint()

    return _finalize(state)


async def _run_auth_preflight(client: MatrixClient, state: MatrixRuntimeState) -> None:
    auth_payload = await call_tool(client, "saxo_auth_status", {})
    state.preflight = PreflightFlags(
        fixtures_ok=state.preflight.fixtures_ok,
        account_ok=state.preflight.account_ok,
        auth_ok=True,
        session_ok=state.preflight.session_ok,
    )
    _record(state, "saxo_auth_status", auth_payload, {})
    refresh = await call_tool(client, "saxo_refresh_token", {})
    _record(state, "saxo_refresh_token", refresh, {})
    if needs_recovery(auth_payload) or str(refresh.get("status", "")) in {
        "completed",
        "token_refreshed",
        "passed",
    }:
        auth_payload = await call_tool(client, "saxo_auth_status", {})
        _record(state, "saxo_auth_status", auth_payload, {})
    session = await call_tool(client, "saxo_get_session_capabilities", {})
    session_ok = str(session.get("status", "")) in {"completed", "passed"}
    if str(session.get("status", "")) == "auth_required":
        state.errors.append("sim_session_auth_required")
    state.preflight = PreflightFlags(
        fixtures_ok=state.preflight.fixtures_ok,
        account_ok=state.preflight.account_ok,
        auth_ok=state.preflight.auth_ok,
        session_ok=session_ok,
    )
    _record(state, "saxo_get_session_capabilities", session, {})


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


async def _run_disclaimer_phase(client: MatrixClient, state: MatrixRuntimeState) -> None:
    probe_input = await discover_disclaimer_with_retry()
    lookup_args: dict[str, JsonValue] = {
        "disclaimer_tokens": [probe_input.disclaimer_token],
    }
    required = await call_tool(client, "saxo_get_required_disclaimers", lookup_args)
    _record(state, "saxo_get_required_disclaimers", required, lookup_args)
    response_args: dict[str, JsonValue] = {
        "disclaimer_context": probe_input.disclaimer_context,
        "disclaimer_token": probe_input.disclaimer_token,
        "response_type": "Accepted",
    }
    response = await call_tool(client, "saxo_register_disclaimer_response", response_args)
    status_name = disclaimer_response_status(response, probe_input)
    disclaimer_ok = (
        probe_input.real_disclaimer_input_found
        and disclaimer_response_completion_claim_allowed(status_name)
    )
    if not probe_input.real_disclaimer_input_found:
        state.errors.append("disclaimer_context_unavailable")
    elif not disclaimer_ok:
        state.errors.append("disclaimer_response_failed")
    state.preflight = PreflightFlags(
        fixtures_ok=state.preflight.fixtures_ok,
        account_ok=state.preflight.account_ok,
        auth_ok=state.preflight.auth_ok,
        session_ok=state.preflight.session_ok,
        disclaimer_ok=disclaimer_ok,
    )
    _record(state, "saxo_register_disclaimer_response", response, response_args)
    state.lifecycle_seen.add("saxo_register_disclaimer_response")


async def _run_analytics_phase(client: MatrixClient, state: MatrixRuntimeState) -> None:
    """Exercise each analytics adapter once through the actual FastMCP call path."""
    coverage_errors = assert_analytics_case_coverage(analytics_sim_contracts())
    state.errors.extend(coverage_errors)
    for tool, arguments in analytics_primary_calls():
        payload = await call_tool(client, tool, arguments)
        status: Literal["completed", "expected_refusal"] = (
            "expected_refusal" if payload.get("status") == "refused" else "completed"
        )
        _record(state, tool, payload, arguments, status=status)


async def _run_preview_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
    account_key: str,
    fixtures: MatrixFixtures,
) -> None:
    create_args = write_preview_args(account_key, fixtures)
    create_preview = await call_tool(client, "saxo_create_write_preview", create_args)
    _record(state, "saxo_create_write_preview", create_preview, create_args)
    state.lifecycle_seen.add("saxo_create_write_preview")
    token = create_preview.get("preview_token")
    if isinstance(token, str) and token:
        commit_args: dict[str, JsonValue] = {"preview_token": token}
        commit = await call_tool(client, "saxo_commit_write_preview", commit_args)
        _record(state, "saxo_commit_write_preview", commit, commit_args)
        state.lifecycle_seen.add("saxo_commit_write_preview")
    else:
        state.errors.append("write_preview_token_missing")


async def _run_order_mutation_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
    account_key: str,
) -> None:
    for production in (True, False):
        for write_class in ORDER_WRITE_CLASSES:
            base = ORDER_WRITE_SPECS[write_class]
            spec = (
                replace(base, tool_name=PRODUCTION_ORDER_TOOL_NAMES[write_class])
                if production
                else base
            )
            preview, setup = await create_order_preview_for_matrix(client, spec, account_key)
            tool_payload = await call_order_tool_for_matrix(client, spec, preview)
            cleanup = await post_tool_cleanup_for_matrix(
                client,
                spec,
                account_key,
                setup,
                preview,
            )
            combined: dict[str, JsonValue] = {
                "preview": preview,
                "tool": tool_payload,
                "cleanup": cleanup,
            }
            arguments: dict[str, JsonValue] = {
                "write_class": write_class,
                "production": production,
            }
            _record(state, spec.tool_name, combined, arguments)
            state.lifecycle_seen.add(spec.tool_name)
            if cleanup.get("present_after_cleanup") is True:
                state.uncleaned += 1


async def _run_trading_write_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
    account_key: str,
) -> None:
    for spec in trading_write_specs():
        row = await exercise_trading_write_spec(client, spec, account_key)
        state.registered_ops.append(spec.operation_id)
        payload: dict[str, JsonValue] = dict(row)
        state.hosts.update(hosts_of(payload))
        state.live_events += live_transport_events(payload)
        if payload.get("cleanup_required") is True and payload.get("cleanup_status") not in {
            "completed",
            "not_required",
            None,
        }:
            state.uncleaned += 1
        for tool_name in ("saxo_prepare_trading_write", "saxo_execute_trading_write"):
            if tool_name not in state.receipts:
                _record(
                    state,
                    tool_name,
                    payload,
                    {"operation_id": spec.operation_id},
                )
            state.lifecycle_seen.add(tool_name)


async def _run_trailing_phase(
    client: MatrixClient,
    state: MatrixRuntimeState,
    account_key: str,
    fixtures: MatrixFixtures,
) -> None:
    preview_args = order_preview_args(account_key, fixtures)
    order_preview = await call_tool(client, "saxo_create_order_preview", preview_args)
    _record(state, "saxo_create_order_preview", order_preview, preview_args)
    state.lifecycle_seen.add("saxo_create_order_preview")

    multileg_args: dict[str, JsonValue] = {
        "account_key": account_key,
        "option_root_id": 120,
        "options_strategy_type": "Straddle",
    }
    defaults = await call_tool(client, "saxo_get_multileg_order_defaults", multileg_args)
    _record(state, "saxo_get_multileg_order_defaults", defaults, multileg_args)

    stream_args: dict[str, JsonValue] = {
        "context_id": "todo15ctx",
        "reference_id": "todo15prices",
        "uics": [fixtures.stream_uic],
        "asset_type": "FxSpot",
        "wait_seconds": 5.0,
    }
    stream = await call_tool(client, "saxo_create_streaming_price_subscription", stream_args)
    _record(state, "saxo_create_streaming_price_subscription", stream, stream_args)
    state.lifecycle_seen.add("saxo_create_streaming_price_subscription")
    cleanup_args: dict[str, JsonValue] = {"context_id": "todo15ctx"}
    cleanup = await call_tool(client, "saxo_cleanup_streaming_subscriptions", cleanup_args)
    _record(state, "saxo_cleanup_streaming_subscriptions", cleanup, cleanup_args)
    state.lifecycle_seen.add("saxo_cleanup_streaming_subscriptions")

    for tool, arguments in auth_lifecycle_calls():
        if tool in state.receipts:
            continue
        payload = await call_tool(client, tool, arguments)
        _record(state, tool, payload, arguments)


def _record(
    state: MatrixRuntimeState,
    tool: str,
    payload: dict[str, JsonValue],
    arguments: dict[str, JsonValue],
    *,
    status: Literal["completed", "expected_refusal"] = "completed",
) -> None:
    state.receipts[tool] = receipt_for(tool, payload, arguments, status=status)
    state.hosts.update(hosts_of(payload))
    state.live_events += live_transport_events(payload)
    state.live_mutation_calls += live_mutation_calls_in(payload)


def _finalize(state: MatrixRuntimeState) -> SimToolMatrixReceipt:
    expected, _ = manifest_tools(SCENARIO_MANIFEST)
    missing = sorted(expected - set(state.receipts))
    for tool in missing:
        state.errors.append(f"missing_tool_receipt:{tool}")
    ordered_lifecycle = tuple(tool for tool in LIFECYCLE_TOOLS if tool in state.lifecycle_seen)
    if ordered_lifecycle != LIFECYCLE_TOOLS:
        missing_life = [tool for tool in LIFECYCLE_TOOLS if tool not in state.lifecycle_seen]
        state.errors.append("lifecycle_incomplete:" + ",".join(missing_life))
    if state.before != state.after:
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
    unchanged = state.before == state.after
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
        disclaimer_response_completed=state.preflight.disclaimer_ok,
        fixture_reference_validated=state.preflight.fixtures_ok,
        account_allowlist_resolved=state.preflight.account_ok,
        auth_status_completed=state.preflight.auth_ok,
        session_capabilities_completed=state.preflight.session_ok,
        before_state_fingerprint=state.before,
        after_state_fingerprint=state.after,
        uncleaned_resources=state.uncleaned,
        hosts=tuple(sorted(state.hosts)),
        live_events=state.live_events,
        live_mutation_calls=state.live_mutation_calls,
        analytics_tool_receipt_count=analytics_count,
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        cleanup_complete=cleanup_complete,
        account_state_unchanged=unchanged,
        redacted_publication=True,
        errors=tuple(state.errors),
    )


def _blocked(state: MatrixRuntimeState) -> SimToolMatrixReceipt:
    return SimToolMatrixReceipt(
        status="blocked",
        reason=state.errors[0],
        tool_receipts=tuple(state.receipts[tool] for tool in sorted(state.receipts)),
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_completed=False,
        fixture_reference_validated=state.preflight.fixtures_ok,
        account_allowlist_resolved=state.preflight.account_ok,
        auth_status_completed=state.preflight.auth_ok,
        session_capabilities_completed=state.preflight.session_ok,
        before_state_fingerprint={},
        after_state_fingerprint={},
        uncleaned_resources=0,
        hosts=tuple(sorted(state.hosts)),
        live_events=state.live_events,
        live_mutation_calls=state.live_mutation_calls,
        analytics_tool_receipt_count=len(set(state.receipts) & set(ANALYTICS_TOOL_IDS)),
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        cleanup_complete=False,
        account_state_unchanged=False,
        redacted_publication=True,
        errors=tuple(state.errors),
    )


def _fail(out: Path, reason: str) -> int:
    write_scanned_json(out, {"status": "failed", "reason": reason, "environment": "SIM"})
    return 1
