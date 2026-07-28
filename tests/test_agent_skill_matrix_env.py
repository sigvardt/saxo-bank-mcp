from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

import saxo_bank_mcp.agent_skill_matrix_producer as matrix_producer
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, CommandResult
from saxo_bank_mcp.agent_skill_install_env import write_bearing_env_keys
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_matrix import MatrixPlanOptions, SimFixtureOptions
from saxo_bank_mcp.agent_skill_matrix_env import (
    OWNER_DIR_MODE,
    OWNER_FILE_MODE,
    MatrixEnvError,
    cleanup_matrix_isolated_runtime,
    matrix_runtime_root,
    prepare_matrix_child_receipt_path,
    prepare_matrix_isolated_runtime,
    require_matrix_runtime_cleanup,
    resolve_matrix_child_evidence_path,
)


def _write_auth_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source_root = tmp_path / "auth-source"
    source_root.mkdir()
    credential = source_root / "sim-credentials.txt"
    token = source_root / "token-cache.json"
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
    token.write_text("{}", encoding="utf-8")
    credential.chmod(0o600)
    token.chmod(0o600)
    monkeypatch.setenv("SAXO_MCP_SIM_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(token))
    monkeypatch.delenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", raising=False)
    monkeypatch.delenv("SAXO_MCP_LIVE_APP_KEY", raising=False)
    return credential, token


def test_prepare_matrix_runtime_is_contained_owner_only_and_sim_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "task-15-sim" / "manual"
    evidence.mkdir(parents=True)
    _write_auth_sources(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "evil-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "evil-codex"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "evil-tmp"))
    monkeypatch.setenv("SAXO_MCP_LIVE_CREDENTIAL_FILE", str(tmp_path / "live-creds"))
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(tmp_path / "live-token"))
    (tmp_path / "live-creds").write_text("live", encoding="utf-8")
    (tmp_path / "live-token").write_text("{}", encoding="utf-8")

    runtime = prepare_matrix_isolated_runtime(evidence)
    root = runtime.run_root.resolve()
    try:
        assert root == matrix_runtime_root(evidence).resolve()
        assert root.is_relative_to(evidence.resolve())
        assert (root.stat().st_mode & 0o777) == OWNER_DIR_MODE
        for key in write_bearing_env_keys():
            value = runtime.env.get(key)
            if not value:
                continue
            assert Path(value).resolve().is_relative_to(root), key
        assert runtime.env["HOME"] == str(runtime.home.resolve())
        assert runtime.env["CODEX_HOME"] == str(runtime.codex_home.resolve())
        assert runtime.env["TMPDIR"] == str((root / "tmp").resolve())
        assert runtime.env["UV_CACHE_DIR"] == str((root / "uv-cache").resolve())
        assert runtime.env["SAXO_MCP_ENVIRONMENT"] == "SIM"
        assert runtime.env["SAXO_MCP_ENABLE_LIVE_READS"] == "0"
        assert runtime.env["SAXO_MCP_ENABLE_LIVE_WRITES"] == ""
        assert "SAXO_MCP_LIVE_CREDENTIAL_FILE" not in runtime.env
        assert "SAXO_MCP_LIVE_TOKEN_CACHE_PATH" not in runtime.env
        assert Path(runtime.env["SAXO_MCP_SIM_CREDENTIAL_FILE"]).resolve().is_relative_to(root)
        assert Path(runtime.env["SAXO_MCP_TOKEN_CACHE_PATH"]).resolve().is_relative_to(root)
        for path in (runtime.sim_credential_path, runtime.token_cache_path):
            mode = path.stat().st_mode
            assert stat.S_ISREG(mode)
            assert (mode & 0o777) == OWNER_FILE_MODE
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)
        assert not runtime.run_root.exists()


