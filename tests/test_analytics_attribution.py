from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from saxo_bank_mcp.analytics_attribution import (
    AttributionDataset,
    AttributionPosition,
    NamedReturnDifference,
    analyze_portfolio_attribution,
)
from saxo_bank_mcp.analytics_fx import FxQuote
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import BenchmarkInput, SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000003"
_DATASET = "ds_00000000000040008000000000000033"
_HANDLE_A = "ih_00000000000040008000000000000033"
_HANDLE_B = "ih_00000000000040008000000000000034"
_AT = datetime(2026, 2, 1, tzinfo=UTC)


def _source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:r2",
        capture_fingerprint_sha256="b" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _dataset(
    *,
    saxo_return: str = "5",
    differences: tuple[NamedReturnDifference, ...] = (),
    benchmark: BenchmarkInput | None = None,
    quality_state: QualityState = QualityState.COMPLETE,
) -> AttributionDataset:
    return AttributionDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        as_of=_AT,
        reporting_currency="USD",
        opening_value=Decimal(1000),
        positions=(
            AttributionPosition(
                account_alias=_ALIAS,
                instrument_handle=_HANDLE_A,
                currency="USD",
                local_market_effect=Decimal(30),
                currency_effect=Decimal(0),
                income_effect=Decimal(10),
                trading_cost_effect=Decimal(-2),
                occurred_at=_AT,
            ),
            AttributionPosition(
                account_alias=_ALIAS,
                instrument_handle=_HANDLE_B,
                currency="EUR",
                local_market_effect=Decimal(10),
                currency_effect=Decimal(5),
                income_effect=Decimal(0),
                trading_cost_effect=Decimal(-5),
                occurred_at=_AT,
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
        declared_total_return_percentage=Decimal(5),
        saxo_total_return_percentage=Decimal(saxo_return),
        source_bindings=tuple(
            _source(contract_id)
            for contract_id in (
                "performance_summary_v4",
                "performance_timeseries_v4",
                "transactions_v1",
                "bookings_v1",
            )
        ),
        quality_state=quality_state,
        missing_fields=("history.before_start",) if quality_state is QualityState.PARTIAL else (),
        warnings=(),
        benchmark=benchmark,
        named_differences=differences,
    )


def test_attribution_contributions_currency_and_cost_components_reconcile() -> None:
    result = analyze_portfolio_attribution(
        _dataset(
            benchmark=BenchmarkInput(
                kind="saxo_reported",
                return_percentage=Decimal(4),
                currency="USD",
                instrument_handle=None,
                is_official=True,
                annual_fee_percentage=None,
                tracking_difference_disclosed=False,
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    assert sum(item.total_effect for item in values.positions) == Decimal("50.0")
    assert sum(item.contribution_percentage for item in values.positions) == Decimal("5.00")
    assert (
        values.local_market_effect
        + values.currency_effect
        + values.income_effect
        - values.trading_cost
        == values.total_effect
    )
    assert values.total_effect / values.opening_value * Decimal(100) == (
        values.total_return_percentage
    )
    assert values.trading_cost == Decimal("8.0")


def test_attribution_missing_benchmark_is_disclosed_without_inventing_one() -> None:
    result = analyze_portfolio_attribution(
        _dataset(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.benchmark is None
    assert "benchmark_unavailable" in result.warnings


def test_attribution_partial_history_is_reduced_with_its_named_gap() -> None:
    result = analyze_portfolio_attribution(
        _dataset(quality_state=QualityState.PARTIAL),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert "partial_history" in result.warnings


def test_attribution_unexplained_saxo_difference_refuses_but_named_difference_reduces() -> None:
    refused = analyze_portfolio_attribution(
        _dataset(saxo_return="5.1"),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    explained = analyze_portfolio_attribution(
        _dataset(
            saxo_return="5.1",
            differences=(
                NamedReturnDifference(
                    calculated_minus_saxo=Decimal("-0.1"),
                    reason_code="valuation_timing",
                ),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(refused, ResearchRefusal)
    assert refused.reason_code == "attribution_reconciliation_unexplained"
    assert not isinstance(explained, ResearchRefusal)
    assert explained.status is ResearchStatus.REDUCED
    assert explained.reconciliation.state == "named_difference"
