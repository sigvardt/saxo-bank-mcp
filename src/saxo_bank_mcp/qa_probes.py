from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, now_utc, write_json
from saxo_bank_mcp._redaction import redact_json, redact_text, scan_secret_paths
from saxo_bank_mcp.config import SaxoRuntimeConfig
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_auth_probes import handle_auth_status, handle_sim_auth, handle_token_cache
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.server import (
    SaxoHealth,
    mcp,
)

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
TOOL_PAYLOAD_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
__all__ = (
    "handle_auth_status",
    "handle_sim_auth",
    "handle_token_cache",
)


async def call_saxo_health() -> SaxoHealth:
    async with Client(mcp) as client:
        result = await client.call_tool("saxo_health", {})
    return HEALTH_ADAPTER.validate_python(result.structured_content)


async def call_tool_payload(
    name: str,
    arguments: dict[str, JsonValue],
    *,
    raise_on_error: bool = True,
) -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments, raise_on_error=raise_on_error)
    return TOOL_PAYLOAD_ADAPTER.validate_python(result.structured_content)


async def call_live_session_capabilities_payload() -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {}
    return await call_tool_payload("saxo_get_session_capabilities", arguments)


async def call_live_write_refusal_payload() -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {"preview_token": "LIVE-WRITE-REFUSAL-PROBE"}
    return await call_tool_payload("saxo_place_sim_order", arguments, raise_on_error=False)


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
        raise TypeError("health event redaction returned non-object")
    write_json(out, redacted)
    return 0


def write_incomplete(out: Path, command: str, detail: str) -> int:
    write_json(out, base_event(command, "incomplete", detail))
    return 1


def handle_gitignore_secret(out: Path) -> int:
    text = Path(".gitignore").read_text(encoding="utf-8") if Path(".gitignore").exists() else ""
    missing = [pattern for pattern, _ in GITIGNORE_SECRET_DUMMIES if pattern not in text]
    dummy_paths = [path for _, path in GITIGNORE_SECRET_DUMMIES]
    created_dirs: list[Path] = []
    cleanup_removed: list[str] = []
    git_check: dict[str, JsonValue]

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
    write_json(
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
    return 0 if passed else 1


def handle_live_read(out: Path, skip_out: Path) -> int:
    live_environ = _live_probe_environ()
    runtime = SaxoRuntimeConfig.from_env(live_environ)
    if runtime.effective_read_environment() != "LIVE":
        event = {
            **base_event(
                "live-read",
                "skipped_no_live_credentials",
                "LIVE read env vars or credentials are absent",
            ),
            "requested_environment": "LIVE",
            "effective_read_environment": runtime.effective_read_environment(),
            "live_reads_enabled": runtime.live_reads_enabled,
            "live_credentials_present": runtime.live_credentials_present,
            "network_call_made": False,
            "live_write_called": False,
            "order_or_subscription_created": False,
            "prompted_user": False,
            "private_identifiers_present": False,
            "missing_requirements": _live_read_probe_missing_requirements(runtime),
        }
        return _write_secret_scanned_event(out, event, mirrors=(skip_out,))

    with _temporary_env({"SAXO_MCP_ENVIRONMENT": "LIVE"}):
        payload = anyio.run(call_live_session_capabilities_payload)
    status = str(payload.get("status", "failed"))
    event = {
        **base_event("live-read", status, "FastMCP live read-only session probe returned"),
        **payload,
        "live_write_called": bool(payload.get("live_write_called", False)),
        "order_or_subscription_created": bool(payload.get("order_or_subscription_created", False)),
        "prompted_user": False,
        "private_identifiers_redacted": True,
    }
    exit_code = _write_secret_scanned_event(out, event)
    if status != "passed":
        return 1
    return exit_code


def handle_live_write_refusal(out: Path) -> int:
    with _temporary_env({"SAXO_MCP_ENVIRONMENT": "LIVE"}):
        payload = anyio.run(call_live_write_refusal_payload)
    status = str(payload.get("status", "failed"))
    event = {
        **base_event("live-write-refusal", status, "FastMCP order tool refused LIVE write"),
        **payload,
        "live_write_called": bool(payload.get("live_write_called", False)),
        "order_or_subscription_created": bool(payload.get("order_or_subscription_created", False)),
    }
    exit_code = _write_secret_scanned_event(out, event)
    if status != "refused" or payload.get("refusal_reason") != "missing_live_write_enablement":
        return 1
    return exit_code


def handle_live_read_refusal(out: Path) -> int:
    enabled = os.environ.get("SAXO_MCP_ENABLE_LIVE_READS") == "1"
    requested_environment = os.environ.get("SAXO_MCP_ENVIRONMENT", "SIM").upper()
    status = "failed" if enabled else "refused"
    detail = (
        "LIVE read env var is enabled before read tooling exists"
        if enabled
        else "LIVE read enablement is absent"
    )
    write_json(
        out,
        {
            **base_event("live-read-refusal", status, detail),
            "environment": requested_environment,
            "live_reads": enabled,
            "live_writes": False,
            "scope_used": False,
            "network_call_made": False,
            "reason": "live_reads_enabled" if enabled else "missing_live_read_enablement",
        },
    )
    return 1 if enabled else 0


def handle_secret_scan(out: Path, paths: list[str]) -> int:
    findings, scan_errors = scan_secret_paths(paths)
    write_json(
        out,
        {
            **base_event(
                "secret-scan",
                "passed" if not findings and not scan_errors else "failed",
                "credential regex scan",
            ),
            "paths": list(paths),
            "findings": findings,
            "scan_errors": scan_errors,
        },
    )
    return 0 if not findings and not scan_errors else 1


def handle_denial(args: argparse.Namespace) -> int:
    command = str(args.command)
    detail = "fail-closed policy denied the operation before MCP tools exist"
    payload = base_event(command, "denied", detail)
    for key in ("missing", "method", "path", "service"):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = str(value)
    write_json(args.out, payload)
    return 0


def _live_probe_environ() -> dict[str, str]:
    source = dict(os.environ)
    source["SAXO_MCP_ENVIRONMENT"] = "LIVE"
    return source


def _live_read_probe_missing_requirements(runtime: SaxoRuntimeConfig) -> list[str]:
    missing: list[str] = []
    if not runtime.live_reads_enabled:
        missing.append("SAXO_MCP_ENABLE_LIVE_READS=1")
    if not runtime.live_credentials_present:
        missing.append("LIVE credentials")
    missing.append("SAXO_MCP_LIVE_TOKEN_CACHE_PATH")
    return missing


@contextmanager
def _temporary_env(updates: Mapping[str, str]) -> Generator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_secret_scanned_event(
    out: Path,
    payload: dict[str, JsonValue],
    *,
    mirrors: tuple[Path, ...] = (),
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("LIVE QA event redaction returned non-object")
    write_json(out, redacted)
    for mirror in mirrors:
        write_json(mirror, redacted)

    findings, scan_errors = scan_secret_paths([str(out), *(str(mirror) for mirror in mirrors)])
    scanned: dict[str, JsonValue] = {
        **redacted,
        "secret_scan": {"findings": findings, "scan_errors": scan_errors},
    }
    write_json(out, scanned)
    for mirror in mirrors:
        write_json(mirror, scanned)
    return 0 if not findings and not scan_errors else 1
