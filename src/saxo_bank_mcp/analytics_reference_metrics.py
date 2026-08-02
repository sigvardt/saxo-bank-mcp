from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import NormalDist
from typing import Literal, Protocol

type QuantileMethod = Literal["linear", "lower", "higher", "midpoint", "nearest"]

_REFERENCE_ROOT_GRID_SIZE = 4_097
_REFERENCE_ROOT_ITERATIONS = 200
_REFERENCE_ROOT_TOLERANCE = 1e-12
_REFERENCE_ROOT_INTERVAL_TOLERANCE = 1e-14
_REFERENCE_MIN_SAMPLE_COUNT = 2


class _CashFlowLike(Protocol):
    @property
    def economic_at(self) -> datetime: ...

    @property
    def kind(self) -> str: ...

    @property
    def amount(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...


class _FxQuoteLike(Protocol):
    @property
    def base_currency(self) -> str: ...

    @property
    def quote_currency(self) -> str: ...

    @property
    def rate(self) -> Decimal: ...

    @property
    def observed_at(self) -> datetime: ...


def reference_simple_returns(prices: Iterable[float]) -> tuple[float, ...]:
    """Compute simple returns with a deliberately scalar reference loop."""
    values = _reference_vector(prices, "prices", minimum_count=2)
    if any(value <= 0.0 for value in values):
        raise ValueError("prices must be positive")
    return tuple(values[index] / values[index - 1] - 1.0 for index in range(1, len(values)))


def reference_log_returns(prices: Iterable[float]) -> tuple[float, ...]:
    """Compute logarithmic returns without using the production vector path."""
    values = _reference_vector(prices, "prices", minimum_count=2)
    if any(value <= 0.0 for value in values):
        raise ValueError("prices must be positive")
    return tuple(math.log(values[index] / values[index - 1]) for index in range(1, len(values)))


def reference_cumulative_returns(returns: Iterable[float]) -> tuple[float, ...]:
    """Compute chained growth one observation at a time."""
    values = _reference_vector(returns, "returns", minimum_count=1)
    growth = 1.0
    result: list[float] = []
    for value in values:
        if value < -1.0:
            raise ValueError("return is below minus one")
        growth *= 1.0 + value
        result.append(growth - 1.0)
    return tuple(result)


def reference_cumulative_return(returns: Iterable[float]) -> float:
    """Compute the final chained simple return."""
    values = reference_cumulative_returns(returns)
    return values[-1]


def reference_annualized_return(
    total_return: float,
    observation_count: int,
    periods_per_year: float,
) -> float:
    """Annualize a total return from the declared observation count."""
    total = _reference_scalar(total_return, "total return")
    count = _reference_observation_count(observation_count)
    if count <= 0:
        raise ValueError("observation count must be positive")
    annualization = _reference_positive(periods_per_year, "periods per year")
    if 1.0 + total <= 0.0:
        raise ValueError("growth must be positive")
    try:
        result = (1.0 + total) ** (annualization / count) - 1.0
    except OverflowError as error:
        raise ValueError("annualized return is not finite") from error
    if not math.isfinite(result):
        raise ValueError("annualized return is not finite")
    return result


def reference_time_weighted_return(
    valuations: Iterable[float],
    external_portfolio_flows: Iterable[float],
) -> float:
    """Compute TWR with end-boundary flow removal in a scalar loop."""
    values = _reference_vector(valuations, "valuations", minimum_count=2)
    flows = _reference_vector(
        external_portfolio_flows,
        "external portfolio flows",
        minimum_count=1,
    )
    if len(flows) != len(values) - 1:
        raise ValueError("TWR inputs are not aligned")
    if any(value <= 0.0 for value in values):
        raise ValueError("valuations must be positive")
    growth = 1.0
    for index, flow in enumerate(flows):
        adjusted_end = values[index + 1] - flow
        if adjusted_end < 0.0:
            raise ValueError("adjusted value cannot be negative")
        growth *= adjusted_end / values[index]
    return growth - 1.0


def reference_money_weighted_return(
    investor_cash_flows: Iterable[float],
    periods: Iterable[float] | None = None,
) -> float:
    """Solve periodic investor-signed discounted cash flows by scalar bracketing."""
    flows = _reference_vector(
        investor_cash_flows,
        "investor cash flows",
        minimum_count=2,
    )
    times = (
        tuple(float(index) for index in range(len(flows)))
        if periods is None
        else _reference_vector(periods, "cash-flow periods", minimum_count=2)
    )
    _reference_validate_rate_inputs(flows, times)
    return _reference_solve_discount_rate(flows, times)


def reference_xirr(
    investor_cash_flows: Iterable[float],
    economic_dates: Sequence[date],
) -> float:
    """Solve an ACT/365 annual return from ordered economic dates."""
    flows = _reference_vector(
        investor_cash_flows,
        "investor cash flows",
        minimum_count=2,
    )
    dates = _reference_dates(economic_dates, len(flows))
    start = dates[0]
    periods = [(economic_date - start).days / 365.0 for economic_date in dates]
    _reference_validate_rate_inputs(flows, tuple(periods))
    return _reference_solve_discount_rate(flows, tuple(periods))


def reference_cagr(start_value: float, end_value: float, years: float) -> float:
    """Compute compound annual growth from positive endpoints."""
    start = _reference_positive(start_value, "starting value")
    end = _reference_positive(end_value, "ending value")
    elapsed = _reference_positive(years, "elapsed years")
    return (end / start) ** (1.0 / elapsed) - 1.0


def reference_active_returns(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
) -> tuple[float, ...]:
    """Subtract aligned benchmark observations in a scalar loop."""
    subject, benchmark = _reference_aligned(
        subject_returns,
        benchmark_returns,
        minimum_count=1,
    )
    return tuple(left - right for left, right in zip(subject, benchmark, strict=True))


def reference_active_return(subject_return: float, benchmark_return: float) -> float:
    """Subtract an aggregate benchmark return from an aggregate subject return."""
    return _reference_scalar(subject_return, "subject return") - _reference_scalar(
        benchmark_return,
        "benchmark return",
    )


def reference_tracking_error(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    periods_per_year: float,
) -> float:
    """Compute annualized sample deviation of scalar active returns."""
    active = reference_active_returns(subject_returns, benchmark_returns)
    if len(active) < _REFERENCE_MIN_SAMPLE_COUNT:
        raise ValueError("tracking error requires two observations")
    return _reference_sample_deviation(active) * math.sqrt(
        _reference_positive(periods_per_year, "periods per year"),
    )


def reference_upside_capture(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
) -> float:
    """Compute geometric capture over positive benchmark periods."""
    return _reference_capture(subject_returns, benchmark_returns, positive=True)


def reference_downside_capture(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
) -> float:
    """Compute geometric capture over negative benchmark periods."""
    return _reference_capture(subject_returns, benchmark_returns, positive=False)


def reference_volatility(returns: Iterable[float], periods_per_year: float) -> float:
    """Compute annualized sample volatility through scalar deviations."""
    values = _reference_vector(returns, "returns", minimum_count=2)
    return _reference_sample_deviation(values) * math.sqrt(
        _reference_positive(periods_per_year, "periods per year"),
    )


def reference_downside_deviation(
    returns: Iterable[float],
    target_returns: float | Iterable[float],
    periods_per_year: float,
) -> float:
    """Compute annualized target-relative downside root mean square."""
    values = _reference_vector(returns, "returns", minimum_count=1)
    targets = _reference_rates(target_returns, len(values), "target returns")
    squared_sum = 0.0
    for value, target in zip(values, targets, strict=True):
        difference = value - target
        downside = min(0.0, difference)
        squared_sum += downside * downside
    annualization = _reference_positive(periods_per_year, "periods per year")
    return math.sqrt(squared_sum / len(values)) * math.sqrt(annualization)


def reference_drawdown_series(values: Iterable[float]) -> tuple[float, ...]:
    """Compute decline from the running peak without vector accumulators."""
    series = _reference_vector(values, "cumulative values", minimum_count=1)
    if series[0] <= 0.0 or any(value < 0.0 for value in series):
        raise ValueError("drawdown values are invalid")
    peak = series[0]
    result: list[float] = []
    for value in series:
        peak = max(peak, value)
        result.append(value / peak - 1.0)
    return tuple(result)


def reference_maximum_drawdown(values: Iterable[float]) -> float:
    """Return the lowest scalar reference drawdown."""
    return min(reference_drawdown_series(values))


def reference_sharpe_ratio(
    returns: Iterable[float],
    risk_free_returns: float | Iterable[float],
    periods_per_year: float,
) -> float:
    """Compute the annualized excess-return Sharpe ratio."""
    values = _reference_vector(returns, "returns", minimum_count=2)
    risk_free = _reference_rates(risk_free_returns, len(values), "risk-free returns")
    annualization = _reference_positive(periods_per_year, "periods per year")
    denominator = _reference_sample_deviation(values) * math.sqrt(annualization)
    if denominator == 0.0:
        raise ValueError("Sharpe denominator is zero")
    excess_sum = 0.0
    for value, rate in zip(values, risk_free, strict=True):
        excess_sum += value - rate
    return (excess_sum / len(values) * annualization) / denominator


def reference_sortino_ratio(
    returns: Iterable[float],
    target_returns: float | Iterable[float],
    periods_per_year: float,
) -> float:
    """Compute target-relative annual return over downside deviation."""
    values = _reference_vector(returns, "returns", minimum_count=1)
    targets = _reference_rates(target_returns, len(values), "target returns")
    annualization = _reference_positive(periods_per_year, "periods per year")
    denominator = reference_downside_deviation(values, targets, annualization)
    if denominator == 0.0:
        raise ValueError("Sortino denominator is zero")
    difference_sum = 0.0
    for value, target in zip(values, targets, strict=True):
        difference_sum += value - target
    return (difference_sum / len(values) * annualization) / denominator


def reference_calmar_ratio(
    annualized_return_value: float,
    maximum_drawdown_value: float,
) -> float:
    """Compute annual return over maximum drawdown magnitude."""
    annual = _reference_scalar(annualized_return_value, "annualized return")
    drawdown = _reference_scalar(maximum_drawdown_value, "maximum drawdown")
    if drawdown > 0.0 or drawdown < -1.0 or drawdown == 0.0:
        raise ValueError("maximum drawdown is invalid")
    return annual / abs(drawdown)


def reference_historical_var(
    returns: Iterable[float],
    confidence: float,
    *,
    method: QuantileMethod = "linear",
) -> float:
    """Compute empirical lower-tail VaR with a hand-written quantile."""
    values = sorted(_reference_vector(returns, "returns", minimum_count=1))
    probability = _reference_confidence(confidence)
    quantile = _reference_quantile(values, 1.0 - probability, method)
    return max(0.0, -quantile)


def reference_expected_shortfall(
    returns: Iterable[float],
    confidence: float,
    *,
    method: QuantileMethod = "linear",
) -> float:
    """Compute the inclusive mean loss beyond scalar historical VaR."""
    values = _reference_vector(returns, "returns", minimum_count=1)
    threshold = reference_historical_var(values, confidence, method=method)
    tail = [-value for value in values if value <= -threshold]
    if not tail:
        raise ValueError("expected-shortfall tail is empty")
    return sum(tail) / len(tail)


def reference_parametric_var(
    mean_return: float,
    standard_deviation: float,
    confidence: float,
) -> float:
    """Compute normal VaR with the standard-library inverse CDF."""
    mean = _reference_scalar(mean_return, "mean return")
    deviation = _reference_scalar(standard_deviation, "standard deviation")
    if deviation < 0.0:
        raise ValueError("standard deviation cannot be negative")
    probability = _reference_confidence(confidence)
    lower_tail_z = NormalDist().inv_cdf(1.0 - probability)
    return max(0.0, -(mean + deviation * lower_tail_z))


def reference_covariance(x: Iterable[float], y: Iterable[float]) -> float:
    """Compute sample covariance by paired scalar deviations."""
    left, right = _reference_aligned(x, y, minimum_count=2)
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    total = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        total += (left_value - left_mean) * (right_value - right_mean)
    return total / (len(left) - 1)


def reference_correlation(x: Iterable[float], y: Iterable[float]) -> float:
    """Compute Pearson correlation from independent scalar moments."""
    left, right = _reference_aligned(x, y, minimum_count=2)
    denominator = _reference_sample_deviation(left) * _reference_sample_deviation(right)
    if denominator == 0.0:
        raise ValueError("correlation denominator is zero")
    result = reference_covariance(left, right) / denominator
    return min(1.0, max(-1.0, result))


def reference_beta(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
) -> float:
    """Compute benchmark sensitivity from scalar sample covariance."""
    subject, benchmark = _reference_aligned(
        subject_returns,
        benchmark_returns,
        minimum_count=2,
    )
    denominator = reference_covariance(benchmark, benchmark)
    if denominator == 0.0:
        raise ValueError("benchmark variance is zero")
    return reference_covariance(subject, benchmark) / denominator


def reference_alpha(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    risk_free_returns: float | Iterable[float],
    periods_per_year: float,
) -> float:
    """Compute annualized excess-return regression intercept."""
    subject, benchmark = _reference_aligned(
        subject_returns,
        benchmark_returns,
        minimum_count=2,
    )
    risk_free = _reference_rates(risk_free_returns, len(subject), "risk-free returns")
    subject_excess: list[float] = []
    benchmark_excess: list[float] = []
    for subject_value, benchmark_value, rate in zip(
        subject,
        benchmark,
        risk_free,
        strict=True,
    ):
        subject_excess.append(subject_value - rate)
        benchmark_excess.append(benchmark_value - rate)
    sensitivity = reference_beta(subject_excess, benchmark_excess)
    intercept = sum(subject_excess) / len(subject_excess) - sensitivity * (
        sum(benchmark_excess) / len(benchmark_excess)
    )
    return intercept * _reference_positive(periods_per_year, "periods per year")


def reference_fx_convert(  # noqa: PLR0913
    amount: Decimal,
    source_currency: str,
    target_currency: str,
    *,
    quote_base: str,
    quote_currency: str,
    quote_rate: Decimal,
    minor_unit: Decimal | None = None,
) -> Decimal:
    """Convert Decimal money with an explicit independent quote direction."""
    _reference_decimal(amount, "amount")
    _reference_decimal(quote_rate, "quote rate")
    if quote_rate <= 0:
        raise ValueError("quote rate must be positive")
    if source_currency == target_currency:
        converted = amount
    elif source_currency == quote_base and target_currency == quote_currency:
        converted = amount * quote_rate
    elif source_currency == quote_currency and target_currency == quote_base:
        converted = amount / quote_rate
    else:
        raise ValueError("quote direction does not match")
    return _reference_round(converted, minor_unit)


def reference_normalize_cash_flows(
    events: Sequence[_CashFlowLike],
    *,
    reporting_currency: str,
    quotes: Sequence[_FxQuoteLike],
    minor_unit: Decimal | None = None,
) -> tuple[tuple[str, Decimal, Decimal, Decimal], ...]:
    """Normalize cash-flow signs and FX in an independent stable scalar loop."""
    indexed_events = list(enumerate(events))
    indexed_events.sort(key=lambda item: (item[1].economic_at, item[0]))
    result: list[tuple[str, Decimal, Decimal, Decimal]] = []
    for _, event in indexed_events:
        magnitude = _reference_convert_at(
            event.amount,
            event.currency,
            reporting_currency,
            event.economic_at,
            quotes,
            minor_unit,
        )
        if event.kind == "deposit":
            portfolio_amount = magnitude
            external = magnitude
        elif event.kind == "withdrawal":
            portfolio_amount = -magnitude
            external = -magnitude
        elif event.kind in {"fee", "financing", "tax"}:
            portfolio_amount = -magnitude
            external = Decimal(0) * magnitude
        elif event.kind == "income":
            portfolio_amount = magnitude
            external = Decimal(0) * magnitude
        else:
            raise ValueError("cash-flow kind is unsupported")
        result.append((event.kind, portfolio_amount, external, -external))
    return tuple(result)


def _reference_vector(
    values: Iterable[float],
    label: str,
    *,
    minimum_count: int,
) -> tuple[float, ...]:
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if len(result) < minimum_count or any(not math.isfinite(value) for value in result):
        raise ValueError(f"{label} is incomplete")
    return result


def _reference_aligned(
    left: Iterable[float],
    right: Iterable[float],
    *,
    minimum_count: int,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    left_values = _reference_vector(left, "left series", minimum_count=minimum_count)
    right_values = _reference_vector(right, "right series", minimum_count=minimum_count)
    if len(left_values) != len(right_values):
        raise ValueError("series lengths differ")
    return left_values, right_values


def _reference_scalar(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} is invalid")
    return result


def _reference_positive(value: float, label: str) -> float:
    result = _reference_scalar(value, label)
    if result <= 0.0:
        raise ValueError(f"{label} must be positive")
    return result


def _reference_observation_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("observation count must be an integer")
    return value


def _reference_rates(
    value: float | Iterable[float],
    count: int,
    label: str,
) -> tuple[float, ...]:
    if isinstance(value, bool):
        raise TypeError(f"{label} is invalid")
    if isinstance(value, int | float):
        scalar = _reference_scalar(value, label)
        return tuple(scalar for _ in range(count))
    vector = _reference_vector(value, label, minimum_count=1)
    if len(vector) != count:
        raise ValueError(f"{label} is not aligned")
    return vector


def _reference_sample_deviation(values: tuple[float, ...]) -> float:
    mean = sum(values) / len(values)
    squared_sum = 0.0
    for value in values:
        squared_sum += (value - mean) ** 2
    return math.sqrt(squared_sum / (len(values) - 1))


def _reference_capture(
    subject_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    *,
    positive: bool,
) -> float:
    subject, benchmark = _reference_aligned(
        subject_returns,
        benchmark_returns,
        minimum_count=1,
    )
    subject_growth = 1.0
    benchmark_growth = 1.0
    count = 0
    for subject_value, benchmark_value in zip(subject, benchmark, strict=True):
        selected = benchmark_value > 0.0 if positive else benchmark_value < 0.0
        if selected:
            if subject_value < -1.0 or benchmark_value < -1.0:
                raise ValueError("capture return is below minus one")
            subject_growth *= 1.0 + subject_value
            benchmark_growth *= 1.0 + benchmark_value
            count += 1
    if count == 0:
        raise ValueError("no capture observations")
    subject_geometric = subject_growth ** (1.0 / count) - 1.0
    benchmark_geometric = benchmark_growth ** (1.0 / count) - 1.0
    if benchmark_geometric == 0.0:
        raise ValueError("capture denominator is zero")
    return subject_geometric / benchmark_geometric


def _reference_validate_rate_inputs(
    flows: tuple[float, ...],
    periods: tuple[float, ...],
) -> None:
    if len(flows) != len(periods):
        raise ValueError("cash-flow inputs are not aligned")
    if not any(value < 0.0 for value in flows) or not any(value > 0.0 for value in flows):
        raise ValueError("both cash-flow signs are required")
    if periods[0] != 0.0 or periods[-1] <= 0.0:
        raise ValueError("cash-flow timing endpoints are invalid")
    previous = periods[0]
    for current in periods:
        if current < 0.0 or current < previous:
            raise ValueError("cash-flow periods are not ordered")
        previous = current


def _reference_solve_discount_rate(
    flows: tuple[float, ...],
    periods: tuple[float, ...],
) -> float:
    lower_log_growth = math.log(1e-12)
    upper_log_growth = math.log1p(1_000_000.0)
    step = (upper_log_growth - lower_log_growth) / (_REFERENCE_ROOT_GRID_SIZE - 1)
    scale = max(1.0, sum(abs(value) for value in flows))
    tolerance = _REFERENCE_ROOT_TOLERANCE * scale
    roots: list[float] = []
    previous_log_growth = lower_log_growth
    previous_value = _reference_discounted_value(flows, periods, previous_log_growth)
    for index in range(_REFERENCE_ROOT_GRID_SIZE):
        current_log_growth = lower_log_growth + index * step
        if index == _REFERENCE_ROOT_GRID_SIZE - 1:
            current_log_growth = upper_log_growth
        current_value = _reference_discounted_value(flows, periods, current_log_growth)
        if abs(current_value) <= tolerance:
            roots.append(current_log_growth)
        if index > 0 and previous_value * current_value < 0.0:
            roots.append(
                _reference_bisect(
                    flows,
                    periods,
                    previous_log_growth,
                    current_log_growth,
                    tolerance,
                ),
            )
        previous_log_growth = current_log_growth
        previous_value = current_value
    unique_roots: list[float] = []
    for root in sorted(roots):
        if not unique_roots or not math.isclose(root, unique_roots[-1], abs_tol=1e-10):
            unique_roots.append(root)
    if len(unique_roots) != 1:
        raise ValueError("discounted cash flows do not have one bounded solution")
    return math.expm1(unique_roots[0])


def _reference_discounted_value(
    flows: tuple[float, ...],
    periods: tuple[float, ...],
    log_growth: float,
) -> float:
    total = 0.0
    for flow, period in zip(flows, periods, strict=True):
        exponent = min(700.0, max(-700.0, -period * log_growth))
        total += flow * math.exp(exponent)
    return total


def _reference_bisect(
    flows: tuple[float, ...],
    periods: tuple[float, ...],
    lower: float,
    upper: float,
    tolerance: float,
) -> float:
    lower_value = _reference_discounted_value(flows, periods, lower)
    for _ in range(_REFERENCE_ROOT_ITERATIONS):
        middle = (lower + upper) / 2.0
        middle_value = _reference_discounted_value(flows, periods, middle)
        if abs(middle_value) <= tolerance or upper - lower <= _REFERENCE_ROOT_INTERVAL_TOLERANCE:
            return middle
        if lower_value * middle_value > 0.0:
            lower = middle
            lower_value = middle_value
        else:
            upper = middle
    return (lower + upper) / 2.0


def _reference_dates(values: Sequence[date], expected_count: int) -> tuple[date, ...]:
    if len(values) != expected_count:
        raise ValueError("cash flows and dates are not aligned")
    result = [_reference_economic_date(value) for value in values]
    previous = result[0]
    for current in result:
        if current < previous:
            raise ValueError("economic dates are not ordered")
        previous = current
    if result[-1] <= result[0]:
        raise ValueError("economic dates do not span time")
    return tuple(result)


def _reference_economic_date(value: object) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("economic datetimes must use UTC")
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("economic date is invalid")


def _reference_confidence(value: float) -> float:
    probability = _reference_scalar(value, "confidence")
    if not 0.0 < probability < 1.0:
        raise ValueError("confidence is invalid")
    return probability


def _reference_quantile(
    values: list[float],
    probability: float,
    method: QuantileMethod,
) -> float:
    position = probability * (len(values) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if method == "lower":
        return values[lower_index]
    if method == "higher":
        return values[upper_index]
    if method == "nearest":
        return values[round(position)]
    if method == "midpoint":
        return (values[lower_index] + values[upper_index]) / 2.0
    if method != "linear":
        raise ValueError("quantile method is unsupported")
    weight = position - lower_index
    return values[lower_index] + weight * (values[upper_index] - values[lower_index])


def _reference_decimal(value: object, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")


def _reference_round(value: Decimal, minor_unit: Decimal | None) -> Decimal:
    if minor_unit is None:
        return value
    _reference_decimal(minor_unit, "minor unit")
    if minor_unit <= 0:
        raise ValueError("minor unit must be positive")
    units = (value / minor_unit).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return units * minor_unit


def _reference_convert_at(  # noqa: PLR0913
    amount: Decimal,
    source_currency: str,
    target_currency: str,
    economic_at: datetime,
    quotes: Sequence[_FxQuoteLike],
    minor_unit: Decimal | None,
) -> Decimal:
    if source_currency == target_currency:
        return _reference_round(amount, minor_unit)
    eligible: list[_FxQuoteLike] = []
    for quote in quotes:
        currencies_match = {
            quote.base_currency,
            quote.quote_currency,
        } == {source_currency, target_currency}
        if currencies_match and quote.observed_at <= economic_at:
            eligible.append(quote)
    if not eligible:
        raise ValueError("eligible reference FX quote is missing")
    latest_at = max(quote.observed_at for quote in eligible)
    latest = [quote for quote in eligible if quote.observed_at == latest_at]
    converted = [
        reference_fx_convert(
            amount,
            source_currency,
            target_currency,
            quote_base=quote.base_currency,
            quote_currency=quote.quote_currency,
            quote_rate=quote.rate,
            minor_unit=minor_unit,
        )
        for quote in latest
    ]
    if any(value != converted[0] for value in converted[1:]):
        raise ValueError("reference FX quote direction is ambiguous")
    return converted[0]


__all__ = (
    "reference_active_return",
    "reference_active_returns",
    "reference_alpha",
    "reference_annualized_return",
    "reference_beta",
    "reference_cagr",
    "reference_calmar_ratio",
    "reference_correlation",
    "reference_covariance",
    "reference_cumulative_return",
    "reference_cumulative_returns",
    "reference_downside_capture",
    "reference_downside_deviation",
    "reference_drawdown_series",
    "reference_expected_shortfall",
    "reference_fx_convert",
    "reference_historical_var",
    "reference_log_returns",
    "reference_maximum_drawdown",
    "reference_money_weighted_return",
    "reference_normalize_cash_flows",
    "reference_parametric_var",
    "reference_sharpe_ratio",
    "reference_simple_returns",
    "reference_sortino_ratio",
    "reference_time_weighted_return",
    "reference_tracking_error",
    "reference_upside_capture",
    "reference_volatility",
    "reference_xirr",
)
