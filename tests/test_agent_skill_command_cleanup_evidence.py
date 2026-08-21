from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import saxo_bank_mcp.agent_skill_command_runner as command_runner

MIN_ESCAPED_TARGET_COUNT = 2
MIN_GROUP_SCANS = 2
OWNER_FILE_MODE = 0o600
REUSED_PID = 4242
REUSED_PGID = 4343
LEADER_REUSE_AFTER_READS = 2
ROOT_BINDING_POLL_COUNT = 2
ROOT_ADMISSION_POLL_COUNT = 5
POST_AND_FINAL_SNAPSHOT_COUNT = 2
EXPECTED_DEDUPED_OBSERVATION_COUNT = 2
DUPLICATE_OCCURRENCE_COUNT = 2
LARGE_PIPE_PAYLOAD_SIZE = 2 * 1024 * 1024


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PRIVATE_SENTINEL": "DO_NOT_PUBLISH",
    }


def test_run_command_drains_large_stdout_and_stderr_while_child_is_running(
    tmp_path: Path,
) -> None:
    """A child larger than both pipe buffers must exit before the watchdog."""
    child_code = (
        "import sys\n"
        f"sys.stdout.write('o' * {LARGE_PIPE_PAYLOAD_SIZE})\n"
        "sys.stdout.flush()\n"
        f"sys.stderr.write('e' * {LARGE_PIPE_PAYLOAD_SIZE})\n"
        "sys.stderr.flush()\n"
    )

    result = command_runner.run_command(
        "large_pipe_payload",
        (sys.executable, "-c", child_code),
        cwd=tmp_path,
        env=_env(tmp_path),
        timeout_seconds=2,
    )

    assert result.receipt.exit_code == 0
    assert result.receipt.timed_out is False
    assert result.stdout == "o" * LARGE_PIPE_PAYLOAD_SIZE
    assert result.stderr == "e" * LARGE_PIPE_PAYLOAD_SIZE
    assert result.receipt.stdout_sha256 == hashlib.sha256(result.stdout.encode()).hexdigest()
    assert result.receipt.stderr_sha256 == hashlib.sha256(result.stderr.encode()).hexdigest()


def test_run_command_stuck_output_drain_preserves_timeout_and_unknown_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that cannot drain must not publish zero residue or erase timeout truth."""

    class StuckOutputDrain:
        join_count = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            self.join_count += 1

        def is_alive(self) -> bool:
            return self.join_count == 1

    monkeypatch.setattr(command_runner, "_OutputDrainThread", StuckOutputDrain)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "stuck_output_drain",
            (sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=0,
        )

    error = caught.value
    assert error.receipt.timed_out is True
    assert error.remaining_process_count is None
    assert error.remaining_process_group_count is None
    assert error.cleanup_identity_evidence_status == "write-failed"
    assert error.cleanup_unknown_reason == "cleanup_receipt_path_missing"


def test_run_command_communicate_error_forces_unknown_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pipe reader cannot authenticate complete cleanup or zero residue."""
    real_popen = command_runner.subprocess.Popen
    popen_factory = cast("Callable[..., subprocess.Popen[str]]", real_popen)

    def popen_with_failed_communicate(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[str]:
        process = popen_factory(*args, **kwargs)

        def fail_communicate(*_args: object, **_kwargs: object) -> tuple[str, str]:
            raise OSError("synthetic communicate failure")

        process.communicate = fail_communicate  # type: ignore[method-assign]
        return process

    monkeypatch.setattr(command_runner.subprocess, "Popen", popen_with_failed_communicate)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "failed_output_drain",
            (sys.executable, "-c", "raise SystemExit(0)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
        )

    error = caught.value
    assert error.receipt.timed_out is False
    assert error.remaining_process_count is None
    assert error.remaining_process_group_count is None
    assert error.cleanup_identity_evidence_status == "write-failed"
    assert error.cleanup_unknown_reason == "cleanup_receipt_path_missing"


def test_escaped_session_cleanup_writes_owner_only_identity_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = (tmp_path / "escaped-cleanup.json").resolve()
    admission_marker = (tmp_path / "escaped-child-admitted").resolve()
    original_capture = command_runner.RootBoundProcessCleanupAdmission.capture_scope

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    def capture_after_child_is_admitted(
        admission: command_runner.RootBoundProcessCleanupAdmission,
    ) -> command_runner.ProcessCleanupScope:
        scope = original_capture(admission)
        if any(identity.pid != admission.process.pid for identity in scope.identities):
            admission_marker.touch(mode=OWNER_FILE_MODE)
        return scope

    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import os,subprocess,sys,time\n"
        "child = subprocess.Popen("
        f"[sys.executable, '-c', {child_code!r}], start_new_session=True)\n"
        "deadline = time.monotonic() + 5\n"
        "while not os.path.exists(os.environ['ADMISSION_MARKER']):\n"
        "    if time.monotonic() >= deadline:\n"
        "        child.kill()\n"
        "        child.wait(timeout=2)\n"
        "        raise SystemExit(99)\n"
        "    time.sleep(0.001)\n"
        "raise SystemExit(3)\n"
    )
    env = _env(tmp_path)
    env["ADMISSION_MARKER"] = str(admission_marker)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(
        command_runner.RootBoundProcessCleanupAdmission,
        "capture_scope",
        capture_after_child_is_admitted,
    )

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "escaped_identity_probe",
            (sys.executable, "-c", parent_code),
            cwd=tmp_path,
            env=env,
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )

    error = caught.value
    assert error.cleanup_identity_receipt_sha256 is not None
    receipt = command_runner.verify_command_cleanup_identity_receipt(
        receipt_path,
        expected_receipt_sha256=error.cleanup_identity_receipt_sha256,
    )
    assert receipt is not None
    assert receipt.root_pid == error.receipt.pid
    assert receipt.root_pgid == error.receipt.pgid
    assert receipt.target_count == len(receipt.targets)
    assert receipt.target_count >= MIN_ESCAPED_TARGET_COUNT
    assert any(target.pgid != receipt.root_pgid for target in receipt.targets)
    assert all(target.birth_identity_sha256 is not None for target in receipt.targets)
    assert all(target.terminal_state in {"absent", "zombie"} for target in receipt.targets)
    assert all(
        target.termination_outcome in {"no_longer_running", "non_executing_zombie"}
        for target in receipt.targets
    )
    assert receipt_path.stat().st_mode & 0o777 == OWNER_FILE_MODE


def test_zombie_target_is_distinguished_from_executing_process() -> None:
    process = subprocess.Popen(
        (sys.executable, "-c", "import os; os._exit(0)"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = process.pid
    try:
        identity = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            identity = command_runner.capture_process_cleanup_identity(pid)
            if identity is not None and identity.initial_state == "zombie":
                break
            time.sleep(0.02)
        assert identity is not None
        assert identity.initial_state == "zombie"

        evidence = command_runner.observe_process_cleanup_target(identity)

        assert evidence.pid == pid
        assert evidence.terminal_state == "zombie"
        assert evidence.termination_outcome == "non_executing_zombie"
    finally:
        process.wait(timeout=2)


def test_pid_reuse_is_bound_to_birth_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    original = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="old-birth",
        initial_state="running",
    )
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=5151,
        birth_identity="new-birth",
        state="running",
    )

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    monkeypatch.setattr(command_runner, "read_process_observation", replacement_observation)

    evidence = command_runner.observe_process_cleanup_target(original)

    assert evidence.pid == REUSED_PID
    assert evidence.terminal_state == "identity_reused"
    assert evidence.termination_outcome == "identity_changed"
    assert evidence.birth_identity_sha256 != evidence.terminal_birth_identity_sha256


def test_birth_bound_cleanup_does_not_signal_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = getattr(command_runner, "cleanup_birth_bound_processes", None)
    assert cleanup is not None
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="original-birth",
        initial_state="running",
    )
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="replacement-birth",
        state="running",
    )
    signals: list[tuple[int, object]] = []

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", replacement_observation)
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = cleanup(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
    )

    assert signals == []
    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
    assert snapshot.remaining_process_group_count == 0
    assert snapshot.targets[0].termination_outcome == "identity_changed"


def test_detection_only_birth_history_excludes_reused_pid_from_cleanup_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numeric PID reused after capture is not evidence of a surviving task process."""
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        initial_state="running",
    )
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="unrelated-replacement-birth",
        state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

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

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
        observed_identities=(historical,),
    )

    assert signals == []
    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
    assert snapshot.remaining_process_group_count == 0
    assert snapshot.offending_observations == ()


def test_detection_only_birth_history_keeps_same_process_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-running process with the captured birth remains genuine unknown residue."""
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        initial_state="running",
    )
    survivor = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def survivor_observation(_pid: int) -> command_runner.ProcessObservation:
        return survivor

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        survivor_observation,
    )
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
        observed_identities=(historical,),
    )

    assert signals == []
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "uncaptured_member"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert len(snapshot.offending_observations) == 1
    assert snapshot.offending_observations[0].expected_birth_identity_sha256 == (
        hashlib.sha256(b"task-birth").hexdigest()
    )


def test_detection_only_birth_history_keeps_unobservable_pid_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable current birth cannot authenticate PID reuse."""
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        initial_state="running",
    )
    unobservable = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="",
        state="unknown",
    )

    def unobservable_observation(_pid: int) -> command_runner.ProcessObservation:
        return unobservable

    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        unobservable_observation,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
        observed_identities=(historical,),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "observation_unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert len(snapshot.offending_observations) == 1
    assert snapshot.offending_observations[0].expected_birth_identity_sha256 == (
        hashlib.sha256(b"task-birth").hexdigest()
    )


def test_numeric_pid_without_birth_history_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live numeric PID with no captured birth cannot be dismissed as reuse."""
    unknown_owner = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="unbound-current-birth",
        state="running",
    )

    def unknown_owner_observation(_pid: int) -> command_runner.ProcessObservation:
        return unknown_owner

    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        unknown_owner_observation,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "uncaptured_member"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert snapshot.offending_observations[0].expected_birth_identity_sha256 is None


def test_unobserved_pid_outside_discovery_group_without_birth_history_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live child can move groups, so PGID drift alone cannot prove PID reuse."""
    discovery_pgid = REUSED_PGID
    replacement_pgid = discovery_pgid + 1
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=replacement_pgid,
        birth_identity="unrelated-replacement-birth",
        state="running",
    )

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        replacement_observation,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(discovery_pgid,),
        tracked_pid_groups=((REUSED_PID, discovery_pgid),),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "uncaptured_member"
    assert snapshot.offending_observations[0].expected_pgid == discovery_pgid


def test_observed_birth_mismatch_proves_cross_group_numeric_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_pgid = REUSED_PGID
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=discovery_pgid + 1,
        birth_identity="unrelated-replacement-birth",
        state="running",
    )
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=discovery_pgid,
        birth_identity="task-child-birth",
        initial_state="running",
    )

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        replacement_observation,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(discovery_pgid,),
        tracked_pid_groups=((REUSED_PID, discovery_pgid),),
        observed_identities=(historical,),
    )

    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
    assert snapshot.remaining_process_group_count == 0
    assert snapshot.offending_observations == ()


def test_observed_same_birth_group_migration_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_pgid = REUSED_PGID
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=discovery_pgid,
        birth_identity="task-child-birth",
        initial_state="running",
    )
    moved_child = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=discovery_pgid + 1,
        birth_identity=historical.birth_identity,
        state="running",
    )

    def moved_observation(_pid: int) -> command_runner.ProcessObservation:
        return moved_child

    monkeypatch.setattr(command_runner, "read_process_observation", moved_observation)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(discovery_pgid,),
        tracked_pid_groups=((REUSED_PID, discovery_pgid),),
        observed_identities=(historical,),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "changed_identity_or_group_member"
    assert snapshot.offending_observations[0].observation_state == "group_changed"


def test_unobserved_pid_still_in_discovery_group_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-group live PID without a birth identity remains genuine uncertainty."""
    current = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="unbound-current-birth",
        state="running",
    )

    def current_observation(_pid: int) -> command_runner.ProcessObservation:
        return current

    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        current_observation,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(REUSED_PGID,),
        tracked_pid_groups=((REUSED_PID, REUSED_PGID),),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "uncaptured_member"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert snapshot.offending_observations[0].expected_pgid == REUSED_PGID


