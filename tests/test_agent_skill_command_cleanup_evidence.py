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
