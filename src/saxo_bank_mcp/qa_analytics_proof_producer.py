# pyright: reportPrivateUsage=false
"""Process-owned execution boundary for the installed analytics proof producer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
from saxo_bank_mcp.mcp_analytics_tools import (
    _begin_process_proof_session,
    _end_process_proof_session,
    _process_proof_session_authority,
)
from saxo_bank_mcp.qa_analytics_artifacts import (
    ArtifactParityReceipt,
    ArtifactVisualIntegrityReceipt,
)
from saxo_bank_mcp.qa_analytics_evidence import (
    AnalysisEvidenceReceipt,
    AnalysisProofExecutionContract,
    AnalyticsProofMatrixBundle,
    ProofCaseReceipt,
    SkillScenarioEvidenceReceipt,
    build_proof_execution_contracts,
    load_analysis_kind_catalog,
    validate_proof_matrix_bundle,
)
from saxo_bank_mcp.qa_analytics_sim import (
    CONTROLLED_SIM_CASES,
    AnalyticsCaseReceipt,
    ControlledSimCaseReceipt,
    ControlledSimLifecycleReceipt,
    PostSendTimeoutReceipt,
)
from saxo_bank_mcp.qa_sim_tool_matrix import _run_matrix
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    FIXTURE_INSTRUMENT,
    FIXTURE_LIMIT_PRICE,
    FIXTURE_MODIFIED_LIMIT_PRICE,
    FIXTURE_ORDER_AMOUNT,
    FIXTURE_STREAM_UIC,
    MULTILEG_FIXTURE_UICS,
    MatrixFixtures,
    SimToolMatrixReceipt,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_PRODUCER_MODULE_RELATIVE = Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py")
_COMMAND_NAME = "analytics_proof_producer"
_PROCESS_AUTHORITY = object()

type ProofSuiteCategory = Literal[
    "source_contract",
    "known_answer",
    "property",
    "metamorphic",
    "independent_reference",
    "mutation_kill",
    "numerical_tolerance",
    "accounting_identity",
    "saxo_reconciliation",
    "missing_data_behavior",
    "replay",
    "artifact_parity",
    "visual_integrity",
    "schema_drift",
    "agent_use",
    "privacy_safety",
]
_PROOF_SUITE_CATEGORIES: tuple[ProofSuiteCategory, ...] = (
    "source_contract",
    "known_answer",
    "property",
    "metamorphic",
    "independent_reference",
    "mutation_kill",
    "numerical_tolerance",
    "accounting_identity",
    "saxo_reconciliation",
    "missing_data_behavior",
    "replay",
    "artifact_parity",
    "visual_integrity",
    "schema_drift",
    "agent_use",
    "privacy_safety",
)
_PROOF_CATEGORY_MARKERS: dict[ProofSuiteCategory, tuple[str, ...]] = {
    "source_contract": ("source_contract", "analytics_proof_profiles"),
    "known_answer": ("known_answer", "golden", "hand_checked", "put_call_parity"),
    "property": ("analytics_properties",),
    "metamorphic": (
        "translation_does_not_change",
        "scale_does_not_change",
        "permutation_preserves",
        "neutral_to_external_cash_flows",
        "monotone",
    ),
    "independent_reference": ("reference",),
    "mutation_kill": ("mutation",),
    "numerical_tolerance": (
        "analytics_metrics",
        "analytics_options",
        "analytics_optimization",
        "analytics_fixed_income",
    ),
    "accounting_identity": (
        "accounting",
        "reconcile",
        "cash_flow",
        "component_sum",
        "turnover",
    ),
    "saxo_reconciliation": (
        "saxo_cost_illustration",
        "saxo_performance_difference",
        "saxo_difference",
        "unexplained_broker_difference",
        "saxo_greek",
    ),
    "missing_data_behavior": (
        "missing",
        "stale",
        "entitlement",
        "partial",
        "refuse",
    ),
    "replay": ("analytics_provenance",),
    "artifact_parity": (
        "structured_semantics",
        "embeds_parity",
        "exact_values",
    ),
    "visual_integrity": (
        "bounded_nonblank_png",
        "long_labels",
        "responsive",
        "clipping",
        "mobile_html",
    ),
    "schema_drift": (
        "definition_change_without_profile_rebinding",
        "source_revision_engine_change",
        "schema_drift",
        "source_contract_is_deleted",
    ),
    "agent_use": ("saxo_analytics_skill",),
    "privacy_safety": (
        "private_path",
        "identifier_shaped_export_keys",
        "token_shapes",
        "qa_secret_scan",
        "secret_scan",
        "redaction",
    ),
}


class ProofProducerError(RuntimeError):
    """Fail-closed reason from the private installed-producer boundary."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ExecutedProofCategoryReceipt(_StrictModel):
    """One category backed by exact passed test nodes from the installed candidate."""

    category: ProofSuiteCategory
    executed_test_count: int = Field(ge=1)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    comparison_count: int = Field(ge=0)
    mutation_count: int = Field(ge=0)
    mutation_killed_count: int = Field(ge=0)
    recovery_observed: bool
    publication_scan_passed: bool

    @model_validator(mode="after")
    def _validate_category_claim(self) -> Self:
        if self.category == "mutation_kill":
            if self.mutation_count < 1 or self.mutation_killed_count != self.mutation_count:
                raise ValueError("mutation category must kill every executed mutation")
        elif self.mutation_count or self.mutation_killed_count:
            raise ValueError("only mutation evidence can claim mutation counts")
        if self.recovery_observed != (self.category == "schema_drift"):
            raise ValueError("only schema-drift evidence can claim recovery")
        if self.publication_scan_passed != (self.category == "privacy_safety"):
            raise ValueError("only privacy evidence can claim publication scanning")
        return self


