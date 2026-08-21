from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_skill_cli_support import ReleaseCommand, run_release
from test_agent_skill_evidence_support import (
    InstallFixture,
    build_install_fixture,
    build_release_evidence,
    git,
    reason,
    write_json,
)

import saxo_bank_mcp.agent_skill_release as release_module
from saxo_bank_mcp.agent_skill_install_qa import load_install_report_for_consumers
from saxo_bank_mcp.agent_skill_matrix import ExecutedMatrixReport, load_verified_matrix_report
from saxo_bank_mcp.agent_skill_release_models import ReleaseAssembleOptions


@pytest.fixture(scope="module")
def installed_report(tmp_path_factory: pytest.TempPathFactory) -> InstallFixture:
    return build_install_fixture(tmp_path_factory.mktemp("release-installed-report"))


def test_release_accepts_exact_reconciled_sim_audit_delta(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, plan, live = build_release_evidence(
        tmp_path,
        installed_report,
        include_privacy=True,
    )
    out = evidence / "release-v1" / "manifest.json"
    latest = evidence / "latest.json"

    fixture_install, fixture_errors = load_install_report_for_consumers(
        installed_report.report,
    )
    assert fixture_install is not None, fixture_errors
    production_install = SimpleNamespace(
        candidate_commit=fixture_install.candidate_commit,
        execution_mode="installed_verification",
        expected_tools=fixture_install.expected_tools,
        global_state_unchanged=True,
        process_cleanup=SimpleNamespace(complete=True),
    )

    def load_production_install(_path: Path) -> tuple[SimpleNamespace, tuple[()]]:
        return production_install, ()

    monkeypatch.setattr(
        release_module,
        "load_install_report_for_consumers",
        load_production_install,
    )
    matrix_path = evidence / "task-15-sim" / "manual" / "tool-matrix.json"
    ExecutedMatrixReport.model_validate_json(matrix_path.read_text(encoding="utf-8"))
    matrix, matrix_errors = load_verified_matrix_report(matrix_path)
    assert matrix is not None, matrix_errors

    result = release_module.assemble_release(
        ReleaseAssembleOptions(
            repo=installed_report.repo,
            plan=plan,
            evidence_root=evidence,
            release="release-v1",
            source_commit=installed_report.commit,
            verify_live_proof=live,
            out=out,
            latest=latest,
            check=False,
        )
    )

    assert result == 0, out.read_text(encoding="utf-8") if out.is_file() else str(result)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["sim_state_unchanged"] is True
    latest_payload = json.loads(latest.read_text(encoding="utf-8"))
    assert latest_payload["manifest"] == "release-v1/manifest.json"
    assert latest_payload["source_commit"] == installed_report.commit


def test_release_rejects_missing_empty_and_stale_evidence(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    missing_out = tmp_path / "missing.json"
    empty_out = tmp_path / "empty.json"
    stale_out = tmp_path / "stale.json"
    latest = tmp_path / "latest.json"
    empty_root = tmp_path / "empty-evidence"
    empty_root.mkdir()
    nonempty = tmp_path / "evidence"
    nonempty.mkdir()
    (nonempty / "marker").write_text("fixture", encoding="utf-8")
    stale = git("rev-parse", "HEAD^", cwd=installed_report.repo).stdout.strip()

    missing = run_release(
        ReleaseCommand(
            evidence=tmp_path / "missing",
            commit="HEAD",
            out=missing_out,
            latest=latest,
            repo=installed_report.repo,
        )
    )
    stale_result = run_release(
        ReleaseCommand(
            evidence=nonempty,
            commit=stale,
            out=stale_out,
            latest=latest,
            repo=installed_report.repo,
        )
    )
    empty = run_release(
        ReleaseCommand(
            evidence=empty_root,
            commit="HEAD",
            out=empty_out,
            latest=latest,
            repo=installed_report.repo,
        )
    )

    assert missing.returncode != 0
    assert reason(missing_out) == "evidence_root_not_found"
    assert empty.returncode != 0
    assert reason(empty_out) == "evidence_root_empty"
    assert stale_result.returncode != 0
    assert reason(stale_out) == "source_commit_stale"
    assert not latest.exists()


def test_release_requires_live_proof_argument(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "marker").write_text("fixture", encoding="utf-8")
    out = tmp_path / "out.json"

    result = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit="HEAD",
            out=out,
            latest=tmp_path / "latest.json",
            repo=installed_report.repo,
        )
    )

    assert result.returncode != 0
    assert reason(out) == "live_proof_required"


