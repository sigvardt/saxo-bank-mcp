from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp.live_account_refs import LiveAccount
from saxo_bank_mcp.live_precheck_results import PrecheckRequestSummary

type BuySell = Literal["Buy", "Sell"]


class LiveOrderPrecheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    account_id: str | None = Field(default=None, min_length=1)
    account_ref: str | None = Field(default=None, min_length=1)
    uic: int = Field(gt=0)
    asset_type: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9]*$")
    amount: float = Field(gt=0, allow_inf_nan=False)
    buy_sell: BuySell

    @model_validator(mode="after")
    def one_account_selector(self) -> LiveOrderPrecheckRequest:
        if self.account_id is not None and self.account_ref is not None:
            raise PydanticCustomError(
                "account_selector_conflict",
                "provide account_id or account_ref, not both",
            )
        return self


class SaxoPrecheckBody(TypedDict):
    AccountKey: str
    Amount: float
    AssetType: str
    BuySell: BuySell
    FieldGroups: list[str]
    ManualOrder: bool
    OrderDuration: dict[str, str]
    OrderType: str
    Uic: int


def precheck_body(
    order: LiveOrderPrecheckRequest,
    account: LiveAccount,
) -> SaxoPrecheckBody:
    return {
        "AccountKey": account.account_key,
        "Amount": order.amount,
        "AssetType": order.asset_type,
        "BuySell": order.buy_sell,
        "FieldGroups": ["Costs", "MarginImpactBuySell"],
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "DayOrder"},
        "OrderType": "Market",
        "Uic": order.uic,
    }


def precheck_request_summary(order: LiveOrderPrecheckRequest) -> PrecheckRequestSummary:
    return {
        "amount": order.amount,
        "asset_type": order.asset_type,
        "buy_sell": order.buy_sell,
        "duration_type": "DayOrder",
        "field_groups": ["Costs", "MarginImpactBuySell"],
        "manual_order": False,
        "order_type": "Market",
        "uic": order.uic,
    }
