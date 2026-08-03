from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Self

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_ghost_portfolio import (
    GhostPortfolioVerification,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
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
from saxo_bank_mcp.analytics_strategy_schema import (
    STRATEGY_PARAMETER_LIMIT,
    EvaluationSplit,
    HoldoutSplit,
    IndicatorSpec,
    SignalRule,
    StrategyDefinition,
    strategy_definition_fingerprint,
    strategy_parameter_count,
)

type FloatArray = NDArray[np.float64]
type BacktestVerificationState = Literal["unverified", "verified"]
type BacktestFillCause = Literal["entry", "exit", "rebalance"]

_SOURCE_SCOPE: Final = "saxo_openapi"
_BACKTEST_SOURCE_CONTRACTS: Final = ("chart_v3", "reference_instruments_v1")
_MAXIMUM_BACKTEST_BARS: Final = 10_000
_MINIMUM_BACKTEST_BARS: Final = 2
_FLOAT_TOLERANCE: Final = 1e-9


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class HistoricalBar(_StrictModel):
    """One cutoff-bound Saxo bar; a zero terminal close is delisting-only."""

    instrument_handle: InstrumentHandle
    at: UtcDateTime
    open_price: float | None = Field(default=None, allow_inf_nan=False)
    close_price: float | None = Field(default=None, allow_inf_nan=False)
    lifecycle_state: Literal["active", "delisted"]

    @model_validator(mode="after")
    def validate_prices(self) -> Self:
        if self.open_price is not None and self.open_price <= 0:
            raise ValueError("historical open price must be positive")
        if self.close_price is not None:
            if self.close_price < 0:
                raise ValueError("historical close price cannot be negative")
            if self.lifecycle_state == "active" and self.close_price == 0:
                raise ValueError("only a terminal delisting close may equal zero")
        return self


class BacktestDataset(_StrictModel):
    """One single-instrument source-bound research history."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    as_of: UtcDateTime
    bars: tuple[HistoricalBar, ...] = Field(min_length=2, max_length=_MAXIMUM_BACKTEST_BARS)
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_interval_count: int = Field(ge=0)
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]
    universe_scope: Literal["single_instrument"]

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        timestamps = tuple(bar.at for bar in self.bars)
        if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
            raise ValueError("backtest bars must be unique and strictly ordered")
        if any(bar.instrument_handle != self.instrument_handle for bar in self.bars):
            raise ValueError("backtest bars must match the dataset instrument handle")
        if self.bars[-1].at > self.as_of:
            raise ValueError("backtest bars cannot follow the dataset cutoff")
        delisted = tuple(
            index for index, bar in enumerate(self.bars) if bar.lifecycle_state == "delisted"
        )
        if delisted and delisted != (len(self.bars) - 1,):
            raise ValueError("a delisting event must be unique and terminal")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial backtest quality must match explicit missing fields")
        return self


class BacktestRequest(_StrictModel):
    dataset: BacktestDataset
    strategy: StrategyDefinition
    starting_equity: float = Field(gt=0, allow_inf_nan=False)


class BacktestCostSummary(_StrictModel):
    commission: float = Field(ge=0, allow_inf_nan=False)
    fixed_fees: float = Field(ge=0, allow_inf_nan=False)
    slippage: float = Field(ge=0, allow_inf_nan=False)
    total: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        expected = self.commission + self.fixed_fees + self.slippage
        if not math.isclose(self.total, expected, abs_tol=_FLOAT_TOLERANCE, rel_tol=1e-12):
            raise ValueError("backtest cost components must sum to total")
        return self


class BacktestFill(_StrictModel):
    decision_at: UtcDateTime
    fill_at: UtcDateTime
    cause: BacktestFillCause
    target_weight: float = Field(ge=-1, le=1, allow_inf_nan=False)
    turnover: float = Field(gt=0, le=2, allow_inf_nan=False)
    commission: float = Field(ge=0, allow_inf_nan=False)
    fixed_fee: float = Field(ge=0, allow_inf_nan=False)
    slippage: float = Field(ge=0, allow_inf_nan=False)
    total_cost: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_timing_and_cost(self) -> Self:
        if self.fill_at <= self.decision_at:
            raise ValueError("modeled fill must follow its signal decision")
        expected = self.commission + self.fixed_fee + self.slippage
        if not math.isclose(self.total_cost, expected, abs_tol=_FLOAT_TOLERANCE, rel_tol=1e-12):
            raise ValueError("modeled fill cost components must sum")
        return self


class BacktestEquityPoint(_StrictModel):
    at: UtcDateTime
    equity: float = Field(ge=0, allow_inf_nan=False)


class BacktestCostSensitivity(_StrictModel):
    zero_cost_ending_equity: float = Field(ge=0, allow_inf_nan=False)
    modeled_ending_equity: float = Field(ge=0, allow_inf_nan=False)
    double_cost_ending_equity: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_monotonicity(self) -> Self:
        if self.zero_cost_ending_equity + _FLOAT_TOLERANCE < self.modeled_ending_equity:
            raise ValueError("modeled costs cannot improve ending equity")
        if self.modeled_ending_equity + _FLOAT_TOLERANCE < self.double_cost_ending_equity:
            raise ValueError("doubled costs cannot improve ending equity")
        return self


class BacktestSplitWindow(_StrictModel):
    label: Literal[
        "in_sample",
        "holdout",
        "walk_forward_train",
        "walk_forward_evaluation",
    ]
    fold_index: int | None = Field(default=None, ge=1)
    start_at: UtcDateTime
    end_at: UtcDateTime
    return_ratio: float = Field(ge=-1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end_at < self.start_at:
            raise ValueError("backtest split window end precedes its start")
        walk_forward = self.label.startswith("walk_forward")
        if walk_forward != (self.fold_index is not None):
            raise ValueError("fold index must match walk-forward window labels")
        return self


class PrivateBacktestValues(_StrictModel):
    """Owner-only modeled equity, fills, costs, and split performance."""

    starting_equity: float = Field(gt=0, allow_inf_nan=False)
    ending_equity: float = Field(ge=0, allow_inf_nan=False)
    total_return_ratio: float = Field(ge=-1, allow_inf_nan=False)
    maximum_drawdown: float = Field(ge=-1, le=0, allow_inf_nan=False)
    total_turnover: float = Field(ge=0, allow_inf_nan=False)
    costs: BacktestCostSummary
    cost_sensitivity: BacktestCostSensitivity
    fills: tuple[BacktestFill, ...]
    equity_curve: tuple[BacktestEquityPoint, ...] = Field(min_length=2)
    split_windows: tuple[BacktestSplitWindow, ...] = Field(min_length=2)
    ending_position_weight: float = Field(le=1, allow_inf_nan=False)
    delisting_event_count: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if not math.isclose(
            self.ending_equity,
            self.equity_curve[-1].equity,
            abs_tol=_FLOAT_TOLERANCE,
            rel_tol=1e-12,
        ):
            raise ValueError("ending equity must equal the final path value")
        expected_return = self.ending_equity / self.starting_equity - 1.0
        if not math.isclose(
            self.total_return_ratio,
            expected_return,
            abs_tol=_FLOAT_TOLERANCE,
            rel_tol=1e-12,
        ):
            raise ValueError("backtest total return must reconcile to ending equity")
        return self


class BacktestResult(_StrictModel):
    """Bounded research output with explicit model limitations and no trade authority."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["strategy_backtest"] = "strategy_backtest"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateBacktestValues | None
    verification_state: BacktestVerificationState
    ghost_validation_state: Literal["not_run", "passed"]
    parameter_count: int = Field(ge=0, le=STRATEGY_PARAMETER_LIMIT)
    parameter_limit: Literal[12] = STRATEGY_PARAMETER_LIMIT
    warm_up_bars: int = Field(ge=1)
    split_kind: Literal["holdout", "walk_forward"]
    lookahead_control: Literal["signal_at_close_fill_next_bar_open"] = (
        "signal_at_close_fill_next_bar_open"
    )
    order_fill_model: Literal["next_bar_open"] = "next_bar_open"
    survivorship_disclosure: Literal["single_instrument_only_no_point_in_time_universe_claim"] = (
        "single_instrument_only_no_point_in_time_universe_claim"
    )
    overfit_warnings: tuple[ContractName, ...]
    warnings: tuple[ContractName, ...]
    evidence: PortfolioPublicEvidence
    is_not_advice: Literal[True] = True
    is_not_forecast: Literal[True] = True
    model_distribution_only: Literal[False] = False
    prediction_claim: Literal[False] = False
    recommendation_authority: Literal[False] = False
    order_creation_authority: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    does_not_verify: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_delivery_and_verification(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("backtest values do not match their delivery visibility")
        if (self.verification_state == "verified") != (self.ghost_validation_state == "passed"):
            raise ValueError("backtest verification requires equivalent ghost validation")
        return self


@dataclass(frozen=True, slots=True)
class _ExecutionRun:
    ending_equity: float
    total_return_ratio: float
    total_turnover: float
    costs: BacktestCostSummary
    fills: tuple[BacktestFill, ...]
    equity_curve: tuple[BacktestEquityPoint, ...]
    ending_position_weight: float


def run_backtest(  # noqa: PLR0911
    request: BacktestRequest,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    ghost_verification: GhostPortfolioVerification | None = None,
) -> BacktestResult | ResearchRefusal:
    """Run the bounded vectorized signal engine with explicit next-open execution."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    dataset = request.dataset
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_BACKTEST_SOURCE_CONTRACTS,
        analysis_kind="strategy_backtest",
        dataset_id=dataset.dataset_id,
        instrument_handles=(dataset.instrument_handle,),
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state is not QualityState.COMPLETE or source_assessment:
        return _refusal(
            request,
            "backtest_source_incomplete",
            "backtesting requires complete current source bindings and coverage",
            missing_fields=dataset.missing_fields,
            warnings=source_assessment,
        )
    if dataset.missing_interval_count:
        return _refusal(
            request,
            "backtest_missing_bar",
            "the bounded history contains a missing interval and cannot be filled implicitly",
            missing_fields=("price_bar",),
        )
    if any(bar.open_price is None or bar.close_price is None for bar in dataset.bars):
        return _refusal(
            request,
            "backtest_order_fill_model_unavailable",
            "next-bar-open fills require every modeled open and close",
            missing_fields=("open_price", "close_price"),
        )
    warm_up = _warm_up_bars(request.strategy)
    if len(dataset.bars) < warm_up + 2:
        return _refusal(
            request,
            "backtest_warmup_incomplete",
            "history does not cover indicator warm-up plus a next-bar fill and evaluation",
            missing_fields=("indicator_warmup",),
        )
    ghost_state = _assess_ghost_verification(
        request,
        ghost_verification,
    )
    if isinstance(ghost_state, ResearchRefusal):
        return ghost_state

    closes = np.asarray([_price(bar.close_price) for bar in dataset.bars], dtype=np.float64)
    entry = _rule_matches(closes, request.strategy.entry)
    exit_ = _rule_matches(closes, request.strategy.exit)
    decision_targets, decision_causes = _decision_targets(
        request,
        entry=entry,
        exit_=exit_,
        warm_up=warm_up,
    )
    try:
        baseline = _execute_path(
            request,
            decision_targets=decision_targets,
            decision_causes=decision_causes,
            cost_multiplier=1.0,
        )
        zero_cost = _execute_path(
            request,
            decision_targets=decision_targets,
            decision_causes=decision_causes,
            cost_multiplier=0.0,
        )
        double_cost = _execute_path(
            request,
            decision_targets=decision_targets,
            decision_causes=decision_causes,
            cost_multiplier=2.0,
        )
        split_windows = _split_windows(baseline.equity_curve, request.strategy.evaluation_split)
    except (ArithmeticError, OverflowError, ValueError):
        return _refusal(
            request,
            "backtest_measure_undefined",
            "the bounded backtest is undefined for the supplied finite inputs and split",
        )

    parameter_count = strategy_parameter_count(request.strategy)
    overfit_warnings = _overfit_warnings(parameter_count, len(dataset.bars))
    warnings: set[str] = {
        *dataset.warnings,
        *overfit_warnings,
        "survivorship_scope_single_instrument_only",
        "delisting_history_limited_to_bound_bars",
    }
    if ghost_state == "not_run":
        warnings.add("backtest_unverified_pending_equivalent_sim_ghost")
    if dataset.bars[-1].lifecycle_state == "delisted":
        warnings.add("terminal_delisting_value_applied")
    private_values = PrivateBacktestValues(
        starting_equity=request.starting_equity,
        ending_equity=baseline.ending_equity,
        total_return_ratio=baseline.total_return_ratio,
        maximum_drawdown=_maximum_drawdown(baseline.equity_curve),
        total_turnover=baseline.total_turnover,
        costs=baseline.costs,
        cost_sensitivity=BacktestCostSensitivity(
            zero_cost_ending_equity=zero_cost.ending_equity,
            modeled_ending_equity=baseline.ending_equity,
            double_cost_ending_equity=double_cost.ending_equity,
        ),
        fills=baseline.fills,
        equity_curve=baseline.equity_curve,
        split_windows=split_windows,
        ending_position_weight=baseline.ending_position_weight,
        delisting_event_count=int(dataset.bars[-1].lifecycle_state == "delisted"),
    )
    material: tuple[BaseModel, ...] = (
        (request,) if ghost_verification is None else (request, ghost_verification)
    )
    verification_state: BacktestVerificationState = (
        "verified" if ghost_state == "passed" else "unverified"
    )
    return BacktestResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        instrument_handle=dataset.instrument_handle,
        visibility=visibility,
        private_values=private_values if private_delivery else None,
        verification_state=verification_state,
        ghost_validation_state=ghost_state,
        parameter_count=parameter_count,
        warm_up_bars=warm_up,
        split_kind=request.strategy.evaluation_split.kind,
        overfit_warnings=overfit_warnings,
        warnings=tuple(sorted(warnings)),
        evidence=build_public_evidence(
            analysis_kind="strategy_backtest",
            dataset_ids=(dataset.dataset_id,),
            account_aliases=(dataset.account_alias,),
            source_bindings=dataset.source_bindings,
            material=material,
        ),
        does_not_verify=(
            ("future_performance",)
            if verification_state == "verified"
            else ("equivalent_controlled_sim_ghost", "future_performance")
        ),
    )


def _assess_ghost_verification(
    request: BacktestRequest,
    verification: GhostPortfolioVerification | None,
) -> Literal["not_run", "passed"] | ResearchRefusal:
    if verification is None:
        return "not_run"
    dataset = request.dataset
    if (
        verification.dataset_id != dataset.dataset_id
        or verification.account_alias != dataset.account_alias
        or verification.instrument_handle != dataset.instrument_handle
        or verification.strategy_fingerprint_sha256
        != strategy_definition_fingerprint(request.strategy)
        or verification.fill_model != request.strategy.rebalancing.fill_timing
        or verification.environment != "SIM"
        or verification.cleanup_state != "proved_equal"
    ):
        return _refusal(
            request,
            "ghost_strategy_or_source_mismatch",
            "ghost verification is not equivalent to this dataset, strategy, and fill model",
        )
    return _refusal(
        request,
        "ghost_authenticated_receipt_required",
        (
            "caller-constructed ghost verification lacks internal MCP receipt issuance "
            "and ledger provenance"
        ),
    )


def _warm_up_bars(strategy: StrategyDefinition) -> int:
    return max(
        _indicator_warmup(indicator)
        for rule in (strategy.entry, strategy.exit)
        for indicator in (rule.left, rule.right_indicator)
        if indicator is not None
    )


def _indicator_warmup(indicator: IndicatorSpec) -> int:
    if indicator.kind == "rate_of_change":
        return indicator.window + 1
    return indicator.window


def _indicator_values(closes: FloatArray, indicator: IndicatorSpec) -> FloatArray:
    values = np.full(closes.shape, np.nan, dtype=np.float64)
    if indicator.kind == "close":
        values[:] = closes
        return values
    if indicator.kind == "simple_moving_average":
        cumulative = np.concatenate((np.asarray([0.0]), np.cumsum(closes)))
        values[indicator.window - 1 :] = (
            cumulative[indicator.window :] - cumulative[: -indicator.window]
        ) / float(indicator.window)
        return values
    previous = closes[: -indicator.window]
    current = closes[indicator.window :]
    valid = previous != 0.0
    output = np.full(previous.shape, np.nan, dtype=np.float64)
    output[valid] = current[valid] / previous[valid] - 1.0
    values[indicator.window :] = output
    return values


def _rule_matches(closes: FloatArray, rule: SignalRule) -> NDArray[np.bool_]:
    left = _indicator_values(closes, rule.left)
    right = (
        np.full(closes.shape, rule.threshold, dtype=np.float64)
        if rule.right_indicator is None
        else _indicator_values(closes, rule.right_indicator)
    )
    valid = np.isfinite(left) & np.isfinite(right)
    matches = np.zeros(closes.shape, dtype=np.bool_)
    if rule.comparison == "greater_than":
        matches[valid] = left[valid] > right[valid]
        return matches
    if rule.comparison == "less_than":
        matches[valid] = left[valid] < right[valid]
        return matches
    if len(closes) < _MINIMUM_BACKTEST_BARS:
        return matches
    previous_valid = valid[:-1]
    current_valid = valid[1:]
    if rule.comparison == "crosses_above":
        crossing = (
            previous_valid & current_valid & (left[:-1] <= right[:-1]) & (left[1:] > right[1:])
        )
    else:
        crossing = (
            previous_valid & current_valid & (left[:-1] >= right[:-1]) & (left[1:] < right[1:])
        )
    matches[1:] = crossing
    return matches


def _decision_targets(
    request: BacktestRequest,
    *,
    entry: NDArray[np.bool_],
    exit_: NDArray[np.bool_],
    warm_up: int,
) -> tuple[FloatArray, tuple[BacktestFillCause | None, ...]]:
    count = len(request.dataset.bars)
    targets = np.zeros(count, dtype=np.float64)
    causes: list[BacktestFillCause | None] = [None] * count
    state = 0.0
    direction = 1.0 if request.strategy.direction == "long" else -1.0
    intended_weight = direction * request.strategy.sizing.target_weight
    first_decision = warm_up - 1
    for index, bar in enumerate(request.dataset.bars):
        scheduled = index >= first_decision and (
            (index - first_decision) % request.strategy.rebalancing.interval_bars == 0
        )
        if scheduled and bar.lifecycle_state != "delisted":
            if state == 0.0 and bool(entry[index]):
                state = intended_weight
                causes[index] = "entry"
            elif state != 0.0 and bool(exit_[index]):
                state = 0.0
                causes[index] = "exit"
            elif state != 0.0:
                state = intended_weight
                causes[index] = "rebalance"
        if bar.lifecycle_state == "delisted":
            state = 0.0
        targets[index] = state
    return targets, tuple(causes)


def _execute_path(  # noqa: PLR0915
    request: BacktestRequest,
    *,
    decision_targets: FloatArray,
    decision_causes: Sequence[BacktestFillCause | None],
    cost_multiplier: float,
) -> _ExecutionRun:
    bars = request.dataset.bars
    equity = request.starting_equity
    cash = request.starting_equity
    position_units = 0.0
    turnover_total = 0.0
    commission_total = 0.0
    fixed_total = 0.0
    slippage_total = 0.0
    fills: list[BacktestFill] = []
    curve = [BacktestEquityPoint(at=bars[0].at, equity=equity)]
    for index in range(1, len(bars)):
        previous = bars[index - 1]
        current = bars[index]
        current_open = _price(current.open_price)
        current_close = _price(current.close_price)
        equity_at_open = cash + position_units * current_open
        if not math.isfinite(equity_at_open) or equity_at_open <= 0.0:
            raise ArithmeticError("modeled equity is exhausted before fill")
        cause = decision_causes[index - 1]
        turnover = 0.0
        commission = 0.0
        fixed_fee = 0.0
        slippage = 0.0
        total_cost = 0.0
        if cause is not None:
            target = float(decision_targets[index - 1])
            current_weight = position_units * current_open / equity_at_open
            turnover = abs(target - current_weight)
            if turnover > _FLOAT_TOLERANCE:
                commission = (
                    equity_at_open
                    * turnover
                    * request.strategy.transaction_costs.commission_basis_points
                    / 10_000.0
                    * cost_multiplier
                )
                fixed_fee = request.strategy.transaction_costs.fixed_cost_per_fill * cost_multiplier
                slippage = (
                    equity_at_open
                    * turnover
                    * request.strategy.slippage.basis_points
                    / 10_000.0
                    * cost_multiplier
                )
                total_cost = commission + fixed_fee + slippage
                equity_after_cost = equity_at_open - total_cost
                if not math.isfinite(equity_after_cost) or equity_after_cost < 0:
                    raise ArithmeticError("modeled transaction costs exhaust equity")
                target_value = target * equity_after_cost
                position_units = target_value / current_open
                cash = equity_after_cost - target_value
                fills.append(
                    BacktestFill(
                        decision_at=previous.at,
                        fill_at=current.at,
                        cause=cause,
                        target_weight=target,
                        turnover=turnover,
                        commission=commission,
                        fixed_fee=fixed_fee,
                        slippage=slippage,
                        total_cost=total_cost,
                    )
                )
            else:
                turnover = 0.0
        equity = cash + position_units * current_close
        if (
            not math.isfinite(equity)
            or equity < 0
            or (equity <= 0 and position_units != 0.0 and current.lifecycle_state != "delisted")
        ):
            raise ArithmeticError("modeled backtest path is undefined")
        if current.lifecycle_state == "delisted":
            position_units = 0.0
            cash = equity
        turnover_total += turnover
        commission_total += commission
        fixed_total += fixed_fee
        slippage_total += slippage
        curve.append(BacktestEquityPoint(at=current.at, equity=equity))
    total_cost = commission_total + fixed_total + slippage_total
    if equity == 0.0 and position_units != 0.0:
        raise ArithmeticError("ending position weight is undefined after equity exhaustion")
    ending_position_weight = (
        0.0 if equity == 0.0 else position_units * _price(bars[-1].close_price) / equity
    )
    if not math.isfinite(ending_position_weight) or ending_position_weight > 1.0:
        raise ArithmeticError("ending position weight is outside the solvent cash model")
    return _ExecutionRun(
        ending_equity=equity,
        total_return_ratio=equity / request.starting_equity - 1.0,
        total_turnover=turnover_total,
        costs=BacktestCostSummary(
            commission=commission_total,
            fixed_fees=fixed_total,
            slippage=slippage_total,
            total=total_cost,
        ),
        fills=tuple(fills),
        equity_curve=tuple(curve),
        ending_position_weight=ending_position_weight,
    )


def _split_windows(
    curve: Sequence[BacktestEquityPoint],
    split: EvaluationSplit,
) -> tuple[BacktestSplitWindow, ...]:
    by_time = {point.at: point.equity for point in curve}
    if isinstance(split, HoldoutSplit):
        required = (curve[0].at, split.train_end_at, split.holdout_start_at, curve[-1].at)
        if any(at not in by_time for at in required):
            raise ValueError("holdout boundary is outside exact bar coverage")
        return (
            _split_window(
                by_time,
                label="in_sample",
                start_at=curve[0].at,
                end_at=split.train_end_at,
            ),
            _split_window(
                by_time,
                label="holdout",
                start_at=split.holdout_start_at,
                end_at=curve[-1].at,
            ),
        )
    windows: list[BacktestSplitWindow] = []
    for fold_index, fold in enumerate(split.folds, start=1):
        boundaries = (
            fold.train_start_at,
            fold.train_end_at,
            fold.evaluation_start_at,
            fold.evaluation_end_at,
        )
        if any(at not in by_time for at in boundaries):
            raise ValueError("walk-forward boundary is outside exact bar coverage")
        windows.extend(
            (
                _split_window(
                    by_time,
                    label="walk_forward_train",
                    start_at=fold.train_start_at,
                    end_at=fold.train_end_at,
                    fold_index=fold_index,
                ),
                _split_window(
                    by_time,
                    label="walk_forward_evaluation",
                    start_at=fold.evaluation_start_at,
                    end_at=fold.evaluation_end_at,
                    fold_index=fold_index,
                ),
            )
        )
    return tuple(windows)


def _split_window(
    by_time: dict[UtcDateTime, float],
    *,
    label: Literal[
        "in_sample",
        "holdout",
        "walk_forward_train",
        "walk_forward_evaluation",
    ],
    start_at: UtcDateTime,
    end_at: UtcDateTime,
    fold_index: int | None = None,
) -> BacktestSplitWindow:
    start = by_time[start_at]
    end = by_time[end_at]
    if start <= 0:
        raise ArithmeticError("split return is undefined after equity exhaustion")
    return BacktestSplitWindow(
        label=label,
        fold_index=fold_index,
        start_at=start_at,
        end_at=end_at,
        return_ratio=end / start - 1.0,
    )


def _maximum_drawdown(curve: Sequence[BacktestEquityPoint]) -> float:
    peak = curve[0].equity
    maximum = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        if peak > 0:
            maximum = min(maximum, point.equity / peak - 1.0)
    return maximum


def _overfit_warnings(parameter_count: int, observation_count: int) -> tuple[str, ...]:
    if parameter_count * 10 > observation_count:
        return ("parameter_count_high_for_observations",)
    return ()


def _price(value: float | None) -> float:
    if value is None:
        raise ValueError("required backtest price is missing")
    return value


def _refusal(
    request: BacktestRequest,
    reason_code: str,
    reason: str,
    *,
    missing_fields: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> ResearchRefusal:
    dataset = request.dataset
    return ResearchRefusal(
        analysis_kind="strategy_backtest",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=(dataset.instrument_handle,),
        missing_fields=tuple(sorted(set(missing_fields))),
        warnings=tuple(sorted({*dataset.warnings, *warnings})),
        source_scope=None,
    )
