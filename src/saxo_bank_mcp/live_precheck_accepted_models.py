from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_precheck_proof_models import BuySell, SanitizedPrecheck
from saxo_bank_mcp.live_precheck_results import NON_PLACEMENT_TRADE_BLOCKERS


class PrecheckRequestSummaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    amount: float = Field(gt=0, allow_inf_nan=False)
    asset_type: str = Field(min_length=1)
    buy_sell: BuySell
    duration_type: Literal["DayOrder"]
    field_groups: list[Literal["Costs", "MarginImpactBuySell"]]
    manual_order: Literal[False]
    order_type: Literal["Market"]
    uic: int = Field(gt=0)


class AcceptedPrecheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    status: Literal["precheck_accepted"]
    tool_name: Literal["saxo_precheck_live_order"]
    environment: Literal["LIVE"]
    endpoint_path: Literal["/trade/v2/orders/precheck"]
    access_level: Literal["Personal:Read"]
    network_call_made: Literal[True]
    trade_readiness: Literal["not_assessed"]
    account_key_redacted: Literal[True]
    account_refs_process_scoped: Literal[True]
    account_lookup_endpoint_called: Literal[True]
    instrument_lookup_endpoint_called: Literal[True]
    instrument_tradable: Literal[True]
    precheck_endpoint_called: Literal[True]
    order_placement_endpoint_called: Literal[False]
    order_change_endpoint_called: Literal[False]
    order_cancel_endpoint_called: Literal[False]
    disclaimer_response_endpoint_called: Literal[False]
    order_identifier_present: Literal[False]
    requires_order_readback: Literal[False]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    precheck_blockers: list[JsonValue] = Field(max_length=0)
    trade_blockers: list[
        Literal[
            "trade_readiness_not_assessed",
            "live_write_disabled",
            "human_approval_required",
        ]
    ] = Field(min_length=3, max_length=3)
    does_not_verify: list[
        Literal[
            "order placement",
            "order cancellation",
            "real-money write readiness",
        ]
    ] = Field(min_length=3, max_length=3)
    http_status: Literal[200]
    precheck_result: Literal["Ok"]
    error_code: Literal[""]
    estimated_cash_required: float | None = Field(default=None, allow_inf_nan=False)
    estimated_cash_required_currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{3}$",
    )
    estimated_total_cost_in_account_currency: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    disclaimer_count: Literal[0]
    requires_disclaimer_review: Literal[False]
    root_result_explicitly_ok: Literal[True]
    child_result_count: int = Field(ge=0)
    all_returned_results_explicitly_ok: Literal[True]
    disclaimer_object_present: Literal[False]
    error_object_present: Literal[False]
    precheck_request_accepted: Literal[True]
    account_id: str = Field(min_length=1)
    account_ref: str = Field(min_length=1, repr=False)
    request_summary: PrecheckRequestSummaryModel

    @field_validator("trade_blockers")
    @classmethod
    def require_non_placement_blockers(cls, value: list[str]) -> list[str]:
        if value != list(NON_PLACEMENT_TRADE_BLOCKERS):
            raise PydanticCustomError(
                "placement_blockers_invalid",
                "accepted precheck must retain all placement blockers",
            )
        return value

    def sanitized(self) -> SanitizedPrecheck:
        return {
            "status": self.status,
            "http_status": self.http_status,
            "precheck_result": self.precheck_result,
            "estimated_cash_required": self.estimated_cash_required,
            "estimated_cash_required_currency": self.estimated_cash_required_currency,
            "estimated_total_cost_in_account_currency": (
                self.estimated_total_cost_in_account_currency
            ),
            "disclaimer_count": self.disclaimer_count,
            "requires_disclaimer_review": self.requires_disclaimer_review,
            "root_result_explicitly_ok": self.root_result_explicitly_ok,
            "child_result_count": self.child_result_count,
            "all_returned_results_explicitly_ok": (self.all_returned_results_explicitly_ok),
            "disclaimer_object_present": self.disclaimer_object_present,
            "error_object_present": self.error_object_present,
            "precheck_request_accepted": self.precheck_request_accepted,
            "account_lookup_endpoint_called": self.account_lookup_endpoint_called,
            "instrument_lookup_endpoint_called": self.instrument_lookup_endpoint_called,
            "instrument_tradable": self.instrument_tradable,
            "precheck_endpoint_called": self.precheck_endpoint_called,
            "order_placement_endpoint_called": self.order_placement_endpoint_called,
            "order_change_endpoint_called": self.order_change_endpoint_called,
            "order_cancel_endpoint_called": self.order_cancel_endpoint_called,
            "disclaimer_response_endpoint_called": self.disclaimer_response_endpoint_called,
            "order_identifier_present": self.order_identifier_present,
            "requires_order_readback": self.requires_order_readback,
            "live_write_called": self.live_write_called,
            "order_or_subscription_created": self.order_or_subscription_created,
            "request_summary": {
                "amount": self.request_summary.amount,
                "asset_type": self.request_summary.asset_type,
                "buy_sell": self.request_summary.buy_sell,
                "duration_type": self.request_summary.duration_type,
                "field_groups": list(self.request_summary.field_groups),
                "manual_order": self.request_summary.manual_order,
                "order_type": self.request_summary.order_type,
                "uic": self.request_summary.uic,
            },
        }
