from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from statistics import NormalDist
from typing import Literal, cast

import numpy as np
from numpy.typing import ArrayLike, NDArray

type FloatVector = NDArray[np.float64]
type QuantileMethod = Literal["linear", "lower", "higher", "midpoint", "nearest"]

_ROOT_GRID_SIZE = 4_097
_ROOT_ITERATIONS = 200
_ROOT_RELATIVE_TOLERANCE = 1e-12
_ROOT_INTERVAL_TOLERANCE = 1e-14
_MIN_ONE_PLUS_RATE = 1e-12
_MAX_RATE = 1_000_000.0
_MIN_SAMPLE_COUNT = 2
_QUANTILE_METHODS = frozenset({"linear", "lower", "higher", "midpoint", "nearest"})


class FinancialMetricError(ValueError):
    """Raised when a financial metric is undefined for the supplied complete input."""


def simple_returns(prices: ArrayLike) -> FloatVector:
    """Return ``P_t / P_(t-1) - 1`` for a complete positive price series."""
    values = _as_vector(prices, "prices", minimum_count=2)
    _require_strictly_positive(values, "prices")
    return np.asarray(values[1:] / values[:-1] - 1.0, dtype=np.float64)


def log_returns(prices: ArrayLike) -> FloatVector:
    """Return natural-log price relatives for a complete positive price series."""
    values = _as_vector(prices, "prices", minimum_count=2)
    _require_strictly_positive(values, "prices")
    return np.asarray(np.log(values[1:] / values[:-1]), dtype=np.float64)


def cumulative_returns(returns: ArrayLike) -> FloatVector:
    """Chain periodic simple returns without rounding intermediate growth."""
    values = _as_vector(returns, "returns", minimum_count=1)
    if np.any(values < -1.0):
        raise FinancialMetricError("returns cannot imply a loss greater than the investment")
    return np.asarray(np.cumprod(1.0 + values) - 1.0, dtype=np.float64)


def cumulative_return(returns: ArrayLike) -> float:
    """Return the final compounded simple return."""
    return float(cumulative_returns(returns)[-1])


def annualized_return(
    total_return: float,
    observation_count: int,
    periods_per_year: float,
) -> float:
    """Annualize one total return over an exact count of equal observation periods."""
    total = _finite_scalar(total_return, "total return")
    count = _observation_count(observation_count)
    if count <= 0:
        raise FinancialMetricError("annualization requires a positive observation count")
    annualization = _positive_scalar(periods_per_year, "periods per year")
    growth = 1.0 + total
    if growth <= 0.0:
        raise FinancialMetricError("annualization requires positive compounded growth")
    try:
        result = math.pow(growth, annualization / count) - 1.0
    except OverflowError as error:
        raise FinancialMetricError("annualized return is not finite") from error
    if not math.isfinite(result):
        raise FinancialMetricError("annualized return is not finite")
    return result


def time_weighted_return(
    valuations: ArrayLike,
    external_portfolio_flows: ArrayLike,
) -> float:
    """Chain subperiod returns with positive deposits and negative withdrawals removed."""
    values = _as_vector(valuations, "valuations", minimum_count=2)
    flows = _as_vector(
        external_portfolio_flows,
        "external portfolio flows",
        minimum_count=1,
    )
    if flows.size != values.size - 1:
        raise FinancialMetricError("TWR requires exactly one end-boundary flow per subperiod")
    _require_strictly_positive(values, "valuations")
    adjusted_end_values = values[1:] - flows
    if np.any(adjusted_end_values < 0.0):
        raise FinancialMetricError("TWR adjusted subperiod value cannot be negative")
    subperiod_growth = adjusted_end_values / values[:-1]
    return float(np.prod(subperiod_growth, dtype=np.float64) - 1.0)


def money_weighted_return(
    investor_cash_flows: ArrayLike,
    periods: ArrayLike | None = None,
) -> float:
    """Solve the periodic investor-signed discounted cash-flow equation for ``r > -1``."""
    flows = _as_vector(investor_cash_flows, "investor cash flows", minimum_count=2)
    times = (
        np.arange(flows.size, dtype=np.float64)
        if periods is None
        else _as_vector(periods, "cash-flow periods", minimum_count=2)
    )
    _validate_rate_inputs(flows, times)
    return _solve_discount_rate(flows, times)


