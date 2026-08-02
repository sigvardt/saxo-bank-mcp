from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from saxo_bank_mcp.analytics_fx import FxQuote, convert_amount

type CashFlowKind = Literal[
    "deposit",
    "withdrawal",
    "fee",
    "financing",
    "tax",
    "income",
]

_CASH_FLOW_KINDS: Final = frozenset(
    {"deposit", "withdrawal", "fee", "financing", "tax", "income"},
)
_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")
_PORTFOLIO_SIGNS: Final[dict[str, Decimal]] = {
    "deposit": Decimal(1),
    "withdrawal": Decimal(-1),
    "fee": Decimal(-1),
    "financing": Decimal(-1),
    "tax": Decimal(-1),
    "income": Decimal(1),
}
_EXTERNAL_PORTFOLIO_SIGNS: Final[dict[str, Decimal]] = {
    "deposit": Decimal(1),
    "withdrawal": Decimal(-1),
    "fee": Decimal(0),
    "financing": Decimal(0),
    "tax": Decimal(0),
    "income": Decimal(0),
}


@dataclass(frozen=True, slots=True)
class CashFlowEvent:
    """A magnitude-classified economic event before reporting-currency conversion."""

    economic_at: datetime
    kind: CashFlowKind
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        """Validate magnitude convention, currency code, and UTC timing."""
        if self.kind not in _CASH_FLOW_KINDS:
            raise ValueError("cash-flow kind is unsupported")
        _require_cash_flow_amount(self.amount)
        if self.amount < 0:
            raise ValueError("cash-flow amount must be a nonnegative magnitude")
        if _CURRENCY_PATTERN.fullmatch(self.currency) is None:
            raise ValueError("cash-flow currency must be an uppercase ISO-style code")
        if self.economic_at.tzinfo is None or self.economic_at.utcoffset() != timedelta(0):
            raise ValueError("cash-flow timestamp must use UTC")


@dataclass(frozen=True, slots=True)
class NormalizedCashFlow:
    """One reporting-currency event with explicit portfolio and investor signs."""

    economic_at: datetime
    kind: CashFlowKind
    reporting_currency: str
    reporting_amount: Decimal
    external_portfolio_flow: Decimal
    investor_cash_flow: Decimal


def normalize_cash_flows(
    events: Sequence[CashFlowEvent],
    *,
    reporting_currency: str,
    quotes: Sequence[FxQuote],
    minor_unit: Decimal | None = None,
) -> tuple[NormalizedCashFlow, ...]:
    """Normalize rows in stable economic order without treating internal charges as flows."""
    if not events:
        raise ValueError("cash-flow normalization requires at least one event")
    if _CURRENCY_PATTERN.fullmatch(reporting_currency) is None:
        raise ValueError("reporting currency must be an uppercase ISO-style code")
    ordered = sorted(
        enumerate(events),
        key=lambda item: (item[1].economic_at, item[0]),
    )
    normalized: list[NormalizedCashFlow] = []
    for _, event in ordered:
        magnitude = convert_amount(
            event.amount,
            event.currency,
            reporting_currency,
            at=event.economic_at,
            quotes=quotes,
            minor_unit=minor_unit,
        )
        portfolio_amount = magnitude * _PORTFOLIO_SIGNS[event.kind]
        external_portfolio_flow = magnitude * _EXTERNAL_PORTFOLIO_SIGNS[event.kind]
        normalized.append(
            NormalizedCashFlow(
                economic_at=event.economic_at,
                kind=event.kind,
                reporting_currency=reporting_currency,
                reporting_amount=portfolio_amount,
                external_portfolio_flow=external_portfolio_flow,
                investor_cash_flow=-external_portfolio_flow,
            ),
        )
    return tuple(normalized)


def _require_cash_flow_amount(value: object) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("cash-flow amount must be a finite Decimal")


__all__ = (
    "CashFlowEvent",
    "CashFlowKind",
    "NormalizedCashFlow",
    "normalize_cash_flows",
)