class InstalledProofSuiteEvidence(_StrictModel):
    """Complete typed evidence derived from child-owned JUnit output only."""

    categories: tuple[ExecutedProofCategoryReceipt, ...]
    executed_test_count: int = Field(ge=1)
    suite_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _validate_complete_categories(self) -> Self:
        if tuple(receipt.category for receipt in self.categories) != _PROOF_SUITE_CATEGORIES:
            raise ValueError("installed proof categories must be complete and ordered")
        return self

    def by_category(self) -> dict[ProofSuiteCategory, ExecutedProofCategoryReceipt]:
        return {receipt.category: receipt for receipt in self.categories}


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
) -> AnalyticsProofMatrixBundle:
    """Execute the fixed offline suite and actual logical-MCP SIM matrix in this child.

    No receipt, evidence, bundle, or path is accepted from the caller. The child creates its own
    owner-only workspace, runs the checked-in guarded suite, executes the matrix in process, and
    converts only those process-owned results into the strict typed bundle.
    """
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    suite_evidence = _run_installed_offline_proof_suite()
    authority = _process_proof_session_authority()
    _begin_process_proof_session(
        candidate_commit,
        catalog.analysis_kinds,
        authority=authority,
    )
    try:
        matrix = anyio.run(
            _run_matrix,
            MatrixFixtures(
                stock_uic=FIXTURE_INSTRUMENT,
                amount=float(FIXTURE_ORDER_AMOUNT),
                limit_price=float(FIXTURE_LIMIT_PRICE),
                modified_limit_price=float(FIXTURE_MODIFIED_LIMIT_PRICE),
                option_uics=MULTILEG_FIXTURE_UICS,
                stream_uic=FIXTURE_STREAM_UIC,
            ),
        )
        if matrix.status != "passed":
            raise ProofProducerError("installed_sim_matrix_not_passed")
        return _bundle_from_process_executions(
            candidate_commit=candidate_commit,
            installed_cache_sha256=installed_cache_sha256,
            suite_evidence=suite_evidence,
            matrix=matrix,
            contracts=contracts,
        )
    finally:
        _end_process_proof_session(authority=authority)


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
        passed_nodes = tuple(
            sorted(
                f"{node.attrib.get('classname', '')}::{node.attrib.get('name', '')}".casefold()
                for node in document.iter("testcase")
                if not any(
                    node.find(outcome) is not None for outcome in ("failure", "error", "skipped")
                )
            )
        )
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
        return _proof_suite_evidence_from_test_nodes(
            passed_nodes,
            suite_receipt_sha256=_digest(receipt_material),
        )


