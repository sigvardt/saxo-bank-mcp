from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json


class ToolMatrixReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed"]
    execution_mode: Literal["manifest_validation", "verify_only"]
    environment: str
    tool_count: int
    unique_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    unexpected_tools: tuple[str, ...]
    cleanup: dict[str, JsonValue]
    errors: tuple[str, ...]

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


JSON_ADAPTER = TypeAdapter(dict[str, JsonValue])


def build_manifest_matrix_report(
    *,
    manifest: Path,
    environment: str,
    require_tools: int,
    out: Path,
) -> int:
    tools, errors = _manifest_tools(manifest)
    missing = () if len(tools) == require_tools else ("tool_count_mismatch",)
    report = ToolMatrixReport(
        status="failed" if errors or missing else "passed",
        execution_mode="manifest_validation",
        environment=environment,
        tool_count=len(tools),
        unique_tools=tuple(sorted(tools)),
        missing_tools=missing,
        unexpected_tools=(),
        cleanup={"complete": True, "created_mcp_calls": 0, "created_saxo_calls": 0},
        errors=tuple(errors),
    )
    write_json(out, report.to_json_value())
    return 0 if report.status == "passed" else 1


def verify_matrix_report(
    *,
    report_path: Path,
    require_environment: str,
    out: Path,
) -> int:
    try:
        payload = JSON_ADAPTER.validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        write_json(out, {"status": "failed", "reason": "invalid_tool_matrix_report"})
        return 1
    errors: list[str] = []
    if payload.get("environment") != require_environment:
        errors.append("environment_mismatch")
    if payload.get("status") != "passed":
        errors.append("source_report_not_passed")
    cleanup = payload.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("complete") is not True:
        errors.append("cleanup_incomplete")
    write_json(out, {"status": "failed" if errors else "passed", "errors": errors})
    return 0 if not errors else 1


def _manifest_tools(manifest: Path) -> tuple[frozenset[str], list[str]]:
    try:
        payload = JSON_ADAPTER.validate_json(manifest.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return frozenset(), ["invalid_manifest"]
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return frozenset(), ["missing_scenarios"]
    tools = {
        str(item["tool"])
        for item in scenarios
        if isinstance(item, dict) and isinstance(item.get("tool"), str)
    }
    return frozenset(tools), []
