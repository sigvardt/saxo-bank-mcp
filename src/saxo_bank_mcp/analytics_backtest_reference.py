from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_models import UtcDateTime
from saxo_bank_mcp.analytics_strategy_schema import (
    IndicatorSpec,
    SignalRule,
    StrategyDefinition,
)

_MINIMUM_REFERENCE_BARS = 2
_REFERENCE_TOLERANCE = 1e-12


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ReferenceBar(_StrictModel):
    """Small-fixture bar schema for the independent event-loop oracle."""

    at: UtcDateTime
    open_price: float | None = Field(default=None, allow_inf_nan=False)
    close_price: float | None = Field(default=None, allow_inf_nan=False)
    lifecycle_state: Literal["active", "delisted"]

    @model_validator(mode="after")
    def validate_prices(self) -> Self:
        if self.open_price is not None and self.open_price <= 0:
            raise ValueError("reference open price must be positive")
        if self.close_price is not None:
            if self.close_price < 0:
                raise ValueError("reference close price cannot be negative")
            if self.lifecycle_state == "active" and self.close_price == 0:
                raise ValueError("only a terminal delisting close may equal zero")
        return self


class ReferenceBacktestSummary(_StrictModel):
    ending_equity: float = Field(ge=0, allow_inf_nan=False)
    total_return_ratio: float = Field(ge=-1, allow_inf_nan=False)
    total_turnover: float = Field(ge=0, allow_inf_nan=False)
    total_cost: float = Field(ge=0, allow_inf_nan=False)
    fill_count: int = Field(ge=0)


def run_event_loop_reference(  # noqa: C901, PLR0912, PLR0915
    bars: Sequence[ReferenceBar],
    strategy: StrategyDefinition,
    *,
    starting_equity: float,
) -> ReferenceBacktestSummary:
    """Slow event loop sharing schemas but no production indicator or execution helpers."""
    if not math.isfinite(starting_equity) or starting_equity <= 0:
        raise ValueError("reference starting equity must be positive and finite")
    if len(bars) < _MINIMUM_REFERENCE_BARS:
        raise ValueError("reference backtest requires at least two bars")
    if any(bar.open_price is None or bar.close_price is None for bar in bars):
        raise ValueError("reference next-open model requires complete opens and closes")

    warm_up = max(
        _indicator_warmup(indicator)
        for rule in (strategy.entry, strategy.exit)
        for indicator in (rule.left, rule.right_indicator)
        if indicator is not None
    )
    equity = starting_equity
    cash = starting_equity
    position_units = 0.0
    logical_target = 0.0
    pending_target = _next_target(
        bars,
        strategy,
        index=0,
        current_target=logical_target,
        warm_up=warm_up,
    )
    if pending_target is not None:
        logical_target = pending_target
    turnover_total = 0.0
    cost_total = 0.0
    fill_count = 0

    for index in range(1, len(bars)):
        current = bars[index]
        current_open = _required_price(current.open_price)
        current_close = _required_price(current.close_price)
        equity_at_open = cash + position_units * current_open
        if not math.isfinite(equity_at_open) or equity_at_open <= 0.0:
            raise ArithmeticError("reference equity is exhausted before fill")
        turnover = 0.0
        cost = 0.0
        if pending_target is not None:
            current_weight = position_units * current_open / equity_at_open
            turnover = abs(pending_target - current_weight)
            if turnover > _REFERENCE_TOLERANCE:
                commission = (
                    equity_at_open
                    * turnover
                    * strategy.transaction_costs.commission_basis_points
                    / 10_000.0
                )
                fixed_fee = strategy.transaction_costs.fixed_cost_per_fill
                slippage = (
                    equity_at_open
                    * turnover
                    * strategy.slippage.basis_points
                    / 10_000.0
                )
                cost = commission + fixed_fee + slippage
                equity_after_cost = equity_at_open - cost
                if not math.isfinite(equity_after_cost) or equity_after_cost < 0:
                    raise ArithmeticError("reference costs exhaust modeled equity")
                target_value = pending_target * equity_after_cost
                position_units = target_value / current_open
                cash = equity_after_cost - target_value
                fill_count += 1
            else:
                turnover = 0.0
        equity = cash + position_units * current_close
        if not math.isfinite(equity) or equity < 0:
            raise ArithmeticError("reference path is undefined")
        turnover_total += turnover
        cost_total += cost
        if current.lifecycle_state == "delisted":
            position_units = 0.0
            cash = equity
            logical_target = 0.0
            pending_target = None
        else:
            pending_target = _next_target(
                bars,
                strategy,
                index=index,
                current_target=logical_target,
                warm_up=warm_up,
            )
            if pending_target is not None:
                logical_target = pending_target

    return ReferenceBacktestSummary(
        ending_equity=equity,
        total_return_ratio=equity / starting_equity - 1.0,
        total_turnover=turnover_total,
        total_cost=cost_total,
        fill_count=fill_count,
    )


