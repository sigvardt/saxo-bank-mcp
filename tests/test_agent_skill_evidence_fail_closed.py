from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_skill_evidence_support import (
    ROOT,
    InstallFixture,
    build_install_fixture,
    errors,
    reason,
    run_cli,
    write_json,
)

INSTALL_QA = ROOT / "scripts/qa_dual_plugin_install.py"
MATRIX_RUNNER = ROOT / "scripts/run_mcp_tool_matrix.py"


@pytest.fixture(scope="module")
def installed_report(tmp_path_factory: pytest.TempPathFactory) -> InstallFixture:
    return build_install_fixture(tmp_path_factory.mktemp("installed-report"))


def test_install_rejects_nonexistent_repo_with_exact_reason(tmp_path: Path) -> None:
    out = tmp_path / "out.json"

    result = run_cli(INSTALL_QA, "--repo", str(tmp_path / "missing"), "--out", str(out))

    assert result.returncode != 0
    assert reason(out) == "repo_not_found"


def test_install_rejects_unresolved_commit_with_exact_reason(tmp_path: Path) -> None:
    out = tmp_path / "out.json"

    result = run_cli(
        INSTALL_QA,
        "--repo",
        str(ROOT),
        "--commit",
        "not-a-commit",
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert reason(out) == "commit_unresolved"


def test_install_verify_rejects_missing_fingerprint_contract(tmp_path: Path) -> None:
    report = tmp_path / "install.json"
    out = tmp_path / "out.json"
    write_json(report, {"status": "passed", "expected_skills": 8, "expected_tools": 39})

    result = run_cli(
        INSTALL_QA,
        "--verify-only",
        "--install-report",
        str(report),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert "global_state_fingerprints_missing" in errors(out)


def test_install_rejects_isolated_home_collision(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    run_root = tmp_path / "run"
    claude_home = tmp_path / "claude-global"
    run_root.mkdir()
    claude_home.mkdir()

    result = run_cli(
        INSTALL_QA,
        "--repo",
        str(ROOT),
        "--run-root",
        str(run_root),
        "--codex-global-home",
        str(run_root),
        "--claude-global-home",
        str(claude_home),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert reason(out) == "isolated_home_collision"


def test_matrix_rejects_missing_and_invalid_install_reports(tmp_path: Path) -> None:
    missing_out = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid_out = tmp_path / "invalid-out.json"
    invalid.write_text("not json", encoding="utf-8")

    missing = run_cli(MATRIX_RUNNER, "--out", str(missing_out))
    malformed = run_cli(
        MATRIX_RUNNER,
        "--install-report",
        str(invalid),
        "--out",
        str(invalid_out),
    )

    assert missing.returncode != 0
    assert reason(missing_out) == "missing_install_report"
    assert malformed.returncode != 0
    assert reason(invalid_out) == "invalid_install_report"


def test_matrix_manifest_only_zero_calls_is_validated_not_passed(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    out = tmp_path / "matrix.json"

    result = run_cli(
        MATRIX_RUNNER,
        "--install-report",
        str(installed_report.report),
        "--fixture-stock-uic",
        "211",
        "--fixture-amount",
        "1",
        "--fixture-limit-price",
        "50",
        "--fixture-modified-limit-price",
        "51",
        "--fixture-option-uics",
        "30004846,30004926",
        "--fixture-stream-uic",
        "21",
        "--out",
        str(out),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stderr
    assert payload["status"] == "validated"
    assert payload["execution_mode"] == "manifest_validation"
    assert payload["created_mcp_calls"] == 0


def test_matrix_verify_rejects_zero_call_execution_report(tmp_path: Path) -> None:
    report = tmp_path / "matrix.json"
    out = tmp_path / "out.json"
    write_json(
        report,
        {
            "status": "passed",
            "execution_mode": "sim_execution",
            "environment": "SIM",
            "tool_count": 39,
            "tool_calls": [],
            "cleanup": {"complete": True},
        },
    )

    result = run_cli(
        MATRIX_RUNNER,
        "--verify-only",
        "--report",
        str(report),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert "tool_call_evidence_missing" in errors(out)
