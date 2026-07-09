from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.mcp_tool_results import ToolResult, auth_required
from saxo_bank_mcp.token_cache import inspect_token_cache


@dataclass(frozen=True, slots=True)
class CachedTokenReady:
    token: SaxoTokenSet


@dataclass(frozen=True, slots=True)
class CachedTokenBlocked:
    result: ToolResult


type CachedTokenCheck = CachedTokenReady | CachedTokenBlocked


def cached_token_for_tool(tool_name: str, cache_path: Path) -> CachedTokenCheck:
    inspection = inspect_token_cache(cache_path)
    token = inspection["token"]
    if token is not None:
        if token.environment == "LIVE":
            return CachedTokenBlocked(auth_required(tool_name, "token_environment_mismatch"))
        return CachedTokenReady(token)
    reason = (
        "token_cache_unreadable"
        if inspection["present"] and not inspection["readable"]
        else "token_cache_missing"
    )
    return CachedTokenBlocked(auth_required(tool_name, reason))
