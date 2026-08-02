from __future__ import annotations

import math
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import ResearchRefusal
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    SafeAccountScope,
)

type OptionType = Literal["call", "put"]
type PricingModel = Literal["black_scholes", "black_76"]
type ReferenceKind = Literal["spot", "forward_or_futures"]

_MODEL_OUTPUT: Final = "model_output"
_SQRT_TWO: Final = math.sqrt(2.0)
_SQRT_TWO_PI: Final = math.sqrt(2.0 * math.pi)
_NUMERIC_ZERO_TOLERANCE: Final = 1e-12
_MAXIMUM_STRATEGY_LEGS: Final = 16


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class OptionContract(_StrictModel):
    """One explicit supported-or-refusable vanilla option model contract."""

    dataset_id: DatasetId
    option_handle: InstrumentHandle
    underlying_handle: InstrumentHandle
    contract_currency: IsoCurrencyCode
    pricing_model: PricingModel
    reference_kind: ReferenceKind
    option_type: OptionType
    exercise_style: Literal["european", "american"]
    payoff_style: Literal["vanilla", "path_dependent"]
    rate_model: Literal["constant", "complex"]
    reference_price: float = Field(allow_inf_nan=False)
    strike: float = Field(allow_inf_nan=False)
    time_to_expiry_years: float = Field(allow_inf_nan=False)
    risk_free_rate: float = Field(allow_inf_nan=False)
    dividend_yield: float | None = Field(default=None, allow_inf_nan=False)
    days_per_year: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_handles(self) -> Self:
        if self.option_handle == self.underlying_handle:
            raise ValueError("option and underlying handles must differ")
        return self


class OptionModelInput(_StrictModel):
    """One complete persisted volatility input for a declared option contract."""

    contract: OptionContract
    volatility: float = Field(allow_inf_nan=False)


class OptionGreeks(_StrictModel):
    """Frozen Greek units: theta per day, vega/rho per one percentage point."""

    delta: float = Field(allow_inf_nan=False)
    gamma: float = Field(allow_inf_nan=False)
    theta_per_day: float = Field(allow_inf_nan=False)
    vega_per_volatility_point: float = Field(allow_inf_nan=False)
    rho_per_rate_point: float = Field(allow_inf_nan=False)


class OptionModelValues(_StrictModel):
    """Bounded European model value, never a broker fact or forecast."""

    pricing_model: PricingModel
    reference_kind: ReferenceKind
    option_type: OptionType
    value: float = Field(ge=0, allow_inf_nan=False)
    greeks: OptionGreeks | None
    warnings: tuple[ContractName, ...]
    metric_class: Literal["model_output"] = _MODEL_OUTPUT
    is_not_forecast: Literal[True] = True
    recommendation_authority: Literal[False] = False
    order_creation_authority: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False


class ImpliedVolatilityRequest(_StrictModel):
    """Explicit bracket and convergence policy for the frozen bisection definition."""

    contract: OptionContract
    observed_price: float = Field(allow_inf_nan=False)
    sigma_min: float = Field(allow_inf_nan=False)
    sigma_max: float = Field(allow_inf_nan=False)
    price_tolerance: float = Field(allow_inf_nan=False)
    sigma_tolerance: float = Field(allow_inf_nan=False)
    maximum_iterations: int = Field(ge=1, le=10_000)


class ImpliedVolatilityResult(_StrictModel):
    """Root that reproduces the declared observed price under one supported model."""

    volatility: float = Field(gt=0, allow_inf_nan=False)
    iterations: int = Field(ge=0)
    price_residual: float = Field(ge=0, allow_inf_nan=False)
    metric_class: Literal["model_output"] = _MODEL_OUTPUT
    is_not_forecast: Literal[True] = True


class OptionStrategyLeg(_StrictModel):
    """One signed option leg; negative quantity reverses value and every Greek."""

    model_input: OptionModelInput
    quantity: float = Field(allow_inf_nan=False)
    contract_multiplier: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_quantity(self) -> Self:
        if self.quantity == 0.0:
            raise ValueError("option strategy leg quantity must be non-zero")
        return self


