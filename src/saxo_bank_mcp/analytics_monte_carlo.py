from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Sequence
from decimal import Decimal, DecimalException, localcontext
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    IsoCurrencyCode,
    QualityState,
    SafeAccountScope,
    UtcDateTime,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioPublicEvidence,
    SaxoSourceBinding,
    assess_source_bindings,
    build_public_evidence,
    require_delivery_boundary,
)

_SOURCE_SCOPE: Final = "saxo_openapi"
_CALIBRATION_SOURCE_CONTRACTS: Final = ("performance_timeseries_v4",)
_MINIMUM_CALIBRATION_OBSERVATIONS: Final = 30
_MAXIMUM_SIMULATION_STEPS: Final = 2_000_000
_BOOTSTRAP_ALGORITHM: Final = "seeded_non_circular_moving_block_bootstrap_v1"
_PROBABILITY_LABEL: Final = "seeded_block_bootstrap_model_distribution_not_prediction"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ReturnObservation(_StrictModel):
    """One complete, cutoff-bounded calibration return."""

    at: UtcDateTime
    return_ratio: Decimal = Field(ge=-1, allow_inf_nan=False)


class ReturnCalibrationDataset(_StrictModel):
    """Exact Saxo-bound return calibration series for one account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    reporting_currency: IsoCurrencyCode
    observations: tuple[ReturnObservation, ...] = Field(
        min_length=_MINIMUM_CALIBRATION_OBSERVATIONS,
    )
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_calibration(self) -> Self:
        timestamps = tuple(observation.at for observation in self.observations)
        if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
            raise ValueError("calibration observations must be unique and strictly ordered")
        if any(timestamp > self.as_of for timestamp in timestamps):
            raise ValueError("calibration observations cannot follow the model cutoff")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial calibration quality must match explicit missing fields")
        return self


class CashFlowPeriod(_StrictModel):
    """One explicit future contribution and withdrawal period."""

    period_index: int = Field(ge=1)
    contribution: Decimal = Field(ge=0, allow_inf_nan=False)
    withdrawal: Decimal = Field(ge=0, allow_inf_nan=False)


class BootstrapGoalRequest(_StrictModel):
    """Fully specified seeded bootstrap and sequence-of-returns goal model."""

    calibration: ReturnCalibrationDataset
    starting_value: Decimal = Field(gt=0, allow_inf_nan=False)
    explicit_goal: Decimal = Field(ge=0, allow_inf_nan=False)
    explicit_ruin_threshold: Decimal = Field(ge=0, allow_inf_nan=False)
    horizon_periods: int = Field(ge=1)
    horizon_years: Decimal = Field(gt=0, allow_inf_nan=False)
    path_count: int = Field(ge=1, le=100_000)
    block_length: int = Field(ge=1)
    random_seed: int = Field(ge=0, lt=2**64)
    cash_flow_timing: Literal["start_of_period"]
    cash_flows: tuple[CashFlowPeriod, ...] = Field(min_length=1)
    include_inflation_adjustment: bool
    annual_inflation_assumption: Decimal | None = Field(
        default=None,
        gt=-1,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_model_state(self) -> Self:
        expected_periods = tuple(range(1, self.horizon_periods + 1))
        if tuple(flow.period_index for flow in self.cash_flows) != expected_periods:
            raise ValueError("cash-flow schedule must be complete and strictly period ordered")
        if self.block_length > len(self.calibration.observations):
            raise ValueError("block length cannot exceed the calibration sample")
        if self.path_count * self.horizon_periods > _MAXIMUM_SIMULATION_STEPS:
            raise ValueError("requested bootstrap work exceeds the deterministic bound")
        if not self.include_inflation_adjustment and self.annual_inflation_assumption is not None:
            raise ValueError("unused inflation assumptions are not accepted")
        return self


class DistributionSummary(_StrictModel):
    """Five deterministic empirical order statistics from model paths."""

    minimum: Decimal = Field(ge=0, allow_inf_nan=False)
    lower: Decimal = Field(ge=0, allow_inf_nan=False)
    median: Decimal = Field(ge=0, allow_inf_nan=False)
    upper: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        ordered = (self.minimum, self.lower, self.median, self.upper, self.maximum)
        if ordered != tuple(sorted(ordered)):
            raise ValueError("distribution summary must be non-decreasing")
        return self


class PrivateBootstrapGoalValues(_StrictModel):
    """Owner-only model distribution values and exact probability identities."""

    reporting_currency: IsoCurrencyCode
    ending_values: DistributionSummary
    inflation_adjusted_ending_values: DistributionSummary | None
    goal_probability: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    ruin_probability: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    ordered_failure_probability: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    order_neutral_failure_probability: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    sequence_of_returns_risk: Decimal = Field(ge=-1, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_probabilities(self) -> Self:
        if self.ordered_failure_probability != Decimal(1) - self.goal_probability:
            raise ValueError("ordered goal failure must complement goal probability")
        if (
            self.sequence_of_returns_risk
            != self.ordered_failure_probability - self.order_neutral_failure_probability
        ):
            raise ValueError("sequence risk must equal ordered minus order-neutral failure")
        return self


class BootstrapGoalResult(_StrictModel):
    """Seeded model distribution, explicitly not a prediction or broker forecast."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["goal_model"] = "goal_model"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateBootstrapGoalValues | None
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence
    random_seed: int = Field(ge=0, lt=2**64)
    path_count: int = Field(ge=1)
    bootstrap_algorithm: Literal["seeded_non_circular_moving_block_bootstrap_v1"] = (
        _BOOTSTRAP_ALGORITHM
    )
    probability_label: Literal["seeded_block_bootstrap_model_distribution_not_prediction"] = (
        _PROBABILITY_LABEL
    )
    is_not_forecast: Literal[True] = True
    model_distribution_only: Literal[True] = True
    prediction_claim: Literal[False] = False
    does_not_verify: tuple[Literal["future_goal_attainment", "future_returns"], ...] = (
        "future_goal_attainment",
        "future_returns",
    )

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("goal-model values do not match their delivery visibility")
        return self


