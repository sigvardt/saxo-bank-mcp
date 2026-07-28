from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_skill_eval_models import (
    EXPECTED_SKILLS,
    load_eval_cases,
    load_scenario_tools,
)
from saxo_bank_mcp.agent_skill_eval_runner import resolve_tool_grants
from saxo_bank_mcp.agent_skill_eval_validation import validate_eval_suite

ROOT: Final = Path(__file__).resolve().parents[1]
MIN_CASE_COUNT: Final = 10
EXPECTED_TOOL_COUNT: Final = 39
QA_DRY_RUN_RECORDS: Final = 2
EXPECTED_ROUTER_CASES: Final = frozenset(
    {
        "router-ambiguous-environment",
        "router-approval-bypass",
        "router-auth",
        "router-choose-best",
        "router-incident-recovery",
        "router-live-trade",
        "router-qa",
        "router-read-then-trade",
        "router-sim-trade",
        "router-streaming",
        "router-unsupported-operation",
    },
)
VALIDATOR: Final = ROOT / "scripts/validate_agent_skill_evals.py"
DUAL_RUNNER: Final = ROOT / "scripts/run_dual_harness_skill_evals.py"
MATRIX_RUNNER: Final = ROOT / "scripts/run_mcp_tool_matrix.py"
INSTALL_QA: Final = ROOT / "scripts/qa_dual_plugin_install.py"
RELEASE_ASSEMBLER: Final = ROOT / "scripts/assemble_agent_skill_release.py"


def test_eval_suite_covers_all_tools_and_skills() -> None:
    cases = load_eval_cases(ROOT / "evals/saxo-bank")
    tools = load_scenario_tools(ROOT)
    result = validate_eval_suite(root=ROOT, case_root=ROOT / "evals/saxo-bank")

    used_tools = {
        tool
        for case in cases
        for tool in (
            *case.required_logical_tools,
            *case.forbidden_logical_tools,
            *(member for group in case.required_tool_groups for member in group),
            *case.exact_tool_grants.get("codex", ()),
            *case.exact_tool_grants.get("claude", ()),
        )
    }
    positive_skills = {case.expected_skill for case in cases}

    assert result.status == "passed", result.errors
    assert len(cases) >= MIN_CASE_COUNT
    assert len(tools) == EXPECTED_TOOL_COUNT
    assert used_tools == tools
    assert positive_skills >= EXPECTED_SKILLS


def test_eval_cases_forbid_broad_grants_and_live_mutations() -> None:
    cases = load_eval_cases(ROOT / "evals/saxo-bank")

    for case in cases:
        assert "*" not in json.dumps(case.exact_tool_grants, sort_keys=True)
        assert not case.natural_prompt.startswith(("$saxo-bank-mcp:", "/saxo-bank-mcp:"))
        if case.environment == "LIVE":
            live_grants = set(case.exact_tool_grants["codex"]) | set(
                case.exact_tool_grants["claude"]
            )
            assert "saxo_place_order" not in live_grants
            assert "saxo_execute_trading_write" not in live_grants


def test_router_eval_cases_are_structured_plan_only_and_tool_free() -> None:
    # Given: router-proof cases loaded through the production parser.
    cases = tuple(
        case
        for case in load_eval_cases(ROOT / "evals/saxo-bank")
        if "router-proof" in case.tags
    )

    # When: their structured expectations and grants are inspected.
    case_ids = frozenset(case.id for case in cases)

    # Then: every required route has one matched, tool-free expectation.
    assert case_ids == EXPECTED_ROUTER_CASES
    for case in cases:
        expectation = case.router_expectation
        assert expectation is not None
        assert not case.required_logical_tools
        assert not case.exact_tool_grants["codex"]
        assert not case.exact_tool_grants["claude"]
        assert case.harness_prompts["codex"] == case.harness_prompts["claude"]
        assert expectation.evidence_need
        if expectation.primary_skill is None:
            assert case.expected_skill == "saxo-bank"
        else:
            assert case.expected_skill == expectation.primary_skill


def test_exact_tool_permission_resolution_has_no_wildcards() -> None:
    grants = resolve_tool_grants("claude", ("saxo_health", "saxo_auth_status"))

    assert grants == (
        "mcp__saxo-bank-mcp__saxo_auth_status",
        "mcp__saxo-bank-mcp__saxo_health",
    )
    assert all("*" not in grant for grant in grants)


def test_validator_failure_fixtures_are_sanitized() -> None:
    fixtures = (
        "missing-expected-skill",
        "missing-cleanup",
        "wildcard-grant",
        "unbounded-timeout",
        "stale-expected-tool",
        "malformed-live-write",
        "harness-prefix-prompt",
    )

    for fixture in fixtures:
        result = _run(VALIDATOR, "--self-test-fixture", fixture)
        assert result.returncode != 0
        assert "CANARY" not in result.stdout + result.stderr


def test_dry_run_dual_runner_records_no_model_mcp_or_saxo_calls(tmp_path: Path) -> None:
    out = tmp_path / "evals.json"

    result = _run(DUAL_RUNNER, "--harness", "both", "--tag", "qa", "--dry-run", "--out", str(out))
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stderr
    assert payload["status"] == "planned"
    assert payload["execution_mode"] == "manifest_validation"
    assert payload["case_count"] == QA_DRY_RUN_RECORDS
    assert {record["status"] for record in payload["records"]} == {"planned"}
    assert {record["no_model_call"] for record in payload["records"]} == {True}
    assert {record["no_mcp_call"] for record in payload["records"]} == {True}
    assert {record["no_saxo_call"] for record in payload["records"]} == {True}


def test_supporting_cli_fixtures_and_verify_modes(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.json"
    install = tmp_path / "install.json"
    release = tmp_path / "manifest.json"
    latest = tmp_path / "latest.json"
    codex_global = tmp_path / "codex-global"
    claude_global = tmp_path / "claude-global"
    codex_global.mkdir()
    claude_global.mkdir()

    assert (
        _run(
            INSTALL_QA,
            "--repo",
            ".",
            "--commit",
            "HEAD",
            "--run-root",
            str(tmp_path / "install-root"),
            "--codex-global-home",
            str(codex_global),
            "--claude-global-home",
            str(claude_global),
            "--dry-run",
            "--out",
            str(install),
        ).returncode
        == 0
    )
    assert json.loads(install.read_text(encoding="utf-8"))["status"] == "planned"
    assert _run(MATRIX_RUNNER, "--out", str(matrix)).returncode != 0
    assert json.loads(matrix.read_text(encoding="utf-8"))["reason"] == "missing_install_report"
    assert (
        _run(
            RELEASE_ASSEMBLER,
            "--evidence-root",
            str(tmp_path / "empty"),
            "--release",
            "release-v1",
            "--source-commit",
            "HEAD",
            "--out",
            str(release),
            "--latest",
            str(latest),
        ).returncode
        != 0
    )
    assert not latest.exists()
    assert (
        _run(
            INSTALL_QA, "--self-test-fixture", "private-file", "--out", str(tmp_path / "bad.json")
        ).returncode
        != 0
    )


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
