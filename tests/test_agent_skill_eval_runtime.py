from __future__ import annotations

import inspect
import json
import os
import shutil
import stat
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest

import saxo_bank_mcp.agent_skill_eval_commands as eval_commands
import saxo_bank_mcp.agent_skill_eval_execution as eval_execution
import saxo_bank_mcp.agent_skill_eval_process as eval_process
import saxo_bank_mcp.agent_skill_eval_runner as eval_runner
import saxo_bank_mcp.agent_skill_matrix_env as matrix_env
import saxo_bank_mcp.agent_skill_router_eval_execution as router_execution
from saxo_bank_mcp import agent_skill_install_cli_driver as cli_driver
from saxo_bank_mcp.agent_skill_command_runner import (
    ProcessCleanupTerminalSnapshot,
    process_group_members,
    process_still_running,
    remaining_live_pgids,
    remaining_live_pids,
)
from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots, execute_model_case
from saxo_bank_mcp.agent_skill_eval_models import (
    EvalRunRecord,
    EvalRunReport,
    RouterDecision,
    SkillEvalCase,
    load_eval_cases,
)
from saxo_bank_mcp.agent_skill_eval_native_preflight import (
    CodexNativeCaseMcpBinding,
    CodexNativePreflightReceipt,
)
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager, ManagedProcessResult
from saxo_bank_mcp.agent_skill_eval_runner import EvalRunOptions, run_eval_suite
from saxo_bank_mcp.agent_skill_matrix_env import (
    OWNER_DIR_MODE,
    OWNER_FILE_MODE,
    MatrixEnvError,
    eval_runtime_root,
    prepare_eval_isolated_runtime,
    require_matrix_runtime_cleanup,
    resolve_actual_claude_auth_home,
    resolve_actual_codex_auth_home,
)
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    RouterCaseContext,
    RouterHomes,
    RouterSourceBinding,
    execute_router_model_case,
)
from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.token_cache import save_token_cache

TIMEOUT_EXIT_CODE: Final = 124
CLAUDE_SETTINGS_CANARY: Final = "GLOBAL-SETTINGS-HOOKS-CANARY"
CLAUDE_MCP_CANARY: Final = "GLOBAL-MCP-CANARY"
CLAUDE_PROJECT_HISTORY_CANARY: Final = "PROJECT-HISTORY-CANARY"
CLAUDE_HOOKS_CANARY: Final = "HOOKS-DIR-CANARY"
CLAUDE_SESSION_HISTORY_CANARY: Final = "SESSION-HISTORY-CANARY"

ROOT: Final = Path(__file__).resolve().parents[1]
CASE_ROOT: Final = ROOT / "evals/saxo-bank"
DIGEST: Final = "d" * 64
BOTH_HARNESS_COUNT: Final = 2
VERSION_PROBE_CLEANUP_CALL_INDEX: Final = 2


def _sim_token(
    *,
    access: str = "access-token-original",
    refresh: str | None = "refresh-token-original",
    verifier: str | None = "code-verifier-original",
    environment: TokenEnvironment | None = "SIM",
) -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=access,
        refresh_token=refresh,
        code_verifier=verifier,
        environment=environment,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _write_auth_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: SaxoTokenSet | None = None,
) -> tuple[Path, Path]:
    source_root = tmp_path / "auth-source"
    source_root.mkdir(exist_ok=True)
    credential = source_root / "sim-credentials.txt"
    token_path = source_root / "token-cache.json"
    credential.write_text(
        "App Key\n"
        "app-key-fixture-value\n"
        "Access Control\n"
        "code\n"
        "Grant Type\n"
        "PKCE\n"
        "Auth endpoint\n"
        "https://sim.logonvalidation.net/authorize\n"
        "Token endpoint\n"
        "https://sim.logonvalidation.net/token\n",
        encoding="utf-8",
    )
    save_token_cache(token_path, token or _sim_token())
    credential.chmod(0o600)
    monkeypatch.setenv("SAXO_MCP_SIM_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(token_path))
    monkeypatch.delenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_APP_KEY", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_CLIENT_ID", raising=False)
    return credential, token_path


