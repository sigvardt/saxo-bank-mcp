"""Generated coverage and strict proof contracts for all production analytics kinds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.agent_skill_matrix import SCENARIO_MANIFEST, ScenarioManifest
from saxo_bank_mcp.analytics_chart_semantics import core_template_bindings
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionCatalog,
    NumericTolerance,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    EngineProofBinding,
    ProofProfileCatalog,
    SourceContractProofBinding,
    load_proof_profile_catalog,
)
from saxo_bank_mcp.analytics_source_contracts import (
    load_source_contract_catalog,
    source_contract_catalog_sha256,
)
from saxo_bank_mcp.qa_analytics_artifacts import (
    ArtifactParityReceipt,
    ArtifactVisualIntegrityReceipt,
    artifact_evidence_errors,
)
from saxo_bank_mcp.qa_analytics_sim import (
    AnalyticsToolCaseEvidence,
    ControlledSimLifecycleReceipt,
    PostSendTimeoutReceipt,
    analytics_case_contract_sha256,
    analytics_case_evidence_errors,
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS, ANALYTICS_TOOL_IDS

type ProofExecutionKind = Literal[
    "source_contract",
    "known_answer",
    "property",
    "metamorphic",
    "independent_reference",
    "mutation_kill",
    "numerical_tolerance",
    "accounting_identity",
    "saxo_reconciliation",
    "executable_sim",
    "artifact_parity",
    "visual_integrity",
    "schema_drift",
    "agent_use",
    "privacy_safety",
]
type ProofCaseState = Literal["passed", "failed", "degraded", "refused", "not_applicable"]

PROOF_EXECUTION_KINDS: Final[tuple[ProofExecutionKind, ...]] = (
    "source_contract",
    "known_answer",
    "property",
    "metamorphic",
    "independent_reference",
    "mutation_kill",
    "numerical_tolerance",
    "accounting_identity",
    "saxo_reconciliation",
    "executable_sim",
    "artifact_parity",
    "visual_integrity",
    "schema_drift",
    "agent_use",
    "privacy_safety",
)
_SOURCE_CATALOG_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data/analytics/analysis_kind_catalog.json"
)
_WORKTREE_CATALOG_PATH: Final = Path("data/analytics/analysis_kind_catalog.json")
ANALYSIS_KIND_CATALOG_PATH: Final = (
    _SOURCE_CATALOG_PATH if _SOURCE_CATALOG_PATH.is_file() else _WORKTREE_CATALOG_PATH
)
_SAFE_ID_PATTERN: Final = r"^[a-z][a-z0-9_]{0,127}$"
type ExactOfflineOperationKind = Literal[
    "known_answer_comparison",
    "property_assertion",
    "independent_reference_comparison",
    "accounting_identity_comparison",
]


@dataclass(frozen=True, slots=True)
class ExactOfflineProofBinding:
    """One exact installed assertion and its measured call semantics."""

    test_node_id: str
    operation_kind: ExactOfflineOperationKind
    observed_calls_per_case: int
    comparisons_per_case: int = 0
    reference_calls_per_case: int = 0


EXACT_OFFLINE_PROOF_SUPPORT: Final[
    dict[tuple[str, ProofExecutionKind], ExactOfflineProofBinding]
] = {
    ("cash_and_settlement", "accounting_identity"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_liquidity::test_cash_settlement_and_multi_currency_identities_reconcile",
        operation_kind="accounting_identity_comparison",
        observed_calls_per_case=1,
        comparisons_per_case=1,
    ),
    ("fixed_income", "known_answer"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_fixed_income::test_golden_par_bond_yield_duration_convexity_carry_and_roll_down",
        operation_kind="known_answer_comparison",
        observed_calls_per_case=1,
        comparisons_per_case=1,
    ),
    ("fixed_income", "property"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_fixed_income::test_property_positive_cash_flow_price_falls_as_yield_rises",
        operation_kind="property_assertion",
        observed_calls_per_case=2,
    ),
    ("instrument_price_return", "independent_reference"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_properties::test_property_price_scale_does_not_change_returns",
        operation_kind="independent_reference_comparison",
        observed_calls_per_case=4,
        comparisons_per_case=1,
        reference_calls_per_case=1,
    ),
    ("instrument_price_return", "property"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_properties::test_property_price_scale_does_not_change_returns",
        operation_kind="property_assertion",
        observed_calls_per_case=4,
    ),
    ("portfolio_minimum_variance", "independent_reference"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_optimization::test_minimum_variance_known_answer_reference_and_kkt_residuals",
        operation_kind="independent_reference_comparison",
        observed_calls_per_case=2,
        comparisons_per_case=1,
        reference_calls_per_case=1,
    ),
    ("portfolio_minimum_variance", "known_answer"): ExactOfflineProofBinding(
        test_node_id="tests.test_analytics_optimization::test_minimum_variance_known_answer_reference_and_kkt_residuals",
        operation_kind="known_answer_comparison",
        observed_calls_per_case=2,
        comparisons_per_case=1,
    ),
}
_OFFLINE_PROOF_KINDS: Final[frozenset[ProofExecutionKind]] = frozenset(
    {
        "source_contract",
        "known_answer",
        "property",
        "metamorphic",
        "independent_reference",
        "mutation_kill",
        "numerical_tolerance",
        "accounting_identity",
        "saxo_reconciliation",
        "schema_drift",
        "privacy_safety",
    },
)


def exact_analysis_measurement_node_id(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
) -> str:
    """Return the domain assertion bound to one exact supported proof pair."""
    try:
        return EXACT_OFFLINE_PROOF_SUPPORT[(analysis_kind, case_kind)].test_node_id
    except KeyError as error:
        raise EvidenceCoverageError("analysis measurement node is not catalogued") from error


def exact_offline_proof_binding(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
) -> ExactOfflineProofBinding:
    """Return measured semantics for one exact supported proof pair."""
    try:
        return EXACT_OFFLINE_PROOF_SUPPORT[(analysis_kind, case_kind)]
    except KeyError as error:
        raise EvidenceCoverageError("analysis proof binding is not catalogued") from error


class EvidenceCoverageError(ValueError):
    """Raised when checked-in evidence coverage diverges from a frozen inventory."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class AnalysisKindCatalog(_StrictModel):
    """One generated, duplicate-free inventory for the final proof candidate."""

    schema_version: Literal["1"]
    catalog_version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$")
    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    metric_definition_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_contract_ids: tuple[str, ...]
    metric_definition_ids: tuple[str, ...]
    proof_profile_ids: tuple[str, ...]
    analysis_kinds: tuple[str, ...]
    analysis_tool_ids: tuple[str, ...]
    tool_ids: tuple[str, ...]
    artifact_template_ids: tuple[str, ...]
    skill_scenario_tools: tuple[str, ...]
    evidence_receipt_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_unique_inventory(self) -> Self:
        inventories = (
            (self.source_contract_ids, "source contracts"),
            (self.metric_definition_ids, "metric definitions"),
            (self.proof_profile_ids, "proof profiles"),
            (self.analysis_kinds, "analysis kinds"),
            (self.tool_ids, "tools"),
            (self.artifact_template_ids, "artifact templates"),
            (self.skill_scenario_tools, "skill scenarios"),
            (self.evidence_receipt_ids, "evidence receipts"),
        )
        for values, label in inventories:
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{label} must be present exactly once")
        if not (
            len(self.proof_profile_ids)
            == len(self.analysis_kinds)
            == len(self.analysis_tool_ids)
            == len(self.evidence_receipt_ids)
        ):
            raise ValueError("analysis, profile, and evidence inventories must align")
        for analysis_kind, profile_id, receipt_id in zip(
            self.analysis_kinds,
            self.proof_profile_ids,
            self.evidence_receipt_ids,
            strict=True,
        ):
            if profile_id != f"vp_{analysis_kind}_v1":
                raise ValueError("proof profile inventory is not bound to its analysis kind")
            if receipt_id != f"ae_{analysis_kind}_v1":
                raise ValueError("evidence receipt inventory is not bound to its analysis kind")
        if any(tool_id not in ANALYTICS_TOOL_IDS for tool_id in self.analysis_tool_ids):
            raise ValueError("analysis kinds must map to analytics tools")
        return self


