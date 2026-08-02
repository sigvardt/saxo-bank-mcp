from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from itertools import pairwise
from statistics import NormalDist
from typing import Literal, cast

import numpy as np
from numpy.typing import ArrayLike, NDArray

type FloatVector = NDArray[np.float64]
type QuantileMethod = Literal["linear", "lower", "higher", "midpoint", "nearest"]

_ROOT_ITERATIONS = 200
_ROOT_RELATIVE_TOLERANCE = 1e-12
_ROOT_INTERVAL_TOLERANCE = 1e-13
_ROOT_DUPLICATE_TOLERANCE = 1e-11
_BOUNDARY_DECIMAL_PRECISION = 80
_BOUNDARY_ROOT_TOLERANCE = Decimal("1e-45")
_BOUNDARY_INTERIOR_STEP = Decimal("1e-12")
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
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        relatives = values[1:] / values[:-1]
        result = np.asarray(relatives - 1.0, dtype=np.float64)
    if np.any(relatives <= 0.0) or not np.all(np.isfinite(result)):
        raise FinancialMetricError("simple returns are not finite")
    return result


def log_returns(prices: ArrayLike) -> FloatVector:
    """Return natural-log price relatives for a complete positive price series."""
    values = _as_vector(prices, "prices", minimum_count=2)
    _require_strictly_positive(values, "prices")
    result = np.asarray(np.log(values[1:]) - np.log(values[:-1]), dtype=np.float64)
    return _finite_output_vector(result, "log returns")


def cumulative_returns(returns: ArrayLike) -> FloatVector:
    """Chain periodic simple returns without rounding intermediate growth."""
    values = _as_vector(returns, "returns", minimum_count=1)
    if np.any(values < -1.0):
        raise FinancialMetricError("returns cannot imply a loss greater than the investment")
    with np.errstate(over="ignore", invalid="ignore"):
        result = np.asarray(np.cumprod(1.0 + values) - 1.0, dtype=np.float64)
    return _finite_output_vector(result, "cumulative returns")


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
    if np.any(values[:-1] <= 0.0):
        raise FinancialMetricError("TWR requires strictly positive opening valuations")
    if values[-1] < 0.0:
        raise FinancialMetricError("TWR final valuation cannot be negative")
    with np.errstate(over="ignore", invalid="ignore"):
        adjusted_end_values = values[1:] - flows
    _finite_output_vector(adjusted_end_values, "TWR adjusted values")
    if np.any(adjusted_end_values < 0.0):
        raise FinancialMetricError("TWR adjusted subperiod value cannot be negative")
    with np.errstate(over="ignore", invalid="ignore"):
        subperiod_growth = adjusted_end_values / values[:-1]
        result = float(np.prod(subperiod_growth, dtype=np.float64) - 1.0)
    return _finite_output_scalar(result, "time-weighted return")


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
        result = math.expm1((math.log(end) - math.log(start)) / elapsed)
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
    with np.errstate(over="ignore", invalid="ignore"):
        result = np.asarray(subject - benchmark, dtype=np.float64)
    return _finite_output_vector(result, "active returns")


def active_return(subject_return: float, benchmark_return: float) -> float:
    """Return aggregate arithmetic subject return minus benchmark return."""
    result = _finite_scalar(subject_return, "subject return") - _finite_scalar(
        benchmark_return, "benchmark return"
    )
    return _finite_output_scalar(result, "active return")


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
    return _rescale_product(
        _scaled_sample_deviation(active),
        (math.sqrt(annualization),),
        "tracking error",
    )


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
    return _rescale_product(
        _scaled_sample_deviation(values),
        (math.sqrt(annualization),),
        "volatility",
    )


def downside_deviation(
    returns: ArrayLike,
    target_returns: float | ArrayLike,
    periods_per_year: float,
) -> float:
    """Return annualized root mean square of all target-relative negative deviations."""
    values = _as_vector(returns, "returns", minimum_count=1)
    targets = _aligned_rate_vector(target_returns, values.size, "target returns")
    annualization = _positive_scalar(periods_per_year, "periods per year")
    scale = max(float(np.max(np.abs(values))), float(np.max(np.abs(targets))))
    if scale == 0.0:
        return 0.0
    normalized_downside = np.minimum(values / scale - targets / scale, 0.0)
    normalized_rms = math.sqrt(float(np.mean(np.square(normalized_downside))))
    return _rescale_product(
        normalized_rms * math.sqrt(annualization),
        (scale,),
        "downside deviation",
    )


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
    result = np.asarray(series / running_peaks - 1.0, dtype=np.float64)
    return _finite_output_vector(result, "drawdowns")


