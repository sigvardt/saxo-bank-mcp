from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from shutil import which

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, now_utc
from saxo_bank_mcp.loop_manifest import GitState

GitStateProvider = Callable[[], GitState]
JSON_MAPPING_ADAPTER = TypeAdapter(dict[str, JsonValue])


def render_report(
    title: str,
    *,
    passed: bool,
    checks: Iterable[tuple[str, bool, str]],
    git_state_provider: GitStateProvider,
) -> str:
    git_state = git_state_provider()
    lines = [
        f"# {title}",
        "",
        f"- checked_at: `{now_utc()}`",
        f"- status: `{'passed' if passed else 'failed'}`",
        f"- git_sha: `{git_state.sha}`",
        f"- git_dirty: `{str(git_state.dirty).lower()}`",
        "",
        "## Checks",
    ]
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        lines.append(f"- `{mark}` {name}: {detail}")
    lines.append("")
    return "\n".join(lines)


def command_check(name: str, command: tuple[str, ...], timeout: int = 120) -> tuple[str, bool, str]:
    executable = which(command[0])
    if executable is None:
        return name, False, f"{command[0]} not found"
    try:
        result = subprocess.run(
            (executable, *command[1:]),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return name, False, type(exc).__name__
    detail = "exit 0" if result.returncode == 0 else f"exit {result.returncode}"
    return name, result.returncode == 0, detail


def validate_evidence_payload(
    payload: dict[str, JsonValue],
    allowed_statuses: frozenset[str],
    current_sha: str,
) -> tuple[bool, str]:
    detail = payload.get("detail")
    if isinstance(detail, str) and any(
        word in detail.lower() for word in ("not implemented", "placeholder", "todo", "incomplete")
    ):
        return False, "placeholder loop_harness artifact"

    err: str | None = None
    status = payload.get("status")
    git = payload.get("git")

    if payload.get("driver") != "loop_harness":
        err = "missing or invalid driver (must be 'loop_harness')"
    elif not (
        isinstance(status, str)
        and isinstance(payload.get("command"), str)
        and isinstance(payload.get("checked_at"), str)
        and isinstance(git, dict)
    ):
        err = "missing required fields or invalid types"
    else:
        sha = git.get("sha")
        dirty = git.get("dirty")
        if current_sha == "unavailable":
            err = "current git state is not replayable"
        elif dirty is not False:
            err = "evidence was captured from a dirty or unavailable git state"
        elif not isinstance(sha, str) or sha != current_sha:
            err = f"evidence git SHA {sha!r} does not match current HEAD {current_sha!r}"
        elif status not in allowed_statuses:
            err = f"status {status!r} not in {sorted(allowed_statuses)}"

    if err is not None:
        return False, err
    return True, f"status {status!r}"


def evidence_status_check(
    path: str,
    allowed_statuses: frozenset[str],
    git_state_provider: GitStateProvider,
) -> tuple[str, bool, str]:
    evidence_path = Path(path)
    if not evidence_path.exists():
        return path, False, "missing"
    try:
        payload = JSON_MAPPING_ADAPTER.validate_json(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return path, False, f"invalid JSON evidence: {type(exc).__name__}"

    git_state = git_state_provider()
    ok, detail = validate_evidence_payload(payload, allowed_statuses, git_state.sha)
    return path, ok, detail