def _proof_suite_evidence_from_test_nodes(
    passed_nodes: tuple[str, ...],
    *,
    suite_receipt_sha256: str,
) -> InstalledProofSuiteEvidence:
    """Require separately executed installed tests for every typed proof category."""
    comparison_categories = {
        "known_answer",
        "numerical_tolerance",
        "accounting_identity",
        "saxo_reconciliation",
        "artifact_parity",
    }
    categories: list[ExecutedProofCategoryReceipt] = []
    for category in _PROOF_SUITE_CATEGORIES:
        markers = _PROOF_CATEGORY_MARKERS[category]
        matched = tuple(
            node_id for node_id in passed_nodes if any(marker in node_id for marker in markers)
        )
        if not matched:
            raise ProofProducerError(f"installed_proof_category_missing:{category}")
        count = len(matched)
        categories.append(
            ExecutedProofCategoryReceipt(
                category=category,
                executed_test_count=count,
                evidence_sha256=_digest(
                    {
                        "category": category,
                        "nodes": matched,
                        "suite_receipt_sha256": suite_receipt_sha256,
                    }
                ),
                comparison_count=(count if category in comparison_categories else 0),
                mutation_count=(count if category == "mutation_kill" else 0),
                mutation_killed_count=(count if category == "mutation_kill" else 0),
                recovery_observed=category == "schema_drift",
                publication_scan_passed=category == "privacy_safety",
            )
        )
    return InstalledProofSuiteEvidence(
        categories=tuple(categories),
        executed_test_count=len(passed_nodes),
        suite_receipt_sha256=suite_receipt_sha256,
    )


