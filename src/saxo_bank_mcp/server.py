from __future__ import annotations

import argparse
import sys
from typing import Final, Literal, TypedDict

from fastmcp import FastMCP

from saxo_bank_mcp.config import SaxoAuthStatus, SaxoRuntimeConfig
from saxo_bank_mcp.mcp_auth_tools import (
    saxo_exchange_pkce_code,
    saxo_get_session_capabilities,
    saxo_refresh_token,
    saxo_start_pkce_login,
)
from saxo_bank_mcp.mcp_entitlement_tools import (
    ENTITLEMENTS_TOOL_DESCRIPTION,
    saxo_get_entitlements,
)
from saxo_bank_mcp.mcp_order_tools import (
    ORDER_WRITE_TOOL_DESCRIPTION,
    saxo_cancel_multileg_sim_order,
    saxo_cancel_sim_order,
    saxo_cancel_sim_orders_by_instrument,
    saxo_modify_multileg_sim_order,
    saxo_modify_sim_order,
    saxo_place_multileg_sim_order,
    saxo_place_sim_order,
)
from saxo_bank_mcp.mcp_safety_tools import (
    COMMIT_TOOL_DESCRIPTION,
    PREVIEW_TOOL_DESCRIPTION,
    SAFETY_STATUS_TOOL_DESCRIPTION,
    saxo_commit_write_preview,
    saxo_create_write_preview,
    saxo_safety_status,
)
from saxo_bank_mcp.mcp_streaming_tools import (
    STREAMING_CLEANUP_TOOL_DESCRIPTION,
    STREAMING_TOOL_DESCRIPTION,
    saxo_cleanup_streaming_subscriptions,
    saxo_create_streaming_price_subscription,
)
from saxo_bank_mcp.mcp_tool_results import (
    PKCE_EXCHANGE_TOOL_DESCRIPTION,
    PKCE_START_TOOL_DESCRIPTION,
    REFRESH_TOOL_DESCRIPTION,
    SESSION_CAPABILITIES_TOOL_DESCRIPTION,
)
from saxo_bank_mcp.mcp_trade_tools import (
    DISCLAIMER_LOOKUP_TOOL_DESCRIPTION,
    DISCLAIMER_RESPONSE_TOOL_DESCRIPTION,
    MULTILEG_DEFAULTS_TOOL_DESCRIPTION,
    ORDER_PREVIEW_TOOL_DESCRIPTION,
    saxo_create_order_preview,
    saxo_get_multileg_order_defaults,
    saxo_get_required_disclaimers,
    saxo_register_disclaimer_response,
)
from saxo_bank_mcp.read_tools import (
    REGISTERED_CALL_TOOL_DESCRIPTION,
    saxo_call_registered_endpoint,
)
from saxo_bank_mcp.registry_list_tools import (
    READ_LIST_TOOL_DESCRIPTION,
    saxo_list_registered_endpoints,
)

SERVICE_NAME: Final = "saxo-bank-mcp"
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8000
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
    mode: Literal["SIM"]
    live_writes: Literal[False]
    scope: Literal["local_mcp_server_liveness_only"]
    verifies: list[HealthVerification]
    does_not_verify: list[HealthNonVerification]


mcp: Final = FastMCP(SERVICE_NAME)


@mcp.tool(description=HEALTH_TOOL_DESCRIPTION)
def saxo_health() -> SaxoHealth:
    return {
        "status": "passed",
        "service": SERVICE_NAME,
        "mode": "SIM",
        "live_writes": False,
        "scope": HEALTH_SCOPE,
        "verifies": list(HEALTH_VERIFIES),
        "does_not_verify": list(HEALTH_DOES_NOT_VERIFY),
    }


@mcp.tool(description=AUTH_STATUS_TOOL_DESCRIPTION)
def saxo_auth_status() -> SaxoAuthStatus:
    return SaxoRuntimeConfig.from_env().redacted_status()


mcp.tool(description=PKCE_START_TOOL_DESCRIPTION)(saxo_start_pkce_login)
mcp.tool(description=PKCE_EXCHANGE_TOOL_DESCRIPTION)(saxo_exchange_pkce_code)
mcp.tool(description=REFRESH_TOOL_DESCRIPTION)(saxo_refresh_token)
mcp.tool(description=SESSION_CAPABILITIES_TOOL_DESCRIPTION)(saxo_get_session_capabilities)
mcp.tool(description=ENTITLEMENTS_TOOL_DESCRIPTION)(saxo_get_entitlements)
mcp.tool(description=READ_LIST_TOOL_DESCRIPTION)(saxo_list_registered_endpoints)
mcp.tool(description=REGISTERED_CALL_TOOL_DESCRIPTION)(saxo_call_registered_endpoint)
mcp.tool(description=SAFETY_STATUS_TOOL_DESCRIPTION)(saxo_safety_status)
mcp.tool(description=PREVIEW_TOOL_DESCRIPTION)(saxo_create_write_preview)
mcp.tool(description=COMMIT_TOOL_DESCRIPTION)(saxo_commit_write_preview)
mcp.tool(description=ORDER_PREVIEW_TOOL_DESCRIPTION)(saxo_create_order_preview)
mcp.tool(description=MULTILEG_DEFAULTS_TOOL_DESCRIPTION)(saxo_get_multileg_order_defaults)
mcp.tool(description=DISCLAIMER_LOOKUP_TOOL_DESCRIPTION)(saxo_get_required_disclaimers)
mcp.tool(description=DISCLAIMER_RESPONSE_TOOL_DESCRIPTION)(saxo_register_disclaimer_response)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_place_sim_order)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_modify_sim_order)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_cancel_sim_order)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_cancel_sim_orders_by_instrument)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_place_multileg_sim_order)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_modify_multileg_sim_order)
mcp.tool(description=ORDER_WRITE_TOOL_DESCRIPTION)(saxo_cancel_multileg_sim_order)
mcp.tool(description=STREAMING_TOOL_DESCRIPTION)(saxo_create_streaming_price_subscription)
mcp.tool(description=STREAMING_CLEANUP_TOOL_DESCRIPTION)(saxo_cleanup_streaming_subscriptions)


def run_stdio() -> None:
    mcp.run()


def run_http(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    mcp.run(transport="http", host=host, port=port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Saxo Bank MCP server.")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transport = str(args.transport)
    match transport:
        case "stdio":
            run_stdio()
        case "http":
            run_http(host=str(args.host), port=int(args.port))
        case _:
            raise SystemExit(f"unsupported transport: {transport}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
