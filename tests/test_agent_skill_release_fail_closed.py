from __future__ import annotations

from pathlib import Path

import pytest
from agent_skill_cli_support import ReleaseCommand, run_release
from test_agent_skill_evidence_support import (
    InstallFixture,
    build_install_fixture,
    build_release_evidence,
    git,
    reason,
)


@pytest.fixture(scope="module")
def installed_report(tmp_path_factory: pytest.TempPathFactory) -> InstallFixture:
    return build_install_fixture(tmp_path_factory.mktemp("release-installed-report"))


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
    evidence, plan, live = build_release_evidence(
        tmp_path, installed_report, include_privacy=False
    )
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
    evidence, plan, live = build_release_evidence(
        tmp_path, installed_report, include_privacy=True
    )
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
