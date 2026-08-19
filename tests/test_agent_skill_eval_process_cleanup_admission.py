from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import threading
from pathlib import Path

import pytest

import saxo_bank_mcp.agent_skill_command_runner as command_runner
import saxo_bank_mcp.agent_skill_eval_process as eval_process
import saxo_bank_mcp.agent_skill_eval_runner as eval_runner

ROOT_PID = 73_100
CHILD_PID = ROOT_PID + 1
MOVED_PGID = ROOT_PID + 2
MINIMUM_ROOT_CHECKS = 3
REQUIRED_SCOPE_SNAPSHOTS = 2
EXPECTED_OBSERVATION_COUNT = 2
OWNER_FILE_MODE = 0o600


def test_retained_cleanup_publication_is_atomic_no_clobber_and_directory_synced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = (tmp_path / "retained-cleanup.json").resolve()
    payloads = {"a": "first\n", "b": "second\n"}
    barrier = threading.Barrier(3)
    results: dict[str, bool] = {}
    directory_syncs: list[int] = []
    original_fsync = os.fsync
    creator = command_runner._atomic_owner_only_create  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    def record_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs.append(descriptor)
        original_fsync(descriptor)

    def publish(label: str) -> None:
        barrier.wait()
        results[label] = creator(destination, payloads[label])

    monkeypatch.setattr(command_runner.os, "fsync", record_fsync)
    threads = tuple(
        threading.Thread(target=publish, args=(label,), daemon=True) for label in payloads
    )
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(results.values()) == 1
    assert destination.read_text(encoding="utf-8") in payloads.values()
    assert destination.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert destination.stat().st_nlink == 1
    assert directory_syncs


