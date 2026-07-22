from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_precheck_ledger_models import SafeLedgerEvent, SafeLedgerReport
from saxo_bank_mcp.live_precheck_proof_audit import (
    exposed_ledger_allows_proof,
    ledger_allows_proof,
    request_ledgers_match,
    transport_boundary_allows_proof,
    transport_boundary_matches,
)
from saxo_bank_mcp.request_ledger import HostRole, RequestLedgerEvent, RequestPhase
from saxo_bank_mcp.transport_boundary import TransportBoundaryEvent


class StrictReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProofTraceEvent(StrictReleaseModel):
    timestamp: str = Field(min_length=1)
    phase: RequestPhase
    host_role: HostRole
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    query_names: list[str]
    query_present: bool
    status: int | None

    def request_event(self) -> RequestLedgerEvent:
        return RequestLedgerEvent(
            timestamp=self.timestamp,
            phase=self.phase,
            host_role=self.host_role,
            method=self.method,
            path=self.path,
            query_names=tuple(self.query_names),
            query_present=self.query_present,
            status=self.status,
        )

    def safe_event(self) -> SafeLedgerEvent:
        return SafeLedgerEvent(**self.model_dump())

    def boundary_event(self) -> TransportBoundaryEvent:
        return TransportBoundaryEvent(**self.model_dump())


class ProofSecretScan(StrictReleaseModel):
    clean: Literal[True]
    finding_count: Literal[0]
    pattern_classes: list[str] | None = None
    scan_error_count: Literal[0]


class ProofAccountBinding(StrictReleaseModel):
    account_id: Literal["<redacted>"]
    account_position: int | None = Field(default=None, ge=0)
    selector_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source: str | None = None


class ProofInstrument(StrictReleaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)
    asset_type: str = Field(min_length=1)
    buy_sell: Literal["Buy", "Sell"]
    uic: int = Field(gt=0)
    verified_tradable_before_precheck: Literal[True]


class ProofRequestSummary(StrictReleaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)
    asset_type: str = Field(min_length=1)
    buy_sell: Literal["Buy", "Sell"]
    duration_type: Literal["DayOrder"]
    field_groups: list[Literal["Costs", "MarginImpactBuySell"]]
    manual_order: Literal[False]
    order_type: Literal["Market"]
    uic: int = Field(gt=0)


class ProofPrecheck(StrictReleaseModel):
    status: Literal["precheck_accepted"]
    http_status: Literal[200]
    precheck_result: Literal["Ok"]
    precheck_request_accepted: Literal[True]
    root_result_explicitly_ok: Literal[True]
    all_returned_results_explicitly_ok: Literal[True]
    account_lookup_endpoint_called: Literal[True]
    instrument_lookup_endpoint_called: Literal[True]
    instrument_tradable: Literal[True]
    precheck_endpoint_called: Literal[True]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    order_placement_endpoint_called: Literal[False]
    order_change_endpoint_called: Literal[False]
    order_cancel_endpoint_called: Literal[False]
    disclaimer_response_endpoint_called: Literal[False]
    order_identifier_present: Literal[False]
    requires_order_readback: Literal[False]
    estimated_cash_required_value_present: bool
    estimated_cash_required_currency_present: bool
    estimated_total_cost_in_account_currency_value_present: bool
    disclaimer_count: Literal[0]
    requires_disclaimer_review: Literal[False]
    child_result_count: int = Field(ge=0)
    disclaimer_object_present: Literal[False]
    error_object_present: Literal[False]
    request_summary: ProofRequestSummary


class ProofAccountCounts(StrictReleaseModel):
    active: int = Field(ge=1)
    total: int = Field(ge=1)

    @model_validator(mode="after")
    def verify_active_count(self) -> ProofAccountCounts:
        if self.active > self.total:
            raise PydanticCustomError(
                "proof_account_counts",
                "active account count exceeds total account count",
            )
        return self


class ProofStateCounts(StrictReleaseModel):
    orders: int = Field(ge=0)
    positions: int = Field(ge=0)
    trade_messages: int = Field(ge=0)