class OptionStrategyRequest(_StrictModel):
    """One bounded same-underlying strategy with explicit premium and eligible cost."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    strategy_id: ContractName
    legs: tuple[OptionStrategyLeg, ...] = Field(
        min_length=1,
        max_length=_MAXIMUM_STRATEGY_LEGS,
    )
    net_premium: float = Field(allow_inf_nan=False)
    eligible_costs: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_common_strategy_basis(self) -> Self:
        first = self.legs[0].model_input.contract
        comparable = (
            first.underlying_handle,
            first.contract_currency,
            first.pricing_model,
            first.reference_kind,
            first.reference_price,
            first.time_to_expiry_years,
            first.risk_free_rate,
            first.dividend_yield,
            first.days_per_year,
        )
        handles: list[str] = []
        for leg in self.legs:
            contract = leg.model_input.contract
            if contract.dataset_id != self.dataset_id:
                raise ValueError("option strategy legs must match the strategy dataset")
            if (
                contract.underlying_handle,
                contract.contract_currency,
                contract.pricing_model,
                contract.reference_kind,
                contract.reference_price,
                contract.time_to_expiry_years,
                contract.risk_free_rate,
                contract.dividend_yield,
                contract.days_per_year,
            ) != comparable:
                raise ValueError("option strategy legs must share one exact model basis")
            handles.append(contract.option_handle)
        if len(handles) != len(set(handles)):
            raise ValueError("option strategy leg handles must be unique")
        return self


class StrategyPayoffPoint(_StrictModel):
    """Exact leg sum at one declared expiry reference price."""

    expiry_reference_price: float = Field(gt=0, allow_inf_nan=False)
    leg_payoffs: tuple[float, ...]
    total_payoff: float = Field(allow_inf_nan=False)


class StrategyGreeks(OptionGreeks):
    """Quantity and contract-multiplier weighted strategy Greeks."""


class OptionGreekComparison(_StrictModel):
    """Side-by-side Greek comparison that never blends model and broker values."""

    state: Literal["within_tolerance", "disagreement"]
    local: OptionGreeks
    saxo_reported: OptionGreeks
    differences: OptionGreeks
    absolute_tolerance: float = Field(ge=0, allow_inf_nan=False)
    relative_tolerance: float = Field(ge=0, allow_inf_nan=False)
    averaged: Literal[False] = False


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / _SQRT_TWO))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / _SQRT_TWO_PI


def _contract_refusal(
    contract: OptionContract,
    *,
    analysis_kind: str,
    reason_code: str,
    reason: str,
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind=analysis_kind,
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(contract.dataset_id,),
        instrument_handles=(contract.option_handle, contract.underlying_handle),
        source_scope=None,
    )


def _strategy_refusal(
    request: OptionStrategyRequest,
    *,
    reason_code: str,
    reason: str,
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="option_payoff",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(request.dataset_id,),
        instrument_handles=tuple(
            leg.model_input.contract.option_handle for leg in request.legs
        ),
        source_scope=None,
    )


def _capability_refusal(  # noqa: PLR0911
    contract: OptionContract,
    *,
    analysis_kind: str,
) -> ResearchRefusal | None:
    if contract.exercise_style == "american":
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_american_unsupported",
            reason="American exercise is outside the bounded European option capability",
        )
    if contract.payoff_style == "path_dependent":
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_path_dependency_unsupported",
            reason="path-dependent option payoffs are outside the bounded vanilla capability",
        )
    if contract.rate_model == "complex":
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_complex_rates_unsupported",
            reason="complex rate models are outside the constant-rate option capability",
        )
    expected_reference = (
        "spot" if contract.pricing_model == "black_scholes" else "forward_or_futures"
    )
    if contract.reference_kind != expected_reference:
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_model_branch_invalid",
            reason="the pricing model and declared reference branch do not match",
        )
    if contract.pricing_model == "black_scholes" and contract.dividend_yield is None:
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_dividend_yield_required",
            reason="Black-Scholes requires an explicit dividend or carry yield",
        )
    if contract.pricing_model == "black_76" and contract.dividend_yield is not None:
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_model_branch_invalid",
            reason=(
                "Black-76 holds the futures or forward reference fixed and accepts no "
                "dividend yield"
            ),
        )
    if contract.reference_price <= 0.0 or contract.strike <= 0.0:
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_price_domain_invalid",
            reason="option reference price and strike must be positive",
        )
    if contract.time_to_expiry_years < 0.0:
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_expiry_invalid",
            reason="option time to expiry must not be negative",
        )
    if contract.days_per_year <= 0.0:
        return _contract_refusal(
            contract,
            analysis_kind=analysis_kind,
            reason_code="option_day_count_invalid",
            reason="option days per year must be positive",
        )
    return None


def model_option(model_input: OptionModelInput) -> OptionModelValues | ResearchRefusal:
    """Evaluate the frozen European Black-Scholes or Black-76 branch."""
    contract = model_input.contract
    refusal = _capability_refusal(contract, analysis_kind="derivatives_model")
    if refusal is not None:
        return refusal
    if contract.time_to_expiry_years == 0.0:
        intrinsic = (
            max(contract.reference_price - contract.strike, 0.0)
            if contract.option_type == "call"
            else max(contract.strike - contract.reference_price, 0.0)
        )
        return OptionModelValues(
            pricing_model=contract.pricing_model,
            reference_kind=contract.reference_kind,
            option_type=contract.option_type,
            value=intrinsic,
            greeks=None,
            warnings=("option_greeks_unavailable_at_expiry",),
        )
    if model_input.volatility <= 0.0:
        return _contract_refusal(
            contract,
            analysis_kind="derivatives_model",
            reason_code="option_volatility_nonpositive",
            reason="positive volatility is required before expiry",
        )
    try:
        if contract.pricing_model == "black_scholes":
            value, greeks = _black_scholes(model_input)
        else:
            value, greeks = _black_76(model_input)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        return _contract_refusal(
            contract,
            analysis_kind="derivatives_model",
            reason_code="option_model_undefined",
            reason="the supplied finite inputs do not define a stable supported option value",
        )
    if value < 0.0 and abs(value) <= _NUMERIC_ZERO_TOLERANCE:
        value = 0.0
    if value < 0.0 or not math.isfinite(value) or not _greeks_are_finite(greeks):
        return _contract_refusal(
            contract,
            analysis_kind="derivatives_model",
            reason_code="option_model_undefined",
            reason="the supplied finite inputs do not define a stable supported option value",
        )
    return OptionModelValues(
        pricing_model=contract.pricing_model,
        reference_kind=contract.reference_kind,
        option_type=contract.option_type,
        value=value,
        greeks=greeks,
        warnings=(),
    )


def _black_scholes(model_input: OptionModelInput) -> tuple[float, OptionGreeks]:
    contract = model_input.contract
    volatility = model_input.volatility
    time = contract.time_to_expiry_years
    root_time = math.sqrt(time)
    dividend_yield = contract.dividend_yield
    if dividend_yield is None:
        raise ValueError("Black-Scholes dividend yield is absent")
    d1 = (
        math.log(contract.reference_price / contract.strike)
        + (
            contract.risk_free_rate
            - dividend_yield
            + 0.5 * volatility * volatility
        )
        * time
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    rate_discount = math.exp(-contract.risk_free_rate * time)
    dividend_discount = math.exp(-dividend_yield * time)
    density = _normal_pdf(d1)
    if contract.option_type == "call":
        value = (
            contract.reference_price * dividend_discount * _normal_cdf(d1)
            - contract.strike * rate_discount * _normal_cdf(d2)
        )
        delta = dividend_discount * _normal_cdf(d1)
        theta_year = (
            -contract.reference_price
            * dividend_discount
            * density
            * volatility
            / (2.0 * root_time)
            - contract.risk_free_rate
            * contract.strike
            * rate_discount
            * _normal_cdf(d2)
            + dividend_yield
            * contract.reference_price
            * dividend_discount
            * _normal_cdf(d1)
        )
        rho = (
            contract.strike
            * time
            * rate_discount
            * _normal_cdf(d2)
            / 100.0
        )
    else:
        value = (
            contract.strike * rate_discount * _normal_cdf(-d2)
            - contract.reference_price * dividend_discount * _normal_cdf(-d1)
        )
        delta = dividend_discount * (_normal_cdf(d1) - 1.0)
        theta_year = (
            -contract.reference_price
            * dividend_discount
            * density
            * volatility
            / (2.0 * root_time)
            + contract.risk_free_rate
            * contract.strike
            * rate_discount
            * _normal_cdf(-d2)
            - dividend_yield
            * contract.reference_price
            * dividend_discount
            * _normal_cdf(-d1)
        )
        rho = (
            -contract.strike
            * time
            * rate_discount
            * _normal_cdf(-d2)
            / 100.0
        )
    gamma = (
        dividend_discount
        * density
        / (contract.reference_price * volatility * root_time)
    )
    vega = (
        contract.reference_price
        * dividend_discount
        * density
        * root_time
        / 100.0
    )
    return value, OptionGreeks(
        delta=delta,
        gamma=gamma,
        theta_per_day=theta_year / contract.days_per_year,
        vega_per_volatility_point=vega,
        rho_per_rate_point=rho,
    )


def _black_76(model_input: OptionModelInput) -> tuple[float, OptionGreeks]:
    contract = model_input.contract
    volatility = model_input.volatility
    time = contract.time_to_expiry_years
    root_time = math.sqrt(time)
    d1 = (
        math.log(contract.reference_price / contract.strike)
        + 0.5 * volatility * volatility * time
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    discount = math.exp(-contract.risk_free_rate * time)
    density = _normal_pdf(d1)
    if contract.option_type == "call":
        value = discount * (
            contract.reference_price * _normal_cdf(d1)
            - contract.strike * _normal_cdf(d2)
        )
        delta = discount * _normal_cdf(d1)
    else:
        value = discount * (
            contract.strike * _normal_cdf(-d2)
            - contract.reference_price * _normal_cdf(-d1)
        )
        delta = discount * (_normal_cdf(d1) - 1.0)
    gamma = discount * density / (
        contract.reference_price * volatility * root_time
    )
    theta_year = (
        contract.risk_free_rate * value
        - discount
        * contract.reference_price
        * density
        * volatility
        / (2.0 * root_time)
    )
    vega = discount * contract.reference_price * density * root_time / 100.0
    rho = -time * value / 100.0
    return value, OptionGreeks(
        delta=delta,
        gamma=gamma,
        theta_per_day=theta_year / contract.days_per_year,
        vega_per_volatility_point=vega,
        rho_per_rate_point=rho,
    )


def _greeks_are_finite(greeks: OptionGreeks) -> bool:
    return all(
        math.isfinite(value)
        for value in (
            greeks.delta,
            greeks.gamma,
            greeks.theta_per_day,
            greeks.vega_per_volatility_point,
            greeks.rho_per_rate_point,
        )
    )


def solve_implied_volatility(  # noqa: C901, PLR0911, PLR0912
    request: ImpliedVolatilityRequest,
) -> ImpliedVolatilityResult | ResearchRefusal:
    """Invert one supported option price with the frozen deterministic bisection rule."""
    contract = request.contract
    refusal = _capability_refusal(contract, analysis_kind="derivatives_model")
    if refusal is not None:
        return refusal
    if contract.time_to_expiry_years <= 0.0:
        return _contract_refusal(
            contract,
            analysis_kind="derivatives_model",
            reason_code="implied_volatility_expiry_invalid",
            reason="implied volatility requires positive time to expiry",
        )
    if (
        request.observed_price < 0.0
        or request.sigma_min <= 0.0
        or request.sigma_max <= request.sigma_min
        or request.price_tolerance <= 0.0
        or request.sigma_tolerance <= 0.0
    ):
        return _contract_refusal(
            contract,
            analysis_kind="derivatives_model",
            reason_code="implied_volatility_input_invalid",
            reason="implied-volatility price, bracket, and tolerances are invalid",
        )

    def residual(volatility: float) -> float | ResearchRefusal:
        modeled = model_option(OptionModelInput(contract=contract, volatility=volatility))
        if isinstance(modeled, ResearchRefusal):
            return modeled
        return modeled.value - request.observed_price

    lower = request.sigma_min
    upper = request.sigma_max
    lower_residual = residual(lower)
    upper_residual = residual(upper)
    if isinstance(lower_residual, ResearchRefusal):
        return lower_residual
    if isinstance(upper_residual, ResearchRefusal):
        return upper_residual
    if lower_residual == 0.0:
        return ImpliedVolatilityResult(
            volatility=lower,
            iterations=0,
            price_residual=0.0,
        )
    if upper_residual == 0.0:
        return ImpliedVolatilityResult(
            volatility=upper,
            iterations=0,
            price_residual=0.0,
        )
    if lower_residual * upper_residual > 0.0:
        return _contract_refusal(
            contract,
            analysis_kind="derivatives_model",
            reason_code="implied_volatility_unbracketed",
            reason="the declared volatility interval does not bracket the observed option price",
        )
    for iteration in range(1, request.maximum_iterations + 1):
        midpoint = (lower + upper) / 2.0
        midpoint_residual = residual(midpoint)
        if isinstance(midpoint_residual, ResearchRefusal):
            return midpoint_residual
        if (
            abs(midpoint_residual) <= request.price_tolerance
            or upper - lower <= request.sigma_tolerance
        ):
            return ImpliedVolatilityResult(
                volatility=midpoint,
                iterations=iteration,
                price_residual=abs(midpoint_residual),
            )
        if lower_residual * midpoint_residual <= 0.0:
            upper = midpoint
        else:
            lower = midpoint
            lower_residual = midpoint_residual
    return _contract_refusal(
        contract,
        analysis_kind="derivatives_model",
        reason_code="implied_volatility_not_converged",
        reason="implied-volatility bisection did not converge within the explicit limit",
    )


def strategy_payoff(
    request: OptionStrategyRequest,
    *,
    expiry_reference_price: float,
) -> StrategyPayoffPoint | ResearchRefusal:
    """Return the exact signed leg sum, premium, and eligible-cost expiry payoff."""
    if not math.isfinite(expiry_reference_price) or expiry_reference_price <= 0.0:
        return _strategy_refusal(
            request,
            reason_code="option_expiry_reference_invalid",
            reason="expiry reference price must be positive and finite",
        )
    leg_payoffs: list[float] = []
    for leg in request.legs:
        contract = leg.model_input.contract
        refusal = _capability_refusal(contract, analysis_kind="option_payoff")
        if refusal is not None:
            return _strategy_refusal(
                request,
                reason_code=refusal.reason_code,
                reason=refusal.reason,
            )
        intrinsic = (
            max(expiry_reference_price - contract.strike, 0.0)
            if contract.option_type == "call"
            else max(contract.strike - expiry_reference_price, 0.0)
        )
        leg_payoffs.append(leg.quantity * leg.contract_multiplier * intrinsic)
    total = math.fsum(leg_payoffs) - request.net_premium - request.eligible_costs
    if not math.isfinite(total) or any(not math.isfinite(value) for value in leg_payoffs):
        return _strategy_refusal(
            request,
            reason_code="option_payoff_undefined",
            reason="the supplied finite inputs do not define a stable strategy payoff",
        )
    return StrategyPayoffPoint(
        expiry_reference_price=expiry_reference_price,
        leg_payoffs=tuple(leg_payoffs),
        total_payoff=total,
    )


def aggregate_strategy_greeks(
    request: OptionStrategyRequest,
) -> StrategyGreeks | ResearchRefusal:
    """Aggregate each analytic Greek with the exact signed leg weight."""
    weighted: list[tuple[float, OptionGreeks]] = []
    for leg in request.legs:
        modeled = model_option(leg.model_input)
        if isinstance(modeled, ResearchRefusal):
            return _strategy_refusal(
                request,
                reason_code=modeled.reason_code,
                reason=modeled.reason,
            )
        if modeled.greeks is None:
            return _strategy_refusal(
                request,
                reason_code="option_greeks_undefined",
                reason="strategy Greeks are undefined at expiry",
            )
        weighted.append((leg.quantity * leg.contract_multiplier, modeled.greeks))
    values = StrategyGreeks(
        delta=math.fsum(weight * greek.delta for weight, greek in weighted),
        gamma=math.fsum(weight * greek.gamma for weight, greek in weighted),
        theta_per_day=math.fsum(
            weight * greek.theta_per_day for weight, greek in weighted
        ),
        vega_per_volatility_point=math.fsum(
            weight * greek.vega_per_volatility_point for weight, greek in weighted
        ),
        rho_per_rate_point=math.fsum(
            weight * greek.rho_per_rate_point for weight, greek in weighted
        ),
    )
    if not _greeks_are_finite(values):
        return _strategy_refusal(
            request,
            reason_code="option_greeks_undefined",
            reason="the supplied finite inputs do not define stable strategy Greeks",
        )
    return values


def compare_greek_vectors(
    local: OptionGreeks,
    saxo_reported: OptionGreeks,
    *,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> OptionGreekComparison:
    """Compare two vectors component-by-component without averaging disagreements."""
    if (
        not math.isfinite(absolute_tolerance)
        or not math.isfinite(relative_tolerance)
        or absolute_tolerance < 0.0
        or relative_tolerance < 0.0
    ):
        raise ValueError("Greek comparison tolerances must be finite and non-negative")
    differences = OptionGreeks(
        delta=local.delta - saxo_reported.delta,
        gamma=local.gamma - saxo_reported.gamma,
        theta_per_day=local.theta_per_day - saxo_reported.theta_per_day,
        vega_per_volatility_point=(
            local.vega_per_volatility_point
            - saxo_reported.vega_per_volatility_point
        ),
        rho_per_rate_point=local.rho_per_rate_point - saxo_reported.rho_per_rate_point,
    )
    pairs = (
        (local.delta, saxo_reported.delta),
        (local.gamma, saxo_reported.gamma),
        (local.theta_per_day, saxo_reported.theta_per_day),
        (local.vega_per_volatility_point, saxo_reported.vega_per_volatility_point),
        (local.rho_per_rate_point, saxo_reported.rho_per_rate_point),
    )
    within_tolerance = all(
        math.isclose(
            local_value,
            broker_value,
            rel_tol=relative_tolerance,
            abs_tol=absolute_tolerance,
        )
        for local_value, broker_value in pairs
    )
    return OptionGreekComparison(
        state="within_tolerance" if within_tolerance else "disagreement",
        local=local,
        saxo_reported=saxo_reported,
        differences=differences,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
    )


__all__ = (
    "ImpliedVolatilityRequest",
    "ImpliedVolatilityResult",
    "OptionContract",
    "OptionGreekComparison",
    "OptionGreeks",
    "OptionModelInput",
    "OptionModelValues",
    "OptionStrategyLeg",
    "OptionStrategyRequest",
    "StrategyGreeks",
    "StrategyPayoffPoint",
    "aggregate_strategy_greeks",
    "compare_greek_vectors",
    "model_option",
    "solve_implied_volatility",
    "strategy_payoff",
)
