# ruff: noqa: PLR0913

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from saxo_bank_mcp.analytics_fx import FxQuote
from saxo_bank_mcp.analytics_income import (
    CorporateActionClaimDataset,
    IncomeDataset,
    IncomeEntry,
    analyze_income,
    authoritative_corporate_action_claim,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000006"
_DATASET = "ds_00000000000040008000000000000036"
_AT = datetime(2026, 4, 1, tzinfo=UTC)


def _source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:r4",
        capture_fingerprint_sha256="d" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _entry(
    key: str,
    kind: Literal["dividend", "coupon", "interest"],
    gross: str,
    tax: str,
    *,
    revision: int = 1,
    currency: str = "USD",
) -> IncomeEntry:
    return IncomeEntry(
        account_alias=_ALIAS,
        event_key_sha256=key * 64,
        revision=revision,
        occurred_at=_AT,
        kind=kind,
        gross_amount=Decimal(gross),
        withholding_tax=Decimal(tax),
        currency=currency,
    )


def _dataset() -> IncomeDataset:
    return IncomeDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        as_of=_AT,
        reporting_currency="USD",
        entries=(
            _entry("7", "dividend", "10", "2"),
            _entry("7", "dividend", "12", "2", revision=2),
            _entry("8", "coupon", "10", "1", currency="EUR"),
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
            _source("transactions_v1"),
            _source("bookings_v1"),
        ),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )


def test_income_corrections_dividends_and_multi_currency_reconcile_gross_to_net() -> None:
    result = analyze_income(
        _dataset(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    assert values.dividends == Decimal(12)
    assert values.coupons == Decimal("12.0")
    assert values.gross_income == Decimal("24.0")
    assert values.withholding_tax == Decimal("3.2")
    assert values.net_income == Decimal("20.8")
    assert values.gross_income - values.withholding_tax == values.net_income


def test_income_money_is_owner_only_and_public_evidence_is_redacted() -> None:
    public = analyze_income(
        _dataset(),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    assert "gross_income" not in serialized
    assert "withholding_tax" not in serialized
    assert "net_income" not in serialized


def test_authoritative_corporate_action_claim_refuses_missing_basis_or_entitlement() -> None:
    missing_basis = authoritative_corporate_action_claim(
        CorporateActionClaimDataset(
            dataset_id=_DATASET,
            account_alias=_ALIAS,
            source_bindings=(
                _source("corporate_action_events_v2"),
                _source("corporate_action_holdings_v2"),
            ),
            entitlement_state="available",
            required_basis_available=False,
        ),
    )
    denied = authoritative_corporate_action_claim(
        CorporateActionClaimDataset(
            dataset_id=_DATASET,
            account_alias=_ALIAS,
            source_bindings=(
                _source("corporate_action_events_v2"),
                _source("corporate_action_holdings_v2"),
            ),
            entitlement_state="denied",
            required_basis_available=True,
        ),
    )

    assert isinstance(missing_basis, ResearchRefusal)
    assert missing_basis.reason_code == "corporate_action_basis_unavailable"
    assert isinstance(denied, ResearchRefusal)
    assert denied.reason_code == "corporate_action_entitlement_insufficient"
