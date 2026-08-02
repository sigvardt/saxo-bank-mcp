from __future__ import annotations

import math

import numpy as np
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from saxo_bank_mcp.analytics_metrics import (
    cagr,
    correlation,
    covariance,
    cumulative_return,
    drawdown_series,
    expected_shortfall,
    historical_var,
    maximum_drawdown,
    simple_returns,
    time_weighted_return,
    volatility,
)
from saxo_bank_mcp.analytics_reference_metrics import (
    reference_covariance,
    reference_cumulative_return,
    reference_simple_returns,
    reference_time_weighted_return,
)

_PROPERTY_SETTINGS = settings(max_examples=60, deadline=None, derandomize=True)
_FINITE_RETURN = st.floats(
    min_value=-0.8,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)
_POSITIVE_PRICE = st.floats(
    min_value=1.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)


@seed(2026080201)
@_PROPERTY_SETTINGS
@given(
    returns=st.lists(_FINITE_RETURN, min_size=2, max_size=20),
    offset=st.floats(
        min_value=-1.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_translation_does_not_change_volatility(
    returns: list[float],
    offset: float,
) -> None:
    translated = [value + offset for value in returns]

    assert math.isclose(
        volatility(returns, 252.0),
        volatility(translated, 252.0),
        rel_tol=1e-10,
        abs_tol=1e-10,
    )


@seed(2026080202)
@_PROPERTY_SETTINGS
@given(
    prices=st.lists(_POSITIVE_PRICE, min_size=2, max_size=20),
    scale=st.floats(
        min_value=0.01,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_price_scale_does_not_change_returns(
    prices: list[float],
    scale: float,
) -> None:
    scaled = [value * scale for value in prices]

    np.testing.assert_allclose(
        simple_returns(prices),
        simple_returns(scaled),
        rtol=1e-10,
        atol=1e-10,
    )
    np.testing.assert_allclose(
        reference_simple_returns(prices),
        simple_returns(prices),
        rtol=1e-10,
        atol=1e-10,
    )


@seed(2026080203)
@_PROPERTY_SETTINGS
@given(
    x=st.lists(_FINITE_RETURN, min_size=2, max_size=20),
    shift=st.floats(
        min_value=-1.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    permutation_seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_property_paired_permutation_preserves_covariance(
    x: list[float],
    shift: float,
    permutation_seed: int,
) -> None:
    y = [2.0 * value + shift for value in x]
    order = np.random.default_rng(permutation_seed).permutation(len(x))
    permuted_x = [x[int(index)] for index in order]
    permuted_y = [y[int(index)] for index in order]

    assert math.isclose(
        covariance(x, y),
        covariance(permuted_x, permuted_y),
        rel_tol=1e-10,
        abs_tol=1e-10,
    )
    assert math.isclose(
        covariance(x, y),
        reference_covariance(x, y),
        rel_tol=1e-10,
        abs_tol=1e-10,
    )


@seed(2026080204)
@_PROPERTY_SETTINGS
@given(prices=st.lists(_POSITIVE_PRICE, min_size=2, max_size=20))
def test_property_compounding_matches_price_endpoints(prices: list[float]) -> None:
    returns = simple_returns(prices)
    expected = prices[-1] / prices[0] - 1.0

    assert math.isclose(cumulative_return(returns), expected, rel_tol=1e-10, abs_tol=1e-10)
    assert math.isclose(
        reference_cumulative_return(returns),
        expected,
        rel_tol=1e-10,
        abs_tol=1e-10,
    )


@seed(2026080205)
@_PROPERTY_SETTINGS
@given(
    lower=st.floats(
        min_value=0.01,
        max_value=1_000_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    increment=st.floats(
        min_value=0.0,
        max_value=1_000_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    years=st.floats(
        min_value=0.5,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_cagr_is_monotone_in_ending_value(
    lower: float,
    increment: float,
    years: float,
) -> None:
    higher = lower + increment

    assert cagr(100.0, higher, years) >= cagr(100.0, lower, years)


@seed(2026080206)
@_PROPERTY_SETTINGS
@given(
    periodic_return=st.floats(
        min_value=-0.2,
        max_value=0.2,
        allow_nan=False,
        allow_infinity=False,
    ),
    flows=st.lists(
        st.floats(
            min_value=-10.0,
            max_value=10.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_property_twr_is_neutral_to_external_cash_flows(
    periodic_return: float,
    flows: list[float],
) -> None:
    valuations = [100.0]
    for flow in flows:
        valuations.append(valuations[-1] * (1.0 + periodic_return) + flow)
    expected = (1.0 + periodic_return) ** len(flows) - 1.0

    assert math.isclose(
        time_weighted_return(valuations, flows),
        expected,
        rel_tol=1e-10,
        abs_tol=1e-10,
    )
    assert math.isclose(
        reference_time_weighted_return(valuations, flows),
        expected,
        rel_tol=1e-10,
        abs_tol=1e-10,
    )


@seed(2026080207)
@_PROPERTY_SETTINGS
@given(values=st.lists(_POSITIVE_PRICE, min_size=1, max_size=30))
def test_property_drawdown_is_always_between_full_loss_and_zero(values: list[float]) -> None:
    drawdowns = drawdown_series(values)

    assert np.all(drawdowns <= 0.0)
    assert np.all(drawdowns >= -1.0)
    assert -1.0 <= maximum_drawdown(values) <= 0.0


@seed(2026080208)
@_PROPERTY_SETTINGS
@given(
    x=st.lists(_FINITE_RETURN, min_size=2, max_size=20),
    y=st.lists(_FINITE_RETURN, min_size=2, max_size=20),
)
def test_property_covariance_is_symmetric_for_aligned_series(
    x: list[float],
    y: list[float],
) -> None:
    size = min(len(x), len(y))
    aligned_x = x[:size]
    aligned_y = y[:size]

    assert math.isclose(
        covariance(aligned_x, aligned_y),
        covariance(aligned_y, aligned_x),
        rel_tol=1e-10,
        abs_tol=1e-10,
    )


@seed(2026080209)
@_PROPERTY_SETTINGS
@given(
    values=st.lists(
        st.floats(
            min_value=-0.5,
            max_value=0.5,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=2,
        max_size=30,
    ),
)
def test_property_expected_shortfall_is_at_least_historical_var(
    values: list[float],
) -> None:
    values[0] = -max(abs(values[0]), 1e-12)
    value_at_risk = historical_var(values, 0.8)
    tail_mean = expected_shortfall(values, 0.8)

    assert tail_mean + 1e-12 >= value_at_risk


@seed(2026080210)
@_PROPERTY_SETTINGS
@given(random_seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_property_deterministic_seed_repeats_identical_metric_output(random_seed: int) -> None:
    first_prices = np.random.default_rng(random_seed).uniform(1.0, 100.0, size=20)
    second_prices = np.random.default_rng(random_seed).uniform(1.0, 100.0, size=20)

    np.testing.assert_array_equal(simple_returns(first_prices), simple_returns(second_prices))


@seed(2026080211)
@_PROPERTY_SETTINGS
@given(
    x=st.lists(
        st.integers(min_value=-5_000, max_value=5_000).map(lambda value: value / 10_000),
        min_size=2,
        max_size=20,
        unique=True,
    ),
    positive_scale=st.floats(
        min_value=0.01,
        max_value=100.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_positive_scale_preserves_correlation(
    x: list[float],
    positive_scale: float,
) -> None:
    scaled = [value * positive_scale for value in x]

    assert math.isclose(correlation(x, scaled), 1.0, rel_tol=1e-10, abs_tol=1e-10)
