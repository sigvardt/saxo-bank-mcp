from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.saxo_http_error_info import SaxoErrorCode
from saxo_bank_mcp.strict_json import parse_json_value

_ORDER_IDENTIFIER_KEYS: Final = frozenset(
    {"MultiLegOrderId", "OrderId", "OrderIds"},
)
_JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
_RESERVED_QUALIFIER_KEY_PARTS: Final = ("disclaim", "error", "order", "result")


class PreTradeDisclaimers(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    disclaimer_context: str | None = Field(default=None, alias="DisclaimerContext")
    disclaimer_tokens: list[str] = Field(alias="DisclaimerTokens")


class PrecheckErrorInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    error_code: SaxoErrorCode = Field(alias="ErrorCode")
    message: str | None = Field(default=None, alias="Message")
    pretrade_disclaimers: PreTradeDisclaimers | None = Field(
        default=None,
        alias="PreTradeDisclaimers",
    )


class PrecheckOrderResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cost: dict[str, JsonValue] | None = Field(default=None, alias="Cost")
    cost_in_account_currency: dict[str, JsonValue] | None = Field(
        default=None,
        alias="CostInAccountCurrency",
    )
    error_info: PrecheckErrorInfo | None = Field(default=None, alias="ErrorInfo")
    estimated_cash_required: float | None = Field(
        default=None,
        alias="EstimatedCashRequired",
        allow_inf_nan=False,
    )
    estimated_cash_required_currency: str | None = Field(
        default=None,
        alias="EstimatedCashRequiredCurrency",
        pattern=r"^[A-Z]{3}$",
    )
    estimated_total_cost: float | None = Field(
        default=None,
        alias="EstimatedTotalCost",
        allow_inf_nan=False,
    )
    estimated_total_cost_in_account_currency: float | None = Field(
        default=None,
        alias="EstimatedTotalCostInAccountCurrency",
        allow_inf_nan=False,
    )
    external_reference: str | None = Field(default=None, alias="ExternalReference")
    instrument_to_account_conversion_rate: float | None = Field(
        default=None,
        alias="InstrumentToAccountConversionRate",
        allow_inf_nan=False,
    )
    margin_impact_buy_sell: dict[str, JsonValue] | None = Field(
        default=None,
        alias="MarginImpactBuySell",
    )
    precheck_result: Literal["Ok"] | None = Field(default=None, alias="PreCheckResult")
    pretrade_disclaimers: PreTradeDisclaimers | None = Field(
        default=None,
        alias="PreTradeDisclaimers",
    )

    @field_validator("cost", "cost_in_account_currency", "margin_impact_buy_sell")
    @classmethod
    def reject_nested_acceptance_signals(
        cls,
        value: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue] | None:
        if value is not None and _contains_reserved_qualifier_key(value):
            raise PydanticCustomError(
                "reserved_qualifier_key",
                "free-form qualifier objects must not contain acceptance signals",
            )
        return value


class LivePrecheckResponse(PrecheckOrderResult):
    orders: list[PrecheckOrderResult] = Field(default_factory=list, alias="Orders")


_PRECHECK_ADAPTER: Final = TypeAdapter(LivePrecheckResponse)


def parse_precheck_response(content: bytes) -> LivePrecheckResponse:
    return _PRECHECK_ADAPTER.validate_python(parse_json_value(content), strict=True)


def contains_order_identifier(content: bytes) -> bool:
    payload = _JSON_OBJECT_ADAPTER.validate_python(parse_json_value(content), strict=True)
    return _contains_identifier(payload)


def _contains_reserved_qualifier_key(value: JsonValue) -> bool:
    if isinstance(value, Mapping):
        return any(_is_reserved_qualifier_key(key) for key in value) or any(
            _contains_reserved_qualifier_key(item) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(_contains_reserved_qualifier_key(item) for item in value)
    return False


def _normalized_key(key: str) -> str:
    return "".join(character.lower() for character in key if character.isalnum())


def _is_reserved_qualifier_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _RESERVED_QUALIFIER_KEY_PARTS)


def _contains_identifier(value: JsonValue) -> bool:
    if isinstance(value, Mapping):
        return bool(_ORDER_IDENTIFIER_KEYS.intersection(value)) or any(
            _contains_identifier(item) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(_contains_identifier(item) for item in value)
    return False
