from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from functools import cache
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from saxo_bank_mcp.strict_json import parse_json_value

type FingerprintScope = Literal["account_money_state_fields", "raw_response_body"]
type StrictFiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class TransactionsNotBookedDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    accrual: StrictFiniteFloat | None = Field(default=None, alias="Accrual")
    additional_transaction_cost: StrictFiniteFloat | None = Field(
        default=None,
        alias="AdditionalTransactionCost",
    )
    bond_value: StrictFiniteFloat | None = Field(default=None, alias="BondValue")
    cash_deposit: StrictFiniteFloat | None = Field(default=None, alias="CashDeposit")
    cash_reservation: StrictFiniteFloat | None = Field(
        default=None,
        alias="CashReservation",
    )
    cash_withdrawal: StrictFiniteFloat | None = Field(
        default=None,
        alias="CashWithdrawal",
    )
    certificates_value: StrictFiniteFloat | None = Field(
        default=None,
        alias="CertificatesValue",
    )
    commission: StrictFiniteFloat | None = Field(default=None, alias="Commission")
    exchange_fee: StrictFiniteFloat | None = Field(default=None, alias="ExchangeFee")
    external_charges: StrictFiniteFloat | None = Field(
        default=None,
        alias="ExternalCharges",
    )
    funds_reserved_by_order: StrictFiniteFloat | None = Field(
        default=None,
        alias="FundsReservedByOrder",
    )
    ipo_subscription_fee: StrictFiniteFloat | None = Field(
        default=None,
        alias="IpoSubscriptionFee",
    )
    leveraged_knock_out_products_value: StrictFiniteFloat | None = Field(
        default=None,
        alias="LeveragedKnockOutProductsValue",
    )
    mutual_fund_value: StrictFiniteFloat | None = Field(
        default=None,
        alias="MutualFundValue",
    )
    option_premium: StrictFiniteFloat | None = Field(
        default=None,
        alias="OptionPremium",
    )
    share_value: StrictFiniteFloat | None = Field(default=None, alias="ShareValue")
    stamp_duty: StrictFiniteFloat | None = Field(default=None, alias="StampDuty")
    warrant_premium: StrictFiniteFloat | None = Field(
        default=None,
        alias="WarrantPremium",
    )


class SpendingPowerDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    current: StrictFiniteFloat | None = Field(default=None, alias="Current")
    maximum: StrictFiniteFloat | None = Field(default=None, alias="Maximum")


class AccountMoneyState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    currency: str = Field(alias="Currency", min_length=1)
    cash_balance: StrictFiniteFloat = Field(alias="CashBalance")
    cash_available_for_trading: StrictFiniteFloat | None = Field(
        default=None,
        alias="CashAvailableForTrading",
    )
    cash_blocked: StrictFiniteFloat | None = Field(default=None, alias="CashBlocked")
    cash_blocked_from_withdrawal: StrictFiniteFloat | None = Field(
        default=None,
        alias="CashBlockedFromWithdrawal",
    )
    financing_accruals: StrictFiniteFloat | None = Field(
        default=None,
        alias="FinancingAccruals",
    )
    funds_available_for_settlement: StrictFiniteFloat | None = Field(
        default=None,
        alias="FundsAvailableForSettlement",
    )
    funds_reserved_for_settlement: StrictFiniteFloat | None = Field(
        default=None,
        alias="FundsReservedForSettlement",
    )
    spending_power: StrictFiniteFloat | None = Field(default=None, alias="SpendingPower")
    spending_power_detail: SpendingPowerDetail | None = Field(
        default=None,
        alias="SpendingPowerDetail",
    )
    transactions_not_booked: StrictFiniteFloat | None = Field(
        default=None,
        alias="TransactionsNotBooked",
    )
    transactions_not_booked_detail: TransactionsNotBookedDetail | None = Field(
        default=None,
        alias="TransactionsNotBookedDetail",
    )
    variation_margin_cash_balance: StrictFiniteFloat | None = Field(
        default=None,
        alias="VariationMarginCashBalance",
    )


_MONEY_STATE_ADAPTER: Final = TypeAdapter(AccountMoneyState)
_MONEY_STATE_OPERATIONS: Final = frozenset(
    {
        "get.port.v1.balances",
        "get.port.v1.balances.marginoverview",
        "get.port.v1.balances.me",
    },
)
_MONEY_STATE_HMAC_DOMAIN: Final = b"saxo-bank-mcp:account-money-state:v1\x00"


@cache
def _money_state_hmac_key(process_token: int) -> bytes:
    del process_token
    return secrets.token_bytes(32)


def response_fingerprint(
    operation_id: str,
    content: bytes,
) -> tuple[str, FingerprintScope]:
    if operation_id not in _MONEY_STATE_OPERATIONS:
        return hashlib.sha256(content).hexdigest(), "raw_response_body"
    money_state = _MONEY_STATE_ADAPTER.validate_python(
        parse_json_value(content),
        strict=True,
    )
    canonical = json.dumps(
        money_state.model_dump(mode="json", by_alias=True),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    process_key = _money_state_hmac_key(os.getpid())
    digest = hmac.digest(
        process_key,
        _MONEY_STATE_HMAC_DOMAIN + canonical,
        "sha256",
    ).hex()
    return digest, "account_money_state_fields"


def is_balance_operation(operation_id: str) -> bool:
    return operation_id.startswith("get.port.v1.balances")