def test_checked_tree_snapshot_retains_each_discovery_time_pid_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_pid = 81_000
    child_pid = 81_001
    root_pgid = root_pid
    child_pgid = child_pid

    monkeypatch.setattr(
        command_runner,
        "_process_identity_table_checked",
        lambda: (
            (
                (
                    1,
                    command_runner.ProcessCleanupIdentity(
                        pid=root_pid,
                        pgid=root_pgid,
                        birth_identity="root-birth",
                        initial_state="running",
                    ),
                ),
                (
                    root_pid,
                    command_runner.ProcessCleanupIdentity(
                        pid=child_pid,
                        pgid=child_pgid,
                        birth_identity="child-birth",
                        initial_state="running",
                    ),
                ),
            ),
            True,
        ),
    )

    pids, pgids, coverage, pid_groups, discovered_identities = (
        command_runner._snapshot_tree_checked(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
            root_pid,
            root_pgid,
        )
    )

    assert pids == (root_pid, child_pid)
    assert pgids == (root_pgid, child_pgid)
    assert coverage == "complete"
    assert pid_groups == (
        (root_pid, root_pgid),
        (child_pid, child_pgid),
    )
    assert tuple(identity.pid for identity in discovered_identities) == (root_pid, child_pid)


def test_process_identity_table_captures_birth_in_the_discovery_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_pid = 81_100

    class Completed:
        returncode = 0
        stdout = f"{root_pid} 1 {root_pid} S Thu Aug 20 09:15:20 2026\n"

    def fake_run(*_args: object, **_kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr(command_runner.subprocess, "run", fake_run)

    rows, observed = command_runner._process_identity_table_checked()  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    assert observed is True
    assert rows == (
        (
            1,
            command_runner.ProcessCleanupIdentity(
                pid=root_pid,
                pgid=root_pid,
                birth_identity="Thu Aug 20 09:15:20 2026",
                initial_state="running",
            ),
        ),
    )


def test_root_bound_scope_retains_group_for_child_that_vanishes_before_birth_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_pid = 82_000
    child_pid = 82_001
    child_pgid = child_pid
    root_identity = command_runner.ProcessCleanupIdentity(
        pid=root_pid,
        pgid=root_pid,
        birth_identity="root-birth",
        initial_state="running",
    )

    class ActiveProcess:
        pid = root_pid

        def poll(self) -> None:
            return None

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == root_pid:
            return command_runner.ProcessObservation(
                pid=root_pid,
                pgid=root_pid,
                birth_identity="root-birth",
                state="running",
            )
        assert pid == child_pid
        return None

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "_process_identity_table_checked",
        lambda: (
            (
                (1, root_identity),
                (
                    root_pid,
                    command_runner.ProcessCleanupIdentity(
                        pid=child_pid,
                        pgid=child_pgid,
                        birth_identity="child-birth",
                        initial_state="running",
                    ),
                ),
            ),
            True,
        ),
    )

    admission = command_runner.RootBoundProcessCleanupAdmission(
        process=ActiveProcess(),  # type: ignore[arg-type]
        root_pgid=root_pid,
        root_identity=root_identity,
    )
    scope = admission.capture_scope()

    assert scope.coverage_status == "complete"
    assert scope.identities == (root_identity,)
    assert scope.tracked_pid_groups == (
        (root_pid, root_pid),
        (child_pid, child_pgid),
    )
    assert any(
        identity.pid == child_pid and identity.birth_identity == "child-birth"
        for identity in scope.observed_identities
    )


def test_closed_root_detection_does_not_create_trusted_pid_group_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_pid = 82_050
    replacement_pid = root_pid + 1
    replacement_pgid = root_pid + 2
    root_identity = command_runner.ProcessCleanupIdentity(
        pid=root_pid,
        pgid=root_pid,
        birth_identity="root-birth",
        initial_state="running",
    )

    class ExitedProcess:
        pid = root_pid

        def poll(self) -> int:
            return 0

    def snapshot(
        observed_root_pid: int | None,
        observed_pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
        tuple[tuple[int, int], ...],
    ]:
        assert observed_root_pid is None
        assert observed_pgid == root_pid
        return (
            (replacement_pid,),
            (root_pid, replacement_pgid),
            "complete",
            ((replacement_pid, replacement_pgid),),
        )

    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", snapshot)

    scope = command_runner.RootBoundProcessCleanupAdmission(
        process=ExitedProcess(),  # type: ignore[arg-type]
        root_pgid=root_pid,
        root_identity=root_identity,
    ).capture_scope()

    assert replacement_pid in scope.tracked_pids
    assert replacement_pgid in scope.tracked_pgids
    assert scope.tracked_pid_groups == ((root_pid, root_pid),)


def test_cleanup_scope_keeps_detection_only_identities_separate_from_signal_targets() -> None:
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        initial_state="running",
    )

    scope = command_runner.ProcessCleanupScope(
        identities=(),
        observed_identities=(historical,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(REUSED_PGID,),
        coverage_status="complete",
    )

    assert scope.identities == ()
    assert scope.observed_identities == (historical,)


def test_cleanup_scope_rejects_observed_identity_outside_numeric_scope() -> None:
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        initial_state="running",
    )

    with pytest.raises(ValueError, match="observed cleanup identity is outside tracked scope"):
        command_runner.ProcessCleanupScope(
            identities=(),
            observed_identities=(historical,),
            tracked_pids=(),
            tracked_pgids=(REUSED_PGID,),
            coverage_status="complete",
        )


def test_cleanup_scope_rejects_observed_group_outside_numeric_scope() -> None:
    historical = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="task-birth",
        initial_state="running",
    )

    with pytest.raises(ValueError, match="observed cleanup identity is outside tracked scope"):
        command_runner.ProcessCleanupScope(
            identities=(),
            observed_identities=(historical,),
            tracked_pids=(REUSED_PID,),
            tracked_pgids=(REUSED_PGID + 1,),
            coverage_status="complete",
        )


def test_birth_bound_cleanup_rechecks_identity_immediately_before_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PID reused after target selection must never receive the historical signal."""
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="original-birth",
        initial_state="running",
    )
    observations = iter(
        (
            command_runner.ProcessObservation(
                pid=REUSED_PID,
                pgid=REUSED_PGID,
                birth_identity="original-birth",
                state="running",
            ),
            command_runner.ProcessObservation(
                pid=REUSED_PID,
                pgid=REUSED_PGID,
                birth_identity="replacement-birth",
                state="running",
            ),
        ),
    )
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="replacement-birth",
        state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def observation(_pid: int) -> command_runner.ProcessObservation:
        return next(observations, replacement)

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
    )

    assert signals == []
    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
    assert snapshot.targets[0].termination_outcome == "identity_changed"


def test_birth_bound_cleanup_terminal_rescan_rejects_late_member_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member appearing after leader exit is not captured and makes coverage unknown."""
    late_pid = REUSED_PID + 1
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="leader-birth",
        initial_state="running",
    )
    leader_running = True
    group_scans = 0

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == REUSED_PID:
            if not leader_running:
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=REUSED_PID,
                birth_identity="leader-birth",
                state="running",
            )
        if pid == late_pid:
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=REUSED_PID,
                birth_identity="late-birth",
                state="running",
            )
        return None

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        nonlocal group_scans
        assert pgid == REUSED_PID
        group_scans += 1
        members = (REUSED_PID,) if group_scans == 1 else (late_pid,)
        return members, True

    def signal_pid(pid: int, _sig: signal.Signals) -> None:
        nonlocal leader_running
        if pid == REUSED_PID:
            leader_running = False

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(command_runner, "process_group_members_with_coverage", group_members)
    monkeypatch.setattr(command_runner, "_signal_pid", signal_pid)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(REUSED_PID,),
    )

    assert group_scans >= MIN_GROUP_SCANS
    assert all(target.pid != late_pid for target in snapshot.targets)
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "uncaptured_member"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_birth_bound_cleanup_reused_group_leader_never_signals_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PGID,
        pgid=REUSED_PGID,
        birth_identity="old-leader",
        initial_state="running",
    )
    unrelated_member = REUSED_PGID + 1
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation:
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=REUSED_PGID,
            birth_identity="new-leader" if pid == REUSED_PGID else "unrelated-member",
            state="running",
        )

    def group_members(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (REUSED_PGID, unrelated_member), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )

    snapshot = command_runner.cleanup_birth_bound_processes(
        (identity,),
        tracked_pids=(REUSED_PGID,),
        tracked_pgids=(REUSED_PGID,),
    )

    assert signals == []
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "changed_identity_or_group_member"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert tuple(target.pid for target in snapshot.targets) == (REUSED_PGID,)
    assert snapshot.targets[0].termination_outcome == "identity_changed"


def test_absent_group_leader_never_discovers_or_signals_uncaptured_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader_pid = REUSED_PGID
    unrelated_pid = REUSED_PID
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-original",
        initial_state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == leader_pid:
            return None
        assert pid == unrelated_pid
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=leader_pid,
            birth_identity="unrelated-current-member",
            state="running",
        )

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        assert pgid == leader_pid
        return (unrelated_pid,), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader,),
        tracked_pids=(leader_pid,),
        tracked_pgids=(leader_pid,),
    )

    assert signals == []
    assert tuple(target.pid for target in snapshot.targets) == (leader_pid,)
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "uncaptured_member"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


@pytest.mark.parametrize("leader_outcome", ["absent", "reused"])
def test_member_seen_after_leader_identity_loss_is_detection_only(
    monkeypatch: pytest.MonkeyPatch,
    leader_outcome: str,
) -> None:
    """A classification-time leader match cannot authorize a later new member."""
    leader_pid = REUSED_PGID
    new_member_pid = leader_pid + 1
    leader_state = "original"
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-original",
        initial_state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == leader_pid:
            if leader_state == "absent":
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=leader_pid,
                birth_identity=(
                    "leader-original" if leader_state == "original" else "leader-reused"
                ),
                state="running",
            )
        assert pid == new_member_pid
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=leader_pid,
            birth_identity="new-member",
            state="running",
        )

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        nonlocal leader_state
        assert pgid == leader_pid
        leader_state = leader_outcome
        return (leader_pid, new_member_pid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader,),
        tracked_pids=(leader_pid,),
        tracked_pgids=(leader_pid,),
    )

    assert all(pid != new_member_pid for pid, _sig in signals)
    assert all(target.pid != new_member_pid for target in snapshot.targets)
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_member_discovered_before_leader_exit_is_not_admitted_for_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leader exit between group observation and signalling cannot widen cleanup scope."""
    leader_pid = REUSED_PGID
    new_member_pid = leader_pid + 1
    leader_running = True
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-original",
        initial_state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        nonlocal leader_running
        if pid == leader_pid:
            if not leader_running:
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=leader_pid,
                birth_identity="leader-original",
                state="running",
            )
        assert pid == new_member_pid
        leader_running = False
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=leader_pid,
            birth_identity="new-member",
            state="running",
        )

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        assert pgid == leader_pid
        return (leader_pid, new_member_pid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader,),
        tracked_pids=(leader_pid,),
        tracked_pgids=(leader_pid,),
    )

    assert signals == []
    assert all(target.pid != new_member_pid for target in snapshot.targets)
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_new_group_member_is_never_signaled_while_captured_leader_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group discovery detects an uncaptured member but never turns it into a target."""
    leader_pid = REUSED_PGID
    new_member_pid = leader_pid + 1
    running = {leader_pid: True, new_member_pid: True}
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-original",
        initial_state="running",
    )
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if not running[pid]:
            return None
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=leader_pid,
            birth_identity="leader-original" if pid == leader_pid else "new-member",
            state="running",
        )

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        assert pgid == leader_pid
        return tuple(pid for pid, is_running in running.items() if is_running), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        running[pid] = False

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader,),
        tracked_pids=(leader_pid,),
        tracked_pgids=(leader_pid,),
    )

    assert (leader_pid, signal.SIGTERM) in signals
    assert all(pid != new_member_pid for pid, _sig in signals)
    assert all(target.pid != new_member_pid for target in snapshot.targets)
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_absent_group_leader_still_cleans_captured_same_birth_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader_pid = REUSED_PGID
    child_pid = REUSED_PID
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-original",
        initial_state="running",
    )
    child = command_runner.ProcessCleanupIdentity(
        pid=child_pid,
        pgid=leader_pid,
        birth_identity="child-original",
        initial_state="running",
    )
    child_running = True
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == leader_pid or not child_running:
            return None
        assert pid == child_pid
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=leader_pid,
            birth_identity="child-original",
            state="running",
        )

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        assert pgid == leader_pid
        return ((child_pid,) if child_running else ()), True

    def signal_pid(pid: int, sig: signal.Signals) -> None:
        nonlocal child_running
        signals.append((pid, sig))
        child_running = False

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", signal_pid)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader, child),
        tracked_pids=(leader_pid, child_pid),
        tracked_pgids=(leader_pid,),
    )

    assert signals == [(child_pid, signal.SIGTERM)]
    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
    assert snapshot.remaining_process_group_count == 0
    assert tuple(target.pid for target in snapshot.targets) == (child_pid, leader_pid)


