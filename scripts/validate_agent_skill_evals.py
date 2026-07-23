#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/validate_agent_skill_evals.py --all
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_eval_validation import (
    self_test_fixture_errors,
    validate_eval_suite,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Saxo agent skill eval cases.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--case-root", type=Path, default=Path("evals/saxo-bank"))
    parser.add_argument(
        "--self-test-fixture",
        choices=(
            "missing-expected-skill",
            "missing-cleanup",
            "wildcard-grant",
            "unbounded-timeout",
            "stale-expected-tool",
            "malformed-live-write",
            "harness-prefix-prompt",
        ),
    )
    args = parser.parse_args(argv)
    if args.self_test_fixture is not None:
        errors = self_test_fixture_errors(str(args.self_test_fixture))
        for error in errors:
            sys.stderr.write(f"{error}\n")
        return 1 if errors else 0
    result = validate_eval_suite(case_root=args.case_root)
    sys.stdout.write(result.line() + "\n")
    for error in result.errors:
        sys.stderr.write(f"{error}\n")
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
