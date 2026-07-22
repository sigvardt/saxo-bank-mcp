from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from saxo_bank_mcp import evidence_publication, qa, qa_readme_probe
from saxo_bank_mcp._evidence import JsonValue

REQUIRED_GITIGNORE_PATTERNS = (
    ".omo/",
    ".codegraph/",
    ".env",
    "*credential*",
    "*secret*",
    "*token*",
    "*.log",
)


def init_git_repo(path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for gitignore probe tests")
    subprocess.run(
        [git, "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_gitignore_secret_probe_checks_dummy_files_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text("\n".join(REQUIRED_GITIGNORE_PATTERNS) + "\n", encoding="utf-8")
    out = tmp_path / "gitignore.json"

    result = qa.main(["gitignore-secret", "--out", str(out)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["missing_patterns"] == []
    assert report["remaining_exists"] == []
    assert set(report["cleanup_removed"]) == set(report["dummy_paths"])
    assert report["git_check"]["status"] == "passed"
    assert set(report["git_check"]["ignored_paths"]) == set(report["dummy_paths"])
    assert not any((tmp_path / path).exists() for path in report["dummy_paths"])


def test_gitignore_secret_probe_fails_when_dummy_is_not_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text(
        "\n".join(pattern for pattern in REQUIRED_GITIGNORE_PATTERNS if pattern != "*.log") + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "gitignore.json"

    result = qa.main(["gitignore-secret", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert "*.log" in report["missing_patterns"]
    assert "task-1-qa.log" in report["missing_patterns"]
    assert report["remaining_exists"] == []


def test_health_probe_calls_fastmcp_saxo_health(tmp_path: Path) -> None:
    out = tmp_path / "health.json"

    result = qa.main(["health", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["tool_name"] == "saxo_health"
    assert report["driver"] == "loop_harness"
    assert report["mode"] == "SIM"
    assert report["live_writes"] is False
    assert "not implemented" not in report["detail"]


def test_readme_smoke_passes_with_required_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    required_text = "\n".join(qa_readme_probe.README_REQUIRED_MARKERS)
    (tmp_path / "README.md").write_text(required_text, encoding="utf-8")
    (docs / "operator-guide.md").write_text(required_text, encoding="utf-8")
    (docs / "incident-cleanup.md").write_text(required_text, encoding="utf-8")
    out = tmp_path / "readme-smoke.json"

    result = qa.main(["readme-smoke", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["required_command_markers_present"] is True
    assert report["missing_command_markers"] == []
    assert report["health_status"] == "passed"
    assert report["auth_status_status"] == "passed"
    assert report["copied_secret_values_detected"] is False
    assert report["prompted_user"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    assert set(report["final_verify_help_exit_codes"].values()) == {0}


def test_readme_smoke_fails_when_command_marker_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    missing_marker = qa_readme_probe.README_REQUIRED_MARKERS[0]
    required_text = "\n".join(
        marker for marker in qa_readme_probe.README_REQUIRED_MARKERS if marker != missing_marker
    )
    (tmp_path / "README.md").write_text(required_text, encoding="utf-8")
    (docs / "operator-guide.md").write_text(required_text, encoding="utf-8")
    (docs / "incident-cleanup.md").write_text(required_text, encoding="utf-8")
    out = tmp_path / "readme-smoke.json"

    result = qa.main(["readme-smoke", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["required_command_markers_present"] is False
    assert report["missing_command_markers"] == [missing_marker]


def test_readme_smoke_rejects_before_persisting_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "readme-smoke.json"
    marker = "rejected-readme-candidate"

    def rejected_payload(_payload: JsonValue) -> dict[str, JsonValue]:
        return {"status": "passed", "unsafe_marker": marker}

    def reject_marker(
        _label: str,
        text: str,
    ) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
        findings: list[dict[str, JsonValue]] = (
            [{"pattern_class": "credential_regex"}] if marker in text else []
        )
        return findings, []

    monkeypatch.setattr(
        qa_readme_probe,
        "redact_json",
        rejected_payload,
    )
    monkeypatch.setattr(
        evidence_publication,
        "scan_secret_text",
        reject_marker,
    )

    result = qa_readme_probe.handle_readme_smoke(out)

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }
    assert marker not in out.read_text(encoding="utf-8")
