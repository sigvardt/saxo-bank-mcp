from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2


def retry_after_seconds(headers: httpx2.Headers) -> float | None:
    values: list[float] = []
    for name, raw_value in headers.multi_items():
        normalized = name.lower()
        if normalized == "retry-after":
            parsed = _retry_after_value(raw_value)
        elif normalized.startswith("x-ratelimit-") and normalized.endswith("-reset"):
            parsed = _positive_float(raw_value)
        else:
            continue
        if parsed is not None:
            values.append(parsed)
    return max(values) if values else None


def _retry_after_value(value: str) -> float | None:
    seconds = _positive_float(value)
    if seconds is not None:
        return seconds
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    seconds_until_retry = (retry_at - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds_until_retry) if math.isfinite(seconds_until_retry) else None


def _positive_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None
