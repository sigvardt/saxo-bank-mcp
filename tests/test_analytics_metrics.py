from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np
import pytest
from pydantic import BaseModel, ConfigDict, Field

import saxo_bank_mcp.analytics_metrics as production_metrics
from saxo_bank_mcp.analytics_cashflows import (
    CashFlowEvent,
    NormalizedCashFlow,
    normalize_cash_flows,
)
from saxo_bank_mcp.analytics_fx import (
    FxNormalizationError,
    FxQuote,
    convert_amount,
    normalize_money_amounts,
)
from saxo_bank_mcp.analytics_metrics import (
    FinancialMetricError,
    active_return,
    active_returns,
    alpha,
    annualized_return,
    beta,
    cagr,
    calmar_ratio,
    correlation,
    covariance,
    cumulative_return,
    cumulative_returns,
    downside_capture,
    downside_deviation,
    drawdown_series,
    expected_shortfall,
    historical_var,
    log_returns,
    maximum_drawdown,
    money_weighted_return,
    parametric_var,
    sharpe_ratio,
    simple_returns,
    sortino_ratio,
    time_weighted_return,
    tracking_error,
    upside_capture,
    volatility,
    xirr,
)
from saxo_bank_mcp.analytics_reference_metrics import (
    reference_active_return,
    reference_active_returns,
    reference_alpha,
    reference_annualized_return,
    reference_beta,
    reference_cagr,
    reference_calmar_ratio,
    reference_correlation,
    reference_covariance,
    reference_cumulative_return,
    reference_cumulative_returns,
    reference_downside_capture,
    reference_downside_deviation,
    reference_drawdown_series,
    reference_expected_shortfall,
    reference_fx_convert,
    reference_historical_var,
    reference_log_returns,
    reference_maximum_drawdown,
    reference_money_weighted_return,
    reference_normalize_cash_flows,
    reference_parametric_var,
    reference_sharpe_ratio,
    reference_simple_returns,
    reference_sortino_ratio,
    reference_time_weighted_return,
    reference_tracking_error,
    reference_upside_capture,
    reference_volatility,
    reference_xirr,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures/analytics/golden_metrics.json"
_ABS_TOLERANCE = 1e-12
_REL_TOLERANCE = 1e-10


class _StrictFixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _ReturnFixture(_StrictFixtureModel):
    prices: tuple[float, ...]
    simple: tuple[float, ...]
    log: tuple[float, ...]
    cumulative: tuple[float, ...]
    annualized_total_return: float
    annualized_observation_count: int
    annualized_periods_per_year: float
    annualized_expected: float


class _PerformanceFixture(_StrictFixtureModel):
    twr_valuations: tuple[float, ...]
    twr_external_portfolio_flows: tuple[float, ...]
    twr_expected: float
    mwr_investor_cash_flows: tuple[float, ...]
    mwr_periods: tuple[float, ...]
    mwr_expected: float
    xirr_investor_cash_flows: tuple[float, ...]
    xirr_dates: tuple[date, ...]
    xirr_expected: float
    xirr_leap_dates: tuple[date, ...]
    xirr_leap_expected: float
    cagr_start: float
    cagr_end: float
    cagr_years: float
    cagr_expected: float
    subject_returns: tuple[float, ...]
    benchmark_returns: tuple[float, ...]
    active_returns: tuple[float, ...]
    aggregate_subject_return: float
    aggregate_benchmark_return: float
    aggregate_active_return: float
    tracking_subject_returns: tuple[float, ...]
    tracking_benchmark_returns: tuple[float, ...]
    tracking_periods_per_year: float
    tracking_error_expected: float
    capture_subject_returns: tuple[float, ...]
    capture_benchmark_returns: tuple[float, ...]
    upside_capture_expected: float
    downside_capture_expected: float


class _RiskFixture(_StrictFixtureModel):
    volatility_returns: tuple[float, ...]
    periods_per_year: float
    volatility_expected: float
    downside_target: float
    downside_expected: float
    drawdown_values: tuple[float, ...]
    drawdowns_expected: tuple[float, ...]
    maximum_drawdown_expected: float
    sharpe_returns: tuple[float, ...]
    sharpe_risk_free: float
    sharpe_expected: float
    sortino_returns: tuple[float, ...]
    sortino_target: float
    sortino_expected: float
    calmar_annualized_return: float
    calmar_drawdown: float
    calmar_expected: float
    tail_returns: tuple[float, ...]
    tail_confidence: float
    historical_var_expected: float
    expected_shortfall_expected: float
    parametric_mean: float
    parametric_standard_deviation: float
    parametric_confidence: float
    parametric_var_expected: float


class _DependenceFixture(_StrictFixtureModel):
    x: tuple[float, ...]
    y: tuple[float, ...]
    covariance_expected: float
    correlation_expected: float
    beta_expected: float
    risk_free_return: float
    periods_per_year: float
    alpha_expected: float


class _CashFlowEventFixture(_StrictFixtureModel):
    economic_at: datetime
    kind: Literal["deposit", "withdrawal", "fee", "financing", "tax", "income"]
    amount: Decimal
    currency: str


class _NormalizedCashFlowFixture(_StrictFixtureModel):
    kind: Literal["deposit", "withdrawal", "fee", "financing", "tax", "income"]
    reporting_amount: Decimal
    external_portfolio_flow: Decimal
    investor_cash_flow: Decimal


class _CashFlowFixture(_StrictFixtureModel):
    reporting_currency: str
    minor_unit: Decimal
    events: tuple[_CashFlowEventFixture, ...]
    expected: tuple[_NormalizedCashFlowFixture, ...]


class _FxQuoteFixture(_StrictFixtureModel):
    base_currency: str
    quote_currency: str
    rate: Decimal
    observed_at: datetime


class _FxFixture(_StrictFixtureModel):
    quotes: tuple[_FxQuoteFixture, ...]
    conversion_at: datetime
    direct_amount: Decimal
    direct_expected: Decimal
    inverse_amount: Decimal
    inverse_expected: Decimal
    same_currency_amount: Decimal
    same_currency_half_even_expected: Decimal
    multiple_amounts: tuple[Decimal, ...]
    multiple_currencies: tuple[str, ...]
    multiple_times: tuple[datetime, ...]
    multiple_expected: tuple[Decimal, ...]


class _GoldenFixture(_StrictFixtureModel):
    schema_version: Literal["1"]
    returns: _ReturnFixture
    performance: _PerformanceFixture
    risk: _RiskFixture
    dependence: _DependenceFixture
    cashflows: _CashFlowFixture
    fx: _FxFixture
    edge_case_ids: tuple[str, ...] = Field(min_length=15)
    mutation_ids: tuple[str, ...] = Field(min_length=7, max_length=7)


@pytest.fixture(scope="module")
def golden() -> _GoldenFixture:
    return _GoldenFixture.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _assert_close(actual: float, expected: float) -> None:
    assert math.isclose(
        actual,
        expected,
        rel_tol=_REL_TOLERANCE,
        abs_tol=_ABS_TOLERANCE,
    )


def _assert_array_close(actual: object, expected: tuple[float, ...]) -> None:
    np.testing.assert_allclose(
        np.asarray(actual, dtype=np.float64),
        np.asarray(expected, dtype=np.float64),
        rtol=_REL_TOLERANCE,
        atol=_ABS_TOLERANCE,
    )


def test_golden_fixture_is_public_deterministic_and_complete(golden: _GoldenFixture) -> None:
    raw = _FIXTURE_PATH.read_text(encoding="utf-8").casefold()

    assert golden.schema_version == "1"
    assert len(golden.edge_case_ids) == len(set(golden.edge_case_ids))
    assert golden.mutation_ids == (
        "sign",
        "denominator",
        "annualization",
        "date_order",
        "fee_omission",
        "fx_direction",
        "off_by_one",
    )
    assert '"account' not in raw
    assert '"token' not in raw


def test_return_series_match_hand_checked_golden_and_reference(golden: _GoldenFixture) -> None:
    case = golden.returns

    _assert_array_close(simple_returns(case.prices), case.simple)
    _assert_array_close(reference_simple_returns(case.prices), case.simple)
    _assert_array_close(log_returns(case.prices), case.log)
    _assert_array_close(reference_log_returns(case.prices), case.log)
    _assert_array_close(cumulative_returns(case.simple), case.cumulative)
    _assert_array_close(reference_cumulative_returns(case.simple), case.cumulative)
    _assert_close(cumulative_return(case.simple), case.cumulative[-1])
    _assert_close(reference_cumulative_return(case.simple), case.cumulative[-1])
    _assert_close(
        annualized_return(
            case.annualized_total_return,
            case.annualized_observation_count,
            case.annualized_periods_per_year,
        ),
        case.annualized_expected,
    )
    _assert_close(
        reference_annualized_return(
            case.annualized_total_return,
            case.annualized_observation_count,
            case.annualized_periods_per_year,
        ),
        case.annualized_expected,
    )


def test_twr_mwr_xirr_and_cagr_match_golden_and_reference(golden: _GoldenFixture) -> None:
    case = golden.performance

    _assert_close(
        time_weighted_return(
            case.twr_valuations,
            case.twr_external_portfolio_flows,
        ),
        case.twr_expected,
    )
    _assert_close(
        reference_time_weighted_return(
            case.twr_valuations,
            case.twr_external_portfolio_flows,
        ),
        case.twr_expected,
    )
    _assert_close(
        money_weighted_return(case.mwr_investor_cash_flows, case.mwr_periods),
        case.mwr_expected,
    )
    _assert_close(
        reference_money_weighted_return(
            case.mwr_investor_cash_flows,
            case.mwr_periods,
        ),
        case.mwr_expected,
    )
    _assert_close(
        xirr(case.xirr_investor_cash_flows, case.xirr_dates),
        case.xirr_expected,
    )
    _assert_close(
        reference_xirr(case.xirr_investor_cash_flows, case.xirr_dates),
        case.xirr_expected,
    )
    _assert_close(
        xirr(case.xirr_investor_cash_flows, case.xirr_leap_dates),
        case.xirr_leap_expected,
    )
    _assert_close(
        reference_xirr(case.xirr_investor_cash_flows, case.xirr_leap_dates),
        case.xirr_leap_expected,
    )
    _assert_close(cagr(case.cagr_start, case.cagr_end, case.cagr_years), case.cagr_expected)
    _assert_close(
        reference_cagr(case.cagr_start, case.cagr_end, case.cagr_years),
        case.cagr_expected,
    )


def test_active_tracking_and_capture_metrics_match_golden_and_reference(
    golden: _GoldenFixture,
) -> None:
    case = golden.performance

    _assert_array_close(
        active_returns(case.subject_returns, case.benchmark_returns),
        case.active_returns,
    )
    _assert_array_close(
        reference_active_returns(case.subject_returns, case.benchmark_returns),
        case.active_returns,
    )
    _assert_close(
        active_return(case.aggregate_subject_return, case.aggregate_benchmark_return),
        case.aggregate_active_return,
    )
    _assert_close(
        reference_active_return(
            case.aggregate_subject_return,
            case.aggregate_benchmark_return,
        ),
        case.aggregate_active_return,
    )
    _assert_close(
        tracking_error(
            case.tracking_subject_returns,
            case.tracking_benchmark_returns,
            case.tracking_periods_per_year,
        ),
        case.tracking_error_expected,
    )
    _assert_close(
        reference_tracking_error(
            case.tracking_subject_returns,
            case.tracking_benchmark_returns,
            case.tracking_periods_per_year,
        ),
        case.tracking_error_expected,
    )
    _assert_close(
        upside_capture(case.capture_subject_returns, case.capture_benchmark_returns),
        case.upside_capture_expected,
    )
    _assert_close(
        reference_upside_capture(
            case.capture_subject_returns,
            case.capture_benchmark_returns,
        ),
        case.upside_capture_expected,
    )
    _assert_close(
        downside_capture(case.capture_subject_returns, case.capture_benchmark_returns),
        case.downside_capture_expected,
    )
    _assert_close(
        reference_downside_capture(
            case.capture_subject_returns,
            case.capture_benchmark_returns,
        ),
        case.downside_capture_expected,
    )


def test_risk_metrics_match_hand_checked_golden_and_reference(golden: _GoldenFixture) -> None:
    case = golden.risk

    _assert_close(
        volatility(case.volatility_returns, case.periods_per_year),
        case.volatility_expected,
    )
    _assert_close(
        reference_volatility(case.volatility_returns, case.periods_per_year),
        case.volatility_expected,
    )
    _assert_close(
        downside_deviation(
            case.volatility_returns,
            case.downside_target,
            case.periods_per_year,
        ),
        case.downside_expected,
    )
    _assert_close(
        reference_downside_deviation(
            case.volatility_returns,
            case.downside_target,
            case.periods_per_year,
        ),
        case.downside_expected,
    )
    _assert_array_close(drawdown_series(case.drawdown_values), case.drawdowns_expected)
    _assert_array_close(
        reference_drawdown_series(case.drawdown_values),
        case.drawdowns_expected,
    )
    _assert_close(maximum_drawdown(case.drawdown_values), case.maximum_drawdown_expected)
    _assert_close(
        reference_maximum_drawdown(case.drawdown_values),
        case.maximum_drawdown_expected,
    )
    _assert_close(
        sharpe_ratio(
            case.sharpe_returns,
            case.sharpe_risk_free,
            case.periods_per_year,
        ),
        case.sharpe_expected,
    )
    _assert_close(
        reference_sharpe_ratio(
            case.sharpe_returns,
            case.sharpe_risk_free,
            case.periods_per_year,
        ),
        case.sharpe_expected,
    )
    _assert_close(
        sortino_ratio(
            case.sortino_returns,
            case.sortino_target,
            case.periods_per_year,
        ),
        case.sortino_expected,
    )
    _assert_close(
        reference_sortino_ratio(
            case.sortino_returns,
            case.sortino_target,
            case.periods_per_year,
        ),
        case.sortino_expected,
    )
    _assert_close(
        calmar_ratio(case.calmar_annualized_return, case.calmar_drawdown),
        case.calmar_expected,
    )
    _assert_close(
        reference_calmar_ratio(case.calmar_annualized_return, case.calmar_drawdown),
        case.calmar_expected,
    )


def test_var_and_expected_shortfall_match_golden_and_reference(golden: _GoldenFixture) -> None:
    case = golden.risk

    _assert_close(
        historical_var(case.tail_returns, case.tail_confidence, method="linear"),
        case.historical_var_expected,
    )
    _assert_close(
        reference_historical_var(case.tail_returns, case.tail_confidence, method="linear"),
        case.historical_var_expected,
    )
    _assert_close(
        expected_shortfall(case.tail_returns, case.tail_confidence, method="linear"),
        case.expected_shortfall_expected,
    )
    _assert_close(
        reference_expected_shortfall(
            case.tail_returns,
            case.tail_confidence,
            method="linear",
        ),
        case.expected_shortfall_expected,
    )
    _assert_close(
        parametric_var(
            case.parametric_mean,
            case.parametric_standard_deviation,
            case.parametric_confidence,
        ),
        case.parametric_var_expected,
    )
    _assert_close(
        reference_parametric_var(
            case.parametric_mean,
            case.parametric_standard_deviation,
            case.parametric_confidence,
        ),
        case.parametric_var_expected,
    )


def test_dependence_beta_and_alpha_match_golden_and_reference(golden: _GoldenFixture) -> None:
    case = golden.dependence

    _assert_close(covariance(case.x, case.y), case.covariance_expected)
    _assert_close(reference_covariance(case.x, case.y), case.covariance_expected)
    _assert_close(correlation(case.x, case.y), case.correlation_expected)
    _assert_close(reference_correlation(case.x, case.y), case.correlation_expected)
    _assert_close(beta(case.y, case.x), case.beta_expected)
    _assert_close(reference_beta(case.y, case.x), case.beta_expected)
    _assert_close(
        alpha(case.y, case.x, case.risk_free_return, case.periods_per_year),
        case.alpha_expected,
    )
    _assert_close(
        reference_alpha(
            case.y,
            case.x,
            case.risk_free_return,
            case.periods_per_year,
        ),
        case.alpha_expected,
    )


def _fx_quotes(golden: _GoldenFixture) -> tuple[FxQuote, ...]:
    return tuple(
        FxQuote(
            base_currency=item.base_currency,
            quote_currency=item.quote_currency,
            rate=item.rate,
            observed_at=item.observed_at,
        )
        for item in golden.fx.quotes
    )


def test_decimal_fx_direction_cutoff_and_half_even_rounding(golden: _GoldenFixture) -> None:
    case = golden.fx
    quotes = _fx_quotes(golden)
    minor_unit = Decimal("0.01")

    assert (
        convert_amount(
            case.direct_amount,
            "USD",
            "DKK",
            at=case.conversion_at,
            quotes=quotes,
            minor_unit=minor_unit,
        )
        == case.direct_expected
    )
    assert (
        convert_amount(
            case.inverse_amount,
            "DKK",
            "USD",
            at=case.conversion_at,
            quotes=quotes,
            minor_unit=minor_unit,
        )
        == case.inverse_expected
    )
    assert (
        convert_amount(
            case.same_currency_amount,
            "USD",
            "USD",
            at=case.conversion_at,
            quotes=(),
            minor_unit=minor_unit,
        )
        == case.same_currency_half_even_expected
    )
    assert (
        reference_fx_convert(
            case.direct_amount,
            "USD",
            "DKK",
            quote_base="USD",
            quote_currency="DKK",
            quote_rate=Decimal("7.00"),
            minor_unit=minor_unit,
        )
        == case.direct_expected
    )
    assert (
        reference_fx_convert(
            case.inverse_amount,
            "DKK",
            "USD",
            quote_base="USD",
            quote_currency="DKK",
            quote_rate=Decimal("7.00"),
            minor_unit=minor_unit,
        )
        == case.inverse_expected
    )


def test_multiple_currency_fx_normalization_uses_each_event_timestamp(
    golden: _GoldenFixture,
) -> None:
    case = golden.fx

    actual = normalize_money_amounts(
        case.multiple_amounts,
        case.multiple_currencies,
        case.multiple_times,
        reporting_currency="DKK",
        quotes=_fx_quotes(golden),
        minor_unit=Decimal("0.01"),
    )

    assert actual == case.multiple_expected


def _cash_flow_events(golden: _GoldenFixture) -> tuple[CashFlowEvent, ...]:
    return tuple(
        CashFlowEvent(
            economic_at=item.economic_at,
            kind=item.kind,
            amount=item.amount,
            currency=item.currency,
        )
        for item in golden.cashflows.events
    )


def _normalized_values(
    rows: tuple[NormalizedCashFlow, ...],
) -> tuple[tuple[str, Decimal, Decimal, Decimal], ...]:
    return tuple(
        (
            row.kind,
            row.reporting_amount,
            row.external_portfolio_flow,
            row.investor_cash_flow,
        )
        for row in rows
    )


def test_cash_flow_normalization_preserves_signs_timing_and_internal_fees(
    golden: _GoldenFixture,
) -> None:
    case = golden.cashflows
    events = _cash_flow_events(golden)
    expected = tuple(
        (
            row.kind,
            row.reporting_amount,
            row.external_portfolio_flow,
            row.investor_cash_flow,
        )
        for row in case.expected
    )

    actual = normalize_cash_flows(
        events,
        reporting_currency=case.reporting_currency,
        quotes=_fx_quotes(golden),
        minor_unit=case.minor_unit,
    )
    reference = reference_normalize_cash_flows(
        events,
        reporting_currency=case.reporting_currency,
        quotes=_fx_quotes(golden),
        minor_unit=case.minor_unit,
    )

    assert _normalized_values(actual) == expected
    assert reference == expected
    assert actual[0].economic_at == actual[1].economic_at
    assert actual[0].kind == "deposit"
    assert actual[1].kind == "fee"


@pytest.mark.parametrize(
    "call",
    [
        lambda: simple_returns([100.0]),
        lambda: simple_returns([100.0, math.nan]),
        lambda: simple_returns([100.0, 0.0]),
        lambda: log_returns([100.0, -1.0]),
        lambda: cumulative_returns([]),
        lambda: cumulative_returns([0.1, -1.1]),
        lambda: annualized_return(0.1, 0, 12.0),
        lambda: annualized_return(-1.0, 1, 12.0),
        lambda: annualized_return(1e308, 1, 252.0),
    ],
)
def test_return_edges_refuse_missing_nonfinite_and_invalid_growth(
    call: Callable[[], object],
) -> None:
    with pytest.raises(FinancialMetricError):
        call()


def test_performance_edges_refuse_ambiguous_timing_and_denominators() -> None:
    with pytest.raises(FinancialMetricError):
        time_weighted_return([100.0, 110.0, 120.0], [10.0])
    with pytest.raises(FinancialMetricError):
        time_weighted_return([0.0, 100.0], [0.0])
    with pytest.raises(FinancialMetricError):
        money_weighted_return([-100.0, -10.0], [0.0, 1.0])
    with pytest.raises(FinancialMetricError):
        money_weighted_return([-100.0, 110.0], [1.0, 0.0])
    with pytest.raises(FinancialMetricError):
        xirr([-100.0, 110.0], [date(2024, 1, 2), date(2024, 1, 1)])
    with pytest.raises(FinancialMetricError):
        xirr(
            [-100.0, 110.0],
            [
                datetime(2024, 1, 1, tzinfo=UTC).replace(tzinfo=None),
                datetime(2025, 1, 1, tzinfo=UTC).replace(tzinfo=None),
            ],
        )
    with pytest.raises(FinancialMetricError):
        cagr(0.0, 100.0, 1.0)


def test_aligned_and_capture_edges_refuse_partial_or_undefined_results() -> None:
    with pytest.raises(FinancialMetricError):
        active_returns([0.1], [0.1, 0.2])
    with pytest.raises(FinancialMetricError):
        tracking_error([0.1], [0.0], 252.0)
    with pytest.raises(FinancialMetricError):
        upside_capture([-0.1], [-0.2])
    with pytest.raises(FinancialMetricError):
        upside_capture([0.1], [0.0])
    with pytest.raises(FinancialMetricError):
        downside_capture([0.0], [0.1])


def test_risk_edges_refuse_insufficient_samples_and_zero_denominators() -> None:
    with pytest.raises(FinancialMetricError):
        volatility([0.1], 252.0)
    with pytest.raises(FinancialMetricError):
        downside_deviation([], 0.0, 252.0)
    with pytest.raises(FinancialMetricError):
        drawdown_series([])
    with pytest.raises(FinancialMetricError):
        maximum_drawdown([0.0, 1.0])
    with pytest.raises(FinancialMetricError):
        maximum_drawdown([100.0, -1.0])
    with pytest.raises(FinancialMetricError):
        sharpe_ratio([0.1, 0.1], 0.0, 252.0)
    with pytest.raises(FinancialMetricError):
        sortino_ratio([0.1, 0.2], 0.0, 252.0)
    with pytest.raises(FinancialMetricError):
        calmar_ratio(0.1, 0.0)


def test_tail_and_dependence_edges_refuse_undefined_statistics() -> None:
    with pytest.raises(FinancialMetricError):
        historical_var([0.0], 1.0)
    with pytest.raises(FinancialMetricError):
        expected_shortfall([], 0.95)
    with pytest.raises(FinancialMetricError):
        parametric_var(0.0, -0.1, 0.95)
    with pytest.raises(FinancialMetricError):
        covariance([0.1], [0.2])
    with pytest.raises(FinancialMetricError):
        correlation([0.1, 0.1], [0.2, 0.3])
    with pytest.raises(FinancialMetricError):
        beta([0.1, 0.2], [0.1, 0.1])
    with pytest.raises(FinancialMetricError):
        alpha([0.1], [0.2, 0.3], 0.0, 252.0)


def test_cash_flow_and_fx_edges_refuse_missing_or_ambiguous_values(
    golden: _GoldenFixture,
) -> None:
    with pytest.raises(ValueError, match="nonnegative magnitude"):
        CashFlowEvent(
            economic_at=datetime(2026, 1, 1, tzinfo=UTC),
            kind="deposit",
            amount=Decimal(-1),
            currency="USD",
        )
    with pytest.raises(ValueError, match="timestamp must use UTC"):
        CashFlowEvent(
            economic_at=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
            kind="deposit",
            amount=Decimal(1),
            currency="USD",
        )
    with pytest.raises(ValueError, match="FX rate must be positive"):
        FxQuote(
            base_currency="USD",
            quote_currency="DKK",
            rate=Decimal(0),
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(FxNormalizationError):
        convert_amount(
            Decimal(1),
            "GBP",
            "DKK",
            at=golden.fx.conversion_at,
            quotes=_fx_quotes(golden),
        )
    with pytest.raises(FxNormalizationError):
        convert_amount(
            Decimal(1),
            "USD",
            "DKK",
            at=datetime(2026, 1, 1, tzinfo=UTC),
            quotes=_fx_quotes(golden),
        )


def test_reference_path_does_not_call_patched_production_formulas(
    monkeypatch: pytest.MonkeyPatch,
    golden: _GoldenFixture,
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reference path called a production formula")

    for name in (
        "simple_returns",
        "cumulative_return",
        "volatility",
        "covariance",
        "money_weighted_return",
    ):
        monkeypatch.setattr(production_metrics, name, fail_if_called)

    assert reference_simple_returns(golden.returns.prices)
    _assert_close(
        reference_cumulative_return(golden.returns.simple),
        golden.returns.cumulative[-1],
    )
    _assert_close(
        reference_volatility(
            golden.risk.volatility_returns,
            golden.risk.periods_per_year,
        ),
        golden.risk.volatility_expected,
    )
    _assert_close(
        reference_covariance(golden.dependence.x, golden.dependence.y),
        golden.dependence.covariance_expected,
    )
    _assert_close(
        reference_money_weighted_return(
            golden.performance.mwr_investor_cash_flows,
            golden.performance.mwr_periods,
        ),
        golden.performance.mwr_expected,
    )


def test_mutation_kill_sign(golden: _GoldenFixture) -> None:
    case = golden.performance
    mutant = case.aggregate_benchmark_return - case.aggregate_subject_return

    _assert_close(
        active_return(case.aggregate_subject_return, case.aggregate_benchmark_return),
        case.aggregate_active_return,
    )
    assert not math.isclose(mutant, case.aggregate_active_return, abs_tol=_ABS_TOLERANCE)


def test_mutation_kill_denominator(golden: _GoldenFixture) -> None:
    case = golden.dependence
    mutant = float(np.cov(case.x, case.y, ddof=0)[0, 1])

    _assert_close(covariance(case.x, case.y), case.covariance_expected)
    assert not math.isclose(mutant, case.covariance_expected, abs_tol=_ABS_TOLERANCE)


def test_mutation_kill_annualization(golden: _GoldenFixture) -> None:
    case = golden.risk
    mutant = float(np.std(case.volatility_returns, ddof=1)) * case.periods_per_year

    _assert_close(
        volatility(case.volatility_returns, case.periods_per_year),
        case.volatility_expected,
    )
    assert not math.isclose(mutant, case.volatility_expected, abs_tol=_ABS_TOLERANCE)


def test_mutation_kill_date_order() -> None:
    with pytest.raises(FinancialMetricError):
        xirr([-100.0, 110.0], [date(2025, 1, 1), date(2024, 1, 1)])


def test_mutation_kill_fee_omission(golden: _GoldenFixture) -> None:
    rows = normalize_cash_flows(
        _cash_flow_events(golden),
        reporting_currency=golden.cashflows.reporting_currency,
        quotes=_fx_quotes(golden),
        minor_unit=golden.cashflows.minor_unit,
    )
    total_owner_effect = sum((row.reporting_amount for row in rows), Decimal(0))
    fee_omission_mutant = sum(
        (row.reporting_amount for row in rows if row.kind != "fee"),
        Decimal(0),
    )

    assert total_owner_effect == Decimal("572.70")
    assert fee_omission_mutant != total_owner_effect


def test_mutation_kill_fx_direction(golden: _GoldenFixture) -> None:
    case = golden.fx
    mutant = (case.direct_amount / Decimal("7.00")).quantize(Decimal("0.01"))

    assert (
        convert_amount(
            case.direct_amount,
            "USD",
            "DKK",
            at=case.conversion_at,
            quotes=_fx_quotes(golden),
            minor_unit=Decimal("0.01"),
        )
        == case.direct_expected
    )
    assert mutant != case.direct_expected


def test_mutation_kill_off_by_one(golden: _GoldenFixture) -> None:
    actual = simple_returns(golden.returns.prices)
    mutant = actual[:-1]

    assert len(actual) == len(golden.returns.prices) - 1
    assert len(mutant) != len(golden.returns.simple)
