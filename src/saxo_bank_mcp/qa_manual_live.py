"""Auditable manual LIVE scenarios and evidence schema. # noqa: SIZE_OK."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from fastmcp.client.client import CallToolResult

from saxo_bank_mcp._evidence import JsonValue, now_utc
from saxo_bank_mcp._redaction import redact_text
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.http_client import (
    NetworkTransportForbiddenError,
    forbid_network_transport,
)
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.server import mcp

_SCHEMA_VERSION: Final = "saxo-manual-live-boundary-v1"
_GENERATOR: Final = "saxo_bank_mcp.qa_manual_live"
_WARNING_CAPTURE_CANARY: Final = "manual live boundary warning capture canary"
_SOURCE_PATHS: Final = (
    "src/saxo_bank_mcp/qa_manual_live.py",
    "src/saxo_bank_mcp/server.py",
    "src/saxo_bank_mcp/fastmcp_logging_safety.py",
    "src/saxo_bank_mcp/live_precheck_tool.py",
    "src/saxo_bank_mcp/order_mutation_execution.py",
    "src/saxo_bank_mcp/read_tools.py",
)


@dataclass(frozen=True, slots=True)
class _ScenarioSpec:
    scenario_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    argument_shape: Mapping[str, JsonValue]
    expected_status: str
    env_updates: Mapping[str, str | None]


class _WarningCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def handle_manual_live_boundary(out: Path) -> int:
    marker = f"rejected-{secrets.token_urlsafe(32)}"
    capture = _WarningCapture()
    root_logger = logging.getLogger()
    fastmcp_logger = logging.getLogger("fastmcp")
    root_logger.addHandler(capture)
    fastmcp_logger.addHandler(capture)
    try:
        logging.getLogger("fastmcp.server.server").warning(_WARNING_CAPTURE_CANARY)
        scenarios = anyio.run(_run_scenarios, marker, capture)
    finally:
        fastmcp_logger.removeHandler(capture)
        root_logger.removeHandler(capture)

    warning_capture_verified = any(
        record.name == "fastmcp.server.server"
        and record.getMessage() == _WARNING_CAPTURE_CANARY
        for record in capture.records
    )
    passed = warning_capture_verified and all(
        item.get("status") == "passed" for item in scenarios
    )
    warning_transcript = _warning_transcript(capture.records, marker)
    payload: dict[str, JsonValue] = {
        "schema_version": _SCHEMA_VERSION,
        "status": "passed" if passed else "failed",
        "checked_at": now_utc(),
        "scope": "local_fastmcp_live_safety_boundaries",
        "generator": _GENERATOR,
        "generator_source_sha256": _sha256(Path(__file__)),
        "git": current_git_state().model_dump(mode="json"),
        "replay_command": _replay_command(out),
        "source_hash_algorithm": "sha256",
        "source_hashes": [_source_entry(path) for path in _SOURCE_PATHS],
        "rejected_input": {
            "generated_for_this_run": True,
            "persisted": False,
            "sha256": hashlib.sha256(marker.encode()).hexdigest(),
        },
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "warning_log_transcript": warning_transcript,
        "warning_capture_verified": warning_capture_verified,
        "warning_log_transcript_sha256": hashlib.sha256(
            json.dumps(warning_transcript, sort_keys=True).encode(),
        ).hexdigest(),
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }
    published = write_scanned_json(out, payload)
    return 0 if passed and published else 1


async def _run_scenarios(
    marker: str,
    capture: _WarningCapture,
) -> list[dict[str, JsonValue]]:
    return [
        await _call_scenario(
            _ScenarioSpec(
                scenario_id="generic_fastmcp_validation",
                tool_name="saxo_call_registered_endpoint",
                arguments={"method": "GET", "path": [marker]},
                argument_shape={"method": "str", "path": "list[str]"},
                expected_status="invalid_arguments",
                env_updates={},
            ),
            marker=marker,
            capture=capture,
        ),
        await _call_scenario(
            _ScenarioSpec(
                scenario_id="live_precheck_validation",
                tool_name="saxo_precheck_live_order",
                arguments={"order": {"unexpected": marker}},
                argument_shape={"order": {"unexpected": "str"}},
                expected_status="invalid_request",
                env_updates={"SAXO_MCP_ENVIRONMENT": "LIVE"},
            ),
            marker=marker,
            capture=capture,
        ),
        await _call_scenario(
            _ScenarioSpec(
                scenario_id="live_write_refusal",
                tool_name="saxo_place_sim_order",
                arguments={"preview_token": marker},
                argument_shape={"preview_token": "str"},
                expected_status="refused",
                env_updates={"SAXO_MCP_ENVIRONMENT": "LIVE"},
            ),
            marker=marker,
            capture=capture,
        ),
        await _call_scenario(
            _ScenarioSpec(
                scenario_id="disabled_live_read_refusal",
                tool_name="saxo_call_registered_endpoint",
                arguments={"method": "GET", "path": "/root/v1/diagnostics/get"},
                argument_shape={"method": "str", "path": "str"},
                expected_status="live_not_called",
                env_updates={
                    "SAXO_MCP_ENVIRONMENT": "LIVE",
                    "SAXO_MCP_ENABLE_LIVE_READS": None,
                },
            ),
            marker=marker,
            capture=capture,
        ),
    ]


async def _call_scenario(
    spec: _ScenarioSpec,
    *,
    marker: str,
    capture: _WarningCapture,
) -> dict[str, JsonValue]:
    record_start = len(capture.records)
    result: CallToolResult | None = None
    exception_type = ""
    transport_constructed = False
    try:
        with _temporary_env(spec.env_updates), forbid_network_transport() as sentinel:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    spec.tool_name,
                    spec.arguments,
                    raise_on_error=False,
                )
            transport_constructed = sentinel.constructed
    except NetworkTransportForbiddenError:
        transport_constructed = True
        exception_type = "NetworkTransportForbiddenError"
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        exception_type = type(error).__name__

    records = capture.records[record_start:]
    warning_messages = [record.getMessage() for record in records]
    marker_in_logs = any(marker in message for message in warning_messages)
    marker_in_result = result is not None and marker in repr(
        (result.content, result.structured_content),
    )
    structured = result.structured_content if result is not None else None
    actual_status = (
        str(structured.get("status", "")) if isinstance(structured, Mapping) else ""
    )
    safety_fields_passed = _safety_fields_passed(spec.scenario_id, structured)
    passed = (
        result is not None
        and actual_status == spec.expected_status
        and not transport_constructed
        and not marker_in_logs
        and not marker_in_result
        and not exception_type
        and safety_fields_passed
    )
    return {
        "scenario_id": spec.scenario_id,
        "status": "passed" if passed else "failed",
        "tool_name": spec.tool_name,
        "argument_shape": dict(spec.argument_shape),
        "expected_status": spec.expected_status,
        "actual_status": actual_status,
        "result_is_error": None if result is None else result.is_error,
        "result_content": _safe_result_content(result, marker),
        "structured_result_keys": (
            sorted(str(key) for key in structured) if isinstance(structured, Mapping) else []
        ),
        "warning_records": _warning_transcript(records, marker),
        "rejected_input_absent_from_warning_logs": not marker_in_logs,
        "rejected_input_absent_from_mcp_result": not marker_in_result,
        "transport_constructed": transport_constructed,
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "safety_fields_passed": safety_fields_passed,
        "exception_type": exception_type,
    }


def _safety_fields_passed(
    scenario_id: str,
    structured: Mapping[str, JsonValue] | None,
) -> bool:
    if scenario_id not in {"live_write_refusal", "disabled_live_read_refusal"}:
        return True
    return bool(
        structured is not None
        and structured.get("network_call_made") is False
        and structured.get("live_write_called") is False
        and structured.get("order_or_subscription_created") is False
    )


def _safe_result_content(result: CallToolResult | None, marker: str) -> list[JsonValue]:
    if result is None:
        return []
    safe: list[JsonValue] = []
    for block in result.content:
        block_type = str(getattr(block, "type", "unknown"))
        raw_text = getattr(block, "text", None)
        text = "" if not isinstance(raw_text, str) else _safe_text(raw_text, marker)
        safe.append({"type": block_type, "text": text})
    return safe


def _warning_transcript(
    records: Sequence[logging.LogRecord],
    marker: str,
) -> list[JsonValue]:
    return [
        {
            "logger": record.name,
            "level": record.levelname,
            "message": _safe_text(record.getMessage(), marker),
        }
        for record in records
    ]


def _safe_text(text: str, marker: str) -> str:
    return redact_text(text.replace(marker, "<rejected-input>"))


def _source_entry(relative_path: str) -> dict[str, JsonValue]:
    path = _repo_root() / relative_path
    return {
        "path": relative_path,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _replay_command(out: Path) -> list[JsonValue]:
    resolved = out.resolve(strict=False)
    root = Path.cwd().resolve()
    output = os.path.relpath(resolved, root) if resolved.is_relative_to(root) else out.name
    return [
        "uv",
        "run",
        "python",
        "-m",
        "saxo_bank_mcp.qa",
        "manual-live-boundary",
        "--out",
        output,
    ]


@contextmanager
def _temporary_env(updates: Mapping[str, str | None]) -> Generator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
