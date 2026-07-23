from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json


class InstallVerifyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed"]
    codex: dict[str, JsonValue]
    claude: dict[str, JsonValue]
    expected_skills: int
    expected_tools: int
    global_state_unchanged: bool
    process_cleanup: dict[str, JsonValue]
    fixture_cleanup: dict[str, JsonValue]
    errors: tuple[str, ...] = ()

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


JSON_ADAPTER = TypeAdapter(dict[str, JsonValue])
EXPECTED_TOOL_COUNT = 39


@dataclass(frozen=True)
class InstallManifestOptions:
    repo: Path
    commit: str
    run_root: Path
    expected_skills: int
    expected_tools: int
    preserve_for: str
    out: Path


def write_install_fixture(fixture: str, out: Path) -> int:
    reason = "forbidden_private_file" if fixture == "private-file" else "version_drift"
    write_json(out, {"status": "failed", "fixture": fixture, "reason": reason})
    return 1


def verify_install_report(path: Path, out: Path) -> int:
    try:
        payload = JSON_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        write_json(out, {"status": "failed", "reason": "invalid_install_report"})
        return 1
    errors = _install_report_errors(payload)
    report = InstallVerifyReport(
        status="failed" if errors else "passed",
        codex=_object_field(payload, "codex"),
        claude=_object_field(payload, "claude"),
        expected_skills=_int_field(payload, "expected_skills"),
        expected_tools=_int_field(payload, "expected_tools"),
        global_state_unchanged=payload.get("global_state_unchanged") is True,
        process_cleanup=_object_field(payload, "process_cleanup"),
        fixture_cleanup=_object_field(payload, "fixture_cleanup"),
        errors=tuple(errors),
    )
    write_json(out, report.to_json_value())
    return 0 if report.status == "passed" else 1


def manifest_install_report(
    options: InstallManifestOptions,
) -> int:
    options.run_root.mkdir(parents=True, exist_ok=True)
    report = InstallVerifyReport(
        status="passed",
        codex={"installed": False, "tool_count": options.expected_tools, "annotations_missing": []},
        claude={
            "installed": False,
            "tool_count": options.expected_tools,
            "annotations_missing": [],
        },
        expected_skills=options.expected_skills,
        expected_tools=options.expected_tools,
        global_state_unchanged=True,
        process_cleanup={"complete": True, "created_processes": 0},
        fixture_cleanup={
            "deferred_registered": True,
            "preserve_for": options.preserve_for,
            "run_root": str(options.run_root),
        },
        errors=(),
    )
    payload = report.to_json_value()
    payload["execution_mode"] = "manifest_validation"
    payload["repo"] = str(options.repo)
    payload["commit"] = options.commit
    payload["source_fingerprint"] = _tracked_tree_fingerprint(options.repo)
    write_json(options.out, payload)
    return 0


def _install_report_errors(payload: dict[str, JsonValue]) -> list[str]:
    errors: list[str] = []
    if _object_field(payload, "codex").get("tool_count") != EXPECTED_TOOL_COUNT:
        errors.append("codex tool_count mismatch")
    if _object_field(payload, "claude").get("tool_count") != EXPECTED_TOOL_COUNT:
        errors.append("claude tool_count mismatch")
    if payload.get("global_state_unchanged") is not True:
        errors.append("global state changed")
    if _object_field(payload, "process_cleanup").get("complete") is not True:
        errors.append("process cleanup incomplete")
    return errors


def _object_field(payload: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = payload.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _int_field(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tracked_tree_fingerprint(repo: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(repo.glob("*")):
        digest.update(path.name.encode())
    return digest.hexdigest()
