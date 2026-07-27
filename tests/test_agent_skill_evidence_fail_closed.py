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
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_privacy import write_minimal_privacy_pair
from saxo_bank_mcp.agent_skill_matrix import (
    LIFECYCLE_TOOLS,
    SCENARIO_MANIFEST,
    MatrixPlanOptions,
    SimFixtureOptions,
)
from saxo_bank_mcp.agent_skill_matrix_producer import (
    run_real_matrix_report,
    validated_exact_tool_receipt,
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
    # Keep run_root under the source repo so published evidence paths stay resolvable
    # without embedding private absolute roots outside the candidate tree.
    run_root = source / "runtime"
    log = tmp_path / "commands.jsonl"
    _write_fake_plugin_cli(fake_bin / "codex", log)
    _write_fake_plugin_cli(fake_bin / "claude", log)
    _write_fake_uv_install_probe(fake_bin / "uv", log)
    out = tmp_path / "install.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    codex_global = tmp_path / "codex-global"
    claude_global = tmp_path / "claude-global"
    codex_global.mkdir()
    claude_global.mkdir()
    commit = __import__("subprocess").check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
    ).strip()
    write_minimal_privacy_pair(
        out.parent,
        candidate_commit=commit,
        clone_commit=commit,
        run_root=run_root,
    )

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
    assert f"plugin update {plugin_ref} --scope user" in commands
    # Initial install probes + post-restore probes for both clients.
    assert sum(1 for row in commands if row.startswith("run --project ")) >= (
        EXPECTED_INSTALL_STARTUP_PROBES
    )


def test_matrix_normal_mode_requires_actual_exact_tool_probe_receipts(
    tmp_path: Path,
    installed_report: InstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "tool-matrix.json"

    def run_actual_probe(
        cache: Path,
        receipt_dir: Path,
        tool: str,
    ) -> CommandResult:
        _ = cache
        receipt_out = receipt_dir / f"{tool}.json"
        return run_command(
            f"probe_{tool}",
            (
                sys.executable,
                "-m",
                "saxo_bank_mcp.qa",
                "exact-tool",
                "--tool",
                tool,
                "--out",
                str(receipt_out),
            ),
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", str(tmp_path)),
            },
        )

    monkeypatch.setattr(matrix_producer, "_run_tool_probe", run_actual_probe)
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
        ),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert result == 0
    assert payload["status"] == "passed"
    assert payload["execution_mode"] == "sim_execution"
    assert len(payload["tool_calls"]) == EXPECTED_TOOL_CALLS
    assert len({row["tool"] for row in payload["tool_calls"]}) == EXPECTED_TOOL_CALLS
    assert tuple(payload["lifecycle_calls"]) == LIFECYCLE_TOOLS
    assert len(payload["command_receipts"]) == EXPECTED_TOOL_CALLS
    assert payload["preflight"]["complete"] is True
    assert payload["transport_ledger"]["sim_only"] is True
    assert payload["transport_ledger"]["live_events"] == 0
    assert payload["cleanup"]["uncleaned_resources"] == 0
    assert {call["tool"] for call in payload["tool_calls"] if call["requested_tool_covered"]} == {
        call["tool"] for call in payload["tool_calls"]
    }
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((out.parent / "probe-receipts").glob("*.json"))
    ]
    assert len(receipts) == EXPECTED_TOOL_CALLS
    assert all(receipt["fastmcp_called"] is True for receipt in receipts)
    assert {
        path.stem: json.loads(path.read_text(encoding="utf-8"))["logical_tool"]
        for path in sorted((out.parent / "probe-receipts").glob("*.json"))
    } == {tool: tool for tool in payload["unique_tools"]}
    assert {receipt["fastmcp_result_status"] for receipt in receipts} <= {
        "invalid_arguments",
        "invalid_request",
        "refused",
    }
    assert all(receipt["client_used"] is False for receipt in receipts)
    assert all(receipt["mcp_transport_used"] is False for receipt in receipts)


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
                "    print('Skills (8) saxo-auth-session, saxo-bank, saxo-openapi, "
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
                "    print(json.dumps({'tool_count': 39, 'annotations_missing': []}))",
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
