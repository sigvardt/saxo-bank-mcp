from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final
from unittest.mock import MagicMock

import pytest

from saxo_bank_mcp.agent_catalog_runtime import CatalogIssue, CatalogValidationError
from saxo_bank_mcp.agent_skill_static_gate_checks import (
    cache_dangerous_findings,
    frontmatter_findings,
    link_findings,
    version_parity_errors,
    wildcard_findings,
)
from saxo_bank_mcp.agent_skill_static_gate_constants import FIXTURE_CANARY
from saxo_bank_mcp.agent_skill_static_gate_fixtures import (
    fake_secret_errors,
    missing_annotation_errors,
    stale_generated_row_errors,
)
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
CANARY: Final = FIXTURE_CANARY
EXPECTED_FIXTURE_RESULTS: Final = {
    "missing-annotation": ("missing_annotation",),
    "stale-generated-row": ("stale_generated_row",),
    "bad-skill-frontmatter": ("bad_skill_frontmatter",),
    "manifest-version-drift": ("manifest_version_drift",),
    "wildcard-grant": ("wildcard_grant",),
    "fake-secret": ("fake_secret",),
}


def test_static_gates_pass_on_current_worktree() -> None:
    result = run_static_gates(ROOT)

    assert result.status == "passed", result.errors
    assert result.skill_count == EXPECTED_SKILL_COUNT
    assert result.version_parity is True
    assert result.wildcard_findings == ()
    assert result.link_findings == ()
    assert result.frontmatter_findings == ()
    assert result.nested_reference_findings == ()
    assert result.cache_dangerous_findings == ()


def test_public_secret_scan_paths_cover_dual_harness_surface() -> None:
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

    assert frozenset(PUBLIC_SECRET_SCAN_PATHS) == required


@pytest.mark.parametrize("fixture_name", sorted(FAILURE_FIXTURES))
def test_named_failure_fixtures_use_production_validators_without_canary_echo(
    fixture_name: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test-fixture", fixture_name],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    expected = EXPECTED_FIXTURE_RESULTS[fixture_name]

    assert completed.returncode != 0
    assert expected[0] in combined
    assert CANARY not in combined
    errors = self_test_fixture_errors(fixture_name, root=ROOT)
    assert errors == expected
    assert CANARY not in "\n".join(errors)


def test_missing_annotation_puts_canary_in_runtime_tool_id() -> None:
    errors = missing_annotation_errors(CANARY)
    assert errors == ("missing_annotation",)
    assert missing_annotation_errors("OTHER_CANARY_VALUE") == ("missing_annotation",)


def test_stale_generated_row_requires_exact_stale_operation_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_other(_inputs: object) -> None:
        raise CatalogValidationError((CatalogIssue("unknown_tool", "x"),))

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_fixtures.validate_catalog_sources",
        raise_other,
    )
    assert stale_generated_row_errors(ROOT, CANARY) == ("fixture_setup_error",)


def test_stale_generated_row_rejects_mixed_stale_and_other_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_mixed(_inputs: object) -> None:
        raise CatalogValidationError(
            (
                CatalogIssue("stale_operation", "get.fixture.stale"),
                CatalogIssue("unknown_tool", "x"),
            ),
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_fixtures.validate_catalog_sources",
        raise_mixed,
    )
    assert stale_generated_row_errors(ROOT, CANARY) == ("fixture_setup_error",)


def test_stale_generated_row_rejects_empty_issue_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_empty(_inputs: object) -> None:
        raise CatalogValidationError(())

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_fixtures.validate_catalog_sources",
        raise_empty,
    )
    assert stale_generated_row_errors(ROOT, CANARY) == ("fixture_setup_error",)


def test_stale_generated_row_rejects_no_exception_validator_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def accept(_inputs: object) -> None:
        return None

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_fixtures.validate_catalog_sources",
        accept,
    )
    assert stale_generated_row_errors(ROOT, CANARY) == ("fixture_setup_error",)


def test_stale_generated_row_happy_path_is_exact() -> None:
    assert self_test_fixture_errors("stale-generated-row", root=ROOT) == ("stale_generated_row",)


def test_fake_secret_distinguishes_findings_from_scan_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def scan_error(_label: str, _text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return [], [{"error": "scanner_internal_error"}]

    def scan_finding(_label: str, _text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return [{"pattern_class": "credential_regex"}], []

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_fixtures.scan_secret_text",
        scan_error,
    )
    assert fake_secret_errors(CANARY) == ("fixture_scan_error",)
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_fixtures.scan_secret_text",
        scan_finding,
    )
    assert fake_secret_errors(CANARY) == ("fake_secret",)


