from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastmcp import Client

import saxo_bank_mcp.safety as safety_module
import saxo_bank_mcp.safety_checks as safety_checks_module
from saxo_bank_mcp.audit import audit_log_path
from saxo_bank_mcp.safety import (
    TEST_APPROVAL_FACTOR,
    AccountCurrencyRisk,
    SafetyConfig,
    SafetyKernel,
    WritePreviewRequest,
    reset_safety_state,
)
from saxo_bank_mcp.server import mcp


def write_fixture() -> WritePreviewRequest:
    return WritePreviewRequest(
        operation_id="trade.order.place",
        account_key="SIM-ACCOUNT-1",
        instrument_uic=21,
        quantity=10,
        estimated_notional=500,
        account_currency="USD",
        risk=AccountCurrencyRisk(
            cost=500,
            cash_required=500,
            margin_impact=20,
            contract_multiplier=1,
            conversion_known=True,
        ),
        request_body={"BuySell": "Buy", "OrderType": "Market"},
    )


def safety_config(tmp_path: Path) -> SafetyConfig:
    return SafetyConfig(
        environment="SIM",
        live_writes_enabled=False,
        global_kill_switch=False,
        account_allowlist=frozenset({"SIM-ACCOUNT-1"}),
        instrument_allowlist=frozenset({21}),
        max_quantity=100,
        max_notional=1_000,
        audit_dir=tmp_path / "audit",
    )


@pytest.mark.parametrize(
    ("config_changes", "request_changes", "reason"),
    [
        ({"global_kill_switch": True}, {}, "global_kill_switch_active"),
        ({"account_allowlist": frozenset({"OTHER"})}, {}, "account_not_allowlisted"),
        ({"instrument_allowlist": frozenset({99})}, {}, "instrument_not_allowlisted"),
        ({}, {"quantity": 101}, "quantity_limit_exceeded"),
        ({}, {"estimated_notional": 1_001}, "notional_limit_exceeded"),
        (
            {},
            {
                "risk": AccountCurrencyRisk(
                    cost=500,
                    cash_required=500,
                    margin_impact=20,
                    contract_multiplier=1,
                    conversion_known=False,
                ),
            },
            "account_currency_conversion_unknown",
        ),
        (
            {},
            {
                "risk": AccountCurrencyRisk(
                    cost=500,
                    cash_required=500,
                    margin_impact=None,
                    contract_multiplier=1,
                    conversion_known=True,
                ),
            },
            "margin_impact_unknown",
        ),
        (
            {},
            {
                "risk": AccountCurrencyRisk(
                    cost=500,
                    cash_required=500,
                    margin_impact=20,
                    contract_multiplier=None,
                    conversion_known=True,
                ),
            },
            "contract_multiplier_unknown",
        ),
    ],
)
def test_safety_preview_denies_exact_missing_condition(
    tmp_path: Path,
    config_changes: dict[str, Any],
    request_changes: dict[str, Any],
    reason: str,
) -> None:
    config = safety_config(tmp_path).model_copy(update=config_changes)
    request = write_fixture().model_copy(update=request_changes)

    result = SafetyKernel(config).create_preview(request)

    assert result["status"] == "denied"
    assert reason in result.get("denial_reasons", [])
    assert result["environment"] == "SIM"
    assert result["saxo_endpoint_called"] is False


def test_safety_preview_commit_is_autonomous_in_sim_and_writes_owner_only_audit(
    tmp_path: Path,
) -> None:
    reset_safety_state()
    kernel = SafetyKernel(safety_config(tmp_path))
    preview = kernel.create_preview(write_fixture())

    assert preview["status"] == "preview_created"
    assert "preview_token" in preview
    preview_token = str(preview.get("preview_token", ""))
    approved = kernel.commit_preview(preview_token, approval_factor=None)

    assert approved["status"] == "approved_for_simulation"
    assert approved["execution_performed"] is False
    assert approved["saxo_endpoint_called"] is False
    assert approved["simulation_only"] is True
    assert approved["order_placed"] is False
    assert "safe for later write layer" not in approved["next_action"]
    assert "SIM needs no human approval" in preview["next_action"]

    path = audit_log_path(tmp_path / "audit")
    assert not path.resolve(strict=False).is_relative_to(Path.cwd().resolve(strict=False))
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {event["event"] for event in events} >= {
        "preview_created",
        "commit_approved",
    }


