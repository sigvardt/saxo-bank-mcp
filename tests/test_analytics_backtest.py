from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_backtest import (
    BacktestDataset,
    BacktestRequest,
    BacktestResult,
    HistoricalBar,
    PrivateBacktestValues,
    run_backtest,
)
from saxo_bank_mcp.analytics_backtest_reference import (
    ReferenceBar,
    run_event_loop_reference,
)
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_strategy_schema import (
    FixedWeightSizing,
    HoldoutSplit,
    IndicatorSpec,
    PortfolioConstraints,
    RebalanceSchedule,
    SignalRule,
    SlippageModel,
    StrategyDefinition,
    StrategySchemaError,
    TransactionCostModel,
    WalkForwardFold,
    WalkForwardSplit,
    parse_strategy_definition,
    strategy_parameter_count,
)

_ALIAS = "aa_00000000000040008000000000000071"
_DATASET = "ds_00000000000040008000000000000071"
_HANDLE = "ih_00000000000040008000000000000071"
_START = datetime(2025, 1, 1, tzinfo=UTC)
_CLOSES = (10.0, 10.0, 12.0, 13.0, 14.0, 10.0, 9.0, 9.0, 10.0)
_EXPECTED_WARM_UP = 2
_EXPECTED_PARAMETER_COUNT = 4
_EXPECTED_PARAMETER_LIMIT = 12
_EXPECTED_WALK_FORWARD_WINDOWS = 4
_FINAL_SHORT_BAR_INDEX = 5


def _source(
    contract_id: str,
    *,
    quality: QualityState = QualityState.COMPLETE,
    entitlement: Literal["available", "partial", "denied"] = "available",
) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t18",
        capture_fingerprint_sha256=("1" if contract_id == "chart_v3" else "2") * 64,
        quality_state=quality,
        entitlement_state=entitlement,
    )


def _sources() -> tuple[SaxoSourceBinding, ...]:
    return (_source("chart_v3"), _source("reference_instruments_v1"))


def _bar(
    index: int,
    close: float,
    *,
    open_price: float | None = None,
    lifecycle_state: Literal["active", "delisted"] = "active",
) -> HistoricalBar:
    return HistoricalBar(
        instrument_handle=_HANDLE,
        at=_START + timedelta(days=index),
        open_price=close if open_price is None else open_price,
        close_price=close,
        lifecycle_state=lifecycle_state,
    )


def _bars() -> tuple[HistoricalBar, ...]:
    return tuple(_bar(index, close) for index, close in enumerate(_CLOSES))


def _holdout() -> HoldoutSplit:
    return HoldoutSplit(
        kind="holdout",
        train_end_at=_START + timedelta(days=4),
        holdout_start_at=_START + timedelta(days=5),
    )


def _strategy(  # noqa: PLR0913
    *,
    direction: Literal["long", "short"] = "long",
    weight: float = 0.5,
    commission_basis_points: float = 10.0,
    fixed_cost_per_fill: float = 1.0,
    slippage_basis_points: float = 5.0,
    interval_bars: int = 1,
    split: HoldoutSplit | WalkForwardSplit | None = None,
) -> StrategyDefinition:
    return StrategyDefinition(
        entry=SignalRule(
            left=IndicatorSpec(kind="close", window=1),
            comparison="greater_than",
            right_indicator=IndicatorSpec(kind="simple_moving_average", window=2),
            threshold=None,
        ),
        exit=SignalRule(
            left=IndicatorSpec(kind="close", window=1),
            comparison="less_than",
            right_indicator=IndicatorSpec(kind="simple_moving_average", window=2),
            threshold=None,
        ),
        direction=direction,
        sizing=FixedWeightSizing(kind="fixed_weight", target_weight=weight),
        rebalancing=RebalanceSchedule(
            kind="every_n_bars",
            interval_bars=interval_bars,
            fill_timing="next_bar_open",
        ),
        constraints=PortfolioConstraints(
            allow_long=True,
            allow_short=True,
            maximum_absolute_position_weight=0.8,
            maximum_gross_exposure=0.8,
            minimum_cash_weight=0.2,
        ),
        transaction_costs=TransactionCostModel(
            commission_basis_points=commission_basis_points,
            fixed_cost_per_fill=fixed_cost_per_fill,
            currency="USD",
        ),
        slippage=SlippageModel(
            kind=("none" if slippage_basis_points == 0.0 else "fixed_basis_points"),
            basis_points=slippage_basis_points,
        ),
        evaluation_split=_holdout() if split is None else split,
        missing_bar_policy="refuse",
        delisting_policy="terminal_close",
    )


