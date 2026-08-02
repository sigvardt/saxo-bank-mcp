# ruff: noqa: PLR0913, PLR2004

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import QualityState, VisibilityMode
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_trade_review import (
    ClosedTrade,
    DecisionBarReference,
    DecisionPointQuote,
    TradeFill,
    TradeReviewDataset,
    analyze_trading_mirror,
)

_ALIAS = "aa_00000000000040008000000000000012"
_DATASET = "ds_00000000000040008000000000000042"
_HANDLE_A = "ih_00000000000040008000000000000041"
_HANDLE_B = "ih_00000000000040008000000000000042"
_START = datetime(2026, 5, 5, 10, tzinfo=UTC)
_QUOTE_CAPTURE = "b" * 64
_SECOND_QUOTE_CAPTURE = "e" * 64
_BAR_CAPTURE = "f" * 64


def _source(contract_id: str, *, capture: str = "a" * 64) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision="fixture:t14",
        capture_fingerprint_sha256=capture,
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _fill(
    key: str,
    *,
    role: Literal["entry", "exit"],
    side: Literal["buy", "sell"],
    quantity: str,
    price: str,
    at: datetime,
    handle: str = _HANDLE_A,
    revision: int = 1,
) -> TradeFill:
    return TradeFill(
        account_alias=_ALIAS,
        instrument_handle=handle,
        event_key_sha256=key * 64,
        revision=revision,
        occurred_at=at,
        role=role,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        currency="USD",
    )


def _quote(
    *,
    handle: str = _HANDLE_A,
    quality: QualityState = QualityState.COMPLETE,
    captured_by_mcp: bool = True,
    captured_at: datetime = _START,
    capture: str = _QUOTE_CAPTURE,
) -> DecisionPointQuote:
    return DecisionPointQuote(
        dataset_id="ds_00000000000040008000000000000043",
        instrument_handle=handle,
        captured_at=captured_at,
        bid=Decimal(99),
        ask=Decimal(101),
        price_type="Tradable",
        delayed_by_minutes=0,
        quality_state=quality,
        entitlement_state="available",
        source_binding=_source("info_price_v1", capture=capture),
        captured_by_mcp=captured_by_mcp,
        warnings=(),
    )


def _bar(*, handle: str = _HANDLE_A) -> DecisionBarReference:
    return DecisionBarReference(
        dataset_id="ds_00000000000040008000000000000044",
        instrument_handle=handle,
        decision_at=_START,
        bar_start_at=_START - timedelta(minutes=5),
        bar_end_at=_START + timedelta(minutes=5),
        close_price=Decimal(100),
        source_binding=_source("chart_v3", capture=_BAR_CAPTURE),
    )


def _winner(
    *,
    quote: DecisionPointQuote | None = None,
    bar: DecisionBarReference | None = None,
) -> ClosedTrade:
    return ClosedTrade(
        trade_key_sha256="c" * 64,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE_A,
        decision_at=_START,
        fills=(
            _fill(
                "1",
                role="entry",
                side="buy",
                quantity="5",
                price="102",
                at=_START + timedelta(minutes=1),
            ),
            _fill(
                "1",
                role="entry",
                side="buy",
                quantity="5",
                price="101",
                at=_START + timedelta(minutes=1),
                revision=2,
            ),
            _fill(
                "2",
                role="entry",
                side="buy",
                quantity="5",
                price="99",
                at=_START + timedelta(minutes=2),
            ),
            _fill(
                "3",
                role="exit",
                side="sell",
                quantity="10",
                price="110",
                at=_START + timedelta(days=1, minutes=1),
            ),
        ),
        costs=Decimal(2),
        cost_currency="USD",
        counterfactual_at=_START + timedelta(days=2),
        counterfactual_price=Decimal(105),
        decision_quote=quote,
        decision_bar=bar,
    )


