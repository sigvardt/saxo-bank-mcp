from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from saxo_bank_mcp import qa
from saxo_bank_mcp._redaction import REDACTED, redact_json
from saxo_bank_mcp.hard_task_manifest import (
    DEFAULT_INCOMPLETE_TOOL_IDS,
    HARD_TASK_SPECS,
    HardTaskSpec,
    validate_hard_task_manifest,
)
from saxo_bank_mcp.order_mutation_models import ORDER_WRITE_SPECS
from saxo_bank_mcp.qa_account import (
    QA_ACCOUNT_ENV,
    SimAccountKeyResolution,
    resolve_sim_account_key,
)
from saxo_bank_mcp.qa_order_probes import class_report_for_qa
from saxo_bank_mcp.qa_trade_probes import (
    DisclaimerProbeInput,
    disclaimer_lookup_status,
    disclaimer_response_status,
)

EXPECTED_HARD_TASK_TOOL_COUNT = 13


async def resolve_test_account(
    default_account_key: str = "SIM-ACCOUNT-1",
) -> SimAccountKeyResolution:
    return await resolve_sim_account_key(
        default_account_key=default_account_key,
        tool_name="test_tool",
    )


def test_default_hard_task_manifest_covers_exact_incomplete_tools() -> None:
    manifest = validate_hard_task_manifest(
        registered_tool_ids=DEFAULT_INCOMPLETE_TOOL_IDS,
    )

    assert manifest.status == "passed"
    assert manifest.expected_tool_count == EXPECTED_HARD_TASK_TOOL_COUNT
    assert manifest.covered_tool_count == EXPECTED_HARD_TASK_TOOL_COUNT
    assert manifest.covered_tool_ids == tuple(sorted(DEFAULT_INCOMPLETE_TOOL_IDS))
    assert manifest.missing_tool_ids == ()
    assert manifest.unexpected_tool_ids == ()
    assert manifest.duplicate_tool_ids == ()
    assert manifest.registered_missing_tool_ids == ()
    assert manifest.validation_errors == ()
    assert {spec.fastmcp_tool_name for spec in manifest.specs} == set(DEFAULT_INCOMPLETE_TOOL_IDS)


def test_hard_task_manifest_rejects_live_write_payload() -> None:
    data = HARD_TASK_SPECS[0].model_dump(mode="json")
    data["live_write_allowed"] = True

    with pytest.raises(ValidationError, match="live_write_allowed"):
        HardTaskSpec.model_validate(data)


def test_hard_task_manifest_rejects_risky_task_without_approval_gate() -> None:
    spec = HARD_TASK_SPECS[0].model_copy(update={"requires_two_factor_approval": False})

    manifest = validate_hard_task_manifest(
        (spec,),
        expected_tool_ids=(spec.tool_id,),
        registered_tool_ids=(spec.tool_id,),
    )

    assert manifest.status == "failed"
    assert manifest.validation_errors == (
        f"{spec.tool_id}: risky task requires two-factor approval gate",
    )


def test_hard_task_manifest_rejects_missing_auth_handling() -> None:
    spec = HARD_TASK_SPECS[0].model_copy(update={"allowed_noncompletion_statuses": ("failed",)})

    manifest = validate_hard_task_manifest(
        (spec,),
        expected_tool_ids=(spec.tool_id,),
        registered_tool_ids=(spec.tool_id,),
    )

    assert manifest.status == "failed"
    assert manifest.validation_errors == (
        f"{spec.tool_id}: missing credentials must fail as auth_required/incomplete_auth_required",
    )


def test_hard_task_manifest_rejects_unregistered_tool_reference() -> None:
    spec = HARD_TASK_SPECS[0]

    manifest = validate_hard_task_manifest(
        (spec,),
        expected_tool_ids=(spec.tool_id,),
        registered_tool_ids=("saxo_health",),
    )

    assert manifest.status == "failed"
    assert manifest.registered_missing_tool_ids == (spec.tool_id,)
    assert manifest.validation_errors == (
        f"hard task spec references unregistered FastMCP tool: {spec.tool_id}",
    )


