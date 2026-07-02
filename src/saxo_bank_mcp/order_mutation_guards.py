from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.order_mutation_models import JsonObject, OrderWriteSpec
from saxo_bank_mcp.safety_models import WritePreviewRequest

JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
FLOAT_EPSILON: Final = 0.000_000_001


def request_body_coherence_reasons(
    spec: OrderWriteSpec,
    request: WritePreviewRequest,
) -> tuple[str, ...]:
    body = request.request_body
    reasons = [*_account_key_reasons(body, request.account_key)]
    body_uic = _instrument_uic(body)
    if _requires_instrument(spec):
        reasons.extend(_required_int_reasons(body_uic, request.instrument_uic, "instrument_uic"))
    if body_uic is not None and body_uic != request.instrument_uic:
        reasons.append("request_body_instrument_uic_mismatch")

    asset_type_reasons = _asset_type_reasons(spec, body)
    reasons.extend(asset_type_reasons)

    body_quantity = _quantity(body)
    if _requires_quantity(spec):
        reasons.extend(_required_float_reasons(body_quantity, request.quantity, "quantity"))
    if body_quantity is not None and not _same_number(body_quantity, request.quantity):
        reasons.append("request_body_quantity_mismatch")
    return tuple(reasons)


def _account_key_reasons(
    body: Mapping[str, JsonValue],
    expected_account_key: str,
) -> tuple[str, ...]:
    account_key = _string(body.get("AccountKey"))
    if account_key is None:
        return ("request_body_account_key_missing",)
    if account_key != expected_account_key:
        return ("request_body_account_key_mismatch",)
    return ()


def _asset_type_reasons(
    spec: OrderWriteSpec,
    body: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    if "AssetType" not in spec.query_keys and spec.write_class not in {"place", "modify"}:
        return ()
    asset_type = _string(body.get("AssetType"))
    if asset_type is None:
        return ("request_body_asset_type_missing",)
    return ()


def _required_int_reasons(value: int | None, expected: int, field: str) -> tuple[str, ...]:
    if value is None:
        return (f"request_body_{field}_missing",)
    if value != expected:
        return (f"request_body_{field}_mismatch",)
    return ()


def _required_float_reasons(value: float | None, expected: float, field: str) -> tuple[str, ...]:
    if value is None:
        return (f"request_body_{field}_missing",)
    if not _same_number(value, expected):
        return (f"request_body_{field}_mismatch",)
    return ()


def _requires_instrument(spec: OrderWriteSpec) -> bool:
    return spec.write_class in {
        "place",
        "modify",
        "cancel-by-instrument",
        "multileg-place",
        "multileg-modify",
    }


def _requires_quantity(spec: OrderWriteSpec) -> bool:
    return spec.write_class in {
        "place",
        "modify",
        "multileg-place",
        "multileg-modify",
    }


def _instrument_uic(body: Mapping[str, JsonValue]) -> int | None:
    value = _number(body.get("Uic"))
    if value is not None:
        return int(value)
    return _first_leg_int(body, "Uic")


def _quantity(body: Mapping[str, JsonValue]) -> float | None:
    value = _number(body.get("Amount"))
    if value is not None:
        return value
    return _first_leg_number(body, "Amount")


def _first_leg_int(body: Mapping[str, JsonValue], key: str) -> int | None:
    value = _first_leg_number(body, key)
    return None if value is None else int(value)


def _first_leg_number(body: Mapping[str, JsonValue], key: str) -> float | None:
    legs = body.get("Legs")
    if isinstance(legs, str) or not isinstance(legs, Sequence) or not legs:
        return None
    first = _object(legs[0])
    if first is None:
        return None
    return _number(first.get(key))


def _object(value: object) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _string(value: JsonValue | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: JsonValue | None) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _same_number(left: float, right: float) -> bool:
    return abs(left - right) <= FLOAT_EPSILON
