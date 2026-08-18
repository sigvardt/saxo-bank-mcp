from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import saxo_bank_mcp.agent_skill_command_runner as command_runner

MIN_ESCAPED_TARGET_COUNT = 2
MIN_GROUP_SCANS = 2
OWNER_FILE_MODE = 0o600
REUSED_PID = 4242
REUSED_PGID = 4343
LEADER_REUSE_AFTER_READS = 2


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PRIVATE_SENTINEL": "DO_NOT_PUBLISH",
    }


def test_escaped_session_cleanup_writes_owner_only_identity_receipt(tmp_path: Path) -> None:
    receipt_path = (tmp_path / "escaped-cleanup.json").resolve()
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
        "time.sleep(0.35); sys.exit(3)"
    )

    with pytest.raises(command_runner.CommandFailureError) as caught:
        command_runner.run_command(
            "escaped_identity_probe",
            (sys.executable, "-c", parent_code),
            cwd=tmp_path,
            env=_env(tmp_path),
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
    assert snapshot.cleanup_status == "complete"
    assert snapshot.remaining_process_count == 0
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
        tracked_pgids=(REUSED_PGID,),
    )

    assert snapshot.cleanup_status == "unknown"
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

    def counted_cleanup(
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
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
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        _ = (root_pid, pgid)
        raise OSError("injected_post_spawn_observation_failure")

    def counted_cleanup(
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        *,
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_roots.extend(identity.pid for identity in identities)
        return original_cleanup(
            identities,
            tracked_pids=tracked_pids,
            tracked_pgids=tracked_pgids,
        )

    monkeypatch.setattr(command_runner, "_snapshot_tree", broken_snapshot)
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
    assert caught.value.remaining_process_count == 0
    assert caught.value.remaining_process_group_count == 0
    assert caught.value.cleanup_identity_evidence_status == "authenticated"
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
    assert unknown.receipt_sha256 is None
    assert not receipt_path.exists()

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
