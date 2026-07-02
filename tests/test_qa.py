from __future__ import annotations

import json
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from saxo_bank_mcp import qa, qa_readme_probe, qa_safety_probes

EXPECTED_CLIENT_APP_SECRET_FINDINGS = 2
EXPECTED_STREAMING_CONNECTIONS = 4
EXPECTED_PRICE_INSTRUMENTS = 200
REQUIRED_GITIGNORE_PATTERNS = (
    ".omo/",
    ".codegraph/",
    ".env",
    "*credential*",
    "*secret*",
    "*token*",
    "*.log",
)


def init_git_repo(path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for gitignore probe tests")
    subprocess.run(
        [git, "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_gitignore_secret_probe_checks_dummy_files_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text("\n".join(REQUIRED_GITIGNORE_PATTERNS) + "\n", encoding="utf-8")
    out = tmp_path / "gitignore.json"

    result = qa.main(["gitignore-secret", "--out", str(out)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["missing_patterns"] == []
    assert report["remaining_exists"] == []
    assert set(report["cleanup_removed"]) == set(report["dummy_paths"])
    assert report["git_check"]["status"] == "passed"
    assert set(report["git_check"]["ignored_paths"]) == set(report["dummy_paths"])
    assert not any((tmp_path / path).exists() for path in report["dummy_paths"])


def test_gitignore_secret_probe_fails_when_dummy_is_not_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text(
        "\n".join(pattern for pattern in REQUIRED_GITIGNORE_PATTERNS if pattern != "*.log") + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "gitignore.json"

    result = qa.main(["gitignore-secret", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert "*.log" in report["missing_patterns"]
    assert "task-1-qa.log" in report["missing_patterns"]
    assert report["remaining_exists"] == []


def test_health_probe_calls_fastmcp_saxo_health(tmp_path: Path) -> None:
    out = tmp_path / "health.json"

    result = qa.main(["health", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["tool_name"] == "saxo_health"
    assert report["driver"] == "loop_harness"
    assert report["mode"] == "SIM"
    assert report["live_writes"] is False
    assert "not implemented" not in report["detail"]


def test_readme_smoke_passes_with_required_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    required_text = "\n".join(qa_readme_probe.README_REQUIRED_MARKERS)
    (tmp_path / "README.md").write_text(required_text, encoding="utf-8")
    (docs / "operator-guide.md").write_text(required_text, encoding="utf-8")
    (docs / "incident-cleanup.md").write_text(required_text, encoding="utf-8")
    out = tmp_path / "readme-smoke.json"

    result = qa.main(["readme-smoke", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["required_command_markers_present"] is True
    assert report["missing_command_markers"] == []
    assert report["health_status"] == "passed"
    assert report["auth_status_status"] == "passed"
    assert report["copied_secret_values_detected"] is False
    assert report["prompted_user"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}
    assert set(report["final_verify_help_exit_codes"].values()) == {0}


def test_readme_smoke_fails_when_command_marker_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    missing_marker = qa_readme_probe.README_REQUIRED_MARKERS[0]
    required_text = "\n".join(
        marker for marker in qa_readme_probe.README_REQUIRED_MARKERS if marker != missing_marker
    )
    (tmp_path / "README.md").write_text(required_text, encoding="utf-8")
    (docs / "operator-guide.md").write_text(required_text, encoding="utf-8")
    (docs / "incident-cleanup.md").write_text(required_text, encoding="utf-8")
    out = tmp_path / "readme-smoke.json"

    result = qa.main(["readme-smoke", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["required_command_markers_present"] is False
    assert report["missing_command_markers"] == [missing_marker]


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


def test_secret_scan_ignores_variable_names(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("refresh_token = None\n", encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["paths"] == [str(target)]
    assert report["findings"] == []


def test_secret_scan_detects_access_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    key_parts = ["access", "token"]
    key = "_".join(key_parts)
    token = string.ascii_lowercase
    target.write_text(f'{key} = "{token}"\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_unquoted_access_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    key_parts = ["access", "token"]
    key = "_".join(key_parts)
    target.write_text(f"{key}={string.ascii_lowercase}\n", encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_json_access_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    key_parts = ["access", "token"]
    key = "_".join(key_parts)
    target.write_text(f'{{"{key}": "{string.ascii_lowercase}"}}\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_pascalcase_saxo_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text(f'{{"AccessToken": "{string.ascii_lowercase}"}}\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_pascalcase_client_and_app_secret(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text(
        f"ClientSecret={string.ascii_lowercase}\nAppSecret={string.ascii_lowercase}\n",
        encoding="utf-8",
    )
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert len(report["findings"]) == EXPECTED_CLIENT_APP_SECRET_FINDINGS


def test_secret_scan_ignores_account_key_placeholder(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    target.write_text('{"AccountKey": "{AccountKey}"}\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 0
    assert json.loads(out.read_text(encoding="utf-8"))["findings"] == []


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
    assert "two independent approval factors" in report["missing_requirements"]
    assert report["network_call_made"] is False


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
    assert report["network_call_made"] is False
    assert report["reason"] == "missing_live_read_enablement"


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

    def fake_scan(paths: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        assert paths == [str(out)]
        return ([{"path": str(out), "pattern": "fake-secret"}], [])

    monkeypatch.setattr(qa_safety_probes, "scan_secret_paths", fake_scan)

    result = qa.main(["approval-happy", "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "passed"
    assert report["secret_scan"]["findings"] == [
        {"path": str(out), "pattern": "fake-secret"},
    ]


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


def test_secret_scan_missing_path_fails_closed(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist_at_all"
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(missing_path), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["scan_errors"] == [{"path": str(missing_path), "error": "missing_path"}]
