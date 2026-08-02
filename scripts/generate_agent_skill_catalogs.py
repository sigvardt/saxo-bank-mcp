#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/generate_agent_skill_catalogs.py --check
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue, write_text
from saxo_bank_mcp.agent_catalog_fixtures import self_test_fixture
from saxo_bank_mcp.agent_catalog_markdown import banner, cell, code
from saxo_bank_mcp.agent_catalog_render import rows
from saxo_bank_mcp.agent_catalog_runtime import (
    CatalogValidationError,
    RuntimeTool,
    load_json_file,
    runtime_tools,
)
from saxo_bank_mcp.agent_catalog_validation import route_operation_ids
from saxo_bank_mcp.endpoint_registry import EndpointInventory, load_inventory
from saxo_bank_mcp.server_tool_ids import (
    ALL_LOGICAL_TOOL_IDS,
    ANALYTICS_TOOL_IDS,
    EXPECTED_TOOL_COUNT,
)

ROOT: Final = Path(__file__).resolve().parents[1]
ROUTES_PATH: Final = Path("data/saxo/agent_tool_routes.json")
STATUSES_PATH: Final = Path("data/saxo/agent_status_routes.json")
SCENARIOS_PATH: Final = Path("data/saxo/agent_tool_scenarios.json")
INVENTORY_PATH: Final = Path("data/saxo/openapi_inventory.json")
CATALOG_PATH: Final = Path("skills/saxo-bank/references/tool-catalog.md")
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

    routes = load_json_file(ROOT / ROUTES_PATH)
    route_rows = {str(row["tool"]): row for row in rows(routes, "tool_routes")}
    inventory = load_inventory(ROOT / INVENTORY_PATH)
    workflow = (ROOT / ANALYTICS_WORKFLOW_PATH).read_text(encoding="utf-8")
    workflow_tools = frozenset(_TOOL_ROW_PATTERN.findall(workflow))
    if workflow_tools != frozenset(ANALYTICS_TOOL_IDS):
        raise Task22CatalogError("analytics_workflow_tool_set")
    case_files = tuple(sorted((ROOT / ANALYTICS_CASE_ROOT).glob("*/case.yaml")))
    if len(case_files) != EXPECTED_ANALYTICS_SCENARIOS:
        raise Task22CatalogError("analytics_scenario_count")

    expected = _tool_catalog(tools, route_rows, inventory)
    target = ROOT / CATALOG_PATH
    if check:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            raise Task22CatalogError("generated_drift")
    else:
        write_text(target, expected)

    operation_count = inventory.operation_count
    implemented = sum(1 for item in inventory.operations if item.status == "implemented")
    refused = sum(1 for item in inventory.operations if item.status == "refused")
    legacy_scenarios = len(rows(load_json_file(ROOT / SCENARIOS_PATH), "scenarios"))
    statuses = rows(load_json_file(ROOT / STATUSES_PATH), "status_routes")
    plain_successes = [
        str(row["status"]) for row in statuses if row.get("unqualified_mutation_success") is True
    ]
    only_completed = str(plain_successes == ["completed"]).lower()
    return (
        f"tool_count={len(tools)} operation_count={operation_count} "
        f"implemented_count={implemented} refused_count={refused} "
        f"service_group_count={len(inventory.service_group_counts)} "
        f"scenarios={legacy_scenarios} only_completed_unqualified_success={only_completed} "
        f"skill_count={skill_count} "
        f"analytics_tool_count={len(ANALYTICS_TOOL_IDS)} "
        f"analytics_scenarios={len(case_files)}"
    )


def _tool_catalog(
    tools: tuple[RuntimeTool, ...],
    route_rows: dict[str, dict[str, JsonValue]],
    inventory: EndpointInventory,
) -> str:
    source_digest = _catalog_source_hash()
    lines = banner("Saxo MCP tool catalog", source_digest)
    lines.extend(
        [
            "| Tool | Skill | Route | Operations | Annotations | Metadata | Input schema |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for tool in tools:
        name = tool.name
        route = route_rows.get(name)
        if name in ANALYTICS_TOOL_IDS:
            owner = "saxo-analytics"
            route_kind = _analytics_route_kind(name)
            operations = "local/domain service"
        elif route is not None:
            owner = str(route["owning_skill"])
            route_kind = str(route["route_kind"])
            operations = ", ".join(route_operation_ids(route, inventory)) or "local/session"
        else:
            raise Task22CatalogError("missing_legacy_route")
        lines.append(
            "| "
            + " | ".join(
                (
                    cell(name),
                    cell(owner),
                    cell(route_kind),
                    cell(operations),
                    code(tool.annotations),
                    code(tool.metadata),
                    code(tool.input_schema),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _analytics_route_kind(tool_id: str) -> str:
    if tool_id in {
        "saxo_resolve_research_universe",
        "saxo_manage_research_universe",
        "saxo_sync_research_data",
    }:
        return "bounded analytics source/local state"
    if tool_id in {
        "saxo_render_analysis",
        "saxo_export_analysis",
        "saxo_manage_analysis_job",
        "saxo_preview_analytics_deletion",
        "saxo_delete_analytics_data",
    }:
        return "owner-local analytics state"
    return "bounded analytics read/compute"


def _catalog_source_hash() -> str:
    paths = (
        ROUTES_PATH,
        STATUSES_PATH,
        SCENARIOS_PATH,
        INVENTORY_PATH,
        Path("src/saxo_bank_mcp/server_tool_ids.py"),
        Path("src/saxo_bank_mcp/tool_annotations.py"),
        Path("src/saxo_bank_mcp/tool_metadata.py"),
        Path("src/saxo_bank_mcp/analytics_tool_descriptions.py"),
        ANALYTICS_WORKFLOW_PATH,
        *tuple(
            path.relative_to(ROOT)
            for path in sorted((ROOT / ANALYTICS_CASE_ROOT).glob("*/case.yaml"))
        ),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
