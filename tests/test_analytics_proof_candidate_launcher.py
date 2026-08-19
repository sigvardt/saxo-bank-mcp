from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts/run_analytics_proof_matrix.py"
INTENT_NAME = "proof-runtime-consumption-intent.json"
OWNER_FILE_MODE = 0o600


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for candidate-root launcher coverage")
    result = subprocess.run(
        (git, "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _detached_candidate(tmp_path: Path, name: str) -> tuple[Path, str, str, Path]:
    return _detached_candidate_with_runner(tmp_path, name, runner_kind="stdlib")


def _detached_candidate_with_runner(
    tmp_path: Path,
    name: str,
    *,
    runner_kind: Literal["stdlib", "third_party", "third_party_exit"],
) -> tuple[Path, str, str, Path]:
    root = tmp_path / name
    script = root / "scripts/run_analytics_proof_matrix.py"
    producer = root / "src/saxo_bank_mcp/qa_analytics_proof_producer.py"
    marker = root / "candidate-runner-started.json"
    script.parent.mkdir(parents=True)
    producer.parent.mkdir(parents=True)
    lines = [
        "from __future__ import annotations",
        "import hashlib",
        "import json",
        "import os",
        "import sys",
        "from pathlib import Path",
    ]
    if runner_kind != "stdlib":
        lines.extend(
            (
                "from pydantic import BaseModel",
                "class CandidatePayload(BaseModel):",
                "    candidate_name: str",
                "    cwd_is_candidate: bool",
                "    runner_is_candidate: bool",
                "    source_root_is_candidate: bool",
                "    bound_flag_count: int",
                "    entry_receipt_seen: bool",
            ),
        )
    lines.extend(
        (
            "root = Path(__file__).resolve().parents[1]",
            "expected_runner = root / 'scripts/run_analytics_proof_matrix.py'",
            "source_root = Path(os.environ['SAXO_ANALYTICS_CANDIDATE_SOURCE_ROOT'])",
            "marker = root / 'candidate-runner-started.json'",
            "out = Path(sys.argv[sys.argv.index('--out') + 1])",
            "payload = {",
            f"    'candidate_name': {name!r},",
            "    'cwd_is_candidate': Path.cwd().resolve() == root,",
            "    'runner_is_candidate': Path(__file__).resolve() == expected_runner,",
            "    'source_root_is_candidate': source_root.resolve() == root,",
            "    'bound_flag_count': sys.argv.count('--candidate-root-bound'),",
            "}",
        ),
    )
    if runner_kind != "stdlib":
        lines.extend(
            (
                "outer_name = out.name.removesuffix('.candidate-result.json')",
                "receipt = out.with_name(f'{outer_name}.candidate-runner.json')",
                "entry = json.loads(receipt.read_text(encoding='utf-8'))",
                "payload['entry_receipt_seen'] = (",
                "    entry['phase'] == 'entry' and entry['spawned'] is False",
                ")",
            ),
        )
        lines.append("payload = CandidatePayload(**payload).model_dump(mode='json')")
    lines.append("marker.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')")
    if runner_kind == "third_party_exit":
        lines.append("raise SystemExit(7)")
    else:
        lines.extend(
            (
                "def digest(value: object) -> str:",
                (
                    "    return hashlib.sha256(json.dumps(value, allow_nan=False, "
                    "separators=(',', ':'), sort_keys=True).encode()).hexdigest()"
                ),
                "candidate_commit = os.environ['SAXO_ANALYTICS_CANDIDATE_COMMIT']",
                "contract_sha256 = os.environ['SAXO_ANALYTICS_CANDIDATE_CONTRACT_SHA256']",
                (
                    "analysis_kind_count = int(os.environ["
                    "'SAXO_ANALYTICS_CANDIDATE_ANALYSIS_KIND_COUNT'])"
                ),
                (
                    "evidence_receipt_count = int(os.environ["
                    "'SAXO_ANALYTICS_CANDIDATE_EVIDENCE_RECEIPT_COUNT'])"
                ),
                "boundary_material = {",
                "    'schema_version': '1',",
                "    'receipt_kind': 'codex_native_boundary_failure',",
                "    'status': 'refused',",
                "    'harness_policy': 'codex_native_v1',",
                "    'candidate_commit': candidate_commit,",
                "    'failure_evidence_status': 'missing',",
                "    'producer_authenticated': False,",
                "    'boundary_phase': 'producer_validation',",
                "    'command_state': 'not_started',",
                "    'cleanup_status': 'unknown',",
                "    'candidate_runner_receipt_sha256': None,",
                "    'candidate_runner_cleanup_status': 'unknown',",
                "    'candidate_runner_result_status': 'unknown',",
                "    'candidate_runner_result_sha256': None,",
                "    'runtime_consumption_intent_sha256': None,",
                "    'runtime_cleanup_receipt_sha256': None,",
                "    'completed_phases': None,",
                "    'current_phase': None,",
                "    'sim_preflight_status': 'unknown',",
                "    'sim_preflight': None,",
                "    'network_call_made': None,",
                "    'model_event_count': None,",
                "    'mcp_event_count': None,",
                "    'saxo_event_count': None,",
                "    'execution_performed': None,",
                "    'broker_write_made': None,",
                "    'live_mutation_calls': None,",
                "    'purchase_occurred': None,",
                "    'disclaimer_response_made': None,",
                "    'child_cleanup_status': 'unknown',",
                "    'outer_runtime_cleanup_status': 'unknown',",
                "    'reason': 'proof_candidate_fixture_success',",
                "    'redacted_publication': True,",
                "}",
                (
                    "boundary = {**boundary_material, 'boundary_receipt_sha256': "
                    "digest(boundary_material)}"
                ),
                "publication_material = {",
                "    'schema_version': '1',",
                "    'receipt_kind': 'codex_native_proof_publication',",
                "    'harness_policy': 'codex_native_v1',",
                "    'candidate_commit': candidate_commit,",
                "    'analysis_kind_count': analysis_kind_count,",
                "    'evidence_receipt_count': evidence_receipt_count,",
                "    'contract_sha256': contract_sha256,",
                "    'result_kind': 'boundary_failure',",
                "    'result': boundary,",
                "    'redacted_publication': True,",
                "}",
                (
                    "publication = {**publication_material, 'publication_sha256': "
                    "digest(publication_material)}"
                ),
                "out.write_text(json.dumps(publication, sort_keys=True), encoding='utf-8')",
                "out.chmod(0o600)",
            ),
        )
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    producer.write_text("# candidate proof producer fixture\n", encoding="utf-8")
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "candidate-launcher@invalid")
    _git(root, "config", "user.name", "Candidate Launcher Test")
    _git(
        root,
        "add",
        "scripts/run_analytics_proof_matrix.py",
        "src/saxo_bank_mcp/qa_analytics_proof_producer.py",
    )
    _git(root, "commit", "--quiet", "-m", "candidate")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    _git(root, "checkout", "--quiet", "--detach", commit)
    return root, commit, tree, marker


def _write_install_binding(
    path: Path,
    *,
    candidate_commit: str,
    candidate_tree: str,
    run_root: Path,
) -> None:
    path.write_text(
        json.dumps(
            {
                "candidate_commit": candidate_commit,
                "proof_runtime": {"binding": {"candidate_tree": candidate_tree}},
                "run_root": str(run_root),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _launch(  # noqa: PLR0913
    *,
    candidate_root: Path | None,
    candidate_commit: str,
    install_report: Path,
    output: Path,
    codex_home: Path,
    root_bound: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(RUNNER),
        "--candidate-commit",
        candidate_commit,
    ]
    if candidate_root is not None:
        command.extend(("--candidate-source-root", str(candidate_root)))
    if root_bound:
        command.append("--candidate-root-bound")
    command.extend(
        (
            "--install-report",
            str(install_report),
            "--codex-global-home",
            str(codex_home),
            "--harness-policy",
            "codex_native_v1",
            "--out",
            str(output),
        ),
    )
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )


def test_docs_head_launcher_executes_exact_detached_candidate_runner(
    tmp_path: Path,
) -> None:
    candidate, commit, tree, marker = _detached_candidate(tmp_path, "candidate")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "bound_flag_count": 1,
        "candidate_name": "candidate",
        "cwd_is_candidate": True,
        "runner_is_candidate": True,
        "source_root_is_candidate": True,
    }
    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    publication = publication_module.verify_codex_native_proof_publication(
        output.read_text(encoding="utf-8"),
    )
    assert publication.candidate_commit == commit
    assert publication.result.reason == "proof_candidate_fixture_success"
    assert not (run_root / INTENT_NAME).exists()


def test_candidate_launcher_preserves_venv_for_real_third_party_import(
    tmp_path: Path,
) -> None:
    candidate, commit, tree, marker = _detached_candidate_with_runner(
        tmp_path,
        "dependency-candidate",
        runner_kind="third_party",
    )
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["candidate_name"] == (
        "dependency-candidate"
    )
    assert json.loads(marker.read_text(encoding="utf-8"))["entry_receipt_seen"] is True
    receipt = output.with_name(f"{output.name}.candidate-runner.json")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["spawned"] is True
    assert payload["exit_code"] == 0
    assert payload["result_present"] is True
    assert payload["cleanup_status"] == "complete"
    assert receipt.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert not (run_root / INTENT_NAME).exists()


def test_candidate_launcher_publishes_truthful_exit_when_result_is_missing(
    tmp_path: Path,
) -> None:
    candidate, commit, tree, marker = _detached_candidate_with_runner(
        tmp_path,
        "failed-candidate",
        runner_kind="third_party_exit",
    )
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    publication = json.loads(output.read_text(encoding="utf-8"))
    receipt_path = output.with_name(f"{output.name}.candidate-runner.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert marker.is_file()
    assert publication["result"]["reason"] == "proof_candidate_runner_result_missing"
    assert publication["result"]["command_state"] == "completed"
    assert publication["result"]["candidate_runner_cleanup_status"] == "complete"
    assert publication["result"]["candidate_runner_receipt_sha256"] == receipt["receipt_sha256"]
    assert receipt == {
        "candidate_commit": commit,
        "candidate_tree": tree,
        "cleanup_identity_evidence_status": "authenticated",
        "cleanup_identity_receipt_sha256": receipt["cleanup_identity_receipt_sha256"],
        "cleanup_status": "complete",
        "cleanup_unknown_reason": None,
        "command_schema_sha256": receipt["command_schema_sha256"],
        "command_sha256": receipt["command_sha256"],
        "exit_code": 7,
        "harness_policy": "codex_native_v1",
        "phase": "exit",
        "receipt_kind": "codex_native_candidate_runner",
        "receipt_sha256": receipt["receipt_sha256"],
        "result_present": False,
        "result_sha256": None,
        "schema_version": "1",
        "spawned": True,
    }
    assert re.fullmatch(r"[a-f0-9]{64}", receipt["command_sha256"])
    assert re.fullmatch(r"[a-f0-9]{64}", receipt["command_schema_sha256"])
    assert all("/" not in str(value) for value in receipt.values())
    assert receipt_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert not (run_root / INTENT_NAME).exists()

    publication_module = import_module("saxo_bank_mcp.qa_analytics_proof_publication")
    verified = publication_module.verify_codex_native_candidate_runner_receipt(
        receipt_path.read_text(encoding="utf-8"),
    )
    assert verified.receipt_sha256 == receipt["receipt_sha256"]
    for changed in (
        {**receipt, "exit_code": 8},
        {**receipt, "private_path": "/forbidden"},
    ):
        with pytest.raises(ValidationError):
            publication_module.verify_codex_native_candidate_runner_receipt(
                json.dumps(changed),
            )


def test_candidate_launcher_rejects_forwarded_relative_path_before_spawn(
    tmp_path: Path,
) -> None:
    candidate, commit, tree, marker = _detached_candidate(tmp_path, "candidate")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )
    relative_output = Path(os.path.relpath(output, REPO_ROOT))

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=relative_output,
        codex_home=codex_home,
    )

    publication = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert publication["result"]["reason"] == "proof_candidate_runner_path_not_absolute"
    assert publication["result"]["command_state"] == "not_started"
    assert not marker.exists()
    assert not (run_root / INTENT_NAME).exists()


def test_launcher_refuses_missing_candidate_root_before_consumption(tmp_path: Path) -> None:
    _, commit, tree, _ = _detached_candidate(tmp_path, "candidate")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=None,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["result_kind"] == "boundary_failure"
    assert payload["result"]["reason"] == "proof_source_root_missing"
    assert payload["result"]["command_state"] == "not_started"
    assert not (run_root / INTENT_NAME).exists()


def test_launcher_refuses_wrong_candidate_root_before_runner_or_consumption(
    tmp_path: Path,
) -> None:
    expected, commit, tree, _ = _detached_candidate(tmp_path, "expected")
    wrong, _, _, wrong_marker = _detached_candidate(tmp_path, "wrong")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=wrong,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert expected != wrong
    assert result.returncode == 1
    assert payload["result_kind"] == "boundary_failure"
    assert payload["result"]["reason"] == "proof_source_candidate_mismatch"
    assert payload["result"]["command_state"] == "not_started"
    assert not wrong_marker.exists()
    assert not (run_root / INTENT_NAME).exists()


def test_launcher_refuses_forged_bound_flag_from_docs_head_before_consumption(
    tmp_path: Path,
) -> None:
    candidate, commit, tree, marker = _detached_candidate(tmp_path, "candidate")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
        root_bound=True,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["result_kind"] == "boundary_failure"
    assert payload["result"]["reason"] == "proof_source_entrypoint_mismatch"
    assert payload["result"]["command_state"] == "not_started"
    assert not marker.exists()
    assert not (run_root / INTENT_NAME).exists()


def test_launcher_refuses_attached_candidate_root_before_consumption(tmp_path: Path) -> None:
    candidate, commit, tree, marker = _detached_candidate(tmp_path, "candidate")
    _git(candidate, "switch", "--quiet", "-c", "attached")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["result_kind"] == "boundary_failure"
    assert payload["result"]["reason"] == "proof_source_worktree_not_detached"
    assert payload["result"]["command_state"] == "not_started"
    assert not marker.exists()
    assert not (run_root / INTENT_NAME).exists()


def test_launcher_refuses_untracked_dirty_candidate_before_runner_or_consumption(
    tmp_path: Path,
) -> None:
    candidate, commit, tree, marker = _detached_candidate(tmp_path, "candidate")
    (candidate / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    run_root = tmp_path / "install-runtime"
    run_root.mkdir(mode=0o700)
    report = tmp_path / "install.json"
    output = tmp_path / "proof.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    _write_install_binding(
        report,
        candidate_commit=commit,
        candidate_tree=tree,
        run_root=run_root,
    )

    result = _launch(
        candidate_root=candidate,
        candidate_commit=commit,
        install_report=report,
        output=output,
        codex_home=codex_home,
    )

    payload: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 1
    assert payload["result_kind"] == "boundary_failure"
    assert payload["result"]["reason"] == "proof_source_worktree_not_clean"
    assert payload["result"]["command_state"] == "not_started"
    assert not marker.exists()
    assert not (run_root / INTENT_NAME).exists()
