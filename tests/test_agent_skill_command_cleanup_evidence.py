from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import saxo_bank_mcp.agent_skill_command_runner as command_runner

MIN_ESCAPED_TARGET_COUNT = 2
OWNER_FILE_MODE = 0o600
REUSED_PID = 4242


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
    original_cleanup = command_runner._cleanup_tracked  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    original_write = command_runner.write_command_cleanup_identity_receipt
    original_remaining_pids = command_runner.remaining_live_pids
    original_remaining_pgids = command_runner.remaining_live_pgids

    def counted_cleanup(
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        *,
        root_pid: int | None,
        pgid: int | None,
    ) -> None:
        nonlocal cleanup_calls, cleanup_complete, inside_cleanup
        cleanup_calls += 1
        events.append("cleanup")
        inside_cleanup = True
        try:
            original_cleanup(
                tracked_pids,
                tracked_pgids,
                root_pid=root_pid,
                pgid=pgid,
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
        identities: tuple[command_runner.ProcessCleanupIdentity, ...],
    ) -> command_runner.CommandCleanupIdentityEvidence:
        assert cleanup_complete
        events.append("observe-write")
        return original_write(
            path,
            name=name,
            argv=argv,
            cwd=cwd,
            root_pid=root_pid,
            root_pgid=root_pgid,
            identities=identities,
        )

    def recorded_remaining_pids(pids: tuple[int, ...]) -> tuple[int, ...]:
        if cleanup_complete and not inside_cleanup:
            events.append("terminal-count")
        return original_remaining_pids(pids)

    def recorded_remaining_pgids(pgids: tuple[int, ...]) -> tuple[int, ...]:
        if cleanup_complete and not inside_cleanup:
            events.append("terminal-count")
        return original_remaining_pgids(pgids)

    monkeypatch.setattr(command_runner, "_cleanup_tracked", counted_cleanup)
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
    assert events.index("observe-write") < events.index("terminal-count")
    assert events[-1] == "terminal-count"
    assert caught.value.remaining_process_count == 0
    assert caught.value.remaining_process_group_count == 0


def test_post_spawn_oserror_cleans_once_and_observes_terminal_survivor_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_roots: list[int] = []
    cleanup_calls = 0
    original_cleanup = command_runner._cleanup_tracked  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    def broken_snapshot(
        root_pid: int | None,
        pgid: int | None,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        _ = (root_pid, pgid)
        raise OSError("injected_post_spawn_observation_failure")

    def counted_cleanup(
        tracked_pids: tuple[int, ...],
        tracked_pgids: tuple[int, ...],
        *,
        root_pid: int | None,
        pgid: int | None,
    ) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if root_pid is not None:
            cleanup_roots.append(root_pid)
        original_cleanup(
            tracked_pids,
            tracked_pgids,
            root_pid=root_pid,
            pgid=pgid,
        )

    monkeypatch.setattr(command_runner, "_snapshot_tree", broken_snapshot)
    monkeypatch.setattr(command_runner, "_cleanup_tracked", counted_cleanup)

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
