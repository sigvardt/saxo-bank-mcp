# pyright: reportPrivateUsage=false
"""Eval-runtime account discovery stays inside the logical FastMCP matrix path."""

from __future__ import annotations

import inspect

from saxo_bank_mcp.agent_skill_matrix_env import (
    SIM_ORDER_LIFECYCLE_CASE_ID,
    SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC,
    apply_case_eval_allowlists,
)
from saxo_bank_mcp.qa_sim_tool_matrix import _run_mcp_account_and_fixture_preflight
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import account_discovery_call


def test_account_discovery_is_a_logical_mcp_call() -> None:
    tool_id, arguments = account_discovery_call()

    assert tool_id == "saxo_call_registered_endpoint"
    assert arguments == {
        "method": "GET",
        "path": "/port/v1/accounts/me",
        "params": {},
    }
    source = inspect.getsource(_run_mcp_account_and_fixture_preflight)
    assert "account_discovery_call" in source
    assert "call_tool" in source


def test_apply_case_eval_allowlists_binds_lifecycle_uic_only() -> None:
    base = {"SAXO_MCP_ENVIRONMENT": "SIM"}
    lifecycle = apply_case_eval_allowlists(base, case_id=SIM_ORDER_LIFECYCLE_CASE_ID)
    assert lifecycle["SAXO_MCP_INSTRUMENT_ALLOWLIST"] == SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC
    assert "SAXO_MCP_ACCOUNT_ALLOWLIST" not in lifecycle
    other = apply_case_eval_allowlists(base, case_id="auth-recovery")
    assert "SAXO_MCP_INSTRUMENT_ALLOWLIST" not in other
