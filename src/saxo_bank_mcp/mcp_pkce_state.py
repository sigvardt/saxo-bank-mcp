from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from saxo_bank_mcp.auth import SaxoPendingAuthorization
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.mcp_tool_results import ToolResult, auth_required
from saxo_bank_mcp.token_cache import (
    delete_pending_authorization,
    inspect_pending_authorization,
    pending_authorization_path,
)

PENDING_AUTH_TTL: Final = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class PendingAuthorizationReady:
    pending: SaxoPendingAuthorization


@dataclass(frozen=True, slots=True)
class PendingAuthorizationBlocked:
    result: ToolResult


type PendingAuthorizationCheck = PendingAuthorizationReady | PendingAuthorizationBlocked


def pending_authorization_for_exchange(
    settings: SimAuthSettings,
    state: str,
) -> PendingAuthorizationCheck:
    pending_path = pending_authorization_path(settings.cache_path)
    inspection = inspect_pending_authorization(pending_path)
    pending = inspection["pending"]
    if pending is None:
        reason = (
            "pending_pkce_unreadable"
            if inspection["present"] and not inspection["readable"]
            else "pending_pkce_state_missing"
        )
        return PendingAuthorizationBlocked(
            auth_required("saxo_exchange_pkce_code", reason),
        )
    if pending.state != state:
        return PendingAuthorizationBlocked(
            auth_required("saxo_exchange_pkce_code", "pending_pkce_state_mismatch"),
        )
    if datetime.now(UTC) - pending.created_at > PENDING_AUTH_TTL:
        delete_pending_authorization(pending_path)
        return PendingAuthorizationBlocked(
            auth_required("saxo_exchange_pkce_code", "pending_pkce_expired"),
        )
    if pending.redirect_uri != settings.redirect_uri:
        return PendingAuthorizationBlocked(
            auth_required("saxo_exchange_pkce_code", "redirect_uri_changed_since_login_start"),
        )
    return PendingAuthorizationReady(pending)
