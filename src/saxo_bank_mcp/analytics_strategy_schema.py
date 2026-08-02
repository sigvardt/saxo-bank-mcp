from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.analytics_models import IsoCurrencyCode, UtcDateTime

type IndicatorKind = Literal["close", "simple_moving_average", "rate_of_change"]
type RuleComparison = Literal[
    "greater_than",
    "less_than",
    "crosses_above",
    "crosses_below",
]
type StrategyDirection = Literal["long", "short"]

_MAXIMUM_INDICATOR_WINDOW = 1_000
_MAXIMUM_REBALANCE_INTERVAL = 1_000
_MINIMUM_MOVING_AVERAGE_WINDOW = 2
STRATEGY_PARAMETER_LIMIT = 12


class StrategySchemaError(ValueError):
    """A value-free rejection for payloads outside the typed strategy catalog."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class IndicatorSpec(_StrictModel):
    """One approved deterministic indicator and its explicit warm-up window."""

    kind: IndicatorKind
    window: int = Field(ge=1, le=_MAXIMUM_INDICATOR_WINDOW)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.kind == "close" and self.window != 1:
            raise ValueError("close indicator window must equal one")
        if (
            self.kind == "simple_moving_average"
            and self.window < _MINIMUM_MOVING_AVERAGE_WINDOW
        ):
            raise ValueError("simple moving average window must be at least two")
        return self


class SignalRule(_StrictModel):
    """One typed comparison between approved indicators or a numeric threshold."""

    left: IndicatorSpec
    comparison: RuleComparison
    right_indicator: IndicatorSpec | None
    threshold: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_right_operand(self) -> Self:
        if (self.right_indicator is None) == (self.threshold is None):
            raise ValueError("a signal rule requires exactly one typed right operand")
        return self


class FixedWeightSizing(_StrictModel):
    kind: Literal["fixed_weight"]
    target_weight: float = Field(gt=0, le=1, allow_inf_nan=False)


class RebalanceSchedule(_StrictModel):
    kind: Literal["every_n_bars"]
    interval_bars: int = Field(ge=1, le=_MAXIMUM_REBALANCE_INTERVAL)
    fill_timing: Literal["next_bar_open"]


class PortfolioConstraints(_StrictModel):
    allow_long: bool
    allow_short: bool
    maximum_absolute_position_weight: float = Field(gt=0, le=1, allow_inf_nan=False)
    maximum_gross_exposure: float = Field(gt=0, le=2, allow_inf_nan=False)
    minimum_cash_weight: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_constraint_set(self) -> Self:
        if not self.allow_long and not self.allow_short:
            raise ValueError("at least one position direction must be allowed")
        if self.maximum_absolute_position_weight > self.maximum_gross_exposure:
            raise ValueError("position limit cannot exceed gross exposure limit")
        return self


class TransactionCostModel(_StrictModel):
    """Positive cost magnitudes charged on each modeled fill."""

    commission_basis_points: float = Field(ge=0, le=1_000, allow_inf_nan=False)
    fixed_cost_per_fill: float = Field(ge=0, allow_inf_nan=False)
    currency: IsoCurrencyCode


class SlippageModel(_StrictModel):
    kind: Literal["none", "fixed_basis_points"]
    basis_points: float = Field(ge=0, le=1_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_basis_points(self) -> Self:
        if self.kind == "none" and self.basis_points != 0.0:
            raise ValueError("none slippage requires zero basis points")
        return self


class HoldoutSplit(_StrictModel):
    kind: Literal["holdout"]
    train_end_at: UtcDateTime
    holdout_start_at: UtcDateTime

    @model_validator(mode="after")
    def validate_boundaries(self) -> Self:
        if self.holdout_start_at <= self.train_end_at:
            raise ValueError("holdout must start after the training cutoff")
        return self


class WalkForwardFold(_StrictModel):
    train_start_at: UtcDateTime
    train_end_at: UtcDateTime
    evaluation_start_at: UtcDateTime
    evaluation_end_at: UtcDateTime

    @model_validator(mode="after")
    def validate_boundaries(self) -> Self:
        if self.train_end_at < self.train_start_at:
            raise ValueError("walk-forward training end precedes its start")
        if self.evaluation_start_at <= self.train_end_at:
            raise ValueError("walk-forward evaluation must follow training")
        if self.evaluation_end_at < self.evaluation_start_at:
            raise ValueError("walk-forward evaluation end precedes its start")
        return self


class WalkForwardSplit(_StrictModel):
    kind: Literal["walk_forward"]
    folds: tuple[WalkForwardFold, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_fold_order(self) -> Self:
        evaluation_starts = tuple(fold.evaluation_start_at for fold in self.folds)
        if evaluation_starts != tuple(sorted(evaluation_starts)):
            raise ValueError("walk-forward folds must be evaluation-time ordered")
        for left, right in pairwise(self.folds):
            if right.evaluation_start_at <= left.evaluation_end_at:
                raise ValueError("walk-forward evaluation windows must not overlap")
        return self


type EvaluationSplit = Annotated[
    HoldoutSplit | WalkForwardSplit,
    Field(discriminator="kind"),
]


class StrategyDefinition(_StrictModel):
    """Bounded strategy definition with no code, query, path, or callback fields."""

    entry: SignalRule
    exit: SignalRule
    direction: StrategyDirection
    sizing: FixedWeightSizing
    rebalancing: RebalanceSchedule
    constraints: PortfolioConstraints
    transaction_costs: TransactionCostModel
    slippage: SlippageModel
    evaluation_split: EvaluationSplit
    missing_bar_policy: Literal["refuse"]
    delisting_policy: Literal["terminal_close"]

    @model_validator(mode="after")
    def validate_strategy_constraints(self) -> Self:
        if self.entry == self.exit:
            raise ValueError("entry and exit rules must differ")
        if self.direction == "long" and not self.constraints.allow_long:
            raise ValueError("long direction is forbidden by the constraint set")
        if self.direction == "short" and not self.constraints.allow_short:
            raise ValueError("short direction is forbidden by the constraint set")
        weight = self.sizing.target_weight
        if weight > self.constraints.maximum_absolute_position_weight:
            raise ValueError("sizing exceeds the absolute position limit")
        if weight > self.constraints.maximum_gross_exposure:
            raise ValueError("sizing exceeds the gross exposure limit")
        if 1.0 - weight < self.constraints.minimum_cash_weight - 1e-12:
            raise ValueError("sizing violates the minimum cash constraint")
        if strategy_parameter_count(self) > STRATEGY_PARAMETER_LIMIT:
            raise ValueError("strategy exceeds the bounded parameter limit")
        return self


def parse_strategy_definition(payload: Mapping[str, object]) -> StrategyDefinition:
    """Parse only the approved catalog and return a value-free refusal on any deviation."""
    try:
        return StrategyDefinition.model_validate(payload)
    except ValidationError:
        raise StrategySchemaError(
            "strategy payload is outside the approved declarative strategy catalog",
        ) from None


def strategy_parameter_count(strategy: StrategyDefinition) -> int:
    """Count explicit numeric strategy degrees of freedom, excluding cost assumptions."""

    def indicator_count(indicator: IndicatorSpec | None) -> int:
        return int(indicator is not None and indicator.kind != "close")

    def rule_count(rule: SignalRule) -> int:
        return (
            indicator_count(rule.left)
            + indicator_count(rule.right_indicator)
            + int(rule.threshold is not None)
        )

    return (
        rule_count(strategy.entry)
        + rule_count(strategy.exit)
        + 1  # fixed target weight
        + 1  # rebalance interval
    )


def strategy_definition_fingerprint(strategy: StrategyDefinition) -> str:
    """Return a deterministic safe binding for an exact declarative strategy."""
    payload = strategy.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
