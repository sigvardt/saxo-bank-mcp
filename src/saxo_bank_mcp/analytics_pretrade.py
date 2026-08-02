from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_costs import (
    CostComponents,
    CostReconciliation,
    NamedCostDifference,
    SaxoCostIllustration,
    reconcile_cost_illustration,
)
from saxo_bank_mcp.analytics_fx import FxNormalizationError, FxQuote, convert_amount
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
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
    PortfolioPublicEvidence,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)
from saxo_bank_mcp.analytics_trade_review import (
    DecisionBarReference,
    DecisionPointQuote,
    ExecutionQualityEvidence,
    classify_decision_reference,
)
from saxo_bank_mcp.trade_preview import AnalyticsTradePreviewInput

_SOURCE_SCOPE: Final = "saxo_openapi"
_PRETRADE_SOURCE_CONTRACTS: Final = (
    "positions_v1",
    "balances_v1",
    "costs_v1",
)
_MONEY_TOLERANCE: Final = Decimal("0.01")
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type ProposalSide = Literal["buy", "sell"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class TradeProposal(_StrictModel):
    """Bounded analytical proposal data with no order or approval material."""

    origin_analysis_id: AnalysisId
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    decision_at: UtcDateTime
    side: ProposalSide
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    proposal_price: Decimal = Field(gt=0, allow_inf_nan=False)
    contract_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)
    instrument_currency: IsoCurrencyCode
    reporting_currency: IsoCurrencyCode
    current_position_quantity: Decimal = Field(allow_inf_nan=False)
    current_position_exposure: Decimal = Field(allow_inf_nan=False)
    portfolio_value: Decimal = Field(gt=0, allow_inf_nan=False)
    current_currency_exposure: Decimal = Field(allow_inf_nan=False)
    buying_power_available: Decimal = Field(ge=0, allow_inf_nan=False)
    estimated_cash_required: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_available: Decimal = Field(ge=0, allow_inf_nan=False)
    estimated_margin_required: Decimal = Field(ge=0, allow_inf_nan=False)
    holding_period_days: int = Field(ge=0)
    cost_estimate: CostComponents
    saxo_illustration: SaxoCostIllustration | None
    named_cost_difference: NamedCostDifference | None
    decision_quote: DecisionPointQuote | None
    decision_bar: DecisionBarReference | None
    fx_quotes: tuple[FxQuote, ...] = ()
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial pretrade quality must match explicit missing fields")
        if self.decision_quote is not None and (
            self.decision_quote.instrument_handle != self.instrument_handle
        ):
            raise ValueError("decision quote must match the proposal instrument handle")
        if self.decision_bar is not None and (
            self.decision_bar.instrument_handle != self.instrument_handle
            or self.decision_bar.decision_at != self.decision_at
        ):
            raise ValueError("decision bar must match the proposal and decision timestamp")
        return self


class PrivatePreTradeValues(_StrictModel):
    """Owner-only impact card values, independent of order preview authority."""

    reporting_currency: IsoCurrencyCode
    instrument_currency: IsoCurrencyCode
    holding_period_days: int = Field(ge=0)
    signed_order_notional: Decimal = Field(allow_inf_nan=False)
    projected_position_quantity: Decimal = Field(allow_inf_nan=False)
    projected_instrument_exposure: Decimal = Field(allow_inf_nan=False)
    projected_concentration_percentage: Decimal = Field(ge=0, allow_inf_nan=False)
    projected_currency_exposure: Decimal = Field(allow_inf_nan=False)
    buying_power_after: Decimal = Field(allow_inf_nan=False)
    margin_after: Decimal = Field(allow_inf_nan=False)
    costs: CostComponents
    cost_reconciliation: CostReconciliation
    quote_evidence: ExecutionQualityEvidence