def test_prepare_matrix_runtime_fails_closed_when_token_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "manual"
    evidence.mkdir()
    credential = tmp_path / "cred.txt"
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
    monkeypatch.setenv("SAXO_MCP_SIM_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(tmp_path / "missing-token.json"))
    with pytest.raises(MatrixEnvError, match="token_cache_missing"):
        prepare_matrix_isolated_runtime(evidence)
    assert not matrix_runtime_root(evidence).exists()


def testrun_sim_matrix_probe_passes_isolated_env_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "manual"
    evidence.mkdir()
    cache = tmp_path / "installed-cache"
    cache.mkdir()
    receipt_dir = evidence / "probe-receipts"
    receipt_dir.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 180,  # noqa: ARG001
    ) -> CommandResult:
        captured["name"] = name
        captured["env"] = env
        captured["cwd"] = cwd
        assert env is not None
        root = matrix_runtime_root(evidence).resolve()
        assert Path(env["HOME"]).resolve().is_relative_to(root)
        assert Path(env["CODEX_HOME"]).resolve().is_relative_to(root)
        assert Path(env["TMPDIR"]).resolve().is_relative_to(root)
        assert Path(env["UV_CACHE_DIR"]).resolve().is_relative_to(root)
        assert Path(env["SAXO_MCP_SIM_CREDENTIAL_FILE"]).resolve().is_relative_to(root)
        assert Path(env["SAXO_MCP_TOKEN_CACHE_PATH"]).resolve().is_relative_to(root)
        assert env.get("SAXO_MCP_LIVE_CREDENTIAL_FILE") in (None, "")
        runtime_exists = matrix_runtime_root(evidence).exists()
        captured["runtime_exists_during_command"] = runtime_exists
        receipt = CommandReceipt(
            name=name,
            argv=tuple(argv),
            cwd=str(cwd),
            pid=1,
            pgid=1,
            exit_code=0,
            stdout_sha256="e" * 64,
            stderr_sha256="e" * 64,
            timed_out=False,
            cleanup_attempted=True,
        )
        return CommandResult(receipt=receipt, stdout="", stderr="")

    monkeypatch.setattr(matrix_producer, "run_command", fake_run_command)
    options = MatrixPlanOptions(
        manifest=Path("data/saxo/agent_tool_scenarios.json"),
        environment="SIM",
        require_tools=39,
        install_report=tmp_path / "install.json",
        fixtures=SimFixtureOptions(
            stock_uic="211",
            amount="1",
            limit_price="50",
            modified_limit_price="51",
            option_uics="30004846,30004926",
            stream_uic="21",
        ),
        out=evidence / "tool-matrix.json",
    )
    result = matrix_producer.run_sim_matrix_probe(cache, receipt_dir, options)
    assert result.receipt.exit_code == 0
    assert captured["env"] is not None
    assert captured["runtime_exists_during_command"] is True
    assert not matrix_runtime_root(evidence).exists()


def testrun_sim_matrix_probe_cleans_up_after_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "manual"
    evidence.mkdir()
    cache = tmp_path / "installed-cache"
    cache.mkdir()
    receipt_dir = evidence / "probe-receipts"
    receipt_dir.mkdir()
    _write_auth_sources(tmp_path, monkeypatch)

    def failing_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 180,  # noqa: ARG001
    ) -> CommandResult:
        assert env is not None
        assert matrix_runtime_root(evidence).exists()
        receipt = CommandReceipt(
            name=name,
            argv=tuple(argv),
            cwd=str(cwd),
            pid=2,
            pgid=2,
            exit_code=1,
            stdout_sha256="e" * 64,
            stderr_sha256="e" * 64,
            timed_out=False,
            cleanup_attempted=True,
        )
        raise CommandFailureError(receipt)

    monkeypatch.setattr(matrix_producer, "run_command", failing_run_command)
    options = MatrixPlanOptions(
        manifest=Path("data/saxo/agent_tool_scenarios.json"),
        environment="SIM",
        require_tools=39,
        install_report=tmp_path / "install.json",
        fixtures=SimFixtureOptions(
            stock_uic="211",
            amount="1",
            limit_price="50",
            modified_limit_price="51",
            option_uics="30004846,30004926",
            stream_uic="21",
        ),
        out=evidence / "tool-matrix.json",
    )
    with pytest.raises(CommandFailureError):
        matrix_producer.run_sim_matrix_probe(cache, receipt_dir, options)
    assert not matrix_runtime_root(evidence).exists()


def test_prepare_matrix_child_receipt_path_removes_regular_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "task-15-sim" / "manual"
    receipt_dir = evidence / "probe-receipts"
    receipt_dir.mkdir(parents=True)
    regular = receipt_dir / "sim-tool-matrix.json"
    regular.write_text('{"status":"blocked","reason":"stale"}', encoding="utf-8")
    prepare_matrix_child_receipt_path(regular)
    assert not regular.exists()

    target = receipt_dir / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = receipt_dir / "linked-receipt.json"
    link.symlink_to(target)
    with pytest.raises(MatrixEnvError, match="child_out_symlink"):
        prepare_matrix_child_receipt_path(link)
    assert link.is_symlink()
    assert target.is_file()

    directory = receipt_dir / "not-a-file"
    directory.mkdir()
    with pytest.raises(MatrixEnvError, match="child_out_not_regular"):
        prepare_matrix_child_receipt_path(directory)


