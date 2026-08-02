from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
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
_INCOME_SOURCE_CONTRACTS: Final = ("transactions_v1", "bookings_v1")
_CORPORATE_ACTION_SOURCE_CONTRACTS: Final = (
    "corporate_action_events_v2",
    "corporate_action_holdings_v2",
)
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type IncomeKind = Literal["dividend", "coupon", "interest"]
type CorporateActionEntitlement = Literal["available", "partial", "denied"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class IncomeEntry(_StrictModel):
    """One revisioned gross-income booking and its withholding amount."""

    account_alias: SafeAccountScope
    event_key_sha256: Sha256Fingerprint
    revision: int = Field(ge=1)
    occurred_at: UtcDateTime
    kind: IncomeKind
    gross_amount: Decimal = Field(ge=0, allow_inf_nan=False)
    withholding_tax: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: IsoCurrencyCode

    @model_validator(mode="after")
    def validate_net(self) -> Self:
        if self.withholding_tax > self.gross_amount:
            raise ValueError("withholding tax cannot exceed gross income")
        return self


class IncomeDataset(_StrictModel):
    """Bounded income history for exactly one safe account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    entries: tuple[IncomeEntry, ...]
    fx_quotes: tuple[FxQuote, ...]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if any(entry.account_alias != self.account_alias for entry in self.entries):
            raise ValueError("income entry account alias must match the dataset account alias")
        if any(entry.occurred_at > self.as_of for entry in self.entries):
            raise ValueError("income entries cannot follow the dataset cutoff")
        revisions = tuple((entry.event_key_sha256, entry.revision) for entry in self.entries)
        if len(revisions) != len(set(revisions)):
            raise ValueError("income event revisions must be unique")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial income quality must match explicit missing fields")
        return self


class PrivateIncomeValues(_StrictModel):
    """Owner-only gross, tax, and net income totals."""

    reporting_currency: IsoCurrencyCode
    dividends: Decimal = Field(ge=0, allow_inf_nan=False)
    coupons: Decimal = Field(ge=0, allow_inf_nan=False)
    interest: Decimal = Field(ge=0, allow_inf_nan=False)
    gross_income: Decimal = Field(ge=0, allow_inf_nan=False)
    withholding_tax: Decimal = Field(ge=0, allow_inf_nan=False)
    net_income: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.gross_income != self.dividends + self.coupons + self.interest:
            raise ValueError("income components must sum to gross income")
        if self.net_income != self.gross_income - self.withholding_tax:
            raise ValueError("gross income less withholding must equal net income")
        return self


class IncomeResult(_StrictModel):
    """Income analytics with private values omitted from public evidence."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["income_calendar"] = "income_calendar"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateIncomeValues | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("income values do not match their delivery visibility")
        return self


class CorporateActionClaimDataset(_StrictModel):
    """Value-free basis and entitlement inputs for authoritative event fields."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_bindings: tuple[SaxoSourceBinding, ...]
    entitlement_state: CorporateActionEntitlement
    required_basis_available: bool


class CorporateActionClaimResult(_StrictModel):
    """Bounded authority over only the exact entitled frozen source fields."""

    status: Literal[ResearchStatus.COMPLETE] = ResearchStatus.COMPLETE
    analysis_kind: Literal["corporate_action_center"] = "corporate_action_center"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    authoritative_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...] = ()


def analyze_income(
    dataset: IncomeDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> IncomeResult | ResearchRefusal:
    """Apply booking corrections once and reconcile gross less tax to net income."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_INCOME_SOURCE_CONTRACTS,
        analysis_kind="income_calendar",
        dataset_id=dataset.dataset_id,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            dataset,
            "income_data_unusable",
            "income history is missing, invalid, or stale",
        )
    try:
        values = _income_values(dataset)
    except FxNormalizationError:
        return _refusal(
            dataset,
            "income_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
        )
    warnings = set(dataset.warnings)
    warnings.update(source_assessment)
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("partial_history")
    return IncomeResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="income_calendar",
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=dataset,
        ),
    )


def authoritative_corporate_action_claim(
    dataset: CorporateActionClaimDataset,
) -> CorporateActionClaimResult | ResearchRefusal:
    """Refuse unless exact event, holding, basis, quality, and entitlement are bound."""
    if dataset.entitlement_state != "available":
        return ResearchRefusal(
            analysis_kind="corporate_action_center",
            reason_code="corporate_action_entitlement_insufficient",
            reason="authoritative corporate-action fields require complete entitlement",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(),
            source_scope=None,
        )
    if not dataset.required_basis_available:
        return ResearchRefusal(
            analysis_kind="corporate_action_center",
            reason_code="corporate_action_basis_unavailable",
            reason="the required event and holding basis is unavailable",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(),
            missing_fields=("event_or_holding_basis",),
            source_scope=None,
        )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_CORPORATE_ACTION_SOURCE_CONTRACTS,
        analysis_kind="corporate_action_center",
        dataset_id=dataset.dataset_id,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if source_assessment:
        return ResearchRefusal(
            analysis_kind="corporate_action_center",
            reason_code="corporate_action_basis_incomplete",
            reason="authoritative corporate-action fields require complete source quality",
            dataset_ids=(dataset.dataset_id,),
            instrument_handles=(),
            source_scope=None,
        )
    return CorporateActionClaimResult(
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        authoritative_fields=(
            "Amount",
            "EventId",
            "EventType",
            "ExDate",
            "PayDate",
            "Uic",
        ),
    )


def _income_values(dataset: IncomeDataset) -> PrivateIncomeValues:
    totals = {kind: Decimal(0) for kind in ("dividend", "coupon", "interest")}
    tax_total = Decimal(0)
    for entry in _latest_entries(dataset.entries):
        totals[entry.kind] += convert_amount(
            entry.gross_amount,
            entry.currency,
            dataset.reporting_currency,
            at=entry.occurred_at,
            quotes=dataset.fx_quotes,
        )
        tax_total += convert_amount(
            entry.withholding_tax,
            entry.currency,
            dataset.reporting_currency,
            at=entry.occurred_at,
            quotes=dataset.fx_quotes,
        )
    gross = sum(totals.values(), Decimal(0))
    return PrivateIncomeValues(
        reporting_currency=dataset.reporting_currency,
        dividends=totals["dividend"],
        coupons=totals["coupon"],
        interest=totals["interest"],
        gross_income=gross,
        withholding_tax=tax_total,
        net_income=gross - tax_total,
    )


def _latest_entries(entries: Sequence[IncomeEntry]) -> tuple[IncomeEntry, ...]:
    by_key: dict[str, IncomeEntry] = {}
    for entry in entries:
        current = by_key.get(entry.event_key_sha256)
        if current is None or entry.revision > current.revision:
            by_key[entry.event_key_sha256] = entry
    return tuple(by_key[key] for key in sorted(by_key))


def _refusal(dataset: IncomeDataset, reason_code: str, reason: str) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="income_calendar",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(),
        source_scope=None,
    )


__all__ = (
    "CorporateActionClaimDataset",
    "CorporateActionClaimResult",
    "IncomeDataset",
    "IncomeEntry",
    "IncomeResult",
    "PrivateIncomeValues",
    "analyze_income",
    "authoritative_corporate_action_claim",
)