def test_group_leader_reuse_after_member_selection_prevents_member_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader_pid = REUSED_PGID
    member_pid = REUSED_PID
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-original",
        initial_state="running",
    )
    member = command_runner.ProcessCleanupIdentity(
        pid=member_pid,
        pgid=leader_pid,
        birth_identity="member-original",
        initial_state="running",
    )
    leader_reads = 0
    signals: list[tuple[int, signal.Signals]] = []

    def observation(pid: int) -> command_runner.ProcessObservation:
        nonlocal leader_reads
        if pid == member_pid:
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=leader_pid,
                birth_identity="member-original",
                state="running",
            )
        leader_reads += 1
        return command_runner.ProcessObservation(
            pid=leader_pid,
            pgid=leader_pid,
            birth_identity=(
                "leader-original" if leader_reads <= LEADER_REUSE_AFTER_READS else "leader-reused"
            ),
            state="running",
        )

    def group_members(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (leader_pid, member_pid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader, member),
        tracked_pids=(leader_pid, member_pid),
        tracked_pgids=(leader_pid,),
    )

    assert signals == []
    assert snapshot.remaining_process_count in {1, None}
    assert snapshot.cleanup_status in {"failed", "unknown"}


def test_birth_bound_cleanup_does_not_signal_reused_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = getattr(command_runner, "cleanup_birth_bound_processes", None)
    assert cleanup is not None
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="original-birth",
        initial_state="running",
    )
    replacement_pid = REUSED_PID + 1
    signals: list[tuple[int, object]] = []
    group_signals: list[int] = []

    def record_group_signal(pgid: int, *, escalate: bool) -> None:
        _ = escalate
        group_signals.append(pgid)

    def absent_observation(_pid: int) -> None:
        return None

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        members = (replacement_pid,) if pgid == REUSED_PGID else ()
        return members, True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(command_runner, "read_process_observation", absent_observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )
    monkeypatch.setattr(
        command_runner,
        "terminate_process_group",
        record_group_signal,
    )

    snapshot = cleanup(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(REUSED_PGID,),
    )

    assert signals == []
    assert group_signals == []
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_birth_bound_cleanup_same_birth_survivor_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = getattr(command_runner, "cleanup_birth_bound_processes", None)
    assert cleanup is not None
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="same-birth",
        initial_state="running",
    )
    survivor = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="same-birth",
        state="running",
    )
    signals: list[tuple[int, object]] = []

    def survivor_observation(_pid: int) -> command_runner.ProcessObservation:
        return survivor

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", survivor_observation)
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    snapshot = cleanup(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(REUSED_PID,),
    )

    assert [signal for _pid, signal in signals] == [
        command_runner.signal.SIGTERM,
        command_runner.signal.SIGKILL,
    ]
    assert snapshot.cleanup_status == "failed"
    assert snapshot.remaining_process_count == 1
    assert snapshot.remaining_process_group_count == 1
    assert snapshot.targets[0].termination_outcome == "still_running"


def test_birth_bound_cleanup_zombie_is_not_a_candidate_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = getattr(command_runner, "cleanup_birth_bound_processes", None)
    assert cleanup is not None
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="same-birth",
        initial_state="running",
    )
    zombie = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="same-birth",
        state="zombie",
    )
    signals: list[tuple[int, object]] = []

    def zombie_observation(_pid: int) -> command_runner.ProcessObservation:
        return zombie

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(command_runner, "read_process_observation", zombie_observation)
    monkeypatch.setattr(
        command_runner,
        "_signal_pid",
        record_signal,
    )

    snapshot = cleanup(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
    )

    assert signals == []
    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
    assert snapshot.remaining_process_group_count == 0
    assert snapshot.targets[0].termination_outcome == "non_executing_zombie"


def test_birth_bound_cleanup_unknown_identity_coverage_fails_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = getattr(command_runner, "cleanup_birth_bound_processes", None)
    assert cleanup is not None
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="same-birth",
        initial_state="running",
    )
    unknown_pid = REUSED_PID + 1
    unknown = command_runner.ProcessObservation(
        pid=unknown_pid,
        pgid=REUSED_PGID,
        birth_identity="",
        state="unknown",
    )

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == REUSED_PID:
            return None
        return unknown

    monkeypatch.setattr(command_runner, "read_process_observation", observation)

    snapshot = cleanup(
        (identity,),
        tracked_pids=(REUSED_PID, unknown_pid),
        tracked_pgids=(),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_member"
    assert snapshot.coverage_subreason == "observation_unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_birth_bound_cleanup_incomplete_group_table_has_typed_unknown_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="known-birth",
        initial_state="running",
    )

    def incomplete_group(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (), False

    def missing_observation(_pid: int) -> command_runner.ProcessObservation | None:
        return None

    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        incomplete_group,
    )
    monkeypatch.setattr(command_runner, "read_process_observation", missing_observation)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(REUSED_PGID,),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.coverage_stage == "group_table"
    assert snapshot.coverage_subreason == "table_incomplete"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_verified_cleanup_receipt_uses_authenticated_semantic_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = getattr(command_runner, "cleanup_birth_bound_processes", None)
    assert cleanup is not None
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="original-birth",
        initial_state="running",
    )
    replacement = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PGID,
        birth_identity="replacement-birth",
        state="running",
    )

    def replacement_observation(_pid: int) -> command_runner.ProcessObservation:
        return replacement

    monkeypatch.setattr(command_runner, "read_process_observation", replacement_observation)
    snapshot = cleanup(
        (identity,),
        tracked_pids=(REUSED_PID,),
        tracked_pgids=(),
    )
    receipt_path = (tmp_path / "semantic-cleanup.json").resolve()
    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="semantic-counts",
        argv=("python",),
        cwd=tmp_path,
        root_pid=REUSED_PID,
        root_pgid=REUSED_PGID,
        terminal_snapshot=snapshot,
    )
    assert evidence.receipt_sha256 is not None

    def always_running(_pid: int) -> bool:
        return True

    monkeypatch.setattr(command_runner, "process_still_running", always_running)
    verified = command_runner.verify_command_cleanup_identity_receipt(
        receipt_path,
        expected_receipt_sha256=evidence.receipt_sha256,
    )

    assert verified is not None
    assert verified.remaining_process_count == 0
    assert verified.remaining_process_group_count == 0

    original_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered = json.loads(json.dumps(original_payload))
    tampered["targets"][0]["terminal_state"] = "running"
    material = {key: value for key, value in tampered.items() if key != "receipt_sha256"}
    tampered_digest = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    tampered["receipt_sha256"] = tampered_digest
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_identity_receipt(
            receipt_path,
            expected_receipt_sha256=tampered_digest,
        )
        is None
    )

    unknown = json.loads(json.dumps(original_payload))
    unknown["targets"][0]["terminal_state"] = "unknown"
    unknown["targets"][0]["termination_outcome"] = "unknown"
    unknown["targets"][0]["terminal_birth_identity_sha256"] = None
    material = {key: value for key, value in unknown.items() if key != "receipt_sha256"}
    unknown_digest = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    unknown["receipt_sha256"] = unknown_digest
    receipt_path.write_text(json.dumps(unknown), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_identity_receipt(
            receipt_path,
            expected_receipt_sha256=unknown_digest,
        )
        is None
    )


def test_process_observation_failure_remains_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="known-birth",
        initial_state="running",
    )
    unknown = command_runner.ProcessObservation(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="",
        state="unknown",
    )

    def unknown_observation(_pid: int) -> command_runner.ProcessObservation:
        return unknown

    monkeypatch.setattr(command_runner, "read_process_observation", unknown_observation)

    evidence = command_runner.observe_process_cleanup_target(identity)

    assert evidence.terminal_state == "unknown"
    assert evidence.termination_outcome == "unknown"
    assert evidence.terminal_birth_identity_sha256 is None


def test_cleanup_identity_receipt_rejects_tamper_binding_and_private_content(
    tmp_path: Path,
) -> None:
    receipt_path = (tmp_path / "cleanup.json").resolve()
    private_argument = "PRIVATE_ARGUMENT_DO_NOT_PUBLISH"
    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "privacy_probe",
            (
                sys.executable,
                "-c",
                "import os,sys; assert os.environ['PRIVATE_SENTINEL']; sys.exit(9)",
                private_argument,
            ),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )
    digest = caught.value.cleanup_identity_receipt_sha256
    assert digest is not None
    raw = receipt_path.read_text(encoding="utf-8")
    assert private_argument not in raw
    assert "DO_NOT_PUBLISH" not in raw
    assert str(tmp_path) not in raw
    verified = command_runner.verify_command_cleanup_identity_receipt(
        receipt_path,
        expected_receipt_sha256=digest,
    )
    assert verified is not None
    assert verified.watcher_drain_status == "drained"
    assert (
        command_runner.verify_command_cleanup_identity_receipt(
            receipt_path,
            expected_receipt_sha256="f" * 64,
        )
        is None
    )

    payload = json.loads(raw)
    payload["targets"][0]["terminal_state"] = "running"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path.chmod(0o600)
    assert (
        command_runner.verify_command_cleanup_identity_receipt(
            receipt_path,
            expected_receipt_sha256=digest,
        )
        is None
    )

    payload = json.loads(raw)
    payload["watcher_drain_status"] = "discarded"
    material = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    tampered_digest = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    payload["receipt_sha256"] = tampered_digest
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_identity_receipt(
            receipt_path,
            expected_receipt_sha256=tampered_digest,
        )
        is None
    )


