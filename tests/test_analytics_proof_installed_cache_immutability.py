from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import cast

import pytest

import saxo_bank_mcp.agent_skill_eval_commands as eval_commands
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, CommandReceipt
from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots, execute_model_case
from saxo_bank_mcp.agent_skill_eval_models import load_eval_cases
from saxo_bank_mcp.agent_skill_eval_process import ManagedProcessResult
from saxo_bank_mcp.agent_skill_eval_runner import resolve_tool_grants

ROOT = Path(__file__).resolve().parents[1]


def test_installed_eval_runner_never_uses_the_cache_as_writable_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    cache_root = tmp_path / "installed-cache"
    codex_home = tmp_path / "codex-home"
    source_repo = tmp_path / "source-repo"
    for path in (cache_root, codex_home, source_repo):
        path.mkdir(mode=0o700)
    monkeypatch.chdir(cache_root)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("SAXO_ANALYTICS_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("SAXO_ANALYTICS_SOURCE_REPO", str(source_repo))
    observed: dict[str, object] = {}

    def mutate_cwd_then_fail(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> None:
        del env, timeout_seconds
        observed.update(name=name, argv=argv, cwd=cwd)
        (cwd / ".pytest_cache").mkdir()
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=17,
                pgid=17,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "run_command", mutate_cwd_then_fail)

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_agent_evaluation_command_failed",
    ):
        producer._run_installed_agent_evaluation(  # noqa: SLF001
            candidate_commit="a" * 40,
            installed_cache_sha256="b" * 64,
            harness_policy="codex_native_v1",
        )

    command = cast("tuple[str, ...]", observed["argv"])
    assert observed["cwd"] != cache_root
    assert command[0] == sys.executable
    assert Path(command[command.index("--case-root") + 1]).is_absolute()
    assert not (cache_root / ".pytest_cache").exists()


def test_model_case_uses_private_work_root_not_installed_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "installed-plugin"
    codex_home = tmp_path / "codex-home"
    temp_root = tmp_path / "run-temp"
    for path in (plugin_root, codex_home, temp_root):
        path.mkdir(mode=0o700)
    case = next(
        item for item in load_eval_cases(ROOT / "evals/saxo-analytics") if item.id == "cost-xray"
    )
    observed: dict[str, object] = {}

    class MutatingManager:
        created_processes = 0
        remaining_processes = 0
        process_cleanup = "complete"

        def run(
            self,
            command: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: float,
        ) -> ManagedProcessResult:
            del env, timeout_seconds
            observed.update(command=command, cwd=cwd)
            (cwd / ".pytest_cache").mkdir()
            return ManagedProcessResult(
                stdout="{",
                stderr="",
                returncode=1,
                timed_out=False,
                created_processes=1,
                terminated_processes=1,
                remaining_processes=0,
                process_cleanup="complete",
            )

    def fake_executable(command: str, _env: Mapping[str, str]) -> str:
        return f"/usr/bin/{command}"

    monkeypatch.setattr(eval_commands, "resolve_cli_executable", fake_executable)
    execute_model_case(
        case,
        "codex",
        resolve_tool_grants("codex", case.exact_tool_grants["codex"]),
        roots=HarnessRoots(
            codex_plugin_root=plugin_root,
            claude_plugin_root=plugin_root,
            codex_home=codex_home,
            claude_home=None,
        ),
        env={
            "HOME": str(codex_home),
            "TMPDIR": str(temp_root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        process_manager=MutatingManager(),  # type: ignore[arg-type]
    )

    command = cast("tuple[str, ...]", observed["command"])
    work_root = cast("Path", observed["cwd"])
    assert work_root != plugin_root
    assert Path(command[command.index("-C") + 1]) == work_root
    assert not (plugin_root / ".pytest_cache").exists()
