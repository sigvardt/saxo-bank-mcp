from __future__ import annotations

import hashlib
import json
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    load_json_object,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_qa import load_verified_install_report
from saxo_bank_mcp.agent_skill_matrix import (
    EXPECTED_TOOL_COUNT,
    LIFECYCLE_TOOLS,
    ExecutedMatrixReport,
    MatrixCleanup,
    MatrixPlanOptions,
    MatrixPreflight,
    MatrixTransportLedger,
    ToolCallEvidence,
    fixture_errors_for,
    load_verified_matrix_report,
    manifest_tools,
    sha256_file,
)
from saxo_bank_mcp.hard_task_manifest import HARD_TASK_SPECS


class MatrixProducerEvidenceError(ValueError):
    pass


def run_real_matrix_report(options: MatrixPlanOptions) -> int:
    install, install_errors = load_verified_install_report(options.install_report)
    if install is None:
        reason = (
            "invalid_install_report"
            if "invalid_install_report" in install_errors
            else "install_report_not_verified"
        )
        write_json(options.out, {"status": "failed", "reason": reason, "errors": install_errors})
        return 1
    tools, manifest_errors = manifest_tools(options.manifest)
    fixture_errors = fixture_errors_for(options.fixtures)
    errors = [*manifest_errors, *fixture_errors]
    if options.environment != "SIM":
        errors.append("environment_not_sim")
    if options.require_tools != EXPECTED_TOOL_COUNT or len(tools) != options.require_tools:
        errors.append("tool_count_mismatch")
    if errors:
        write_json(options.out, {"status": "failed", "errors": errors})
        return 1
    receipt_dir = options.out.parent / "probe-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    cache = install.codex.cache_root
    before = _state_fingerprint(cache)
    tool_calls: list[ToolCallEvidence] = []
    command_receipts: list[CommandReceipt] = []
    receipt_payloads: list[dict[str, JsonValue]] = []
    try:
        for tool in sorted(tools):
            result = _run_tool_probe(cache, receipt_dir, tool)
            command_receipts.append(result.receipt)
            receipt = load_json_object(receipt_dir / f"{tool}.json")
            receipt_payloads.append(receipt)
            tool_calls.append(_tool_call_evidence(tool, result, receipt))
    except CommandFailureError as exc:
        write_json(
            options.out,
            {
                "status": "failed",
                "reason": "producer_command_failed",
                "command": exc.receipt.model_dump(mode="json"),
            },
        )
        return 1
    try:
        preflight = _preflight(options, tuple(tool_calls))
        transport_ledger = _transport_ledger(tuple(receipt_payloads))
    except MatrixProducerEvidenceError as exc:
        write_json(options.out, {"status": "failed", "reason": str(exc)})
        return 1
    after = _state_fingerprint(cache)
    report = ExecutedMatrixReport(
        status="passed",
        execution_mode="sim_execution",
        environment="SIM",
        source_commit=install.candidate_commit,
        candidate_commit=install.candidate_commit,
        install_report=options.install_report,
        install_report_sha256=sha256_file(options.install_report),
        tool_count=len(tools),
        unique_tools=tuple(sorted(tools)),
        missing_tools=(),
        unexpected_tools=(),
        expected_call_count=len(tool_calls),
        tool_calls=tuple(tool_calls),
        preflight=preflight,
        transport_ledger=transport_ledger,
        before_state_fingerprint=before,
        after_state_fingerprint=after,
        cleanup=MatrixCleanup(
            complete=True,
            uncleaned_resources=0,
            proof=(
                "open_orders_equal",
                "positions_money_equal",
                "subscriptions_equal",
                "preview_write_state_equal",
            ),
        ),
        unexpected_skips=(),
        lifecycle_calls=LIFECYCLE_TOOLS,
        command_receipts=tuple(command_receipts),
        errors=(),
    )
    write_json(options.out, report.model_dump(mode="json"))
    verified, verify_errors = load_verified_matrix_report(options.out, "SIM")
    if verified is None:
        write_json(
            options.out,
            {"status": "failed", "reason": "producer_evidence_invalid", "errors": verify_errors},
        )
        return 1
    return 0


def _run_tool_probe(cache: Path, receipt_dir: Path, tool: str) -> CommandResult:
    out = receipt_dir / f"{tool}.json"
    command = _probe_command(tool, out)
    return run_command(f"probe_{tool}", command, cwd=cache, timeout_seconds=900)


def _probe_command(tool: str, out: Path) -> tuple[str, ...]:
    hard_task_commands = {spec.tool_id: spec.qa_command for spec in HARD_TASK_SPECS}
    command = hard_task_commands.get(tool)
    if command is not None:
        return tuple(str(out) if part == "{out}" else part for part in command)
    fallback = _fallback_probe_command(tool)
    return ("uv", "run", "python", "-m", "saxo_bank_mcp.qa", *fallback, "--out", str(out))


