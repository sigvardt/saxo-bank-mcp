from __future__ import annotations

from pathlib import Path

import pytest

from saxo_bank_mcp import final_verify_code
from saxo_bank_mcp.loop_manifest import GitState


def test_code_gate_allows_full_suite_five_minutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def command_check(
        name: str,
        command: tuple[str, ...],
        timeout: int = 120,
    ) -> tuple[str, bool, str]:
        del command
        calls.append((name, timeout))
        return name, True, "exit 0"

    def write_scanned_text(_out: Path, _content: str) -> bool:
        return True

    monkeypatch.setattr(final_verify_code, "command_check", command_check)
    monkeypatch.setattr(final_verify_code, "write_scanned_text", write_scanned_text)

    result = final_verify_code.verify_code(
        tmp_path / "report.md",
        lambda: GitState(sha="test-sha", dirty=True),
    )

    assert result == 0
    assert ("pytest", 300) in calls
