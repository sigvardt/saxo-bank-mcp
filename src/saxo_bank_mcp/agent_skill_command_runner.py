from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


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


def run_command(
    name: str,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 180,
) -> CommandResult:
    executable = shutil_which(argv[0], path=None if env is None else env.get("PATH"))
    command: tuple[str, ...] = argv if executable is None else (executable, *argv[1:])
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_merged_env(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if process is None:
            receipt = _receipt(
                name,
                argv,
                cwd,
                None,
                None,
                124,
                "",
                type(exc).__name__,
                timed_out=True,
                cleanup_attempted=True,
            )
            raise CommandFailureError(receipt) from exc
        pgid = os.getpgid(process.pid)
        _terminate_group(pgid)
        stdout, stderr = process.communicate()
        receipt = _receipt(
            name,
            argv,
            cwd,
            process.pid,
            pgid,
            124,
            stdout,
            stderr,
            timed_out=True,
            cleanup_attempted=True,
        )
        raise CommandFailureError(receipt) from exc
    except OSError as exc:
        receipt = _receipt(
            name,
            argv,
            cwd,
            None,
            None,
            124,
            "",
            type(exc).__name__,
            timed_out=False,
            cleanup_attempted=False,
        )
        raise CommandFailureError(receipt) from exc
    result = CommandResult(
        receipt=_receipt(
            name,
            argv,
            cwd,
            process.pid,
            pgid,
            process.returncode,
            stdout,
            stderr,
            timed_out=False,
            cleanup_attempted=False,
        ),
        stdout=stdout,
        stderr=stderr,
    )
    if process.returncode != 0:
        raise CommandFailureError(result.receipt)
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


def load_json_object(path: Path) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return {}


def shutil_which(command: str, *, path: str | None) -> str | None:
    import shutil  # noqa: PLC0415

    return shutil.which(command, path=path)


def _merged_env(env: dict[str, str] | None) -> dict[str, str]:
    merged = os.environ.copy()
    if env is not None:
        merged.update(env)
    return merged


def _terminate_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return


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
