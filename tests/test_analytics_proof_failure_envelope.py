from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, NoReturn, cast

import pytest
from pydantic import ValidationError

import saxo_bank_mcp.agent_skill_eval_process as eval_process
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    run_command,
)
from saxo_bank_mcp.agent_skill_eval_failure_records import (
    unobservable_model_failure_record,
)
from saxo_bank_mcp.agent_skill_eval_models import EvalRunRecord, EvalRunReport, load_eval_cases
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager, ManagedProcessResult
from saxo_bank_mcp.agent_skill_eval_runner import resolve_tool_grants
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    RouterCaseContext,
    RouterHomes,
    execute_router_model_case,
)
from saxo_bank_mcp.qa_analytics_proof_failure import (
    CodexNativeAgentEvaluationCaseSummary,
    CodexNativeAgentEvaluationFailureSummary,
    CodexNativeBootstrapEnvelope,
    CodexNativeBootstrapVerification,
    CodexNativeChildFailureEnvelope,
    CodexNativeProofPhase,
    CodexNativeProofProgress,
    CodexNativeSimPreflightReceipt,
    CodexNativeVerifiedChildFailure,
    build_child_failure_envelope,
    verify_bootstrap_envelope_file,
    verify_child_failure_envelope,
)
from saxo_bank_mcp.qa_analytics_proof_publication import (
    CodexNativeCandidateRunnerReceipt,
    CodexNativeProofPublication,
)

CANDIDATE = "1" * 40
CACHE_SHA256 = "2" * 64
MODULE_SHA256 = "3" * 64
CATALOG_SHA256 = "4" * 64
CONTRACT_SHA256 = "5" * 64
RUNTIME_BINDING_SHA256 = "7" * 64
INSTALL_REPORT_SHA256 = "8" * 64
AGENT_MODEL_EVENT_COUNT = 2
AGENT_TOTAL_MCP_EVENT_COUNT = 4
CLI_TOTAL_MCP_EVENT_COUNT = 3
CRASH_EXIT_CODE = -9
FAILED_CHILD_EXIT_CODE = 7
EXPECTED_BINDING_CALLS = 2
FAILED_EVAL_CASE_COUNT = 2
FAILED_EVAL_REQUIRED_TOOL_COUNT = 3
FAILED_MCP_PROBE_EXIT_CODE = 23
OWNER_FILE_MODE = 0o600
EXPECTED_RECEIPT_WRITE_COUNT = 2
RAW_ASSISTANT_EVENT_COUNT = 2
EVAL_CREATED_PROCESS_COUNT = 4
EVAL_TERMINATED_PROCESS_COUNT = 2


def _bootstrap_envelope(
    *,
    child_exit_code: int | None = 1,
    state: str = "failed",
    contract_sha256: str = CONTRACT_SHA256,
) -> CodexNativeBootstrapEnvelope:
    completed_phases = (
        ("entry", "producer_import", "producer_handoff", "producer_execution")
        if state == "complete"
        else ("entry", "producer_import", "producer_handoff")
    )
    material: dict[str, object] = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_bootstrap",
        "harness_policy": "codex_native_v1",
        "candidate_commit": CANDIDATE,
        "installed_cache_sha256": CACHE_SHA256,
        "bootstrap_module_sha256": "6" * 64,
        "producer_module_sha256": MODULE_SHA256,
        "catalog_sha256": CATALOG_SHA256,
        "contract_sha256": contract_sha256,
        "runtime_binding_sha256": RUNTIME_BINDING_SHA256,
        "install_report_sha256": INSTALL_REPORT_SHA256,
        "bootstrap_state": state,
        "completed_bootstrap_phases": completed_phases,
        "current_bootstrap_phase": "complete" if state == "complete" else "producer_execution",
        "child_exit_code": child_exit_code,
        "sim_preflight_status": "unknown",
        "network_call_made": None,
        "model_event_count": None,
        "mcp_event_count": None,
        "saxo_event_count": None,
        "execution_performed": None,
        "broker_write_made": None,
        "live_mutation_calls": None,
        "purchase_occurred": None,
        "disclaimer_response_made": None,
        "child_cleanup_status": "unknown",
        "child_remaining_process_count": None,
        "child_remaining_process_group_count": None,
        "reason": (
            "proof_bootstrap_producer_nonzero" if state == "failed" else "proof_bootstrap_complete"
        ),
        "redacted_publication": True,
    }
    return CodexNativeBootstrapEnvelope.model_validate(
        {
            **material,
            "envelope_sha256": hashlib.sha256(
                json.dumps(
                    material,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode(),
            ).hexdigest(),
        },
    )


def _bootstrap_verification(
    *,
    contract_sha256: str = CONTRACT_SHA256,
) -> CodexNativeBootstrapVerification:
    return CodexNativeBootstrapVerification(
        status="authenticated",
        envelope=_bootstrap_envelope(contract_sha256=contract_sha256),
    )


def _progress() -> CodexNativeProofProgress:
    return CodexNativeProofProgress(
        candidate_commit=CANDIDATE,
        installed_cache_sha256=CACHE_SHA256,
        producer_module_sha256=MODULE_SHA256,
        catalog_sha256=CATALOG_SHA256,
        contract_sha256=CONTRACT_SHA256,
    )


def _passed_preflight() -> CodexNativeSimPreflightReceipt:
    return CodexNativeSimPreflightReceipt(
        status="passed",
        requested_environment="SIM",
        effective_read_environment="SIM",
        live_reads=False,
        live_writes=False,
        capabilities_status="passed",
        reason=None,
        http_status=200,
        network_call_made=True,
        session_capabilities_proven=True,
    )


def _failed_agent_evaluation_report() -> EvalRunReport:
    records = (
        EvalRunRecord(
            case_id="options",
            harness="codex",
            status="passed",
            execution_mode="model_execution",
            expected_skill="saxo-analytics",
            required_logical_tools=("saxo_model_derivatives",),
            forbidden_logical_tools=(),
            resolved_tool_grants=("mcp__saxo_bank_mcp__saxo_model_derivatives",),
            transcript_assertions_passed=True,
            no_model_call=False,
            no_mcp_call=False,
            no_saxo_call=False,
            error="",
            model_tool_event_count=1,
            model_command_event_count=0,
            model_mcp_event_count=1,
            model_saxo_event_count=1,
            client_version="PRIVATE_CLIENT_SENTINEL",
            invoked_logical_tools=("saxo_model_derivatives",),
            invoked_logical_tool_count=1,
            grant_status="passed",
            assertion_status="passed",
        ),
        EvalRunRecord(
            case_id="cost-xray",
            harness="codex",
            status="failed",
            execution_mode="model_execution",
            expected_skill="saxo-analytics",
            required_logical_tools=(
                "saxo_get_research_dataset",
                "saxo_analyze_portfolio",
                "saxo_export_analysis",
            ),
            forbidden_logical_tools=(),
            resolved_tool_grants=(
                "mcp__saxo_bank_mcp__saxo_get_research_dataset",
                "mcp__saxo_bank_mcp__saxo_analyze_portfolio",
                "mcp__saxo_bank_mcp__saxo_export_analysis",
            ),
            transcript_assertions_passed=False,
            no_model_call=False,
            no_mcp_call=False,
            no_saxo_call=False,
            error="required_tool_missing",
            model_tool_event_count=1,
            model_command_event_count=0,
            model_mcp_event_count=1,
            model_saxo_event_count=1,
            client_version="PRIVATE_CLIENT_SENTINEL",
            invoked_logical_tools=("saxo_get_research_dataset",),
            invoked_logical_tool_count=1,
            grant_status="passed",
            assertion_status="failed",
            assistant_message_present=True,
            required_all_assertion_results=(True, False, True),
            required_any_assertion_results=(True,),
            forbidden_assertion_absent_results=(True, True),
            raw_assistant_event_count=2,
            raw_assistant_events_sha256="a" * 64,
            final_assistant_text_sha256="b" * 64,
            raw_assistant_message_present=True,
            raw_assistant_required_all_assertion_results=(True, True, True),
            raw_assistant_required_any_assertion_results=(True,),
            raw_assistant_forbidden_assertion_absent_results=(True, True),
        ),
    )
    return EvalRunReport(
        status="failed",
        harness="codex",
        environment="LOCAL+SIM",
        execution_mode="model_execution",
        selected_case_count=len(records),
        case_count=len(records),
        records=records,
        cleanup={
            "complete": True,
            "process_cleanup": "passed",
            "runtime_cleanup": "passed",
            "token_promote": "passed",
            "created_processes": 4,
            "terminated_processes": 2,
            "remaining_processes": 0,
            "process_timed_out": False,
            "raw_transcripts_persisted": 0,
        },
        before_global_state={"private_marker": "DO_NOT_COPY"},
        after_global_state={"private_marker": "DO_NOT_COPY"},
        global_state_unchanged=True,
        skipped_count=0,
        nonzero_on_skip=True,
        source_commit=CANDIDATE,
    )


def _write_failed_agent_report(path: Path) -> str:
    payload = _failed_agent_evaluation_report().model_dump(mode="json")
    failed_record = cast("list[dict[str, Any]]", payload["records"])[1]
    failed_record["plugin_list_exit_code"] = 0
    failed_record["plugin_list_stdout_schema_sha256"] = "e" * 64
    failed_record["mcp_probe_stage"] = "command_exit"
    failed_record["mcp_probe_exit_code"] = FAILED_MCP_PROBE_EXIT_CODE
    failed_record["mcp_probe_stdout_schema_sha256"] = "f" * 64
    payload["run_cleanup"] = {"complete": True, "remaining_processes": 0}
    payload["installation_fixture_preserved"] = True
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    path.write_text(encoded, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _progress_with_failed_agent_summary(tmp_path: Path) -> CodexNativeProofProgress:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    report_path = tmp_path / "failed-eval.json"
    _write_failed_agent_report(report_path)
    producer._record_failed_agent_report_progress(  # noqa: SLF001
        progress,
        report_path=report_path,
        candidate_commit=CANDIDATE,
    )
    return progress


def _failure(
    progress: CodexNativeProofProgress,
    *,
    reason: str = "proof_injected_failure",
) -> CodexNativeChildFailureEnvelope:
    return build_child_failure_envelope(
        progress,
        child_exit_code=1,
        reason=reason,
    )


def _consumption_evidence() -> SimpleNamespace:
    return SimpleNamespace(
        intent=SimpleNamespace(intent_sha256="a" * 64),
        cleanup=SimpleNamespace(cleanup_receipt_sha256="b" * 64),
    )


def _verify(raw: str, **overrides: object) -> CodexNativeVerifiedChildFailure:
    values: dict[str, object] = {
        "raw_stdout": raw,
        "candidate_commit": CANDIDATE,
        "installed_cache_sha256": CACHE_SHA256,
        "producer_module_sha256": MODULE_SHA256,
        "catalog_sha256": CATALOG_SHA256,
        "contract_sha256": CONTRACT_SHA256,
        "child_exit_code": 1,
        "command_timed_out": False,
        "command_cleanup_attempted": True,
        "command_stdout_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "command_stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "remaining_process_count": 0,
        "remaining_process_group_count": 0,
        "cleanup_identity_evidence_status": "no-target-observed",
        "runtime_cleanup_status": "complete",
        "bootstrap_verification": _bootstrap_verification(),
    }
    values.update(overrides)
    return verify_child_failure_envelope(**cast("Any", values))


def _load_proof_matrix_script() -> ModuleType:
    path = Path("scripts/run_analytics_proof_matrix.py").resolve()
    spec = spec_from_file_location("test_run_analytics_proof_matrix", path)
    if spec is None or spec.loader is None:
        raise AssertionError("proof matrix script could not be loaded")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_candidate_entrypoint_binding(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    candidate_root: Path,
) -> None:
    def load_binding(_path: Path, *, candidate_commit: str) -> tuple[str, str]:
        assert candidate_commit == CANDIDATE
        return CANDIDATE, "9" * 40

    def validate_source(
        source_root: Path,
        *,
        candidate_commit: str,
        candidate_tree: str,
    ) -> Path:
        assert source_root == candidate_root
        assert candidate_commit == CANDIDATE
        assert candidate_tree == "9" * 40
        return candidate_root

    def require_entrypoint(source_root: Path) -> None:
        assert source_root == candidate_root

    monkeypatch.setattr(script, "_load_candidate_launch_binding", load_binding)
    monkeypatch.setattr(script, "validate_codex_native_candidate_source_root", validate_source)
    monkeypatch.setattr(script, "_require_candidate_entrypoint_binding", require_entrypoint)


def _candidate_publication(
    script: ModuleType,
    *,
    result_kind: Literal["boundary_failure", "verified_child_failure", "verified_result"],
    candidate_commit: str = CANDIDATE,
    contract_sha256: str | None = None,
) -> CodexNativeProofPublication:
    publication = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    catalog = script.load_analysis_kind_catalog(script.ANALYSIS_KIND_CATALOG_PATH)
    contracts = script.build_proof_execution_contracts(catalog=catalog)
    actual_contract_sha256 = contract_sha256 or script._digest(  # noqa: SLF001
        [contract.model_dump(mode="json") for contract in contracts]
    )
    if result_kind == "boundary_failure":
        result = publication.build_codex_native_boundary_failure(
            candidate_commit=candidate_commit,
            reason="proof_inner_failure_retained",
        )
    elif result_kind == "verified_child_failure":
        progress = CodexNativeProofProgress(
            candidate_commit=candidate_commit,
            installed_cache_sha256=CACHE_SHA256,
            producer_module_sha256=MODULE_SHA256,
            catalog_sha256=CATALOG_SHA256,
            contract_sha256=actual_contract_sha256,
        )
        child = _failure(progress, reason="proof_inner_failure_retained")
        result = _verify(
            child.model_dump_json(),
            candidate_commit=candidate_commit,
            contract_sha256=actual_contract_sha256,
            bootstrap_verification=_bootstrap_verification(
                contract_sha256=actual_contract_sha256,
            ),
        )
    else:
        bootstrap = _bootstrap_envelope(
            child_exit_code=0,
            state="complete",
            contract_sha256=actual_contract_sha256,
        )
        result = producer.CodexNativeVerifiedInstalledProofValidation(
            status="validated",
            candidate_commit=candidate_commit,
            installed_cache_sha256=CACHE_SHA256,
            catalog_sha256=CATALOG_SHA256,
            contract_sha256=actual_contract_sha256,
            producer_authenticated=True,
            execution_performed=True,
            process_local_activation=True,
            executed_receipt_count=len(catalog.evidence_receipt_ids),
            proof_execution_sha256="a" * 64,
            bundle_sha256="b" * 64,
            validation_errors=(),
            sim_preflight=_passed_preflight(),
            network_call_made=True,
            bootstrap_authenticated=True,
            bootstrap_envelope=bootstrap,
        )
    return publication.build_codex_native_proof_publication(
        candidate_commit=candidate_commit,
        analysis_kind_count=len(contracts),
        evidence_receipt_count=len(catalog.evidence_receipt_ids),
        contract_sha256=actual_contract_sha256,
        result_kind=result_kind,
        result=result,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed",
        "tampered",
        "candidate",
        "contract",
        "policy",
        "analysis_count",
        "receipt_count",
        "mode",
    ],
)
def test_candidate_result_authentication_rejects_untrusted_output(
    tmp_path: Path,
    mutation: str,
) -> None:
    script = _load_proof_matrix_script()
    catalog = script.load_analysis_kind_catalog(script.ANALYSIS_KIND_CATALOG_PATH)
    contracts = script.build_proof_execution_contracts(catalog=catalog)
    contract_sha256 = script._digest(  # noqa: SLF001
        [contract.model_dump(mode="json") for contract in contracts],
    )
    candidate_commit = "2" * 40 if mutation == "candidate" else CANDIDATE
    candidate_contract = "3" * 64 if mutation == "contract" else contract_sha256
    publication = _candidate_publication(
        script,
        result_kind="boundary_failure",
        candidate_commit=candidate_commit,
        contract_sha256=candidate_contract,
    )
    payload = publication.model_dump(mode="json")
    if mutation == "tampered":
        payload["publication_sha256"] = "f" * 64
    elif mutation == "policy":
        payload["harness_policy"] = "dual_v1"
        payload["publication_sha256"] = script._digest(  # noqa: SLF001
            {key: value for key, value in payload.items() if key != "publication_sha256"},
        )
    raw = "{" if mutation == "malformed" else json.dumps(payload, sort_keys=True)
    inner_result = tmp_path / "proof.json.candidate-result.json"
    inner_result.write_text(raw, encoding="utf-8")
    inner_result.chmod(0o644 if mutation == "mode" else 0o600)

    authenticated = script._load_authenticated_candidate_result(  # noqa: SLF001
        inner_result,
        candidate_commit=CANDIDATE,
        contract_sha256=contract_sha256,
        analysis_kind_count=(
            len(contracts) + 1 if mutation == "analysis_count" else len(contracts)
        ),
        evidence_receipt_count=(
            len(catalog.evidence_receipt_ids) + 1
            if mutation == "receipt_count"
            else len(catalog.evidence_receipt_ids)
        ),
    )

    assert authenticated is None


@pytest.mark.parametrize("result_kind", ["verified_result", "verified_child_failure"])
def test_candidate_runner_cleanup_failure_preserves_authenticated_inner_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result_kind: Literal["verified_result", "verified_child_failure"],
) -> None:
    script = _load_proof_matrix_script()
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(mode=0o700)
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)
    inner_publication = _candidate_publication(script, result_kind=result_kind)
    output = tmp_path / "proof.json"

    def fail_after_result(  # noqa: PLR0913
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        cleanup_identity_receipt_path: Path | None = None,
    ) -> NoReturn:
        del env, timeout_seconds, cleanup_identity_receipt_path
        result_path = Path(argv[argv.index("--out") + 1])
        assert result_path == output.with_name(f"{output.name}.candidate-result.json")
        assert result_path != output
        result_path.write_text(inner_publication.model_dump_json(), encoding="utf-8")
        result_path.chmod(0o600)
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=17,
                pgid=17,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=1,
            remaining_process_group_count=1,
        )

    monkeypatch.setattr(script, "run_command", fail_after_result)

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    outer = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    retained_path = output.with_name(f"{output.name}.candidate-result.json")
    retained = publication_module.verify_codex_native_proof_publication(
        retained_path.read_text(encoding="utf-8"),
    )
    receipt_path = output.with_name(f"{output.name}.candidate-runner.json")
    receipt = publication_module.verify_codex_native_candidate_runner_receipt(
        receipt_path.read_text(encoding="utf-8"),
    )
    retained_sha256 = hashlib.sha256(retained_path.read_bytes()).hexdigest()

    assert exit_code == 1
    assert outer.result.reason == "proof_candidate_runner_cleanup_failed"
    assert outer.result.candidate_runner_cleanup_status == "failed"
    assert outer.result.candidate_runner_result_status == "authenticated"
    assert retained == inner_publication
    assert receipt.cleanup_status == "failed"
    assert receipt.result_sha256 == retained_sha256
    assert outer.result.candidate_runner_result_sha256 == retained_sha256
    assert outer.result.execution_performed is None
    assert outer.result.network_call_made is None
    assert retained_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert receipt_path.stat().st_mode & 0o777 == OWNER_FILE_MODE


