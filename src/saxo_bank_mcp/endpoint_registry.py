from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, TypeAdapter

from saxo_bank_mcp._evidence import JsonValue

type EndpointMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
type OperationStatus = Literal["implemented", "refused"]

INVENTORY_SOURCE_URL: Final = "https://www.developer.saxo/openapi/referencedocs"
INVENTORY_RETRIEVED_AT: Final = "2026-07-01"
EXPECTED_OPERATION_COUNT: Final = 294
EXPECTED_SERVICE_GROUP_COUNTS: Final[dict[str, int]] = {
    "Account History": 11,
    "Asset Transfers": 20,
    "Chart": 4,
    "Client Management": 17,
    "Client Reporting": 5,
    "Client Services": 18,
    "Corporate Actions": 15,
    "Disclaimer Management": 2,
    "Ens": 4,
    "Market Overview": 4,
    "Partner Integration": 31,
    "Portfolio": 64,
    "Reference Data": 18,
    "Regulatory Services": 11,
    "Root Services": 18,
    "Trading": 45,
    "Value Add": 7,
}
_DEFAULT_INVENTORY = Path(__file__).resolve().parents[2] / "data/saxo/openapi_inventory.json"


class EndpointOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    service_group: str
    service: str
    method: EndpointMethod
    path_template: str
    query_template: str
    documentation_url: str
    read_write_class: str
    risk_class: str
    auth_requirement: str
    request_model: str
    response_model: str
    rate_rule: str
    cleanup_rule: str | None
    status: OperationStatus
    refusal_reason: str


class EndpointInventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_url: str
    retrieved_at: str
    service_group_counts: dict[str, int]
    operation_count: int
    operations: tuple[EndpointOperation, ...]


@dataclass(frozen=True, slots=True)
class RegisteredEndpoint:
    operation: EndpointOperation
    resolved_path: str


def load_inventory(path: Path | None = None) -> EndpointInventory:
    if path is None:
        return _load_default_inventory()
    return TypeAdapter(EndpointInventory).validate_json(path.read_text(encoding="utf-8"))


@cache
def _load_default_inventory() -> EndpointInventory:
    return TypeAdapter(EndpointInventory).validate_json(
        _DEFAULT_INVENTORY.read_text(encoding="utf-8"),
    )


def validate_inventory(inventory: EndpointInventory) -> dict[str, JsonValue]:
    errors: list[str] = []
    operation_ids = [operation.operation_id for operation in inventory.operations]
    duplicates = sorted({value for value in operation_ids if operation_ids.count(value) > 1})
    unclassified = [
        operation.operation_id
        for operation in inventory.operations
        if not operation.read_write_class or not operation.risk_class
    ]
    undecided = [
        operation.operation_id
        for operation in inventory.operations
        if operation.status not in {"implemented", "refused"}
    ]
    missing_reasons = [
        operation.operation_id
        for operation in inventory.operations
        if operation.status == "refused" and not operation.refusal_reason
    ]
    if inventory.source_url != INVENTORY_SOURCE_URL:
        errors.append("source_url_mismatch")
    if inventory.retrieved_at != INVENTORY_RETRIEVED_AT:
        errors.append("retrieved_at_mismatch")
    if inventory.operation_count != EXPECTED_OPERATION_COUNT:
        errors.append("operation_count_mismatch")
    if inventory.service_group_counts != EXPECTED_SERVICE_GROUP_COUNTS:
        errors.append("service_group_counts_mismatch")
    if inventory.operation_count != len(inventory.operations):
        errors.append("operation_list_count_mismatch")
    if duplicates:
        errors.append("duplicate_operation_ids")
    if unclassified:
        errors.append("unclassified_operations")
    if undecided:
        errors.append("undecided_operations")
    if missing_reasons:
        errors.append("refused_operations_missing_reason")
    implemented = sum(1 for operation in inventory.operations if operation.status == "implemented")
    refused = sum(1 for operation in inventory.operations if operation.status == "refused")
    status: Literal["passed", "failed"] = "failed" if errors else "passed"
    return {
        "status": status,
        "source_url": inventory.source_url,
        "retrieved_at": inventory.retrieved_at,
        "service_group_counts": inventory.service_group_counts,
        "operation_count": inventory.operation_count,
        "implemented_count": implemented,
        "refused_count": refused,
        "unclassified_count": len(unclassified),
        "undecided_count": len(undecided),
        "missing_refusal_reason_count": len(missing_reasons),
        "duplicate_operation_ids": duplicates,
        "errors": errors,
    }


def find_registered_operation(method: str, path: str) -> EndpointOperation | None:
    registered = find_registered_endpoint(method, path)
    if registered is None:
        return None
    return registered.operation


def find_registered_endpoint(method: str, path: str) -> RegisteredEndpoint | None:
    clean_path = _relative_saxo_path(path)
    if clean_path is None:
        return None
    wanted_method = method.upper()
    for operation in load_inventory().operations:
        if operation.method != wanted_method:
            continue
        resolved_path = _resolved_path(operation.path_template, clean_path)
        if resolved_path is not None:
            return RegisteredEndpoint(operation=operation, resolved_path=resolved_path)
    return None


def registered_operations_for_path(path: str) -> tuple[EndpointOperation, ...]:
    clean_path = _relative_saxo_path(path)
    if clean_path is None:
        return ()
    return tuple(
        operation
        for operation in load_inventory().operations
        if _resolved_path(operation.path_template, clean_path) is not None
    )


def path_rejection_reason(path: str) -> str | None:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return "absolute_url_rejected"
    if not path.startswith("/"):
        return "relative_path_required"
    return None


def implemented_read_operations(
    inventory: EndpointInventory | None = None,
) -> tuple[EndpointOperation, ...]:
    source = load_inventory() if inventory is None else inventory
    return tuple(
        operation
        for operation in source.operations
        if operation.method == "GET"
        and operation.status == "implemented"
        and operation.read_write_class == "read"
    )


def _relative_saxo_path(path: str) -> str | None:
    if path_rejection_reason(path) is not None:
        return None
    return urlparse(path).path


def _resolved_path(template: str, clean_path: str) -> str | None:
    if template == clean_path:
        return clean_path
    template_parts = template.strip("/").split("/")
    path_parts = clean_path.strip("/").split("/")
    if len(template_parts) != len(path_parts):
        return None
    for template_part, path_part in zip(template_parts, path_parts, strict=True):
        is_placeholder = template_part.startswith("{") and template_part.endswith("}")
        if is_placeholder and _safe_path_value(path_part):
            continue
        if template_part != path_part:
            return None
    return clean_path


def _safe_path_value(path_part: str) -> bool:
    lowered = path_part.lower()
    return (
        bool(path_part)
        and path_part not in {".", ".."}
        and "%2f" not in lowered
        and "%5c" not in lowered
        and all(char.isprintable() for char in path_part)
    )
