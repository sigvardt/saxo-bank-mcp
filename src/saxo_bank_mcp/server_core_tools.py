from __future__ import annotations

from typing import Final, Literal, TypedDict

from saxo_bank_mcp.auth_status import EffectiveReadEnvironment, SaxoAuthStatus
from saxo_bank_mcp.config import SaxoRuntimeConfig

SERVICE_NAME: Final = "saxo-bank-mcp"
HEALTH_SCOPE: Final = "local_mcp_server_liveness_only"
type HealthVerification = Literal[
    "local MCP process is running",
    "FastMCP tool call path is ready",
]
type HealthNonVerification = Literal[
    "Saxo connectivity",
    "credentials/session",
    "account access",
    "trading readiness/order placement",
    "live write readiness",
]
HEALTH_VERIFIES: Final[tuple[HealthVerification, ...]] = (
    "local MCP process is running",
    "FastMCP tool call path is ready",
)
HEALTH_DOES_NOT_VERIFY: Final[tuple[HealthNonVerification, ...]] = (
    "Saxo connectivity",
    "credentials/session",
    "account access",
    "trading readiness/order placement",
    "live write readiness",
)
HEALTH_TOOL_DESCRIPTION: Final = (
    "Reports local MCP server liveness/readiness only. Does not verify Saxo connectivity, "
    "credentials/session, account access, trading readiness/order placement, "
    "or live write readiness."
)
AUTH_STATUS_TOOL_DESCRIPTION: Final = (
    "Reports local Saxo auth configuration/cache state without secrets or network calls. "
    "Does not prove Saxo login, account access, session validity, session capabilities, "
    "trading readiness, or live-write permission."
)


class SaxoHealth(TypedDict):
    status: Literal["passed"]
    service: Literal["saxo-bank-mcp"]
    mode: EffectiveReadEnvironment
    live_writes: Literal[False]
    scope: Literal["local_mcp_server_liveness_only"]
    verifies: list[HealthVerification]
    does_not_verify: list[HealthNonVerification]


def saxo_health() -> SaxoHealth:
    runtime = SaxoRuntimeConfig.from_env()
    return {
        "status": "passed",
        "service": SERVICE_NAME,
        "mode": runtime.effective_read_environment(),
        "live_writes": False,
        "scope": HEALTH_SCOPE,
        "verifies": list(HEALTH_VERIFIES),
        "does_not_verify": list(HEALTH_DOES_NOT_VERIFY),
    }


def saxo_auth_status() -> SaxoAuthStatus:
    return SaxoRuntimeConfig.from_env().redacted_status()