def test_hard_task_manifest_qa_command_writes_secret_safe_report(tmp_path: Path) -> None:
    out = tmp_path / "hard-task-manifest.json"

    result = qa.main(["hard-task-manifest", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["expected_tool_count"] == EXPECTED_HARD_TASK_TOOL_COUNT
    assert report["covered_tool_count"] == EXPECTED_HARD_TASK_TOOL_COUNT
    assert report["missing_tool_ids"] == []
    assert report["validation_errors"] == []
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_sim_account_resolution_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(QA_ACCOUNT_ENV, "SIM-OVERRIDE")

    resolution = anyio.run(resolve_test_account)

    assert resolution.account_key == "SIM-OVERRIDE"
    assert resolution.source == "env_override"
    assert resolution.to_safe_json()["account_key_redacted"] is True


def test_sim_account_resolution_falls_back_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv(QA_ACCOUNT_ENV, raising=False)

    resolution = anyio.run(resolve_test_account)

    assert resolution.account_key == "SIM-ACCOUNT-1"
    assert resolution.source == "fixture_no_cached_token"
    assert resolution.discovered is False
    assert resolution.network_call_made is False


@pytest.mark.parametrize(
    ("command", "tool_name"),
    [
        ("trade-multileg-defaults", "saxo_get_multileg_order_defaults"),
        ("trade-disclaimer-lookup", "saxo_get_required_disclaimers"),
        ("trade-disclaimer-response", "saxo_register_disclaimer_response"),
    ],
)
def test_trade_hard_task_probes_use_fastmcp_without_leaking_secrets(
    command: str,
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "client-id-value")
    out = tmp_path / f"{command}.json"

    result = qa.main([command, "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "incomplete_auth_required"
    assert report["tool_name"] == tool_name
    assert report["fastmcp_called"] is True
    assert report["network_call_made"] is False
    assert report["live_write"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    assert "client-id-value" not in json.dumps(report)


def test_cancel_by_instrument_empty_success_is_not_mutation_proof() -> None:
    report = class_report_for_qa(
        ORDER_WRITE_SPECS["cancel-by-instrument"],
        {"status": "preview_created"},
        {
            "status": "completed",
            "network_call_made": True,
            "x_request_id_present": True,
            "order_result_parsed": True,
            "port_orders_readback": False,
            "trade_messages_readback": True,
            "mutation_may_have_occurred": True,
            "mutation_content_verified": False,
            "retry_unsafe": False,
            "order_cancelled": None,
        },
    )

    assert report["status"] == "exercised"
    assert report["real_mutation_proven"] is False
    assert report["port_orders_readback"] is False


def test_synthetic_disclaimer_errors_are_exercised_not_passed() -> None:
    synthetic_disclaimer_handle = "fixture-disclaimer-handle"
    probe_input = DisclaimerProbeInput(
        disclaimer_context="fixture-context",
        disclaimer_token=synthetic_disclaimer_handle,
        source="synthetic_invalid_token",
        real_disclaimer_input_found=False,
        discovery_status="real_precheck_disclaimer_not_available",
        discovery_network_call_made=True,
        precheck_http_status=400,
        precheck_error_code="",
        candidate_count=1,
    )

    lookup_status = disclaimer_lookup_status(
        {
            "status": "http_error",
            "response": {"ErrorCode": "InValidDisclaimerToken"},
        },
        probe_input,
    )
    response_status = disclaimer_response_status(
        {
            "status": "http_error",
            "response": {"ErrorCode": "DisclaimerFetchError"},
        },
        probe_input,
    )

    assert lookup_status == "exercised"
    assert response_status == "exercised"


def test_order_identifiers_are_redacted_from_evidence() -> None:
    redacted = redact_json(
        {
            "OrderId": "67762872",
            "order_ids": ["67762872"],
            "MultiLegOrderId": "abc123",
        },
    )

    assert redacted == {
        "OrderId": REDACTED,
        "order_ids": REDACTED,
        "MultiLegOrderId": REDACTED,
    }
