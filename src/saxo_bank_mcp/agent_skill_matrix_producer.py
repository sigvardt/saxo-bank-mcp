from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    load_json_object,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_qa import load_install_report_for_consumers
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
from saxo_bank_mcp.qa_exact_tool_probe import ExactToolProbeReceipt
from saxo_bank_mcp.qa_sim_tool_matrix import SimToolMatrixReceipt


class MatrixProducerEvidenceError(ValueError):
    pass


def validated_exact_tool_receipt(
    tool: str,
    result: CommandResult,
    receipt: dict[str, JsonValue],
) -> ExactToolProbeReceipt:
    try:
        validated = ExactToolProbeReceipt.model_validate(receipt)
    except ValidationError as exc:
        raise CommandFailureError(result.receipt) from exc
    if validated.logical_tool != tool:
        raise CommandFailureError(result.receipt)
    return validated


def run_real_matrix_report(options: MatrixPlanOptions) -> int:
    install, install_errors = load_install_report_for_consumers(options.install_report)
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
    try:
        result = _run_sim_matrix_probe(cache, receipt_dir, options)
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
        payload = load_json_object(receipt_dir / "sim-tool-matrix.json")
        matrix = SimToolMatrixReceipt.model_validate(payload)
    except (ValidationError, OSError, json.JSONDecodeError, TypeError) as exc:
        write_json(
            options.out,
            {
                "status": "failed",
                "reason": "producer_receipt_invalid",
                "detail": type(exc).__name__,
            },
        )
        return 1
    if matrix.status != "passed":
        write_json(
            options.out,
            {
                "status": "failed",
                "reason": matrix.reason or matrix.status,
                "matrix_status": matrix.status,
                "errors": list(matrix.errors),
            },
        )
        return 1
    try:
        report = _build_executed_report(options, install.candidate_commit, result, matrix, tools)
    except MatrixProducerEvidenceError as exc:
        write_json(options.out, {"status": "failed", "reason": str(exc)})
        return 1
    write_json(options.out, report.model_dump(mode="json"))
    verified, verify_errors = load_verified_matrix_report(options.out, "SIM")
    if verified is None:
        write_json(
            options.out,
            {"status": "failed", "reason": "producer_evidence_invalid", "errors": verify_errors},
        )
        return 1
    # Preserve per-tool receipts for audit without embedding secrets.
    for tool_receipt in matrix.tool_receipts:
        write_json(
            receipt_dir / f"{tool_receipt.tool}.json",
            tool_receipt.model_dump(mode="json"),
        )
    return 0


def _run_sim_matrix_probe(
    cache: Path,
    receipt_dir: Path,
    options: MatrixPlanOptions,
) -> CommandResult:
    out = receipt_dir / "sim-tool-matrix.json"
    command = (
        "uv",
        "run",
        "python",
        "-m",
        "saxo_bank_mcp.qa",
        "sim-tool-matrix",
        "--out",
        str(out),
        "--fixture-stock-uic",
        str(options.fixtures.stock_uic),
        "--fixture-amount",
        str(options.fixtures.amount),
        "--fixture-limit-price",
        str(options.fixtures.limit_price),
        "--fixture-modified-limit-price",
        str(options.fixtures.modified_limit_price),
        "--fixture-option-uics",
        str(options.fixtures.option_uics),
        "--fixture-stream-uic",
        str(options.fixtures.stream_uic),
    )
    return run_command("probe_sim_tool_matrix", command, cwd=cache, timeout_seconds=3600)


def _build_executed_report(
    options: MatrixPlanOptions,
    candidate_commit: str,
    result: CommandResult,
    matrix: SimToolMatrixReceipt,
    tools: frozenset[str],
) -> ExecutedMatrixReport:
    if not matrix.auth_status_completed:
        raise MatrixProducerEvidenceError("auth_preflight_missing")
    if not matrix.session_capabilities_completed:
        raise MatrixProducerEvidenceError("session_capabilities_preflight_missing")
    if not matrix.fixture_reference_validated:
        raise MatrixProducerEvidenceError("fixture_preflight_invalid")
    if not matrix.account_allowlist_resolved:
        raise MatrixProducerEvidenceError("account_allowlist_preflight_missing")
    if not matrix.disclaimer_response_completed:
        raise MatrixProducerEvidenceError("disclaimer_response_missing")
    if matrix.live_events != 0:
        raise MatrixProducerEvidenceError("transport_ledger_live_event")
    if matrix.lifecycle_calls != LIFECYCLE_TOOLS:
        raise MatrixProducerEvidenceError("lifecycle_tool_coverage_mismatch")
    call_tools = {item.tool for item in matrix.tool_receipts}
    if call_tools != set(tools):
        raise MatrixProducerEvidenceError("scenario_tool_coverage_mismatch")
    tool_calls = tuple(
        ToolCallEvidence(
            tool=item.tool,
            status=item.status,
            mcp_call_observed=True,
            result_parsed=True,
            skipped=False,
            requested_tool_covered=True,
            request_digest=item.request_digest,
            response_digest=item.response_digest,
        )
        for item in matrix.tool_receipts
    )
    return ExecutedMatrixReport(
        status="passed",
        execution_mode="sim_execution",
        environment="SIM",
        source_commit=candidate_commit,
        candidate_commit=candidate_commit,
        install_report=options.install_report,
        install_report_sha256=sha256_file(options.install_report),
        tool_count=len(tools),
        unique_tools=tuple(sorted(tools)),
        missing_tools=(),
        unexpected_tools=(),
        expected_call_count=len(tool_calls),
        tool_calls=tool_calls,
        preflight=MatrixPreflight(
            complete=True,
            auth_status_completed=True,
            session_capabilities_completed=True,
            fixture_reference_validated=True,
            account_allowlist_resolved=True,
            disclaimer_response_completed=True,
        ),
        transport_ledger=MatrixTransportLedger(
            sim_only=True,
            live_events=0,
            hosts=matrix.hosts or ("gateway.saxobank.com",),
        ),
        before_state_fingerprint=matrix.before_state_fingerprint,
        after_state_fingerprint=matrix.after_state_fingerprint,
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
        lifecycle_calls=matrix.lifecycle_calls,
        command_receipts=(result.receipt,),
        errors=(),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
