from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

import yaml
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_catalog_render import catalog_inputs
from saxo_bank_mcp.agent_skill_eval_models import SkillEvalCase
from saxo_bank_mcp.agent_skill_eval_validation import validate_eval_suite
from saxo_bank_mcp.agent_skill_install_models import EXPECTED_TOOLS
from saxo_bank_mcp.agent_skill_install_qa import (
    EXPECTED_SKILL_COUNT as INSTALL_SKILL_COUNT,
)
from saxo_bank_mcp.agent_skill_install_qa import (
    EXPECTED_TOOL_COUNT as INSTALL_TOOL_COUNT,
)
from saxo_bank_mcp.agent_skill_matrix import EXPECTED_TOOL_COUNT as MATRIX_TOOL_COUNT
from saxo_bank_mcp.server_tool_ids import (
    ALL_LOGICAL_TOOL_IDS,
    ANALYTICS_TOOL_IDS,
    EXPECTED_TOOL_COUNT,
)

ROOT: Final = Path(__file__).resolve().parents[1]
MAX_SKILL_LINES: Final = 500
EXPECTED_SKILL_COUNT: Final = 9
EXPECTED_RUNTIME_TOOL_COUNT: Final = 60
EXPECTED_DUAL_RECORD_COUNT: Final = 2
EXPECTED_EVAL_CASE_COUNT: Final = 34
SKILL_ROOT: Final = ROOT / "skills/saxo-analytics"
SKILL_PATH: Final = SKILL_ROOT / "SKILL.md"
OPENAI_PATH: Final = SKILL_ROOT / "agents/openai.yaml"
REFERENCE_NAMES: Final = frozenset(
    {
        "analytics-workflows.md",
        "correctness-and-interpretation.md",
        "privacy-and-storage.md",
        "research-to-trade.md",
    }
)
CASE_ROOT: Final = ROOT / "evals/saxo-analytics"
EXPECTED_CASE_IDS: Final = frozenset(
    {
        "artifact-delivery",
        "backtest-limitations",
        "cost-xray",
        "deletion",
        "market-comparison",
        "optimization",
        "options",
        "portfolio-briefing",
        "research-to-precheck",
        "scenario",
    }
)
BROKER_WRITE_TOOLS: Final = frozenset(
    {
        "saxo_commit_write_preview",
        "saxo_execute_trading_write",
        "saxo_place_order",
        "saxo_place_sim_order",
        "saxo_register_disclaimer_response",
    }
)
JSON_ADAPTER: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
STRING_MAPPING_ADAPTER: Final[TypeAdapter[dict[str, str]]] = TypeAdapter(dict[str, str])
OPENAI_ADAPTER: Final[TypeAdapter[dict[str, dict[str, str]]]] = TypeAdapter(
    dict[str, dict[str, str]]
)


def test_saxo_analytics_skill_is_portable_and_matched_for_both_clients(
    tmp_path: Path,
) -> None:
    assert SKILL_PATH.is_file()
    assert OPENAI_PATH.is_file()
    assert _relative_files(SKILL_ROOT) == {
        "SKILL.md",
        "agents/openai.yaml",
        *(f"references/{name}" for name in REFERENCE_NAMES),
    }

    metadata, body = _parse_skill(SKILL_PATH)
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "saxo-analytics"
    assert {"analytics", "portfolio", "scenario", "backtest"} <= set(
        re.findall(r"[a-z]+", metadata["description"].lower())
    )
    assert len((metadata["name"] + body).splitlines()) < MAX_SKILL_LINES

    openai = OPENAI_ADAPTER.validate_python(yaml.safe_load(OPENAI_PATH.read_text(encoding="utf-8")))
    assert set(openai) == {"interface"}
    interface = openai["interface"]
    assert isinstance(interface, dict)
    assert set(interface) == {"display_name", "short_description", "default_prompt"}
    assert "$saxo-bank-mcp:saxo-analytics" in str(interface["default_prompt"])

    codex_skills = tmp_path / "codex-plugin/skills"
    claude_skills = tmp_path / "claude-plugin/skills"
    shutil.copytree(ROOT / "skills", codex_skills)
    shutil.copytree(ROOT / "skills", claude_skills)
    assert len(tuple(codex_skills.glob("*/SKILL.md"))) == EXPECTED_SKILL_COUNT
    assert len(tuple(claude_skills.glob("*/SKILL.md"))) == EXPECTED_SKILL_COUNT
    codex = codex_skills / "saxo-analytics"
    claude = claude_skills / "saxo-analytics"
    assert _tree_bytes(codex) == _tree_bytes(claude) == _tree_bytes(SKILL_ROOT)

    project_version = _project_version()
    assert _json(ROOT / ".codex-plugin/plugin.json")["version"] == project_version
    assert _json(ROOT / ".claude-plugin/plugin.json")["version"] == project_version
    assert _marketplace_version(ROOT / ".agents/plugins/marketplace.json") == project_version
    assert _marketplace_version(ROOT / ".claude-plugin/marketplace.json") == project_version


