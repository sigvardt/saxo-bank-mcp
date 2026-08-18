from __future__ import annotations

import inspect
import signal
from pathlib import Path
from types import ModuleType

import pytest

import saxo_bank_mcp.agent_skill_codex_install as codex_install
import saxo_bank_mcp.agent_skill_command_runner as command_runner
import saxo_bank_mcp.agent_skill_eval_process as eval_process
import saxo_bank_mcp.agent_skill_install_producer as install_producer


def test_nested_eval_uses_birth_bound_cleanup_not_raw_pid_or_group_signals() -> None:
    source = inspect.getsource(eval_process)

    assert "cleanup_birth_bound_processes" in source
    assert "terminate_process_group" not in source
    assert "os.kill(" not in source
    assert "os.killpg(" not in source


def test_nested_eval_does_not_signal_pid_reused_after_scope_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 8844
    identity = command_runner.ProcessCleanupIdentity(
        pid=pid,
        pgid=pid,
        birth_identity="nested-original",
        initial_state="running",
    )
    scope = command_runner.ProcessCleanupScope(
        identities=(identity,),
        tracked_pids=(pid,),
        tracked_pgids=(),
    )
    replacement = command_runner.ProcessObservation(
        pid=pid,
        pgid=pid,
        birth_identity="nested-replacement",
        state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        returncode = 3

        def __init__(self) -> None:
            self.pid = pid

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            _ = timeout
            return "", ""

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    def fake_getpgid(_pid: int) -> int:
        return pid

    def fake_scope(_root_pid: int, _root_pgid: int) -> command_runner.ProcessCleanupScope:
        return scope

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    def record_signal(target: int, sig: signal.Signals) -> None:
        signals.append((target, sig))

    monkeypatch.setattr(eval_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(eval_process.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(eval_process, "capture_process_cleanup_scope", fake_scope)
    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        replacement_observation,
    )
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )

    result = eval_process.EvalProcessManager().run(
        ("/bin/false",),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        timeout_seconds=1,
    )

    assert signals == []
    assert result.process_cleanup == "passed"
    assert result.remaining_processes == 0


@pytest.mark.parametrize("module", [codex_install, install_producer])
def test_exact_install_paths_never_cleanup_historical_numeric_groups(module: ModuleType) -> None:
    source = inspect.getsource(module)

    assert "cleanup_recorded_groups" not in source
    assert "remaining_live_pgids" not in source
    assert "remaining_live_pids" not in source


def test_exact_install_failed_cleanup_does_not_signal_reused_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, ...]] = []
    run_root = tmp_path / "run"
    marketplace = tmp_path / "marketplace"
    run_root.mkdir()
    marketplace.mkdir()

    def record_historical_group(pgids: tuple[int, ...]) -> None:
        signals.append(pgids)

    def disposable_cleanup(*_args: object, **_kwargs: object) -> None:
        return

    monkeypatch.setattr(
        codex_install,
        "cleanup_recorded_groups",
        record_historical_group,
        raising=False,
    )
    monkeypatch.setattr(codex_install, "require_disposable_cleanup", disposable_cleanup)

    codex_install._cleanup_failed_run(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        run_root,
        marketplace,
        8844,
    )

    assert signals == []
