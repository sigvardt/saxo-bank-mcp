from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from saxo_bank_mcp.analytics_fixed_income import (
    FixedIncomeCashFlow,
    FixedIncomeDataset,
    ResearchRefusal,
    ResearchStatus,
    analyze_fixed_income,
    convexity,
    modified_duration,
    price_from_yield,
    yield_to_maturity,
)

_HANDLE = "ih_00000000000040008000000000000020"
_DATASET = "ds_00000000000040008000000000000020"
_AS_OF = datetime(2026, 4, 1, tzinfo=UTC)
_PROPERTY_SETTINGS = settings(max_examples=40, deadline=None, derandomize=True)


def _bond(
    *,
    entitlement_state: Literal["available", "partial", "denied"] = "available",
    include_core: bool = True,
    include_carry_and_roll: bool = True,
) -> FixedIncomeDataset:
    return FixedIncomeDataset(
        dataset_id=_DATASET,
        instrument_handle=_HANDLE,
        as_of=_AS_OF,
        entitlement_state=entitlement_state,
        dirty_price=100.0 if include_core else None,
        cash_flows=(
            FixedIncomeCashFlow(years_from_settlement=1.0, amount=5.0),
            FixedIncomeCashFlow(years_from_settlement=2.0, amount=105.0),
        )
        if include_core
        else (),
        compounding_frequency=1 if include_core else None,
        day_count_basis="ACT/365" if include_core else None,
        settlement_at=_AS_OF if include_core else None,
        horizon_coupon_cashflows=2.5 if include_carry_and_roll else None,
        accrued_interest_change=0.5 if include_carry_and_roll else None,
        financing_cost=0.25 if include_carry_and_roll else None,
        same_curve_shorter_maturity_price=101.5 if include_carry_and_roll else None,
    )


def test_golden_par_bond_yield_duration_convexity_carry_and_roll_down() -> None:
    dataset = _bond()

    result = analyze_fixed_income(dataset)

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.yield_to_maturity == pytest.approx(0.05, abs=1e-12)
    present_values = (5.0 / 1.05, 105.0 / 1.05**2)
    macaulay = (1.0 * present_values[0] + 2.0 * present_values[1]) / 100.0
    expected_modified = macaulay / 1.05
    expected_convexity = (5.0 * 1.0 * 2.0 / 1.05**3 + 105.0 * 2.0 * 3.0 / 1.05**4) / 100.0
    assert result.modified_duration == pytest.approx(expected_modified)
    assert result.convexity == pytest.approx(expected_convexity)
    assert result.carry == pytest.approx(2.75)
    assert result.roll_down == pytest.approx(1.5)
    assert result.source_scope is None
    assert "fixed_income_source_contract_unbound" in result.warnings


def test_caller_availability_cannot_claim_unbound_fixed_income_saxo_provenance() -> None:
    result = analyze_fixed_income(_bond(entitlement_state="available"))

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.source_scope is None
    assert "fixed_income_source_contract_unbound" in result.warnings


def test_individual_fixed_income_formulas_match_hand_calculation() -> None:
    cash_flows = _bond().cash_flows
    rate = yield_to_maturity(cash_flows, dirty_price=100.0, compounding_frequency=1)

    assert rate == pytest.approx(0.05, abs=1e-12)
    assert price_from_yield(cash_flows, rate, 1) == pytest.approx(100.0)
    assert modified_duration(cash_flows, rate, 1, dirty_price=100.0) == pytest.approx(
        (5.0 / 1.05 + 2.0 * 105.0 / 1.05**2) / 100.0 / 1.05,
    )
    assert convexity(cash_flows, rate, 1, dirty_price=100.0) == pytest.approx(
        (5.0 * 2.0 / 1.05**3 + 105.0 * 6.0 / 1.05**4) / 100.0,
    )


def test_missing_authoritative_fields_refuse_all_fixed_income_measures() -> None:
    result = analyze_fixed_income(_bond(include_core=False))

    assert isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REFUSED
    assert result.reason_code == "fixed_income_fields_insufficient"
    assert set(result.missing_fields) == {
        "cash_flows",
        "compounding_frequency",
        "day_count_basis",
        "dirty_price",
        "settlement_at",
    }


@pytest.mark.parametrize("entitlement", ["denied", "partial"])
def test_fixed_income_refuses_insufficient_entitlement(
    entitlement: Literal["denied", "partial"],
) -> None:
    result = analyze_fixed_income(_bond(entitlement_state=entitlement))

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "fixed_income_entitlement_insufficient"
    assert result.source_scope is None
    assert "saxo" not in result.reason.casefold()


def test_missing_carry_and_curve_fields_reduce_without_inventing_them() -> None:
    result = analyze_fixed_income(_bond(include_carry_and_roll=False))

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.carry is None
    assert result.roll_down is None
    assert set(result.missing_fields) == {
        "accrued_interest_change",
        "financing_cost",
        "horizon_coupon_cashflows",
        "same_curve_shorter_maturity_price",
    }


def test_extreme_finite_inputs_return_measure_undefined_instead_of_dividing_by_zero() -> None:
    dataset = FixedIncomeDataset(
        dataset_id=_DATASET,
        instrument_handle=_HANDLE,
        as_of=_AS_OF,
        entitlement_state="available",
        dirty_price=1e308,
        cash_flows=(
            FixedIncomeCashFlow(years_from_settlement=1_000.0, amount=1e-20),
        ),
        compounding_frequency=1,
        day_count_basis="ACT/365",
        settlement_at=_AS_OF,
    )

    result = analyze_fixed_income(dataset)

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "fixed_income_measure_undefined"
    assert result.source_scope is None


@seed(2026080203)
@_PROPERTY_SETTINGS
@given(
    lower_yield=st.floats(
        min_value=-0.5,
        max_value=0.5,
        allow_nan=False,
        allow_infinity=False,
    ),
    increment=st.floats(
        min_value=1e-6,
        max_value=0.5,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_positive_cash_flow_price_falls_as_yield_rises(
    lower_yield: float,
    increment: float,
) -> None:
    cash_flows = _bond().cash_flows
    higher_yield = lower_yield + increment

    assert price_from_yield(cash_flows, higher_yield, 1) < price_from_yield(
        cash_flows,
        lower_yield,
        1,
    )


def test_invalid_fixed_income_cash_flow_is_rejected_before_calculation() -> None:
    with pytest.raises(ValidationError):
        FixedIncomeCashFlow(years_from_settlement=0.0, amount=5.0)
    with pytest.raises(ValidationError):
        FixedIncomeCashFlow(years_from_settlement=1.0, amount=math.inf)