def xirr(
    investor_cash_flows: ArrayLike,
    economic_dates: Sequence[date],
) -> float:
    """Solve annual money-weighted return with UTC economic dates and ACT/365 exponents."""
    flows = _as_vector(investor_cash_flows, "investor cash flows", minimum_count=2)
    dates = _ordered_economic_dates(economic_dates, expected_count=flows.size)
    start = dates[0]
    periods = np.asarray(
        [(economic_date - start).days / 365.0 for economic_date in dates],
        dtype=np.float64,
    )
    _validate_rate_inputs(flows, periods)
    return _solve_discount_rate(flows, periods)


def cagr(start_value: float, end_value: float, years: float) -> float:
    """Return compound annual growth over a positive ACT-profile year fraction."""
    start = _positive_scalar(start_value, "starting value")
    end = _positive_scalar(end_value, "ending value")
    elapsed = _positive_scalar(years, "elapsed years")
    try:
        result = math.pow(end / start, 1.0 / elapsed) - 1.0
    except OverflowError as error:
        raise FinancialMetricError("CAGR result is not finite") from error
    if not math.isfinite(result):
        raise FinancialMetricError("CAGR result is not finite")
    return result


def active_returns(subject_returns: ArrayLike, benchmark_returns: ArrayLike) -> FloatVector:
    """Return aligned arithmetic subject-minus-benchmark periodic returns."""
    subject, benchmark = _aligned_vectors(
        subject_returns,
        benchmark_returns,
        minimum_count=1,
    )
    return np.asarray(subject - benchmark, dtype=np.float64)


def active_return(subject_return: float, benchmark_return: float) -> float:
    """Return aggregate arithmetic subject return minus benchmark return."""
    return _finite_scalar(subject_return, "subject return") - _finite_scalar(
        benchmark_return,
        "benchmark return",
    )


def tracking_error(
    subject_returns: ArrayLike,
    benchmark_returns: ArrayLike,
    periods_per_year: float,
) -> float:
    """Return annualized sample standard deviation of aligned active returns."""
    active = active_returns(subject_returns, benchmark_returns)
    if active.size < _MIN_SAMPLE_COUNT:
        raise FinancialMetricError("tracking error requires at least two aligned periods")
    annualization = _positive_scalar(periods_per_year, "periods per year")
    return float(np.std(active, ddof=1) * math.sqrt(annualization))


def upside_capture(subject_returns: ArrayLike, benchmark_returns: ArrayLike) -> float:
    """Return geometric subject-to-benchmark capture in positive benchmark periods."""
    return _capture_ratio(subject_returns, benchmark_returns, positive=True)


def downside_capture(subject_returns: ArrayLike, benchmark_returns: ArrayLike) -> float:
    """Return geometric subject-to-benchmark capture in negative benchmark periods."""
    return _capture_ratio(subject_returns, benchmark_returns, positive=False)


def volatility(returns: ArrayLike, periods_per_year: float) -> float:
    """Return annualized sample standard deviation of complete periodic returns."""
    values = _as_vector(returns, "returns", minimum_count=2)
    annualization = _positive_scalar(periods_per_year, "periods per year")
    return float(np.std(values, ddof=1) * math.sqrt(annualization))


def downside_deviation(
    returns: ArrayLike,
    target_returns: float | ArrayLike,
    periods_per_year: float,
) -> float:
    """Return annualized root mean square of all target-relative negative deviations."""
    values = _as_vector(returns, "returns", minimum_count=1)
    targets = _aligned_rate_vector(target_returns, values.size, "target returns")
    annualization = _positive_scalar(periods_per_year, "periods per year")
    downside = np.minimum(values - targets, 0.0)
    return float(math.sqrt(float(np.mean(np.square(downside)))) * math.sqrt(annualization))


def drawdown_series(values: ArrayLike) -> FloatVector:
    """Return nonpositive decline from each running cumulative-value peak."""
    series = _as_vector(values, "cumulative values", minimum_count=1)
    if series[0] <= 0.0 or np.any(series < 0.0):
        raise FinancialMetricError(
            "drawdown requires a positive start and nonnegative cumulative values",
        )
    running_peaks = np.maximum.accumulate(series)
    if np.any(running_peaks <= 0.0):
        raise FinancialMetricError("drawdown running peaks must stay positive")
    return np.asarray(series / running_peaks - 1.0, dtype=np.float64)


def maximum_drawdown(values: ArrayLike) -> float:
    """Return the most negative point in the complete drawdown series."""
    return float(np.min(drawdown_series(values)))