def test_candidate_runner_unknown_cleanup_propagates_typed_digest_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_proof_matrix_script()
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(mode=0o700)
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)
    inner_publication = _candidate_publication(script, result_kind="verified_child_failure")
    output = tmp_path / "proof.json"
    cleanup_digest = "d" * 64
    try:
        command_error = CommandFailureError(
            CommandReceipt(
                name="analytics_candidate_runner",
                argv=("candidate",),
                cwd=str(candidate_root),
                pid=17,
                pgid=17,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=None,
            remaining_process_group_count=None,
            cleanup_identity_evidence_status="observation-unknown",
            cleanup_identity_receipt_sha256=cleanup_digest,
            cleanup_unknown_reason="watcher_drain_unknown",
        )
    except TypeError as error:
        pytest.fail(f"typed candidate cleanup evidence is not accepted: {type(error).__name__}")

    def fail_after_result(  # noqa: PLR0913
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        cleanup_identity_receipt_path: Path | None = None,
    ) -> NoReturn:
        del name, cwd, env, timeout_seconds, cleanup_identity_receipt_path
        result_path = Path(argv[argv.index("--out") + 1])
        result_path.write_text(inner_publication.model_dump_json(), encoding="utf-8")
        result_path.chmod(0o600)
        raise command_error

    monkeypatch.setattr(script, "run_command", fail_after_result)

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    outer = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    receipt_path = output.with_name(f"{output.name}.candidate-runner.json")
    receipt = publication_module.verify_codex_native_candidate_runner_receipt(
        receipt_path.read_text(encoding="utf-8"),
    )

    assert exit_code == 1
    assert outer.result.reason == "proof_candidate_runner_cleanup_failed"
    assert outer.result.candidate_runner_cleanup_status == "unknown"
    assert outer.result.candidate_runner_cleanup_evidence_status == "observation-unknown"
    assert outer.result.candidate_runner_cleanup_receipt_sha256 == cleanup_digest
    assert outer.result.candidate_runner_cleanup_unknown_reason == "watcher_drain_unknown"
    assert receipt.cleanup_status == "unknown"
    assert receipt.cleanup_identity_evidence_status == "observation-unknown"
    assert receipt.cleanup_identity_receipt_sha256 == cleanup_digest
    assert receipt.cleanup_unknown_reason == "watcher_drain_unknown"
    assert outer.result.execution_performed is None
    assert outer.result.model_event_count is None
    assert outer.result.mcp_event_count is None
    assert outer.result.saxo_event_count is None
    assert outer.result.broker_write_made is None
    assert outer.result.live_mutation_calls is None
    assert outer.result.purchase_occurred is None
    assert outer.result.disclaimer_response_made is None


@pytest.mark.parametrize("mutation", ["malformed", "tampered", "binding"])
def test_candidate_runner_cleanup_failure_copies_no_untrusted_inner_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    script = _load_proof_matrix_script()
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(mode=0o700)
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)
    wrong_candidate = "2" * 40 if mutation == "binding" else CANDIDATE
    untrusted = _candidate_publication(
        script,
        result_kind="boundary_failure",
        candidate_commit=wrong_candidate,
    ).model_dump(mode="json")
    if mutation == "tampered":
        untrusted["publication_sha256"] = "f" * 64
    raw = "{" if mutation == "malformed" else json.dumps(untrusted, sort_keys=True)
    output = tmp_path / "proof.json"

    def fail_after_result(  # noqa: PLR0913
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        cleanup_identity_receipt_path: Path | None = None,
    ) -> NoReturn:
        del env, timeout_seconds, cleanup_identity_receipt_path
        result_path = Path(argv[argv.index("--out") + 1])
        result_path.write_text(raw, encoding="utf-8")
        result_path.chmod(0o600)
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=17,
                pgid=17,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=1,
            remaining_process_group_count=1,
        )

    monkeypatch.setattr(script, "run_command", fail_after_result)

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    outer = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    retained_path = output.with_name(f"{output.name}.candidate-result.json")
    receipt_path = output.with_name(f"{output.name}.candidate-runner.json")
    receipt = publication_module.verify_codex_native_candidate_runner_receipt(
        receipt_path.read_text(encoding="utf-8"),
    )

    assert exit_code == 1
    assert outer.result.reason == "proof_candidate_runner_cleanup_failed"
    assert outer.result.candidate_runner_result_status == "unknown"
    assert outer.result.candidate_runner_result_sha256 is None
    assert outer.result.execution_performed is None
    assert outer.result.network_call_made is None
    assert receipt.result_present is False
    assert receipt.result_sha256 is None
    assert not retained_path.exists()


