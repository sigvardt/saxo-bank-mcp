from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_tribunal_index import no_registered_tools

from saxo_bank_mcp import tribunal_index


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