def _seed_cli_auth_sources(tmp_path: Path) -> tuple[Path, Path]:
    """Actual global CLI auth roots (source_*). No retained plugin registration here."""
    codex = tmp_path / "actual-codex-auth"
    claude = tmp_path / "actual-claude-auth"
    codex.mkdir()
    claude.mkdir()
    (codex / "auth.json").write_text('{"token":"codex-auth-fixture"}\n', encoding="utf-8")
    (codex / "auth.json").chmod(0o600)
    claude_cfg = claude / ".claude"
    claude_cfg.mkdir()
    # Canaries: global settings/hooks/MCP/project/history must never seed into runtime.
    (claude_cfg / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {"PreToolUse": [{"command": CLAUDE_SETTINGS_CANARY}]},
                "mcpServers": {"canary": {"command": CLAUDE_MCP_CANARY}},
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (claude_cfg / "settings.json").chmod(0o600)
    (claude / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    "/canary-project": {
                        "history": [CLAUDE_PROJECT_HISTORY_CANARY],
                    },
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (claude / ".claude.json").chmod(0o600)
    hooks_dir = claude_cfg / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "canary.sh").write_text(f"{CLAUDE_HOOKS_CANARY}\n", encoding="utf-8")
    (claude_cfg / ".credentials.json").write_text(
        '{"claude":"auth-fixture"}\n',
        encoding="utf-8",
    )
    (claude_cfg / ".credentials.json").chmod(0o600)
    # Transcripts/history must never be copied into disposable homes.
    history_line = f"{CLAUDE_SESSION_HISTORY_CANARY}\n"
    (codex / "history.jsonl").write_text(history_line, encoding="utf-8")
    (claude_cfg / "history.jsonl").write_text(history_line, encoding="utf-8")
    return codex, claude


def _seed_retained_plugin_homes(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Retained install homes + codex plugin root (options.codex_home / claude_home)."""
    codex = tmp_path / "retained-codex-home"
    claude = tmp_path / "retained-claude-home"
    codex.mkdir()
    claude.mkdir()
    (codex / "config.toml").write_text("# retained plugin registration\n", encoding="utf-8")
    (codex / "config.toml").chmod(0o600)
    plugins = codex / "plugins"
    plugins.mkdir()
    (plugins / "index.json").write_text('{"plugins":{}}\n', encoding="utf-8")
    (plugins / "index.json").chmod(0o600)
    plugin_root = plugins / "cache" / "sigvardt" / "saxo-bank-mcp" / "0.1.0"
    plugin_root.mkdir(parents=True)
    skill = plugin_root / "skills" / "saxo-bank" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# retained plugin\n", encoding="utf-8")
    skill.chmod(0o600)
    marketplace_manifest = plugin_root / ".agents" / "plugins" / "marketplace.json"
    marketplace_manifest.parent.mkdir(parents=True)
    marketplace_manifest.write_text(
        json.dumps(
            {
                "name": "sigvardt",
                "plugins": [
                    {
                        "name": "saxo-bank-mcp",
                        "source": {"source": "local", "path": "."},
                        "version": "0.1.0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir()
    plugin_manifest.write_text(
        json.dumps({"name": "saxo-bank-mcp", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (plugin_root / "history.jsonl").write_text("raw-transcript\n", encoding="utf-8")
    claude_cfg = claude / ".claude"
    claude_cfg.mkdir()
    plugin_dir = claude_cfg / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "installed_plugins.json").write_text("{}\n", encoding="utf-8")
    (plugin_dir / "installed_plugins.json").chmod(0o600)
    (plugin_dir / "known_marketplaces.json").write_text("{}\n", encoding="utf-8")
    (plugin_dir / "known_marketplaces.json").chmod(0o600)
    return codex, claude, plugin_root


def _seed_cli_sources(tmp_path: Path) -> tuple[Path, Path]:
    """Compat helper: auth sources only (plugin state is seeded separately)."""
    return _seed_cli_auth_sources(tmp_path)


def _claude_credential_document(marker: str, *, expires_at: int) -> dict[str, object]:
    """Synthetic file-backed Claude OAuth document.

    The material is bound to a local name so no long literal follows an
    ``accessToken``/``refreshToken`` key and the public secret scan stays clean.
    """
    material = f"synthetic-{marker}-oauth-material"
    return {
        "claudeAiOauth": {
            "accessToken": material,
            "refreshToken": material + "-refresh",
            "expiresAt": expires_at,
            "refreshTokenExpiresAt": expires_at,
        },
        "mcpOAuth": {},
    }


def _claude_global_canaries() -> tuple[str, ...]:
    return (
        CLAUDE_SETTINGS_CANARY,
        CLAUDE_MCP_CANARY,
        CLAUDE_PROJECT_HISTORY_CANARY,
        CLAUDE_HOOKS_CANARY,
        CLAUDE_SESSION_HISTORY_CANARY,
    )


def _assert_runtime_excludes_claude_global_canaries(run_root: Path) -> None:
    """Prove settings/hooks/MCP/project/history canaries never enter runtime files."""
    canaries = _claude_global_canaries()
    assert not (run_root / "home" / ".claude" / "settings.json").exists()
    assert not (run_root / "home" / ".claude.json").exists()
    assert not (run_root / "home" / ".claude" / "hooks").exists()
    assert not (run_root / "home" / ".claude" / "history.jsonl").exists()
    for path in run_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for canary in canaries:
            assert canary not in text, f"{canary} leaked into {path.relative_to(run_root)}"
            assert canary not in path.name


def _binding() -> RouterSourceBinding:
    return RouterSourceBinding(
        source_commit="abc123",
        router_source_sha256=DIGEST,
        file_digests={"skills/saxo-bank/SKILL.md": DIGEST},
        file_contents={},
        codex_plugin_root=str(ROOT),
        claude_plugin_root=str(ROOT),
    )


def _passed_record(
    case: SkillEvalCase,
    harness: str,
    *,
    error: str = "",
) -> EvalRunRecord:
    failed = bool(error)
    router = case.router_expectation is not None
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,  # type: ignore[arg-type]
        status="failed" if failed else "passed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=(),
        transcript_assertions_passed=not failed,
        no_model_call=False,
        no_mcp_call=True,
        no_saxo_call=True,
        error=error,
        client_version="codex 0.0-test" if harness == "codex" else "claude 0.0-test",
        router_source_mode="source_equivalent" if router else None,
        router_source_sha256=DIGEST if router else "",
        model_tool_event_count=0 if router else None,
        model_command_event_count=0 if router else None,
        model_mcp_event_count=0 if router else None,
        model_saxo_event_count=0 if router else None,
    )


def _options(  # noqa: PLR0913
    tmp_path: Path,
    *,
    dry_run: bool,
    credential_mode: str,
    case_id: str = "router-auth",
    harness: str = "both",
    source_codex_home: Path | None = None,
    source_claude_home: Path | None = None,
    expected_source_commit: str | None = None,
) -> EvalRunOptions:
    return EvalRunOptions(
        harness=harness,  # type: ignore[arg-type]
        case_id=case_id,
        tag=None,
        environment=None,
        case_root=CASE_ROOT,
        codex_plugin_root=ROOT,
        claude_plugin_root=ROOT,
        codex_home=None,
        claude_home=None,
        out=tmp_path / "out" / "evals.json",
        dry_run=dry_run,
        nonzero_on_skip=False,
        expected_source_commit=expected_source_commit,
        credential_mode=credential_mode,
        source_codex_home=source_codex_home,
        source_claude_home=source_claude_home,
    )


def _install_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(_request: object) -> RouterSourceBinding:
        return _binding()

    monkeypatch.setattr(eval_runner, "resolve_router_source_binding", _resolve)


def _stub_versions(
    *,
    env: dict[str, str],
    process_manager: object | None = None,
) -> dict[str, str]:
    _ = (env, process_manager)
    return {"codex": "c", "claude": "c"}


def test_prepare_eval_runtime_strips_parent_secrets_and_is_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_auth_sources(tmp_path)
    retained_codex, retained_claude, plugin_root = _seed_retained_plugin_homes(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "evil-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "evil-codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-parent-anthropic")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(tmp_path / "live-creds"))
    (tmp_path / "live-creds").write_text("live", encoding="utf-8")

    runtime = prepare_eval_isolated_runtime(
        evidence,
        source_codex_home=codex_src,
        source_claude_home=claude_src,
        retained_codex_home=retained_codex,
        retained_claude_home=retained_claude,
        retained_codex_plugin_root=plugin_root,
    )
    try:
        root = runtime.run_root.resolve()
        assert root == eval_runtime_root(evidence).resolve()
        assert (root.stat().st_mode & 0o777) == OWNER_DIR_MODE
        assert runtime.env["SAXO_MCP_ENVIRONMENT"] == "SIM"
        assert runtime.env["SAXO_MCP_ENABLE_LIVE_READS"] == "0"
        assert runtime.env["SAXO_MCP_ENABLE_LIVE_WRITES"] == ""
        assert "SAXO_MCP_ACCOUNT_ALLOWLIST" not in runtime.env
        assert "OPENAI_API_KEY" not in runtime.env
        assert "ANTHROPIC_API_KEY" not in runtime.env
        assert "SAXO_MCP_LIVE_APP_KEY" not in runtime.env
        assert "SAXO_MCP_LIVE_CREDENTIAL_FILE" not in runtime.env
        assert Path(runtime.env["HOME"]).resolve().is_relative_to(root)
        assert Path(runtime.env["CODEX_HOME"]).resolve().is_relative_to(root)
        assert Path(runtime.env["SAXO_MCP_SIM_CREDENTIAL_FILE"]).resolve().is_relative_to(root)
        assert Path(runtime.env["SAXO_MCP_TOKEN_CACHE_PATH"]).resolve().is_relative_to(root)
        for path in (runtime.sim_credential_path, runtime.token_cache_path):
            mode = path.stat().st_mode
            assert stat.S_ISREG(mode)
            assert (mode & 0o777) == OWNER_FILE_MODE
        assert (runtime.codex_home / "auth.json").is_file()
        assert (runtime.home / ".claude" / ".credentials.json").is_file()
        assert (runtime.codex_home / "plugins" / "index.json").is_file()
        assert (runtime.codex_home / "config.toml").is_file()
        seeded_plugin = (
            runtime.codex_home / "plugins" / "cache" / "sigvardt" / "saxo-bank-mcp" / "0.1.0"
        )
        assert (seeded_plugin / "skills" / "saxo-bank" / "SKILL.md").is_file()
        assert (seeded_plugin / "skills" / "saxo-bank" / "SKILL.md").read_bytes() == (
            plugin_root / "skills" / "saxo-bank" / "SKILL.md"
        ).read_bytes()
        assert not (seeded_plugin / "history.jsonl").exists()
        # File-auth only: never seed global settings, hooks, MCP, project, or history.
        _assert_runtime_excludes_claude_global_canaries(runtime.run_root)
        assert str(codex_src) not in json.dumps({"env": runtime.env})
        assert str(claude_src) not in json.dumps({"env": runtime.env})
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)
        assert not runtime.run_root.exists()


def test_prepare_codex_native_eval_runtime_never_reads_or_copies_claude_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, _claude_src = _seed_cli_auth_sources(tmp_path)
    retained_codex, _retained_claude, plugin_root = _seed_retained_plugin_homes(tmp_path)

    def reject_claude_lookup(_preferred: Path | None) -> Path:
        raise AssertionError("native runtime resolved Claude state")

    monkeypatch.setattr(matrix_env, "_resolve_claude_source_home", reject_claude_lookup)

    runtime = prepare_eval_isolated_runtime(
        evidence,
        source_codex_home=codex_src,
        source_claude_home=None,
        retained_codex_home=retained_codex,
        retained_claude_home=None,
        retained_codex_plugin_root=plugin_root,
        harness_policy="codex_native_v1",
    )
    try:
        assert (runtime.codex_home / "auth.json").is_file()
        assert (runtime.codex_home / "config.toml").is_file()
        config_path = runtime.codex_home / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        marketplace = config["marketplaces"]["sigvardt"]
        contained_marketplace = Path(marketplace["source"]).resolve()
        assert contained_marketplace.is_relative_to(runtime.codex_home.resolve())
        assert (contained_marketplace / ".agents/plugins/marketplace.json").is_file()
        assert str(retained_codex.resolve()) not in config_path.read_text(encoding="utf-8")
        assert not (runtime.home / ".claude").exists()
        assert "CLAUDE_CONFIG_DIR" not in runtime.env
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in runtime.env
        assert "EVAL_CLI_CLAUDE_BIN" not in runtime.env
        assert runtime.claude_auth_source is None
        assert runtime.claude_auth_source_digest is None
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)


def test_codex_native_runtime_uses_exact_marketplace_registration_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, _claude_src = _seed_cli_auth_sources(tmp_path)
    retained_codex, _retained_claude, plugin_root = _seed_retained_plugin_homes(tmp_path)
    calls: list[tuple[Path, Path]] = []

    def fake_install(marketplace: Path, env: dict[str, str]) -> tuple[object, ...]:
        home = Path(env["CODEX_HOME"])
        calls.append((marketplace.resolve(), home.resolve()))
        target = home / "plugins/cache/sigvardt/saxo-bank-mcp/0.1.0"
        shutil.copytree(marketplace, target)
        (home / "config.toml").write_text("# installed by exact CLI flow\n", encoding="utf-8")
        (home / "config.toml").chmod(0o600)
        return ()

    monkeypatch.setattr(cli_driver, "run_codex_install", fake_install)

    runtime = prepare_eval_isolated_runtime(
        evidence,
        source_codex_home=codex_src,
        source_claude_home=None,
        retained_codex_home=retained_codex,
        retained_claude_home=None,
        retained_codex_plugin_root=plugin_root,
        harness_policy="codex_native_v1",
    )
    try:
        assert len(calls) == 1
        marketplace, home = calls[0]
        assert home == runtime.codex_home.resolve()
        assert marketplace.is_relative_to(runtime.run_root.resolve())
        assert not marketplace.is_relative_to(runtime.codex_home.resolve() / "plugins/cache")
        assert (
            runtime.codex_home
            / "plugins/cache/sigvardt/saxo-bank-mcp/0.1.0/skills/saxo-bank/SKILL.md"
        ).is_file()
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)


def test_prepare_eval_runtime_rejects_symlink_cli_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    real = tmp_path / "real-auth.json"
    real.write_text("{}", encoding="utf-8")
    real.chmod(0o600)
    link = codex_src / "auth.json"
    link.unlink()
    link.symlink_to(real)
    with pytest.raises(MatrixEnvError, match="cli_auth_source_symlink"):
        prepare_eval_isolated_runtime(
            evidence,
            source_codex_home=codex_src,
            source_claude_home=claude_src,
        )
    assert not eval_runtime_root(evidence).exists()


def test_dry_run_and_none_mode_remain_usable_without_clients(tmp_path: Path) -> None:
    options = _options(tmp_path, dry_run=True, credential_mode="none")
    options.out.parent.mkdir(parents=True, exist_ok=True)
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["status"] == "planned"
    assert payload["cleanup"]["credential_mode"] == "none"
    assert payload["cleanup"]["runtime_cleanup"] == "not_required"


def test_unknown_credential_mode_fails_closed(tmp_path: Path) -> None:
    options = _options(tmp_path, dry_run=True, credential_mode="owned-copy")
    options.out.parent.mkdir(parents=True, exist_ok=True)
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    assert code != 0
    assert payload["status"] == "failed"
    assert payload["cleanup"]["source_binding"]["error"] == "credential_mode_unknown"


def test_malformed_model_output_keeps_runner_event_aggregates_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-auth")
    record = EvalRunRecord(
        case_id=case.id,
        harness="codex",
        status="failed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=(),
        transcript_assertions_passed=None,
        no_model_call=False,
        no_mcp_call=None,
        no_saxo_call=None,
        model_output_observability="unknown",
        error="malformed_output",
        model_tool_event_count=None,
        model_command_event_count=None,
        model_mcp_event_count=None,
        model_saxo_event_count=None,
        invoked_logical_tools=None,
        invoked_logical_tool_count=None,
        grant_status="unknown",
        assertion_status="unknown",
    )

    def malformed_outcome(_options: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            records=(record,),
            enforced_mode="none",
            versions={"codex": "test"},
            cleanup={
                "complete": True,
                "process_cleanup": "passed",
                "runtime_cleanup": "not_required",
                "token_promote": "not_required",
                "created_processes": 1,
                "terminated_processes": 1,
                "remaining_processes": 0,
            },
            error="malformed_output",
        )

    monkeypatch.setattr(eval_runner, "_select_execution_outcome", malformed_outcome)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        harness="codex",
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)

    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code != 0
    assert payload["cleanup"]["model_tool_events"] is None
    assert payload["cleanup"]["model_command_events"] is None
    assert payload["cleanup"]["created_mcp_calls"] is None
    assert payload["cleanup"]["model_saxo_events"] is None
    assert payload["cleanup"]["invoked_logical_tool_count"] is None


@pytest.mark.parametrize(
    ("expected_error", "result"),
    [
        (
            "timeout_expired",
            ManagedProcessResult(
                stdout="private-output-must-not-be-parsed",
                stderr="",
                returncode=124,
                timed_out=True,
                created_processes=1,
                terminated_processes=1,
                remaining_processes=0,
                process_cleanup="passed",
            ),
        ),
        (
            "process_cleanup_unknown",
            ManagedProcessResult(
                stdout="private-output-must-not-be-parsed",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=0,
                remaining_processes=None,
                process_cleanup="unknown",
            ),
        ),
        (
            "process_cleanup_residue",
            ManagedProcessResult(
                stdout="private-output-must-not-be-parsed",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=0,
                remaining_processes=1,
                process_cleanup="residue",
            ),
        ),
    ],
)
def test_process_record_refuses_unobservable_post_launch_result(
    expected_error: str,
    result: ManagedProcessResult,
) -> None:
    """An unparsed post-launch result never becomes an observable empty trace."""
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-auth")
    record_from_process = eval_execution._record_from_process  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    record = record_from_process(case, "codex", (), result)

    assert record.status == "failed"
    assert record.error == expected_error
    assert record.transcript_assertions_passed is None
    assert record.model_output_observability == "unknown"
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_tool_event_count is None
    assert record.model_command_event_count is None
    assert record.model_mcp_event_count is None
    assert record.model_saxo_event_count is None
    assert record.invoked_logical_tools is None
    assert record.invoked_logical_tool_count is None
    assert record.grant_status == "unknown"
    assert record.assertion_status == "unknown"
    assert record.assistant_message_present is None
    assert "private-output-must-not-be-parsed" not in record.model_dump_json()


@pytest.mark.parametrize(
    ("returncode", "expected_error"),
    [(0, "malformed_output"), (1, "process_nonzero_exit")],
)
def test_parser_exception_after_launch_is_unobservable(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected_error: str,
) -> None:
    """A parser exception cannot turn an executed child into a known empty trace."""
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-auth")
    result = ManagedProcessResult(
        stdout="private-unparsed-output",
        stderr="",
        returncode=returncode,
        timed_out=False,
        created_processes=1,
        terminated_processes=1,
        remaining_processes=0,
        process_cleanup="passed",
    )

    def fail_parse(_harness: str, _stdout: str) -> eval_execution.ModelToolTrace:
        raise ValueError("injected parser failure")

    monkeypatch.setattr(eval_execution, "_parse_trace", fail_parse)
    record = eval_execution._record_from_stdout(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        case, "codex", (), result
    )

    assert record.error == expected_error
    assert record.transcript_assertions_passed is None
    assert record.model_output_observability == "unknown"
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_command_event_count is None
    assert record.invoked_logical_tools is None
    assert record.assertion_status == "unknown"
    assert "private-unparsed-output" not in record.model_dump_json()


@pytest.mark.parametrize("case_id", ["qa-evidence-readiness", "router-auth"])
@pytest.mark.parametrize("failure", [ProcessLookupError(), OSError("post-spawn failure")])
def test_post_spawn_scope_failure_stays_unknown_in_runner_and_failure_summary(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    failure: OSError,
) -> None:
    """A production runner cannot publish complete cleanup or zero calls after Popen."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    _install_binding(monkeypatch)
    if case_id == "router-auth":
        actual_digest = router_execution.router_source_digest(ROOT)

        def resolve_router_binding(_request: object) -> RouterSourceBinding:
            return replace(_binding(), router_source_sha256=actual_digest)

        monkeypatch.setattr(eval_runner, "resolve_router_source_binding", resolve_router_binding)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)

    class FakeProcess:
        pid = 73_200
        returncode: int | None = None

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(eval_process.subprocess, "Popen", fake_popen)

    def fail_getpgid(_pid: int) -> int:
        raise failure

    monkeypatch.setattr(eval_process.os, "getpgid", fail_getpgid)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        case_id=case_id,
        harness="codex",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
        expected_source_commit="abc123",
    )

    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code != 0
    assert payload["cleanup"]["complete"] is False
    assert payload["cleanup"]["created_processes"] == 1
    assert payload["cleanup"]["terminated_processes"] == 0
    assert payload["cleanup"]["remaining_processes"] is None
    assert payload["cleanup"]["process_cleanup"] == "unknown"
    assert payload["cleanup"]["model_tool_events"] is None
    assert payload["cleanup"]["model_command_events"] is None
    assert payload["cleanup"]["created_mcp_calls"] is None
    assert payload["cleanup"]["model_saxo_events"] is None
    assert payload["cleanup"]["invoked_logical_tool_count"] is None
    record = payload["records"][0]
    assert record["error"] == (
        "process_lookup_error" if isinstance(failure, ProcessLookupError) else "os_error"
    )
    assert record["transcript_assertions_passed"] is None
    assert record["model_output_observability"] == "unknown"
    assert record["no_mcp_call"] is None
    assert record["no_saxo_call"] is None
    assert record["invoked_logical_tools"] is None
    assert record["invoked_logical_tool_count"] is None
    assert record["grant_status"] == "unknown"
    assert record["assertion_status"] == "unknown"

    report_payload = dict(payload)
    report_payload.pop("installation_fixture_preserved")
    report_payload.pop("run_cleanup")
    report = EvalRunReport.model_validate(report_payload)
    report_bytes = options.out.read_bytes()
    summary = producer._agent_evaluation_failure_summary(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        report_bytes=report_bytes,
        report=report,
    )
    assert summary is not None
    assert summary.cleanup.status == "unknown"
    assert summary.cleanup.remaining_process_count is None
    assert summary.cases[0].model_output_observability == "unknown"
    assert summary.cases[0].no_mcp_call is None
    assert summary.cases[0].no_saxo_call is None


