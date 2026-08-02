from __future__ import annotations

import hashlib
import json
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
    InstrumentHandle,
    IsoCurrencyCode,
    PortfolioSnapshotId,
    QualityState,
    SafeAccountScope,
    Sha256Fingerprint,
    SourceRevision,
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_SOURCE_SCOPE: Final = "saxo_openapi"
_MONEY_TOLERANCE: Final = Decimal("0.01")
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)
_PORTFOLIO_SOURCE_CONTRACTS: Final = (
    "performance_summary_v4",
    "performance_timeseries_v4",
    "transactions_v1",
    "bookings_v1",
)

type SourceEntitlement = Literal["available", "partial", "denied"]
type BenchmarkKind = Literal["saxo_reported", "proxy"]
type MoneyDifferenceMetric = Literal["closing_value", "total_profit_loss"]
type MoneyDifferenceReason = Literal[
    "booking_cutoff",
    "fx_translation",
    "rounding",
    "scope_alignment",
    "valuation_timing",
]


class PortfolioAnalyticsError(ValueError):
    """Raised when a requested delivery boundary is unsafe."""


class LedgerKind(StrEnum):
    """Frozen calculation roles for normalized portfolio bookings."""

    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    DIVIDEND = "dividend"
    COMMISSION = "commission"
    FEE = "fee"
    FINANCING = "financing"
    TAX = "tax"
    TRADING_PNL = "trading_pnl"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class SaxoSourceBinding(_StrictModel):
    """Value-free binding to one exact frozen Saxo source contract and capture."""

    contract_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    contract_sha256: Sha256Fingerprint
    source_revision: SourceRevision
    capture_fingerprint_sha256: Sha256Fingerprint
    quality_state: QualityState
    entitlement_state: SourceEntitlement


class LedgerEntry(_StrictModel):
    """One normalized, revisioned portfolio booking with a hashed source identity."""

    account_alias: SafeAccountScope
    event_key_sha256: Sha256Fingerprint
    revision: int = Field(ge=1)
    occurred_at: UtcDateTime
    kind: LedgerKind
    amount: Decimal = Field(allow_inf_nan=False)
    currency: IsoCurrencyCode

    @model_validator(mode="after")
    def validate_sign(self) -> Self:
        if self.kind is not LedgerKind.TRADING_PNL and self.amount < 0:
            raise ValueError("non-P&L ledger amounts must be non-negative magnitudes")
        return self


class BenchmarkInput(_StrictModel):
    """Broker benchmark or explicitly disclosed tradable proxy."""

    kind: BenchmarkKind
    return_percentage: Decimal = Field(allow_inf_nan=False)
    currency: IsoCurrencyCode
    instrument_handle: InstrumentHandle | None
    is_official: bool
    annual_fee_percentage: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    tracking_difference_disclosed: bool

    @model_validator(mode="after")
    def validate_disclosure(self) -> Self:
        if self.kind == "saxo_reported":
            if not self.is_official or self.instrument_handle is not None:
                raise ValueError("Saxo-reported benchmarks must be official and not proxy-bound")
            if self.annual_fee_percentage is not None or self.tracking_difference_disclosed:
                raise ValueError("official benchmark inputs cannot carry proxy disclosures")
            return self
        if (
            self.is_official
            or self.instrument_handle is None
            or self.annual_fee_percentage is None
            or not self.tracking_difference_disclosed
        ):
            raise ValueError("proxy benchmarks require handle, fee, and tracking disclosures")
        return self


class SaxoPerformanceTotals(_StrictModel):
    """Compatible broker totals used only for exact reconciliation."""

    closing_value: Decimal = Field(allow_inf_nan=False)
    total_profit_loss: Decimal = Field(allow_inf_nan=False)
    currency: IsoCurrencyCode


class NamedMoneyDifference(_StrictModel):
    """One explicitly classified difference against a compatible Saxo total."""

    metric: MoneyDifferenceMetric
    calculated_minus_saxo: Decimal = Field(allow_inf_nan=False)
    reason_code: MoneyDifferenceReason


