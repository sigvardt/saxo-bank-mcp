from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from saxo_bank_mcp.agent_skill_command_runner import (
    ProcessCleanupTerminalSnapshot,
    capture_process_cleanup_scope,
    cleanup_birth_bound_processes,
    shutil_which,
)
from saxo_bank_mcp.agent_skill_eval_commands import path_with_cli_dirs
from saxo_bank_mcp.subprocess_environment import preserve_parent_temp_environment

KILL_WAIT_SECONDS = 1.0


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
    _cleanup_snapshots: list[ProcessCleanupTerminalSnapshot] = field(
        default_factory=list,
        repr=False,
    )

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        # Missing cwd raises FileNotFoundError on Popen; fail with the cwd path so
        # callers can distinguish it from a missing CLI binary.
        if not cwd.is_dir():
            raise FileNotFoundError(2, "No such file or directory", str(cwd))
        child_env = preserve_parent_temp_environment(env)
        path_value = child_env.get("PATH") or os.environ.get("PATH") or "/usr/bin:/bin"
        child_env["PATH"] = path_with_cli_dirs(
            path_value,
            "uv",
            "codex",
            "claude",
            "git",
            "node",
        )
        executable = _resolve_run_executable(command[0], child_env.get("PATH"))
        argv = (executable, *command[1:])
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        root_pid = process.pid
        pgid = os.getpgid(root_pid)
        scope = capture_process_cleanup_scope(process, pgid)
        created = max(1, len(scope.identities))
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
        snapshot = cleanup_birth_bound_processes(
            scope.identities,
            tracked_pids=scope.tracked_pids,
            tracked_pgids=scope.tracked_pgids,
        )
        self._cleanup_snapshots.append(snapshot)
        terminated = snapshot.signaled_process_count
        self.terminated_processes += terminated
        if timed_out:
            try:
                stdout, stderr = process.communicate(timeout=KILL_WAIT_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                stdout = stdout or ""
                stderr = stderr or "communicate_timeout"
        remaining = snapshot.remaining_process_count or 0
        self.remaining_processes = remaining
        cleanup = "passed" if snapshot.cleanup_status == "complete" else "residue"
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
        """Publish the shared primitive's terminal semantics without re-signalling IDs."""
        if not self._cleanup_snapshots and self.process_cleanup == "not_required":
            return self.snapshot()
        known_remaining = tuple(
            snapshot.remaining_process_count
            for snapshot in self._cleanup_snapshots
            if snapshot.remaining_process_count is not None
        )
        self.remaining_processes = sum(known_remaining)
        self.process_cleanup = (
            "passed"
            if all(snapshot.cleanup_status == "complete" for snapshot in self._cleanup_snapshots)
            else "residue"
        )
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        return {
            "created_processes": self.created_processes,
            "terminated_processes": self.terminated_processes,
            "remaining_processes": self.remaining_processes,
            "timed_out": self.timed_out,
            "process_cleanup": self.process_cleanup,
        }


def _resolve_run_executable(command0: str, path: str | None) -> str:
    """Prefer an absolute executable that still exists; then PATH; then host PATH.

    Never realpath() the binary: Claude Code's brew launcher is a symlink to
    claude.exe and must be exec'd via the public path.
    """
    candidate = Path(command0).expanduser()
    if candidate.is_absolute() or "/" in command0:
        absolute = candidate if candidate.is_absolute() else candidate.absolute()
        try:
            if absolute.is_file() and os.access(absolute, os.X_OK):
                return str(absolute)
        except OSError:
            pass
    found = shutil_which(command0, path=path)
    if found is None:
        found = shutil_which(Path(command0).name, path=path)
    if found is None:
        import shutil  # noqa: PLC0415

        found = shutil.which(command0) or shutil.which(Path(command0).name)
    if found is None:
        # Keep original so Popen raises FileNotFoundError with a stable name.
        return command0
    found_path = Path(found).expanduser()
    if not found_path.is_absolute():
        found_path = found_path.absolute()
    return str(found_path)
