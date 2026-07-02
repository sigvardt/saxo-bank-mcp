from __future__ import annotations

from pathlib import Path

import pytest

from saxo_bank_mcp.config import SaxoRuntimeConfig


def test_auth_status_reports_refused_token_cache_path_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()

    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_SIM_APP_KEY": "sim-app-key",
            "SAXO_MCP_SIM_REDIRECT_URI": "https://example.test/callback",
            "SAXO_MCP_TOKEN_CACHE_PATH": str(repo / "token-cache.json"),
        },
        repo_root=repo,
    )

    status = config.redacted_status()

    assert status["token_cache_present"] is False
    assert status["token_cache_readable"] is False
    assert status["blocking_reasons"] == ["token_cache_path_refused"]
    assert "outside the repository" in status["next_action"]