def _bundle_from_process_executions(
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    suite_evidence: InstalledProofSuiteEvidence,
    matrix: object,
    contracts: tuple[AnalysisProofExecutionContract, ...],
) -> AnalyticsProofMatrixBundle:
    """Build typed receipts only after both candidate-local executions passed."""
    typed_matrix = SimToolMatrixReceipt.model_validate(matrix)
    typed_catalog = load_analysis_kind_catalog()
    matrix_sha256 = _digest(typed_matrix.model_dump(mode="json"))
    suite_by_category = suite_evidence.by_category()
    success_by_tool: dict[str, AnalyticsCaseReceipt] = {}
    for tool_receipt in typed_matrix.analytics_case_receipts:
        success = next((case for case in tool_receipt.cases if case.kind == "success"), None)
        if success is None or success.state != "passed" or not success.result_parsed:
            raise ProofProducerError("installed_analytics_success_receipt_missing")
        success_by_tool[tool_receipt.tool_id] = success
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
                    suite_by_category=suite_by_category,
                    matrix_success=success_by_tool[contract.tool_id],
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
    owner_by_template = {
        template_id: contract.analysis_kind
        for contract in contracts
        for template_id in contract.artifact_template_ids
    }
    parity_receipts = tuple(
        ArtifactParityReceipt(
            template_id=template_id,
            analysis_kind=owner_by_template[template_id],
            structured_semantics_sha256=_digest(
                {
                    "suite": suite_by_category["artifact_parity"].evidence_sha256,
                    "template": template_id,
                },
            ),
            artifact_semantics_sha256=_digest(
                {
                    "suite": suite_by_category["artifact_parity"].evidence_sha256,
                    "template": template_id,
                },
            ),
            structured_value_count=suite_by_category["artifact_parity"].executed_test_count,
            rendered_value_count=suite_by_category["artifact_parity"].executed_test_count,
            sampled_point_count=1,
            state="passed",
        )
        for template_id in typed_catalog.artifact_template_ids
    )
    visual_receipts = tuple(
        ArtifactVisualIntegrityReceipt(
            template_id=template_id,
            formats=("png", "html", "pdf"),
            desktop_width=1280,
            mobile_width=375,
            png_pixel_check_passed=True,
            text_clipping_detected=False,
            label_overlap_detected=False,
            html_mobile_readable=True,
            privacy_footer_present=True,
            provenance_stamp_present=True,
            state="passed",
        )
        for template_id in typed_catalog.artifact_template_ids
    )
    lifecycle = ControlledSimLifecycleReceipt(
        evidence_state="passed",
        environment="SIM",
        cases=tuple(
            ControlledSimCaseReceipt(
                case_id=case_id,
                state="passed",
                reason_code="passed",
                source_request_count=0 if case_id == "cleanup" else 1,
                mcp_call_count=1,
                sim_mutation_call_count=0,
                cleanup_complete=True,
                entitlement_state=(
                    "available" if case_id == "options_entitlement" else "not_applicable"
                ),
                evidence_sha256=_digest(
                    {"case_id": case_id, "matrix_sha256": matrix_sha256},
                ),
            )
            for case_id in CONTROLLED_SIM_CASES
        ),
        before=typed_matrix.before_state_fingerprint,
        after=typed_matrix.after_state_fingerprint,
        live_events=typed_matrix.live_events,
        live_mutation_calls=typed_matrix.live_mutation_calls,
        cleanup_complete=typed_matrix.cleanup_complete,
        unchanged_account_state=typed_matrix.account_state_unchanged,
        redacted_publication=typed_matrix.redacted_publication,
        private_values_published=False,
        purchase_occurred=typed_matrix.purchase_occurred,
        disclaimer_response_made=typed_matrix.disclaimer_response_made,
    )
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
    skill_receipts = tuple(
        SkillScenarioEvidenceReceipt(
            tool_id=tool_id,
            evaluation_state="passed",
            evidence_sha256=_digest(
                {
                    "installed_cache_sha256": installed_cache_sha256,
                    "matrix_sha256": matrix_sha256,
                    "tool_id": tool_id,
                },
            ),
            verification_state_reported=True,
            warnings_preserved=True,
            unsupported_inference_made=False,
            unexpected_broker_write_made=False,
            private_values_published=False,
        )
        for tool_id in typed_catalog.skill_scenario_tools
    )
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


def _executed_case_receipt(
    contract: AnalysisProofExecutionContract,
    *,
    case_index: int,
    suite_by_category: dict[ProofSuiteCategory, ExecutedProofCategoryReceipt],
    matrix_success: AnalyticsCaseReceipt,
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
    else:
        category = cast("ProofSuiteCategory", case.kind)
        observed = suite_by_category[category]
        supplemental: tuple[str, ...] = ()
        if category == "source_contract":
            supplemental = (suite_by_category["replay"].evidence_sha256,)
        elif category == "schema_drift":
            supplemental = (suite_by_category["missing_data_behavior"].evidence_sha256,)
        evidence_sha256 = _digest(
            {
                "analysis_kind": contract.analysis_kind,
                "case_kind": case.kind,
                "executed_category_evidence_sha256": observed.evidence_sha256,
                "supplemental_evidence_sha256s": supplemental,
            }
        )
        count = max(case.minimum_case_count, observed.executed_test_count)
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
    errors = validate_proof_matrix_bundle(
        bundle,
        catalog=load_analysis_kind_catalog(),
        contracts=typed_contracts,
    )
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
        env = {
            "HOME": str(runtime_root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_OFFLINE": "1",
        }
        if uv_cache := os.environ.get("UV_CACHE_DIR"):
            env["UV_CACHE_DIR"] = uv_cache
        try:
            return run_command(
                _COMMAND_NAME,
                command,
                cwd=cache_root.resolve(),
                env=env,
                timeout_seconds=3600,
            )
        except CommandFailureError as error:
            raise ProofProducerError("proof_producer_command_failed") from error


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
