from __future__ import annotations

import ast
import inspect
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, cast

import pytest

from saxo_bank_mcp import agent_skill_install_cli_driver as install_driver
from saxo_bank_mcp import agent_skill_router_eval_execution as router_execution
from saxo_bank_mcp.agent_skill_command_runner import CommandResult
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager, ManagedProcessResult
from saxo_bank_mcp.agent_skill_install_env import build_isolated_env
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt

EXPECTED_CODEX_INSTALL_COMMANDS = 7
EXPECTED_CLAUDE_INSTALL_COMMANDS = 8
OWNER_ONLY_EXECUTABLE_MODE = 0o700


class _ClientVersionCallable(Protocol):
    def __call__(
        self,
        client: Literal["codex", "claude"],
        env: dict[str, str],
        *,
        process_manager: EvalProcessManager,
    ) -> str: ...


def _result(name: str, argv: tuple[str, ...], cwd: Path) -> CommandResult:
    return CommandResult(
        receipt=CommandReceipt(
            name=name,
            argv=argv,
            cwd=str(cwd),
            pid=1,
            pgid=1,
            exit_code=0,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
        ),
        stdout="",
        stderr="",
    )


def _assert_codex_file_only(argv: tuple[str, ...]) -> None:
    assert Path(argv[0]).name == "codex"
    assert 'cli_auth_credentials_store="file"' in argv
    assert 'mcp_oauth_credentials_store="file"' in argv


def _assert_claude_non_ui(argv: tuple[str, ...]) -> None:
    assert Path(argv[0]).name.startswith("claude")
    assert "--no-chrome" in argv
    assert "--bare" in argv


def test_all_install_client_commands_are_file_only_and_non_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_run(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 180,
    ) -> CommandResult:
        _ = (env, timeout_seconds)
        captured.append(argv)
        return _result(name, argv, cwd)

    monkeypatch.setattr(install_driver, "run_command", fake_run)
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    install_driver.discover_cli_help(tmp_path, env, env)
    install_driver.run_codex_install(tmp_path, env)
    install_driver.run_claude_install(tmp_path, env)
    reinstall_codex = cast(
        "Callable[[Path, dict[str, str], str], tuple[CommandResult, ...]]",
        install_driver.__dict__["_reinstall_codex"],
    )
    update_claude = cast(
        "Callable[[Path, dict[str, str], str], tuple[CommandResult, ...]]",
        install_driver.__dict__["_update_claude"],
    )
    reinstall_codex(tmp_path, env, "audit")
    update_claude(tmp_path, env, "audit")

    codex = tuple(argv for argv in captured if Path(argv[0]).name == "codex")
    claude = tuple(argv for argv in captured if Path(argv[0]).name.startswith("claude"))
    assert len(codex) == EXPECTED_CODEX_INSTALL_COMMANDS
    assert len(claude) == EXPECTED_CLAUDE_INSTALL_COMMANDS
    for argv in codex:
        _assert_codex_file_only(argv)
    for argv in claude:
        _assert_claude_non_ui(argv)

    tree = ast.parse(inspect.getsource(install_driver))
    raw_client_tuples = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Tuple)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value in {"codex", "claude"}
    )
    assert raw_client_tuples == ()


def test_version_probes_use_non_ui_file_only_commands(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    class Recorder:
        def run(
            self,
            command: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: float,
        ) -> ManagedProcessResult:
            _ = (cwd, env, timeout_seconds)
            commands.append(command)
            return ManagedProcessResult(
                stdout="client 1.0\n",
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=0,
                remaining_processes=0,
                process_cleanup="passed",
            )

    env = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    recorder = Recorder()
    manager = cast("EvalProcessManager", recorder)
    client_version = cast(
        "_ClientVersionCallable",
        router_execution.__dict__["_client_version"],
    )
    assert (
        client_version(
            "codex",
            env,
            process_manager=manager,
        )
        == "client 1.0"
    )
    assert (
        client_version(
            "claude",
            env,
            process_manager=manager,
        )
        == "client 1.0"
    )
    _assert_codex_file_only(commands[0])
    _assert_claude_non_ui(commands[1])


def test_isolated_environment_blocks_real_keychain_command(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    probe = tmp_path / "probe"
    for path in (home, codex_home, probe):
        path.mkdir()

    env = build_isolated_env(
        home=home,
        codex_home=codex_home,
        run_root=tmp_path,
        probe_env=probe,
    )

    guard = Path(env["PATH"].split(":", maxsplit=1)[0]) / "security"
    assert guard.is_file()
    assert not guard.is_symlink()
    assert stat.S_ISREG(guard.lstat().st_mode)
    assert guard.stat().st_mode & 0o777 == OWNER_ONLY_EXECUTABLE_MODE
    assert guard.read_text(encoding="utf-8") == "#!/bin/sh\nexit 77\n"
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(home / ".claude")


def test_direct_claude_plugin_validation_is_bare_and_non_ui() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "claude --bare --no-chrome plugin validate --strict ." in workflow
