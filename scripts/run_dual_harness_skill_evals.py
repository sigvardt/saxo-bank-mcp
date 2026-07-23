#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/run_dual_harness_skill_evals.py --harness both --dry-run --out evals.json
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp._evidence import write_json
from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions, run_eval_suite


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
    args = parser.parse_args(argv)
    if args.fixture is not None:
        return _write_failure_fixture(args.fixture, args.out)
    return run_eval_suite(
        EvalRunOptions(
            harness=args.harness,
            case_id=args.case_id,
            tag=args.tag,
            environment=args.environment,
            case_root=args.case_root,
            codex_plugin_root=args.codex_plugin_root,
            claude_plugin_root=args.claude_plugin_root,
            codex_home=args.codex_home,
            claude_home=args.claude_home,
            out=args.out,
            dry_run=bool(args.dry_run),
            nonzero_on_skip=bool(args.nonzero_on_skip),
        ),
    )


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
