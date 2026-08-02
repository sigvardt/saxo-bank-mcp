from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, DecimalException, localcontext

_REFERENCE_ASSET_COUNT = 2


class OptimizerReferenceError(ValueError):
    """Raised when a bounded independent reference case is undefined."""


def reference_two_asset_minimum_variance(
    covariance: Sequence[Sequence[Decimal]],
) -> tuple[Decimal, Decimal]:
    """Solve the fully invested two-asset minimum-variance case in closed form."""
    first_variance, covariance_value, second_variance = _two_asset_covariance(covariance)
    try:
        with localcontext() as context:
            context.prec = 50
            denominator = first_variance + second_variance - Decimal(2) * covariance_value
            if denominator <= 0:
                raise OptimizerReferenceError("reference minimum variance is undefined")
            first_weight = (second_variance - covariance_value) / denominator
            second_weight = Decimal(1) - first_weight
    except DecimalException as exc:
        raise OptimizerReferenceError("reference minimum variance is undefined") from exc
    if not first_weight.is_finite() or not second_weight.is_finite():
        raise OptimizerReferenceError("reference minimum variance is undefined")
    return first_weight, second_weight


def reference_two_asset_risk_parity(
    covariance: Sequence[Sequence[Decimal]],
) -> tuple[Decimal, Decimal]:
    """Solve positive two-asset equal-risk contribution weights independently."""
    first_variance, _, second_variance = _two_asset_covariance(covariance)
    if first_variance <= 0 or second_variance <= 0:
        raise OptimizerReferenceError("reference risk parity requires positive variances")
    try:
        with localcontext() as context:
            context.prec = 50
            first_volatility = first_variance.sqrt()
            second_volatility = second_variance.sqrt()
            denominator = first_volatility + second_volatility
            first_weight = second_volatility / denominator
            second_weight = Decimal(1) - first_weight
    except DecimalException as exc:
        raise OptimizerReferenceError("reference risk parity is undefined") from exc
    if not first_weight.is_finite() or not second_weight.is_finite():
        raise OptimizerReferenceError("reference risk parity is undefined")
    return first_weight, second_weight


def reference_portfolio_variance(
    covariance: Sequence[Sequence[Decimal]],
    weights: Sequence[Decimal],
) -> Decimal:
    """Evaluate a covariance quadratic form without production optimizer helpers."""
    size = len(weights)
    if size == 0 or len(covariance) != size or any(len(row) != size for row in covariance):
        raise OptimizerReferenceError("reference covariance and weights must have equal size")
    try:
        with localcontext() as context:
            context.prec = 50
            value = sum(
                (
                    weights[row]
                    * covariance[row][column]
                    * weights[column]
                    for row in range(size)
                    for column in range(size)
                ),
                Decimal(0),
            )
    except (DecimalException, IndexError) as exc:
        raise OptimizerReferenceError("reference variance is undefined") from exc
    if not value.is_finite():
        raise OptimizerReferenceError("reference variance is undefined")
    return value


def _two_asset_covariance(
    covariance: Sequence[Sequence[Decimal]],
) -> tuple[Decimal, Decimal, Decimal]:
    if len(covariance) != _REFERENCE_ASSET_COUNT or any(
        len(row) != _REFERENCE_ASSET_COUNT for row in covariance
    ):
        raise OptimizerReferenceError("reference formulation supports exactly two assets")
    first_variance = covariance[0][0]
    upper_covariance = covariance[0][1]
    lower_covariance = covariance[1][0]
    second_variance = covariance[1][1]
    if upper_covariance != lower_covariance:
        raise OptimizerReferenceError("reference covariance must be symmetric")
    if not all(
        value.is_finite()
        for value in (
            first_variance,
            upper_covariance,
            lower_covariance,
            second_variance,
        )
    ):
        raise OptimizerReferenceError("reference covariance must be finite")
    return first_variance, upper_covariance, second_variance


__all__ = (
    "OptimizerReferenceError",
    "reference_portfolio_variance",
    "reference_two_asset_minimum_variance",
    "reference_two_asset_risk_parity",
)
