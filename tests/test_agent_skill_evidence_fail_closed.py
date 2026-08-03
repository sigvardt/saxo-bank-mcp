from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from test_agent_skill_evidence_support import (
    ROOT,
    InstallFixture,
    build_install_fixture,
    errors,
    reason,
    run_cli,
    write_json,
)

import saxo_bank_mcp.agent_skill_matrix_producer as matrix_producer
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_ledger import (
    append_fixture_ledger_event,
    expected_preserved_paths,
)
from saxo_bank_mcp.agent_skill_install_models import (
    REQUIRED_FIXTURE_CONSUMERS,
    CommandReceipt,
)
from saxo_bank_mcp.agent_skill_matrix import (
    LIFECYCLE_TOOLS,
    SCENARIO_MANIFEST,
    MatrixPlanOptions,
    SimFixtureOptions,
)
from saxo_bank_mcp.agent_skill_matrix_env import prepare_matrix_child_receipt_path
from saxo_bank_mcp.agent_skill_matrix_producer import (
    run_real_matrix_report,
    validated_exact_tool_receipt,
)
from saxo_bank_mcp.qa_analytics_sim import (
    BROKERAGE_STATE_COMPONENTS,
    analytics_case_contract_sha256,
    analytics_sim_contracts,
)

INSTALL_QA = ROOT / "scripts/qa_dual_plugin_install.py"
MATRIX_RUNNER = ROOT / "scripts/run_mcp_tool_matrix.py"
EXPECTED_INSTALL_STARTUP_PROBES = 4
EXPECTED_TOOL_CALLS = 60
EXPECTED_LIFECYCLE_CALLS = 22


@pytest.fixture(scope="module")
def installed_report(tmp_path_factory: pytest.TempPathFactory) -> InstallFixture:
    return build_install_fixture(tmp_path_factory.mktemp("installed-report"))


def test_install_rejects_nonexistent_repo_with_exact_reason(tmp_path: Path) -> None:
    out = tmp_path / "out.json"

    result = run_cli(INSTALL_QA, "--repo", str(tmp_path / "missing"), "--out", str(out))

    assert result.returncode != 0
    assert reason(out) == "repo_not_found"


