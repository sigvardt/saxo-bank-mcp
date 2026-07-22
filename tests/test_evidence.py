from __future__ import annotations

from pathlib import Path

import pytest

from saxo_bank_mcp._evidence import write_json
from saxo_bank_mcp.evidence_publication import write_scanned_text


def test_write_json_atomically_replaces_without_backup(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"

    write_json(out, {"status": "failed"})
    write_json(out, {"status": "passed"})

    assert out.read_text(encoding="utf-8") == '{\n  "status": "passed"\n}\n'
    assert tuple(tmp_path.iterdir()) == (out,)


def test_scanned_text_never_publishes_rejected_candidate(tmp_path: Path) -> None:
    out = tmp_path / "gate.md"
    secret = ".".join(("eyJ" + ("A" * 30), "B" * 30, "C" * 30))

    published = write_scanned_text(out, f"# Gate\n\n{secret}\n")

    assert published is False
    assert secret not in out.read_text(encoding="utf-8")
    assert tuple(tmp_path.iterdir()) == (out,)


def test_json_evidence_rejects_nonfinite_numbers(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"

    with pytest.raises(ValueError, match="Out of range float values"):
        write_json(out, {"unsafe_number": float("inf")})

    assert not out.exists()
