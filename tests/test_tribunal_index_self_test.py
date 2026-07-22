from __future__ import annotations

from pathlib import Path

from saxo_bank_mcp import tribunal_index


def test_tribunal_index_self_test_fails(tmp_path: Path) -> None:
    out = tmp_path / "missing.txt"

    result = tribunal_index.main(["--self-test-missing-artifact", "--out", str(out)])

    assert result == 1
    assert "self_test_missing_tool" in out.read_text(encoding="utf-8")
