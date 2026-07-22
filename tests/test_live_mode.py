from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp import qa
from saxo_bank_mcp._evidence import JsonValue

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
    assert report["missing_requirements"] == [
        "SAXO_MCP_ENABLE_LIVE_READS=1",
        "LIVE credentials",
        "SAXO_MCP_LIVE_TOKEN_CACHE_PATH",
    ]
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
        safe: dict[str, JsonValue] = {
            "live_write_called": False,
            "order_or_subscription_created": False,
        }

        def registered(
            operation_id: str,
            path: str,
            *,
            auth_exercised: bool,
            fingerprint_scope: str = "raw_response_body",
        ) -> dict[str, JsonValue]:
            return {
                **safe,
                "status": "passed",
                "tool_name": "saxo_call_registered_endpoint",
                "operation_id": operation_id,
                "method": "GET",
                "path": path,
                    "response_fingerprint_scope": fingerprint_scope,
                    "http_status": 200,
                    "network_call_made": True,
                "auth_exercised": auth_exercised,
            }

        return {
            "saxo_get_session_capabilities": {
                **safe,
                "status": "passed",
                "tool_name": "saxo_get_session_capabilities",
                "network_call_made": True,
            },
            "saxo_get_entitlements": {
                **safe,
                "status": "passed",
                "tool_name": "saxo_get_entitlements",
                "network_call_made": True,
            },
            "saxo_list_registered_endpoints": {
                **safe,
                "status": "metadata_only_not_ready_for_trading",
                "tool_name": "saxo_list_registered_endpoints",
                "network_call_made": False,
            },
            "saxo_call_registered_endpoint_public_diagnostics": {
                **registered(
                    "get.root.v1.diagnostics.get",
                    "/root/v1/diagnostics/get",
                    auth_exercised=False,
                ),
            },
            "saxo_call_registered_endpoint_authenticated_account": {
                **registered(
                    "get.port.v1.accounts.me",
                    "/port/v1/accounts/me",
                    auth_exercised=True,
                ),
            },
            "saxo_call_registered_endpoint_balances": {
                **registered(
                    "get.port.v1.balances.me",
                    "/port/v1/balances/me",
                    auth_exercised=True,
                    fingerprint_scope="account_money_state_fields",
                ),
            },
            "saxo_call_registered_endpoint_positions": {
                **registered(
                    "get.port.v1.positions.me",
                    "/port/v1/positions/me",
                    auth_exercised=True,
                ),
            },
            "saxo_call_registered_endpoint_orders": {
                **registered(
                    "get.port.v1.orders.me",
                    "/port/v1/orders/me",
                    auth_exercised=True,
                ),
            },
            "saxo_call_registered_endpoint_prices": {
                **registered(
                    "get.trade.v1.infoprices",
                    "/trade/v1/infoprices",
                    auth_exercised=True,
                ),
            },
        }

    monkeypatch.setattr("saxo_bank_mcp.qa_live_probes.call_live_read_payloads", fake_live_payloads)

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