def test_safety_duplicate_and_rate_guards_block_reuse(tmp_path: Path) -> None:
    reset_safety_state()
    kernel = SafetyKernel(safety_config(tmp_path))
    first = kernel.create_preview(write_fixture())
    assert first["status"] == "preview_created"
    first_token = str(first.get("preview_token", ""))
    assert (
        kernel.commit_preview(first_token, approval_factor=TEST_APPROVAL_FACTOR)["status"]
        == "approved_for_simulation"
    )

    duplicate_preview = kernel.create_preview(write_fixture())
    assert duplicate_preview["status"] == "denied"
    assert "duplicate_request" in duplicate_preview.get("denial_reasons", [])


def test_commit_rechecks_current_kill_switch(tmp_path: Path) -> None:
    reset_safety_state()
    preview = SafetyKernel(safety_config(tmp_path)).create_preview(write_fixture())
    assert preview["status"] == "preview_created"
    token = str(preview.get("preview_token", ""))

    stopped = SafetyKernel(
        safety_config(tmp_path).model_copy(update={"global_kill_switch": True}),
    ).commit_preview(token, approval_factor=None)

    assert stopped["status"] == "denied"
    assert stopped.get("denial_reason") == "global_kill_switch_active"
    assert stopped["saxo_endpoint_called"] is False


def test_preview_audit_failure_does_not_store_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()

    def fail_preview_audit(*_args: object, **_kwargs: object) -> Path:
        raise OSError("preview audit unavailable")

    monkeypatch.setattr(safety_module, "append_audit_event", fail_preview_audit)
    kernel = SafetyKernel(safety_config(tmp_path))

    result = kernel.create_preview(write_fixture())

    assert result["status"] == "denied"
    assert "audit_write_failed" in result.get("denial_reasons", [])
    status = kernel.status()
    assert status["pending_preview_count"] == 0
    assert status["committed_fingerprint_count"] == 0


def test_preview_check_audit_oserror_denies_without_storing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()

    def fail_preview_check(*_args: object, **_kwargs: object) -> Path:
        raise OSError("preview check audit unavailable")

    monkeypatch.setattr(safety_checks_module, "append_audit_event", fail_preview_check)
    kernel = SafetyKernel(safety_config(tmp_path))

    result = kernel.create_preview(write_fixture())

    assert result["status"] == "denied"
    assert "audit_write_failed" in result.get("denial_reasons", [])
    status = kernel.status()
    assert status["pending_preview_count"] == 0
    assert status["committed_fingerprint_count"] == 0


def test_commit_audit_failure_does_not_mark_request_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    kernel = SafetyKernel(safety_config(tmp_path))
    preview = kernel.create_preview(write_fixture())
    assert preview["status"] == "preview_created"
    token = str(preview.get("preview_token", ""))

    def fail_commit_audit(*_args: object, **_kwargs: object) -> Path:
        raise OSError("commit audit unavailable")

    monkeypatch.setattr(safety_module, "append_audit_event", fail_commit_audit)

    result = kernel.commit_preview(token, approval_factor=TEST_APPROVAL_FACTOR)

    assert result["status"] == "denied"
    assert result.get("denial_reason") == "audit_write_failed"
    status = kernel.status()
    assert status["pending_preview_count"] == 1
    assert status["committed_fingerprint_count"] == 0


def test_audit_path_refuses_repository(tmp_path: Path) -> None:
    config = safety_config(tmp_path).model_copy(update={"audit_dir": Path.cwd() / ".audit"})

    result = SafetyKernel(config).create_preview(write_fixture())

    assert result["status"] == "denied"
    assert "audit_path_refused" in result.get("denial_reasons", [])


