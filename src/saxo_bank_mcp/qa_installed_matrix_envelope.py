"""Strict candidate-bound envelope for one installed SIM matrix child result."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt

_ANALYSIS_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")

InstalledMatrixFailurePhase = Literal[
    "input_validation",
    "runtime_setup",
    "server_setup",
    "matrix_execution",
    "cleanup",
    "envelope_validation",
]
InstalledMatrixFailureCategory = Literal[
    "io_error",
    "runtime_error",
    "type_error",
    "validation_error",
    "unexpected_error",
]
InstalledMatrixFailureDetail = Literal[
    "unknown",
    "pydantic_analytics_case_receipt",
    "pydantic_controlled_sim_case_receipt",
    "pydantic_controlled_sim_lifecycle_receipt",
    "pydantic_ghost_lifecycle_evidence",
    "pydantic_ghost_workflow_request",
    "pydantic_matrix_scenario_receipt",
    "pydantic_proof_profile",
    "pydantic_proof_profile_catalog",
    "pydantic_sim_tool_matrix_receipt",
    "pydantic_sim_tool_matrix_pass_incomplete",
    "pydantic_stored_backtest_execution_context",
    "pydantic_strategy_definition",
    "origin_controlled_sim_ghost_phase",
    "origin_controlled_sim_lifecycle_receipt",
    "origin_finalize",
    "origin_installed_matrix_session",
    "origin_run_matrix",
]


def matrix_receipt_sha256(matrix: SimToolMatrixReceipt) -> str:
    """Hash one strict matrix receipt using its canonical JSON representation."""
    payload = json.dumps(
        matrix.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class InstalledMatrixEnvelope(BaseModel):
    """Bind a child matrix to the exact candidate and ordered request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1"] = "1"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    analysis_kinds: tuple[str, ...] = Field(min_length=1)
    matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    matrix: SimToolMatrixReceipt

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if any(_ANALYSIS_KIND_PATTERN.fullmatch(kind) is None for kind in self.analysis_kinds):
            raise ValueError("installed matrix analysis kind is invalid")
        if len(self.analysis_kinds) != len(set(self.analysis_kinds)):
            raise ValueError("installed matrix analysis kinds must be unique")
        if self.matrix_sha256 != matrix_receipt_sha256(self.matrix):
            raise ValueError("installed matrix digest mismatch")
        return self


def installed_matrix_failure_sha256(
    *,
    candidate_commit: str,
    analysis_kinds: tuple[str, ...],
    failure_phase: InstalledMatrixFailurePhase,
    failure_category: InstalledMatrixFailureCategory,
    failure_detail: InstalledMatrixFailureDetail | None = None,
) -> str:
    """Hash one path-free installed-child failure description."""
    material = {
        "analysis_kinds": list(analysis_kinds),
        "candidate_commit": candidate_commit,
        "failure_category": failure_category,
        "failure_phase": failure_phase,
        "receipt_kind": "installed_matrix_child_failure",
        "schema_version": "1",
    }
    if failure_detail is not None:
        material["failure_detail"] = failure_detail
    payload = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class InstalledMatrixFailureEnvelope(BaseModel):
    """Bind one privacy-safe child failure class to the exact matrix request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["installed_matrix_child_failure"] = "installed_matrix_child_failure"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    analysis_kinds: tuple[str, ...] = Field(min_length=1)
    failure_phase: InstalledMatrixFailurePhase
    failure_category: InstalledMatrixFailureCategory
    failure_detail: InstalledMatrixFailureDetail | None = None
    envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if any(_ANALYSIS_KIND_PATTERN.fullmatch(kind) is None for kind in self.analysis_kinds):
            raise ValueError("installed matrix analysis kind is invalid")
        if len(self.analysis_kinds) != len(set(self.analysis_kinds)):
            raise ValueError("installed matrix analysis kinds must be unique")
        expected = installed_matrix_failure_sha256(
            candidate_commit=self.candidate_commit,
            analysis_kinds=self.analysis_kinds,
            failure_phase=self.failure_phase,
            failure_category=self.failure_category,
            failure_detail=self.failure_detail,
        )
        if self.envelope_sha256 != expected:
            raise ValueError("installed matrix failure digest mismatch")
        return self


def build_installed_matrix_failure_envelope(
    *,
    candidate_commit: str,
    analysis_kinds: tuple[str, ...],
    failure_phase: InstalledMatrixFailurePhase,
    failure_category: InstalledMatrixFailureCategory,
    failure_detail: InstalledMatrixFailureDetail | None = None,
) -> InstalledMatrixFailureEnvelope:
    """Build one canonical strict installed-child failure envelope."""
    return InstalledMatrixFailureEnvelope(
        candidate_commit=candidate_commit,
        analysis_kinds=analysis_kinds,
        failure_phase=failure_phase,
        failure_category=failure_category,
        failure_detail=failure_detail,
        envelope_sha256=installed_matrix_failure_sha256(
            candidate_commit=candidate_commit,
            analysis_kinds=analysis_kinds,
            failure_phase=failure_phase,
            failure_category=failure_category,
            failure_detail=failure_detail,
        ),
    )
