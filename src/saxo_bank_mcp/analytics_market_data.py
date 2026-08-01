from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_models import InstrumentHandle

_INSTRUMENT_HANDLE_ADAPTER: Final[TypeAdapter[InstrumentHandle]] = TypeAdapter(
    InstrumentHandle,
)
_MAX_PRICE_TYPE_LENGTH: Final = 64


class MarketDataError(RuntimeError):
    """Base error for normalized Saxo market data."""


class MarketDataValidationError(MarketDataError):
    """Raised when validated Saxo source rows cannot be normalized honestly."""


class ChartInterval(StrEnum):
    """Supported bounded Saxo chart horizons."""

    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    THIRTY_MINUTES = "30m"
    ONE_HOUR = "1h"
    ONE_DAY = "1d"

    @property
    def minutes(self) -> int:
        """Return the Saxo chart horizon in minutes."""
        return {
            ChartInterval.ONE_MINUTE: 1,
            ChartInterval.FIVE_MINUTES: 5,
            ChartInterval.FIFTEEN_MINUTES: 15,
            ChartInterval.THIRTY_MINUTES: 30,
            ChartInterval.ONE_HOUR: 60,
            ChartInterval.ONE_DAY: 1_440,
        }[self]

    @property
    def delta(self) -> timedelta:
        """Return the exact interval duration used for request bounds."""
        return timedelta(minutes=self.minutes)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class NormalizedPriceBar(_StrictModel):
    """One normalized unadjusted Saxo chart bar."""

    instrument_handle: InstrumentHandle
    bar_time: datetime
    interval: ChartInterval
    open_value: float | None = Field(allow_inf_nan=False)
    high_value: float | None = Field(allow_inf_nan=False)
    low_value: float | None = Field(allow_inf_nan=False)
    close_value: float = Field(allow_inf_nan=False)
    volume_value: float | None = Field(allow_inf_nan=False)
    price_type: str | None = Field(max_length=64)
    adjusted: Literal[False] = False


class NormalizedPriceSeries(_StrictModel):
    """Normalized bars plus explicit quality and return labels."""

    bars: tuple[NormalizedPriceBar, ...]
    missing_interval_count: int = Field(ge=0)
    warnings: tuple[str, ...]
    return_series_label: Literal["price_return"] = "price_return"
    adjustment_status: Literal["unadjusted"] = "unadjusted"
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class NormalizedQuote(_StrictModel):
    """One Saxo quote with explicit delay and freshness state."""

    instrument_handle: InstrumentHandle
    captured_at: datetime
    bid_value: float | None = Field(allow_inf_nan=False)
    ask_value: float | None = Field(allow_inf_nan=False)
    mid_value: float | None = Field(allow_inf_nan=False)
    delayed_by_minutes: int | None = Field(ge=0)
    price_type: str | None = Field(max_length=64)
    freshness: Literal["fresh", "stale"]
    warnings: tuple[str, ...]
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class NormalizedOptionReference(_StrictModel):
    """One complete option reference from an entitled Saxo root."""

    source_identifier: int = Field(ge=0)
    expiry: date
    strike_value: float = Field(allow_inf_nan=False)
    put_call: Literal["call", "put"]
    currency: None = None
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class NormalizedOptionChain(_StrictModel):
    """Complete option references plus explicit unavailable dimensions."""

    option_root_id: int = Field(ge=0)
    expiry: date
    options: tuple[NormalizedOptionReference, ...]
    warnings: tuple[str, ...]
    fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def normalize_price_series(
    *,
    rows: Sequence[Mapping[str, object]],
    instrument_handle: str,
    interval: ChartInterval,
    start: datetime,
    end: datetime,
) -> NormalizedPriceSeries:
    """Normalize bounded Saxo chart rows without inventing missing values."""
    handle = _validate_instrument_handle(instrument_handle)
    _require_utc_range(start, end)
    normalized: list[tuple[datetime, datetime, NormalizedPriceBar]] = []
    for row in rows:
        source_time = _required_timestamp(row.get("Time"))
        _require_interval_boundary(source_time, interval)
        bar_time = source_time.astimezone(UTC)
        if not start <= bar_time <= end:
            continue
        normalized.append(
            (
                source_time.replace(tzinfo=None),
                bar_time,
                NormalizedPriceBar(
                    instrument_handle=handle,
                    bar_time=bar_time,
                    interval=interval,
                    open_value=_optional_number(row.get("OpenBid")),
                    high_value=_optional_number(row.get("HighBid")),
                    low_value=_optional_number(row.get("LowBid")),
                    close_value=_required_number(row.get("CloseBid")),
                    volume_value=_optional_number(row.get("Volume")),
                    price_type=_optional_text(row.get("PriceType")),
                ),
            ),
        )
    normalized.sort(key=lambda item: item[1])
    utc_times = tuple(item[1] for item in normalized)
    if len(set(utc_times)) != len(utc_times):
        raise MarketDataValidationError("chart rows contain a duplicate bar timestamp")
    missing_intervals = _missing_intervals(
        tuple(item[0] for item in normalized),
        interval,
    )
    bars = tuple(item[2] for item in normalized)
    warnings: set[str] = set()
    if missing_intervals:
        warnings.add("observed_interval_gap")
    if any(bar.volume_value is None for bar in bars):
        warnings.add("volume_missing")
    fingerprint = _fingerprint(
        [bar.model_dump(mode="json") for bar in bars],
    )
    return NormalizedPriceSeries(
        bars=bars,
        missing_interval_count=missing_intervals,
        warnings=tuple(sorted(warnings)),
        fingerprint_sha256=fingerprint,
    )


