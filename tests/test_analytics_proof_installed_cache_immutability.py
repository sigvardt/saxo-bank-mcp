from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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


def test_installed_proof_suite_routes_pytest_and_hypothesis_state_outside_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    cache_root = tmp_path / "installed-cache"
    launcher = cache_root / "scripts/run-pytest"
    test_file = cache_root / "tests/test_analytics_sample.py"
    project_environment = tmp_path / "project-environment"
    launcher.parent.mkdir(parents=True)
    test_file.parent.mkdir()
    project_environment.mkdir(mode=0o700)
    launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    launcher.chmod(0o700)
    test_file.write_text("def test_sample(): pass\n", encoding="utf-8")
    monkeypatch.chdir(cache_root)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    def native_project_environment(_root: Path) -> Path:
        return project_environment

    monkeypatch.setattr(
        producer,
        "_native_proof_project_environment",
        native_project_environment,
    )
    observed: dict[str, object] = {}

    def mutate_unrouted_state_then_fail(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> None:
        del timeout_seconds
        observed.update(name=name, argv=argv, cwd=cwd, env=env)
        if not ({"-p", "no:cacheprovider"} <= set(argv)):
            (cwd / ".pytest_cache").mkdir()
        hypothesis_root = env.get("HYPOTHESIS_STORAGE_DIRECTORY")
        if hypothesis_root is None:
            (cwd / ".hypothesis").mkdir()
        else:
            Path(hypothesis_root).mkdir(parents=True, exist_ok=True)
        raise CommandFailureError(
            CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=23,
                pgid=23,
                exit_code=1,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "run_command", mutate_unrouted_state_then_fail)

    with pytest.raises(producer.ProofProducerError, match="installed_proof_suite_failed"):
        producer._run_installed_offline_proof_suite(  # noqa: SLF001
            harness_policy="codex_native_v1",
        )

    command = cast("tuple[str, ...]", observed["argv"])
    environment = cast("dict[str, str]", observed["env"])
    hypothesis_root = Path(environment["HYPOTHESIS_STORAGE_DIRECTORY"])
    assert {"-p", "no:cacheprovider"} <= set(command)
    assert environment["UV_NO_SYNC"] == "1"
    assert environment["UV_NO_BUILD_ISOLATION"] == "1"
    assert hypothesis_root != cache_root
    assert not hypothesis_root.is_relative_to(cache_root)
    assert not (cache_root / ".pytest_cache").exists()
    assert not (cache_root / ".hypothesis").exists()


def test_offline_wheel_build_uses_bound_backend_without_shared_cache(
    tmp_path: Path,
) -> None:
    uv_path = shutil.which("uv")
    assert uv_path is not None
    isolated_cache = tmp_path / "uv-cache"
    project_environment = Path(sys.prefix).resolve()
    result = subprocess.run(
        (
            uv_path,
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(tmp_path / "wheel"),
        ),
        cwd=ROOT,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(isolated_cache),
            "UV_NO_BUILD_ISOLATION": "1",
            "UV_NO_SYNC": "1",
            "UV_OFFLINE": "1",
            "UV_PROJECT_ENVIRONMENT": str(project_environment),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert tuple((tmp_path / "wheel").glob("saxo_bank_mcp-*.whl"))


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