def testrun_sim_matrix_probe_removes_stale_receipt_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "manual"
    evidence.mkdir()
    cache = tmp_path / "installed-cache"
    cache.mkdir()
    receipt_dir = evidence / "probe-receipts"
    receipt_dir.mkdir()
    stale = receipt_dir / "sim-tool-matrix.json"
    stale.write_text(
        '{"status":"blocked","reason":"disclaimer_context_unavailable"}',
        encoding="utf-8",
    )
    _write_auth_sources(tmp_path, monkeypatch)
    seen: dict[str, bool] = {}

    def failing_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 180,  # noqa: ARG001
    ) -> CommandResult:
        _ = env
        out_path = Path(argv[argv.index("--out") + 1])
        seen["stale_gone_at_spawn"] = not out_path.exists()
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=tuple(argv),
                cwd=str(cwd),
                pid=4,
                pgid=4,
                exit_code=1,
                stdout_sha256="e" * 64,
                stderr_sha256="e" * 64,
                timed_out=False,
                cleanup_attempted=True,
            )
        )

    monkeypatch.setattr(matrix_producer, "run_command", failing_run_command)
    options = MatrixPlanOptions(
        manifest=Path("data/saxo/agent_tool_scenarios.json"),
        environment="SIM",
        require_tools=39,
        install_report=tmp_path / "install.json",
        fixtures=SimFixtureOptions(
            stock_uic="211",
            amount="1",
            limit_price="50",
            modified_limit_price="51",
            option_uics="30004846,30004926",
            stream_uic="21",
        ),
        out=evidence / "tool-matrix.json",
    )
    with pytest.raises(CommandFailureError):
        matrix_producer.run_sim_matrix_probe(cache, receipt_dir, options)
    assert seen["stale_gone_at_spawn"] is True
    assert not stale.exists()
    assert not matrix_runtime_root(evidence).exists()


def testrun_sim_matrix_probe_rejects_symlink_receipt_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "manual"
    evidence.mkdir()
    cache = tmp_path / "installed-cache"
    cache.mkdir()
    receipt_dir = evidence / "probe-receipts"
    receipt_dir.mkdir()
    target = evidence / "secret-elsewhere.json"
    target.write_text('{"status":"blocked"}', encoding="utf-8")
    link = receipt_dir / "sim-tool-matrix.json"
    link.symlink_to(target)
    _write_auth_sources(tmp_path, monkeypatch)
    called = {"run_command": False}

    def must_not_run(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 180,  # noqa: ARG001
    ) -> CommandResult:
        _ = name, argv, cwd, env
        called["run_command"] = True
        raise AssertionError("spawn must not run for symlink child out")

    monkeypatch.setattr(matrix_producer, "run_command", must_not_run)
    options = MatrixPlanOptions(
        manifest=Path("data/saxo/agent_tool_scenarios.json"),
        environment="SIM",
        require_tools=39,
        install_report=tmp_path / "install.json",
        fixtures=SimFixtureOptions(
            stock_uic="211",
            amount="1",
            limit_price="50",
            modified_limit_price="51",
            option_uics="30004846,30004926",
            stream_uic="21",
        ),
        out=evidence / "tool-matrix.json",
    )
    with pytest.raises(MatrixEnvError, match="child_out_symlink"):
        matrix_producer.run_sim_matrix_probe(cache, receipt_dir, options)
    assert called["run_command"] is False
    assert link.is_symlink()
    assert target.is_file()
    assert not matrix_runtime_root(evidence).exists()


def test_cleanup_matrix_runtime_is_idempotent(tmp_path: Path) -> None:
    root = matrix_runtime_root(tmp_path)
    root.mkdir(parents=True)
    assert cleanup_matrix_isolated_runtime(root) == []
    assert cleanup_matrix_isolated_runtime(root) == []