@pytest.mark.parametrize(
    ("transition", "expected_created", "expected_cleanup", "child_signaled"),
    [
        ("stable", 2, "passed", True),
        ("moved", 1, "unknown", False),
        ("replaced", 1, "unknown", False),
        ("missing", 1, "passed", False),
        ("unknown", 1, "unknown", False),
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
    ) -> tuple[
        tuple[int, ...],
        tuple[int, ...],
        command_runner.CleanupCoverage,
    ]:
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
        return visible, (ROOT_PID, MOVED_PGID), "complete"

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
            ppid=ROOT_PID,
            birth_identity=child_birth,
            state="unknown" if child_unknown else "running",
            process_category="process_observer" if child_unknown else None,
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
    monkeypatch.setattr(command_runner, "_snapshot_tree_checked", fake_snapshot)
    monkeypatch.setattr(command_runner, "read_process_observation", fake_observation)
    monkeypatch.setattr(command_runner, "process_group_members", group_members)
    monkeypatch.setattr(
        command_runner,
        "process_group_members_with_coverage",
        group_members_with_coverage,
    )
    monkeypatch.setattr(command_runner, "_signal_pid", record_signal)
    monkeypatch.setattr(command_runner.time, "sleep", skip_wait)

    manager = eval_process.EvalProcessManager()
    result = manager.run(
        ("/bin/true",),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        timeout_seconds=1,
    )

    assert process.poll_calls >= MINIMUM_ROOT_CHECKS
    assert snapshot_calls >= REQUIRED_SCOPE_SNAPSHOTS
    assert result.created_processes == expected_created
    assert result.process_cleanup == expected_cleanup
    if expected_cleanup == "unknown":
        assert result.remaining_processes is None
    terminal = manager._cleanup_snapshots[0]  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    if transition in {"moved", "replaced", "unknown"}:
        assert terminal.offending_observations
        assert any(
            item.detection_source == "second_snapshot" and item.admission_phase == "admission_open"
            for item in terminal.offending_observations
        )
        if transition == "unknown":
            assert any(
                item.process_category == "process_observer" and item.process_identity_sha256 is None
                for item in terminal.offending_observations
            )
    else:
        assert terminal.offending_observations == ()
    assert any(pid == CHILD_PID for pid, _sig in signals) is child_signaled


@pytest.mark.parametrize("with_known_zero", [False, True])
def test_nested_eval_cleanup_keeps_unknown_remaining_count_nullable(
    with_known_zero: bool,  # noqa: FBT001
) -> None:
    """Unknown nested cleanup never becomes zero through filtering or empty summation."""
    manager = eval_process.EvalProcessManager()
    cleanup_snapshots = manager._cleanup_snapshots  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    unknown = command_runner.ProcessCleanupTerminalSnapshot(
        targets=(),
        coverage_status="unknown",
        coverage_stage="target_observation",
        coverage_subreason="observation_unknown",
    )
    if with_known_zero:
        cleanup_snapshots.append(
            command_runner.ProcessCleanupTerminalSnapshot(
                targets=(),
                coverage_status="complete",
            ),
        )
    cleanup_snapshots.append(unknown)

    snapshot = manager.finalize()

    assert manager.remaining_processes is None
    assert manager.process_cleanup == "unknown"
    assert snapshot["remaining_processes"] is None
    assert snapshot["process_cleanup"] == "unknown"


def test_nested_eval_cleanup_with_no_terminal_snapshot_is_unknown() -> None:
    """A started nested process without terminal evidence cannot publish passed cleanup."""
    manager = eval_process.EvalProcessManager(process_cleanup="pending")

    snapshot = manager.finalize()

    assert manager.remaining_processes is None
    assert manager.process_cleanup == "unknown"
    assert snapshot["remaining_processes"] is None
    assert snapshot["process_cleanup"] == "unknown"


@pytest.mark.parametrize("failure", [ProcessLookupError(), OSError("post-spawn failure")])
def test_nested_eval_records_pending_cleanup_before_post_spawn_scope_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    """A successful Popen is counted before PGID or scope capture can fail."""

    class FakeProcess:
        pid = ROOT_PID
        returncode: int | None = None

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(eval_process.subprocess, "Popen", fake_popen)

    def fail_getpgid(_pid: int) -> int:
        raise failure

    monkeypatch.setattr(eval_process.os, "getpgid", fail_getpgid)
    manager = eval_process.EvalProcessManager()

    with pytest.raises(type(failure)):
        manager.run(
            ("/bin/true",),
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
            timeout_seconds=1,
        )

    snapshot = manager.finalize()
    assert snapshot["created_processes"] == 1
    assert snapshot["terminated_processes"] == 0
    assert snapshot["remaining_processes"] is None
    assert snapshot["process_cleanup"] == "unknown"


def test_nested_eval_cleanup_writes_digest_bound_private_observation_receipt(
    tmp_path: Path,
) -> None:
    """Nested offending observations survive only in one owner-only authenticated receipt."""
    receipt_path = (tmp_path / "nested-cleanup.json").resolve()
    expected_birth = "a" * 64
    observed_birth = "b" * 64
    offender = command_runner.ProcessCleanupOffendingObservation(
        pid=74_001,
        pgid=74_002,
        expected_pgid=74_000,
        ppid=74_000,
        birth_identity_sha256=observed_birth,
        expected_birth_identity_sha256=expected_birth,
        detection_source="second_snapshot",
        observation_state="group_changed",
        admission_phase="admission_open",
        occurrence_count=2,
        process_category=None,
        process_identity_sha256="c" * 64,
    )
    manager = eval_process.EvalProcessManager(cleanup_receipt_path=receipt_path)
    manager._cleanup_snapshots.append(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        command_runner.ProcessCleanupTerminalSnapshot(
            targets=(),
            coverage_status="unknown",
            coverage_stage="group_member",
            coverage_subreason="changed_identity_or_group_member",
            offending_observations=(offender,),
        ),
    )

    snapshot = manager.finalize()

    digest = snapshot["process_cleanup_receipt_sha256"]
    assert isinstance(digest, str)
    assert snapshot["process_cleanup_evidence_status"] == "observation-unknown"
    assert snapshot["process_cleanup_unknown_reason"] == "coverage_unknown"
    cleanup_fields = eval_runner._cleanup_fields(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
        process_manager=manager,
        process_error="process_cleanup_unknown",
        promote_error=None,
        cleanup_error=None,
    )
    assert cleanup_fields["process_cleanup_receipt_sha256"] == digest
    assert cleanup_fields["process_cleanup_evidence_status"] == "observation-unknown"
    receipt = command_runner.verify_eval_process_cleanup_receipt(
        receipt_path,
        expected_receipt_sha256=digest,
    )
    assert receipt is not None
    assert receipt.snapshot_count == 1
    assert receipt.offending_observation_count == EXPECTED_OBSERVATION_COUNT
    assert receipt.offending_observations == (offender,)
    assert receipt_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    raw = receipt_path.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw
    assert str(tmp_path) not in raw

    diagnostic_tamper = json.loads(raw)
    diagnostic_tamper["cleanup_snapshots"][0]["coverage_subreason"] = "uncaptured_member"
    diagnostic_material = {
        key: value for key, value in diagnostic_tamper.items() if key != "receipt_sha256"
    }
    diagnostic_tamper["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            diagnostic_material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(diagnostic_tamper), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_eval_process_cleanup_receipt(
            receipt_path,
            expected_receipt_sha256=diagnostic_tamper["receipt_sha256"],
        )
        is None
    )

    tampered = json.loads(raw)
    tampered["offending_observations"][0]["occurrence_count"] = 3
    tampered_material = {key: value for key, value in tampered.items() if key != "receipt_sha256"}
    tampered["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            tampered_material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    receipt_path.chmod(OWNER_FILE_MODE)
    assert (
        command_runner.verify_eval_process_cleanup_receipt(
            receipt_path,
            expected_receipt_sha256=tampered["receipt_sha256"],
        )
        is None
    )

    receipt_path.write_text(raw, encoding="utf-8")
    receipt_path.chmod(0o644)
    assert (
        command_runner.verify_eval_process_cleanup_receipt(
            receipt_path,
            expected_receipt_sha256=digest,
        )
        is None
    )

    receipt_path.chmod(OWNER_FILE_MODE)
    retained_path = (tmp_path / "retained-cleanup.json").resolve()
    retained = command_runner.retain_eval_process_cleanup_receipt(
        receipt_path,
        retained_path,
        expected_receipt_sha256=digest,
    )
    assert retained is not None
    assert retained.receipt_sha256 == digest
    assert retained_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    retained_bytes = retained_path.read_bytes()
    assert (
        command_runner.retain_eval_process_cleanup_receipt(
            receipt_path,
            retained_path,
            expected_receipt_sha256=digest,
        )
        is None
    )
    assert retained_path.read_bytes() == retained_bytes


def test_nested_eval_merge_preserves_repeated_observation_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same observation at admission and cleanup remains one item with count two."""
    offender = command_runner.ProcessCleanupOffendingObservation(
        pid=75_001,
        pgid=75_000,
        expected_pgid=75_000,
        ppid=75_000,
        birth_identity_sha256="a" * 64,
        expected_birth_identity_sha256=None,
        detection_source="historical_pid_check",
        observation_state="running",
        admission_phase="admission_closed",
        occurrence_count=1,
        process_category=None,
        process_identity_sha256="b" * 64,
    )

    class FakeProcess:
        pid = 75_000
        returncode: int | None = 0

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            _ = timeout
            return "", ""

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    def fake_getpgid(_pid: int) -> int:
        return 75_000

    def fake_scope(
        _process: FakeProcess,
        _pgid: int,
    ) -> command_runner.ProcessCleanupScope:
        return command_runner.ProcessCleanupScope(
            identities=(),
            tracked_pids=(75_000, 75_001),
            tracked_pgids=(75_000,),
            coverage_status="unknown",
            coverage_stage="group_member",
            coverage_subreason="uncaptured_member",
            offending_observations=(offender,),
        )

    def fake_cleanup(
        _identities: tuple[command_runner.ProcessCleanupIdentity, ...],
        **_kwargs: object,
    ) -> command_runner.ProcessCleanupTerminalSnapshot:
        return command_runner.ProcessCleanupTerminalSnapshot(
            targets=(),
            coverage_status="unknown",
            coverage_stage="group_member",
            coverage_subreason="uncaptured_member",
            offending_observations=(offender,),
        )

    monkeypatch.setattr(eval_process.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(eval_process.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(eval_process, "capture_process_cleanup_scope", fake_scope)
    monkeypatch.setattr(eval_process, "cleanup_birth_bound_processes", fake_cleanup)
    manager = eval_process.EvalProcessManager()

    manager.run(
        ("/bin/true",),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        timeout_seconds=1,
    )

    terminal = manager._cleanup_snapshots[0]  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert terminal.offending_observations == (
        offender.model_copy(update={"occurrence_count": EXPECTED_OBSERVATION_COUNT}),
    )
