from __future__ import annotations

import os
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.endpoint_registry import EndpointInventory, EndpointOperation, load_inventory

NONTRADE_REFUSAL_REASON: Final = "risky_non_trading_write_refused"
NO_SAFE_NONTRADE_OPERATION_REASON: Final = "no_proven_sim_safe_no_money_operation"
SAFE_NONTRADE_WRITES_ENV: Final = "SAXO_MCP_ENABLE_SAFE_NONTRADE_WRITES"
SAFE_NONTRADE_WRITES_ACK: Final = "SIM_SAFE_NO_MONEY_SIDE_EFFECTS_ONLY"
NONTRADE_SERVICE_CLASSES: Final[dict[str, str]] = {
    "Asset Transfers": "money_or_asset_movement",
    "Client Management": "client_or_account_setup_change",
    "Partner Integration": "partner_action_on_behalf",
    "Regulatory Services": "regulatory_data_change",
    "Disclaimer Management": "disclaimer_state_change_requires_todo_6",
}
SERVICE_ALIASES: Final[dict[str, str]] = {
    "asset-transfers": "Asset Transfers",
    "asset transfers": "Asset Transfers",
    "client-management": "Client Management",
    "client management": "Client Management",
    "partner-integration": "Partner Integration",
    "partner integration": "Partner Integration",
    "regulatory-services": "Regulatory Services",
    "regulatory services": "Regulatory Services",
    "disclaimer-management": "Disclaimer Management",
    "disclaimer management": "Disclaimer Management",
}
SAFE_NONTRADE_WRITE_OPERATION_IDS: Final[frozenset[str]] = frozenset()


def service_group_for_slug(service: str) -> str | None:
    normalized = service.strip().lower().replace("_", "-")
    if normalized in SERVICE_ALIASES:
        return SERVICE_ALIASES[normalized]
    for group in NONTRADE_SERVICE_CLASSES:
        if normalized == group.lower():
            return group
    return None


def nontrade_write_operations(
    service_group: str | None = None,
    *,
    inventory: EndpointInventory | None = None,
) -> tuple[EndpointOperation, ...]:
    source = load_inventory() if inventory is None else inventory
    return tuple(
        operation
        for operation in source.operations
        if operation.service_group in NONTRADE_SERVICE_CLASSES
        and operation.read_write_class == "write_or_subscription"
        and operation.method != "GET"
        and (service_group is None or operation.service_group == service_group)
    )


def safe_nontrade_write_operations(
    *,
    inventory: EndpointInventory | None = None,
) -> tuple[EndpointOperation, ...]:
    if not safe_nontrade_writes_enabled():
        return ()
    return tuple(
        operation
        for operation in nontrade_write_operations(inventory=inventory)
        if operation.operation_id in SAFE_NONTRADE_WRITE_OPERATION_IDS
    )


def safe_nontrade_writes_enabled() -> bool:
    return os.environ.get(SAFE_NONTRADE_WRITES_ENV) == SAFE_NONTRADE_WRITES_ACK


def nontrade_write_operation_for_id(operation_id: str) -> EndpointOperation | None:
    wanted = operation_id.strip()
    return next(
        (
            operation
            for operation in nontrade_write_operations()
            if operation.operation_id == wanted
        ),
        None,
    )


def nontrade_safety_class(operation: EndpointOperation) -> str:
    return NONTRADE_SERVICE_CLASSES.get(
        operation.service_group,
        "unclassified_write_fail_closed",
    )


def nontrade_refusal_reason(operation: EndpointOperation) -> str:
    if operation.operation_id in SAFE_NONTRADE_WRITE_OPERATION_IDS:
        return ""
    if operation.service_group in NONTRADE_SERVICE_CLASSES:
        return NONTRADE_REFUSAL_REASON
    return "unclassified_nontrade_write_service"


def first_nontrade_write_operation(service_group: str) -> EndpointOperation | None:
    return next(iter(nontrade_write_operations(service_group)), None)


def nontrade_classification_rows() -> list[dict[str, JsonValue]]:
    return [
        {
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "method": operation.method,
            "path_template": operation.path_template,
            "safety_class": nontrade_safety_class(operation),
            "safe_for_sim_without_money_side_effect": (
                operation.operation_id in SAFE_NONTRADE_WRITE_OPERATION_IDS
            ),
            "registry_status": operation.status,
            "registry_refusal_reason": operation.refusal_reason,
            "policy_refusal_reason": nontrade_refusal_reason(operation),
        }
        for operation in nontrade_write_operations()
    ]


def all_nontrade_writes_are_refused() -> bool:
    return all(operation.status == "refused" for operation in nontrade_write_operations())
