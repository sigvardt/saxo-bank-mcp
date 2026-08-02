from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_fx import FxNormalizationError, FxQuote, convert_amount
from saxo_bank_mcp.analytics_instruments import ResearchRefusal, ResearchStatus
from saxo_bank_mcp.analytics_models import (
    ContractName,
    DatasetId,
    InstrumentHandle,
    IsoCurrencyCode,
    QualityState,
    SafeAccountScope,
    Sha256Fingerprint,
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
_BASE_SOURCE_CONTRACTS: Final = (
    "transactions_v1",
    "closed_positions_history_v1",
)
_UNUSABLE_QUALITY: Final = frozenset(
    {QualityState.MISSING, QualityState.INVALID, QualityState.STALE},
)

type TradeRole = Literal["entry", "exit"]
type TradeSide = Literal["buy", "sell"]
type TradeDirection = Literal["long", "short"]
type QuoteEntitlement = Literal["available", "partial", "denied"]
type ExecutionEvidenceClass = Literal[
    "exact_mcp_decision_quote",
    "bar_approximation",
    "unavailable",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class DecisionPointQuote(_StrictModel):
    """One quality-bound decision quote captured through the MCP source boundary."""

    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    captured_at: UtcDateTime
    bid: Decimal = Field(gt=0, allow_inf_nan=False)
    ask: Decimal = Field(gt=0, allow_inf_nan=False)
    price_type: str = Field(min_length=1, max_length=64)
    delayed_by_minutes: int | None = Field(default=None, ge=0)
    quality_state: QualityState
    entitlement_state: QuoteEntitlement
    source_binding: SaxoSourceBinding
    captured_by_mcp: bool
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_quote(self) -> Self:
        if self.ask < self.bid:
            raise ValueError("decision quote ask cannot be below bid")
        return self


class DecisionBarReference(_StrictModel):
    """Nearest Saxo chart bar, explicitly unsuitable for exact quote claims."""

    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    decision_at: UtcDateTime
    bar_start_at: UtcDateTime
    bar_end_at: UtcDateTime
    close_price: Decimal = Field(gt=0, allow_inf_nan=False)
    source_binding: SaxoSourceBinding

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if self.bar_end_at < self.bar_start_at:
            raise ValueError("decision bar end cannot precede its start")
        if not self.bar_start_at <= self.decision_at <= self.bar_end_at:
            raise ValueError("decision timestamp must fall within the approximation bar")
        return self


class TradeFill(_StrictModel):
    """One revisioned partial fill with signed role expressed separately from magnitude."""

    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    event_key_sha256: Sha256Fingerprint
    revision: int = Field(ge=1)
    occurred_at: UtcDateTime
    role: TradeRole
    side: TradeSide
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    currency: IsoCurrencyCode


class ClosedTrade(_StrictModel):
    """One closed long or short trade including corrected partial fills."""

    trade_key_sha256: Sha256Fingerprint
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle
    decision_at: UtcDateTime
    fills: tuple[TradeFill, ...] = Field(min_length=2)
    costs: Decimal = Field(ge=0, allow_inf_nan=False)
    cost_currency: IsoCurrencyCode
    counterfactual_at: UtcDateTime
    counterfactual_price: Decimal = Field(gt=0, allow_inf_nan=False)
    decision_quote: DecisionPointQuote | None
    decision_bar: DecisionBarReference | None

    @model_validator(mode="after")
    def validate_trade(self) -> Self:  # noqa: C901
        if any(fill.account_alias != self.account_alias for fill in self.fills):
            raise ValueError("trade fill account alias must match the trade account alias")
        if any(fill.instrument_handle != self.instrument_handle for fill in self.fills):
            raise ValueError("trade fills must match the trade instrument handle")
        if any(fill.occurred_at < self.decision_at for fill in self.fills):
            raise ValueError("trade fills cannot precede the bounded decision timestamp")
        revisions = tuple((fill.event_key_sha256, fill.revision) for fill in self.fills)
        if len(revisions) != len(set(revisions)):
            raise ValueError("trade fill revisions must be unique")
        latest = _latest_fills(self.fills)
        entries = tuple(fill for fill in latest if fill.role == "entry")
        exits = tuple(fill for fill in latest if fill.role == "exit")
        if not entries or not exits:
            raise ValueError("closed trades require entry and exit fills")
        entry_sides = {fill.side for fill in entries}
        exit_sides = {fill.side for fill in exits}
        if len(entry_sides) != 1 or len(exit_sides) != 1 or entry_sides == exit_sides:
            raise ValueError("closed trade entry and exit sides must be exact opposites")
        entry_quantity = sum((fill.quantity for fill in entries), Decimal(0))
        exit_quantity = sum((fill.quantity for fill in exits), Decimal(0))
        if entry_quantity != exit_quantity:
            raise ValueError("closed trade entry and exit quantities must reconcile")
        if len({fill.currency for fill in latest}) != 1:
            raise ValueError("one closed trade must use one instrument currency")
        if self.counterfactual_at < max(fill.occurred_at for fill in exits):
            raise ValueError("do-nothing counterfactual cannot precede the trade exit")
        if self.decision_quote is not None and (
            self.decision_quote.instrument_handle != self.instrument_handle
        ):
            raise ValueError("decision quote must match the trade instrument handle")
        if self.decision_bar is not None and (
            self.decision_bar.instrument_handle != self.instrument_handle
            or self.decision_bar.decision_at != self.decision_at
        ):
            raise ValueError("decision bar must match the trade and decision timestamp")
        return self


class TradeReviewDataset(_StrictModel):
    """Bounded post-session trade history for one safe account alias."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    start_at: UtcDateTime
    end_at: UtcDateTime
    reporting_currency: IsoCurrencyCode
    trades: tuple[ClosedTrade, ...] = Field(min_length=1)
    fx_quotes: tuple[FxQuote, ...]
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[ContractName, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if self.end_at < self.start_at:
            raise ValueError("trade review end cannot precede its start")
        if any(trade.account_alias != self.account_alias for trade in self.trades):
            raise ValueError("trade account alias must match the review account alias")
        if any(
            trade.decision_at < self.start_at or trade.counterfactual_at > self.end_at
            for trade in self.trades
        ):
            raise ValueError("trade material must fall within the review period")
        keys = tuple(trade.trade_key_sha256 for trade in self.trades)
        if len(keys) != len(set(keys)):
            raise ValueError("closed trade keys must be unique")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial trade-review quality must match explicit missing fields")
        return self


class ExecutionQualityEvidence(_StrictModel):
    """Exact quote evidence, a labeled bar reference, or an unavailable metric."""

    evidence_class: ExecutionEvidenceClass
    arrival_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    midpoint: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    spread: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    reference_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    execution_difference_per_unit: Decimal | None = Field(
        default=None,
        allow_inf_nan=False,
    )
    approximation_rule: Literal["nearest_saxo_chart_bar_close"] | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        exact_values = (self.arrival_price, self.midpoint, self.spread)
        if self.evidence_class == "exact_mcp_decision_quote":
            if any(value is None for value in exact_values) or any(
                value is not None
                for value in (self.reference_price, self.approximation_rule)
            ):
                raise ValueError("exact execution evidence requires only exact quote fields")
        elif self.evidence_class == "bar_approximation":
            if any(value is not None for value in exact_values) or (
                self.reference_price is None or self.approximation_rule is None
            ):
                raise ValueError("bar evidence must keep exact quote fields unavailable")
        elif any(
            value is not None
            for value in (
                *exact_values,
                self.reference_price,
                self.execution_difference_per_unit,
                self.approximation_rule,
            )
        ):
            raise ValueError("unavailable execution evidence cannot carry values")
        return self


class PrivateTradeReview(_StrictModel):
    """Owner-only result for one closed trade."""

    instrument_handle: InstrumentHandle
    direction: TradeDirection
    price_currency: IsoCurrencyCode
    reporting_currency: IsoCurrencyCode
    fill_count: int = Field(ge=2)
    entry_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    entry_vwap: Decimal = Field(gt=0, allow_inf_nan=False)
    exit_vwap: Decimal = Field(gt=0, allow_inf_nan=False)
    turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    costs: Decimal = Field(ge=0, allow_inf_nan=False)
    gross_profit_loss: Decimal = Field(allow_inf_nan=False)
    net_profit_loss: Decimal = Field(allow_inf_nan=False)
    holding_hours: Decimal = Field(ge=0, allow_inf_nan=False)
    averaging_down_events: int = Field(ge=0)
    do_nothing_counterfactual_gross_profit_loss: Decimal = Field(allow_inf_nan=False)
    execution_quality: ExecutionQualityEvidence


class SessionCockpit(_StrictModel):
    """Owner-only bounded session totals without any execution capability."""

    trade_count: int = Field(ge=1)
    fill_count: int = Field(ge=2)
    turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    costs: Decimal = Field(ge=0, allow_inf_nan=False)
    gross_profit_loss: Decimal = Field(allow_inf_nan=False)
    net_profit_loss: Decimal = Field(allow_inf_nan=False)


class PostSessionReportCard(_StrictModel):
    """Descriptive behavior and outcome measures, never a causal diagnosis."""

    winners: int = Field(ge=0)
    losers: int = Field(ge=0)
    win_rate_percentage: Decimal = Field(ge=0, le=100, allow_inf_nan=False)
    average_winner: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    average_loser: Decimal | None = Field(default=None, lt=0, allow_inf_nan=False)
    winner_loser_asymmetry: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    average_holding_hours: Decimal = Field(ge=0, allow_inf_nan=False)
    averaging_down_events: int = Field(ge=0)
    do_nothing_counterfactual_gross_profit_loss: Decimal = Field(allow_inf_nan=False)
    exact_quote_reviews: int = Field(ge=0)
    bar_approximation_reviews: int = Field(ge=0)


class PrivateTradingMirror(_StrictModel):
    """Owner-only session cockpit, trade rows, and post-session report card."""

    reporting_currency: IsoCurrencyCode
    trades: tuple[PrivateTradeReview, ...]
    session_cockpit: SessionCockpit
    report_card: PostSessionReportCard


class TradingMirrorResult(_StrictModel):
    """Trading mirror with value-free public evidence and explicit limitations."""

    status: Literal[ResearchStatus.COMPLETE, ResearchStatus.REDUCED]
    analysis_kind: Literal["trading_mirror"] = "trading_mirror"
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    source_scope: Literal["saxo_openapi"] = _SOURCE_SCOPE
    visibility: VisibilityMode
    private_values: PrivateTradingMirror | None
    warnings: tuple[ContractName, ...]
    does_not_verify: tuple[Literal["causal_explanation", "future_performance"], ...]
    evidence: PortfolioPublicEvidence

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if private != (self.private_values is not None):
            raise ValueError("trade-review values do not match their delivery visibility")
        return self


def analyze_trading_mirror(  # noqa: C901, PLR0912
    dataset: TradeReviewDataset,
    *,
    visibility: VisibilityMode,
    trusted_local_host: bool,
) -> TradingMirrorResult | ResearchRefusal:
    """Build a descriptive session report from bounded Saxo trade material."""
    private_delivery = require_delivery_boundary(
        visibility,
        trusted_local_host=trusted_local_host,
    )
    handles = tuple(trade.instrument_handle for trade in dataset.trades)
    source_assessment = assess_source_bindings(
        dataset.source_bindings,
        required_contract_ids=_BASE_SOURCE_CONTRACTS,
        analysis_kind="trading_mirror",
        dataset_id=dataset.dataset_id,
        instrument_handles=handles,
    )
    if isinstance(source_assessment, ResearchRefusal):
        return source_assessment
    if dataset.quality_state in _UNUSABLE_QUALITY:
        return _refusal(
            dataset,
            "trade_review_data_unusable",
            "trade-review inputs are missing, invalid, or stale",
        )
    warnings = set(dataset.warnings)
    warnings.update(source_assessment)
    evidence_bindings = list(dataset.source_bindings)
    try:
        rows: list[PrivateTradeReview] = []
        for trade in dataset.trades:
            reference_bindings = tuple(
                (reference.source_binding, contract_id)
                for reference, contract_id in (
                    (trade.decision_quote, "info_price_v1"),
                    (trade.decision_bar, "chart_v3"),
                )
                if reference is not None
            )
            for reference_binding, contract_id in reference_bindings:
                if reference_binding.contract_id != contract_id:
                    return _refusal(
                        dataset,
                        "execution_source_binding_invalid",
                        "execution references require exact quote or chart source bindings",
                    )
                reference_assessment = assess_source_bindings(
                    (reference_binding,),
                    required_contract_ids=(contract_id,),
                    analysis_kind="execution_quality",
                    dataset_id=dataset.dataset_id,
                    instrument_handles=(trade.instrument_handle,),
                )
                if isinstance(reference_assessment, ResearchRefusal):
                    return reference_assessment
                warnings.update(reference_assessment)
                evidence_bindings.append(reference_binding)
            execution = classify_execution_evidence(
                trade,
                refusal_dataset_id=dataset.dataset_id,
            )
            if isinstance(execution, ResearchRefusal):
                warnings.add(execution.reason_code)
                execution_evidence = unavailable_execution_evidence()
            else:
                execution_evidence, evidence_warnings = execution
                warnings.update(evidence_warnings)
            rows.append(_trade_review(trade, dataset, execution_evidence))
    except FxNormalizationError:
        return _refusal(
            dataset,
            "trade_review_fx_basis_missing",
            "an eligible Saxo FX conversion is unavailable",
        )
    if dataset.quality_state is QualityState.PARTIAL:
        warnings.add("partial_trade_history")
    private_values = _trading_mirror_values(dataset.reporting_currency, tuple(rows))
    evidence_dataset_ids = {dataset.dataset_id}
    for trade in dataset.trades:
        if trade.decision_quote is not None:
            evidence_dataset_ids.add(trade.decision_quote.dataset_id)
        if trade.decision_bar is not None:
            evidence_dataset_ids.add(trade.decision_bar.dataset_id)
    return TradingMirrorResult(
        status=ResearchStatus.REDUCED if warnings else ResearchStatus.COMPLETE,
        dataset_id=dataset.dataset_id,
        account_alias=dataset.account_alias,
        visibility=visibility,
        private_values=private_values if private_delivery else None,
        warnings=tuple(sorted(warnings)),
        does_not_verify=("causal_explanation", "future_performance"),
        evidence=build_public_evidence(
            analysis_kind="trading_mirror",
            dataset_ids=tuple(sorted(evidence_dataset_ids)),
            account_aliases=(dataset.account_alias,),
            source_bindings=tuple(evidence_bindings),
            material=dataset,
        ),
    )


def classify_execution_evidence(
    trade: ClosedTrade,
    *,
    refusal_dataset_id: str,
) -> tuple[ExecutionQualityEvidence, tuple[str, ...]] | ResearchRefusal:
    """Use an exact MCP quote, else a labeled Saxo bar, else refuse the metric."""
    latest = _latest_fills(trade.fills)
    entries = tuple(fill for fill in latest if fill.role == "entry")
    entry_quantity = sum((fill.quantity for fill in entries), Decimal(0))
    entry_vwap = sum(
        (fill.quantity * fill.price for fill in entries),
        Decimal(0),
    ) / entry_quantity
    return classify_decision_reference(
        decision_at=trade.decision_at,
        instrument_handle=trade.instrument_handle,
        observed_price=entry_vwap,
        side=entries[0].side,
        decision_quote=trade.decision_quote,
        decision_bar=trade.decision_bar,
        refusal_dataset_id=refusal_dataset_id,
        allow_unavailable=False,
    )


def classify_decision_reference(  # noqa: PLR0913
    *,
    decision_at: UtcDateTime,
    instrument_handle: str,
    observed_price: Decimal,
    side: TradeSide,
    decision_quote: DecisionPointQuote | None,
    decision_bar: DecisionBarReference | None,
    refusal_dataset_id: str,
    allow_unavailable: bool,
) -> tuple[ExecutionQualityEvidence, tuple[str, ...]] | ResearchRefusal:
    """Apply the shared exact-quote, labeled-bar, and unavailable evidence gate."""
    direction: TradeDirection = "long" if side == "buy" else "short"
    quote = decision_quote
    binding = None if quote is None else quote.source_binding
    quote_is_exact = (
        quote is not None
        and quote.captured_by_mcp
        and quote.instrument_handle == instrument_handle
        and quote.captured_at == decision_at
        and quote.quality_state is QualityState.COMPLETE
        and quote.entitlement_state == "available"
        and quote.delayed_by_minutes in {None, 0}
        and quote.price_type.replace(" ", "").casefold() == "tradable"
        and not quote.warnings
        and binding is not None
        and binding.quality_state is QualityState.COMPLETE
        and binding.entitlement_state == "available"
        and binding.contract_id == "info_price_v1"
    )
    if quote_is_exact and quote is not None:
        midpoint = (quote.bid + quote.ask) / Decimal(2)
        return (
            ExecutionQualityEvidence(
                evidence_class="exact_mcp_decision_quote",
                arrival_price=midpoint,
                midpoint=midpoint,
                spread=quote.ask - quote.bid,
                reference_price=None,
                execution_difference_per_unit=(
                    observed_price - midpoint
                    if direction == "long"
                    else midpoint - observed_price
                ),
                approximation_rule=None,
            ),
            (),
        )
    bar = decision_bar
    if (
        bar is not None
        and bar.instrument_handle == instrument_handle
        and bar.decision_at == decision_at
        and bar.source_binding.contract_id == "chart_v3"
        and bar.source_binding.entitlement_state != "denied"
        and bar.source_binding.quality_state not in _UNUSABLE_QUALITY
    ):
        return (
            ExecutionQualityEvidence(
                evidence_class="bar_approximation",
                arrival_price=None,
                midpoint=None,
                spread=None,
                reference_price=bar.close_price,
                execution_difference_per_unit=(
                    observed_price - bar.close_price
                    if direction == "long"
                    else bar.close_price - observed_price
                ),
                approximation_rule="nearest_saxo_chart_bar_close",
            ),
            ("bar_approximation", "exact_decision_quote_unavailable"),
        )
    if allow_unavailable:
        return (
            unavailable_execution_evidence(),
            ("decision_quote_metric_unavailable",),
        )
    return ResearchRefusal(
        analysis_kind="execution_quality",
        reason_code="execution_reference_unavailable",
        reason="an exact MCP decision quote or bounded Saxo chart bar is required",
        dataset_ids=(refusal_dataset_id,),
        instrument_handles=(instrument_handle,),
        missing_fields=("decision_quote", "decision_bar"),
        source_scope=None,
    )


def unavailable_execution_evidence() -> ExecutionQualityEvidence:
    """Return a value-free marker used when a containing analysis reduces the metric."""
    return ExecutionQualityEvidence(
        evidence_class="unavailable",
        arrival_price=None,
        midpoint=None,
        spread=None,
        reference_price=None,
        execution_difference_per_unit=None,
        approximation_rule=None,
    )


def _trade_review(
    trade: ClosedTrade,
    dataset: TradeReviewDataset,
    execution: ExecutionQualityEvidence,
) -> PrivateTradeReview:
    latest = _latest_fills(trade.fills)
    entries = tuple(fill for fill in latest if fill.role == "entry")
    exits = tuple(fill for fill in latest if fill.role == "exit")
    entry_quantity = sum((fill.quantity for fill in entries), Decimal(0))
    entry_vwap = _vwap(entries)
    exit_vwap = _vwap(exits)
    direction = _direction(entries[0])
    sign = Decimal(1) if direction == "long" else Decimal(-1)
    price_currency = entries[0].currency
    exit_at = max(fill.occurred_at for fill in exits)
    gross_native = sign * (exit_vwap - entry_vwap) * entry_quantity
    gross = convert_amount(
        gross_native,
        price_currency,
        dataset.reporting_currency,
        at=exit_at,
        quotes=dataset.fx_quotes,
    )
    costs = convert_amount(
        trade.costs,
        trade.cost_currency,
        dataset.reporting_currency,
        at=exit_at,
        quotes=dataset.fx_quotes,
    )
    turnover = sum(
        (
            convert_amount(
                fill.quantity * fill.price,
                fill.currency,
                dataset.reporting_currency,
                at=fill.occurred_at,
                quotes=dataset.fx_quotes,
            )
            for fill in latest
        ),
        Decimal(0),
    )
    counterfactual = convert_amount(
        sign * (trade.counterfactual_price - entry_vwap) * entry_quantity,
        price_currency,
        dataset.reporting_currency,
        at=trade.counterfactual_at,
        quotes=dataset.fx_quotes,
    )
    return PrivateTradeReview(
        instrument_handle=trade.instrument_handle,
        direction=direction,
        price_currency=price_currency,
        reporting_currency=dataset.reporting_currency,
        fill_count=len(latest),
        entry_quantity=entry_quantity,
        entry_vwap=entry_vwap,
        exit_vwap=exit_vwap,
        turnover=turnover,
        costs=costs,
        gross_profit_loss=gross,
        net_profit_loss=gross - costs,
        holding_hours=_duration_hours(
            max(fill.occurred_at for fill in exits)
            - min(fill.occurred_at for fill in entries),
        ),
        averaging_down_events=_averaging_down_events(entries, direction),
        do_nothing_counterfactual_gross_profit_loss=counterfactual,
        execution_quality=execution,
    )


def _trading_mirror_values(
    reporting_currency: str,
    rows: tuple[PrivateTradeReview, ...],
) -> PrivateTradingMirror:
    winners = tuple(row.net_profit_loss for row in rows if row.net_profit_loss > 0)
    losers = tuple(row.net_profit_loss for row in rows if row.net_profit_loss < 0)
    average_winner = _mean(winners)
    average_loser = _mean(losers)
    asymmetry = (
        None
        if average_winner is None or average_loser is None
        else average_winner / abs(average_loser)
    )
    costs = sum((row.costs for row in rows), Decimal(0))
    gross = sum((row.gross_profit_loss for row in rows), Decimal(0))
    return PrivateTradingMirror(
        reporting_currency=reporting_currency,
        trades=rows,
        session_cockpit=SessionCockpit(
            trade_count=len(rows),
            fill_count=sum(row.fill_count for row in rows),
            turnover=sum((row.turnover for row in rows), Decimal(0)),
            costs=costs,
            gross_profit_loss=gross,
            net_profit_loss=gross - costs,
        ),
        report_card=PostSessionReportCard(
            winners=len(winners),
            losers=len(losers),
            win_rate_percentage=Decimal(len(winners)) / Decimal(len(rows)) * Decimal(100),
            average_winner=average_winner,
            average_loser=average_loser,
            winner_loser_asymmetry=asymmetry,
            average_holding_hours=sum(
                (row.holding_hours for row in rows),
                Decimal(0),
            )
            / Decimal(len(rows)),
            averaging_down_events=sum(row.averaging_down_events for row in rows),
            do_nothing_counterfactual_gross_profit_loss=sum(
                (row.do_nothing_counterfactual_gross_profit_loss for row in rows),
                Decimal(0),
            ),
            exact_quote_reviews=sum(
                row.execution_quality.evidence_class == "exact_mcp_decision_quote"
                for row in rows
            ),
            bar_approximation_reviews=sum(
                row.execution_quality.evidence_class == "bar_approximation" for row in rows
            ),
        ),
    )


def _latest_fills(fills: Sequence[TradeFill]) -> tuple[TradeFill, ...]:
    latest: dict[str, TradeFill] = {}
    for fill in fills:
        current = latest.get(fill.event_key_sha256)
        if current is None or fill.revision > current.revision:
            latest[fill.event_key_sha256] = fill
    return tuple(
        sorted(
            latest.values(),
            key=lambda fill: (fill.occurred_at, fill.event_key_sha256),
        ),
    )


def _vwap(fills: Sequence[TradeFill]) -> Decimal:
    quantity = sum((fill.quantity for fill in fills), Decimal(0))
    return sum((fill.quantity * fill.price for fill in fills), Decimal(0)) / quantity


def _direction(entry: TradeFill) -> TradeDirection:
    return "long" if entry.side == "buy" else "short"


def _averaging_down_events(
    entries: Sequence[TradeFill],
    direction: TradeDirection,
) -> int:
    ordered = tuple(sorted(entries, key=lambda fill: (fill.occurred_at, fill.event_key_sha256)))
    total_quantity = ordered[0].quantity
    weighted_price = ordered[0].quantity * ordered[0].price
    events = 0
    for fill in ordered[1:]:
        running_vwap = weighted_price / total_quantity
        if (direction == "long" and fill.price < running_vwap) or (
            direction == "short" and fill.price > running_vwap
        ):
            events += 1
        total_quantity += fill.quantity
        weighted_price += fill.quantity * fill.price
    return events


def _duration_hours(value: timedelta) -> Decimal:
    seconds = (
        Decimal(value.days * 86_400 + value.seconds)
        + Decimal(value.microseconds) / Decimal(1_000_000)
    )
    return seconds / Decimal(3_600)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return None if not values else sum(values, Decimal(0)) / Decimal(len(values))


def _refusal(
    dataset: TradeReviewDataset,
    reason_code: str,
    reason: str,
) -> ResearchRefusal:
    return ResearchRefusal(
        analysis_kind="trading_mirror",
        reason_code=reason_code,
        reason=reason,
        dataset_ids=(dataset.dataset_id,),
        instrument_handles=tuple(trade.instrument_handle for trade in dataset.trades),
        source_scope=None,
    )


__all__ = (
    "ClosedTrade",
    "DecisionBarReference",
    "DecisionPointQuote",
    "ExecutionQualityEvidence",
    "PostSessionReportCard",
    "PrivateTradeReview",
    "PrivateTradingMirror",
    "SessionCockpit",
    "TradeFill",
    "TradeReviewDataset",
    "TradingMirrorResult",
    "analyze_trading_mirror",
    "classify_decision_reference",
    "classify_execution_evidence",
    "unavailable_execution_evidence",
)
