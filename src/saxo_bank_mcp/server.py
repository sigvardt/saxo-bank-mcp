from __future__ import annotations

import argparse
import os
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Final

from saxo_bank_mcp.fastmcp_logging_safety import (
    FASTMCP_VALIDATION_SAFETY_TRANSFORM,
    SafeFastMCP,
    install_fastmcp_argument_log_filter,
)
from saxo_bank_mcp.mcp_analytics_tools import shutdown_analytics_runtime
from saxo_bank_mcp.mcp_request_ledger_tools import SAFE_REQUEST_LEDGER_MIDDLEWARE
from saxo_bank_mcp.server_core_tools import (
    AUTH_STATUS_TOOL_DESCRIPTION,
    HEALTH_DOES_NOT_VERIFY,
    HEALTH_SCOPE,
    HEALTH_TOOL_DESCRIPTION,
    HEALTH_VERIFIES,
    SERVICE_NAME,
    SaxoHealth,
    saxo_auth_status,
    saxo_health,
)
from saxo_bank_mcp.server_eval_tool_filter import (
    EvalToolFilterError,
    resolve_eval_tool_filter,
)
from saxo_bank_mcp.server_tool_registration import register_saxo_tools

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8000

# Re-exports for existing importers/tests.
__all__ = (
    "AUTH_STATUS_TOOL_DESCRIPTION",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_DOES_NOT_VERIFY",
    "HEALTH_SCOPE",
    "HEALTH_TOOL_DESCRIPTION",
    "HEALTH_VERIFIES",
    "SERVICE_NAME",
    "SaxoHealth",
    "create_mcp_server",
    "main",
    "mcp",
    "run_http",
    "run_stdio",
    "saxo_auth_status",
    "saxo_health",
)


def create_mcp_server(
    *,
    allowed_tools: frozenset[str] | None = None,
) -> SafeFastMCP:
    """Build a SafeFastMCP instance with optional SIM-only eval tool filter."""
    install_fastmcp_argument_log_filter()
    server = SafeFastMCP(
        SERVICE_NAME,
        strict_input_validation=False,
        lifespan=_analytics_lifespan,
    )
    server.add_transform(FASTMCP_VALIDATION_SAFETY_TRANSFORM)
    server.add_middleware(SAFE_REQUEST_LEDGER_MIDDLEWARE)
    register_saxo_tools(server, allowed_tools=allowed_tools)
    return server


@asynccontextmanager
async def _analytics_lifespan(_server: object) -> AsyncGenerator[dict[str, bool]]:
    """Tie the in-process analytics runtime to one FastMCP process lifespan."""
    try:
        yield {"analytics_runtime_owned": True}
    finally:
        await shutdown_analytics_runtime()


def create_mcp_server_from_env(env: Mapping[str, str] | None = None) -> SafeFastMCP:
    """Build server from env; eval filter activates only with the explicit flag."""
    try:
        allowed = resolve_eval_tool_filter(os.environ if env is None else env)
    except EvalToolFilterError as exc:
        raise SystemExit(f"eval tool filter rejected: {exc.reason}") from exc
    return create_mcp_server(allowed_tools=allowed)


mcp: Final = create_mcp_server_from_env()


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