def test_candidate_runner_receipt_write_failure_retains_authenticated_inner_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_proof_matrix_script()
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(mode=0o700)
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)
    inner_publication = _candidate_publication(
        script,
        result_kind="verified_child_failure",
    )
    output = tmp_path / "proof.json"

    def child_result(  # noqa: PLR0913
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        cleanup_identity_receipt_path: Path | None = None,
    ) -> CommandResult:
        del env, timeout_seconds, cleanup_identity_receipt_path
        result_path = Path(argv[argv.index("--out") + 1])
        result_path.write_text(inner_publication.model_dump_json(), encoding="utf-8")
        result_path.chmod(0o600)
        return CommandResult(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=17,
                pgid=17,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout="",
            stderr="",
        )

    real_writer = script.write_codex_native_candidate_runner_receipt
    write_count = 0

    def fail_exit_receipt(
        path: Path,
        receipt: CodexNativeCandidateRunnerReceipt,
    ) -> bool:
        nonlocal write_count
        write_count += 1
        return real_writer(path, receipt) if write_count == 1 else False

    monkeypatch.setattr(script, "run_command", child_result)
    monkeypatch.setattr(
        script,
        "write_codex_native_candidate_runner_receipt",
        fail_exit_receipt,
    )

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    outer = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    retained_path = output.with_name(f"{output.name}.candidate-result.json")
    retained_sha256 = hashlib.sha256(retained_path.read_bytes()).hexdigest()

    assert exit_code == 1
    assert outer.result.reason == "proof_candidate_runner_receipt_write_failed"
    assert outer.result.candidate_runner_receipt_sha256 is None
    assert outer.result.candidate_runner_result_status == "authenticated"
    assert outer.result.candidate_runner_result_sha256 == retained_sha256
    assert outer.result.execution_performed is None
    assert retained_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert write_count == EXPECTED_RECEIPT_WRITE_COUNT


def test_candidate_runner_cleanup_complete_publishes_authenticated_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_proof_matrix_script()
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(mode=0o700)
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)
    inner_publication = _candidate_publication(script, result_kind="verified_result")
    output = tmp_path / "proof.json"

    def child_result(  # noqa: PLR0913
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        cleanup_identity_receipt_path: Path | None = None,
    ) -> CommandResult:
        del env, timeout_seconds, cleanup_identity_receipt_path
        result_path = Path(argv[argv.index("--out") + 1])
        result_path.write_text(inner_publication.model_dump_json(), encoding="utf-8")
        result_path.chmod(0o600)
        return CommandResult(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=17,
                pgid=17,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(script, "run_command", child_result)

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    published = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    receipt_path = output.with_name(f"{output.name}.candidate-runner.json")
    receipt = publication_module.verify_codex_native_candidate_runner_receipt(
        receipt_path.read_text(encoding="utf-8"),
    )

    assert exit_code == 0
    assert published == inner_publication
    assert receipt.cleanup_status == "complete"
    assert (
        receipt.result_sha256
        == hashlib.sha256(
            output.with_name(f"{output.name}.candidate-result.json").read_bytes()
        ).hexdigest()
    )


def test_failure_before_preflight_proves_only_no_execution() -> None:
    envelope = _failure(_progress(), reason="proof_before_preflight_injected")

    assert envelope.current_phase == "before_preflight"
    assert envelope.completed_phases == ()
    assert envelope.sim_preflight_status == "not_started"
    assert envelope.execution_performed is False
    assert envelope.network_call_made is False
    assert envelope.model_event_count == 0
    assert envelope.mcp_event_count == 0
    assert envelope.saxo_event_count == 0
    assert envelope.broker_write_made is False
    assert envelope.live_mutation_calls == 0
    assert envelope.purchase_occurred is False
    assert envelope.disclaimer_response_made is False


def test_failure_after_preflight_retains_network_provenance() -> None:
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")

    envelope = _failure(progress, reason="proof_after_preflight_injected")

    assert envelope.current_phase == "sim_preflight"
    assert envelope.completed_phases == ("sim_preflight",)
    assert envelope.sim_preflight_status == "passed"
    assert envelope.network_call_made is True
    assert envelope.execution_performed is True
    assert envelope.model_event_count == 0
    assert envelope.mcp_event_count == 1
    assert envelope.saxo_event_count is None


def test_failure_after_model_and_mcp_activity_retains_exact_counts() -> None:
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    progress.record_agent_activity(
        model_event_count=AGENT_MODEL_EVENT_COUNT,
        mcp_event_count=3,
        saxo_event_count=1,
    )

    envelope = _failure(progress, reason="proof_agent_evaluation_injected")

    assert envelope.current_phase == "agent_evaluation"
    assert envelope.model_event_count == AGENT_MODEL_EVENT_COUNT
    assert envelope.mcp_event_count == AGENT_TOTAL_MCP_EVENT_COUNT
    assert envelope.saxo_event_count is None
    assert envelope.broker_write_made is None
    assert envelope.live_mutation_calls is None
    assert envelope.purchase_occurred is None
    assert envelope.disclaimer_response_made is None


def test_failed_eval_report_survives_temp_cleanup_as_strict_summary(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    codex_home = tmp_path / "codex-home"
    source_repo = tmp_path / "source-repo"
    codex_home.mkdir(mode=0o700)
    source_repo.mkdir(mode=0o700)
    monkeypatch.setenv("SAXO_ANALYTICS_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("SAXO_ANALYTICS_SOURCE_REPO", str(source_repo))
    report_parent: Path | None = None
    expected_report_sha256 = ""

    def fail_after_report(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NoReturn:
        nonlocal report_parent, expected_report_sha256
        del env, timeout_seconds
        report_path = Path(argv[argv.index("--out") + 1])
        report_parent = report_path.parent
        expected_report_sha256 = _write_failed_agent_report(report_path)
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=17,
                pgid=17,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout="",
            stderr="",
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "run_command", fail_after_report)
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_agent_evaluation_command_failed",
    ):
        producer._run_installed_agent_evaluation(  # noqa: SLF001
            candidate_commit=CANDIDATE,
            installed_cache_sha256=CACHE_SHA256,
            harness_policy="codex_native_v1",
            progress=progress,
        )

    summary = getattr(progress, "agent_evaluation_failure_summary", None)
    assert report_parent is not None
    assert not report_parent.exists()
    assert summary is not None
    assert summary.report_sha256 == expected_report_sha256
    assert summary.report_status == "failed"
    assert summary.case_count == FAILED_EVAL_CASE_COUNT
    assert summary.failed_case_count == 1
    assert tuple(item.case_id for item in summary.cases) == ("options", "cost-xray")
    failed = summary.cases[1]
    assert failed.error == "required_tool_missing"
    assert failed.model_output_observability == "observable"
    assert failed.no_mcp_call is False
    assert failed.no_saxo_call is False
    assert failed.required_logical_tool_count == FAILED_EVAL_REQUIRED_TOOL_COUNT
    assert failed.invoked_logical_tool_count == 1
    assert failed.required_logical_tool_ids == (
        "saxo_get_research_dataset",
        "saxo_analyze_portfolio",
        "saxo_export_analysis",
    )
    assert failed.invoked_logical_tool_ids == ("saxo_get_research_dataset",)
    assert failed.plugin_list_exit_code == 0
    assert failed.plugin_list_stdout_schema_sha256 == "e" * 64
    assert failed.mcp_probe_stage == "command_exit"
    assert failed.mcp_probe_exit_code == FAILED_MCP_PROBE_EXIT_CODE
    assert failed.mcp_probe_stdout_schema_sha256 == "f" * 64
    assert failed.assistant_message_present is True
    assert failed.required_all_assertion_results == (True, False, True)
    assert failed.required_any_assertion_results == (True,)
    assert failed.forbidden_assertion_absent_results == (True, True)
    assert failed.raw_assistant_event_count == RAW_ASSISTANT_EVENT_COUNT
    assert failed.raw_assistant_events_sha256 == "a" * 64
    assert failed.final_assistant_text_sha256 == "b" * 64
    assert failed.raw_assistant_message_present is True
    assert failed.raw_assistant_required_all_assertion_results == (True, True, True)
    assert failed.raw_assistant_required_any_assertion_results == (True,)
    assert failed.raw_assistant_forbidden_assertion_absent_results == (True, True)
    assert set(failed.model_dump(mode="json")) == {
        "case_id",
        "status",
        "error",
        "assertion_status",
        "grant_status",
        "model_output_observability",
        "no_mcp_call",
        "no_saxo_call",
        "required_logical_tool_ids",
        "required_logical_tool_count",
        "invoked_logical_tool_ids",
        "invoked_logical_tool_count",
        "model_tool_event_count",
        "model_command_event_count",
        "model_mcp_event_count",
        "model_saxo_event_count",
        "plugin_list_exit_code",
        "plugin_list_stdout_schema_sha256",
        "mcp_probe_stage",
        "mcp_probe_exit_code",
        "mcp_probe_stdout_schema_sha256",
        "mcp_config_sha256",
        "mcp_config_path_identity_sha256",
        "assistant_message_present",
        "required_all_assertion_results",
        "required_any_assertion_results",
        "forbidden_assertion_absent_results",
        "raw_assistant_event_count",
        "raw_assistant_events_sha256",
        "final_assistant_text_sha256",
        "raw_assistant_message_present",
        "raw_assistant_required_all_assertion_results",
        "raw_assistant_required_any_assertion_results",
        "raw_assistant_forbidden_assertion_absent_results",
    }
    assert summary.cleanup.status == "complete"
    assert summary.cleanup.process_cleanup == "passed"
    assert summary.cleanup.runtime_cleanup == "passed"
    assert summary.cleanup.token_promote == "passed"  # noqa: S105
    assert summary.cleanup.created_process_count == EVAL_CREATED_PROCESS_COUNT
    assert summary.cleanup.terminated_process_count == EVAL_TERMINATED_PROCESS_COUNT
    assert summary.cleanup.remaining_process_count == 0
    assert summary.cleanup.process_timed_out is False
    assert summary.cleanup.persisted_raw_output_count == 0
    assert set(summary.model_dump(mode="json")) == {
        "schema_version",
        "receipt_kind",
        "report_sha256",
        "report_status",
        "case_count",
        "failed_case_count",
        "cases",
        "cleanup",
        "summary_sha256",
    }
    rendered = summary.model_dump_json()
    assert "PRIVATE_CLIENT_SENTINEL" not in rendered
    assert "DO_NOT_COPY" not in rendered
    assert "transcript" not in rendered
    assert "stderr" not in rendered
    assert "/" not in rendered


def test_mixed_malformed_eval_evidence_stays_unknown_through_publication() -> None:  # noqa: PLR0915
    """One decoded tool before malformed JSON cannot prove calls, tools, or aggregate counts."""
    case = next(
        item for item in load_eval_cases(Path("evals/saxo-analytics")) if item.id == "scenario"
    )
    grants = resolve_tool_grants("codex", case.exact_tool_grants["codex"])
    private_sentinel = "private-transcript-sentinel"
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "tool-before-malformed",
                        "type": "mcp_tool_call",
                        "server": "saxo_bank_mcp",
                        "tool": "saxo_run_scenario",
                    },
                },
            ),
            f"not-json-{private_sentinel}",
        ),
    )
    result = ManagedProcessResult(
        stdout=stream,
        stderr="",
        returncode=0,
        timed_out=False,
        created_processes=1,
        terminated_processes=1,
        remaining_processes=0,
        process_cleanup="passed",
    )
    execution = import_module("saxo_bank_mcp.agent_skill_eval_execution")
    record = execution._record_from_stdout(  # noqa: SLF001
        case,
        "codex",
        grants,
        result,
    )
    assert isinstance(record, EvalRunRecord)
    assert record.model_output_observability == "unknown"
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_tool_event_count is None
    assert record.model_command_event_count is None
    assert record.model_mcp_event_count is None
    assert record.model_saxo_event_count is None
    assert record.invoked_logical_tools is None
    assert record.invoked_logical_tool_count is None

    report = EvalRunReport(
        status="failed",
        harness="codex",
        environment="LOCAL",
        execution_mode="model_execution",
        selected_case_count=1,
        case_count=1,
        records=(record,),
        cleanup={
            "complete": True,
            "process_cleanup": "passed",
            "runtime_cleanup": "passed",
            "token_promote": "not_required",
            "created_processes": 1,
            "terminated_processes": 1,
            "remaining_processes": 0,
            "process_timed_out": False,
            "raw_transcripts_persisted": 0,
        },
        before_global_state={},
        after_global_state={},
        global_state_unchanged=True,
        skipped_count=0,
        nonzero_on_skip=True,
        source_commit=CANDIDATE,
    )
    report_bytes = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    summary = producer._agent_evaluation_failure_summary(  # noqa: SLF001
        report_bytes=report_bytes,
        report=report,
    )
    assert summary is not None
    case_summary = summary.cases[0]
    assert case_summary.model_output_observability == "unknown"
    assert case_summary.no_mcp_call is None
    assert case_summary.no_saxo_call is None
    assert case_summary.invoked_logical_tool_ids is None
    assert case_summary.invoked_logical_tool_count is None
    assert case_summary.model_mcp_event_count is None
    assert case_summary.model_saxo_event_count is None

    inconsistent = case_summary.model_dump(mode="json")
    inconsistent["no_mcp_call"] = False
    with pytest.raises(ValidationError):
        CodexNativeAgentEvaluationCaseSummary.model_validate(inconsistent, strict=True)

    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    producer._record_agent_report_progress(progress, report)  # noqa: SLF001
    progress.record_agent_evaluation_failure(summary)
    verified = _verify(
        _failure(
            progress,
            reason="installed_agent_evaluation_command_failed",
        ).model_dump_json(),
    )
    assert verified.failure_evidence_status == "authenticated"
    assert verified.mcp_event_count is None
    assert verified.saxo_event_count is None
    assert verified.agent_evaluation_failure_summary == summary

    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    published = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )
    assert published == publication
    rendered = publication.model_dump_json()
    assert private_sentinel not in rendered
    assert "tool-before-malformed" not in rendered


