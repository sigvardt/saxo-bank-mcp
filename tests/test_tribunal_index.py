from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saxo_bank_mcp import tribunal_index


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
        "fixed_feedback": [{"finding": "x", "fix": "y", "evidence": "audit.md"}],
        "remaining_actionable_feedback": [],
        "refusal_reason": None,
        "exemption_reason": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    (directory / "tribunal-completion.json").write_text(json.dumps(payload), encoding="utf-8")


def test_tribunal_index_accepts_valid_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        session_capabilities_only,
    )
    root = tmp_path / "tribunal"
    write_completion(
        root / "saxo_get_session_capabilities" / "001",
        "saxo_get_session_capabilities",
    )
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["source"] == "fastmcp_tool_list"
    assert report["no_artifacts_seen"] is False
    assert report["missing_tool_ids"] == []
    assert report["invalid_artifact_errors"] == []
    assert report["remaining_actionable_feedback_complete_tool_ids"] == []
    assert report["no_hidden_deferred_state"] is True
    assert report["no_deferred_state"] is True
    assert report["completed_tool_ids"] == ["saxo_get_session_capabilities"]


def test_tribunal_index_self_test_fails(tmp_path: Path) -> None:
    out = tmp_path / "missing.txt"

    result = tribunal_index.main(["--self-test-missing-artifact", "--out", str(out)])

    assert result == 1
    assert "self_test_missing_tool" in out.read_text(encoding="utf-8")


def test_tribunal_index_marks_incomplete_tools_as_deferred(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        place_order_only,
    )
    root = tmp_path / "tribunal"
    completion_path = root / "saxo_place_sim_order" / "001" / "tribunal-completion.json"
    write_completion(completion_path.parent, "saxo_place_sim_order")
    payload = json.loads(completion_path.read_text(encoding="utf-8"))
    payload["status"] = "incomplete"
    payload["risk_class"] = "money_moving"
    payload["fixed_feedback"] = []
    payload["remaining_actionable_feedback"] = ["token-backed SIM proof missing"]
    completion_path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["no_hidden_deferred_state"] is True
    assert report["no_deferred_state"] is False
    assert report["has_incomplete_tools"] is True
    assert report["incomplete_tool_ids"] == ["saxo_place_sim_order"]


def test_tribunal_index_surfaces_remaining_feedback_on_complete_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        session_capabilities_only,
    )
    root = tmp_path / "tribunal"
    completion_path = (
        root
        / "saxo_get_session_capabilities"
        / "001"
        / "tribunal-completion.json"
    )
    write_completion(completion_path.parent, "saxo_get_session_capabilities")
    payload = json.loads(completion_path.read_text(encoding="utf-8"))
    payload["remaining_actionable_feedback"] = ["still unsafe"]
    completion_path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["remaining_actionable_feedback_complete_tool_ids"] == [
        "saxo_get_session_capabilities",
    ]
    assert report["invalid_artifact_errors"]


def test_tribunal_index_empty_requires_bootstrap_flag(tmp_path: Path) -> None:
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(tmp_path / "empty"), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["no_artifacts_seen"] is True


def test_tribunal_index_empty_bootstrap_can_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        no_registered_tools,
    )
    out = tmp_path / "report.json"

    result = tribunal_index.main(
        ["--root", str(tmp_path / "empty"), "--out", str(out), "--allow-empty-bootstrap"],
    )

    assert result == 0


def test_tribunal_index_rejects_placeholder_tribunal_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        session_capabilities_only,
    )
    root = tmp_path / "tribunal"
    completion_path = (
        root
        / "saxo_get_session_capabilities"
        / "001"
        / "tribunal-completion.json"
    )
    write_completion(completion_path.parent, "saxo_get_session_capabilities")

    tribunal_dir = completion_path.parent / "tribunal"
    shutil.rmtree(tribunal_dir)
    tribunal_dir.mkdir()

    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert any(
        "tribunal_run missing normalized.json" in e
        for e in report["invalid_artifact_errors"]
    )


def test_tribunal_index_rejects_complete_tool_with_needs_human_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        session_capabilities_only,
    )
    root = tmp_path / "tribunal"
    completion_path = (
        root
        / "saxo_get_session_capabilities"
        / "001"
        / "tribunal-completion.json"
    )
    write_completion(completion_path.parent, "saxo_get_session_capabilities")
    judge_output = (
        completion_path.parent
        / "tribunal"
        / "rounds"
        / "round-01"
        / "judge-output.json"
    )
    judge_output.write_text(
        '{"verdict": {"status": "needs-human"}}',
        encoding="utf-8",
    )
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert any(
        "highest verdict must be confirmed" in e
        for e in report["invalid_artifact_errors"]
    )


def test_tribunal_index_rejects_complete_tool_with_cross_tool_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        session_capabilities_only,
    )
    root = tmp_path / "tribunal"
    completion_path = (
        root
        / "saxo_get_session_capabilities"
        / "001"
        / "tribunal-completion.json"
    )
    write_completion(completion_path.parent, "saxo_get_session_capabilities")
    judge_input = completion_path.parent / "tribunal" / "judge-input.md"
    judge_input.write_text(
        "Hard task for `saxo_refresh_token` through the real MCP path.",
        encoding="utf-8",
    )
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert any(
        "does not mention tool_id" in e for e in report["invalid_artifact_errors"]
    )
