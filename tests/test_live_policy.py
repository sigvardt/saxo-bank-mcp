from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saxo_bank_mcp import qa
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.live_mode import live_cached_token_for_tool
from saxo_bank_mcp.mcp_token_state import CachedTokenBlocked, cached_token_for_tool
from saxo_bank_mcp.token_cache import save_token_cache


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


def test_sim_cached_token_refuses_live_environment_token(tmp_path: Path) -> None:
    cache = tmp_path / "sim-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )

    result = cached_token_for_tool("saxo_get_session_capabilities", cache)

    assert isinstance(result, CachedTokenBlocked)
    assert result.result["status"] == "auth_required"
    assert result.result["reason"] == "token_environment_mismatch"
    assert result.result["network_call_made"] is False


def test_tool_inventory_qa_probe_records_machine_readable_metadata(tmp_path: Path) -> None:
    out = tmp_path / "tool-inventory.json"

    result = qa.main(["tool-inventory", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["tool_count"] == len(report["all_tools"])
    assert report["metadata_missing_tools"] == []
    assert report["metadata_unregistered_tools"] == []
    assert "saxo_call_registered_endpoint" in report["live_network_read_tools"]
    assert "saxo_get_multileg_order_defaults" in report["sim_only_network_read_tools"]
    assert "saxo_get_multileg_order_defaults" not in report["write_or_state_changing_tools"]
    assert (
        report["tool_metadata"]["saxo_get_required_disclaimers"]["safe_in_live_read_mode"] is False
    )
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


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
    assert report["verifies"] == [
        "LIVE order tools refuse before any network call",
        "LIVE order tools list every real-money enablement gate",
    ]
    assert report["does_not_verify"] == [
        "LIVE order placement",
        "LIVE order modification",
        "LIVE order cancellation",
        "LIVE account state change",
        "LIVE trading permission",
    ]
    assert report["missing_requirements"] == [
        "SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK",
        "LIVE credentials",
        "LIVE account allowlist",
        "low notional and quantity limits",
        "kill switch ready",
        "server-created preview token",
        "one exact-action approval statement sent by the human in agent chat",
        "precheck/defaults before placement",
        "throttling and duplicate-submit guard",
        "redacted audit trail outside repository",
        "daily activity review/monitoring",
        "explicit later live-write enablement decision",
    ]
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