def maximum_drawdown(values: ArrayLike) -> float:
    """Return the most negative point in the complete drawdown series."""
    return _finite_output_scalar(float(np.min(drawdown_series(values))), "maximum drawdown")


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
    denominator = _rescale_product(
        _scaled_sample_deviation(values),
        (math.sqrt(annualization),),
        "Sharpe denominator",
    )
    if denominator == 0.0:
        raise FinancialMetricError("Sharpe ratio is undefined for zero volatility")
    numerator = _scaled_mean_difference(
        values,
        risk_free,
        annualization,
        "Sharpe numerator",
    )
    return _finite_output_scalar(numerator / denominator, "Sharpe ratio")


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
    numerator = _scaled_mean_difference(
        values,
        targets,
        annualization,
        "Sortino numerator",
    )
    return _finite_output_scalar(numerator / denominator, "Sortino ratio")


def calmar_ratio(annualized_return_value: float, maximum_drawdown_value: float) -> float:
    """Return annualized return divided by maximum drawdown magnitude."""
    annual = _finite_scalar(annualized_return_value, "annualized return")
    drawdown = _finite_scalar(maximum_drawdown_value, "maximum drawdown")
    if drawdown > 0.0 or drawdown < -1.0:
        raise FinancialMetricError("maximum drawdown must be in the interval [-1, 0]")
    if drawdown == 0.0:
        raise FinancialMetricError("Calmar ratio is undefined for zero drawdown")
    return _finite_output_scalar(annual / abs(drawdown), "Calmar ratio")


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
    scale = float(np.max(np.abs(values)))
    if scale == 0.0:
        return 0.0
    quantile = float(np.quantile(values / scale, 1.0 - probability, method=method))
    normalized_loss = max(0.0, -quantile)
    return _rescale_product(normalized_loss, (scale,), "historical VaR")


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
    scale = float(np.max(np.abs(tail)))
    if scale == 0.0:
        return 0.0
    normalized_mean_loss = float(np.mean(-(tail / scale)))
    return _rescale_product(normalized_mean_loss, (scale,), "expected shortfall")


def parametric_var(mean_return: float, standard_deviation: float, confidence: float) -> float:
    """Return positive normal-distribution loss magnitude at the declared confidence."""
    mean = _finite_scalar(mean_return, "mean return")
    deviation = _finite_scalar(standard_deviation, "standard deviation")
    if deviation < 0.0:
        raise FinancialMetricError("standard deviation cannot be negative")
    probability = _confidence(confidence)
    lower_tail_z = NormalDist().inv_cdf(1.0 - probability)
    scale = max(abs(mean), abs(deviation))
    if scale == 0.0:
        return 0.0
    normalized_loss = max(
        0.0,
        -(mean / scale + deviation / scale * lower_tail_z),
    )
    return _rescale_product(normalized_loss, (scale,), "parametric VaR")


def covariance(x: ArrayLike, y: ArrayLike) -> float:
    """Return sample covariance for complete aligned observations."""
    left, right = _aligned_vectors(x, y, minimum_count=2)
    centered_left, left_scale = _scaled_centered(left)
    centered_right, right_scale = _scaled_centered(right)
    normalized = float(np.dot(centered_left, centered_right) / (left.size - 1))
    return _rescale_product(normalized, (left_scale, right_scale), "covariance")


def correlation(x: ArrayLike, y: ArrayLike) -> float:
    """Return Pearson correlation for complete aligned observations."""
    left, right = _aligned_vectors(x, y, minimum_count=2)
    centered_left, _ = _scaled_centered(left)
    centered_right, _ = _scaled_centered(right)
    left_sum_of_squares = float(np.dot(centered_left, centered_left))
    right_sum_of_squares = float(np.dot(centered_right, centered_right))
    if left_sum_of_squares == 0.0 or right_sum_of_squares == 0.0:
        raise FinancialMetricError("correlation is undefined for zero variance")
    denominator = math.sqrt(left_sum_of_squares) * math.sqrt(right_sum_of_squares)
    result = float(np.dot(centered_left, centered_right)) / denominator
    result = _finite_output_scalar(result, "correlation")
    return min(1.0, max(-1.0, result))


