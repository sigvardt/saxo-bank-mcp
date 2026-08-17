"""Strict publication envelope for executed Codex-native analytics proofs."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.qa_analytics_proof_failure import CodexNativeVerifiedChildFailure
from saxo_bank_mcp.qa_analytics_proof_producer import (
    CodexNativeVerifiedInstalledProofValidation,
)

_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_COMMIT_PATTERN = r"^[a-f0-9]{40}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class CodexNativeBoundaryFailureReceipt(_StrictModel):
    """Parent-issued unknown result when no authenticated child receipt exists."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_boundary_failure"] = "codex_native_boundary_failure"
    status: Literal["refused"] = "refused"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    failure_evidence_status: Literal["missing"] = "missing"
    producer_authenticated: Literal[False] = False
    completed_phases: None = None
    current_phase: None = None
    sim_preflight_status: Literal["unknown"] = "unknown"
    sim_preflight: None = None
    network_call_made: None = None
    model_event_count: None = None
    mcp_event_count: None = None
    saxo_event_count: None = None
    execution_performed: None = None
    broker_write_made: None = None
    live_mutation_calls: None = None
    purchase_occurred: None = None
    disclaimer_response_made: None = None
    child_cleanup_status: Literal["unknown"] = "unknown"
    outer_runtime_cleanup_status: Literal["unknown"] = "unknown"
    reason: str = Field(pattern=r"^proof_[a-z0-9_]{1,122}$")
    redacted_publication: Literal[True] = True
    boundary_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_digest(self) -> Self:
        material = self.model_dump(mode="json", exclude={"boundary_receipt_sha256"})
        if self.boundary_receipt_sha256 != _digest(material):
            raise ValueError("native boundary receipt digest mismatch")
        return self


type CodexNativePublishedResult = (
    CodexNativeVerifiedInstalledProofValidation
    | CodexNativeVerifiedChildFailure
    | CodexNativeBoundaryFailureReceipt
)
type CodexNativePublishedResultKind = Literal[
    "verified_result",
    "verified_child_failure",
    "boundary_failure",
]


class CodexNativeProofPublication(_StrictModel):
    """One digest covers all native proof publication fields and its typed result."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_proof_publication"] = "codex_native_proof_publication"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    analysis_kind_count: int = Field(ge=1)
    evidence_receipt_count: int = Field(ge=1)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_kind: CodexNativePublishedResultKind
    result: CodexNativePublishedResult
    redacted_publication: Literal[True] = True
    publication_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_publication(self) -> Self:
        expected_type = {
            "verified_result": CodexNativeVerifiedInstalledProofValidation,
            "verified_child_failure": CodexNativeVerifiedChildFailure,
            "boundary_failure": CodexNativeBoundaryFailureReceipt,
        }[self.result_kind]
        if type(self.result) is not expected_type:
            raise ValueError("native publication result kind mismatch")
        if self.result.candidate_commit != self.candidate_commit:
            raise ValueError("native publication candidate mismatch")
        nested_contract = getattr(self.result, "contract_sha256", self.contract_sha256)
        if nested_contract != self.contract_sha256:
            raise ValueError("native publication contract mismatch")
        if self.result.harness_policy != self.harness_policy:
            raise ValueError("native publication policy mismatch")
        material = self.model_dump(mode="json", exclude={"publication_sha256"})
        if self.publication_sha256 != _digest(material):
            raise ValueError("native publication digest mismatch")
        return self


def build_codex_native_boundary_failure(
    *,
    candidate_commit: str,
    reason: str,
) -> CodexNativeBoundaryFailureReceipt:
    material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_boundary_failure",
        "status": "refused",
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "failure_evidence_status": "missing",
        "producer_authenticated": False,
        "completed_phases": None,
        "current_phase": None,
        "sim_preflight_status": "unknown",
        "sim_preflight": None,
        "network_call_made": None,
        "model_event_count": None,
        "mcp_event_count": None,
        "saxo_event_count": None,
        "execution_performed": None,
        "broker_write_made": None,
        "live_mutation_calls": None,
        "purchase_occurred": None,
        "disclaimer_response_made": None,
        "child_cleanup_status": "unknown",
        "outer_runtime_cleanup_status": "unknown",
        "reason": reason,
        "redacted_publication": True,
    }
    return CodexNativeBoundaryFailureReceipt.model_validate(
        {**material, "boundary_receipt_sha256": _digest(material)},
        strict=True,
    )


def build_codex_native_proof_publication(  # noqa: PLR0913
    *,
    candidate_commit: str,
    analysis_kind_count: int,
    evidence_receipt_count: int,
    contract_sha256: str,
    result_kind: CodexNativePublishedResultKind,
    result: CodexNativePublishedResult,
) -> CodexNativeProofPublication:
    material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_publication",
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "analysis_kind_count": analysis_kind_count,
        "evidence_receipt_count": evidence_receipt_count,
        "contract_sha256": contract_sha256,
        "result_kind": result_kind,
        "result": result.model_dump(mode="json"),
        "redacted_publication": True,
    }
    return CodexNativeProofPublication.model_validate(
        {
            **{key: value for key, value in material.items() if key != "result"},
            "result": result,
            "publication_sha256": _digest(material),
        },
        strict=True,
    )


def verify_codex_native_proof_publication(raw: str) -> CodexNativeProofPublication:
    """Strict round-trip parser; malformed, extra, or tampered JSON is rejected."""
    return CodexNativeProofPublication.model_validate_json(raw, strict=True)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