def normalize_quote(
    *,
    row: Mapping[str, object],
    instrument_handle: str,
    captured_at: datetime,
    evaluated_at: datetime,
    max_age: timedelta,
) -> NormalizedQuote:
    """Normalize one quote without hiding delay, staleness, or missing values."""
    handle = _validate_instrument_handle(instrument_handle)
    _require_utc_instant(captured_at, "quote capture")
    _require_utc_instant(evaluated_at, "quote evaluation")
    if evaluated_at < captured_at:
        raise MarketDataValidationError("quote evaluation precedes its capture")
    if max_age <= timedelta(0):
        raise MarketDataValidationError("quote maximum age must be positive")
    quote_value = row.get("Quote")
    if quote_value is None:
        quote: Mapping[str, object] = {}
    elif isinstance(quote_value, Mapping):
        quote = cast("Mapping[str, object]", quote_value)
    else:
        raise MarketDataValidationError("quote payload is invalid")
    bid_value = _optional_number(quote.get("Bid"))
    ask_value = _optional_number(quote.get("Ask"))
    mid_value = _optional_number(quote.get("Mid"))
    delayed_by_minutes = _optional_nonnegative_integer(
        quote.get("DelayedByMinutes"),
    )
    price_type = _optional_text(quote.get("PriceType"))
    price_types = (
        price_type,
        _optional_text(row.get("PriceTypeAsk")),
        _optional_text(row.get("PriceTypeBid")),
    )
    warnings: set[str] = set()
    if (delayed_by_minutes or 0) > 0 or any(
        value is not None and "delay" in value.casefold() for value in price_types
    ):
        warnings.add("quote_delayed")
    freshness: Literal["fresh", "stale"] = (
        "stale" if evaluated_at - captured_at > max_age else "fresh"
    )
    if freshness == "stale":
        warnings.add("quote_stale")
    fingerprint = _fingerprint(
        {
            "ask_value": ask_value,
            "bid_value": bid_value,
            "captured_at": captured_at.isoformat(),
            "delayed_by_minutes": delayed_by_minutes,
            "instrument_handle": handle,
            "mid_value": mid_value,
            "price_type": price_type,
        },
    )
    return NormalizedQuote(
        instrument_handle=handle,
        captured_at=captured_at,
        bid_value=bid_value,
        ask_value=ask_value,
        mid_value=mid_value,
        delayed_by_minutes=delayed_by_minutes,
        price_type=price_type,
        freshness=freshness,
        warnings=tuple(sorted(warnings)),
        fingerprint_sha256=fingerprint,
    )


def normalize_option_chain(
    *,
    row: Mapping[str, object],
    expected_root_id: int,
    expiry: date,
) -> NormalizedOptionChain:
    """Normalize one single-expiry option reference response without inventing fields."""
    if type(expected_root_id) is not int or expected_root_id < 0:
        raise MarketDataValidationError("option root ID is invalid")
    if isinstance(expiry, datetime) or type(expiry) is not date:
        raise MarketDataValidationError("option expiry is invalid")
    if row.get("OptionRootId") != expected_root_id:
        raise MarketDataValidationError("option root ID does not match its request")
    available_expiries = _option_expiries(row.get("ExpiryDates"))
    if expiry not in available_expiries:
        raise MarketDataValidationError("requested option expiry is unavailable")
    warnings: set[str] = set()
    options: list[NormalizedOptionReference] = []
    identifiers: set[int] = set()
    for value in _specific_option_values(row.get("SpecificOptions")):
        option = _normalized_option_reference(value, expiry)
        if option is None:
            warnings.add("option_reference_incomplete")
            continue
        if option.source_identifier in identifiers:
            raise MarketDataValidationError("specific option identifier is duplicated")
        identifiers.add(option.source_identifier)
        options.append(option)
    options.sort(key=lambda item: (item.strike_value, item.put_call, item.source_identifier))
    if options:
        warnings.add("option_currency_missing")
    fingerprint = _fingerprint(
        [option.model_dump(mode="json") for option in options],
    )
    return NormalizedOptionChain(
        option_root_id=expected_root_id,
        expiry=expiry,
        options=tuple(options),
        warnings=tuple(sorted(warnings)),
        fingerprint_sha256=fingerprint,
    )