def test_fastmcp_safety_tools_preview_and_autonomous_sim_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", "SIM-ACCOUNT-1")
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", "21")

    async def call_tools() -> tuple[dict[str, object], dict[str, object]]:
        async with Client(mcp) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed}
            expected_tools = {
                "saxo_create_write_preview",
                "saxo_commit_write_preview",
                "saxo_safety_status",
            }
            assert expected_tools <= names
            preview_result = await client.call_tool(
                "saxo_create_write_preview",
                write_fixture().model_dump(mode="json"),
            )
            preview = preview_result.structured_content
            assert isinstance(preview, dict)
            approved_result = await client.call_tool(
                "saxo_commit_write_preview",
                {"preview_token": preview["preview_token"]},
            )
            approved = approved_result.structured_content
        assert isinstance(approved, dict)
        return preview, approved

    preview, approved = anyio.run(call_tools)

    assert preview["status"] == "preview_created"
    assert approved["status"] == "approved_for_simulation"
    assert approved["approval_factor_mode"] == "autonomous_sim"
    assert "SIM-ACCOUNT-1" not in json.dumps(approved)


def test_live_configuration_requires_enablement_then_one_exact_chat_approval(
    tmp_path: Path,
) -> None:
    reset_safety_state()
    live_config = safety_config(tmp_path).model_copy(update={"environment": "LIVE"})
    live_preview = SafetyKernel(live_config).create_preview(write_fixture())

    assert live_preview["status"] == "denied"
    assert live_preview["order_placed"] is False
    assert "live_writes_disabled" in live_preview.get("denial_reasons", [])

    reset_safety_state()
    live_kernel = SafetyKernel(live_config.model_copy(update={"live_writes_enabled": True}))
    preview = live_kernel.create_preview(write_fixture())
    assert preview["status"] == "preview_created"
    token = str(preview.get("preview_token", ""))
    prompt = str(preview.get("approval_prompt", ""))

    missing = live_kernel.commit_preview(token, approval_factor=None)
    wrong = live_kernel.commit_preview(token, approval_factor="APPROVE SOMETHING ELSE")
    approved = live_kernel.commit_preview(token, approval_factor=prompt)

    assert missing.get("denial_reason") == "chat_approval_missing"
    assert wrong.get("denial_reason") == "chat_approval_mismatch"
    assert approved["status"] == "approved_for_execution"
    assert approved["simulation_only"] is False
    assert preview["request_fingerprint"] in prompt


def test_identical_live_order_previews_require_distinct_chat_approvals(
    tmp_path: Path,
) -> None:
    reset_safety_state()
    live_kernel = SafetyKernel(
        safety_config(tmp_path).model_copy(
            update={"environment": "LIVE", "live_writes_enabled": True},
        ),
    )

    first = live_kernel.create_preview(write_fixture())
    second = live_kernel.create_preview(write_fixture())

    assert first["status"] == "preview_created"
    assert second["status"] == "preview_created"
    assert first["request_fingerprint"] == second["request_fingerprint"]
    first_token = first.get("preview_token")
    second_token = second.get("preview_token")
    first_prompt = first.get("approval_prompt")
    second_prompt = second.get("approval_prompt")
    assert isinstance(first_token, str)
    assert isinstance(second_token, str)
    assert isinstance(first_prompt, str)
    assert isinstance(second_prompt, str)
    assert first_token != second_token
    assert first_prompt != second_prompt
    wrong_preview = live_kernel.commit_preview(
        second_token,
        approval_factor=first_prompt,
    )
    assert wrong_preview.get("denial_reason") == "chat_approval_mismatch"


def test_live_write_flag_is_denied_as_simulation_boundary(tmp_path: Path) -> None:
    reset_safety_state()
    config = safety_config(tmp_path).model_copy(update={"live_writes_enabled": True})
    preview = SafetyKernel(config).create_preview(write_fixture())

    assert preview["status"] == "denied"
    assert "live_write_flag_invalid_in_sim" in preview.get("denial_reasons", [])