def test_saxo_analytics_references_are_flat_resolved_and_official() -> None:
    references = SKILL_ROOT / "references"
    assert references.is_dir()
    assert {path.name for path in references.iterdir() if path.is_file()} == REFERENCE_NAMES
    assert not any(path.is_dir() for path in references.iterdir())

    allowed_urls = _official_urls()
    for source in sorted(SKILL_ROOT.rglob("*.md")):
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", source.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://")):
                assert target in allowed_urls
            else:
                resolved = (source.parent / target).resolve()
                assert resolved.is_relative_to(SKILL_ROOT.resolve())
                assert resolved.is_file()


def test_generated_analytics_catalog_matches_the_registered_sixty_tools() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md"))
    )
    workflow = (SKILL_ROOT / "references/analytics-workflows.md").read_text(encoding="utf-8")
    catalog = (ROOT / "skills/saxo-bank/references/tool-catalog.md").read_text(encoding="utf-8")

    assert EXPECTED_TOOL_COUNT == EXPECTED_RUNTIME_TOOL_COUNT
    assert set(ANALYTICS_TOOL_IDS) <= set(re.findall(r"`(saxo_[a-z0-9_]+)`", combined))
    table_tools = frozenset(
        match.group(1)
        for line in workflow.splitlines()
        if (match := re.match(r"\| `(saxo_[a-z0-9_]+)` \|", line))
    )
    assert table_tools == frozenset(ANALYTICS_TOOL_IDS)
    assert all(f"| {tool_id} | saxo-analytics |" in catalog for tool_id in ANALYTICS_TOOL_IDS)

    completed = subprocess.run(
        [sys.executable, "scripts/generate_agent_skill_catalogs.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "tool_count=60" in completed.stdout
    assert "skill_count=9" in completed.stdout
    assert "analytics_tool_count=21" in completed.stdout
    assert "analytics_scenarios=10" in completed.stdout


def test_shared_release_truth_validates_all_nine_skills_and_sixty_tools() -> None:
    inputs = catalog_inputs(ROOT)
    evaluation = validate_eval_suite(root=ROOT, case_root=ROOT / "evals")

    assert len(inputs.tools) == EXPECTED_RUNTIME_TOOL_COUNT
    assert INSTALL_SKILL_COUNT == EXPECTED_SKILL_COUNT
    assert INSTALL_TOOL_COUNT == EXPECTED_TOOLS == MATRIX_TOOL_COUNT == EXPECTED_RUNTIME_TOOL_COUNT
    assert evaluation.status == "passed", evaluation.errors
    assert evaluation.skill_count == EXPECTED_SKILL_COUNT
    assert evaluation.tool_count == EXPECTED_RUNTIME_TOOL_COUNT
    assert evaluation.case_count == EXPECTED_EVAL_CASE_COUNT


def test_router_selects_analytics_and_keeps_trade_as_an_explicit_follow_on() -> None:
    router = (ROOT / "skills/saxo-bank/SKILL.md").read_text(encoding="utf-8")
    contract = (ROOT / "skills/saxo-bank/references/router-contract.md").read_text(encoding="utf-8")
    combined = f"{router}\n{contract}".lower()

    assert "[saxo-analytics](../saxo-analytics/skill.md)" in router.lower()
    assert "portfolio" in combined
    assert "cost x-ray" in combined
    assert "backtest" in combined
    assert "saxo-analytics first" in combined
    assert "saxo_propose_trade_from_analysis" in combined
    assert "separate explicit follow-on" in combined
    assert "stop before" in combined
    assert "broker write" in combined


def test_skill_requires_proof_state_privacy_and_recovery_without_replacement_math() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md"))
    ).lower()

    assert "analysis_id" in combined
    assert {"verified", "degraded", "refused"} <= set(re.findall(r"[a-z_]+", combined))
    assert "replacement math" in combined
    assert "owner context" in combined
    assert "quality warnings" in combined
    assert "no disclaimer response" in combined
    for recovery in (
        "ambiguity",
        "entitlement gap",
        "stale data",
        "schema quarantine",
        "large job",
        "expired handle",
        "deletion-preview expiry",
    ):
        assert recovery in combined


def test_ten_matched_hard_tasks_cover_every_analytics_tool_and_stop_before_write() -> None:
    case_files = tuple(sorted(CASE_ROOT.glob("*/case.yaml")))
    assert {path.parent.name for path in case_files} == EXPECTED_CASE_IDS
    cases = tuple(_case(path) for path in case_files)
    assert {case.id for case in cases} == EXPECTED_CASE_IDS

    covered: set[str] = set()
    for case in cases:
        assert case.expected_skill == "saxo-analytics"
        assert case.harness_prompts == {
            "codex": case.natural_prompt,
            "claude": case.natural_prompt,
        }
        assert case.exact_tool_grants["codex"] == case.exact_tool_grants["claude"]
        assert not any("*" in grant or "mcp__plugin_" in grant for grant in _grants(case))
        assert set(case.required_logical_tools) <= _grants(case)
        assert _grants(case) <= set(ANALYTICS_TOOL_IDS)
        assert set(case.required_logical_tools) | set(case.forbidden_logical_tools) <= set(
            ALL_LOGICAL_TOOL_IDS
        )
        assert "analysis_id" in case.transcript_assertions.required_all
        assert {"verified", "degraded", "refused"} <= set(case.transcript_assertions.required_any)
        assert "replacement math" in case.transcript_assertions.forbidden
        covered.update(case.required_logical_tools)

    assert covered == set(ANALYTICS_TOOL_IDS)
    precheck = next(case for case in cases if case.id == "research-to-precheck")
    assert "saxo_propose_trade_from_analysis" in precheck.required_logical_tools
    assert set(precheck.forbidden_logical_tools) >= BROKER_WRITE_TOOLS
    assert BROKER_WRITE_TOOLS.isdisjoint(_grants(precheck))
    assert "stop before broker write" in precheck.transcript_assertions.required_all


def test_local_fixture_dual_evaluation_is_matched_and_has_zero_external_activity(
    tmp_path: Path,
) -> None:
    report = tmp_path / "analytics-local-fixture.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_dual_harness_skill_evals.py",
            "--harness",
            "both",
            "--fixture",
            "analytics-research-to-precheck",
            "--out",
            str(report),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = _json(report)
    assert payload["status"] == "passed"
    assert payload["execution_mode"] == "local_fixture_validation"
    assert payload["case_count"] == EXPECTED_DUAL_RECORD_COUNT
    assert payload["external_calls"] == 0
    assert payload["broker_writes"] == 0
    assert payload["disclaimer_responses"] == 0
    records = payload["records"]
    assert isinstance(records, list)
    harnesses: set[str] = set()
    for record in records:
        assert isinstance(record, dict)
        harness = record.get("harness")
        assert isinstance(harness, str)
        harnesses.add(harness)
    assert harnesses == {"codex", "claude"}
    assert all(record.get("status") == "passed" for record in records if isinstance(record, dict))


def test_ci_checks_analytics_skill_generation_install_parity_and_local_dual_fixture() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "tests/test_saxo_analytics_skill.py" in ci
    assert "skills/saxo-analytics" in ci
    assert "generate_agent_skill_catalogs.py --check" in ci
    assert "analytics-research-to-precheck" in ci
    assert "--harness both" in ci
    assert "Analytics dual packaging and version parity" in ci


def test_public_skill_and_eval_text_contains_no_private_or_secret_shaped_values() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for base in (SKILL_ROOT, CASE_ROOT)
        for path in sorted(base.rglob("*"))
        if path.is_file()
    )
    lowered = text.lower()

    assert not re.search(r"/(?:users|home|volumes)/[^\s]+", lowered)
    assert not re.search(r"\b\d{8,12}\b", text)
    assert not re.search(r"(?i)bearer\s+[a-z0-9._-]+", text)
    assert not re.search(r"(?i)(?:access|refresh)[_-]?token\s*[:=]\s*\S+", text)
    assert "raw account id" in lowered
    assert "private values" in lowered


