from __future__ import annotations

import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Literal

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.audit import audit_log_path
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, reset_safety_state
from saxo_bank_mcp.server import mcp

FIXTURE_ACCOUNT = "SIM-ACCOUNT-1"
FIXTURE_INSTRUMENT = "21"
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


class QaSafetyProbeSerializationError(TypeError):
    pass


def handle_approval_happy(out: Path) -> int:
    payload = anyio.run(_approval_flow, "approve")
    return _write_redacted_with_secret_scan(out, payload, "passed")


def handle_approval_denied(out: Path, missing: str) -> int:
    payload = anyio.run(_approval_flow, "deny")
    expected_reason = _missing_to_reason(missing)
    status = "denied" if payload.get("denial_reason") == expected_reason else "failed"
    event = {**payload, "status": status, "expected_denial_reason": expected_reason}
    return _write_redacted_with_secret_scan(out, event, "denied")


async def _approval_flow(mode: Literal["approve", "deny"]) -> dict[str, JsonValue]:
    reset_safety_state()
    with _safety_env():
        async with Client(mcp) as client:
            preview_result = await client.call_tool("saxo_create_write_preview", _fixture_request())
            preview = _payload(preview_result.structured_content)
            commit_args: dict[str, JsonValue] = {"preview_token": str(preview["preview_token"])}
            if mode == "approve":
                commit_args["approval_factor"] = TEST_APPROVAL_FACTOR
            commit_result = await client.call_tool("saxo_commit_write_preview", commit_args)
            commit = _payload(commit_result.structured_content)
        raw_audit_dir = os.environ.get(
            "SAXO_MCP_AUDIT_DIR",
            str(Path.home() / ".local/state/saxo-bank-mcp/audit"),
        )
        audit_path = audit_log_path(Path(raw_audit_dir))
        status = "passed" if commit.get("status") == "approved_for_simulation" else "failed"
        request_fingerprint = str(preview["request_fingerprint"])
        return {
            **base_event(
                "approval-happy" if mode == "approve" else "approval-denied",
                status,
                "FastMCP safety preview/commit flow exercised",
            ),
            "environment": "SIM",
            "preview_status": str(preview["status"]),
            "commit_status": str(commit["status"]),
            "denial_reason": str(commit.get("denial_reason", "")),
            "same_request_fingerprint": commit.get("request_fingerprint") == request_fingerprint,
            "prompted_user": False,
            "approval_factor_mode": str(commit.get("approval_factor_mode", "test_only_sim")),
            "preview_token_redacted": True,
            "audit_path": str(audit_path),
            "audit_path_inside_repo": _is_inside_repo(audit_path),
            "audit_mode": _audit_mode(audit_path),
            "preview": preview,
            "commit": commit,
            "git": current_git_state().model_dump(mode="json"),
        }


def _fixture_request() -> dict[str, JsonValue]:
    return {
        "operation_id": "trade.order.place",
        "account_key": FIXTURE_ACCOUNT,
        "instrument_uic": int(FIXTURE_INSTRUMENT),
        "quantity": 10,
        "estimated_notional": 500,
        "account_currency": "USD",
        "risk": {
            "cost": 500,
            "cash_required": 500,
            "margin_impact": 20,
            "contract_multiplier": 1,
            "conversion_known": True,
        },
        "request_body": {"BuySell": "Buy", "OrderType": "Market"},
    }


def _payload(value: JsonValue) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _write_redacted_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    success_status: str,
) -> int:
    redacted_value = redact_json(payload)
    if not isinstance(redacted_value, Mapping):
        raise QaSafetyProbeSerializationError
    redacted = dict(redacted_value)
    redacted["secret_scan"] = {"findings": [], "scan_errors": []}
    published = write_scanned_json(out, redacted)
    return 0 if redacted.get("status") == success_status and published else 1


def _missing_to_reason(missing: str) -> str:
    if missing == "approval-factor":
        return "approval_factor_missing"
    return missing.replace("-", "_")


@contextmanager
def _safety_env() -> Generator[None]:
    previous = {key: os.environ.get(key) for key in _SAFETY_ENV_DEFAULTS}
    try:
        for key, value in _SAFETY_ENV_DEFAULTS.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _is_inside_repo(path: Path) -> bool:
    return path.resolve(strict=False).is_relative_to(Path.cwd().resolve(strict=False))


def _audit_mode(path: Path) -> str | None:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return None


_SAFETY_ENV_DEFAULTS = {
    "SAXO_MCP_ENVIRONMENT": "SIM",
    "SAXO_MCP_ACCOUNT_ALLOWLIST": FIXTURE_ACCOUNT,
    "SAXO_MCP_INSTRUMENT_ALLOWLIST": FIXTURE_INSTRUMENT,
}