def test_cleanup_unknown_eval_evidence_stays_unknown_through_publication() -> None:  # noqa: PLR0915
    """An unparsed post-launch cleanup refusal stays unknown in every signed layer."""
    case = next(
        item for item in load_eval_cases(Path("evals/saxo-analytics")) if item.id == "scenario"
    )
    grants = resolve_tool_grants("codex", case.exact_tool_grants["codex"])
    private_sentinel = "private-cleanup-unknown-output"
    result = ManagedProcessResult(
        stdout=private_sentinel,
        stderr="",
        returncode=0,
        timed_out=False,
        created_processes=1,
        terminated_processes=0,
        remaining_processes=None,
        process_cleanup="unknown",
    )
    execution = import_module("saxo_bank_mcp.agent_skill_eval_execution")
    record = execution._record_from_process(  # noqa: SLF001
        case,
        "codex",
        grants,
        result,
    )
    assert isinstance(record, EvalRunRecord)
    assert record.transcript_assertions_passed is None
    assert record.model_output_observability == "unknown"
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_tool_event_count is None
    assert record.model_command_event_count is None
    assert record.model_mcp_event_count is None
    assert record.model_saxo_event_count is None
    assert record.invoked_logical_tools is None
    assert record.invoked_logical_tool_count is None
    assert record.grant_status == "unknown"
    assert record.assertion_status == "unknown"

    report = EvalRunReport(
        status="failed",
        harness="codex",
        environment="LOCAL",
        execution_mode="model_execution",
        selected_case_count=1,
        case_count=1,
        records=(record,),
        cleanup={
            "complete": False,
            "process_cleanup": "unknown",
            "runtime_cleanup": "passed",
            "token_promote": "not_required",
            "created_processes": 1,
            "terminated_processes": 0,
            "remaining_processes": None,
            "process_timed_out": False,
            "raw_transcripts_persisted": 0,
        },
        before_global_state={},
        after_global_state={},
        global_state_unchanged=True,
        skipped_count=0,
        nonzero_on_skip=True,
        source_commit=CANDIDATE,
    )
    report_bytes = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    summary = producer._agent_evaluation_failure_summary(  # noqa: SLF001
        report_bytes=report_bytes,
        report=report,
    )
    assert summary is not None
    case_summary = summary.cases[0]
    assert case_summary.model_output_observability == "unknown"
    assert case_summary.no_mcp_call is None
    assert case_summary.no_saxo_call is None
    assert case_summary.invoked_logical_tool_ids is None
    assert case_summary.invoked_logical_tool_count is None
    assert case_summary.model_tool_event_count is None
    assert case_summary.model_command_event_count is None
    assert case_summary.model_mcp_event_count is None
    assert case_summary.model_saxo_event_count is None
    assert case_summary.grant_status == "unknown"
    assert case_summary.assertion_status == "unknown"
    assert summary.cleanup.status == "unknown"
    assert summary.cleanup.remaining_process_count is None

    inconsistent = case_summary.model_dump(mode="json")
    inconsistent["model_mcp_event_count"] = 0
    with pytest.raises(ValidationError):
        CodexNativeAgentEvaluationCaseSummary.model_validate(inconsistent, strict=True)
    inconsistent = case_summary.model_dump(mode="json")
    inconsistent["assistant_message_present"] = False
    with pytest.raises(ValidationError):
        CodexNativeAgentEvaluationCaseSummary.model_validate(inconsistent, strict=True)

    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    producer._record_agent_report_progress(progress, report)  # noqa: SLF001
    progress.record_agent_evaluation_failure(summary)
    verified = _verify(
        _failure(
            progress,
            reason="installed_agent_evaluation_command_failed",
        ).model_dump_json(),
    )
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    published = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )

    assert published.result.agent_evaluation_failure_summary == summary
    # The process launch itself is known. Only parse-derived event/call facts are unknown.
    assert published.result.model_event_count == 1
    assert published.result.mcp_event_count is None
    assert published.result.saxo_event_count is None
    assert published.result.broker_write_made is None
    assert published.result.live_mutation_calls is None
    assert published.result.purchase_occurred is None
    assert published.result.disclaimer_response_made is None
    assert private_sentinel not in publication.model_dump_json()


def test_router_post_spawn_failure_stays_unknown_through_publication(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A router Popen followed by PGID failure keeps signed facts unknown end to end."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    case = next(
        item for item in load_eval_cases(Path("evals/saxo-bank")) if item.id == "router-auth"
    )
    private_sentinel = "private-router-post-spawn-output"

    class FakeProcess:
        pid = 73_300
        returncode: int | None = None

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    def fail_getpgid(_pid: int) -> int:
        raise ProcessLookupError("injected post-spawn failure")

    monkeypatch.setattr(eval_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(eval_process.os, "getpgid", fail_getpgid)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    manager = EvalProcessManager()
    record = execute_router_model_case(
        case,
        "codex",
        (),
        RouterCaseContext(
            plugin_root=Path.cwd(),
            homes=RouterHomes(),
            expected_router_source_sha256=None,
        ),
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(runtime_root),
            "TMPDIR": str(runtime_root),
        },
        process_manager=manager,
    )
    cleanup = manager.finalize()

    assert record.error == "process_lookup_error"
    assert record.model_output_observability == "unknown"
    assert record.router_decision is None
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_tool_event_count is None
    assert record.model_command_event_count is None
    assert record.model_mcp_event_count is None
    assert record.model_saxo_event_count is None
    assert record.invoked_logical_tools is None
    assert record.invoked_logical_tool_count is None
    assert cleanup["created_processes"] == 1
    assert cleanup["remaining_processes"] is None
    assert cleanup["process_cleanup"] == "unknown"
    assert private_sentinel not in record.model_dump_json()

    injected_record = record.model_dump(mode="json")
    assert case.router_expectation is not None
    injected_record["router_decision"] = {
        **case.router_expectation.model_dump(mode="json"),
        "execution_allowed": False,
    }
    with pytest.raises(ValidationError):
        EvalRunRecord.model_validate(injected_record, strict=True)

    report = EvalRunReport(
        status="failed",
        harness="codex",
        environment="LOCAL",
        execution_mode="model_execution",
        selected_case_count=1,
        case_count=1,
        records=(record,),
        cleanup={
            "complete": False,
            "process_cleanup": "unknown",
            "runtime_cleanup": "passed",
            "token_promote": "not_required",
            "created_processes": 1,
            "terminated_processes": 0,
            "remaining_processes": None,
            "process_timed_out": False,
            "raw_transcripts_persisted": 0,
        },
        before_global_state={},
        after_global_state={},
        global_state_unchanged=True,
        skipped_count=0,
        nonzero_on_skip=True,
        source_commit=CANDIDATE,
    )
    report_bytes = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    summary = producer._agent_evaluation_failure_summary(  # noqa: SLF001
        report_bytes=report_bytes,
        report=report,
    )
    assert summary is not None
    case_summary = summary.cases[0]
    assert case_summary.model_output_observability == "unknown"
    assert case_summary.no_mcp_call is None
    assert case_summary.no_saxo_call is None
    assert case_summary.invoked_logical_tool_ids is None
    assert case_summary.invoked_logical_tool_count is None
    assert case_summary.model_mcp_event_count is None
    assert case_summary.model_saxo_event_count is None

    injected_summary = case_summary.model_dump(mode="json")
    injected_summary["model_mcp_event_count"] = 0
    with pytest.raises(ValidationError):
        CodexNativeAgentEvaluationCaseSummary.model_validate(injected_summary, strict=True)

    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    producer._record_agent_report_progress(progress, report)  # noqa: SLF001
    progress.record_agent_evaluation_failure(summary)
    verified = _verify(
        _failure(
            progress,
            reason="installed_agent_evaluation_command_failed",
        ).model_dump_json(),
    )
    assert verified.mcp_event_count is None
    assert verified.saxo_event_count is None
    assert verified.agent_evaluation_failure_summary == summary

    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    published = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )
    assert published == publication
    assert published.result.mcp_event_count is None
    assert published.result.saxo_event_count is None
    assert private_sentinel not in publication.model_dump_json()


def test_failed_eval_summary_rejects_diagnostic_tamper_extra_and_cleanup_tamper(
    tmp_path: Path,
) -> None:
    progress = _progress_with_failed_agent_summary(tmp_path)
    summary = progress.agent_evaluation_failure_summary
    assert summary is not None

    vector_tamper = summary.model_dump(mode="json")
    cast("list[dict[str, Any]]", vector_tamper["cases"])[1][
        "raw_assistant_required_all_assertion_results"
    ] = [False, False, False]
    with pytest.raises(ValidationError):
        type(summary).model_validate(vector_tamper, strict=True)

    extra_content = summary.model_dump(mode="json")
    cast("list[dict[str, Any]]", extra_content["cases"])[1]["raw_assistant_text"] = (
        "PRIVATE_TRANSCRIPT_DO_NOT_PUBLISH"
    )
    with pytest.raises(ValidationError):
        type(summary).model_validate(extra_content, strict=True)

    cleanup_tamper = summary.model_dump(mode="json")
    cast("dict[str, Any]", cleanup_tamper["cleanup"])["remaining_process_count"] = 1
    with pytest.raises(ValidationError):
        type(summary).model_validate(cleanup_tamper, strict=True)