def beta(subject_returns: ArrayLike, benchmark_returns: ArrayLike) -> float:
    """Return subject sensitivity to the aligned benchmark return series."""
    subject, benchmark = _aligned_vectors(
        subject_returns,
        benchmark_returns,
        minimum_count=2,
    )
    centered_subject, subject_scale = _scaled_centered(subject)
    centered_benchmark, benchmark_scale = _scaled_centered(benchmark)
    normalized_variance = float(np.dot(centered_benchmark, centered_benchmark))
    if normalized_variance == 0.0 or benchmark_scale == 0.0:
        raise FinancialMetricError("beta is undefined for zero benchmark variance")
    normalized_covariance = float(np.dot(centered_subject, centered_benchmark))
    scale_ratio = subject_scale / benchmark_scale
    result = normalized_covariance / normalized_variance * scale_ratio
    return _finite_output_scalar(result, "beta")


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
    with np.errstate(over="ignore", invalid="ignore"):
        subject_excess = np.asarray(subject - risk_free, dtype=np.float64)
        benchmark_excess = np.asarray(benchmark - risk_free, dtype=np.float64)
    _finite_output_vector(subject_excess, "subject excess returns")
    _finite_output_vector(benchmark_excess, "benchmark excess returns")
    sensitivity = beta(subject_excess, benchmark_excess)
    result = float(
        (np.mean(subject_excess) - sensitivity * np.mean(benchmark_excess)) * annualization,
    )
    return _finite_output_scalar(result, "alpha")


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
    subject_geometric = _geometric_mean_return(selected_subject, "subject capture return")
    benchmark_geometric = _geometric_mean_return(
        selected_benchmark,
        "benchmark capture return",
    )
    if benchmark_geometric == 0.0:
        raise FinancialMetricError("capture ratio benchmark return is zero")
    return _finite_output_scalar(
        subject_geometric / benchmark_geometric,
        "capture ratio",
    )


def _geometric_mean_return(values: FloatVector, label: str) -> float:
    if np.any(values == -1.0):
        return -1.0
    mean_log_growth = float(np.mean(np.log1p(values)))
    try:
        result = math.expm1(mean_log_growth)
    except OverflowError as error:
        raise FinancialMetricError(f"{label} is not finite") from error
    return _finite_output_scalar(result, label)


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


def _finite_output_vector(values: FloatVector, label: str) -> FloatVector:
    if not np.all(np.isfinite(values)):
        raise FinancialMetricError(f"{label} are not finite")
    return values


def _finite_output_scalar(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise FinancialMetricError(f"{label} is not finite")
    return value


def _scaled_centered(values: FloatVector) -> tuple[FloatVector, float]:
    scale = float(np.max(np.abs(values)))
    if scale == 0.0:
        return np.zeros_like(values), 0.0
    normalized = values / scale
    centered = np.asarray(normalized - np.mean(normalized), dtype=np.float64)
    return _finite_output_vector(centered, "centered observations"), scale


def _scaled_sample_deviation(values: FloatVector) -> float:
    centered, scale = _scaled_centered(values)
    normalized_deviation = math.sqrt(float(np.dot(centered, centered)) / (values.size - 1))
    return _rescale_product(normalized_deviation, (scale,), "sample deviation")


def _scaled_mean_difference(
    left: FloatVector,
    right: FloatVector,
    multiplier: float,
    label: str,
) -> float:
    scale = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))))
    if scale == 0.0:
        return 0.0
    normalized_mean = float(np.mean(left / scale - right / scale))
    return _rescale_product(normalized_mean * multiplier, (scale,), label)


