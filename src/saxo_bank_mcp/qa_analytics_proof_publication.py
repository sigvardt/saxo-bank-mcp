"""Strict publication envelope for executed Codex-native analytics proofs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.agent_skill_command_runner import (
    CleanupCoverageStage,
    CleanupCoverageSubreason,
    CleanupIdentityEvidenceKind,
    CleanupUnknownReason,
)
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
    candidate_runner_cleanup_evidence_status: CleanupIdentityEvidenceKind = "no-target-observed"
    candidate_runner_cleanup_receipt_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    candidate_runner_cleanup_unknown_reason: CleanupUnknownReason | None = None
    candidate_runner_cleanup_coverage_stage: CleanupCoverageStage | None = None
    candidate_runner_cleanup_coverage_subreason: CleanupCoverageSubreason | None = None
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
    def _validate_digest(self) -> Self:  # noqa: C901
        material = self.model_dump(mode="json", exclude={"boundary_receipt_sha256"})
        accepted_materials = _boundary_material_variants(material)
        legacy_cleanup = self.boundary_receipt_sha256 != _digest(material)
        if not legacy_cleanup and not _cleanup_evidence_is_consistent(
            status=self.candidate_runner_cleanup_evidence_status,
            receipt_sha256=self.candidate_runner_cleanup_receipt_sha256,
            unknown_reason=self.candidate_runner_cleanup_unknown_reason,
            coverage_stage=self.candidate_runner_cleanup_coverage_stage,
            coverage_subreason=self.candidate_runner_cleanup_coverage_subreason,
            allow_legacy_missing=not bool(
                {
                    "candidate_runner_cleanup_coverage_stage",
                    "candidate_runner_cleanup_coverage_subreason",
                }
                & self.model_fields_set
            ),
        ):
            raise ValueError("native candidate runner cleanup evidence differs")
        if (
            self.candidate_runner_receipt_sha256 is None
            and self.candidate_runner_cleanup_status != "unknown"
        ):
            raise ValueError("native candidate runner cleanup lacks receipt")
        if (
            not legacy_cleanup
            and self.candidate_runner_receipt_sha256 is not None
            and self.candidate_runner_cleanup_status == "unknown"
            and self.candidate_runner_cleanup_evidence_status
            not in {"observation-unknown", "write-failed"}
        ):
            raise ValueError("unknown native candidate cleanup lacks typed evidence")
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
        accepted_digests = {_digest(candidate) for candidate in accepted_materials}
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
    cleanup_identity_evidence_status: CleanupIdentityEvidenceKind = "no-target-observed"
    cleanup_identity_receipt_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    cleanup_unknown_reason: CleanupUnknownReason | None = None
    cleanup_coverage_stage: CleanupCoverageStage | None = None
    cleanup_coverage_subreason: CleanupCoverageSubreason | None = None
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        prior_diagnostic_material = dict(material)
        prior_diagnostic_material.pop("cleanup_coverage_stage")
        prior_diagnostic_material.pop("cleanup_coverage_subreason")
        legacy_material = dict(prior_diagnostic_material)
        legacy_material.pop("cleanup_identity_evidence_status")
        legacy_material.pop("cleanup_identity_receipt_sha256")
        legacy_material.pop("cleanup_unknown_reason")
        legacy_defaults = (
            self.cleanup_identity_evidence_status == "no-target-observed"
            and self.cleanup_identity_receipt_sha256 is None
            and self.cleanup_unknown_reason is None
            and self.cleanup_coverage_stage is None
            and self.cleanup_coverage_subreason is None
        )
        legacy_cleanup = legacy_defaults and self.receipt_sha256 == _digest(legacy_material)
        if not legacy_cleanup and not _cleanup_evidence_is_consistent(
            status=self.cleanup_identity_evidence_status,
            receipt_sha256=self.cleanup_identity_receipt_sha256,
            unknown_reason=self.cleanup_unknown_reason,
            coverage_stage=self.cleanup_coverage_stage,
            coverage_subreason=self.cleanup_coverage_subreason,
            allow_legacy_missing=not bool(
                {"cleanup_coverage_stage", "cleanup_coverage_subreason"} & self.model_fields_set
            ),
        ):
            raise ValueError("candidate runner cleanup evidence differs")
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
        if (
            not legacy_cleanup
            and self.spawned
            and self.cleanup_status == "unknown"
            and self.cleanup_identity_evidence_status not in {"observation-unknown", "write-failed"}
        ):
            raise ValueError("unknown candidate cleanup lacks typed evidence")
        if self.result_present != (self.result_sha256 is not None):
            raise ValueError("candidate runner result digest mismatch")
        accepted_digests = {_digest(material)}
        if self.cleanup_coverage_stage is None and self.cleanup_coverage_subreason is None:
            accepted_digests.add(_digest(prior_diagnostic_material))
        if legacy_defaults:
            accepted_digests.add(_digest(legacy_material))
        if self.receipt_sha256 not in accepted_digests:
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
    def _validate_publication(self) -> Self:  # noqa: C901
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
        publication_materials = [material]
        if isinstance(self.result, CodexNativeVerifiedChildFailure):
            if self.result.agent_evaluation_failure_summary is None:
                publication_materials.extend(
                    _publication_variants_without_result_field(
                        publication_materials,
                        "agent_evaluation_failure_summary",
                    ),
                )
            if self.result.outer_process_cleanup_unknown_reason is None:
                publication_materials.extend(
                    _publication_variants_without_result_field(
                        publication_materials,
                        "outer_process_cleanup_unknown_reason",
                    ),
                )
            if (
                self.result.outer_process_cleanup_coverage_stage is None
                and self.result.outer_process_cleanup_coverage_subreason is None
            ):
                for field in (
                    "outer_process_cleanup_coverage_stage",
                    "outer_process_cleanup_coverage_subreason",
                ):
                    publication_materials.extend(
                        _publication_variants_without_result_field(
                            publication_materials,
                            field,
                        ),
                    )
        if isinstance(self.result, CodexNativeBoundaryFailureReceipt):
            publication_materials.extend(
                {**material, "result": result_material}
                for result_material in _boundary_material_variants(
                    dict(material["result"]),
                )[1:]
            )
        accepted_digests = {_digest(candidate) for candidate in publication_materials}
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
    candidate_runner_cleanup_evidence_status: CleanupIdentityEvidenceKind = ("no-target-observed"),
    candidate_runner_cleanup_receipt_sha256: str | None = None,
    candidate_runner_cleanup_unknown_reason: CleanupUnknownReason | None = None,
    candidate_runner_cleanup_coverage_stage: CleanupCoverageStage | None = None,
    candidate_runner_cleanup_coverage_subreason: CleanupCoverageSubreason | None = None,
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
        "candidate_runner_cleanup_evidence_status": candidate_runner_cleanup_evidence_status,
        "candidate_runner_cleanup_receipt_sha256": candidate_runner_cleanup_receipt_sha256,
        "candidate_runner_cleanup_unknown_reason": candidate_runner_cleanup_unknown_reason,
        "candidate_runner_cleanup_coverage_stage": candidate_runner_cleanup_coverage_stage,
        "candidate_runner_cleanup_coverage_subreason": (
            candidate_runner_cleanup_coverage_subreason
        ),
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
    cleanup_identity_evidence_status: CleanupIdentityEvidenceKind = "no-target-observed",
    cleanup_identity_receipt_sha256: str | None = None,
    cleanup_unknown_reason: CleanupUnknownReason | None = None,
    cleanup_coverage_stage: CleanupCoverageStage | None = None,
    cleanup_coverage_subreason: CleanupCoverageSubreason | None = None,
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
        "cleanup_identity_evidence_status": cleanup_identity_evidence_status,
        "cleanup_identity_receipt_sha256": cleanup_identity_receipt_sha256,
        "cleanup_unknown_reason": cleanup_unknown_reason,
        "cleanup_coverage_stage": cleanup_coverage_stage,
        "cleanup_coverage_subreason": cleanup_coverage_subreason,
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


def _cleanup_evidence_is_consistent(  # noqa: PLR0911, PLR0913
    *,
    status: CleanupIdentityEvidenceKind,
    receipt_sha256: str | None,
    unknown_reason: CleanupUnknownReason | None,
    coverage_stage: CleanupCoverageStage | None,
    coverage_subreason: CleanupCoverageSubreason | None,
    allow_legacy_missing: bool = False,
) -> bool:
    if (coverage_stage is None) != (coverage_subreason is None):
        return False
    has_diagnostic = coverage_stage is not None
    if has_diagnostic:
        allowed: dict[CleanupCoverageStage, frozenset[CleanupCoverageSubreason]] = {
            "first_snapshot": frozenset({"snapshot_failure"}),
            "second_snapshot": frozenset({"snapshot_failure"}),
            "watcher_capture": frozenset({"capture_failure"}),
            "post_exit_snapshot": frozenset({"snapshot_failure"}),
            "final_snapshot": frozenset({"snapshot_failure"}),
            "group_table": frozenset({"table_incomplete"}),
            "group_member": frozenset(
                {
                    "uncaptured_member",
                    "changed_identity_or_group_member",
                    "observation_unknown",
                },
            ),
            "target_observation": frozenset({"observation_unknown"}),
        }
        if coverage_subreason not in allowed[coverage_stage]:
            return False
    if status == "authenticated":
        return receipt_sha256 is not None and unknown_reason is None and not has_diagnostic
    if status == "no-target-observed":
        return receipt_sha256 is None and unknown_reason is None and not has_diagnostic
    if status == "observation-unknown":
        if receipt_sha256 is None or unknown_reason not in {
            "watcher_publication_discarded",
            "watcher_still_running",
            "watcher_drain_unknown",
            "coverage_unknown",
            "target_observation_unknown",
            "cleanup_state_unknown",
        }:
            return False
        if unknown_reason == "coverage_unknown":
            return (
                allow_legacy_missing
                if not has_diagnostic
                else coverage_stage != ("target_observation")
            )
        if unknown_reason == "target_observation_unknown":
            return (
                allow_legacy_missing
                if not has_diagnostic
                else (
                    coverage_stage == "target_observation"
                    and coverage_subreason == "observation_unknown"
                )
            )
        return True
    return (
        receipt_sha256 is None
        and not has_diagnostic
        and unknown_reason
        in {
            "cleanup_receipt_path_missing",
            "cleanup_receipt_write_failed",
            "cleanup_evidence_inconsistent",
            "cleanup_evidence_unavailable",
        }
    )


def _boundary_material_variants(material: dict[str, object]) -> tuple[dict[str, object], ...]:
    """Return the current boundary material and exact historical default-only shapes."""
    variants = [dict(material)]
    if (
        material.get("candidate_runner_cleanup_coverage_stage") is None
        and material.get("candidate_runner_cleanup_coverage_subreason") is None
    ):
        prior_diagnostic = dict(material)
        prior_diagnostic.pop("candidate_runner_cleanup_coverage_stage")
        prior_diagnostic.pop("candidate_runner_cleanup_coverage_subreason")
        variants.append(prior_diagnostic)
    if not (
        material.get("candidate_runner_cleanup_evidence_status") == "no-target-observed"
        and material.get("candidate_runner_cleanup_receipt_sha256") is None
        and material.get("candidate_runner_cleanup_unknown_reason") is None
        and material.get("candidate_runner_cleanup_coverage_stage") is None
        and material.get("candidate_runner_cleanup_coverage_subreason") is None
    ):
        return tuple(variants)
    without_cleanup = dict(variants[-1])
    without_cleanup.pop("candidate_runner_cleanup_evidence_status")
    without_cleanup.pop("candidate_runner_cleanup_receipt_sha256")
    without_cleanup.pop("candidate_runner_cleanup_unknown_reason")
    variants.append(without_cleanup)
    if not (
        material.get("candidate_runner_result_status") == "unknown"
        and material.get("candidate_runner_result_sha256") is None
    ):
        return tuple(variants)
    without_result = dict(without_cleanup)
    without_result.pop("candidate_runner_result_status")
    without_result.pop("candidate_runner_result_sha256")
    variants.append(without_result)
    if not (
        material.get("candidate_runner_receipt_sha256") is None
        and material.get("candidate_runner_cleanup_status") == "unknown"
    ):
        return tuple(variants)
    oldest = dict(without_result)
    oldest.pop("candidate_runner_receipt_sha256")
    oldest.pop("candidate_runner_cleanup_status")
    variants.append(oldest)
    return tuple(variants)


def _publication_variants_without_result_field(
    materials: list[dict[str, object]],
    field: str,
) -> list[dict[str, object]]:
    variants: list[dict[str, object]] = []
    for material in tuple(materials):
        result = dict(cast("dict[str, object]", material["result"]))
        result.pop(field)
        variants.append({**material, "result": result})
    return variants