def test_unknown_nested_eval_cleanup_keeps_nullable_count_through_publication() -> None:
    """Authenticated nested cleanup uncertainty stays nullable and fail closed end to end."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    report = _failed_agent_evaluation_report().model_copy(
        update={
            "cleanup": {
                "complete": False,
                "process_cleanup": "unknown",
                "runtime_cleanup": "passed",
                "token_promote": "passed",
                "created_processes": EVAL_CREATED_PROCESS_COUNT,
                "terminated_processes": EVAL_TERMINATED_PROCESS_COUNT,
                "remaining_processes": None,
                "process_timed_out": False,
                "raw_transcripts_persisted": 0,
            },
        },
    )
    report_bytes = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    summary = producer._agent_evaluation_failure_summary(  # noqa: SLF001
        report_bytes=report_bytes,
        report=report,
    )

    assert summary is not None
    assert summary.cleanup is not None
    assert summary.cleanup.status == "unknown"
    assert summary.cleanup.process_cleanup == "unknown"
    assert summary.cleanup.created_process_count == EVAL_CREATED_PROCESS_COUNT
    assert summary.cleanup.terminated_process_count == EVAL_TERMINATED_PROCESS_COUNT
    assert summary.cleanup.remaining_process_count is None

    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    producer._record_agent_report_progress(progress, report)  # noqa: SLF001
    progress.record_agent_evaluation_failure(summary)
    verified = _verify(
        _failure(
            progress,
            reason="installed_agent_evaluation_command_failed",
        ).model_dump_json(),
    )
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    parsed = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )

    assert parsed.result.agent_evaluation_failure_summary == summary
    assert parsed.result.model_event_count == AGENT_MODEL_EVENT_COUNT
    assert parsed.result.mcp_event_count == CLI_TOTAL_MCP_EVENT_COUNT
    assert parsed.result.saxo_event_count is None
    assert parsed.result.broker_write_made is None
    assert parsed.result.live_mutation_calls is None
    assert parsed.result.purchase_occurred is None
    assert parsed.result.disclaimer_response_made is None
    assert "PRIVATE_CLIENT_SENTINEL" not in publication.model_dump_json()

    tampered = summary.model_dump(mode="json")
    cast("dict[str, Any]", tampered["cleanup"])["remaining_process_count"] = 0
    with pytest.raises(ValidationError):
        CodexNativeAgentEvaluationFailureSummary.model_validate(tampered, strict=True)


def test_outer_cleanup_identity_receipt_digest_is_authenticated_and_public_only_as_digest(
    tmp_path: Path,
) -> None:
    progress = _progress_with_failed_agent_summary(tmp_path)
    child = _failure(progress, reason="installed_agent_evaluation_command_failed")
    cleanup_digest = "c" * 64

    verified = _verify(
        child.model_dump_json(),
        cleanup_identity_evidence_status="authenticated",
        cleanup_identity_receipt_sha256=cleanup_digest,
    )

    assert verified.outer_process_cleanup_evidence_status == "authenticated"
    assert verified.outer_process_cleanup_receipt_sha256 == cleanup_digest
    rendered = verified.model_dump_json()
    assert "pid" not in rendered.lower()
    assert "birth" not in rendered.lower()
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    round_trip = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )
    assert round_trip.result.outer_process_cleanup_evidence_status == "authenticated"
    assert round_trip.result.outer_process_cleanup_receipt_sha256 == cleanup_digest

    tampered = publication.model_dump(mode="json")
    cast("dict[str, Any]", tampered["result"])["outer_process_cleanup_evidence_status"] = (
        "observation-unknown"
    )
    with pytest.raises(ValidationError):
        publication_module.verify_codex_native_proof_publication(json.dumps(tampered))


def test_outer_unknown_cleanup_receipt_reason_is_authenticated_and_fail_closed(
    tmp_path: Path,
) -> None:
    child = _failure(_progress_with_failed_agent_summary(tmp_path))
    cleanup_digest = "d" * 64
    try:
        verified = _verify(
            child.model_dump_json(),
            cleanup_identity_evidence_status="observation-unknown",
            cleanup_identity_receipt_sha256=cleanup_digest,
            cleanup_identity_unknown_reason="watcher_drain_unknown",
            remaining_process_count=None,
            remaining_process_group_count=None,
        )
    except TypeError as error:
        pytest.fail(f"typed unknown cleanup evidence is not accepted: {type(error).__name__}")

    assert verified.failure_evidence_status == "cleanup_failed"
    assert verified.reason == "proof_child_cleanup_failed"
    assert verified.outer_process_cleanup_evidence_status == "observation-unknown"
    assert verified.outer_process_cleanup_receipt_sha256 == cleanup_digest
    assert verified.outer_process_cleanup_unknown_reason == "watcher_drain_unknown"
    assert verified.model_event_count is None
    assert verified.mcp_event_count is None
    assert verified.saxo_event_count is None
    assert verified.broker_write_made is None
    assert verified.live_mutation_calls is None
    assert verified.purchase_occurred is None
    assert verified.disclaimer_response_made is None

    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    round_trip = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )
    assert round_trip.result.outer_process_cleanup_receipt_sha256 == cleanup_digest
    assert round_trip.result.outer_process_cleanup_unknown_reason == "watcher_drain_unknown"
    rendered = round_trip.model_dump_json()
    assert "PRIVATE" not in rendered
    assert "pid" not in rendered.lower()
    assert "birth" not in rendered.lower()

    tampered = publication.model_dump(mode="json")
    cast("dict[str, Any]", tampered["result"])["outer_process_cleanup_unknown_reason"] = (
        "watcher_still_running"
    )
    with pytest.raises(ValidationError):
        publication_module.verify_codex_native_proof_publication(json.dumps(tampered))


@pytest.mark.parametrize(
    ("status", "digest"),
    [
        ("authenticated", None),
        ("no-target-observed", "c" * 64),
        ("observation-unknown", "c" * 64),
        ("write-failed", "c" * 64),
    ],
)
def test_outer_cleanup_inconsistent_optional_digest_is_normalized_fail_closed(
    tmp_path: Path,
    status: str,
    digest: str | None,
) -> None:
    child = _failure(_progress_with_failed_agent_summary(tmp_path))

    verified = _verify(
        child.model_dump_json(),
        cleanup_identity_evidence_status=status,
        cleanup_identity_receipt_sha256=digest,
    )

    assert verified.failure_evidence_status == "cleanup_failed"
    assert verified.reason == "proof_child_cleanup_failed"
    assert verified.outer_process_cleanup_evidence_status == "write-failed"
    assert verified.outer_process_cleanup_receipt_sha256 is None
    assert verified.outer_process_cleanup_unknown_reason == "cleanup_evidence_inconsistent"
    assert verified.outer_remaining_process_count is None
    assert verified.outer_remaining_process_group_count is None


def test_outer_cleanup_missing_receipt_fails_closed_and_is_privacy_safe(tmp_path: Path) -> None:
    child = _failure(_progress_with_failed_agent_summary(tmp_path))
    verified = _verify(
        child.model_dump_json(),
        cleanup_identity_evidence_status="write-failed",
        cleanup_identity_receipt_sha256=None,
        cleanup_identity_unknown_reason="cleanup_receipt_write_failed",
    )
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    round_trip = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )

    assert round_trip.result.failure_evidence_status == "cleanup_failed"
    assert round_trip.result.reason == "proof_child_cleanup_failed"
    assert round_trip.result.outer_process_cleanup_evidence_status == "write-failed"
    assert round_trip.result.outer_process_cleanup_receipt_sha256 is None
    assert round_trip.result.outer_process_cleanup_unknown_reason == "cleanup_receipt_write_failed"
    assert round_trip.result.outer_remaining_process_count is None
    assert round_trip.result.outer_remaining_process_group_count is None
    assert round_trip.result.model_event_count is None
    assert round_trip.result.mcp_event_count is None
    assert round_trip.result.saxo_event_count is None
    rendered = round_trip.model_dump_json()
    assert "PRIVATE" not in rendered
    assert "pid" not in rendered.lower()
    assert "birth" not in rendered.lower()


@pytest.mark.parametrize(
    ("status", "digest", "remaining_process_count", "remaining_group_count"),
    [
        ("observation-unknown", None, 0, 0),
        ("write-failed", None, 0, 0),
        ("authenticated", None, 0, 0),
        ("no-target-observed", "c" * 64, 0, 0),
        ("authenticated", "c" * 64, None, 0),
        ("authenticated", "c" * 64, 0, None),
    ],
)
def test_outer_cleanup_untrusted_or_inconsistent_evidence_fails_closed(
    tmp_path: Path,
    status: str,
    digest: str | None,
    remaining_process_count: int | None,
    remaining_group_count: int | None,
) -> None:
    child = _failure(_progress_with_failed_agent_summary(tmp_path))

    verified = _verify(
        child.model_dump_json(),
        cleanup_identity_evidence_status=status,
        cleanup_identity_receipt_sha256=digest,
        remaining_process_count=remaining_process_count,
        remaining_process_group_count=remaining_group_count,
    )

    assert verified.failure_evidence_status == "cleanup_failed"
    assert verified.reason == "proof_child_cleanup_failed"
    assert verified.model_event_count is None
    assert verified.mcp_event_count is None
    assert verified.saxo_event_count is None


def test_failed_eval_summary_is_authenticated_through_publication(tmp_path: Path) -> None:
    progress = _progress_with_failed_agent_summary(tmp_path)
    summary = getattr(progress, "agent_evaluation_failure_summary", None)
    assert summary is not None
    child = _failure(progress, reason="installed_agent_evaluation_command_failed")

    verified = _verify(child.model_dump_json())
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    parsed = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )

    assert child.agent_evaluation_failure_summary == summary
    assert verified.agent_evaluation_failure_summary == summary
    assert parsed.result.agent_evaluation_failure_summary == summary
    assert parsed.result.broker_write_made is None
    assert parsed.result.live_mutation_calls is None
    assert parsed.result.purchase_occurred is None
    assert parsed.result.disclaimer_response_made is None


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (OSError("private operating-system detail"), "os_error"),
        (PermissionError("private permission detail"), "permission_error"),
        (FileNotFoundError("private path detail"), "file_not_found_error"),
        (ProcessLookupError("private process detail"), "process_lookup_error"),
        (ValueError("private value detail"), "value_error"),
        (KeyError("private key detail"), "key_error"),
        (RuntimeError("private runtime detail"), "model_execution_error"),
    ],
)
def test_exception_reason_is_allowlisted_and_signed_through_publication(
    failure: Exception,
    expected_reason: str,
) -> None:
    """Caught exception types survive every signed layer without private detail."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    case = next(
        item for item in load_eval_cases(Path("evals/saxo-bank")) if item.id == "router-auth"
    )
    record = unobservable_model_failure_record(case, "codex", (), failure)
    assert record.error == expected_reason
    assert record.error in {
        "os_error",
        "permission_error",
        "file_not_found_error",
        "process_lookup_error",
        "value_error",
        "key_error",
        "model_execution_error",
    }
    report = EvalRunReport(
        status="failed",
        harness="codex",
        environment="LOCAL",
        execution_mode="model_execution",
        selected_case_count=1,
        case_count=1,
        records=(record,),
        cleanup={
            "complete": True,
            "process_cleanup": "passed",
            "runtime_cleanup": "passed",
            "token_promote": "passed",
            "created_processes": 1,
            "terminated_processes": 1,
            "remaining_processes": 0,
            "process_timed_out": False,
            "raw_transcripts_persisted": 0,
        },
        before_global_state={},
        after_global_state={},
        global_state_unchanged=True,
        skipped_count=0,
        nonzero_on_skip=True,
        source_commit=CANDIDATE,
    )
    report_bytes = json.dumps(
        report.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    summary = producer._agent_evaluation_failure_summary(  # noqa: SLF001
        report_bytes=report_bytes,
        report=report,
    )
    assert summary is not None
    assert summary.cases[0].error == expected_reason
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    progress.record_agent_evaluation_failure(summary)
    child = _failure(progress, reason="installed_agent_evaluation_command_failed")
    verified = _verify(child.model_dump_json())
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    parsed = publication_module.verify_codex_native_proof_publication(
        publication.model_dump_json(),
    )

    assert child.agent_evaluation_failure_summary is not None
    assert child.agent_evaluation_failure_summary.cases[0].error == expected_reason
    assert verified.agent_evaluation_failure_summary is not None
    assert verified.agent_evaluation_failure_summary.cases[0].error == expected_reason
    assert parsed.result.agent_evaluation_failure_summary is not None
    assert parsed.result.agent_evaluation_failure_summary.cases[0].error == expected_reason
    rendered = publication.model_dump_json()
    assert "private " not in rendered
    assert parsed.result.broker_write_made is None
    assert parsed.result.live_mutation_calls is None
    assert parsed.result.purchase_occurred is None
    assert parsed.result.disclaimer_response_made is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"mcp_probe_exit_code": None},
        {"mcp_probe_stdout_schema_sha256": None},
        {"mcp_probe_stage": "command_start"},
        {
            "mcp_probe_stage": "complete",
            "mcp_probe_exit_code": FAILED_MCP_PROBE_EXIT_CODE,
        },
        {"mcp_probe_stdout_schema_sha256": "invalid"},
        {"raw_stderr": "DO_NOT_PUBLISH"},
        {"required_all_assertion_results": [True, "DO_NOT_PUBLISH"]},
        {"assistant_message_present": "yes"},
    ],
)
def test_failed_eval_case_summary_rejects_incomplete_tampered_or_extra_mcp_probe_evidence(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    progress = _progress_with_failed_agent_summary(tmp_path)
    summary = getattr(progress, "agent_evaluation_failure_summary", None)
    assert summary is not None
    payload = summary.cases[1].model_dump(mode="json")
    payload.update(mutation)

    with pytest.raises(ValidationError):
        CodexNativeAgentEvaluationCaseSummary.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("error", "transcript_assertion_failed"),
        ("model_output_observability", "unknown"),
        ("no_mcp_call", True),
        ("invoked_logical_tool_ids", None),
        ("plugin_list_exit_code", 99),
        ("plugin_list_stdout_schema_sha256", "f" * 64),
        ("mcp_probe_stage", "payload_parse"),
        ("mcp_probe_exit_code", 99),
        ("mcp_probe_stdout_schema_sha256", "a" * 64),
        ("raw_transcript", "DO_NOT_PUBLISH"),
    ],
)
def test_publication_rejects_tampered_or_extra_eval_failure_material(
    tmp_path: Path,
    mutation: str,
    value: object,
) -> None:
    progress = _progress_with_failed_agent_summary(tmp_path)
    child = _failure(progress, reason="installed_agent_evaluation_command_failed")
    verified = _verify(child.model_dump_json())
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=54,
        evidence_receipt_count=54,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    payload = publication.model_dump(mode="json")
    result = cast("dict[str, Any]", payload["result"])
    summary = cast("dict[str, Any]", result["agent_evaluation_failure_summary"])
    cases = cast("list[dict[str, Any]]", summary["cases"])
    cases[1][mutation] = value

    with pytest.raises(ValidationError):
        publication_module.verify_codex_native_proof_publication(json.dumps(payload))