def _rescale_product(value: float, scales: tuple[float, ...], label: str) -> float:
    if value == 0.0 or any(scale == 0.0 for scale in scales):
        return 0.0
    mantissa, exponent = math.frexp(value)
    for scale in scales:
        scale_mantissa, scale_exponent = math.frexp(scale)
        mantissa *= scale_mantissa
        exponent += scale_exponent
        mantissa, adjustment = math.frexp(mantissa)
        exponent += adjustment
    try:
        result = math.ldexp(mantissa, exponent)
    except OverflowError as error:
        raise FinancialMetricError(f"{label} is not finite") from error
    if not math.isfinite(result) or result == 0.0:
        raise FinancialMetricError(f"{label} is not finite")
    return result


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
    coefficients, normalized_periods = _normalized_discount_terms(flows, periods)
    if coefficients.size == 0:
        raise FinancialMetricError("money-weighted return has multiple bounded real solutions")
    roots = _isolate_discount_roots(
        coefficients,
        normalized_periods,
        lower_y,
        upper_y,
    )
    roots = [
        root
        for root in roots
        if _discount_root_is_inside_declared_boundaries(
            flows,
            periods,
            root[0],
            lower_y,
            upper_y,
        )
    ]
    if not roots:
        raise FinancialMetricError("money-weighted return has no bounded real solution")
    if len(roots) != 1 or roots[0][1]:
        raise FinancialMetricError("money-weighted return has multiple bounded real solutions")
    return _finite_output_scalar(math.expm1(roots[0][0]), "money-weighted return")


def _discount_root_is_inside_declared_boundaries(
    flows: FloatVector,
    periods: FloatVector,
    root: float,
    lower: float,
    upper: float,
) -> bool:
    if root == lower:
        return _high_precision_boundary_root(flows, periods, lower=True)
    if root == upper:
        return _high_precision_boundary_root(flows, periods, lower=False)
    return True


def _high_precision_boundary_root(
    flows: FloatVector,
    periods: FloatVector,
    *,
    lower: bool,
) -> bool:
    with localcontext() as context:
        context.prec = _BOUNDARY_DECIMAL_PRECISION
        growth = Decimal("1e-12") if lower else Decimal(1_000_001)
        inside_growth = growth * (
            Decimal(1) + _BOUNDARY_INTERIOR_STEP if lower else Decimal(1) - _BOUNDARY_INTERIOR_STEP
        )
        boundary_value, boundary_magnitude = _decimal_discounted_value(
            flows,
            periods,
            growth,
        )
        inside_value, inside_magnitude = _decimal_discounted_value(
            flows,
            periods,
            inside_growth,
        )
    boundary_is_root = abs(boundary_value) <= _BOUNDARY_ROOT_TOLERANCE * boundary_magnitude
    inside_is_distinct = abs(inside_value) > _BOUNDARY_ROOT_TOLERANCE * inside_magnitude
    return boundary_is_root and inside_is_distinct


def _decimal_discounted_value(
    flows: FloatVector,
    periods: FloatVector,
    growth: Decimal,
) -> tuple[Decimal, Decimal]:
    total = Decimal(0)
    magnitude = Decimal(0)
    try:
        for flow, period in zip(flows, periods, strict=True):
            coefficient = Decimal(str(float(flow)))
            exponent = Decimal(str(float(period)))
            if exponent == exponent.to_integral_value():
                weight = growth ** -int(exponent)
            else:
                weight = (-exponent * growth.ln()).exp()
            term = coefficient * weight
            total += term
            magnitude += abs(term)
    except DecimalException as error:
        raise FinancialMetricError("cash-flow boundary cannot be evaluated") from error
    return total, magnitude


def _normalized_discount_terms(
    flows: FloatVector,
    periods: FloatVector,
) -> tuple[FloatVector, FloatVector]:
    unique_periods, inverse = np.unique(periods, return_inverse=True)
    combined_flows = np.zeros(unique_periods.size, dtype=np.float64)
    np.add.at(combined_flows, inverse, flows)
    nonzero = combined_flows != 0.0
    if not np.any(nonzero):
        return (
            np.asarray([], dtype=np.float64),
            np.asarray([], dtype=np.float64),
        )
    coefficients = combined_flows[nonzero]
    normalized_periods = unique_periods[nonzero]
    normalized_periods = normalized_periods - normalized_periods[0]
    coefficients = coefficients / float(np.max(np.abs(coefficients)))
    return (
        np.asarray(coefficients, dtype=np.float64),
        np.asarray(normalized_periods, dtype=np.float64),
    )


