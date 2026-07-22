from __future__ import annotations

import argparse
import sys
from typing import Final, Literal, TypedDict

from saxo_bank_mcp.auth_status import EffectiveReadEnvironment, SaxoAuthStatus
from saxo_bank_mcp.config import SaxoRuntimeConfig
from saxo_bank_mcp.fastmcp_logging_safety import (
    FASTMCP_VALIDATION_SAFETY_TRANSFORM,
    SafeFastMCP,
    install_fastmcp_argument_log_filter,
)
from saxo_bank_mcp.mcp_request_ledger_tools import SAFE_REQUEST_LEDGER_MIDDLEWARE
from saxo_bank_mcp.server_tool_registration import register_saxo_tools
from saxo_bank_mcp.tool_annotations import annotation_for_tool

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
    mode: EffectiveReadEnvironment
    live_writes: Literal[False]
    scope: Literal["local_mcp_server_liveness_only"]
    verifies: list[HealthVerification]
    does_not_verify: list[HealthNonVerification]


install_fastmcp_argument_log_filter()
mcp: Final = SafeFastMCP(SERVICE_NAME, strict_input_validation=False)
mcp.add_transform(FASTMCP_VALIDATION_SAFETY_TRANSFORM)
mcp.add_middleware(SAFE_REQUEST_LEDGER_MIDDLEWARE)


@mcp.tool(description=HEALTH_TOOL_DESCRIPTION, annotations=annotation_for_tool("saxo_health"))
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


@mcp.tool(
    description=AUTH_STATUS_TOOL_DESCRIPTION,
    annotations=annotation_for_tool("saxo_auth_status"),
)
def saxo_auth_status() -> SaxoAuthStatus:
    return SaxoRuntimeConfig.from_env().redacted_status()


register_saxo_tools(mcp)


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
    if transport == "stdio":
        run_stdio()
    elif transport == "http":
        run_http(host=str(args.host), port=int(args.port))
    else:
        raise SystemExit(f"unsupported transport: {transport}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