def _cache_tree_snapshot(cache: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(cache.rglob("*")):
        key = str(path.relative_to(cache))
        snapshot[key] = path.read_bytes() if path.is_file() else None
    return snapshot


def testrun_sim_matrix_probe_relative_out_is_absolute_under_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative receipt_dir/--out must not write into the installed-cache cwd."""
    evidence = tmp_path / "task-15-sim" / "manual"
    evidence.mkdir(parents=True)
    cache = tmp_path / "installed-cache"
    cache.mkdir()
    (cache / "fixture-marker.txt").write_text("retained-cache\n", encoding="utf-8")
    cache_before = _cache_tree_snapshot(cache)
    _write_auth_sources(tmp_path, monkeypatch)
    monkeypatch.chdir(evidence)
    captured: dict[str, Any] = {}

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 180,  # noqa: ARG001
    ) -> CommandResult:
        _ = env
        out_index = argv.index("--out") + 1
        out_arg = Path(argv[out_index])
        captured["out"] = out_arg
        captured["cwd"] = cwd
        assert out_arg.is_absolute()
        assert out_arg.resolve().is_relative_to(evidence.resolve())
        assert not out_arg.resolve().is_relative_to(cache.resolve())
        assert cwd == cache
        receipt = CommandReceipt(
            name=name,
            argv=tuple(argv),
            cwd=str(cwd),
            pid=3,
            pgid=3,
            exit_code=0,
            stdout_sha256="e" * 64,
            stderr_sha256="e" * 64,
            timed_out=False,
            cleanup_attempted=True,
        )
        return CommandResult(receipt=receipt, stdout="", stderr="")

    monkeypatch.setattr(matrix_producer, "run_command", fake_run_command)
    options = MatrixPlanOptions(
        manifest=Path("data/saxo/agent_tool_scenarios.json"),
        environment="SIM",
        require_tools=39,
        install_report=Path("install.json"),
        fixtures=SimFixtureOptions(
            stock_uic="211",
            amount="1",
            limit_price="50",
            modified_limit_price="51",
            option_uics="30004846,30004926",
            stream_uic="21",
        ),
        out=Path("tool-matrix.json"),
    )
    receipt_dir = Path("probe-receipts")
    result = matrix_producer.run_sim_matrix_probe(cache, receipt_dir, options)
    expected_out = (evidence / "probe-receipts" / "sim-tool-matrix.json").resolve()
    assert result.receipt.exit_code == 0
    assert captured["out"] == expected_out
    assert captured["out"].is_absolute()
    assert _cache_tree_snapshot(cache) == cache_before
    assert not any(path.name == "sim-tool-matrix.json" for path in cache.rglob("*"))
    assert expected_out.parent.is_dir()
    assert (expected_out.parent.stat().st_mode & 0o777) == OWNER_DIR_MODE
    assert not matrix_runtime_root(evidence).exists()


def test_resolve_matrix_child_evidence_path_rejects_escape_and_cache_alias(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "task-15-sim" / "manual"
    evidence.mkdir(parents=True)
    # Cache nested under evidence so the alias check (not only outside) can fire.
    cache = evidence / "installed-cache"
    cache.mkdir()
    outside_cache = tmp_path / "outside-installed-cache"
    outside_cache.mkdir()
    runtime = matrix_runtime_root(evidence)
    runtime.mkdir(parents=True)

    with pytest.raises(MatrixEnvError, match="child_out_outside_evidence_root"):
        resolve_matrix_child_evidence_path(
            tmp_path / "escape" / "sim-tool-matrix.json",
            evidence_root=evidence,
            installed_cache=outside_cache,
            runtime_root=runtime,
        )
    with pytest.raises(MatrixEnvError, match="child_out_aliases_installed_cache"):
        resolve_matrix_child_evidence_path(
            cache / "sim-tool-matrix.json",
            evidence_root=evidence,
            installed_cache=cache,
            runtime_root=runtime,
        )
    with pytest.raises(MatrixEnvError, match="child_out_aliases_matrix_runtime"):
        resolve_matrix_child_evidence_path(
            runtime / "sim-tool-matrix.json",
            evidence_root=evidence,
            installed_cache=cache,
            runtime_root=runtime,
        )
    with pytest.raises(MatrixEnvError, match="child_out_outside_evidence_root"):
        resolve_matrix_child_evidence_path(
            evidence / "probe-receipts" / ".." / ".." / "escape.json",
            evidence_root=evidence,
            installed_cache=cache,
            runtime_root=runtime,
        )

    contained = resolve_matrix_child_evidence_path(
        evidence / "probe-receipts" / "sim-tool-matrix.json",
        evidence_root=evidence,
        installed_cache=cache,
        runtime_root=runtime,
    )
    assert contained.is_absolute()
    assert contained == (evidence / "probe-receipts" / "sim-tool-matrix.json").resolve()
    assert contained.parent.is_dir()
    assert (contained.parent.stat().st_mode & 0o777) == OWNER_DIR_MODE