def _parse_skill(path: Path) -> tuple[dict[str, str], str]:
    opening, frontmatter, body = path.read_text(encoding="utf-8").split("---", maxsplit=2)
    assert not opening.strip()
    metadata = STRING_MAPPING_ADAPTER.validate_python(yaml.safe_load(frontmatter))
    return metadata, body


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _json(path: Path) -> dict[str, JsonValue]:
    return JSON_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def _project_version() -> str:
    payload = JSON_ADAPTER.validate_python(
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    )
    project = payload.get("project")
    assert isinstance(project, dict)
    version = project.get("version")
    assert isinstance(version, str)
    return version


def _marketplace_version(path: Path) -> JsonValue:
    plugins = _json(path)["plugins"]
    assert isinstance(plugins, list)
    assert len(plugins) == 1
    plugin = plugins[0]
    assert isinstance(plugin, dict)
    return plugin["version"]


def _official_urls() -> frozenset[str]:
    urls = _json(ROOT / "data/saxo/official_skill_links.json")["urls"]
    assert isinstance(urls, list)
    assert all(isinstance(url, str) for url in urls)
    return frozenset(str(url) for url in urls)


def _case(path: Path) -> SkillEvalCase:
    return SkillEvalCase.model_validate_json(path.read_text(encoding="utf-8"))


def _grants(case: SkillEvalCase) -> set[str]:
    return {tool for grants in case.exact_tool_grants.values() for tool in grants}