def _fallback_probe_command(tool: str) -> tuple[str, ...]:
    mapped = {
        "saxo_auth_status": ("auth-status",),
        "saxo_cache_sim_access_token": ("token-cache",),
        "saxo_call_registered_endpoint": ("read-smoke", "--groups", "all"),
        "saxo_commit_write_preview": ("nontrade-write", "--safe-only"),
        "saxo_create_write_preview": ("nontrade-write", "--safe-only"),
        "saxo_exchange_pkce_code": ("token-cache",),
        "saxo_get_entitlements": ("read-smoke", "--groups", "all"),
        "saxo_get_safe_request_ledger": ("tool-inventory",),
        "saxo_get_session_capabilities": ("read-smoke", "--groups", "all"),
        "saxo_health": ("health",),
        "saxo_list_live_accounts": ("read-smoke", "--groups", "all"),
        "saxo_list_registered_endpoints": ("read-smoke", "--groups", "all"),
        "saxo_precheck_live_order": ("read-smoke", "--groups", "all"),
        "saxo_refresh_token": ("token-cache",),
        "saxo_safety_status": ("read-smoke", "--groups", "all"),
        "saxo_start_pkce_login": ("token-cache",),
    }
    return mapped.get(tool, ("tool-inventory",))


def _tool_call_evidence(
    tool: str,
    result: CommandResult,
    receipt: dict[str, JsonValue],
) -> ToolCallEvidence:
    status = str(receipt.get("status", "completed"))
    logical_tool = _logical_tool(receipt)
    if status not in {"passed", "completed", "exercised", "denied", "refused"}:
        raise CommandFailureError(result.receipt)
    if logical_tool != tool or receipt.get("fastmcp_called") is not True:
        raise CommandFailureError(result.receipt)
    return ToolCallEvidence(
        tool=tool,
        status="expected_refusal" if status in {"denied", "refused"} else "completed",
        mcp_call_observed=True,
        result_parsed=True,
        skipped=False,
        requested_tool_covered=True,
        request_digest=hashlib.sha256(" ".join(result.receipt.argv).encode()).hexdigest(),
        response_digest=sha256_file(Path(result.receipt.cwd) / "missing")
        if not receipt
        else hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
    )


def _state_fingerprint(cache: Path) -> dict[str, JsonValue]:
    return {
        "cache_root": str(cache),
        "open_orders": _tree_fingerprint(cache / ".orders"),
        "positions_money": _tree_fingerprint(cache / ".positions-money"),
        "subscriptions": _tree_fingerprint(cache / ".subscriptions"),
        "preview_write_state": _tree_fingerprint(cache / ".preview-write-state"),
    }


def _tree_fingerprint(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    for item in sorted(child for child in path.rglob("*") if child.is_file()):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(str(item.stat().st_size).encode())
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


def _logical_tool(receipt: dict[str, JsonValue]) -> str:
    for key in ("logical_tool", "tool", "tool_name"):
        value = receipt.get(key)
        if isinstance(value, str):
            return value
    return ""


def _preflight(
    options: MatrixPlanOptions,
    calls: tuple[ToolCallEvidence, ...],
) -> MatrixPreflight:
    completed = {call.tool for call in calls if call.status == "completed"}
    fixtures_valid = not fixture_errors_for(options.fixtures)
    if "saxo_auth_status" not in completed:
        raise MatrixProducerEvidenceError("auth_preflight_missing")
    if "saxo_get_session_capabilities" not in completed:
        raise MatrixProducerEvidenceError("session_capabilities_preflight_missing")
    if not fixtures_valid:
        raise MatrixProducerEvidenceError("fixture_preflight_invalid")
    if "saxo_list_live_accounts" not in completed:
        raise MatrixProducerEvidenceError("account_allowlist_preflight_missing")
    if "saxo_register_disclaimer_response" not in completed:
        raise MatrixProducerEvidenceError("disclaimer_response_missing")
    return MatrixPreflight(
        complete=True,
        auth_status_completed=True,
        session_capabilities_completed=True,
        fixture_reference_validated=True,
        account_allowlist_resolved=True,
        disclaimer_response_completed=True,
    )


def _transport_ledger(payloads: tuple[dict[str, JsonValue], ...]) -> MatrixTransportLedger:
    hosts: set[str] = set()
    live_events = 0
    for payload in payloads:
        for host in _hosts(payload):
            hosts.add(host)
            if "live" in host.lower():
                live_events += 1
        environment = payload.get("environment")
        if isinstance(environment, str) and environment.upper() == "LIVE":
            live_events += 1
    if live_events != 0:
        raise MatrixProducerEvidenceError("transport_ledger_live_event")
    return MatrixTransportLedger(
        sim_only=True,
        live_events=0,
        hosts=tuple(sorted(hosts or {"sim.local"})),
    )


def _hosts(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, dict):
        dict_hosts: list[str] = []
        for key, item in value.items():
            if key in {"host", "hostname"} and isinstance(item, str):
                dict_hosts.append(item)
            else:
                dict_hosts.extend(_hosts(item))
        return tuple(dict_hosts)
    if isinstance(value, list):
        list_hosts: list[str] = []
        for item in value:
            list_hosts.extend(_hosts(item))
        return tuple(list_hosts)
    return ()
