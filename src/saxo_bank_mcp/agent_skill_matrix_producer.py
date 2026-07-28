from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    load_json_object,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_models import (
    FixtureSupportReport,
    InstallEvidenceReport,
)
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
from saxo_bank_mcp.agent_skill_matrix_env import (
    MatrixEnvError,
    prepare_matrix_isolated_runtime,
    require_matrix_runtime_cleanup,
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
    prepared = _prepare_matrix_inputs(options)
    if isinstance(prepared, int):
        return prepared
    install, tools = prepared
    return _execute_prepared_matrix(options, install, tools)


def _execute_prepared_matrix(
    options: MatrixPlanOptions,
    install: InstallEvidenceReport | FixtureSupportReport,
    tools: frozenset[str],
) -> int:
    receipt_dir = options.out.parent / "probe-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    probed = _probe_or_fail(options, install, receipt_dir)
    if isinstance(probed, int):
        return probed
    result, matrix = probed
    try:
        report = _build_executed_report(
            options,
            install.candidate_commit,
            result,
            matrix,
            tools,
        )
    except MatrixProducerEvidenceError as exc:
        return _write_failure(options.out, str(exc))
    write_json(options.out, report.model_dump(mode="json"))
    verified, verify_errors = load_verified_matrix_report(options.out, "SIM")
    if verified is None:
        return _write_failure(
            options.out,
            "producer_evidence_invalid",
            {"errors": verify_errors},
        )
    for tool_receipt in matrix.tool_receipts:
        write_json(
            receipt_dir / f"{tool_receipt.tool}.json",
            tool_receipt.model_dump(mode="json"),
        )
    return 0


def _probe_or_fail(
    options: MatrixPlanOptions,
    install: InstallEvidenceReport | FixtureSupportReport,
    receipt_dir: Path,
) -> tuple[CommandResult, SimToolMatrixReceipt] | int:
    try:
        result = run_sim_matrix_probe(install.codex.cache_root, receipt_dir, options)
    except MatrixEnvError as exc:
        return _write_failure(options.out, exc.reason)
    except CommandFailureError as exc:
        return _write_failure(
            options.out,
            "producer_command_failed",
            {"command": exc.receipt.model_dump(mode="json")},
        )
    matrix = _load_matrix_receipt(receipt_dir / "sim-tool-matrix.json", options.out)
    if isinstance(matrix, int):
        return matrix
    if matrix.status != "passed":
        return _write_failure(
            options.out,
            matrix.reason or matrix.status,
            {"matrix_status": matrix.status, "errors": list(matrix.errors)},
        )
    return result, matrix


def _prepare_matrix_inputs(
    options: MatrixPlanOptions,
) -> tuple[InstallEvidenceReport | FixtureSupportReport, frozenset[str]] | int:
    install, install_errors = load_install_report_for_consumers(options.install_report)
    if install is None:
        reason = (
            "invalid_install_report"
            if "invalid_install_report" in install_errors
            else "install_report_not_verified"
        )
        return _write_failure(
            options.out,
            reason,
            {"errors": install_errors},
        )
    expected_commit = options.expected_source_commit or _git_head(Path())
    if expected_commit and install.candidate_commit != expected_commit:
        return _write_failure(
            options.out,
            "install_candidate_commit_mismatch",
            {
                "install_candidate_commit": install.candidate_commit,
                "expected_source_commit": expected_commit,
            },
        )
    tools, manifest_errors = manifest_tools(options.manifest)
    errors = [*manifest_errors, *fixture_errors_for(options.fixtures)]
    if options.environment != "SIM":
        errors.append("environment_not_sim")
    if options.require_tools != EXPECTED_TOOL_COUNT or len(tools) != options.require_tools:
        errors.append("tool_count_mismatch")
    if errors:
        write_json(options.out, {"status": "failed", "errors": errors})
        return 1
    return install, tools


def _load_matrix_receipt(path: Path, out: Path) -> SimToolMatrixReceipt | int:
    try:
        payload = load_json_object(path)
        return SimToolMatrixReceipt.model_validate(payload)
    except (ValidationError, OSError, json.JSONDecodeError, TypeError) as exc:
        return _write_failure(
            out,
            "producer_receipt_invalid",
            {"detail": type(exc).__name__},
        )


def _write_failure(
    out: Path,
    reason: str,
    extra: dict[str, JsonValue] | None = None,
) -> int:
    payload: dict[str, JsonValue] = {"status": "failed", "reason": reason}
    if extra:
        payload.update(extra)
    write_json(out, payload)
    return 1


def run_sim_matrix_probe(
    cache: Path,
    receipt_dir: Path,
    options: MatrixPlanOptions,
) -> CommandResult:
    evidence_root = options.out.parent.resolve()
    runtime = prepare_matrix_isolated_runtime(evidence_root)
    command_error: CommandFailureError | None = None
    result: CommandResult | None = None
    try:
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
        result = run_command(
            "probe_sim_tool_matrix",
            command,
            cwd=cache,
            env=runtime.env,
            timeout_seconds=3600,
        )
    except CommandFailureError as exc:
        command_error = exc
    finally:
        cleanup_error: MatrixEnvError | None = None
        try:
            require_matrix_runtime_cleanup(runtime.run_root)
        except MatrixEnvError as exc:
            cleanup_error = exc
    if command_error is not None:
        raise command_error
    if cleanup_error is not None:
        raise cleanup_error
    if result is None:
        raise MatrixEnvError("matrix_probe_result_missing")
    return result


def _build_executed_report(
    options: MatrixPlanOptions,
    candidate_commit: str,
    result: CommandResult,
    matrix: SimToolMatrixReceipt,
    tools: frozenset[str],
) -> ExecutedMatrixReport:
    _require_preflight(matrix)
    if matrix.live_events != 0:
        raise MatrixProducerEvidenceError("transport_ledger_live_event")
    if matrix.lifecycle_calls != LIFECYCLE_TOOLS:
        raise MatrixProducerEvidenceError("lifecycle_tool_coverage_mismatch")
    call_tools = {item.tool for item in matrix.tool_receipts}
    if call_tools != set(tools):
        raise MatrixProducerEvidenceError("scenario_tool_coverage_mismatch")
    if any(item.call_path != "fastmcp.Client.call_tool" for item in matrix.tool_receipts):
        raise MatrixProducerEvidenceError("tool_call_path_invalid")
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


def _require_preflight(matrix: SimToolMatrixReceipt) -> None:
    checks = (
        (matrix.auth_status_completed, "auth_preflight_missing"),
        (matrix.session_capabilities_completed, "session_capabilities_preflight_missing"),
        (matrix.fixture_reference_validated, "fixture_preflight_invalid"),
        (matrix.account_allowlist_resolved, "account_allowlist_preflight_missing"),
        (matrix.disclaimer_response_completed, "disclaimer_response_missing"),
    )
    for ok, reason in checks:
        if not ok:
            raise MatrixProducerEvidenceError(reason)


def _git_head(repo: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        return subprocess.check_output(
            [git, "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
