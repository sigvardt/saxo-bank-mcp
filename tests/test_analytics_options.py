from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from saxo_bank_mcp.analytics_derivatives_reference import (
    reference_black_76,
    reference_black_scholes,
    reference_implied_volatility,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal
from saxo_bank_mcp.analytics_options import (
    ImpliedVolatilityRequest,
    ImpliedVolatilityResult,
    OptionContract,
    OptionGreekComparison,
    OptionGreeks,
    OptionModelInput,
    OptionModelValues,
    OptionStrategyLeg,
    OptionStrategyRequest,
    StrategyGreeks,
    aggregate_strategy_greeks,
    compare_greek_vectors,
    model_option,
    solve_implied_volatility,
    strategy_payoff,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "analytics" / "golden_options.json"
_DATASET = "ds_00000000000040008000000000000071"
_OPTION_HANDLE = "ih_00000000000040008000000000000071"
_SECOND_OPTION_HANDLE = "ih_00000000000040008000000000000072"
_UNDERLYING_HANDLE = "ih_00000000000040008000000000000073"
_ALIAS = "aa_00000000000040008000000000000071"
_ABSOLUTE_TOLERANCE = 1e-9
_RELATIVE_TOLERANCE = 1e-7
_EXPIRY_INTRINSIC = 10.0
_DEEP_CALL_FLOOR = 998.0
_DEEP_PUT_CEILING = 1e-12
_LOW_PAYOFF = -6.0
_MIDDLE_PAYOFF = 4.0
_HIGH_PAYOFF = 14.0


class _GoldenValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: float
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_volatility_point: float
    rho_per_rate_point: float


class _GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reference_price: float
    strike: float
    time_to_expiry_years: float
    volatility: float
    risk_free_rate: float
    dividend_yield: float | None = None
    call: _GoldenValues
    put: _GoldenValues


class _GoldenFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    black_scholes: _GoldenCase
    black_76: _GoldenCase


def _golden() -> _GoldenFixture:
    return _GoldenFixture.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _contract(  # noqa: PLR0913
    *,
    pricing_model: Literal["black_scholes", "black_76"] = "black_scholes",
    option_type: Literal["call", "put"] = "call",
    option_handle: str = _OPTION_HANDLE,
    reference_price: float = 100.0,
    strike: float = 100.0,
    time_to_expiry_years: float = 1.0,
    risk_free_rate: float = 0.05,
    dividend_yield: float | None = 0.0,
    exercise_style: Literal["european", "american"] = "european",
    payoff_style: Literal["vanilla", "path_dependent"] = "vanilla",
    rate_model: Literal["constant", "complex"] = "constant",
) -> OptionContract:
    return OptionContract(
        dataset_id=_DATASET,
        option_handle=option_handle,
        underlying_handle=_UNDERLYING_HANDLE,
        contract_currency="USD",
        pricing_model=pricing_model,
        reference_kind="spot" if pricing_model == "black_scholes" else "forward_or_futures",
        option_type=option_type,
        exercise_style=exercise_style,
        payoff_style=payoff_style,
        rate_model=rate_model,
        reference_price=reference_price,
        strike=strike,
        time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=risk_free_rate,
        dividend_yield=(dividend_yield if pricing_model == "black_scholes" else None),
        days_per_year=365.0,
    )


def _modeled(contract: OptionContract, volatility: float = 0.2) -> OptionModelValues:
    result = model_option(OptionModelInput(contract=contract, volatility=volatility))
    assert not isinstance(result, ResearchRefusal), (result.reason_code, result.reason)
    return result


def _assert_close(actual: float, expected: float) -> None:
    assert math.isclose(
        actual,
        expected,
        rel_tol=_RELATIVE_TOLERANCE,
        abs_tol=_ABSOLUTE_TOLERANCE,
    )


def _assert_greeks(actual: OptionGreeks, expected: _GoldenValues) -> None:
    _assert_close(actual.delta, expected.delta)
    _assert_close(actual.gamma, expected.gamma)
    _assert_close(actual.theta_per_day, expected.theta_per_day)
    _assert_close(actual.vega_per_volatility_point, expected.vega_per_volatility_point)
    _assert_close(actual.rho_per_rate_point, expected.rho_per_rate_point)


def test_black_scholes_put_call_parity_and_cross_library_golden() -> None:
    case = _golden().black_scholes
    call = _modeled(_contract(option_type="call"), case.volatility)
    put = _modeled(_contract(option_type="put"), case.volatility)
    reference_call = reference_black_scholes(
        option_type="call",
        spot=case.reference_price,
        strike=case.strike,
        time_to_expiry_years=case.time_to_expiry_years,
        volatility=case.volatility,
        risk_free_rate=case.risk_free_rate,
        dividend_yield=case.dividend_yield or 0.0,
        days_per_year=365.0,
    )
    reference_put = reference_black_scholes(
        option_type="put",
        spot=case.reference_price,
        strike=case.strike,
        time_to_expiry_years=case.time_to_expiry_years,
        volatility=case.volatility,
        risk_free_rate=case.risk_free_rate,
        dividend_yield=case.dividend_yield or 0.0,
        days_per_year=365.0,
    )

    _assert_close(call.value, case.call.value)
    _assert_close(put.value, case.put.value)
    _assert_close(call.value, reference_call.value)
    _assert_close(put.value, reference_put.value)
    assert call.greeks is not None
    assert put.greeks is not None
    _assert_greeks(call.greeks, case.call)
    _assert_greeks(put.greeks, case.put)
    _assert_close(call.greeks.delta, reference_call.delta)
    _assert_close(put.greeks.delta, reference_put.delta)
    parity_right = case.reference_price - case.strike * math.exp(-case.risk_free_rate)
    _assert_close(call.value - put.value, parity_right)


def test_black_76_put_call_parity_and_cross_library_golden() -> None:
    case = _golden().black_76
    call = _modeled(
        _contract(pricing_model="black_76", option_type="call", dividend_yield=None),
        case.volatility,
    )
    put = _modeled(
        _contract(pricing_model="black_76", option_type="put", dividend_yield=None),
        case.volatility,
    )
    reference_call = reference_black_76(
        option_type="call",
        futures_price=case.reference_price,
        strike=case.strike,
        time_to_expiry_years=case.time_to_expiry_years,
        volatility=case.volatility,
        risk_free_rate=case.risk_free_rate,
        days_per_year=365.0,
    )

    _assert_close(call.value, case.call.value)
    _assert_close(put.value, case.put.value)
    _assert_close(call.value, reference_call.value)
    assert call.greeks is not None
    assert put.greeks is not None
    _assert_greeks(call.greeks, case.call)
    _assert_greeks(put.greeks, case.put)
    parity_right = math.exp(-case.risk_free_rate) * (
        case.reference_price - case.strike
    )
    _assert_close(call.value - put.value, parity_right)


def test_analytic_greeks_match_independent_finite_differences() -> None:
    contract = _contract(reference_price=103.0, strike=97.0, risk_free_rate=0.03)
    volatility = 0.24
    modeled = _modeled(contract, volatility)
    assert modeled.greeks is not None
    price_step = 0.001
    volatility_step = 0.00001
    rate_step = 0.00001
    time_step = 0.00001
    up = _modeled(contract.model_copy(update={"reference_price": 103.0 + price_step}), volatility)
    down = _modeled(
        contract.model_copy(update={"reference_price": 103.0 - price_step}),
        volatility,
    )
    delta = (up.value - down.value) / (2.0 * price_step)
    gamma = (up.value - 2.0 * modeled.value + down.value) / (price_step**2)
    volatility_up = _modeled(contract, volatility + volatility_step)
    volatility_down = _modeled(contract, volatility - volatility_step)
    vega = (
        (volatility_up.value - volatility_down.value)
        / (2.0 * volatility_step)
        / 100.0
    )
    rate_up = _modeled(
        contract.model_copy(update={"risk_free_rate": 0.03 + rate_step}),
        volatility,
    )
    rate_down = _modeled(
        contract.model_copy(update={"risk_free_rate": 0.03 - rate_step}),
        volatility,
    )
    rho = (rate_up.value - rate_down.value) / (2.0 * rate_step) / 100.0
    time_down = _modeled(
        contract.model_copy(update={"time_to_expiry_years": 1.0 - time_step}),
        volatility,
    )
    theta = (time_down.value - modeled.value) / time_step / 365.0

    assert math.isclose(modeled.greeks.delta, delta, abs_tol=2e-7)
    assert math.isclose(modeled.greeks.gamma, gamma, abs_tol=2e-6)
    assert math.isclose(modeled.greeks.theta_per_day, theta, abs_tol=2e-7)
    assert math.isclose(modeled.greeks.vega_per_volatility_point, vega, abs_tol=2e-7)
    assert math.isclose(modeled.greeks.rho_per_rate_point, rho, abs_tol=2e-7)


def test_expiry_returns_intrinsic_and_zero_volatility_elsewhere_refuses() -> None:
    expired_call = _modeled(
        _contract(reference_price=110.0, strike=100.0, time_to_expiry_years=0.0),
        0.0,
    )
    expired_put = _modeled(
        _contract(
            option_type="put",
            reference_price=90.0,
            strike=100.0,
            time_to_expiry_years=0.0,
        ),
        0.0,
    )
    zero_volatility = model_option(
        OptionModelInput(contract=_contract(), volatility=0.0),
    )

    assert expired_call.value == _EXPIRY_INTRINSIC
    assert expired_put.value == _EXPIRY_INTRINSIC
    assert expired_call.greeks is None
    assert expired_put.greeks is None
    assert expired_call.warnings == ("option_greeks_unavailable_at_expiry",)
    assert isinstance(zero_volatility, ResearchRefusal)
    assert zero_volatility.reason_code == "option_volatility_nonpositive"


def test_deep_in_and_out_of_money_supported_values_remain_finite() -> None:
    deep_call = _modeled(
        _contract(reference_price=1000.0, strike=1.0, risk_free_rate=0.05),
        0.01,
    )
    deep_put = _modeled(
        _contract(
            option_type="put",
            reference_price=1000.0,
            strike=1.0,
            risk_free_rate=0.05,
        ),
        0.01,
    )

    assert math.isfinite(deep_call.value)
    assert math.isfinite(deep_put.value)
    assert deep_call.value > _DEEP_CALL_FLOOR
    assert deep_put.value < _DEEP_PUT_CEILING


def test_implied_volatility_bisection_and_cross_library_recover_input() -> None:
    contract = _contract(option_type="put", reference_price=102.0, strike=99.0)
    observed = _modeled(contract, 0.31).value
    request = ImpliedVolatilityRequest(
        contract=contract,
        observed_price=observed,
        sigma_min=0.001,
        sigma_max=3.0,
        price_tolerance=1e-12,
        sigma_tolerance=1e-12,
        maximum_iterations=256,
    )
    result = solve_implied_volatility(request)
    reference = reference_implied_volatility(
        pricing_model="black_scholes",
        option_type="put",
        reference_price=102.0,
        strike=99.0,
        time_to_expiry_years=1.0,
        observed_price=observed,
        risk_free_rate=0.05,
        dividend_yield=0.0,
        sigma_min=0.001,
        sigma_max=3.0,
    )

    assert isinstance(result, ImpliedVolatilityResult)
    assert math.isclose(result.volatility, 0.31, abs_tol=1e-9)
    assert math.isclose(result.volatility, reference, abs_tol=1e-9)
    assert result.price_residual <= request.price_tolerance or math.isclose(
        result.volatility,
        0.31,
        abs_tol=request.sigma_tolerance,
    )


def test_unbracketed_implied_volatility_refuses_exactly() -> None:
    result = solve_implied_volatility(
        ImpliedVolatilityRequest(
            contract=_contract(),
            observed_price=200.0,
            sigma_min=0.001,
            sigma_max=1.0,
            price_tolerance=1e-12,
            sigma_tolerance=1e-12,
            maximum_iterations=64,
        ),
    )

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == "implied_volatility_unbracketed"


def _strategy() -> OptionStrategyRequest:
    return OptionStrategyRequest(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        strategy_id="call_spread_fixture",
        legs=(
            OptionStrategyLeg(
                model_input=OptionModelInput(
                    contract=_contract(strike=90.0),
                    volatility=0.2,
                ),
                quantity=1.0,
                contract_multiplier=1.0,
            ),
            OptionStrategyLeg(
                model_input=OptionModelInput(
                    contract=_contract(
                        option_handle=_SECOND_OPTION_HANDLE,
                        strike=110.0,
                    ),
                    volatility=0.2,
                ),
                quantity=-1.0,
                contract_multiplier=1.0,
            ),
        ),
        net_premium=5.0,
        eligible_costs=1.0,
    )


def test_multi_leg_payoff_and_aggregate_greeks_reconcile_to_leg_sums() -> None:
    request = _strategy()
    payoff = strategy_payoff(request, expiry_reference_price=100.0)
    aggregate = aggregate_strategy_greeks(request)
    leg_greeks = tuple(_modeled(leg.model_input.contract).greeks for leg in request.legs)

    assert not isinstance(payoff, ResearchRefusal)
    assert payoff.leg_payoffs == (10.0, -0.0)
    assert payoff.total_payoff == _MIDDLE_PAYOFF
    assert isinstance(aggregate, StrategyGreeks)
    assert all(item is not None for item in leg_greeks)
    first = leg_greeks[0]
    second = leg_greeks[1]
    assert first is not None
    assert second is not None
    _assert_close(aggregate.delta, first.delta - second.delta)
    _assert_close(aggregate.gamma, first.gamma - second.gamma)
    _assert_close(aggregate.theta_per_day, first.theta_per_day - second.theta_per_day)
    _assert_close(
        aggregate.vega_per_volatility_point,
        first.vega_per_volatility_point - second.vega_per_volatility_point,
    )
    _assert_close(
        aggregate.rho_per_rate_point,
        first.rho_per_rate_point - second.rho_per_rate_point,
    )


def test_expiry_payoff_is_piecewise_linear_in_supported_limiting_regions() -> None:
    request = _strategy()
    low = strategy_payoff(request, expiry_reference_price=80.0)
    middle = strategy_payoff(request, expiry_reference_price=100.0)
    high = strategy_payoff(request, expiry_reference_price=120.0)

    assert not isinstance(low, ResearchRefusal)
    assert not isinstance(middle, ResearchRefusal)
    assert not isinstance(high, ResearchRefusal)
    assert low.total_payoff == _LOW_PAYOFF
    assert middle.total_payoff == _MIDDLE_PAYOFF
    assert high.total_payoff == _HIGH_PAYOFF


def test_greek_comparison_preserves_both_sides_and_never_averages() -> None:
    local = OptionGreeks(
        delta=0.5,
        gamma=0.02,
        theta_per_day=-0.01,
        vega_per_volatility_point=0.3,
        rho_per_rate_point=0.4,
    )
    broker = local.model_copy(update={"delta": 0.55, "gamma": 0.03})
    comparison = compare_greek_vectors(
        local,
        broker,
        absolute_tolerance=1e-9,
        relative_tolerance=1e-7,
    )

    assert isinstance(comparison, OptionGreekComparison)
    assert comparison.state == "disagreement"
    assert comparison.local == local
    assert comparison.saxo_reported == broker
    assert comparison.differences.delta == pytest.approx(-0.05)
    assert comparison.averaged is False
    assert not hasattr(comparison, "average")


@pytest.mark.parametrize(
    ("contract", "reason_code"),
    [
        (_contract(exercise_style="american"), "option_american_unsupported"),
        (_contract(payoff_style="path_dependent"), "option_path_dependency_unsupported"),
        (_contract(rate_model="complex"), "option_complex_rates_unsupported"),
    ],
)
def test_unsupported_option_capabilities_refuse_exactly(
    contract: OptionContract,
    reason_code: str,
) -> None:
    result = model_option(OptionModelInput(contract=contract, volatility=0.2))

    assert isinstance(result, ResearchRefusal)
    assert result.reason_code == reason_code


def test_option_contracts_reject_arbitrary_execution_surfaces() -> None:
    data = _contract().model_dump()
    for field, value in (
        ("sql", "select positions"),
        ("python", "lambda value: value"),
        ("expression", "spot * delta"),
        ("path", "relative/input.json"),
        ("network_callback", "https://example.invalid"),
        ("order", {"side": "Buy"}),
    ):
        with pytest.raises(ValidationError):
            OptionContract.model_validate({**data, field: value})
    fixture_text = json.dumps(json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))).lower()
    for forbidden in ("accountkey", "clientkey", "balance", "holding", "orderid"):
        assert forbidden not in fixture_text
    assert Decimal("0.2") == Decimal(str(_golden().black_scholes.volatility))
