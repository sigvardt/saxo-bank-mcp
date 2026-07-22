from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_tribunal_index import session_capabilities_only, write_completion

from saxo_bank_mcp import evidence_publication, tribunal_index


def test_tribunal_index_propagates_secret_scan_publication_failure(
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

    def reject_candidate(
        _label: str,
        _text: str,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return ([{"pattern_class": "credential_regex"}], [])

    monkeypatch.setattr(evidence_publication, "scan_secret_text", reject_candidate)

    result = tribunal_index.main(["--root", str(root), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "status": "failed",
        "reason": "evidence_secret_scan_failed",
    }


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
    completion_path = root / "saxo_get_session_capabilities" / "001" / "tribunal-completion.json"
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
