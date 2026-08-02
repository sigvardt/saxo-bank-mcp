from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_position_sizing import (
    PositionSizingRequest,
    size_position,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)

_ALIAS = "aa_00000000000040008000000000000051"
_DATASET = "ds_00000000000040008000000000000051"
_HANDLE = "ih_00000000000040008000000000000051"


def _source(
    contract_id: str,
    *,
    quality: QualityState = QualityState.COMPLETE,
    entitlement: Literal["available", "partial", "denied"] = "available",
) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t15",
        capture_fingerprint_sha256=("a" if contract_id == "balances_v1" else "b") * 64,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _request(  # noqa: PLR0913
    *,
    method: Literal["stop_distance", "volatility"] = "stop_distance",
    maximum_loss: Decimal | None = Decimal(1000),
    risk_budget_confirmed: bool = True,
    entry_price: Decimal = Decimal(100),
    stop_price: Decimal | None = Decimal(95),
    volatility_measure: Decimal | None = None,
    volatility_multiplier: Decimal | None = None,
    portfolio_value: Decimal = Decimal(100000),
    maximum_weight: Decimal = Decimal("0.5"),
    buying_power: Decimal = Decimal(100000),
    reserved_buffer: Decimal = Decimal(0),
    estimated_transaction_cost: Decimal = Decimal(0),
    margin_headroom: Decimal = Decimal(100000),
    margin_requirement_per_money_unit: Decimal = Decimal("0.2"),
    lot_size: Decimal = Decimal(1),
    quality_state: QualityState = QualityState.COMPLETE,
    source_bindings: tuple[SaxoSourceBinding, ...] | None = None,
) -> PositionSizingRequest:
    return PositionSizingRequest(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE,
        reporting_currency="USD",
        method=method,
        maximum_loss=maximum_loss,
        risk_budget_confirmed=risk_budget_confirmed,
        entry_price=entry_price,
        stop_price=stop_price,
        volatility_measure=volatility_measure,
        volatility_multiplier=volatility_multiplier,
        value_per_price_unit=Decimal(1),
        lot_size=lot_size,
        portfolio_value=portfolio_value,
        maximum_weight=maximum_weight,
        buying_power=buying_power,
        reserved_buffer=reserved_buffer,
        estimated_transaction_cost=estimated_transaction_cost,
        margin_headroom=margin_headroom,
        margin_requirement_per_money_unit=margin_requirement_per_money_unit,
        source_bindings=source_bindings
        or (
            _source("balances_v1"),
            _source("positions_v1"),
            _source("costs_v1"),
        ),
        quality_state=quality_state,
        missing_fields=("costs.TotalCost",) if quality_state is QualityState.PARTIAL else (),
        warnings=(),
    )


