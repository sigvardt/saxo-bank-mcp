from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import LIVE_ENDPOINTS, SaxoEnvironment, SaxoRuntimeConfig
from saxo_bank_mcp.mcp_tool_results import SESSION_DOES_NOT_VERIFY, ToolResult
from saxo_bank_mcp.session import SESSION_CAPABILITIES_PATH, SessionRequestError
from saxo_bank_mcp.token_cache import TokenCachePathError, inspect_token_cache, token_cache_path

type LiveReadSettingsErrorCode = Literal[
    "live_environment_required",
    "live_reads_disabled",
    "live_credentials_missing",
    "live_token_cache_path_missing",
    "live_token_cache_path_refused",
    "token_environment_mismatch",
]

LIVE_WRITE_MISSING_REQUIREMENTS: Final[tuple[str, ...]] = (
    "SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK",
    "LIVE credentials",
    "LIVE account allowlist",
    "low notional and quantity limits",
    "kill switch ready",
    "server-created preview token",
    "two independent approval factors",
    "precheck/defaults before placement",
    "throttling and duplicate-submit guard",
    "redacted audit trail outside repository",
    "daily activity review/monitoring",
    "explicit later live-write enablement decision",
)


class ReadOnlySettings(Protocol):
    rest_base_url: str


class LiveReadSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    rest_base_url: str
    cache_path: Path


@dataclass(frozen=True, slots=True)
class LiveReadSettingsError(Exception):
    code: LiveReadSettingsErrorCode
    detail: str