def test_tampered_eval_failure_summary_publishes_unknown(tmp_path: Path) -> None:
    progress = _progress_with_failed_agent_summary(tmp_path)
    child = _failure(progress, reason="installed_agent_evaluation_command_failed")
    payload = child.model_dump(mode="json")
    summary = cast("dict[str, Any]", payload["agent_evaluation_failure_summary"])
    cases = cast("list[dict[str, Any]]", summary["cases"])
    cases[1]["error"] = "transcript_assertion_failed"
    raw = json.dumps(payload)

    verified = _verify(
        raw,
        command_stdout_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )

    assert verified.failure_evidence_status == "malformed"
    assert verified.agent_evaluation_failure_summary is None
    assert verified.model_event_count is None
    assert verified.mcp_event_count is None
    assert verified.broker_write_made is None
    assert verified.purchase_occurred is None


def test_legacy_child_without_eval_summary_remains_authenticated() -> None:
    child = _failure(_progress(), reason="proof_before_preflight_injected")
    payload = child.model_dump(mode="json")
    payload.pop("agent_evaluation_failure_summary")
    material = {key: value for key, value in payload.items() if key != "envelope_sha256"}
    payload["envelope_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    raw = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)

    verified = _verify(raw)

    assert verified.failure_evidence_status == "authenticated"
    assert verified.agent_evaluation_failure_summary is None


@pytest.mark.parametrize(
    ("phase", "completed"),
    [
        ("offline_proof", ("sim_preflight", "agent_evaluation")),
        (
            "sim_matrix",
            ("sim_preflight", "agent_evaluation", "offline_proof"),
        ),
        (
            "bundle_validation",
            ("sim_preflight", "agent_evaluation", "offline_proof", "sim_matrix"),
        ),
        (
            "cleanup",
            (
                "sim_preflight",
                "agent_evaluation",
                "offline_proof",
                "sim_matrix",
                "bundle_validation",
            ),
        ),
    ],
)
def test_failure_envelope_preserves_injected_phase(
    phase: CodexNativeProofPhase,
    completed: tuple[CodexNativeProofPhase, ...],
) -> None:
    progress = _progress()
    for item in completed:
        progress.begin_phase(item)
        if item == "sim_preflight":
            progress.record_preflight(_passed_preflight())
        if item == "agent_evaluation":
            progress.record_agent_activity(
                model_event_count=1,
                mcp_event_count=0,
                saxo_event_count=0,
            )
        progress.complete_phase(item)
    progress.begin_phase(phase)

    envelope = _failure(progress, reason=f"proof_{phase}_injected")

    assert envelope.current_phase == phase
    assert envelope.completed_phases == completed
    assert envelope.execution_performed is True


def test_authenticated_child_failure_survives_parent_verification() -> None:
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    progress.record_agent_activity(
        model_event_count=1,
        mcp_event_count=0,
        saxo_event_count=0,
    )
    progress.complete_phase("agent_evaluation")
    progress.begin_phase("offline_proof")
    child = _failure(progress, reason="proof_offline_proof_failed")

    verified = _verify(child.model_dump_json())

    assert verified.failure_evidence_status == "authenticated"
    assert verified.producer_authenticated is True
    assert verified.current_phase == "offline_proof"
    assert verified.network_call_made is True
    assert verified.execution_performed is True
    assert verified.reason == "proof_offline_proof_failed"
    assert verified.outer_runtime_cleanup_status == "complete"
    assert verified.outer_remaining_process_count == 0


@pytest.mark.parametrize(
    ("raw", "expected_status"),
    [
        ("", "missing"),
        ("{", "malformed"),
        (json.dumps({"schema_version": "1"}), "malformed"),
    ],
)
def test_missing_or_malformed_envelope_publishes_unknown(
    raw: str,
    expected_status: str,
) -> None:
    verified = _verify(raw)

    assert verified.failure_evidence_status == expected_status
    assert verified.producer_authenticated is False
    assert verified.current_phase is None
    assert verified.sim_preflight_status == "unknown"
    assert verified.execution_performed is None
    assert verified.network_call_made is None
    assert verified.model_event_count is None
    assert verified.mcp_event_count is None
    assert verified.saxo_event_count is None
    assert verified.broker_write_made is None
    assert verified.live_mutation_calls is None
    assert verified.purchase_occurred is None
    assert verified.disclaimer_response_made is None


def test_tampered_envelope_publishes_unknown() -> None:
    child = _failure(_progress())
    payload = child.model_dump(mode="json")
    payload["reason"] = "tampered_reason"
    raw = json.dumps(payload)

    verified = _verify(raw)

    assert verified.failure_evidence_status == "tampered"
    assert verified.producer_authenticated is False
    assert verified.execution_performed is None
    assert verified.network_call_made is None
    assert verified.broker_write_made is None


def test_child_crash_without_envelope_publishes_unknown() -> None:
    verified = _verify(
        "",
        child_exit_code=CRASH_EXIT_CODE,
        command_timed_out=False,
    )

    assert verified.failure_evidence_status == "crashed"
    assert verified.child_exit_code == CRASH_EXIT_CODE
    assert verified.execution_performed is None
    assert verified.network_call_made is None


def test_outer_cleanup_failure_invalidates_negative_outcomes() -> None:
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    progress.record_agent_activity(
        model_event_count=1,
        mcp_event_count=1,
        saxo_event_count=1,
    )
    child = _failure(progress)

    verified = _verify(
        child.model_dump_json(),
        runtime_cleanup_status="failed",
        remaining_process_count=1,
        remaining_process_group_count=1,
    )

    assert verified.failure_evidence_status == "cleanup_failed"
    assert verified.producer_authenticated is True
    assert verified.current_phase == "agent_evaluation"
    assert verified.execution_performed is True
    assert verified.network_call_made is True
    assert verified.model_event_count is None
    assert verified.mcp_event_count is None
    assert verified.saxo_event_count is None
    assert verified.broker_write_made is None
    assert verified.live_mutation_calls is None
    assert verified.outer_remaining_process_count == 1


def test_failure_schema_has_no_sensitive_or_raw_output_surface() -> None:
    child = _failure(_progress())
    verified = _verify(child.model_dump_json())
    rendered = json.dumps(verified.model_dump(mode="json"), sort_keys=True).lower()

    for forbidden in (
        "access_token",
        "refresh_token",
        "account_id",
        "client_key",
        "analysis_id",
        "authorization_url",
        "callback_query",
        "raw_stdout",
        "raw_stderr",
        "broker_payload",
    ):
        assert forbidden not in rendered


def test_arbitrary_safe_looking_reason_is_redacted() -> None:
    sensitive_looking_reason = "owneraccountreference123456"

    child = _failure(_progress(), reason=sensitive_looking_reason)

    assert child.reason == "proof_child_failed"
    assert sensitive_looking_reason not in child.model_dump_json()


def test_nonzero_command_retains_ephemeral_output_and_cleanup_counts(tmp_path: Path) -> None:
    stdout = '{"receipt_kind":"test_failure"}'

    with pytest.raises(CommandFailureError) as caught:
        run_command(
            "failing_child",
            (
                sys.executable,
                "-c",
                "import os, sys; print(os.environ['PROBE_VALUE'], end=''); sys.exit(7)",
            ),
            cwd=tmp_path,
            env={
                "PATH": str(Path(sys.executable).parent),
                "PROBE_VALUE": stdout,
            },
            timeout_seconds=10,
        )

    error = caught.value
    assert error.receipt.exit_code == FAILED_CHILD_EXIT_CODE
    assert error.stdout == stdout
    assert error.stderr == ""
    assert error.remaining_process_count == 0
    assert error.remaining_process_group_count == 0
    assert stdout not in repr(error.receipt)
    assert stdout not in repr(error)


def test_native_cli_failure_after_preflight_emits_typed_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    def fail_after_preflight(**kwargs: object) -> NoReturn:
        progress = kwargs["progress"]
        assert isinstance(progress, CodexNativeProofProgress)
        progress.begin_phase("agent_evaluation")
        progress.record_agent_activity(
            model_event_count=1,
            mcp_event_count=2,
            saxo_event_count=1,
        )
        raise producer.ProofProducerError("proof_agent_evaluation_injected")

    monkeypatch.setattr(producer, "_run_codex_native_sim_preflight", _passed_preflight)
    monkeypatch.setattr(producer, "_execute_installed_proof_bundle", fail_after_preflight)

    exit_code = producer.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--installed-cache-sha256",
            CACHE_SHA256,
            "--harness-policy",
            "codex_native_v1",
        ],
    )

    envelope = import_module(
        "saxo_bank_mcp.qa_analytics_proof_failure",
    ).CodexNativeChildFailureEnvelope.model_validate_json(capsys.readouterr().out)
    assert exit_code == 1
    assert envelope.current_phase == "agent_evaluation"
    assert envelope.completed_phases == ("sim_preflight",)
    assert envelope.network_call_made is True
    assert envelope.model_event_count == 1
    assert envelope.mcp_event_count == CLI_TOTAL_MCP_EVENT_COUNT
    assert envelope.execution_performed is True
    assert envelope.broker_write_made is None


def test_native_cli_failure_before_preflight_emits_no_execution_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    calls = 0

    def fail_after_progress_binding() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CATALOG_SHA256, CONTRACT_SHA256
        raise producer.ProofProducerError("proof_before_preflight_injected")

    monkeypatch.setattr(producer, "_installed_contract_digests", fail_after_progress_binding)

    exit_code = producer.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--installed-cache-sha256",
            CACHE_SHA256,
            "--harness-policy",
            "codex_native_v1",
        ],
    )

    envelope = CodexNativeChildFailureEnvelope.model_validate_json(capsys.readouterr().out)
    assert exit_code == 1
    assert calls == EXPECTED_BINDING_CALLS
    assert envelope.current_phase == "before_preflight"
    assert envelope.completed_phases == ()
    assert envelope.execution_performed is False
    assert envelope.network_call_made is False
    assert envelope.model_event_count == 0
    assert envelope.mcp_event_count == 0
    assert envelope.saxo_event_count == 0


