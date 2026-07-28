from __future__ import annotations

import inspect
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

import saxo_bank_mcp.agent_skill_eval_runner as eval_runner
import saxo_bank_mcp.agent_skill_router_eval_execution as router_execution
from saxo_bank_mcp.agent_skill_command_runner import remaining_live_pgids
from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots, execute_model_case
from saxo_bank_mcp.agent_skill_eval_models import EvalRunRecord, SkillEvalCase, load_eval_cases
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
from saxo_bank_mcp.agent_skill_router_eval_execution import RouterSourceBinding
from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.token_cache import save_token_cache

TIMEOUT_EXIT_CODE: Final = 124

ROOT: Final = Path(__file__).resolve().parents[1]
CASE_ROOT: Final = ROOT / "evals/saxo-bank"
DIGEST: Final = "d" * 64
BOTH_HARNESS_COUNT: Final = 2


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
    (claude_cfg / "settings.json").write_text("{}\n", encoding="utf-8")
    (claude_cfg / "settings.json").chmod(0o600)
    (claude_cfg / ".credentials.json").write_text(
        '{"claude":"auth-fixture"}\n',
        encoding="utf-8",
    )
    (claude_cfg / ".credentials.json").chmod(0o600)
    # Transcripts must never be copied into disposable homes.
    (codex / "history.jsonl").write_text("raw-transcript\n", encoding="utf-8")
    (claude_cfg / "history.jsonl").write_text("raw-transcript\n", encoding="utf-8")
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
        assert not (runtime.codex_home / "history.jsonl").exists()
        assert not (runtime.home / ".claude" / "history.jsonl").exists()
        assert str(codex_src) not in json.dumps({"env": runtime.env})
        assert str(claude_src) not in json.dumps({"env": runtime.env})
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)
        assert not runtime.run_root.exists()


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
    assert payload["cleanup"]["credential_mode"] == "none"


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
            stdout=(
                "matched natural prompts 39 logical tools cleanup "
                "LIVE no-purchase proof no wildcard exact tool grants"
            ),
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
        (),
        roots=HarnessRoots(
            codex_plugin_root=ROOT,
            claude_plugin_root=ROOT,
            codex_home=tmp_path / "codex",
            claude_home=tmp_path / "home",
        ),
        env=isolated,
        process_manager=manager,
    )
    assert record.status == "passed"
    assert captured["env"] is isolated
    assert captured["env"]["MARKER"] == "isolated-env-marker"
    assert "OPENAI_API_KEY" not in captured["env"]


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
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)


def test_process_manager_kills_fake_parent_and_descendant_group(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    # Real fake parent that spawns a descendant in the same process group.
    manager = EvalProcessManager()
    result = manager.run(
        ("/bin/sh", "-c", "sleep 30 & wait"),
        cwd=tmp_path,
        env=env,
        timeout_seconds=1,
    )
    snapshot = manager.finalize()
    assert result.timed_out is True
    assert result.created_processes >= 1
    assert result.remaining_processes == 0
    assert result.process_cleanup == "passed"
    assert manager.remaining_processes == 0
    assert manager.created_processes >= 1
    assert manager.terminated_processes >= 1
    assert result.returncode == TIMEOUT_EXIT_CODE
    assert snapshot["remaining_processes"] == 0
    # Entire group is gone after finalize.
    assert remaining_live_pgids(()) == ()


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