@pytest.mark.parametrize("root_transition", ["exited-absent", "exited-reused", "birth-mismatch"])
def test_run_command_closed_root_gate_never_admits_replacement(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_transition: str,
) -> None:
    """A completed or birth-mismatched Popen root closes admission before scans."""
    root_pid = REUSED_PGID
    replacement_pid = root_pid + 1
    root_state = "original"
    replacement_running = True
    admitted_for_cleanup: list[int] = []
    signals: list[tuple[int, signal.Signals]] = []
    original_cleanup = command_runner.cleanup_birth_bound_processes

    class ExitedProcess:
        pid = root_pid
        returncode: int | None = None
        poll_count = 0

        def poll(self) -> int | None:
            nonlocal root_state
            self.poll_count += 1
            root_state = "absent" if root_transition == "exited-absent" else "reused"
            if root_transition == "birth-mismatch" and self.poll_count == 1:
                return None
            self.returncode = 0
            return 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            _ = timeout
            return "", ""

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == root_pid:
            if root_state == "absent":
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=root_pid,
                birth_identity=(
                    "root-original" if root_state == "original" else "root-replacement"
                ),
                state="running",
            )
        assert pid == replacement_pid
        if not replacement_running:
            return None
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=root_pid,
            birth_identity="unrelated-replacement-member",
            state="running",
        )

    def snapshot(
        observed_root_pid: int | None,
        observed_pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
    ]:
        assert observed_root_pid is None
        assert observed_pgid == root_pid
        if root_state == "original":
            return (root_pid,), (root_pid,), "complete"
        current = (replacement_pid,) if root_state == "absent" else (root_pid, replacement_pid)
        return current, (root_pid,), "complete"

    def group_members(pgid: int) -> tuple[int, ...]:
        assert pgid == root_pid
        members: list[int] = []
        if root_state == "reused":
            members.append(root_pid)
        if replacement_running:
            members.append(replacement_pid)
        return tuple(members)

    def group_members_with_coverage(pgid: int) -> tuple[tuple[int, ...], bool]:
        return group_members(pgid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        nonlocal replacement_running
        signals.append((pid, sig))
        if pid == replacement_pid:
            replacement_running = False

    def record_cleanup(  # noqa: PLR0913
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        tracked_pid_groups: tuple[tuple[int, int], ...] = (),
        observed_identities: tuple[command_runner.ProcessCleanupIdentity, ...] = (),
        coverage_status: command_runner.CleanupCoverage = "complete",
        coverage_stage: command_runner.CleanupCoverageStage | None = None,
        coverage_subreason: command_runner.CleanupCoverageSubreason | None = None,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        admitted_for_cleanup.extend(identity.pid for identity in identities)
        return original_cleanup(
            identities,
            tracked_pids=tracked_pids,
            tracked_pgids=tracked_pgids,
            tracked_pid_groups=tracked_pid_groups,
            observed_identities=observed_identities,
            coverage_status=coverage_status,
            coverage_stage=coverage_stage,
            coverage_subreason=coverage_subreason,
        )

    def fake_popen(*_args: object, **_kwargs: object) -> ExitedProcess:
        return ExitedProcess()

    def no_sleep(_seconds: float) -> None:
        return

    def root_group_id(_pid: int) -> int:
        return root_pid

    monkeypatch.setattr(command_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(command_runner.os, "getpgid", root_group_id)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", snapshot)
    monkeypatch.setattr(command_runner, "process_group_members", group_members)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members_with_coverage,
    )
    monkeypatch.setattr(command_runner, "cleanup_birth_bound_processes", record_cleanup)
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "post_exit_reuse",
            (sys.executable, "-c", "raise SystemExit(0)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
        )

    assert replacement_pid not in admitted_for_cleanup
    assert all(pid != replacement_pid for pid, _sig in signals)
    assert caught.value.stderr == "process_cleanup_unknown"
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None


def test_run_command_post_exit_root_pid_reuse_does_not_expand_unrelated_tree(  # noqa: C901
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-used numeric root PID cannot add another process group after exit."""
    root_pid = REUSED_PID
    unrelated_pid = root_pid + 100
    unrelated_pgid = unrelated_pid
    root_state = "original"
    snapshot_root_args: list[int | None] = []

    class ExitingProcess:
        pid = root_pid
        returncode: int | None = None
        poll_count = 0

        def poll(self) -> int | None:
            nonlocal root_state
            self.poll_count += 1
            if self.poll_count <= ROOT_ADMISSION_POLL_COUNT:
                return None
            root_state = "exited"
            self.returncode = 0
            return 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            return "", ""

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == root_pid:
            if root_state == "exited":
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=root_pid,
                birth_identity="root-birth",
                state="running",
            )
        if pid == unrelated_pid:
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=unrelated_pgid,
                birth_identity="unrelated-birth",
                state="running",
            )
        return None

    def snapshot(
        observed_root_pid: int | None,
        observed_pgid: int | None,
    ) -> tuple[tuple[int, ...], tuple[int, ...], command_runner.CleanupCoverage]:
        assert observed_pgid == root_pid
        snapshot_root_args.append(observed_root_pid)
        if root_state == "original":
            return (root_pid,), (root_pid,), "complete"
        if observed_root_pid is None:
            return (), (root_pid,), "complete"
        return (root_pid, unrelated_pid), (root_pid, unrelated_pgid), "complete"

    def fake_popen(*_args: object, **_kwargs: object) -> ExitingProcess:
        return ExitingProcess()

    def root_group_id(_pid: int) -> int:
        return root_pid

    def empty_group(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (), True

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(
        command_runner.subprocess,
        "Popen",
        fake_popen,
    )
    monkeypatch.setattr(command_runner.os, "getpgid", root_group_id)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", snapshot)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        empty_group,
    )
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    result = command_runner.run_command(
        "post_exit_unrelated_tree",
        (sys.executable, "-c", "raise SystemExit(0)"),
        cwd=tmp_path,
        env=_env(tmp_path),
        timeout_seconds=5,
    )

    assert result.receipt.exit_code == 0
    assert unrelated_pid not in result.receipt.argv
    assert snapshot_root_args[-POST_AND_FINAL_SNAPSHOT_COUNT:] == [None, None]


def test_run_command_ignores_cross_group_reuse_of_unobserved_discovered_child(  # noqa: C901
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished child reused in another group is not task cleanup residue."""
    root_pid = 82_100
    vanished_child_pid = root_pid + 1
    vanished_child_pgid = root_pid + 10
    replacement_pgid = root_pid + 20
    root_running = True
    signals: list[tuple[int, signal.Signals]] = []

    class ExitingProcess:
        pid = root_pid
        returncode: int | None = None
        poll_count = 0

        def poll(self) -> int | None:
            nonlocal root_running
            self.poll_count += 1
            if self.poll_count <= ROOT_ADMISSION_POLL_COUNT:
                return None
            root_running = False
            self.returncode = 0
            return 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            return "", ""

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == root_pid:
            if not root_running:
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=root_pid,
                birth_identity="root-birth",
                state="running",
            )
        if pid == vanished_child_pid:
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=replacement_pgid,
                birth_identity="unrelated-replacement-birth",
                state="running",
            )
        return None

    def capture_scope(
        admission: command_runner.RootBoundProcessCleanupAdmission,
    ) -> command_runner.ProcessCleanupScope:
        assert admission.root_identity is not None
        historical_child = command_runner.ProcessCleanupIdentity(
            pid=vanished_child_pid,
            pgid=vanished_child_pgid,
            birth_identity="task-child-birth",
            initial_state="running",
        )
        return command_runner.ProcessCleanupScope(
            identities=(admission.root_identity,),
            tracked_pids=(root_pid, vanished_child_pid),
            tracked_pgids=(root_pid, vanished_child_pgid),
            coverage_status="complete",
            tracked_pid_groups=(
                (root_pid, root_pid),
                (vanished_child_pid, vanished_child_pgid),
            ),
            observed_identities=(admission.root_identity, historical_child),
        )

    def snapshot(
        observed_root_pid: int | None,
        observed_pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
        tuple[tuple[int, int], ...],
    ]:
        assert observed_root_pid is None
        assert observed_pgid == root_pid
        return (), (root_pid, vanished_child_pgid), "complete", ()

    def empty_group(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (), True

    def fake_popen(*_args: object, **_kwargs: object) -> ExitingProcess:
        return ExitingProcess()

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))

    def root_group_id(_pid: int) -> int:
        return root_pid

    monkeypatch.setattr(command_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(command_runner.os, "getpgid", root_group_id)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner.RootBoundProcessCleanupAdmission,
        "capture_scope",
        capture_scope,
    )
    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", snapshot)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        empty_group,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)

    result = command_runner.run_command(
        "cross_group_child_pid_reuse",
        (sys.executable, "-c", "raise SystemExit(0)"),
        cwd=tmp_path,
        env=_env(tmp_path),
        timeout_seconds=5,
    )

    assert result.receipt.exit_code == 0
    assert result.cleanup_identity_evidence_status == "no-target-observed"
    assert signals == []


@pytest.mark.parametrize(
    "child_transition",
    [
        "legitimate",
        "replaced-self-group",
        "moved-group-after-observation",
        "missing-after-observation",
        "unknown-after-observation",
    ],
)
def test_run_command_second_scope_admits_only_still_bound_child(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_transition: str,
) -> None:
    """A child must retain its discovered root scope through identity admission."""
    root_pid = REUSED_PGID
    child_pid = root_pid + 1
    group_peer_pid = root_pid + 2
    running = {root_pid: True, child_pid: True, group_peer_pid: True}
    child_observed = False
    child_group_moved = False
    second_scope_taken = False
    poll_calls = 0
    admitted_for_cleanup: list[int] = []
    observed_for_cleanup: list[int] = []
    signals: list[tuple[int, signal.Signals]] = []
    original_cleanup = command_runner.cleanup_birth_bound_processes

    class ActiveProcess:
        pid = root_pid
        returncode: int | None = None

        def poll(self) -> int | None:
            nonlocal poll_calls
            poll_calls += 1
            if (
                child_transition
                in {
                    "moved-group-after-observation",
                    "missing-after-observation",
                    "unknown-after-observation",
                }
                and poll_calls >= 4  # noqa: PLR2004
            ):
                self.returncode = 3
                return 3
            return None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            _ = timeout
            return "", ""

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        nonlocal child_observed
        if not running[pid]:
            return None
        if pid == child_pid and second_scope_taken:
            if child_transition == "missing-after-observation":
                return None
            if child_transition == "unknown-after-observation":
                return command_runner.ProcessObservation(
                    pid=pid,
                    pgid=root_pid,
                    birth_identity="child-original",
                    state="unknown",
                )
        if pid == child_pid:
            child_observed = True
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=(
                child_pid
                if pid == child_pid
                and (
                    child_transition == "replaced-self-group"
                    or (child_transition == "moved-group-after-observation" and child_group_moved)
                )
                else root_pid
            ),
            birth_identity=(
                "root-original"
                if pid == root_pid
                else "unrelated-replacement"
                if child_transition == "replaced-self-group"
                else "child-original"
                if pid == child_pid
                else "peer-original"
            ),
            state="running",
        )

    def snapshot(
        observed_root_pid: int | None,
        observed_pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
    ]:
        nonlocal child_group_moved, second_scope_taken
        assert observed_root_pid in {None, root_pid}
        assert observed_pgid == root_pid
        if child_observed:
            second_scope_taken = True
        if child_transition == "moved-group-after-observation" and child_observed:
            child_group_moved = True
        visible = tuple(
            pid
            for pid, is_running in running.items()
            if is_running
            and not (
                pid == child_pid and child_transition == "replaced-self-group" and child_observed
            )
        )
        if child_transition == "moved-group-after-observation" and child_group_moved:
            return visible, (root_pid, child_pid), "complete"
        return visible, (root_pid,), "complete"

    def group_members(pgid: int) -> tuple[int, ...]:
        assert pgid in {root_pid, child_pid}
        return tuple(
            pid
            for pid, is_running in running.items()
            if is_running
            and (
                pid == child_pid
                if pgid == child_pid
                else pid != child_pid
                or not (
                    child_transition == "replaced-self-group"
                    or (child_transition == "moved-group-after-observation" and child_group_moved)
                )
            )
        )

    def group_members_with_coverage(pgid: int) -> tuple[tuple[int, ...], bool]:
        return group_members(pgid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        running[pid] = False

    def record_cleanup(  # noqa: PLR0913
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        tracked_pid_groups: tuple[tuple[int, int], ...] = (),
        observed_identities: tuple[command_runner.ProcessCleanupIdentity, ...] = (),
        coverage_status: command_runner.CleanupCoverage = "complete",
        coverage_stage: command_runner.CleanupCoverageStage | None = None,
        coverage_subreason: command_runner.CleanupCoverageSubreason | None = None,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        admitted_for_cleanup.extend(identity.pid for identity in identities)
        observed_for_cleanup.extend(identity.pid for identity in observed_identities)
        return original_cleanup(
            identities,
            tracked_pids=tracked_pids,
            tracked_pgids=tracked_pgids,
            tracked_pid_groups=tracked_pid_groups,
            observed_identities=observed_identities,
            coverage_status=coverage_status,
            coverage_stage=coverage_stage,
            coverage_subreason=coverage_subreason,
        )

    def fake_popen(*_args: object, **_kwargs: object) -> ActiveProcess:
        return ActiveProcess()

    def no_sleep(_seconds: float) -> None:
        return

    def root_group_id(_pid: int) -> int:
        return root_pid

    monkeypatch.setattr(command_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(command_runner.os, "getpgid", root_group_id)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", snapshot)
    monkeypatch.setattr(command_runner, "process_group_members", group_members)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members_with_coverage,
    )
    monkeypatch.setattr(command_runner, "cleanup_birth_bound_processes", record_cleanup)
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", no_sleep)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "active_root_child",
            (sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=0,
        )

    refused_transitions = {
        "replaced-self-group",
        "moved-group-after-observation",
        "missing-after-observation",
        "unknown-after-observation",
    }
    root_exits_early = refused_transitions - {"replaced-self-group"}
    assert caught.value.receipt.timed_out is (child_transition not in root_exits_early)
    if child_transition == "replaced-self-group":
        assert child_pid not in observed_for_cleanup
    else:
        assert child_pid in observed_for_cleanup
    if child_transition in refused_transitions:
        assert child_pid not in admitted_for_cleanup
        assert all(pid != child_pid for pid, _sig in signals)
        assert caught.value.remaining_process_count is None
        assert caught.value.remaining_process_group_count is None
    else:
        assert child_pid in admitted_for_cleanup
        assert (child_pid, signal.SIGTERM) in signals
        assert caught.value.remaining_process_count == 0
        assert caught.value.remaining_process_group_count == 0


def test_timeout_cleans_once_before_terminal_observation_write_and_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = (tmp_path / "timeout-cleanup.json").resolve()
    cleanup_calls = 0
    cleanup_complete = False
    inside_cleanup = False
    events: list[str] = []
    original_cleanup = command_runner.cleanup_birth_bound_processes
    original_write = command_runner.write_command_cleanup_identity_receipt
    original_remaining_pids = command_runner.remaining_live_pids
    original_remaining_pgids = command_runner.remaining_live_pgids

    def counted_cleanup(  # noqa: PLR0913
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        tracked_pid_groups: tuple[tuple[int, int], ...] = (),
        observed_identities: tuple[command_runner.ProcessCleanupIdentity, ...] = (),
        coverage_status: command_runner.CleanupCoverage = "complete",
        coverage_stage: command_runner.CleanupCoverageStage | None = None,
        coverage_subreason: command_runner.CleanupCoverageSubreason | None = None,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        nonlocal cleanup_calls, cleanup_complete, inside_cleanup
        cleanup_calls += 1
        events.append("cleanup")
        inside_cleanup = True
        try:
            return original_cleanup(
                identities,
                tracked_pids=tracked_pids,
                tracked_pgids=tracked_pgids,
                tracked_pid_groups=tracked_pid_groups,
                observed_identities=observed_identities,
                coverage_status=coverage_status,
                coverage_stage=coverage_stage,
                coverage_subreason=coverage_subreason,
            )
        finally:
            inside_cleanup = False
            cleanup_complete = True

    def recorded_write(  # noqa: PLR0913
        path: Path,
        *,
        name: str,
        argv: tuple[str, ...],
        cwd: Path,
        root_pid: int,
        root_pgid: int,
        identities: tuple[command_runner.ProcessCleanupIdentity, ...] | None = None,
        terminal_snapshot: command_runner.ProcessCleanupTerminalSnapshot | None = None,
    ) -> command_runner.CommandCleanupIdentityEvidence:
        assert cleanup_complete
        assert terminal_snapshot is not None
        events.append("observe-write")
        return original_write(
            path,
            name=name,
            argv=argv,
            cwd=cwd,
            root_pid=root_pid,
            root_pgid=root_pgid,
            identities=identities,
            terminal_snapshot=terminal_snapshot,
        )

    def recorded_remaining_pids(pids: tuple[int, ...]) -> tuple[int, ...]:
        if cleanup_complete and not inside_cleanup:
            events.append("terminal-count")
        return original_remaining_pids(pids)

    def recorded_remaining_pgids(pgids: tuple[int, ...]) -> tuple[int, ...]:
        if cleanup_complete and not inside_cleanup:
            events.append("terminal-count")
        return original_remaining_pgids(pgids)

    monkeypatch.setattr(command_runner, "cleanup_birth_bound_processes", counted_cleanup)
    monkeypatch.setattr(command_runner, "write_command_cleanup_identity_receipt", recorded_write)
    monkeypatch.setattr(command_runner, "remaining_live_pids", recorded_remaining_pids)
    monkeypatch.setattr(command_runner, "remaining_live_pgids", recorded_remaining_pgids)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "timeout_cleanup_order",
            (sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=0,
            cleanup_identity_receipt_path=receipt_path,
        )

    assert caught.value.receipt.timed_out is True
    assert cleanup_calls == 1
    assert events.index("cleanup") < events.index("observe-write")
    assert "terminal-count" not in events
    assert caught.value.remaining_process_count == 0
    assert caught.value.remaining_process_group_count == 0


def test_post_spawn_oserror_cleans_once_and_observes_terminal_survivor_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_roots: list[int] = []
    cleanup_calls = 0
    original_cleanup = command_runner.cleanup_birth_bound_processes

    def broken_snapshot(
        root_pid: int | None,
        pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
    ]:
        _ = (root_pid, pgid)
        raise OSError("injected_post_spawn_observation_failure")

    def counted_cleanup(  # noqa: PLR0913
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        tracked_pid_groups: tuple[tuple[int, int], ...] = (),
        observed_identities: tuple[command_runner.ProcessCleanupIdentity, ...] = (),
        coverage_status: command_runner.CleanupCoverage = "complete",
        coverage_stage: command_runner.CleanupCoverageStage | None = None,
        coverage_subreason: command_runner.CleanupCoverageSubreason | None = None,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_roots.extend(identity.pid for identity in identities)
        return original_cleanup(
            identities,
            tracked_pids=tracked_pids,
            tracked_pgids=tracked_pgids,
            tracked_pid_groups=tracked_pid_groups,
            observed_identities=observed_identities,
            coverage_status=coverage_status,
            coverage_stage=coverage_stage,
            coverage_subreason=coverage_subreason,
        )

    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", broken_snapshot)
    monkeypatch.setattr(command_runner, "cleanup_birth_bound_processes", counted_cleanup)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "post_spawn_oserror_cleanup",
            (sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=(tmp_path / "oserror-cleanup.json").resolve(),
        )

    assert len(cleanup_roots) == 1
    assert cleanup_calls == 1
    assert command_runner.process_still_running(cleanup_roots[0]) is False
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None
    assert caught.value.cleanup_identity_evidence_status == "observation-unknown"
    assert caught.value.cleanup_unknown_reason == "coverage_unknown"
    assert caught.value.cleanup_identity_receipt_sha256 is not None


def test_cleanup_evidence_distinguishes_no_target_unknown_and_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = (tmp_path / "cleanup-status.json").resolve()
    no_target = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="no-target",
        argv=("python",),
        cwd=tmp_path,
        root_pid=REUSED_PID,
        root_pgid=REUSED_PID,
        identities=(),
    )
    assert no_target.evidence_status == "no-target-observed"
    assert no_target.receipt_sha256 is None

    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="known-birth",
        initial_state="running",
    )
    unknown_target = command_runner.ProcessCleanupTargetReceipt(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity_sha256="a" * 64,
        terminal_birth_identity_sha256=None,
        terminal_state="unknown",
        termination_outcome="unknown",
    )

    def observe_unknown(
        _identity: command_runner.ProcessCleanupIdentity,
    ) -> command_runner.ProcessCleanupTargetReceipt:
        return unknown_target

    monkeypatch.setattr(command_runner, "observe_process_cleanup_target", observe_unknown)
    unknown = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="unknown",
        argv=("python",),
        cwd=tmp_path,
        root_pid=REUSED_PID,
        root_pgid=REUSED_PID,
        identities=(identity,),
    )
    assert unknown.evidence_status == "observation-unknown"
    assert unknown.receipt_sha256 is not None
    assert unknown.unknown_reason == "target_observation_unknown"
    verified_unknown = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=unknown.receipt_sha256,
    )
    assert verified_unknown is not None
    assert verified_unknown.unknown_reason == "target_observation_unknown"
    assert verified_unknown.remaining_process_count is None
    assert verified_unknown.remaining_process_group_count is None

    known_target = unknown_target.model_copy(
        update={
            "terminal_state": "absent",
            "termination_outcome": "no_longer_running",
        },
    )

    def observe_known(
        _identity: command_runner.ProcessCleanupIdentity,
    ) -> command_runner.ProcessCleanupTargetReceipt:
        return known_target

    def fail_write(_path: Path, _text: str) -> bool:
        return False

    monkeypatch.setattr(command_runner, "observe_process_cleanup_target", observe_known)
    monkeypatch.setattr(command_runner, "_atomic_owner_only_write", fail_write)
    write_failed = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="write-failed",
        argv=("python",),
        cwd=tmp_path,
        root_pid=REUSED_PID,
        root_pgid=REUSED_PID,
        identities=(identity,),
    )
    assert write_failed.evidence_status == "write-failed"
    assert write_failed.receipt_sha256 is None