def _option_expiries(value: object) -> tuple[date, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise MarketDataValidationError("option expiry list is invalid")
    raw_values = cast("Sequence[object]", value)
    if any(not isinstance(item, str) for item in raw_values):
        raise MarketDataValidationError("option expiry list is invalid")
    text_values = cast("Sequence[str]", raw_values)
    try:
        return tuple(date.fromisoformat(item) for item in text_values)
    except ValueError as error:
        raise MarketDataValidationError("option expiry list is invalid") from error


def _specific_option_values(value: object) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise MarketDataValidationError("specific option list is invalid")
    raw_values = cast("Sequence[object]", value)
    if any(not isinstance(item, Mapping) for item in raw_values):
        raise MarketDataValidationError("specific option reference is invalid")
    return tuple(cast("Mapping[str, object]", item) for item in raw_values)


def _normalized_option_reference(
    value: Mapping[str, object],
    expiry: date,
) -> NormalizedOptionReference | None:
    source_identifier = value.get("Uic")
    strike_value = value.get("Strike")
    put_call_value = value.get("PutCall")
    if source_identifier is None or strike_value is None or put_call_value is None:
        return None
    if type(source_identifier) is not int or source_identifier < 0:
        raise MarketDataValidationError("specific option identifier is invalid")
    strike = _required_number(strike_value)
    if not isinstance(put_call_value, str):
        raise MarketDataValidationError("specific option put-call value is invalid")
    normalized_put_call = put_call_value.casefold()
    if normalized_put_call not in {"call", "put"}:
        raise MarketDataValidationError("specific option put-call value is invalid")
    put_call = cast("Literal['call', 'put']", normalized_put_call)
    fingerprint = _fingerprint(
        {
            "expiry": expiry.isoformat(),
            "put_call": put_call,
            "source_identifier": source_identifier,
            "strike_value": strike,
        },
    )
    return NormalizedOptionReference(
        source_identifier=source_identifier,
        expiry=expiry,
        strike_value=strike,
        put_call=put_call,
        fingerprint_sha256=fingerprint,
    )


def _validate_instrument_handle(value: str) -> str:
    try:
        return _INSTRUMENT_HANDLE_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise MarketDataValidationError("instrument handle is invalid") from error


def _require_utc_range(start: datetime, end: datetime) -> None:
    if (
        start.tzinfo is None
        or start.utcoffset() != timedelta(0)
        or end.tzinfo is None
        or end.utcoffset() != timedelta(0)
    ):
        raise MarketDataValidationError("market data range must use UTC")
    if end < start:
        raise MarketDataValidationError("market data range end precedes its start")


def _require_utc_instant(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise MarketDataValidationError(f"{label} must use UTC")


def _required_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise MarketDataValidationError("chart time is missing or invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MarketDataValidationError("chart time is missing or invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataValidationError("chart time must include an exchange offset")
    return parsed


def _require_interval_boundary(value: datetime, interval: ChartInterval) -> None:
    minutes_from_midnight = value.hour * 60 + value.minute
    if value.second or value.microsecond or minutes_from_midnight % interval.minutes:
        raise MarketDataValidationError("chart time is not on an interval boundary")


def _required_number(value: object) -> float:
    parsed = _optional_number(value)
    if parsed is None:
        raise MarketDataValidationError("chart close is missing or invalid")
    return parsed


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MarketDataValidationError("chart numeric field is invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MarketDataValidationError("chart numeric field is invalid")
    return parsed


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_PRICE_TYPE_LENGTH:
        raise MarketDataValidationError("chart price type is invalid")
    return value


def _optional_nonnegative_integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MarketDataValidationError("quote delay is invalid")
    return value


def _missing_intervals(
    local_times: Sequence[datetime],
    interval: ChartInterval,
) -> int:
    missing = 0
    for previous, current in pairwise(local_times):
        elapsed = current - previous
        if elapsed > interval.delta:
            missing += max(1, int(elapsed / interval.delta) - 1)
    return missing


def _fingerprint(value: object) -> str:
    material = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


__all__ = (
    "ChartInterval",
    "MarketDataError",
    "MarketDataValidationError",
    "NormalizedOptionChain",
    "NormalizedOptionReference",
    "NormalizedPriceBar",
    "NormalizedPriceSeries",
    "NormalizedQuote",
    "normalize_option_chain",
    "normalize_price_series",
    "normalize_quote",
)