def test_native_wrapper_authenticates_nonzero_child_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    progress = _progress()
    progress.begin_phase("sim_preflight")
    progress.record_preflight(_passed_preflight())
    progress.complete_phase("sim_preflight")
    progress.begin_phase("agent_evaluation")
    child = _failure(progress, reason="proof_agent_evaluation_command_failed")
    command = ("proof-child",)
    cache_root = tmp_path / "cache"
    source_repo = tmp_path / "source"
    retained_codex_home = tmp_path / "retained-codex"
    install_report = tmp_path / "install.json"
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(0o600)
    install = SimpleNamespace(
        proof_runtime=SimpleNamespace(
            binding_path=tmp_path / "proof-runtime-binding.json",
            binding=SimpleNamespace(
                interpreter=tmp_path / "proof-runtime/bin/python",
                binding_sha256=RUNTIME_BINDING_SHA256,
            ),
        ),
    )
    for path in (cache_root, source_repo, retained_codex_home):
        path.mkdir(mode=0o700)

    def prepare(runtime_root: Path, **_kwargs: object) -> SimpleNamespace:
        codex_home = runtime_root / "codex-home"
        codex_home.mkdir(mode=0o700)
        return SimpleNamespace(
            env={"PATH": "/usr/bin:/bin"},
            codex_home=codex_home,
            run_root=runtime_root,
        )

    def fail_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NoReturn:
        del env, timeout_seconds
        stdout = child.model_dump_json()
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=11,
                pgid=11,
                exit_code=1,
                stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout=stdout,
            stderr="",
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "prepare_eval_isolated_runtime", prepare)
    monkeypatch.setattr(producer, "run_command", fail_command)

    def native_command(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return command

    def bootstrap_verifier(
        *_args: object,
        **_kwargs: object,
    ) -> CodexNativeBootstrapVerification:
        return _bootstrap_verification()

    monkeypatch.setattr(producer, "_codex_native_producer_command", native_command)
    monkeypatch.setattr(
        producer,
        "verify_bootstrap_envelope_file",
        bootstrap_verifier,
    )

    def no_op(_value: object) -> None:
        return

    monkeypatch.setattr(producer, "promote_rotated_sim_token_cache", no_op)
    monkeypatch.setattr(producer, "require_matrix_runtime_cleanup", no_op)
    cleanup_calls: list[object] = []

    def cleanup_proof_runtime(value: object) -> SimpleNamespace:
        cleanup_calls.append(value)
        return _consumption_evidence()

    monkeypatch.setattr(producer, "cleanup_codex_proof_runtime", cleanup_proof_runtime)
    monkeypatch.setattr(
        producer,
        "_installed_contract_digests",
        lambda: (CATALOG_SHA256, CONTRACT_SHA256),
    )

    with pytest.raises(producer.CodexNativeProofFailureError) as caught:
        producer._execute_codex_native_installed_child(  # noqa: SLF001
            cache_root,
            source_repo=source_repo,
            retained_codex_home=retained_codex_home,
            install=install,
            install_report_path=install_report,
            install_report_sha256=hashlib.sha256(install_report.read_bytes()).hexdigest(),
            candidate_commit=CANDIDATE,
            installed_cache_sha256=CACHE_SHA256,
            bootstrap_module_sha256="6" * 64,
            producer_module_sha256=MODULE_SHA256,
            catalog_sha256=CATALOG_SHA256,
            contract_sha256=CONTRACT_SHA256,
        )

    receipt = caught.value.receipt
    assert receipt.failure_evidence_status == "authenticated"
    assert receipt.current_phase == "agent_evaluation"
    assert receipt.network_call_made is True
    assert receipt.execution_performed is True
    assert receipt.outer_runtime_cleanup_status == "complete"
    assert cleanup_calls == [install]


def test_native_wrapper_cleans_retained_runtime_when_eval_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    install = SimpleNamespace(
        proof_runtime=SimpleNamespace(
            binding_path=tmp_path / "proof-runtime-binding.json",
            binding=SimpleNamespace(
                interpreter=tmp_path / "proof-runtime/bin/python",
                binding_sha256=RUNTIME_BINDING_SHA256,
            ),
        ),
    )
    cache_root = tmp_path / "cache"
    source_repo = tmp_path / "source"
    retained_codex_home = tmp_path / "retained-codex"
    install_report = tmp_path / "install.json"
    for directory in (cache_root, source_repo, retained_codex_home):
        directory.mkdir(mode=0o700)
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(0o600)

    def fail_prepare(*_args: object, **_kwargs: object) -> NoReturn:
        raise producer.MatrixEnvError("injected_prepare_failure")

    cleanup_calls: list[object] = []

    def cleanup_proof_runtime(value: object) -> SimpleNamespace:
        cleanup_calls.append(value)
        return _consumption_evidence()

    monkeypatch.setattr(producer, "prepare_eval_isolated_runtime", fail_prepare)
    monkeypatch.setattr(
        producer,
        "cleanup_codex_proof_runtime",
        cleanup_proof_runtime,
    )

    with pytest.raises(producer.ProofProducerError) as caught:
        producer._execute_codex_native_installed_child(  # noqa: SLF001
            cache_root,
            source_repo=source_repo,
            retained_codex_home=retained_codex_home,
            install=install,
            install_report_path=install_report,
            install_report_sha256=hashlib.sha256(install_report.read_bytes()).hexdigest(),
            candidate_commit=CANDIDATE,
            installed_cache_sha256=CACHE_SHA256,
            bootstrap_module_sha256="6" * 64,
            producer_module_sha256=MODULE_SHA256,
            catalog_sha256=CATALOG_SHA256,
            contract_sha256=CONTRACT_SHA256,
        )

    assert type(caught.value).__name__ == "CodexNativeBoundaryFailureError"
    assert caught.value.reason == "proof_sim_auth_lease_unavailable"
    assert caught.value.boundary_phase == "runtime_preparation"
    assert caught.value.command_state == "not_started"
    assert caught.value.cleanup_status == "complete"
    assert caught.value.runtime_consumption_intent_sha256 == "a" * 64
    assert caught.value.runtime_cleanup_receipt_sha256 == "b" * 64
    assert cleanup_calls == [install]


def test_native_wrapper_preserves_bootstrap_verification_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    cache_root = tmp_path / "cache"
    source_repo = tmp_path / "source"
    retained_codex_home = tmp_path / "retained-codex"
    install_report = tmp_path / "install.json"
    for directory in (cache_root, source_repo, retained_codex_home):
        directory.mkdir(mode=0o700)
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(0o600)
    install = SimpleNamespace(
        proof_runtime=SimpleNamespace(
            binding_path=tmp_path / "proof-runtime-binding.json",
            binding=SimpleNamespace(
                interpreter=tmp_path / "proof-runtime/bin/python",
                binding_sha256=RUNTIME_BINDING_SHA256,
            ),
        ),
    )
    command = ("proof-child",)

    def prepare(runtime_root: Path, **_kwargs: object) -> SimpleNamespace:
        codex_home = runtime_root / "codex-home"
        codex_home.mkdir(mode=0o700)
        return SimpleNamespace(
            env={"PATH": "/usr/bin:/bin"},
            codex_home=codex_home,
            run_root=runtime_root,
        )

    def pass_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> object:
        del env, timeout_seconds
        return producer.CommandResult(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=12,
                pgid=12,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"{}").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=False,
            ),
            stdout="{}",
            stderr="",
        )

    def native_command(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return command

    def missing_bootstrap(
        *_args: object,
        **_kwargs: object,
    ) -> CodexNativeBootstrapVerification:
        return CodexNativeBootstrapVerification(status="missing", envelope=None)

    def no_op(_value: object) -> None:
        return

    def consume_runtime(_value: object) -> SimpleNamespace:
        return _consumption_evidence()

    monkeypatch.setattr(producer, "prepare_eval_isolated_runtime", prepare)
    monkeypatch.setattr(producer, "run_command", pass_command)
    monkeypatch.setattr(
        producer,
        "_codex_native_producer_command",
        native_command,
    )
    monkeypatch.setattr(
        producer,
        "verify_bootstrap_envelope_file",
        missing_bootstrap,
    )
    monkeypatch.setattr(producer, "promote_rotated_sim_token_cache", no_op)
    monkeypatch.setattr(producer, "require_matrix_runtime_cleanup", no_op)
    monkeypatch.setattr(
        producer,
        "cleanup_codex_proof_runtime",
        consume_runtime,
    )

    with pytest.raises(producer.ProofProducerError) as caught:
        producer._execute_codex_native_installed_child(  # noqa: SLF001
            cache_root,
            source_repo=source_repo,
            retained_codex_home=retained_codex_home,
            install=install,
            install_report_path=install_report,
            install_report_sha256=hashlib.sha256(install_report.read_bytes()).hexdigest(),
            candidate_commit=CANDIDATE,
            installed_cache_sha256=CACHE_SHA256,
            bootstrap_module_sha256="6" * 64,
            producer_module_sha256=MODULE_SHA256,
            catalog_sha256=CATALOG_SHA256,
            contract_sha256=CONTRACT_SHA256,
        )

    assert type(caught.value).__name__ == "CodexNativeBoundaryFailureError"
    assert caught.value.reason == "proof_bootstrap_receipt_invalid"
    assert caught.value.boundary_phase == "bootstrap_verification"
    assert caught.value.command_state == "completed"
    assert caught.value.cleanup_status == "complete"


def test_native_wrapper_cleans_retained_runtime_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    cache_root = tmp_path / "cache"
    source_repo = tmp_path / "source"
    retained_codex_home = tmp_path / "retained-codex"
    install_report = tmp_path / "install.json"
    for directory in (cache_root, source_repo, retained_codex_home):
        directory.mkdir(mode=0o700)
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(0o600)
    install = SimpleNamespace(
        proof_runtime=SimpleNamespace(
            binding_path=tmp_path / "proof-runtime-binding.json",
            binding=SimpleNamespace(
                interpreter=tmp_path / "proof-runtime/bin/python",
                binding_sha256=RUNTIME_BINDING_SHA256,
            ),
        ),
    )
    command = ("proof-child",)

    def prepare(runtime_root: Path, **_kwargs: object) -> SimpleNamespace:
        codex_home = runtime_root / "codex-home"
        codex_home.mkdir(mode=0o700)
        return SimpleNamespace(
            env={"PATH": "/usr/bin:/bin"},
            codex_home=codex_home,
            run_root=runtime_root,
        )

    def pass_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> object:
        del env, timeout_seconds
        return producer.CommandResult(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=12,
                pgid=12,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"{}").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=False,
            ),
            stdout="{}",
            stderr="",
        )

    cleanup_calls: list[object] = []

    def native_command(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return command

    def bootstrap_verifier(
        *_args: object,
        **_kwargs: object,
    ) -> CodexNativeBootstrapVerification:
        return CodexNativeBootstrapVerification(
            status="authenticated",
            envelope=_bootstrap_envelope(child_exit_code=0, state="complete"),
        )

    def no_op(_value: object) -> None:
        return

    def cleanup_proof_runtime(value: object) -> SimpleNamespace:
        cleanup_calls.append(value)
        return _consumption_evidence()

    monkeypatch.setattr(producer, "prepare_eval_isolated_runtime", prepare)
    monkeypatch.setattr(producer, "run_command", pass_command)
    monkeypatch.setattr(
        producer,
        "_codex_native_producer_command",
        native_command,
    )
    monkeypatch.setattr(
        producer,
        "verify_bootstrap_envelope_file",
        bootstrap_verifier,
    )
    monkeypatch.setattr(producer, "promote_rotated_sim_token_cache", no_op)
    monkeypatch.setattr(producer, "require_matrix_runtime_cleanup", no_op)
    monkeypatch.setattr(
        producer,
        "cleanup_codex_proof_runtime",
        cleanup_proof_runtime,
    )

    execution = producer._execute_codex_native_installed_child(  # noqa: SLF001
        cache_root,
        source_repo=source_repo,
        retained_codex_home=retained_codex_home,
        install=install,
        install_report_path=install_report,
        install_report_sha256=hashlib.sha256(install_report.read_bytes()).hexdigest(),
        candidate_commit=CANDIDATE,
        installed_cache_sha256=CACHE_SHA256,
        bootstrap_module_sha256="6" * 64,
        producer_module_sha256=MODULE_SHA256,
        catalog_sha256=CATALOG_SHA256,
        contract_sha256=CONTRACT_SHA256,
    )

    assert execution.result.receipt.exit_code == 0
    assert cleanup_calls == [install]


def test_native_wrapper_cleans_retained_runtime_after_child_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    cache_root = tmp_path / "cache"
    source_repo = tmp_path / "source"
    retained_codex_home = tmp_path / "retained-codex"
    install_report = tmp_path / "install.json"
    for directory in (cache_root, source_repo, retained_codex_home):
        directory.mkdir(mode=0o700)
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(0o600)
    install = SimpleNamespace(
        proof_runtime=SimpleNamespace(
            binding_path=tmp_path / "proof-runtime-binding.json",
            binding=SimpleNamespace(
                interpreter=tmp_path / "proof-runtime/bin/python",
                binding_sha256=RUNTIME_BINDING_SHA256,
            ),
        ),
    )
    command = ("proof-child",)

    def prepare(runtime_root: Path, **_kwargs: object) -> SimpleNamespace:
        codex_home = runtime_root / "codex-home"
        codex_home.mkdir(mode=0o700)
        return SimpleNamespace(
            env={"PATH": "/usr/bin:/bin"},
            codex_home=codex_home,
            run_root=runtime_root,
        )

    def crash_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NoReturn:
        del env, timeout_seconds
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=13,
                pgid=13,
                exit_code=CRASH_EXIT_CODE,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout="",
            stderr="",
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    cleanup_calls: list[object] = []

    def native_command(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return command

    def bootstrap_verifier(
        *_args: object,
        **_kwargs: object,
    ) -> CodexNativeBootstrapVerification:
        return CodexNativeBootstrapVerification(
            status="authenticated",
            envelope=_bootstrap_envelope(child_exit_code=CRASH_EXIT_CODE),
        )

    def no_op(_value: object) -> None:
        return

    def cleanup_proof_runtime(value: object) -> SimpleNamespace:
        cleanup_calls.append(value)
        return _consumption_evidence()

    monkeypatch.setattr(producer, "prepare_eval_isolated_runtime", prepare)
    monkeypatch.setattr(producer, "run_command", crash_command)
    monkeypatch.setattr(
        producer,
        "_codex_native_producer_command",
        native_command,
    )
    monkeypatch.setattr(
        producer,
        "verify_bootstrap_envelope_file",
        bootstrap_verifier,
    )
    monkeypatch.setattr(producer, "promote_rotated_sim_token_cache", no_op)
    monkeypatch.setattr(producer, "require_matrix_runtime_cleanup", no_op)
    monkeypatch.setattr(
        producer,
        "cleanup_codex_proof_runtime",
        cleanup_proof_runtime,
    )

    with pytest.raises(producer.CodexNativeProofFailureError) as caught:
        producer._execute_codex_native_installed_child(  # noqa: SLF001
            cache_root,
            source_repo=source_repo,
            retained_codex_home=retained_codex_home,
            install=install,
            install_report_path=install_report,
            install_report_sha256=hashlib.sha256(install_report.read_bytes()).hexdigest(),
            candidate_commit=CANDIDATE,
            installed_cache_sha256=CACHE_SHA256,
            bootstrap_module_sha256="6" * 64,
            producer_module_sha256=MODULE_SHA256,
            catalog_sha256=CATALOG_SHA256,
            contract_sha256=CONTRACT_SHA256,
        )

    assert caught.value.receipt.failure_evidence_status == "crashed"
    assert caught.value.receipt.outer_runtime_cleanup_status == "complete"
    assert cleanup_calls == [install]


def test_proof_matrix_cli_publishes_typed_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_proof_matrix_script()
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    catalog = script.load_analysis_kind_catalog(script.ANALYSIS_KIND_CATALOG_PATH)
    contracts = script.build_proof_execution_contracts(catalog=catalog)
    actual_contract_sha256 = script._digest(  # noqa: SLF001
        [contract.model_dump(mode="json") for contract in contracts],
    )
    progress = CodexNativeProofProgress(
        candidate_commit=CANDIDATE,
        installed_cache_sha256=CACHE_SHA256,
        producer_module_sha256=MODULE_SHA256,
        catalog_sha256=CATALOG_SHA256,
        contract_sha256=actual_contract_sha256,
    )
    child = _failure(progress, reason="proof_before_preflight_injected")
    receipt = _verify(
        child.model_dump_json(),
        contract_sha256=actual_contract_sha256,
        bootstrap_verification=_bootstrap_verification(
            contract_sha256=actual_contract_sha256,
        ),
    )
    install = SimpleNamespace(candidate_commit=CANDIDATE)

    def load_install(*_args: object, **_kwargs: object) -> tuple[SimpleNamespace, tuple[str, ...]]:
        return install, ()

    monkeypatch.setattr(script, "load_verified_codex_install_report", load_install)
    candidate_root = tmp_path / "candidate"
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)

    def fail_producer(*_args: object, **_kwargs: object) -> NoReturn:
        raise producer.CodexNativeProofFailureError(receipt)

    monkeypatch.setattr(script, "run_verified_codex_native_producer", fail_producer)
    output = tmp_path / "proof.json"

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--candidate-root-bound",
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["receipt_kind"] == "codex_native_proof_publication"
    assert payload["result_kind"] == "verified_child_failure"
    assert payload["result"]["failure_evidence_status"] == "authenticated"
    assert payload["result"]["execution_performed"] is False
    assert payload["result"]["network_call_made"] is False
    assert payload["result"]["broker_write_made"] is False
    assert payload["result"]["reason"] == "proof_before_preflight_injected"
    assert "raw_stdout" not in payload
    assert "raw_stderr" not in payload


def test_proof_matrix_cli_publishes_typed_native_boundary_discriminators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _load_proof_matrix_script()
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    install = SimpleNamespace(candidate_commit=CANDIDATE)

    def load_install(*_args: object, **_kwargs: object) -> tuple[SimpleNamespace, tuple[str, ...]]:
        return install, ()

    monkeypatch.setattr(
        script,
        "load_verified_codex_install_report",
        load_install,
    )
    candidate_root = tmp_path / "candidate"
    _patch_candidate_entrypoint_binding(monkeypatch, script, candidate_root)

    def fail_boundary(*_args: object, **_kwargs: object) -> NoReturn:
        raise producer.CodexNativeBoundaryFailureError(
            reason="proof_bootstrap_receipt_invalid",
            boundary_phase="bootstrap_verification",
            command_state="completed",
            cleanup_status="complete",
            runtime_consumption_intent_sha256="a" * 64,
            runtime_cleanup_receipt_sha256="b" * 64,
        )

    monkeypatch.setattr(script, "run_verified_codex_native_producer", fail_boundary)
    output = tmp_path / "proof.json"

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
            "--candidate-source-root",
            str(candidate_root),
            "--candidate-root-bound",
            "--install-report",
            str(tmp_path / "install.json"),
            "--codex-global-home",
            str(tmp_path / "codex-home"),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ],
    )

    publication = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    assert exit_code == 1
    assert publication.result_kind == "boundary_failure"
    assert publication.result.reason == "proof_bootstrap_receipt_invalid"
    assert publication.result.boundary_phase == "bootstrap_verification"
    assert publication.result.command_state == "completed"
    assert publication.result.cleanup_status == "complete"
    assert publication.result.runtime_consumption_intent_sha256 == "a" * 64
    assert publication.result.runtime_cleanup_receipt_sha256 == "b" * 64
    assert publication.result.network_call_made is None
    assert publication.result.execution_performed is None


