from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saxo_bank_mcp import qa
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.live_mode import live_cached_token_for_tool
from saxo_bank_mcp.token_cache import save_token_cache


def test_live_read_probe_writes_no_credentials_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SAXO_MCP_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_CLIENT_SECRET", raising=False)
    out = tmp_path / "live-read.json"
    no_credentials = tmp_path / "live-read-no-credentials.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(no_credentials)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report == json.loads(no_credentials.read_text(encoding="utf-8"))
    assert report["status"] == "skipped_no_live_credentials"
    assert report["requested_environment"] == "LIVE"
    assert report["effective_read_environment"] == "LIVE_READ_DISABLED"
    assert report["live_reads_enabled"] is False
    assert report["live_credentials_present"] is False
    assert report["network_call_made"] is False
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert report["prompted_user"] is False
    assert report["private_identifiers_present"] is False
    assert "private_identifiers_redacted" not in report
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_live_read_probe_requires_live_token_cache_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_CLIENT_ID", "live-client-id")
    monkeypatch.setenv("SAXO_MCP_LIVE_CLIENT_SECRET", "live-client-secret")
    monkeypatch.delenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", raising=False)
    out = tmp_path / "live-read.json"
    no_credentials = tmp_path / "live-read-no-credentials.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(no_credentials)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert not no_credentials.exists()
    assert report["status"] == "auth_required"
    assert report["tool_name"] == "saxo_get_session_capabilities"
    assert report["requested_environment"] == "LIVE"
    assert report["environment"] == "LIVE"
    assert report["reason"] == "live_token_cache_path_missing"
    assert report["network_call_made"] is False
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    assert "live-client-id" not in json.dumps(report)
    assert "live-client-secret" not in json.dumps(report)


def test_live_cached_token_refuses_sim_environment_token(tmp_path: Path) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="sim-access-token",  # noqa: S106
            environment="SIM",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )

    result = live_cached_token_for_tool("saxo_get_session_capabilities", cache)

    assert isinstance(result, dict)
    assert result["status"] == "auth_required"
    assert result["reason"] == "token_environment_mismatch"
    assert result["network_call_made"] is False
    assert "LIVE-issued token" in str(result["next_action"])


def test_live_write_refusal_drives_fastmcp_order_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_WRITES", raising=False)
    out = tmp_path / "live-write-refusal.json"

    result = qa.main(["live-write-refusal", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "refused"
    assert report["tool_name"] == "saxo_place_sim_order"
    assert report["requested_environment"] == "LIVE"
    assert report["environment"] == "LIVE"
    assert report["refusal_reason"] == "missing_live_write_enablement"
    assert report["fastmcp_called"] is True
    assert report["network_call_made"] is False
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert report["missing_requirements"] == [
        "SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK",
        "LIVE credentials",
        "LIVE account allowlist",
        "low notional and quantity limits",
        "kill switch ready",
        "two independent approval factors",
        "explicit later live-write enablement decision",
    ]
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