def test_release_rejects_missing_privacy_and_keeps_latest(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    evidence, plan, live = build_release_evidence(tmp_path, installed_report, include_privacy=False)
    out = tmp_path / "manifest.json"
    latest = tmp_path / "latest.json"
    latest.write_text('{"release":"release-v0"}\n', encoding="utf-8")
    before = latest.read_bytes()

    result = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit=installed_report.commit,
            out=out,
            latest=latest,
            plan=plan,
            live=live,
            repo=installed_report.repo,
        )
    )

    assert result.returncode != 0
    assert reason(out) == "privacy_evidence_missing"
    assert latest.read_bytes() == before


def test_release_rejects_bogus_commit_and_missing_task_evidence(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "marker").write_text("fixture", encoding="utf-8")
    plan = tmp_path / "plan.md"
    plan.write_text("# Fixture\n", encoding="utf-8")
    latest = tmp_path / "latest.json"
    bogus_out = tmp_path / "bogus.json"
    task_out = tmp_path / "task.json"

    bogus = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit="not-a-commit",
            out=bogus_out,
            latest=latest,
            repo=installed_report.repo,
        )
    )
    missing_task = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit=installed_report.commit,
            out=task_out,
            latest=latest,
            plan=plan,
            live=tmp_path / "not-yet-validated.json",
            repo=installed_report.repo,
        )
    )

    assert bogus.returncode != 0
    assert reason(bogus_out) == "source_commit_unresolved"
    assert missing_task.returncode != 0
    assert reason(task_out) == "task_evidence_missing"
    assert not latest.exists()


def test_release_rejects_missing_install_artifact(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    evidence, plan, live = build_release_evidence(tmp_path, installed_report, include_privacy=True)
    (evidence / "task-14-installed-cache/manual/install.json").unlink()
    out = tmp_path / "out.json"
    latest = tmp_path / "latest.json"

    result = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit=installed_report.commit,
            out=out,
            latest=latest,
            plan=plan,
            live=live,
            repo=installed_report.repo,
        )
    )

    assert result.returncode != 0
    assert reason(out) == "sim_or_install_evidence_missing"
    assert not latest.exists()


def test_release_requires_explicit_task_status(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    evidence, plan, live = build_release_evidence(tmp_path, installed_report, include_privacy=True)
    claim = evidence / "task-12-fixture" / "DoneClaim.json"
    write_json(claim, {"source_commit": installed_report.commit})
    out = evidence / "release-v1" / "manifest.json"
    latest = evidence / "latest.json"

    result = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit=installed_report.commit,
            out=out,
            latest=latest,
            plan=plan,
            live=live,
            repo=installed_report.repo,
        )
    )

    assert result.returncode != 0
    assert reason(out) == "task_evidence_status_missing"
    assert not latest.exists()


def test_release_rejects_publication_paths_outside_evidence_root(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    evidence, plan, live = build_release_evidence(tmp_path, installed_report, include_privacy=True)
    out = tmp_path / "outside-manifest.json"
    latest = evidence / "latest.json"

    result = run_release(
        ReleaseCommand(
            evidence=evidence,
            commit=installed_report.commit,
            out=out,
            latest=latest,
            plan=plan,
            live=live,
            repo=installed_report.repo,
        )
    )

    assert result.returncode != 0
    assert not out.exists()
    assert not latest.exists()
