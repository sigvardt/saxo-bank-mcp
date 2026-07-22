from __future__ import annotations

from pathlib import Path

from saxo_bank_mcp.agent_catalog_render import catalog_inputs, rows, set_rows
from saxo_bank_mcp.agent_catalog_runtime import CatalogIssue, CatalogValidationError
from saxo_bank_mcp.agent_catalog_statuses import emitted_statuses
from saxo_bank_mcp.agent_catalog_validation import (
    CatalogValidationInputs,
    validate_catalog_sources,
)


def self_test_fixture(root: Path, fixture: str) -> None:
    inputs = catalog_inputs(root)
    match fixture:
        case "unknown-tool":
            route_rows = rows(inputs.routes, "tool_routes")
            route_rows.append(dict(route_rows[0], tool="saxo_unknown_fixture"))
            set_rows(inputs.routes, "tool_routes", route_rows)
        case "stale-operation":
            route_rows = rows(inputs.routes, "tool_routes")
            route_rows[0]["operation_ids"] = ["get.fixture.stale"]
            set_rows(inputs.routes, "tool_routes", route_rows)
        case "missing-status":
            status_rows = rows(inputs.statuses, "status_routes")
            kept = [row for row in status_rows if row.get("status") != "completed"]
            set_rows(inputs.statuses, "status_routes", kept)
        case "unclassified-status-module":
            if not isinstance(inputs.statuses, dict):
                raise CatalogValidationError((CatalogIssue("malformed_json", fixture),))
            value = inputs.statuses.get("status_module_classification")
            if not isinstance(value, dict):
                raise CatalogValidationError(
                    (CatalogIssue("missing_status_classification", fixture),),
                )
            included = rows(value, "included")
            kept = [
                row for row in included if row.get("module") != "src/saxo_bank_mcp/live_mode.py"
            ]
            set_rows(value, "included", kept)
        case "uncovered-tool":
            scenario_rows = rows(inputs.scenarios, "scenarios")
            kept = [row for row in scenario_rows if row.get("tool") != "saxo_health"]
            set_rows(inputs.scenarios, "scenarios", kept)
        case _:
            raise CatalogValidationError((CatalogIssue("unknown_fixture", fixture),))
    validate_catalog_sources(
        CatalogValidationInputs(
            inputs.inventory,
            inputs.routes,
            inputs.statuses,
            inputs.scenarios,
            inputs.tools,
            emitted_statuses(root, inputs.statuses),
        ),
    )