def _next_target(
    bars: Sequence[ReferenceBar],
    strategy: StrategyDefinition,
    *,
    index: int,
    current_target: float,
    warm_up: int,
) -> float | None:
    first_decision = warm_up - 1
    if index < first_decision:
        return None
    if (index - first_decision) % strategy.rebalancing.interval_bars:
        return None
    entry = _rule_matches(bars, strategy.entry, index)
    exit_ = _rule_matches(bars, strategy.exit, index)
    direction = 1.0 if strategy.direction == "long" else -1.0
    intended_weight = direction * strategy.sizing.target_weight
    if current_target == 0.0 and entry:
        return intended_weight
    if current_target != 0.0 and exit_:
        return 0.0
    if current_target != 0.0:
        return intended_weight
    return None


def _rule_matches(  # noqa: PLR0911
    bars: Sequence[ReferenceBar],
    rule: SignalRule,
    index: int,
) -> bool:
    current = _comparison_state(bars, rule, index)
    if current is None:
        return False
    if rule.comparison == "greater_than":
        return current
    if rule.comparison == "less_than":
        return not current and not _operands_equal(bars, rule, index)
    if index == 0:
        return False
    previous = _comparison_state(bars, rule, index - 1)
    if previous is None:
        return False
    if rule.comparison == "crosses_above":
        return current and not previous
    return (
        not current
        and not _operands_equal(bars, rule, index)
        and (previous or _operands_equal(bars, rule, index - 1))
    )


def _comparison_state(
    bars: Sequence[ReferenceBar],
    rule: SignalRule,
    index: int,
) -> bool | None:
    left = _indicator_value(bars, rule.left, index)
    right = (
        rule.threshold
        if rule.right_indicator is None
        else _indicator_value(bars, rule.right_indicator, index)
    )
    if left is None or right is None:
        return None
    return left > right


def _operands_equal(
    bars: Sequence[ReferenceBar],
    rule: SignalRule,
    index: int,
) -> bool:
    left = _indicator_value(bars, rule.left, index)
    right = (
        rule.threshold
        if rule.right_indicator is None
        else _indicator_value(bars, rule.right_indicator, index)
    )
    return left is not None and right is not None and left == right


def _indicator_value(
    bars: Sequence[ReferenceBar],
    indicator: IndicatorSpec,
    index: int,
) -> float | None:
    close = _required_price(bars[index].close_price)
    if indicator.kind == "close":
        return close
    if indicator.kind == "simple_moving_average":
        start = index - indicator.window + 1
        if start < 0:
            return None
        values = tuple(_required_price(bar.close_price) for bar in bars[start : index + 1])
        return sum(values) / indicator.window
    previous_index = index - indicator.window
    if previous_index < 0:
        return None
    previous = _required_price(bars[previous_index].close_price)
    if previous == 0.0:
        return None
    return close / previous - 1.0


def _indicator_warmup(indicator: IndicatorSpec) -> int:
    if indicator.kind == "rate_of_change":
        return indicator.window + 1
    return indicator.window


def _required_price(value: float | None) -> float:
    if value is None:
        raise ValueError("reference price is missing")
    return value
