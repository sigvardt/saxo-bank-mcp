# ruff: noqa: PLR0913

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import (
    BenchmarkInput,
    LedgerEntry,
    LedgerKind,
    NamedMoneyDifference,
    PortfolioPeriodDataset,
    SaxoPerformanceTotals,
    SaxoSourceBinding,
    analyze_multi_account_portfolios,
    analyze_portfolio_truth,
    authoritative_tax_lot_export,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS_A = "aa_00000000000040008000000000000001"
_ALIAS_B = "aa_00000000000040008000000000000002"
_DATASET_A = "ds_00000000000040008000000000000031"
_DATASET_B = "ds_00000000000040008000000000000032"
_SNAPSHOT_A = "ps_00000000000040008000000000000031"
_SNAPSHOT_B = "ps_00000000000040008000000000000032"
_BENCHMARK_HANDLE = "ih_00000000000040008000000000000031"
_START = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 1, 31, tzinfo=UTC)


def _source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:r1",
        capture_fingerprint_sha256="a" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _entry(
    key: str,
    kind: LedgerKind,
    amount: str,
    *,
    revision: int = 1,
    account_alias: str = _ALIAS_A,
    currency: str = "USD",
) -> LedgerEntry:
    return LedgerEntry(
        account_alias=account_alias,
        event_key_sha256=key * 64,
        revision=revision,
        occurred_at=_END,
        kind=kind,
        amount=Decimal(amount),
        currency=currency,
    )


def _benchmark() -> BenchmarkInput:
    return BenchmarkInput(
        kind="saxo_reported",
        return_percentage=Decimal("4.5"),
        currency="USD",
        instrument_handle=None,
        is_official=True,
        annual_fee_percentage=None,
        tracking_difference_disclosed=False,
    )


def _dataset(
    *,
    account_alias: str = _ALIAS_A,
    dataset_id: str = _DATASET_A,
    snapshot_id: str = _SNAPSHOT_A,
    quality_state: QualityState = QualityState.COMPLETE,
    benchmark: BenchmarkInput | None = None,
    entries: tuple[LedgerEntry, ...] | None = None,
    saxo_totals: SaxoPerformanceTotals | None = None,
    differences: tuple[NamedMoneyDifference, ...] = (),
    warnings: tuple[str, ...] = (),
) -> PortfolioPeriodDataset:
    ledger = entries or (
        _entry("1", LedgerKind.DEPOSIT, "200", account_alias=account_alias),
        _entry("2", LedgerKind.WITHDRAWAL, "50", account_alias=account_alias),
        _entry("3", LedgerKind.DIVIDEND, "8", account_alias=account_alias),
        _entry("3", LedgerKind.DIVIDEND, "10", revision=2, account_alias=account_alias),
        _entry("4", LedgerKind.COMMISSION, "1", account_alias=account_alias),
        _entry("5", LedgerKind.FEE, "1", account_alias=account_alias),
        _entry("6", LedgerKind.TRADING_PNL, "42", account_alias=account_alias),
    )
    return PortfolioPeriodDataset(
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        account_alias=account_alias,
        start_at=_START,
        end_at=_END,
        reporting_currency="USD",
        opening_value=Decimal(1000),
        closing_value=Decimal(1200),
        ledger_entries=ledger,
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
        warnings=warnings,
        benchmark=benchmark,
        saxo_totals=saxo_totals
        or SaxoPerformanceTotals(
            closing_value=Decimal(1200),
            total_profit_loss=Decimal(50),
            currency="USD",
        ),
        named_differences=differences,
    )


