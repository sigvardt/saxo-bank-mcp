from __future__ import annotations

from typing import Final

LIVE_APPROVAL_PREFIX: Final = "APPROVE SAXO LIVE WRITE"


def live_approval_statement(action: str, request_binding: str) -> str:
    return f"{LIVE_APPROVAL_PREFIX}: {action}. AUTHORIZATION {request_binding}"