def _loser() -> ClosedTrade:
    return ClosedTrade(
        trade_key_sha256="d" * 64,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE_B,
        decision_at=_START + timedelta(hours=1),
        fills=(
            _fill(
                "4",
                role="entry",
                side="buy",
                quantity="4",
                price="100",
                at=_START + timedelta(hours=1, minutes=1),
                handle=_HANDLE_B,
            ),
            _fill(
                "5",
                role="exit",
                side="sell",
                quantity="4",
                price="90",
                at=_START + timedelta(hours=3, minutes=1),
                handle=_HANDLE_B,
            ),
        ),
        costs=Decimal(0),
        cost_currency="USD",
        counterfactual_at=_START + timedelta(hours=4),
        counterfactual_price=Decimal(95),
        decision_quote=_quote(
            handle=_HANDLE_B,
            captured_at=_START + timedelta(hours=1),
            capture=_SECOND_QUOTE_CAPTURE,
        ),
        decision_bar=None,
    )


def _dataset(
    trades: tuple[ClosedTrade, ...],
) -> TradeReviewDataset:
    return TradeReviewDataset(
        dataset_id=_DATASET,
        account_alias=_ALIAS,
        start_at=_START,
        end_at=_START + timedelta(days=3),
        reporting_currency="USD",
        trades=trades,
        fx_quotes=(),
        source_bindings=(
            _source("transactions_v1"),
            _source("closed_positions_history_v1"),
        ),
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )


