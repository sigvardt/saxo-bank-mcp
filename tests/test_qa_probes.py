from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import evidence_publication, qa

EXPECTED_STREAMING_CONNECTIONS = 4
EXPECTED_PRICE_INSTRUMENTS = 200


def test_auth_status_probe_calls_fastmcp_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_CLIENT_ID", "client-id-value")
    out = tmp_path / "auth-status.json"

    result = qa.main(["auth-status", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["tool_name"] == "saxo_auth_status"
    assert report["auth"]["requested_environment"] == "SIM"
    assert report["auth"]["sim_credentials_present"] is True
    assert report["auth"]["sim_credential_source"] == "env"
    assert "token_cache_path" not in report["auth"]
    assert report["auth"]["scope_used"] is False
    assert "client-id-value" not in json.dumps(report)


def test_token_cache_probe_reports_default_and_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = tmp_path / "token-cache.json"

    result = qa.main(["token-cache", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["default_path_ok"] is True
    assert report["repo_path_refused"] is True
    assert report["sync_path_refused"] is True


def test_sim_auth_probe_reports_external_auth_block_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "client-id-value")
    out = tmp_path / "sim-auth.json"

    result = qa.main(["sim-auth", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "blocked_external_auth_material"
    assert report["machine_completion_blocker"] == "sim_redirect_uri_missing"
    assert report["machine_completion_possible"] is False
    assert report["prompted_user"] is False
    assert report["network_call_made"] is False
    assert "registered_redirect_uri" in report["missing_auth_material"]
    assert "valid_sim_token_cache_or_portal_token" in report["missing_auth_material"]
    assert report["pkce_start_attempt"]["status"] == "auth_required"
    assert "oauth-authorization-code-grant-pkce" in json.dumps(
        report["official_saxo_auth_references"],
    )
    assert "client-id-value" not in json.dumps(report)


def test_stream_probe_writes_incomplete_auth_required_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "client-id-value")
    out = tmp_path / "stream.json"

    result = qa.main(
        [
            "stream",
            "--out",
            str(out),
            "--require-frame",
            "--expect-connections",
            "4",
            "--expect-price-instruments",
            "200",
        ],
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "incomplete_auth_required"
    assert report["tool_name"] == "saxo_create_streaming_price_subscription"
    assert report["fastmcp_called"] is True
    assert report["streaming_completion_claim_allowed"] is False
    assert report["subscription_snapshot_recorded"] is False
    assert report["websocket_frame_recorded"] is False
    assert report["network_call_made"] is False
    assert report["requested_expect_connections"] == EXPECTED_STREAMING_CONNECTIONS
    assert report["requested_expect_price_instruments"] == EXPECTED_PRICE_INSTRUMENTS
    assert report["limits_match_official"] is True
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    assert "client-id-value" not in json.dumps(report)


def test_stream_cleanup_probe_removes_simulated_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "client-id-value")
    out = tmp_path / "stream-cleanup.json"

    result = qa.main(["stream-cleanup", "--out", str(out), "--simulate-leak"])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "incomplete_auth_required"
    assert report["tool_name"] == "saxo_cleanup_streaming_subscriptions"
    assert report["simulated_leak"] is True
    assert report["local_removed_reference_ids"] == ["Price_QA"]
    assert report["local_open_records_after"] == []
    assert report["network_call_made"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_approval_happy_probe_uses_fastmcp_and_redacts_sensitive_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    out = tmp_path / "approval-happy.json"

    result = qa.main(["approval-happy", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["preview_status"] == "preview_created"
    assert report["commit_status"] == "approved_for_simulation"
    assert report["approval_factor_mode"] == "test_only_sim"
    assert report["preview_token_redacted"] is True
    assert report["audit_path_inside_repo"] is False
    assert report["audit_mode"] == "0o600"
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    dumped = json.dumps(report)
    assert "SIM_TEST_APPROVED" not in dumped
    assert "SIM-ACCOUNT-1" not in dumped


def test_approval_happy_probe_fails_when_redacted_evidence_scan_finds_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    out = tmp_path / "approval-happy.json"

    def fake_scan(
        label: str,
        _text: str,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        assert label == out.name
        return ([{"path": str(out), "pattern": "fake-secret"}], [])

    monkeypatch.setattr(evidence_publication, "scan_secret_text", fake_scan)

    result = qa.main(["approval-happy", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }


def test_approval_denied_probe_names_missing_approval_factor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    out = tmp_path / "approval-denied.json"

    result = qa.main(
        ["approval-denied", "--missing", "approval-factor", "--out", str(out)],
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "denied"
    assert report["commit_status"] == "denied"
    assert report["denial_reason"] == "approval_factor_missing"
    assert report["same_request_fingerprint"] is True
    assert report["audit_path_inside_repo"] is False


def test_manifest_command_writes_run_metadata(tmp_path: Path) -> None:
    out = tmp_path / "manifest.json"

    result = qa.main(
        [
            "manifest",
            "--out",
            str(out),
            "--run-id",
            "run-1",
            "--scenario-id",
            "scenario-1",
            "--expected-status",
            "passed",
            "--command",
            "python -m saxo_bank_mcp.qa health",
            "--evidence-path",
            "out.json",
        ],
    )

    assert result == 0
    assert json.loads(out.read_text(encoding="utf-8"))["scenario_id"] == "scenario-1"


@pytest.mark.parametrize("command", ["health", "auth-status", "manifest"])
def test_core_qa_writers_refuse_before_publishing_rejected_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = tmp_path / f"{command}.json"
    marker = "rejected-core-writer-marker"

    def fake_scan(
        _label: str,
        _text: str,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return ([{"path": marker, "pattern": "synthetic"}], [])

    monkeypatch.setattr(evidence_publication, "scan_secret_text", fake_scan)
    arguments = [command, "--out", str(out)]
    if command == "manifest":
        arguments.extend(
            [
                "--run-id", "run-1", "--scenario-id", "scenario-1",
                "--expected-status", "passed", "--command", "health",
                "--evidence-path", "out.json",
            ],
        )

    result = qa.main(arguments)

    raw = out.read_text(encoding="utf-8")
    assert result == 1
    assert marker not in raw
    assert json.loads(raw) == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }
