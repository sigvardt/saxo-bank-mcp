#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/run_agent_skill_static_gates.py --check
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_static_gates import (
    FAILURE_FIXTURES,
    StaticGateResult,
    run_static_gates,
    self_test_fixture_errors,
)

CHECK_CHOICES = (
    "all",
    "version-parity",
    "wildcard",
    "links",
    "frontmatter",
    "nested-references",
    "cache-dangerous",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run dual-harness Saxo skill static gates.")
    parser.add_argument("--check", nargs="?", const="all", choices=CHECK_CHOICES)
    parser.add_argument(
        "--self-test-fixture",
        choices=sorted(FAILURE_FIXTURES),
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if args.self_test_fixture is not None:
        errors = self_test_fixture_errors(str(args.self_test_fixture), root=root)
        for error in errors:
            sys.stderr.write(f"{error}\n")
        return 1 if errors else 0
    if args.check is None:
        parser.error("one of --check or --self-test-fixture is required")
    result = run_static_gates(root)
    sys.stdout.write(result.line() + "\n")
    selected = _selected_errors(result, str(args.check))
    for error in selected:
        sys.stderr.write(f"{error}\n")
    if args.check == "all":
        return 0 if result.status == "passed" else 1
    return 0 if not selected else 1


def _selected_errors(result: StaticGateResult, check: str) -> tuple[str, ...]:
    mapping: dict[str, tuple[str, ...]] = {
        "all": result.errors,
        "version-parity": (() if result.version_parity else ("manifest_version_drift",)),
        "wildcard": result.wildcard_findings,
        "links": result.link_findings,
        "frontmatter": result.frontmatter_findings,
        "nested-references": result.nested_reference_findings,
        "cache-dangerous": result.cache_dangerous_findings,
    }
    return mapping[check]


if __name__ == "__main__":
    sys.exit(main())
