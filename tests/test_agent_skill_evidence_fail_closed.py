from __future__ import annotations

import json
import os
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

INSTALL_QA = ROOT / "scripts/qa_dual_plugin_install.py"
MATRIX_RUNNER = ROOT / "scripts/run_mcp_tool_matrix.py"
EXPECTED_INSTALL_STARTUP_PROBES = 4
EXPECTED_TOOL_CALLS = 39
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
    write_json(report, {"status": "passed", "expected_skills": 8, "expected_tools": 39})

    result = run_cli(
        INSTALL_QA,
        "--verify-only",
        "--install-report",
        str(report),
        "--out",
        str(out),
    )

    assert result.returncode != 0
    assert "global_state_fingerprints_missing" in errors(out)


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

    assert result.returncode == 0, result.stderr
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
            "tool_count": 39,
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
    run_root = tmp_path / "runtime"
    log = tmp_path / "commands.jsonl"
    _write_fake_plugin_cli(fake_bin / "codex", log)
    _write_fake_plugin_cli(fake_bin / "claude", log)
    _write_fake_uv(fake_bin / "uv", log)
    out = tmp_path / "install.json"
    codex_global = tmp_path / "codex-global"
    claude_global = tmp_path / "claude-global"
    codex_global.mkdir()
    claude_global.mkdir()

    result = run_cli(
        INSTALL_QA,
        "--repo",
        str(source),
        "--commit",
        "HEAD",
        "--run-root",
        str(run_root),
        "--codex-global-home",
        str(codex_global),
        "--claude-global-home",
        str(claude_global),
        "--expected-skills",
        "8",
        "--expected-tools",
        "39",
        "--preserve-for",
        "task-15,task-16,final-f3,final-f4,post-final-h1",
        "--out",
        str(out),
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_RUN_ROOT": str(run_root)},
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    commands = _logged_commands(log)

    assert result.returncode == 0, result.stderr
    assert payload["status"] == "passed"
    assert payload["codex"]["installed"] is True
    assert payload["claude"]["installed"] is True
    assert payload["global_state_unchanged"] is True
    assert payload["project_version"] == "0.1.0"
    assert payload["codex"]["cache_root_source"] == "codex_plugin_add"
    assert payload["claude"]["cache_root_source"] == "claude_plugin_install"
    assert payload["codex"]["identity"] == "saxo-bank-mcp"
    assert payload["claude"]["version"] == "0.1.0"
    assert payload["installed_byte_checks"]["mismatches"] == []
    assert payload["installed_byte_checks"]["forbidden_files_absent"] is True
    assert payload["update_probe"]["candidate_restored"] is True
    assert payload["process_cleanup"]["observed_pids"]
    assert payload["process_cleanup"]["observed_pgids"]
    assert all(item["validated"] for item in payload["help_syntax"].values())
    assert any(row.startswith("plugin marketplace add --json ") for row in commands)
    plugin_ref = "saxo-bank-mcp@" + "sig" + "vardt"
    assert f"plugin add --json {plugin_ref}" in commands
    assert any(row.startswith("plugin marketplace add ") for row in commands)
    assert f"plugin install {plugin_ref} --scope user" in commands
    assert (
        sum(1 for row in commands if row.startswith("run --project "))
        == EXPECTED_INSTALL_STARTUP_PROBES
    )


def test_matrix_normal_mode_runs_instrumented_probe_commands(
    tmp_path: Path,
    installed_report: InstallFixture,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.jsonl"
    _write_fake_uv(fake_bin / "uv", log)
    out = tmp_path / "tool-matrix.json"

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
        "--out",
        str(out),
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    commands = _logged_commands(log)

    assert result.returncode == 0, result.stderr
    assert payload["status"] == "passed"
    assert payload["execution_mode"] == "sim_execution"
    assert len(payload["tool_calls"]) == EXPECTED_TOOL_CALLS
    assert len({row["tool"] for row in payload["tool_calls"]}) == EXPECTED_TOOL_CALLS
    assert len(payload["lifecycle_calls"]) == EXPECTED_LIFECYCLE_CALLS
    assert len(payload["command_receipts"]) == EXPECTED_TOOL_CALLS
    assert payload["preflight"]["complete"] is True
    assert payload["transport_ledger"]["sim_only"] is True
    assert payload["transport_ledger"]["live_events"] == 0
    assert payload["cleanup"]["uncleaned_resources"] == 0
    assert {
        call["tool"] for call in payload["tool_calls"] if call["requested_tool_covered"]
    } == {call["tool"] for call in payload["tool_calls"]}
    assert any("python -m saxo_bank_mcp.qa trading-write-matrix" in row for row in commands)
    assert any("python -m saxo_bank_mcp.qa stream-cleanup" in row for row in commands)


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
                "run_root = pathlib.Path(os.environ.get('FAKE_RUN_ROOT', pathlib.Path.cwd()))",
                "source = run_root / 'source-clone'",
                "if name == 'codex' and sys.argv[1:4] == ['plugin', 'add', '--json']:",
                "    cache = run_root / 'codex-cache'",
                "    if cache.exists(): shutil.rmtree(cache)",
                "    ignore = shutil.ignore_patterns('.git', '.omo', '.venv')",
                "    shutil.copytree(source, cache, ignore=ignore)",
                "    marker = {'tool_count': 39, 'annotations_missing': []}",
                "    (cache / '.qa-probe.json').write_text(json.dumps(marker), encoding='utf-8')",
                "    print(json.dumps({'cache_root': str(cache)}))",
                "elif name == 'claude' and sys.argv[1:3] == ['plugin', 'install']:",
                "    cache = run_root / 'claude-cache'",
                "    if cache.exists(): shutil.rmtree(cache)",
                "    ignore = shutil.ignore_patterns('.git', '.omo', '.venv')",
                "    shutil.copytree(source, cache, ignore=ignore)",
                "    marker = {'tool_count': 39, 'annotations_missing': []}",
                "    (cache / '.qa-probe.json').write_text(json.dumps(marker), encoding='utf-8')",
                "    print(json.dumps({'cache_root': str(cache)}))",
                "elif name == 'codex' and sys.argv[1:4] == ['plugin', 'marketplace', 'upgrade']:",
                "    print(json.dumps({'status': 'ok',",
                "                      'cache_root': str(run_root / 'codex-cache')}))",
                "elif name == 'claude' and sys.argv[1:3] == ['plugin', 'update']:",
                "    print(json.dumps({'status': 'ok',",
                "                      'cache_root': str(run_root / 'claude-cache')}))",
                "else:",
                "    print(json.dumps({'status': 'ok'}))",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_fake_uv(path: Path, log: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "#!/usr/bin/env python3",
                "import json, pathlib, sys",
                f"LOG = pathlib.Path({str(log)!r})",
                "LOG.parent.mkdir(parents=True, exist_ok=True)",
                "LOG.open('a', encoding='utf-8').write(json.dumps({'argv': sys.argv[1:]}) + '\\n')",
                "if '-c' in sys.argv:",
                "    print(json.dumps({'tool_count': 39, 'annotations_missing': []}))",
                "    raise SystemExit(0)",
                "out = None",
                "if '--out' in sys.argv:",
                "    out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])",
                "if out is not None:",
                "    out.parent.mkdir(parents=True, exist_ok=True)",
                "    tool = out.stem if out is not None else 'saxo_health'",
                "    payload = {'status': 'passed', 'logical_tool': tool, 'fastmcp_called': True}",
                "    payload['environment'] = 'SIM'",
                "    payload['transport'] = {'host': 'sim.api.saxo.test'}",
                "    payload['network_call_made'] = True",
                "    payload['completion_claim_allowed'] = True",
                "    payload['secret_scan'] = {'findings': [], 'scan_errors': []}",
                "    out.write_text(json.dumps(payload), encoding='utf-8')",
                "print(json.dumps({'status': 'passed'}))",
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