def _isolate_discount_roots(
    coefficients: FloatVector,
    periods: FloatVector,
    lower: float,
    upper: float,
) -> list[tuple[float, bool]]:
    if coefficients.size < _MIN_SAMPLE_COUNT or _sign_changes(coefficients) == 0:
        return []

    critical_roots: list[tuple[float, bool]] = []
    if _sign_changes(coefficients) > 1:
        derivative_coefficients = -periods[1:] * coefficients[1:]
        derivative_periods = periods[1:] - periods[1]
        derivative_coefficients = derivative_coefficients / float(
            np.max(np.abs(derivative_coefficients)),
        )
        critical_roots = _isolate_discount_roots(
            np.asarray(derivative_coefficients, dtype=np.float64),
            np.asarray(derivative_periods, dtype=np.float64),
            lower,
            upper,
        )

    critical_points = [root for root, _ in critical_roots]
    points = [lower, *critical_points, upper]
    roots: list[tuple[float, bool]] = []
    for boundary in (lower, upper):
        value, magnitude = _scaled_discounted_value(coefficients, periods, boundary)
        if _is_discount_root(value, magnitude):
            _append_discount_root(roots, boundary, repeated=False)
    for critical_point in critical_points:
        value, magnitude = _scaled_discounted_value(
            coefficients,
            periods,
            critical_point,
        )
        if _is_discount_root(value, magnitude):
            _append_discount_root(roots, critical_point, repeated=True)
    for left, right in pairwise(points):
        left_value, left_magnitude = _scaled_discounted_value(
            coefficients,
            periods,
            left,
        )
        right_value, right_magnitude = _scaled_discounted_value(
            coefficients,
            periods,
            right,
        )
        if _is_discount_root(left_value, left_magnitude) or _is_discount_root(
            right_value,
            right_magnitude,
        ):
            continue
        if math.copysign(1.0, left_value) == math.copysign(1.0, right_value):
            continue
        root = _bisect_discounted_value(
            coefficients,
            periods,
            left,
            right,
        )
        _append_discount_root(roots, root, repeated=False)
    return roots


def _sign_changes(coefficients: FloatVector) -> int:
    signs = np.signbit(coefficients)
    return int(np.count_nonzero(signs[1:] != signs[:-1]))


def _scaled_discounted_value(
    coefficients: FloatVector,
    periods: FloatVector,
    log_growth: float,
) -> tuple[float, float]:
    with np.errstate(over="ignore", invalid="ignore"):
        exponents = -periods * log_growth
    if not np.all(np.isfinite(exponents)):
        raise FinancialMetricError("cash-flow periods are too large to evaluate")
    shifted_weights = np.exp(exponents - float(np.max(exponents)))
    terms = coefficients * shifted_weights
    value = math.fsum(float(term) for term in terms)
    magnitude = math.fsum(abs(float(term)) for term in terms)
    return value, magnitude


def _is_discount_root(value: float, magnitude: float) -> bool:
    return abs(value) <= _ROOT_RELATIVE_TOLERANCE * magnitude


def _append_discount_root(
    roots: list[tuple[float, bool]],
    candidate: float,
    *,
    repeated: bool,
) -> None:
    for index, (existing, existing_repeated) in enumerate(roots):
        if math.isclose(
            candidate,
            existing,
            rel_tol=_ROOT_DUPLICATE_TOLERANCE,
            abs_tol=_ROOT_DUPLICATE_TOLERANCE,
        ):
            roots[index] = (existing, existing_repeated or repeated)
            return
    roots.append((candidate, repeated))
    roots.sort(key=lambda item: item[0])


def _bisect_discounted_value(
    coefficients: FloatVector,
    periods: FloatVector,
    lower: float,
    upper: float,
) -> float:
    lower_value, _ = _scaled_discounted_value(coefficients, periods, lower)
    for _ in range(_ROOT_ITERATIONS):
        middle = (lower + upper) / 2.0
        middle_value, middle_magnitude = _scaled_discounted_value(
            coefficients,
            periods,
            middle,
        )
        if _is_discount_root(middle_value, middle_magnitude) or upper - lower <= (
            _ROOT_INTERVAL_TOLERANCE * max(1.0, abs(middle))
        ):
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
