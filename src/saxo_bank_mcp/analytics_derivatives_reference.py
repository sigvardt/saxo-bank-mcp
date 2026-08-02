from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from scipy.optimize import (  # pyright: ignore[reportMissingTypeStubs]
    brentq,  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]
)
from scipy.special import ndtr  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

type ReferenceOptionType = Literal["call", "put"]
type ReferencePricingModel = Literal["black_scholes", "black_76"]


class _BrentRoot(Protocol):
    def __call__(  # noqa: PLR0913
        self,
        function: Callable[[float], float],
        lower: float,
        upper: float,
        *,
        xtol: float,
        rtol: float,
        maxiter: int,
    ) -> float: ...


_brent_root = cast("_BrentRoot", brentq)


class ReferenceDerivativeError(ValueError):
    """Raised when the independent bounded derivative reference is undefined."""


@dataclass(frozen=True)
class ReferenceOptionValues:
    """Independent-library value and frozen-unit Greek vector."""

    value: float
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_volatility_point: float
    rho_per_rate_point: float


def _cdf(value: float) -> float:
    return float(ndtr(value))


def _pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _validate_common(
    reference_price: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    days_per_year: float,
) -> None:
    values = (
        reference_price,
        strike,
        time_to_expiry_years,
        volatility,
        days_per_year,
    )
    if any(not math.isfinite(value) for value in values):
        raise ReferenceDerivativeError("reference option inputs must be finite")
    if (
        reference_price <= 0.0
        or strike <= 0.0
        or time_to_expiry_years <= 0.0
        or volatility <= 0.0
        or days_per_year <= 0.0
    ):
        raise ReferenceDerivativeError("reference option inputs are outside the supported domain")


def reference_black_scholes(  # noqa: PLR0913
    *,
    option_type: ReferenceOptionType,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float,
    dividend_yield: float,
    days_per_year: float,
) -> ReferenceOptionValues:
    """Evaluate Black-Scholes with SciPy's independent normal CDF."""
    _validate_common(spot, strike, time_to_expiry_years, volatility, days_per_year)
    if not math.isfinite(risk_free_rate) or not math.isfinite(dividend_yield):
        raise ReferenceDerivativeError("reference rates must be finite")
    time = time_to_expiry_years
    root_time = math.sqrt(time)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility * volatility) * time
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    rate_discount = math.exp(-risk_free_rate * time)
    dividend_discount = math.exp(-dividend_yield * time)
    density = _pdf(d1)
    if option_type == "call":
        value = spot * dividend_discount * _cdf(d1) - strike * rate_discount * _cdf(d2)
        delta = dividend_discount * _cdf(d1)
        theta_year = (
            -spot * dividend_discount * density * volatility / (2.0 * root_time)
            - risk_free_rate * strike * rate_discount * _cdf(d2)
            + dividend_yield * spot * dividend_discount * _cdf(d1)
        )
        rho = strike * time * rate_discount * _cdf(d2) / 100.0
    else:
        value = strike * rate_discount * _cdf(-d2) - spot * dividend_discount * _cdf(-d1)
        delta = dividend_discount * (_cdf(d1) - 1.0)
        theta_year = (
            -spot * dividend_discount * density * volatility / (2.0 * root_time)
            + risk_free_rate * strike * rate_discount * _cdf(-d2)
            - dividend_yield * spot * dividend_discount * _cdf(-d1)
        )
        rho = -strike * time * rate_discount * _cdf(-d2) / 100.0
    result = ReferenceOptionValues(
        value=value,
        delta=delta,
        gamma=dividend_discount * density / (spot * volatility * root_time),
        theta_per_day=theta_year / days_per_year,
        vega_per_volatility_point=(
            spot * dividend_discount * density * root_time / 100.0
        ),
        rho_per_rate_point=rho,
    )
    if any(not math.isfinite(item) for item in result.__dict__.values()):
        raise ReferenceDerivativeError("reference Black-Scholes output is not finite")
    return result


