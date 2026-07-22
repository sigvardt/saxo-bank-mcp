from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue

type JsonObject = dict[str, JsonValue]
type OrderWriteClass = Literal[
    "place",
    "modify",
    "cancel",
    "cancel-by-instrument",
    "multileg-place",
    "multileg-modify",
    "multileg-cancel",
]
type OrderWriteMethod = Literal["POST", "PATCH", "DELETE"]
type OrderWriteOutcome = Literal[
    "success",
    "failed",
    "partial_success",
    "unknown_state",
    "rate_limited",
]

UNKNOWN_STATE_ERROR_CODES: Final = frozenset(
    {"OrderCommandPending", "OrderCommandTimeout", "TradeNotCompleted"},
)
DUPLICATE_ERROR_CODES: Final = frozenset({"DuplicateRequest", "RepeatTradeOnAutoQuote"})
HTTP_DUPLICATE_REQUEST: Final = 409
HTTP_ACCEPTED: Final = 202
HTTP_RATE_LIMITED: Final = 429
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class OrderWriteSpec:
    write_class: OrderWriteClass
    tool_name: str
    operation_id: str
    method: OrderWriteMethod
    endpoint_path: str
    route_key: str | None
    query_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedOrderWriteResponse:
    outcome: OrderWriteOutcome
    order_ids: tuple[str, ...]
    error_codes: tuple[str, ...]
    needs_readback: bool
    partial_success: bool
    trade_not_completed: bool
    duplicate_request: bool
    rate_limited: bool

    def to_json_value(self) -> JsonObject:
        return {
            "outcome": self.outcome,
            "order_ids": list(self.order_ids),
            "error_codes": list(self.error_codes),
            "needs_readback": self.needs_readback,
            "partial_success": self.partial_success,
            "trade_not_completed": self.trade_not_completed,
            "duplicate_request": self.duplicate_request,
            "rate_limited": self.rate_limited,
        }


ORDER_WRITE_SPECS: Final[Mapping[OrderWriteClass, OrderWriteSpec]] = MappingProxyType(
    {
        "place": OrderWriteSpec(
            write_class="place",
            tool_name="saxo_place_sim_order",
            operation_id="post.trade.v2.orders",
            method="POST",
            endpoint_path="/trade/v2/orders",
            route_key=None,
            query_keys=(),
        ),
        "modify": OrderWriteSpec(
            write_class="modify",
            tool_name="saxo_modify_sim_order",
            operation_id="patch.trade.v2.orders",
            method="PATCH",
            endpoint_path="/trade/v2/orders",
            route_key=None,
            query_keys=(),
        ),
        "cancel": OrderWriteSpec(
            write_class="cancel",
            tool_name="saxo_cancel_sim_order",
            operation_id="delete.trade.v2.orders.orderids",
            method="DELETE",
            endpoint_path="/trade/v2/orders/{OrderIds}",
            route_key="OrderIds",
            query_keys=("AccountKey",),
        ),
        "cancel-by-instrument": OrderWriteSpec(
            write_class="cancel-by-instrument",
            tool_name="saxo_cancel_sim_orders_by_instrument",
            operation_id="delete.trade.v2.orders",
            method="DELETE",
            endpoint_path="/trade/v2/orders",
            route_key=None,
            query_keys=("AccountKey", "AssetType", "Uic"),
        ),
        "multileg-place": OrderWriteSpec(
            write_class="multileg-place",
            tool_name="saxo_place_multileg_sim_order",
            operation_id="post.trade.v2.orders.multileg",
            method="POST",
            endpoint_path="/trade/v2/orders/multileg",
            route_key=None,
            query_keys=(),
        ),
        "multileg-modify": OrderWriteSpec(
            write_class="multileg-modify",
            tool_name="saxo_modify_multileg_sim_order",
            operation_id="patch.trade.v2.orders.multileg",
            method="PATCH",
            endpoint_path="/trade/v2/orders/multileg",
            route_key=None,
            query_keys=(),
        ),
        "multileg-cancel": OrderWriteSpec(
            write_class="multileg-cancel",
            tool_name="saxo_cancel_multileg_sim_order",
            operation_id="delete.trade.v2.orders.multileg.multilegorderid",
            method="DELETE",
            endpoint_path="/trade/v2/orders/multileg/{MultiLegOrderId}",
            route_key="MultiLegOrderId",
            query_keys=("AccountKey",),
        ),
    },
)
PRODUCTION_ORDER_TOOL_NAMES: Final[Mapping[OrderWriteClass, str]] = MappingProxyType(
    {
        "place": "saxo_place_order",
        "modify": "saxo_modify_order",
        "cancel": "saxo_cancel_order",
        "cancel-by-instrument": "saxo_cancel_orders_by_instrument",
        "multileg-place": "saxo_place_multileg_order",
        "multileg-modify": "saxo_modify_multileg_order",
        "multileg-cancel": "saxo_cancel_multileg_order",
    },
)
ORDER_WRITE_CLASSES: Final[tuple[OrderWriteClass, ...]] = tuple(ORDER_WRITE_SPECS)


