from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_fx import FxNormalizationError, FxQuote, convert_amount
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    QualityState,
    SafeAccountScope,
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    BenchmarkInput,
    PortfolioPublicEvidence,
    ReconciliationSummary,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)

_SOURCE_SCOPE: Final = "saxo_openapi"
_PERCENTAGE_TOLERANCE: Final = Decimal("0.000001")
_ATTRIBUTION_SOURCE_CONTRACTS: Final = (
    "performance_summary_v4",
    "performance_timeseries_v4",
    "transactions_v1",
    "bookings_v1",
)
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type AttributionDifferenceReason = Literal[
    "cutoff_alignment",
    "fx_translation",
    "rounding",
    "valuation_timing",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class AttributionPosition(_StrictModel):
    """Money effects for one position before reporting-currency normalization."""

    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    currency: IsoCurrencyCode
    local_market_effect: Decimal = Field(allow_inf_nan=False)
    currency_effect: Decimal = Field(allow_inf_nan=False)
    income_effect: Decimal = Field(allow_inf_nan=False)
    trading_cost_effect: Decimal = Field(le=0, allow_inf_nan=False)
    occurred_at: UtcDateTime


class NamedReturnDifference(_StrictModel):
    """Explicit percentage-point difference against Saxo performance."""

    calculated_minus_saxo: Decimal = Field(allow_inf_nan=False)
    reason_code: AttributionDifferenceReason


class AttributionDataset(_StrictModel):
    """Bounded component-attribution inputs for exactly one safe account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    opening_value: Decimal = Field(gt=0, allow_inf_nan=False)
    positions: tuple[AttributionPosition, ...] = Field(min_length=1)
    fx_quotes: tuple[FxQuote, ...]
    declared_total_return_percentage: Decimal = Field(allow_inf_nan=False)
    saxo_total_return_percentage: Decimal | None = Field(default=None, allow_inf_nan=False)
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]
    benchmark: BenchmarkInput | None
    named_differences: tuple[NamedReturnDifference, ...] = Field(max_length=1)

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if any(position.account_alias != self.account_alias for position in self.positions):
            raise ValueError(
                "attribution position account alias must match the dataset account alias",
            )
        handles = tuple(position.instrument_handle for position in self.positions)
        if len(handles) != len(set(handles)):
            raise ValueError("attribution positions must use unique instrument handles")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial attribution quality must match explicit missing fields")
        return self


class PrivatePositionAttribution(_StrictModel):
    """Owner-only reporting-currency effects for one safe instrument handle."""

    instrument_handle: InstrumentHandle
    local_market_effect: Decimal = Field(allow_inf_nan=False)
    currency_effect: Decimal = Field(allow_inf_nan=False)
    income_effect: Decimal = Field(allow_inf_nan=False)
    trading_cost_effect: Decimal = Field(le=0, allow_inf_nan=False)
    total_effect: Decimal = Field(allow_inf_nan=False)
    contribution_percentage: Decimal = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_components(self) -> Self:
        if self.total_effect != (
            self.local_market_effect
            + self.currency_effect
            + self.income_effect
            + self.trading_cost_effect
        ):
            raise ValueError("position attribution components must reconcile")
        return self


class PrivateAttributionValues(_StrictModel):
    """Owner-only portfolio attribution totals and identities."""

    reporting_currency: IsoCurrencyCode
    opening_value: Decimal = Field(gt=0, allow_inf_nan=False)
    positions: tuple[PrivatePositionAttribution, ...]
    local_market_effect: Decimal = Field(allow_inf_nan=False)
    currency_effect: Decimal = Field(allow_inf_nan=False)
    income_effect: Decimal = Field(allow_inf_nan=False)
    trading_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    total_effect: Decimal = Field(allow_inf_nan=False)
    total_return_percentage: Decimal = Field(allow_inf_nan=False)
    benchmark_return_percentage: Decimal | None = Field(default=None, allow_inf_nan=False)
    active_return_percentage: Decimal | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.total_effect != (
            self.local_market_effect + self.currency_effect + self.income_effect - self.trading_cost
        ):
            raise ValueError("portfolio attribution components must reconcile")
        if sum((item.total_effect for item in self.positions), Decimal(0)) != self.total_effect:
            raise ValueError("position effects must sum to the portfolio effect")
        if (
            sum(
                (item.contribution_percentage for item in self.positions),
                Decimal(0),
            )
            != self.total_return_percentage
        ):
            raise ValueError("position contributions must sum to total return")
        if (self.benchmark_return_percentage is None) != (self.active_return_percentage is None):
            raise ValueError("benchmark and active return must be available together")
        if (
            self.benchmark_return_percentage is not None
            and self.active_return_percentage
            != self.total_return_percentage - self.benchmark_return_percentage
        ):
            raise ValueError("active return must reconcile to the disclosed benchmark")
        return self


class AttributionResult(_StrictModel):
    """Component attribution with owner-bound values and value-free evidence."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["portfolio_attribution"] = "portfolio_attribution"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateAttributionValues | None
    benchmark: BenchmarkInput | None
    reconciliation: ReconciliationSummary
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("attribution values do not match their delivery visibility")
        if not private and self.benchmark is not None:
            raise ValueError("public attribution cannot contain benchmark values")
        return self


def analyze_portfolio_attribution(
    dataset: AttributionDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> AttributionResult | ResearchRefusal:
    """Calculate exact linked money components and reconcile their total to Saxo."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_ATTRIBUTION_SOURCE_CONTRACTS,
        analysis_kind="portfolio_attribution",
        dataset_id=dataset.dataset_id,
        instrument_handles=tuple(item.instrument_handle for item in dataset.positions),
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            dataset,
            "attribution_data_unusable",
            "attribution inputs are missing, invalid, or stale",
        )
    try:
        values = _attribution_values(dataset)
    except FxNormalizationError:
        return _refusal(
            dataset,
            "attribution_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
        )
    if (
        abs(values.total_return_percentage - dataset.declared_total_return_percentage)
        > _PERCENTAGE_TOLERANCE
    ):
        return _refusal(
            dataset,
            "attribution_identity_failed",
            "position contributions do not reconcile to the declared portfolio return",
        )
    reconciliation = _reconcile_saxo_return(dataset, values.total_return_percentage)
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
        warnings.add("attribution_reconciled_with_named_difference")
    return AttributionResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        benchmark=dataset.benchmark if private_delivery else None,
        reconciliation=reconciliation,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="portfolio_attribution",
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=dataset,
        ),
    )


def _attribution_values(dataset: AttributionDataset) -> PrivateAttributionValues:
    positions: list[PrivatePositionAttribution] = []
    for item in dataset.positions:
        local = _convert(item.local_market_effect, item, dataset)
        currency = _convert(item.currency_effect, item, dataset)
        income = _convert(item.income_effect, item, dataset)
        cost = _convert(item.trading_cost_effect, item, dataset)
        total = local + currency + income + cost
        positions.append(
            PrivatePositionAttribution(
                instrument_handle=item.instrument_handle,
                local_market_effect=local,
                currency_effect=currency,
                income_effect=income,
                trading_cost_effect=cost,
                total_effect=total,
                contribution_percentage=total / dataset.opening_value * Decimal(100),
            ),
        )
    local_total = sum((item.local_market_effect for item in positions), Decimal(0))
    currency_total = sum((item.currency_effect for item in positions), Decimal(0))
    income_total = sum((item.income_effect for item in positions), Decimal(0))
    cost_total = -sum((item.trading_cost_effect for item in positions), Decimal(0))
    effect_total = local_total + currency_total + income_total - cost_total
    return_percentage = effect_total / dataset.opening_value * Decimal(100)
    benchmark_return = (
        dataset.benchmark.return_percentage if dataset.benchmark is not None else None
    )
    return PrivateAttributionValues(
        reporting_currency=dataset.reporting_currency,
        opening_value=dataset.opening_value,
        positions=tuple(positions),
        local_market_effect=local_total,
        currency_effect=currency_total,
        income_effect=income_total,
        trading_cost=cost_total,
        total_effect=effect_total,
        total_return_percentage=return_percentage,
        benchmark_return_percentage=benchmark_return,
        active_return_percentage=(
            return_percentage - benchmark_return if benchmark_return is not None else None
        ),
    )


def _convert(
    value: Decimal,
    item: AttributionPosition,
    dataset: AttributionDataset,
) -> Decimal:
    return convert_amount(
        value,
        item.currency,
        dataset.reporting_currency,
        at=item.occurred_at,
        quotes=dataset.fx_quotes,
    )


def _reconcile_saxo_return(
    dataset: AttributionDataset,
    calculated: Decimal,
) -> ReconciliationSummary | ResearchRefusal:
    if dataset.saxo_total_return_percentage is None:
        return _refusal(
            dataset,
            "saxo_attribution_total_missing",
            "a compatible Saxo performance return is required for attribution reconciliation",
        )
    difference = calculated - dataset.saxo_total_return_percentage
    mismatch = abs(difference) > _PERCENTAGE_TOLERANCE
    if not mismatch and not dataset.named_differences:
        return ReconciliationSummary(state="exact", reason_codes=())
    if (
        not mismatch
        or len(dataset.named_differences) != 1
        or abs(dataset.named_differences[0].calculated_minus_saxo - difference)
        > _PERCENTAGE_TOLERANCE
    ):
        return _refusal(
            dataset,
            "attribution_reconciliation_unexplained",
            "a compatible Saxo attribution difference is missing an exact named reason",
        )
    return ReconciliationSummary(
        state="named_difference",
        reason_codes=(dataset.named_differences[0].reason_code,),
    )


def _refusal(
    dataset: AttributionDataset,
    reason_code: str,
    reason: str,
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="portfolio_attribution",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=tuple(item.instrument_handle for item in dataset.positions),
        source_scope=None,
    )


__all__ = (
    "AttributionDataset",
    "AttributionPosition",
    "AttributionResult",
    "NamedReturnDifference",
    "PrivateAttributionValues",
    "analyze_portfolio_attribution",
)
