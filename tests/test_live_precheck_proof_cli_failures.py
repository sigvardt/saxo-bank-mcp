from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastmcp.tools import ToolResult
from test_live_precheck_proof_support import (
    JSON_OBJECT_ADAPTER,
    TransportScenario,
    run_proof,
)

from saxo_bank_mcp.live_precheck_results import (
    PrecheckRequestSummary,
    precheck_response_result,
)


@pytest.mark.parametrize(
    "failure_mode",
    [
        "coercible_scalar",
        "error_flag",
        "nested_reserved_key",
        "nested_shadow_key",
        "nonfinite_nested",
        "order_identifier_signal",
    ],
)
def test_proof_rejects_unsafe_fastmcp_precheck_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    def error_flagged_result(
        response: httpx2.Response,
        *,
        account_id: str,
        account_ref: str,
        request_summary: PrecheckRequestSummary,
    ) -> ToolResult:
        accepted = precheck_response_result(
            response,
            account_id=account_id,
            account_ref=account_ref,
            request_summary=request_summary,
        )
        payload = JSON_OBJECT_ADAPTER.validate_python(accepted.structured_content)
        if failure_mode == "coercible_scalar":
            payload["child_result_count"] = "0"
        elif failure_mode == "nested_reserved_key":
            payload["audit"] = {"ErrorInfo": {"ErrorCode": "Rejected"}}
        elif failure_mode == "nested_shadow_key":
            payload["audit"] = {"precheck_result": "Rejected"}
        elif failure_mode == "nonfinite_nested":
            payload["audit"] = {"estimated_cost": float("inf")}
        elif failure_mode == "order_identifier_signal":
            payload["order_identifier_present"] = True
        return ToolResult(
            structured_content=payload,
            is_error=failure_mode == "error_flag",
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_live_trade_tools.precheck_response_result",
        error_flagged_result,
    )
    result, report, requests = run_proof(tmp_path, monkeypatch)

    assert result == 1
    assert report["status"] == "aborted"
    assert report["abort_stage"] == "precheck"
    assert report["abort_reason"] == "precheck_rejected_or_invalid"
    assert requests.count(("POST", "/openapi/trade/v2/orders/precheck")) == 1


def test_ambiguous_active_accounts_abort_before_state_or_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(account_count=2),
    )

    assert result == 1
    assert report["status"] == "aborted"
    assert report["abort_stage"] == "account_selection"
    assert report["abort_reason"] == "account_selection_required"
    assert report["account_counts"] == {"active": 2, "total": 2}
    assert requests == [("GET", "/openapi/port/v1/accounts/me")]
    assert "precheck" not in report


def test_nontradable_instrument_aborts_inside_precheck_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(instrument_tradable=False),
    )

    assert result == 1
    assert report["status"] == "aborted"
    assert report["abort_stage"] == "precheck"
    assert report["abort_reason"] == "precheck_rejected_or_invalid"
    assert requests[-2:] == [
        ("GET", "/openapi/port/v1/accounts/me"),
        ("GET", "/openapi/ref/v1/instruments/details/30031/Stock"),
    ]
    assert ("POST", "/openapi/trade/v2/orders/precheck") not in requests
