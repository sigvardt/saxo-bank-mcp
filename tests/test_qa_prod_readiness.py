from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import qa, qa_prod_readiness
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.http_client import create_async_client

EXPECTED_PROD_READINESS_RAPID_ITERATIONS = 16
ARGPARSE_USAGE_ERROR = 2


def test_prod_readiness_reports_saxo_live_access_requirements(tmp_path: Path) -> None:
    out = tmp_path / "prod-readiness.json"

    result = qa.main(["prod-readiness", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    requirement_ids = {item["id"] for item in report["requirements"]}
    assert result == 0
    assert report["status"] == "passed"
    assert report["status_scope"] == "code_safety_checks_only"
    assert report["code_safety_checks_passed"] is True
    assert report["production_ready"] is False
    assert report["command"] == "prod-readiness"
    assert report["live_write_ready"] is False
    assert report["network_call_made"] is False
    assert report["order_or_subscription_created"] is False
    assert report["rapid_call_probe"]["status"] == "passed"
    assert report["rapid_call_probe"]["iterations"] == EXPECTED_PROD_READINESS_RAPID_ITERATIONS
    assert report["rapid_call_probe"]["failures"] == []
    assert report["live_write_refusal_probe"]["status"] == "refused"
    assert report["live_write_refusal_probe"]["transport_constructed"] is False
    assert report["live_write_refusal_probe"]["network_call_made"] is False
    assert report["live_write_refusal_probe"]["order_or_subscription_created"] is False
    assert report["secret_scan"]["findings"] == []
    assert report["secret_scan"]["scan_errors"] == []
    assert "pyproject.toml" in report["secret_scan"]["paths"]
    assert report["transport_constructed"] is False
    acceptable_statuses = {
        "implemented",
        "refused_until_live_enablement",
        "evidence_required_live",
    }
    assert not [
        item for item in report["requirements"] if item["status"] not in acceptable_statuses
    ]
    assert {
        "public_secret_containment",
        "pkce_saxo_login",
        "monkey_rapid_calls",
        "openapi_400_investigation",
        "throttling_409_429",
        "many_positions_orders",
        "currency_and_price_display",
        "fractional_amounts",
        "all_order_mutation_shapes",
        "invalid_order_prevention",
        "automated_trading_limits",
        "versioning_tolerance",
        "sim_before_live",
        "live_write_refusal",
    } <= requirement_ids


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("network_call_made", "true"),
        ("live_write_called", "true"),
        ("order_or_subscription_created", "true"),
        ("network_call_made", "missing"),
        ("live_write_called", "missing"),
        ("order_or_subscription_created", "missing"),
    ],
)
def test_prod_readiness_fails_closed_and_reports_unsafe_refusal_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutation: str,
) -> None:
    payload: dict[str, JsonValue] = {
        "status": "refused",
        "refusal_reason": "missing_live_write_enablement",
        "missing_requirements": [],
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }
    if mutation == "true":
        payload[field] = True
    else:
        del payload[field]
    monkeypatch.setattr(qa_prod_readiness, "_run_live_write_refusal_probe", lambda: payload)
    out = tmp_path / "prod-readiness.json"

    result = qa.main(["prod-readiness", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["code_safety_checks_passed"] is False
    assert report["production_ready"] is False
    assert report[field] is True


def test_prod_readiness_fails_for_wrong_live_write_refusal_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wrong_reason_payload() -> dict[str, JsonValue]:
        return {
            "status": "refused",
            "refusal_reason": "unexpected_reason",
            "missing_requirements": [],
            "transport_constructed": False,
            "network_call_made": False,
            "live_write_called": False,
            "order_or_subscription_created": False,
        }

    monkeypatch.setattr(
        qa_prod_readiness,
        "_run_live_write_refusal_probe",
        wrong_reason_payload,
    )
    out = tmp_path / "prod-readiness.json"

    result = qa.main(["prod-readiness", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"


def test_prod_readiness_refusal_probe_fails_when_transport_is_constructed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def construct_transport() -> dict[str, JsonValue]:
        async with create_async_client():
            return {}

    monkeypatch.setattr(
        qa_prod_readiness,
        "call_live_write_refusal_payload",
        construct_transport,
    )
    out = tmp_path / "prod-readiness.json"

    result = qa.main(["prod-readiness", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["transport_constructed"] is True


@pytest.mark.parametrize(
    "field",
    ["network_call_made", "live_write_called", "order_or_subscription_created"],
)
@pytest.mark.parametrize("false_like", [0, "", [], {}, None])
def test_prod_readiness_refusal_probe_rejects_false_like_non_booleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    false_like: JsonValue,
) -> None:
    async def malformed_payload() -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "status": "refused",
            "refusal_reason": "missing_live_write_enablement",
            "network_call_made": False,
            "live_write_called": False,
            "order_or_subscription_created": False,
        }
        payload[field] = false_like
        return payload

    monkeypatch.setattr(
        qa_prod_readiness,
        "call_live_write_refusal_payload",
        malformed_payload,
    )
    out = tmp_path / "prod-readiness.json"

    result = qa.main(["prod-readiness", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report[field] is True


def test_prod_readiness_requires_output_path(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        qa.main(["prod-readiness"])

    assert error.value.code == ARGPARSE_USAGE_ERROR
    assert "--out" in capsys.readouterr().err
