from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp.live_precheck_collection_models import StateCollectionStructureJson

type BuySell = Literal["Buy", "Sell"]
type RegisteredFingerprintScope = Literal[
    "account_money_state_fields",
    "raw_response_body",
]
type AbortStage = Literal[
    "account_listing",
    "account_selection",
    "artifact",
    "authentication",
    "instrument_validation",
    "precheck",
    "proof_policy",
    "proof_runner",
    "state_after",
    "state_before",
]
type AbortReason = Literal[
    "account_selection_required",
    "artifact_secret_scan_failed",
    "concurrent_request_ledger",
    "instrument_identity_mismatch",
    "instrument_not_tradable",
    "instrument_read_failed",
    "instrument_response_invalid",
    "invalid_account_response",
    "live_settings_invalid",
    "live_token_not_ready",
    "precheck_binding_mismatch",
    "precheck_rejected_or_invalid",
    "request_ledger_policy_failed",
    "request_ledger_parity_failed",
    "session_request_ledger_unavailable",
    "source_provenance_incomplete",
    "state_changed",
    "state_collection_shape_invalid",
    "state_read_failed",
    "transport_boundary_capture_unavailable",
    "transport_boundary_parity_failed",
    "unexpected_internal_error",
]


class ProofOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    uic: int = Field(gt=0)
    asset_type: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9]*$")
    amount: float = Field(gt=0, allow_inf_nan=False)
    buy_sell: BuySell
    account_position: int | None = Field(default=None, ge=1)


class OpaqueAccount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    account_id: str = Field(alias="account_id", min_length=1)
    account_ref: str = Field(min_length=1, repr=False)
    active: bool


class AccountListing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    status: Literal["accounts_listed"]
    environment: Literal["LIVE"]
    accounts: list[OpaqueAccount]
    account_count: int = Field(ge=0)
    active_account_count: int = Field(ge=0)
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]


class RegisteredRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    status: Literal["passed"]
    environment: Literal["LIVE"]
    method: Literal["GET"]
    path: str
    response: str | None
    response_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_fingerprint_scope: RegisteredFingerprintScope
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]


class AccountMoneyStateScopeJson(TypedDict):
    fingerprint_scope: Literal["modeled_account_money_state_fields"]
    no_change_conclusion_requires: list[
        Literal[
            "modeled_account_money_state_fields_unchanged",
            "complete_no_write_transport_evidence",
        ]
    ]
    limitation: Literal[
        "not_proof_that_every_possible_saxo_balance_field_was_observed"
    ]

class AccountCountJson(TypedDict):
    active: int
    total: int


class StateCountJson(TypedDict):
    orders: int
    positions: int
    trade_messages: int


class UnchangedJson(TypedDict):
    account_money_state_fields: bool
    orders: bool
    orders_count: bool
    positions: bool
    positions_count: bool
    trade_messages: bool
    trade_messages_count: bool


class RequestSummaryJson(TypedDict):
    amount: float
    asset_type: str
    buy_sell: BuySell
    duration_type: Literal["DayOrder"]
    field_groups: list[str]
    manual_order: Literal[False]
    order_type: Literal["Market"]
    uic: int


class SanitizedPrecheck(TypedDict):
    status: Literal["precheck_accepted"]
    http_status: int
    precheck_result: Literal["Ok"]
    estimated_cash_required: float | None
    estimated_cash_required_currency: str | None
    estimated_total_cost_in_account_currency: float | None
    disclaimer_count: Literal[0]
    requires_disclaimer_review: Literal[False]
    root_result_explicitly_ok: Literal[True]
    child_result_count: int
    all_returned_results_explicitly_ok: Literal[True]
    disclaimer_object_present: Literal[False]
    error_object_present: Literal[False]
    precheck_request_accepted: Literal[True]
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
    request_summary: RequestSummaryJson


class PersistedPrecheck(TypedDict):
    status: Literal["precheck_accepted"]
    http_status: int
    precheck_result: Literal["Ok"]
    estimated_cash_required_value_present: bool
    estimated_cash_required_currency_present: bool
    estimated_total_cost_in_account_currency_value_present: bool
    disclaimer_count: Literal[0]
    requires_disclaimer_review: Literal[False]
    root_result_explicitly_ok: Literal[True]
    child_result_count: int
    all_returned_results_explicitly_ok: Literal[True]
    disclaimer_object_present: Literal[False]
    error_object_present: Literal[False]
    precheck_request_accepted: Literal[True]
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
    request_summary: RequestSummaryJson


class InstrumentJson(TypedDict):
    amount: float
    asset_type: str
    buy_sell: BuySell
    uic: int
    verified_tradable_before_precheck: Literal[True]


class AccountBindingJson(TypedDict):
    source: Literal["visible_account_id_and_process_scoped_ref"]
    account_id: str
    account_position: int
    selector_sha256: str


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    counts: StateCountJson
    balance_fingerprint: str
    orders_fingerprint: str
    positions_fingerprint: str
    trade_messages_fingerprint: str
    collection_structure: StateCollectionStructureJson


@dataclass(frozen=True, slots=True)
class ExecutionCompleted:
    account_counts: AccountCountJson
    account_binding: AccountBindingJson
    instrument: InstrumentJson
    before: StateSnapshot
    after: StateSnapshot
    precheck: SanitizedPrecheck


@dataclass(frozen=True, slots=True)
class ExecutionAborted:
    account_counts: AccountCountJson | None
    stage: AbortStage
    reason: AbortReason


type ExecutionOutcome = ExecutionCompleted | ExecutionAborted
