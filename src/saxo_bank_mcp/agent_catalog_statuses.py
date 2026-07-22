from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_catalog_runtime import CatalogIssue, CatalogValidationError

STATUS_CALL_ARGS: Final = {
    "common_result": 0,
    "_called_result": 0,
    "_common_result": 0,
    "_error_result": 1,
    "base_payload": 1,
}
STATUS_RETURN_FUNCTIONS: Final = frozenset(
    {"_precheck_status", "_response_status", "_execution_status", "_status_with_cleanup"},
)
UNAVAILABLE_STAGES: Final = frozenset({"account_lookup", "instrument_lookup", "precheck"})
JSON_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
EMPTY_STATUSES: Final[frozenset[str]] = frozenset()


@dataclass(frozen=True, slots=True)
class StatusModule:
    module: str
    statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StatusModuleExclusion:
    module: str
    reason: str


@dataclass(frozen=True, slots=True)
class StatusDiscovery:
    included: tuple[StatusModule, ...]
    excluded: tuple[StatusModuleExclusion, ...]
    unclassified: tuple[StatusModule, ...]
    stale_classifications: tuple[str, ...]

    @property
    def statuses(self) -> frozenset[str]:
        return frozenset(status for module in self.included for status in module.statuses)


def emitted_statuses(root: Path, source: JsonValue | None = None) -> frozenset[str]:
    discovery = status_discovery(root, source)
    issues = [
        *(
            CatalogIssue("unclassified_status_module", item.module)
            for item in discovery.unclassified
        ),
        *(
            CatalogIssue("stale_status_module", module)
            for module in discovery.stale_classifications
        ),
    ]
    if issues:
        raise CatalogValidationError(tuple(issues))
    return discovery.statuses


def status_discovery(root: Path, source: JsonValue | None = None) -> StatusDiscovery:
    candidates = {module.module: module for module in _candidate_status_modules(root)}
    included_names, exclusions = _classification(root, source)
    excluded_names = frozenset(item.module for item in exclusions)
    classified = included_names | excluded_names
    included = tuple(candidates[name] for name in sorted(included_names & candidates.keys()))
    unclassified = tuple(candidates[name] for name in sorted(candidates.keys() - classified))
    stale = tuple(sorted(classified - candidates.keys()))
    return StatusDiscovery(included, exclusions, unclassified, stale)


def _candidate_status_modules(root: Path) -> tuple[StatusModule, ...]:
    modules: list[StatusModule] = []
    for path in sorted((root / "src/saxo_bank_mcp").glob("*.py")):
        statuses = _file_statuses(path)
        if statuses:
            modules.append(StatusModule(path.relative_to(root).as_posix(), tuple(sorted(statuses))))
    return tuple(modules)


