from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")


class FxNormalizationError(ValueError):
    """Raised when an exact Saxo-only currency conversion cannot be proved."""


@dataclass(frozen=True, slots=True)
class FxQuote:
    """One quote where one base unit equals ``rate`` quote-currency units."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        """Validate quote direction, rate precision, and UTC timing."""
        _require_currency(self.base_currency)
        _require_currency(self.quote_currency)
        if self.base_currency == self.quote_currency:
            raise ValueError("FX quote currencies must differ")
        _require_decimal(self.rate, "FX rate")
        if self.rate <= 0:
            raise ValueError("FX rate must be positive")
        _require_utc(self.observed_at, "FX quote timestamp")


def convert_amount(  # noqa: PLR0913
    amount: Decimal,
    source_currency: str,
    target_currency: str,
    *,
    at: datetime,
    quotes: Sequence[FxQuote],
    minor_unit: Decimal | None = None,
) -> Decimal:
    """Convert one Decimal amount with the latest eligible direct or reciprocal quote."""
    _require_decimal(amount, "money amount")
    _require_currency(source_currency)
    _require_currency(target_currency)
    _require_utc(at, "conversion timestamp")
    if source_currency == target_currency:
        return _round_minor_unit(amount, minor_unit)

    eligible = tuple(
        quote
        for quote in quotes
        if quote.observed_at <= at
        and {
            quote.base_currency,
            quote.quote_currency,
        }
        == {source_currency, target_currency}
    )
    if not eligible:
        raise FxNormalizationError("eligible FX quote is missing")
    latest_at = max(quote.observed_at for quote in eligible)
    latest = tuple(quote for quote in eligible if quote.observed_at == latest_at)
    converted = tuple(
        _apply_quote(amount, source_currency, target_currency, quote) for quote in latest
    )
    if any(value != converted[0] for value in converted[1:]):
        raise FxNormalizationError("latest FX quote direction is ambiguous")
    return _round_minor_unit(converted[0], minor_unit)


def normalize_money_amounts(  # noqa: PLR0913
    amounts: Sequence[Decimal],
    currencies: Sequence[str],
    economic_times: Sequence[datetime],
    *,
    reporting_currency: str,
    quotes: Sequence[FxQuote],
    minor_unit: Decimal | None = None,
) -> tuple[Decimal, ...]:
    """Convert aligned money rows at each row's own economic timestamp."""
    if not amounts or len(amounts) != len(currencies) or len(amounts) != len(economic_times):
        raise FxNormalizationError("money rows must be non-empty and exactly aligned")
    return tuple(
        convert_amount(
            amount,
            currency,
            reporting_currency,
            at=economic_at,
            quotes=quotes,
            minor_unit=minor_unit,
        )
        for amount, currency, economic_at in zip(
            amounts,
            currencies,
            economic_times,
            strict=True,
        )
    )


def _apply_quote(
    amount: Decimal,
    source_currency: str,
    target_currency: str,
    quote: FxQuote,
) -> Decimal:
    if source_currency == quote.base_currency and target_currency == quote.quote_currency:
        return amount * quote.rate
    if source_currency == quote.quote_currency and target_currency == quote.base_currency:
        return amount / quote.rate
    raise FxNormalizationError("FX quote does not match the requested currency direction")


def _round_minor_unit(value: Decimal, minor_unit: Decimal | None) -> Decimal:
    if minor_unit is None:
        return value
    _require_decimal(minor_unit, "currency minor unit")
    if minor_unit <= 0:
        raise FxNormalizationError("currency minor unit must be positive")
    units = (value / minor_unit).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return units * minor_unit


def _require_decimal(value: object, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")


def _require_currency(value: str) -> None:
    if _CURRENCY_PATTERN.fullmatch(value) is None:
        raise ValueError("currency must be an uppercase ISO-style code")


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC")


__all__ = (
    "FxNormalizationError",
    "FxQuote",
    "convert_amount",
    "normalize_money_amounts",
)
