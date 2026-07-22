from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import evidence_publication
from saxo_bank_mcp import hard_task_manifest as manifest_module
from saxo_bank_mcp._evidence import JsonValue


def test_hard_task_manifest_rejects_before_persisting_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "hard-task-manifest.json"
    marker = "rejected-hard-task-manifest-candidate"

    def rejected_payload(_payload: JsonValue) -> dict[str, JsonValue]:
        return {"status": "passed", "unsafe_marker": marker}

    def reject_marker(
        _label: str,
        text: str,
    ) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
        findings: list[dict[str, JsonValue]] = (
            [{"pattern_class": "credential_regex"}] if marker in text else []
        )
        return findings, []

    monkeypatch.setattr(manifest_module, "redact_json", rejected_payload)
    monkeypatch.setattr(evidence_publication, "scan_secret_text", reject_marker)

    result = manifest_module.handle_hard_task_manifest(
        out,
        manifest_module.DEFAULT_INCOMPLETE_TOOL_IDS,
    )

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }
    assert marker not in out.read_text(encoding="utf-8")
