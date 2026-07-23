#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/run_mcp_tool_matrix.py --environment SIM --out matrix.json
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_matrix import build_manifest_matrix_report, verify_matrix_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or verify the Saxo MCP tool matrix.")
    parser.add_argument(
        "--manifest", type=Path, default=Path("data/saxo/agent_tool_scenarios.json")
    )
    parser.add_argument("--environment", default="SIM")
    parser.add_argument("--require-tools", type=int, default=39)
    parser.add_argument("--install-report", type=Path, default=None)
    parser.add_argument("--fixture-stock-uic", default=None)
    parser.add_argument("--fixture-amount", default=None)
    parser.add_argument("--fixture-limit-price", default=None)
    parser.add_argument("--fixture-modified-limit-price", default=None)
    parser.add_argument("--fixture-option-uics", default=None)
    parser.add_argument("--fixture-stream-uic", default=None)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--require-environment", default="SIM")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.verify_only:
        if args.report is None:
            sys.stderr.write("missing --report for --verify-only\n")
            return 1
        return verify_matrix_report(
            report_path=args.report,
            require_environment=str(args.require_environment),
            out=args.out,
        )
    return build_manifest_matrix_report(
        manifest=args.manifest,
        environment=str(args.environment),
        require_tools=int(args.require_tools),
        out=args.out,
    )


if __name__ == "__main__":
    sys.exit(main())