def test_mcp_decision_quote_allows_exact_arrival_midpoint_and_spread_claims() -> None:
    result = analyze_trading_mirror(
        _dataset((_winner(quote=_quote()),)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.COMPLETE
    values = result.private_values
    assert values is not None
    execution = values.trades[0].execution_quality
    assert execution.evidence_class == "exact_mcp_decision_quote"
    assert execution.arrival_price == Decimal(100)
    assert execution.midpoint == Decimal(100)
    assert execution.spread == Decimal(2)
    assert execution.reference_price is None


def test_stale_or_non_mcp_quote_falls_back_only_to_labeled_bar_approximation() -> None:
    stale = analyze_trading_mirror(
        _dataset(
            (_winner(quote=_quote(quality=QualityState.STALE), bar=_bar()),),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    not_mcp = analyze_trading_mirror(
        _dataset(
            (_winner(quote=_quote(captured_by_mcp=False), bar=_bar()),),
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    for result in (stale, not_mcp):
        assert not isinstance(result, ResearchRefusal)
        assert result.status is ResearchStatus.REDUCED
        assert result.private_values is not None
        execution = result.private_values.trades[0].execution_quality
        assert execution.evidence_class == "bar_approximation"
        assert execution.arrival_price is None
        assert execution.midpoint is None
        assert execution.spread is None
        assert execution.reference_price == Decimal(100)
        assert execution.approximation_rule == "nearest_saxo_chart_bar_close"


def test_execution_metric_is_unavailable_without_discarding_other_trade_review() -> None:
    result = analyze_trading_mirror(
        _dataset((_winner(),)),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    assert result.status is ResearchStatus.REDUCED
    assert result.private_values is not None
    execution = result.private_values.trades[0].execution_quality
    assert execution.evidence_class == "unavailable"
    assert execution.arrival_price is None
    assert execution.midpoint is None
    assert execution.spread is None
    assert "execution_reference_unavailable" in result.warnings


def test_trading_mirror_handles_partial_fills_corrections_and_behavior_metrics() -> None:
    result = analyze_trading_mirror(
        _dataset((_winner(quote=_quote()), _loser())),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )

    assert not isinstance(result, ResearchRefusal)
    values = result.private_values
    assert values is not None
    winner = values.trades[0]
    assert winner.entry_quantity == Decimal(10)
    assert winner.entry_vwap == Decimal(100)
    assert winner.exit_vwap == Decimal(110)
    assert winner.gross_profit_loss == Decimal(100)
    assert winner.net_profit_loss == Decimal(98)
    assert winner.holding_hours == Decimal(24)
    assert winner.averaging_down_events == 1
    assert winner.do_nothing_counterfactual_gross_profit_loss == Decimal(50)
    assert values.session_cockpit.trade_count == 2
    assert values.session_cockpit.fill_count == 5
    assert values.report_card.winners == 1
    assert values.report_card.losers == 1
    assert values.report_card.win_rate_percentage == Decimal(50)
    assert values.report_card.average_winner == Decimal(98)
    assert values.report_card.average_loser == Decimal(-40)
    assert values.report_card.winner_loser_asymmetry == Decimal("2.45")
    assert values.report_card.averaging_down_events == 1
    assert "causal_explanation" in result.does_not_verify


@pytest.mark.parametrize(
    ("entry_side", "exit_side"),
    [("buy", "sell"), ("sell", "buy")],
)
def test_chronological_inventory_accepts_long_and_short_partial_fills(
    entry_side: Literal["buy", "sell"],
    exit_side: Literal["buy", "sell"],
) -> None:
    trade = ClosedTrade(
        trade_key_sha256="a" * 64,
        account_alias=_ALIAS,
        instrument_handle=_HANDLE_A,
        decision_at=_START,
        fills=(
            _fill(
                "6",
                role="entry",
                side=entry_side,
                quantity="2",
                price="100",
                at=_START + timedelta(minutes=1),
            ),
            _fill(
                "7",
                role="exit",
                side=exit_side,
                quantity="1",
                price="101",
                at=_START + timedelta(minutes=2),
            ),
            _fill(
                "8",
                role="entry",
                side=entry_side,
                quantity="1",
                price="99",
                at=_START + timedelta(minutes=3),
            ),
            _fill(
                "9",
                role="exit",
                side=exit_side,
                quantity="2",
                price="102",
                at=_START + timedelta(minutes=4),
            ),
        ),
        costs=Decimal(0),
        cost_currency="USD",
        counterfactual_at=_START + timedelta(minutes=5),
        counterfactual_price=Decimal(100),
        decision_quote=None,
        decision_bar=None,
    )

    assert len(trade.fills) == 4


@pytest.mark.parametrize(
    "fills",
    [
        (
            _fill(
                "a",
                role="exit",
                side="sell",
                quantity="1",
                price="101",
                at=_START + timedelta(minutes=1),
            ),
            _fill(
                "b",
                role="entry",
                side="buy",
                quantity="1",
                price="100",
                at=_START + timedelta(minutes=2),
            ),
        ),
        (
            _fill(
                "c",
                role="entry",
                side="sell",
                quantity="1",
                price="100",
                at=_START + timedelta(minutes=1),
            ),
            _fill(
                "d",
                role="exit",
                side="buy",
                quantity="2",
                price="99",
                at=_START + timedelta(minutes=2),
            ),
            _fill(
                "e",
                role="entry",
                side="sell",
                quantity="1",
                price="98",
                at=_START + timedelta(minutes=3),
            ),
        ),
    ],
    ids=("exit_before_entry", "over_exit_before_later_entry"),
)
def test_exit_before_entry_or_over_exit_fails_closed(
    fills: tuple[TradeFill, ...],
) -> None:
    with pytest.raises(ValidationError, match="chronological position inventory"):
        ClosedTrade(
            trade_key_sha256="f" * 64,
            account_alias=_ALIAS,
            instrument_handle=_HANDLE_A,
            decision_at=_START,
            fills=fills,
            costs=Decimal(0),
            cost_currency="USD",
            counterfactual_at=_START + timedelta(minutes=5),
            counterfactual_price=Decimal(100),
            decision_quote=None,
            decision_bar=None,
        )


def test_trade_review_money_is_owner_only_and_public_evidence_is_redacted() -> None:
    public = analyze_trading_mirror(
        _dataset((_winner(quote=_quote()),)),
        visibility=VisibilityMode.PUBLIC_EVIDENCE,
        trusted_local_host=False,
    )

    assert not isinstance(public, ResearchRefusal)
    assert public.private_values is None
    serialized = public.evidence.model_dump_json()
    for private_field in (
        "entry_vwap",
        "exit_vwap",
        "profit_loss",
        "turnover",
        "cost",
        "counterfactual",
        "currency",
    ):
        assert private_field not in serialized
