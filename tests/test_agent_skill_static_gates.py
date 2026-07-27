from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp.agent_skill_static_gates import (
    FAILURE_FIXTURES,
    PUBLIC_SECRET_SCAN_PATHS,
    StaticGateResult,
    run_static_gates,
    self_test_fixture_errors,
)

ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPT: Final = ROOT / "scripts/run_agent_skill_static_gates.py"
EXPECTED_SKILL_COUNT: Final = 8


def test_static_gates_pass_on_current_worktree() -> None:
    # Given: the dual-harness packaging, skills, evals, and catalogs are committed.
    # When: every pure-Python static gate runs against the repository root.
    result = run_static_gates(ROOT)

    # Then: version, skill, wildcard, link, and public-tree gates are clean.
    assert result.status == "passed", result.errors
    assert result.skill_count == EXPECTED_SKILL_COUNT
    assert result.version_parity is True
    assert result.wildcard_findings == ()
    assert result.link_findings == ()
    assert result.frontmatter_findings == ()
    assert result.nested_reference_findings == ()
    assert result.cache_dangerous_findings == ()


def test_public_secret_scan_paths_cover_dual_harness_surface() -> None:
    # Given: Todo 13 requires public secret scanning of dual-harness surfaces.
    required = {
        "README.md",
        "docs",
        "src",
        "tests",
        "pyproject.toml",
        "uv.lock",
        ".github",
        ".gitignore",
        ".mcp.json",
        ".agents",
        ".codex-plugin",
        ".claude-plugin",
        "skills",
        "scripts",
        "evals",
        "data/saxo",
    }

    # When: the published path set is inspected.
    # Then: every dual-harness public path is included exactly once.
    assert frozenset(PUBLIC_SECRET_SCAN_PATHS) == required


@pytest.mark.parametrize("fixture_name", sorted(FAILURE_FIXTURES))
def test_named_failure_fixtures_exit_nonzero_with_class_only(fixture_name: str) -> None:
    # Given: the six Todo 13 negative fixtures and a redaction canary.
    canary = "CANARY_DO_NOT_ECHO"

    # When: the static-gate CLI validates that fixture.
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test-fixture", fixture_name, canary],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}"

    # Then: the process fails with only the named class and never echoes the canary.
    assert completed.returncode != 0
    assert fixture_name.replace("-", "_") in combined or fixture_name in combined
    assert canary not in combined
    assert self_test_fixture_errors(fixture_name)
    assert canary not in "\n".join(self_test_fixture_errors(fixture_name))


def test_static_gate_cli_check_exits_zero_and_prints_summary() -> None:
    # Given: the committed dual-harness tree.
    # When: the CLI check entrypoint runs.
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    # Then: the gate is green and the summary is machine-readable.
    assert completed.returncode == 0, completed.stderr
    assert "status=passed" in completed.stdout
    assert f"skill_count={EXPECTED_SKILL_COUNT}" in completed.stdout
    assert "version_parity=true" in completed.stdout


def test_static_gate_result_serializes_without_sensitive_values() -> None:
    # Given: a failed gate result with only sanitized class labels.
    result = StaticGateResult(
        status="failed",
        skill_count=0,
        version_parity=False,
        wildcard_findings=("wildcard_grant",),
        link_findings=(),
        frontmatter_findings=("bad_skill_frontmatter",),
        nested_reference_findings=(),
        cache_dangerous_findings=(),
        errors=("manifest_version_drift", "fake_secret"),
    )

    # When: the result is rendered for evidence.
    payload = json.loads(result.to_json())

    # Then: only class labels are present.
    assert payload["status"] == "failed"
    assert payload["errors"] == ["manifest_version_drift", "fake_secret"]
    assert "token" not in result.to_json().lower()