def test_cleanup_observation_error_writes_bound_unknown_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = (tmp_path / "observation-error.json").resolve()
    identity = command_runner.ProcessCleanupIdentity(
        pid=REUSED_PID,
        pgid=REUSED_PID,
        birth_identity="known-birth",
        initial_state="running",
    )

    def fail_observation(
        _identity: command_runner.ProcessCleanupIdentity,
    ) -> command_runner.ProcessCleanupTargetReceipt:
        raise OSError("injected_private_observation_error")

    monkeypatch.setattr(command_runner, "observe_process_cleanup_target", fail_observation)

    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="observation-error",
        argv=("python",),
        cwd=tmp_path,
        root_pid=REUSED_PID,
        root_pgid=REUSED_PID,
        identities=(identity,),
    )

    assert evidence.evidence_status == "observation-unknown"
    assert evidence.unknown_reason == "target_observation_unknown"
    assert evidence.receipt_sha256 is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=evidence.receipt_sha256,
    )
    assert receipt is not None
    assert receipt.unknown_reason == "target_observation_unknown"
    assert receipt.target_count == 1
    assert receipt.remaining_process_count is None
    assert receipt.remaining_process_group_count is None
    assert "injected_private" not in receipt_path.read_text(encoding="utf-8")


def test_run_command_discards_late_watcher_scope_and_refuses_false_zero(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admission pass still in flight at the drain deadline cannot prove cleanup."""
    root_release = (tmp_path / "release-root").resolve()
    receipt_path = (tmp_path / "watcher-cleanup.json").resolve()
    watcher_scope_ready = threading.Event()
    release_watcher = threading.Event()
    watcher_finished = threading.Event()
    escaped_pid = REUSED_PID + 900
    cleanup_identity_pids: list[int] = []
    outcome: dict[str, object] = {}
    original_capture = command_runner.RootBoundProcessCleanupAdmission.capture_scope
    original_cleanup = command_runner.cleanup_birth_bound_processes

    def delayed_watcher_capture(
        admission: command_runner.RootBoundProcessCleanupAdmission,
    ) -> command_runner.ProcessCleanupScope:
        scope = original_capture(admission)
        if (
            threading.current_thread().name.startswith("cmd-watch-")
            and not watcher_scope_ready.is_set()
        ):
            escaped = command_runner.ProcessCleanupIdentity(
                pid=escaped_pid,
                pgid=escaped_pid,
                birth_identity="escaped-before-freeze",
                initial_state="running",
            )
            scope = command_runner.ProcessCleanupScope(
                identities=(*scope.identities, escaped),
                tracked_pids=(*scope.tracked_pids, escaped_pid),
                tracked_pgids=(*scope.tracked_pgids, escaped_pid),
                coverage_status=scope.coverage_status,
                observed_identities=scope.observed_identities,
                coverage_stage=scope.coverage_stage,
                coverage_subreason=scope.coverage_subreason,
            )
            watcher_scope_ready.set()
            if not release_watcher.wait(timeout=5):
                raise AssertionError("watcher release was not delivered")
            watcher_finished.set()
        return scope

    def record_cleanup(  # noqa: PLR0913
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        tracked_pid_groups: tuple[tuple[int, int], ...] = (),
        observed_identities: tuple[command_runner.ProcessCleanupIdentity, ...] = (),
        coverage_status: command_runner.CleanupCoverage = "complete",
        coverage_stage: command_runner.CleanupCoverageStage | None = None,
        coverage_subreason: command_runner.CleanupCoverageSubreason | None = None,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        cleanup_identity_pids.extend(identity.pid for identity in identities)
        return original_cleanup(
            identities,
            tracked_pids=tracked_pids,
            tracked_pgids=tracked_pgids,
            tracked_pid_groups=tracked_pid_groups,
            observed_identities=observed_identities,
            coverage_status=coverage_status,
            coverage_stage=coverage_stage,
            coverage_subreason=coverage_subreason,
        )

    code = (
        "import os,time\n"
        "while not os.path.exists(os.environ['ROOT_RELEASE']):\n"
        "    time.sleep(0.001)\n"
    )
    env = _env(tmp_path)
    env["ROOT_RELEASE"] = str(root_release)

    monkeypatch.setattr(
        command_runner.RootBoundProcessCleanupAdmission,
        "capture_scope",
        delayed_watcher_capture,
    )
    monkeypatch.setattr(command_runner, "cleanup_birth_bound_processes", record_cleanup)
    monkeypatch.setattr(command_runner, "WATCHER_DRAIN_SECONDS", 0.01, raising=False)

    def invoke() -> None:
        try:
            outcome["result"] = command_runner.run_command(
                "watcher_freeze_probe",
                (sys.executable, "-c", code),
                cwd=tmp_path,
                env=env,
                timeout_seconds=5,
                cleanup_identity_receipt_path=receipt_path,
            )
        except BaseException as exc:  # noqa: BLE001 - test captures thread outcome
            outcome["error"] = exc

    worker = threading.Thread(target=invoke, name="watcher-freeze-test")
    worker.start()
    try:
        assert watcher_scope_ready.wait(timeout=5)
        root_release.touch(mode=OWNER_FILE_MODE)
        worker.join(timeout=5)
        assert not worker.is_alive()
        error = outcome.get("error")
        assert isinstance(error, command_runner.CommandFailureError)
        assert error.stderr == "process_cleanup_unknown"
        assert error.remaining_process_count is None
        assert error.remaining_process_group_count is None
        assert error.cleanup_identity_evidence_status == "observation-unknown"
        assert error.cleanup_unknown_reason == "watcher_still_running"
        assert error.cleanup_identity_receipt_sha256 is not None
        unknown_receipt = command_runner.verify_command_cleanup_unknown_receipt(
            receipt_path,
            expected_receipt_sha256=error.cleanup_identity_receipt_sha256,
        )
        assert unknown_receipt is not None
        assert unknown_receipt.unknown_reason == "watcher_still_running"
        assert unknown_receipt.watcher_drain_status == "still-running"
        assert unknown_receipt.coverage_status == "complete"
        assert unknown_receipt.remaining_process_count is None
        assert unknown_receipt.remaining_process_group_count is None
        assert escaped_pid not in cleanup_identity_pids
    finally:
        release_watcher.set()
        worker.join(timeout=5)
    assert watcher_finished.wait(timeout=5)


def test_run_command_completed_watcher_publish_is_drained_and_authenticated(
    tmp_path: Path,
) -> None:
    """A watcher that publishes and drains before freeze retains complete evidence."""
    receipt_path = (tmp_path / "drained-watcher.json").resolve()
    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "drained_watcher_probe",
            (sys.executable, "-c", "import time; time.sleep(0.05); raise SystemExit(7)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )

    digest = caught.value.cleanup_identity_receipt_sha256
    assert caught.value.cleanup_identity_evidence_status == "authenticated"
    assert digest is not None
    receipt = command_runner.verify_command_cleanup_identity_receipt(
        receipt_path,
        expected_receipt_sha256=digest,
    )
    assert receipt is not None
    assert receipt.watcher_drain_status == "drained"
    assert receipt.remaining_process_count == 0
    assert receipt.remaining_process_group_count == 0


def test_run_command_watcher_join_oserror_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed watcher drain observation cannot authenticate complete cleanup."""
    receipt_path = (tmp_path / "watcher-join-oserror.json").resolve()
    real_thread = threading.Thread
    watcher_instances: list[JoinErrorWatcher] = []

    class JoinErrorWatcher:
        def __init__(
            self,
            *,
            target: object,
            name: str,
            daemon: bool,
        ) -> None:
            assert callable(target)
            self._thread = real_thread(target=target, name=name, daemon=daemon)
            watcher_instances.append(self)

        def start(self) -> None:
            self._thread.start()

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            raise OSError("injected_watcher_join_failure")

        def is_alive(self) -> bool:
            return self._thread.is_alive()

        def wait_for_exit(self) -> None:
            self._thread.join(timeout=5)

    monkeypatch.setattr(command_runner.threading, "Thread", JoinErrorWatcher)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "watcher_join_oserror",
            (sys.executable, "-c", "import time; time.sleep(0.05)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )

    for watcher in watcher_instances:
        watcher.wait_for_exit()
    assert caught.value.stderr == "process_cleanup_unknown"
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None
    assert caught.value.cleanup_identity_evidence_status == "observation-unknown"
    assert caught.value.cleanup_unknown_reason == "watcher_drain_unknown"
    assert caught.value.cleanup_identity_receipt_sha256 is not None
    unknown_receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=caught.value.cleanup_identity_receipt_sha256,
    )
    assert unknown_receipt is not None
    assert unknown_receipt.unknown_reason == "watcher_drain_unknown"
    assert unknown_receipt.watcher_drain_status == "unknown"
    assert unknown_receipt.coverage_status == "complete"
    assert unknown_receipt.remaining_process_count is None
    assert unknown_receipt.remaining_process_group_count is None


def test_run_command_watcher_capture_oserror_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed in-watcher admission observation cannot authenticate cleanup."""
    receipt_path = (tmp_path / "watcher-capture-oserror.json").resolve()
    original_capture = command_runner.RootBoundProcessCleanupAdmission.capture_scope

    def fail_watcher_capture(
        admission: command_runner.RootBoundProcessCleanupAdmission,
    ) -> command_runner.ProcessCleanupScope:
        if threading.current_thread().name.startswith("cmd-watch-"):
            raise OSError("injected_watcher_capture_failure")
        return original_capture(admission)

    monkeypatch.setattr(
        command_runner.RootBoundProcessCleanupAdmission,
        "capture_scope",
        fail_watcher_capture,
    )

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "watcher_capture_oserror",
            (sys.executable, "-c", "import time; time.sleep(0.05)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )

    assert caught.value.stderr == "process_cleanup_unknown"
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None
    assert caught.value.cleanup_identity_evidence_status == "observation-unknown"
    assert caught.value.cleanup_unknown_reason == "watcher_drain_unknown"
    assert caught.value.cleanup_coverage_stage == "watcher_capture"
    assert caught.value.cleanup_coverage_subreason == "capture_failure"
    assert caught.value.cleanup_identity_receipt_sha256 is not None
    unknown_receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=caught.value.cleanup_identity_receipt_sha256,
    )
    assert unknown_receipt is not None
    assert unknown_receipt.unknown_reason == "watcher_drain_unknown"
    assert unknown_receipt.watcher_drain_status == "unknown"
    assert unknown_receipt.coverage_status == "unknown"
    assert unknown_receipt.coverage_stage == "watcher_capture"
    assert unknown_receipt.coverage_subreason == "capture_failure"
    assert unknown_receipt.remaining_process_count is None
    assert unknown_receipt.remaining_process_group_count is None


@pytest.mark.parametrize("failed_snapshot", [1, 2])
def test_root_bound_scope_keeps_transient_checked_snapshot_failure_unknown(
    monkeypatch: pytest.MonkeyPatch,
    failed_snapshot: int,
) -> None:
    """A recovered first or second process-table failure cannot regain complete coverage."""
    root_pid = REUSED_PID + 1_200
    calls = 0

    class FakeProcess:
        pid = root_pid

        @staticmethod
        def poll() -> None:
            return None

    root_identity = command_runner.ProcessCleanupIdentity(
        pid=root_pid,
        pgid=root_pid,
        birth_identity="stable-root-birth",
        initial_state="running",
    )

    def checked_snapshot(
        observed_root_pid: int | None,
        observed_root_pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
    ]:
        nonlocal calls
        assert observed_root_pid == root_pid
        assert observed_root_pgid == root_pid
        calls += 1
        return (
            (root_pid,),
            (root_pid,),
            "unknown" if calls == failed_snapshot else "complete",
        )

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        assert pid == root_pid
        return command_runner.ProcessObservation(
            pid=root_pid,
            pgid=root_pid,
            birth_identity="stable-root-birth",
            state="running",
        )

    monkeypatch.setattr(
        command_runner,
        "_snapshot_tree_checked",
        checked_snapshot,
        raising=False,
    )
    monkeypatch.setattr(command_runner, "read_process_observation", observation)

    scope = command_runner.RootBoundProcessCleanupAdmission(
        process=FakeProcess(),  # type: ignore[arg-type]
        root_pgid=root_pid,
        root_identity=root_identity,
    ).capture_scope()

    assert calls == failed_snapshot
    assert scope.coverage_status == "unknown"
    assert scope.coverage_stage == ("first_snapshot" if failed_snapshot == 1 else "second_snapshot")
    assert scope.coverage_subreason == "snapshot_failure"
    assert scope.identities == (root_identity,)


@pytest.mark.parametrize(
    ("failed_snapshot", "expected_stage"),
    [(1, "post_exit_snapshot"), (2, "final_snapshot")],
)
def test_run_command_binds_post_exit_and_final_snapshot_failures(  # noqa: C901
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_snapshot: int,
    expected_stage: command_runner.CleanupCoverageStage,
) -> None:
    root_pid = REUSED_PGID
    snapshot_calls = 0
    receipt_path = (tmp_path / f"{expected_stage}.json").resolve()

    class ExitingProcess:
        pid = root_pid
        returncode: int | None = None
        poll_calls = 0

        def poll(self) -> int | None:
            self.poll_calls += 1
            if self.poll_calls <= ROOT_BINDING_POLL_COUNT:
                return None
            self.returncode = 0
            return 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            _ = timeout
            return "", ""

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    def fake_popen(*_args: object, **_kwargs: object) -> ExitingProcess:
        return ExitingProcess()

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        assert pid == root_pid
        return command_runner.ProcessObservation(
            pid=root_pid,
            pgid=root_pid,
            birth_identity="root-birth",
            state="running",
        )

    def capture_scope(
        _admission: command_runner.RootBoundProcessCleanupAdmission,
    ) -> command_runner.ProcessCleanupScope:
        return command_runner.ProcessCleanupScope(
            identities=(),
            tracked_pids=(root_pid,),
            tracked_pgids=(root_pid,),
            coverage_status="complete",
        )

    def checked_snapshot(
        observed_root_pid: int | None,
        observed_root_pgid: int | None,
    ) -> tuple[tuple[int, ...], tuple[int, ...], command_runner.CleanupCoverage]:
        nonlocal snapshot_calls
        assert observed_root_pid is None
        assert observed_root_pgid == root_pid
        snapshot_calls += 1
        return (
            (root_pid,),
            (root_pid,),
            "unknown" if snapshot_calls == failed_snapshot else "complete",
        )

    def root_group_id(_pid: int) -> int:
        return root_pid

    def empty_group(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (), True

    monkeypatch.setattr(command_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(command_runner.os, "getpgid", root_group_id)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner.RootBoundProcessCleanupAdmission,
        "capture_scope",
        capture_scope,
    )
    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", checked_snapshot)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        empty_group,
    )

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "snapshot_failure",
            ("ignored",),
            cwd=tmp_path,
            env=_env(tmp_path),
            cleanup_identity_receipt_path=receipt_path,
        )

    assert snapshot_calls == POST_AND_FINAL_SNAPSHOT_COUNT
    assert caught.value.cleanup_unknown_reason == "coverage_unknown"
    assert caught.value.cleanup_coverage_stage == expected_stage
    assert caught.value.cleanup_coverage_subreason == "snapshot_failure"
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=caught.value.cleanup_identity_receipt_sha256 or "",
    )
    assert receipt is not None
    assert receipt.coverage_stage == expected_stage
    assert receipt.coverage_subreason == "snapshot_failure"


def test_process_cleanup_scope_rejects_unknown_coverage_value() -> None:
    """A caller cannot publish an unrecognised cleanup-coverage state."""
    with pytest.raises(ValueError, match="cleanup scope coverage status"):
        command_runner.ProcessCleanupScope(
            identities=(),
            tracked_pids=(),
            tracked_pgids=(),
            coverage_status="invalid",  # type: ignore[arg-type]
        )


def test_run_command_keeps_recovered_watcher_process_table_failure_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later good watcher snapshot cannot erase one incomplete process-table view."""
    receipt_path = (tmp_path / "watcher-table-unknown.json").resolve()
    original_checked = command_runner._snapshot_tree_checked  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    injected = False
    injection_lock = threading.Lock()

    def checked_snapshot(
        root_pid: int | None,
        root_pgid: int | None,
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
        tuple[tuple[int, int], ...],
        tuple[command_runner.ProcessCleanupIdentity, ...],
    ]:
        nonlocal injected
        pids, pgids, coverage, pid_groups, discovered_identities = original_checked(
            root_pid,
            root_pgid,
        )
        if threading.current_thread().name.startswith("cmd-watch-"):
            with injection_lock:
                if not injected:
                    injected = True
                    return pids, pgids, "unknown", pid_groups, discovered_identities
        return pids, pgids, coverage, pid_groups, discovered_identities

    monkeypatch.setattr(
        command_runner,
        "_snapshot_tree_checked",
        checked_snapshot,
        raising=False,
    )

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "watcher_process_table_unknown",
            (sys.executable, "-c", "import time; time.sleep(0.08)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )

    assert injected is True
    assert caught.value.stderr == "process_cleanup_unknown"
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None
    assert caught.value.cleanup_identity_evidence_status == "observation-unknown"
    assert caught.value.cleanup_unknown_reason == "coverage_unknown"
    assert caught.value.cleanup_coverage_stage in {"first_snapshot", "second_snapshot"}
    assert caught.value.cleanup_coverage_subreason == "snapshot_failure"
    digest = caught.value.cleanup_identity_receipt_sha256
    assert digest is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=digest,
    )
    assert receipt is not None
    assert receipt.unknown_reason == "coverage_unknown"
    assert receipt.coverage_status == "unknown"
    assert receipt.coverage_stage == caught.value.cleanup_coverage_stage
    assert receipt.coverage_subreason == "snapshot_failure"
    assert receipt.remaining_process_count is None
    assert receipt.remaining_process_group_count is None


@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_run_command_terminal_target_observation_error_writes_unknown_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    """A terminal target read failure is contained and published as typed unknown evidence."""
    receipt_path = (tmp_path / "terminal-observation-unknown.json").resolve()

    def fail_terminal_observation(
        _identity: command_runner.ProcessCleanupIdentity,
    ) -> command_runner.ProcessCleanupTargetReceipt:
        raise failure_type("PRIVATE_TERMINAL_OBSERVATION_DETAIL")

    monkeypatch.setattr(
        command_runner,
        "observe_process_cleanup_target",
        fail_terminal_observation,
    )

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "terminal_target_observation_unknown",
            (sys.executable, "-c", "import time; time.sleep(0.05)"),
            cwd=tmp_path,
            env=_env(tmp_path),
            timeout_seconds=5,
            cleanup_identity_receipt_path=receipt_path,
        )

    assert caught.value.stderr == "process_cleanup_unknown"
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None
    assert caught.value.cleanup_identity_evidence_status == "observation-unknown"
    assert caught.value.cleanup_unknown_reason == "target_observation_unknown"
    assert caught.value.cleanup_coverage_stage == "target_observation"
    assert caught.value.cleanup_coverage_subreason == "observation_unknown"
    digest = caught.value.cleanup_identity_receipt_sha256
    assert digest is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=digest,
    )
    assert receipt is not None
    assert receipt.unknown_reason == "target_observation_unknown"
    assert receipt.coverage_status == "unknown"
    assert receipt.coverage_stage == "target_observation"
    assert receipt.coverage_subreason == "observation_unknown"
    assert receipt.remaining_process_count is None
    assert receipt.remaining_process_group_count is None
    assert "PRIVATE_TERMINAL" not in receipt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("drain_status", ["discarded", "still-running", "unknown"])