def test_native_boundary_error_sanitizes_untyped_reason() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    error = producer.CodexNativeBoundaryFailureError(
        reason="untyped_private_detail",
        boundary_phase="local_boundary",
        command_state="unknown",
        cleanup_status="unknown",
    )

    assert error.reason == "proof_producer_local_boundary_failed"


def test_native_boundary_failure_retains_consumption_receipts_when_other_cleanup_fails() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    error = producer._native_boundary_failure(  # noqa: SLF001
        reason="proof_sim_auth_lease_cleanup_failed",
        boundary_phase="isolated_runtime_cleanup",
        command_state="completed",
        consumption=cast("Any", _consumption_evidence()),
        cleanup_failed=True,
    )

    assert error.cleanup_status == "failed"
    assert error.runtime_consumption_intent_sha256 == "a" * 64
    assert error.runtime_cleanup_receipt_sha256 == "b" * 64


def test_bootstrap_file_verifier_authenticates_bindings_and_owner_mode(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    envelope = _bootstrap_envelope()
    path = tmp_path / "bootstrap.json"
    path.write_text(envelope.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)

    verified = verify_bootstrap_envelope_file(
        path,
        expected_parent=tmp_path,
        candidate_commit=CANDIDATE,
        installed_cache_sha256=CACHE_SHA256,
        bootstrap_module_sha256="6" * 64,
        producer_module_sha256=MODULE_SHA256,
        catalog_sha256=CATALOG_SHA256,
        contract_sha256=CONTRACT_SHA256,
        child_exit_code=1,
        runtime_binding_sha256=RUNTIME_BINDING_SHA256,
        install_report_sha256=INSTALL_REPORT_SHA256,
    )

    assert verified.status == "authenticated"
    assert verified.envelope == envelope


def test_legacy_bootstrap_round_trip_keeps_original_digest_contract() -> None:
    payload = _bootstrap_envelope().model_dump(mode="json")
    payload.pop("runtime_binding_sha256")
    payload.pop("install_report_sha256")
    material = {key: value for key, value in payload.items() if key != "envelope_sha256"}
    payload["envelope_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    legacy = CodexNativeBootstrapEnvelope.model_validate_json(json.dumps(payload))

    round_tripped = CodexNativeBootstrapEnvelope.model_validate_json(legacy.model_dump_json())

    assert round_tripped.envelope_sha256 == payload["envelope_sha256"]
    assert round_tripped.runtime_binding_sha256 is None


@pytest.mark.parametrize("mutation", ["digest", "binding", "runtime_binding", "mode"])
def test_bootstrap_file_verifier_rejects_tampering_or_unsafe_mode(
    tmp_path: Path,
    mutation: str,
) -> None:
    tmp_path.chmod(0o700)
    payload = _bootstrap_envelope().model_dump(mode="json")
    if mutation == "digest":
        payload["envelope_sha256"] = "f" * 64
    elif mutation == "binding":
        payload["candidate_commit"] = "a" * 40
    elif mutation == "runtime_binding":
        payload["runtime_binding_sha256"] = "a" * 64
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o644 if mutation == "mode" else 0o600)

    verified = verify_bootstrap_envelope_file(
        path,
        expected_parent=tmp_path,
        candidate_commit=CANDIDATE,
        installed_cache_sha256=CACHE_SHA256,
        bootstrap_module_sha256="6" * 64,
        producer_module_sha256=MODULE_SHA256,
        catalog_sha256=CATALOG_SHA256,
        contract_sha256=CONTRACT_SHA256,
        child_exit_code=1,
        runtime_binding_sha256=RUNTIME_BINDING_SHA256,
        install_report_sha256=INSTALL_REPORT_SHA256,
    )

    assert verified.status in {"tampered", "unsafe"}
    assert verified.envelope is None


def test_authenticated_child_is_unknown_without_bootstrap_evidence() -> None:
    child = _failure(_progress())

    verified = _verify(
        child.model_dump_json(),
        bootstrap_verification=CodexNativeBootstrapVerification(
            status="missing",
            envelope=None,
        ),
    )

    assert verified.failure_evidence_status == "missing"
    assert verified.producer_authenticated is False
    assert verified.execution_performed is None
    assert verified.network_call_made is None
    assert verified.bootstrap_evidence_status == "missing"
    assert verified.bootstrap_envelope is None


def test_native_sealed_command_invokes_direct_bootstrap_before_producer(
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    cache_root = tmp_path / "cache"
    envelope_path = tmp_path / "runtime" / "bootstrap.json"

    command = producer._codex_native_producer_command(  # noqa: SLF001
        cache_root,
        envelope_path,
        CANDIDATE,
        CACHE_SHA256,
        "6" * 64,
        MODULE_SHA256,
        CATALOG_SHA256,
        CONTRACT_SHA256,
        producer_python=tmp_path / "proof-runtime/bin/python",
        runtime_binding_path=tmp_path / "proof-runtime-binding.json",
        runtime_binding_sha256="7" * 64,
        install_report_path=tmp_path / "install.json",
        install_report_sha256="8" * 64,
    )

    assert Path(command[0]).is_absolute()
    assert command[1:3] == ("-I", "-S")
    assert command[3].endswith("src/saxo_bank_mcp/qa_analytics_proof_bootstrap.py")
    assert command[4:6] == ("--envelope-path", str(envelope_path.resolve()))
    assert command[0] != "uv"
    assert "uv" not in command
    assert "--uv-executable" not in command
    assert "--producer-root" in command
    assert command[command.index("--producer-python") + 1] == str(
        (tmp_path / "proof-runtime/bin/python").absolute(),
    )
    assert command[command.index("--runtime-binding-sha256") + 1] == "7" * 64
    assert command[command.index("--install-report-sha256") + 1] == "8" * 64
    assert command[-2:] == ("--harness-policy", "codex_native_v1")


def test_native_sealed_command_is_independent_of_caller_python_and_uv_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "caller-pythonpath"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "caller-uv-cache"))
    cache_root = tmp_path / "cache"
    producer_python = tmp_path / "proof-runtime/bin/python"

    command = producer._codex_native_producer_command(  # noqa: SLF001
        cache_root,
        tmp_path / "runtime/bootstrap.json",
        CANDIDATE,
        CACHE_SHA256,
        "6" * 64,
        MODULE_SHA256,
        CATALOG_SHA256,
        CONTRACT_SHA256,
        producer_python=producer_python,
        runtime_binding_path=tmp_path / "proof-runtime-binding.json",
        runtime_binding_sha256="7" * 64,
        install_report_path=tmp_path / "install.json",
        install_report_sha256="8" * 64,
    )

    assert Path(command[0]).is_absolute()
    assert command[1:3] == ("-I", "-S")
    assert command[3].endswith("qa_analytics_proof_bootstrap.py")
    assert "PYTHONPATH" not in " ".join(command)
    assert "UV_CACHE_DIR" not in " ".join(command)
    assert command[command.index("--producer-python") + 1] == str(producer_python.absolute())