def test_codex_native_policy_rejects_non_codex_harness(tmp_path: Path) -> None:
    options = replace(
        _options(tmp_path, dry_run=True, credential_mode="none"),
        harness_policy="codex_native_v1",
    )

    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code != 0
    assert payload["cleanup"]["source_binding"]["error"] == "native_harness_must_be_codex"
    assert payload["cleanup"]["credential_mode"] == "none"


def test_codex_native_default_selection_excludes_live_cases(tmp_path: Path) -> None:
    options = replace(
        _options(
            tmp_path,
            dry_run=True,
            credential_mode="none",
            case_id="router-auth",
            harness="codex",
        ),
        case_id=None,
        harness_policy="codex_native_v1",
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)

    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code == 0
    assert payload["environment"] == "LOCAL+SIM"
    assert payload["records"]
    assert not {
        "live-read-precheck-no-purchase",
        "router-approval-bypass",
        "router-live-trade",
    } & {record["case_id"] for record in payload["records"]}


def test_codex_native_analytics_execution_binds_schema_valid_fixture_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, _claude_src = _seed_cli_auth_sources(tmp_path)
    captured_prompts: list[str] = []

    def fake_execute_model_case(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = (grants, roots, env, expected_router_source_sha256, process_manager)
        captured_prompts.append(case.harness_prompts["codex"])
        return _passed_record(case, harness)

    def fake_codex_client_version(**_kwargs: object) -> str:
        return "codex-test"

    def fake_native_preflight(**kwargs: object) -> CodexNativePreflightReceipt:
        binding = kwargs["mcp_config_binding"]
        logical_grants = cast("tuple[object, ...]", kwargs["logical_grants"])
        assert isinstance(binding, CodexNativeCaseMcpBinding)
        assert isinstance(logical_grants, tuple)
        assert all(isinstance(item, str) for item in logical_grants)
        return CodexNativePreflightReceipt(
            plugin_enabled=True,
            mcp_started=True,
            visible_logical_tools=cast("tuple[str, ...]", logical_grants),
            plugin_list_exit_code=0,
            plugin_list_stdout_schema_sha256="e" * 64,
            mcp_probe_stage="complete",
            mcp_probe_exit_code=0,
            mcp_probe_stdout_schema_sha256="f" * 64,
            mcp_config_sha256=binding.config_sha256,
            mcp_config_path_identity_sha256=binding.path_identity_sha256,
        )

    monkeypatch.setattr(eval_runner, "codex_client_version", fake_codex_client_version)
    monkeypatch.setattr(eval_runner, "execute_model_case", fake_execute_model_case)
    monkeypatch.setattr(
        eval_runner,
        "preflight_codex_native_case",
        fake_native_preflight,
    )
    options = EvalRunOptions(
        harness="codex",
        case_id="artifact-delivery",
        tag=None,
        environment=None,
        case_root=ROOT / "evals",
        codex_plugin_root=ROOT,
        claude_plugin_root=ROOT,
        codex_home=None,
        claude_home=None,
        out=tmp_path / "out" / "evals.json",
        dry_run=False,
        nonzero_on_skip=True,
        credential_mode="ephemeral-owner-only-copy",
        source_codex_home=codex_src,
        source_claude_home=None,
        harness_policy="codex_native_v1",
    )

    code = run_eval_suite(options)

    assert code == 0
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert prompt.startswith(
        "Use $saxo-bank-mcp:saxo-analytics for this installed Codex-native hard workflow."
    )
    assert (
        'saxo_render_analysis {"analysis_id":"an_00000000000040008000000000000000",'
        '"template_id":"relative_performance"}'
    ) in prompt
    assert (
        'saxo_manage_analysis_job {"action":"check","job_id":"jb_00000000000040008000000000000000"}'
    ) in prompt
    assert "Call every required tool even when an earlier fixture is refused" in prompt
    assert "Do not pre-emptively refuse before making these controlled QA calls" in prompt
    assert "The analytics capability context is already current for this case" in prompt
    assert "The harness owns cleanup for this contained fixture" in prompt
    assert "Never call an ungranted capability or deletion tool" in prompt
    assert "Final answer must include: analysis_id; owner-only; quality warnings" in prompt
    assert (
        "Final receipt: `analysis_id: <returned or fixture id>; "
        "state: <verified|degraded|refused>; owner-only; quality warnings`."
    ) in prompt


def test_codex_native_research_precheck_prompt_requires_exact_final_receipt() -> None:
    case = next(
        candidate
        for candidate in load_eval_cases(ROOT / "evals/saxo-analytics")
        if candidate.id == "research-to-precheck"
    )

    bound = eval_runner._codex_native_fixture_bound_case(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        case,
        harness="codex",
        harness_policy="codex_native_v1",
    )

    prompt = bound.harness_prompts["codex"]
    assert "Final receipt:" in prompt
    assert "analysis_id: <result analysis_id or fixture analysis_id>" in prompt
    assert "state: <verified|degraded|refused>" in prompt
    assert "stop before broker write" in prompt


def test_codex_native_scenario_prompt_requires_exact_phrase_then_numeric_values() -> None:
    case = next(
        candidate
        for candidate in load_eval_cases(ROOT / "evals/saxo-analytics")
        if candidate.id == "scenario"
    )

    bound = eval_runner._codex_native_fixture_bound_case(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        case,
        harness="codex",
        harness_policy="codex_native_v1",
    )

    prompt = bound.harness_prompts["codex"]
    assert (
        "Final scenario receipt must contain this exact ordered text: "
        "explicit numeric shocks -0.10 0.05."
    ) in prompt
    assert "Final scenario receipt" not in case.harness_prompts["claude"]


def test_codex_native_safety_execution_prompt_binds_exact_case_id() -> None:
    case = next(
        candidate
        for candidate in load_eval_cases(ROOT / "evals/saxo-bank")
        if candidate.id == "codex-native-safety-boundary"
    )

    bound = eval_runner._codex_native_fixture_bound_case(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        case,
        harness="codex",
        harness_policy="codex_native_v1",
    )

    prompt = bound.harness_prompts["codex"]
    assert "Use $saxo-bank-mcp:saxo-qa-operations" in prompt
    assert "case ID codex-native-safety-boundary" in prompt


def test_saxo_qa_openai_metadata_invokes_skill_without_blanket_plan_only_mismatch() -> None:
    metadata = (ROOT / "skills/saxo-qa-operations/agents/openai.yaml").read_text(encoding="utf-8")

    assert "Use $saxo-bank-mcp:saxo-qa-operations" in metadata
    assert "without executing model, MCP, or broker calls" not in metadata


def test_codex_native_policy_rejects_explicit_live_selection(tmp_path: Path) -> None:
    options = replace(
        _options(
            tmp_path,
            dry_run=True,
            credential_mode="none",
            case_id="router-live-trade",
            harness="codex",
        ),
        environment="LIVE",
        harness_policy="codex_native_v1",
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)

    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code != 0
    assert payload["cleanup"]["source_binding"]["error"] == "native_live_environment_forbidden"
    assert payload["records"] == []


def test_sim_model_execution_without_ephemeral_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="none",
        expected_source_commit="HEAD",
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)
    _install_binding(monkeypatch)
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    assert code != 0
    assert payload["cleanup"]["runtime_error"] == "credential_mode_required_for_sim"
    assert payload["cleanup"]["credential_mode"] == "none"
    assert payload["case_count"] == 0