def test_watcher_drain_failure_makes_semantic_cleanup_unknown(
    drain_status: command_runner.WatcherDrainState,
) -> None:
    snapshot = command_runner.ProcessCleanupTerminalSnapshot(
        targets=(),
        coverage_status="complete",
        watcher_drain_status=drain_status,
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None


def test_unknown_cleanup_receipt_is_bound_private_and_rejects_reason_tamper(
    tmp_path: Path,
) -> None:
    receipt_path = (tmp_path / "unknown-cleanup.json").resolve()
    snapshot = command_runner.ProcessCleanupTerminalSnapshot(
        targets=(),
        coverage_status="complete",
        watcher_drain_status="discarded",
    )

    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="PRIVATE_COMMAND_DO_NOT_PUBLISH",
        argv=("/private/interpreter", "PRIVATE_ARGUMENT_DO_NOT_PUBLISH"),
        cwd=tmp_path,
        root_pid=101,
        root_pgid=101,
        terminal_snapshot=snapshot,
    )

    assert evidence.evidence_status == "observation-unknown"
    assert evidence.unknown_reason == "watcher_publication_discarded"
    assert evidence.receipt_sha256 is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=evidence.receipt_sha256,
    )
    assert receipt is not None
    assert receipt.unknown_reason == "watcher_publication_discarded"
    assert receipt.watcher_drain_status == "discarded"
    assert receipt.coverage_status == "complete"
    assert receipt.remaining_process_count is None
    assert receipt.remaining_process_group_count is None
    raw = receipt_path.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw
    assert str(tmp_path) not in raw
    assert receipt_path.stat().st_mode & 0o777 == OWNER_FILE_MODE

    payload = json.loads(raw)
    payload["unknown_reason"] = "watcher_still_running"
    material = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_unknown_receipt(
            receipt_path,
            expected_receipt_sha256=payload["receipt_sha256"],
        )
        is None
    )


