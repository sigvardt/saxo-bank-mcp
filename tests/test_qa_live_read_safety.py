"""LIVE read aggregate safety regression matrix. # noqa: SIZE_OK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp import qa, qa_live_probes
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.qa_live_read_contract import EXPECTED_NETWORK_CALL_COUNT

REQUIRED_STATUSES: Final[dict[str, str]] = {
    "saxo_get_session_capabilities": "passed",
    "saxo_get_entitlements": "passed",
    "saxo_list_registered_endpoints": "metadata_only_not_ready_for_trading",
    "saxo_call_registered_endpoint_public_diagnostics": "passed",
    "saxo_call_registered_endpoint_authenticated_account": "passed",
    "saxo_call_registered_endpoint_balances": "passed",
    "saxo_call_registered_endpoint_positions": "passed",
    "saxo_call_registered_endpoint_orders": "passed",
    "saxo_call_registered_endpoint_prices": "passed",
}

UNSAFE_VALUES: Final[tuple[tuple[str, JsonValue], ...]] = (
    ("true", True),
    ("zero", 0),
    ("empty_string", ""),
    ("empty_list", []),
    ("empty_object", {}),
    ("null", None),
)


def test_live_read_cli_passes_when_every_scenario_reports_exact_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", _safe_payloads)
    out = tmp_path / "live-read.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["tool_statuses"] == REQUIRED_STATUSES
    assert report["read_scenarios_exercised"] == list(REQUIRED_STATUSES)
    assert report["network_read_count"] == EXPECTED_NETWORK_CALL_COUNT


@pytest.mark.parametrize("scenario_change", ["extra", "missing"])
def test_live_read_cli_requires_the_exact_scenario_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_change: str,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    payloads = _safe_payloads_sync()
    if scenario_change == "extra":
        payloads["unexpected_live_read"] = {
            "status": "passed",
            "tool_name": "unexpected_live_read",
            "network_call_made": True,
            "live_write_called": False,
            "order_or_subscription_created": False,
        }
    else:
        del payloads["saxo_get_entitlements"]

    async def changed_payloads() -> dict[str, dict[str, JsonValue]]:
        return payloads

    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", changed_payloads)
    out = tmp_path / f"live-read-{scenario_change}-scenario.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"



@pytest.mark.parametrize("field", ["live_write_called", "order_or_subscription_created"])
def test_live_read_cli_fails_when_scenario_safety_field_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    payloads = _safe_payloads_sync()
    del payloads["saxo_get_session_capabilities"][field]

    async def unsafe_payloads() -> dict[str, dict[str, JsonValue]]:
        return payloads

    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", unsafe_payloads)
    out = tmp_path / "live-read.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["tool_statuses"] == REQUIRED_STATUSES
    assert report[field] is True


@pytest.mark.parametrize("field", ["live_write_called", "order_or_subscription_created"])
@pytest.mark.parametrize(("value_name", "value"), UNSAFE_VALUES)
def test_live_read_cli_fails_when_scenario_safety_field_is_not_exact_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value_name: str,
    value: JsonValue,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    payloads = _safe_payloads_sync()
    payloads["saxo_get_session_capabilities"][field] = value

    async def unsafe_payloads() -> dict[str, dict[str, JsonValue]]:
        return payloads

    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", unsafe_payloads)
    out = tmp_path / f"live-read-{field}-{value_name}.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["tool_statuses"] == REQUIRED_STATUSES
    assert report[field] is True


@pytest.mark.parametrize(
    ("scenario", "field"),
    [
        ("saxo_get_session_capabilities", "network_call_made"),
        ("saxo_list_registered_endpoints", "network_call_made"),
        ("saxo_call_registered_endpoint_public_diagnostics", "auth_exercised"),
        ("saxo_call_registered_endpoint_authenticated_account", "auth_exercised"),
    ],
)
def test_live_read_cli_fails_when_transport_proof_field_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    field: str,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    payloads = _safe_payloads_sync()
    del payloads[scenario][field]

    async def incomplete_payloads() -> dict[str, dict[str, JsonValue]]:
        return payloads

    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", incomplete_payloads)
    out = tmp_path / f"live-read-missing-{field}.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["tool_statuses"] == REQUIRED_STATUSES


@pytest.mark.parametrize(
    ("scenario", "field", "value"),
    [
        ("saxo_get_session_capabilities", "network_call_made", False),
        ("saxo_get_session_capabilities", "network_call_made", 1),
        ("saxo_list_registered_endpoints", "network_call_made", True),
        ("saxo_call_registered_endpoint_public_diagnostics", "auth_exercised", True),
        ("saxo_call_registered_endpoint_authenticated_account", "auth_exercised", False),
        ("saxo_call_registered_endpoint_authenticated_account", "auth_exercised", "true"),
    ],
)
def test_live_read_cli_fails_when_transport_proof_field_is_not_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    field: str,
    value: JsonValue,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    payloads = _safe_payloads_sync()
    payloads[scenario][field] = value

    async def invalid_payloads() -> dict[str, dict[str, JsonValue]]:
        return payloads

    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", invalid_payloads)
    out = tmp_path / f"live-read-invalid-{field}.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["tool_statuses"] == REQUIRED_STATUSES


@pytest.mark.parametrize(
    ("scenario", "field", "value"),
    [
        ("saxo_get_session_capabilities", "tool_name", "wrong_tool"),
        ("saxo_call_registered_endpoint_balances", "operation_id", "wrong.operation"),
        (
            "saxo_call_registered_endpoint_balances",
            "response_fingerprint_scope",
            "raw_response_body",
        ),
    ],
)
def test_live_read_cli_binds_each_scenario_to_its_expected_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    field: str,
    value: str,
) -> None:
    _enable_live_probe(tmp_path, monkeypatch)
    payloads = _safe_payloads_sync()
    payloads[scenario][field] = value

    async def changed_payloads() -> dict[str, dict[str, JsonValue]]:
        return payloads

    monkeypatch.setattr(qa_live_probes, "call_live_read_payloads", changed_payloads)
    out = tmp_path / "live-read-identity-mismatch.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(tmp_path / "skip.json")])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "failed"


async def _safe_payloads() -> dict[str, dict[str, JsonValue]]:
    return _safe_payloads_sync()


def _safe_payloads_sync() -> dict[str, dict[str, JsonValue]]:
    payloads: dict[str, dict[str, JsonValue]] = {
        scenario: {
            "status": status,
            "tool_name": scenario,
            "network_call_made": True,
            "live_write_called": False,
            "order_or_subscription_created": False,
        }
        for scenario, status in REQUIRED_STATUSES.items()
    }
    payloads["saxo_list_registered_endpoints"]["network_call_made"] = False
    payloads["saxo_call_registered_endpoint_public_diagnostics"]["auth_exercised"] = False
    payloads["saxo_get_session_capabilities"]["tool_name"] = "saxo_get_session_capabilities"
    payloads["saxo_get_entitlements"]["tool_name"] = "saxo_get_entitlements"
    payloads["saxo_list_registered_endpoints"]["tool_name"] = (
        "saxo_list_registered_endpoints"
    )
    registered_identities = {
        "saxo_call_registered_endpoint_public_diagnostics": (
            "get.root.v1.diagnostics.get",
            "/root/v1/diagnostics/get",
        ),
        "saxo_call_registered_endpoint_authenticated_account": (
            "get.port.v1.accounts.me",
            "/port/v1/accounts/me",
        ),
        "saxo_call_registered_endpoint_balances": (
            "get.port.v1.balances.me",
            "/port/v1/balances/me",
        ),
        "saxo_call_registered_endpoint_positions": (
            "get.port.v1.positions.me",
            "/port/v1/positions/me",
        ),
        "saxo_call_registered_endpoint_orders": (
            "get.port.v1.orders.me",
            "/port/v1/orders/me",
        ),
        "saxo_call_registered_endpoint_prices": (
            "get.trade.v1.infoprices",
            "/trade/v1/infoprices",
        ),
    }
    for scenario, (operation_id, path) in registered_identities.items():
        payloads[scenario].update(
            {
                "tool_name": "saxo_call_registered_endpoint",
                "operation_id": operation_id,
                "method": "GET",
                "path": path,
                "http_status": 200,
                "response_fingerprint_scope": (
                    "account_money_state_fields"
                    if scenario == "saxo_call_registered_endpoint_balances"
                    else "raw_response_body"
                ),
            },
        )
    for scenario in (
        "saxo_call_registered_endpoint_authenticated_account",
        "saxo_call_registered_endpoint_balances",
        "saxo_call_registered_endpoint_positions",
        "saxo_call_registered_endpoint_orders",
        "saxo_call_registered_endpoint_prices",
    ):
        payloads[scenario]["auth_exercised"] = True
    return payloads


def _enable_live_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "fixture-live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "token-cache.json"))
