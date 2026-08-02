# ruff: noqa: PLR0913

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_exposure import (
    ExposureDataset,
    ExposurePosition,
    NamedExposureDifference,
    analyze_portfolio_exposure,
)
from saxo_bank_mcp.analytics_fx import FxQuote
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS_A = "aa_00000000000040008000000000000004"
_ALIAS_B = "aa_00000000000040008000000000000005"
_DATASET = "ds_00000000000040008000000000000035"
_STOCK = "ih_00000000000040008000000000000035"
_SHORT = "ih_00000000000040008000000000000036"
_OPTION = "ih_00000000000040008000000000000037"
_AT = datetime(2026, 3, 1, tzinfo=UTC)


def _source(contract_id: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:r3",
        capture_fingerprint_sha256="c" * 64,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _position(
    handle: str,
    *,
    kind: Literal["cash", "option", "future", "cfd", "fx"],
    quantity: str,
    price: str,
    currency: str,
    multiplier: str = "1",
    delta: str | None = "1",
    broker_exposure: str | None,
    account_alias: str = _ALIAS_A,
) -> ExposurePosition:
    return ExposurePosition(
        account_alias=account_alias,
        instrument_handle=handle,
        instrument_kind=kind,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
        contract_multiplier=Decimal(multiplier),
        delta=None if delta is None else Decimal(delta),
        currency=currency,
        broker_reported_exposure=(None if broker_exposure is None else Decimal(broker_exposure)),
        as_of=_AT,
    )


def _dataset(
    *,
    option_broker_exposure: str = "500",
    option_delta: str | None = "0.5",
    differences: tuple[NamedExposureDifference, ...] = (),
    quality_state: QualityState = QualityState.COMPLETE,
) -> ExposureDataset:
    return ExposureDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS_A,
        as_of=_AT,
        reporting_currency="USD",
        positions=(
            _position(
                _STOCK,
                kind="cash",
                quantity="10",
                price="10",
                currency="USD",
                broker_exposure="100",
            ),
            _position(
                _SHORT,
                kind="cash",
                quantity="-5",
                price="10",
                currency="EUR",
                broker_exposure="-50",
            ),
            _position(
                _OPTION,
                kind="option",
                quantity="2",
                price="50",
                currency="USD",
                multiplier="10",
                delta=option_delta,
                broker_exposure=option_broker_exposure,
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
            _source("positions_v1"),
            _source("exposure_instruments_v1"),
        ),
        quality_state=quality_state,
        missing_fields=("history.before_start",) if quality_state is QualityState.PARTIAL else (),
        named_differences=differences,
    )


def test_exposure_handles_shorts_derivatives_fx_and_allocation_identities() -> None:
    result = analyze_portfolio_exposure(
        _dataset(),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    assert values.long_exposure == Decimal(600)
    assert values.short_exposure == Decimal("60.0")
    assert values.gross_exposure == Decimal("660.0")
    assert values.net_exposure == Decimal("540.0")
    assert values.long_exposure - values.short_exposure == values.net_exposure
    assert values.long_exposure + values.short_exposure == values.gross_exposure
    assert sum(item.exposure for item in values.positions) == values.net_exposure
    assert sum(abs(item.gross_weight_percentage) for item in values.positions) == Decimal(100)


def test_exposure_refuses_unexplained_broker_difference_and_accepts_named_reason() -> None:
    refused = analyze_portfolio_exposure(
        _dataset(option_broker_exposure="510"),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    explained = analyze_portfolio_exposure(
        _dataset(
            option_broker_exposure="510",
            differences=(
                NamedExposureDifference(
                    instrument_handle=_OPTION,
                    calculated_minus_saxo=Decimal(-10),
                    reason_code="delta_snapshot_timing",
                ),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(refused, ResearchRefusal)
    assert refused.reason_code == "exposure_reconciliation_unexplained"
    assert not isinstance(explained, ResearchRefusal)
    assert explained.status is ResearchStatus.REDUCED
    assert explained.reconciliation.state == "named_difference"


def test_exposure_refuses_derivative_without_delta_and_reduces_partial_history() -> None:
    missing_delta = analyze_portfolio_exposure(
        _dataset(option_delta=None),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    partial = analyze_portfolio_exposure(
        _dataset(quality_state=QualityState.PARTIAL),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(missing_delta, ResearchRefusal)
    assert missing_delta.reason_code == "derivative_exposure_basis_missing"
    assert not isinstance(partial, ResearchRefusal)
    assert partial.status is ResearchStatus.REDUCED
    assert "partial_history" in partial.warnings


def test_exposure_account_alias_isolation_and_public_redaction() -> None:
    with pytest.raises(ValidationError, match="account alias"):
        ExposureDataset(
            dataset_id=_DATASET,
            account_alias=_ALIAS_A,
            as_of=_AT,
            reporting_currency="USD",
            positions=(
                _position(
                    _STOCK,
                    kind="cash",
                    quantity="1",
                    price="1",
                    currency="USD",
                    broker_exposure="1",
                    account_alias=_ALIAS_B,
                ),
            ),
            fx_quotes=(),
            source_bindings=(
                _source("positions_v1"),
                _source("exposure_instruments_v1"),
            ),
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            named_differences=(),
        )

    public = analyze_portfolio_exposure(
        _dataset(),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )
    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    assert "long_exposure" not in serialized
    assert "short_exposure" not in serialized
    assert "gross_weight_percentage" not in serialized
