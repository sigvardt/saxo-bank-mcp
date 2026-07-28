from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from saxo_bank_mcp.agent_skill_command_runner import (
    descendant_pids,
    process_group_members,
    process_still_running,
    remaining_live_pgids,
    remaining_live_pids,
    shutil_which,
    terminate_process_group,
)

TERM_WAIT_SECONDS = 1.0
KILL_WAIT_SECONDS = 1.0
COMMUNICATE_GRACE_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class ManagedProcessResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    created_processes: int
    terminated_processes: int
    remaining_processes: int
    process_cleanup: str


@dataclass(slots=True)
class EvalProcessManager:
    """Process-group manager for eval model, router, and client-version paths."""

    created_processes: int = 0
    terminated_processes: int = 0
    remaining_processes: int = 0
    timed_out: bool = False
    process_cleanup: str = "not_required"
    _tracked_pgids: list[int] = field(default_factory=list, repr=False)

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        executable = shutil_which(command[0], path=env.get("PATH"))
        argv = command if executable is None else (executable, *command[1:])
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        root_pid = process.pid
        pgid = os.getpgid(root_pid)
        self._tracked_pgids.append(pgid)
        created = _snapshot_live_count(root_pid, pgid)
        self.created_processes += created
        self.process_cleanup = "pending"
        timed_out = False
        stdout = ""
        stderr = ""
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.timed_out = True
            terminated = _terminate_group(pgid, root_pid=root_pid)
            self.terminated_processes += terminated
            try:
                stdout, stderr = process.communicate(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=COMMUNICATE_GRACE_SECONDS)
        else:
            # Parent exited; still reclaim any client-spawned group members (MCP children).
            terminated = _terminate_group(pgid, root_pid=root_pid)
            self.terminated_processes += terminated
        remaining = _count_remaining(root_pid, pgid)
        self.remaining_processes = remaining
        cleanup = "passed" if remaining == 0 else "residue"
        self.process_cleanup = cleanup
        # Preserve exact zero. `or 124` would turn successful exit 0 into 124.
        returncode = 124 if timed_out or process.returncode is None else int(process.returncode)
        return ManagedProcessResult(
            stdout=stdout or "",
            stderr=stderr or "",
            returncode=returncode,
            timed_out=timed_out,
            created_processes=created,
            terminated_processes=terminated,
            remaining_processes=remaining,
            process_cleanup=cleanup,
        )

    def finalize(self) -> dict[str, object]:
        """Reclaim every tracked process group before token promotion / runtime delete."""
        if not self._tracked_pgids and self.process_cleanup == "not_required":
            return self.snapshot()
        terminated = 0
        for pgid in sorted(set(self._tracked_pgids)):
            terminated += _terminate_group(pgid, root_pid=None)
        self.terminated_processes += terminated
        remaining = 0
        for pgid in sorted(set(self._tracked_pgids)):
            remaining += len(remaining_live_pgids((pgid,)))
            remaining += len(remaining_live_pids(process_group_members(pgid)))
        # Remaining is process-group membership count, not group count.
        live: set[int] = set()
        for pgid in sorted(set(self._tracked_pgids)):
            live.update(process_group_members(pgid))
        self.remaining_processes = len(live)
        if self.remaining_processes:
            self.process_cleanup = "residue"
        elif self.timed_out:
            self.process_cleanup = "passed"
        else:
            self.process_cleanup = "passed"
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        return {
            "created_processes": self.created_processes,
            "terminated_processes": self.terminated_processes,
            "remaining_processes": self.remaining_processes,
            "timed_out": self.timed_out,
            "process_cleanup": self.process_cleanup,
        }


def _snapshot_live_count(root_pid: int, pgid: int) -> int:
    pids = set(descendant_pids(root_pid))
    pids.add(root_pid)
    pids.update(process_group_members(pgid))
    return max(1, len({pid for pid in pids if process_still_running(pid)}))


def _count_remaining(root_pid: int | None, pgid: int) -> int:
    live: set[int] = set()
    if root_pid is not None:
        live.update(pid for pid in descendant_pids(root_pid) if process_still_running(pid))
        if process_still_running(root_pid):
            live.add(root_pid)
    live.update(process_group_members(pgid))
    return len(live)


def _terminate_group(pgid: int, *, root_pid: int | None) -> int:
    before = set(process_group_members(pgid))
    if root_pid is not None:
        before.update(descendant_pids(root_pid))
        before.add(root_pid)
    before = {pid for pid in before if process_still_running(pid)}
    if not before and not remaining_live_pgids((pgid,)):
        return 0
    terminate_process_group(pgid, escalate=True)
    if root_pid is not None:
        for pid in reversed(tuple(before)):
            _signal_pid(pid, signal.SIGTERM)
        time.sleep(TERM_WAIT_SECONDS)
        for pid in reversed(tuple(pid for pid in before if process_still_running(pid))):
            _signal_pid(pid, signal.SIGKILL)
        time.sleep(KILL_WAIT_SECONDS)
    after = {pid for pid in before if process_still_running(pid)}
    after.update(process_group_members(pgid))
    return max(0, len(before) - len(after))


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return