def _private(request: PositionSizingRequest):  # noqa: ANN202
    result = size_position(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    assert not isinstance(result, ResearchRefusal)
    assert result.private_values is not None
    return result, result.private_values


def test_stop_sizing_uses_explicit_loss_budget_and_exact_constraints() -> None:
    result, values = _private(_request())

    assert result.status is ResearchStatus.COMPLETE
    assert values.maximum_loss == Decimal(1000)
    assert values.risk_budget_source == "caller_supplied"
    assert values.stop_distance == Decimal(5)
    assert values.unit_loss == Decimal(5)
    assert values.risk_quantity == Decimal(200)
    assert values.concentration_limit == Decimal(50000)
    assert values.concentration_quantity == Decimal(500)
    assert values.buying_power_limit == Decimal(100000)
    assert values.buying_power_quantity == Decimal(1000)
    assert values.margin_constraint == Decimal(500000)
    assert values.margin_quantity == Decimal(5000)
    assert values.quantity == Decimal(200)
    assert values.estimated_loss_at_invalidation == Decimal(1000)
    assert values.limiting_constraints == ("maximum_loss",)
    assert result.proposal_only is True
    assert result.risk_budget_chosen_by_engine is False
    assert result.approval_authority is False
    assert result.execution_authority is False


def test_sizing_refuses_missing_unconfirmed_budget_and_zero_unit_loss() -> None:
    missing = size_position(
        _request(maximum_loss=None, risk_budget_confirmed=False),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    zero_distance = size_position(
        _request(stop_price=Decimal(100)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(missing, ResearchRefusal)
    assert missing.reason_code == "caller_risk_budget_required"
    assert isinstance(zero_distance, ResearchRefusal)
    assert zero_distance.reason_code == "position_unit_loss_undefined"


def test_volatility_sizing_scales_with_budget_and_requires_explicit_method_inputs() -> None:
    base_request = _request(
        method="volatility",
        stop_price=None,
        volatility_measure=Decimal(2),
        volatility_multiplier=Decimal("2.5"),
        portfolio_value=Decimal(1000000),
        maximum_weight=Decimal(1),
        buying_power=Decimal(1000000),
        margin_headroom=Decimal(1000000),
        margin_requirement_per_money_unit=Decimal("0.01"),
    )
    _, base = _private(base_request)
    _, doubled = _private(
        _request(
            method="volatility",
            maximum_loss=Decimal(2000),
            stop_price=None,
            volatility_measure=Decimal(2),
            volatility_multiplier=Decimal("2.5"),
            portfolio_value=Decimal(1000000),
            maximum_weight=Decimal(1),
            buying_power=Decimal(1000000),
            margin_headroom=Decimal(1000000),
            margin_requirement_per_money_unit=Decimal("0.01"),
        ),
    )
    missing = size_position(
        _request(method="volatility", stop_price=None),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert base.method == "volatility"
    assert base.unit_loss == Decimal(5)
    assert base.quantity == Decimal(200)
    assert doubled.quantity == base.quantity * 2
    assert isinstance(missing, ResearchRefusal)
    assert missing.reason_code == "volatility_sizing_inputs_required"


def test_tighter_portfolio_broker_or_margin_constraint_never_increases_size() -> None:
    _, baseline = _private(_request())
    _, concentration_limited = _private(_request(maximum_weight=Decimal("0.05")))
    _, buying_power_limited = _private(
        _request(buying_power=Decimal(6000), estimated_transaction_cost=Decimal(1000)),
    )
    _, margin_limited = _private(
        _request(margin_headroom=Decimal(1000), margin_requirement_per_money_unit=Decimal("0.2")),
    )
    _, zero_headroom = _private(_request(margin_headroom=Decimal(0)))

    assert concentration_limited.quantity <= baseline.quantity
    assert buying_power_limited.quantity <= baseline.quantity
    assert margin_limited.quantity <= baseline.quantity
    assert concentration_limited.quantity == Decimal(50)
    assert buying_power_limited.quantity == Decimal(50)
    assert margin_limited.quantity == Decimal(50)
    assert zero_headroom.quantity == Decimal(0)
    assert zero_headroom.limiting_constraints == ("margin",)


def test_sizing_refuses_unbound_or_unusable_saxo_inputs() -> None:
    missing_costs = size_position(
        _request(source_bindings=(_source("balances_v1"),)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    denied_balance = size_position(
        _request(
            source_bindings=(
                _source("balances_v1", entitlement="denied"),
                _source("positions_v1"),
                _source("costs_v1"),
            ),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    stale = size_position(
        _request(quality_state=QualityState.STALE),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert isinstance(missing_costs, ResearchRefusal)
    assert missing_costs.reason_code == "source_contract_missing"
    assert isinstance(denied_balance, ResearchRefusal)
    assert denied_balance.reason_code == "source_entitlement_insufficient"
    assert isinstance(stale, ResearchRefusal)
    assert stale.reason_code == "position_sizing_data_unusable"


def test_sizing_public_evidence_redacts_money_and_input_rejects_arbitrary_logic() -> None:
    public = size_position(
        _request(maximum_loss=Decimal(1234), portfolio_value=Decimal(987654)),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    assert public.evidence.private_values_redacted is True
    rendered = json.dumps(public.model_dump(mode="json"), sort_keys=True)
    assert "1234" not in rendered
    assert "987654" not in rendered
    with pytest.raises(ValidationError):
        PositionSizingRequest.model_validate(
            {
                **_request().model_dump(),
                "risk_budget_expression": "portfolio_value * 0.01",
            },
        )
