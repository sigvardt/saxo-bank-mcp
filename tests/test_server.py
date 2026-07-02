from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest
from fastmcp import Client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_saxo_health_returns_sim_safe_status() -> None:
    module = import_module("saxo_bank_mcp.server")

    async with Client(module.mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool("saxo_health", {})

    health_tool = next(tool for tool in tools if tool.name == "saxo_health")
    assert health_tool.description is not None
    assert "local MCP server liveness/readiness only" in health_tool.description
    for unsupported_check in (
        "Saxo connectivity",
        "credentials/session",
        "account access",
        "trading readiness/order placement",
        "live write readiness",
    ):
        assert unsupported_check in health_tool.description

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["service"] == "saxo-bank-mcp"
    assert payload["mode"] == "SIM"
    assert payload["live_writes"] is False
    assert payload["scope"] == "local_mcp_server_liveness_only"
    assert payload["verifies"] == [
        "local MCP process is running",
        "FastMCP tool call path is ready",
    ]
    assert payload["does_not_verify"] == [
        "Saxo connectivity",
        "credentials/session",
        "account access",
        "trading readiness/order placement",
        "live write readiness",
    ]


@pytest.mark.anyio
async def test_saxo_auth_status_reports_sim_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SAXO_MCP_ENVIRONMENT", raising=False)
    module = import_module("saxo_bank_mcp.server")

    async with Client(module.mcp) as client:
        tools = await client.list_tools()
        result = await client.call_tool("saxo_auth_status", {})

    auth_tool = next(tool for tool in tools if tool.name == "saxo_auth_status")
    assert auth_tool.description is not None
    assert "secrets" in auth_tool.description
    for unsupported_check in (
        "Saxo login",
        "account access",
        "session validity",
        "session capabilities",
        "trading readiness",
        "live-write permission",
    ):
        assert unsupported_check in auth_tool.description
    payload = result.structured_content
    assert payload is not None
    serialized = str(payload)
    assert payload["requested_environment"] == "SIM"
    assert payload["effective_read_environment"] == "SIM"
    assert payload["live_reads"] is False
    assert payload["live_writes"] is False
    assert payload["scope_used"] is False
    assert "local Saxo environment selection" in payload["verifies"]
    assert "Saxo login/server-side authentication" in payload["does_not_verify"]
    assert "trading/order readiness" in payload["does_not_verify"]
    assert payload["blocking_reasons"]
    assert isinstance(payload["next_action"], str)
    assert payload["next_action"]
    assert "secret" not in serialized.lower()


@pytest.mark.anyio
async def test_safety_tool_descriptions_prevent_false_write_confidence() -> None:
    module = import_module("saxo_bank_mcp.server")

    async with Client(module.mcp) as client:
        tools = await client.list_tools()

    descriptions = {tool.name: tool.description or "" for tool in tools}
    expected_fragments = {
        "saxo_safety_status": (
            "Does not call Saxo",
            "order placement",
            "live-write readiness",
        ),
        "saxo_create_write_preview": (
            "Does not call Saxo",
            "place orders",
            "preview token is sensitive",
            "local simulation commit",
        ),
        "saxo_commit_write_preview": (
            "separate out-of-band approval factor",
            "Agents must not derive or expose",
            "Does not call Saxo",
            "place orders",
            "live-write readiness",
        ),
    }
    for tool_name, fragments in expected_fragments.items():
        assert tool_name in descriptions
        for fragment in fragments:
            assert fragment in descriptions[tool_name]


@pytest.mark.anyio
async def test_streaming_tools_are_registered_with_saxo_limits_and_safety() -> None:
    module = import_module("saxo_bank_mcp.server")

    async with Client(module.mcp) as client:
        tools = await client.list_tools()
        denied = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "POST", "path": "/trade/v1/prices/subscriptions"},
            raise_on_error=False,
        )

    descriptions = {tool.name: tool.description or "" for tool in tools}
    for tool_name in (
        "saxo_create_streaming_price_subscription",
        "saxo_cleanup_streaming_subscriptions",
    ):
        assert tool_name in descriptions
        assert "sim-streaming.saxobank.com/sim/oapi/streaming/ws" in descriptions[tool_name]
        assert "Authorization header" in descriptions[tool_name]
        assert "4 simultaneous streaming connections" in descriptions[tool_name]
        assert "200 price instruments" in descriptions[tool_name]

    payload = denied.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["network_call_made"] is False


def test_fastmcp_dependency_is_exactly_pinned() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "fastmcp==3.4.2" in pyproject["project"]["dependencies"]
    assert "httpx2[brotli,http2,zstd]==2.5.0" in pyproject["project"]["dependencies"]
    assert "websockets==16.0" in pyproject["project"]["dependencies"]


def test_package_module_help_exits_without_starting_server() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "saxo_bank_mcp", "--help"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "--transport" in result.stdout
