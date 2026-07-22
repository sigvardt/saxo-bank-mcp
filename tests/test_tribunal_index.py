from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def session_capabilities_only() -> frozenset[str]:
    return frozenset({"saxo_get_session_capabilities"})


def no_registered_tools() -> frozenset[str]:
    return frozenset()


def place_order_only() -> frozenset[str]:
    return frozenset({"saxo_place_sim_order"})


def write_completion(directory: Path, tool_id: str) -> None:
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(
        json.dumps({"name": tool_id, "inputSchema": {"type": "object"}}),
        encoding="utf-8",
    )
    (directory / "task.md").write_text(
        f"Drive {tool_id} through the real MCP tool path.",
        encoding="utf-8",
    )
    (directory / "input.json").write_text('{"arguments": {}}', encoding="utf-8")
    (directory / "output.json").write_text(
        '{"status": "passed", "fastmcp_called": true}',
        encoding="utf-8",
    )
    (directory / "audit.md").write_text(
        "Safety and agent-experience controls reviewed with enough detail "
        "to prove this artifact is not placeholder evidence.",
        encoding="utf-8",
    )
    (directory / "tribunal" / "rounds" / "round-01").mkdir(parents=True)
    (directory / "tribunal" / "normalized.json").write_text(
        '{"round_artifacts": ["rounds/round-01/judge-output.json"]}',
        encoding="utf-8",
    )
    (directory / "tribunal" / "rounds" / "round-01" / "judge-output.json").write_text(
        '{"verdict": {"status": "confirmed"}}',
        encoding="utf-8",
    )
    (directory / "tribunal" / "judge-input.md").write_text(
        f"Hard task for `{tool_id}` through the real MCP path.",
        encoding="utf-8",
    )
    payload = {
        "tool_id": tool_id,
        "status": "complete",
        "mcp_tool_schema": "schema.json",
        "task": "task.md",
        "input": "input.json",
        "output": "output.json",
        "error": None,
        "audit": "audit.md",
        "tribunal_run": "tribunal",
        "mcp_execution": {
            "surface": "fastmcp_tool_call",
            "tool_id": tool_id,
            "evidence": "output.json",
            "mock_only": False,
            "unit_tests_only": False,
            "internal_client_only": False,
            "pre_implementation_only": False,
            "notes": "FastMCP tool call captured from the real MCP path.",
        },
        "fixed_feedback": [{"finding": "x", "fix": "y", "evidence": "audit.md"}],
        "remaining_actionable_feedback": [],
        "refusal_reason": None,
        "exemption_reason": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    (directory / "tribunal-completion.json").write_text(json.dumps(payload), encoding="utf-8")