@pytest.mark.parametrize(
    ("stage", "subreason"),
    [
        ("post_exit_snapshot", "snapshot_failure"),
        ("final_snapshot", "snapshot_failure"),
        ("group_table", "table_incomplete"),
        ("group_member", "uncaptured_member"),
        ("group_member", "changed_identity_or_group_member"),
        ("group_member", "observation_unknown"),
    ],
)
def test_unknown_cleanup_receipt_authenticates_allowlisted_coverage_diagnostic(
    tmp_path: Path,
    stage: command_runner.CleanupCoverageStage,
    subreason: command_runner.CleanupCoverageSubreason,
) -> None:
    """Every cleanup uncertainty class remains distinct in the digest-bound receipt."""
    receipt_path = (tmp_path / f"{stage}-{subreason}.json").resolve()
    observations: tuple[command_runner.ProcessCleanupOffendingObservation, ...] = ()
    if stage == "group_member":
        state: command_runner.CleanupObservationState = (
            "running"
            if subreason == "uncaptured_member"
            else "identity_changed"
            if subreason == "changed_identity_or_group_member"
            else "unknown"
        )
        observations = (
            command_runner.ProcessCleanupOffendingObservation(
                pid=113,
                pgid=111,
                expected_pgid=111,
                ppid=111,
                birth_identity_sha256="a" * 64,
                expected_birth_identity_sha256=("b" * 64 if state == "identity_changed" else None),
                detection_source="historical_pid_check",
                observation_state=state,
                admission_phase="admission_closed",
                occurrence_count=1,
                process_category=None,
                process_identity_sha256="c" * 64,
            ),
        )
    snapshot = command_runner.ProcessCleanupTerminalSnapshot(
        targets=(),
        coverage_status="unknown",
        coverage_stage=stage,
        coverage_subreason=subreason,
        offending_observations=observations,
    )

    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="coverage_diagnostic",
        argv=("python",),
        cwd=tmp_path,
        root_pid=111,
        root_pgid=111,
        terminal_snapshot=snapshot,
    )

    assert evidence.cleanup_coverage_stage == stage
    assert evidence.cleanup_coverage_subreason == subreason
    assert evidence.receipt_sha256 is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=evidence.receipt_sha256,
    )
    assert receipt is not None
    assert receipt.coverage_stage == stage
    assert receipt.coverage_subreason == subreason


def test_unknown_cleanup_receipt_rejects_diagnostic_tamper_and_reads_legacy(
    tmp_path: Path,
) -> None:
    receipt_path = (tmp_path / "coverage-diagnostic.json").resolve()
    observation = command_runner.ProcessCleanupOffendingObservation(
        pid=113,
        pgid=112,
        expected_pgid=112,
        ppid=112,
        birth_identity_sha256="a" * 64,
        expected_birth_identity_sha256=None,
        detection_source="historical_pid_check",
        observation_state="running",
        admission_phase="admission_closed",
        occurrence_count=1,
        process_category=None,
        process_identity_sha256="b" * 64,
    )
    snapshot = command_runner.ProcessCleanupTerminalSnapshot(
        targets=(),
        coverage_status="unknown",
        coverage_stage="group_member",
        coverage_subreason="uncaptured_member",
        offending_observations=(observation,),
    )
    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="coverage_diagnostic",
        argv=("python",),
        cwd=tmp_path,
        root_pid=112,
        root_pgid=112,
        terminal_snapshot=snapshot,
    )
    assert evidence.receipt_sha256 is not None
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["coverage_subreason"] = "changed_identity_or_group_member"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_unknown_receipt(
            receipt_path,
            expected_receipt_sha256=evidence.receipt_sha256,
        )
        is None
    )

    legacy = dict(payload)
    legacy["coverage_subreason"] = "uncaptured_member"
    legacy.pop("coverage_stage")
    legacy.pop("coverage_subreason")
    material = {key: value for key, value in legacy.items() if key != "receipt_sha256"}
    legacy["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(legacy), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    verified = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=legacy["receipt_sha256"],
    )
    assert verified is not None
    assert verified.coverage_stage is None
    assert verified.coverage_subreason is None


def test_current_cleanup_evidence_rejects_missing_or_mismatched_diagnostic() -> None:
    """Only a parsed legacy receipt may omit coverage diagnostics."""
    with pytest.raises(ValueError, match="coverage-unknown"):
        command_runner.CommandCleanupIdentityEvidence(
            evidence_status="observation-unknown",
            receipt_sha256="d" * 64,
            unknown_reason="coverage_unknown",
        )
    with pytest.raises(ValueError, match="target-observation"):
        command_runner.CommandCleanupIdentityEvidence(
            evidence_status="observation-unknown",
            receipt_sha256="d" * 64,
            unknown_reason="target_observation_unknown",
            cleanup_coverage_stage="group_member",
            cleanup_coverage_subreason="observation_unknown",
        )


