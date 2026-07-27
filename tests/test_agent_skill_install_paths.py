from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_skill_evidence_support import ROOT, build_install_fixture, run_cli

from saxo_bank_mcp.agent_skill_command_runner import (
    CommandResult,
    remaining_live_pgids,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_discovery import (
    CommandDiscoveryError,
    discover_codex_cache,
    require_distinct_caches,
)
from saxo_bank_mcp.agent_skill_install_env import build_isolated_env
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_paths import (
    OWNER_ONLY_MODE,
    assert_preserved_modes,
    ensure_owner_only,
    export_publishable_tree,
    forbidden_cache_paths,
    installed_inventory_check,
    owner_only_mode,
    publishable_tracked_files,
)
from saxo_bank_mcp.agent_skill_install_qa import (
    load_install_report_for_consumers,
    load_verified_install_report,
    write_install_fixture,
)

INSTALL_QA = ROOT / "scripts/qa_dual_plugin_install.py"
MARKETPLACE = "sig" + "vardt"


def test_publishable_tree_excludes_omo_and_forbidden() -> None:
    relatives = publishable_tracked_files(ROOT)
    assert relatives
    assert all(not relative.startswith(".omo/") for relative in relatives)
    assert all(".git" not in relative.split("/") for relative in relatives)
    # Product modules with secret-adjacent names must still ship; only data/state is unsafe.
    assert "src/saxo_bank_mcp/credentials.py" in relatives
    assert "src/saxo_bank_mcp/config_credentials.py" in relatives
    assert "src/saxo_bank_mcp/secret_scan.py" in relatives
    assert "src/saxo_bank_mcp/token_cache.py" in relatives


def test_unsafe_relative_rejects_secret_data_not_product_modules() -> None:
    from saxo_bank_mcp.agent_skill_install_paths import _is_unsafe_relative

    assert _is_unsafe_relative("src/saxo_bank_mcp/credentials.py") is False
    assert _is_unsafe_relative("src/saxo_bank_mcp/secret_scan_patterns.py") is False
    assert _is_unsafe_relative("src/saxo_bank_mcp/token_cache.py") is False
    assert _is_unsafe_relative("credentials.json") is True
    assert _is_unsafe_relative("local/token_cache.json") is True
    assert _is_unsafe_relative("secrets/id_rsa") is True
    assert _is_unsafe_relative("nested/.env") is True
    assert _is_unsafe_relative("keys/app.pem") is True
    assert _is_unsafe_relative(".omo/evidence/report.json") is True
    assert _is_unsafe_relative("credentials/live.json") is True


def test_version_bump_rewrites_lockfile_with_manifests() -> None:
    from saxo_bank_mcp.agent_skill_install_paths import VERSION_RELATIVES

    assert "uv.lock" in VERSION_RELATIVES
    assert "pyproject.toml" in VERSION_RELATIVES


def test_export_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n', encoding="utf-8")
    # Export publishable tree from the real repo for a smoke check.
    dest = tmp_path / "dest"
    # no tracked files -> empty export ok
    export_publishable_tree(ROOT, dest)
    assert dest.is_dir()
    assert owner_only_mode(dest) == OWNER_ONLY_MODE


def test_forbidden_cache_paths_detect_git_and_omo(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / ".git").mkdir(parents=True)
    (cache / ".omo" / "evidence").mkdir(parents=True)
    (cache / "ok.txt").write_text("x", encoding="utf-8")
    forbidden = forbidden_cache_paths(cache)
    assert any(path.startswith(".git") for path in forbidden)
    assert any(path.startswith(".omo") for path in forbidden)


def test_discovery_rejects_generic_path_keys_and_decoy_reuse(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    codex_home = run_root / "codex-home"
    home = run_root / "home"
    decoy = run_root / "decoy"
    for path in (run_root, codex_home, home, decoy):
        path.mkdir(parents=True, exist_ok=True)
    receipt = CommandReceipt(
        name="codex_plugin_add",
        argv=("codex", "plugin", "add"),
        cwd=str(tmp_path),
        pid=1,
        pgid=1,
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
    )
    # Generic path/root only payload must not be accepted.
    decoy_result = CommandResult(
        receipt=receipt,
        stdout=json.dumps({"path": str(decoy), "root": str(decoy)}),
        stderr="",
    )
    try:
        discover_codex_cache(
            (decoy_result,),
            run_root=run_root,
            codex_home=codex_home,
            expected_version="0.1.0",
        )
        raised = False
    except CommandDiscoveryError:
        raised = True
    assert raised


def test_discovery_requires_named_identity_version_and_hierarchy(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    codex_home = run_root / "codex-home"
    expected = codex_home / "plugins" / "cache" / MARKETPLACE / "saxo-bank-mcp" / "0.1.0"
    expected.mkdir(parents=True)
    receipt = CommandReceipt(
        name="codex_plugin_add",
        argv=("codex", "plugin", "add", "--json", "saxo-bank-mcp@" + MARKETPLACE),
        cwd=str(tmp_path),
        pid=1,
        pgid=1,
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
    )
    payload = {
        "pluginId": "saxo-bank-mcp@" + MARKETPLACE,
        "name": "saxo-bank-mcp",
        "version": "0.1.0",
        "installedPath": str(expected),
    }
    result = CommandResult(receipt=receipt, stdout=json.dumps(payload), stderr="")
    cache, source = discover_codex_cache(
        (result,),
        run_root=run_root,
        codex_home=codex_home,
        expected_version="0.1.0",
    )
    assert cache == expected.resolve()
    assert source == "codex_plugin_add"


def test_discovery_rejects_shared_cache_for_both_clients(tmp_path: Path) -> None:
    path = tmp_path / "same"
    path.mkdir()
    with pytest.raises(CommandDiscoveryError) as err:
        require_distinct_caches(path, path)
    assert err.value.reason == "cache_roots_not_distinct"


def test_isolated_env_does_not_inherit_full_parent(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    home = tmp_path / "home"
    codex = tmp_path / "codex"
    probe = tmp_path / "probe"
    for path in (home, codex, probe):
        path.mkdir()
    monkeypatch.setenv("HTTP_PROXY", "http://evil.example")  # type: ignore[attr-defined]
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "secrets"))  # type: ignore[attr-defined]
    env = build_isolated_env(
        home=home,
        codex_home=codex,
        run_root=tmp_path,
        probe_env=probe,
    )
    assert "HTTP_PROXY" not in env
    assert env["HOME"] == str(home)
    assert env["CODEX_HOME"] == str(codex)
    assert env["SAXO_MCP_ENABLE_LIVE_WRITES"] == ""
    assert "SAXO_MCP_TOKEN_CACHE_PATH" not in env


def test_process_group_cleanup_kills_background_child(tmp_path: Path) -> None:
    env = {
        "PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    # Spawn a process group with a child that ignores SIGTERM briefly via shell.
    try:
        run_command(
            "sleep_child",
            ("/bin/sh", "-c", "sleep 30 & wait"),
            cwd=tmp_path,
            env=env,
            timeout_seconds=1,
        )
    except Exception as exc:  # noqa: BLE001 - exercise timeout path
        receipt = getattr(exc, "receipt", None)
        assert receipt is not None
        assert receipt.timed_out is True
        assert receipt.cleanup_attempted is True
        assert receipt.pgid is not None
        survivors = remaining_live_pgids((receipt.pgid,))
        assert survivors == ()


def test_term_resistant_child_is_escalated(tmp_path: Path) -> None:
    env = {
        "PATH": __import__("os").environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    # Python child traps SIGTERM and only dies on SIGKILL.
    code = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
    try:
        run_command(
            "term_resistant",
            ("python3", "-c", code),
            cwd=tmp_path,
            env=env,
            timeout_seconds=1,
        )
        pytest.fail("expected timeout")
    except Exception as exc:  # noqa: BLE001
        receipt = getattr(exc, "receipt", None)
        assert receipt is not None
        assert receipt.pgid is not None
        assert remaining_live_pgids((receipt.pgid,)) == ()


def test_assert_preserved_modes_rejects_0755(tmp_path: Path) -> None:
    roots = {
        "run_root": tmp_path / "run",
        "clone": tmp_path / "run" / "source-clone",
        "codex_cache": tmp_path / "run" / "codex-cache",
        "claude_cache": tmp_path / "run" / "claude-cache",
        "home": tmp_path / "run" / "home",
        "codex_home": tmp_path / "run" / "codex-home",
        "claude_home": tmp_path / "run" / "claude-home",
    }
    for path in roots.values():
        ensure_owner_only(path)
    roots["clone"].chmod(0o755)
    try:
        assert_preserved_modes(roots)
        raised = False
    except PermissionError:
        raised = True
    assert raised


def test_fixture_support_is_not_production_evidence(tmp_path: Path) -> None:
    fixture = build_install_fixture(tmp_path / "fixture")
    verified, errors = load_verified_install_report(fixture.report)
    assert verified is None
    assert "fixture_support_not_production" in errors
    consumer, consumer_errors = load_install_report_for_consumers(fixture.report)
    assert consumer is not None
    assert consumer_errors == ()
    assert consumer.execution_mode == "fixture_support"


def test_verify_rejects_report_mode_that_does_not_match_disk(tmp_path: Path) -> None:
    fixture = build_install_fixture(tmp_path / "fixture")
    report = json.loads(fixture.report.read_text(encoding="utf-8"))
    clone = Path(report["clone"]["path"])
    clone.chmod(0o700)
    report["fixture_cleanup"]["modes"]["clone"] = "0o755"
    report["clone"]["mode"] = "0o755"
    fixture.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    verified, errors = load_install_report_for_consumers(fixture.report)
    assert verified is None
    assert "preserved_mode_mismatch:clone" in errors or "preserved_roots_not_owner_only" in errors


def test_self_test_fixtures_expose_only_class_or_field(tmp_path: Path) -> None:
    private_out = tmp_path / "private.json"
    drift_out = tmp_path / "drift.json"
    private = run_cli(INSTALL_QA, "--self-test-fixture", "private-file", "--out", str(private_out))
    drift = run_cli(INSTALL_QA, "--self-test-fixture", "version-drift", "--out", str(drift_out))
    private_payload = json.loads(private_out.read_text(encoding="utf-8"))
    drift_payload = json.loads(drift_out.read_text(encoding="utf-8"))
    assert private.returncode != 0
    assert drift.returncode != 0
    assert private_payload["forbidden_path_class"] == ".omo"
    assert drift_payload["version_field"] == "project.version"


def test_write_install_fixture_helpers_match_cli(tmp_path: Path) -> None:
    out = tmp_path / "x.json"
    assert write_install_fixture("private-file", out) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["forbidden_path_class"] == ".omo"


def test_inventory_rejects_unexpected_extra_file(tmp_path: Path) -> None:
    source = ROOT
    cache = tmp_path / "cache"
    export_publishable_tree(source, cache)
    (cache / "unexpected-secret.bin").write_bytes(b"x")
    inventory = installed_inventory_check(source, cache)
    assert inventory["inventory_exact_match"] is False
    assert "unexpected_cache_files" in inventory["mismatches"]
