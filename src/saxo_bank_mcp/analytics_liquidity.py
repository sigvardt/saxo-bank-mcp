from __future__ import annotations

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
_SETTLEMENT_TOLERANCE: Final = Decimal("0.01")
_LIQUIDITY_SOURCE_CONTRACTS: Final = ("balances_v1", "bookings_v1")
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class CashBalance(_StrictModel):
    """One currency balance with exact settlement and margin fields."""

    account_alias: SafeAccountScope
    currency: IsoCurrencyCode
    cash_balance: Decimal = Field(allow_inf_nan=False)
    transactions_not_booked: Decimal = Field(allow_inf_nan=False)
    funds_reserved_for_settlement: Decimal = Field(ge=0, allow_inf_nan=False)
    funds_available_for_settlement: Decimal | None = Field(default=None, allow_inf_nan=False)
    spending_power: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    margin_available_for_trading: Decimal | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )


class LiquidityDataset(_StrictModel):
    """Bounded multi-currency cash state for one safe account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    balances: tuple[CashBalance, ...] = Field(min_length=1)
    fx_quotes: tuple[FxQuote, ...]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if any(balance.account_alias != self.account_alias for balance in self.balances):
            raise ValueError("cash balance account alias must match the dataset account alias")
        currencies = tuple(balance.currency for balance in self.balances)
        if len(currencies) != len(set(currencies)):
            raise ValueError("cash balance currencies must be unique")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial liquidity quality must match explicit missing fields")
        return self


class PrivateLiquidityValues(_StrictModel):
    """Owner-only cash, settlement, buying-power, and margin totals."""

    reporting_currency: IsoCurrencyCode
    cash_balance: Decimal = Field(allow_inf_nan=False)
    transactions_not_booked: Decimal = Field(allow_inf_nan=False)
    funds_reserved_for_settlement: Decimal = Field(ge=0, allow_inf_nan=False)
    projected_settled_cash: Decimal = Field(allow_inf_nan=False)
    funds_available_for_settlement: Decimal | None = Field(default=None, allow_inf_nan=False)
    settlement_difference: Decimal | None = Field(default=None, allow_inf_nan=False)
    spending_power: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    margin_available_for_trading: Decimal | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.projected_settled_cash != (
            self.cash_balance + self.transactions_not_booked - self.funds_reserved_for_settlement
        ):
            raise ValueError("cash settlement components must reconcile")
        if (self.funds_available_for_settlement is None) != (self.settlement_difference is None):
            raise ValueError("settlement comparison fields must be available together")
        if (
            self.funds_available_for_settlement is not None
            and self.settlement_difference
            != self.projected_settled_cash - self.funds_available_for_settlement
        ):
            raise ValueError("settlement difference must reconcile to available funds")
        return self


class LiquidityResult(_StrictModel):
    """Cash-and-settlement result with no invented market-depth liquidity score."""

    status: Literal[ResearchStatus.REDUCED] = ResearchStatus.REDUCED
    analysis_kind: Literal["cash_and_settlement"] = "cash_and_settlement"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateLiquidityValues | None
    liquidity_score: None = None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("liquidity values do not match their delivery visibility")
        if "liquidity_score_unavailable_no_bound_depth" not in self.warnings:
            raise ValueError("unavailable liquidity score requires its bounded reason")
        return self


def analyze_cash_and_settlement(
    dataset: LiquidityDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> LiquidityResult | ResearchRefusal:
    """Normalize cash currencies and prove the exact settlement identity."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_LIQUIDITY_SOURCE_CONTRACTS,
        analysis_kind="cash_and_settlement",
        dataset_id=dataset.dataset_id,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            dataset,
            "cash_settlement_data_unusable",
            "cash and settlement inputs are missing, invalid, or stale",
        )
    try:
        values = _liquidity_values(dataset)
    except FxNormalizationError:
        return _refusal(
            dataset,
            "cash_settlement_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
        )
    if (
        values.settlement_difference is not None
        and abs(values.settlement_difference) > _SETTLEMENT_TOLERANCE
    ):
        return _refusal(
            dataset,
            "cash_settlement_reconciliation_failed",
            "calculated and broker-reported settlement totals do not reconcile",
        )
    warnings = set(dataset.warnings)
    warnings.update(source_assessment)
    warnings.add("liquidity_score_unavailable_no_bound_depth")
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("partial_history")
    return LiquidityResult(
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="cash_and_settlement",
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=dataset,
        ),
    )


def _liquidity_values(dataset: LiquidityDataset) -> PrivateLiquidityValues:
    cash = _sum_required(dataset, "cash_balance")
    unbooked = _sum_required(dataset, "transactions_not_booked")
    reserved = _sum_required(dataset, "funds_reserved_for_settlement")
    available = _sum_optional(dataset, "funds_available_for_settlement")
    spending = _sum_optional(dataset, "spending_power")
    margin = _sum_optional(dataset, "margin_available_for_trading")
    projected = cash + unbooked - reserved
    return PrivateLiquidityValues(
        reporting_currency=dataset.reporting_currency,
        cash_balance=cash,
        transactions_not_booked=unbooked,
        funds_reserved_for_settlement=reserved,
        projected_settled_cash=projected,
        funds_available_for_settlement=available,
        settlement_difference=projected - available if available is not None else None,
        spending_power=spending,
        margin_available_for_trading=margin,
    )


def _sum_required(
    dataset: LiquidityDataset,
    field: Literal[
        "cash_balance",
        "transactions_not_booked",
        "funds_reserved_for_settlement",
    ],
) -> Decimal:
    total = Decimal(0)
    for balance in dataset.balances:
        total += convert_amount(
            getattr(balance, field),
            balance.currency,
            dataset.reporting_currency,
            at=dataset.as_of,
            quotes=dataset.fx_quotes,
        )
    return total


def _sum_optional(
    dataset: LiquidityDataset,
    field: Literal[
        "funds_available_for_settlement",
        "spending_power",
        "margin_available_for_trading",
    ],
) -> Decimal | None:
    values = tuple(getattr(balance, field) for balance in dataset.balances)
    if any(value is None for value in values):
        return None
    return sum(
        (
            convert_amount(
                value,
                balance.currency,
                dataset.reporting_currency,
                at=dataset.as_of,
                quotes=dataset.fx_quotes,
            )
            for balance, value in zip(dataset.balances, values, strict=True)
            if value is not None
        ),
        Decimal(0),
    )


def _refusal(dataset: LiquidityDataset, reason_code: str, reason: str) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="cash_and_settlement",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(),
        source_scope=None,
    )


__all__ = (
    "CashBalance",
    "LiquidityDataset",
    "LiquidityResult",
    "PrivateLiquidityValues",
    "analyze_cash_and_settlement",
)
