from __future__ import annotations

from datetime import UTC, datetime
from difflib import get_close_matches
from typing import Final

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.endpoint_registry import (
    EndpointOperation,
    load_inventory,
    validate_inventory,
)
from saxo_bank_mcp.read_tools import (
    READ_DOES_NOT_VERIFY,
    READINESS_PREREQUISITES,
    ReadObject,
    ReadToolResult,
)

READ_LIST_TOOL_DESCRIPTION: Final = (
    "Lists checked-in Saxo OpenAPI registry entries. This only reports documented endpoint "
    "metadata and does not call Saxo, prove account access, or prove trading readiness."
)
STRING_LIST_ADAPTER: Final[TypeAdapter[list[str]]] = TypeAdapter(list[str])
MAX_SNAPSHOT_AGE_DAYS: Final = 30
REGISTRY_WARNING_NOTICE: Final = (
    "Registry metadata only; does not verify account access, trading readiness, "
    "live access, or official-live completeness."
)


def saxo_list_registered_endpoints(
    service_group: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> ReadToolResult:
    inventory = load_inventory()
    validation = validate_inventory(inventory)
    valid_service_groups = list(inventory.service_group_counts)
    selected = [
        operation
        for operation in inventory.operations
        if service_group is None or operation.service_group == service_group
    ]
    unknown_service_group = service_group is not None and not selected
    capped_limit = max(0, min(limit, 100))
    capped_offset = max(0, offset)
    page_end = capped_offset + capped_limit
    registry_valid = validation.get("status") == "passed"
    next_offset = page_end if page_end < len(selected) else None
    snapshot_age_days = _snapshot_age_days(inventory.retrieved_at)
    return {
        "tool_name": "saxo_list_registered_endpoints",
        "status": "metadata_only_not_ready_for_trading",
        "warning_notice": REGISTRY_WARNING_NOTICE,
        "registry_load_state": "loaded" if registry_valid else "validation_failed",
        "source_url": inventory.source_url,
        "snapshot_date": inventory.retrieved_at,
        "snapshot_age_days": snapshot_age_days,
        "snapshot_stale": snapshot_age_days > MAX_SNAPSHOT_AGE_DAYS,
        "snapshot_group_count": len(valid_service_groups),
        "snapshot_completeness_scope": "statically_checked_in_not_reconciled_with_live",
        "coverage_basis": "curated_snapshot_not_reconciled_with_live",
        "official_group_source_url": inventory.source_url,
        "official_group_completeness_verified": False,
        "official_group_reconciliation": "not_run",
        "live_source_fetched": False,
        "registry_only": True,
        "execution_readiness_verified": False,
        "live_access": False,
        "account_access_verified": False,
        "trading_ready": False,
        "inventory_validation_status": "self_consistent" if registry_valid else "inconsistent",
        "inventory_validation_scope": "internal_snapshot_consistency_only",
        "inventory_validation_errors": _string_list(validation.get("errors")),
        "verifies": [
            "checked_in_registry_loaded",
            "internal_snapshot_self_consistency",
        ],
        "total_operation_count": inventory.operation_count,
        "matched_count": len(selected),
        "returned_count": len(selected[capped_offset:page_end]),
        "offset": capped_offset,
        "next_offset": next_offset,
        "truncated": next_offset is not None,
        "unknown_service_group": unknown_service_group,
        "service_group_error": "unknown_service_group" if unknown_service_group else "",
        "service_group_suggestions": _service_group_suggestions(
            service_group,
            valid_service_groups,
        ),
        "valid_service_groups": valid_service_groups,
        "service_group_counts": inventory.service_group_counts,
        "service_group_support": _service_group_support(inventory.operations),
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
        "readiness_prerequisites": list(READINESS_PREREQUISITES),
        "operations": [
            _operation_summary(operation) for operation in selected[capped_offset:page_end]
        ],
    }


def _operation_summary(operation: EndpointOperation) -> ReadObject:
    return {
        "operation_id": operation.operation_id,
        "service_group": operation.service_group,
        "service": operation.service,
        "method": operation.method,
        "path_template": operation.path_template,
        "read_write_class": operation.read_write_class,
        "risk_class": operation.risk_class,
        "auth_requirement": operation.auth_requirement,
        "mcp_support_policy": _support_policy(operation),
        "live_reachability_verified": False,
        "account_readiness_verified": False,
        "refusal_reason": _refusal_reason(operation),
    }


def _service_group_support(operations: tuple[EndpointOperation, ...]) -> list[ReadObject]:
    service_groups = sorted({operation.service_group for operation in operations})
    support: list[ReadObject] = []
    for service_group in service_groups:
        group_operations = [
            operation for operation in operations if operation.service_group == service_group
        ]
        support.append(
            {
                "service_group": service_group,
                "registered_read_definitions": sum(
                    1
                    for operation in group_operations
                    if operation.status == "implemented"
                    and operation.method == "GET"
                    and operation.read_write_class == "read"
                ),
                "refused_operations": sum(
                    1 for operation in group_operations if operation.status == "refused"
                ),
                "write_or_subscription_operations": sum(
                    1
                    for operation in group_operations
                    if operation.read_write_class == "write_or_subscription"
                ),
                "diagnostic_read_operations": sum(
                    1
                    for operation in group_operations
                    if operation.read_write_class == "diagnostic_read"
                ),
            },
        )
    return support


def _support_policy(operation: EndpointOperation) -> str:
    if operation.status == "implemented":
        return "read_only_definition_registered"
    if operation.read_write_class == "write_or_subscription":
        return "write_operations_disabled_by_policy"
    return "operation_disabled_by_policy"


def _refusal_reason(operation: EndpointOperation) -> str:
    if not operation.refusal_reason:
        return ""
    return operation.refusal_reason


def _snapshot_age_days(snapshot_date: str) -> int:
    retrieved = datetime.fromisoformat(snapshot_date).date()
    return (datetime.now(UTC).date() - retrieved).days


def _service_group_suggestions(
    service_group: str | None,
    valid_service_groups: list[str],
) -> list[str]:
    if service_group is None or service_group in valid_service_groups:
        return []
    return get_close_matches(service_group, valid_service_groups, n=3, cutoff=0.45)


def _string_list(value: JsonValue | None) -> list[str]:
    try:
        return STRING_LIST_ADAPTER.validate_python(value)
    except ValidationError:
        return []
