#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/run_codex_skill_evals.py --dry-run --out /tmp/codex-evals.json
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions, run_eval_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Codex Saxo skill eval cases.")
    parser.add_argument("--case-root", type=Path, default=Path("evals/saxo-bank"))
    parser.add_argument("--case", dest="case_id", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--environment", default=None)
    parser.add_argument("--codex-plugin-root", type=Path, default=Path())
    parser.add_argument("--codex-home", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--nonzero-on-skip", action="store_true", default=True)
    args = parser.parse_args(argv)
    return run_eval_suite(
        EvalRunOptions(
            harness="codex",
            case_id=args.case_id,
            tag=args.tag,
            environment=args.environment,
            case_root=args.case_root,
            codex_plugin_root=args.codex_plugin_root,
            claude_plugin_root=Path(),
            codex_home=args.codex_home,
            claude_home=None,
            out=args.out,
            dry_run=bool(args.dry_run),
            nonzero_on_skip=bool(args.nonzero_on_skip),
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