def test_manifest_version_drift_fixture_invokes_version_parity_errors() -> None:
    assert version_parity_errors(ROOT) == ()
    errors = self_test_fixture_errors("manifest-version-drift", root=ROOT)
    assert errors == ("manifest_version_drift",)
    assert CANARY not in "\n".join(errors)


def test_wildcard_fixture_invokes_wildcard_findings_including_unicode_keys(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / ".claude-plugin"
    plugin.mkdir()
    plugin.joinpath("plugin.json").write_text(
        "{\n"
        '  "name": "saxo-bank-mcp",\n'
        '  "version": "0.1.0",\n'
        f'  "permi\\u0073sions": ["*{CANARY}*"]\n'
        "}\n",
        encoding="utf-8",
    )

    findings = wildcard_findings(tmp_path)

    assert findings == ("wildcard_grant",)
    assert CANARY not in "\n".join(findings)


def test_cache_dangerous_detects_data_saxo_token_cache_when_tracked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "data" / "saxo").mkdir(parents=True)
    (tmp_path / "data" / "saxo" / "token-cache.json").write_text("{}\n", encoding="utf-8")

    def tracked_paths(_root: Path) -> tuple[str, ...]:
        return ("data/saxo/token-cache.json",)

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_checks._tracked_public_paths",
        tracked_paths,
    )

    findings = cache_dangerous_findings(tmp_path)

    assert findings == ("cache_dangerous_public_file",)


@pytest.mark.parametrize(
    "failure",
    ["oserror", "timeout", "nonzero"],
)
def test_cache_dangerous_fails_closed_on_git_enumeration_errors(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        if failure == "oserror":
            raise OSError("git unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=30)
        completed = MagicMock()
        completed.returncode = 128
        completed.stdout = ""
        completed.stderr = "should-not-be-printed"
        return completed

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_static_gate_checks.subprocess.run",
        boom,
    )

    findings = cache_dangerous_findings(ROOT)

    assert findings == ("cache_public_path_scan_error",)


def test_official_link_inventory_rejects_allowlisted_prefix_unknown_path(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills" / "saxo-bank"
    skills.mkdir(parents=True)
    skills.joinpath("SKILL.md").write_text(
        "# x\n\n[broken](https://www.developer.saxo/openapi/learn/todo13-missing)\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "data" / "saxo"
    inventory.mkdir(parents=True)
    inventory.joinpath("official_skill_links.json").write_text(
        json.dumps({"urls": ["https://www.developer.saxo/openapi/learn/environments"]}),
        encoding="utf-8",
    )

    findings = link_findings(tmp_path)

    assert findings == ("unofficial_link",)


def test_malformed_skill_yaml_cli_never_echoes_canary(tmp_path: Path) -> None:
    skills = tmp_path / "skills" / "saxo-bank"
    skills.mkdir(parents=True)
    skills.joinpath("SKILL.md").write_text(
        f"---\nname: saxo-bank\ndescription: [{CANARY}\n---\n",
        encoding="utf-8",
    )
    for skill in (
        "saxo-auth-session",
        "saxo-openapi",
        "saxo-qa-operations",
        "saxo-reads",
        "saxo-safety-recovery",
        "saxo-streaming",
        "saxo-trading",
    ):
        path = tmp_path / "skills" / skill
        path.mkdir(parents=True)
        path.joinpath("SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: ok\n---\n",
            encoding="utf-8",
        )
    findings = frontmatter_findings(tmp_path)
    assert findings == ("bad_skill_frontmatter",)
    assert CANARY not in "\n".join(findings)

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test-fixture", "bad-skill-frontmatter"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode != 0
    assert "bad_skill_frontmatter" in combined
    assert CANARY not in combined


def test_static_gate_cli_check_exits_zero_and_prints_summary() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "status=passed" in completed.stdout
    assert f"skill_count={EXPECTED_SKILL_COUNT}" in completed.stdout
    assert "version_parity=true" in completed.stdout


def test_static_gate_result_serializes_without_sensitive_values() -> None:
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

    payload = json.loads(result.to_json())

    assert payload["status"] == "failed"
    assert payload["errors"] == ["manifest_version_drift", "fake_secret"]
    assert "token" not in result.to_json().lower()
