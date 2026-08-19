from __future__ import annotations

import signal
from pathlib import Path

import pytest

import saxo_bank_mcp.agent_skill_command_runner as command_runner
import saxo_bank_mcp.agent_skill_eval_process as eval_process

ROOT_PID = 73_100
CHILD_PID = ROOT_PID + 1
MOVED_PGID = ROOT_PID + 2
MINIMUM_ROOT_CHECKS = 3
REQUIRED_SCOPE_SNAPSHOTS = 2


@pytest.mark.parametrize(
    ("transition", "expected_created", "expected_cleanup", "child_signaled"),
    [
        ("stable", 2, "passed", True),
        ("moved", 1, "residue", False),
        ("replaced", 1, "residue", False),
        ("missing", 1, "passed", False),
        ("unknown", 1, "residue", False),
    ],
)
def test_nested_eval_admission_is_bracketed_by_live_root_and_exact_second_scope(  # noqa: C901, PLR0913, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    expected_created: int,
    expected_cleanup: str,
    child_signaled: bool,  # noqa: FBT001
) -> None:
    """A nested-eval child is signalable only after two exact root-bound observations."""
    root_running = True
    child_running = True
    child_birth = "child-original"
    child_pgid = ROOT_PID
    child_unknown = False
    snapshot_calls = 0
    signals: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        pid = ROOT_PID
        returncode: int | None = None
        poll_calls = 0

        def poll(self) -> int | None:
            self.poll_calls += 1
            return self.returncode

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            nonlocal root_running, child_running, child_birth, child_pgid, child_unknown
            _ = timeout
            if snapshot_calls < 2:  # noqa: PLR2004
                if transition == "moved":
                    child_pgid = MOVED_PGID
                elif transition == "replaced":
                    child_birth = "child-replacement"
                    child_pgid = CHILD_PID
                elif transition == "missing":
                    child_running = False
                elif transition == "unknown":
                    child_unknown = True
            root_running = False
            self.returncode = 0
            return "", ""

    process = FakeProcess()

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    def fake_snapshot(
        root_pid: int | None,
        root_pgid: int | None,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        nonlocal snapshot_calls, child_running, child_birth, child_pgid, child_unknown
        assert root_pid == ROOT_PID
        assert root_pgid == ROOT_PID
        snapshot_calls += 1
        if snapshot_calls == 2:  # noqa: PLR2004
            if transition == "moved":
                child_pgid = MOVED_PGID
            elif transition == "replaced":
                child_birth = "child-replacement"
                child_pgid = CHILD_PID
            elif transition == "missing":
                child_running = False
            elif transition == "unknown":
                child_unknown = True
        visible = (ROOT_PID,) + ((CHILD_PID,) if child_running else ())
        # Keeping both groups visible catches independent PID/PGID membership checks.
        return visible, (ROOT_PID, MOVED_PGID)

    def fake_observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == ROOT_PID:
            if not root_running:
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=ROOT_PID,
                birth_identity="root-original",
                state="running",
            )
        if pid != CHILD_PID or not child_running:
            return None
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=child_pgid,
            birth_identity=child_birth,
            state="unknown" if child_unknown else "running",
        )

    def group_members(pgid: int) -> tuple[int, ...]:
        members: list[int] = []
        if root_running and pgid == ROOT_PID:
            members.append(ROOT_PID)
        if child_running and child_pgid == pgid:
            members.append(CHILD_PID)
        return tuple(members)

    def group_members_with_coverage(pgid: int) -> tuple[tuple[int, ...], bool]:
        return group_members(pgid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        nonlocal child_running
        signals.append((pid, sig))
        if pid == CHILD_PID:
            child_running = False

    def fake_getpgid(_pid: int) -> int:
        return ROOT_PID

    def skip_wait(_seconds: float) -> None:
        return

    monkeypatch.setattr(eval_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(eval_process.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(command_runner, "_snapshot_tree", fake_snapshot)
    monkeypatch.setattr(command_runner, "read_process_observation", fake_observation)
    monkeypatch.setattr(command_runner, "process_group_members", group_members)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members_with_coverage,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", skip_wait)

    result = eval_process.EvalProcessManager().run(
        ("/bin/true",),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        timeout_seconds=1,
    )

    assert process.poll_calls >= MINIMUM_ROOT_CHECKS
    assert snapshot_calls >= REQUIRED_SCOPE_SNAPSHOTS
    assert result.created_processes == expected_created
    assert result.process_cleanup == expected_cleanup
    assert any(pid == CHILD_PID for pid, _sig in signals) is child_signaled
