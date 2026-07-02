from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from saxo_bank_mcp import tribunal_index


def write_completion(directory: Path, tool_id: str) -> None:
    directory.mkdir(parents=True)
    for name in ("schema.json", "task.md", "input.json", "output.json", "audit.md"):
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "tribunal").mkdir()
    (directory / "tribunal" / "judge-output.json").write_text("{}", encoding="utf-8")
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


def test_tribunal_index_accepts_valid_completion(tmp_path: Path) -> None:
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
    assert report["no_artifacts_seen"] is False
    assert report["completed_tool_ids"] == ["saxo_get_session_capabilities"]


def test_tribunal_index_self_test_fails(tmp_path: Path) -> None:
    out = tmp_path / "missing.txt"

    result = tribunal_index.main(["--self-test-missing-artifact", "--out", str(out)])

    assert result == 1
    assert "self_test_missing_tool" in out.read_text(encoding="utf-8")


def test_tribunal_index_empty_requires_bootstrap_flag(tmp_path: Path) -> None:
    out = tmp_path / "report.json"

    result = tribunal_index.main(["--root", str(tmp_path / "empty"), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["no_artifacts_seen"] is True


def test_tribunal_index_empty_bootstrap_can_pass(tmp_path: Path) -> None:
    out = tmp_path / "report.json"

    result = tribunal_index.main(
        ["--root", str(tmp_path / "empty"), "--out", str(out), "--allow-empty-bootstrap"],
    )

    assert result == 0