def test_both_execution_paths_receive_exact_isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    captured: dict[str, list[dict[str, str]]] = {
        "router_envs": [],
        "model_envs": [],
        "version_envs": [],
    }

    def fake_client_versions(
        *,
        env: dict[str, str],
        process_manager: object | None = None,
    ) -> dict[str, str]:
        _ = process_manager
        captured["version_envs"].append(dict(env))
        return {"codex": "codex 0.0-test", "claude": "claude 0.0-test"}

    def fake_execute_model_case(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = process_manager
        _ = (grants, roots, expected_router_source_sha256)
        if case.router_expectation is not None:
            captured["router_envs"].append(dict(env))
        else:
            captured["model_envs"].append(dict(env))
        return _passed_record(case, harness)

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", fake_client_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", fake_execute_model_case)

    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    assert code == 0, payload
    assert payload["cleanup"]["credential_mode"] == "ephemeral-owner-only-copy"
    assert payload["cleanup"]["runtime_cleanup"] == "passed"
    # token_promote is a status label, not a secret.
    assert payload["cleanup"]["token_promote"] == "passed"  # noqa: S105
    assert payload["run_cleanup"]["complete"] is True
    assert not eval_runtime_root(evidence).exists()
    assert len(captured["version_envs"]) == 1
    assert len(captured["router_envs"]) == BOTH_HARNESS_COUNT
    shared = captured["version_envs"][0]
    for env in (*captured["router_envs"], *captured["version_envs"]):
        assert env == shared
        assert env["SAXO_MCP_ENVIRONMENT"] == "SIM"
        assert "OPENAI_API_KEY" not in env
        assert Path(env["HOME"]).resolve().is_relative_to(evidence.resolve())
        assert Path(env["CODEX_HOME"]).resolve().is_relative_to(evidence.resolve())


