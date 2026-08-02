from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from saxo_bank_mcp.analytics_fx import FxQuote
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_liquidity import (
    CashBalance,
    LiquidityDataset,
    analyze_cash_and_settlement,
)
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000007"
_DATASET = "ds_00000000000040008000000000000037"
_AT = datetime(2026, 5, 1, tzinfo=UTC)


def _source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:r5",
        capture_fingerprint_sha256="e" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _dataset(*, partial: bool = False) -> LiquidityDataset:
    return LiquidityDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        as_of=_AT,
        reporting_currency="USD",
        balances=(
            CashBalance(
                account_alias=_ALIAS,
                currency="USD",
                cash_balance=Decimal(100),
                transactions_not_booked=Decimal(20),
                funds_reserved_for_settlement=Decimal(10),
                funds_available_for_settlement=Decimal(110),
                spending_power=Decimal(200),
                margin_available_for_trading=Decimal(50),
            ),
            CashBalance(
                account_alias=_ALIAS,
                currency="EUR",
                cash_balance=Decimal(50),
                transactions_not_booked=Decimal(-5),
                funds_reserved_for_settlement=Decimal(5),
                funds_available_for_settlement=None if partial else Decimal(40),
                spending_power=Decimal(80),
                margin_available_for_trading=Decimal(20),
            ),
        ),
        fx_quotes=(
            FxQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.2"),
                observed_at=_AT,
            ),
        ),
        source_bindings=(
            _source("balances_v1"),
            _source("bookings_v1"),
        ),
        quality_state=QualityState.PARTIAL if partial else QualityState.COMPLETE,
        missing_fields=("FundsAvailableForSettlement",) if partial else (),
        warnings=(),
    )


def test_cash_settlement_and_multi_currency_identities_reconcile() -> None:
    result = analyze_cash_and_settlement(
        _dataset(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    values = result.private_values
    assert values is not None
    assert values.cash_balance == Decimal("160.0")
    assert values.transactions_not_booked == Decimal("14.0")
    assert values.funds_reserved_for_settlement == Decimal("16.0")
    assert values.projected_settled_cash == Decimal("158.0")
    assert values.funds_available_for_settlement == Decimal("158.0")
    assert values.settlement_difference == Decimal("0.0")
    assert values.spending_power == Decimal("296.0")
    assert values.margin_available_for_trading == Decimal("74.0")
    assert result.liquidity_score is None
    assert "liquidity_score_unavailable_no_bound_depth" in result.warnings


def test_partial_cash_history_reduces_without_fabricating_available_funds() -> None:
    result = analyze_cash_and_settlement(
        _dataset(partial=True),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.private_values is not None
    assert result.private_values.funds_available_for_settlement is None
    assert "partial_history" in result.warnings


def test_liquidity_private_boundary_and_public_evidence_redaction() -> None:
    public = analyze_cash_and_settlement(
        _dataset(),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    assert "cash_balance" not in serialized
    assert "spending_power" not in serialized
    assert "margin_available_for_trading" not in serialized
