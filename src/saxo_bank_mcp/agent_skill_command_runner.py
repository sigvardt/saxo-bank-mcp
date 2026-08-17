from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.subprocess_environment import preserve_parent_temp_environment

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
TERM_WAIT_SECONDS = 1.0
KILL_WAIT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    receipt: CommandReceipt
    stdout: str
    stderr: str

    def json_stdout(self) -> dict[str, JsonValue]:
        try:
            return JSON_OBJECT_ADAPTER.validate_json(self.stdout)
        except ValidationError:
            return {}

    def json_value(self) -> JsonValue | None:
        try:
            return cast("JsonValue", json.loads(self.stdout))
        except json.JSONDecodeError:
            return None


@dataclass(frozen=True, slots=True)
class CommandFailureError(Exception):
    receipt: CommandReceipt
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    remaining_process_count: int | None = None
    remaining_process_group_count: int | None = None


def run_command(  # noqa: C901, PLR0915
    name: str,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 180,
) -> CommandResult:
    """Run a command; always clean the process group on success, fail, interrupt, timeout."""
    if env is None:
        msg = "isolated env is required"
        raise ValueError(msg)
    executable = shutil_which(argv[0], path=env.get("PATH"))
    command: tuple[str, ...] = argv if executable is None else (executable, *argv[1:])
    process: subprocess.Popen[str] | None = None
    pgid: int | None = None
    root_pid: int | None = None
    tracked_pids: list[int] = []
    tracked_pgids: list[int] = []
    stop_watch = threading.Event()
    watch_lock = threading.Lock()
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = 124

    def _watch() -> None:
        while not stop_watch.is_set():
            if root_pid is None:
                time.sleep(0.001)
                continue
            pids, pgids = _snapshot_tree(root_pid, pgid)
            with watch_lock:
                tracked_pids[:] = sorted(set(tracked_pids) | set(pids))
                tracked_pgids[:] = sorted(set(tracked_pgids) | set(pgids))
            time.sleep(0.001)

    watcher: threading.Thread | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=preserve_parent_temp_environment(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        root_pid = process.pid
        pgid = os.getpgid(process.pid)
        # Immediate snapshot so fast-exit parents still leave tracked members.
        first_pids, first_pgids = _snapshot_tree(root_pid, pgid)
        with watch_lock:
            tracked_pids[:] = list(first_pids)
            tracked_pgids[:] = list(first_pgids)
        watcher = threading.Thread(target=_watch, name=f"cmd-watch-{name}", daemon=True)
        watcher.start()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            # Continuous capture while parent is alive (escaped groups / new sessions).
            pids_now, pgids_now = _snapshot_tree(root_pid, pgid)
            with watch_lock:
                tracked_pids[:] = sorted(set(tracked_pids) | set(pids_now))
                tracked_pgids[:] = sorted(set(tracked_pgids) | set(pgids_now))
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.005)
        with watch_lock:
            pids = tuple(tracked_pids)
            pgids = tuple(tracked_pgids)
        pids, pgids = _merge_snapshots(pids, pgids, *_snapshot_tree(root_pid, pgid))
        # Always include original process group: redirected sleepers share it after parent exit.
        # pgid is assigned from os.getpgid after Popen; keep it in the tracked set even if
        # the parent has already exited and descendants remain only under that group.
        tracked_pgid = pgid
        pgids = tuple(sorted(set(pgids) | {tracked_pgid}))
        pids = tuple(sorted(set(pids) | set(process_group_members(tracked_pgid))))
        # Kill descendants before draining pipes so background children cannot hold pipes open.
        _cleanup_tracked(pids, pgids, root_pid=root_pid, pgid=pgid)
        try:
            stdout, stderr = process.communicate(timeout=KILL_WAIT_SECONDS + TERM_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            stdout = stdout or ""
            stderr = stderr or "communicate_timeout"
        if timed_out:
            exit_code = 124
            receipt = _receipt(
                name,
                argv,
                cwd,
                root_pid,
                pgid,
                exit_code,
                stdout,
                stderr,
                timed_out=True,
                cleanup_attempted=True,
            )
            raise CommandFailureError(
                receipt,
                stdout,
                stderr,
                len(remaining_live_pids(pids)),
                len(remaining_live_pgids(pgids)),
            )
        exit_code = int(process.returncode if process.returncode is not None else 124)
    except OSError as exc:
        with watch_lock:
            pids = tuple(tracked_pids)
            pgids = tuple(tracked_pgids)
        _cleanup_tracked(pids, pgids, root_pid=root_pid, pgid=pgid)
        receipt = _receipt(
            name,
            argv,
            cwd,
            root_pid,
            pgid,
            124,
            "",
            type(exc).__name__,
            timed_out=False,
            cleanup_attempted=True,
        )
        raise CommandFailureError(
            receipt,
            "",
            type(exc).__name__,
            len(remaining_live_pids(pids)),
            len(remaining_live_pgids(pgids)),
        ) from exc
    finally:
        stop_watch.set()
        if watcher is not None:
            watcher.join(timeout=1.0)
        with watch_lock:
            pids = tuple(tracked_pids)
            pgids = tuple(tracked_pgids)
        _cleanup_tracked(pids, pgids, root_pid=root_pid, pgid=pgid)

    receipt = _receipt(
        name,
        argv,
        cwd,
        root_pid,
        pgid,
        exit_code,
        stdout,
        stderr,
        timed_out=False,
        cleanup_attempted=True,
    )
    result = CommandResult(receipt=receipt, stdout=stdout, stderr=stderr)
    if exit_code != 0:
        with watch_lock:
            final_pids = tuple(tracked_pids)
            final_pgids = tuple(tracked_pgids)
        raise CommandFailureError(
            result.receipt,
            result.stdout,
            result.stderr,
            len(remaining_live_pids(final_pids)),
            len(remaining_live_pgids(final_pgids)),
        )
    return result


def process_still_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remaining_live_pids(pids: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(pid for pid in sorted(set(pids)) if process_still_running(pid))


def remaining_live_pgids(pgids: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(
        pgid
        for pgid in sorted(set(pgids))
        if any(process_still_running(pid) for pid in process_group_members(pgid))
    )


def process_group_members(pgid: int) -> tuple[int, ...]:
    members: list[int] = []
    for pid, _ppid, group in _process_table():
        if group == pgid:
            members.append(pid)
    if not members and process_still_running(pgid):
        members.append(pgid)
    return tuple(sorted(set(members)))


def descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Return root and all descendants via ppid walk, including escaped process groups."""
    table = _process_table()
    children: dict[int, list[int]] = {}
    for pid, ppid, _group in table:
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        stack.extend(children.get(current, ()))
    return tuple(sorted(pid for pid in found if process_still_running(pid)))


def terminate_process_group(pgid: int, *, escalate: bool) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        for pid in process_group_members(pgid):
            _signal_pid(pid, signal.SIGTERM)
    if _wait_pgid_exit(pgid, TERM_WAIT_SECONDS):
        return
    if not escalate:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        for pid in process_group_members(pgid):
            _signal_pid(pid, signal.SIGKILL)
    _wait_pgid_exit(pgid, KILL_WAIT_SECONDS)


def terminate_pid_tree(root_pid: int, *, escalate: bool) -> None:
    pids = descendant_pids(root_pid)
    for pid in reversed(pids):
        _signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + TERM_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not remaining_live_pids(pids):
            return
        time.sleep(0.05)
    if not escalate:
        return
    for pid in reversed(descendant_pids(root_pid)):
        _signal_pid(pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + KILL_WAIT_SECONDS
    while time.monotonic() < kill_deadline:
        if not remaining_live_pids(descendant_pids(root_pid)):
            return
        time.sleep(0.05)


def cleanup_recorded_groups(pgids: tuple[int, ...]) -> tuple[int, ...]:
    for pgid in sorted(set(pgids)):
        if remaining_live_pgids((pgid,)):
            terminate_process_group(pgid, escalate=True)
    return remaining_live_pgids(pgids)


def load_json_object(path: Path) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return {}


def shutil_which(command: str, *, path: str | None) -> str | None:
    import shutil  # noqa: PLC0415

    return shutil.which(command, path=path)


def _snapshot_tree(
    root_pid: int | None,
    pgid: int | None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    pids: set[int] = set()
    pgids: set[int] = set()
    if root_pid is not None:
        pids.update(descendant_pids(root_pid))
        pids.add(root_pid)
    if pgid is not None:
        pgids.add(pgid)
        members = process_group_members(pgid)
        pids.update(members)
    table = {pid: group for pid, _ppid, group in _process_table()}
    for pid in list(pids):
        group = table.get(pid)
        if group is not None:
            pgids.add(group)
            pids.update(process_group_members(group))
    return tuple(sorted(pids)), tuple(sorted(pgids))


def _merge_snapshots(
    pids_a: tuple[int, ...],
    pgids_a: tuple[int, ...],
    pids_b: tuple[int, ...],
    pgids_b: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(sorted(set(pids_a) | set(pids_b))), tuple(sorted(set(pgids_a) | set(pgids_b)))


def _cleanup_tracked(
    tracked_pids: tuple[int, ...],
    tracked_pgids: tuple[int, ...],
    *,
    root_pid: int | None,
    pgid: int | None,
) -> None:
    pgids = set(tracked_pgids)
    if pgid is not None:
        pgids.add(pgid)
    for group in sorted(pgids):
        if remaining_live_pgids((group,)):
            terminate_process_group(group, escalate=True)
    pids = set(tracked_pids)
    if root_pid is not None:
        pids.update(descendant_pids(root_pid))
        pids.add(root_pid)
    live = remaining_live_pids(tuple(pids))
    for pid in reversed(live):
        _signal_pid(pid, signal.SIGTERM)
    if remaining_live_pids(live):
        time.sleep(TERM_WAIT_SECONDS)
    for pid in reversed(remaining_live_pids(live)):
        _signal_pid(pid, signal.SIGKILL)
    for group in sorted(pgids):
        if remaining_live_pgids((group,)):
            terminate_process_group(group, escalate=True)


def _process_table() -> tuple[tuple[int, int, int], ...]:
    try:
        output = subprocess.run(
            ("ps", "-axo", "pid=,ppid=,pgid="),  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    rows: list[tuple[int, int, int]] = []
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:  # noqa: PLR2004
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return tuple(rows)


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _wait_pgid_exit(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not remaining_live_pgids((pgid,)):
            return True
        time.sleep(0.05)
    return not remaining_live_pgids((pgid,))


def _receipt(  # noqa: PLR0913
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    pid: int | None,
    pgid: int | None,
    exit_code: int,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool,
    cleanup_attempted: bool,
) -> CommandReceipt:
    return CommandReceipt(
        name=name,
        argv=argv,
        cwd=str(cwd),
        pid=pid,
        pgid=pgid,
        exit_code=exit_code,
        stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
        timed_out=timed_out,
        cleanup_attempted=cleanup_attempted,
    )