def test_non_router_path_receives_same_isolated_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    seen: list[dict[str, str]] = []

    def fake_execute_model_case(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = process_manager
        _ = (grants, roots, expected_router_source_sha256)
        assert case.router_expectation is None
        seen.append(dict(env))
        return _passed_record(case, harness)

    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", fake_execute_model_case)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        case_id="qa-evidence-readiness",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    assert code == 0
    assert seen
    assert all(env == seen[0] for env in seen)
    assert not eval_runtime_root(evidence).exists()


def test_rotated_token_promoted_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _, source_token = _write_auth_sources(tmp_path, monkeypatch, token=_sim_token())
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    rotated = _sim_token(
        access="access-token-rotated",
        refresh="refresh-token-rotated",
        verifier="code-verifier-rotated",
    )

    def mutate_then_pass(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = process_manager
        _ = (grants, roots, expected_router_source_sha256)
        save_token_cache(Path(env["SAXO_MCP_TOKEN_CACHE_PATH"]), rotated)
        return _passed_record(case, harness)

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", mutate_then_pass)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    assert code == 0
    promoted = source_token.read_text(encoding="utf-8")
    assert "access-token-rotated" in promoted
    assert "refresh-token-rotated" in promoted
    assert not eval_runtime_root(evidence).exists()


def test_rotated_claude_credentials_promoted_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch, token=_sim_token())
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    source = claude_src / ".claude" / ".credentials.json"
    original = _claude_credential_document("original", expires_at=4_102_444_800_000)
    rotated = _claude_credential_document("rotated", expires_at=4_102_444_900_000)
    source.write_text(json.dumps(original), encoding="utf-8")
    source.chmod(0o600)

    def mutate_then_pass(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = process_manager
        _ = (grants, roots, expected_router_source_sha256)
        contained = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        contained.write_text(json.dumps(rotated), encoding="utf-8")
        contained.chmod(0o600)
        return _passed_record(case, harness)

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", mutate_then_pass)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )

    code = run_eval_suite(options)

    assert code == 0
    assert json.loads(source.read_text(encoding="utf-8")) == rotated
    assert source.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert not eval_runtime_root(evidence).exists()


def test_claude_credential_promotion_refuses_concurrent_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch, token=_sim_token())
    codex_src, claude_src = _seed_cli_sources(tmp_path)
    source = claude_src / ".claude" / ".credentials.json"
    original = _claude_credential_document("original", expires_at=4_102_444_800_000)
    rotated = _claude_credential_document("rotated", expires_at=4_102_444_900_000)
    concurrent: dict[str, object] = {
        "claudeAiOauth": original["claudeAiOauth"],
        "mcpOAuth": {"new": {}},
    }
    source.write_text(json.dumps(original), encoding="utf-8")
    source.chmod(0o600)

    def mutate_then_pass(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = process_manager
        _ = (grants, roots, expected_router_source_sha256)
        contained = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        contained.write_text(json.dumps(rotated), encoding="utf-8")
        contained.chmod(0o600)
        source.write_text(json.dumps(concurrent), encoding="utf-8")
        source.chmod(0o600)
        return _passed_record(case, harness)

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", mutate_then_pass)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )

    code = run_eval_suite(options)

    payload = json.loads(options.out.read_text(encoding="utf-8"))
    assert code == 1
    assert payload["cleanup"]["runtime_error"] == "claude_auth_promote_source_changed"
    assert json.loads(source.read_text(encoding="utf-8")) == concurrent
    assert not eval_runtime_root(evidence).exists()


def test_cleanup_runs_on_child_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)

    def fail_case(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = process_manager
        _ = (grants, roots, env, expected_router_source_sha256)
        return _passed_record(case, harness, error="TimeoutExpired")

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", fail_case)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    assert code != 0
    assert payload["cleanup"]["credential_mode"] == "ephemeral-owner-only-copy"
    assert payload["cleanup"]["runtime_cleanup"] == "passed"
    assert not eval_runtime_root(evidence).exists()


def test_cleanup_residue_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)

    def pass_case(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = (grants, roots, env, expected_router_source_sha256, process_manager)
        return _passed_record(case, harness)

    def raise_residue(_run_root: Path) -> None:
        raise MatrixEnvError("matrix_runtime_cleanup_residue")

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", pass_case)
    monkeypatch.setattr(eval_runner, "require_matrix_runtime_cleanup", raise_residue)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        harness="codex",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    rendered = json.dumps(payload)
    assert code != 0
    assert payload["cleanup"]["runtime_error"] == "matrix_runtime_cleanup_residue"
    assert payload["cleanup"]["runtime_cleanup"] == "residue"
    assert payload["cleanup"]["complete"] is False
    assert payload["cleanup"]["credential_mode"] == "ephemeral-owner-only-copy"
    assert payload["case_count"] >= 1
    assert "token-cache" not in rendered
    assert "refresh-token" not in rendered


def test_non_router_execute_model_case_uses_provided_env_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "qa-evidence-readiness")
    isolated = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "TMPDIR": str(tmp_path / "tmp"),
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
        "MARKER": "isolated-env-marker",
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "codex").mkdir()
    (tmp_path / "tmp").mkdir()
    captured: dict[str, Any] = {}
    manager = EvalProcessManager()
    required = case.required_logical_tools
    stream_lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": f"call-{index}",
                    "type": "mcp_tool_call",
                    "server": "saxo_bank_mcp",
                    "tool": tool,
                },
            },
        )
        for index, tool in enumerate(required)
    ]
    stream_lines.append(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "answer",
                    "type": "agent_message",
                    "text": (
                        "matched natural prompts 60 logical tools cleanup "
                        "LIVE no-purchase proof no wildcard exact tool grants"
                    ),
                },
            },
        ),
    )
    grants = tuple(f"mcp__saxo_bank_mcp__{tool}" for tool in required)

    def fake_run(
        self: EvalProcessManager,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        _ = (self, command, cwd, timeout_seconds)
        captured["env"] = env
        return ManagedProcessResult(
            stdout="\n".join(stream_lines),
            stderr="",
            returncode=0,
            timed_out=False,
            created_processes=1,
            terminated_processes=1,
            remaining_processes=0,
            process_cleanup="passed",
        )

    monkeypatch.setattr(EvalProcessManager, "run", fake_run)
    record = execute_model_case(
        case,
        "codex",
        grants,
        roots=HarnessRoots(
            codex_plugin_root=ROOT,
            claude_plugin_root=ROOT,
            codex_home=tmp_path / "codex",
            claude_home=tmp_path / "home",
        ),
        env=isolated,
        process_manager=manager,
    )
    assert record.status == "passed", record.error
    # Launch env is a hardened copy of the provided isolated env (PATH/CLI pins),
    # not a reference to os.environ and not a mutated parent secret bag.
    assert captured["env"] is not None
    assert captured["env"]["MARKER"] == "isolated-env-marker"
    assert captured["env"]["HOME"] == isolated["HOME"]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"].get("SAXO_MCP_ENVIRONMENT") == isolated["SAXO_MCP_ENVIRONMENT"]


def test_router_client_versions_require_env_argument() -> None:
    # Fail closed: no os.environ.copy based signature.
    sig = inspect.signature(router_execution.client_versions)
    assert list(sig.parameters) == ["env", "process_manager"]
    sig2 = inspect.signature(router_execution.execute_router_model_case)
    assert "env" in sig2.parameters
    assert "process_manager" in sig2.parameters


def test_install_report_omitted_source_flags_use_actual_auth_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Formal --install-report path: source-home flags omitted; retained homes are plugin-only."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    actual_codex, actual_claude = _seed_cli_auth_sources(tmp_path)
    retained_codex, retained_claude, plugin_root = _seed_retained_plugin_homes(tmp_path)
    # Simulate omitted source flags by clearing env and pointing defaults at actual roots.
    # CLAUDE_CONFIG_DIR must be cleared or an operator shell value would resolve the seed
    # to the real local credential file instead of the isolated test root.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(actual_claude))
    monkeypatch.setenv("CODEX_HOME", str(actual_codex))
    # Retained install homes intentionally do NOT contain real auth credentials.
    assert not (retained_codex / "auth.json").exists()

    runtime = prepare_eval_isolated_runtime(
        evidence,
        source_codex_home=None,
        source_claude_home=None,
        retained_codex_home=retained_codex,
        retained_claude_home=retained_claude,
        retained_codex_plugin_root=plugin_root,
    )
    try:
        assert resolve_actual_codex_auth_home(None) == actual_codex
        assert resolve_actual_claude_auth_home(None) == actual_claude
        assert (runtime.codex_home / "auth.json").read_text(encoding="utf-8") == (
            actual_codex / "auth.json"
        ).read_text(encoding="utf-8")
        assert (runtime.home / ".claude" / ".credentials.json").read_text(encoding="utf-8") == (
            actual_claude / ".claude" / ".credentials.json"
        ).read_text(encoding="utf-8")
        assert not (runtime.home / ".claude" / "settings.json").exists()
        assert not (runtime.home / ".claude.json").exists()
        assert (runtime.codex_home / "config.toml").read_text(encoding="utf-8") == (
            retained_codex / "config.toml"
        ).read_text(encoding="utf-8")
        seeded = runtime.codex_home / "plugins" / "cache" / "sigvardt" / "saxo-bank-mcp" / "0.1.0"
        assert (seeded / "skills" / "saxo-bank" / "SKILL.md").read_bytes() == (
            plugin_root / "skills" / "saxo-bank" / "SKILL.md"
        ).read_bytes()
        assert (seeded / "skills" / "saxo-bank" / "SKILL.md").stat().st_mode & 0o777 == (
            OWNER_FILE_MODE
        )
        # Retained source remains untouched.
        assert (plugin_root / "skills" / "saxo-bank" / "SKILL.md").is_file()
        assert not (seeded / "history.jsonl").exists()
        _assert_runtime_excludes_claude_global_canaries(runtime.run_root)
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)


