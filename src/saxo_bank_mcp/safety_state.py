from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from saxo_bank_mcp.safety_models import StoredPreview

_PREVIEWS: dict[str, StoredPreview] = {}
SAXO_DUPLICATE_REQUEST_WINDOW_SECONDS: Final = 15
_COMMITTED_FINGERPRINTS: dict[str, datetime] = {}
_COMMITTED_PREVIEW_TOKENS: set[str] = set()
_last_commit_at: list[datetime | None] = [None]


def reset_safety_state() -> None:
    _PREVIEWS.clear()
    _COMMITTED_FINGERPRINTS.clear()
    _COMMITTED_PREVIEW_TOKENS.clear()
    _last_commit_at[0] = None


def pending_preview_count() -> int:
    return len(_PREVIEWS)


def committed_fingerprint_count() -> int:
    _prune_committed_fingerprints()
    return len(_COMMITTED_FINGERPRINTS)


def store_preview(token: str, preview: StoredPreview) -> None:
    _PREVIEWS[token] = preview


def get_preview(token: str) -> StoredPreview | None:
    return _PREVIEWS.get(token)


def is_committed(fingerprint: str) -> bool:
    _prune_committed_fingerprints()
    return fingerprint in _COMMITTED_FINGERPRINTS


def is_preview_token_committed(token_fingerprint: str) -> bool:
    return token_fingerprint in _COMMITTED_PREVIEW_TOKENS


def mark_committed(fingerprint: str, preview_token_fingerprint: str) -> None:
    now = datetime.now(UTC)
    _COMMITTED_FINGERPRINTS[fingerprint] = now
    _COMMITTED_PREVIEW_TOKENS.add(preview_token_fingerprint)
    _last_commit_at[0] = now


def rate_limit_reason() -> str | None:
    if _last_commit_at[0] is None:
        return None
    elapsed = datetime.now(UTC) - _last_commit_at[0]
    if elapsed.total_seconds() < 1:
        return "rate_limit_1_order_per_second"
    return None


def _prune_committed_fingerprints() -> None:
    now = datetime.now(UTC)
    expired = [
        fingerprint
        for fingerprint, committed_at in _COMMITTED_FINGERPRINTS.items()
        if (now - committed_at).total_seconds() >= SAXO_DUPLICATE_REQUEST_WINDOW_SECONDS
    ]
    for fingerprint in expired:
        del _COMMITTED_FINGERPRINTS[fingerprint]