def reference_black_76(  # noqa: PLR0913
    *,
    option_type: ReferenceOptionType,
    futures_price: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    risk_free_rate: float,
    days_per_year: float,
) -> ReferenceOptionValues:
    """Evaluate Black-76 with SciPy's independent normal CDF."""
    _validate_common(
        futures_price,
        strike,
        time_to_expiry_years,
        volatility,
        days_per_year,
    )
    if not math.isfinite(risk_free_rate):
        raise ReferenceDerivativeError("reference rate must be finite")
    time = time_to_expiry_years
    root_time = math.sqrt(time)
    d1 = (
        math.log(futures_price / strike) + 0.5 * volatility * volatility * time
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    discount = math.exp(-risk_free_rate * time)
    density = _pdf(d1)
    if option_type == "call":
        value = discount * (futures_price * _cdf(d1) - strike * _cdf(d2))
        delta = discount * _cdf(d1)
    else:
        value = discount * (strike * _cdf(-d2) - futures_price * _cdf(-d1))
        delta = discount * (_cdf(d1) - 1.0)
    result = ReferenceOptionValues(
        value=value,
        delta=delta,
        gamma=discount * density / (futures_price * volatility * root_time),
        theta_per_day=(
            risk_free_rate * value
            - discount * futures_price * density * volatility / (2.0 * root_time)
        )
        / days_per_year,
        vega_per_volatility_point=(
            discount * futures_price * density * root_time / 100.0
        ),
        rho_per_rate_point=-time * value / 100.0,
    )
    if any(not math.isfinite(item) for item in result.__dict__.values()):
        raise ReferenceDerivativeError("reference Black-76 output is not finite")
    return result


def reference_implied_volatility(  # noqa: PLR0913
    *,
    pricing_model: ReferencePricingModel,
    option_type: ReferenceOptionType,
    reference_price: float,
    strike: float,
    time_to_expiry_years: float,
    observed_price: float,
    risk_free_rate: float,
    dividend_yield: float | None,
    sigma_min: float,
    sigma_max: float,
) -> float:
    """Invert a supported price independently with SciPy's Brent solver."""
    if (
        not math.isfinite(observed_price)
        or observed_price < 0.0
        or sigma_min <= 0.0
        or sigma_max <= sigma_min
    ):
        raise ReferenceDerivativeError("reference IV inputs are invalid")

    def residual(volatility: float) -> float:
        if pricing_model == "black_scholes":
            if dividend_yield is None:
                raise ReferenceDerivativeError("Black-Scholes dividend yield is absent")
            value = reference_black_scholes(
                option_type=option_type,
                spot=reference_price,
                strike=strike,
                time_to_expiry_years=time_to_expiry_years,
                volatility=volatility,
                risk_free_rate=risk_free_rate,
                dividend_yield=dividend_yield,
                days_per_year=365.0,
            ).value
        else:
            if dividend_yield is not None:
                raise ReferenceDerivativeError("Black-76 accepts no dividend yield")
            value = reference_black_76(
                option_type=option_type,
                futures_price=reference_price,
                strike=strike,
                time_to_expiry_years=time_to_expiry_years,
                volatility=volatility,
                risk_free_rate=risk_free_rate,
                days_per_year=365.0,
            ).value
        return value - observed_price

    try:
        result = _brent_root(
            residual,
            sigma_min,
            sigma_max,
            xtol=1e-14,
            rtol=1e-14,
            maxiter=1_000,
        )
    except (ArithmeticError, RuntimeError, ValueError) as exc:
        raise ReferenceDerivativeError("reference IV root is undefined") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise ReferenceDerivativeError("reference IV root is invalid")
    return result


__all__ = (
    "ReferenceDerivativeError",
    "ReferenceOptionValues",
    "reference_black_76",
    "reference_black_scholes",
    "reference_implied_volatility",
)