def test_eval_auth_seed_excludes_global_settings_hooks_mcp_project_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canaries in global Claude settings/hooks/MCP/project/history never enter runtime."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_auth_sources(tmp_path)
    # Source roots deliberately contain non-auth global state.
    assert (claude_src / ".claude" / "settings.json").is_file()
    assert (claude_src / ".claude.json").is_file()
    assert (claude_src / ".claude" / "hooks" / "canary.sh").is_file()
    assert CLAUDE_SETTINGS_CANARY in (claude_src / ".claude" / "settings.json").read_text(
        encoding="utf-8",
    )
    assert CLAUDE_MCP_CANARY in (claude_src / ".claude" / "settings.json").read_text(
        encoding="utf-8",
    )
    assert CLAUDE_PROJECT_HISTORY_CANARY in (claude_src / ".claude.json").read_text(
        encoding="utf-8",
    )

    runtime = prepare_eval_isolated_runtime(
        evidence,
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    try:
        # Only the file-auth artifact is copied.
        assert (runtime.home / ".claude" / ".credentials.json").read_text(encoding="utf-8") == (
            claude_src / ".claude" / ".credentials.json"
        ).read_text(encoding="utf-8")
        _assert_runtime_excludes_claude_global_canaries(runtime.run_root)
        # Canaries also stay out of any evidence-shaped serialization of the runtime env.
        rendered = json.dumps(
            {
                "env": runtime.env,
                "paths": [str(runtime.home), str(runtime.run_root)],
            },
        )
        for canary in _claude_global_canaries():
            assert canary not in rendered
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)
        assert not runtime.run_root.exists()


