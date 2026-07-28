#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/run_dual_harness_skill_evals.py --harness both --dry-run --out evals.json
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from saxo_bank_mcp._evidence import write_json
from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions, run_eval_suite
from saxo_bank_mcp.agent_skill_install_qa import load_install_report_for_consumers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run matched Codex and Claude Saxo skill evals.")
    parser.add_argument("--harness", choices=("codex", "claude", "both"), required=True)
    parser.add_argument("--case-root", type=Path, default=Path("evals/saxo-bank"))
    parser.add_argument("--case", dest="case_id", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--environment", choices=("LOCAL", "SIM", "LIVE"), default=None)
    parser.add_argument("--codex-plugin-root", type=Path, default=Path())
    parser.add_argument("--claude-plugin-root", type=Path, default=Path())
    parser.add_argument("--codex-home", type=Path, default=None)
    parser.add_argument("--claude-home", type=Path, default=None)
    parser.add_argument("--install-report", type=Path, default=None)
    parser.add_argument("--credential-mode", default="none")
    parser.add_argument(
        "--fixture",
        choices=("after-send-timeout", "incomplete-evicted-ledger", "state-fingerprint-mismatch"),
        default=None,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--nonzero-on-skip", action="store_true", default=True)
    parser.add_argument("--expected-source-commit", default=None)
    parser.add_argument("--expected-router-source-sha256", default=None)
    parser.add_argument("--source-repo", type=Path, default=Path())
    args = parser.parse_args(argv)
    if args.fixture is not None:
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


if __name__ == "__main__":
    sys.exit(main())
