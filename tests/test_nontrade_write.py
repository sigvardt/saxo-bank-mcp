from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from saxo_bank_mcp import nontrade_policy, qa, qa_nontrade_probes
from saxo_bank_mcp.safety import SafetyKernel, reset_safety_state
from saxo_bank_mcp.server import mcp

NONTRADE_WRITE_OPERATION_COUNT = 48


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_nontrade_policy_classifies_risky_write_groups() -> None:
    rows = nontrade_policy.nontrade_classification_rows()
    groups = {str(row["service_group"]): str(row["safety_class"]) for row in rows}

    assert rows
    assert groups["Asset Transfers"] == "money_or_asset_movement"
    assert groups["Client Management"] == "client_or_account_setup_change"
    assert groups["Partner Integration"] == "partner_action_on_behalf"
    assert groups["Regulatory Services"] == "regulatory_data_change"
    disclaimer = next(
        row for row in rows if row["operation_id"] == "post.dm.v2.disclaimers"
    )
    refused_rows = [row for row in rows if row is not disclaimer]
    assert disclaimer["registry_status"] == "implemented"
    assert disclaimer["policy_refusal_reason"] == ""
    assert all(str(row["registry_status"]) == "refused" for row in refused_rows)
    assert all(
        str(row["policy_refusal_reason"]) == "risky_non_trading_write_refused"
        for row in refused_rows
    )
    assert nontrade_policy.safe_nontrade_write_operations() == ()
    assert nontrade_policy.all_nontrade_writes_are_refused() is True


def test_nontrade_write_probe_skips_when_no_safe_operation(tmp_path: Path) -> None:
    out = tmp_path / "nontrade-write.json"

    result = qa_nontrade_probes.handle_nontrade_write(out, safe_only=True)

    report = _load(out)
    assert result == 0
    assert report["status"] == "skipped_no_safe_operation"
    assert report["safe_only"] is True
    assert report["prompted_user"] is False
    assert report["preview_and_commit_exercised"] is False
    preview_denial = report["generic_preview_denial"]
    assert isinstance(preview_denial, dict)
    assert preview_denial["status"] == "denied"
    assert preview_denial["mcp_is_error"] is True
    assert preview_denial["refusal_reason"] == "risky_non_trading_write_refused"
    assert preview_denial["preview_created"] is False
    assert report["skipped_operations_refused"] is True
    assert report["live_write"] is False
    assert report["order_or_subscription_created"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


@pytest.mark.anyio
async def test_create_write_preview_refuses_nontrade_operation_before_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", "SIM-ACCOUNT-1")
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", "21")
    operation = nontrade_policy.first_nontrade_write_operation("Asset Transfers")
    assert operation is not None

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_write_preview",
            {
                "operation_id": operation.operation_id,
                "account_key": "SIM-ACCOUNT-1",
                "instrument_uic": 21,
                "quantity": 1,
                "estimated_notional": 100,
                "account_currency": "USD",
                "risk": {
                    "cost": 100,
                    "cash_required": 100,
                    "margin_impact": 1,
                    "contract_multiplier": 1,
                    "conversion_known": True,
                },
                "request_body": {"SideEffect": "nontrade"},
            },
            raise_on_error=False,
        )
        minimal_result = await client.call_tool(
            "saxo_create_write_preview",
            {"operation_id": operation.operation_id},
            raise_on_error=False,
        )

    payload = result.structured_content
    minimal_payload = minimal_result.structured_content
    assert result.is_error is True
    assert payload is not None
    assert minimal_result.is_error is True
    assert minimal_payload is not None
    assert minimal_payload["status"] == "denied"
    assert minimal_payload["refusal_reason"] == "risky_non_trading_write_refused"
    assert payload["status"] == "denied"
    assert payload["preview_created"] is False
    assert payload["approval_requested"] is False
    assert payload["network_call_made"] is False
    assert payload["order_or_subscription_created"] is False
    assert payload["service_group"] == "Asset Transfers"
    assert payload["safety_class"] == "money_or_asset_movement"
    assert payload["refusal_reason"] == "risky_non_trading_write_refused"
    assert "preview_token" not in payload
    assert SafetyKernel().status()["pending_preview_count"] == 0


def test_nontrade_denied_probe_uses_real_fastmcp_path(tmp_path: Path) -> None:
    out = tmp_path / "asset-transfer-denied.json"

    result = qa_nontrade_probes.handle_nontrade_denied(out, service="asset-transfers")

    report = _load(out)
    registered_call = report["registered_call"]
    assert isinstance(registered_call, dict)
    assert result == 0
    assert report["status"] == "denied"
    assert report["service_group"] == "Asset Transfers"
    assert report["refusal_reason"] == "risky_non_trading_write_refused"
    assert report["preview_created"] is False
    assert report["approval_requested"] is False
    assert report["network_call_made"] is False
    assert registered_call["tool_name"] == "saxo_call_registered_endpoint"
    assert registered_call["status"] == "denied"
    assert registered_call["mcp_is_error"] is True
    assert registered_call["denial_reason"] == "write_operations_disabled_by_policy"
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_nontrade_denied_cli_covers_client_management(tmp_path: Path) -> None:
    out = tmp_path / "client-management-denied.json"

    result = qa.main(
        ["nontrade-denied", "--service", "client-management", "--out", str(out)],
    )

    report = _load(out)
    assert result == 0
    assert report["status"] == "denied"
    assert report["service_group"] == "Client Management"
    assert report["safety_class"] == "client_or_account_setup_change"
    assert report["preview_created"] is False
    assert report["approval_requested"] is False
    assert report["network_call_made"] is False


def test_nontrade_denied_unknown_service_fails_closed(tmp_path: Path) -> None:
    out = tmp_path / "unknown-denied.json"

    result = qa_nontrade_probes.handle_nontrade_denied(out, service="not-a-service")

    report = _load(out)
    assert result == 0
    assert report["status"] == "denied"
    assert report["safety_class"] == "unclassified_write_fail_closed"
    assert report["refusal_reason"] == "unclassified_nontrade_write_service"
    assert report["preview_created"] is False
    assert report["network_call_made"] is False


def test_nontrade_denial_sweep_exercises_every_registered_write(
    tmp_path: Path,
) -> None:
    out = tmp_path / "sweep.json"

    result = qa.main(["nontrade-denial-sweep", "--out", str(out)])

    report = _load(out)
    assert result == 0
    assert report["status"] == "passed"
    assert report["operation_count"] == NONTRADE_WRITE_OPERATION_COUNT
    assert report["denied_count"] == NONTRADE_WRITE_OPERATION_COUNT
    assert report["mcp_error_count"] == NONTRADE_WRITE_OPERATION_COUNT
    assert report["network_call_made"] is False
    assert report["preview_created"] is False
    assert report["approval_requested"] is False
    assert report["order_or_subscription_created"] is False