class PreTradeImpact(_StrictModel):
    """Private impact card plus a value-free, non-authorizing preview pointer."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["pretrade_impact"] = "pretrade_impact"
    analysis_id: AnalysisId
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivatePreTradeValues | None
    warnings: tuple[ContractName, ...]
    preview_input: AnalyticsTradePreviewInput
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery_and_pointer(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("pretrade values do not match their delivery visibility")
        if (
            self.preview_input.analysis_id != self.analysis_id
            or self.preview_input.account_alias != self.account_alias
            or self.preview_input.instrument_handle != self.instrument_handle
        ):
            raise ValueError("preview input must point to this exact impact card")
        return self


def build_pretrade_impact(  # noqa: C901, PLR0911, PLR0912, PLR0915
    analysis_id: AnalysisId,
    proposal: TradeProposal,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> PreTradeImpact | ResearchRefusal:
    """Build an impact card and a non-authorizing preview pointer from Saxo data."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    if analysis_id != proposal.origin_analysis_id:
        return _refusal(
            proposal,
            "analysis_id_mismatch",
            "the proposal must remain bound to its originating analysis",
        )
    source_assessment = assess_source_bindings(
        proposal.source_bindings,
        required_contract_ids=_PRETRADE_SOURCE_CONTRACTS,
        analysis_kind="pretrade_impact",
        dataset_id=proposal.dataset_id,
        instrument_handles=(proposal.instrument_handle,),
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if proposal.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            proposal,
            "pretrade_data_unusable",
            "pretrade inputs are missing, invalid, or stale",
        )
    reference_bindings: list[SaxoSourceBinding] = []
    reference_warnings: set[str] = set()
    if proposal.decision_quote is not None:
        quote_bindings = (proposal.decision_quote.source_binding,)
    else:
        quote_bindings = tuple(
            binding
            for binding in proposal.source_bindings
            if binding.contract_id == "info_price_v1"
        )
    quote_assessment = assess_source_bindings(
        quote_bindings,
        required_contract_ids=("info_price_v1",),
        analysis_kind="pretrade_impact",
        dataset_id=proposal.dataset_id,
        instrument_handles=(proposal.instrument_handle,),
    )
    if isinstance(quote_assessment, ResearchRefusal):
        return quote_assessment
    reference_warnings.update(quote_assessment)
    if proposal.decision_quote is not None:
        reference_bindings.append(proposal.decision_quote.source_binding)
    if proposal.decision_bar is not None:
        bar_assessment = assess_source_bindings(
            (proposal.decision_bar.source_binding,),
            required_contract_ids=("chart_v3",),
            analysis_kind="pretrade_impact",
            dataset_id=proposal.dataset_id,
            instrument_handles=(proposal.instrument_handle,),
        )
        if isinstance(bar_assessment, ResearchRefusal):
            return bar_assessment
        reference_warnings.update(bar_assessment)
        reference_bindings.append(proposal.decision_bar.source_binding)
    evidence_result = classify_decision_reference(
        decision_at=proposal.decision_at,
        instrument_handle=proposal.instrument_handle,
        observed_price=proposal.proposal_price,
        side=proposal.side,
        decision_quote=proposal.decision_quote,
        decision_bar=proposal.decision_bar,
        refusal_dataset_id=proposal.dataset_id,
        allow_unavailable=True,
    )
    if isinstance(evidence_result, ResearchRefusal):
        return evidence_result
    quote_evidence, quote_warnings = evidence_result
    try:
        signed_quantity = proposal.quantity if proposal.side == "buy" else -proposal.quantity
        native_notional = (
            signed_quantity * proposal.proposal_price * proposal.contract_multiplier
        )
        signed_notional = convert_amount(
            native_notional,
            proposal.instrument_currency,
            proposal.reporting_currency,
            at=proposal.decision_at,
            quotes=proposal.fx_quotes,
        )
        if abs(abs(signed_notional) - proposal.cost_estimate.turnover) > _MONEY_TOLERANCE:
            return _refusal(
                proposal,
                "pretrade_turnover_mismatch",
                "the cost turnover must equal the proposed reporting-currency notional",
            )
        cost_reconciliation = reconcile_cost_illustration(
            local_total=proposal.cost_estimate.total_cost,
            reporting_currency=proposal.reporting_currency,
            illustration=proposal.saxo_illustration,
            named_difference=proposal.named_cost_difference,
            at=proposal.decision_at,
            fx_quotes=proposal.fx_quotes,
            analysis_kind="pretrade_impact",
            dataset_id=proposal.dataset_id,
            instrument_handles=(proposal.instrument_handle,),
        )
    except FxNormalizationError:
        return _refusal(
            proposal,
            "pretrade_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
        )
    if isinstance(cost_reconciliation, ResearchRefusal):
        return cost_reconciliation
    values = PrivatePreTradeValues(
        reporting_currency=proposal.reporting_currency,
        instrument_currency=proposal.instrument_currency,
        holding_period_days=proposal.holding_period_days,
        signed_order_notional=signed_notional,
        projected_position_quantity=proposal.current_position_quantity + signed_quantity,
        projected_instrument_exposure=(
            proposal.current_position_exposure + signed_notional
        ),
        projected_concentration_percentage=(
            abs(proposal.current_position_exposure + signed_notional)
            / proposal.portfolio_value
            * Decimal(100)
        ),
        projected_currency_exposure=(
            proposal.current_currency_exposure + signed_notional
        ),
        buying_power_after=(
            proposal.buying_power_available
            - proposal.estimated_cash_required
            - proposal.cost_estimate.total_cost
        ),
        margin_after=proposal.margin_available - proposal.estimated_margin_required,
        costs=proposal.cost_estimate,
        cost_reconciliation=cost_reconciliation,
        quote_evidence=quote_evidence,
    )
    warnings = set(proposal.warnings)
    warnings.update(source_assessment)
    warnings.update(reference_warnings)
    warnings.update(quote_warnings)
    if proposal.quality_state is QualityState.PARTIAL:
        warnings.add("partial_pretrade_basis")
    if values.buying_power_after < 0:
        warnings.add("estimated_buying_power_shortfall")
    if values.margin_after < 0:
        warnings.add("estimated_margin_shortfall")
    if cost_reconciliation.state == "unavailable":
        warnings.add("saxo_cost_illustration_unavailable")
    elif cost_reconciliation.state == "named_difference":
        warnings.add("cost_illustration_named_difference")
    proposal_fingerprint = _fingerprint(proposal)
    impact_fingerprint = _fingerprint(values)
    preview_input = AnalyticsTradePreviewInput(
        analysis_id=analysis_id,
        proposal_fingerprint_sha256=proposal_fingerprint,
        impact_card_fingerprint_sha256=impact_fingerprint,
        account_alias=proposal.account_alias,
        instrument_handle=proposal.instrument_handle,
    )
    dataset_ids = {proposal.dataset_id}
    if proposal.decision_quote is not None:
        dataset_ids.add(proposal.decision_quote.dataset_id)
    if proposal.decision_bar is not None:
        dataset_ids.add(proposal.decision_bar.dataset_id)
    return PreTradeImpact(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        analysis_id=analysis_id,
        dataset_id=proposal.dataset_id,
        account_alias=proposal.account_alias,
        instrument_handle=proposal.instrument_handle,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        preview_input=preview_input,
        evidence=build_public_evidence(
            analysis_kind="pretrade_impact",
            dataset_ids=tuple(sorted(dataset_ids)),
            account_aliases=(proposal.account_alias,),
            source_bindings=(*proposal.source_bindings, *reference_bindings),
            material=proposal,
        ),
    )


def _fingerprint(model: BaseModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _refusal(
    proposal: TradeProposal,
    reason_code: str,
    reason: str,
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="pretrade_impact",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(proposal.dataset_id,),
        instrument_handles=(proposal.instrument_handle,),
        source_scope=None,
    )


__all__ = (
    "PreTradeImpact",
    "PrivatePreTradeValues",
    "TradeProposal",
    "build_pretrade_impact",
)