def _file_statuses(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = _string_assignments(tree)
    statuses: set[str] = set()
    for node in ast.walk(tree):
        _collect_status_literal(node, names, statuses)
        _collect_dynamic_stage_status(node, names, statuses)
    for function in _status_return_functions(tree):
        for child in ast.walk(function):
            if isinstance(child, ast.Return):
                statuses.update(_expression_statuses(child.value, names))
    return frozenset(status for status in statuses if status)


def _string_assignments(tree: ast.Module) -> dict[str, frozenset[str]]:
    assignments: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values = _assignment_strings(target.id, node.value)
                    if values:
                        assignments.setdefault(target.id, set()).update(values)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            values = _assignment_strings(node.target.id, node.value)
            if values:
                assignments.setdefault(node.target.id, set()).update(values)
    return {name: frozenset(values) for name, values in assignments.items()}


def _collect_status_literal(
    node: ast.AST,
    names: dict[str, frozenset[str]],
    statuses: set[str],
) -> None:
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if _constant_string(key) == "status":
                statuses.update(_expression_statuses(value, names))
    if isinstance(node, ast.Assign) and _assigns_name(node, "status"):
        statuses.update(_expression_statuses(node.value, names))
    if isinstance(node, ast.AnnAssign) and _ann_assigns_name(node, "status"):
        statuses.update(_literal_strings(node.annotation))
        statuses.update(_expression_statuses(node.value, names))
    if isinstance(node, ast.TypeAlias) and _type_alias_is_status(node):
        statuses.update(_literal_strings(node.value))
    if isinstance(node, ast.Call):
        _collect_call_statuses(node, names, statuses)


def _collect_call_statuses(
    node: ast.Call,
    names: dict[str, frozenset[str]],
    statuses: set[str],
) -> None:
    call_name = _call_name(node)
    index = STATUS_CALL_ARGS.get(call_name) if call_name is not None else None
    if index is not None and len(node.args) > index:
        statuses.update(_expression_statuses(node.args[index], names))
    for keyword in node.keywords:
        if keyword.arg == "status":
            statuses.update(_expression_statuses(keyword.value, names))


def _collect_dynamic_stage_status(
    node: ast.AST,
    names: dict[str, frozenset[str]],
    statuses: set[str],
) -> None:
    if not isinstance(node, ast.Assign) or not _assigns_name(node, "failure_stage"):
        return
    for stage in _expression_statuses(node.value, names):
        if stage in UNAVAILABLE_STAGES:
            statuses.add(f"{stage}_unavailable")


def _expression_statuses(
    node: ast.AST | None,
    names: dict[str, frozenset[str]],
) -> frozenset[str]:
    statuses = set(_literal_strings(node))
    if isinstance(node, ast.Name):
        statuses.update(names.get(node.id, frozenset()))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
    ):
        if isinstance(node.func.value, ast.Name):
            statuses.update(names.get(node.func.value.id, frozenset()))
        for argument in node.args[1:]:
            statuses.update(_expression_statuses(argument, names))
    if isinstance(node, ast.IfExp):
        statuses.update(_expression_statuses(node.body, names))
        statuses.update(_expression_statuses(node.orelse, names))
    return frozenset(statuses)


def _literal_strings(node: ast.AST | None) -> frozenset[str]:
    value = _constant_string(node)
    if value is not None:
        return frozenset((value,))
    if isinstance(node, ast.Subscript):
        return _literal_strings(node.slice)
    if isinstance(node, ast.Tuple):
        return frozenset(item for child in node.elts for item in _literal_strings(child))
    return frozenset()


def _assignment_strings(target_name: str, node: ast.AST | None) -> frozenset[str]:
    if isinstance(node, ast.Dict) and "STATUS" in target_name.upper():
        return frozenset(item for child in node.values for item in _literal_strings(child))
    return _literal_strings(node)


def _status_return_functions(tree: ast.Module) -> tuple[ast.FunctionDef, ...]:
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in STATUS_RETURN_FUNCTIONS
    )


def _classification(
    root: Path,
    source: JsonValue | None,
) -> tuple[frozenset[str], tuple[StatusModuleExclusion, ...]]:
    data = _status_source(root) if source is None else source
    root_object = data if isinstance(data, dict) else {}
    classification = root_object.get("status_module_classification")
    class_object = classification if isinstance(classification, dict) else {}
    included = _module_names(class_object.get("included"))
    excluded = tuple(
        StatusModuleExclusion(module, reason)
        for module, reason in _module_reasons(class_object.get("excluded"))
    )
    return included, excluded


def _status_source(root: Path) -> JsonValue:
    return JSON_ADAPTER.validate_python(
        json.loads((root / "data/saxo/agent_status_routes.json").read_text(encoding="utf-8")),
    )


def _module_names(value: JsonValue | None) -> frozenset[str]:
    return frozenset(module for module, _reason in _module_reasons(value))


def _module_reasons(value: JsonValue | None) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for row in value:
        if isinstance(row, dict):
            module = row.get("module")
            reason = row.get("reason")
            if isinstance(module, str) and isinstance(reason, str):
                pairs.append((module, reason))
    return tuple(pairs)


def _type_alias_is_status(node: ast.TypeAlias) -> bool:
    return "Status" in node.name.id


def _assigns_name(node: ast.Assign, name: str) -> bool:
    return any(isinstance(target, ast.Name) and target.id == name for target in node.targets)


def _ann_assigns_name(node: ast.AnnAssign, name: str) -> bool:
    return isinstance(node.target, ast.Name) and node.target.id == name


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _constant_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
