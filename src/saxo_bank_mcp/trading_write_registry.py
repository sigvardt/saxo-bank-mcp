from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Final, Literal

from saxo_bank_mcp.endpoint_registry import EndpointMethod, EndpointOperation, load_inventory

type TradingWriteRisk = Literal[
    "specialized_order",
    "money_moving",
    "state_change",
    "subscription",
    "read_like_post",
]

SPECIALIZED_ORDER_TOOLS: Final[dict[str, str]] = {
    "post.trade.v2.orders": "saxo_place_order",
    "patch.trade.v2.orders": "saxo_modify_order",
    "delete.trade.v2.orders": "saxo_cancel_orders_by_instrument",
    "delete.trade.v2.orders.orderids": "saxo_cancel_order",
    "post.trade.v2.orders.multileg": "saxo_place_multileg_order",
    "patch.trade.v2.orders.multileg": "saxo_modify_multileg_order",
    "delete.trade.v2.orders.multileg.multilegorderid": "saxo_cancel_multileg_order",
}
_MONEY_MOVING_SERVICES: Final = frozenset({"Positions", "v1 Trades", "v2 Trades"})


@dataclass(frozen=True, slots=True)
class TradingWriteSpec:
    operation_id: str
    method: EndpointMethod
    path_template: str
    service: str
    documentation_url: str
    cleanup_rule: str | None
    risk: TradingWriteRisk
    path_parameter_names: tuple[str, ...]
    query_parameter_names: tuple[str, ...]
    required_query_parameter_names: tuple[str, ...]
    specialized_tool: str | None


@cache
def trading_write_specs() -> tuple[TradingWriteSpec, ...]:
    return tuple(
        _spec(operation)
        for operation in load_inventory().operations
        if operation.service_group == "Trading" and operation.method != "GET"
    )


def trading_write_spec(operation_id: str) -> TradingWriteSpec | None:
    return next(
        (spec for spec in trading_write_specs() if spec.operation_id == operation_id),
        None,
    )


def _spec(operation: EndpointOperation) -> TradingWriteSpec:
    specialized_tool = SPECIALIZED_ORDER_TOOLS.get(operation.operation_id)
    return TradingWriteSpec(
        operation_id=operation.operation_id,
        method=operation.method,
        path_template=operation.path_template,
        service=operation.service,
        documentation_url=operation.documentation_url,
        cleanup_rule=operation.cleanup_rule,
        risk=_risk(operation, specialized_tool),
        path_parameter_names=_placeholder_names(operation.path_template),
        query_parameter_names=_query_names(operation.query_template),
        required_query_parameter_names=_required_query_names(operation),
        specialized_tool=specialized_tool,
    )


def _risk(
    operation: EndpointOperation,
    specialized_tool: str | None,
) -> TradingWriteRisk:
    if specialized_tool is not None:
        return "specialized_order"
    if operation.service in _MONEY_MOVING_SERVICES:
        return "money_moving"
    if "/subscriptions" in operation.path_template:
        return "subscription"
    if operation.operation_id in {
        "post.trade.v2.orders.precheck",
        "post.trade.v2.orders.multileg.precheck",
        "post.trade.v1.prices.multileg",
    }:
        return "read_like_post"
    return "state_change"


def _placeholder_names(template: str) -> tuple[str, ...]:
    return tuple(
        part[1:-1]
        for part in template.split("/")
        if part.startswith("{") and part.endswith("}")
    )


def _query_names(template: str) -> tuple[str, ...]:
    if not template:
        return ()
    return tuple(part.split("=", 1)[0] for part in template.split("&"))


def _required_query_names(operation: EndpointOperation) -> tuple[str, ...]:
    names = _query_names(operation.query_template)
    if operation.operation_id == "put.trade.v1.messages.seen":
        return names
    return ()
