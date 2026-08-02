# ruff: noqa: FURB157, PLR0913, PLR2004

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from saxo_bank_mcp.analytics_costs import (
    CostBooking,
    CostComponent,
    CostDataset,
    NamedCostDifference,
    SaxoCostIllustration,
    analyze_cost_xray,
)
from saxo_bank_mcp.analytics_fx import FxQuote
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000011"
_DATASET = "ds_00000000000040008000000000000041"
_AT = datetime(2026, 5, 1, tzinfo=UTC)


def _source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t14",
        capture_fingerprint_sha256="a" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _booking(
    key: str,
    component: CostComponent,
    amount: str,
    *,
    revision: int = 1,
    currency: str = "USD",
    fill_key: str | None = None,
) -> CostBooking:
    return CostBooking(
        account_alias=_ALIAS,
        event_key_sha256=key * 64,
        revision=revision,
        occurred_at=_AT,
        component=component,
        source_amount=Decimal(amount),
        currency=currency,
        fill_key_sha256=None if fill_key is None else fill_key * 64,
    )


def _dataset(
    *,
    bookings: tuple[CostBooking, ...] | None = None,
    illustration: SaxoCostIllustration | None = None,
    named_difference: NamedCostDifference | None = None,
    fx_quotes: tuple[FxQuote, ...] | None = None,
) -> CostDataset:
    rows = bookings or (
        _booking("1", CostComponent.COMMISSION, "-1"),
        _booking("1", CostComponent.COMMISSION, "-1.5", revision=2),
        _booking("2", CostComponent.SPREAD, "-2"),
        _booking("3", CostComponent.FX_CONVERSION, "-1", currency="EUR"),
        _booking("4", CostComponent.FINANCING, "-0.5"),
        _booking("5", CostComponent.BORROW, "-0.25"),
        _booking("6", CostComponent.CUSTODY, "-0.25"),
        _booking("7", CostComponent.TAX, "-1"),
        _booking("8", CostComponent.TURNOVER, "100", fill_key="a"),
        _booking("9", CostComponent.TURNOVER, "150", fill_key="b"),
    )
    return CostDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        as_of=_AT,
        reporting_currency="USD",
        bookings=rows,
        fx_quotes=(
            FxQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.2"),
                observed_at=_AT,
            ),
        )
        if fx_quotes is None
        else fx_quotes,
        source_bindings=tuple(
            _source(contract_id)
            for contract_id in (
                "transactions_v1",
                "bookings_v1",
                "closed_positions_history_v1",
                "costs_v1",
            )
        ),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
        saxo_illustration=illustration
        or SaxoCostIllustration(
            commission=Decimal("1.5"),
            stamp_duty=Decimal("1"),
            total_cost=Decimal("6.70"),
            currency="USD",
        ),
        named_difference=named_difference,
    )


def test_cost_components_sum_with_positive_fee_signs_partial_fills_and_correction() -> None:
    result = analyze_cost_xray(
        _dataset(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    assert values.components.commission == Decimal("1.5")
    assert values.components.spread == Decimal(2)
    assert values.components.fx_conversion == Decimal("1.2")
    assert values.components.financing == Decimal("0.5")
    assert values.components.borrow == Decimal("0.25")
    assert values.components.custody == Decimal("0.25")
    assert values.components.tax == Decimal(1)
    assert values.components.total_cost == Decimal("6.70")
    assert values.components.total_cost == sum(
        (
            values.components.commission,
            values.components.spread,
            values.components.fx_conversion,
            values.components.financing,
            values.components.borrow,
            values.components.custody,
            values.components.tax,
        ),
        Decimal(0),
    )
    assert values.components.turnover == Decimal(250)
    assert values.partial_fill_count == 2
    assert values.reconciliation.state == "exact"


def test_charge_classification_normalizes_positive_or_negative_source_fee_signs() -> None:
    result = analyze_cost_xray(
        _dataset(
            bookings=(
                _booking("1", CostComponent.COMMISSION, "1"),
                _booking("8", CostComponent.TURNOVER, "100", fill_key="a"),
            ),
            illustration=SaxoCostIllustration(
                commission=Decimal(1),
                stamp_duty=Decimal(0),
                total_cost=Decimal(1),
                currency="USD",
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.private_values is not None
    assert result.private_values.components.commission == Decimal(1)
    assert result.private_values.components.total_cost == Decimal(1)


def test_missing_exact_fx_conversion_refuses_multi_currency_costs() -> None:
    result = analyze_cost_xray(
        _dataset(fx_quotes=()),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "cost_fx_basis_missing"


def test_saxo_cost_illustration_requires_exact_or_named_reconciliation() -> None:
    illustration = SaxoCostIllustration(
        commission=Decimal("1.5"),
        stamp_duty=Decimal(1),
        total_cost=Decimal("6.50"),
        currency="USD",
    )
    unexplained = analyze_cost_xray(
        _dataset(illustration=illustration),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    named = analyze_cost_xray(
        _dataset(
            illustration=illustration,
            named_difference=NamedCostDifference(
                calculated_minus_saxo=Decimal("0.20"),
                reason_code="partial_fill_rounding",
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(unexplained, ResearchRefusal)
    assert unexplained.reason_code == "cost_illustration_reconciliation_unexplained"
    assert not isinstance(named, ResearchRefusal)
    assert named.status is ResearchStatus.REDUCED
    assert named.private_values is not None
    assert named.private_values.reconciliation.state == "named_difference"
    assert named.private_values.reconciliation.reason_codes == (
        "partial_fill_rounding",
    )


def test_cost_money_is_owner_only_and_public_evidence_is_value_free() -> None:
    public = analyze_cost_xray(
        _dataset(),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    for private_field in (
        "commission",
        "spread",
        "fx_conversion",
        "financing",
        "borrow",
        "custody",
        "tax",
        "turnover",
        "total_cost",
        "currency",
    ):
        assert private_field not in serialized
    assert public.evidence.private_values_redacted is True
