from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.qa_candidate_full_suite import (
    CandidateFullSuiteError,
    run_candidate_full_suite,
    verify_candidate_full_suite_receipt,
)

TEST_COUNT = 7
SKIPPED_COUNT = 2
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700


def _git(repo: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise AssertionError("git is required for the test fixture")
    result = subprocess.run(
        (executable, *args),
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, *, exit_code: int = 0, failures: int = 0) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "run-pytest"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "target = next(\n"
        "    value.split('=', 1)[1]\n"
        "    for value in sys.argv\n"
        "    if value.startswith('--junitxml=')\n"
        ")\n"
        "pathlib.Path(target).write_text(\n"
        f'    \'<testsuites tests="{TEST_COUNT}" failures="{failures}" errors="0" '
        f'skipped="{SKIPPED_COUNT}" time="1.5" />\',\n'
        "    encoding='utf-8',\n"
        ")\n"
        "print('suite output stays ephemeral')\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "qa" + "@example.invalid")
    _git(repo, "config", "user.name", "QA")
    _git(repo, "add", "scripts/run-pytest")
    _git(repo, "commit", "-qm", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _environment(temp_root: Path) -> dict[str, str]:
    return {
        **os.environ,
        "TMPDIR": str(temp_root),
        "TMP": str(temp_root),
        "TEMP": str(temp_root),
    }


def test_full_suite_runner_retains_owner_only_junit_and_bound_receipt(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(tmp_path)
    evidence = tmp_path / "evidence"
    temp_root = tmp_path / "external-temp"
    temp_root.mkdir(mode=OWNER_DIRECTORY_MODE)

    receipt = run_candidate_full_suite(
        repo=repo,
        candidate_commit=candidate,
        evidence_root=evidence,
        environment=_environment(temp_root),
        platform_name="test",
    )

    junit = evidence / "full-suite.xml"
    receipt_path = evidence / "full-suite-receipt.json"
    assert receipt.status == "passed"
    assert receipt.candidate_commit == candidate
    assert receipt.candidate_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert receipt.external_temp_root == str(temp_root.resolve())
    assert receipt.exit_code == 0
    assert receipt.test_count == TEST_COUNT
    assert receipt.failure_count == 0
    assert receipt.error_count == 0
    assert receipt.skipped_count == SKIPPED_COUNT
    assert receipt.junit_retained is True
    assert receipt.raw_output_retained is False
    assert stat.S_IMODE(evidence.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert stat.S_IMODE(junit.stat().st_mode) == OWNER_FILE_MODE
    assert stat.S_IMODE(receipt_path.stat().st_mode) == OWNER_FILE_MODE
    assert verify_candidate_full_suite_receipt(receipt_path) == receipt
    assert "suite output stays ephemeral" not in receipt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation", ["candidate", "dirty", "temp"])
def test_full_suite_runner_refuses_before_launcher_on_untrusted_boundary(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo, candidate = _repo(tmp_path)
    evidence = tmp_path / "evidence"
    temp_root = tmp_path / "external-temp"
    temp_root.mkdir(mode=OWNER_DIRECTORY_MODE)
    environment = _environment(temp_root)
    if mutation == "candidate":
        candidate = "a" * 40
    elif mutation == "dirty":
        (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    else:
        environment["TEMP"] = str(tmp_path / "different-temp")

    with pytest.raises(CandidateFullSuiteError):
        run_candidate_full_suite(
            repo=repo,
            candidate_commit=candidate,
            evidence_root=evidence,
            environment=environment,
            platform_name="test",
        )

    assert not evidence.exists()


def test_full_suite_runner_retains_failed_exit_and_exact_counts(tmp_path: Path) -> None:
    repo, candidate = _repo(tmp_path, exit_code=1, failures=1)
    evidence = tmp_path / "evidence"
    temp_root = tmp_path / "external-temp"
    temp_root.mkdir(mode=OWNER_DIRECTORY_MODE)

    receipt = run_candidate_full_suite(
        repo=repo,
        candidate_commit=candidate,
        evidence_root=evidence,
        environment=_environment(temp_root),
        platform_name="test",
    )

    assert receipt.status == "failed"
    assert receipt.exit_code == 1
    assert receipt.failure_count == 1
    assert (
        verify_candidate_full_suite_receipt(
            evidence / "full-suite-receipt.json",
        )
        == receipt
    )


def test_full_suite_receipt_rejects_tampering(tmp_path: Path) -> None:
    repo, candidate = _repo(tmp_path)
    evidence = tmp_path / "evidence"
    temp_root = tmp_path / "external-temp"
    temp_root.mkdir(mode=OWNER_DIRECTORY_MODE)
    run_candidate_full_suite(
        repo=repo,
        candidate_commit=candidate,
        evidence_root=evidence,
        environment=_environment(temp_root),
        platform_name="test",
    )
    path = evidence / "full-suite-receipt.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["test_count"] = 8
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(OWNER_FILE_MODE)

    with pytest.raises(ValidationError):
        verify_candidate_full_suite_receipt(path)
