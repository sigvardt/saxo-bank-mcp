from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, run_command
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.qa_analytics_proof_failure import (
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

CANDIDATE = "1" * 40
CACHE_SHA256 = "2" * 64
MODULE_SHA256 = "3" * 64
CATALOG_SHA256 = "4" * 64
CONTRACT_SHA256 = "5" * 64
AGENT_MODEL_EVENT_COUNT = 2
AGENT_TOTAL_MCP_EVENT_COUNT = 4
CLI_TOTAL_MCP_EVENT_COUNT = 3
CRASH_EXIT_CODE = -9
FAILED_CHILD_EXIT_CODE = 7
EXPECTED_BINDING_CALLS = 2


def _bootstrap_envelope(
    *,
    child_exit_code: int | None = 1,
    state: str = "failed",
    contract_sha256: str = CONTRACT_SHA256,
) -> CodexNativeBootstrapEnvelope:
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
        "bootstrap_state": state,
        "completed_bootstrap_phases": (
            "entry",
            "producer_import",
            "producer_handoff",
        ),
        "current_bootstrap_phase": "producer_execution",
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
            "proof_bootstrap_producer_nonzero"
            if state == "failed"
            else "proof_bootstrap_producer_started"
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

    def native_command(*_args: object) -> tuple[str, ...]:
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

    def fail_producer(*_args: object, **_kwargs: object) -> NoReturn:
        raise producer.CodexNativeProofFailureError(receipt)

    monkeypatch.setattr(script, "run_verified_codex_native_producer", fail_producer)
    output = tmp_path / "proof.json"

    exit_code = script.main(
        [
            "--candidate-commit",
            CANDIDATE,
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
    )

    assert verified.status == "authenticated"
    assert verified.envelope == envelope


@pytest.mark.parametrize("mutation", ["digest", "binding", "mode"])
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
    )

    assert Path(command[0]).is_absolute()
    assert command[1:3] == ("-I", "-S")
    assert command[3].endswith("src/saxo_bank_mcp/qa_analytics_proof_bootstrap.py")
    assert command[4:6] == ("--envelope-path", str(envelope_path.resolve()))
    assert command[0] != "uv"
    assert "uv" not in command[:4]
    assert "--uv-executable" in command
    assert "--producer-root" in command
    assert command[-2:] == ("--harness-policy", "codex_native_v1")


def test_native_sealed_command_keeps_bootstrap_first_when_uv_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    def _missing_uv(_name: str) -> None:
        return None

    monkeypatch.setattr(producer.shutil, "which", _missing_uv)
    cache_root = tmp_path / "cache"

    command = producer._codex_native_producer_command(  # noqa: SLF001
        cache_root,
        tmp_path / "runtime/bootstrap.json",
        CANDIDATE,
        CACHE_SHA256,
        "6" * 64,
        MODULE_SHA256,
        CATALOG_SHA256,
        CONTRACT_SHA256,
    )

    assert Path(command[0]).is_absolute()
    assert command[1:3] == ("-I", "-S")
    assert command[3].endswith("qa_analytics_proof_bootstrap.py")
    launcher = Path(command[command.index("--uv-executable") + 1])
    assert launcher.is_absolute()
    assert launcher.name == ".missing-proof-uv"