class ProofLedger(StrictReleaseModel):
    status: Literal["passed"]
    ledger_complete: Literal[True]
    safe_fields_only: Literal[True]
    only_precheck_gateway_non_get: Literal[True]
    unsafe_gateway_request_detected: Literal[False]
    order_placement_endpoint_called: Literal[False]
    events: list[ProofTraceEvent] = Field(min_length=1)
    events_evicted: Literal[0]
    negative_proof_available: Literal[True]
    scope: Literal["current_mcp_session"]

    def safe_report(self) -> SafeLedgerReport:
        return SafeLedgerReport(
            status=self.status,
            scope=self.scope,
            safe_fields_only=self.safe_fields_only,
            ledger_complete=self.ledger_complete,
            events_evicted=self.events_evicted,
            negative_proof_available=self.negative_proof_available,
            only_precheck_gateway_non_get=self.only_precheck_gateway_non_get,
            unsafe_gateway_request_detected=self.unsafe_gateway_request_detected,
            order_placement_endpoint_called=self.order_placement_endpoint_called,
            events=tuple(event.safe_event() for event in self.events),
        )


class ProofTransport(StrictReleaseModel):
    collector_complete: Literal[True]
    safe_fields_only: Literal[True]
    collector_exit_code: Literal[0]
    collector_credentials_inherited: Literal[False]
    collector_process: Literal["separate_process"]
    events: list[ProofTraceEvent] = Field(min_length=1)
    transport_layer: Literal["httpx_async_base_transport"]


class ProofUnchanged(StrictReleaseModel):
    account_money_state_fields: Literal[True]
    orders: Literal[True]
    orders_count: Literal[True]
    positions: Literal[True]
    positions_count: Literal[True]
    trade_messages: Literal[True]
    trade_messages_count: Literal[True]


class ProofSource(StrictReleaseModel):
    git_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    dirty_source_sha256: dict[str, str]


class ProofReport(StrictReleaseModel):
    status: Literal["completed"]
    timestamp: str | None = None
    driver: str | None = None
    token_validity_lower_bound_seconds: int | None = Field(default=None, ge=0)
    request_ledger: list[ProofTraceEvent] = Field(min_length=1)
    mcp_request_ledger: ProofLedger
    request_ledger_parity: Literal[True]
    transport_boundary_capture: ProofTransport
    transport_boundary_parity: Literal[True]
    source: ProofSource
    trace_scope: dict[str, JsonValue] | None = None
    account_money_state_scope: dict[str, JsonValue] | None = None
    trade_readiness: Literal["precheck_only_not_order_ready"]
    does_not_verify: list[str] | None = None
    instrument: ProofInstrument
    account_binding: ProofAccountBinding
    account_counts: ProofAccountCounts
    before_counts: ProofStateCounts
    after_counts: ProofStateCounts
    unchanged: ProofUnchanged
    response_structure: dict[str, JsonValue] | None = None
    precheck: ProofPrecheck
    secret_scan: ProofSecretScan

    @model_validator(mode="after")
    def verify_trace_contract(self) -> ProofReport:
        outer = [event.request_event() for event in self.request_ledger]
        exposed = self.mcp_request_ledger.safe_report()
        boundary = [
            event.boundary_event() for event in self.transport_boundary_capture.events
        ]
        if not all(
            (
                ledger_allows_proof(outer),
                exposed_ledger_allows_proof(exposed),
                request_ledgers_match(outer, exposed),
                transport_boundary_allows_proof(boundary),
                transport_boundary_matches(boundary, outer, exposed),
            ),
        ):
            raise PydanticCustomError(
                "proof_trace_contract",
                "retained proof traces do not establish the no-placement request contract",
            )
        request = self.precheck.request_summary
        if (
            request.amount != self.instrument.amount
            or request.asset_type != self.instrument.asset_type
            or request.buy_sell != self.instrument.buy_sell
            or request.uic != self.instrument.uic
            or request.field_groups != ["Costs", "MarginImpactBuySell"]
            or self.before_counts != self.after_counts
        ):
            raise PydanticCustomError(
                "proof_fact_contract",
                "retained precheck facts do not match the executed no-purchase proof",
            )
        return self
