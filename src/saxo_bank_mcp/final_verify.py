from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.final_verify_code import CODE_REQUIRED_PATHS
from saxo_bank_mcp.final_verify_code import verify_code as _verify_code
from saxo_bank_mcp.final_verify_mcp import (
    MCP_ALLOWED_STATUSES,
    MCP_REQUIRED_EVIDENCE,
)
from saxo_bank_mcp.final_verify_mcp import (
    verify_mcp as _verify_mcp,
)
from saxo_bank_mcp.final_verify_plan import PLAN_MARKERS
from saxo_bank_mcp.final_verify_plan import verify_plan as _verify_plan
from saxo_bank_mcp.final_verify_scope import (
    SCOPE_REQUIRED_PATHS,
    registry_tool_ids,
    run_scope_tribunal_index,
)
from saxo_bank_mcp.final_verify_scope import (
    verify_scope as _verify_scope,
)
from saxo_bank_mcp.loop_manifest import current_git_state

__all__ = (
    "CODE_REQUIRED_PATHS",
    "MCP_ALLOWED_STATUSES",
    "MCP_REQUIRED_EVIDENCE",
    "PLAN_MARKERS",
    "SCOPE_REQUIRED_PATHS",
    "build_parser",
    "current_git_state",
    "main",
    "registry_tool_ids",
    "run_scope_tribunal_index",
    "verify_code",
    "verify_mcp",
    "verify_plan",
    "verify_scope",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run final verification gates.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--out", type=Path, required=True)

    for name in ("code", "mcp", "scope"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--out", type=Path, required=True)
    return parser


def verify_plan(plan_path: Path, out: Path) -> int:
    return _verify_plan(plan_path, out, current_git_state)


def verify_code(out: Path) -> int:
    return _verify_code(out, current_git_state)


def verify_mcp(out: Path) -> int:
    return _verify_mcp(out, current_git_state)


def verify_scope(out: Path) -> int:
    return _verify_scope(out, current_git_state)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = str(args.command)
    match command:
        case "plan":
            return verify_plan(args.plan, args.out)
        case "code":
            return verify_code(args.out)
        case "mcp":
            return verify_mcp(args.out)
        case "scope":
            return verify_scope(args.out)
        case unreachable:
            raise AssertionError(unreachable)


if __name__ == "__main__":
    sys.exit(main())
