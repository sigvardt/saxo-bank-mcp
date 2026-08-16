#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/run_dual_harness_skill_evals.py --harness both --dry-run --out evals.json
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final, cast

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_eval_models import Harness, SkillEvalCase
from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions, run_eval_suite
from saxo_bank_mcp.agent_skill_install_qa import load_install_report_for_consumers

ANALYTICS_LOCAL_FIXTURE: Final = "analytics-research-to-precheck"
ANALYTICS_CASE_PATH: Final = Path("evals/saxo-analytics/research-to-precheck/case.yaml")
ANALYTICS_FIXTURE_PATH: Final = Path("evals/saxo-analytics/research-to-precheck/local-fixture.json")
BROKER_WRITE_TOOLS: Final = frozenset(
    {
        "saxo_commit_write_preview",
        "saxo_execute_trading_write",
        "saxo_place_order",
        "saxo_place_sim_order",
        "saxo_register_disclaimer_response",
    }
)
JSON_OBJECT_ADAPTER: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run matched Codex and Claude Saxo skill evals.")
    parser.add_argument("--harness", choices=("codex", "claude", "both"), required=True)
    parser.add_argument("--case-root", type=Path, default=Path("evals"))
    parser.add_argument("--case", dest="case_id", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--environment", choices=("LOCAL", "SIM", "LIVE"), default=None)
    parser.add_argument("--codex-plugin-root", type=Path, default=Path())
    parser.add_argument("--claude-plugin-root", type=Path, default=Path())
    parser.add_argument("--codex-home", type=Path, default=None)
    parser.add_argument("--claude-home", type=Path, default=None)
    parser.add_argument(
        "--source-codex-home",
        type=Path,
        default=None,
        help="Read-only Codex home used to seed disposable auth/config (not evidence).",
    )
    parser.add_argument(
        "--source-claude-home",
        type=Path,
        default=None,
        help="Read-only Claude home used to seed disposable auth/config (not evidence).",
    )
    parser.add_argument("--install-report", type=Path, default=None)
    parser.add_argument(
        "--credential-mode",
        choices=("none", "ephemeral-owner-only-copy"),
        default="none",
    )
    parser.add_argument(
        "--fixture",
        choices=(
            "after-send-timeout",
            "incomplete-evicted-ledger",
            "state-fingerprint-mismatch",
            ANALYTICS_LOCAL_FIXTURE,
        ),
        default=None,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--nonzero-on-skip", action="store_true", default=True)
    parser.add_argument("--expected-source-commit", default=None)
    parser.add_argument("--expected-router-source-sha256", default=None)
    parser.add_argument("--source-repo", type=Path, default=Path())
    parser.add_argument(
        "--harness-policy",
        choices=("dual_v1", "codex_native_v1"),
        default="dual_v1",
    )
    args = parser.parse_args(argv)
    if args.fixture is not None:
        if args.fixture == ANALYTICS_LOCAL_FIXTURE:
            return _run_analytics_local_fixture(
                harness=cast("str", args.harness),
                out=args.out,
            )
        return _write_failure_fixture(args.fixture, args.out)
    bound = _bind_install_report(args)
    if isinstance(bound, int):
        return bound
    codex_plugin_root, claude_plugin_root, codex_home, claude_home, expected_source_commit = bound
    return run_eval_suite(
        EvalRunOptions(
            harness=args.harness,
            case_id=args.case_id,
            tag=args.tag,
            environment=args.environment,
            case_root=args.case_root,
            codex_plugin_root=codex_plugin_root,
            claude_plugin_root=claude_plugin_root,
            codex_home=codex_home,
            claude_home=claude_home,
            out=args.out,
            dry_run=bool(args.dry_run),
            nonzero_on_skip=bool(args.nonzero_on_skip),
            expected_source_commit=expected_source_commit,
            expected_router_source_sha256=args.expected_router_source_sha256,
            source_repo=args.source_repo,
            install_report=args.install_report,
            credential_mode=str(args.credential_mode),
            source_codex_home=args.source_codex_home,
            source_claude_home=args.source_claude_home,
            harness_policy=args.harness_policy,
        ),
    )


def _bind_install_report(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path | None, Path | None, str | None] | int:
    codex_plugin_root = args.codex_plugin_root
    claude_plugin_root = args.claude_plugin_root
    codex_home = args.codex_home
    claude_home = args.claude_home
    expected_source_commit = args.expected_source_commit
    if args.install_report is None:
        return (
            codex_plugin_root,
            claude_plugin_root,
            codex_home,
            claude_home,
            expected_source_commit,
        )
    install, install_errors = load_install_report_for_consumers(args.install_report)
    if install is None:
        write_json(
            args.out,
            {
                "status": "failed",
                "reason": "install_report_not_verified",
                "errors": list(install_errors),
            },
        )
        return 1
    head = expected_source_commit or _git_head(args.source_repo)
    if head and install.candidate_commit != head:
        write_json(
            args.out,
            {
                "status": "failed",
                "reason": "install_candidate_commit_mismatch",
                "install_candidate_commit": install.candidate_commit,
                "expected_source_commit": head,
            },
        )
        return 1
    run_root = Path(install.fixture_cleanup.run_root)
    return (
        install.codex.cache_root,
        install.claude.cache_root,
        codex_home or (run_root / "codex-home"),
        claude_home or (run_root / "home"),
        head or install.candidate_commit,
    )


def _git_head(repo: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        return subprocess.check_output(
            [git, "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _write_failure_fixture(fixture: str, out: Path) -> int:
    payload = {
        "status": "failed",
        "fixture": fixture,
        "execution_mode": "fixture_validation",
        "mutation_calls": 1 if fixture == "after-send-timeout" else 0,
        "reconciliation_reads": 1 if fixture == "after-send-timeout" else 0,
        "blind_retries": 0,
        "negative_proof_available": False,
        "purchase_occurred": False,
    }
    write_json(out, payload)
    return 1


def _run_analytics_local_fixture(*, harness: str, out: Path) -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        case = SkillEvalCase.model_validate_json(
            (root / ANALYTICS_CASE_PATH).read_text(encoding="utf-8")
        )
        raw = JSON_OBJECT_ADAPTER.validate_json(
            (root / ANALYTICS_FIXTURE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        write_json(out, _analytics_fixture_failure("fixture_invalid"))
        return 1
    if raw.get("case_id") != case.id:
        write_json(out, _analytics_fixture_failure("fixture_binding_invalid"))
        return 1
    responses = raw.get("responses")
    if not isinstance(responses, dict):
        write_json(out, _analytics_fixture_failure("fixture_responses_invalid"))
        return 1

    selected: tuple[Harness, ...] = (
        ("codex", "claude") if harness == "both" else (cast("Harness", harness),)
    )
    records: list[dict[str, JsonValue]] = []
    for client in selected:
        response = responses.get(client)
        passed, invoked = _analytics_fixture_response_passes(case, client, response)
        records.append(
            {
                "case_id": case.id,
                "harness": client,
                "status": "passed" if passed else "failed",
                "expected_skill": case.expected_skill,
                "invoked_logical_tools": list(invoked),
                "assertions_passed": passed,
            }
        )
    passed = bool(records) and all(record["status"] == "passed" for record in records)
    write_json(
        out,
        {
            "status": "passed" if passed else "failed",
            "fixture": ANALYTICS_LOCAL_FIXTURE,
            "execution_mode": "local_fixture_validation",
            "case_count": len(records),
            "records": records,
            "external_calls": 0,
            "broker_writes": 0,
            "disclaimer_responses": 0,
            "raw_transcripts_persisted": 0,
        },
    )
    return 0 if passed else 1


def _analytics_fixture_response_passes(
    case: SkillEvalCase,
    harness: Harness,
    response: JsonValue,
) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(response, dict):
        return False, ()
    text = response.get("text")
    invoked_value = response.get("invoked_logical_tools")
    if not isinstance(text, str) or not isinstance(invoked_value, list):
        return False, ()
    if not all(isinstance(item, str) for item in invoked_value):
        return False, ()
    invoked = tuple(cast("list[str]", invoked_value))
    lowered = text.lower()
    assertions = case.transcript_assertions
    required_all = all(phrase.lower() in lowered for phrase in assertions.required_all)
    required_any = not assertions.required_any or any(
        phrase.lower() in lowered for phrase in assertions.required_any
    )
    forbidden_clear = all(phrase.lower() not in lowered for phrase in assertions.forbidden)
    grants = frozenset(case.exact_tool_grants[harness])
    invoked_set = frozenset(invoked)
    tools_valid = (
        frozenset(case.required_logical_tools) <= invoked_set <= grants
        and invoked_set.isdisjoint(case.forbidden_logical_tools)
        and invoked_set.isdisjoint(BROKER_WRITE_TOOLS)
    )
    return required_all and required_any and forbidden_clear and tools_valid, invoked


def _analytics_fixture_failure(reason: str) -> dict[str, JsonValue]:
    return {
        "status": "failed",
        "fixture": ANALYTICS_LOCAL_FIXTURE,
        "execution_mode": "local_fixture_validation",
        "reason": reason,
        "case_count": 0,
        "records": [],
        "external_calls": 0,
        "broker_writes": 0,
        "disclaimer_responses": 0,
        "raw_transcripts_persisted": 0,
    }


if __name__ == "__main__":
    sys.exit(main())