def test_persistent_late_descendant_is_private_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late member is never signalled and survives as correlated private evidence."""
    leader_pid = 7101
    late_pid = 7102
    leader_running = True
    signals: list[tuple[int, signal.Signals]] = []
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-birth",
        initial_state="running",
    )

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == leader_pid:
            if not leader_running:
                return None
            return command_runner.ProcessObservation(
                pid=pid,
                pgid=leader_pid,
                ppid=1,
                birth_identity="leader-birth",
                state="running",
                process_category=None,
                process_identity_sha256="a" * 64,
            )
        assert pid == late_pid
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=leader_pid,
            ppid=leader_pid,
            birth_identity="late-birth",
            state="running",
            process_category="process_observer",
            process_identity_sha256=None,
        )

    def group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
        assert pgid == leader_pid
        return (leader_pid, late_pid), True

    def record_signal(pid: int, sig: signal.Signals) -> None:
        nonlocal leader_running
        signals.append((pid, sig))
        if pid == leader_pid:
            leader_running = False

    def skip_wait(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", skip_wait)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader,),
        tracked_pids=(leader_pid,),
        tracked_pgids=(leader_pid,),
    )

    assert all(pid != late_pid for pid, _sig in signals)
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert tuple(item.pid for item in snapshot.offending_observations) == (late_pid, late_pid)
    assert tuple(item.detection_source for item in snapshot.offending_observations) == (
        "pre_signal_group_scan",
        "post_signal_group_rescan",
    )
    assert all(
        item.admission_phase == "admission_closed" for item in snapshot.offending_observations
    )
    assert all(
        item.process_category == "process_observer" and item.process_identity_sha256 is None
        for item in snapshot.offending_observations
    )

    receipt_path = (tmp_path / "late-private.json").resolve()
    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="PRIVATE_COMMAND_DO_NOT_PUBLISH",
        argv=("/private/interpreter", "PRIVATE_ARGUMENT_DO_NOT_PUBLISH"),
        cwd=tmp_path,
        root_pid=leader_pid,
        root_pgid=leader_pid,
        terminal_snapshot=snapshot,
    )
    assert evidence.receipt_sha256 is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=evidence.receipt_sha256,
    )
    assert receipt is not None
    assert receipt.offending_observations == snapshot.offending_observations
    assert receipt.remaining_process_count is None
    assert receipt.remaining_process_group_count is None
    assert receipt_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    raw = receipt_path.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw
    assert str(tmp_path) not in raw


@pytest.mark.parametrize(
    ("command_name", "expected_category"),
    [("ps", "process_observer"), ("/private/runtime/python3", None)],
)
def test_process_observation_reduces_command_identity_to_category_or_hash(
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    expected_category: command_runner.CleanupProcessCategory | None,
) -> None:
    """The ps observer is classed safely; every other command is retained only as a hash."""
    parent_pid = 101
    process_group_id = 202
    process_id = 303
    completed = subprocess.CompletedProcess(
        args=("ps",),
        returncode=0,
        stdout=(
            f"{parent_pid} {process_group_id} Rs   Wed Aug 19 14:46:16 2026     {command_name}\n"
        ),
        stderr="",
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return completed

    monkeypatch.setattr(command_runner.subprocess, "run", fake_run)

    observation = command_runner.read_process_observation(process_id)

    assert observation is not None
    assert observation.pid == process_id
    assert observation.ppid == parent_pid
    assert observation.pgid == process_group_id
    assert observation.birth_identity == "Wed Aug 19 14:46:16 2026"
    assert observation.process_category == expected_category
    if expected_category is None:
        assert (
            observation.process_identity_sha256
            == hashlib.sha256(
                command_name.encode(),
            ).hexdigest()
        )
    else:
        assert observation.process_identity_sha256 is None
    assert command_name not in repr(observation.process_identity_sha256)


def test_vanished_uncaptured_member_retains_absent_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader_pid = 7201
    vanished_pid = 7202
    leader = command_runner.ProcessCleanupIdentity(
        pid=leader_pid,
        pgid=leader_pid,
        birth_identity="leader-birth",
        initial_state="running",
    )
    group_scans = 0
    leader_running = True

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        if pid == vanished_pid:
            return None
        if not leader_running:
            return None
        return command_runner.ProcessObservation(
            pid=leader_pid,
            pgid=leader_pid,
            ppid=1,
            birth_identity="leader-birth",
            state="running",
            process_identity_sha256="b" * 64,
        )

    def group_members(_pgid: int) -> tuple[tuple[int, ...], bool]:
        nonlocal group_scans
        group_scans += 1
        return ((leader_pid, vanished_pid) if group_scans == 1 else ()), True

    def record_signal(pid: int, _sig: signal.Signals) -> None:
        nonlocal leader_running
        if pid == leader_pid:
            leader_running = False

    def skip_wait(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", skip_wait)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (leader,),
        tracked_pids=(leader_pid,),
        tracked_pgids=(leader_pid,),
    )

    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert len(snapshot.offending_observations) == 1
    vanished = snapshot.offending_observations[0]
    assert vanished.pid == vanished_pid
    assert vanished.pgid == leader_pid
    assert vanished.ppid is None
    assert vanished.birth_identity_sha256 is None
    assert vanished.observation_state == "absent"
    assert vanished.detection_source == "pre_signal_group_scan"


def test_reused_pid_and_pgid_are_hashed_and_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 7301
    identity = command_runner.ProcessCleanupIdentity(
        pid=pid,
        pgid=pid,
        birth_identity="old-birth",
        initial_state="running",
    )
    signals: list[int] = []

    def observation(_pid: int) -> command_runner.ProcessObservation:
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=pid,
            ppid=1,
            birth_identity="replacement-birth",
            state="running",
            process_identity_sha256="c" * 64,
        )

    def group_members(_pgid: int) -> tuple[tuple[int, ...], bool]:
        return (pid,), True

    def record_signal(target: int, _sig: signal.Signals) -> None:
        signals.append(target)

    def skip_wait(_seconds: float) -> None:
        return

    monkeypatch.setattr(command_runner, "read_process_observation", observation)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", skip_wait)

    snapshot = command_runner.cleanup_birth_bound_processes(
        (identity,),
        tracked_pids=(pid,),
        tracked_pgids=(pid,),
    )

    assert signals == []
    assert snapshot.cleanup_status == "unknown"
    assert snapshot.remaining_process_count is None
    assert snapshot.remaining_process_group_count is None
    assert tuple(item.observation_state for item in snapshot.offending_observations) == (
        "identity_changed",
        "identity_changed",
    )
    assert all(item.pid == pid and item.pgid == pid for item in snapshot.offending_observations)
    assert all(
        item.birth_identity_sha256 == hashlib.sha256(b"replacement-birth").hexdigest()
        for item in snapshot.offending_observations
    )


def test_unknown_cleanup_observation_schema_rejects_tamper_extra_and_reads_legacy(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    receipt_path = (tmp_path / "private-observations.json").resolve()
    observation = command_runner.ProcessCleanupOffendingObservation(
        pid=7401,
        pgid=7400,
        expected_pgid=7400,
        ppid=7400,
        birth_identity_sha256="d" * 64,
        expected_birth_identity_sha256=None,
        detection_source="historical_pid_check",
        observation_state="running",
        admission_phase="admission_closed",
        occurrence_count=1,
        process_category=None,
        process_identity_sha256="e" * 64,
    )
    snapshot = command_runner.ProcessCleanupTerminalSnapshot(
        targets=(),
        coverage_status="unknown",
        coverage_stage="group_member",
        coverage_subreason="uncaptured_member",
        offending_observations=(observation,),
    )
    evidence = command_runner.write_command_cleanup_identity_receipt(
        receipt_path,
        name="PRIVATE_COMMAND",
        argv=("/private/runtime", "PRIVATE_ARGUMENT"),
        cwd=tmp_path,
        root_pid=7400,
        root_pgid=7400,
        terminal_snapshot=snapshot,
    )
    digest = evidence.receipt_sha256
    assert digest is not None
    original = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert original["observation_evidence_state"] == "current"

    tampered = json.loads(json.dumps(original))
    tampered["offending_observations"][0]["pid"] = 9999
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_unknown_receipt(
            receipt_path,
            expected_receipt_sha256=digest,
        )
        is None
    )

    extra = json.loads(json.dumps(original))
    extra["offending_observations"][0]["raw_command"] = "/private/do-not-publish"
    material = {key: value for key, value in extra.items() if key != "receipt_sha256"}
    extra["receipt_sha256"] = hashlib.sha256(
        json.dumps(material, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(extra), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_unknown_receipt(
            receipt_path,
            expected_receipt_sha256=extra["receipt_sha256"],
        )
        is None
    )

    prior_current_shape = json.loads(json.dumps(original))
    prior_current_shape.pop("observation_evidence_state")
    material = {key: value for key, value in prior_current_shape.items() if key != "receipt_sha256"}
    prior_current_shape["receipt_sha256"] = hashlib.sha256(
        json.dumps(material, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(prior_current_shape), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    prior_current = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=prior_current_shape["receipt_sha256"],
    )
    assert prior_current is not None
    assert prior_current.observation_evidence_state == "legacy-current"
    assert prior_current.offending_observations is not None
    assert prior_current.offending_observations[0].occurrence_count == 1

    prior_observation_shape = json.loads(json.dumps(original))
    for field in (
        "expected_pgid",
        "expected_birth_identity_sha256",
        "occurrence_count",
    ):
        prior_observation_shape["offending_observations"][0].pop(field)
    prior_observation_shape.pop("observation_evidence_state")
    material = {
        key: value for key, value in prior_observation_shape.items() if key != "receipt_sha256"
    }
    prior_observation_shape["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(prior_observation_shape), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    prior_verified = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=prior_observation_shape["receipt_sha256"],
    )
    assert prior_verified is not None
    assert prior_verified.offending_observations is not None
    assert prior_verified.observation_evidence_state == "historical"
    assert prior_verified.offending_observations[0].expected_pgid is None
    assert prior_verified.offending_observations[0].occurrence_count is None

    mixed = json.loads(json.dumps(original))
    mixed.pop("observation_evidence_state")
    historical_observation = json.loads(json.dumps(mixed["offending_observations"][0]))
    for field in (
        "expected_pgid",
        "expected_birth_identity_sha256",
        "occurrence_count",
    ):
        historical_observation.pop(field)
    mixed["offending_observations"].append(historical_observation)
    material = {key: value for key, value in mixed.items() if key != "receipt_sha256"}
    mixed["receipt_sha256"] = hashlib.sha256(
        json.dumps(material, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(mixed), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_command_cleanup_unknown_receipt(
            receipt_path,
            expected_receipt_sha256=mixed["receipt_sha256"],
        )
        is None
    )

    legacy = json.loads(json.dumps(original))
    legacy.pop("offending_observations")
    legacy.pop("observation_evidence_state")
    material = {key: value for key, value in legacy.items() if key != "receipt_sha256"}
    legacy["receipt_sha256"] = hashlib.sha256(
        json.dumps(material, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(legacy), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    verified = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=legacy["receipt_sha256"],
    )
    assert verified is not None
    assert verified.observation_evidence_state == "none"
    assert verified.offending_observations is None
    raw = receipt_path.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw
    assert str(tmp_path) not in raw


def test_run_command_binds_scope_observations_only_into_private_unknown_receipt(  # noqa: C901
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_pid = 7500
    offender = command_runner.ProcessCleanupOffendingObservation(
        pid=7501,
        pgid=root_pid,
        expected_pgid=root_pid + 2,
        ppid=root_pid,
        birth_identity_sha256="f" * 64,
        expected_birth_identity_sha256="e" * 64,
        detection_source="second_snapshot",
        observation_state="group_changed",
        admission_phase="admission_open",
        occurrence_count=1,
        process_category=None,
        process_identity_sha256="a" * 64,
    )
    receipt_path = (tmp_path / "scope-observation.json").resolve()

    class ExitingProcess:
        pid = root_pid
        returncode: int | None = None
        poll_calls = 0

        def poll(self) -> int | None:
            self.poll_calls += 1
            if self.poll_calls <= ROOT_BINDING_POLL_COUNT:
                return None
            self.returncode = 0
            return 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            _ = timeout
            return "", ""

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

        def is_alive(self) -> bool:
            return False

    def capture_scope(
        _admission: command_runner.RootBoundProcessCleanupAdmission,
    ) -> command_runner.ProcessCleanupScope:
        return command_runner.ProcessCleanupScope(
            identities=(),
            tracked_pids=(root_pid, offender.pid),
            tracked_pgids=(root_pid,),
            coverage_status="unknown",
            coverage_stage="group_member",
            coverage_subreason="changed_identity_or_group_member",
            offending_observations=(offender,),
        )

    def unknown_cleanup(  # noqa: PLR0913
        _identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        tracked_pid_groups: tuple[tuple[int, int], ...] = (),
        coverage_status: command_runner.CleanupCoverage = "complete",
        coverage_stage: command_runner.CleanupCoverageStage | None = None,
        coverage_subreason: command_runner.CleanupCoverageSubreason | None = None,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        del tracked_pids, tracked_pgids, tracked_pid_groups
        assert coverage_status == "unknown"
        return command_runner.ProcessCleanupTerminalSnapshot(
            targets=(),
            coverage_status=coverage_status,
            coverage_stage=coverage_stage,
            coverage_subreason=coverage_subreason,
        )

    def fake_popen(*_args: object, **_kwargs: object) -> ExitingProcess:
        return ExitingProcess()

    def fake_getpgid(_pid: int) -> int:
        return root_pid

    def root_observation(pid: int) -> command_runner.ProcessObservation:
        return command_runner.ProcessObservation(
            pid=pid,
            pgid=root_pid,
            birth_identity="root-birth",
            state="running",
        )

    def empty_snapshot(
        _pid: int | None,
        _pgid: int | None,
    ) -> tuple[tuple[int, ...], tuple[int, ...], command_runner.CleanupCoverage]:
        return (), (), "complete"

    monkeypatch.setattr(
        command_runner.subprocess,
        "Popen",
        fake_popen,
    )
    monkeypatch.setattr(command_runner.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(
        command_runner,
        "read_process_observation",
        root_observation,
    )
    monkeypatch.setattr(
        command_runner.RootBoundProcessCleanupAdmission,
        "capture_scope",
        capture_scope,
    )
    monkeypatch.setattr(
        command_runner,
        "_snapshot_tree_checked",
        empty_snapshot,
    )
    monkeypatch.setattr(command_runner, "cleanup_birth_bound_processes", unknown_cleanup)

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "private_scope_probe",
            ("ignored",),
            cwd=tmp_path,
            env=_env(tmp_path),
            cleanup_identity_receipt_path=receipt_path,
        )

    digest = caught.value.cleanup_identity_receipt_sha256
    assert digest is not None
    receipt = command_runner.verify_command_cleanup_unknown_receipt(
        receipt_path,
        expected_receipt_sha256=digest,
    )
    assert receipt is not None
    assert receipt.offending_observations == (offender.model_copy(update={"occurrence_count": 2}),)
    assert caught.value.remaining_process_count is None
    assert caught.value.remaining_process_group_count is None


def test_cleanup_observations_bind_expected_identity_and_multiplicity() -> None:
    """Changed-process evidence must prove what changed and how often it was seen."""
    expected_birth = hashlib.sha256(b"expected-birth").hexdigest()
    observed_birth = hashlib.sha256(b"observed-birth").hexdigest()
    changed_group = command_runner.ProcessCleanupOffendingObservation(
        pid=7601,
        pgid=7602,
        expected_pgid=7600,
        ppid=7600,
        birth_identity_sha256=observed_birth,
        expected_birth_identity_sha256=expected_birth,
        detection_source="post_signal_group_rescan",
        observation_state="group_changed",
        admission_phase="admission_closed",
        occurrence_count=1,
        process_category=None,
        process_identity_sha256="a" * 64,
    )
    changed_identity = command_runner.ProcessCleanupOffendingObservation(
        pid=7603,
        pgid=7600,
        expected_pgid=7600,
        ppid=7600,
        birth_identity_sha256=observed_birth,
        expected_birth_identity_sha256=expected_birth,
        detection_source="historical_pid_check",
        observation_state="identity_changed",
        admission_phase="admission_closed",
        occurrence_count=1,
        process_category=None,
        process_identity_sha256="b" * 64,
    )

    combined = command_runner._dedupe_offending_observations(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        [changed_group, changed_group, changed_identity],
    )

    assert len(combined) == EXPECTED_DEDUPED_OBSERVATION_COUNT
    assert combined[0].expected_pgid == changed_group.expected_pgid
    assert combined[0].pgid == changed_group.pgid
    assert combined[0].occurrence_count == DUPLICATE_OCCURRENCE_COUNT
    assert combined[1].expected_birth_identity_sha256 == expected_birth
    assert combined[1].birth_identity_sha256 == observed_birth
    assert combined[1].occurrence_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "pid": 7701,
            "pgid": 7700,
            "expected_pgid": 7700,
            "ppid": 7700,
            "birth_identity_sha256": "c" * 64,
            "expected_birth_identity_sha256": "d" * 64,
            "detection_source": "post_signal_group_rescan",
            "observation_state": "group_changed",
            "admission_phase": "admission_closed",
            "occurrence_count": 1,
            "process_category": None,
            "process_identity_sha256": "e" * 64,
        },
        {
            "pid": 7702,
            "pgid": 7700,
            "expected_pgid": 7700,
            "ppid": 7700,
            "birth_identity_sha256": "f" * 64,
            "expected_birth_identity_sha256": "f" * 64,
            "detection_source": "historical_pid_check",
            "observation_state": "identity_changed",
            "admission_phase": "admission_closed",
            "occurrence_count": 1,
            "process_category": None,
            "process_identity_sha256": "a" * 64,
        },
        {
            "pid": 7703,
            "pgid": 7700,
            "expected_pgid": 7700,
            "ppid": 7700,
            "birth_identity_sha256": "b" * 64,
            "detection_source": "historical_pid_check",
            "observation_state": "running",
            "admission_phase": "admission_closed",
            "process_category": None,
            "process_identity_sha256": "c" * 64,
        },
    ],
)
def test_cleanup_observation_rejects_uncorrelated_changed_state(
    payload: dict[str, object],
) -> None:
    """Changed-group and changed-identity states require a real before/after delta."""
    with pytest.raises(ValueError, match=r"changed-(group|identity)|correlation fields"):
        command_runner.ProcessCleanupOffendingObservation.model_validate(payload, strict=True)


def test_current_group_member_unknown_receipt_requires_correlated_observation() -> None:
    """A current process-caused unknown receipt cannot authenticate an empty observation set."""
    material: dict[str, object] = {
        "schema_version": "1",
        "receipt_kind": "command_cleanup_unknown",
        "command_identity_sha256": "a" * 64,
        "root_pid": 7800,
        "root_pgid": 7800,
        "unknown_reason": "coverage_unknown",
        "watcher_drain_status": "drained",
        "coverage_status": "unknown",
        "coverage_stage": "group_member",
        "coverage_subreason": "uncaptured_member",
        "target_count": 1,
        "remaining_process_count": None,
        "remaining_process_group_count": None,
        "offending_observations": (),
    }
    material["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in material.items() if key != "receipt_sha256"},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()

    with pytest.raises(ValueError, match="group-member cleanup evidence"):
        command_runner.CommandCleanupUnknownReceipt.model_validate(material, strict=True)
