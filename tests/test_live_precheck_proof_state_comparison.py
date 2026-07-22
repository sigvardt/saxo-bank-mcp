from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from test_live_precheck_proof_support import (
    JSON_OBJECT_ADAPTER,
    TransportScenario,
    run_proof,
)

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_precheck_collection_models import (
    CollectionPayload,
    StateCollectionStructureJson,
)
from saxo_bank_mcp.live_precheck_proof_models import (
    AccountListing,
    RegisteredRead,
    StateSnapshot,
)
from saxo_bank_mcp.live_precheck_state_compare import unchanged_state


def test_unchanged_state_uses_account_money_state_fields_key() -> None:
    collection_structure: StateCollectionStructureJson = {
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
    }
    before = StateSnapshot(
        counts={"orders": 0, "positions": 0, "trade_messages": 0},
        balance_fingerprint="same",
        orders_fingerprint="same",
        positions_fingerprint="same",
        trade_messages_fingerprint="same",
        collection_structure=collection_structure,
    )
    after = StateSnapshot(
        counts={"orders": 0, "positions": 0, "trade_messages": 0},
        balance_fingerprint="same",
        orders_fingerprint="same",
        positions_fingerprint="same",
        trade_messages_fingerprint="same",
        collection_structure=collection_structure,
    )

    unchanged = unchanged_state(before, after)

    assert unchanged["account_money_state_fields"] is True
    assert "balances" not in unchanged


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ErrorCode": "Failure"},
        {"data": []},
        {"Data": [], "__next": "pagination-is-not-proven"},
    ],
)
def test_collection_payload_rejects_unknown_object_shapes(payload: JsonValue) -> None:
    with pytest.raises(ValidationError):
        CollectionPayload.model_validate(payload)


def test_collection_payload_rejects_declared_count_mismatch() -> None:
    with pytest.raises(ValidationError):
        CollectionPayload.model_validate({"Data": [], "__count": 1})


@pytest.mark.parametrize(
    "payload",
    [{"Data": [], "__count": 0}, {"Data": [], "MaxRows": 1000, "__count": 0}],
)
def test_collection_payload_accepts_observed_collection_shapes(payload: JsonValue) -> None:
    assert CollectionPayload.model_validate(payload).count == 0


def test_collection_payload_rejects_missing_declared_count() -> None:
    with pytest.raises(ValidationError):
        CollectionPayload.model_validate({"Data": []})


def test_collection_payload_rejects_top_level_array() -> None:
    with pytest.raises(ValidationError):
        CollectionPayload.model_validate([])


def test_registered_read_rejects_unshipped_cash_state_scope() -> None:
    with pytest.raises(ValidationError):
        RegisteredRead.model_validate(
            {
                "status": "passed",
                "environment": "LIVE",
                "method": "GET",
                "path": "/port/v1/balances/me",
                "response": None,
                "response_fingerprint": "0" * 64,
                "response_fingerprint_scope": "account_cash_state_fields",
                "live_write_called": False,
                "order_or_subscription_created": False,
            },
        )


@pytest.mark.parametrize(
    "safety_fields",
    [
        {},
        {"live_write_called": True, "order_or_subscription_created": False},
        {"live_write_called": False, "order_or_subscription_created": True},
    ],
)
def test_account_listing_requires_exact_false_safety_fields(
    safety_fields: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        AccountListing.model_validate(
            {
                "status": "accounts_listed",
                "environment": "LIVE",
                "accounts": [],
                "account_count": 0,
                "active_account_count": 0,
                **safety_fields,
            },
        )


@pytest.mark.parametrize(
    "safety_fields",
    [
        {},
        {"live_write_called": True, "order_or_subscription_created": False},
        {"live_write_called": False, "order_or_subscription_created": True},
    ],
)
def test_registered_read_requires_exact_false_safety_fields(
    safety_fields: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        RegisteredRead.model_validate(
            {
                "status": "passed",
                "environment": "LIVE",
                "method": "GET",
                "path": "/port/v1/balances/me",
                "response": None,
                "response_fingerprint": "0" * 64,
                "response_fingerprint_scope": "account_money_state_fields",
                **safety_fields,
            },
        )


def test_proof_rejects_trade_messages_envelope_with_named_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, requests = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(messages_envelope=True),
    )

    assert result == 1
    assert report["status"] == "aborted"
    assert report["abort_stage"] == "state_before"
    assert report["abort_reason"] == "state_collection_shape_invalid"
    assert requests.count(("POST", "/openapi/trade/v2/orders/precheck")) == 0


@pytest.mark.parametrize(
    "scenario",
    [
        TransportScenario(orders_top_level_list=True),
        TransportScenario(duplicate_order_count=True),
    ],
)
def test_proof_rejects_invalid_order_collection_shape_before_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: TransportScenario,
) -> None:
    result, report, requests = run_proof(tmp_path, monkeypatch, scenario)

    assert result == 1
    assert report["status"] == "aborted"
    assert report["abort_stage"] == "state_before"
    assert report["abort_reason"] == "state_collection_shape_invalid"
    assert requests.count(("POST", "/openapi/trade/v2/orders/precheck")) == 0


@pytest.mark.parametrize(
    ("collection", "unchanged_field"),
    [
        ("orders", "orders"),
        ("positions", "positions"),
        ("trade_messages", "trade_messages"),
    ],
)
def test_same_count_changed_collection_aborts_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collection: Literal["orders", "positions", "trade_messages"],
    unchanged_field: str,
) -> None:
    result, report, _ = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(mutate_after_precheck=collection, nonzero_state=True),
    )

    unchanged = JSON_OBJECT_ADAPTER.validate_python(report["unchanged"])
    assert result == 1
    assert report["status"] == "aborted"
    assert report["before_counts"] == report["after_counts"]
    assert unchanged[unchanged_field] is False
    assert unchanged[f"{unchanged_field}_count"] is True


def test_unchanged_nonempty_orders_and_positions_complete_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, _ = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(nonzero_state=True),
    )

    unchanged = JSON_OBJECT_ADAPTER.validate_python(report["unchanged"])
    assert result == 0
    assert report["status"] == "completed"
    assert report["before_counts"] == {
        "orders": 1,
        "positions": 1,
        "trade_messages": 1,
    }
    assert report["after_counts"] == report["before_counts"]
    assert unchanged["account_money_state_fields"] is True
    assert unchanged["orders"] is True
    assert unchanged["orders_count"] is True
    assert unchanged["positions"] is True
    assert unchanged["positions_count"] is True


def test_completed_proof_does_not_persist_money_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, report, _ = run_proof(
        tmp_path,
        monkeypatch,
        TransportScenario(nonzero_state=True),
    )

    unchanged = JSON_OBJECT_ADAPTER.validate_python(report["unchanged"])
    serialized = str(report)

    assert result == 0
    assert unchanged["account_money_state_fields"] is True
    assert isinstance(unchanged["account_money_state_fields"], bool)
    assert "balance_fingerprint" not in serialized
    assert "response_fingerprint" not in serialized
