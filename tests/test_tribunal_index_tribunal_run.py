from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_tribunal_index import session_capabilities_only, write_completion

from saxo_bank_mcp import tribunal_index


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
    completion_path = root / "saxo_get_session_capabilities" / "001" / "tribunal-completion.json"
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
        "tribunal_run missing normalized.json" in e for e in report["invalid_artifact_errors"]
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
    completion_path = root / "saxo_get_session_capabilities" / "001" / "tribunal-completion.json"
    write_completion(completion_path.parent, "saxo_get_session_capabilities")
    judge_output = completion_path.parent / "tribunal" / "rounds" / "round-01" / "judge-output.json"
    judge_output.write_text(
        '{"verdict": {"status": "needs-human"}}',
        encoding="utf-8",
    )
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert any("highest verdict must be confirmed" in e for e in report["invalid_artifact_errors"])


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
    completion_path = root / "saxo_get_session_capabilities" / "001" / "tribunal-completion.json"
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
    assert any("does not mention tool_id" in e for e in report["invalid_artifact_errors"])
