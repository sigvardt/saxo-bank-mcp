from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_fx import FxNormalizationError, FxQuote, convert_amount
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    IsoCurrencyCode,
    QualityState,
    SafeAccountScope,
    Sha256Fingerprint,
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioPublicEvidence,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)

_SOURCE_SCOPE: Final = "saxo_openapi"
_COST_SOURCE_CONTRACTS: Final = (
    "transactions_v1",
    "bookings_v1",
    "closed_positions_history_v1",
    "costs_v1",
)
_MONEY_TOLERANCE: Final = Decimal("0.01")
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type CostDifferenceReason = Literal[
    "booking_correction",
    "fx_translation",
    "holding_period",
    "partial_fill_rounding",
    "quote_timing",
]


class CostComponent(StrEnum):
    """Frozen calculation roles for cost and turnover bookings."""

    COMMISSION = "commission"
    SPREAD = "spread"
    FX_CONVERSION = "fx_conversion"
    FINANCING = "financing"
    BORROW = "borrow"
    CUSTODY = "custody"
    TAX = "tax"
    TURNOVER = "turnover"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class CostBooking(_StrictModel):
    """One signed Saxo booking revision with only safe source identities."""

    account_alias: SafeAccountScope
    event_key_sha256: Sha256Fingerprint
    revision: int = Field(ge=1)
    occurred_at: UtcDateTime
    component: CostComponent
    source_amount: Decimal = Field(allow_inf_nan=False)
    currency: IsoCurrencyCode
    fill_key_sha256: Sha256Fingerprint | None


class CostComponents(_StrictModel):
    """Positive cost magnitudes plus turnover, with an exact total identity."""

    commission: Decimal = Field(ge=0, allow_inf_nan=False)
    spread: Decimal = Field(ge=0, allow_inf_nan=False)
    fx_conversion: Decimal = Field(ge=0, allow_inf_nan=False)
    financing: Decimal = Field(ge=0, allow_inf_nan=False)
    borrow: Decimal = Field(ge=0, allow_inf_nan=False)
    custody: Decimal = Field(ge=0, allow_inf_nan=False)
    tax: Decimal = Field(ge=0, allow_inf_nan=False)
    turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    total_cost: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        expected = (
            self.commission
            + self.spread
            + self.fx_conversion
            + self.financing
            + self.borrow
            + self.custody
            + self.tax
        )
        if self.total_cost != expected:
            raise ValueError("cost components must sum exactly to total cost")
        return self


class SaxoCostIllustration(_StrictModel):
    """The exact fields available from the frozen Saxo cost illustration."""

    commission: Decimal = Field(ge=0, allow_inf_nan=False)
    stamp_duty: Decimal = Field(ge=0, allow_inf_nan=False)
    total_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: IsoCurrencyCode

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_cost < self.commission + self.stamp_duty:
            raise ValueError("Saxo total cost cannot be below its disclosed components")
        return self


class NamedCostDifference(_StrictModel):
    """One exact reporting-currency difference against the Saxo illustration."""

    calculated_minus_saxo: Decimal = Field(allow_inf_nan=False)
    reason_code: CostDifferenceReason


class CostReconciliation(_StrictModel):
    """Value-free state of the eligible Saxo illustration comparison."""

    state: Literal["exact", "named_difference", "unavailable"]
    reason_codes: tuple[ContractName, ...]


class PrivateCostXray(_StrictModel):
    """Owner-only normalized costs and turnover."""

    reporting_currency: IsoCurrencyCode
    components: CostComponents
    partial_fill_count: int = Field(ge=0)
    reconciliation: CostReconciliation


class CostDataset(_StrictModel):
    """Bounded, revisioned Saxo cost material for one safe account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    bookings: tuple[CostBooking, ...]
    fx_quotes: tuple[FxQuote, ...]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]
    saxo_illustration: SaxoCostIllustration | None
    named_difference: NamedCostDifference | None

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if any(booking.account_alias != self.account_alias for booking in self.bookings):
            raise ValueError("cost booking account alias must match the dataset account alias")
        if any(booking.occurred_at > self.as_of for booking in self.bookings):
            raise ValueError("cost bookings cannot follow the dataset cutoff")
        revisions = tuple(
            (booking.event_key_sha256, booking.revision) for booking in self.bookings
        )
        if len(revisions) != len(set(revisions)):
            raise ValueError("cost booking revisions must be unique")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial cost quality must match explicit missing fields")
        return self


class CostXrayResult(_StrictModel):
    """Cost X-ray with owner money separated from public evidence."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["cost_xray"] = "cost_xray"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateCostXray | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("cost values do not match their delivery visibility")
        return self


