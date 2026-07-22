from __future__ import annotations

import argparse
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, now_utc
from saxo_bank_mcp._redaction import (
    redact_json,
    redact_text,
    scan_secret_paths,
    secret_scan_pattern_classes,
)
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_auth_probes import handle_auth_status, handle_sim_auth, handle_token_cache
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.qa_live_probes import (
    handle_live_read,
    handle_live_read_refusal,
    handle_live_write_refusal,
    handle_tool_inventory,
)
from saxo_bank_mcp.qa_mcp_probe_calls import (
    call_live_read_payloads,
    call_live_write_refusal_payload,
    call_tool_inventory_payload,
    call_tool_payload,
)
from saxo_bank_mcp.server import SaxoHealth, mcp

GITIGNORE_SECRET_DUMMIES = (
    (".omo/", Path(".omo/task-1-qa-ignore-dummy.txt")),
    (".codegraph/", Path(".codegraph/task-1-qa-ignore-dummy.txt")),
    (".env", Path(".env")),
    ("*credential*", Path("task-1-qa-credential.txt")),
    ("*secret*", Path("task-1-qa-secret.txt")),
    ("*token*", Path("task-1-qa-token.txt")),
    ("*.log", Path("task-1-qa.log")),
)
HEALTH_ADAPTER: Final = TypeAdapter(SaxoHealth)

__all__ = (
    "call_live_read_payloads",
    "call_live_write_refusal_payload",
    "call_tool_inventory_payload",
    "call_tool_payload",
    "handle_auth_status",
    "handle_gitignore_secret",
    "handle_health",
    "handle_live_read",
    "handle_live_read_refusal",
    "handle_live_write_refusal",
    "handle_secret_scan",
    "handle_sim_auth",
    "handle_token_cache",
    "handle_tool_inventory",
    "write_incomplete",
)


class QaProbeSerializationError(TypeError):
    pass


async def call_saxo_health() -> SaxoHealth:
    async with Client(mcp) as client:
        result = await client.call_tool("saxo_health", {})
    return HEALTH_ADAPTER.validate_python(result.structured_content)


def handle_health(out: Path) -> int:
    payload = anyio.run(call_saxo_health)
    detail = redact_text(
        f"FastMCP in-process saxo_health returned {payload['service']} "
        f"in {payload['mode']} mode with live_writes={payload['live_writes']}; "
        f"scope={payload['scope']}.",
    )
    event: dict[str, JsonValue] = {
        "checked_at": now_utc(),
        "status": payload["status"],
        "tool_name": "saxo_health",
        "driver": "loop_harness",
        "mode": payload["mode"],
        "live_writes": payload["live_writes"],
        "scope": payload["scope"],
        "verifies": payload["verifies"],
        "does_not_verify": payload["does_not_verify"],
        "detail": detail,
        "git": current_git_state().model_dump(mode="json"),
    }
    redacted = redact_json(event)
    if not isinstance(redacted, dict):
        raise QaProbeSerializationError
    return 0 if write_scanned_json(out, redacted) else 1


def write_incomplete(out: Path, command: str, detail: str) -> int:
    write_scanned_json(out, base_event(command, "incomplete", detail))
    return 1


def handle_gitignore_secret(out: Path) -> int:
    text = Path(".gitignore").read_text(encoding="utf-8") if Path(".gitignore").exists() else ""
    missing = [pattern for pattern, _ in GITIGNORE_SECRET_DUMMIES if pattern not in text]
    dummy_paths = [path for _, path in GITIGNORE_SECRET_DUMMIES]
    created_dirs: list[Path] = []
    cleanup_removed: list[str] = []
    git_check: dict[str, JsonValue] = {"status": "failed", "error": "not_run"}
    try:
        for path in dummy_paths:
            if path.exists():
                missing.append(f"dummy path already exists: {path}")
                continue
            parent = path.parent
            if parent != Path() and not parent.exists():
                parent.mkdir(parents=True)
                created_dirs.append(parent)
            path.write_text("dummy ignore probe\n", encoding="utf-8")
        git = shutil.which("git")
        if git is None:
            git_check = {"status": "failed", "error": "git_not_found"}
        else:
            checked_paths = [str(path) for path in dummy_paths]
            check = subprocess.run(
                [git, "check-ignore", "--stdin"],
                input="\n".join(checked_paths) + "\n",
                capture_output=True,
                text=True,
                check=False,
            )
            ignored_paths = check.stdout.splitlines()
            missing.extend(path for path in checked_paths if path not in ignored_paths)
            git_check = {
                "status": "passed" if check.returncode == 0 else "failed",
                "returncode": check.returncode,
                "ignored_paths": ignored_paths,
                "stderr": check.stderr.strip(),
            }
    finally:
        for path in dummy_paths:
            if path.exists():
                path.unlink()
                cleanup_removed.append(str(path))
        for path in reversed(created_dirs):
            with suppress(OSError):
                path.rmdir()
    remaining_exists = [str(path) for path in dummy_paths if path.exists()]
    missing = sorted(set(missing))
    passed = not missing and not remaining_exists and git_check.get("status") == "passed"
    published = write_scanned_json(
        out,
        {
            **base_event(
                "gitignore-secret",
                "passed" if passed else "failed",
                "required ignore patterns and dummy cleanup checked",
            ),
            "dummy_paths": [str(path) for path in dummy_paths],
            "git_check": git_check,
            "cleanup_removed": cleanup_removed,
            "remaining_exists": remaining_exists,
            "missing_patterns": missing,
        },
    )
    return 0 if passed and published else 1


def handle_secret_scan(out: Path, paths: list[str]) -> int:
    findings, scan_errors = scan_secret_paths(paths)
    candidate = redact_json(
        {
            **base_event(
                "secret-scan",
                "passed" if not findings and not scan_errors else "failed",
                "credential regex scan",
            ),
            "paths": list(paths),
            "pattern_classes": list(secret_scan_pattern_classes()),
            "findings": findings,
            "scan_errors": scan_errors,
        },
    )
    if not isinstance(candidate, dict):
        raise QaProbeSerializationError
    published = write_scanned_json(
        out,
        candidate,
    )
    return 0 if not findings and not scan_errors and published else 1


def handle_denial(args: argparse.Namespace) -> int:
    command = str(args.command)
    payload = base_event(command, "denied", "fail-closed policy denied the operation")
    for key in ("missing", "method", "path", "service"):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = str(value)
    return 0 if write_scanned_json(args.out, payload) else 1
