from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SaxoRuntimeConfig
from saxo_bank_mcp.live_mode import live_cached_token_for_tool
from saxo_bank_mcp.token_cache import save_token_cache


def test_live_cached_token_refuses_untagged_token(tmp_path: Path) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="untagged-access-token",  # noqa: S106
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )

    result = live_cached_token_for_tool("saxo_get_session_capabilities", cache)

    assert isinstance(result, dict)
    assert result["status"] == "auth_required"
    assert result["reason"] == "token_environment_mismatch"
    assert result["network_call_made"] is False


def test_auth_status_blocks_untagged_token_cache_in_live_mode(tmp_path: Path) -> None:
    live_cache = tmp_path / "state" / "live-token.json"
    save_token_cache(
        live_cache,
        SaxoTokenSet(
            access_token="untagged-access-token",  # noqa: S106
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    config = SaxoRuntimeConfig.from_env(
        {
            "SAXO_MCP_ENVIRONMENT": "LIVE",
            "SAXO_MCP_ENABLE_LIVE_READS": "1",
            "SAXO_MCP_LIVE_APP_KEY": "live-key",
            "SAXO_MCP_LIVE_TOKEN_CACHE_PATH": str(live_cache),
        },
        repo_root=tmp_path / "repo",
    )

    status = config.redacted_status()

    assert status["effective_read_environment"] == "LIVE"
    assert status["token_cache_environment"] is None
    assert status["blocking_reasons"] == ["token_environment_mismatch"]
    assert status["next_action"].startswith("replace the token cache with a LIVE-issued")
