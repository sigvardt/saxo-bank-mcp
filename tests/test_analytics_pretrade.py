# ruff: noqa: PLR2004

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_costs import (
    CostComponents,
    NamedCostDifference,
    SaxoCostIllustration,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_pretrade import TradeProposal, build_pretrade_impact
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_trade_review import (
    DecisionBarReference,
    DecisionPointQuote,
)
from saxo_bank_mcp.trade_preview import AnalyticsTradePreviewInput

_ANALYSIS = "an_00000000000040008000000000000041"
_OTHER_ANALYSIS = "an_00000000000040008000000000000042"
_ALIAS = "aa_00000000000040008000000000000013"
_DATASET = "ds_00000000000040008000000000000045"
_QUOTE_DATASET = "ds_00000000000040008000000000000046"
_BAR_DATASET = "ds_00000000000040008000000000000047"
_HANDLE = "ih_00000000000040008000000000000043"
_AT = datetime(2026, 5, 8, 12, tzinfo=UTC)
_QUOTE_CAPTURE = "e" * 64
_BAR_CAPTURE = "f" * 64


def _source(
    contract_id: str,
    *,
    capture: str = "a" * 64,
    quality: QualityState = QualityState.COMPLETE,
    entitlement: Literal["available", "partial", "denied"] = "available",
) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t14",
        capture_fingerprint_sha256=capture,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _quote(*, quality: QualityState = QualityState.COMPLETE) -> DecisionPointQuote:
    return DecisionPointQuote(
        dataset_id=_QUOTE_DATASET,
        instrument_handle=_HANDLE,
        captured_at=_AT,
        bid=Decimal(99),
        ask=Decimal(101),
        price_type="Tradable",
        delayed_by_minutes=0,
        quality_state=quality,
        entitlement_state="available",
        source_binding=_source("info_price_v1", capture=_QUOTE_CAPTURE),
        captured_by_mcp=True,
        warnings=(),
    )


def _bar() -> DecisionBarReference:
    return DecisionBarReference(
        dataset_id=_BAR_DATASET,
        instrument_handle=_HANDLE,
        decision_at=_AT,
        bar_start_at=_AT - timedelta(minutes=5),
        bar_end_at=_AT + timedelta(minutes=5),
        close_price=Decimal(100),
        source_binding=_source("chart_v3", capture=_BAR_CAPTURE),
    )


def _costs() -> CostComponents:
    return CostComponents(
        commission=Decimal(2),
        spread=Decimal(1),
        fx_conversion=Decimal(0),
        financing=Decimal("0.5"),
        borrow=Decimal(0),
        custody=Decimal(0),
        tax=Decimal("0.5"),
        turnover=Decimal(1000),
        total_cost=Decimal(4),
    )


def _proposal(
    *,
    quote: DecisionPointQuote | None = None,
    bar: DecisionBarReference | None = None,
    illustration: SaxoCostIllustration | None = None,
    named_difference: NamedCostDifference | None = None,
    side: Literal["buy", "sell"] = "buy",
) -> TradeProposal:
    bindings = [
        _source("positions_v1"),
        _source("balances_v1"),
        _source("costs_v1"),
    ]
    if quote is None:
        bindings.append(
            _source(
                "info_price_v1",
                capture=_QUOTE_CAPTURE,
                quality=QualityState.PARTIAL,
                entitlement="partial",
            ),
        )
    return TradeProposal(
        origin_analysis_id=_ANALYSIS,
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE,
        decision_at=_AT,
        side=side,
        quantity=Decimal(10),
        proposal_price=Decimal(100),
        contract_multiplier=Decimal(1),
        instrument_currency="USD",
        reporting_currency="USD",
        current_position_quantity=Decimal(5),
        current_position_exposure=Decimal(500),
        portfolio_value=Decimal(2000),
        current_currency_exposure=Decimal(700),
        buying_power_available=Decimal(2000),
        estimated_cash_required=Decimal(1000),
        margin_available=Decimal(500),
        estimated_margin_required=Decimal(100),
        holding_period_days=10,
        cost_estimate=_costs(),
        saxo_illustration=illustration
        or SaxoCostIllustration(
            commission=Decimal(2),
            stamp_duty=Decimal("0.5"),
            total_cost=Decimal(4),
            currency="USD",
        ),
        named_cost_difference=named_difference,
        decision_quote=quote,
        decision_bar=bar,
        source_bindings=tuple(bindings),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )


def test_pretrade_impact_card_reconciles_components_and_builds_preview_input_only() -> None:
    result = build_pretrade_impact(
        _ANALYSIS,
        _proposal(quote=_quote()),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    assert values.instrument_currency == "USD"
    assert values.holding_period_days == 10
    assert values.signed_order_notional == Decimal(1000)
    assert values.projected_position_quantity == Decimal(15)
    assert values.projected_instrument_exposure == Decimal(1500)
    assert values.projected_concentration_percentage == Decimal(75)
    assert values.projected_currency_exposure == Decimal(1700)
    assert values.buying_power_after == Decimal(996)
    assert values.margin_after == Decimal(400)
    assert values.costs.total_cost == Decimal(4)
    assert values.quote_evidence.evidence_class == "exact_mcp_decision_quote"
    assert values.quote_evidence.midpoint == Decimal(100)
    assert values.quote_evidence.spread == Decimal(2)
    assert result.preview_input.analysis_id == _ANALYSIS
    assert result.preview_input.account_alias == _ALIAS
    assert result.preview_input.instrument_handle == _HANDLE
    assert result.preview_input.requires_fresh_saxo_precheck is True
    assert result.preview_input.carries_approval is False
    assert result.preview_input.approval_authority is False
    assert result.preview_input.execution_authority is False
    assert not hasattr(result.preview_input, "approve")
    assert not hasattr(result.preview_input, "execute")
    assert set(result.preview_input.model_dump()) == {
        "analysis_id",
        "proposal_fingerprint_sha256",
        "impact_card_fingerprint_sha256",
        "account_alias",
        "instrument_handle",
        "requires_fresh_saxo_precheck",
        "carries_approval",
        "approval_authority",
        "execution_authority",
    }


def test_preview_input_rejects_authority_tokens_and_order_material() -> None:
    base = {
        "analysis_id": _ANALYSIS,
        "proposal_fingerprint_sha256": "a" * 64,
        "impact_card_fingerprint_sha256": "b" * 64,
        "account_alias": _ALIAS,
        "instrument_handle": _HANDLE,
    }
    with pytest.raises(ValidationError):
        AnalyticsTradePreviewInput.model_validate(
            {**base, "approval_authority": True},
        )
    with pytest.raises(ValidationError):
        AnalyticsTradePreviewInput.model_validate(
            {**base, "execution_authority": True},
        )
    with pytest.raises(ValidationError):
        AnalyticsTradePreviewInput.model_validate(
            {**base, "preview_token": "not-allowed"},
        )
    with pytest.raises(ValidationError):
        AnalyticsTradePreviewInput.model_validate(
            {**base, "order_body": {"Amount": 10}},
        )


def test_missing_exact_quote_uses_labeled_bar_or_marks_quote_metric_unavailable() -> None:
    approximated = build_pretrade_impact(
        _ANALYSIS,
        _proposal(bar=_bar()),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    unavailable = build_pretrade_impact(
        _ANALYSIS,
        _proposal(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(approximated, ResearchRefusal)
    assert approximated.status is ResearchStatus.REDUCED
    assert approximated.private_values is not None
    bar_evidence = approximated.private_values.quote_evidence
    assert bar_evidence.evidence_class == "bar_approximation"
    assert bar_evidence.arrival_price is None
    assert bar_evidence.midpoint is None
    assert bar_evidence.spread is None
    assert bar_evidence.reference_price == Decimal(100)
    assert bar_evidence.approximation_rule == "nearest_saxo_chart_bar_close"

    assert not isinstance(unavailable, ResearchRefusal)
    assert unavailable.status is ResearchStatus.REDUCED
    assert unavailable.private_values is not None
    no_evidence = unavailable.private_values.quote_evidence
    assert no_evidence.evidence_class == "unavailable"
    assert no_evidence.arrival_price is None
    assert no_evidence.midpoint is None
    assert no_evidence.spread is None
    assert no_evidence.reference_price is None
    assert "decision_quote_metric_unavailable" in unavailable.warnings


def test_stale_quote_never_supports_exact_pretrade_spread_claim() -> None:
    result = build_pretrade_impact(
        _ANALYSIS,
        _proposal(quote=_quote(quality=QualityState.STALE)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.private_values is not None
    assert result.private_values.quote_evidence.evidence_class == "unavailable"
    assert result.private_values.quote_evidence.midpoint is None
    assert result.private_values.quote_evidence.spread is None


def test_partial_bound_quote_capture_cannot_support_exact_quote_claims() -> None:
    quote = _quote()
    partial_quote = quote.model_copy(
        update={
            "source_binding": quote.source_binding.model_copy(
                update={
                    "quality_state": QualityState.PARTIAL,
                    "entitlement_state": "partial",
                },
            ),
        },
    )
    proposal = _proposal(quote=partial_quote)

    result = build_pretrade_impact(
        _ANALYSIS,
        proposal,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.private_values is not None
    assert result.private_values.quote_evidence.evidence_class == "unavailable"
    assert result.private_values.quote_evidence.midpoint is None
    assert result.private_values.quote_evidence.spread is None


def test_pretrade_cost_illustration_mismatch_requires_an_exact_named_difference() -> None:
    illustration = SaxoCostIllustration(
        commission=Decimal(2),
        stamp_duty=Decimal("0.5"),
        total_cost=Decimal("3.75"),
        currency="USD",
    )
    unexplained = build_pretrade_impact(
        _ANALYSIS,
        _proposal(quote=_quote(), illustration=illustration),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    named = build_pretrade_impact(
        _ANALYSIS,
        _proposal(
            quote=_quote(),
            illustration=illustration,
            named_difference=NamedCostDifference(
                calculated_minus_saxo=Decimal("0.25"),
                reason_code="quote_timing",
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(unexplained, ResearchRefusal)
    assert unexplained.reason_code == "cost_illustration_reconciliation_unexplained"
    assert not isinstance(named, ResearchRefusal)
    assert named.status is ResearchStatus.REDUCED
    assert "cost_illustration_named_difference" in named.warnings


def test_sell_proposal_preserves_signed_exposure_and_currency_effects() -> None:
    result = build_pretrade_impact(
        _ANALYSIS,
        _proposal(quote=_quote(), side="sell"),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.private_values is not None
    assert result.private_values.signed_order_notional == Decimal(-1000)
    assert result.private_values.projected_position_quantity == Decimal(-5)
    assert result.private_values.projected_instrument_exposure == Decimal(-500)
    assert result.private_values.projected_currency_exposure == Decimal(-300)


def test_pretrade_analysis_id_must_match_and_private_values_stay_owner_only() -> None:
    mismatch = build_pretrade_impact(
        _OTHER_ANALYSIS,
        _proposal(quote=_quote()),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    public = build_pretrade_impact(
        _ANALYSIS,
        _proposal(quote=_quote()),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert isinstance(mismatch, ResearchRefusal)
    assert mismatch.reason_code == "analysis_id_mismatch"
    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    for private_field in (
        "notional",
        "position_quantity",
        "exposure",
        "concentration",
        "buying_power",
        "margin",
        "cost",
        "currency",
        "price",
    ):
        assert private_field not in serialized
