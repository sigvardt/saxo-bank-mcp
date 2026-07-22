from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SaxoEnvironment
from saxo_bank_mcp.safety_models import SafetyConfig
from saxo_bank_mcp.trading_write_registry import TradingWriteRisk
from saxo_bank_mcp.trading_write_state import TradingWriteRequest

_QUANTITY_KEYS: Final = ("Amount", "Quantity")
_FLOAT_EPSILON: Final = 0.000_000_001


def trading_write_safety_errors(
    request: TradingWriteRequest,
    risk: TradingWriteRisk,
    environment: SaxoEnvironment,
    safety: SafetyConfig,
) -> tuple[str, ...]:
    errors = list(_common_errors(request, environment, safety))
    match risk:
        case "money_moving":
            errors.extend(_money_moving_errors(request, safety))
        case "read_like_post" | "specialized_order" | "state_change" | "subscription":
            pass
    return tuple(dict.fromkeys(errors))


def _common_errors(
    request: TradingWriteRequest,
    environment: SaxoEnvironment,
    safety: SafetyConfig,
) -> tuple[str, ...]:
    errors: list[str] = []
    if safety.global_kill_switch:
        errors.append("global_kill_switch_active")
    if environment == SaxoEnvironment.LIVE and not safety.live_writes_enabled:
        errors.append("live_writes_disabled")

    accounts = _account_keys(request)
    if len(set(accounts)) > 1:
        errors.append("account_key_binding_mismatch")
    elif accounts:
        if not safety.account_allowlist:
            errors.append("account_allowlist_missing")
        elif accounts[0] not in safety.account_allowlist:
            errors.append("account_not_allowlisted")
    return tuple(errors)


def _money_moving_errors(  # noqa: C901, PLR0912
    request: TradingWriteRequest,
    safety: SafetyConfig,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not _account_keys(request):
        errors.append("account_key_missing")

    if request.instrument_uic is None:
        errors.append("instrument_uic_missing")
    else:
        request_uics = _numeric_values(request, ("Uic",))
        if request_uics and any(value != request.instrument_uic for value in request_uics):
            errors.append("instrument_uic_binding_mismatch")
        if not safety.instrument_allowlist:
            errors.append("instrument_allowlist_missing")
        elif request.instrument_uic not in safety.instrument_allowlist:
            errors.append("instrument_not_allowlisted")

    if request.quantity is None:
        errors.append("quantity_missing")
    else:
        request_quantities = _numeric_values(request, _QUANTITY_KEYS)
        if request_quantities and any(
            abs(value) != request.quantity for value in request_quantities
        ):
            errors.append("quantity_binding_mismatch")
        if request.quantity > safety.max_quantity:
            errors.append("quantity_limit_exceeded")

    if request.estimated_notional is None:
        errors.append("estimated_notional_missing")
    else:
        derived_notional = _derived_notional_lower_bound(request.request_body)
        if (
            derived_notional is not None
            and request.estimated_notional + _FLOAT_EPSILON < derived_notional
        ):
            errors.append("estimated_notional_binding_mismatch")
        if request.estimated_notional > safety.max_notional:
            errors.append("notional_limit_exceeded")
    return tuple(errors)


def _account_keys(request: TradingWriteRequest) -> tuple[str, ...]:
    values: list[str] = []
    if request.account_key is not None and request.account_key.strip():
        values.append(request.account_key.strip())
    for source in (request.request_body, request.query_parameters):
        account = source.get("AccountKey")
        if isinstance(account, str) and account.strip():
            values.append(account.strip())
    return tuple(dict.fromkeys(values))


def _numeric_values(
    request: TradingWriteRequest,
    keys: tuple[str, ...],
) -> tuple[float, ...]:
    values: list[float] = []
    for source in (request.request_body, request.query_parameters):
        values.extend(_numeric_values_from(source, keys))
    return tuple(values)


def _numeric_values_from(
    source: Mapping[str, JsonValue],
    keys: tuple[str, ...],
) -> tuple[float, ...]:
    return tuple(
        float(value)
        for key in keys
        if (value := source.get(key)) is not None
        and isinstance(value, int | float)
        and not isinstance(value, bool)
    )


def _derived_notional_lower_bound(value: JsonValue) -> float | None:
    if isinstance(value, Mapping):
        amount = _first_numeric(value, _QUANTITY_KEYS)
        price = _first_numeric(value, ("Price",))
        direct = None if amount is None or price is None else abs(amount * price)
        child_values: list[float] = []
        for item in value.values():
            child = _derived_notional_lower_bound(item)
            if child is not None:
                child_values.append(child)
        nested = sum(child_values) if child_values else None
        if direct is None:
            return nested
        if nested is None:
            return direct
        return max(direct, nested)
    if isinstance(value, list):
        children: list[float] = []
        for item in value:
            child = _derived_notional_lower_bound(item)
            if child is not None:
                children.append(child)
        return sum(children) if children else None
    return None


def _first_numeric(source: Mapping[str, JsonValue], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None
