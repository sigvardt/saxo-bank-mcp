from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp._evidence import JsonValue


class Counts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: int = Field(gt=0)
    operations: int = Field(gt=0)
    implemented: int = Field(gt=0)
    refused: int = Field(gt=0)
    service_groups: int = Field(gt=0)

    @model_validator(mode="after")
    def operation_parts_match(self) -> Self:
        if self.implemented + self.refused != self.operations:
            msg = "implemented and refused counts must equal operations"
            raise ValueError(msg)
        return self


class TaskClaim(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    status: str | None = None
    source_commit: str | None = None
    sha: str | None = None
    commit: str | None = None
    counts: Counts | None = None

    def bound_commit(self) -> str | None:
        return self.source_commit or self.sha or self.commit


class Completion(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    complete: Literal[True]


class AgentEvalEvidence(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    status: Literal["passed"]
    execution_mode: Literal["model_execution"]
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    skipped_count: Literal[0]
    global_state_unchanged: Literal[True]
    cleanup: Completion
    live_mutation_calls: Literal[0] | None = None


class LiveSource(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    git_head: str = Field(pattern=r"^[a-f0-9]{40}$")


class LiveProof(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    status: Literal["passed"]
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    source: LiveSource
    environment: Literal["LIVE"]
    state_unchanged: Literal[True]
    ledger_complete: Literal[True]
    negative_proof_available: Literal[True]
    live_mutation_calls: Literal[0]
    purchase_occurred: Literal[False]
    cleanup: Completion


class PrivacyEvidence(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    status: Literal["passed"]
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    findings_count: Literal[0]
    scan_errors_count: Literal[0]


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    release: str
    source_commit: str
    public_fingerprint: str
    counts: Counts
    task_evidence: dict[str, str]
    artifact_digests: dict[str, str]
    global_state_unchanged: Literal[True]
    sim_state_unchanged: Literal[True]
    cleanup_complete: Literal[True]
    privacy_scan_clean: Literal[True]
    live_mutation_calls: Literal[0]
    purchase_occurred: Literal[False]
    errors: tuple[str, ...]

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class ReleaseAssembleOptions:
    repo: Path
    plan: Path | None
    evidence_root: Path
    release: str
    source_commit: str
    verify_live_proof: Path | None
    out: Path
    latest: Path
    check: bool


@dataclass(frozen=True, slots=True)
class ReleaseInputs:
    counts: Counts
    tasks: dict[str, Path]
    install: Path
    matrix: Path
    sim_evals: Path
    live_evals: Path
    live: Path
    privacy: Path
