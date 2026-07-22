from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from pathlib import Path

import pytest

from saxo_bank_mcp import qa, qa_manual_live
from saxo_bank_mcp._evidence import JsonValue

EXPECTED_SCENARIOS = 4
EXPECTED_SHA256_LENGTH = 64
EXPECTED_SOURCE_HASHES = 6


def _fixed_token(_nbytes: int | None = None) -> str:
    return "manual-boundary-marker"


def _reject_publication(_path: Path, _payload: Mapping[str, JsonValue]) -> bool:
    return False


def test_manual_live_boundary_publishes_reconstructable_safe_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_suffix = _fixed_token()
    monkeypatch.setattr(secrets, "token_urlsafe", _fixed_token)
    out = tmp_path / "manual-qa.json"

    result = qa.main(["manual-live-boundary", "--out", str(out)])

    assert result == 0
    raw = out.read_text(encoding="utf-8")
    assert marker_suffix not in raw
    report = json.loads(raw)
    assert report["schema_version"] == "saxo-manual-live-boundary-v1"
    assert report["status"] == "passed"
    assert report["scenario_count"] == EXPECTED_SCENARIOS
    assert {scenario["scenario_id"] for scenario in report["scenarios"]} == {
        "generic_fastmcp_validation",
        "live_precheck_validation",
        "live_write_refusal",
        "disabled_live_read_refusal",
    }
    assert {scenario["status"] for scenario in report["scenarios"]} == {"passed"}
    assert all(scenario["transport_constructed"] is False for scenario in report["scenarios"])
    assert report["network_call_made"] is False
    assert report["live_write_called"] is False
    assert report["order_or_subscription_created"] is False
    assert report["warning_capture_verified"] is True
    assert any(
        item["logger"] == "fastmcp.server.server"
        and item["message"] == "manual live boundary warning capture canary"
        for item in report["warning_log_transcript"]
    )
    assert report["rejected_input"] == {
        "generated_for_this_run": True,
        "persisted": False,
        "sha256": hashlib.sha256(f"rejected-{marker_suffix}".encode()).hexdigest(),
    }
    assert report["generator"] == "saxo_bank_mcp.qa_manual_live"
    assert len(report["generator_source_sha256"]) == EXPECTED_SHA256_LENGTH
    assert len(report["source_hashes"]) >= EXPECTED_SOURCE_HASHES
    expected_command_prefix = [
        "uv",
        "run",
        "python",
        "-m",
        "saxo_bank_mcp.qa",
        "manual-live-boundary",
    ]
    assert report["replay_command"][: len(expected_command_prefix)] == expected_command_prefix


def test_manual_live_boundary_fails_when_publication_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa_manual_live, "write_scanned_json", _reject_publication)

    result = qa_manual_live.handle_manual_live_boundary(tmp_path / "manual-qa.json")

    assert result == 1