def resolve_live_read_settings(
    environ: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> LiveReadSettings:
    runtime = SaxoRuntimeConfig.from_env(environ, repo_root=repo_root)
    match runtime.requested_environment:
        case SaxoEnvironment.LIVE:
            pass
        case SaxoEnvironment.SIM:
            raise LiveReadSettingsError(
                "live_environment_required",
                "SAXO_MCP_ENVIRONMENT=LIVE is required for LIVE read-only calls",
            )

    match runtime.effective_read_environment():
        case "LIVE":
            pass
        case "LIVE_READ_DISABLED":
            if not runtime.live_reads_enabled:
                raise LiveReadSettingsError(
                    "live_reads_disabled",
                    "SAXO_MCP_ENABLE_LIVE_READS=1 is required for LIVE read-only calls",
                )
            raise LiveReadSettingsError(
                "live_credentials_missing",
                "SAXO_MCP_LIVE_CLIENT_ID and SAXO_MCP_LIVE_CLIENT_SECRET are required",
            )
        case "SIM":
            raise LiveReadSettingsError(
                "live_environment_required",
                "SAXO_MCP_ENVIRONMENT=LIVE is required for LIVE read-only calls",
            )

    source = environ if environ is not None else os.environ
    raw_cache_path = source.get("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", "").strip()
    if not raw_cache_path:
        raise LiveReadSettingsError(
            "live_token_cache_path_missing",
            "SAXO_MCP_LIVE_TOKEN_CACHE_PATH must point to a LIVE token cache outside the repo",
        )
    try:
        cache = token_cache_path(Path(raw_cache_path), repo_root=repo_root)
    except TokenCachePathError as error:
        raise LiveReadSettingsError("live_token_cache_path_refused", str(error)) from error
    return LiveReadSettings(rest_base_url=LIVE_ENDPOINTS.rest_base_url, cache_path=cache)


def live_read_refused_for_runtime(tool_name: str, runtime: SaxoRuntimeConfig) -> ToolResult:
    return {
        "status": "refused",
        "tool_name": tool_name,
        "requested_environment": runtime.requested_environment.value,
        "effective_read_environment": runtime.effective_read_environment(),
        "environment": "LIVE",
        "live_reads": False,
        "live_writes": False,
        "scope_used": False,
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "reason": "missing_live_read_enablement",
        "missing_requirements": live_read_missing_requirements(runtime),
        "next_action": (
            "provide LIVE read credentials, enable LIVE reads, and configure a LIVE token cache"
        ),
        "verifies": [],
        "does_not_verify": list(SESSION_DOES_NOT_VERIFY),
    }


def live_read_auth_required(tool_name: str, reason: str) -> ToolResult:
    return {
        "status": "auth_required",
        "tool_name": tool_name,
        "requested_environment": "LIVE",
        "environment": "LIVE",
        "reason": reason,
        "missing_requirements": live_read_missing_requirements_for_reason(reason),
        "scope_used": False,
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "next_action": live_read_next_action(reason),
        "verifies": [],
        "does_not_verify": list(SESSION_DOES_NOT_VERIFY),
    }


def live_cached_token_for_tool(tool_name: str, cache_path: Path) -> SaxoTokenSet | ToolResult:
    inspection = inspect_token_cache(cache_path)
    token = inspection["token"]
    if token is None:
        reason = (
            "token_cache_unreadable"
            if inspection["present"] and not inspection["readable"]
            else "token_cache_missing"
        )
        return live_read_auth_required(tool_name, reason)
    if token.redacted_status()["is_expired"]:
        return live_read_auth_required(tool_name, "token_cache_expired")
    if token.environment == "SIM":
        return live_read_auth_required(tool_name, "token_environment_mismatch")
    return token


def live_session_error(tool_name: str, error: SessionRequestError) -> ToolResult:
    return {
        "status": "session_capabilities_failed",
        "tool_name": tool_name,
        "requested_environment": "LIVE",
        "environment": "LIVE",
        "endpoint_path": SESSION_CAPABILITIES_PATH,
        "reason": error.code,
        "http_status": error.http_status,
        "detail": error.detail,
        "scope_used": False,
        "network_call_made": error.code in {"http_error", "invalid_capabilities_response"},
        "live_write_called": False,
        "order_or_subscription_created": False,
        "verifies": [],
        "does_not_verify": list(SESSION_DOES_NOT_VERIFY),
    }


def live_write_refusal_payload(
    *,
    tool_name: str,
    write_class: str,
    operation_id: str,
) -> dict[str, JsonValue]:
    return {
        "status": "refused",
        "tool_name": tool_name,
        "write_class": write_class,
        "operation_id": operation_id,
        "requested_environment": "LIVE",
        "environment": "LIVE",
        "refusal_reason": "missing_live_write_enablement",
        "missing_requirements": list(LIVE_WRITE_MISSING_REQUIREMENTS),
        "fastmcp_called": True,
        "network_call_made": False,
        "live_write": False,
        "live_write_called": False,
        "order_placed": False,
        "order_modified": False,
        "order_cancelled": False,
        "order_or_subscription_created": False,
        "preview_token_redacted": True,
        "approval_factor_mode": "disabled_for_live",
        "next_action": (
            "do not perform LIVE writes until the explicit live-write enablement plan exists"
        ),
        "verifies": [
            "LIVE order tools refuse before any network call",
            "LIVE order tools list every real-money enablement gate",
        ],
        "does_not_verify": [
            "LIVE order placement",
            "LIVE order modification",
            "LIVE order cancellation",
            "LIVE account state change",
            "LIVE trading permission",
        ],
    }


def live_read_missing_requirements(runtime: SaxoRuntimeConfig) -> list[str]:
    missing: list[str] = []
    if not runtime.live_reads_enabled:
        missing.append("SAXO_MCP_ENABLE_LIVE_READS=1")
    if not runtime.live_credentials_present:
        missing.append("LIVE credentials")
    missing.append("SAXO_MCP_LIVE_TOKEN_CACHE_PATH")
    return missing


def live_read_missing_requirements_for_reason(reason: str) -> list[str]:
    match reason:
        case "live_environment_required":
            return ["SAXO_MCP_ENVIRONMENT=LIVE"]
        case "live_reads_disabled":
            return ["SAXO_MCP_ENABLE_LIVE_READS=1"]
        case "live_credentials_missing":
            return ["LIVE credentials"]
        case "live_token_cache_path_missing" | "live_token_cache_path_refused":
            return ["SAXO_MCP_LIVE_TOKEN_CACHE_PATH"]
        case (
            "token_cache_missing"
            | "token_cache_unreadable"
            | "token_cache_expired"
            | "token_environment_mismatch"
        ):
            return ["valid LIVE token cache"]
        case _:
            return ["LIVE read enablement"]


def live_read_next_action(reason: str) -> str:
    match reason:
        case "token_cache_expired":
            return "provide a fresh LIVE read token cache before retrying"
        case "token_cache_missing" | "token_cache_unreadable":
            return "provide a readable LIVE token cache outside the repository"
        case "token_environment_mismatch":
            return "replace the LIVE token cache with a LIVE-issued token before retrying"
        case _:
            return "configure explicit LIVE read enablement before retrying"
