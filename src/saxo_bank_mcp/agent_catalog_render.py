from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue, write_text
from saxo_bank_mcp.agent_catalog_markdown import banner, cell, code
from saxo_bank_mcp.agent_catalog_runtime import (
    CatalogIssue,
    CatalogValidationError,
    RuntimeTool,
    load_json_file,
    runtime_tools,
    source_hash,
)
from saxo_bank_mcp.agent_catalog_statuses import emitted_statuses
from saxo_bank_mcp.agent_catalog_validation import (
    CatalogValidationInputs,
    route_operation_ids,
    validate_catalog_sources,
)
from saxo_bank_mcp.endpoint_registry import EndpointInventory, load_inventory

SOURCE_PATHS: Final = (
    Path("data/saxo/agent_tool_routes.json"),
    Path("data/saxo/agent_status_routes.json"),
    Path("data/saxo/agent_tool_scenarios.json"),
    Path("data/saxo/openapi_inventory.json"),
)
OUTPUT_PATHS: Final = {
    "tools": Path("skills/saxo-bank/references/tool-catalog.md"),
    "operations": Path("skills/saxo-openapi/references/operation-catalog.md"),
    "statuses": Path("skills/saxo-safety-recovery/references/status-recovery.md"),
    "scenarios": Path("skills/saxo-qa-operations/references/scenario-coverage.md"),
}


@dataclass(frozen=True, slots=True)
class CatalogSummary:
    tool_count: int
    operation_count: int
    implemented_count: int
    refused_count: int
    service_group_count: int
    scenario_count: int
    only_completed_unqualified_success: bool

    def line(self) -> str:
        success = str(self.only_completed_unqualified_success).lower()
        return (
            f"tool_count={self.tool_count} operation_count={self.operation_count} "
            f"implemented_count={self.implemented_count} refused_count={self.refused_count} "
            f"service_group_count={self.service_group_count} scenarios={self.scenario_count} "
            f"only_completed_unqualified_success={success}"
        )


def generate_catalogs(root: Path, *, check: bool) -> CatalogSummary:
    inputs = catalog_inputs(root)
    outputs = _render_all(inputs)
    if check:
        _check_outputs(root, outputs)
    else:
        for relative, content in outputs.items():
            write_text(root / relative, content)
    return _summary(inputs)


@dataclass(frozen=True, slots=True)
class _CatalogInputs:
    inventory: EndpointInventory
    routes: JsonValue
    statuses: JsonValue
    scenarios: JsonValue
    tools: tuple[RuntimeTool, ...]
    hash_value: str


def catalog_inputs(root: Path) -> _CatalogInputs:
    inventory = load_inventory(root / "data/saxo/openapi_inventory.json")
    routes = load_json_file(root / "data/saxo/agent_tool_routes.json")
    statuses = load_json_file(root / "data/saxo/agent_status_routes.json")
    scenarios = load_json_file(root / "data/saxo/agent_tool_scenarios.json")
    tools = runtime_tools()
    validate_catalog_sources(
        CatalogValidationInputs(
            inventory,
            routes,
            statuses,
            scenarios,
            tools,
            emitted_statuses(root, statuses),
        ),
    )
    return _CatalogInputs(
        inventory,
        routes,
        statuses,
        scenarios,
        tools,
        source_hash(root, SOURCE_PATHS),
    )


def _render_all(inputs: _CatalogInputs) -> dict[Path, str]:
    return {
        OUTPUT_PATHS["tools"]: _tool_catalog(inputs),
        OUTPUT_PATHS["operations"]: _operation_catalog(inputs),
        OUTPUT_PATHS["statuses"]: _status_catalog(inputs),
        OUTPUT_PATHS["scenarios"]: _scenario_catalog(inputs),
    }


def _tool_catalog(inputs: _CatalogInputs) -> str:
    routes = {str(row["tool"]): row for row in rows(inputs.routes, "tool_routes")}
    lines = banner("Saxo MCP tool catalog", inputs.hash_value)
    lines.extend(
        [
            "| Tool | Skill | Route | Operations | Annotations | Metadata | Input schema |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ],
    )
    for tool in inputs.tools:
        route = routes[tool.name]
        operations = ", ".join(route_operation_ids(route, inputs.inventory)) or "local/session"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(tool.name),
                    _cell(str(route["owning_skill"])),
                    _cell(str(route["route_kind"])),
                    _cell(operations),
                    code(tool.annotations),
                    code(tool.metadata),
                    code(tool.input_schema),
                ),
            )
            + " |",
        )
    return "\n".join(lines) + "\n"