def sharpe_ratio(
    returns: ArrayLike,
    risk_free_returns: float | ArrayLike,
    periods_per_year: float,
) -> float:
    """Return annualized mean excess return divided by annualized return volatility."""
    values = _as_vector(returns, "returns", minimum_count=2)
    risk_free = _aligned_rate_vector(
        risk_free_returns,
        values.size,
        "risk-free returns",
    )
    annualization = _positive_scalar(periods_per_year, "periods per year")
    denominator = float(np.std(values, ddof=1) * math.sqrt(annualization))
    if denominator == 0.0:
        raise FinancialMetricError("Sharpe ratio is undefined for zero volatility")
    numerator = float(np.mean(values - risk_free) * annualization)
    return numerator / denominator


def sortino_ratio(
    returns: ArrayLike,
    target_returns: float | ArrayLike,
    periods_per_year: float,
) -> float:
    """Return annualized target-relative return divided by annualized downside deviation."""
    values = _as_vector(returns, "returns", minimum_count=1)
    targets = _aligned_rate_vector(target_returns, values.size, "target returns")
    annualization = _positive_scalar(periods_per_year, "periods per year")
    denominator = downside_deviation(values, targets, annualization)
    if denominator == 0.0:
        raise FinancialMetricError("Sortino ratio is undefined for zero downside deviation")
    return float(np.mean(values - targets) * annualization) / denominator


def calmar_ratio(annualized_return_value: float, maximum_drawdown_value: float) -> float:
    """Return annualized return divided by maximum drawdown magnitude."""
    annual = _finite_scalar(annualized_return_value, "annualized return")
    drawdown = _finite_scalar(maximum_drawdown_value, "maximum drawdown")
    if drawdown > 0.0 or drawdown < -1.0:
        raise FinancialMetricError("maximum drawdown must be in the interval [-1, 0]")
    if drawdown == 0.0:
        raise FinancialMetricError("Calmar ratio is undefined for zero drawdown")
    return annual / abs(drawdown)


def historical_var(
    returns: ArrayLike,
    confidence: float,
    *,
    method: QuantileMethod = "linear",
) -> float:
    """Return positive empirical loss magnitude at the declared lower-tail quantile."""
    values = _as_vector(returns, "returns", minimum_count=1)
    probability = _confidence(confidence)
    if method not in _QUANTILE_METHODS:
        raise FinancialMetricError("historical VaR quantile method is unsupported")
    quantile = float(np.quantile(values, 1.0 - probability, method=method))
    return max(0.0, -quantile)


def expected_shortfall(
    returns: ArrayLike,
    confidence: float,
    *,
    method: QuantileMethod = "linear",
) -> float:
    """Return positive mean loss at or beyond the inclusive historical-VaR threshold."""
    values = _as_vector(returns, "returns", minimum_count=1)
    value_at_risk = historical_var(values, confidence, method=method)
    tail = values[values <= -value_at_risk]
    if tail.size == 0:
        raise FinancialMetricError("expected shortfall tail is empty")
    return float(np.mean(-tail))


def parametric_var(mean_return: float, standard_deviation: float, confidence: float) -> float:
    """Return positive normal-distribution loss magnitude at the declared confidence."""
    mean = _finite_scalar(mean_return, "mean return")
    deviation = _finite_scalar(standard_deviation, "standard deviation")
    if deviation < 0.0:
        raise FinancialMetricError("standard deviation cannot be negative")
    probability = _confidence(confidence)
    lower_tail_z = NormalDist().inv_cdf(1.0 - probability)
    return max(0.0, -(mean + deviation * lower_tail_z))


def covariance(x: ArrayLike, y: ArrayLike) -> float:
    """Return sample covariance for complete aligned observations."""
    left, right = _aligned_vectors(x, y, minimum_count=2)
    centered_left = left - np.mean(left)
    centered_right = right - np.mean(right)
    return float(np.dot(centered_left, centered_right) / (left.size - 1))


def correlation(x: ArrayLike, y: ArrayLike) -> float:
    """Return Pearson correlation for complete aligned observations."""
    left, right = _aligned_vectors(x, y, minimum_count=2)
    left_deviation = float(np.std(left, ddof=1))
    right_deviation = float(np.std(right, ddof=1))
    denominator = left_deviation * right_deviation
    if denominator == 0.0:
        raise FinancialMetricError("correlation is undefined for zero variance")
    result = covariance(left, right) / denominator
    return min(1.0, max(-1.0, result))


def beta(subject_returns: ArrayLike, benchmark_returns: ArrayLike) -> float:
    """Return subject sensitivity to the aligned benchmark return series."""
    subject, benchmark = _aligned_vectors(
        subject_returns,
        benchmark_returns,
        minimum_count=2,
    )
    benchmark_variance = covariance(benchmark, benchmark)
    if benchmark_variance == 0.0:
        raise FinancialMetricError("beta is undefined for zero benchmark variance")
    return covariance(subject, benchmark) / benchmark_variance


