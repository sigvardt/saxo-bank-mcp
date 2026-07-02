from __future__ import annotations

from datetime import UTC, datetime

from saxo_bank_mcp.safety_models import StoredPreview

_PREVIEWS: dict[str, StoredPreview] = {}
_COMMITTED_FINGERPRINTS: set[str] = set()
_last_commit_at: list[datetime | None] = [None]


def reset_safety_state() -> None:
    _PREVIEWS.clear()
    _COMMITTED_FINGERPRINTS.clear()
    _last_commit_at[0] = None


def pending_preview_count() -> int:
    return len(_PREVIEWS)


def committed_fingerprint_count() -> int:
    return len(_COMMITTED_FINGERPRINTS)


def store_preview(token: str, preview: StoredPreview) -> None:
    _PREVIEWS[token] = preview


def get_preview(token: str) -> StoredPreview | None:
    return _PREVIEWS.get(token)


def is_committed(fingerprint: str) -> bool:
    return fingerprint in _COMMITTED_FINGERPRINTS


def mark_committed(fingerprint: str) -> None:
    _COMMITTED_FINGERPRINTS.add(fingerprint)
    _last_commit_at[0] = datetime.now(UTC)


def rate_limit_reason() -> str | None:
    if _last_commit_at[0] is None:
        return None
    elapsed = datetime.now(UTC) - _last_commit_at[0]
    if elapsed.total_seconds() < 1:
        return "rate_limit_1_order_per_second"
    return None
