from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_chart_semantics import core_template_bindings
from saxo_bank_mcp.analytics_metric_definitions import load_metric_definition_catalog
from saxo_bank_mcp.analytics_proof_profiles import load_proof_profile_catalog
from saxo_bank_mcp.analytics_source_contracts import load_source_contract_catalog
from saxo_bank_mcp.qa_analytics_artifacts import (
    ArtifactParityReceipt,
    ArtifactVisualIntegrityReceipt,
)
from saxo_bank_mcp.qa_analytics_evidence import (
    PROOF_EXECUTION_KINDS,
    AnalysisEvidenceReceipt,
    AnalyticsProofMatrixBundle,
    EvidenceCoverageError,
    ProofCaseReceipt,
    ProofExecutionKind,
    SkillScenarioEvidenceReceipt,
    build_proof_execution_contracts,
    catalog_coverage_errors,
    load_analysis_kind_catalog,
    validate_analysis_evidence,
    validate_proof_matrix_bundle,
)
from saxo_bank_mcp.qa_analytics_sim import (
    BROKERAGE_STATE_COMPONENTS,
    CONTROLLED_SIM_CASES,
    AnalyticsCaseKind,
    AnalyticsCaseReceipt,
    AnalyticsCaseState,
    AnalyticsToolCaseEvidence,
    BrokerageStateComponent,
    BrokerageStateFingerprint,
    ControlledSimCaseReceipt,
    ControlledSimLifecycleReceipt,
    PostSendTimeoutReceipt,
    analytics_case_contract_sha256,
    analytics_sim_contracts,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import receipt_for
from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS, ANALYTICS_TOOL_IDS

EXPECTED_SOURCE_CONTRACT_COUNT: Final = 18
EXPECTED_METRIC_COUNT: Final = 206
EXPECTED_ANALYSIS_KIND_COUNT: Final = 54
EXPECTED_TOOL_COUNT: Final = 60
EXPECTED_ARTIFACT_TEMPLATE_COUNT: Final = 20


def _case_receipt(kind: ProofExecutionKind, *, applicable: bool = True) -> ProofCaseReceipt:
    if not applicable:
        return ProofCaseReceipt(
            kind=kind,
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
    comparison_kinds = {
        "known_answer",
        "numerical_tolerance",
        "accounting_identity",
        "saxo_reconciliation",
        "artifact_parity",
    }
    return ProofCaseReceipt(
        kind=kind,
        state="passed",
        reason_code="passed",
        evidence_sha256="c" * 64,
        executed_case_count=1,
        failed_case_count=0,
        comparison_count=int(kind in comparison_kinds),
        unexplained_difference_count=0,
        mutation_count=int(kind == "mutation_kill"),
        mutation_killed_count=int(kind == "mutation_kill"),
        environment="SIM" if kind == "executable_sim" else None,
        recovery_observed=kind == "schema_drift",
        publication_scan_passed=kind == "privacy_safety",
    )


def _analytics_case_receipt(kind: AnalyticsCaseKind) -> AnalyticsCaseReceipt:
    states: dict[AnalyticsCaseKind, AnalyticsCaseState] = {
        "success": "passed",
        "degradation": "degraded",
        "refusal": "refused",
        "privacy": "passed",
        "timeout": "timed_out",
        "recovery": "reconciled",
    }
    return AnalyticsCaseReceipt(
        kind=kind,
        state=states[kind],
        reason_code=f"{kind}_observed",
        network_call_made=False,
        broker_write_made=False,
        private_values_published=False,
        evidence_sha256="8" * 64,
    )


def test_generated_catalog_covers_every_frozen_inventory_exactly_once() -> None:
    definitions = load_metric_definition_catalog()
    proofs = load_proof_profile_catalog(definitions=definitions)
    sources = load_source_contract_catalog()
    catalog = load_analysis_kind_catalog()

    assert catalog_coverage_errors(catalog) == ()
    assert (
        len(catalog.source_contract_ids)
        == len(set(catalog.source_contract_ids))
        == EXPECTED_SOURCE_CONTRACT_COUNT
    )
    assert set(catalog.source_contract_ids) == {
        contract.contract_id for contract in sources.contracts
    }
    assert (
        len(catalog.metric_definition_ids)
        == len(set(catalog.metric_definition_ids))
        == EXPECTED_METRIC_COUNT
    )
    assert set(catalog.metric_definition_ids) == set(definitions.production_metric_ids)
    assert (
        len(catalog.analysis_kinds)
        == len(set(catalog.analysis_kinds))
        == EXPECTED_ANALYSIS_KIND_COUNT
    )
    assert set(catalog.analysis_kinds) == set(proofs.production_analysis_kinds)
    assert len(catalog.analysis_tool_ids) == len(catalog.analysis_kinds)
    assert (
        len(catalog.proof_profile_ids)
        == len(set(catalog.proof_profile_ids))
        == EXPECTED_ANALYSIS_KIND_COUNT
    )
    assert set(catalog.proof_profile_ids) == {
        profile.proof_profile_id for profile in proofs.profiles
    }
    assert len(catalog.tool_ids) == len(set(catalog.tool_ids)) == EXPECTED_TOOL_COUNT
    assert set(catalog.tool_ids) == ALL_LOGICAL_TOOL_IDS
    expected_templates = {binding.template_id for binding in core_template_bindings()}
    assert (
        len(catalog.artifact_template_ids)
        == len(set(catalog.artifact_template_ids))
        == EXPECTED_ARTIFACT_TEMPLATE_COUNT
    )
    assert set(catalog.artifact_template_ids) == expected_templates
    assert (
        len(catalog.skill_scenario_tools)
        == len(set(catalog.skill_scenario_tools))
        == EXPECTED_TOOL_COUNT
    )
    assert set(catalog.skill_scenario_tools) == ALL_LOGICAL_TOOL_IDS
    assert (
        len(catalog.evidence_receipt_ids)
        == len(set(catalog.evidence_receipt_ids))
        == EXPECTED_ANALYSIS_KIND_COUNT
    )


def test_catalog_loader_rejects_duplicate_or_missing_coverage(tmp_path: Path) -> None:
    payload = load_analysis_kind_catalog().model_dump(mode="json")
    payload["tool_ids"] = [*payload["tool_ids"], payload["tool_ids"][0]]
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((ValidationError, EvidenceCoverageError)):
        load_analysis_kind_catalog(duplicate)


def test_every_analysis_kind_has_one_complete_proof_execution_contract() -> None:
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)

    assert len(contracts) == len(catalog.analysis_kinds) == EXPECTED_ANALYSIS_KIND_COUNT
    assert {contract.analysis_kind for contract in contracts} == set(catalog.analysis_kinds)
    assert {contract.evidence_receipt_id for contract in contracts} == set(
        catalog.evidence_receipt_ids
    )
    for contract in contracts:
        assert contract.tool_id in catalog.tool_ids
        assert contract.output_schema_version == "1"
        assert contract.definition_catalog_sha256 == catalog.metric_definition_catalog_sha256
        assert contract.source_catalog_sha256 == catalog.source_contract_catalog_sha256
        assert tuple(binding.contract_id for binding in contract.source_bindings) == (
            contract.source_contract_ids
        )
        assert contract.engine_bindings
        assert tuple(case.kind for case in contract.cases) == PROOF_EXECUTION_KINDS
        assert len(contract.metric_tolerances) == len(set(contract.metric_ids))
        assert {item.metric_id for item in contract.metric_tolerances} == set(contract.metric_ids)
        assert all(item.mode != "global" for item in contract.metric_tolerances)


def test_proof_contracts_name_independent_reference_and_mutation_requirements() -> None:
    contracts = build_proof_execution_contracts()

    for contract in contracts:
        by_kind = {case.kind: case for case in contract.cases}
        assert by_kind["known_answer"].requirement_code == "known_answer_exact"
        assert by_kind["property"].requirement_code == "seeded_property_invariants"
        assert by_kind["metamorphic"].requirement_code == "named_metamorphic_relations"
        assert by_kind["independent_reference"].independent_path_required is True
        assert by_kind["mutation_kill"].minimum_case_count >= 1
        assert by_kind["schema_drift"].required_recovery == "quarantine_or_refusal"
        assert by_kind["executable_sim"].required_environment == "SIM"


def test_artifact_parity_rejects_renderer_recalculation_and_visual_failure() -> None:
    digest = "a" * 64
    with pytest.raises(ValidationError, match="fingerprint"):
        ArtifactParityReceipt(
            template_id="relative_performance",
            analysis_kind="market_comparison",
            structured_semantics_sha256=digest,
            artifact_semantics_sha256="b" * 64,
            structured_value_count=3,
            rendered_value_count=3,
            sampled_point_count=3,
            state="passed",
        )
    with pytest.raises(ValidationError, match="visual"):
        ArtifactVisualIntegrityReceipt(
            template_id="relative_performance",
            formats=("png", "html"),
            desktop_width=1280,
            mobile_width=375,
            png_pixel_check_passed=True,
            text_clipping_detected=True,
            label_overlap_detected=False,
            html_mobile_readable=True,
            privacy_footer_present=True,
            provenance_stamp_present=True,
            state="passed",
        )


def test_publishable_analysis_evidence_requires_every_case_exactly_once() -> None:
    contract = build_proof_execution_contracts()[0]
    checks = tuple(
        _case_receipt(case.kind, applicable=case.applicability == "required")
        for case in contract.cases
    )
    receipt = AnalysisEvidenceReceipt(
        evidence_receipt_id=contract.evidence_receipt_id,
        candidate_commit="d" * 40,
        analysis_kind=contract.analysis_kind,
        proof_profile_id=contract.proof_profile_id,
        state="passed",
        checks=checks,
        redacted_publication=True,
        private_values_published=False,
        broker_write_made=False,
        live_mutation_calls=0,
    )

    assert validate_analysis_evidence((receipt,), contracts=(contract,)) == ()
    with pytest.raises(ValidationError, match="proof checks"):
        AnalysisEvidenceReceipt.model_validate(
            {**receipt.model_dump(mode="json"), "checks": (*checks, checks[0])},
        )


def test_failed_or_incomplete_proof_cannot_be_published_as_passed() -> None:
    contract = build_proof_execution_contracts()[0]
    checks = [
        _case_receipt(case.kind, applicable=case.applicability == "required")
        for case in contract.cases
    ]
    known_index = next(index for index, item in enumerate(checks) if item.kind == "known_answer")
    checks[known_index] = checks[known_index].model_copy(
        update={
            "state": "failed",
            "reason_code": "known_answer_mismatch",
            "failed_case_count": 1,
        },
    )

    with pytest.raises(ValidationError, match="passed evidence"):
        AnalysisEvidenceReceipt(
            evidence_receipt_id=contract.evidence_receipt_id,
            candidate_commit="f" * 40,
            analysis_kind=contract.analysis_kind,
            proof_profile_id=contract.proof_profile_id,
            state="passed",
            checks=tuple(checks),
            redacted_publication=True,
            private_values_published=False,
            broker_write_made=False,
            live_mutation_calls=0,
        )


def test_complete_cross_layer_bundle_validates_every_receipt_once() -> None:
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    candidate = "1" * 40
    analysis_receipts = tuple(
        AnalysisEvidenceReceipt(
            evidence_receipt_id=contract.evidence_receipt_id,
            candidate_commit=candidate,
            analysis_kind=contract.analysis_kind,
            proof_profile_id=contract.proof_profile_id,
            state="passed",
            checks=tuple(
                _case_receipt(case.kind, applicable=case.applicability == "required")
                for case in contract.cases
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
    parity = tuple(
        ArtifactParityReceipt(
            template_id=template_id,
            analysis_kind=owner_by_template[template_id],
            structured_semantics_sha256="2" * 64,
            artifact_semantics_sha256="2" * 64,
            structured_value_count=1,
            rendered_value_count=1,
            sampled_point_count=1,
            state="passed",
        )
        for template_id in catalog.artifact_template_ids
    )
    visual = tuple(
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
        for template_id in catalog.artifact_template_ids
    )
    matrix_receipts = tuple(
        receipt_for(tool, {"status": "completed"}, {}) for tool in sorted(catalog.tool_ids)
    )
    state_payload: dict[str, JsonValue] = {
        "state": {"count": 0, "fingerprint": "3" * 64},
    }
    matrix = SimToolMatrixReceipt(
        status="passed",
        tool_receipts=matrix_receipts,
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_completed=True,
        fixture_reference_validated=True,
        account_allowlist_resolved=True,
        auth_status_completed=True,
        session_capabilities_completed=True,
        before_state_fingerprint=state_payload,
        after_state_fingerprint=state_payload,
        uncleaned_resources=0,
        hosts=("gateway.saxobank.com",),
        live_events=0,
        live_mutation_calls=0,
        analytics_tool_receipt_count=len(ANALYTICS_TOOL_IDS),
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        cleanup_complete=True,
        account_state_unchanged=True,
        redacted_publication=True,
        errors=(),
    )
    brokerage_state = BrokerageStateFingerprint(
        components=tuple(
            BrokerageStateComponent(name=name, count=0, fingerprint_sha256="4" * 64)
            for name in BROKERAGE_STATE_COMPONENTS
        ),
    )
    lifecycle = ControlledSimLifecycleReceipt(
        environment="SIM",
        cases=tuple(
            ControlledSimCaseReceipt(
                case_id=case_id,
                state="passed",
                reason_code="passed",
                source_request_count=0,
                sim_mutation_call_count=0,
                cleanup_complete=True,
                entitlement_state=(
                    "available" if case_id == "options_entitlement" else "not_applicable"
                ),
                evidence_sha256="5" * 64,
            )
            for case_id in CONTROLLED_SIM_CASES
        ),
        before=brokerage_state,
        after=brokerage_state,
        live_events=0,
        live_mutation_calls=0,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=False,
        disclaimer_response_made=False,
    )
    bundle = AnalyticsProofMatrixBundle(
        candidate_commit=candidate,
        analysis_receipts=analysis_receipts,
        artifact_parity_receipts=parity,
        artifact_visual_receipts=visual,
        sim_tool_matrix=matrix,
        analytics_tool_case_receipts=tuple(
            AnalyticsToolCaseEvidence(
                tool_id=contract.tool_id,
                cases=tuple(_analytics_case_receipt(case.kind) for case in contract.cases),
            )
            for contract in analytics_sim_contracts()
        ),
        controlled_sim_lifecycle=lifecycle,
        post_send_timeout=PostSendTimeoutReceipt(
            operation_kind="controlled_sim_fixture",
            timeout_observed=True,
            stop_new_writes=True,
            reconciliation_attempted=True,
            reconciliation_state="no_effect_observed",
            matching_effect_count=0,
            blind_retry_attempted=False,
            recovery_action="refuse_retry",
            ledger_fingerprint_sha256="6" * 64,
        ),
        skill_scenario_receipts=tuple(
            SkillScenarioEvidenceReceipt(
                tool_id=tool_id,
                evaluation_state="passed",
                evidence_sha256="7" * 64,
                verification_state_reported=True,
                warnings_preserved=True,
                unsupported_inference_made=False,
                unexpected_broker_write_made=False,
                private_values_published=False,
            )
            for tool_id in catalog.skill_scenario_tools
        ),
        privacy_scan_passed=True,
        secret_scan_passed=True,
        private_values_published=False,
    )

    assert validate_proof_matrix_bundle(bundle, catalog=catalog, contracts=contracts) == (
        "proof_profiles_not_active",
    )

    legacy_matrix_payload = matrix.model_dump(mode="json")
    for field_name in (
        "live_mutation_calls",
        "analytics_tool_receipt_count",
        "analytics_case_contract_sha256",
        "cleanup_complete",
        "account_state_unchanged",
        "redacted_publication",
    ):
        legacy_matrix_payload.pop(field_name)
    legacy_bundle = bundle.model_copy(
        update={
            "sim_tool_matrix": SimToolMatrixReceipt.model_validate(legacy_matrix_payload),
        },
    )

    assert {
        "sim_tool_matrix_live_mutation",
        "analytics_sim_tool_receipt_count_mismatch",
        "analytics_sim_case_contract_mismatch",
        "sim_tool_matrix_cleanup_incomplete",
        "sim_tool_matrix_account_state_changed",
        "sim_tool_matrix_publication_not_redacted",
    } <= set(
        validate_proof_matrix_bundle(
            legacy_bundle,
            catalog=catalog,
            contracts=contracts,
        ),
    )