def _operation_catalog(inputs: _CatalogInputs) -> str:
    lines = banner("Saxo OpenAPI operation catalog", inputs.hash_value)
    lines.extend(
        [
            "| Operation | Group | Service | Method | Path | Status | Risk | Refusal |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ],
    )
    for operation in sorted(inputs.inventory.operations, key=lambda item: item.operation_id):
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(operation.operation_id),
                    _cell(operation.service_group),
                    _cell(operation.service),
                    operation.method,
                    _cell(operation.path_template),
                    operation.status,
                    _cell(operation.risk_class),
                    _cell(operation.refusal_reason or ""),
                ),
            )
            + " |",
        )
    return "\n".join(lines) + "\n"


def _status_catalog(inputs: _CatalogInputs) -> str:
    lines = banner("Saxo status and recovery catalog", inputs.hash_value)
    lines.extend(
        [
            "| Status | Mutation possible | Retry class | Next action | Safe wording | Source |",
            "| --- | --- | --- | --- | --- | --- |",
        ],
    )
    sorted_rows = sorted(
        rows(inputs.statuses, "status_routes"),
        key=lambda item: str(item["status"]),
    )
    for row in sorted_rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(str(row["status"])),
                    str(row["mutation_possible"]).lower(),
                    _cell(str(row["retry_class"])),
                    _cell(str(row["next_action"])),
                    _cell(str(row["safe_user_wording"])),
                    _cell(str(row["source"])),
                ),
            )
            + " |",
        )
    return "\n".join(lines) + "\n"


def _scenario_catalog(inputs: _CatalogInputs) -> str:
    lines = banner("Saxo scenario coverage catalog", inputs.hash_value)
    lines.extend(
        [
            "| Tool | Type | Environment | SIM executable | Invocation | Cleanup |",
            "| --- | --- | --- | --- | --- | --- |",
        ],
    )
    for row in sorted(rows(inputs.scenarios, "scenarios"), key=lambda item: str(item["tool"])):
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(str(row["tool"])),
                    _cell(str(row["scenario_type"])),
                    _cell(str(row["environment"])),
                    str(row["executable_in_sim"]).lower(),
                    _cell(str(row["invocation"])),
                    _cell(str(row["cleanup"])),
                ),
            )
            + " |",
        )
    return "\n".join(lines) + "\n"


def _summary(inputs: _CatalogInputs) -> CatalogSummary:
    implemented = sum(
        1 for operation in inputs.inventory.operations if operation.status == "implemented"
    )
    refused = sum(1 for operation in inputs.inventory.operations if operation.status == "refused")
    successes = [
        str(row["status"])
        for row in rows(inputs.statuses, "status_routes")
        if row["unqualified_mutation_success"] is True
    ]
    return CatalogSummary(
        len(inputs.tools),
        inputs.inventory.operation_count,
        implemented,
        refused,
        len(inputs.inventory.service_group_counts),
        len(rows(inputs.scenarios, "scenarios")),
        successes == ["completed"],
    )


def _check_outputs(root: Path, outputs: dict[Path, str]) -> None:
    issues = [
        CatalogIssue("generated_drift", str(path))
        for path, expected in outputs.items()
        if not (root / path).exists() or (root / path).read_text(encoding="utf-8") != expected
    ]
    if issues:
        raise CatalogValidationError(tuple(issues))


def rows(data: JsonValue, key: str) -> list[dict[str, JsonValue]]:
    if not isinstance(data, dict):
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    value = data.get(key)
    if not isinstance(value, list):
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    rows: list[dict[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CatalogValidationError((CatalogIssue("malformed_json", key),))
        rows.append(dict(item))
    return rows


def set_rows(data: JsonValue, key: str, source_rows: list[dict[str, JsonValue]]) -> None:
    if not isinstance(data, dict):
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    data[key] = source_rows


def _cell(value: str) -> str:
    return cell(value)