def alpha(
    subject_returns: ArrayLike,
    benchmark_returns: ArrayLike,
    risk_free_returns: float | ArrayLike,
    periods_per_year: float,
) -> float:
    """Return annualized regression intercept of aligned excess returns."""
    subject, benchmark = _aligned_vectors(
        subject_returns,
        benchmark_returns,
        minimum_count=2,
    )
    risk_free = _aligned_rate_vector(
        risk_free_returns,
        subject.size,
        "risk-free returns",
    )
    annualization = _positive_scalar(periods_per_year, "periods per year")
    subject_excess = subject - risk_free
    benchmark_excess = benchmark - risk_free
    sensitivity = beta(subject_excess, benchmark_excess)
    return float(
        (np.mean(subject_excess) - sensitivity * np.mean(benchmark_excess)) * annualization,
    )


def _capture_ratio(
    subject_returns: ArrayLike,
    benchmark_returns: ArrayLike,
    *,
    positive: bool,
) -> float:
    subject, benchmark = _aligned_vectors(
        subject_returns,
        benchmark_returns,
        minimum_count=1,
    )
    mask = benchmark > 0.0 if positive else benchmark < 0.0
    if not np.any(mask):
        raise FinancialMetricError("capture ratio has no eligible benchmark periods")
    selected_subject = subject[mask]
    selected_benchmark = benchmark[mask]
    if np.any(selected_subject < -1.0) or np.any(selected_benchmark < -1.0):
        raise FinancialMetricError("capture ratio cannot compound a return below -100 percent")
    count = int(np.count_nonzero(mask))
    subject_geometric = math.pow(float(np.prod(1.0 + selected_subject)), 1.0 / count) - 1.0
    benchmark_geometric = math.pow(float(np.prod(1.0 + selected_benchmark)), 1.0 / count) - 1.0
    if benchmark_geometric == 0.0:
        raise FinancialMetricError("capture ratio benchmark return is zero")
    return subject_geometric / benchmark_geometric


def _as_vector(values: ArrayLike, label: str, *, minimum_count: int) -> FloatVector:
    try:
        vector = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise FinancialMetricError(f"{label} must be a numeric vector") from error
    if vector.ndim != 1 or vector.size < minimum_count:
        raise FinancialMetricError(
            f"{label} requires at least {minimum_count} complete observations",
        )
    if not np.all(np.isfinite(vector)):
        raise FinancialMetricError(f"{label} must contain only finite observations")
    return np.asarray(vector, dtype=np.float64)


def _aligned_vectors(
    left: ArrayLike,
    right: ArrayLike,
    *,
    minimum_count: int,
) -> tuple[FloatVector, FloatVector]:
    left_vector = _as_vector(left, "left series", minimum_count=minimum_count)
    right_vector = _as_vector(right, "right series", minimum_count=minimum_count)
    if left_vector.size != right_vector.size:
        raise FinancialMetricError("aligned series must have equal lengths")
    return left_vector, right_vector


def _aligned_rate_vector(
    value: float | ArrayLike,
    count: int,
    label: str,
) -> FloatVector:
    if np.isscalar(value):
        scalar = _finite_scalar(cast("float", value), label)
        return np.full(count, scalar, dtype=np.float64)
    vector = _as_vector(value, label, minimum_count=1)
    if vector.size != count:
        raise FinancialMetricError(f"{label} must align exactly with returns")
    return vector


