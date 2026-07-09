from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp import qa
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.live_mode import live_cached_token_for_tool
from saxo_bank_mcp.mcp_token_state import CachedTokenBlocked, cached_token_for_tool
from saxo_bank_mcp.token_cache import save_token_cache

EXPECTED_LIVE_NETWORK_READ_COUNT: Final = 8


def test_live_read_probe_writes_no_credentials_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SAXO_MCP_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_APP_KEY", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_CLIENT_ID", raising=False)
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(tmp_path / "missing-live.json"))
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
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.delenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", raising=False)
    out = tmp_path / "live-read.json"
    no_credentials = tmp_path / "live-read-no-credentials.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(no_credentials)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert not no_credentials.exists()
    assert report["status"] == "failed"
    assert report["requested_environment"] == "LIVE"
    assert report["tool_statuses"]["saxo_get_session_capabilities"] == "auth_required"
    assert report["tool_statuses"]["saxo_get_entitlements"] == "auth_required"
    assert report["tool_statuses"]["saxo_call_registered_endpoint_public_diagnostics"] == (
        "auth_required"
    )
    assert report["tool_statuses"]["saxo_call_registered_endpoint_authenticated_account"] == (
        "auth_required"
    )
    assert report["tool_results"]["saxo_get_session_capabilities"]["reason"] == (
        "live_token_cache_path_missing"
    )
    assert report["network_call_made"] is False
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    assert "live-app-key" not in json.dumps(report)


def test_live_read_probe_records_all_read_tool_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "live-token.json"))
    out = tmp_path / "live-read.json"
    no_credentials = tmp_path / "live-read-no-credentials.json"

    async def fake_live_payloads() -> dict[str, dict[str, JsonValue]]:
        return {
            "saxo_get_session_capabilities": {
                "status": "passed",
                "tool_name": "saxo_get_session_capabilities",
                "network_call_made": True,
            },
            "saxo_get_entitlements": {
                "status": "passed",
                "tool_name": "saxo_get_entitlements",
                "network_call_made": True,
                "live_write_called": False,
            },
            "saxo_list_registered_endpoints": {
                "status": "metadata_only_not_ready_for_trading",
                "tool_name": "saxo_list_registered_endpoints",
                "network_call_made": False,
            },
            "saxo_call_registered_endpoint_public_diagnostics": {
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "network_call_made": True,
                "auth_exercised": False,
            },
            "saxo_call_registered_endpoint_authenticated_account": {
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "network_call_made": True,
                "auth_exercised": True,
            },
            "saxo_call_registered_endpoint_balances": {
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "network_call_made": True,
                "auth_exercised": True,
            },
            "saxo_call_registered_endpoint_positions": {
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "network_call_made": True,
                "auth_exercised": True,
            },
            "saxo_call_registered_endpoint_orders": {
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "network_call_made": True,
                "auth_exercised": True,
            },
            "saxo_call_registered_endpoint_prices": {
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "network_call_made": True,
                "auth_exercised": True,
            },
        }

    monkeypatch.setattr("saxo_bank_mcp.qa_probes.call_live_read_payloads", fake_live_payloads)

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(no_credentials)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["read_tools_exercised"] == [
        "saxo_call_registered_endpoint",
        "saxo_get_entitlements",
        "saxo_get_session_capabilities",
        "saxo_list_registered_endpoints",
    ]
    assert report["read_scenarios_exercised"] == [
        "saxo_get_session_capabilities",
        "saxo_get_entitlements",
        "saxo_list_registered_endpoints",
        "saxo_call_registered_endpoint_public_diagnostics",
        "saxo_call_registered_endpoint_authenticated_account",
        "saxo_call_registered_endpoint_balances",
        "saxo_call_registered_endpoint_positions",
        "saxo_call_registered_endpoint_orders",
        "saxo_call_registered_endpoint_prices",
    ]
    assert report["live_read_coverage"] == {
        "accounts": True,
        "balances": True,
        "positions": True,
        "orders": True,
        "prices": True,
        "streaming": "not_applicable_to_read_only_get_tools",
    }
    assert report["authenticated_registered_read_passed"] is True
    assert report["network_read_count"] == EXPECTED_LIVE_NETWORK_READ_COUNT
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert not no_credentials.exists()
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


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
        report["tool_metadata"]["saxo_get_required_disclaimers"]["safe_in_live_read_mode"]
        is False
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
        "two independent approval factors",
        "precheck/defaults before placement",
        "throttling and duplicate-submit guard",
        "redacted audit trail outside repository",
        "daily activity review/monitoring",
        "explicit later live-write enablement decision",
    ]
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
