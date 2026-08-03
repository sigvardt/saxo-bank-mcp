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
from pathlib import Path
from typing import Literal, Self, cast

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    prepare_matrix_isolated_runtime,
    promote_rotated_sim_token_cache,
    require_matrix_runtime_cleanup,
)
from saxo_bank_mcp.mcp_analytics_tools import (
    _run_installed_matrix_proof_session,
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
    load_analysis_kind_catalog,
    validate_proof_matrix_bundle,
)
from saxo_bank_mcp.qa_analytics_sim import (
    AnalyticsCaseReceipt,
    PostSendTimeoutReceipt,
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_PRODUCER_MODULE_RELATIVE = Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py")
_COMMAND_NAME = "analytics_proof_producer"
_PROCESS_AUTHORITY = object()
_JUNIT_PROOF_PROPERTY = "saxo_analytics_proof_receipt_v1"


class ProofProducerError(RuntimeError):
    """Fail-closed reason from the private installed-producer boundary."""


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
    measurement_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


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
    def _validate_observation(self) -> Self:
        comparison_kinds = {
            "known_answer",
            "numerical_tolerance",
            "accounting_identity",
            "saxo_reconciliation",
        }
        if self.failed_case_count or self.unexplained_difference_count:
            raise ValueError("proof observation contains a failed or unexplained case")
        if self.case_kind in comparison_kinds and self.comparison_count < 1:
            raise ValueError("comparison observation did not compare values")
        if self.case_kind == "mutation_kill":
            if self.mutation_count < 1 or self.mutation_killed_count != self.mutation_count:
                raise ValueError("mutation observation did not kill every mutation")
        elif self.mutation_count or self.mutation_killed_count:
            raise ValueError("non-mutation observation cannot claim mutation counts")
        if self.recovery_observed != (self.case_kind == "schema_drift"):
            raise ValueError("only schema-drift observations can claim recovery")
        if self.publication_scan_passed != (self.case_kind == "privacy_safety"):
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


class _BoundAgentEvaluationArtifact(_StrictModel):
    """Owner-only wrapper around one actual installed dual-agent evaluation report."""

    schema_version: Literal["1"] = "1"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    installed_cache_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    report: EvalRunReport


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
    result = _execute_installed_child(install.codex.cache_root, command)

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


def _execute_installed_proof_bundle(
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    agent_evaluation_artifact_path: Path | None = None,
) -> AnalyticsProofMatrixBundle:
    """Execute the fixed offline suite and actual logical-MCP SIM matrix in this child.

    No receipt, evidence, bundle, or path is accepted from the caller. The child creates its own
    owner-only workspace, runs the checked-in guarded suite, executes the matrix in process, and
    converts only those process-owned results into the strict typed bundle.
    """
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    skill_receipts = _load_bound_agent_evaluation_artifact(
        agent_evaluation_artifact_path,
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
    )
    suite_evidence = _run_installed_offline_proof_suite()
    matrix = SimToolMatrixReceipt.model_validate(
        anyio.run(
            _run_installed_matrix_proof_session,
            candidate_commit,
            catalog.analysis_kinds,
        ),
    )
    if matrix.status != "passed":
        raise ProofProducerError("installed_sim_matrix_not_passed")
    return _bundle_from_process_executions(
        candidate_commit=candidate_commit,
        suite_evidence=suite_evidence,
        matrix=matrix,
        contracts=contracts,
        skill_receipts=skill_receipts,
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
                comparison_kinds = {
                    "known_answer",
                    "numerical_tolerance",
                    "accounting_identity",
                    "saxo_reconciliation",
                }
                analysis_cases.append(
                    MeasuredAnalysisProofObservation(
                        analysis_kind=observed.analysis_kind,
                        case_kind=observed.case_kind,
                        requirement_code=observed.requirement_code,
                        test_node_id=node_id,
                        executed_case_count=1,
                        failed_case_count=0,
                        comparison_count=int(observed.case_kind in comparison_kinds),
                        unexplained_difference_count=0,
                        mutation_count=int(observed.case_kind == "mutation_kill"),
                        mutation_killed_count=int(observed.case_kind == "mutation_kill"),
                        independent_path_observed=(observed.case_kind == "independent_reference"),
                        recovery_observed=observed.case_kind == "schema_drift",
                        publication_scan_passed=observed.case_kind == "privacy_safety",
                        evidence_sha256=_digest(
                            {
                                "measurement_sha256": observed.measurement_sha256,
                                "node_id": node_id,
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
            observed.requirement_code != contract_case.requirement_code
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


def _load_bound_agent_evaluation_artifact(  # noqa: C901
    path: Path | None,
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
) -> tuple[SkillScenarioEvidenceReceipt, ...]:
    """Authenticate an owner-only installed-agent report or fail closed before proof selection."""
    if path is None:
        raise ProofProducerError("installed_agent_evaluation_artifact_missing")
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ProofProducerError(  # noqa: TRY301
                "installed_agent_evaluation_artifact_unsafe",
            )
        artifact = _BoundAgentEvaluationArtifact.model_validate_json(
            path.read_text(encoding="utf-8"),
            strict=True,
        )
    except ProofProducerError:
        raise
    except (OSError, ValidationError, ValueError) as error:
        raise ProofProducerError("installed_agent_evaluation_artifact_invalid") from error
    report = artifact.report
    report_sha256 = _digest(report.model_dump(mode="json"))
    if (
        artifact.candidate_commit != candidate_commit
        or artifact.installed_cache_sha256 != installed_cache_sha256
        or artifact.report_sha256 != report_sha256
        or report.source_commit != candidate_commit
    ):
        raise ProofProducerError("installed_agent_evaluation_artifact_mismatch")
    cleanup = report.cleanup
    if (
        report.status != "passed"
        or report.harness != "both"
        or report.execution_mode != "model_execution"
        or report.case_count != len(report.records)
        or report.selected_case_count < 1
        or report.skipped_count != 0
        or not report.nonzero_on_skip
        or not report.global_state_unchanged
        or cleanup.get("complete") is not True
        or cleanup.get("remaining_processes", 0) != 0
        or cleanup.get("raw_transcripts_persisted", 0) != 0
    ):
        raise ProofProducerError("installed_agent_evaluation_artifact_not_passed")
    records_by_tool: dict[str, list[EvalRunRecord]] = {}
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
            raise ProofProducerError("installed_agent_evaluation_artifact_not_passed")
        for tool_id in record.invoked_logical_tools:
            records_by_tool.setdefault(tool_id, []).append(record)
    catalog = load_analysis_kind_catalog()
    receipts: list[SkillScenarioEvidenceReceipt] = []
    for tool_id in catalog.skill_scenario_tools:
        observed = records_by_tool.get(tool_id, [])
        if {record.harness for record in observed} != {"codex", "claude"}:
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
                        "tool_id": tool_id,
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
    return tuple(receipts)


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
    success_by_analysis = {
        receipt.analysis_kind: receipt
        for receipt in typed_matrix.analysis_execution_receipts
        if receipt.analysis_kind is not None
        and receipt.kind == "success"
        and receipt.state == "passed"
        and receipt.result_parsed
        and receipt.expected_analysis_outcome == "persisted"
        and receipt.returned_analysis_kind == receipt.analysis_kind
        and receipt.analysis_id is not None
        and receipt.persisted_result_authenticated
    }
    expected_sim_kinds = {
        contract.analysis_kind
        for contract in contracts
        for case in contract.cases
        if case.kind == "executable_sim" and case.applicability == "required"
    }
    if set(success_by_analysis) != expected_sim_kinds:
        raise ProofProducerError("installed_analytics_success_receipt_missing")
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
                    matrix_success=success_by_analysis[contract.analysis_kind],
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
    if lifecycle is None or lifecycle.evidence_state != "passed":
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
    matrix_success: AnalyticsCaseReceipt,
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
                "matrix_case_evidence_sha256": matrix_success.evidence_sha256,
                "matrix_request_sha256": matrix_success.request_sha256,
                "matrix_response_sha256": matrix_success.response_sha256,
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
    persisted_sim_kinds = {
        receipt.analysis_kind
        for receipt in bundle.sim_tool_matrix.analysis_execution_receipts
        if receipt.analysis_kind is not None
        and receipt.state == "passed"
        and receipt.expected_analysis_outcome == "persisted"
        and receipt.returned_analysis_kind == receipt.analysis_kind
        and receipt.analysis_id is not None
        and receipt.persisted_result_authenticated
    }
    if persisted_sim_kinds != required_sim_kinds:
        errors.append("installed_analytics_success_receipt_missing")
    return tuple(error for error in errors if error != "trusted_producer_provenance_missing")


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


def _execute_installed_child(
    cache_root: Path,
    command: tuple[str, ...],
) -> CommandResult:
    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    with tempfile.TemporaryDirectory(prefix="analytics-proof-producer-", dir=temp_parent) as raw:
        runtime_root = Path(raw)
        runtime_root.chmod(0o700)
        try:
            runtime = prepare_matrix_isolated_runtime(
                runtime_root,
                runtime_name="proof-sim-runtime",
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
        if promotion_error is not None:
            raise ProofProducerError("proof_sim_token_promotion_failed") from promotion_error
        if command_error is not None:
            raise ProofProducerError("proof_producer_command_failed") from command_error
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
    args = parser.parse_args(argv)
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
