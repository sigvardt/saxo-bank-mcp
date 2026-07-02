from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    completed_at: datetime

    @model_validator(mode="after")
    def validate_status_contract(self) -> Self:
        if self.status is CompletionStatus.COMPLETE and self.remaining_actionable_feedback:
            raise ValueError("complete tools cannot have remaining_actionable_feedback")
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
    return tuple(paths)


def _validate_write_money_moving_completion(
    completion: TribunalCompletion,
    base_dir: Path,
    errors: list[str],
) -> None:
    is_write_or_money = completion.risk_class in (RiskClass.WRITE, RiskClass.MONEY_MOVING)
    if not (completion.status is CompletionStatus.COMPLETE and is_write_or_money):
        return

    if not completion.fixed_feedback:
        errors.append(
            "write/money_moving complete tools must have non-empty fixed_feedback from peer review"
        )

    if completion.output is None:
        errors.append("write/money_moving complete tools require output")
        return

    p = Path(completion.output)
    output_path = p if p.is_absolute() else base_dir / completion.output
    if output_path.exists():
        try:
            content = output_path.read_text(encoding="utf-8", errors="ignore").strip()
            compact_content = "".join(content.split())
            if compact_content in TRIVIAL_ARTIFACT_CONTENT:
                errors.append(
                    f"write/money_moving complete tool has trivial output content: {content!r}"
                )
        except OSError as exc:
            errors.append(f"unreadable output evidence: {type(exc).__name__}")

    ap = Path(completion.audit)
    audit_path = ap if ap.is_absolute() else base_dir / completion.audit
    if audit_path.exists():
        try:
            content = audit_path.read_text(encoding="utf-8", errors="ignore").strip()
            if len(content) < MIN_AUDIT_CHARS:
                errors.append(
                    "write/money_moving audit file must contain at least "
                    f"{MIN_AUDIT_CHARS} characters of safety/control descriptions"
                )
        except OSError as exc:
            errors.append(f"unreadable audit evidence: {type(exc).__name__}")


def validate_completion_artifact(path: Path) -> ArtifactValidation:
    try:
        completion = load_completion_file(path)
    except (OSError, UnicodeError, ValidationError) as exc:
        message = (
            type(exc).__name__
            if isinstance(exc, OSError | UnicodeError)
            else exc.errors()[0]["msg"]
        )
        return ArtifactValidation(
            path=str(path),
            tool_id=None,
            status=None,
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

    _validate_write_money_moving_completion(completion, path.parent, errors)

    return ArtifactValidation(
        path=str(path),
        tool_id=completion.tool_id,
        status=completion.status,
        errors=tuple(errors),
    )
