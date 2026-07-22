from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_catalog_runtime import (
    STANDARD_ANNOTATIONS,
    CatalogIssue,
    CatalogValidationError,
    RuntimeTool,
)
from saxo_bank_mcp.endpoint_registry import EndpointInventory, implemented_read_operations
from saxo_bank_mcp.trading_write_registry import trading_write_specs

EXPECTED_TOOL_COUNT: Final = 39


@dataclass(frozen=True, slots=True)
class CatalogValidationInputs:
    inventory: EndpointInventory
    routes: JsonValue
    statuses: JsonValue
    scenarios: JsonValue
    tools: tuple[RuntimeTool, ...]
    required_statuses: frozenset[str]


def validate_catalog_sources(inputs: CatalogValidationInputs) -> None:
    issues = (
        _runtime_issues(inputs.tools)
        + _route_issues(inputs.inventory, inputs.routes, inputs.tools)
        + _scenario_issues(inputs.scenarios, inputs.tools)
        + _status_issues(inputs.statuses, inputs.required_statuses)
        + _operation_count_issues(inputs.inventory)
    )
    if issues:
        raise CatalogValidationError(issues)


def route_operation_ids(route: JsonValue, inventory: EndpointInventory) -> tuple[str, ...]:
    explicit = tuple(_string_list(_field(route, "operation_ids")))
    expanded = tuple(
        operation_id
        for set_name in _string_list(_field(route, "operation_sets"))
        for operation_id in _operation_set(set_name, inventory)
    )
    return tuple(sorted(dict.fromkeys((*explicit, *expanded))))


def _runtime_issues(tools: tuple[RuntimeTool, ...]) -> tuple[CatalogIssue, ...]:
    issues: list[CatalogIssue] = []
    if len(tools) != EXPECTED_TOOL_COUNT:
        issues.append(CatalogIssue("tool_count", str(len(tools))))
    for tool in tools:
        annotations = _object(tool.annotations)
        metadata = _object(tool.metadata)
        issues.extend(
            CatalogIssue("missing_annotation", f"{tool.name}.{field}")
            for field in STANDARD_ANNOTATIONS
            if not isinstance(annotations.get(field), bool)
        )
        if not metadata:
            issues.append(CatalogIssue("missing_metadata", tool.name))
    return tuple(issues)


def _route_issues(
    inventory: EndpointInventory,
    routes: JsonValue,
    tools: tuple[RuntimeTool, ...],
) -> tuple[CatalogIssue, ...]:
    route_rows = _rows(routes, "tool_routes")
    runtime_names = frozenset(tool.name for tool in tools)
    route_names = frozenset(_string_field(row, "tool") for row in route_rows)
    operation_status = {
        operation.operation_id: operation.status for operation in inventory.operations
    }
    issues = _coverage_issues("route", runtime_names, route_names)
    for row in route_rows:
        tool = _string_field(row, "tool")
        if tool not in runtime_names:
            issues.append(CatalogIssue("unknown_tool", tool))
        for operation_id in route_operation_ids(row, inventory):
            status = operation_status.get(operation_id)
            if status is None:
                issues.append(CatalogIssue("stale_operation", operation_id))
            if status == "refused" and _bool_field(row, "executable"):
                issues.append(CatalogIssue("bad_refused_route", operation_id))
    return tuple(issues)


def _scenario_issues(
    scenarios: JsonValue,
    tools: tuple[RuntimeTool, ...],
) -> tuple[CatalogIssue, ...]:
    rows = _rows(scenarios, "scenarios")
    runtime = {tool.name: _object(tool.metadata) for tool in tools}
    names = frozenset(_string_field(row, "tool") for row in rows)
    issues = _coverage_issues("scenario", frozenset(runtime), names)
    for row in rows:
        tool = _string_field(row, "tool")
        if tool not in runtime:
            issues.append(CatalogIssue("unknown_tool", tool))
            continue
        metadata = runtime[tool]
        sim_supported = "SIM" in _string_list(metadata.get("environment_support"))
        if metadata.get("state_changing") is True and sim_supported:
            if _string_field(row, "scenario_type") == "expected_refusal":
                issues.append(CatalogIssue("supported_mutation_refused", tool))
            if not _bool_field(row, "executable_in_sim"):
                issues.append(CatalogIssue("supported_mutation_not_executable", tool))
    return tuple(issues)


def _status_issues(
    statuses: JsonValue,
    required_statuses: frozenset[str],
) -> tuple[CatalogIssue, ...]:
    rows = _rows(statuses, "status_routes")
    known = frozenset(_string_field(row, "status") for row in rows)
    issues = [CatalogIssue("missing_status", status) for status in required_statuses - known]
    for row in rows:
        status = _string_field(row, "status")
        if status not in required_statuses:
            issues.append(CatalogIssue("unknown_status", status))
        success = _bool_field(row, "unqualified_mutation_success")
        if success != (status == "completed"):
            issues.append(CatalogIssue("misleading_success_wording", status))
    return tuple(issues)


def _operation_count_issues(inventory: EndpointInventory) -> tuple[CatalogIssue, ...]:
    implemented = sum(1 for operation in inventory.operations if operation.status == "implemented")
    refused = sum(1 for operation in inventory.operations if operation.status == "refused")
    checks = (
        (inventory.operation_count, 294),
        (implemented, 182),
        (refused, 112),
        (len(inventory.service_group_counts), 17),
    )
    return tuple(
        CatalogIssue("operation_count", str(actual))
        for actual, want in checks
        if actual != want
    )


def _operation_set(name: str, inventory: EndpointInventory) -> tuple[str, ...]:
    if name == "implemented_registered_reads":
        return tuple(operation.operation_id for operation in implemented_read_operations(inventory))
    if name == "trading_write_operations":
        return tuple(spec.operation_id for spec in trading_write_specs())
    if name == "generic_trading_write_operations":
        return tuple(
            spec.operation_id
            for spec in trading_write_specs()
            if spec.specialized_tool is None
        )
    raise CatalogValidationError((CatalogIssue("unknown_operation_set", name),))


def _coverage_issues(
    label: str,
    expected: frozenset[str],
    actual: frozenset[str],
) -> list[CatalogIssue]:
    missing_code = "uncovered_tool" if label == "scenario" else f"missing_{label}"
    return [
        *(CatalogIssue(missing_code, value) for value in sorted(expected - actual)),
        *(CatalogIssue(f"unknown_{label}", value) for value in sorted(actual - expected)),
    ]


def _rows(data: JsonValue, key: str) -> tuple[dict[str, JsonValue], ...]:
    value = _field(data, key)
    if not isinstance(value, list):
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    rows: list[dict[str, JsonValue]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise CatalogValidationError((CatalogIssue("malformed_json", f"{key}[{index}]"),))
        rows.append(dict(item))
    return tuple(rows)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return dict(value) if isinstance(value, dict) else {}


def _field(value: JsonValue, key: str) -> JsonValue:
    if not isinstance(value, dict):
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    return value.get(key)


def _string_field(value: JsonValue, key: str) -> str:
    field = _field(value, key)
    if not isinstance(field, str) or not field:
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    return field


def _bool_field(value: JsonValue, key: str) -> bool:
    field = _field(value, key)
    if not isinstance(field, bool):
        raise CatalogValidationError((CatalogIssue("malformed_json", key),))
    return field


def _string_list(value: JsonValue) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CatalogValidationError((CatalogIssue("malformed_json", "string_list"),))
    return tuple(item for item in value if isinstance(item, str))
