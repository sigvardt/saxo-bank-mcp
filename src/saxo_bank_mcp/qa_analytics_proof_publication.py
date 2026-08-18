"""Strict publication envelope for executed Codex-native analytics proofs."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.qa_analytics_proof_failure import CodexNativeVerifiedChildFailure
from saxo_bank_mcp.qa_analytics_proof_producer import (
    CodexNativeBoundaryPhase,
    CodexNativeCleanupStatus,
    CodexNativeCommandState,
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
    boundary_phase: CodexNativeBoundaryPhase = "producer_validation"
    command_state: CodexNativeCommandState = "not_started"
    cleanup_status: CodexNativeCleanupStatus = "unknown"
    runtime_consumption_intent_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    runtime_cleanup_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
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
    outer_runtime_cleanup_status: CodexNativeCleanupStatus = "unknown"
    reason: str = Field(pattern=r"^proof_[a-z0-9_]{1,122}$")
    redacted_publication: Literal[True] = True
    boundary_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_digest(self) -> Self:
        digests = (
            self.runtime_consumption_intent_sha256,
            self.runtime_cleanup_receipt_sha256,
        )
        if self.cleanup_status == "complete":
            if any(item is None for item in digests):
                raise ValueError("native boundary cleanup receipt missing")
        elif self.cleanup_status == "failed":
            if (
                self.runtime_cleanup_receipt_sha256 is not None
                and self.runtime_consumption_intent_sha256 is None
            ):
                raise ValueError("native boundary cleanup receipt lacks its intent")
        elif any(item is not None for item in digests):
            raise ValueError("unproved native boundary cleanup cannot have receipts")
        if self.outer_runtime_cleanup_status != self.cleanup_status:
            raise ValueError("native boundary cleanup states do not match")
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
        accepted_digests = {_digest(material)}
        if (
            isinstance(self.result, CodexNativeVerifiedChildFailure)
            and self.result.agent_evaluation_failure_summary is None
        ):
            legacy_material = dict(material)
            legacy_result = dict(legacy_material["result"])
            legacy_result.pop("agent_evaluation_failure_summary")
            legacy_material["result"] = legacy_result
            accepted_digests.add(_digest(legacy_material))
        if self.publication_sha256 not in accepted_digests:
            raise ValueError("native publication digest mismatch")
        return self


def build_codex_native_boundary_failure(  # noqa: PLR0913
    *,
    candidate_commit: str,
    reason: str,
    boundary_phase: CodexNativeBoundaryPhase = "producer_validation",
    command_state: CodexNativeCommandState = "not_started",
    cleanup_status: CodexNativeCleanupStatus = "unknown",
    runtime_consumption_intent_sha256: str | None = None,
    runtime_cleanup_receipt_sha256: str | None = None,
) -> CodexNativeBoundaryFailureReceipt:
    material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_boundary_failure",
        "status": "refused",
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "failure_evidence_status": "missing",
        "producer_authenticated": False,
        "boundary_phase": boundary_phase,
        "command_state": command_state,
        "cleanup_status": cleanup_status,
        "runtime_consumption_intent_sha256": runtime_consumption_intent_sha256,
        "runtime_cleanup_receipt_sha256": runtime_cleanup_receipt_sha256,
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
        "outer_runtime_cleanup_status": cleanup_status,
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
