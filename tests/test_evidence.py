from __future__ import annotations

import json
from pathlib import Path

from saxo_bank_mcp._evidence import write_json


def test_write_json_archives_changed_evidence(tmp_path: Path) -> None:
    out = tmp_path / "evidence.json"

    write_json(out, {"status": "failed"})
    write_json(out, {"status": "passed"})

    archives = tuple(tmp_path.glob("evidence.json.*.bak"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["status"] == "failed"