def analyze_cost_xray(
    dataset: CostDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> CostXrayResult | ResearchRefusal:
    """Normalize Saxo cost bookings and cross-check the eligible illustration."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_COST_SOURCE_CONTRACTS,
        analysis_kind="cost_xray",
        dataset_id=dataset.dataset_id,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            "cost_data_unusable",
            "cost inputs are missing, invalid, or stale",
            dataset.dataset_id,
        )
    latest = _latest_bookings(dataset.bookings)
    if any(
        booking.component is CostComponent.TURNOVER and booking.source_amount < 0
        for booking in latest
    ):
        return _refusal(
            "turnover_sign_invalid",
            "Saxo turnover must be a non-negative magnitude",
            dataset.dataset_id,
        )
    try:
        components = _cost_components(dataset, latest)
        reconciliation = reconcile_cost_illustration(
            local_total=components.total_cost,
            reporting_currency=dataset.reporting_currency,
            illustration=dataset.saxo_illustration,
            named_difference=dataset.named_difference,
            at=dataset.as_of,
            fx_quotes=dataset.fx_quotes,
            analysis_kind="cost_xray",
            dataset_id=dataset.dataset_id,
        )
    except FxNormalizationError:
        return _refusal(
            "cost_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
            dataset.dataset_id,
        )
    if isinstance(reconciliation, ResearchRefusal):
        return reconciliation
    warnings = set(dataset.warnings)
    warnings.update(source_assessment)
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("partial_cost_history")
    if reconciliation.state == "unavailable":
        warnings.add("saxo_cost_illustration_unavailable")
    elif reconciliation.state == "named_difference":
        warnings.add("cost_illustration_named_difference")
    values = PrivateCostXray(
        reporting_currency=dataset.reporting_currency,
        components=components,
        partial_fill_count=len(
            {
                booking.fill_key_sha256
                for booking in latest
                if booking.fill_key_sha256 is not None
            },
        ),
        reconciliation=reconciliation,
    )
    return CostXrayResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="cost_xray",
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=dataset,
        ),
    )


def reconcile_cost_illustration(  # noqa: PLR0913
    *,
    local_total: Decimal,
    reporting_currency: str,
    illustration: SaxoCostIllustration | None,
    named_difference: NamedCostDifference | None,
    at: UtcDateTime,
    fx_quotes: Sequence[FxQuote],
    analysis_kind: str,
    dataset_id: str,
    instrument_handles: Sequence[str] = (),
) -> CostReconciliation | ResearchRefusal:
    """Reconcile one finite local estimate to a compatible Saxo illustration."""
    if illustration is None:
        if named_difference is not None:
            return _refusal(
                "cost_illustration_named_difference_invalid",
                "a named difference cannot be used without a Saxo illustration",
                dataset_id,
                analysis_kind=analysis_kind,
                instrument_handles=instrument_handles,
            )
        return CostReconciliation(state="unavailable", reason_codes=())
    saxo_total = convert_amount(
        illustration.total_cost,
        illustration.currency,
        reporting_currency,
        at=at,
        quotes=fx_quotes,
    )
    observed = local_total - saxo_total
    if abs(observed) <= _MONEY_TOLERANCE:
        if named_difference is not None:
            return _refusal(
                "cost_illustration_named_difference_invalid",
                "an exact Saxo reconciliation cannot carry a named difference",
                dataset_id,
                analysis_kind=analysis_kind,
                instrument_handles=instrument_handles,
            )
        return CostReconciliation(state="exact", reason_codes=())
    if (
        named_difference is None
        or abs(named_difference.calculated_minus_saxo - observed) > _MONEY_TOLERANCE
    ):
        return _refusal(
            "cost_illustration_reconciliation_unexplained",
            "the eligible local cost estimate differs from Saxo without an exact named reason",
            dataset_id,
            analysis_kind=analysis_kind,
            instrument_handles=instrument_handles,
        )
    return CostReconciliation(
        state="named_difference",
        reason_codes=(named_difference.reason_code,),
    )


def _latest_bookings(bookings: Sequence[CostBooking]) -> tuple[CostBooking, ...]:
    latest: dict[str, CostBooking] = {}
    for booking in bookings:
        current = latest.get(booking.event_key_sha256)
        if current is None or booking.revision > current.revision:
            latest[booking.event_key_sha256] = booking
    return tuple(latest[key] for key in sorted(latest))


def _cost_components(
    dataset: CostDataset,
    bookings: Sequence[CostBooking],
) -> CostComponents:
    totals = {component: Decimal(0) for component in CostComponent}
    for booking in bookings:
        normalized = convert_amount(
            (
                booking.source_amount
                if booking.component is CostComponent.TURNOVER
                else abs(booking.source_amount)
            ),
            booking.currency,
            dataset.reporting_currency,
            at=booking.occurred_at,
            quotes=dataset.fx_quotes,
        )
        totals[booking.component] += normalized
    total_cost = sum(
        (
            totals[CostComponent.COMMISSION],
            totals[CostComponent.SPREAD],
            totals[CostComponent.FX_CONVERSION],
            totals[CostComponent.FINANCING],
            totals[CostComponent.BORROW],
            totals[CostComponent.CUSTODY],
            totals[CostComponent.TAX],
        ),
        Decimal(0),
    )
    return CostComponents(
        commission=totals[CostComponent.COMMISSION],
        spread=totals[CostComponent.SPREAD],
        fx_conversion=totals[CostComponent.FX_CONVERSION],
        financing=totals[CostComponent.FINANCING],
        borrow=totals[CostComponent.BORROW],
        custody=totals[CostComponent.CUSTODY],
        tax=totals[CostComponent.TAX],
        turnover=totals[CostComponent.TURNOVER],
        total_cost=total_cost,
    )


def _refusal(
    reason_code: str,
    reason: str,
    dataset_id: str,
    *,
    analysis_kind: str = "cost_xray",
    instrument_handles: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=analysis_kind,
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset_id,),
        instrument_handles=tuple(instrument_handles),
        source_scope=None,
    )


__all__ = (
    "CostBooking",
    "CostComponent",
    "CostComponents",
    "CostDataset",
    "CostReconciliation",
    "CostXrayResult",
    "NamedCostDifference",
    "PrivateCostXray",
    "SaxoCostIllustration",
    "analyze_cost_xray",
    "reconcile_cost_illustration",
)
