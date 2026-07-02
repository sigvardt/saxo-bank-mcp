from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class McpExecutionSurface(StrEnum):
    FASTMCP_TOOL_CALL = "fastmcp_tool_call"
    FASTMCP_EVIDENCE_BUNDLE = "fastmcp_evidence_bundle"


class McpExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: str = Field(min_length=1)
    tool_id: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    mock_only: bool = False
    unit_tests_only: bool = False
    internal_client_only: bool = False
    pre_implementation_only: bool = False


ACCEPTED_MCP_EXECUTION_SURFACES: Final = frozenset(
    surface.value for surface in McpExecutionSurface
)


def referenced_mcp_execution_paths(
    execution: McpExecutionEvidence | None,
    base_dir: Path,
) -> tuple[Path, ...]:
    if execution is None:
        return ()
    candidate = Path(execution.evidence)
    return (candidate if candidate.is_absolute() else base_dir / candidate,)


def validate_mcp_execution(
    tool_id: str,
    execution: McpExecutionEvidence | None,
    base_dir: Path,
    errors: list[str],
) -> None:
    if execution is None:
        errors.append("complete tools require mcp_execution")
        return

    if execution.tool_id != tool_id:
        errors.append("complete tool mcp_execution.tool_id must match tool_id")

    if execution.surface not in ACCEPTED_MCP_EXECUTION_SURFACES:
        errors.append(
            "complete tool mcp_execution.surface must be "
            "fastmcp_tool_call or fastmcp_evidence_bundle",
        )

    if (
        execution.mock_only
        or execution.unit_tests_only
        or execution.internal_client_only
        or execution.pre_implementation_only
    ):
        errors.append(
            "complete tool cannot be marked "
            "mock_only/unit_tests_only/internal_client_only/pre_implementation_only",
        )

    evidence_path = referenced_mcp_execution_paths(execution, base_dir)[0]
    if not evidence_path.exists():
        errors.append(f"missing mcp_execution evidence path: {evidence_path}")
        return

    try:
        if evidence_path.is_file() and evidence_path.stat().st_size == 0:
            errors.append(f"empty mcp_execution evidence file: {evidence_path}")
        elif evidence_path.is_dir() and not any(evidence_path.iterdir()):
            errors.append(f"empty mcp_execution evidence directory: {evidence_path}")
    except OSError as exc:
        errors.append(
            f"unreadable mcp_execution evidence path: "
            f"{evidence_path}: {type(exc).__name__}",
        )