def test_process_manager_preserves_successful_zero_returncode(tmp_path: Path) -> None:
    """Regression: successful subprocess returncode=0 must not become 124."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    manager = EvalProcessManager()
    result = manager.run(
        ("/bin/sh", "-c", "printf 'ok\\n'; exit 0"),
        cwd=tmp_path,
        env=env,
        timeout_seconds=5,
    )
    snapshot = manager.finalize()
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.remaining_processes == 0
    assert result.process_cleanup == "passed"
    assert snapshot["remaining_processes"] == 0
    assert snapshot["timed_out"] is False


def test_non_router_success_path_with_real_zero_returncode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-router path can pass when the real managed process exits 0 with structured events."""
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "qa-evidence-readiness")
    required = case.required_logical_tools
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": f"call-{index}",
                    "type": "mcp_tool_call",
                    "server": "saxo_bank_mcp",
                    "tool": tool,
                },
            },
        )
        for index, tool in enumerate(required)
    ]
    lines.append(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "answer",
                    "type": "agent_message",
                    "text": (
                        "matched natural prompts 60 logical tools cleanup "
                        "LIVE no-purchase proof no wildcard exact tool grants"
                    ),
                },
            },
        ),
    )
    out_file = tmp_path / "non-router-success.jsonl"
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "TMPDIR": str(tmp_path / "tmp"),
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "codex").mkdir()
    (tmp_path / "tmp").mkdir()
    grants = tuple(f"mcp__saxo_bank_mcp__{tool}" for tool in required)

    def fake_model_command(
        _case: SkillEvalCase,
        _harness: str,
        _grants: tuple[str, ...],
        _roots: HarnessRoots,
        *,
        env: dict[str, str],
    ) -> tuple[str, ...]:
        _ = env
        return ("/bin/cat", str(out_file))

    monkeypatch.setattr(eval_execution, "_model_command", fake_model_command)
    record = execute_model_case(
        case,
        "codex",
        grants,
        roots=HarnessRoots(
            codex_plugin_root=ROOT,
            claude_plugin_root=ROOT,
            codex_home=tmp_path / "codex",
            claude_home=tmp_path / "home",
        ),
        env=env,
        process_manager=EvalProcessManager(),
    )
    assert record.status == "passed", record.error
    assert record.error == ""
    assert record.invoked_logical_tools == required
    assert record.transcript_assertions_passed is True


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_router_success_path_with_real_zero_returncode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """Router path can pass when the real managed process exits 0 with structured output."""
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-qa")
    assert case.router_expectation is not None
    decision = RouterDecision(
        environment=case.router_expectation.environment,
        intent=case.router_expectation.intent,
        mutation_risk=case.router_expectation.mutation_risk,
        evidence_need=case.router_expectation.evidence_need,
        primary_skill=case.router_expectation.primary_skill,
        follow_on_skills=case.router_expectation.follow_on_skills,
        requires_environment_clarification=(
            case.router_expectation.requires_environment_clarification
        ),
        approval_bypass_refused=case.router_expectation.approval_bypass_refused,
        trade_choice_refused=case.router_expectation.trade_choice_refused,
        execution_allowed=False,
    )
    stream = (
        "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "fixture"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "answer",
                            "type": "agent_message",
                            "text": decision.model_dump_json(),
                        },
                    },
                ),
            ),
        )
        if harness == "codex"
        else json.dumps(
            {
                "type": "result",
                "structured_output": decision.model_dump(mode="json"),
            },
        )
    )
    stream_path = tmp_path / "router-success.jsonl"
    stream_path.write_text(stream + "\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "TMPDIR": str(tmp_path / "tmp"),
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "codex").mkdir()
    (tmp_path / "tmp").mkdir()

    def fake_router_command(_spec: object, **_kwargs: object) -> tuple[str, ...]:
        return ("/bin/cat", str(stream_path))

    monkeypatch.setattr(router_execution, "_router_command", fake_router_command)
    record = execute_router_model_case(
        case,
        cast("Any", harness),
        (),
        RouterCaseContext(
            plugin_root=ROOT,
            homes=RouterHomes(
                codex_home=tmp_path / "codex",
                claude_home=tmp_path / "home",
            ),
            expected_router_source_sha256=None,
        ),
        env=env,
        process_manager=EvalProcessManager(),
    )
    assert record.status == "passed"
    assert record.error == ""
    assert record.model_tool_event_count == 0
    assert record.model_mcp_event_count == 0
    assert record.model_saxo_event_count == 0


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize(
    ("result", "expected_error"),
    [
        (
            ManagedProcessResult(
                stdout="valid-router-output",
                stderr="",
                returncode=124,
                timed_out=True,
                created_processes=1,
                terminated_processes=1,
                remaining_processes=0,
                process_cleanup="passed",
            ),
            "timeout_expired",
        ),
        (
            ManagedProcessResult(
                stdout="valid-router-output",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=1,
                remaining_processes=0,
                process_cleanup="unknown",
            ),
            "process_cleanup_unknown",
        ),
        (
            ManagedProcessResult(
                stdout="valid-router-output",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=1,
                remaining_processes=None,
                process_cleanup="passed",
            ),
            "process_cleanup_unknown",
        ),
        (
            ManagedProcessResult(
                stdout="valid-router-output",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=0,
                remaining_processes=1,
                process_cleanup="residue",
            ),
            "process_cleanup_residue",
        ),
        (
            ManagedProcessResult(
                stdout="private-malformed-router-output",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=1,
                remaining_processes=0,
                process_cleanup="passed",
            ),
            "structured_output_invalid",
        ),
    ],
)
def test_router_post_launch_unobservable_results_never_publish_empty_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    result: ManagedProcessResult,
    expected_error: str,
) -> None:
    """Router timeout, cleanup, and parse failures retain unknown parse-derived facts."""
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-qa")
    assert case.router_expectation is not None
    decision = RouterDecision(
        environment=case.router_expectation.environment,
        intent=case.router_expectation.intent,
        mutation_risk=case.router_expectation.mutation_risk,
        evidence_need=case.router_expectation.evidence_need,
        primary_skill=case.router_expectation.primary_skill,
        follow_on_skills=case.router_expectation.follow_on_skills,
        requires_environment_clarification=(
            case.router_expectation.requires_environment_clarification
        ),
        approval_bypass_refused=case.router_expectation.approval_bypass_refused,
        trade_choice_refused=case.router_expectation.trade_choice_refused,
        execution_allowed=False,
    )
    valid_stdout = (
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "answer",
                    "type": "agent_message",
                    "text": decision.model_dump_json(),
                },
            },
        )
        if harness == "codex"
        else json.dumps(
            {
                "type": "result",
                "structured_output": decision.model_dump(mode="json"),
            },
        )
    )
    routed_result = replace(
        result,
        stdout=(
            result.stdout if result.stdout == "private-malformed-router-output" else valid_stdout
        ),
    )

    def fake_run(
        _self: EvalProcessManager,
        _command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        del cwd, env, timeout_seconds
        return routed_result

    def fake_router_command(_spec: object, **_kwargs: object) -> tuple[str, ...]:
        return ("/bin/true",)

    def fake_client_version(
        _harness: object,
        _env: object,
        *,
        process_manager: object,
    ) -> str:
        del process_manager
        return "fixture-client"

    monkeypatch.setattr(EvalProcessManager, "run", fake_run)
    monkeypatch.setattr(router_execution, "_router_command", fake_router_command)
    monkeypatch.setattr(router_execution, "_client_version", fake_client_version)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()

    record = execute_router_model_case(
        case,
        cast("Any", harness),
        (),
        RouterCaseContext(
            plugin_root=ROOT,
            homes=RouterHomes(),
            expected_router_source_sha256=None,
        ),
        env=env,
        process_manager=EvalProcessManager(),
    )

    assert record.status == "failed"
    assert record.error == expected_error
    assert record.model_output_observability == "unknown"
    assert record.transcript_assertions_passed is None
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_tool_event_count is None
    assert record.model_command_event_count is None
    assert record.model_mcp_event_count is None
    assert record.model_saxo_event_count is None
    assert record.invoked_logical_tools is None
    assert record.invoked_logical_tool_count is None
    assert record.grant_status == "unknown"
    assert record.assertion_status == "unknown"
    assert record.router_decision is None
    assert "private-malformed-router-output" not in record.model_dump_json()


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize(
    ("version_failure", "expected_error"),
    [
        ("timeout", "client_version_timeout"),
        ("cleanup_unknown", "client_version_cleanup_unknown"),
        ("cleanup_residue", "client_version_cleanup_residue"),
        ("permission_error", "permission_error"),
    ],
)
def test_router_version_probe_lifecycle_failure_is_unobservable(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    version_failure: str,
    expected_error: str,
) -> None:
    """A real post-model version child cannot be discarded as a harmless string."""
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-auth")
    assert case.router_expectation is not None
    decision = RouterDecision(
        **case.router_expectation.model_dump(mode="python"),
        execution_allowed=False,
    )
    stream = (
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "answer",
                    "type": "agent_message",
                    "text": decision.model_dump_json(),
                },
            },
        )
        if harness == "codex"
        else json.dumps(
            {
                "type": "result",
                "structured_output": decision.model_dump(mode="json"),
            },
        )
    )
    stream_path = tmp_path / "router.jsonl"
    stream_path.write_text(stream + "\n", encoding="utf-8")
    version_cli = tmp_path / "version-cli"
    if version_failure == "timeout":
        version_cli.write_text("#!/bin/sh\nsleep 0.25\nprintf 'fixture 1.0\\n'\n", encoding="utf-8")
    else:
        version_cli.write_text("#!/bin/sh\nprintf 'fixture 1.0\\n'\n", encoding="utf-8")
    version_cli.chmod(0o700)
    denied_executable = tmp_path / "denied-executable"
    denied_executable.mkdir()

    def fake_router_command(_spec: object, **_kwargs: object) -> tuple[str, ...]:
        return ("/bin/cat", str(stream_path))

    def resolve_version_cli(_name: str, _env: dict[str, str]) -> str:
        return str(denied_executable if version_failure == "permission_error" else version_cli)

    monkeypatch.setattr(router_execution, "_router_command", fake_router_command)
    monkeypatch.setattr(eval_commands, "resolve_cli_executable", resolve_version_cli)
    monkeypatch.setattr(
        router_execution,
        "CLIENT_VERSION_TIMEOUT_SECONDS",
        0.05 if version_failure == "timeout" else 1.0,
        raising=False,
    )
    original_cleanup = eval_process.cleanup_birth_bound_processes
    cleanup_call_count = 0

    def lifecycle_cleanup(*args: object, **kwargs: object) -> ProcessCleanupTerminalSnapshot:
        nonlocal cleanup_call_count
        cleanup_call_count += 1
        snapshot = original_cleanup(*args, **kwargs)  # type: ignore[arg-type]
        if cleanup_call_count != VERSION_PROBE_CLEANUP_CALL_INDEX:
            return snapshot
        if version_failure == "cleanup_unknown":
            return replace(
                snapshot,
                coverage_status="unknown",
                coverage_stage="target_observation",
                coverage_subreason="observation_unknown",
            )
        if version_failure == "cleanup_residue":
            assert snapshot.targets
            first = snapshot.targets[0]
            running = first.model_copy(
                update={
                    "terminal_state": "running",
                    "termination_outcome": "still_running",
                    "terminal_birth_identity_sha256": first.birth_identity_sha256,
                },
            )
            return replace(snapshot, targets=(running, *snapshot.targets[1:]))
        return snapshot

    monkeypatch.setattr(eval_process, "cleanup_birth_bound_processes", lifecycle_cleanup)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(runtime),
        "TMPDIR": str(runtime),
    }
    manager = EvalProcessManager()

    record = execute_router_model_case(
        case,
        cast("Any", harness),
        (),
        RouterCaseContext(
            plugin_root=ROOT,
            homes=RouterHomes(),
            expected_router_source_sha256=None,
        ),
        env=env,
        process_manager=manager,
    )

    assert record.status == "failed"
    assert record.error == expected_error
    assert record.model_output_observability == "unknown"
    assert record.router_decision is None
    assert record.transcript_assertions_passed is None
    assert record.no_mcp_call is None
    assert record.no_saxo_call is None
    assert record.model_tool_event_count is None
    assert record.model_command_event_count is None
    assert record.model_mcp_event_count is None
    assert record.model_saxo_event_count is None
    assert record.invoked_logical_tools is None
    assert record.invoked_logical_tool_count is None
    assert record.grant_status == "unknown"
    assert record.assertion_status == "unknown"
    assert manager.created_processes >= 1
    if version_failure == "timeout":
        assert manager.timed_out is True

    cleanup = manager.finalize()
    cleanup["process_timed_out"] = cleanup.pop("timed_out")
    cleanup.update(
        {
            # Deliberately optimistic. Report assembly must honor the lifecycle fields.
            "complete": True,
            "runtime_cleanup": "passed",
            "token_promote": "passed",
        },
    )

    def failed_outcome(_options: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            records=(record,),
            enforced_mode="ephemeral-owner-only-copy",
            versions={},
            cleanup=cleanup,
            error=record.error,
        )

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "_select_execution_outcome", failed_outcome)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        harness=harness,
        expected_source_commit="abc123",
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code != 0
    assert payload["status"] == "failed"
    assert payload["records"][0]["model_output_observability"] == "unknown"
    assert payload["records"][0]["no_mcp_call"] is None
    assert payload["records"][0]["no_saxo_call"] is None
    assert payload["cleanup"]["model_tool_events"] is None
    assert payload["cleanup"]["model_command_events"] is None
    assert payload["cleanup"]["created_mcp_calls"] is None
    assert payload["cleanup"]["model_saxo_events"] is None
    assert payload["cleanup"]["invoked_logical_tool_count"] is None
    if version_failure in {"timeout", "cleanup_unknown", "cleanup_residue"}:
        assert payload["cleanup"]["complete"] is False