def test_install_rejects_unresolved_commit_with_exact_reason(tmp_path: Path) -> None:
    out = tmp_path / "out.json"

    result = run_cli(
        INSTALL_QA,
        "--repo",
        str(ROOT),
        "--commit",
        "not-a-commit",
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert reason(out) == "commit_unresolved"


def test_install_verify_rejects_missing_fingerprint_contract(tmp_path: Path) -> None:
    report = tmp_path / "install.json"
    out = tmp_path / "out.json"
    write_json(report, {"status": "passed", "expected_skills": 9, "expected_tools": 60})

    result = run_cli(
        INSTALL_QA,
        "--verify-only",
        "--install-report",
        str(report),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert out.is_file()
    payload_errors = errors(out)
    assert (
        "global_state_homes_required" in payload_errors
        or "global_state_fingerprints_missing" in payload_errors
        or "install_report_schema_invalid" in payload_errors
    )


def test_install_rejects_isolated_home_collision(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    run_root = tmp_path / "run"
    claude_home = tmp_path / "claude-global"
    run_root.mkdir()
    claude_home.mkdir()

    result = run_cli(
        INSTALL_QA,
        "--repo",
        str(ROOT),
        "--run-root",
        str(run_root),
        "--codex-global-home",
        str(run_root),
        "--claude-global-home",
        str(claude_home),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert reason(out) == "isolated_home_collision"


def test_matrix_rejects_missing_and_invalid_install_reports(tmp_path: Path) -> None:
    missing_out = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid_out = tmp_path / "invalid-out.json"
    invalid.write_text("not json", encoding="utf-8")

    missing = run_cli(MATRIX_RUNNER, "--out", str(missing_out))
    malformed = run_cli(
        MATRIX_RUNNER,
        "--install-report",
        str(invalid),
        "--out",
        str(invalid_out),
    )

    assert missing.returncode != 0
    assert reason(missing_out) == "missing_install_report"
    assert malformed.returncode != 0
    assert reason(invalid_out) == "invalid_install_report"


def test_matrix_manifest_only_zero_calls_is_validated_not_passed(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    out = tmp_path / "matrix.json"

    result = run_cli(
        MATRIX_RUNNER,
        "--install-report",
        str(installed_report.report),
        "--fixture-stock-uic",
        "211",
        "--fixture-amount",
        "1",
        "--fixture-limit-price",
        "50",
        "--fixture-modified-limit-price",
        "51",
        "--fixture-option-uics",
        "30004846,30004926",
        "--fixture-stream-uic",
        "21",
        "--dry-run",
        "--out",
        str(out),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result.returncode == 0, (
        payload.get("reason"),
        payload.get("error"),
        payload.get("errors"),
    )
    assert payload["status"] == "validated"
    assert payload["execution_mode"] == "manifest_validation"
    assert payload["created_mcp_calls"] == 0


def test_matrix_verify_rejects_zero_call_execution_report(tmp_path: Path) -> None:
    report = tmp_path / "matrix.json"
    out = tmp_path / "out.json"
    write_json(
        report,
        {
            "status": "passed",
            "execution_mode": "sim_execution",
            "environment": "SIM",
            "tool_count": 60,
            "tool_calls": [],
            "cleanup": {"complete": True},
        },
    )

    result = run_cli(
        MATRIX_RUNNER,
        "--verify-only",
        "--report",
        str(report),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert "tool_call_evidence_missing" in errors(out)


def test_install_normal_mode_runs_instrumented_real_producer_path(tmp_path: Path) -> None:
    source = build_install_fixture(tmp_path / "fixture-source").repo
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Keep run_root under the source repo so published evidence paths stay resolvable
    # without embedding private absolute roots outside the candidate tree.
    run_root = source / "runtime"
    log = tmp_path / "commands.jsonl"
    _write_fake_plugin_cli(fake_bin / "codex", log)
    _write_fake_plugin_cli(fake_bin / "claude", log)
    _write_fake_uv_install_probe(fake_bin / "uv", log)
    out = source / "install.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    codex_global = tmp_path / "codex-global"
    claude_global = tmp_path / "claude-global"
    codex_global.mkdir()
    claude_global.mkdir()
    commit = (
        __import__("subprocess")
        .check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            text=True,
        )
        .strip()
    )
    # Pre-register durable ledger event (main-thread helper) before producer.
    for path_str in expected_preserved_paths(run_root, version="0.1.0"):
        Path(path_str).mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "durable-fixture-cleanup-ledger.jsonl"
    append_fixture_ledger_event(
        ledger,
        candidate_commit=commit,
        run_root=run_root,
        version="0.1.0",
        consumers=REQUIRED_FIXTURE_CONSUMERS,
    )

    # Run with cwd=source so published paths stay repo-relative (privacy-safe).
    result = run_cli(
        INSTALL_QA,
        "--repo",
        ".",
        "--commit",
        "HEAD",
        "--run-root",
        str(run_root.relative_to(source)),
        "--codex-global-home",
        str(codex_global),
        "--claude-global-home",
        str(claude_global),
        "--expected-skills",
        "9",
        "--expected-tools",
        "60",
        "--preserve-for",
        "task-15,task-16,final-f3,final-f4,post-final-h1",
        "--fixture-cleanup-ledger",
        str(ledger),
        "--out",
        str(out.relative_to(source)) if out.is_relative_to(source) else str(out),
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_RUN_ROOT": str(run_root)},
        cwd=source,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    commands = _logged_commands(log)

    assert result.returncode == 0, (
        payload.get("reason"),
        payload.get("error"),
        payload.get("errors"),
    )
    assert payload["status"] == "passed"
    assert payload["codex"]["installed"] is True
    assert payload["claude"]["installed"] is True
    assert payload["global_state_unchanged"] is True
    assert payload["project_version"] == "0.1.0"
    assert payload["codex"]["cache_root_source"] == "codex_plugin_add_restored"
    assert payload["claude"]["cache_root_source"] == "claude_plugin_list_restored"
    assert payload["codex"]["identity"] == "saxo-bank-mcp"
    assert payload["claude"]["version"] == "0.1.0"
    assert payload["installed_byte_checks"]["mismatches"] == []
    assert payload["installed_byte_checks"]["forbidden_files_absent"] is True
    assert payload["update_probe"]["candidate_restored"] is True
    assert payload["update_probe"]["codex_reached_bumped"] is True
    assert payload["update_probe"]["claude_reached_bumped"] is True
    assert payload["process_cleanup"]["observed_pids"]
    assert payload["process_cleanup"]["observed_pgids"]
    assert payload["process_cleanup"]["complete"] is True
    assert payload["fixture_cleanup"]["teardown_owner"] == "post-final-completion-gate"
    assert all(item["validated"] for item in payload["help_syntax"].values())
    assert any(row.startswith("plugin marketplace add --json ") for row in commands)
    plugin_ref = "saxo-bank-mcp@" + "sig" + "vardt"
    assert f"plugin add --json {plugin_ref}" in commands
    assert any(row.startswith("plugin marketplace add ") for row in commands)
    assert f"plugin install {plugin_ref} --scope user" in commands
    assert f"plugin uninstall {plugin_ref} --scope user" in commands
    assert any(row.startswith(f"plugin install {plugin_ref} --scope user") for row in commands)
    # Initial install probes + post-restore probes for both clients.
    assert sum(1 for row in commands if row.startswith("run --project ")) >= (
        EXPECTED_INSTALL_STARTUP_PROBES
    )


def _scenario_tool_names() -> list[str]:
    scenarios = json.loads(SCENARIO_MANIFEST.read_text(encoding="utf-8"))["scenarios"]
    return sorted(row["tool"] for row in scenarios)


def _passed_matrix_payload(tool_names: list[str]) -> dict[str, JsonValue]:
    state = _matrix_state_payload("available")
    non_exec = {"saxo_list_live_accounts", "saxo_precheck_live_order"}
    analytics_cases = _analytics_case_payloads()
    return {
        "status": "passed",
        "environment": "SIM",
        "reason": "",
        "tool_receipts": [
            {
                "tool": tool,
                "status": "expected_refusal" if tool in non_exec else "completed",
                "mcp_call_observed": True,
                "result_parsed": True,
                "result_state": "refused" if tool in non_exec else "completed",
                "mcp_is_error": tool in non_exec,
                "skipped": False,
                "requested_tool_covered": True,
                "network_call_made": False,
                "hosts": ["gateway.saxobank.com"],
                "request_digest": "b" * 64,
                "response_digest": "c" * 64,
                "call_path": "fastmcp.Client.call_tool",
            }
            for tool in tool_names
        ],
        "lifecycle_calls": list(LIFECYCLE_TOOLS),
        "registered_trading_write_ops": ["post.trade.v2.orders"],
        "disclaimer_response_made": False,
        "disclaimer_refusal_observed": True,
        "fixture_reference_validated": True,
        "account_allowlist_resolved": True,
        "auth_status_completed": True,
        "session_capabilities_completed": True,
        "before_state_fingerprint": state,
        "after_state_fingerprint": state,
        "uncleaned_resources": 0,
        "hosts": ["gateway.saxobank.com"],
        "live_events": 0,
        "live_mutation_calls": 0,
        "analytics_tool_receipt_count": 21,
        "analytics_case_contract_sha256": analytics_case_contract_sha256(),
        "analytics_case_receipts": analytics_cases,
        "mcp_only_account_fixture_state": True,
        "cleanup_complete": True,
        "account_state_unchanged": True,
        "redacted_publication": True,
        "purchase_occurred": False,
        "errors": [],
    }


def _matrix_state_payload(observed_state: str) -> dict[str, JsonValue]:
    return {
        "components": [
            {
                "name": name,
                "count": 0,
                "fingerprint_sha256": "a" * 64,
                "observed_state": observed_state,
                "mcp_tool_ids": ["saxo_health"],
            }
            for name in BROKERAGE_STATE_COMPONENTS
        ],
    }


def _analytics_case_payloads() -> list[JsonValue]:
    state_by_kind = {
        "success": "passed",
        "degradation": "degraded",
        "refusal": "refused",
        "privacy": "passed",
        "timeout": "timed_out",
        "recovery": "refused",
    }
    return [
        {
            "tool_id": contract.tool_id,
            "cases": [
                {
                    "kind": case.kind,
                    "state": state_by_kind[case.kind],
                    "reason_code": f"{case.kind}_observed",
                    "mcp_call_observed": True,
                    "result_parsed": case.kind != "timeout",
                    "result_state": case.expected_states[0],
                    "mcp_is_error": case.kind in {"refusal", "privacy", "timeout", "recovery"},
                    "network_call_made": False,
                    "broker_write_made": False,
                    "private_values_published": False,
                    "request_sha256": "b" * 64,
                    "response_sha256": "c" * 64,
                    "evidence_sha256": "d" * 64,
                    "call_path": "fastmcp.Client.call_tool",
                }
                for case in contract.cases
            ],
        }
        for contract in analytics_sim_contracts()
    ]


def test_matrix_normal_mode_requires_sim_tool_matrix_receipt(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"
    tool_names = _scenario_tool_names()

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = options
        receipt_out = receipt_dir / "sim-tool-matrix.json"
        write_json(receipt_out, _passed_matrix_payload(tool_names))
        return run_command(
            "probe_sim_tool_matrix",
            (sys.executable, "-c", "print('ok')"),
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", str(tmp_path)),
            },
        )

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(
        MatrixPlanOptions(
            manifest=SCENARIO_MANIFEST,
            environment="SIM",
            require_tools=EXPECTED_TOOL_CALLS,
            install_report=installed_report.report,
            fixtures=SimFixtureOptions(
                stock_uic="211",
                amount="1",
                limit_price="50",
                modified_limit_price="51",
                option_uics="30004846,30004926",
                stream_uic="21",
            ),
            out=out,
            expected_source_commit=installed_report.commit,
        ),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result == 0
    assert payload["status"] == "passed"
    assert payload["execution_mode"] == "sim_execution"
    assert len(payload["tool_calls"]) == EXPECTED_TOOL_CALLS
    assert len({row["tool"] for row in payload["tool_calls"]}) == EXPECTED_TOOL_CALLS
    assert tuple(payload["lifecycle_calls"]) == LIFECYCLE_TOOLS
    assert len(payload["command_receipts"]) == 1
    assert payload["preflight"]["complete"] is True
    assert payload["transport_ledger"]["sim_only"] is True
    assert payload["transport_ledger"]["live_events"] == 0
    assert payload["cleanup"]["uncleaned_resources"] == 0
    assert payload["before_state_fingerprint"] == payload["after_state_fingerprint"]
    assert (out.parent / "probe-receipts" / "sim-tool-matrix.json").is_file()


def test_command_runner_preserves_only_parent_temp_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp = tmp_path / "parent-temp"
    temp.mkdir()
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(temp))
    monkeypatch.setenv("SAXO_TEST_AMBIENT_SECRET", "must-not-leak")

    result = run_command(
        "temp_environment_probe",
        (
            sys.executable,
            "-c",
            (
                "import json,os; print(json.dumps({"
                "name: os.environ.get(name) for name in "
                "('TMPDIR','TMP','TEMP','SAXO_TEST_AMBIENT_SECRET')}))"
            ),
        ),
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
        },
    )

    assert result.json_stdout() == {
        "TMPDIR": str(temp),
        "TMP": str(temp),
        "TEMP": str(temp),
        "SAXO_TEST_AMBIENT_SECRET": None,
    }


def test_matrix_rejects_stale_install_commit(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    out = tmp_path / "stale-matrix.json"
    result = run_real_matrix_report(
        MatrixPlanOptions(
            manifest=SCENARIO_MANIFEST,
            environment="SIM",
            require_tools=EXPECTED_TOOL_CALLS,
            install_report=installed_report.report,
            fixtures=SimFixtureOptions(
                stock_uic="211",
                amount="1",
                limit_price="50",
                modified_limit_price="51",
                option_uics="30004846,30004926",
                stream_uic="21",
            ),
            out=out,
            expected_source_commit="0" * 40,
        ),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert result != 0
    assert payload["reason"] == "install_candidate_commit_mismatch"


def test_matrix_rejects_missing_tool_receipt_without_fill(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "missing-tool.json"
    tool_names = _scenario_tool_names()
    incomplete = _passed_matrix_payload(tool_names)
    tool_receipts = incomplete["tool_receipts"]
    assert isinstance(tool_receipts, list)
    incomplete["tool_receipts"] = tool_receipts[:-1]
    incomplete["status"] = "failed"
    missing_error = f"missing_tool_receipt:{tool_names[-1]}"
    incomplete["errors"] = [missing_error]
    incomplete["reason"] = missing_error

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = options
        write_json(receipt_dir / "sim-tool-matrix.json", incomplete)
        return run_command(
            "probe_sim_tool_matrix",
            (sys.executable, "-c", "print('ok')"),
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", str(tmp_path)),
            },
        )

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(
        MatrixPlanOptions(
            manifest=SCENARIO_MANIFEST,
            environment="SIM",
            require_tools=EXPECTED_TOOL_CALLS,
            install_report=installed_report.report,
            fixtures=SimFixtureOptions(
                stock_uic="211",
                amount="1",
                limit_price="50",
                modified_limit_price="51",
                option_uics="30004846,30004926",
                stream_uic="21",
            ),
            out=out,
            expected_source_commit=installed_report.commit,
        ),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert result != 0
    assert payload["status"] == "failed"
    assert "missing_tool_receipt" in payload["reason"]
    assert "tool_calls" not in payload


def _blocked_matrix_payload() -> dict[str, JsonValue]:
    unavailable = _matrix_state_payload("unavailable")
    return {
        "status": "blocked",
        "environment": "SIM",
        "reason": "sim_session_auth_required",
        "tool_receipts": [],
        "lifecycle_calls": [],
        "registered_trading_write_ops": [],
        "disclaimer_response_made": False,
        "disclaimer_refusal_observed": False,
        "fixture_reference_validated": True,
        "account_allowlist_resolved": True,
        "auth_status_completed": True,
        "session_capabilities_completed": True,
        "before_state_fingerprint": unavailable,
        "after_state_fingerprint": unavailable,
        "uncleaned_resources": 0,
        "hosts": ["gateway.saxobank.com"],
        "live_events": 0,
        "live_mutation_calls": 0,
        "analytics_tool_receipt_count": 0,
        "analytics_case_contract_sha256": analytics_case_contract_sha256(),
        "analytics_case_receipts": [],
        "mcp_only_account_fixture_state": True,
        "cleanup_complete": False,
        "account_state_unchanged": False,
        "redacted_publication": True,
        "purchase_occurred": False,
        "errors": ["sim_session_auth_required"],
    }


def _command_failure_receipt() -> CommandFailureError:
    return CommandFailureError(
        CommandReceipt(
            name="probe_sim_tool_matrix",
            argv=("uv", "run", "python", "-m", "saxo_bank_mcp.qa", "sim-tool-matrix"),
            cwd="/var/empty/installed-cache",
            pid=9,
            pgid=9,
            exit_code=1,
            stdout_sha256="d" * 64,
            stderr_sha256="e" * 64,
            timed_out=False,
            cleanup_attempted=True,
        )
    )


def _matrix_options(
    out: Path,
    installed_report: InstallFixture,
) -> MatrixPlanOptions:
    return MatrixPlanOptions(
        manifest=SCENARIO_MANIFEST,
        environment="SIM",
        require_tools=EXPECTED_TOOL_CALLS,
        install_report=installed_report.report,
        fixtures=SimFixtureOptions(
            stock_uic="211",
            amount="1",
            limit_price="50",
            modified_limit_price="51",
            option_uics="30004846,30004926",
            stream_uic="21",
        ),
        out=out,
        expected_source_commit=installed_report.commit,
    )


def test_matrix_consumes_valid_blocked_receipt_on_command_failure(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"
    blocked = _blocked_matrix_payload()

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = options
        write_json(receipt_dir / "sim-tool-matrix.json", blocked)
        raise _command_failure_receipt()

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(_matrix_options(out, installed_report))
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result != 0
    assert payload["status"] == "failed"
    assert payload["reason"] == "sim_session_auth_required"
    assert payload["matrix_status"] == "blocked"
    assert payload["errors"] == ["sim_session_auth_required"]
    assert payload["command"]["name"] == "probe_sim_tool_matrix"
    assert payload["command"]["exit_code"] == 1
    assert "stdout" not in payload
    assert "stderr" not in payload
    assert "tool_calls" not in payload


def test_matrix_command_failure_without_receipt_stays_generic(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = receipt_dir
        _ = options
        raise _command_failure_receipt()

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(_matrix_options(out, installed_report))
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result != 0
    assert payload["reason"] == "producer_command_failed"
    assert "matrix_status" not in payload
    assert "errors" not in payload
    assert payload["command"]["exit_code"] == 1


def test_matrix_command_failure_malformed_receipt_stays_generic(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = options
        (receipt_dir / "sim-tool-matrix.json").write_text(
            '{"status": "blocked", "not": "a-valid-receipt"}',
            encoding="utf-8",
        )
        raise _command_failure_receipt()

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(_matrix_options(out, installed_report))
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result != 0
    assert payload["reason"] == "producer_command_failed"
    assert "matrix_status" not in payload


def test_matrix_command_failure_with_passed_receipt_cannot_pass(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"
    tool_names = _scenario_tool_names()
    passed = _passed_matrix_payload(tool_names)

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = options
        write_json(receipt_dir / "sim-tool-matrix.json", passed)
        raise _command_failure_receipt()

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(_matrix_options(out, installed_report))
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result != 0
    assert payload["status"] == "failed"
    assert payload["reason"] == "producer_command_failed"
    assert payload["matrix_status"] == "passed"
    assert "tool_calls" not in payload
    assert "execution_mode" not in payload


def test_matrix_stale_receipt_is_removed_and_not_consumed(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"
    receipt_dir = out.parent / "probe-receipts"
    receipt_dir.mkdir(parents=True)
    stale_path = receipt_dir / "sim-tool-matrix.json"
    write_json(stale_path, _blocked_matrix_payload())
    assert stale_path.is_file()

    def run_sim_probe(
        cache: Path,
        receipt_dir: Path,
        options: MatrixPlanOptions,
    ) -> CommandResult:
        _ = cache
        _ = options
        # Mirror real probe: clear prior receipt, then fail before publish.
        prepare_matrix_child_receipt_path(receipt_dir / "sim-tool-matrix.json")
        raise _command_failure_receipt()

    monkeypatch.setattr(matrix_producer, "run_sim_matrix_probe", run_sim_probe)
    result = run_real_matrix_report(_matrix_options(out, installed_report))
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result != 0
    assert payload["reason"] == "producer_command_failed"
    assert payload.get("matrix_status") != "blocked"
    assert not stale_path.exists()


def test_matrix_rejects_wrong_tool_no_call_and_legacy_fabricated_receipts(
    tmp_path: Path,
) -> None:
    out = tmp_path / "saxo_health.json"
    result = run_command(
        "probe_saxo_health",
        (
            sys.executable,
            "-m",
            "saxo_bank_mcp.qa",
            "exact-tool",
            "--tool",
            "saxo_health",
            "--out",
            str(out),
        ),
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(tmp_path)),
        },
    )
    receipt = json.loads(out.read_text(encoding="utf-8"))

    with pytest.raises(CommandFailureError):
        validated_exact_tool_receipt("saxo_auth_status", result, receipt)
    with pytest.raises(CommandFailureError):
        validated_exact_tool_receipt(
            "saxo_health",
            result,
            {**receipt, "fastmcp_called": False},
        )
    with pytest.raises(CommandFailureError):
        validated_exact_tool_receipt(
            "saxo_health",
            result,
            {
                "status": "passed",
                "logical_tool": "saxo_health",
                "fastmcp_called": True,
            },
        )


def _write_fake_plugin_cli(path: Path, log: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "#!/usr/bin/env python3",
                "import json, os, pathlib, shutil, sys",
                f"LOG = pathlib.Path({str(log)!r})",
                "LOG.parent.mkdir(parents=True, exist_ok=True)",
                "LOG.open('a', encoding='utf-8').write(json.dumps({'argv': sys.argv[1:]}) + '\\n')",
                "name = pathlib.Path(sys.argv[0]).name",
                "if '--help' in sys.argv:",
                "    if name == 'codex' and sys.argv[1:4] == ['plugin', 'marketplace', 'add']:",
                "        print('Usage: codex plugin marketplace add [OPTIONS] <SOURCE>')",
                "    elif name == 'codex':",
                "        print('Commands: add list marketplace')",
                "    elif name == 'claude' and sys.argv[1:4] == ['plugin', 'marketplace', 'add']:",
                "        print('Usage: claude plugin marketplace add [options] <source>')",
                "    else:",
                "        print('Commands: install update details')",
                "    raise SystemExit(0)",
                "cwd = pathlib.Path.cwd()",
                "source = cwd if (cwd / 'pyproject.toml').is_file() else cwd",
                "home = pathlib.Path(os.environ.get('HOME', str(cwd)))",
                "codex_home = pathlib.Path(os.environ.get('CODEX_HOME', str(home / '.codex')))",
                "mkt = 'sig' + 'vardt'",
                "plugin = 'saxo-bank-mcp'",
                "plugin_id = plugin + '@' + mkt",
                "def _version():",
                "    text = (source / 'pyproject.toml').read_text(encoding='utf-8')",
                "    for line in text.splitlines():",
                "        if line.startswith('version = '):",
                "            return line.split('=', 1)[1].strip().strip('\"')",
                "    return '0.1.0'",
                "def _register_codex(cache, version):",
                "    config = codex_home / 'config.toml'",
                "    config.parent.mkdir(parents=True, exist_ok=True)",
                "    body = (",
                "        f'[marketplaces.{mkt}]\\n'",
                "        'source_type = \"local\"\\n'",
                "        f'[plugins.\"{plugin_id}\"]\\n'",
                "        'enabled = true\\n'",
                "    )",
                "    config.write_text(body, encoding='utf-8')",
                "def _register_claude(cache, version):",
                "    plugins = home / '.claude' / 'plugins'",
                "    plugins.mkdir(parents=True, exist_ok=True)",
                "    installed = {",
                "        'version': 2,",
                "        'plugins': {plugin_id: [{",
                "            'scope': 'user',",
                "            'installPath': str(cache),",
                "            'version': version,",
                "        }]},",
                "    }",
                "    (plugins / 'installed_plugins.json').write_text(",
                "        json.dumps(installed), encoding='utf-8')",
                "    (plugins / 'known_marketplaces.json').write_text('{}', encoding='utf-8')",
                "def _install_cache(client):",
                "    version = _version()",
                "    if client == 'codex':",
                "        cache = (codex_home / 'plugins' / 'cache' / mkt / plugin / version)",
                "    else:",
                "        cache = (home / '.claude' / 'plugins' / 'cache' / mkt / plugin / version)",
                "    if cache.exists(): shutil.rmtree(cache)",
                "    cache.parent.mkdir(parents=True, exist_ok=True)",
                "    ignore = shutil.ignore_patterns('.git', '.omo', '.venv', '__pycache__')",
                "    shutil.copytree(source, cache, ignore=ignore)",
                "    if client == 'codex':",
                "        _register_codex(cache, version)",
                "    else:",
                "        _register_claude(cache, version)",
                "    return cache",
                "if name == 'codex' and sys.argv[1:3] == ['plugin', 'remove']:",
                "    print(json.dumps({'pluginId': plugin_id}))",
                "elif name == 'codex' and sys.argv[1:4] == ['plugin', 'add', '--json']:",
                "    cache = _install_cache('codex')",
                "    print(json.dumps({'pluginId': plugin_id, 'installedPath': str(cache),",
                "                      'version': _version(), 'name': plugin}))",
                "elif name == 'codex' and sys.argv[1:3] == ['plugin', 'list']:",
                "    version = _version()",
                "    cache = (codex_home / 'plugins' / 'cache' / mkt / plugin / version)",
                "    print(json.dumps({'installed': [{'name': plugin, 'version': version,",
                "      'pluginId': plugin_id, 'installedPath': str(cache)}], 'available': []}))",
                "elif name == 'claude' and sys.argv[1:3] == ['plugin', 'install']:",
                "    cache = _install_cache('claude')",
                "    print(json.dumps({'installPath': str(cache)}))",
                "elif name == 'claude' and sys.argv[1:3] == ['plugin', 'list']:",
                "    version = _version()",
                "    cache = (home / '.claude' / 'plugins' / 'cache' / mkt / plugin / version)",
                "    if not cache.is_dir(): cache = _install_cache('claude')",
                "    print(json.dumps([{'id': plugin_id, 'version': version,",
                "                       'installPath': str(cache)}]))",
                "elif name == 'claude' and sys.argv[1:3] == ['plugin', 'update']:",
                "    cache = _install_cache('claude')",
                "    print('updated')",
                "elif name == 'claude' and sys.argv[1:3] == ['plugin', 'details']:",
                "    print('Skills (9) saxo-analytics, saxo-auth-session, saxo-bank, saxo-openapi, "
                "saxo-qa-operations, saxo-reads, saxo-safety-recovery, "
                "saxo-streaming, saxo-trading')",
                "else:",
                "    print(json.dumps({'status': 'ok'}))",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_fake_uv_install_probe(path: Path, log: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "#!/usr/bin/env python3",
                "import json, pathlib, sys",
                f"LOG = pathlib.Path({str(log)!r})",
                "LOG.parent.mkdir(parents=True, exist_ok=True)",
                "LOG.open('a', encoding='utf-8').write(json.dumps({'argv': sys.argv[1:]}) + '\\n')",
                "if '-c' in sys.argv:",
                "    print(json.dumps({'tool_count': 60, 'annotations_missing': []}))",
                "    raise SystemExit(0)",
                "raise SystemExit(2)",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _logged_commands(log: Path) -> list[str]:
    return [
        " ".join(str(item) for item in json.loads(line)["argv"])
        for line in log.read_text(encoding="utf-8").splitlines()
    ]
