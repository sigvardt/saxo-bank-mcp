from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts/run_analytics_proof_matrix.py"
INTENT_NAME = "proof-runtime-consumption-intent.json"


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
    root = tmp_path / name
    script = root / "scripts/run_analytics_proof_matrix.py"
    producer = root / "src/saxo_bank_mcp/qa_analytics_proof_producer.py"
    marker = root / "candidate-runner-started.json"
    script.parent.mkdir(parents=True)
    producer.parent.mkdir(parents=True)
    script.write_text(
        "\n".join(
            (
                "from __future__ import annotations",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "root = Path(__file__).resolve().parents[1]",
                "expected_runner = root / 'scripts/run_analytics_proof_matrix.py'",
                "source_root = Path(os.environ['SAXO_ANALYTICS_CANDIDATE_SOURCE_ROOT'])",
                "marker = root / 'candidate-runner-started.json'",
                "payload = {",
                f"    'candidate_name': {name!r},",
                "    'cwd_is_candidate': Path.cwd().resolve() == root,",
                "    'runner_is_candidate': Path(__file__).resolve() == expected_runner,",
                "    'source_root_is_candidate': source_root.resolve() == root,",
                "    'bound_flag_count': sys.argv.count('--candidate-root-bound'),",
                "}",
                "marker.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')",
                "out = Path(sys.argv[sys.argv.index('--out') + 1])",
                "out.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')",
            ),
        )
        + "\n",
        encoding="utf-8",
    )
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
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "bound_flag_count": 1,
        "candidate_name": "candidate",
        "cwd_is_candidate": True,
        "runner_is_candidate": True,
        "source_root_is_candidate": True,
    }
    assert marker.is_file()
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