def _always_in_strategy(
    *,
    interval_bars: int,
    direction: Literal["long", "short"] = "long",
) -> StrategyDefinition:
    return _strategy(
        direction=direction,
        commission_basis_points=0.0,
        fixed_cost_per_fill=0.0,
        slippage_basis_points=0.0,
        interval_bars=interval_bars,
    ).model_copy(
        update={
            "entry": SignalRule(
                left=IndicatorSpec(kind="close", window=1),
                comparison="greater_than",
                right_indicator=None,
                threshold=0.0,
            ),
            "exit": SignalRule(
                left=IndicatorSpec(kind="close", window=1),
                comparison="less_than",
                right_indicator=None,
                threshold=0.0,
            ),
        },
    )


def _dataset(  # noqa: PLR0913
    *,
    bars: tuple[HistoricalBar, ...] | None = None,
    source_bindings: tuple[SaxoSourceBinding, ...] | None = None,
    quality_state: QualityState = QualityState.COMPLETE,
    missing_interval_count: int = 0,
    missing_fields: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> BacktestDataset:
    values = _bars() if bars is None else bars
    return BacktestDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE,
        as_of=values[-1].at + timedelta(hours=1),
        bars=values,
        source_bindings=_sources() if source_bindings is None else source_bindings,
        quality_state=quality_state,
        missing_interval_count=missing_interval_count,
        missing_fields=missing_fields,
        warnings=warnings,
        universe_scope="single_instrument",
    )


def _request(
    *,
    dataset: BacktestDataset | None = None,
    strategy: StrategyDefinition | None = None,
) -> BacktestRequest:
    return BacktestRequest(
        dataset=_dataset() if dataset is None else dataset,
        strategy=_strategy() if strategy is None else strategy,
        starting_equity=1000.0,
    )


