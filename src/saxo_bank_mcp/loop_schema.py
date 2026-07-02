from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from saxo_bank_mcp.loop_mcp_execution import (
    McpExecutionEvidence,
    referenced_mcp_execution_paths,
    validate_mcp_execution,
)

MIN_AUDIT_CHARS = 50
TRIVIAL_ARTIFACT_CONTENT = frozenset(
    {"", "0", "0.0", "[]", "[null]", "{}", "{}{}", "false", "null"},
)


class CompletionStatus(StrEnum):
    COMPLETE = "complete"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    EXEMPT = "exempt"


class RiskClass(StrEnum):
    HEALTH = "health"
    READ = "read"
    WRITE = "write"
    MONEY_MOVING = "money_moving"


class FixedFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding: str = Field(min_length=1)
    fix: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class TribunalCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1)
    status: CompletionStatus
    risk_class: RiskClass = RiskClass.READ
    mcp_tool_schema: str = Field(min_length=1)
    task: str = Field(min_length=1)
    input: str = Field(min_length=1)
    output: str | None = None
    error: str | None = None
    audit: str = Field(min_length=1)
    tribunal_run: str = Field(min_length=1)
    fixed_feedback: tuple[FixedFeedback, ...] = ()
    remaining_actionable_feedback: tuple[str, ...] = ()
    refusal_reason: str | None = None
    exemption_reason: str | None = None
    mcp_execution: McpExecutionEvidence | None = None
    completed_at: datetime

    @model_validator(mode="after")
    def validate_status_contract(self) -> Self:
        if self.status is CompletionStatus.COMPLETE and self.output is None:
            raise ValueError("complete tools require output")
        if self.status is CompletionStatus.REFUSED and not self.refusal_reason:
            raise ValueError("refused tools require refusal_reason")
        if self.status is CompletionStatus.EXEMPT and not self.exemption_reason:
            raise ValueError("exempt tools require exemption_reason")
        if self.status is not CompletionStatus.REFUSED and self.refusal_reason:
            raise ValueError("refusal_reason is only valid for refused tools")
        if self.status is not CompletionStatus.EXEMPT and self.exemption_reason:
            raise ValueError("exemption_reason is only valid for exempt tools")
        return self


class ArtifactValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    tool_id: str | None
    status: CompletionStatus | None
    errors: tuple[str, ...]


def load_completion_file(path: Path) -> TribunalCompletion:
    return TribunalCompletion.model_validate_json(path.read_text(encoding="utf-8"))


def referenced_paths(completion: TribunalCompletion, base_dir: Path) -> tuple[Path, ...]:
    values = (
        completion.mcp_tool_schema,
        completion.task,
        completion.input,
        completion.output,
        completion.error,
        completion.audit,
        completion.tribunal_run,
    )
    paths: list[Path] = []
    for value in values:
        if value is None:
            continue
        candidate = Path(value)
        paths.append(candidate if candidate.is_absolute() else base_dir / candidate)
    paths.extend(referenced_mcp_execution_paths(completion.mcp_execution, base_dir))
    return tuple(paths)


