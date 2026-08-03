#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/generate_agent_skill_catalogs.py --check
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Final

from saxo_bank_mcp.agent_catalog_fixtures import self_test_fixture
from saxo_bank_mcp.agent_catalog_render import generate_catalogs
from saxo_bank_mcp.agent_catalog_runtime import (
    CatalogValidationError,
    runtime_tools,
)
from saxo_bank_mcp.server_tool_ids import (
    ALL_LOGICAL_TOOL_IDS,
    ANALYTICS_TOOL_IDS,
    EXPECTED_TOOL_COUNT,
)

ROOT: Final = Path(__file__).resolve().parents[1]
ANALYTICS_WORKFLOW_PATH: Final = Path("skills/saxo-analytics/references/analytics-workflows.md")
ANALYTICS_CASE_ROOT: Final = Path("evals/saxo-analytics")
EXPECTED_ANALYTICS_SCENARIOS: Final = 10
EXPECTED_ANALYTICS_TOOL_COUNT: Final = 21
EXPECTED_SKILL_COUNT: Final = 9
_TOOL_ROW_PATTERN: Final = re.compile(r"^\| `(saxo_[a-z0-9_]+)` \|", re.MULTILINE)


class Task22CatalogError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Saxo agent skill catalogs.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--self-test-fixture",
        choices=(
            "unknown-tool",
            "stale-operation",
            "missing-status",
            "unclassified-status-module",
            "uncovered-tool",
        ),
    )
    parser.add_argument("ignored", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.self_test_fixture is not None:
            self_test_fixture(ROOT, str(args.self_test_fixture))
            return 1
        summary = _generate_task22_catalog(check=bool(args.check))
    except json.JSONDecodeError:
        sys.stderr.write("malformed_json: source\n")
        return 1
    except CatalogValidationError as error:
        sys.stderr.write(error.message() + "\n")
        return 1
    except Task22CatalogError as error:
        sys.stderr.write(f"analytics_catalog_invalid: {error}\n")
        return 1
    sys.stdout.write(summary + "\n")
    return 0


def _generate_task22_catalog(*, check: bool) -> str:
    tools = runtime_tools()
    names = frozenset(tool.name for tool in tools)
    if len(tools) != EXPECTED_TOOL_COUNT or names != ALL_LOGICAL_TOOL_IDS:
        raise Task22CatalogError("runtime_tool_set")
    if (
        len(ANALYTICS_TOOL_IDS) != EXPECTED_ANALYTICS_TOOL_COUNT
        or not frozenset(ANALYTICS_TOOL_IDS) <= names
    ):
        raise Task22CatalogError("analytics_tool_set")
    skill_count = len(tuple((ROOT / "skills").glob("*/SKILL.md")))
    if skill_count != EXPECTED_SKILL_COUNT:
        raise Task22CatalogError("skill_package_count")

    workflow = (ROOT / ANALYTICS_WORKFLOW_PATH).read_text(encoding="utf-8")
    workflow_tools = frozenset(_TOOL_ROW_PATTERN.findall(workflow))
    if workflow_tools != frozenset(ANALYTICS_TOOL_IDS):
        raise Task22CatalogError("analytics_workflow_tool_set")
    case_files = tuple(sorted((ROOT / ANALYTICS_CASE_ROOT).glob("*/case.yaml")))
    if len(case_files) != EXPECTED_ANALYTICS_SCENARIOS:
        raise Task22CatalogError("analytics_scenario_count")

    summary = generate_catalogs(ROOT, check=check)
    return (
        f"{summary.line()} "
        f"skill_count={skill_count} "
        f"analytics_tool_count={len(ANALYTICS_TOOL_IDS)} "
        f"analytics_scenarios={len(case_files)}"
    )


if __name__ == "__main__":
    sys.exit(main())