def _private(
    request: BacktestRequest | None = None,
) -> tuple[BacktestResult, PrivateBacktestValues]:
    result = run_backtest(
        _request() if request is None else request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    assert not isinstance(result, ResearchRefusal), (result.reason_code, result.reason)
    assert result.private_values is not None
    return result, result.private_values


def test_next_bar_fill_prevents_lookahead_and_respects_warmup() -> None:
    result, values = _private()

    assert result.lookahead_control == "signal_at_close_fill_next_bar_open"
    assert result.warm_up_bars == _EXPECTED_WARM_UP
    assert values.fills[0].decision_at == _START + timedelta(days=2)
    assert values.fills[0].fill_at == _START + timedelta(days=3)
    assert all(fill.fill_at > fill.decision_at for fill in values.fills)
    assert values.equity_curve[0].at == _START


def test_future_bar_mutation_cannot_change_an_earlier_fill() -> None:
    request = _request()
    _, baseline = _private(request)
    changed_bars = list(request.dataset.bars)
    changed_bars[-1] = _bar(len(changed_bars) - 1, 1000.0)
    _, changed = _private(_request(dataset=_dataset(bars=tuple(changed_bars))))

    assert baseline.fills[0] == changed.fills[0]
    boundary = _START + timedelta(days=7)
    assert tuple(point for point in baseline.equity_curve if point.at <= boundary) == tuple(
        point for point in changed.equity_curve if point.at <= boundary
    )


def test_parameter_count_is_explicit_and_reported() -> None:
    strategy = _strategy()
    result, _ = _private(_request(strategy=strategy))

    assert strategy_parameter_count(strategy) == _EXPECTED_PARAMETER_COUNT
    assert result.parameter_count == _EXPECTED_PARAMETER_COUNT
    assert result.parameter_limit == _EXPECTED_PARAMETER_LIMIT


def test_cost_and_slippage_components_sum_and_sensitivity_is_monotonic() -> None:
    _, values = _private()

    assert values.costs.total == pytest.approx(
        values.costs.commission + values.costs.fixed_fees + values.costs.slippage
    )
    sensitivity = values.cost_sensitivity
    assert sensitivity.zero_cost_ending_equity >= sensitivity.modeled_ending_equity
    assert sensitivity.modeled_ending_equity >= sensitivity.double_cost_ending_equity
    assert values.ending_equity == pytest.approx(sensitivity.modeled_ending_equity)


def test_cost_increase_cannot_improve_the_same_path() -> None:
    _, cheap = _private(
        _request(
            strategy=_strategy(
                commission_basis_points=0.0,
                fixed_cost_per_fill=0.0,
                slippage_basis_points=0.0,
            )
        )
    )
    _, costly = _private(
        _request(
            strategy=_strategy(
                commission_basis_points=30.0,
                fixed_cost_per_fill=2.0,
                slippage_basis_points=20.0,
            )
        )
    )

    assert costly.ending_equity < cheap.ending_equity
    assert costly.total_return_ratio < cheap.total_return_ratio


def test_rebalance_interval_changes_only_scheduled_decisions() -> None:
    _, daily = _private(_request(strategy=_strategy(interval_bars=1)))
    _, every_three = _private(_request(strategy=_strategy(interval_bars=3)))

    scheduled_decisions = {
        _START + timedelta(days=1 + (3 * index)) for index in range(3)
    }
    assert all(fill.decision_at in scheduled_decisions for fill in every_three.fills)
    assert daily.fills != every_three.fills


def test_unscheduled_half_weight_position_drifts_as_buy_and_hold() -> None:
    bars = tuple(
        _bar(index, close)
        for index, close in enumerate((100.0, 100.0, 200.0, 100.0, 100.0, 100.0))
    )
    request = _request(
        dataset=_dataset(bars=bars),
        strategy=_always_in_strategy(interval_bars=10),
    )

    _, values = _private(request)
    reference = run_event_loop_reference(
        tuple(
            ReferenceBar(
                at=bar.at,
                open_price=bar.open_price,
                close_price=bar.close_price,
                lifecycle_state=bar.lifecycle_state,
            )
            for bar in bars
        ),
        request.strategy,
        starting_equity=request.starting_equity,
    )

    assert values.ending_equity == pytest.approx(1000.0)
    assert values.total_turnover == pytest.approx(0.5)
    assert len(values.fills) == 1
    assert reference.ending_equity == pytest.approx(1000.0)
    assert reference.total_turnover == pytest.approx(0.5)


def test_solvent_short_realized_weight_below_negative_one_matches_reference() -> None:
    bars = tuple(
        _bar(
            index,
            close,
            open_price=100.0 if index == _FINAL_SHORT_BAR_INDEX else close,
        )
        for index, close in enumerate((100.0, 100.0, 100.0, 100.0, 100.0, 180.0))
    )
    request = _request(
        dataset=_dataset(bars=bars),
        strategy=_always_in_strategy(interval_bars=10, direction="short"),
    )
    result = run_backtest(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    reference = run_event_loop_reference(
        tuple(
            ReferenceBar(
                at=bar.at,
                open_price=bar.open_price,
                close_price=bar.close_price,
                lifecycle_state=bar.lifecycle_state,
            )
            for bar in bars
        ),
        request.strategy,
        starting_equity=request.starting_equity,
    )

    assert isinstance(result, BacktestResult)
    assert result.private_values is not None
    values = result.private_values
    assert values.ending_equity == pytest.approx(600.0)
    assert values.total_return_ratio == pytest.approx(-0.4)
    assert values.total_turnover == pytest.approx(0.5)
    assert values.ending_position_weight == pytest.approx(-1.5)
    assert len(values.fills) == 1
    assert values.ending_equity == pytest.approx(reference.ending_equity)
    assert values.total_return_ratio == pytest.approx(reference.total_return_ratio)
    assert values.total_turnover == pytest.approx(reference.total_turnover)
    assert len(values.fills) == reference.fill_count


def test_active_short_zero_equity_close_refuses_before_later_recovery() -> None:
    bars = (
        _bar(0, 100.0),
        _bar(1, 100.0),
        _bar(2, 300.0, open_price=100.0),
        _bar(3, 100.0, open_price=100.0),
        _bar(4, 100.0),
        _bar(5, 100.0),
    )
    request = _request(
        dataset=_dataset(bars=bars),
        strategy=_always_in_strategy(interval_bars=10, direction="short"),
    )
    result = run_backtest(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    reference_error: ArithmeticError | None = None
    try:
        run_event_loop_reference(
            tuple(
                ReferenceBar(
                    at=bar.at,
                    open_price=bar.open_price,
                    close_price=bar.close_price,
                    lifecycle_state=bar.lifecycle_state,
                )
                for bar in bars
            ),
            request.strategy,
            starting_equity=request.starting_equity,
        )
    except ArithmeticError as error:
        reference_error = error

    production_reason = (
        result.reason_code if isinstance(result, ResearchRefusal) else None
    )
    assert (production_reason, reference_error is not None) == (
        "backtest_measure_undefined",
        True,
    )


def test_scheduled_rebalance_records_drift_turnover_and_fill() -> None:
    bars = (
        _bar(0, 100.0),
        _bar(1, 100.0),
        _bar(2, 200.0),
        _bar(3, 100.0, open_price=200.0),
    )
    strategy = _always_in_strategy(interval_bars=2).model_copy(
        update={
            "evaluation_split": HoldoutSplit(
                kind="holdout",
                train_end_at=bars[1].at,
                holdout_start_at=bars[2].at,
            ),
        },
    )
    request = _request(
        dataset=_dataset(bars=bars),
        strategy=strategy,
    )

    _, values = _private(request)
    rebalance = tuple(fill for fill in values.fills if fill.cause == "rebalance")

    assert len(rebalance) == 1
    assert rebalance[0].decision_at == bars[2].at
    assert rebalance[0].fill_at == bars[3].at
    assert rebalance[0].turnover == pytest.approx(1.0 / 6.0)
    assert values.total_turnover == pytest.approx(2.0 / 3.0)


def test_holdout_and_walk_forward_splits_have_exact_nonoverlapping_windows() -> None:
    holdout_result, holdout_values = _private()
    assert holdout_result.split_kind == "holdout"
    assert tuple(window.label for window in holdout_values.split_windows) == (
        "in_sample",
        "holdout",
    )
    assert holdout_values.split_windows[0].end_at < holdout_values.split_windows[1].start_at

    walk_forward = WalkForwardSplit(
        kind="walk_forward",
        folds=(
            WalkForwardFold(
                train_start_at=_START,
                train_end_at=_START + timedelta(days=3),
                evaluation_start_at=_START + timedelta(days=4),
                evaluation_end_at=_START + timedelta(days=5),
            ),
            WalkForwardFold(
                train_start_at=_START + timedelta(days=2),
                train_end_at=_START + timedelta(days=5),
                evaluation_start_at=_START + timedelta(days=6),
                evaluation_end_at=_START + timedelta(days=8),
            ),
        ),
    )
    result, values = _private(_request(strategy=_strategy(split=walk_forward)))

    assert result.split_kind == "walk_forward"
    assert len(values.split_windows) == _EXPECTED_WALK_FORWARD_WINDOWS
    assert tuple(window.fold_index for window in values.split_windows) == (1, 1, 2, 2)


def test_missing_bar_and_missing_open_price_refuse_instead_of_filling() -> None:
    missing_interval = run_backtest(
        _request(dataset=_dataset(missing_interval_count=1)),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(missing_interval, ResearchRefusal)
    assert missing_interval.reason_code == "backtest_missing_bar"

    bars = list(_bars())
    bars[3] = bars[3].model_copy(update={"open_price": None})
    missing_open = run_backtest(
        _request(dataset=_dataset(bars=tuple(bars))),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(missing_open, ResearchRefusal)
    assert missing_open.reason_code == "backtest_order_fill_model_unavailable"


def test_delisting_terminal_value_is_kept_in_the_path() -> None:
    bars = tuple(
        _bar(
            index,
            0.0 if index == len(_CLOSES) - 1 else close,
            open_price=(10.0 if index == len(_CLOSES) - 1 else close),
            lifecycle_state=("delisted" if index == len(_CLOSES) - 1 else "active"),
        )
        for index, close in enumerate(_CLOSES)
    )
    result, values = _private(_request(dataset=_dataset(bars=bars)))

    assert values.delisting_event_count == 1
    assert values.ending_position_weight == 0.0
    assert "terminal_delisting_value_applied" in result.warnings


def test_vectorized_engine_matches_independent_event_loop_reference() -> None:
    request = _request()
    _, values = _private(request)
    reference = run_event_loop_reference(
        tuple(
            ReferenceBar(
                at=bar.at,
                open_price=bar.open_price,
                close_price=bar.close_price,
                lifecycle_state=bar.lifecycle_state,
            )
            for bar in request.dataset.bars
        ),
        request.strategy,
        starting_equity=request.starting_equity,
    )

    assert values.ending_equity == pytest.approx(reference.ending_equity, abs=1e-9)
    assert values.total_return_ratio == pytest.approx(reference.total_return_ratio, abs=1e-12)
    assert values.total_turnover == pytest.approx(reference.total_turnover, abs=1e-12)
    assert values.costs.total == pytest.approx(reference.total_cost, abs=1e-9)
    assert len(values.fills) == reference.fill_count


def test_crosses_below_requires_strictly_below_not_equal_in_reference() -> None:
    strategy = _strategy().model_copy(
        update={
            "entry": SignalRule(
                left=IndicatorSpec(kind="close", window=1),
                comparison="crosses_below",
                right_indicator=None,
                threshold=2.0,
            ),
            "exit": SignalRule(
                left=IndicatorSpec(kind="close", window=1),
                comparison="greater_than",
                right_indicator=None,
                threshold=100.0,
            ),
        }
    )
    bars = tuple(
        ReferenceBar(
            at=_START + timedelta(days=index),
            open_price=close,
            close_price=close,
            lifecycle_state="active",
        )
        for index, close in enumerate((3.0, 2.0, 2.0, 2.0))
    )

    reference = run_event_loop_reference(bars, strategy, starting_equity=1000.0)

    assert reference.fill_count == 0


def test_short_and_cash_constraints_are_enforced() -> None:
    short_result, short_values = _private(_request(strategy=_strategy(direction="short")))
    assert short_result.status is ResearchStatus.REDUCED
    assert all(fill.target_weight <= 0.0 for fill in short_values.fills)

    with pytest.raises(ValidationError):
        StrategyDefinition.model_validate(
            _strategy(direction="short").model_dump()
            | {"constraints": _strategy().constraints.model_copy(update={"allow_short": False})}
        )
    with pytest.raises(ValidationError):
        StrategyDefinition.model_validate(
            _strategy(weight=0.5).model_dump()
            | {
                "constraints": _strategy().constraints.model_copy(
                    update={"minimum_cash_weight": 0.6}
                )
            }
        )


def test_survivorship_and_universe_limitations_are_never_silent() -> None:
    result, _ = _private()

    assert result.survivorship_disclosure == (
        "single_instrument_only_no_point_in_time_universe_claim"
    )
    assert "survivorship_scope_single_instrument_only" in result.warnings
    assert "delisting_history_limited_to_bound_bars" in result.warnings


def test_incomplete_quality_denied_entitlement_and_unbound_source_refuse() -> None:
    partial = run_backtest(
        _request(
            dataset=_dataset(
                quality_state=QualityState.PARTIAL,
                missing_fields=("history.before_start",),
            )
        ),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(partial, ResearchRefusal)
    assert partial.reason_code == "backtest_source_incomplete"

    denied = run_backtest(
        _request(
            dataset=_dataset(
                source_bindings=(
                    _source("chart_v3", entitlement="denied"),
                    _source("reference_instruments_v1"),
                )
            )
        ),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(denied, ResearchRefusal)
    assert denied.reason_code == "source_entitlement_insufficient"

    missing = run_backtest(
        _request(dataset=_dataset(source_bindings=(_source("chart_v3"),))),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(missing, ResearchRefusal)
    assert missing.reason_code == "source_contract_missing"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python", "import os"),
        ("expression", "close[-1] > close[-2]"),
        ("sql", "select * from bars"),
        ("path", "local/strategy.py"),
        ("network_callback", "https://example.invalid/hook"),
    ],
)
def test_strategy_catalog_rejects_code_sql_paths_and_network_callbacks(
    field: str,
    value: str,
) -> None:
    payload = _strategy().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(StrategySchemaError, match="approved declarative strategy catalog"):
        parse_strategy_definition(payload)


def test_same_bar_close_fill_model_is_rejected() -> None:
    payload = _strategy().model_dump(mode="json")
    rebalancing = payload["rebalancing"]
    assert isinstance(rebalancing, dict)
    rebalancing["fill_timing"] = "same_bar_close"

    with pytest.raises(StrategySchemaError):
        parse_strategy_definition(payload)


def test_public_evidence_redacts_owner_money_and_result_has_no_authority() -> None:
    result = run_backtest(
        _request(),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        trusted_local_host=True,
    )
    assert isinstance(result, BacktestResult)
    assert result.private_values is None
    assert result.verification_state == "unverified"
    assert result.order_creation_authority is False
    assert result.approval_authority is False
    assert result.execution_authority is False
    assert result.is_not_advice is True
    assert result.is_not_forecast is True
    assert result.prediction_claim is False
    payload = result.model_dump(mode="json")
    assert payload["evidence"]["private_values_redacted"] is True
    assert "starting_equity" not in json.dumps(payload, sort_keys=True)
    assert "ending_equity" not in json.dumps(payload, sort_keys=True)