def run_bootstrap_goal_model(
    request: BootstrapGoalRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    cancellation_check: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> BootstrapGoalResult | ResearchRefusal:
    """Run deterministic block-bootstrap paths and an order-neutral sequence reference."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    source_assessment = assess_source_bindings(
        request.calibration.source_bindings,
        required_contract_ids=_CALIBRATION_SOURCE_CONTRACTS,
        analysis_kind="goal_model",
        dataset_id=request.calibration.dataset_id,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if request.calibration.quality_state is not QualityState.COMPLETE or source_assessment:
        return _refusal(
            request,
            "model_calibration_incomplete",
            "model probabilities require complete calibration coverage and entitlement",
        )
    if request.include_inflation_adjustment and request.annual_inflation_assumption is None:
        return _refusal(
            request,
            "inflation_assumption_required",
            "inflation-adjusted outcomes require an explicit numeric inflation assumption",
            missing_fields=("annual_inflation_assumption",),
        )
    try:
        values = _simulate(request, cancellation_check=cancellation_check, progress=progress)
    except (ArithmeticError, DecimalException, OverflowError):
        return _refusal(
            request,
            "model_output_undefined",
            "the seeded model distribution is undefined for the supplied finite inputs",
        )
    warnings = set(request.calibration.warnings)
    return BootstrapGoalResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=request.calibration.dataset_id,
        account_alias=request.calibration.account_alias,
        visibility=visibility,
        private_values=values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="goal_model",
            dataset_ids=(request.calibration.dataset_id,),
            account_aliases=(request.calibration.account_alias,),
            source_bindings=request.calibration.source_bindings,
            material=request,
        ),
        random_seed=request.random_seed,
        path_count=request.path_count,
    )


def _simulate(
    request: BootstrapGoalRequest,
    *,
    cancellation_check: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> PrivateBootstrapGoalValues:
    observations = tuple(item.return_ratio for item in request.calibration.observations)
    sample_rng = random.Random(  # noqa: S311 - deterministic model stream, not security.
        _domain_seed(request.random_seed, b"bootstrap-sampling"),
    )
    neutral_rng = random.Random(  # noqa: S311 - independent deterministic reference stream.
        _domain_seed(request.random_seed, b"order-neutral"),
    )
    ending_values: list[Decimal] = []
    goal_hits = 0
    ruin_hits = 0
    neutral_failures = 0
    zero_net_flows = all(flow.contribution == flow.withdrawal for flow in request.cash_flows)
    with localcontext() as context:
        context.prec = 50
        for path_index in range(request.path_count):
            if cancellation_check is not None:
                cancellation_check()
            if progress is not None:
                progress(path_index, request.path_count)
            sampled_returns = _sample_returns(
                observations,
                horizon=request.horizon_periods,
                block_length=request.block_length,
                rng=sample_rng,
            )
            ending_value, ruined = _run_path(request, sampled_returns)
            if zero_net_flows:
                neutral_ending = ending_value
            else:
                neutral_returns = list(sampled_returns)
                neutral_rng.shuffle(neutral_returns)
                neutral_ending, _ = _run_path(request, neutral_returns)
            ending_values.append(ending_value)
            goal_hits += ending_value >= request.explicit_goal
            ruin_hits += ruined
            neutral_failures += neutral_ending < request.explicit_goal
        if progress is not None:
            progress(request.path_count, request.path_count)
        path_count = Decimal(request.path_count)
        goal_probability = Decimal(goal_hits) / path_count
        ruin_probability = Decimal(ruin_hits) / path_count
        ordered_failure = Decimal(request.path_count - goal_hits) / path_count
        neutral_failure = Decimal(neutral_failures) / path_count
        sequence_risk = ordered_failure - neutral_failure
        ending_summary = _distribution_summary(ending_values)
        inflation_summary: DistributionSummary | None = None
        if request.include_inflation_adjustment:
            inflation = request.annual_inflation_assumption
            if inflation is None:
                raise ArithmeticError
            divisor = (Decimal(1) + inflation) ** request.horizon_years
            if not divisor.is_finite() or divisor <= 0:
                raise ArithmeticError
            inflation_summary = _distribution_summary(
                tuple(value / divisor for value in ending_values),
            )
    probabilities = (
        goal_probability,
        ruin_probability,
        ordered_failure,
        neutral_failure,
        sequence_risk,
    )
    if not all(value.is_finite() for value in probabilities):
        raise ArithmeticError
    return PrivateBootstrapGoalValues(
        reporting_currency=request.calibration.reporting_currency,
        ending_values=ending_summary,
        inflation_adjusted_ending_values=inflation_summary,
        goal_probability=goal_probability,
        ruin_probability=ruin_probability,
        ordered_failure_probability=ordered_failure,
        order_neutral_failure_probability=neutral_failure,
        sequence_of_returns_risk=sequence_risk,
    )


def _sample_returns(
    observations: Sequence[Decimal],
    *,
    horizon: int,
    block_length: int,
    rng: random.Random,
) -> tuple[Decimal, ...]:
    sampled: list[Decimal] = []
    maximum_start = len(observations) - block_length
    while len(sampled) < horizon:
        start = rng.randrange(maximum_start + 1)
        sampled.extend(observations[start : start + block_length])
    return tuple(sampled[:horizon])


def _run_path(
    request: BootstrapGoalRequest,
    returns: Sequence[Decimal],
) -> tuple[Decimal, bool]:
    value = request.starting_value
    ruined = value <= request.explicit_ruin_threshold
    for period_return, flow in zip(returns, request.cash_flows, strict=True):
        investable = max(Decimal(0), value + flow.contribution - flow.withdrawal)
        value = investable * (Decimal(1) + period_return)
        if not value.is_finite():
            raise ArithmeticError
        ruined = ruined or value <= request.explicit_ruin_threshold
    return value, ruined


def _distribution_summary(values: Sequence[Decimal]) -> DistributionSummary:
    ordered = tuple(sorted(values))
    if not ordered or not all(value.is_finite() and value >= 0 for value in ordered):
        raise ArithmeticError
    return DistributionSummary(
        minimum=ordered[0],
        lower=_empirical_quantile(ordered, 5, 100),
        median=_empirical_quantile(ordered, 50, 100),
        upper=_empirical_quantile(ordered, 95, 100),
        maximum=ordered[-1],
    )


def _empirical_quantile(
    ordered: Sequence[Decimal],
    numerator: int,
    denominator: int,
) -> Decimal:
    index = (len(ordered) - 1) * numerator // denominator
    return ordered[index]


def _domain_seed(seed: int, domain: bytes) -> int:
    digest = hashlib.sha256(
        b"saxo-bank-mcp:task15-model:v1\x00" + domain + seed.to_bytes(8, "big"),
    ).digest()
    return int.from_bytes(digest[:8], "big")


def _refusal(
    request: BootstrapGoalRequest,
    reason_code: str,
    reason: str,
    *,
    missing_fields: Sequence[str] = (),
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="goal_model",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(request.calibration.dataset_id,),
        instrument_handles=(),
        missing_fields=tuple(sorted(set(missing_fields))),
        source_scope=None,
    )


__all__ = (
    "BootstrapGoalRequest",
    "BootstrapGoalResult",
    "CashFlowPeriod",
    "DistributionSummary",
    "PrivateBootstrapGoalValues",
    "ReturnCalibrationDataset",
    "ReturnObservation",
    "run_bootstrap_goal_model",
)