def _finite_scalar(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise FinancialMetricError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise FinancialMetricError(f"{label} must be a finite number") from error
    if not math.isfinite(result):
        raise FinancialMetricError(f"{label} must be a finite number")
    return result


def _observation_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinancialMetricError("observation count must be an integer")
    return value


def _positive_scalar(value: float, label: str) -> float:
    result = _finite_scalar(value, label)
    if result <= 0.0:
        raise FinancialMetricError(f"{label} must be positive")
    return result


def _require_strictly_positive(values: FloatVector, label: str) -> None:
    if np.any(values <= 0.0):
        raise FinancialMetricError(f"{label} must be strictly positive")


def _validate_rate_inputs(flows: FloatVector, periods: FloatVector) -> None:
    if flows.size != periods.size:
        raise FinancialMetricError("cash flows and periods must have equal lengths")
    if not np.any(flows < 0.0) or not np.any(flows > 0.0):
        raise FinancialMetricError("money-weighted return requires both cash-flow signs")
    if periods[0] != 0.0 or periods[-1] <= 0.0:
        raise FinancialMetricError("cash-flow timing must start at zero and end later")
    if np.any(periods < 0.0) or np.any(np.diff(periods) < 0.0):
        raise FinancialMetricError("cash-flow periods must be nonnegative and ordered")


def _solve_discount_rate(flows: FloatVector, periods: FloatVector) -> float:
    lower_y = math.log(_MIN_ONE_PLUS_RATE)
    upper_y = math.log1p(_MAX_RATE)
    grid = np.unique(
        np.concatenate(
            (
                np.linspace(lower_y, upper_y, _ROOT_GRID_SIZE, dtype=np.float64),
                np.asarray([0.0], dtype=np.float64),
            ),
        ),
    )
    scale = max(1.0, float(np.sum(np.abs(flows))))
    tolerance = _ROOT_RELATIVE_TOLERANCE * scale
    values = np.asarray([_discounted_value(flows, periods, y) for y in grid])
    roots: list[float] = []
    for index, value in enumerate(values):
        if math.isfinite(float(value)) and abs(float(value)) <= tolerance:
            roots.append(float(grid[index]))
    for index in range(grid.size - 1):
        left_value = float(values[index])
        right_value = float(values[index + 1])
        if math.isnan(left_value) or math.isnan(right_value):
            continue
        same_sign = math.copysign(1.0, left_value) == math.copysign(
            1.0,
            right_value,
        )
        if left_value == 0.0 or right_value == 0.0 or same_sign:
            continue
        roots.append(
            _bisect_discounted_value(
                flows,
                periods,
                float(grid[index]),
                float(grid[index + 1]),
                tolerance,
            ),
        )
    unique_roots: list[float] = []
    for root in sorted(roots):
        if not unique_roots or not math.isclose(root, unique_roots[-1], abs_tol=1e-10):
            unique_roots.append(root)
    if not unique_roots:
        raise FinancialMetricError("money-weighted return has no bounded real solution")
    if len(unique_roots) != 1:
        raise FinancialMetricError("money-weighted return has multiple bounded real solutions")
    return math.expm1(unique_roots[0])


def _discounted_value(flows: FloatVector, periods: FloatVector, log_growth: float) -> float:
    with np.errstate(over="ignore", invalid="ignore"):
        discount_factors = np.exp(np.clip(-periods * log_growth, -700.0, 700.0))
        return float(np.dot(flows, discount_factors))


def _bisect_discounted_value(
    flows: FloatVector,
    periods: FloatVector,
    lower: float,
    upper: float,
    tolerance: float,
) -> float:
    lower_value = _discounted_value(flows, periods, lower)
    for _ in range(_ROOT_ITERATIONS):
        middle = (lower + upper) / 2.0
        middle_value = _discounted_value(flows, periods, middle)
        if abs(middle_value) <= tolerance or upper - lower <= _ROOT_INTERVAL_TOLERANCE:
            return middle
        if math.copysign(1.0, lower_value) == math.copysign(1.0, middle_value):
            lower = middle
            lower_value = middle_value
        else:
            upper = middle
    return (lower + upper) / 2.0


def _ordered_economic_dates(
    values: Sequence[date],
    *,
    expected_count: int,
) -> tuple[date, ...]:
    if len(values) != expected_count:
        raise FinancialMetricError("cash flows and economic dates must have equal lengths")
    dates = [_economic_date(value) for value in values]
    if any(right < left for left, right in pairwise(dates)):
        raise FinancialMetricError("economic dates must be ordered without implicit sorting")
    if dates[-1] <= dates[0]:
        raise FinancialMetricError("economic dates must span a positive day count")
    return tuple(dates)


def _economic_date(value: object) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise FinancialMetricError("economic datetimes must use UTC")
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    raise FinancialMetricError("economic date is invalid")


def _confidence(value: float) -> float:
    probability = _finite_scalar(value, "confidence")
    if not 0.0 < probability < 1.0:
        raise FinancialMetricError("confidence must lie strictly between zero and one")
    return probability


__all__ = (
    "FinancialMetricError",
    "FloatVector",
    "QuantileMethod",
    "active_return",
    "active_returns",
    "alpha",
    "annualized_return",
    "beta",
    "cagr",
    "calmar_ratio",
    "correlation",
    "covariance",
    "cumulative_return",
    "cumulative_returns",
    "downside_capture",
    "downside_deviation",
    "drawdown_series",
    "expected_shortfall",
    "historical_var",
    "log_returns",
    "maximum_drawdown",
    "money_weighted_return",
    "parametric_var",
    "sharpe_ratio",
    "simple_returns",
    "sortino_ratio",
    "time_weighted_return",
    "tracking_error",
    "upside_capture",
    "volatility",
    "xirr",
)