class MetricToleranceContract(_StrictModel):
    metric_id: str = Field(pattern=_SAFE_ID_PATTERN)
    definition_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    mode: Literal[
        "exact",
        "absolute_relative",
        "currency_minor_unit",
        "model_calibration",
    ]
    tolerance_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProofCaseContract(_StrictModel):
    kind: ProofExecutionKind
    applicability: Literal["required", "not_applicable"]
    requirement_code: str = Field(pattern=_SAFE_ID_PATTERN)
    minimum_case_count: int = Field(ge=0)
    independent_path_required: bool = False
    required_environment: Literal["SIM"] | None = None
    required_recovery: Literal["quarantine_or_refusal"] | None = None
    broker_write_allowed: Literal[False] = False
    live_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_applicability(self) -> Self:
        if self.applicability == "required" and self.minimum_case_count < 1:
            raise ValueError("required proof cases need at least one execution")
        if self.applicability == "not_applicable" and self.minimum_case_count != 0:
            raise ValueError("not-applicable proof cases cannot claim executions")
        if self.kind == "executable_sim" and self.required_environment != "SIM":
            raise ValueError("executable analytics proof must require SIM")
        if self.kind == "schema_drift" and self.required_recovery != "quarantine_or_refusal":
            raise ValueError("schema drift must quarantine or refuse")
        return self