def test_eval_report_rejects_sticky_version_timeout_after_zero_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleaned-up version timeout still makes the full evaluation report fail."""
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, _claude_src = _seed_cli_sources(tmp_path)
    _install_binding(monkeypatch)
    case = next(item for item in load_eval_cases(CASE_ROOT) if item.id == "router-auth")

    def timed_out_version(
        *,
        env: dict[str, str],
        process_manager: EvalProcessManager | None = None,
    ) -> str:
        assert process_manager is not None
        result = process_manager.run(
            ("/bin/sh", "-c", "sleep 0.25"),
            cwd=Path(env["TMPDIR"]),
            env=env,
            timeout_seconds=0.05,
        )
        assert result.timed_out is True
        assert result.remaining_processes == 0
        assert result.process_cleanup == "passed"
        return "unknown"

    def passed_case(*_args: object, **_kwargs: object) -> EvalRunRecord:
        return _passed_record(case, "codex")

    monkeypatch.setattr(eval_runner, "codex_client_version", timed_out_version)
    monkeypatch.setattr(eval_runner, "execute_model_case", passed_case)
    options = replace(
        _options(
            tmp_path,
            dry_run=False,
            credential_mode="ephemeral-owner-only-copy",
            harness="codex",
            source_codex_home=codex_src,
            expected_source_commit="abc123",
        ),
        harness_policy="codex_native_v1",
    )
    options.out.parent.mkdir(parents=True, exist_ok=True)

    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))

    assert code != 0
    assert payload["status"] == "failed"
    assert payload["cleanup"]["complete"] is False
    assert payload["cleanup"]["process_timed_out"] is True
    assert payload["cleanup"]["process_cleanup"] == "passed"
    assert payload["cleanup"]["remaining_processes"] == 0
    assert payload["cleanup"]["runtime_error"] == "process_timeout"


def test_process_manager_kills_fake_parent_and_descendant_group(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    ids_path = tmp_path / "spawned-ids.txt"
    # Record the real parent shell PID/PGID, then spawn a long-lived group sibling.
    script = (
        f'printf "%s %s\\n" "$$" "$(ps -o pgid= -p $$ | tr -d \'[:space:]\')" > "{ids_path}"; '
        "sleep 60 & wait"
    )
    manager = EvalProcessManager()
    result = manager.run(
        ("/bin/sh", "-c", script),
        cwd=tmp_path,
        env=env,
        timeout_seconds=1,
    )
    snapshot = manager.finalize()
    assert ids_path.is_file()
    pid_text, pgid_text = ids_path.read_text(encoding="utf-8").strip().split()
    spawned_pid = int(pid_text)
    spawned_pgid = int(pgid_text)
    assert spawned_pid > 0
    assert spawned_pgid > 0
    assert result.timed_out is True
    assert result.created_processes >= 1
    assert result.remaining_processes == 0
    assert result.process_cleanup == "passed"
    assert manager.remaining_processes == 0
    assert manager.created_processes >= 1
    assert manager.terminated_processes >= 1
    assert result.returncode == TIMEOUT_EXIT_CODE
    assert snapshot["remaining_processes"] == 0
    # Specific captured process/group must be gone — empty-tuple checks prove nothing.
    assert remaining_live_pgids((spawned_pgid,)) == ()
    assert remaining_live_pids((spawned_pid,)) == ()
    assert process_group_members(spawned_pgid) == ()
    assert process_still_running(spawned_pid) is False


def test_eval_failure_preserves_records_when_cleanup_and_promote_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    codex_src, claude_src = _seed_cli_sources(tmp_path)

    def fail_case(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = (grants, roots, env, expected_router_source_sha256, process_manager)
        return _passed_record(case, harness, error="model_case_failed")

    def fail_promote(_runtime: object) -> None:
        raise MatrixEnvError("token_promote_source_changed")

    def fail_cleanup(_run_root: Path) -> None:
        raise MatrixEnvError("matrix_runtime_cleanup_residue")

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", fail_case)
    monkeypatch.setattr(eval_runner, "promote_rotated_sim_token_cache", fail_promote)
    monkeypatch.setattr(eval_runner, "require_matrix_runtime_cleanup", fail_cleanup)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        harness="codex",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    rendered = json.dumps(payload)
    assert code != 0
    # Primary eval error is preserved; cleanup/promotion are separate fields.
    assert payload["cleanup"]["runtime_error"] == "model_case_failed"
    assert payload["cleanup"]["token_promote"] == "failed"  # noqa: S105
    assert payload["cleanup"]["runtime_cleanup"] == "residue"
    assert payload["cleanup"]["complete"] is False
    assert payload["case_count"] >= 1
    assert any(record["status"] == "failed" for record in payload["records"])
    assert "token-cache" not in rendered
    assert "refresh-token" not in rendered
    assert str(tmp_path) not in rendered


def test_source_token_race_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "out"
    evidence.mkdir()
    original = _sim_token()
    _, source_token = _write_auth_sources(tmp_path, monkeypatch, token=original)
    codex_src, claude_src = _seed_cli_sources(tmp_path)

    def mutate_source_and_contained(  # noqa: PLR0913
        case: SkillEvalCase,
        harness: str,
        grants: tuple[str, ...],
        *,
        roots: HarnessRoots,
        env: dict[str, str],
        expected_router_source_sha256: str | None = None,
        process_manager: object | None = None,
    ) -> EvalRunRecord:
        _ = (grants, roots, expected_router_source_sha256, process_manager)
        save_token_cache(
            Path(env["SAXO_MCP_TOKEN_CACHE_PATH"]),
            _sim_token(
                access="rotated-access",
                refresh="rotated-refresh-secret",
                verifier="rotated-verifier-secret",
            ),
        )
        save_token_cache(
            source_token,
            _sim_token(
                access="concurrent-access",
                refresh="concurrent-refresh-secret",
                verifier="concurrent-verifier-secret",
            ),
        )
        return _passed_record(case, harness)

    _install_binding(monkeypatch)
    monkeypatch.setattr(eval_runner, "client_versions", _stub_versions)
    monkeypatch.setattr(eval_runner, "execute_model_case", mutate_source_and_contained)
    options = _options(
        tmp_path,
        dry_run=False,
        credential_mode="ephemeral-owner-only-copy",
        expected_source_commit="abc123",
        harness="codex",
        source_codex_home=codex_src,
        source_claude_home=claude_src,
    )
    code = run_eval_suite(options)
    payload = json.loads(options.out.read_text(encoding="utf-8"))
    rendered = json.dumps(payload)
    assert code != 0
    assert payload["cleanup"]["runtime_error"] == "token_promote_source_changed"
    assert payload["cleanup"]["token_promote"] == "failed"  # noqa: S105
    assert "rotated-refresh-secret" not in rendered
    assert "concurrent-refresh-secret" not in rendered
    assert str(source_token) not in rendered
