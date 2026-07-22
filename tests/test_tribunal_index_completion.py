from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_tribunal_index import (
    place_order_only,
    session_capabilities_only,
    write_completion,
)

from saxo_bank_mcp import tribunal_index


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
    assert report["root"] == "<external-root>"
    assert report["no_artifacts_seen"] is False
    assert report["missing_tool_ids"] == []
    assert report["invalid_artifact_errors"] == []
    assert report["remaining_actionable_feedback_complete_tool_ids"] == []
    assert report["no_hidden_deferred_state"] is True
    assert report["no_deferred_state"] is True
    assert report["completed_tool_ids"] == ["saxo_get_session_capabilities"]


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
