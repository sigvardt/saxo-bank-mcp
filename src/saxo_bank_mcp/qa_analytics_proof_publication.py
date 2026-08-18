"""Strict publication envelope for executed Codex-native analytics proofs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_analytics_proof_failure import CodexNativeVerifiedChildFailure
from saxo_bank_mcp.qa_analytics_proof_producer import (
    CodexNativeBoundaryPhase,
    CodexNativeCleanupStatus,
    CodexNativeCommandState,
    CodexNativeVerifiedInstalledProofValidation,
)

_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_COMMIT_PATTERN = r"^[a-f0-9]{40}$"
_OWNER_FILE_MODE = 0o600
_OWNER_DIRECTORY_MODE = 0o700


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
    candidate_runner_receipt_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    candidate_runner_cleanup_status: CodexNativeCleanupStatus = "unknown"
    candidate_runner_result_status: Literal["unknown", "authenticated"] = "unknown"
    candidate_runner_result_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
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
        if (
            self.candidate_runner_receipt_sha256 is None
            and self.candidate_runner_cleanup_status != "unknown"
        ):
            raise ValueError("native candidate runner cleanup lacks receipt")
        if (self.candidate_runner_result_status == "authenticated") != (
            self.candidate_runner_result_sha256 is not None
        ):
            raise ValueError("native candidate runner result authentication mismatch")
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
        accepted_digests = {_digest(material)}
        previous_material = dict(material)
        previous_material.pop("candidate_runner_result_status")
        previous_material.pop("candidate_runner_result_sha256")
        accepted_digests.add(_digest(previous_material))
        legacy_material = dict(previous_material)
        legacy_material.pop("candidate_runner_receipt_sha256")
        legacy_material.pop("candidate_runner_cleanup_status")
        accepted_digests.add(_digest(legacy_material))
        if self.boundary_receipt_sha256 not in accepted_digests:
            raise ValueError("native boundary receipt digest mismatch")
        return self


class CodexNativeCandidateRunnerReceipt(_StrictModel):
    """Path-free private entry/exit evidence for the candidate runner."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_candidate_runner"] = "codex_native_candidate_runner"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    candidate_tree: str = Field(pattern=_COMMIT_PATTERN)
    phase: Literal["entry", "exit"]
    spawned: bool
    exit_code: int | None
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_present: bool
    result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    cleanup_status: CodexNativeCleanupStatus
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        if self.phase == "entry" and self.spawned:
            raise ValueError("candidate runner entry cannot prove spawn")
        if not self.spawned and (
            self.exit_code is not None
            or self.result_present
            or self.result_sha256 is not None
            or self.cleanup_status != "not_started"
        ):
            raise ValueError("unspawned candidate runner has execution facts")
        if self.spawned and (self.phase != "exit" or self.exit_code is None):
            raise ValueError("spawned candidate runner lacks exit evidence")
        if self.result_present != (self.result_sha256 is not None):
            raise ValueError("candidate runner result digest mismatch")
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _digest(material):
            raise ValueError("candidate runner receipt digest mismatch")
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
        if isinstance(self.result, CodexNativeBoundaryFailureReceipt):
            previous_material = dict(material)
            previous_result = dict(previous_material["result"])
            previous_result.pop("candidate_runner_result_status")
            previous_result.pop("candidate_runner_result_sha256")
            previous_material["result"] = previous_result
            accepted_digests.add(_digest(previous_material))
            legacy_material = dict(previous_material)
            legacy_result = dict(legacy_material["result"])
            legacy_result.pop("candidate_runner_receipt_sha256")
            legacy_result.pop("candidate_runner_cleanup_status")
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
    candidate_runner_receipt_sha256: str | None = None,
    candidate_runner_cleanup_status: CodexNativeCleanupStatus = "unknown",
    candidate_runner_result_sha256: str | None = None,
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
        "candidate_runner_receipt_sha256": candidate_runner_receipt_sha256,
        "candidate_runner_cleanup_status": candidate_runner_cleanup_status,
        "candidate_runner_result_status": (
            "authenticated" if candidate_runner_result_sha256 is not None else "unknown"
        ),
        "candidate_runner_result_sha256": candidate_runner_result_sha256,
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


def build_codex_native_candidate_runner_receipt(  # noqa: PLR0913
    *,
    candidate_commit: str,
    candidate_tree: str,
    phase: Literal["entry", "exit"],
    spawned: bool,
    exit_code: int | None,
    command_sha256: str,
    command_schema_sha256: str,
    result_sha256: str | None,
    cleanup_status: CodexNativeCleanupStatus,
) -> CodexNativeCandidateRunnerReceipt:
    """Build one self-authenticating path-free runner receipt."""
    material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_candidate_runner",
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "phase": phase,
        "spawned": spawned,
        "exit_code": exit_code,
        "command_sha256": command_sha256,
        "command_schema_sha256": command_schema_sha256,
        "result_present": result_sha256 is not None,
        "result_sha256": result_sha256,
        "cleanup_status": cleanup_status,
    }
    return CodexNativeCandidateRunnerReceipt.model_validate(
        {**material, "receipt_sha256": _digest(material)},
        strict=True,
    )


def candidate_runner_receipt_path(result_path: Path) -> Path:
    """Return the private sibling receipt path for one proof publication."""
    return result_path.with_name(f"{result_path.name}.candidate-runner.json")


def write_codex_native_candidate_runner_receipt(
    path: Path,
    receipt: CodexNativeCandidateRunnerReceipt,
) -> bool:
    """Atomically publish an owner-only runner receipt and read it back."""
    try:
        parent = os.lstat(path.parent)
        existing = os.lstat(path) if path.exists() else None
    except OSError:
        return False
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != _OWNER_DIRECTORY_MODE
        or (
            existing is not None
            and (
                not stat.S_ISREG(existing.st_mode)
                or stat.S_ISLNK(existing.st_mode)
                or existing.st_uid != os.getuid()
                or existing.st_nlink != 1
                or stat.S_IMODE(existing.st_mode) != _OWNER_FILE_MODE
            )
        )
    ):
        return False
    if not write_scanned_json(path, receipt.model_dump(mode="json")):
        return False
    try:
        path.chmod(_OWNER_FILE_MODE)
        metadata = os.lstat(path)
        verified = verify_codex_native_candidate_runner_receipt(
            path.read_text(encoding="utf-8"),
        )
    except (OSError, ValueError):
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
        and verified == receipt
    )


def verify_codex_native_candidate_runner_receipt(
    raw: str,
) -> CodexNativeCandidateRunnerReceipt:
    """Strictly authenticate one path-free candidate-runner receipt."""
    return CodexNativeCandidateRunnerReceipt.model_validate_json(raw, strict=True)


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
