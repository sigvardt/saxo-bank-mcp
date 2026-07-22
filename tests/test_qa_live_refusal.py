from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import qa, qa_live_probes
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.http_client import create_async_client


def test_live_read_skips_without_enablement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)
    out = tmp_path / "live.json"
    skip = tmp_path / "skip.json"

    result = qa.main(["live-read", "--out", str(out), "--skip-out", str(skip)])

    assert result == 0
    assert json.loads(skip.read_text(encoding="utf-8"))["status"] == "skipped_no_live_credentials"


def test_live_write_refusal_still_refuses_when_only_enable_var_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")
    out = tmp_path / "live-write.json"

    result = qa.main(["live-write-refusal", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "refused"
    assert report["refusal_reason"] == "missing_live_write_enablement"
    expected_requirements = {
        "SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK",
        "LIVE credentials",
        "LIVE account allowlist",
        "low notional and quantity limits",
        "kill switch ready",
        "server-created preview token",
        "one exact-action approval statement sent by the human in agent chat",
        "precheck/defaults before placement",
        "throttling and duplicate-submit guard",
        "redacted audit trail outside repository",
        "daily activity review/monitoring",
        "explicit later live-write enablement decision",
    }
    assert expected_requirements <= set(report["missing_requirements"])
    assert report["network_call_made"] is False


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
def test_live_write_refusal_gate_fails_closed_for_unsafe_or_missing_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutation: str,
) -> None:
    payload: dict[str, JsonValue] = {
        "status": "refused",
        "refusal_reason": "missing_live_write_enablement",
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }
    if mutation == "true":
        payload[field] = True
    else:
        del payload[field]

    async def unsafe_payload() -> dict[str, JsonValue]:
        return payload

    monkeypatch.setattr(qa_live_probes, "call_live_write_refusal_payload", unsafe_payload)
    out = tmp_path / "live-write.json"

    result = qa_live_probes.handle_live_write_refusal(out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"


def test_live_write_refusal_gate_fails_when_transport_is_constructed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def construct_transport() -> dict[str, JsonValue]:
        async with create_async_client():
            return {}

    monkeypatch.setattr(qa_live_probes, "call_live_write_refusal_payload", construct_transport)
    out = tmp_path / "live-write.json"

    result = qa_live_probes.handle_live_write_refusal(out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["transport_constructed"] is True


def test_live_read_refusal_checks_read_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    out = tmp_path / "live-read.json"

    result = qa.main(["live-read-refusal", "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["live_reads"] is True
    assert report["network_call_made"] is False


@pytest.mark.parametrize(
    ("field", "mutation", "value"),
    [
        ("live_write_called", "replace", True),
        ("order_or_subscription_created", "replace", True),
        ("live_write_called", "remove", False),
        ("order_or_subscription_created", "remove", False),
        ("live_write_called", "replace", 0),
        ("order_or_subscription_created", "replace", 0),
        ("live_write_called", "replace", None),
        ("order_or_subscription_created", "replace", None),
        ("live_write_called", "replace", "false"),
        ("order_or_subscription_created", "replace", "false"),
    ],
)
def test_live_read_refusal_gate_fails_closed_for_unproven_safety_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutation: str,
    value: JsonValue,
) -> None:
    payload: dict[str, JsonValue] = {
        "status": "live_not_called",
        "reason": "missing_live_read_enablement",
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }
    if mutation == "remove":
        del payload[field]
    else:
        payload[field] = value

    async def unsafe_payload() -> dict[str, JsonValue]:
        return payload

    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)
    monkeypatch.setattr(qa_live_probes, "call_live_read_refusal_payload", unsafe_payload)
    out = tmp_path / "live-read.json"

    result = qa_live_probes.handle_live_read_refusal(out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report[field] is True


def test_live_read_refusal_passes_when_env_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SAXO_MCP_ENABLE_LIVE_READS", raising=False)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    out = tmp_path / "live-read.json"

    result = qa.main(["live-read-refusal", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "refused"
    assert report["command"] == "live-read-refusal"
    assert report["environment"] == "LIVE"
    assert report["live_reads"] is False
    assert report["live_writes"] is False
    assert report["scope_used"] is False
    assert report["fastmcp_tool_called"] is True
    assert report["transport_constructed"] is False
    assert report["network_call_made"] is False
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert report["reason"] == "missing_live_read_enablement"
