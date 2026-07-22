from __future__ import annotations

from typing import Final

LIVE_APPROVAL_PREFIX: Final = "APPROVE SAXO LIVE WRITE"


def live_approval_statement(request_fingerprint: str) -> str:
    return f"{LIVE_APPROVAL_PREFIX} {request_fingerprint}"
