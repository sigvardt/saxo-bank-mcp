from __future__ import annotations

import os
from typing import Final

import pytest
from fastmcp import Client

from saxo_bank_mcp.server import create_mcp_server
from saxo_bank_mcp.server_eval_tool_filter import (
    EVAL_ALLOWED_TOOLS_ENV,
    EVAL_TOOL_FILTER_FLAG,
    EvalToolFilterError,
    derive_eval_tool_filter_env,
    resolve_eval_tool_filter,
)
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS, EXPECTED_TOOL_COUNT

SIM_ENV: Final = {
    "SAXO_MCP_ENVIRONMENT": "SIM",
    "SAXO_MCP_ENABLE_LIVE_READS": "0",
    "SAXO_MCP_ENABLE_LIVE_WRITES": "",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_unfiltered_server_lists_all_39_tools() -> None:
    # Given: production construction with no eval filter.
    server = create_mcp_server()

    # When: list_tools is called through FastMCP.
    async with Client(server) as client:
        tools = await client.list_tools()

    # Then: the full catalog is exposed.
    names = {tool.name for tool in tools}
    assert len(names) == EXPECTED_TOOL_COUNT
    assert names == ALL_LOGICAL_TOOL_IDS


@pytest.mark.anyio
async def test_filtered_server_lists_exact_subset_including_core() -> None:
    # Given: an exact subset that includes health, auth, and live-precheck.
    allowed = frozenset(
        {"saxo_health", "saxo_auth_status", "saxo_precheck_live_order", "saxo_safety_status"},
    )
    server = create_mcp_server(allowed_tools=allowed)

    # When: list_tools is called.
    async with Client(server) as client:
        tools = await client.list_tools()

    # Then: only the requested logical tools are registered.
    assert {tool.name for tool in tools} == allowed


def test_filter_requires_sim_and_explicit_flag() -> None:
    # Given: SIM-only env and an explicit filter flag with known tools.
    env = {
        **SIM_ENV,
        EVAL_TOOL_FILTER_FLAG: "1",
        EVAL_ALLOWED_TOOLS_ENV: "saxo_health,saxo_auth_status",
    }

    # When: the filter is resolved.
    allowed = resolve_eval_tool_filter(env)

    # Then: the exact logical set is returned.
    assert allowed == frozenset({"saxo_health", "saxo_auth_status"})


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        (
            {**SIM_ENV, EVAL_TOOL_FILTER_FLAG: "1", EVAL_ALLOWED_TOOLS_ENV: ""},
            "eval_tool_filter_empty",
        ),
        (
            {**SIM_ENV, EVAL_TOOL_FILTER_FLAG: "1", EVAL_ALLOWED_TOOLS_ENV: "saxo_health,*"},
            "eval_tool_filter_wildcard",
        ),
        (
            {
                **SIM_ENV,
                EVAL_TOOL_FILTER_FLAG: "1",
                EVAL_ALLOWED_TOOLS_ENV: "mcp__saxo_bank_mcp__saxo_health",
            },
            "eval_tool_filter_qualified_name",
        ),
        (
            {**SIM_ENV, EVAL_TOOL_FILTER_FLAG: "1", EVAL_ALLOWED_TOOLS_ENV: "saxo_missing"},
            "eval_tool_filter_unknown_tool",
        ),
        (
            {
                **SIM_ENV,
                EVAL_TOOL_FILTER_FLAG: "1",
                EVAL_ALLOWED_TOOLS_ENV: "saxo_health,saxo_health",
            },
            "eval_tool_filter_duplicate",
        ),
        (
            {
                "SAXO_MCP_ENVIRONMENT": "LIVE",
                "SAXO_MCP_ENABLE_LIVE_READS": "0",
                "SAXO_MCP_ENABLE_LIVE_WRITES": "",
                EVAL_TOOL_FILTER_FLAG: "1",
                EVAL_ALLOWED_TOOLS_ENV: "saxo_health",
            },
            "eval_tool_filter_requires_sim",
        ),
        (
            {
                "SAXO_MCP_ENVIRONMENT": "SIM",
                "SAXO_MCP_ENABLE_LIVE_READS": "1",
                "SAXO_MCP_ENABLE_LIVE_WRITES": "",
                EVAL_TOOL_FILTER_FLAG: "1",
                EVAL_ALLOWED_TOOLS_ENV: "saxo_health",
            },
            "eval_tool_filter_requires_live_reads_off",
        ),
        (
            {
                "SAXO_MCP_ENVIRONMENT": "SIM",
                "SAXO_MCP_ENABLE_LIVE_READS": "0",
                "SAXO_MCP_ENABLE_LIVE_WRITES": "1",
                EVAL_TOOL_FILTER_FLAG: "1",
                EVAL_ALLOWED_TOOLS_ENV: "saxo_health",
            },
            "eval_tool_filter_requires_live_writes_empty",
        ),
        (
            {**SIM_ENV, EVAL_ALLOWED_TOOLS_ENV: "saxo_health"},
            "eval_tool_filter_flag_required",
        ),
        (
            {**SIM_ENV, EVAL_TOOL_FILTER_FLAG: "yes", EVAL_ALLOWED_TOOLS_ENV: "saxo_health"},
            "eval_tool_filter_flag_invalid",
        ),
    ],
)
def test_filter_fail_closed_inputs(env: dict[str, str], reason: str) -> None:
    # Given: an invalid eval filter configuration.
    # When / Then: resolution rejects with a stable reason.
    with pytest.raises(EvalToolFilterError) as exc_info:
        resolve_eval_tool_filter(env)
    assert exc_info.value.reason == reason


def test_derive_child_env_is_fresh_and_sim_locked() -> None:
    # Given: an isolated SIM runtime env.
    base = {**SIM_ENV, "PATH": os.environ.get("PATH", "/usr/bin"), "MARKER": "parent"}

    # When: a per-case child env is derived.
    child = derive_eval_tool_filter_env(base, ("saxo_health", "saxo_auth_status"))

    # Then: the child is a copy with the filter locked and the parent is unchanged.
    assert child is not base
    assert child["MARKER"] == "parent"
    assert child[EVAL_TOOL_FILTER_FLAG] == "1"
    assert child[EVAL_ALLOWED_TOOLS_ENV] == "saxo_health,saxo_auth_status"
    assert EVAL_TOOL_FILTER_FLAG not in base
    assert resolve_eval_tool_filter(child) == frozenset({"saxo_health", "saxo_auth_status"})


def test_derive_child_env_without_grants_clears_filter() -> None:
    # Given: a parent env that somehow carried a filter flag.
    base = {
        **SIM_ENV,
        EVAL_TOOL_FILTER_FLAG: "1",
        EVAL_ALLOWED_TOOLS_ENV: "saxo_health",
    }

    # When: empty grants are applied.
    child = derive_eval_tool_filter_env(base, ())

    # Then: the filter is inactive for that child.
    assert EVAL_TOOL_FILTER_FLAG not in child
    assert EVAL_ALLOWED_TOOLS_ENV not in child
    assert resolve_eval_tool_filter(child) is None
