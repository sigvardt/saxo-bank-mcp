from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_live_precheck_proof_support import (
    ACCESS_TOKEN,
    EXPECTED_SHA256_LENGTH,
    EXPECTED_TOKEN_VALIDITY_SECONDS,
    JSON_OBJECT_ADAPTER,
    JSON_ROWS_ADAPTER,
    RAW_ACCOUNT,
    RAW_BALANCE,
    RAW_MESSAGE,
    RAW_ORDER,
    RAW_POSITION,
    run_proof,
)


def test_proof_completes_through_fastmcp_with_unchanged_state(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(tmp_path, monkeypatch)

    assert result == 0
    assert report["status"] == "completed"
    assert report["driver"] == (
        "FastMCP protocol client with exposed session ledger and out-of-process "
        "transport-boundary capture"
    )
    assert report["token_validity_lower_bound_seconds"] == EXPECTED_TOKEN_VALIDITY_SECONDS
    assert report["account_counts"] == {"active": 1, "total": 1}
    assert report["instrument"] == {
        "amount": 1.0,
        "asset_type": "Stock",
        "buy_sell": "Buy",
        "uic": 30031,
        "verified_tradable_before_precheck": True,
    }
    account_binding = JSON_OBJECT_ADAPTER.validate_python(report["account_binding"])
    assert account_binding["source"] == "visible_account_id_and_process_scoped_ref"
    assert account_binding["account_id"] == "<redacted>"
    assert account_binding["account_position"] == 1
    assert isinstance(account_binding["selector_sha256"], str)
    assert len(account_binding["selector_sha256"]) == EXPECTED_SHA256_LENGTH
    assert report["before_counts"] == {"orders": 0, "positions": 0, "trade_messages": 1}
    assert report["after_counts"] == report["before_counts"]
    assert report["response_structure"] == {
        "account_declared_count_consistent": True,
        "after": {
            "orders": {
                "declared_count_consistent": True,
                "declared_count_present": True,
                "shape": "data_envelope",
            },
            "positions": {
                "declared_count_consistent": True,
                "declared_count_present": True,
                "shape": "data_envelope",
            },
            "trade_messages": {
                "declared_count_consistent": None,
                "declared_count_present": False,
                "shape": "top_level_array",
            },
        },
        "before": {
            "orders": {
                "declared_count_consistent": True,
                "declared_count_present": True,
                "shape": "data_envelope",
            },
            "positions": {
                "declared_count_consistent": True,
                "declared_count_present": True,
                "shape": "data_envelope",
            },
            "trade_messages": {
                "declared_count_consistent": None,
                "declared_count_present": False,
                "shape": "top_level_array",
            },
        },
        "state_read_scope": "all_accounts_me",
    }
    assert report["unchanged"] == {
        "account_money_state_fields": True,
        "orders": True,
        "orders_count": True,
        "positions": True,
        "positions_count": True,
        "trade_messages": True,
        "trade_messages_count": True,
    }
    assert report["account_money_state_scope"] == {
        "fingerprint_scope": "modeled_account_money_state_fields",
        "no_change_conclusion_requires": [
            "modeled_account_money_state_fields_unchanged",
            "complete_no_write_transport_evidence",
        ],
        "limitation": (
            "not_proof_that_every_possible_saxo_balance_field_was_observed"
        ),
    }
    precheck = JSON_OBJECT_ADAPTER.validate_python(report["precheck"])
    secret_scan = JSON_OBJECT_ADAPTER.validate_python(report["secret_scan"])
    ledger = JSON_ROWS_ADAPTER.validate_python(report["request_ledger"])
    mcp_ledger = JSON_OBJECT_ADAPTER.validate_python(report["mcp_request_ledger"])
    assert precheck["status"] == "precheck_accepted"
    assert precheck["root_result_explicitly_ok"] is True
    assert precheck["child_result_count"] == 0
    assert precheck["all_returned_results_explicitly_ok"] is True
    assert precheck["disclaimer_object_present"] is False
    assert precheck["error_object_present"] is False
    assert precheck["account_lookup_endpoint_called"] is True
    assert precheck["instrument_lookup_endpoint_called"] is True
    assert precheck["precheck_endpoint_called"] is True
    assert precheck["estimated_cash_required_value_present"] is True
    assert precheck["estimated_cash_required_currency_present"] is True
    assert precheck["estimated_total_cost_in_account_currency_value_present"] is True
    assert "estimated_cash_required" not in precheck
    assert "estimated_cash_required_currency" not in precheck
    assert "estimated_total_cost_in_account_currency" not in precheck
    assert precheck["order_placement_endpoint_called"] is False
    assert precheck["order_change_endpoint_called"] is False
    assert precheck["order_cancel_endpoint_called"] is False
    assert precheck["disclaimer_response_endpoint_called"] is False
    assert precheck["order_identifier_present"] is False
    assert precheck["requires_order_readback"] is False
    assert precheck["live_write_called"] is False
    assert precheck["order_or_subscription_created"] is False
    assert precheck["request_summary"] == {
        "amount": 1.0,
        "asset_type": "Stock",
        "buy_sell": "Buy",
        "duration_type": "DayOrder",
        "field_groups": ["Costs", "MarginImpactBuySell"],
        "manual_order": False,
        "order_type": "Market",
        "uic": 30031,
    }
    assert secret_scan["clean"] is True
    assert report["request_ledger_parity"] is True
    assert report["transport_boundary_parity"] is True
    boundary = JSON_OBJECT_ADAPTER.validate_python(report["transport_boundary_capture"])
    assert boundary["collector_process"] == "separate_process"
    assert boundary["collector_complete"] is True
    assert boundary["transport_layer"] == "httpx_async_base_transport"
    assert mcp_ledger["ledger_complete"] is True
    assert mcp_ledger["negative_proof_available"] is True
    assert mcp_ledger["order_placement_endpoint_called"] is False
    assert mcp_ledger["only_precheck_gateway_non_get"] is True
    assert requests.count(("POST", "/openapi/trade/v2/orders/precheck")) == 1
    assert len(ledger) == len(requests) * 2


def test_final_json_contains_no_sensitive_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, report, _ = run_proof(tmp_path, monkeypatch)
    serialized = json.dumps(report, sort_keys=True)

    for sensitive in (
        RAW_ACCOUNT,
        RAW_ORDER,
        RAW_POSITION,
        RAW_MESSAGE,
        ACCESS_TOKEN,
        "live-account-",
        "Authorization",
    ):
        assert sensitive not in serialized
    for private_number in (RAW_BALANCE, 10.5, 10.75):
        assert f": {private_number}" not in serialized
    assert "?" not in serialized
