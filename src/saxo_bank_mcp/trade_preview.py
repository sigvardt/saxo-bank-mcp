from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal, cast

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.safety import AccountCurrencyRisk

type JsonObject = dict[str, JsonValue]
type OrderKind = Literal["single", "multileg"]
type DisclaimerState = Literal["none", "accepted", "missing", "unknown"]

TRADE_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "order placement",
    "order modification",
    "order cancellation",
    "position creation",
    "live-write permission",
)
DISCLAIMER_RESPONSE_ENDPOINT_PATH: Final = "/dm/v2/disclaimers"


def operation_id_for_order_kind(kind: OrderKind) -> str:
    return "post.trade.v2.orders" if kind == "single" else "post.trade.v2.orders.multileg"


def precheck_endpoint_for_order_kind(kind: OrderKind) -> str:
    return "/trade/v2/orders/precheck" if kind == "single" else "/trade/v2/orders/multileg/precheck"


def account_currency_risk(
    precheck: Mapping[str, JsonValue],
    order_body: Mapping[str, JsonValue],
) -> tuple[AccountCurrencyRisk, list[str], float]:
    cost = _first_number(precheck, ("EstimatedTotalCostInAccountCurrency",))
    cost_data = _object_at(precheck, "CostInAccountCurrency")
    if cost is None and cost_data is not None:
        cost = _first_number(cost_data, ("Amount", "Value", "TotalCost", "Cost"))
    cash = _first_number(precheck, ("EstimatedCashRequired",))
    margin_data = _object_at(precheck, "MarginImpactBuySell")
    margin = _first_number(precheck, ("MarginImpact",))
    if margin is None and margin_data is not None:
        margin = _first_number(
            margin_data,
            (
                "MarginImpact",
                "InitialMarginAvailableCurrent",
                "InitialMargin",
                "MaintenanceMargin",
            ),
        )
    multiplier = _first_number(order_body, ("ContractMultiplier",))
    if multiplier is None:
        multiplier = _first_number(precheck, ("ContractMultiplier",))
    conversion_rate = _first_number(precheck, ("InstrumentToAccountConversionRate",))
    reasons: list[str] = []
    if cost is None:
        reasons.append("account_currency_cost_unknown")
    if cash is None:
        reasons.append("cash_required_unknown")
    if margin is None:
        reasons.append("margin_impact_unknown")
    if multiplier is None:
        reasons.append("contract_multiplier_unknown")
    if conversion_rate is None:
        reasons.append("account_currency_conversion_unknown")
    estimated_notional = max(value for value in (cost, cash, 0.0) if value is not None)
    return (
        AccountCurrencyRisk(
            cost=cost,
            cash_required=cash,
            margin_impact=margin,
            contract_multiplier=multiplier,
            conversion_known=conversion_rate is not None,
        ),
        reasons,
        estimated_notional,
    )


def account_currency(precheck: Mapping[str, JsonValue]) -> str | None:
    value = precheck.get("EstimatedCashRequiredCurrency")
    return value.strip() if isinstance(value, str) and value.strip() else None


def order_account_key(order_body: Mapping[str, JsonValue]) -> str | None:
    value = order_body.get("AccountKey")
    return value.strip() if isinstance(value, str) and value.strip() else None


def order_instrument_uic(order_body: Mapping[str, JsonValue]) -> int | None:
    value = _first_number(order_body, ("Uic",))
    if value is None:
        legs = _sequence_at(order_body, "Legs")
        if legs:
            first = _as_object(legs[0])
            value = None if first is None else _first_number(first, ("Uic",))
    return None if value is None else int(value)


def order_quantity(order_body: Mapping[str, JsonValue]) -> float | None:
    value = _first_number(order_body, ("Amount",))
    if value is None:
        legs = _sequence_at(order_body, "Legs")
        if legs:
            first = _as_object(legs[0])
            value = None if first is None else _first_number(first, ("Amount",))
    return value


def disclaimer_tokens(precheck: Mapping[str, JsonValue]) -> tuple[str, ...]:
    container = _object_at(precheck, "PreTradeDisclaimers")
    if container is None:
        return ()
    raw_tokens = _sequence_at(container, "DisclaimerTokens")
    if raw_tokens is None:
        return ()
    return tuple(value for value in raw_tokens if isinstance(value, str) and value.strip())


def disclaimer_context(precheck: Mapping[str, JsonValue]) -> str | None:
    container = _object_at(precheck, "PreTradeDisclaimers")
    if container is None:
        return None
    value = container.get("DisclaimerContext")
    return value.strip() if isinstance(value, str) and value.strip() else None


def disclaimer_blockers(
    precheck: Mapping[str, JsonValue],
    details: Mapping[str, JsonValue] | None,
    response_state: DisclaimerState,
) -> tuple[list[str], list[JsonObject]]:
    tokens = disclaimer_tokens(precheck)
    if not tokens:
        return [], []
    if details is None:
        return ["disclaimer_details_missing"], []
    rows = _disclaimer_rows(details)
    if not rows:
        return ["disclaimer_details_missing"], []
    reasons: list[str] = []
    if any(row.get("IsBlocking") is True for row in rows):
        reasons.append("blocking_disclaimer")
    if response_state != "accepted":
        reasons.append("disclaimer_response_required")
    return reasons, rows


def _disclaimer_rows(details: Mapping[str, JsonValue]) -> list[JsonObject]:
    data = _sequence_at(details, "Data")
    if data is None:
        object_value = _as_object(details)
        return [] if object_value is None else [object_value]
    return [row for value in data if (row := _as_object(value)) is not None]


def _object_at(data: Mapping[str, JsonValue], key: str) -> JsonObject | None:
    return _as_object(data.get(key))


def _sequence_at(data: Mapping[str, JsonValue], key: str) -> Sequence[JsonValue] | None:
    value = data.get(key)
    if isinstance(value, str):
        return None
    return cast("Sequence[JsonValue]", value) if isinstance(value, Sequence) else None


def _as_object(value: JsonValue | object) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    return dict(cast("Mapping[str, JsonValue]", value))


def _first_number(data: Mapping[str, JsonValue], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return float(value)
    return None
