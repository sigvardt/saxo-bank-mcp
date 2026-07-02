from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp import tribunal_index

TOOL_1: Final = "saxo_tool_one"
TOOL_2: Final = "saxo_tool_two"


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
        "fixed_feedback": [{"finding": "coverage", "fix": "checked", "evidence": "audit.md"}],
        "remaining_actionable_feedback": [],
        "refusal_reason": None,
        "exemption_reason": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    (directory / "tribunal-completion.json").write_text(json.dumps(payload), encoding="utf-8")


def test_tribunal_index_fails_when_registered_tool_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        lambda: frozenset({TOOL_1, TOOL_2}),
    )
    root = tmp_path / "tribunal"
    write_completion(root / TOOL_1 / "001", TOOL_1)
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["missing_tool_ids"] == [TOOL_2]
    assert any(
        f"missing expected tool artifact: {TOOL_2}" in error
        for error in report["errors"]
    )


def test_tribunal_index_fails_when_tool_artifact_is_duplicated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        lambda: frozenset({TOOL_1}),
    )
    root = tmp_path / "tribunal"
    write_completion(root / TOOL_1 / "001", TOOL_1)
    write_completion(root / TOOL_1 / "002", TOOL_1)
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["duplicate_tool_ids"] == [TOOL_1]
    assert any(
        f"duplicate tool artifact: {TOOL_1}" in error
        for error in report["errors"]
    )


def test_tribunal_index_fails_when_artifact_tool_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        lambda: frozenset({TOOL_1}),
    )
    root = tmp_path / "tribunal"
    write_completion(root / TOOL_1 / "001", TOOL_1)
    write_completion(root / TOOL_2 / "001", TOOL_2)
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["unexpected_tool_ids"] == [TOOL_2]
    expected_message = f"unexpected tool artifact (unregistered tool_id): {TOOL_2}"
    assert any(expected_message in error for error in report["errors"])
