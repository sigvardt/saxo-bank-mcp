from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import evidence_publication

type ScanResult = tuple[list[dict[str, str]], list[dict[str, str]]]


@pytest.mark.parametrize(
    "scan_result",
    [
        ([{"pattern": "tribunal-v19-scan-marker"}], []),
        ([], [{"error": "scan_failed"}]),
    ],
)
def test_scanned_json_rejects_candidate_before_first_write(
    scan_result: ScanResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "tribunal-v19-scan-marker"
    writes: list[str] = []
    out = tmp_path / "evidence.json"

    def reject_candidate(label: str, text: str) -> ScanResult:
        assert label == out.name
        assert marker in text
        return scan_result

    def record_write(path: Path, text: str) -> None:
        assert path == out
        writes.append(text)

    monkeypatch.setattr(evidence_publication, "scan_secret_text", reject_candidate)
    monkeypatch.setattr(evidence_publication, "write_text", record_write)

    published = evidence_publication.write_scanned_json(
        out,
        {"status": "passed", "marker": marker},
    )

    assert published is False
    assert len(writes) == 1
    assert marker not in writes[0]
    assert json.loads(writes[0]) == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }
