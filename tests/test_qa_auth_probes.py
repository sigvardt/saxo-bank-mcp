from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import qa


def test_auth_status_probe_includes_token_cache_environment_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "auth-status.json"

    result = qa.main(["auth-status", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["auth"]["token_cache_refresh_supported"] is None
    assert report["auth"]["token_cache_environment"] is None