def test_portfolio_accounting_identity_flows_corrections_and_costs() -> None:
    result = analyze_portfolio_truth(
        _dataset(benchmark=_benchmark()),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    assert values.deposits == Decimal(200)
    assert values.withdrawals == Decimal(50)
    assert values.dividends == Decimal(10)
    assert values.costs.commission == Decimal(1)
    assert values.costs.fees == Decimal(1)
    assert values.costs.total == Decimal(2)
    assert values.total_profit_loss == Decimal(50)
    assert values.external_cash_flow == Decimal(150)
    assert values.opening_value + values.external_cash_flow + values.total_profit_loss == (
        values.closing_value
    )
    assert values.accounting_difference == Decimal(0)
    assert values.period_return_percentage == Decimal("5.00")


def test_missing_and_proxy_benchmarks_are_explicitly_reduced() -> None:
    missing = analyze_portfolio_truth(
        _dataset(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    proxy = analyze_portfolio_truth(
        _dataset(
            benchmark=BenchmarkInput(
                kind="proxy",
                return_percentage=Decimal("4.25"),
                currency="EUR",
                instrument_handle=_BENCHMARK_HANDLE,
                is_official=False,
                annual_fee_percentage=Decimal("0.20"),
                tracking_difference_disclosed=True,
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(missing, ResearchRefusal)
    assert missing.status is ResearchStatus.REDUCED
    assert missing.benchmark is None
    assert "benchmark_unavailable" in missing.warnings
    assert not isinstance(proxy, ResearchRefusal)
    assert proxy.status is ResearchStatus.REDUCED
    assert proxy.benchmark is not None
    assert proxy.benchmark.kind == "proxy"
    assert proxy.benchmark.is_official is False
    assert proxy.benchmark.tracking_difference_disclosed is True
    assert proxy.benchmark.annual_fee_percentage == Decimal("0.20")
    assert "benchmark_proxy_not_official" in proxy.warnings


def test_private_money_requires_owner_boundary_and_public_evidence_is_value_free() -> None:
    with pytest.raises(ValueError, match="trusted local host"):
        analyze_portfolio_truth(
            _dataset(benchmark=_benchmark()),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=False,
        )

    public = analyze_portfolio_truth(
        _dataset(benchmark=_benchmark()),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    for private_field in (
        "opening_value",
        "closing_value",
        "deposits",
        "withdrawals",
        "dividends",
        "total_profit_loss",
        "return_percentage",
    ):
        assert private_field not in serialized
    assert public.evidence.private_values_redacted is True


def test_public_warning_codes_cannot_carry_caller_financial_values() -> None:
    with pytest.raises(ValidationError, match="warnings"):
        _dataset(
            benchmark=_benchmark(),
            warnings=("cash balance is USD 100",),
        )


def test_every_supplied_source_binding_must_match_the_frozen_catalog() -> None:
    dataset = _dataset(benchmark=_benchmark())
    forged_extra = SaxoSourceBinding(
        contract_id="balances_v1",
        contract_sha256="f" * 64,
        source_revision="fixture:r1",
        capture_fingerprint_sha256="f" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )
    dataset = dataset.model_copy(
        update={"source_bindings": (*dataset.source_bindings, forged_extra)},
    )

    result = analyze_portfolio_truth(
        dataset,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "source_binding_invalid"


def test_unexplained_saxo_performance_difference_refuses_and_named_difference_reduces() -> None:
    mismatched = SaxoPerformanceTotals(
        closing_value=Decimal(1201),
        total_profit_loss=Decimal(50),
        currency="USD",
    )
    refused = analyze_portfolio_truth(
        _dataset(benchmark=_benchmark(), saxo_totals=mismatched),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    explained = analyze_portfolio_truth(
        _dataset(
            benchmark=_benchmark(),
            saxo_totals=mismatched,
            differences=(
                NamedMoneyDifference(
                    metric="closing_value",
                    calculated_minus_saxo=Decimal(-1),
                    reason_code="booking_cutoff",
                ),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(refused, ResearchRefusal)
    assert refused.reason_code == "performance_reconciliation_unexplained"
    assert not isinstance(explained, ResearchRefusal)
    assert explained.status is ResearchStatus.REDUCED
    assert explained.reconciliation.state == "named_difference"


def test_partial_history_reduces_and_account_aliases_remain_isolated() -> None:
    partial = analyze_portfolio_truth(
        _dataset(quality_state=QualityState.PARTIAL, benchmark=_benchmark()),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    second = _dataset(
        account_alias=_ALIAS_B,
        dataset_id=_DATASET_B,
        snapshot_id=_SNAPSHOT_B,
        benchmark=_benchmark(),
    )
    combined = analyze_multi_account_portfolios(
        (
            _dataset(benchmark=_benchmark()),
            second,
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(partial, ResearchRefusal)
    assert partial.status is ResearchStatus.REDUCED
    assert "partial_history" in partial.warnings
    assert not isinstance(combined, ResearchRefusal)
    assert combined.account_aliases == (_ALIAS_A, _ALIAS_B)
    assert tuple(item.account_alias for item in combined.accounts) == (_ALIAS_A, _ALIAS_B)

    with pytest.raises(ValidationError, match="account alias"):
        _dataset(entries=(_entry("7", LedgerKind.DEPOSIT, "1", account_alias=_ALIAS_B),))


def test_authoritative_tax_lot_export_refuses_without_frozen_basis() -> None:
    result = authoritative_tax_lot_export(_dataset(benchmark=_benchmark()))

    assert isinstance(result, ResearchRefusal)
    assert result.analysis_kind == "tax_lot_export"
    assert result.reason_code == "authoritative_tax_lot_basis_unavailable"
    assert result.source_scope is None
