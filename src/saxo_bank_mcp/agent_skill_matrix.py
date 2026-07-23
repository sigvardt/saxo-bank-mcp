from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_install_qa import load_verified_install_report

EXPECTED_TOOL_COUNT = 39
SCENARIO_MANIFEST = Path(__file__).resolve().parents[2] / "data/saxo/agent_tool_scenarios.json"


class ScenarioEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    tool: str


class ScenarioManifest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    scenarios: tuple[ScenarioEntry, ...]


class ToolCallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    status: Literal["completed", "expected_refusal"]
    mcp_call_observed: Literal[True]
    result_parsed: Literal[True]
    skipped: Literal[False]
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class MatrixCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    uncleaned_resources: Literal[0]


class ExecutedMatrixReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    execution_mode: Literal["sim_execution"]
    environment: Literal["SIM"]
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    install_report: Path
    install_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_count: int
    unique_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    unexpected_tools: tuple[str, ...]
    expected_call_count: int
    tool_calls: tuple[ToolCallEvidence, ...] = Field(min_length=1)
    before_state_fingerprint: dict[str, JsonValue]
    after_state_fingerprint: dict[str, JsonValue]
    cleanup: MatrixCleanup
    unexpected_skips: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimFixtureOptions:
    stock_uic: str | None
    amount: str | None
    limit_price: str | None
    modified_limit_price: str | None
    option_uics: str | None
    stream_uic: str | None


@dataclass(frozen=True, slots=True)
class MatrixPlanOptions:
    manifest: Path
    environment: str
    require_tools: int
    install_report: Path
    fixtures: SimFixtureOptions
    out: Path


def build_manifest_matrix_report(options: MatrixPlanOptions) -> int:
    install, install_errors = load_verified_install_report(options.install_report)
    if install is None:
        reason = (
            "invalid_install_report"
            if "invalid_install_report" in install_errors
            else "install_report_not_verified"
        )
        write_json(options.out, {"status": "failed", "reason": reason, "errors": install_errors})
        return 1
    tools, manifest_errors = _manifest_tools(options.manifest)
    fixture_errors = _fixture_errors(options.fixtures)
    errors = [*manifest_errors, *fixture_errors]
    if options.environment != "SIM":
        errors.append("environment_not_sim")
    if options.require_tools != EXPECTED_TOOL_COUNT or len(tools) != options.require_tools:
        errors.append("tool_count_mismatch")
    if install.expected_tools != options.require_tools:
        errors.append("install_tool_count_mismatch")
    if errors:
        write_json(options.out, {"status": "failed", "errors": errors})
        return 1
    write_json(
        options.out,
        {
            "status": "validated",
            "execution_mode": "manifest_validation",
            "environment": options.environment,
            "candidate_commit": install.candidate_commit,
            "install_report": str(options.install_report),
            "install_report_sha256": _sha256_file(options.install_report),
            "tool_count": len(tools),
            "unique_tools": sorted(tools),
            "created_mcp_calls": 0,
            "created_saxo_calls": 0,
            "execution_proof": False,
            "errors": [],
        },
    )
    return 0


def verify_matrix_report(*, report_path: Path, require_environment: str, out: Path) -> int:
    report, errors = load_verified_matrix_report(report_path, require_environment)
    if report is None:
        write_json(out, {"status": "failed", "errors": errors})
        return 1
    write_json(
        out,
        {
            "status": "passed",
            "execution_mode": "sim_execution_verification",
            "environment": report.environment,
            "candidate_commit": report.candidate_commit,
            "tool_count": report.tool_count,
            "tool_call_count": len(report.tool_calls),
            "state_unchanged": (
                report.before_state_fingerprint == report.after_state_fingerprint
            ),
            "cleanup_complete": report.cleanup.complete,
            "errors": [],
        },
    )
    return 0


def load_verified_matrix_report(
    path: Path,
    require_environment: str = "SIM",
) -> tuple[ExecutedMatrixReport | None, tuple[str, ...]]:
    try:
        report = ExecutedMatrixReport.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError:
        return None, ("invalid_tool_matrix_report",)
    except json.JSONDecodeError:
        return None, ("invalid_tool_matrix_report",)
    except ValidationError as exc:
        roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
        errors = ["tool_matrix_report_schema_invalid"]
        if "tool_calls" in roots:
            errors.append("tool_call_evidence_missing")
        return None, tuple(errors)
    errors = _executed_report_errors(report, require_environment)
    return (report, ()) if not errors else (None, tuple(errors))


def _executed_report_errors(report: ExecutedMatrixReport, environment: str) -> list[str]:
    return [
        *_install_binding_errors(report),
        *_tool_call_errors(report),
        *_matrix_state_errors(report, environment),
    ]


def _install_binding_errors(report: ExecutedMatrixReport) -> list[str]:
    errors: list[str] = []
    install, install_errors = load_verified_install_report(report.install_report)
    if install is None:
        errors.extend(("install_report_not_verified", *install_errors))
    elif install.candidate_commit != report.candidate_commit:
        errors.append("install_candidate_commit_mismatch")
    if report.source_commit != report.candidate_commit:
        errors.append("source_commit_mismatch")
    if _sha256_file(report.install_report) != report.install_report_sha256:
        errors.append("install_report_digest_mismatch")
    return errors


def _tool_call_errors(report: ExecutedMatrixReport) -> list[str]:
    errors: list[str] = []
    call_tools = tuple(call.tool for call in report.tool_calls)
    if len(call_tools) != EXPECTED_TOOL_COUNT or len(set(call_tools)) != len(call_tools):
        errors.append("tool_call_count_mismatch")
    if report.expected_call_count != len(call_tools) or report.tool_count != len(call_tools):
        errors.append("expected_call_count_mismatch")
    if set(call_tools) != set(report.unique_tools):
        errors.append("tool_call_coverage_mismatch")
    expected_tools, manifest_errors = _manifest_tools(SCENARIO_MANIFEST)
    if manifest_errors or set(call_tools) != set(expected_tools):
        errors.append("scenario_tool_coverage_mismatch")
    return errors


def _matrix_state_errors(report: ExecutedMatrixReport, environment: str) -> list[str]:
    errors: list[str] = []
    if report.environment != environment:
        errors.append("environment_mismatch")
    if report.missing_tools or report.unexpected_tools or report.unexpected_skips or report.errors:
        errors.append("matrix_report_contains_errors")
    if (
        not report.before_state_fingerprint
        or report.before_state_fingerprint != report.after_state_fingerprint
    ):
        errors.append("state_fingerprint_mismatch")
    return errors


def _manifest_tools(manifest: Path) -> tuple[frozenset[str], list[str]]:
    try:
        payload = ScenarioManifest.model_validate_json(manifest.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return frozenset(), ["invalid_manifest"]
    tools = {item.tool for item in payload.scenarios}
    return frozenset(tools), []


def _fixture_errors(fixtures: SimFixtureOptions) -> list[str]:
    values = {
        "stock_uic": fixtures.stock_uic,
        "amount": fixtures.amount,
        "limit_price": fixtures.limit_price,
        "modified_limit_price": fixtures.modified_limit_price,
        "option_uics": fixtures.option_uics,
        "stream_uic": fixtures.stream_uic,
    }
    return [f"missing_fixture_{name}" for name, value in values.items() if not value]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
