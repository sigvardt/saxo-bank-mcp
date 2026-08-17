# pyright: reportPrivateUsage=false
"""Process-owned execution boundary for the installed analytics proof producer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, Self, cast

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_codex_install import CodexInstallEvidenceReport
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    run_command,
)
from saxo_bank_mcp.agent_skill_eval_models import EvalRunRecord, EvalRunReport
from saxo_bank_mcp.agent_skill_evidence_io import git_output
from saxo_bank_mcp.agent_skill_install_models import (
    FixtureSupportReport,
    InstallEvidenceReport,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    installed_inventory_check,
    publishable_tracked_files,
    tree_digest,
)
from saxo_bank_mcp.agent_skill_matrix_env import (
    MatrixEnvError,
    prepare_eval_isolated_runtime,
    promote_rotated_claude_credentials,
    promote_rotated_sim_token_cache,
    require_matrix_runtime_cleanup,
)
from saxo_bank_mcp.qa_analytics_artifacts import (
    ArtifactParityReceipt,
    ArtifactVisualIntegrityReceipt,
)
from saxo_bank_mcp.qa_analytics_evidence import (
    AnalysisEvidenceReceipt,
    AnalysisProofExecutionContract,
    AnalyticsProofMatrixBundle,
    ProofCaseContract,
    ProofCaseReceipt,
    ProofExecutionKind,
    SkillScenarioEvidenceReceipt,
    build_proof_execution_contracts,
    exact_analysis_measurement_node_id,
    load_analysis_kind_catalog,
    validate_proof_matrix_bundle,
)
from saxo_bank_mcp.qa_analytics_proof_failure import (
    CodexNativeProofProgress,
    CodexNativeSimPreflightReceipt,
    CodexNativeVerifiedChildFailure,
    build_child_failure_envelope,
    safe_failure_reason,
    verify_child_failure_envelope,
)
from saxo_bank_mcp.qa_analytics_sim import (
    AnalyticsCaseReceipt,
    PostSendTimeoutReceipt,
    analytics_case_calls,
)
from saxo_bank_mcp.qa_auth_probes import call_saxo_auth_status, call_tool_payload
from saxo_bank_mcp.qa_codex_native_policy import HarnessPolicy
from saxo_bank_mcp.qa_installed_matrix_envelope import InstalledMatrixEnvelope
from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_PRODUCER_MODULE_RELATIVE = Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py")
_COMMAND_NAME = "analytics_proof_producer"
_AGENT_EVAL_COMMAND_NAME = "analytics_installed_dual_evaluation"
_PROCESS_AUTHORITY = object()
_JUNIT_PROOF_PROPERTY = "saxo_analytics_proof_receipt_v1"
_MATRIX_CHILD_COMMAND_NAME = "analytics_installed_matrix_child"
_MATRIX_CHILD_TIMEOUT_SECONDS = 1800
_MATRIX_CHILD_ENV_KEYS: Final = (
    "HOME",
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "XDG_STATE_HOME",
    "SAXO_MCP_SIM_CREDENTIAL_FILE",
    "SAXO_MCP_SIM_REDIRECT_URI",
    "SAXO_MCP_TOKEN_CACHE_PATH",
    "SAXO_MCP_SIM_AUTH_URL",
    "SAXO_MCP_SIM_TOKEN_URL",
    "SAXO_MCP_ACCOUNT_ALLOWLIST",
    "SAXO_MCP_INSTRUMENT_ALLOWLIST",
)

type MeasuredProofOperationKind = Literal[
    "analysis_result_observation",
    "source_binding_assertion",
    "known_answer_comparison",
    "property_assertion",
    "metamorphic_assertion",
    "independent_reference_comparison",
    "mutation_kill",
    "numerical_tolerance_comparison",
    "accounting_identity_comparison",
    "saxo_reconciliation_comparison",
    "schema_drift_recovery",
    "privacy_scan",
]
_MEASURED_OPERATION_BY_CASE: Final[dict[ProofExecutionKind, MeasuredProofOperationKind]] = {
    "source_contract": "source_binding_assertion",
    "known_answer": "known_answer_comparison",
    "property": "property_assertion",
    "metamorphic": "metamorphic_assertion",
    "independent_reference": "independent_reference_comparison",
    "mutation_kill": "mutation_kill",
    "numerical_tolerance": "numerical_tolerance_comparison",
    "accounting_identity": "accounting_identity_comparison",
    "saxo_reconciliation": "saxo_reconciliation_comparison",
    "schema_drift": "schema_drift_recovery",
    "privacy_safety": "privacy_scan",
    "executable_sim": "source_binding_assertion",
    "artifact_parity": "source_binding_assertion",
    "visual_integrity": "source_binding_assertion",
    "agent_use": "source_binding_assertion",
}


class ProofProducerError(RuntimeError):
    """Fail-closed reason from the private installed-producer boundary."""


class CodexNativeProofFailureError(ProofProducerError):
    """A failed native child with one parent-verified redacted receipt."""

    def __init__(self, receipt: CodexNativeVerifiedChildFailure) -> None:
        """Retain only the verified redacted receipt."""
        self.receipt = receipt
        super().__init__(receipt.reason)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class _AnalysisProofProperty(_StrictModel):
    """One observation emitted by the exact installed test that performed it."""

    receipt_kind: Literal["analysis_case"] = "analysis_case"
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    case_kind: ProofExecutionKind
    requirement_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    measurement_state: Literal["passed", "unavailable"]
    operation_kind: MeasuredProofOperationKind
    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,191}$")
    executed_test_node_id: str = Field(
        pattern=r"^tests\.test_analytics_[a-z0-9_]+::test_[a-z0-9_]+$",
    )
    observed_result_count: int = Field(ge=1)
    observed_result_types: tuple[str, ...] = Field(min_length=1)
    observed_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_value_count: int = Field(ge=1)
    observed_output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executed_case_count: int = Field(ge=1)
    failed_case_count: int = Field(ge=0)
    comparison_count: int = Field(ge=0)
    unexplained_difference_count: int = Field(ge=0)
    mutation_count: int = Field(ge=0)
    mutation_killed_count: int = Field(ge=0)
    independent_path_observed: bool
    recovery_observed: bool
    publication_scan_passed: bool

    @model_validator(mode="after")
    def _validate_measured_operation(self) -> Self:  # noqa: C901, PLR0912
        if not self.operation_id.startswith(f"{self.analysis_kind}_{self.case_kind}_"):
            raise ValueError("proof operation is not bound to the exact analysis contract")
        if self.observed_output_sha256 != self.observed_result_sha256:
            raise ValueError("proof output is not bound to the typed observed result")
        if self.measurement_state == "unavailable":
            if (
                self.operation_kind != "analysis_result_observation"
                or self.comparison_count
                or self.mutation_count
                or self.mutation_killed_count
                or self.independent_path_observed
                or self.recovery_observed
                or self.publication_scan_passed
                or self.failed_case_count
                or self.unexplained_difference_count
            ):
                raise ValueError("unavailable proof observation claims unmeasured semantics")
            return self
        if self.operation_kind == "analysis_result_observation":
            raise ValueError("passed proof requires a measured proof-specific operation")
        comparison_required = self.operation_kind.endswith("_comparison")
        if comparison_required != (self.comparison_count > 0):
            raise ValueError("proof comparison count does not match the measured operation")
        if self.operation_kind == "mutation_kill":
            if self.mutation_count < 1 or self.mutation_killed_count != self.mutation_count:
                raise ValueError("measured mutation operation did not kill every mutation")
        elif self.mutation_count or self.mutation_killed_count:
            raise ValueError("non-mutation operation cannot claim mutation counts")
        if self.independent_path_observed != (
            self.operation_kind == "independent_reference_comparison"
        ):
            raise ValueError("independent-path observation does not match the operation")
        if self.recovery_observed != (self.operation_kind == "schema_drift_recovery"):
            raise ValueError("recovery observation does not match the operation")
        if self.publication_scan_passed != (self.operation_kind == "privacy_scan"):
            raise ValueError("publication scan does not match the operation")
        if self.failed_case_count or self.unexplained_difference_count:
            raise ValueError("measured proof operation contains a failed observation")
        return self


class _ArtifactParityProperty(_StrictModel):
    receipt_kind: Literal["artifact_parity"] = "artifact_parity"
    receipt: ArtifactParityReceipt


class _ArtifactVisualProperty(_StrictModel):
    receipt_kind: Literal["artifact_visual"] = "artifact_visual"
    receipt: ArtifactVisualIntegrityReceipt


class MeasuredAnalysisProofObservation(_StrictModel):
    """Contract-keyed observation bound to one passed installed pytest node."""

    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    case_kind: ProofExecutionKind
    requirement_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    measurement_state: Literal["passed", "unavailable"]
    operation_kind: MeasuredProofOperationKind
    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,191}$")
    executed_test_node_id: str = Field(
        pattern=r"^tests\.test_analytics_[a-z0-9_]+::test_[a-z0-9_]+$",
    )
    observed_result_count: int = Field(ge=1)
    observed_result_types: tuple[str, ...] = Field(min_length=1)
    observed_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_value_count: int = Field(ge=1)
    observed_output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    test_node_id: str = Field(min_length=1, max_length=512)
    executed_case_count: int = Field(ge=1)
    failed_case_count: int = Field(ge=0)
    comparison_count: int = Field(ge=0)
    unexplained_difference_count: int = Field(ge=0)
    mutation_count: int = Field(ge=0)
    mutation_killed_count: int = Field(ge=0)
    independent_path_observed: bool
    recovery_observed: bool
    publication_scan_passed: bool
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _validate_observation(self) -> Self:  # noqa: C901
        if self.observed_output_sha256 != self.observed_result_sha256:
            raise ValueError("proof output is not bound to the typed observed result")
        if self.measurement_state == "unavailable":
            if (
                self.operation_kind != "analysis_result_observation"
                or self.comparison_count
                or self.mutation_count
                or self.mutation_killed_count
                or self.independent_path_observed
                or self.recovery_observed
                or self.publication_scan_passed
                or self.failed_case_count
                or self.unexplained_difference_count
            ):
                raise ValueError("unavailable proof observation claims unmeasured semantics")
            return self
        if self.operation_kind == "analysis_result_observation":
            raise ValueError("passed proof requires a measured proof-specific operation")
        if self.failed_case_count or self.unexplained_difference_count:
            raise ValueError("proof observation contains a failed or unexplained case")
        if self.operation_kind.endswith("_comparison") and self.comparison_count < 1:
            raise ValueError("comparison observation did not compare values")
        if self.operation_kind == "mutation_kill":
            if self.mutation_count < 1 or self.mutation_killed_count != self.mutation_count:
                raise ValueError("mutation observation did not kill every mutation")
        elif self.mutation_count or self.mutation_killed_count:
            raise ValueError("non-mutation observation cannot claim mutation counts")
        if self.independent_path_observed != (
            self.operation_kind == "independent_reference_comparison"
        ):
            raise ValueError("independent-path observation does not match measured operation")
        if self.recovery_observed != (self.operation_kind == "schema_drift_recovery"):
            raise ValueError("only schema-drift observations can claim recovery")
        if self.publication_scan_passed != (self.operation_kind == "privacy_scan"):
            raise ValueError("only privacy observations can claim publication scanning")
        return self


class InstalledProofSuiteEvidence(_StrictModel):
    """Exact typed evidence derived from child-owned JUnit properties only."""

    analysis_cases: tuple[MeasuredAnalysisProofObservation, ...]
    artifact_parity_receipts: tuple[ArtifactParityReceipt, ...]
    artifact_visual_receipts: tuple[ArtifactVisualIntegrityReceipt, ...]
    executed_test_count: int = Field(ge=1)
    suite_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _validate_unique_receipts(self) -> Self:
        keys = tuple((item.analysis_kind, item.case_kind) for item in self.analysis_cases)
        nodes = tuple(item.test_node_id for item in self.analysis_cases)
        if len(keys) != len(set(keys)) or len(nodes) != len(set(nodes)):
            raise ValueError("installed proof observations must be exact and node-unique")
        parity_ids = tuple(item.template_id for item in self.artifact_parity_receipts)
        visual_ids = tuple(item.template_id for item in self.artifact_visual_receipts)
        if len(parity_ids) != len(set(parity_ids)) or len(visual_ids) != len(set(visual_ids)):
            raise ValueError("installed artifact observations must be template-unique")
        return self

    def by_analysis_case(
        self,
    ) -> dict[tuple[str, ProofExecutionKind], MeasuredAnalysisProofObservation]:
        return {(item.analysis_kind, item.case_kind): item for item in self.analysis_cases}


class InstalledProofProducerResult(_StrictModel):
    """Typed stdout issued by the producer running from verified installed bytes."""

    schema_version: Literal["1"] = "1"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    installed_cache_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_module_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["validated", "blocked"]
    execution_performed: bool
    process_local_activation: bool
    executed_receipt_count: int = Field(ge=0)
    proof_execution_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    validation_errors: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        if self.status == "validated":
            if (
                not self.process_local_activation
                or not self.execution_performed
                or self.executed_receipt_count < 1
                or self.bundle_sha256 is None
                or self.validation_errors
            ):
                raise ValueError("validated proof production requires executed receipts")
        elif (
            self.process_local_activation
            or self.execution_performed
            or self.executed_receipt_count != 0
            or self.bundle_sha256 is not None
            or not self.validation_errors
        ):
            raise ValueError("blocked proof production must retain no proof claim")
        return self


class VerifiedInstalledProofValidation(_StrictModel):
    """Redacted validation issued only from one authenticated child execution."""

    schema_version: Literal["1"] = "1"
    status: Literal["validated", "blocked"]
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    installed_cache_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_authenticated: Literal[True]
    execution_performed: bool
    process_local_activation: bool
    executed_receipt_count: int = Field(ge=0)
    proof_execution_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    validation_errors: tuple[str, ...]
    network_call_made: Literal[False] = False
    broker_write_made: Literal[False] = False
    disclaimer_response_made: Literal[False] = False


class CodexNativeInstalledProofProducerResult(InstalledProofProducerResult):
    """Process-owned proof result under the explicit native Codex policy."""

    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    sim_preflight: CodexNativeSimPreflightReceipt
    network_call_made: bool | None = None

    @model_validator(mode="after")
    def _validate_network_provenance(self) -> Self:
        if self.network_call_made != self.sim_preflight.network_call_made:
            raise ValueError("native proof network provenance does not match proof state")
        if self.status == "validated" and self.sim_preflight.status != "passed":
            raise ValueError("validated native proof requires passed SIM preflight")
        return self


class CodexNativeVerifiedInstalledProofValidation(_StrictModel):
    """Authenticated proof result under the explicit native Codex policy."""

    schema_version: Literal["1"] = "1"
    status: Literal["validated", "blocked"]
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    installed_cache_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_authenticated: Literal[True]
    execution_performed: bool
    process_local_activation: bool
    executed_receipt_count: int = Field(ge=0)
    proof_execution_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    validation_errors: tuple[str, ...]
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    sim_preflight: CodexNativeSimPreflightReceipt
    network_call_made: bool | None = None
    broker_write_made: Literal[False] = False
    disclaimer_response_made: Literal[False] = False

    @model_validator(mode="after")
    def _validate_network_provenance(self) -> Self:
        if self.network_call_made != self.sim_preflight.network_call_made:
            raise ValueError("native proof network provenance does not match proof state")
        if self.status == "validated" and self.sim_preflight.status != "passed":
            raise ValueError("validated native proof requires passed SIM preflight")
        return self


def run_verified_installed_producer(
    install: InstallEvidenceReport | FixtureSupportReport,
    *,
    candidate_commit: str,
) -> VerifiedInstalledProofValidation:
    """Execute the producer from one independently verified production cache.

    This function accepts the typed result of ``load_verified_install_report`` only. Caller
    receipt paths and caller-authored proof JSON are never inputs to this boundary.
    """
    if type(install) is not InstallEvidenceReport:
        raise ProofProducerError("fixture_support_not_production")
    if (
        _COMMIT_PATTERN.fullmatch(candidate_commit) is None
        or install.candidate_commit != candidate_commit
    ):
        raise ProofProducerError("proof_installed_candidate_mismatch")
    _require_clean_source_commit(install.repo, candidate_commit)
    clone_digest, codex_digest, claude_digest = _verified_install_digests(install)
    if clone_digest != codex_digest or clone_digest != claude_digest:
        raise ProofProducerError("proof_installed_cache_digest_mismatch")
    expected_module_sha256 = _installed_producer_module_sha256(install.codex.cache_root)
    command = _producer_command(candidate_commit, codex_digest)
    result = _execute_installed_child(
        install.codex.cache_root,
        command,
        claude_cache_root=install.claude.cache_root,
        source_repo=install.clone.path,
        retained_codex_home=install.fixture_cleanup.run_root / "codex-home",
        retained_claude_home=install.fixture_cleanup.run_root / "home",
    )

    _require_clean_source_commit(install.repo, candidate_commit)
    after_digests = _verified_install_digests(install)
    if after_digests != (clone_digest, codex_digest, claude_digest):
        raise ProofProducerError("proof_installed_cache_changed_during_execution")
    if _installed_producer_module_sha256(install.codex.cache_root) != expected_module_sha256:
        raise ProofProducerError("proof_installed_producer_changed_during_execution")
    return _validate_process_owned_result(
        result,
        command=command,
        cache_root=install.codex.cache_root,
        candidate_commit=candidate_commit,
        installed_cache_sha256=codex_digest,
        producer_module_sha256=expected_module_sha256,
        authority=_PROCESS_AUTHORITY,
    )


def run_verified_codex_native_producer(
    install: CodexInstallEvidenceReport,
    *,
    candidate_commit: str,
) -> CodexNativeVerifiedInstalledProofValidation:
    """Execute the unchanged proof suite with the native Codex harness quorum."""
    if type(install) is not CodexInstallEvidenceReport:
        raise ProofProducerError("codex_native_install_required")
    if (
        _COMMIT_PATTERN.fullmatch(candidate_commit) is None
        or install.candidate_commit != candidate_commit
    ):
        raise ProofProducerError("proof_installed_candidate_mismatch")
    _require_clean_source_commit(install.repo, candidate_commit)
    clone_digest, codex_digest = _verified_codex_install_digests(install)
    if clone_digest != codex_digest:
        raise ProofProducerError("proof_installed_cache_digest_mismatch")
    expected_module_sha256 = _installed_producer_module_sha256(install.codex.cache_root)
    command = _codex_native_producer_command(candidate_commit, codex_digest)
    try:
        result = _execute_codex_native_installed_child(
            install.codex.cache_root,
            command,
            source_repo=install.clone.path,
            retained_codex_home=install.run_root / "codex-home",
            candidate_commit=candidate_commit,
            installed_cache_sha256=codex_digest,
            producer_module_sha256=expected_module_sha256,
        )
    except CodexNativeProofFailureError:
        _require_clean_source_commit(install.repo, candidate_commit)
        if _verified_codex_install_digests(install) != (clone_digest, codex_digest):
            raise ProofProducerError("proof_installed_cache_changed_during_execution") from None
        if _installed_producer_module_sha256(install.codex.cache_root) != expected_module_sha256:
            raise ProofProducerError("proof_installed_producer_changed_during_execution") from None
        raise
    _require_clean_source_commit(install.repo, candidate_commit)
    if _verified_codex_install_digests(install) != (clone_digest, codex_digest):
        raise ProofProducerError("proof_installed_cache_changed_during_execution")
    if _installed_producer_module_sha256(install.codex.cache_root) != expected_module_sha256:
        raise ProofProducerError("proof_installed_producer_changed_during_execution")
    return _validate_codex_native_process_owned_result(
        result,
        command=command,
        cache_root=install.codex.cache_root,
        candidate_commit=candidate_commit,
        installed_cache_sha256=codex_digest,
        producer_module_sha256=expected_module_sha256,
        authority=_PROCESS_AUTHORITY,
    )


def produce_installed_result(
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
) -> InstalledProofProducerResult:
    """Issue process-owned receipts for the exact installed proof contract state."""
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise ProofProducerError("proof_candidate_commit_invalid")
    if _SHA256_PATTERN.fullmatch(installed_cache_sha256) is None:
        raise ProofProducerError("proof_installed_cache_digest_invalid")
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    catalog_sha256, contract_sha256 = _installed_contract_digests()
    producer_module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        bundle = _execute_installed_proof_bundle(
            candidate_commit=candidate_commit,
            installed_cache_sha256=installed_cache_sha256,
        )
    except ProofProducerError as error:
        reason = str(error)
        return InstalledProofProducerResult(
            candidate_commit=candidate_commit,
            installed_cache_sha256=installed_cache_sha256,
            producer_module_sha256=producer_module_sha256,
            catalog_sha256=catalog_sha256,
            contract_sha256=contract_sha256,
            status="blocked",
            execution_performed=False,
            process_local_activation=False,
            executed_receipt_count=0,
            proof_execution_sha256=_digest(
                {
                    "candidate_commit": candidate_commit,
                    "installed_cache_sha256": installed_cache_sha256,
                    "producer_module_sha256": producer_module_sha256,
                    "reason": reason,
                },
            ),
            validation_errors=(reason,),
        )
    active_contracts = tuple(
        contract.model_copy(
            update={"proof_activation_state": "active", "quarantine_reason": None},
        )
        for contract in contracts
    )
    validation_errors = _validate_executed_bundle(
        bundle,
        contracts=active_contracts,
        candidate_commit=candidate_commit,
        authority=_PROCESS_AUTHORITY,
    )
    if validation_errors:
        raise ProofProducerError("installed_proof_bundle_invalid")
    bundle_sha256 = _digest(bundle.model_dump(mode="json"))
    return InstalledProofProducerResult(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        producer_module_sha256=producer_module_sha256,
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
        status="validated",
        execution_performed=True,
        process_local_activation=True,
        executed_receipt_count=len(bundle.analysis_receipts),
        proof_execution_sha256=_digest(
            {
                "bundle_sha256": bundle_sha256,
                "candidate_commit": candidate_commit,
                "installed_cache_sha256": installed_cache_sha256,
                "producer_module_sha256": producer_module_sha256,
            },
        ),
        bundle_sha256=bundle_sha256,
        validation_errors=(),
    )


def _run_codex_native_sim_preflight() -> CodexNativeSimPreflightReceipt:
    """Prove a current SIM session before any native model or offline proof work."""
    auth = anyio.run(call_saxo_auth_status)
    if (
        auth["requested_environment"] != "SIM"
        or auth["effective_read_environment"] != "SIM"
        or auth["live_reads"]
        or auth["live_writes"]
    ):
        return CodexNativeSimPreflightReceipt(
            status="blocked",
            requested_environment=auth["requested_environment"],
            effective_read_environment=auth["effective_read_environment"],
            live_reads=auth["live_reads"],
            live_writes=auth["live_writes"],
            capabilities_status="not_called",
            reason="native_preflight_environment_unsafe",
            network_call_made=False,
            session_capabilities_proven=False,
        )

    capability_arguments: dict[str, JsonValue] = {}
    capabilities = anyio.run(
        call_tool_payload,
        "saxo_get_session_capabilities",
        capability_arguments,
    )
    raw_status = capabilities.get("status")
    capabilities_status = raw_status if isinstance(raw_status, str) else "invalid"
    raw_http_status = capabilities.get("http_status")
    http_status = (
        raw_http_status
        if isinstance(raw_http_status, int) and not isinstance(raw_http_status, bool)
        else None
    )
    network_call_made = _native_preflight_network_call_made(
        capabilities,
        http_status=http_status,
    )
    capability_receipt_valid = (
        capabilities_status == "passed"
        and capabilities.get("tool_name") == "saxo_get_session_capabilities"
        and capabilities.get("environment") == "SIM"
        and network_call_made is True
    )
    if capability_receipt_valid:
        return CodexNativeSimPreflightReceipt(
            status="passed",
            requested_environment="SIM",
            effective_read_environment="SIM",
            live_reads=False,
            live_writes=False,
            capabilities_status="passed",
            reason=None,
            http_status=http_status,
            network_call_made=True,
            session_capabilities_proven=True,
        )

    raw_reason = capabilities.get("reason")
    reason = (
        raw_reason
        if isinstance(raw_reason, str) and _SAFE_REASON_PATTERN.fullmatch(raw_reason) is not None
        else "native_preflight_capabilities_invalid"
    )
    return CodexNativeSimPreflightReceipt(
        status="blocked",
        requested_environment="SIM",
        effective_read_environment="SIM",
        live_reads=False,
        live_writes=False,
        capabilities_status=(capabilities_status if capabilities_status != "passed" else "invalid"),
        reason=reason,
        http_status=http_status,
        network_call_made=network_call_made,
        session_capabilities_proven=False,
    )


def _native_preflight_network_call_made(
    capabilities: Mapping[str, object],
    *,
    http_status: int | None,
) -> bool | None:
    if http_status is not None:
        return True
    observed = capabilities.get("network_call_made")
    return observed if isinstance(observed, bool) else None


def produce_codex_native_installed_result(
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    progress: CodexNativeProofProgress | None = None,
) -> CodexNativeInstalledProofProducerResult:
    """Issue process-owned receipts using only the Codex-native model quorum."""
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise ProofProducerError("proof_candidate_commit_invalid")
    if _SHA256_PATTERN.fullmatch(installed_cache_sha256) is None:
        raise ProofProducerError("proof_installed_cache_digest_invalid")
    catalog_sha256, contract_sha256 = _installed_contract_digests()
    producer_module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if progress is None:
        progress = CodexNativeProofProgress(
            candidate_commit=candidate_commit,
            installed_cache_sha256=installed_cache_sha256,
            producer_module_sha256=producer_module_sha256,
            catalog_sha256=catalog_sha256,
            contract_sha256=contract_sha256,
        )
    elif (
        progress.candidate_commit != candidate_commit
        or progress.installed_cache_sha256 != installed_cache_sha256
        or progress.producer_module_sha256 != producer_module_sha256
        or progress.catalog_sha256 != catalog_sha256
        or progress.contract_sha256 != contract_sha256
    ):
        raise ProofProducerError("proof_failure_progress_binding_mismatch")
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    progress.begin_phase("sim_preflight")
    sim_preflight = _run_codex_native_sim_preflight()
    progress.record_preflight(sim_preflight)
    progress.complete_phase("sim_preflight")
    if sim_preflight.status != "passed":
        return _blocked_codex_native_result(
            candidate_commit=candidate_commit,
            installed_cache_sha256=installed_cache_sha256,
            producer_module_sha256=producer_module_sha256,
            catalog_sha256=catalog_sha256,
            contract_sha256=contract_sha256,
            sim_preflight=sim_preflight,
            reason="proof_sim_session_preflight_failed",
        )
    bundle = _execute_installed_proof_bundle(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        harness_policy="codex_native_v1",
        native_sim_preflight=sim_preflight,
        progress=progress,
    )
    active_contracts = tuple(
        contract.model_copy(
            update={"proof_activation_state": "active", "quarantine_reason": None},
        )
        for contract in contracts
    )
    validation_errors = _validate_executed_bundle(
        bundle,
        contracts=active_contracts,
        candidate_commit=candidate_commit,
        authority=_PROCESS_AUTHORITY,
    )
    if validation_errors:
        raise ProofProducerError("installed_proof_bundle_invalid")
    bundle_sha256 = _digest(bundle.model_dump(mode="json"))
    network_call_made = sim_preflight.network_call_made is True or _bundle_network_call_made(
        bundle,
    )
    return CodexNativeInstalledProofProducerResult(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        producer_module_sha256=producer_module_sha256,
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
        status="validated",
        execution_performed=True,
        process_local_activation=True,
        executed_receipt_count=len(bundle.analysis_receipts),
        proof_execution_sha256=_digest(
            {
                "bundle_sha256": bundle_sha256,
                "candidate_commit": candidate_commit,
                "harness_policy": "codex_native_v1",
                "installed_cache_sha256": installed_cache_sha256,
                "network_call_made": network_call_made,
                "producer_module_sha256": producer_module_sha256,
                "sim_preflight": sim_preflight.model_dump(mode="json"),
            },
        ),
        bundle_sha256=bundle_sha256,
        sim_preflight=sim_preflight,
        network_call_made=network_call_made,
        validation_errors=(),
    )


def _blocked_codex_native_result(  # noqa: PLR0913
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
    catalog_sha256: str,
    contract_sha256: str,
    sim_preflight: CodexNativeSimPreflightReceipt,
    reason: str,
) -> CodexNativeInstalledProofProducerResult:
    material = {
        "candidate_commit": candidate_commit,
        "harness_policy": "codex_native_v1",
        "installed_cache_sha256": installed_cache_sha256,
        "network_call_made": sim_preflight.network_call_made,
        "producer_module_sha256": producer_module_sha256,
        "reason": reason,
        "sim_preflight": sim_preflight.model_dump(mode="json"),
    }
    return CodexNativeInstalledProofProducerResult(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        producer_module_sha256=producer_module_sha256,
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
        status="blocked",
        execution_performed=False,
        process_local_activation=False,
        executed_receipt_count=0,
        proof_execution_sha256=_digest(material),
        sim_preflight=sim_preflight,
        network_call_made=sim_preflight.network_call_made,
        validation_errors=(reason,),
    )


def _run_installed_matrix_proof_session(
    candidate_commit: str,
    analysis_kinds: tuple[str, ...],
) -> SimToolMatrixReceipt:
    """Launch the installed SIM child and authenticate its exact serialized envelope."""
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise ProofProducerError("installed_matrix_candidate_invalid")
    if (
        not analysis_kinds
        or len(analysis_kinds) != len(set(analysis_kinds))
        or any(re.fullmatch(r"[a-z][a-z0-9_]{0,127}", kind) is None for kind in analysis_kinds)
    ):
        raise ProofProducerError("installed_matrix_analysis_kinds_invalid")
    if os.environ.get("SAXO_MCP_ENVIRONMENT", "SIM").strip().upper() != "SIM":
        raise ProofProducerError("installed_matrix_environment_invalid")
    environment = {
        key: value for key in _MATRIX_CHILD_ENV_KEYS if (value := os.environ.get(key)) is not None
    }
    environment.update(
        {
            "PATH": environment.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SAXO_MCP_ENABLE_LIVE_READS": "0",
            "SAXO_MCP_ENABLE_LIVE_WRITES": "",
            "SAXO_MCP_ENVIRONMENT": "SIM",
        },
    )
    command = (
        sys.executable,
        "-I",
        "-m",
        "saxo_bank_mcp.qa_installed_matrix_child",
        "--candidate",
        candidate_commit,
        *(part for kind in analysis_kinds for part in ("--analysis-kind", kind)),
    )
    cwd = Path.cwd().resolve()
    try:
        result = run_command(
            _MATRIX_CHILD_COMMAND_NAME,
            command,
            cwd=cwd,
            env=environment,
            timeout_seconds=_MATRIX_CHILD_TIMEOUT_SECONDS,
        )
    except (CommandFailureError, OSError, ValueError) as error:
        raise ProofProducerError("installed_matrix_child_result_invalid") from error
    receipt = result.receipt
    if (
        receipt.name != _MATRIX_CHILD_COMMAND_NAME
        or receipt.argv != command
        or receipt.cwd != str(cwd)
        or receipt.pid is None
        or receipt.pid <= 0
        or receipt.pgid is None
        or receipt.pgid <= 0
        or receipt.exit_code != 0
        or receipt.timed_out
        or not receipt.cleanup_attempted
        or receipt.stdout_sha256 != hashlib.sha256(result.stdout.encode()).hexdigest()
        or receipt.stderr_sha256 != hashlib.sha256(result.stderr.encode()).hexdigest()
        or result.stderr != ""
    ):
        raise ProofProducerError("installed_matrix_child_result_invalid")
    try:
        envelope = InstalledMatrixEnvelope.model_validate_json(result.stdout, strict=True)
    except ValidationError as error:
        raise ProofProducerError("installed_matrix_child_result_invalid") from error
    if envelope.candidate_commit != candidate_commit or envelope.analysis_kinds != analysis_kinds:
        raise ProofProducerError("installed_matrix_child_result_invalid")
    matrix = envelope.matrix
    if (
        matrix.status != "passed"
        or matrix.environment != "SIM"
        or not matrix.redacted_publication
        or matrix.live_events != 0
        or matrix.live_mutation_calls != 0
        or not matrix.cleanup_complete
        or not matrix.account_state_unchanged
        or matrix.before_state_fingerprint != matrix.after_state_fingerprint
        or matrix.uncleaned_resources != 0
        or matrix.errors
        or matrix.purchase_occurred
        or matrix.disclaimer_response_made
    ):
        raise ProofProducerError("installed_matrix_child_result_invalid")
    return matrix


def _execute_installed_proof_bundle(  # noqa: C901
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    harness_policy: HarnessPolicy = "dual_v1",
    native_sim_preflight: CodexNativeSimPreflightReceipt | None = None,
    progress: CodexNativeProofProgress | None = None,
) -> AnalyticsProofMatrixBundle:
    """Execute the fixed offline suite and actual logical-MCP SIM matrix in this child.

    No receipt, evidence, bundle, or path is accepted from the caller. The child creates its own
    owner-only workspace, runs the checked-in guarded suite, executes the matrix in process, and
    converts only those process-owned results into the strict typed bundle.
    """
    if harness_policy == "codex_native_v1":
        if native_sim_preflight is None or native_sim_preflight.status != "passed":
            raise ProofProducerError("native_sim_preflight_required")
        if progress is None:
            raise ProofProducerError("native_failure_progress_required")
    elif native_sim_preflight is not None:
        raise ProofProducerError("native_sim_preflight_not_allowed")
    elif progress is not None:
        raise ProofProducerError("native_failure_progress_not_allowed")
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    if progress is not None:
        progress.begin_phase("agent_evaluation")
    skill_receipts = _run_installed_agent_evaluation(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        harness_policy=harness_policy,
        progress=progress,
    )
    if progress is not None:
        progress.complete_phase("agent_evaluation")
        progress.begin_phase("offline_proof")
    suite_evidence = _run_installed_offline_proof_suite()
    if progress is not None:
        progress.complete_phase("offline_proof")
        progress.begin_phase("sim_matrix")
    matrix = SimToolMatrixReceipt.model_validate(
        _run_installed_matrix_proof_session(
            candidate_commit,
            catalog.analysis_kinds,
        ),
    )
    if progress is not None:
        progress.record_matrix(matrix)
    if matrix.status != "passed":
        raise ProofProducerError("installed_sim_matrix_not_passed")
    if progress is not None:
        progress.complete_phase("sim_matrix")
        progress.begin_phase("bundle_validation")
    bundle = _bundle_from_process_executions(
        candidate_commit=candidate_commit,
        suite_evidence=suite_evidence,
        matrix=matrix,
        contracts=contracts,
        skill_receipts=skill_receipts,
    )
    if progress is not None:
        progress.complete_phase("bundle_validation")
    return bundle


def _bundle_network_call_made(bundle: AnalyticsProofMatrixBundle) -> bool:
    matrix = bundle.sim_tool_matrix
    return (
        any(receipt.network_call_made for receipt in matrix.tool_receipts)
        or any(receipt.network_call_made for receipt in matrix.analysis_execution_receipts)
        or any(
            case.network_call_made
            for tool_receipt in matrix.analytics_case_receipts
            for case in tool_receipt.cases
        )
    )


def _run_installed_offline_proof_suite() -> InstalledProofSuiteEvidence:
    """Run the fixed candidate-local proof selection through the guarded launcher."""
    root = Path.cwd().resolve()
    launcher = root / "scripts/run-pytest"
    tests_root = root / "tests"
    if launcher.is_symlink() or not launcher.is_file() or not tests_root.is_dir():
        raise ProofProducerError("installed_proof_suite_unavailable")
    test_paths = tuple(
        sorted(
            (
                *tests_root.glob("test_analytics_*.py"),
                tests_root / "test_mcp_analytics_tools.py",
                tests_root / "test_qa_analytics_artifacts.py",
                tests_root / "test_saxo_analytics_skill.py",
                tests_root / "test_qa_secret_scan.py",
                tests_root / "test_read_response_redaction.py",
                tests_root / "test_redaction.py",
                tests_root / "test_secret_scan.py",
            ),
        )
    )
    test_paths = tuple(
        path
        for path in test_paths
        if path.is_file() and path.name != "test_analytics_source_process_boundary.py"
    )
    if not test_paths:
        raise ProofProducerError("installed_proof_suite_unavailable")
    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    with tempfile.TemporaryDirectory(prefix="analytics-proof-suite-", dir=temp_parent) as raw:
        runtime_root = Path(raw)
        runtime_root.chmod(0o700)
        junit = runtime_root / "proof-suite.xml"
        command = (
            str(launcher),
            *(str(path.relative_to(root)) for path in test_paths),
            "-q",
            "-o",
            "junit_family=legacy",
            f"--junitxml={junit}",
        )
        env = {
            "HOME": str(runtime_root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SAXO_MCP_ENVIRONMENT": "SIM",
            "UV_OFFLINE": "1",
            "XDG_STATE_HOME": str(runtime_root / "state"),
        }
        if uv_cache := os.environ.get("UV_CACHE_DIR"):
            env["UV_CACHE_DIR"] = uv_cache
        try:
            executed = run_command(
                "analytics_proof_suite",
                command,
                cwd=root,
                env=env,
                timeout_seconds=1800,
            )
        except CommandFailureError as error:
            raise ProofProducerError("installed_proof_suite_failed") from error
        try:
            document = ET.parse(junit).getroot()  # noqa: S314 - owner-local XML
            failures = sum(
                int(node.attrib.get("failures", "0")) + int(node.attrib.get("errors", "0"))
                for node in document.iter("testsuite")
            )
        except (OSError, ValueError, ET.ParseError) as error:
            raise ProofProducerError("installed_proof_suite_receipt_invalid") from error
        passed_testcases = tuple(
            node
            for node in document.iter("testcase")
            if not any(
                node.find(outcome) is not None for outcome in ("failure", "error", "skipped")
            )
        )
        passed_nodes = tuple(sorted(_junit_node_id(node) for node in passed_testcases))
        if not passed_nodes or failures != 0:
            raise ProofProducerError("installed_proof_suite_failed")
        receipt_material = {
            "argv": list(executed.receipt.argv),
            "cleanup_attempted": executed.receipt.cleanup_attempted,
            "exit_code": executed.receipt.exit_code,
            "passed_test_nodes_sha256": _digest(passed_nodes),
            "stderr_sha256": executed.receipt.stderr_sha256,
            "stdout_sha256": executed.receipt.stdout_sha256,
            "test_count": len(passed_nodes),
        }
        return _proof_suite_evidence_from_junit(
            passed_testcases,
            executed_test_count=len(passed_nodes),
            suite_receipt_sha256=_digest(receipt_material),
        )


def _proof_suite_evidence_from_test_nodes(  # pyright: ignore[reportUnusedFunction]
    passed_nodes: tuple[str, ...],
    *,
    suite_receipt_sha256: str,
) -> InstalledProofSuiteEvidence:
    """Reject aggregate node-name evidence retained only for adversarial compatibility tests."""
    _ = passed_nodes, suite_receipt_sha256
    raise ProofProducerError("installed_proof_contract_receipts_missing")


def _junit_node_id(node: ET.Element) -> str:
    return f"{node.attrib.get('classname', '')}::{node.attrib.get('name', '')}"


def _proof_suite_evidence_from_junit(  # noqa: C901, PLR0912
    passed_testcases: tuple[ET.Element, ...],
    *,
    executed_test_count: int,
    suite_receipt_sha256: str,
) -> InstalledProofSuiteEvidence:
    """Parse one strict measured observation from each explicitly reporting test node."""
    analysis_cases: list[MeasuredAnalysisProofObservation] = []
    parity: list[ArtifactParityReceipt] = []
    visual: list[ArtifactVisualIntegrityReceipt] = []
    receipt_nodes: set[str] = set()
    for testcase in passed_testcases:
        node_id = _junit_node_id(testcase)
        properties = testcase.find("properties")
        if properties is None:
            continue
        values = tuple(
            property_node.attrib.get("value", "")
            for property_node in properties.findall("property")
            if property_node.attrib.get("name") == _JUNIT_PROOF_PROPERTY
        )
        if not values:
            continue
        if len(values) != 1 or node_id in receipt_nodes:
            raise ProofProducerError("installed_proof_contract_receipt_ambiguous")
        receipt_nodes.add(node_id)
        try:
            loaded = json.loads(values[0])
        except (TypeError, ValueError) as error:
            raise ProofProducerError("installed_proof_contract_receipt_invalid") from error
        if not isinstance(loaded, dict):
            raise ProofProducerError("installed_proof_contract_receipt_invalid")
        payload = cast("dict[str, object]", loaded)
        receipt_kind = payload.get("receipt_kind")
        try:
            if receipt_kind == "analysis_case":
                observed = _AnalysisProofProperty.model_validate_json(values[0])
                if node_id != _analysis_proof_node_id(
                    observed.analysis_kind,
                    observed.case_kind,
                ):
                    raise ProofProducerError("installed_proof_contract_node_mismatch")
                analysis_cases.append(
                    MeasuredAnalysisProofObservation(
                        analysis_kind=observed.analysis_kind,
                        case_kind=observed.case_kind,
                        requirement_code=observed.requirement_code,
                        measurement_state=observed.measurement_state,
                        operation_kind=observed.operation_kind,
                        operation_id=observed.operation_id,
                        executed_test_node_id=observed.executed_test_node_id,
                        observed_result_count=observed.observed_result_count,
                        observed_result_types=observed.observed_result_types,
                        observed_result_sha256=observed.observed_result_sha256,
                        observed_value_count=observed.observed_value_count,
                        observed_output_sha256=observed.observed_output_sha256,
                        test_node_id=node_id,
                        executed_case_count=observed.executed_case_count,
                        failed_case_count=observed.failed_case_count,
                        comparison_count=observed.comparison_count,
                        unexplained_difference_count=observed.unexplained_difference_count,
                        mutation_count=observed.mutation_count,
                        mutation_killed_count=observed.mutation_killed_count,
                        independent_path_observed=observed.independent_path_observed,
                        recovery_observed=observed.recovery_observed,
                        publication_scan_passed=observed.publication_scan_passed,
                        evidence_sha256=_digest(
                            {
                                "node_id": node_id,
                                "observation": observed.model_dump(mode="json"),
                                "suite_receipt_sha256": suite_receipt_sha256,
                            },
                        ),
                    ),
                )
            elif receipt_kind == "artifact_parity":
                property_receipt = _ArtifactParityProperty.model_validate_json(values[0]).receipt
                if node_id != _artifact_proof_node_id(
                    "parity",
                    property_receipt.template_id,
                ):
                    raise ProofProducerError("installed_proof_contract_node_mismatch")
                parity.append(property_receipt)
            elif receipt_kind == "artifact_visual":
                property_receipt = _ArtifactVisualProperty.model_validate_json(values[0]).receipt
                if node_id != _artifact_proof_node_id(
                    "visual",
                    property_receipt.template_id,
                ):
                    raise ProofProducerError("installed_proof_contract_node_mismatch")
                visual.append(property_receipt)
            else:
                raise ProofProducerError("installed_proof_contract_receipt_invalid")
        except ValidationError as error:
            raise ProofProducerError("installed_proof_contract_receipt_invalid") from error

    evidence = InstalledProofSuiteEvidence(
        analysis_cases=tuple(analysis_cases),
        artifact_parity_receipts=tuple(parity),
        artifact_visual_receipts=tuple(visual),
        executed_test_count=executed_test_count,
        suite_receipt_sha256=suite_receipt_sha256,
    )
    _validate_installed_suite_coverage(evidence)
    return evidence


def _analysis_proof_node_id(analysis_kind: str, case_kind: ProofExecutionKind) -> str:
    return (
        "tests.test_analytics_proof_contracts::test_analysis_proof_contract"
        f"[{analysis_kind}-{case_kind}]"
    )


def _artifact_proof_node_id(kind: Literal["parity", "visual"], template_id: str) -> str:
    return f"tests.test_qa_analytics_artifacts::test_artifact_{kind}_contract[{template_id}]"


def _validate_installed_suite_coverage(evidence: InstalledProofSuiteEvidence) -> None:
    contracts = build_proof_execution_contracts()
    expected_cases: dict[tuple[str, ProofExecutionKind], ProofCaseContract] = {}
    for contract in contracts:
        for case in contract.cases:
            if case.applicability == "required" and case.kind not in {
                "agent_use",
                "executable_sim",
                "artifact_parity",
                "visual_integrity",
            }:
                expected_cases[(contract.analysis_kind, case.kind)] = case
    observed_cases = evidence.by_analysis_case()
    if set(observed_cases) != set(expected_cases):
        raise ProofProducerError("installed_proof_contract_receipts_missing")
    for key, contract_case in expected_cases.items():
        observed = observed_cases[key]
        if (
            observed.measurement_state != "passed"
            or observed.requirement_code != contract_case.requirement_code
            or observed.operation_kind != _MEASURED_OPERATION_BY_CASE[contract_case.kind]
            or observed.executed_test_node_id != exact_analysis_measurement_node_id(*key)
            or observed.executed_case_count < contract_case.minimum_case_count
            or (contract_case.independent_path_required and not observed.independent_path_observed)
        ):
            raise ProofProducerError("installed_proof_contract_observation_mismatch")
    expected_templates = set(load_analysis_kind_catalog().artifact_template_ids)
    if (
        {item.template_id for item in evidence.artifact_parity_receipts} != expected_templates
        or {item.template_id for item in evidence.artifact_visual_receipts} != expected_templates
        or any(item.state != "passed" for item in evidence.artifact_parity_receipts)
        or any(item.state != "passed" for item in evidence.artifact_visual_receipts)
    ):
        raise ProofProducerError("installed_artifact_contract_receipts_missing")


def _run_installed_agent_evaluation(  # noqa: C901, PLR0915
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    harness_policy: HarnessPolicy = "dual_v1",
    progress: CodexNativeProofProgress | None = None,
) -> tuple[SkillScenarioEvidenceReceipt, ...]:
    """Execute the policy-selected harness and authenticate its process-owned report."""

    def consume_process_report(  # noqa: C901, PLR0912, PLR0915
        command_result: CommandResult,
        *,
        command: tuple[str, ...],
        report_path: Path,
        cache_root: Path,
    ) -> tuple[SkillScenarioEvidenceReceipt, ...]:
        receipt = command_result.receipt
        if (
            receipt.name != _AGENT_EVAL_COMMAND_NAME
            or receipt.argv != command
            or Path(receipt.cwd).resolve() != cache_root
            or receipt.pid is None
            or receipt.pgid is None
            or receipt.exit_code != 0
            or receipt.timed_out
            or not receipt.cleanup_attempted
            or receipt.stdout_sha256 != hashlib.sha256(command_result.stdout.encode()).hexdigest()
            or receipt.stderr_sha256 != hashlib.sha256(command_result.stderr.encode()).hexdigest()
        ):
            raise ProofProducerError("installed_agent_evaluation_command_untrusted")
        try:
            metadata = report_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or report_path.is_symlink()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ProofProducerError(  # noqa: TRY301
                    "installed_agent_evaluation_report_unsafe",
                )
            report_bytes = report_path.read_bytes()
            raw = json.loads(report_bytes)
            if not isinstance(raw, dict):
                raise TypeError("agent evaluation report is not an object")  # noqa: TRY301
            payload = dict(cast("dict[str, object]", raw))
            run_cleanup = payload.pop("run_cleanup", None)
            installation_preserved = payload.pop("installation_fixture_preserved", None)
            report = EvalRunReport.model_validate(payload)
        except ProofProducerError:
            raise
        except (OSError, TypeError, ValidationError, ValueError) as error:
            raise ProofProducerError("installed_agent_evaluation_report_invalid") from error
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
        if report.source_commit != candidate_commit:
            raise ProofProducerError("installed_agent_evaluation_candidate_mismatch")
        if (
            not isinstance(run_cleanup, dict)
            or cast(
                "dict[str, object]",
                run_cleanup,
            ).get("complete")
            is not True
        ):
            raise ProofProducerError("installed_agent_evaluation_cleanup_incomplete")
        if installation_preserved is not True:
            raise ProofProducerError("installed_agent_evaluation_installation_changed")
        cleanup = report.cleanup
        if (
            report.status != "passed"
            or report.execution_mode != "model_execution"
            or report.case_count != len(report.records)
            or report.selected_case_count < 1
            or report.skipped_count != 0
            or not report.nonzero_on_skip
            or not report.global_state_unchanged
            or report.before_global_state != report.after_global_state
            or cleanup.get("complete") is not True
            or cleanup.get("remaining_processes", 0) != 0
            or cleanup.get("raw_transcripts_persisted", 0) != 0
        ):
            raise ProofProducerError("installed_agent_evaluation_not_passed")
        _require_agent_evaluation_harness_policy(report, harness_policy=harness_policy)
        invoked_by_tool: dict[str, list[EvalRunRecord]] = {}
        forbidden_by_tool: dict[str, list[EvalRunRecord]] = {}
        required_by_tool: dict[str, list[EvalRunRecord]] = {}
        for record in report.records:
            if (
                record.status != "passed"
                or record.execution_mode != "model_execution"
                or record.no_model_call
                or not record.transcript_assertions_passed
                or record.error
                or record.assertion_status != "passed"
                or record.grant_status not in {"passed", "not_required"}
            ):
                raise ProofProducerError("installed_agent_evaluation_not_passed")
            if "saxo_register_disclaimer_response" in record.invoked_logical_tools or any(
                grant == "saxo_register_disclaimer_response"
                or grant.endswith("__saxo_register_disclaimer_response")
                for grant in record.resolved_tool_grants
            ):
                raise ProofProducerError(
                    "installed_agent_evaluation_disclaimer_response_forbidden",
                )
            for tool_id in record.invoked_logical_tools:
                invoked_by_tool.setdefault(tool_id, []).append(record)
            for tool_id in record.forbidden_logical_tools:
                forbidden_by_tool.setdefault(tool_id, []).append(record)
            for tool_id in record.required_logical_tools:
                required_by_tool.setdefault(tool_id, []).append(record)
        catalog = load_analysis_kind_catalog()
        receipts: list[SkillScenarioEvidenceReceipt] = []
        for tool_id in catalog.skill_scenario_tools:
            required = required_by_tool.get(tool_id, [])
            invoked = invoked_by_tool.get(tool_id, [])
            forbidden = forbidden_by_tool.get(tool_id, [])
            if required:
                if any(tool_id not in record.invoked_logical_tools for record in required):
                    raise ProofProducerError("installed_agent_evaluation_required_tool_missing")
                observed = required
                observation_kind = "invoked"
            elif invoked:
                observed = invoked
                observation_kind = "invoked"
            else:
                observed = forbidden
                observation_kind = "forbidden"
            expected_harnesses = (
                {"codex"} if harness_policy == "codex_native_v1" else {"codex", "claude"}
            )
            if {record.harness for record in observed} != expected_harnesses:
                raise ProofProducerError("installed_agent_evaluation_tool_coverage_missing")
            receipts.append(
                SkillScenarioEvidenceReceipt(
                    tool_id=tool_id,
                    evaluation_state="passed",
                    evidence_sha256=_digest(
                        {
                            "candidate_commit": candidate_commit,
                            "installed_cache_sha256": installed_cache_sha256,
                            "report_sha256": report_sha256,
                            "command_stdout_sha256": receipt.stdout_sha256,
                            "tool_id": tool_id,
                            "observation_kind": observation_kind,
                            "observations": [
                                {
                                    "case_id": record.case_id,
                                    "harness": record.harness,
                                }
                                for record in observed
                            ],
                        },
                    ),
                    verification_state_reported=True,
                    warnings_preserved=True,
                    unsupported_inference_made=False,
                    unexpected_broker_write_made=False,
                    private_values_published=False,
                ),
            )
        if progress is not None:
            _record_agent_report_progress(progress, report)
        return tuple(receipts)

    codex_cache_root = Path.cwd().resolve()
    codex_home_raw = os.environ.get("SAXO_ANALYTICS_CODEX_HOME", "").strip()
    source_repo_raw = os.environ.get("SAXO_ANALYTICS_SOURCE_REPO", "").strip()
    if not codex_home_raw or not source_repo_raw:
        raise ProofProducerError("installed_agent_evaluation_runtime_missing")
    codex_home = Path(codex_home_raw).resolve()
    source_repo = Path(source_repo_raw).resolve()
    roots = [codex_cache_root, codex_home, source_repo]
    claude_cache_root: Path | None = None
    claude_home: Path | None = None
    if harness_policy == "dual_v1":
        claude_raw = os.environ.get("SAXO_ANALYTICS_CLAUDE_CACHE_ROOT", "").strip()
        claude_home_raw = os.environ.get("SAXO_ANALYTICS_CLAUDE_HOME", "").strip()
        if not claude_raw or not claude_home_raw:
            raise ProofProducerError("installed_agent_evaluation_runtime_missing")
        claude_cache_root = Path(claude_raw).resolve()
        claude_home = Path(claude_home_raw).resolve()
        roots.extend((claude_cache_root, claude_home))
    elif harness_policy != "codex_native_v1":
        raise ProofProducerError("installed_agent_harness_policy_unknown")
    for root in roots:
        try:
            metadata = root.lstat()
        except OSError as error:
            raise ProofProducerError("installed_agent_evaluation_runtime_missing") from error
        if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
            raise ProofProducerError("installed_agent_evaluation_runtime_unsafe")
    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    with tempfile.TemporaryDirectory(prefix="analytics-agent-eval-", dir=temp_parent) as raw:
        execution_root = Path(raw)
        execution_root.chmod(0o700)
        report_path = execution_root / (
            "codex-native-evaluation.json"
            if harness_policy == "codex_native_v1"
            else "dual-evaluation.json"
        )
        if harness_policy == "codex_native_v1":
            command = _codex_native_agent_evaluation_command(
                candidate_commit=candidate_commit,
                codex_cache_root=codex_cache_root,
                codex_home=codex_home,
                source_repo=source_repo,
                report_path=report_path,
            )
        else:
            if claude_cache_root is None or claude_home is None:
                raise ProofProducerError("installed_agent_evaluation_runtime_missing")
            command = _agent_evaluation_command(
                candidate_commit=candidate_commit,
                codex_cache_root=codex_cache_root,
                claude_cache_root=claude_cache_root,
                codex_home=codex_home,
                claude_home=claude_home,
                source_repo=source_repo,
                report_path=report_path,
            )
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["UV_OFFLINE"] = "1"
        try:
            result = run_command(
                _AGENT_EVAL_COMMAND_NAME,
                command,
                cwd=codex_cache_root,
                env=env,
                timeout_seconds=7200,
            )
        except CommandFailureError as error:
            if progress is not None:
                _record_failed_agent_report_progress(
                    progress,
                    report_path=report_path,
                    candidate_commit=candidate_commit,
                )
            raise ProofProducerError("installed_agent_evaluation_command_failed") from error
        return consume_process_report(
            result,
            command=command,
            report_path=report_path,
            cache_root=codex_cache_root,
        )


def _require_agent_evaluation_harness_policy(
    report: EvalRunReport,
    *,
    harness_policy: HarnessPolicy,
) -> None:
    if harness_policy == "dual_v1":
        if report.harness != "both":
            raise ProofProducerError("installed_agent_evaluation_not_passed")
        return
    if harness_policy != "codex_native_v1":
        raise ProofProducerError("installed_agent_harness_policy_unknown")
    if report.harness != "codex" or any(record.harness != "codex" for record in report.records):
        raise ProofProducerError("native_agent_harness_forbidden")
    if report.environment not in {"LOCAL", "SIM", "LOCAL+SIM"}:
        raise ProofProducerError("native_agent_environment_untrusted")
    counts: dict[str, int] = {}
    for record in report.records:
        counts[record.case_id] = counts.get(record.case_id, 0) + 1
    if (
        not counts
        or set(counts.values()) != {1}
        or len(counts) != report.selected_case_count
        or report.case_count != report.selected_case_count
        or any(record.no_model_call for record in report.records)
    ):
        raise ProofProducerError("native_agent_model_quorum_invalid")


def _record_agent_report_progress(
    progress: CodexNativeProofProgress,
    report: EvalRunReport,
) -> None:
    """Retain only redacted event counts from one typed native report."""
    if report.harness != "codex" or any(record.harness != "codex" for record in report.records):
        return
    progress.record_agent_activity(
        model_event_count=sum(not record.no_model_call for record in report.records),
        mcp_event_count=_sum_optional_counts(
            tuple(record.model_mcp_event_count for record in report.records),
        ),
        saxo_event_count=_sum_optional_counts(
            tuple(record.model_saxo_event_count for record in report.records),
        ),
    )


def _record_failed_agent_report_progress(
    progress: CodexNativeProofProgress,
    *,
    report_path: Path,
    candidate_commit: str,
) -> None:
    """Observe safe partial event counts without trusting a failed report as proof."""
    try:
        metadata = report_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or report_path.is_symlink()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            return
        raw = json.loads(report_path.read_bytes())
        if not isinstance(raw, dict):
            return
        payload = dict(cast("dict[str, object]", raw))
        payload.pop("run_cleanup", None)
        payload.pop("installation_fixture_preserved", None)
        report = EvalRunReport.model_validate(payload)
    except (OSError, TypeError, ValidationError, ValueError):
        return
    if report.source_commit != candidate_commit:
        return
    _record_agent_report_progress(progress, report)


def _sum_optional_counts(values: tuple[int | None, ...]) -> int | None:
    return (
        sum(cast("tuple[int, ...]", values)) if all(value is not None for value in values) else None
    )


def _codex_native_agent_evaluation_command(
    *,
    candidate_commit: str,
    codex_cache_root: Path,
    codex_home: Path,
    source_repo: Path,
    report_path: Path,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--offline",
        "python",
        "scripts/run_dual_harness_skill_evals.py",
        "--harness",
        "codex",
        "--harness-policy",
        "codex_native_v1",
        "--tag",
        "codex-native-proof",
        "--case-root",
        "evals",
        "--codex-plugin-root",
        str(codex_cache_root),
        "--codex-home",
        str(codex_home),
        "--source-codex-home",
        str(codex_home),
        "--credential-mode",
        "ephemeral-owner-only-copy",
        "--expected-source-commit",
        candidate_commit,
        "--source-repo",
        str(source_repo),
        "--out",
        str(report_path),
    )


def _agent_evaluation_command(  # noqa: PLR0913
    *,
    candidate_commit: str,
    codex_cache_root: Path,
    claude_cache_root: Path,
    codex_home: Path,
    claude_home: Path,
    source_repo: Path,
    report_path: Path,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--offline",
        "python",
        "scripts/run_dual_harness_skill_evals.py",
        "--harness",
        "both",
        "--case-root",
        "evals",
        "--codex-plugin-root",
        str(codex_cache_root),
        "--claude-plugin-root",
        str(claude_cache_root),
        "--codex-home",
        str(codex_home),
        "--claude-home",
        str(claude_home),
        "--source-codex-home",
        str(codex_home),
        "--source-claude-home",
        str(claude_home),
        "--credential-mode",
        "ephemeral-owner-only-copy",
        "--expected-source-commit",
        candidate_commit,
        "--source-repo",
        str(source_repo),
        "--out",
        str(report_path),
    )


def _bundle_from_process_executions(
    *,
    candidate_commit: str,
    suite_evidence: InstalledProofSuiteEvidence,
    matrix: object,
    contracts: tuple[AnalysisProofExecutionContract, ...],
    skill_receipts: tuple[SkillScenarioEvidenceReceipt, ...],
) -> AnalyticsProofMatrixBundle:
    """Build typed receipts only after both candidate-local executions passed."""
    typed_matrix = SimToolMatrixReceipt.model_validate(matrix)
    typed_catalog = load_analysis_kind_catalog()
    suite_by_case = suite_evidence.by_analysis_case()
    expected_sim_kinds = {
        contract.analysis_kind
        for contract in contracts
        for case in contract.cases
        if case.kind == "executable_sim" and case.applicability == "required"
    }
    terminal_by_analysis = _exact_terminal_analysis_receipts(
        typed_matrix,
        expected_kinds=expected_sim_kinds,
    )
    analysis_receipts = tuple(
        AnalysisEvidenceReceipt(
            evidence_receipt_id=contract.evidence_receipt_id,
            candidate_commit=candidate_commit,
            analysis_kind=contract.analysis_kind,
            proof_profile_id=contract.proof_profile_id,
            state="passed",
            checks=tuple(
                _executed_case_receipt(
                    contract,
                    case_index=index,
                    suite_by_case=suite_by_case,
                    matrix_observation=terminal_by_analysis[contract.analysis_kind],
                    parity_receipts=suite_evidence.artifact_parity_receipts,
                    visual_receipts=suite_evidence.artifact_visual_receipts,
                    skill_receipts=skill_receipts,
                )
                for index, _case in enumerate(contract.cases)
            ),
            redacted_publication=True,
            private_values_published=False,
            broker_write_made=False,
            live_mutation_calls=0,
        )
        for contract in contracts
    )
    parity_receipts = suite_evidence.artifact_parity_receipts
    visual_receipts = suite_evidence.artifact_visual_receipts
    lifecycle = typed_matrix.controlled_sim_lifecycle
    if lifecycle is None or lifecycle.evidence_state == "refused":
        raise ProofProducerError("installed_controlled_sim_lifecycle_missing")
    job_cases = next(
        receipt
        for receipt in typed_matrix.analytics_case_receipts
        if receipt.tool_id == "saxo_manage_analysis_job"
    )
    timeout_case = next(case for case in job_cases.cases if case.kind == "timeout")
    recovery_case = next(case for case in job_cases.cases if case.kind == "recovery")
    post_send_timeout = PostSendTimeoutReceipt(
        operation_kind="analytics_job_check",
        timeout_observed=True,
        stop_new_writes=True,
        reconciliation_attempted=True,
        reconciliation_state="no_effect_observed",
        matching_effect_count=0,
        blind_retry_attempted=False,
        recovery_action="reconcile_then_cleanup",
        ledger_fingerprint_sha256=_digest(
            {
                "recovery": recovery_case.evidence_sha256,
                "timeout": timeout_case.evidence_sha256,
            },
        ),
    )
    if tuple(receipt.tool_id for receipt in skill_receipts) != typed_catalog.skill_scenario_tools:
        raise ProofProducerError("installed_agent_evaluation_tool_coverage_missing")
    return AnalyticsProofMatrixBundle(
        candidate_commit=candidate_commit,
        analysis_receipts=analysis_receipts,
        artifact_parity_receipts=parity_receipts,
        artifact_visual_receipts=visual_receipts,
        sim_tool_matrix=typed_matrix,
        analytics_tool_case_receipts=typed_matrix.analytics_case_receipts,
        controlled_sim_lifecycle=lifecycle,
        post_send_timeout=post_send_timeout,
        skill_scenario_receipts=skill_receipts,
        privacy_scan_passed=True,
        secret_scan_passed=True,
        private_values_published=False,
    )


def _executed_case_receipt(  # noqa: PLR0913, PLR0915
    contract: AnalysisProofExecutionContract,
    *,
    case_index: int,
    suite_by_case: dict[
        tuple[str, ProofExecutionKind],
        MeasuredAnalysisProofObservation,
    ],
    matrix_observation: AnalyticsCaseReceipt,
    parity_receipts: tuple[ArtifactParityReceipt, ...],
    visual_receipts: tuple[ArtifactVisualIntegrityReceipt, ...],
    skill_receipts: tuple[SkillScenarioEvidenceReceipt, ...],
) -> ProofCaseReceipt:
    case = contract.cases[case_index]
    if case.applicability == "not_applicable":
        return ProofCaseReceipt(
            kind=case.kind,
            state="not_applicable",
            reason_code="not_applicable_by_frozen_contract",
            evidence_sha256=None,
            executed_case_count=0,
            failed_case_count=0,
            comparison_count=0,
            unexplained_difference_count=0,
            mutation_count=0,
            mutation_killed_count=0,
        )
    if case.kind == "executable_sim":
        evidence_sha256 = _digest(
            {
                "analysis_kind": contract.analysis_kind,
                "case_kind": case.kind,
                "expected_analysis_outcome": matrix_observation.expected_analysis_outcome,
                "matrix_case_evidence_sha256": matrix_observation.evidence_sha256,
                "matrix_request_sha256": matrix_observation.request_sha256,
                "matrix_response_sha256": matrix_observation.response_sha256,
                "matrix_result_state": matrix_observation.result_state,
                "matrix_state": matrix_observation.state,
            }
        )
        count = 1
        comparison_count = 0
        mutation_count = 0
        mutation_killed_count = 0
        recovery_observed = False
        publication_scan_passed = False
        environment: Literal["SIM"] | None = "SIM"
    elif case.kind == "artifact_parity":
        selected = tuple(
            receipt
            for receipt in parity_receipts
            if receipt.template_id in contract.artifact_template_ids
            and receipt.analysis_kind == contract.analysis_kind
        )
        if len(selected) != len(contract.artifact_template_ids):
            raise ProofProducerError("installed_artifact_contract_receipts_missing")
        evidence_sha256 = _digest([item.model_dump(mode="json") for item in selected])
        count = len(selected)
        comparison_count = sum(item.sampled_point_count for item in selected)
        mutation_count = mutation_killed_count = 0
        recovery_observed = publication_scan_passed = False
        environment = None
    elif case.kind == "visual_integrity":
        selected_visual = tuple(
            receipt
            for receipt in visual_receipts
            if receipt.template_id in contract.artifact_template_ids
        )
        if len(selected_visual) != len(contract.artifact_template_ids):
            raise ProofProducerError("installed_artifact_contract_receipts_missing")
        evidence_sha256 = _digest(
            [item.model_dump(mode="json") for item in selected_visual],
        )
        count = len(selected_visual)
        comparison_count = mutation_count = mutation_killed_count = 0
        recovery_observed = publication_scan_passed = False
        environment = None
    elif case.kind == "agent_use":
        selected_skill = next(
            (receipt for receipt in skill_receipts if receipt.tool_id == contract.tool_id),
            None,
        )
        if selected_skill is None or selected_skill.evaluation_state != "passed":
            raise ProofProducerError("installed_agent_evaluation_tool_coverage_missing")
        evidence_sha256 = selected_skill.evidence_sha256
        count = 1
        comparison_count = mutation_count = mutation_killed_count = 0
        recovery_observed = publication_scan_passed = False
        environment = None
    else:
        observed = suite_by_case.get((contract.analysis_kind, case.kind))
        if observed is None:
            raise ProofProducerError("installed_proof_contract_receipts_missing")
        evidence_sha256 = observed.evidence_sha256
        count = observed.executed_case_count
        comparison_count = observed.comparison_count
        mutation_count = observed.mutation_count
        mutation_killed_count = observed.mutation_killed_count
        recovery_observed = observed.recovery_observed
        publication_scan_passed = observed.publication_scan_passed
        environment = None
    return ProofCaseReceipt(
        kind=case.kind,
        state="passed",
        reason_code="passed",
        evidence_sha256=evidence_sha256,
        executed_case_count=count,
        failed_case_count=0,
        comparison_count=comparison_count,
        unexplained_difference_count=0,
        mutation_count=mutation_count,
        mutation_killed_count=mutation_killed_count,
        environment=environment,
        recovery_observed=recovery_observed,
        publication_scan_passed=publication_scan_passed,
    )


def _validate_executed_bundle(
    bundle: AnalyticsProofMatrixBundle,
    *,
    contracts: tuple[AnalysisProofExecutionContract, ...],
    candidate_commit: str,
    authority: object,
) -> tuple[str, ...]:
    """Privately validate one in-memory typed bundle issued by this process only."""
    if authority is not _PROCESS_AUTHORITY:
        raise ProofProducerError("trusted_producer_provenance_missing")
    if (
        type(bundle) is not AnalyticsProofMatrixBundle
        or bundle.candidate_commit != candidate_commit
    ):
        raise ProofProducerError("proof_bundle_candidate_mismatch")
    typed_contracts = tuple(contracts)
    errors = list(
        validate_proof_matrix_bundle(
            bundle,
            catalog=load_analysis_kind_catalog(),
            contracts=typed_contracts,
        ),
    )
    required_sim_kinds = {
        contract.analysis_kind
        for contract in typed_contracts
        for case in contract.cases
        if case.kind == "executable_sim" and case.applicability == "required"
    }
    try:
        _exact_terminal_analysis_receipts(
            bundle.sim_tool_matrix,
            expected_kinds=required_sim_kinds,
        )
    except ProofProducerError:
        errors.append("installed_analytics_terminal_receipt_missing")
    return tuple(error for error in errors if error != "trusted_producer_provenance_missing")


def _exact_terminal_analysis_receipts(
    matrix: SimToolMatrixReceipt,
    *,
    expected_kinds: set[str],
) -> dict[str, AnalyticsCaseReceipt]:
    """Select one exact honest terminal observation for every frozen analysis kind."""
    expected = {
        call.analysis_kind: (call.tool_id, call.expected_analysis_outcome)
        for call in analytics_case_calls()
        if call.analysis_kind is not None
    }
    if set(expected) != expected_kinds:
        raise ProofProducerError("installed_analytics_terminal_contract_mismatch")
    selected: dict[str, AnalyticsCaseReceipt] = {}
    for raw in matrix.analysis_execution_receipts:
        try:
            receipt = AnalyticsCaseReceipt.model_validate(raw.model_dump(mode="python"))
        except ValidationError as error:
            raise ProofProducerError("installed_analytics_terminal_receipt_invalid") from error
        analysis_kind = receipt.analysis_kind
        if analysis_kind is None or receipt.kind != "success" or not receipt.result_parsed:
            continue
        if analysis_kind not in expected:
            raise ProofProducerError("installed_analytics_terminal_receipt_unexpected")
        if analysis_kind in selected:
            raise ProofProducerError("installed_analytics_terminal_receipt_duplicate")
        if receipt.state == "failed" or receipt.expected_analysis_outcome is None:
            raise ProofProducerError("installed_analytics_terminal_receipt_invalid")
        expected_tool, expected_outcome = expected[analysis_kind]
        source_bound_refusal = (
            expected_outcome == "persisted"
            and receipt.expected_analysis_outcome == "refused"
            and receipt.source_precondition_refused
            and receipt.source_precondition_evidence_sha256 is not None
        )
        if receipt.tool_id != expected_tool or (
            receipt.expected_analysis_outcome != expected_outcome and not source_bound_refusal
        ):
            raise ProofProducerError("installed_analytics_terminal_receipt_contract_mismatch")
        selected[analysis_kind] = receipt
    if set(selected) != expected_kinds:
        raise ProofProducerError("installed_analytics_terminal_receipt_missing")
    return selected


def _validate_process_owned_result(  # noqa: PLR0913
    command_result: CommandResult,
    *,
    command: tuple[str, ...],
    cache_root: Path,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
    authority: object,
) -> VerifiedInstalledProofValidation:
    """Authenticate typed child stdout without accepting any caller evidence path."""
    if authority is not _PROCESS_AUTHORITY:
        raise ProofProducerError("trusted_producer_provenance_missing")
    receipt = command_result.receipt
    stdout_sha256 = hashlib.sha256(command_result.stdout.encode()).hexdigest()
    stderr_sha256 = hashlib.sha256(command_result.stderr.encode()).hexdigest()
    if (
        receipt.name != _COMMAND_NAME
        or receipt.argv != command
        or Path(receipt.cwd).resolve() != cache_root.resolve()
        or receipt.pid is None
        or receipt.pgid is None
        or receipt.exit_code != 0
        or receipt.timed_out
        or not receipt.cleanup_attempted
        or receipt.stdout_sha256 != stdout_sha256
        or receipt.stderr_sha256 != stderr_sha256
    ):
        raise ProofProducerError("proof_producer_command_untrusted")
    try:
        produced = InstalledProofProducerResult.model_validate_json(command_result.stdout)
    except ValidationError as error:
        raise ProofProducerError("proof_producer_result_invalid") from error
    catalog_sha256, contract_sha256 = _installed_contract_digests()
    if (
        produced.candidate_commit != candidate_commit
        or produced.installed_cache_sha256 != installed_cache_sha256
        or produced.producer_module_sha256 != producer_module_sha256
        or produced.catalog_sha256 != catalog_sha256
        or produced.contract_sha256 != contract_sha256
    ):
        raise ProofProducerError("proof_producer_candidate_binding_mismatch")
    if produced.status == "validated":
        expected_count = len(build_proof_execution_contracts())
        expected_execution_sha256 = _digest(
            {
                "bundle_sha256": produced.bundle_sha256,
                "candidate_commit": candidate_commit,
                "installed_cache_sha256": installed_cache_sha256,
                "producer_module_sha256": producer_module_sha256,
            },
        )
    else:
        expected_count = 0
        expected_execution_sha256 = _digest(
            {
                "candidate_commit": candidate_commit,
                "installed_cache_sha256": installed_cache_sha256,
                "producer_module_sha256": producer_module_sha256,
                "reason": produced.validation_errors[0],
            },
        )
    if produced.executed_receipt_count != expected_count or (
        produced.proof_execution_sha256 != expected_execution_sha256
    ):
        raise ProofProducerError("proof_producer_execution_binding_mismatch")
    return VerifiedInstalledProofValidation(
        status=produced.status,
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
        producer_authenticated=True,
        execution_performed=produced.execution_performed,
        process_local_activation=produced.process_local_activation,
        executed_receipt_count=produced.executed_receipt_count,
        proof_execution_sha256=produced.proof_execution_sha256,
        bundle_sha256=produced.bundle_sha256,
        validation_errors=produced.validation_errors,
    )


def _validate_codex_native_process_owned_result(  # noqa: PLR0913
    command_result: CommandResult,
    *,
    command: tuple[str, ...],
    cache_root: Path,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
    authority: object,
) -> CodexNativeVerifiedInstalledProofValidation:
    if authority is not _PROCESS_AUTHORITY:
        raise ProofProducerError("trusted_producer_provenance_missing")
    receipt = command_result.receipt
    if (
        receipt.name != _COMMAND_NAME
        or receipt.argv != command
        or Path(receipt.cwd).resolve() != cache_root.resolve()
        or receipt.pid is None
        or receipt.pgid is None
        or receipt.exit_code != 0
        or receipt.timed_out
        or not receipt.cleanup_attempted
        or receipt.stdout_sha256 != hashlib.sha256(command_result.stdout.encode()).hexdigest()
        or receipt.stderr_sha256 != hashlib.sha256(command_result.stderr.encode()).hexdigest()
    ):
        raise ProofProducerError("proof_producer_command_untrusted")
    try:
        produced = CodexNativeInstalledProofProducerResult.model_validate_json(
            command_result.stdout,
        )
    except ValidationError as error:
        raise ProofProducerError("proof_producer_result_invalid") from error
    catalog_sha256, contract_sha256 = _installed_contract_digests()
    if (
        produced.candidate_commit != candidate_commit
        or produced.installed_cache_sha256 != installed_cache_sha256
        or produced.producer_module_sha256 != producer_module_sha256
        or produced.catalog_sha256 != catalog_sha256
        or produced.contract_sha256 != contract_sha256
        or produced.harness_policy != "codex_native_v1"
    ):
        raise ProofProducerError("proof_producer_candidate_binding_mismatch")
    if produced.status == "validated":
        expected_count = len(build_proof_execution_contracts())
        material = {
            "bundle_sha256": produced.bundle_sha256,
            "candidate_commit": candidate_commit,
            "harness_policy": "codex_native_v1",
            "installed_cache_sha256": installed_cache_sha256,
            "network_call_made": produced.network_call_made,
            "producer_module_sha256": producer_module_sha256,
            "sim_preflight": produced.sim_preflight.model_dump(mode="json"),
        }
    else:
        expected_count = 0
        material = {
            "candidate_commit": candidate_commit,
            "harness_policy": "codex_native_v1",
            "installed_cache_sha256": installed_cache_sha256,
            "network_call_made": produced.network_call_made,
            "producer_module_sha256": producer_module_sha256,
            "reason": produced.validation_errors[0],
            "sim_preflight": produced.sim_preflight.model_dump(mode="json"),
        }
    if (
        produced.executed_receipt_count != expected_count
        or produced.proof_execution_sha256 != _digest(material)
    ):
        raise ProofProducerError("proof_producer_execution_binding_mismatch")
    return CodexNativeVerifiedInstalledProofValidation(
        status=produced.status,
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
        producer_authenticated=True,
        execution_performed=produced.execution_performed,
        process_local_activation=produced.process_local_activation,
        executed_receipt_count=produced.executed_receipt_count,
        proof_execution_sha256=produced.proof_execution_sha256,
        bundle_sha256=produced.bundle_sha256,
        sim_preflight=produced.sim_preflight,
        network_call_made=produced.network_call_made,
        validation_errors=produced.validation_errors,
    )


def _producer_command(candidate_commit: str, installed_cache_sha256: str) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--offline",
        "python",
        "-m",
        "saxo_bank_mcp.qa_analytics_proof_producer",
        "--candidate-commit",
        candidate_commit,
        "--installed-cache-sha256",
        installed_cache_sha256,
    )


def _codex_native_producer_command(
    candidate_commit: str,
    installed_cache_sha256: str,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--offline",
        "python",
        "-m",
        "saxo_bank_mcp.qa_analytics_proof_producer",
        "--candidate-commit",
        candidate_commit,
        "--installed-cache-sha256",
        installed_cache_sha256,
        "--harness-policy",
        "codex_native_v1",
    )


def _execute_installed_child(  # noqa: C901, PLR0912, PLR0913
    cache_root: Path,
    command: tuple[str, ...],
    *,
    claude_cache_root: Path,
    source_repo: Path,
    retained_codex_home: Path,
    retained_claude_home: Path,
) -> CommandResult:
    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    with tempfile.TemporaryDirectory(prefix="analytics-proof-producer-", dir=temp_parent) as raw:
        runtime_root = Path(raw)
        runtime_root.chmod(0o700)
        try:
            runtime = prepare_eval_isolated_runtime(
                runtime_root,
                source_codex_home=None,
                source_claude_home=None,
                retained_codex_home=retained_codex_home,
                retained_claude_home=retained_claude_home,
                retained_codex_plugin_root=cache_root,
            )
        except MatrixEnvError as error:
            raise ProofProducerError("proof_sim_auth_lease_unavailable") from error
        command_error: CommandFailureError | None = None
        result: CommandResult | None = None
        promotion_error: MatrixEnvError | None = None
        cleanup_error: MatrixEnvError | None = None
        env = dict(runtime.env)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["UV_OFFLINE"] = "1"
        env["SAXO_ANALYTICS_CLAUDE_CACHE_ROOT"] = str(claude_cache_root.resolve())
        env["SAXO_ANALYTICS_CODEX_HOME"] = str(runtime.codex_home.resolve())
        env["SAXO_ANALYTICS_CLAUDE_HOME"] = str(runtime.home.resolve())
        env["SAXO_ANALYTICS_SOURCE_REPO"] = str(source_repo.resolve())
        if uv_cache := os.environ.get("UV_CACHE_DIR"):
            env["UV_CACHE_DIR"] = uv_cache
        try:
            result = run_command(
                _COMMAND_NAME,
                command,
                cwd=cache_root.resolve(),
                env=env,
                timeout_seconds=3600,
            )
        except CommandFailureError as error:
            command_error = error
        finally:
            try:
                promote_rotated_sim_token_cache(runtime)
            except MatrixEnvError as error:
                promotion_error = error
            try:
                promote_rotated_claude_credentials(runtime)
            except MatrixEnvError as error:
                if promotion_error is None:
                    promotion_error = error
                else:
                    promotion_error = MatrixEnvError(
                        f"{promotion_error.reason}+{error.reason}",
                    )
            try:
                require_matrix_runtime_cleanup(runtime.run_root)
            except MatrixEnvError as error:
                cleanup_error = error
        if promotion_error is not None:
            raise ProofProducerError("proof_sim_token_promotion_failed") from promotion_error
        if command_error is not None:
            raise ProofProducerError("proof_producer_command_failed") from command_error
        if cleanup_error is not None:
            raise ProofProducerError("proof_sim_auth_lease_cleanup_failed") from cleanup_error
        if result is None:
            raise ProofProducerError("proof_producer_result_missing")
        return result


def _execute_codex_native_installed_child(  # noqa: PLR0913
    cache_root: Path,
    command: tuple[str, ...],
    *,
    source_repo: Path,
    retained_codex_home: Path,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
) -> CommandResult:
    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    with tempfile.TemporaryDirectory(prefix="analytics-codex-native-", dir=temp_parent) as raw:
        runtime_root = Path(raw)
        runtime_root.chmod(0o700)
        try:
            runtime = prepare_eval_isolated_runtime(
                runtime_root,
                source_codex_home=None,
                source_claude_home=None,
                retained_codex_home=retained_codex_home,
                retained_claude_home=None,
                retained_codex_plugin_root=cache_root,
                harness_policy="codex_native_v1",
            )
        except MatrixEnvError as error:
            raise ProofProducerError("proof_sim_auth_lease_unavailable") from error
        command_error: CommandFailureError | None = None
        result: CommandResult | None = None
        promotion_error: MatrixEnvError | None = None
        cleanup_error: MatrixEnvError | None = None
        env = dict(runtime.env)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["UV_OFFLINE"] = "1"
        env["SAXO_ANALYTICS_CODEX_HOME"] = str(runtime.codex_home.resolve())
        env["SAXO_ANALYTICS_SOURCE_REPO"] = str(source_repo.resolve())
        env["SAXO_ANALYTICS_HARNESS_POLICY"] = "codex_native_v1"
        if uv_cache := os.environ.get("UV_CACHE_DIR"):
            env["UV_CACHE_DIR"] = uv_cache
        try:
            result = run_command(
                _COMMAND_NAME,
                command,
                cwd=cache_root.resolve(),
                env=env,
                timeout_seconds=3600,
            )
        except CommandFailureError as error:
            command_error = error
        finally:
            try:
                promote_rotated_sim_token_cache(runtime)
            except MatrixEnvError as error:
                promotion_error = error
            try:
                require_matrix_runtime_cleanup(runtime.run_root)
            except MatrixEnvError as error:
                cleanup_error = error
        if command_error is not None:
            command_receipt = command_error.receipt
            command_trusted = (
                command_receipt.name == _COMMAND_NAME
                and command_receipt.argv == command
                and Path(command_receipt.cwd).resolve() == cache_root.resolve()
                and command_receipt.pid is not None
                and command_receipt.pgid is not None
                and command_receipt.exit_code != 0
                and command_receipt.cleanup_attempted
                and command_receipt.stdout_sha256
                == hashlib.sha256(command_error.stdout.encode()).hexdigest()
                and command_receipt.stderr_sha256
                == hashlib.sha256(command_error.stderr.encode()).hexdigest()
            )
            catalog_sha256, contract_sha256 = _installed_contract_digests()
            verified_failure = verify_child_failure_envelope(
                raw_stdout=command_error.stdout if command_trusted else "",
                candidate_commit=candidate_commit,
                installed_cache_sha256=installed_cache_sha256,
                producer_module_sha256=producer_module_sha256,
                catalog_sha256=catalog_sha256,
                contract_sha256=contract_sha256,
                child_exit_code=command_receipt.exit_code,
                command_timed_out=command_receipt.timed_out,
                command_cleanup_attempted=command_receipt.cleanup_attempted,
                command_stdout_sha256=command_receipt.stdout_sha256,
                command_stderr_sha256=command_receipt.stderr_sha256,
                remaining_process_count=command_error.remaining_process_count,
                remaining_process_group_count=command_error.remaining_process_group_count,
                runtime_cleanup_status=(
                    "complete" if promotion_error is None and cleanup_error is None else "failed"
                ),
            )
            raise CodexNativeProofFailureError(verified_failure) from command_error
        if promotion_error is not None:
            raise ProofProducerError("proof_sim_token_promotion_failed") from promotion_error
        if cleanup_error is not None:
            raise ProofProducerError("proof_sim_auth_lease_cleanup_failed") from cleanup_error
        if result is None:
            raise ProofProducerError("proof_producer_result_missing")
        return result


def _require_clean_source_commit(repo: Path, candidate_commit: str) -> None:
    resolved = repo.resolve()
    if git_output(resolved, "rev-parse", "HEAD") != candidate_commit:
        raise ProofProducerError("proof_source_candidate_mismatch")
    if git_output(resolved, "status", "--porcelain", "--untracked-files=no") != "":
        raise ProofProducerError("proof_source_worktree_not_clean")


def _verified_install_digests(
    install: InstallEvidenceReport,
) -> tuple[str, str, str]:
    clone = install.clone.path.resolve()
    publishable = publishable_tracked_files(clone)
    if not publishable:
        raise ProofProducerError("proof_installed_inventory_empty")
    digests = [tree_digest(clone, publishable)]
    for cache in (install.codex.cache_root.resolve(), install.claude.cache_root.resolve()):
        inventory = installed_inventory_check(clone, cache, publishable=publishable)
        if inventory.get("inventory_exact_match") is not True:
            raise ProofProducerError("proof_installed_inventory_mismatch")
        digests.append(tree_digest(cache, publishable))
    return digests[0], digests[1], digests[2]


def _verified_codex_install_digests(
    install: CodexInstallEvidenceReport,
) -> tuple[str, str]:
    clone = install.clone.path.resolve()
    publishable = publishable_tracked_files(clone)
    if not publishable:
        raise ProofProducerError("proof_installed_inventory_empty")
    cache = install.codex.cache_root.resolve()
    inventory = installed_inventory_check(clone, cache, publishable=publishable)
    if inventory.get("inventory_exact_match") is not True:
        raise ProofProducerError("proof_installed_inventory_mismatch")
    return tree_digest(clone, publishable), tree_digest(cache, publishable)


def _installed_producer_module_sha256(cache_root: Path) -> str:
    path = cache_root.resolve() / _PRODUCER_MODULE_RELATIVE
    if path.is_symlink() or not path.is_file():
        raise ProofProducerError("proof_installed_producer_missing")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_contract_digests() -> tuple[str, str]:
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    catalog_material = catalog.model_dump(mode="json")
    contract_material = [contract.model_dump(mode="json") for contract in contracts]
    return _digest(catalog_material), _digest(contract_material)


def _digest(value: object) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(rendered).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the installed analytics proof producer.")
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--installed-cache-sha256", required=True)
    parser.add_argument(
        "--harness-policy",
        choices=("dual_v1", "codex_native_v1"),
        default="dual_v1",
    )
    args = parser.parse_args(argv)
    if args.harness_policy == "codex_native_v1":
        progress: CodexNativeProofProgress | None = None
        try:
            catalog_sha256, contract_sha256 = _installed_contract_digests()
            progress = CodexNativeProofProgress(
                candidate_commit=str(args.candidate_commit),
                installed_cache_sha256=str(args.installed_cache_sha256),
                producer_module_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                catalog_sha256=catalog_sha256,
                contract_sha256=contract_sha256,
            )
            result = produce_codex_native_installed_result(
                candidate_commit=str(args.candidate_commit),
                installed_cache_sha256=str(args.installed_cache_sha256),
                progress=progress,
            )
        except Exception as error:  # noqa: BLE001
            if progress is not None:
                envelope = build_child_failure_envelope(
                    progress,
                    child_exit_code=1,
                    reason=safe_failure_reason(str(error)),
                )
                sys.stdout.write(envelope.model_dump_json())
            return 1
    else:
        try:
            result = produce_installed_result(
                candidate_commit=str(args.candidate_commit),
                installed_cache_sha256=str(args.installed_cache_sha256),
            )
        except (OSError, ProofProducerError, ValidationError, ValueError):
            return 1
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