class PortfolioPeriodDataset(_StrictModel):
    """Bounded inputs for one account and one portfolio accounting period."""

    dataset_id: DatasetId
    snapshot_id: PortfolioSnapshotId
    account_alias: SafeAccountScope
    start_at: UtcDateTime
    end_at: UtcDateTime
    reporting_currency: IsoCurrencyCode
    opening_value: Decimal = Field(gt=0, allow_inf_nan=False)
    closing_value: Decimal = Field(allow_inf_nan=False)
    ledger_entries: tuple[LedgerEntry, ...]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]
    benchmark: BenchmarkInput | None
    saxo_totals: SaxoPerformanceTotals | None
    named_differences: tuple[NamedMoneyDifference, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if self.end_at < self.start_at:
            raise ValueError("portfolio period end must not precede its start")
        if any(entry.account_alias != self.account_alias for entry in self.ledger_entries):
            raise ValueError("ledger entry account alias must match the dataset account alias")
        if any(
            entry.occurred_at < self.start_at or entry.occurred_at > self.end_at
            for entry in self.ledger_entries
        ):
            raise ValueError("ledger entries must fall within the portfolio period")
        revisions = tuple((entry.event_key_sha256, entry.revision) for entry in self.ledger_entries)
        if len(revisions) != len(set(revisions)):
            raise ValueError("ledger event revisions must be unique")
        difference_metrics = tuple(item.metric for item in self.named_differences)
        if len(difference_metrics) != len(set(difference_metrics)):
            raise ValueError("named performance differences must be unique by metric")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial portfolio quality must match explicit missing fields")
        return self


class PortfolioCostComponents(_StrictModel):
    """Positive cost magnitudes counted exactly once."""

    commission: Decimal = Field(ge=0, allow_inf_nan=False)
    fees: Decimal = Field(ge=0, allow_inf_nan=False)
    financing: Decimal = Field(ge=0, allow_inf_nan=False)
    taxes: Decimal = Field(ge=0, allow_inf_nan=False)
    total: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total != self.commission + self.fees + self.financing + self.taxes:
            raise ValueError("portfolio cost components must sum to total cost")
        return self


class PrivatePortfolioTruth(_StrictModel):
    """Owner-only money values for one reconciled accounting period."""

    reporting_currency: IsoCurrencyCode
    opening_value: Decimal = Field(allow_inf_nan=False)
    closing_value: Decimal = Field(allow_inf_nan=False)
    deposits: Decimal = Field(ge=0, allow_inf_nan=False)
    withdrawals: Decimal = Field(ge=0, allow_inf_nan=False)
    dividends: Decimal = Field(ge=0, allow_inf_nan=False)
    trading_profit_loss: Decimal = Field(allow_inf_nan=False)
    costs: PortfolioCostComponents
    external_cash_flow: Decimal = Field(allow_inf_nan=False)
    total_profit_loss: Decimal = Field(allow_inf_nan=False)
    accounting_difference: Decimal = Field(allow_inf_nan=False)
    period_return_percentage: Decimal = Field(allow_inf_nan=False)


class ReconciliationSummary(_StrictModel):
    """Value-free reconciliation state suitable for public evidence."""

    state: Literal["exact", "named_difference"]
    reason_codes: tuple[ContractName, ...]


class PortfolioPublicEvidence(_StrictModel):
    """Value-free public proof pointer for owner-only portfolio analytics."""

    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    dataset_ids: tuple[DatasetId, ...]
    account_aliases: tuple[SafeAccountScope, ...]
    material_fingerprint_sha256: Sha256Fingerprint
    source_contract_sha256s: tuple[Sha256Fingerprint, ...]
    private_values_redacted: Literal[True] = True


class PortfolioTruthResult(_StrictModel):
    """Portfolio accounting result with delivery-bound private values."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["portfolio_performance"] = "portfolio_performance"
    dataset_id: DatasetId
    snapshot_id: PortfolioSnapshotId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivatePortfolioTruth | None
    benchmark: BenchmarkInput | None
    reconciliation: ReconciliationSummary
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("portfolio values do not match their delivery visibility")
        if not private and self.benchmark is not None:
            raise ValueError("public portfolio results cannot contain benchmark values")
        return self


class MultiAccountPortfolioResult(_StrictModel):
    """Alias-isolated portfolio results without cross-account source identities."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["multi_account"] = "multi_account"
    account_aliases: tuple[SafeAccountScope, ...]
    accounts: tuple[PortfolioTruthResult, ...]
    visibility: VisibilityMode
    evidence: PortfolioPublicEvidence
    warnings: tuple[ContractName, ...]


def assess_source_bindings(
    bindings: Sequence[SaxoSourceBinding],
    *,
    required_contract_ids: Sequence[str],
    analysis_kind: str,
    dataset_id: str,
    instrument_handles: Sequence[str] = (),
) -> tuple[str, ...] | ResearchRefusal:
    """Validate exact frozen contract hashes and source quality without trusting labels."""
    by_id = {binding.contract_id: binding for binding in bindings}
    if len(by_id) != len(bindings):
        return _refusal(
            analysis_kind,
            "source_binding_invalid",
            "source contract bindings must be unique",
            dataset_id,
            instrument_handles,
        )
    contracts = source_contracts_by_id()
    if any(
        binding.contract_id not in contracts
        or binding.contract_sha256 != source_contract_fingerprint(contracts[binding.contract_id])
        for binding in bindings
    ):
        return _refusal(
            analysis_kind,
            "source_binding_invalid",
            "a source binding does not match the frozen source catalog",
            dataset_id,
            instrument_handles,
        )
    missing = tuple(sorted(set(required_contract_ids) - set(by_id)))
    if missing:
        return _refusal(
            analysis_kind,
            "source_contract_missing",
            "a required frozen source contract is not bound",
            dataset_id,
            instrument_handles,
            missing_fields=missing,
        )
    warnings: set[str] = set()
    for contract_id in required_contract_ids:
        binding = by_id[contract_id]
        if binding.entitlement_state == "denied":
            return _refusal(
                analysis_kind,
                "source_entitlement_insufficient",
                "a required source is not entitled",
                dataset_id,
                instrument_handles,
            )
        if binding.quality_state in _UNUSABLE_QUALITY:
            return _refusal(
                analysis_kind,
                "source_data_unusable",
                "a required source is missing, invalid, or stale",
                dataset_id,
                instrument_handles,
            )
        if binding.entitlement_state == "partial":
            warnings.add(f"source_entitlement_partial_{contract_id}")
        if binding.quality_state is QualityState.PARTIAL:
            warnings.add(f"source_quality_partial_{contract_id}")
    return tuple(sorted(warnings))


def build_public_evidence(
    *,
    analysis_kind: str,
    dataset_ids: Sequence[str],
    account_aliases: Sequence[str],
    source_bindings: Sequence[SaxoSourceBinding],
    material: BaseModel | Sequence[BaseModel],
) -> PortfolioPublicEvidence:
    """Hash private calculation inputs while publishing no material values."""
    models = (material,) if isinstance(material, BaseModel) else tuple(material)
    payload = [model.model_dump(mode="json") for model in models]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return PortfolioPublicEvidence(
        analysis_kind=analysis_kind,
        dataset_ids=tuple(dataset_ids),
        account_aliases=tuple(account_aliases),
        material_fingerprint_sha256=fingerprint,
        source_contract_sha256s=tuple(
            sorted({binding.contract_sha256 for binding in source_bindings}),
        ),
    )


def require_delivery_boundary(
    visibility: VisibilityMode,
    *,
    trusted_local_host: bool,
) -> bool:
    """Return whether private values may be emitted for the requested visibility."""
    if visibility is VisibilityMode.PRIVATE_USER_RESULT:
        if not trusted_local_host:
            raise PortfolioAnalyticsError(
                "private portfolio values require the trusted local host",
            )
        return True
    if visibility not in {
        VisibilityMode.PUBLIC_EVIDENCE,
        VisibilityMode.FINGERPRINT_ONLY,
        VisibilityMode.REDACTED_PREVIEW,
    }:
        raise PortfolioAnalyticsError("unsupported portfolio delivery visibility")
    return False


def analyze_portfolio_truth(
    dataset: PortfolioPeriodDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    fx_quotes: Sequence[FxQuote] = (),
) -> PortfolioTruthResult | ResearchRefusal:
    """Reconcile one portfolio period without counting corrected bookings twice."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_PORTFOLIO_SOURCE_CONTRACTS,
        analysis_kind="portfolio_performance",
        dataset_id=dataset.dataset_id,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            "portfolio_performance",
            "portfolio_data_unusable",
            "portfolio history is missing, invalid, or stale",
            dataset.dataset_id,
        )
    try:
        values = _portfolio_values(dataset, fx_quotes)
    except FxNormalizationError:
        return _refusal(
            "portfolio_performance",
            "portfolio_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
            dataset.dataset_id,
        )
    if abs(values.accounting_difference) > _MONEY_TOLERANCE:
        return _refusal(
            "portfolio_performance",
            "accounting_identity_failed",
            "opening value, external flows, profit and loss, and closing value do not reconcile",
            dataset.dataset_id,
        )
    reconciliation = _performance_reconciliation(dataset, values)
    if isinstance(reconciliation, ResearchRefusal):
        return reconciliation
    warnings = set(dataset.warnings)
    warnings.update(source_assessment)
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("partial_history")
    if dataset.benchmark is None:
        warnings.add("benchmark_unavailable")
    elif dataset.benchmark.kind == "proxy":
        warnings.add("benchmark_proxy_not_official")
    if reconciliation.state == "named_difference":
        warnings.add("performance_reconciled_with_named_difference")
    evidence = build_public_evidence(
        analysis_kind="portfolio_performance",
        dataset_ids=(dataset.dataset_id,),
        account_aliases=(dataset.account_alias,),
        source_bindings=dataset.source_bindings,
        material=dataset,
    )
    return PortfolioTruthResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        snapshot_id=dataset.snapshot_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        benchmark=dataset.benchmark if private_delivery else None,
        reconciliation=reconciliation,
        warnings=tuple(sorted(warnings)),
        evidence=evidence,
    )


def analyze_multi_account_portfolios(
    datasets: Sequence[PortfolioPeriodDataset],
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    fx_quotes: Sequence[FxQuote] = (),
) -> MultiAccountPortfolioResult | ResearchRefusal:
    """Analyze distinct safe aliases independently and preserve their input order."""
    values = tuple(datasets)
    if not values:
        return ResearchRefusal(
            analysis_kind="multi_account",
            reason_code="portfolio_accounts_missing",
            reason="at least one bounded account dataset is required",
            dataset_ids=(),
            instrument_handles=(),
            source_scope=None,
        )
    aliases = tuple(dataset.account_alias for dataset in values)
    if len(aliases) != len(set(aliases)):
        return ResearchRefusal(
            analysis_kind="multi_account",
            reason_code="account_alias_not_isolated",
            reason="multi-account inputs must use distinct safe account aliases",
            dataset_ids=tuple(dataset.dataset_id for dataset in values),
            instrument_handles=(),
            source_scope=None,
        )
    accounts: list[PortfolioTruthResult] = []
    for dataset in values:
        result = analyze_portfolio_truth(
            dataset,
            visibility=visibility,
            trusted_local_host=trusted_local_host,
            fx_quotes=fx_quotes,
        )
        if isinstance(result, ResearchRefusal):
            return ResearchRefusal(
                analysis_kind="multi_account",
                reason_code=result.reason_code,
                reason=result.reason,
                dataset_ids=result.dataset_ids,
                instrument_handles=result.instrument_handles,
                missing_fields=result.missing_fields,
                warnings=result.warnings,
                source_scope=result.source_scope,
            )
        accounts.append(result)
    warnings = tuple(sorted({warning for item in accounts for warning in item.warnings}))
    bindings = tuple(binding for dataset in values for binding in dataset.source_bindings)
    return MultiAccountPortfolioResult(
        status=(
            ResearchStatus.REDUCED
            if any(item.status is ResearchStatus.REDUCED for item in accounts)
            else ResearchStatus.COMPLETE
        ),
        account_aliases=aliases,
        accounts=tuple(accounts),
        visibility=visibility,
        evidence=build_public_evidence(
            analysis_kind="multi_account",
            dataset_ids=tuple(dataset.dataset_id for dataset in values),
            account_aliases=aliases,
            source_bindings=bindings,
            material=values,
        ),
        warnings=warnings,
    )


def authoritative_tax_lot_export(dataset: PortfolioPeriodDataset) -> ResearchRefusal:
    """Refuse tax-lot authority because no frozen Task 13 source supplies its basis."""
    return ResearchRefusal(
        analysis_kind="tax_lot_export",
        reason_code="authoritative_tax_lot_basis_unavailable",
        reason="the frozen source contracts do not supply authoritative tax-lot basis",
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(),
        missing_fields=("tax_lot_basis",),
        source_scope=None,
    )


def _latest_entries(entries: Sequence[LedgerEntry]) -> tuple[LedgerEntry, ...]:
    by_key: dict[str, LedgerEntry] = {}
    for entry in entries:
        current = by_key.get(entry.event_key_sha256)
        if current is None or entry.revision > current.revision:
            by_key[entry.event_key_sha256] = entry
    return tuple(by_key[key] for key in sorted(by_key))


def _portfolio_values(
    dataset: PortfolioPeriodDataset,
    fx_quotes: Sequence[FxQuote],
) -> PrivatePortfolioTruth:
    totals = {kind: Decimal(0) for kind in LedgerKind}
    for entry in _latest_entries(dataset.ledger_entries):
        totals[entry.kind] += convert_amount(
            entry.amount,
            entry.currency,
            dataset.reporting_currency,
            at=entry.occurred_at,
            quotes=fx_quotes,
        )
    costs = PortfolioCostComponents(
        commission=totals[LedgerKind.COMMISSION],
        fees=totals[LedgerKind.FEE],
        financing=totals[LedgerKind.FINANCING],
        taxes=totals[LedgerKind.TAX],
        total=(
            totals[LedgerKind.COMMISSION]
            + totals[LedgerKind.FEE]
            + totals[LedgerKind.FINANCING]
            + totals[LedgerKind.TAX]
        ),
    )
    external_flow = totals[LedgerKind.DEPOSIT] - totals[LedgerKind.WITHDRAWAL]
    profit_loss = totals[LedgerKind.TRADING_PNL] + totals[LedgerKind.DIVIDEND] - costs.total
    accounting_difference = (
        dataset.closing_value - dataset.opening_value - external_flow - profit_loss
    )
    return PrivatePortfolioTruth(
        reporting_currency=dataset.reporting_currency,
        opening_value=dataset.opening_value,
        closing_value=dataset.closing_value,
        deposits=totals[LedgerKind.DEPOSIT],
        withdrawals=totals[LedgerKind.WITHDRAWAL],
        dividends=totals[LedgerKind.DIVIDEND],
        trading_profit_loss=totals[LedgerKind.TRADING_PNL],
        costs=costs,
        external_cash_flow=external_flow,
        total_profit_loss=profit_loss,
        accounting_difference=accounting_difference,
        period_return_percentage=profit_loss / dataset.opening_value * Decimal(100),
    )


def _performance_reconciliation(
    dataset: PortfolioPeriodDataset,
    values: PrivatePortfolioTruth,
) -> ReconciliationSummary | ResearchRefusal:
    if dataset.saxo_totals is None:
        return _refusal(
            "portfolio_performance",
            "saxo_performance_total_missing",
            "a compatible Saxo performance total is required for reconciliation",
            dataset.dataset_id,
        )
    if dataset.saxo_totals.currency != dataset.reporting_currency:
        return _refusal(
            "portfolio_performance",
            "performance_reconciliation_not_comparable",
            "Saxo and calculated performance totals use different currency bases",
            dataset.dataset_id,
        )
    observed = {
        "closing_value": values.closing_value - dataset.saxo_totals.closing_value,
        "total_profit_loss": (values.total_profit_loss - dataset.saxo_totals.total_profit_loss),
    }
    mismatches = {
        metric: difference
        for metric, difference in observed.items()
        if abs(difference) > _MONEY_TOLERANCE
    }
    named = {difference.metric: difference for difference in dataset.named_differences}
    if set(named) != set(mismatches) or any(
        abs(named[metric].calculated_minus_saxo - difference) > _MONEY_TOLERANCE
        for metric, difference in mismatches.items()
    ):
        return _refusal(
            "portfolio_performance",
            "performance_reconciliation_unexplained",
            "a compatible Saxo performance difference is missing an exact named reason",
            dataset.dataset_id,
        )
    if not mismatches:
        return ReconciliationSummary(state="exact", reason_codes=())
    return ReconciliationSummary(
        state="named_difference",
        reason_codes=tuple(sorted(item.reason_code for item in named.values())),
    )


def _refusal(  # noqa: PLR0913
    analysis_kind: str,
    reason_code: str,
    reason: str,
    dataset_id: str,
    instrument_handles: Sequence[str] = (),
    *,
    missing_fields: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=analysis_kind,
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset_id,),
        instrument_handles=tuple(instrument_handles),
        missing_fields=tuple(sorted(set(missing_fields))),
        source_scope=None,
    )


__all__ = (
    "BenchmarkInput",
    "LedgerEntry",
    "LedgerKind",
    "MultiAccountPortfolioResult",
    "NamedMoneyDifference",
    "PortfolioAnalyticsError",
    "PortfolioPeriodDataset",
    "PortfolioPublicEvidence",
    "PortfolioTruthResult",
    "PrivatePortfolioTruth",
    "ReconciliationSummary",
    "SaxoPerformanceTotals",
    "SaxoSourceBinding",
    "analyze_multi_account_portfolios",
    "analyze_portfolio_truth",
    "assess_source_bindings",
    "authoritative_tax_lot_export",
    "build_public_evidence",
    "require_delivery_boundary",
)