def _artifact_path(value: str, base_dir: Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base_dir / candidate


def _compact_artifact_text(path: Path, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError as exc:
        errors.append(f"unreadable referenced evidence: {type(exc).__name__}")
    return None


def _validate_complete_evidence(
    completion: TribunalCompletion,
    base_dir: Path,
    errors: list[str],
) -> None:
    if completion.status is not CompletionStatus.COMPLETE:
        return

    if completion.remaining_actionable_feedback:
        errors.append(
            "complete tools cannot have remaining_actionable_feedback"
        )

    if not completion.fixed_feedback:
        errors.append("complete tools must have non-empty fixed_feedback from peer review")

    validate_mcp_execution(completion.tool_id, completion.mcp_execution, base_dir, errors)
    _validate_money_moving_completion_flags(completion, base_dir, errors)

    if completion.output is None:
        errors.append("complete tools require output")
        return

    output_path = _artifact_path(completion.output, base_dir)
    if output_path.exists():
        content = _compact_artifact_text(output_path, errors)
        if content is not None:
            compact_content = "".join(content.split())
            if compact_content in TRIVIAL_ARTIFACT_CONTENT:
                errors.append(f"complete tool has trivial output content: {content!r}")

    audit_path = _artifact_path(completion.audit, base_dir)
    if audit_path.exists():
        content = _compact_artifact_text(audit_path, errors)
        if content is not None and len(content) < MIN_AUDIT_CHARS:
            errors.append(
                "complete tool audit file must contain at least "
                f"{MIN_AUDIT_CHARS} characters of safety/control descriptions"
            )


def _validate_money_moving_completion_flags(
    completion: TribunalCompletion,
    base_dir: Path,
    errors: list[str],
) -> None:
    if completion.risk_class is not RiskClass.MONEY_MOVING:
        return
    payload = _completion_output_payload(completion, base_dir)
    if not isinstance(payload, Mapping):
        return
    payload_mapping = cast("Mapping[str, object]", payload)
    completion_claim_allowed = payload_mapping.get("completion_claim_allowed")
    real_mutation_proven = payload_mapping.get("real_mutation_proven")
    if completion_claim_allowed is not False and real_mutation_proven is not False:
        return
    evidence_text = " ".join(
        (
            completion.audit,
            completion.output or "",
            "" if completion.mcp_execution is None else completion.mcp_execution.notes,
            " ".join(f"{item.finding} {item.fix}" for item in completion.fixed_feedback),
        ),
    ).lower()
    if "completion_claim_allowed=false" not in evidence_text:
        errors.append(
            "money-moving complete artifact with completion_claim_allowed=false must "
            "state that limitation",
        )
    if "not proven mutation" not in evidence_text and "not proven" not in evidence_text:
        errors.append(
            "money-moving complete artifact with real_mutation_proven=false must state "
            "that mutation completion is not proven",
        )


def _completion_output_payload(
    completion: TribunalCompletion,
    base_dir: Path,
) -> object:
    if completion.output is None:
        return None
    output_path = _artifact_path(completion.output, base_dir)
    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _judge_verdict_status(path: Path) -> str | None:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    payload_mapping = cast("Mapping[str, object]", payload)
    verdict = payload_mapping.get("verdict")
    if not isinstance(verdict, Mapping):
        return None
    verdict_mapping = cast("Mapping[str, object]", verdict)
    status = verdict_mapping.get("status")
    return status if isinstance(status, str) else None


def _latest_tribunal_verdict_status(
    run_path: Path,
    errors: list[str],
) -> str | None:
    if not (run_path / "normalized.json").is_file():
        errors.append(f"tribunal_run missing normalized.json: {run_path}")
    judge_outputs = tuple(sorted(run_path.glob("rounds/round-*/judge-output.json")))
    if not judge_outputs:
        errors.append(f"tribunal_run missing judge-output.json: {run_path}")
        return None
    latest_status = None
    for judge_output in judge_outputs:
        latest_status = _judge_verdict_status(judge_output) or latest_status
    if latest_status is None:
        errors.append(f"tribunal_run has no parseable judge verdict: {run_path}")
    return latest_status


def _tribunal_run_mentions_tool(
    run_path: Path,
    tool_id: str,
    errors: list[str],
) -> None:
    judge_input_path = run_path / "judge-input.md"
    if not judge_input_path.is_file():
        errors.append(f"complete tool tribunal_run missing judge-input.md: {run_path}")
        return
    try:
        judge_input = judge_input_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        errors.append(f"unreadable judge-input.md: {run_path}: {type(exc).__name__}")
        return
    tool_slug = tool_id.replace("_", "-")
    if tool_id not in judge_input and tool_slug not in judge_input:
        errors.append(f"complete tool tribunal_run does not mention tool_id: {tool_id}")


def _validate_tribunal_run(
    completion: TribunalCompletion,
    base_dir: Path,
    errors: list[str],
) -> None:
    run_path = _artifact_path(completion.tribunal_run, base_dir)
    if not run_path.exists():
        return
    if not run_path.is_dir():
        errors.append(f"tribunal_run must be a run directory: {run_path}")
        return
    latest_verdict_status = _latest_tribunal_verdict_status(run_path, errors)
    if latest_verdict_status is None:
        return
    if completion.status is not CompletionStatus.COMPLETE:
        return
    if latest_verdict_status != "confirmed":
        errors.append(
            "complete tool tribunal_run highest verdict must be confirmed: "
            f"{completion.tool_id} -> {latest_verdict_status}"
        )
    _tribunal_run_mentions_tool(run_path, completion.tool_id, errors)


def _raw_artifact_identity(path: Path) -> tuple[str | None, CompletionStatus | None]:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    payload_mapping = cast("Mapping[str, object]", payload)
    raw_tool_id = payload_mapping.get("tool_id")
    raw_status = payload_mapping.get("status")
    status = None
    if isinstance(raw_status, str):
        try:
            status = CompletionStatus(raw_status)
        except ValueError:
            status = None
    return raw_tool_id if isinstance(raw_tool_id, str) else None, status


def validate_completion_artifact(path: Path) -> ArtifactValidation:
    try:
        completion = load_completion_file(path)
    except (OSError, UnicodeError, ValidationError) as exc:
        raw_tool_id, raw_status = _raw_artifact_identity(path)
        message = (
            type(exc).__name__
            if isinstance(exc, OSError | UnicodeError)
            else exc.errors()[0]["msg"]
        )
        return ArtifactValidation(
            path=str(path),
            tool_id=raw_tool_id,
            status=raw_status,
            errors=(f"schema: {message}",),
        )

    errors: list[str] = []
    if completion.status is CompletionStatus.EXEMPT and completion.tool_id != "saxo_health":
        errors.append("only saxo_health may use status=exempt")
    for candidate in referenced_paths(completion, path.parent):
        if not candidate.exists():
            errors.append(f"missing referenced path: {candidate}")
        else:
            try:
                if candidate.is_file() and candidate.stat().st_size == 0:
                    errors.append(f"empty referenced file: {candidate}")
                elif candidate.is_dir() and not any(candidate.iterdir()):
                    errors.append(f"empty referenced directory: {candidate}")
            except OSError as exc:
                errors.append(f"unreadable referenced path: {candidate}: {type(exc).__name__}")

    _validate_complete_evidence(completion, path.parent, errors)
    _validate_tribunal_run(completion, path.parent, errors)

    return ArtifactValidation(
        path=str(path),
        tool_id=completion.tool_id,
        status=completion.status,
        errors=tuple(errors),
    )