class AnalysisProofExecutionContract(_StrictModel):
    analysis_kind: str = Field(pattern=_SAFE_ID_PATTERN)
    tool_id: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    proof_profile_id: str = Field(pattern=r"^vp_[a-z][a-z0-9_]{0,127}_v1$")
    proof_profile_version: str
    proof_activation_state: Literal["active", "quarantined"]
    quarantine_reason: str | None
    output_schema_version: Literal["1"]
    evidence_receipt_id: str = Field(pattern=r"^ae_[a-z][a-z0-9_]{0,127}_v1$")
    definition_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str | None
    engine_bindings: tuple[EngineProofBinding, ...]
    source_bindings: tuple[SourceContractProofBinding, ...]
    metric_ids: tuple[str, ...]
    source_contract_ids: tuple[str, ...]
    artifact_template_ids: tuple[str, ...]
    metric_tolerances: tuple[MetricToleranceContract, ...]
    cases: tuple[ProofCaseContract, ...]

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if self.proof_profile_id != f"vp_{self.analysis_kind}_v1":
            raise ValueError("proof execution contract names a mismatched profile")
        if self.evidence_receipt_id != f"ae_{self.analysis_kind}_v1":
            raise ValueError("proof execution contract names a mismatched receipt")
        if (
            tuple(binding.contract_id for binding in self.source_bindings)
            != self.source_contract_ids
        ):
            raise ValueError("proof execution source IDs must match exact source bindings")
        for values, label in (
            (self.metric_ids, "metrics"),
            (self.source_contract_ids, "sources"),
            (self.artifact_template_ids, "artifacts"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"proof execution {label} must be unique")
        if tuple(case.kind for case in self.cases) != PROOF_EXECUTION_KINDS:
            raise ValueError("proof execution cases must cover every required kind exactly once")
        tolerance_ids = tuple(item.metric_id for item in self.metric_tolerances)
        if tolerance_ids != self.metric_ids:
            raise ValueError("every metric requires exactly one metric-specific tolerance")
        return self


class ProofCaseReceipt(_StrictModel):
    kind: ProofExecutionKind
    state: ProofCaseState
    reason_code: str = Field(pattern=_SAFE_ID_PATTERN)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    executed_case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    comparison_count: int = Field(ge=0)
    unexplained_difference_count: int = Field(ge=0)
    mutation_count: int = Field(ge=0)
    mutation_killed_count: int = Field(ge=0)
    environment: Literal["SIM"] | None = None
    recovery_observed: bool = False
    publication_scan_passed: bool = False

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:  # noqa: C901
        if self.state == "not_applicable":
            if (
                self.evidence_sha256 is not None
                or self.reason_code == "passed"
                or self.executed_case_count != 0
                or self.failed_case_count != 0
                or self.comparison_count != 0
                or self.mutation_count != 0
                or self.mutation_killed_count != 0
            ):
                raise ValueError("not-applicable proof cannot retain or claim evidence")
        elif self.evidence_sha256 is None:
            raise ValueError("executed proof cases require an evidence fingerprint")
        if self.state == "passed":
            if (
                self.reason_code != "passed"
                or self.executed_case_count < 1
                or self.failed_case_count != 0
                or self.unexplained_difference_count != 0
            ):
                raise ValueError("passed proof cases require clean executed evidence")
            if (
                self.kind
                in {
                    "known_answer",
                    "numerical_tolerance",
                    "accounting_identity",
                    "saxo_reconciliation",
                    "artifact_parity",
                }
                and self.comparison_count < 1
            ):
                raise ValueError("comparison proof requires at least one comparison")
            if self.kind == "mutation_kill" and (
                self.mutation_count < 1 or self.mutation_killed_count != self.mutation_count
            ):
                raise ValueError("mutation proof must kill every requested mutation")
            if self.kind == "executable_sim" and self.environment != "SIM":
                raise ValueError("executable proof requires observed SIM execution")
            if self.kind == "schema_drift" and not self.recovery_observed:
                raise ValueError("schema-drift proof requires fail-closed recovery")
            if self.kind == "privacy_safety" and not self.publication_scan_passed:
                raise ValueError("privacy proof requires a clean publication scan")
        return self


class AnalysisEvidenceReceipt(_StrictModel):
    schema_version: Literal["1"] = "1"
    evidence_receipt_id: str = Field(pattern=r"^ae_[a-z][a-z0-9_]{0,127}_v1$")
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    analysis_kind: str = Field(pattern=_SAFE_ID_PATTERN)
    proof_profile_id: str = Field(pattern=r"^vp_[a-z][a-z0-9_]{0,127}_v1$")
    state: Literal["passed", "degraded", "refused", "failed"]
    checks: tuple[ProofCaseReceipt, ...]
    redacted_publication: bool
    private_values_published: bool
    broker_write_made: bool
    live_mutation_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_publication_claim(self) -> Self:
        if self.evidence_receipt_id != f"ae_{self.analysis_kind}_v1":
            raise ValueError("evidence receipt is not bound to its analysis kind")
        if self.proof_profile_id != f"vp_{self.analysis_kind}_v1":
            raise ValueError("evidence receipt is not bound to its proof profile")
        if tuple(check.kind for check in self.checks) != PROOF_EXECUTION_KINDS:
            raise ValueError("proof checks must appear exactly once in fixed order")
        if self.state == "passed" and any(
            check.state not in {"passed", "not_applicable"} for check in self.checks
        ):
            raise ValueError("passed evidence contains a non-passing proof check")
        if (
            not self.redacted_publication
            or self.private_values_published
            or self.broker_write_made
            or self.live_mutation_calls != 0
        ):
            raise ValueError("publishable analytics evidence violates privacy or write safety")
        return self


class SkillScenarioEvidenceReceipt(_StrictModel):
    tool_id: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    evaluation_state: Literal["passed", "failed", "refused"]
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verification_state_reported: bool
    warnings_preserved: bool
    unsupported_inference_made: bool
    unexpected_broker_write_made: bool
    private_values_published: bool

    @model_validator(mode="after")
    def _validate_pass(self) -> Self:
        if self.evaluation_state == "passed" and (
            not self.verification_state_reported
            or not self.warnings_preserved
            or self.unsupported_inference_made
            or self.unexpected_broker_write_made
            or self.private_values_published
        ):
            raise ValueError("passed skill evidence violates the agent-use contract")
        return self


class AnalyticsProofMatrixBundle(_StrictModel):
    """One complete final-run input; plan-only output can never satisfy this model."""

    schema_version: Literal["1"] = "1"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    analysis_receipts: tuple[AnalysisEvidenceReceipt, ...]
    artifact_parity_receipts: tuple[ArtifactParityReceipt, ...]
    artifact_visual_receipts: tuple[ArtifactVisualIntegrityReceipt, ...]
    sim_tool_matrix: SimToolMatrixReceipt
    analytics_tool_case_receipts: tuple[AnalyticsToolCaseEvidence, ...]
    controlled_sim_lifecycle: ControlledSimLifecycleReceipt
    post_send_timeout: PostSendTimeoutReceipt
    skill_scenario_receipts: tuple[SkillScenarioEvidenceReceipt, ...]
    privacy_scan_passed: Literal[True]
    secret_scan_passed: Literal[True]
    private_values_published: Literal[False]


def validate_proof_matrix_bundle(  # noqa: C901, PLR0912
    bundle: AnalyticsProofMatrixBundle,
    *,
    catalog: AnalysisKindCatalog | None = None,
    contracts: Sequence[AnalysisProofExecutionContract] | None = None,
) -> tuple[str, ...]:
    """Validate claims without granting producer authority from caller-owned data.

    The public validator is intentionally incapable of issuing a final pass. The exact
    installed producer must execute the matrix and use its private process-bound validation
    path; JSON receipts and recomputable hashes are never authority.
    """
    selected_catalog = catalog or load_analysis_kind_catalog()
    selected_contracts = tuple(
        contracts or build_proof_execution_contracts(catalog=selected_catalog)
    )
    errors: list[str] = ["trusted_producer_provenance_missing"]
    errors.extend(
        validate_analysis_evidence(
            bundle.analysis_receipts,
            contracts=selected_contracts,
        ),
    )
    if any(
        receipt.candidate_commit != bundle.candidate_commit for receipt in bundle.analysis_receipts
    ):
        errors.append("candidate_commit_mismatch")
    if any(receipt.state != "passed" for receipt in bundle.analysis_receipts):
        errors.append("analysis_proof_not_passed")
    if any(contract.proof_activation_state != "active" for contract in selected_contracts):
        errors.append("proof_profiles_not_active")
    errors.extend(
        artifact_evidence_errors(
            bundle.artifact_parity_receipts,
            bundle.artifact_visual_receipts,
            expected_template_ids=selected_catalog.artifact_template_ids,
        ),
    )
    expected_artifact_owners = {
        template_id: contract.analysis_kind
        for contract in selected_contracts
        for template_id in contract.artifact_template_ids
    }
    if any(
        expected_artifact_owners.get(receipt.template_id) != receipt.analysis_kind
        for receipt in bundle.artifact_parity_receipts
    ):
        errors.append("artifact_analysis_binding_mismatch")
    if bundle.sim_tool_matrix.status != "passed":
        errors.append("sim_tool_matrix_not_passed")
    if bundle.sim_tool_matrix.live_mutation_calls != 0:
        errors.append("sim_tool_matrix_live_mutation")
    if bundle.sim_tool_matrix.analytics_tool_receipt_count != len(ANALYTICS_TOOL_IDS):
        errors.append("analytics_sim_tool_receipt_count_mismatch")
    if bundle.sim_tool_matrix.analytics_case_contract_sha256 != analytics_case_contract_sha256():
        errors.append("analytics_sim_case_contract_mismatch")
    if bundle.sim_tool_matrix.analytics_case_receipts != bundle.analytics_tool_case_receipts:
        errors.append("analytics_sim_case_receipt_binding_mismatch")
    if bundle.sim_tool_matrix.cleanup_complete is not True:
        errors.append("sim_tool_matrix_cleanup_incomplete")
    if bundle.sim_tool_matrix.account_state_unchanged is not True:
        errors.append("sim_tool_matrix_account_state_changed")
    if bundle.sim_tool_matrix.redacted_publication is not True:
        errors.append("sim_tool_matrix_publication_not_redacted")
    errors.extend(analytics_case_evidence_errors(bundle.analytics_tool_case_receipts))
    if {receipt.tool for receipt in bundle.sim_tool_matrix.tool_receipts} != set(
        selected_catalog.tool_ids,
    ):
        errors.append("sim_tool_receipt_coverage_mismatch")
    skill_tools = tuple(receipt.tool_id for receipt in bundle.skill_scenario_receipts)
    if skill_tools != selected_catalog.skill_scenario_tools:
        errors.append("skill_scenario_receipt_coverage_mismatch")
    if any(receipt.evaluation_state != "passed" for receipt in bundle.skill_scenario_receipts):
        errors.append("skill_scenario_not_passed")
    if bundle.controlled_sim_lifecycle.evidence_state == "refused":
        errors.append("controlled_sim_lifecycle_refused")
    return tuple(errors)


def load_analysis_kind_catalog(path: Path | None = None) -> AnalysisKindCatalog:
    """Load the generated inventory and fail if it diverges from current frozen catalogs."""
    selected = path or ANALYSIS_KIND_CATALOG_PATH
    try:
        catalog = AnalysisKindCatalog.model_validate_json(
            selected.read_text(encoding="utf-8"),
            strict=True,
        )
    except (OSError, ValidationError) as error:
        raise EvidenceCoverageError("analysis-kind evidence catalog is invalid") from error
    errors = catalog_coverage_errors(catalog)
    if errors:
        raise EvidenceCoverageError(",".join(errors))
    return catalog


def catalog_coverage_errors(catalog: AnalysisKindCatalog) -> tuple[str, ...]:
    """Compare every inventory against its canonical source without accepting subsets."""
    definitions = load_metric_definition_catalog()
    profiles = load_proof_profile_catalog(definitions=definitions)
    sources = load_source_contract_catalog()
    scenario_manifest = (
        SCENARIO_MANIFEST
        if SCENARIO_MANIFEST.is_file()
        else Path("data/saxo/agent_tool_scenarios.json")
    )
    try:
        scenarios = ScenarioManifest.model_validate_json(
            scenario_manifest.read_text(encoding="utf-8"),
        )
    except (OSError, ValidationError):
        return ("skill_scenario_manifest_invalid",)
    expected_templates = tuple(binding.template_id for binding in core_template_bindings())
    expected_profile_ids = tuple(profile.proof_profile_id for profile in profiles.profiles)
    expected_receipts = tuple(f"ae_{kind}_v1" for kind in profiles.production_analysis_kinds)
    comparisons = (
        (
            catalog.source_contract_catalog_sha256,
            source_contract_catalog_sha256(),
            "source_contract_catalog_fingerprint_mismatch",
        ),
        (
            catalog.metric_definition_catalog_sha256,
            definitions.fingerprint_sha256,
            "metric_definition_catalog_fingerprint_mismatch",
        ),
        (
            catalog.source_contract_ids,
            tuple(contract.contract_id for contract in sources.contracts),
            "source_contract_coverage_mismatch",
        ),
        (
            catalog.metric_definition_ids,
            definitions.production_metric_ids,
            "metric_definition_coverage_mismatch",
        ),
        (
            catalog.proof_profile_ids,
            expected_profile_ids,
            "proof_profile_coverage_mismatch",
        ),
        (
            catalog.analysis_kinds,
            profiles.production_analysis_kinds,
            "analysis_kind_coverage_mismatch",
        ),
        (
            frozenset(catalog.tool_ids),
            ALL_LOGICAL_TOOL_IDS,
            "tool_coverage_mismatch",
        ),
        (
            catalog.artifact_template_ids,
            expected_templates,
            "artifact_template_coverage_mismatch",
        ),
        (
            catalog.skill_scenario_tools,
            tuple(scenario.tool for scenario in scenarios.scenarios),
            "skill_scenario_coverage_mismatch",
        ),
        (
            catalog.evidence_receipt_ids,
            expected_receipts,
            "evidence_receipt_coverage_mismatch",
        ),
    )
    return tuple(error for actual, expected, error in comparisons if actual != expected)


def build_proof_execution_contracts(
    *,
    catalog: AnalysisKindCatalog | None = None,
    definitions: MetricDefinitionCatalog | None = None,
    profiles: ProofProfileCatalog | None = None,
) -> tuple[AnalysisProofExecutionContract, ...]:
    """Build executable requirements; this does not fabricate passing evidence."""
    selected_catalog = catalog or load_analysis_kind_catalog()
    selected_definitions = definitions or load_metric_definition_catalog()
    selected_profiles = profiles or load_proof_profile_catalog(definitions=selected_definitions)
    definitions_by_id = selected_definitions.by_id()
    profiles_by_kind = {profile.analysis_kind: profile for profile in selected_profiles.profiles}
    contracts: list[AnalysisProofExecutionContract] = []
    for analysis_kind, tool_id, profile_id, evidence_id in zip(
        selected_catalog.analysis_kinds,
        selected_catalog.analysis_tool_ids,
        selected_catalog.proof_profile_ids,
        selected_catalog.evidence_receipt_ids,
        strict=True,
    ):
        profile = profiles_by_kind[analysis_kind]
        metric_ids = tuple(binding.metric_id for binding in profile.metric_definitions)
        source_ids = tuple(binding.contract_id for binding in profile.source_contracts)
        artifacts = profile.artifact_template_ids
        tolerances = tuple(
            _tolerance_contract(
                definitions_by_id[metric_id].tolerance,
                metric_id,
                definitions_by_id[metric_id].definition_version,
            )
            for metric_id in metric_ids
        )
        cases = tuple(
            _proof_case(
                kind,
                analysis_kind=analysis_kind,
                has_metrics=bool(metric_ids),
                has_sources=bool(source_ids),
                has_artifacts=bool(artifacts),
            )
            for kind in PROOF_EXECUTION_KINDS
        )
        contracts.append(
            AnalysisProofExecutionContract(
                analysis_kind=analysis_kind,
                tool_id=tool_id,
                proof_profile_id=profile_id,
                proof_profile_version=profile.profile_version,
                proof_activation_state=profile.activation_state.value,
                quarantine_reason=profile.quarantine_reason,
                output_schema_version=profile.schema_version,
                evidence_receipt_id=evidence_id,
                definition_catalog_sha256=profile.definition_catalog_sha256,
                source_catalog_sha256=profile.source_catalog_sha256,
                source_revision=profile.source_revision,
                engine_bindings=profile.engines,
                source_bindings=profile.source_contracts,
                metric_ids=metric_ids,
                source_contract_ids=source_ids,
                artifact_template_ids=artifacts,
                metric_tolerances=tolerances,
                cases=cases,
            ),
        )
    return tuple(contracts)


def _tolerance_contract(
    tolerance: NumericTolerance,
    metric_id: str,
    definition_version: str,
) -> MetricToleranceContract:
    material = json.dumps(
        tolerance.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return MetricToleranceContract(
        metric_id=metric_id,
        definition_version=definition_version,
        mode=tolerance.mode,
        tolerance_sha256=hashlib.sha256(material.encode()).hexdigest(),
    )


def _proof_case(
    kind: ProofExecutionKind,
    *,
    analysis_kind: str,
    has_metrics: bool,
    has_sources: bool,
    has_artifacts: bool,
) -> ProofCaseContract:
    applicability = "required"
    metric_not_applicable = (
        kind
        in {
            "numerical_tolerance",
            "accounting_identity",
            "saxo_reconciliation",
        }
        and not has_metrics
    )
    artifact_not_applicable = kind in {"artifact_parity", "visual_integrity"} and not has_artifacts
    unsupported_offline_case = (
        kind in _OFFLINE_PROOF_KINDS and (analysis_kind, kind) not in EXACT_OFFLINE_PROOF_SUPPORT
    )
    if metric_not_applicable or artifact_not_applicable or unsupported_offline_case:
        applicability = "not_applicable"
    requirement_codes: dict[ProofExecutionKind, str] = {
        "source_contract": (
            "exact_source_contract_binding" if has_sources else "derived_source_lineage_binding"
        ),
        "known_answer": "known_answer_exact",
        "property": "seeded_property_invariants",
        "metamorphic": "named_metamorphic_relations",
        "independent_reference": "independent_reference_path",
        "mutation_kill": "representative_mutation_kills",
        "numerical_tolerance": "metric_specific_tolerance",
        "accounting_identity": "named_accounting_identity",
        "saxo_reconciliation": "named_saxo_reconciliation",
        "executable_sim": "actual_mcp_sim_execution",
        "artifact_parity": "structured_artifact_value_parity",
        "visual_integrity": "headless_visual_integrity",
        "schema_drift": "schema_drift_fail_closed",
        "agent_use": "matched_skill_scenario",
        "privacy_safety": "redacted_value_free_publication",
    }
    return ProofCaseContract(
        kind=kind,
        applicability=applicability,
        requirement_code=requirement_codes[kind],
        minimum_case_count=1 if applicability == "required" else 0,
        independent_path_required=kind == "independent_reference",
        required_environment="SIM" if kind == "executable_sim" else None,
        required_recovery="quarantine_or_refusal" if kind == "schema_drift" else None,
    )


def validate_analysis_evidence(
    receipts: Sequence[AnalysisEvidenceReceipt],
    *,
    contracts: Sequence[AnalysisProofExecutionContract] | None = None,
) -> tuple[str, ...]:
    """Validate exact receipt coverage and applicability for a frozen final run."""
    expected_contracts = tuple(contracts or build_proof_execution_contracts())
    receipt_ids = tuple(receipt.evidence_receipt_id for receipt in receipts)
    expected_ids = tuple(contract.evidence_receipt_id for contract in expected_contracts)
    errors: list[str] = []
    if len(receipt_ids) != len(set(receipt_ids)):
        errors.append("analysis_evidence_receipts_not_unique")
    if receipt_ids != expected_ids:
        errors.append("analysis_evidence_receipt_coverage_mismatch")
        return tuple(errors)
    by_id = {receipt.evidence_receipt_id: receipt for receipt in receipts}
    for contract in expected_contracts:
        receipt = by_id[contract.evidence_receipt_id]
        if (
            receipt.analysis_kind != contract.analysis_kind
            or receipt.proof_profile_id != contract.proof_profile_id
        ):
            errors.append(f"analysis_evidence_binding_mismatch:{contract.analysis_kind}")
            continue
        for case, observed in zip(contract.cases, receipt.checks, strict=True):
            if case.applicability == "not_applicable" and observed.state != "not_applicable":
                errors.append(f"not_applicable_proof_claimed:{contract.analysis_kind}:{case.kind}")
            if case.applicability == "required" and observed.state == "not_applicable":
                errors.append(f"required_proof_missing:{contract.analysis_kind}:{case.kind}")
    return tuple(errors)