def parse_order_mutation_response(
    payload: Mapping[str, JsonValue],
    *,
    http_status: int,
) -> ParsedOrderWriteResponse:
    order_ids = _unique_strings(
        (
            *_strings_at(payload, "OrderId"),
            *(
                item
                for row in _objects_at(payload, "Orders")
                for item in _strings_at(row, "OrderId")
            ),
        ),
    )
    error_codes = _unique_strings(
        (
            *_error_codes_from(payload),
            *(
                item
                for row in _objects_at(payload, "Orders")
                for item in _error_codes_from(row)
            ),
        ),
    )
    trade_not_completed = "TradeNotCompleted" in error_codes
    partial_success = bool(order_ids) and bool(error_codes)
    rate_limited = http_status == HTTP_RATE_LIMITED
    unknown_state = http_status == HTTP_ACCEPTED or bool(
        UNKNOWN_STATE_ERROR_CODES.intersection(error_codes),
    )
    duplicate_request = http_status == HTTP_DUPLICATE_REQUEST or bool(
        DUPLICATE_ERROR_CODES.intersection(error_codes),
    )
    outcome = _outcome(
        http_status=http_status,
        has_order_id=bool(order_ids),
        has_error=bool(error_codes),
        partial_success=partial_success,
        unknown_state=unknown_state,
        rate_limited=rate_limited,
    )
    return ParsedOrderWriteResponse(
        outcome=outcome,
        order_ids=order_ids,
        error_codes=error_codes,
        needs_readback=partial_success or unknown_state or duplicate_request,
        partial_success=partial_success,
        trade_not_completed=trade_not_completed,
        duplicate_request=duplicate_request,
        rate_limited=rate_limited,
    )


def _outcome(  # noqa: PLR0913
    *,
    http_status: int,
    has_order_id: bool,
    has_error: bool,
    partial_success: bool,
    unknown_state: bool,
    rate_limited: bool,
) -> OrderWriteOutcome:
    if rate_limited:
        return "rate_limited"
    if unknown_state:
        return "unknown_state"
    if partial_success:
        return "partial_success"
    if has_error:
        return "failed"
    if has_order_id or HTTP_SUCCESS_MIN <= http_status < HTTP_SUCCESS_MAX:
        return "success"
    return "failed"


def _objects_at(data: Mapping[str, JsonValue], key: str) -> tuple[JsonObject, ...]:
    value = data.get(key)
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(row for item in value if (row := _as_object(item)) is not None)


def _as_object(value: object) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _strings_at(data: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    value = data.get(key)
    return (value.strip(),) if isinstance(value, str) and value.strip() else ()


def _error_codes_from(data: Mapping[str, JsonValue]) -> tuple[str, ...]:
    error = _as_object(data.get("ErrorInfo"))
    if error is None:
        return ()
    return (*_strings_at(error, "ErrorCode"), *_strings_at(error, "Code"))


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return tuple(unique)
