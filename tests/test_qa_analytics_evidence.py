from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from runpy import run_path
from typing import Final, Literal, NoReturn, cast

import anyio
import pytest
from pydantic import ValidationError

import saxo_bank_mcp.qa_analytics_evidence as evidence_module
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError, CommandResult
from saxo_bank_mcp.agent_skill_eval_models import EvalRunReport
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.analytics_chart_semantics import core_template_bindings
from saxo_bank_mcp.analytics_metric_definitions import load_metric_definition_catalog
from saxo_bank_mcp.analytics_proof_profiles import load_proof_profile_catalog
from saxo_bank_mcp.analytics_source_contracts import load_source_contract_catalog
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.auth_status import AuthStatusInputs, SaxoAuthStatus, build_auth_status
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettings
from saxo_bank_mcp.mcp_token_state import CachedTokenReady
from saxo_bank_mcp.process_scoped_selectors import (
    account_selector_for,
    inject_account_selectors,
)
from saxo_bank_mcp.qa_analytics_artifacts import (
    ArtifactParityReceipt,
    ArtifactVisualIntegrityReceipt,
)
from saxo_bank_mcp.qa_analytics_evidence import (
    PROOF_EXECUTION_KINDS,
    AnalysisEvidenceReceipt,
    AnalysisKindCatalog,
    AnalysisProofExecutionContract,
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
from saxo_bank_mcp.qa_analytics_proof_producer import (
    InstalledProofSuiteEvidence,
    MeasuredAnalysisProofObservation,
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
    analytics_case_calls,
    analytics_case_contract_sha256,
    analytics_sim_contracts,
)
from saxo_bank_mcp.qa_sim_tool_matrix_helpers import receipt_for
from saxo_bank_mcp.qa_sim_tool_matrix_models import FIXTURE_INSTRUMENT, SimToolMatrixReceipt
from saxo_bank_mcp.server_tool_ids import ALL_LOGICAL_TOOL_IDS, ANALYTICS_TOOL_IDS

EXPECTED_SOURCE_CONTRACT_COUNT: Final = 18
EXPECTED_METRIC_COUNT: Final = 206
EXPECTED_ANALYSIS_KIND_COUNT: Final = 54
EXPECTED_TOOL_COUNT: Final = 60
EXPECTED_ARTIFACT_TEMPLATE_COUNT: Final = 20
SHA256_HEX_LENGTH: Final = 64
FIXED_INCOME_PROPERTY_CASES: Final = 40
RETURN_PROPERTY_CASES: Final = 60
MATRIX_CHILD_TIMEOUT_SECONDS: Final = 1800
MATRIX_CHILD_FAILURE_EXIT_CODE: Final = 2


def _sim_auth_status(
    *,
    requested_environment: Literal["SIM", "LIVE"] = "SIM",
    effective_read_environment: Literal["SIM", "LIVE", "LIVE_READ_DISABLED"] = "SIM",
) -> SaxoAuthStatus:
    return build_auth_status(
        AuthStatusInputs(
            requested_environment=requested_environment,
            effective_read_environment=effective_read_environment,
            live_reads_enabled=effective_read_environment == "LIVE",
            sim_credentials_present=True,
            sim_credential_source="file",
            live_credentials_present=effective_read_environment == "LIVE",
            sim_redirect_uri_present=False,
            pending_pkce_authorization_present=True,
            token_cache_path_refused=False,
            token_cache_present=True,
            token_cache_readable=True,
            token_cache_expired=True,
            token_cache_refresh_supported=True,
            token_cache_environment=requested_environment,
        ),
    )


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


def _analytics_case_receipt(
    kind: AnalyticsCaseKind,
    result_state: str,
) -> AnalyticsCaseReceipt:
    states: dict[AnalyticsCaseKind, AnalyticsCaseState] = {
        "success": "passed",
        "degradation": "refused" if result_state == "refused" else "degraded",
        "refusal": "refused",
        "privacy": "passed",
        "timeout": "timed_out",
        "recovery": "reconciled",
    }
    return AnalyticsCaseReceipt(
        kind=kind,
        state=states[kind],
        reason_code=f"{kind}_observed",
        mcp_call_observed=True,
        result_parsed=kind != "timeout",
        result_state=result_state,
        mcp_is_error=kind in {"refusal", "privacy", "timeout"},
        network_call_made=False,
        broker_write_made=False,
        private_values_published=False,
        request_sha256="9" * 64,
        response_sha256="a" * 64,
        evidence_sha256="8" * 64,
        reconciles_request_sha256="b" * 64 if kind == "recovery" else None,
        reconciliation_observation_sha256="c" * 64 if kind == "recovery" else None,
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


def test_proof_contracts_keep_unmeasured_cases_not_applicable() -> None:
    contracts = build_proof_execution_contracts()

    for contract in contracts:
        by_kind = {case.kind: case for case in contract.cases}
        assert by_kind["known_answer"].requirement_code == "known_answer_exact"
        assert by_kind["property"].requirement_code == "seeded_property_invariants"
        assert by_kind["metamorphic"].requirement_code == "named_metamorphic_relations"
        assert by_kind["independent_reference"].independent_path_required is True
        assert by_kind["mutation_kill"].applicability == "not_applicable"
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
    supported_index = next(index for index, item in enumerate(checks) if item.state == "passed")
    checks[supported_index] = checks[supported_index].model_copy(
        update={
            "state": "failed",
            "reason_code": "observed_property_mismatch",
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


def _complete_bundle() -> tuple[
    AnalysisKindCatalog,
    tuple[AnalysisProofExecutionContract, ...],
    AnalyticsProofMatrixBundle,
]:
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
    brokerage_state = BrokerageStateFingerprint(
        components=tuple(
            BrokerageStateComponent(
                name=name,
                count=0,
                fingerprint_sha256="4" * 64,
                observed_state="available",
                mcp_tool_ids=("saxo_health",),
            )
            for name in BROKERAGE_STATE_COMPONENTS
        ),
    )
    analytics_case_receipts = tuple(
        AnalyticsToolCaseEvidence(
            tool_id=contract.tool_id,
            cases=tuple(
                _analytics_case_receipt(case.kind, case.expected_states[0])
                for case in contract.cases
            ),
        )
        for contract in analytics_sim_contracts()
    )
    analysis_calls = tuple(
        call for call in analytics_case_calls() if call.analysis_kind is not None
    )
    analysis_execution_receipts_list: list[AnalyticsCaseReceipt] = []
    for index, call in enumerate(analysis_calls):
        persisted = call.expected_analysis_outcome == "persisted"
        resolved = call.expected_analysis_outcome == "resolved"
        reduced = call.expected_analysis_outcome == "reduced"
        analysis_execution_receipts_list.append(
            AnalyticsCaseReceipt(
                kind="success",
                tool_id=call.tool_id,
                analysis_kind=call.analysis_kind,
                returned_analysis_kind=call.analysis_kind if persisted else None,
                analysis_id=f"an_{index:032x}" if persisted else None,
                expected_analysis_outcome=call.expected_analysis_outcome,
                persisted_result_authenticated=persisted,
                state=("passed" if persisted or resolved else "degraded" if reduced else "refused"),
                reason_code="success_observed",
                mcp_call_observed=True,
                result_parsed=True,
                result_state=(
                    "verified"
                    if persisted
                    else "resolved"
                    if resolved
                    else "reduced"
                    if reduced
                    else "refused"
                ),
                mcp_is_error=not (persisted or resolved),
                network_call_made=False,
                broker_write_made=False,
                private_values_published=False,
                request_sha256="9" * 64,
                response_sha256="a" * 64,
                evidence_sha256="8" * 64,
            ),
        )
    analysis_execution_receipts = tuple(analysis_execution_receipts_list)
    brokerage_after = BrokerageStateFingerprint(
        components=tuple(
            component.model_copy(
                update=(
                    {"fingerprint_sha256": "6" * 64}
                    if component.name == "balances"
                    else {
                        "count": component.count + 2,
                        "fingerprint_sha256": "7" * 64,
                    }
                    if component.name == "trade_messages"
                    else {}
                ),
            )
            for component in brokerage_state.components
        ),
    )
    lifecycle = ControlledSimLifecycleReceipt(
        environment="SIM",
        cases=tuple(
            ControlledSimCaseReceipt(
                case_id=case_id,
                state="passed",
                reason_code="passed",
                source_request_count=0 if case_id == "cleanup" else 1,
                mcp_call_count=1,
                sim_mutation_call_count=(2 if case_id in {"ghost_portfolio", "cleanup"} else 0),
                cleanup_complete=True,
                entitlement_state=(
                    "available" if case_id == "options_entitlement" else "not_applicable"
                ),
                evidence_sha256="5" * 64,
            )
            for case_id in CONTROLLED_SIM_CASES
        ),
        before=brokerage_state,
        after=brokerage_after,
        live_events=0,
        live_mutation_calls=0,
        request_ledger_read_last=True,
        request_ledger_complete=True,
        request_ledger_fingerprint_sha256="9" * 64,
        cleanup_complete=True,
        unchanged_account_state=True,
        redacted_publication=True,
        private_values_published=False,
        purchase_occurred=False,
        disclaimer_response_made=False,
    )
    matrix = SimToolMatrixReceipt(
        status="passed",
        tool_receipts=matrix_receipts,
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_made=False,
        disclaimer_refusal_observed=True,
        fixture_reference_validated=True,
        account_allowlist_resolved=True,
        auth_status_completed=True,
        session_capabilities_completed=True,
        before_state_fingerprint=brokerage_state,
        after_state_fingerprint=brokerage_after,
        uncleaned_resources=0,
        hosts=("gateway.saxobank.com",),
        live_events=0,
        live_mutation_calls=0,
        analytics_tool_receipt_count=len(ANALYTICS_TOOL_IDS),
        analytics_case_contract_sha256=analytics_case_contract_sha256(),
        analytics_case_receipts=analytics_case_receipts,
        analysis_execution_receipts=analysis_execution_receipts,
        controlled_sim_lifecycle=lifecycle,
        mcp_only_account_fixture_state=True,
        cleanup_complete=True,
        account_state_unchanged=True,
        redacted_publication=True,
        errors=(),
    )
    bundle = AnalyticsProofMatrixBundle(
        candidate_commit=candidate,
        analysis_receipts=analysis_receipts,
        artifact_parity_receipts=parity,
        artifact_visual_receipts=visual,
        sim_tool_matrix=matrix,
        analytics_tool_case_receipts=analytics_case_receipts,
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

    return catalog, contracts, bundle


def test_complete_cross_layer_bundle_validates_every_receipt_once() -> None:
    catalog, contracts, bundle = _complete_bundle()

    assert validate_proof_matrix_bundle(bundle, catalog=catalog, contracts=contracts) == (
        "trusted_producer_provenance_missing",
        "proof_profiles_not_active",
    )

    missing_required = bundle.sim_tool_matrix.model_dump(mode="json")
    missing_required.pop("analytics_case_receipts")
    with pytest.raises(ValidationError):
        SimToolMatrixReceipt.model_validate(missing_required)

    active_contracts = tuple(
        contract.model_copy(
            update={
                "proof_activation_state": "active",
                "quarantine_reason": None,
            },
        )
        for contract in contracts
    )
    assert validate_proof_matrix_bundle(
        bundle,
        catalog=catalog,
        contracts=active_contracts,
    ) == ("trusted_producer_provenance_missing",)


def test_caller_written_producer_json_cannot_issue_trusted_provenance(
    tmp_path: Path,
) -> None:
    catalog, contracts, bundle = _complete_bundle()
    evidence_root = tmp_path / "producer-evidence"
    evidence_root.mkdir(mode=0o700)
    evidence_root.chmod(0o700)
    paths = tuple(evidence_root / name for name in ("command.json", "session.json", "probe.json"))
    for path in paths:
        path.write_text(json.dumps({"candidate_commit": bundle.candidate_commit}), encoding="utf-8")
        path.chmod(0o600)

    assert not hasattr(evidence_module, "authenticate_proof_producer_artifacts")
    assert (
        "trusted_provenance"
        not in inspect.signature(
            validate_proof_matrix_bundle,
        ).parameters
    )
    assert validate_proof_matrix_bundle(
        bundle=bundle,
        catalog=catalog,
        contracts=contracts,
    ) == ("trusted_producer_provenance_missing", "proof_profiles_not_active")
    assert all(path.is_file() for path in paths)


def test_proof_runner_requires_executed_production_install_not_fixture_or_paths() -> None:
    runner = (
        Path(__file__).resolve().parents[1] / "scripts/run_analytics_proof_matrix.py"
    ).read_text(encoding="utf-8")

    assert "load_verified_install_report" in runner
    assert "load_install_report_for_consumers" not in runner
    assert "--producer-command-evidence" not in runner
    assert "--producer-session-evidence" not in runner
    assert "--producer-probe-evidence" not in runner
    assert "proof_producer_execution_required" not in runner
    assert "run_verified_installed_producer" in runner


def test_verified_installed_candidate_executes_process_owned_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    install_models = import_module("saxo_bank_mcp.agent_skill_install_models")
    commit = "1" * 40
    installed_candidate = Path(__file__).resolve().parents[1]
    install = install_models.InstallEvidenceReport.model_construct(
        execution_mode="installed_verification",
        candidate_commit=commit,
        repo=installed_candidate,
        clone=install_models.CloneEvidence.model_construct(
            path=installed_candidate,
            commit=commit,
            source_repo=installed_candidate,
            clean=True,
        ),
        codex=install_models.ClientInstallEvidence.model_construct(
            cache_root=installed_candidate,
        ),
        claude=install_models.ClientInstallEvidence.model_construct(
            cache_root=installed_candidate,
        ),
        fixture_cleanup=install_models.FixtureCleanup.model_construct(
            run_root=installed_candidate,
        ),
    )
    cache_sha256 = "2" * 64

    def accept_clean_source(_repo: Path, _candidate_commit: str) -> None:
        return None

    def verified_digests(_install: object) -> tuple[str, str, str]:
        return cache_sha256, cache_sha256, cache_sha256

    def block_proof_bundle(**_kwargs: object) -> NoReturn:
        raise producer.ProofProducerError("installed_sim_requirements_unavailable")

    monkeypatch.setattr(producer, "_require_clean_source_commit", accept_clean_source)
    monkeypatch.setattr(
        producer,
        "_verified_install_digests",
        verified_digests,
    )
    monkeypatch.setattr(
        producer,
        "_execute_installed_proof_bundle",
        block_proof_bundle,
    )

    def execute_child(  # noqa: PLR0913
        cache_root: Path,
        command: tuple[str, ...],
        *,
        claude_cache_root: Path,
        source_repo: Path,
        retained_codex_home: Path,
        retained_claude_home: Path,
    ) -> CommandResult:
        assert claude_cache_root == installed_candidate
        assert source_repo == installed_candidate
        assert retained_codex_home == installed_candidate / "codex-home"
        assert retained_claude_home == installed_candidate / "home"
        produced = producer.produce_installed_result(
            candidate_commit=commit,
            installed_cache_sha256=cache_sha256,
        )
        stdout = produced.model_dump_json()
        return CommandResult(
            receipt=CommandReceipt(
                name="analytics_proof_producer",
                argv=command,
                cwd=str(cache_root.resolve()),
                pid=1,
                pgid=1,
                exit_code=0,
                stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(producer, "_execute_installed_child", execute_child)

    validated = producer.run_verified_installed_producer(
        install,
        candidate_commit=commit,
    )

    assert validated.status == "blocked"
    assert validated.producer_authenticated is True
    assert validated.execution_performed is False
    assert validated.candidate_commit == commit
    assert validated.installed_cache_sha256 == cache_sha256
    assert validated.executed_receipt_count == 0
    assert len(validated.proof_execution_sha256) == SHA256_HEX_LENGTH
    assert validated.validation_errors == ("installed_sim_requirements_unavailable",)

    producer_parameters = inspect.signature(producer.produce_installed_result).parameters
    assert "executed_bundle" not in producer_parameters
    assert "receipt_path" not in producer_parameters
    assert "evidence_path" not in producer_parameters


def test_installed_producer_privately_validates_a_complete_executed_typed_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    catalog, contracts, bundle = _complete_bundle()
    active_contracts = tuple(
        contract.model_copy(
            update={"proof_activation_state": "active", "quarantine_reason": None},
        )
        for contract in contracts
    )

    def selected_contracts(**_kwargs: object) -> tuple[AnalysisProofExecutionContract, ...]:
        return active_contracts

    def executed_bundle(**_kwargs: object) -> AnalyticsProofMatrixBundle:
        return bundle

    monkeypatch.setattr(producer, "load_analysis_kind_catalog", lambda: catalog)
    monkeypatch.setattr(
        producer,
        "build_proof_execution_contracts",
        selected_contracts,
    )
    monkeypatch.setattr(
        producer,
        "_execute_installed_proof_bundle",
        executed_bundle,
    )

    produced = producer.produce_installed_result(
        candidate_commit=bundle.candidate_commit,
        installed_cache_sha256="2" * 64,
    )

    assert produced.status == "validated"
    assert produced.process_local_activation is True
    assert produced.executed_receipt_count == len(active_contracts)
    assert produced.validation_errors == ()
    assert produced.bundle_sha256 == producer._digest(  # noqa: SLF001
        bundle.model_dump(mode="json"),
    )


def test_codex_native_cli_auth_preflight_blocks_before_proof_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    observed_phases: list[str] = []

    async def auth_status() -> SaxoAuthStatus:
        observed_phases.append("auth_status")
        return _sim_auth_status()

    async def capabilities(
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        observed_phases.append("session_capabilities")
        assert name == "saxo_get_session_capabilities"
        assert arguments == {}
        return {
            "status": "auth_required",
            "tool_name": "saxo_get_session_capabilities",
            "environment": "SIM",
            "reason": "http_error",
            "http_status": 401,
            "network_call_made": True,
        }

    def proof_bundle(**_kwargs: object) -> NoReturn:
        observed_phases.append("proof_bundle")
        raise producer.ProofProducerError("proof_work_must_not_start")

    monkeypatch.setattr(producer, "call_saxo_auth_status", auth_status, raising=False)
    monkeypatch.setattr(producer, "call_tool_payload", capabilities, raising=False)
    monkeypatch.setattr(producer, "_execute_installed_proof_bundle", proof_bundle)

    exit_code = producer.main(
        [
            "--candidate-commit",
            "1" * 40,
            "--installed-cache-sha256",
            "2" * 64,
            "--harness-policy",
            "codex_native_v1",
        ],
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert observed_phases == ["auth_status", "session_capabilities"]
    assert payload["status"] == "blocked"
    assert payload["execution_performed"] is False
    assert payload["executed_receipt_count"] == 0
    assert payload["network_call_made"] is True
    assert payload["validation_errors"] == ["proof_sim_session_preflight_failed"]
    assert payload["sim_preflight"] == {
        "capabilities_status": "auth_required",
        "effective_read_environment": "SIM",
        "http_status": 401,
        "live_reads": False,
        "live_writes": False,
        "network_call_made": True,
        "reason": "http_error",
        "redacted_publication": True,
        "requested_environment": "SIM",
        "session_capabilities_proven": False,
        "status": "blocked",
    }


def test_codex_native_preflight_refuses_live_before_capability_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    observed_phases: list[str] = []

    async def auth_status() -> SaxoAuthStatus:
        observed_phases.append("auth_status")
        return _sim_auth_status(
            requested_environment="LIVE",
            effective_read_environment="LIVE",
        )

    async def capabilities(
        _name: str,
        _arguments: dict[str, object],
    ) -> dict[str, object]:
        observed_phases.append("session_capabilities")
        return {}

    monkeypatch.setattr(producer, "call_saxo_auth_status", auth_status, raising=False)
    monkeypatch.setattr(producer, "call_tool_payload", capabilities, raising=False)

    receipt = producer._run_codex_native_sim_preflight()  # noqa: SLF001

    assert observed_phases == ["auth_status"]
    assert receipt.status == "blocked"
    assert receipt.capabilities_status == "not_called"
    assert receipt.reason == "native_preflight_environment_unsafe"
    assert receipt.network_call_made is False
    assert receipt.session_capabilities_proven is False


def test_codex_native_preflight_blocks_malformed_pass_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    async def capabilities(
        _name: str,
        _arguments: dict[str, object],
    ) -> dict[str, object]:
        return {
            "status": "passed",
            "tool_name": "saxo_get_session_capabilities",
            "environment": "SIM",
        }

    async def auth_status() -> SaxoAuthStatus:
        return _sim_auth_status()

    monkeypatch.setattr(producer, "call_saxo_auth_status", auth_status, raising=False)
    monkeypatch.setattr(producer, "call_tool_payload", capabilities, raising=False)

    receipt = producer._run_codex_native_sim_preflight()  # noqa: SLF001

    assert receipt.status == "blocked"
    assert receipt.capabilities_status == "invalid"
    assert receipt.reason == "native_preflight_capabilities_invalid"
    assert receipt.network_call_made is None
    assert receipt.session_capabilities_proven is False


def test_codex_native_later_failure_retains_passed_preflight_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    preflight = producer.CodexNativeSimPreflightReceipt(
        status="passed",
        requested_environment="SIM",
        effective_read_environment="SIM",
        capabilities_status="passed",
        reason=None,
        http_status=200,
        live_reads=False,
        live_writes=False,
        network_call_made=True,
        session_capabilities_proven=True,
    )

    def later_failure(**_kwargs: object) -> NoReturn:
        raise producer.ProofProducerError("installed_offline_proof_suite_failed")

    monkeypatch.setattr(producer, "_run_codex_native_sim_preflight", lambda: preflight)
    monkeypatch.setattr(producer, "_execute_installed_proof_bundle", later_failure)
    catalog_sha256, contract_sha256 = producer._installed_contract_digests()  # noqa: SLF001
    producer_file = producer.__file__
    assert isinstance(producer_file, str)
    progress = producer.CodexNativeProofProgress(
        candidate_commit="1" * 40,
        installed_cache_sha256="2" * 64,
        producer_module_sha256=hashlib.sha256(Path(producer_file).read_bytes()).hexdigest(),
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
    )

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_offline_proof_suite_failed",
    ):
        producer.produce_codex_native_installed_result(
            candidate_commit="1" * 40,
            installed_cache_sha256="2" * 64,
            progress=progress,
        )

    assert progress.sim_preflight == preflight
    assert progress.network_call_made is True
    assert progress.execution_performed is True
    assert progress.completed_phases == ["sim_preflight"]


def test_codex_native_producer_propagates_observed_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    catalog, contracts, bundle = _complete_bundle()
    first_tool = bundle.sim_tool_matrix.tool_receipts[0].model_copy(
        update={"network_call_made": True},
    )
    matrix = bundle.sim_tool_matrix.model_copy(
        update={
            "tool_receipts": (first_tool, *bundle.sim_tool_matrix.tool_receipts[1:]),
        },
    )
    observed_bundle = bundle.model_copy(update={"sim_tool_matrix": matrix})

    def selected_contracts(**_kwargs: object) -> tuple[AnalysisProofExecutionContract, ...]:
        return contracts

    def executed_bundle(**_kwargs: object) -> AnalyticsProofMatrixBundle:
        return observed_bundle

    monkeypatch.setattr(producer, "load_analysis_kind_catalog", lambda: catalog)
    monkeypatch.setattr(
        producer,
        "build_proof_execution_contracts",
        selected_contracts,
    )
    monkeypatch.setattr(
        producer,
        "_execute_installed_proof_bundle",
        executed_bundle,
    )
    monkeypatch.setattr(
        producer,
        "_run_codex_native_sim_preflight",
        lambda: producer.CodexNativeSimPreflightReceipt(
            status="passed",
            requested_environment="SIM",
            effective_read_environment="SIM",
            capabilities_status="passed",
            reason=None,
            http_status=None,
            live_reads=False,
            live_writes=False,
            network_call_made=True,
            session_capabilities_proven=True,
        ),
        raising=False,
    )

    result = producer.produce_codex_native_installed_result(
        candidate_commit=bundle.candidate_commit,
        installed_cache_sha256="2" * 64,
    )

    assert result.status == "validated"
    assert result.network_call_made is True


def test_private_proof_selection_accepts_an_exact_honest_nonpersisted_sim_kind() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, contracts, bundle = _complete_bundle()
    first = bundle.sim_tool_matrix.analysis_execution_receipts[0]
    refused = first.model_copy(
        update={
            "analysis_id": None,
            "expected_analysis_outcome": "refused",
            "mcp_is_error": True,
            "persisted_result_authenticated": False,
            "result_state": "refused",
            "state": "refused",
        },
    )
    matrix = bundle.sim_tool_matrix.model_copy(
        update={
            "analysis_execution_receipts": (
                refused,
                *bundle.sim_tool_matrix.analysis_execution_receipts[1:],
            ),
        },
    )
    observed = bundle.model_copy(update={"sim_tool_matrix": matrix})

    errors = producer._validate_executed_bundle(  # noqa: SLF001
        observed,
        contracts=contracts,
        candidate_commit=bundle.candidate_commit,
        authority=producer._PROCESS_AUTHORITY,  # noqa: SLF001
    )

    assert "installed_analytics_terminal_receipt_missing" not in errors
    assert "installed_analytics_success_receipt_missing" not in errors


def test_terminal_analysis_selection_rejects_self_declared_outcome_or_tool_relabeling() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    catalog, _contracts, bundle = _complete_bundle()
    receipts = bundle.sim_tool_matrix.analysis_execution_receipts
    index = next(
        position
        for position, receipt in enumerate(receipts)
        if receipt.expected_analysis_outcome == "persisted"
    )
    original = receipts[index]
    relabeled = original.model_copy(
        update={
            "analysis_id": None,
            "expected_analysis_outcome": "refused",
            "persisted_result_authenticated": False,
            "returned_analysis_kind": None,
            "state": "refused",
            "result_state": "refused",
        },
    )
    changed = (*receipts[:index], relabeled, *receipts[index + 1 :])
    matrix = bundle.sim_tool_matrix.model_copy(
        update={"analysis_execution_receipts": changed},
    )

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_analytics_terminal_receipt_contract_mismatch",
    ):
        producer._exact_terminal_analysis_receipts(  # noqa: SLF001
            matrix,
            expected_kinds=set(catalog.analysis_kinds),
        )

    wrong_tool = original.model_copy(update={"tool_id": "saxo_analyze_market"})
    changed = (*receipts[:index], wrong_tool, *receipts[index + 1 :])
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_analytics_terminal_receipt_contract_mismatch",
    ):
        producer._exact_terminal_analysis_receipts(  # noqa: SLF001
            bundle.sim_tool_matrix.model_copy(
                update={"analysis_execution_receipts": changed},
            ),
            expected_kinds=set(catalog.analysis_kinds),
        )


def test_terminal_analysis_selection_accepts_exact_server_observed_source_refusal() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    catalog, _contracts, bundle = _complete_bundle()
    receipts = bundle.sim_tool_matrix.analysis_execution_receipts
    index = next(
        position
        for position, receipt in enumerate(receipts)
        if receipt.analysis_kind == "position_sizing"
    )
    refused = receipts[index].model_copy(
        update={
            "analysis_id": None,
            "expected_analysis_outcome": "refused",
            "persisted_result_authenticated": False,
            "returned_analysis_kind": None,
            "source_precondition_refused": True,
            "source_precondition_evidence_sha256": "a" * 64,
            "state": "refused",
            "result_state": "refused",
        },
    )
    changed = (*receipts[:index], refused, *receipts[index + 1 :])

    selected = producer._exact_terminal_analysis_receipts(  # noqa: SLF001
        bundle.sim_tool_matrix.model_copy(
            update={"analysis_execution_receipts": changed},
        ),
        expected_kinds=set(catalog.analysis_kinds),
    )

    assert selected["position_sizing"] == refused


def test_installed_producer_requires_executed_nodes_for_every_proof_category() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_proof_contract_receipts_missing",
    ):
        producer._proof_suite_evidence_from_test_nodes(  # noqa: SLF001
            ("installed::aggregate_marker",),
            suite_receipt_sha256="e" * 64,
        )


def test_installed_proof_contract_receipt_modules_exist_and_cover_exact_catalog() -> None:
    """The installed suite must contain the exact nodes named by the producer."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    tests_root = Path(__file__).parent
    analysis_tests = run_path(str(tests_root / "test_analytics_proof_contracts.py"))
    artifact_tests = run_path(str(tests_root / "test_qa_analytics_artifacts.py"))
    contracts = producer.build_proof_execution_contracts()
    expected_analysis_cases = {
        (contract.analysis_kind, case.kind)
        for contract in contracts
        for case in contract.cases
        if case.applicability == "required"
        and case.kind not in {"agent_use", "executable_sim", "artifact_parity", "visual_integrity"}
    }
    assert set(analysis_tests["ANALYSIS_PROOF_CASES"]) == expected_analysis_cases
    assert set(artifact_tests["ARTIFACT_TEMPLATE_IDS"]) == set(
        producer.load_analysis_kind_catalog().artifact_template_ids,
    )
    assert callable(analysis_tests["test_analysis_proof_contract"])
    assert callable(artifact_tests["test_artifact_parity_contract"])
    assert callable(artifact_tests["test_artifact_visual_contract"])
    assert callable(analysis_tests["_execute_exact_proof_measurement"])
    assert "_DOMAIN_SUPPORT" not in analysis_tests
    assert "_CASE_SUPPORT" not in analysis_tests
    assert "test_qa_analytics_artifacts.py" in inspect.getsource(
        producer._run_installed_offline_proof_suite,  # noqa: SLF001
    )


def test_installed_proof_suite_preserves_measured_junit_properties() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    source = inspect.getsource(producer._run_installed_offline_proof_suite)  # noqa: SLF001
    assert '"junit_family=legacy"' in source


def test_installed_offline_proof_suite_roots_uv_environment_below_run_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    bound_runtime = tmp_path / "bound-proof-runtime"
    bound_runtime.mkdir(mode=0o700)

    def bound_project_environment(_installed_root: Path) -> Path:
        return bound_runtime

    monkeypatch.setattr(
        producer,
        "_native_proof_project_environment",
        bound_project_environment,
    )

    class EnvironmentObservedError(Exception):
        pass

    def observe_environment(
        _name: str,
        _command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NoReturn:
        del timeout_seconds
        run_root = Path(env["HOME"]).resolve()
        project_environment = Path(env["UV_PROJECT_ENVIRONMENT"]).resolve()
        python_install = Path(env["UV_PYTHON_INSTALL_DIR"]).resolve()
        assert cwd.resolve() == Path.cwd().resolve()
        assert project_environment == bound_runtime
        assert python_install == run_root / "uv-python"
        assert project_environment.is_dir()
        assert python_install.is_dir()
        assert project_environment.stat().st_mode & 0o077 == 0
        assert python_install.stat().st_mode & 0o077 == 0
        raise EnvironmentObservedError

    monkeypatch.setattr(producer, "run_command", observe_environment)

    with pytest.raises(EnvironmentObservedError):
        producer._run_installed_offline_proof_suite(  # noqa: SLF001
            harness_policy="codex_native_v1",
        )


def test_native_proof_project_environment_binds_exact_interpreter_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    installed_root = tmp_path / "installed-cache"
    retained_runtime = tmp_path / "retained-proof-runtime"
    installed_root.mkdir(mode=0o700)
    retained_runtime.mkdir(mode=0o700)
    monkeypatch.setattr(producer.sys, "prefix", str(retained_runtime))
    monkeypatch.setenv(
        "SAXO_ANALYTICS_PROOF_PROJECT_ENVIRONMENT",
        str(retained_runtime),
    )
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(retained_runtime))

    assert (
        producer._native_proof_project_environment(  # noqa: SLF001
            installed_root,
        )
        == retained_runtime.resolve()
    )

    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(installed_root))
    with pytest.raises(
        producer.ProofProducerError,
        match="proof_run_root_environment_invalid",
    ):
        producer._native_proof_project_environment(installed_root)  # noqa: SLF001


def test_aggregate_marker_nodes_cannot_mint_contract_keyed_proof() -> None:
    """One passing marker node per category is not evidence for every analysis."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_proof_contract_receipts_missing",
    ):
        producer._proof_suite_evidence_from_test_nodes(  # noqa: SLF001
            ("synthetic::aggregate_category",),
            suite_receipt_sha256="e" * 64,
        )


def test_contract_receipt_cannot_claim_an_unexecuted_supporting_node() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    contract = producer.build_proof_execution_contracts()[0]
    case = next(
        item
        for item in contract.cases
        if item.applicability == "required"
        and item.kind not in {"executable_sim", "artifact_parity", "visual_integrity"}
    )
    testcase = ET.Element(
        "testcase",
        {
            "classname": "tests.test_analytics_proof_contracts",
            "name": f"test_analysis_proof_contract[{contract.analysis_kind}-{case.kind}]",
        },
    )
    properties = ET.SubElement(testcase, "properties")
    ET.SubElement(
        properties,
        "property",
        {
            "name": "saxo_analytics_proof_receipt_v1",
            "value": json.dumps(
                {
                    "analysis_kind": contract.analysis_kind,
                    "case_kind": case.kind,
                    "comparison_count": int(case.kind == "known_answer"),
                    "executed_case_count": 1,
                    "failed_case_count": 0,
                    "independent_path_observed": False,
                    "mutation_count": 0,
                    "mutation_killed_count": 0,
                    "publication_scan_passed": False,
                    "receipt_kind": "analysis_case",
                    "recovery_observed": False,
                    "requirement_code": case.requirement_code,
                    "supporting_test_node_ids": ("tests.test_missing::test_never_executed",),
                    "unexplained_difference_count": 0,
                },
            ),
        },
    )

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_proof_contract_receipt_invalid",
    ):
        producer._proof_suite_evidence_from_junit(  # noqa: SLF001
            (testcase,),
            executed_test_count=1,
            suite_receipt_sha256="e" * 64,
        )


def test_legacy_declared_support_and_flags_are_not_a_measured_proof_receipt() -> None:
    """A receipt cannot substitute node names and self-declared outcome flags for execution."""
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")

    with pytest.raises(ValidationError):
        producer._AnalysisProofProperty.model_validate(  # noqa: SLF001
            {
                "analysis_kind": "fixed_income",
                "case_kind": "mutation_kill",
                "requirement_code": "representative_mutation_kills",
                "supporting_test_node_ids": (
                    "tests.test_analytics_fixed_income::test_golden_par_bond_yield_duration_convexity_carry_and_roll_down",
                ),
                "executed_case_count": 1,
                "failed_case_count": 0,
                "comparison_count": 0,
                "unexplained_difference_count": 0,
                "mutation_count": 1,
                "mutation_killed_count": 1,
                "independent_path_observed": False,
                "recovery_observed": False,
                "publication_scan_passed": False,
            },
        )


def test_contract_emitter_runs_measurements_instead_of_listing_cartesian_nodes() -> None:
    analysis_tests = import_module("test_analytics_proof_contracts")
    source = inspect.getsource(analysis_tests.test_analysis_proof_contract)

    assert "_execute_exact_proof_measurement" in source
    assert "supporting_test_node_ids" not in source
    assert not hasattr(analysis_tests, "_DOMAIN_SUPPORT")
    assert not hasattr(analysis_tests, "_CASE_SUPPORT")
    assert not hasattr(analysis_tests, "_proof_case_measurement_target")


def test_fixed_income_golden_and_property_measurements_keep_distinct_semantics() -> None:
    analysis_tests = import_module("test_analytics_proof_contracts")

    golden = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "fixed_income",
        "known_answer",
    )
    invariant = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "fixed_income",
        "property",
    )

    assert golden.operation_kind == "known_answer_comparison"
    assert golden.executed_test_node_id.endswith(
        "::test_golden_par_bond_yield_duration_convexity_carry_and_roll_down",
    )
    assert golden.executed_case_count == golden.comparison_count == 1
    assert golden.independent_path_observed is False
    assert invariant.operation_kind == "property_assertion"
    assert invariant.executed_test_node_id.endswith(
        "::test_property_positive_cash_flow_price_falls_as_yield_rises",
    )
    assert invariant.executed_case_count == FIXED_INCOME_PROPERTY_CASES
    assert invariant.observed_result_count == FIXED_INCOME_PROPERTY_CASES * 2
    assert invariant.comparison_count == 0


def test_minimum_variance_measurements_prove_known_answer_and_reference_separately() -> None:
    analysis_tests = import_module("test_analytics_proof_contracts")

    known = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "portfolio_minimum_variance",
        "known_answer",
    )
    reference = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "portfolio_minimum_variance",
        "independent_reference",
    )

    assert known.executed_test_node_id == reference.executed_test_node_id
    assert known.operation_kind == "known_answer_comparison"
    assert known.comparison_count == 1
    assert known.independent_path_observed is False
    assert reference.operation_kind == "independent_reference_comparison"
    assert reference.comparison_count == 1
    assert reference.independent_path_observed is True


def test_accounting_identity_and_seeded_property_use_observed_case_counts() -> None:
    analysis_tests = import_module("test_analytics_proof_contracts")

    identity = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "cash_and_settlement",
        "accounting_identity",
    )
    invariant = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "instrument_price_return",
        "property",
    )

    assert identity.operation_kind == "accounting_identity_comparison"
    assert identity.executed_case_count == identity.comparison_count == 1
    assert invariant.operation_kind == "property_assertion"
    assert invariant.executed_case_count == RETURN_PROPERTY_CASES
    assert invariant.observed_result_count == RETURN_PROPERTY_CASES * 4
    assert invariant.comparison_count == 0


def test_unsupported_proof_cases_are_not_emitted_or_relabelled() -> None:
    analysis_tests = import_module("test_analytics_proof_contracts")

    for case_kind in ("known_answer", "property", "source_contract"):
        with pytest.raises(AssertionError, match="unsupported exact proof measurement"):
            analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
                "corporate_action_center",
                case_kind,
            )


def test_offline_proof_support_contract_is_explicit_and_not_cartesian() -> None:
    contracts = build_proof_execution_contracts()
    offline_kinds = {
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
    }
    supported = {
        (contract.analysis_kind, case.kind)
        for contract in contracts
        for case in contract.cases
        if case.kind in offline_kinds and case.applicability == "required"
    }

    assert supported == {
        ("cash_and_settlement", "accounting_identity"),
        ("fixed_income", "known_answer"),
        ("fixed_income", "property"),
        ("instrument_price_return", "independent_reference"),
        ("instrument_price_return", "property"),
        ("portfolio_minimum_variance", "independent_reference"),
        ("portfolio_minimum_variance", "known_answer"),
    }
    corporate = next(
        contract for contract in contracts if contract.analysis_kind == "corporate_action_center"
    )
    by_kind = {case.kind: case for case in corporate.cases}
    assert by_kind["property"].applicability == "not_applicable"
    assert by_kind["known_answer"].applicability == "not_applicable"
    assert all(
        case.applicability == "not_applicable"
        for case in corporate.cases
        if case.kind not in {"agent_use", "artifact_parity", "executable_sim", "visual_integrity"}
    )


def test_measurement_digest_never_hashes_test_function_source_or_return_none() -> None:
    analysis_tests = import_module("test_analytics_proof_contracts")

    source = inspect.getsource(analysis_tests._invoke_measurement)  # noqa: SLF001
    observed = analysis_tests._invoke_measurement(  # noqa: SLF001
        "test_analytics_income",
        "test_authoritative_corporate_action_claim_refuses_missing_basis_or_entitlement",
    )

    assert "inspect.getsource" not in source
    assert "function_source_sha256" not in source
    assert observed.observed_result_count == len(("missing_basis", "denied"))
    assert observed.observed_result_types == ("ResearchRefusal",)
    assert observed.observed_value_count > 0


def test_producer_consumes_measured_operation_fields_instead_of_case_name_flags() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    fields = producer._AnalysisProofProperty.model_fields  # noqa: SLF001

    assert {
        "measurement_state",
        "operation_id",
        "operation_kind",
        "executed_test_node_id",
        "observed_result_count",
        "observed_result_types",
        "observed_result_sha256",
        "observed_value_count",
        "observed_output_sha256",
        "executed_case_count",
        "failed_case_count",
        "comparison_count",
        "unexplained_difference_count",
        "mutation_count",
        "mutation_killed_count",
        "independent_path_observed",
        "recovery_observed",
        "publication_scan_passed",
    } <= set(fields)
    parser_source = inspect.getsource(producer._proof_suite_evidence_from_junit)  # noqa: SLF001
    assert "comparison_kinds" not in parser_source
    assert "int(observed.case_kind" not in parser_source


def test_typed_result_observation_cannot_be_relabelled_as_passed_proof() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    analysis_tests = import_module("test_analytics_proof_contracts")
    measurement = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
        "fixed_income",
        "known_answer",
    )
    payload = {
        name: getattr(measurement, name)
        for name in producer._AnalysisProofProperty.model_fields  # noqa: SLF001
        if name != "receipt_kind"
    }

    passed = producer._AnalysisProofProperty.model_validate(payload)  # noqa: SLF001
    assert passed.measurement_state == "passed"
    with pytest.raises(
        ValidationError,
        match="unavailable proof observation claims unmeasured semantics",
    ):
        producer._AnalysisProofProperty.model_validate(  # noqa: SLF001
            {**payload, "measurement_state": "unavailable"},
        )


def test_installed_producer_accepts_every_supported_exact_observation() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    analysis_tests = import_module("test_analytics_proof_contracts")
    observed: list[MeasuredAnalysisProofObservation] = []
    for analysis_kind, case_kind in analysis_tests.ANALYSIS_PROOF_CASES:
        measurement = analysis_tests._execute_exact_proof_measurement(  # noqa: SLF001
            analysis_kind,
            case_kind,
        )
        observed.append(
            MeasuredAnalysisProofObservation(
                **{
                    name: getattr(measurement, name)
                    for name in producer._AnalysisProofProperty.model_fields  # noqa: SLF001
                    if name != "receipt_kind"
                },
                test_node_id=producer._analysis_proof_node_id(  # noqa: SLF001
                    analysis_kind,
                    case_kind,
                ),
                evidence_sha256="e" * 64,
            ),
        )
    _catalog, _contracts, bundle = _complete_bundle()
    evidence = InstalledProofSuiteEvidence(
        analysis_cases=tuple(observed),
        artifact_parity_receipts=bundle.artifact_parity_receipts,
        artifact_visual_receipts=bundle.artifact_visual_receipts,
        executed_test_count=len(observed),
        suite_receipt_sha256="f" * 64,
    )

    producer._validate_installed_suite_coverage(evidence)  # noqa: SLF001


def _server_proof_launch_authority() -> tuple[str, ...]:
    server = import_module("saxo_bank_mcp.mcp_analytics_tools")
    forbidden_names = {
        "CommandFailureError",
        "SimToolMatrixReceipt",
        "_MATRIX_CHILD_ENV_KEYS",
        "_run_installed_matrix_proof_session",
        "run_command",
    }
    found: set[str] = set()
    seen: set[int] = set()

    def visit(value: object) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        name = getattr(value, "__name__", "")
        module_name = getattr(value, "__module__", "")
        if name in forbidden_names or module_name in {
            "saxo_bank_mcp.agent_skill_command_runner",
            "saxo_bank_mcp.qa_installed_matrix_child",
        }:
            found.add(str(name or module_name))
        if inspect.isfunction(value):
            found.update(forbidden_names & set(value.__globals__))
            for cell in value.__closure__ or ():
                visit(cell.cell_contents)
            for item in value.__defaults__ or ():
                visit(item)
            for item in (value.__kwdefaults__ or {}).values():
                visit(item)

    for module_value in vars(server).values():
        visit(module_value)
    return tuple(sorted(found))


def _installed_matrix_envelope_json(
    matrix: SimToolMatrixReceipt,
    *,
    candidate_commit: str,
    analysis_kinds: tuple[str, ...],
) -> str:
    envelope_module = import_module("saxo_bank_mcp.qa_installed_matrix_envelope")
    envelope = envelope_module.InstalledMatrixEnvelope(
        candidate_commit=candidate_commit,
        analysis_kinds=analysis_kinds,
        matrix_sha256=envelope_module.matrix_receipt_sha256(matrix),
        matrix=matrix,
    )
    return envelope.model_dump_json()


def _installed_matrix_failure_envelope_json(
    *,
    candidate_commit: str,
    analysis_kinds: tuple[str, ...],
    failure_phase: str = "matrix_execution",
    failure_category: str = "runtime_error",
    failure_detail: str | None = None,
) -> str:
    envelope_module = import_module("saxo_bank_mcp.qa_installed_matrix_envelope")
    envelope = envelope_module.build_installed_matrix_failure_envelope(
        candidate_commit=candidate_commit,
        analysis_kinds=analysis_kinds,
        failure_phase=failure_phase,
        failure_category=failure_category,
        failure_detail=failure_detail,
    )
    return envelope.model_dump_json()


def _matrix_command_result(  # noqa: PLR0913
    *,
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    stdout: str,
    stderr: str = "",
    receipt_updates: dict[str, object] | None = None,
) -> CommandResult:
    receipt = CommandReceipt(
        name=name,
        argv=argv,
        cwd=str(cwd),
        pid=321,
        pgid=321,
        exit_code=0,
        stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
        timed_out=False,
        cleanup_attempted=True,
    )
    if receipt_updates:
        receipt = receipt.model_copy(update=receipt_updates)
    return CommandResult(receipt=receipt, stdout=stdout, stderr=stderr)


def test_producer_only_launcher_accepts_exact_strict_child_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison", "bounded_backtest")
    stdout = _installed_matrix_envelope_json(
        bundle.sim_tool_matrix,
        candidate_commit=candidate,
        analysis_kinds=kinds,
    )
    observed_during: tuple[str, ...] | None = None

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        nonlocal observed_during
        observed_during = _server_proof_launch_authority()
        assert name == "analytics_installed_matrix_child"
        assert argv[1:5] == (
            "-I",
            "-B",
            "-m",
            "saxo_bank_mcp.qa_installed_matrix_child",
        )
        assert cwd == Path.cwd().resolve()
        assert env is not None
        assert env["SAXO_MCP_ENVIRONMENT"] == "SIM"
        assert env["SAXO_MCP_ENABLE_LIVE_READS"] == "0"
        assert env["SAXO_MCP_ENABLE_LIVE_WRITES"] == ""
        assert timeout_seconds == MATRIX_CHILD_TIMEOUT_SECONDS
        return _matrix_command_result(name=name, argv=argv, cwd=cwd, stdout=stdout)

    assert _server_proof_launch_authority() == ()
    assert not inspect.iscoroutinefunction(producer._run_installed_matrix_proof_session)  # noqa: SLF001
    monkeypatch.setattr(producer, "run_command", fake_run_command)
    result = producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001

    assert result == bundle.sim_tool_matrix
    assert observed_during == ()
    assert _server_proof_launch_authority() == ()


def test_producer_launcher_returns_authenticated_failed_matrix_for_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison", "bounded_backtest")
    matrix = bundle.sim_tool_matrix.model_copy(
        update={
            "status": "failed",
            "reason": "fixture_reference_invalid",
            "errors": ("fixture_reference_invalid",),
        },
    )
    stdout = _installed_matrix_envelope_json(
        matrix,
        candidate_commit=candidate,
        analysis_kinds=kinds,
    )

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        _ = env, timeout_seconds
        return _matrix_command_result(name=name, argv=argv, cwd=cwd, stdout=stdout)

    monkeypatch.setattr(producer, "run_command", fake_run_command)

    result = producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001

    assert result == matrix


@pytest.mark.parametrize(
    ("matrix_reason", "expected"),
    [
        ("fixture_reference_invalid", "installed_sim_matrix_fixture_reference_invalid"),
        (
            "tool_result_state_mismatch:saxo_analyze_market",
            "installed_sim_matrix_tool_result_state_mismatch_saxo_analyze_market",
        ),
        (
            "tool_result_state_mismatch:not_a_registered_tool",
            "installed_sim_matrix_tool_result_state_mismatch",
        ),
        (
            "tool_result_state_mismatch:saxo_analyze_market/private_detail",
            "installed_sim_matrix_tool_result_state_mismatch",
        ),
        (
            "analytics_case_failed:saxo_backtest_strategy:success",
            "installed_sim_matrix_analytics_case_failed_saxo_backtest_strategy_success",
        ),
        (
            "analytics_case_failed:not_a_registered_tool:success",
            "installed_sim_matrix_analytics_case_failed",
        ),
        (
            "analytics_case_failed:saxo_backtest_strategy:private_detail",
            "installed_sim_matrix_analytics_case_failed",
        ),
        (
            "analysis_execution_coverage_incomplete",
            "installed_sim_matrix_analysis_execution_coverage_incomplete",
        ),
        ("opaque_internal_detail", "installed_sim_matrix_failed"),
        ("", "installed_sim_matrix_failed"),
    ],
)
def test_matrix_failure_reason_is_fixed_and_privacy_safe(
    matrix_reason: str,
    expected: str,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    matrix = bundle.sim_tool_matrix.model_copy(
        update={"status": "failed", "reason": matrix_reason, "errors": (matrix_reason,)},
    )

    assert producer._matrix_failure_reason(matrix) == expected  # noqa: SLF001


def test_producer_launcher_distinguishes_matrix_child_command_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()

    def failed_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> NoReturn:
        _ = env, timeout_seconds
        raise CommandFailureError(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=321,
                pgid=321,
                exit_code=2,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "run_command", failed_run_command)

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_command_failed",
    ):
        producer._run_installed_matrix_proof_session(  # noqa: SLF001
            bundle.candidate_commit,
            ("market_comparison",),
        )


def test_installed_matrix_child_emits_strict_failure_envelope_without_raw_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    child = import_module("saxo_bank_mcp.qa_installed_matrix_child")
    assert child.__file__ is not None
    child_path = Path(child.__file__).resolve()
    candidate = "1" * 40
    kinds = ("market_comparison",)

    def fail_run(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("private child failure detail")

    monkeypatch.setattr(child.anyio, "run", fail_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(child_path),
            "--candidate",
            candidate,
            "--analysis-kind",
            kinds[0],
        ],
    )

    with pytest.raises(SystemExit) as raised:
        run_path(str(child_path), run_name="__main__")

    captured = capsys.readouterr()
    envelope_module = import_module("saxo_bank_mcp.qa_installed_matrix_envelope")
    envelope = envelope_module.InstalledMatrixFailureEnvelope.model_validate_json(
        captured.out,
        strict=True,
    )
    assert raised.value.code == MATRIX_CHILD_FAILURE_EXIT_CODE
    assert captured.err == ""
    assert envelope.candidate_commit == candidate
    assert envelope.analysis_kinds == kinds
    assert envelope.failure_phase == "matrix_execution"
    assert envelope.failure_category == "runtime_error"
    assert envelope.failure_detail == "unknown"
    assert "private" not in captured.out


def test_installed_matrix_child_classifies_pydantic_validation_model_without_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    child = import_module("saxo_bank_mcp.qa_installed_matrix_child")
    assert child.__file__ is not None
    child_path = Path(child.__file__).resolve()
    candidate = "1" * 40
    kinds = ("market_comparison",)

    def fail_run(*_args: object, **_kwargs: object) -> NoReturn:
        _catalog, _contracts, bundle = _complete_bundle()
        payload = bundle.sim_tool_matrix.model_dump(mode="json")
        payload["errors"] = ["private dynamic value"]
        child.SimToolMatrixReceipt.model_validate_json(
            json.dumps(payload),
            strict=True,
        )
        raise AssertionError("unreachable")

    monkeypatch.setattr(child.anyio, "run", fail_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(child_path),
            "--candidate",
            candidate,
            "--analysis-kind",
            kinds[0],
        ],
    )

    with pytest.raises(SystemExit) as raised:
        run_path(str(child_path), run_name="__main__")

    captured = capsys.readouterr()
    envelope_module = import_module("saxo_bank_mcp.qa_installed_matrix_envelope")
    envelope = envelope_module.InstalledMatrixFailureEnvelope.model_validate_json(
        captured.out,
        strict=True,
    )
    assert raised.value.code == MATRIX_CHILD_FAILURE_EXIT_CODE
    assert captured.err == ""
    assert envelope.failure_category == "validation_error"
    assert envelope.failure_detail == "pydantic_sim_tool_matrix_pass_incomplete"
    assert "Field required" not in captured.out
    assert "private dynamic value" not in captured.out


def test_installed_matrix_failure_detail_is_digest_bound_and_legacy_readable() -> None:
    envelope_module = import_module("saxo_bank_mcp.qa_installed_matrix_envelope")
    candidate = "1" * 40
    kinds = ("market_comparison",)
    current = envelope_module.build_installed_matrix_failure_envelope(
        candidate_commit=candidate,
        analysis_kinds=kinds,
        failure_phase="matrix_execution",
        failure_category="validation_error",
        failure_detail="pydantic_sim_tool_matrix_pass_incomplete",
    )
    tampered = current.model_dump(mode="json")
    tampered["failure_detail"] = "origin_finalize"
    with pytest.raises(ValueError, match="installed matrix failure digest mismatch"):
        envelope_module.InstalledMatrixFailureEnvelope.model_validate_json(
            json.dumps(tampered),
            strict=True,
        )

    legacy_digest = envelope_module.installed_matrix_failure_sha256(
        candidate_commit=candidate,
        analysis_kinds=kinds,
        failure_phase="matrix_execution",
        failure_category="validation_error",
        failure_detail=None,
    )
    legacy = envelope_module.InstalledMatrixFailureEnvelope.model_validate(
        {
            "schema_version": "1",
            "receipt_kind": "installed_matrix_child_failure",
            "candidate_commit": candidate,
            "analysis_kinds": kinds,
            "failure_phase": "matrix_execution",
            "failure_category": "validation_error",
            "envelope_sha256": legacy_digest,
        },
    )
    assert legacy.failure_detail is None


def test_installed_matrix_child_records_inner_lifespan_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = import_module("saxo_bank_mcp.qa_installed_matrix_child")
    phases: list[str] = []
    expected_attempts = 2
    clear_count = 0
    shutdown_count = 0

    def clear_session() -> None:
        nonlocal clear_count
        clear_count += 1

    async def fail_first_shutdown() -> None:
        nonlocal shutdown_count
        shutdown_count += 1
        if shutdown_count == 1:
            raise RuntimeError("private first-cleanup failure")

    monkeypatch.setattr(child.tools_module, "shutdown_analytics_runtime", fail_first_shutdown)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="private first-cleanup failure"):
            await child._cleanup_child_runtime(clear_session, phases.append)  # noqa: SLF001
        await child._cleanup_child_runtime(clear_session, phases.append)  # noqa: SLF001

    anyio.run(exercise)

    assert phases == ["cleanup"]
    assert clear_count == expected_attempts
    assert shutdown_count == expected_attempts


def test_installed_matrix_child_arms_exact_bound_sim_allowlists_and_restores_them(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    child = import_module("saxo_bank_mcp.qa_installed_matrix_child")
    matrix_module = import_module("saxo_bank_mcp.qa_sim_tool_matrix")
    selector_module = import_module("saxo_bank_mcp.process_scoped_selectors")
    safety_state_module = import_module("saxo_bank_mcp.safety_state")
    token_state_module = import_module("saxo_bank_mcp.mcp_token_state")
    config_module = import_module("saxo_bank_mcp.config")
    assert child.__file__ is not None
    child_path = Path(child.__file__).resolve()
    candidate = "1" * 40
    raw_account = "sim"
    expected_uic = FIXTURE_INSTRUMENT
    token = SaxoTokenSet(
        access_token="token",  # noqa: S106
        refresh_token="refresh",  # noqa: S106
        code_verifier="verifier",
        environment="SIM",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    selector = account_selector_for(token, raw_account)
    injected = inject_account_selectors(
        {
            "AccountKey": raw_account,
            "ClientKey": "client",
            "AccountId": "private-account-id",
            "Currency": "DKK",
            "AccountType": "Normal",
        },
        token,
    )
    assert isinstance(injected, dict)
    assert injected["SafeAccountSelector"] == selector
    settings = SimAuthSettings(
        app_key="test-app",
        authorization_url=SIM_ENDPOINTS.authorization_url,
        token_url=SIM_ENDPOINTS.token_url,
        rest_base_url=SIM_ENDPOINTS.rest_base_url,
        redirect_uri="",
        cache_path=tmp_path / "token-cache.json",
    )

    def resolve_settings(**_kwargs: object) -> SimAuthSettings:
        return settings

    def ready_token(*_args: object, **_kwargs: object) -> CachedTokenReady:
        return CachedTokenReady(token)

    monkeypatch.setattr(config_module, "resolve_sim_auth_settings", resolve_settings)
    monkeypatch.setattr(
        token_state_module,
        "cached_token_for_tool",
        ready_token,
    )
    monkeypatch.delenv("SAXO_MCP_ACCOUNT_ALLOWLIST", raising=False)
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", str(expected_uic))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "0")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "")
    observed_active = False
    reset_called = False

    def reset_controlled_state() -> None:
        nonlocal reset_called
        reset_called = True

    monkeypatch.setattr(safety_state_module, "reset_safety_state", reset_controlled_state)

    async def inspect_private_session(
        _fixtures: object,
        *,
        proof_recorder: object,
        matrix_server: object,
    ) -> NoReturn:
        _ = matrix_server
        nonlocal observed_active
        proof_recorder.prepare_controlled_sim_safety(  # type: ignore[attr-defined]
            selector,
            expected_uic=expected_uic,
        )
        observed_active = True
        assert os.environ["SAXO_MCP_ACCOUNT_ALLOWLIST"] == raw_account
        assert os.environ["SAXO_MCP_INSTRUMENT_ALLOWLIST"] == str(expected_uic)
        raise RuntimeError("synthetic terminal child failure")

    monkeypatch.setattr(matrix_module, "_run_matrix", inspect_private_session)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(child_path),
            "--candidate",
            candidate,
            "--analysis-kind",
            "market_comparison",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        run_path(str(child_path), run_name="__main__")

    captured = capsys.readouterr()
    assert raised.value.code == MATRIX_CHILD_FAILURE_EXIT_CODE
    assert observed_active is True, captured.out
    assert reset_called is True
    assert "SAXO_MCP_ACCOUNT_ALLOWLIST" not in os.environ
    assert os.environ["SAXO_MCP_INSTRUMENT_ALLOWLIST"] == str(expected_uic)
    assert raw_account not in captured.out
    assert raw_account not in captured.err
    selector_module.clear_process_scoped_selector_state_for_tests()


def test_producer_launcher_authenticates_typed_matrix_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison",)
    stdout = _installed_matrix_failure_envelope_json(
        candidate_commit=candidate,
        analysis_kinds=kinds,
        failure_phase="matrix_execution",
        failure_category="validation_error",
        failure_detail="pydantic_sim_tool_matrix_pass_incomplete",
    )

    def failed_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> NoReturn:
        _ = env, timeout_seconds
        raise CommandFailureError(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=321,
                pgid=321,
                exit_code=2,
                stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout=stdout,
            stderr="",
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "run_command", failed_run_command)
    with pytest.raises(
        producer.ProofProducerError,
        match=(
            "installed_matrix_child_matrix_execution_validation_error_"
            "pydantic_sim_tool_matrix_pass_incomplete"
        ),
    ):
        producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001


@pytest.mark.parametrize("mutation", ["malformed", "tampered", "misbound"])
def test_producer_launcher_refuses_untrusted_matrix_child_failure(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison",)
    payload = json.loads(
        _installed_matrix_failure_envelope_json(
            candidate_commit=candidate,
            analysis_kinds=kinds,
        ),
    )
    if mutation == "malformed":
        stdout = "{}"
    elif mutation == "misbound":
        stdout = _installed_matrix_failure_envelope_json(
            candidate_commit="2" * 40,
            analysis_kinds=kinds,
        )
    else:
        payload["failure_category"] = "io_error"
        stdout = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def failed_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> NoReturn:
        _ = env, timeout_seconds
        raise CommandFailureError(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=321,
                pgid=321,
                exit_code=2,
                stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout=stdout,
            stderr="",
            remaining_process_count=0,
            remaining_process_group_count=0,
        )

    monkeypatch.setattr(producer, "run_command", failed_run_command)
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_command_failed",
    ):
        producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001


def test_producer_launcher_prioritizes_cleanup_unknown_over_typed_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison",)
    stdout = _installed_matrix_failure_envelope_json(
        candidate_commit=candidate,
        analysis_kinds=kinds,
    )

    def failed_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> NoReturn:
        _ = env, timeout_seconds
        raise CommandFailureError(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=321,
                pgid=321,
                exit_code=2,
                stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            stdout=stdout,
            stderr="",
            remaining_process_count=None,
            remaining_process_group_count=None,
            cleanup_identity_receipt_sha256="a" * 64,
            cleanup_identity_evidence_status="observation-unknown",
            cleanup_unknown_reason="coverage_unknown",
            cleanup_coverage_stage="group_member",
            cleanup_coverage_subreason="uncaptured_member",
        )

    monkeypatch.setattr(producer, "run_command", failed_run_command)
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_cleanup_unknown",
    ):
        producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001


def test_producer_launcher_prioritizes_matrix_child_cleanup_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()

    def failed_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> NoReturn:
        _ = env, timeout_seconds
        raise CommandFailureError(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=321,
                pgid=321,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                timed_out=False,
                cleanup_attempted=True,
            ),
            remaining_process_count=None,
            remaining_process_group_count=None,
            cleanup_identity_receipt_sha256="a" * 64,
            cleanup_identity_evidence_status="observation-unknown",
            cleanup_unknown_reason="coverage_unknown",
            cleanup_coverage_stage="group_member",
            cleanup_coverage_subreason="uncaptured_member",
        )

    monkeypatch.setattr(producer, "run_command", failed_run_command)

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_cleanup_unknown",
    ):
        producer._run_installed_matrix_proof_session(  # noqa: SLF001
            bundle.candidate_commit,
            ("market_comparison",),
        )


def test_producer_launcher_distinguishes_matrix_child_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()

    def failed_run_command(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("synthetic start failure")

    monkeypatch.setattr(producer, "run_command", failed_run_command)

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_start_failed",
    ):
        producer._run_installed_matrix_proof_session(  # noqa: SLF001
            bundle.candidate_commit,
            ("market_comparison",),
        )


def test_producer_launcher_classifies_wrapped_matrix_child_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    missing_interpreter = tmp_path / "missing-python"
    monkeypatch.setattr(producer.sys, "executable", str(missing_interpreter))

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_start_failed",
    ):
        producer._run_installed_matrix_proof_session(  # noqa: SLF001
            bundle.candidate_commit,
            ("market_comparison",),
        )


def test_producer_launcher_distinguishes_invalid_matrix_child_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        _ = env, timeout_seconds
        return _matrix_command_result(name=name, argv=argv, cwd=cwd, stdout="{}")

    monkeypatch.setattr(producer, "run_command", fake_run_command)

    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_envelope_invalid",
    ):
        producer._run_installed_matrix_proof_session(  # noqa: SLF001
            bundle.candidate_commit,
            ("market_comparison",),
        )


def test_normal_child_import_exposes_no_ghost_authority_and_declares_envelope() -> None:
    child = import_module("saxo_bank_mcp.qa_installed_matrix_child")

    assert not hasattr(child, "_run_child_matrix")
    assert not hasattr(child, "_process_active_catalog")
    assert not hasattr(child, "InstalledMatrixSession")
    assert child.main.__closure__ is None
    source = inspect.getsource(child.main)
    assert "InstalledMatrixEnvelope" in source
    assert "matrix_receipt_sha256" in source


def test_process_active_profiles_bind_only_metrics_emitted_by_the_runtime_executors() -> None:
    profiles_module = import_module("saxo_bank_mcp.qa_installed_matrix_profiles")
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    expected_metrics = {
        "bounded_backtest": ("total_return",),
        "derivatives_model": ("theoretical_option_value",),
        "instrument_price_return": ("price_return",),
        "margin_fire_drill": ("combined_scenario_effect",),
        "market_comparison": ("price_return",),
        "portfolio_minimum_variance": ("minimum_variance_objective",),
        "portfolio_performance": ("time_weighted_return",),
        "portfolio_risk_parity": ("risk_parity_contribution",),
        "portfolio_scenario": ("custom_shock_effect",),
        "position_sizing": ("position_size",),
        "pretrade_impact": ("estimated_transaction_cost", "maximum_loss"),
        "scenario_combined": ("combined_scenario_effect",),
        "scenario_currency": ("currency_shock_effect",),
        "scenario_custom": ("custom_shock_effect",),
        "scenario_rate": ("rate_shock_effect",),
        "scenario_volatility": ("volatility_shock_effect",),
    }
    active = profiles_module.process_active_catalog(
        catalog,
        definitions=definitions,
        candidate_commit="1" * 40,
        allowed_kinds=frozenset(expected_metrics),
        source_revisions={kind: f"revision-{index}" for index, kind in enumerate(expected_metrics)},
    )
    by_kind = {profile.analysis_kind: profile for profile in active.profiles}

    for kind, metric_ids in expected_metrics.items():
        profile = by_kind[kind]
        assert tuple(binding.metric_id for binding in profile.metric_definitions) == metric_ids
        assert profile.activation_state.value == "active"
        assert profile.source_revision is not None


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("raw_matrix", "installed_matrix_child_envelope_invalid"),
        ("wrong_candidate", "installed_matrix_child_binding_invalid"),
        ("changed_kinds", "installed_matrix_child_binding_invalid"),
        ("reordered_kinds", "installed_matrix_child_binding_invalid"),
        ("mismatched_digest", "installed_matrix_child_envelope_invalid"),
        ("extra_field", "installed_matrix_child_envelope_invalid"),
    ],
)
def test_producer_launcher_refuses_unbound_or_fabricated_child_output(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_reason: str,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison", "bounded_backtest")
    matrix = bundle.sim_tool_matrix
    payload = json.loads(
        _installed_matrix_envelope_json(
            matrix,
            candidate_commit=candidate,
            analysis_kinds=kinds,
        ),
    )
    if mutation == "raw_matrix":
        stdout = matrix.model_dump_json()
    else:
        if mutation == "wrong_candidate":
            payload["candidate_commit"] = "2" * 40
        elif mutation == "changed_kinds":
            payload["analysis_kinds"] = ["market_comparison", "fixed_income"]
        elif mutation == "reordered_kinds":
            payload["analysis_kinds"] = list(reversed(kinds))
        elif mutation == "mismatched_digest":
            payload["matrix_sha256"] = "f" * 64
        elif mutation == "extra_field":
            payload["caller_trust"] = True
        stdout = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        _ = env, timeout_seconds
        return _matrix_command_result(name=name, argv=argv, cwd=cwd, stdout=stdout)

    monkeypatch.setattr(producer, "run_command", fake_run_command)
    with pytest.raises(producer.ProofProducerError, match=expected_reason):
        producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001


@pytest.mark.parametrize(
    ("field", "coercible_value"),
    [
        ("live_events", "0"),
        ("live_mutation_calls", "0"),
        ("uncleaned_resources", "0"),
        ("cleanup_complete", 1),
        ("account_state_unchanged", 1),
        ("redacted_publication", 1),
    ],
)
def test_producer_launcher_strictly_refuses_coercible_nested_safety_fields(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    coercible_value: object,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison",)
    payload = json.loads(
        _installed_matrix_envelope_json(
            bundle.sim_tool_matrix,
            candidate_commit=candidate,
            analysis_kinds=kinds,
        ),
    )
    payload["matrix"][field] = coercible_value
    stdout = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        _ = env, timeout_seconds
        return _matrix_command_result(name=name, argv=argv, cwd=cwd, stdout=stdout)

    monkeypatch.setattr(producer, "run_command", fake_run_command)
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_envelope_invalid",
    ):
        producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001


@pytest.mark.parametrize(
    ("receipt_updates", "stderr"),
    [
        ({"name": "wrong_command"}, ""),
        ({"argv": ("wrong",)}, ""),
        ({"cwd": "/wrong"}, ""),
        ({"pid": None}, ""),
        ({"pid": 0}, ""),
        ({"pgid": None}, ""),
        ({"pgid": 0}, ""),
        ({"exit_code": 1}, ""),
        ({"timed_out": True}, ""),
        ({"cleanup_attempted": False}, ""),
        ({"stdout_sha256": "0" * 64}, ""),
        ({"stderr_sha256": "0" * 64}, "child_error"),
    ],
)
def test_producer_launcher_refuses_command_receipt_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    receipt_updates: dict[str, object],
    stderr: str,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, _contracts, bundle = _complete_bundle()
    candidate = bundle.candidate_commit
    kinds = ("market_comparison",)
    stdout = _installed_matrix_envelope_json(
        bundle.sim_tool_matrix,
        candidate_commit=candidate,
        analysis_kinds=kinds,
    )

    def fake_run_command(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        _ = env, timeout_seconds
        return _matrix_command_result(
            name=name,
            argv=argv,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            receipt_updates=receipt_updates,
        )

    monkeypatch.setattr(producer, "run_command", fake_run_command)
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_matrix_child_command_receipt_invalid",
    ):
        producer._run_installed_matrix_proof_session(candidate, kinds)  # noqa: SLF001


def test_producer_launcher_rejects_duplicate_requested_analysis_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    called = False

    def forbidden_run(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal called
        called = True
        raise AssertionError("child launch must not occur")

    monkeypatch.setattr(producer, "run_command", forbidden_run)
    with pytest.raises(producer.ProofProducerError, match="analysis_kinds_invalid"):
        producer._run_installed_matrix_proof_session(  # noqa: SLF001
            "1" * 40,
            ("market_comparison", "market_comparison"),
        )
    assert called is False


def test_agent_evaluation_artifact_is_required_and_cannot_be_synthesized() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    assert not hasattr(producer, "_BoundAgentEvaluationArtifact")
    assert not hasattr(producer, "_load_bound_agent_evaluation_artifact")
    assert hasattr(producer, "_run_installed_agent_evaluation")
    parameters = inspect.signature(
        producer._run_installed_agent_evaluation,  # noqa: SLF001
    ).parameters
    assert "artifact_path" not in parameters
    assert "report_path" not in parameters
    assert "receipt_path" not in parameters


def _agent_evaluation_report(
    *,
    candidate_commit: str,
    include_all_tools: bool = True,
    execution_mode: Literal["manifest_validation", "model_execution"] = "model_execution",
) -> EvalRunReport:
    eval_models = import_module("saxo_bank_mcp.agent_skill_eval_models")
    tools = (
        load_analysis_kind_catalog().skill_scenario_tools
        if include_all_tools
        else load_analysis_kind_catalog().skill_scenario_tools[:-1]
    )
    records = tuple(
        eval_models.EvalRunRecord(
            case_id=f"analytics-{index}-{harness}",
            harness=harness,
            status="passed",
            execution_mode=execution_mode,
            expected_skill="saxo-analytics",
            required_logical_tools=(
                () if tool_id == "saxo_register_disclaimer_response" else (tool_id,)
            ),
            forbidden_logical_tools=(
                (tool_id,) if tool_id == "saxo_register_disclaimer_response" else ()
            ),
            resolved_tool_grants=(
                () if tool_id == "saxo_register_disclaimer_response" else (tool_id,)
            ),
            transcript_assertions_passed=True,
            no_model_call=False,
            no_mcp_call=False,
            no_saxo_call=True,
            invoked_logical_tools=(
                ("saxo_get_required_disclaimers",)
                if tool_id == "saxo_register_disclaimer_response"
                else (tool_id,)
            ),
            invoked_logical_tool_count=1,
            grant_status="passed",
            assertion_status="passed",
            model_tool_event_count=1,
            model_command_event_count=0,
            model_mcp_event_count=1,
            model_saxo_event_count=0,
        )
        for index, tool_id in enumerate(tools)
        for harness in ("codex", "claude")
    )
    return eval_models.EvalRunReport(
        status="passed",
        harness="both",
        environment="LOCAL",
        execution_mode=execution_mode,
        selected_case_count=len(records),
        case_count=len(records),
        records=records,
        cleanup={
            "complete": True,
            "remaining_processes": 0,
            "raw_transcripts_persisted": 0,
        },
        before_global_state={"state": "safe"},
        after_global_state={"state": "safe"},
        global_state_unchanged=True,
        skipped_count=0,
        nonzero_on_skip=True,
        source_commit=candidate_commit,
    )


def _codex_native_agent_evaluation_report(*, candidate_commit: str) -> EvalRunReport:
    dual = _agent_evaluation_report(candidate_commit=candidate_commit)
    records = tuple(record for record in dual.records if record.harness == "codex")
    return dual.model_copy(
        update={
            "harness": "codex",
            "selected_case_count": len(records),
            "case_count": len(records),
            "records": records,
        },
    )


def test_codex_native_agent_policy_accepts_exact_real_codex_quorum() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    report = _codex_native_agent_evaluation_report(candidate_commit="1" * 40)

    producer._require_agent_evaluation_harness_policy(  # noqa: SLF001
        report,
        harness_policy="codex_native_v1",
    )


def test_codex_native_agent_policy_rejects_non_codex_record() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    report = _agent_evaluation_report(candidate_commit="1" * 40)

    with pytest.raises(producer.ProofProducerError, match="native_agent_harness_forbidden"):
        producer._require_agent_evaluation_harness_policy(  # noqa: SLF001
            report,
            harness_policy="codex_native_v1",
        )


def test_codex_native_agent_policy_rejects_unbounded_environment() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    report = _codex_native_agent_evaluation_report(candidate_commit="1" * 40).model_copy(
        update={"environment": "ALL"},
    )

    with pytest.raises(producer.ProofProducerError, match="native_agent_environment_untrusted"):
        producer._require_agent_evaluation_harness_policy(  # noqa: SLF001
            report,
            harness_policy="codex_native_v1",
        )


def test_codex_native_agent_command_contains_no_claude_inputs(tmp_path: Path) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    command = producer._codex_native_agent_evaluation_command(  # noqa: SLF001
        candidate_commit="1" * 40,
        codex_cache_root=tmp_path / "cache",
        codex_home=tmp_path / "home",
        source_repo=tmp_path / "source",
        report_path=tmp_path / "report.json",
    )

    assert command[command.index("--harness") + 1] == "codex"
    assert command[command.index("--harness-policy") + 1] == "codex_native_v1"
    assert command[command.index("--tag") + 1] == "codex-native-proof"
    assert all("claude" not in value.lower() for value in command)


def test_codex_native_hard_suite_covers_all_tools_without_live_cases() -> None:
    eval_models = import_module("saxo_bank_mcp.agent_skill_eval_models")
    cases = eval_models.select_cases(
        eval_models.load_eval_cases(Path("evals")),
        case_id=None,
        tag="codex-native-proof",
        environment=None,
    )
    covered = {
        tool_id
        for case in cases
        for tool_id in (*case.required_logical_tools, *case.forbidden_logical_tools)
    }

    assert cases
    assert {case.environment for case in cases} <= {"LOCAL", "SIM"}
    assert covered == set(ALL_LOGICAL_TOOL_IDS)


def _fake_agent_evaluation_command(
    report: EvalRunReport,
) -> Callable[..., CommandResult]:
    def execute(
        name: str,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        del env, timeout_seconds
        report_path = Path(argv[argv.index("--out") + 1])
        payload = report.model_dump(mode="json")
        payload["run_cleanup"] = {"complete": True, "remaining_processes": 0}
        payload["installation_fixture_preserved"] = True
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        report_path.chmod(0o600)
        return CommandResult(
            receipt=CommandReceipt(
                name=name,
                argv=argv,
                cwd=str(cwd),
                pid=10,
                pgid=10,
                exit_code=0,
                stdout_sha256=hashlib.sha256(b"").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
                cleanup_attempted=True,
            ),
            stdout="",
            stderr="",
        )

    return execute


def _agent_evaluation_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    for variable, name in (
        ("SAXO_ANALYTICS_CLAUDE_CACHE_ROOT", "claude-cache"),
        ("SAXO_ANALYTICS_CODEX_HOME", "codex-home"),
        ("SAXO_ANALYTICS_CLAUDE_HOME", "claude-home"),
        ("SAXO_ANALYTICS_SOURCE_REPO", "source-repo"),
    ):
        path = tmp_path / name
        path.mkdir(mode=0o700)
        monkeypatch.setenv(variable, str(path))
    return tmp_path / "source-repo"


def test_agent_evaluation_missing_or_caller_authored_inputs_cannot_mint_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    for name in (
        "SAXO_ANALYTICS_CLAUDE_CACHE_ROOT",
        "SAXO_ANALYTICS_CODEX_HOME",
        "SAXO_ANALYTICS_CLAUDE_HOME",
        "SAXO_ANALYTICS_SOURCE_REPO",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_agent_evaluation_runtime_missing",
    ):
        producer._run_installed_agent_evaluation(  # noqa: SLF001
            candidate_commit="1" * 40,
            installed_cache_sha256="2" * 64,
        )
    copied = tmp_path / "copied.json"
    copied.write_text(
        _agent_evaluation_report(candidate_commit="1" * 40).model_dump_json(),
        encoding="utf-8",
    )
    assert copied.is_file()
    assert (
        "artifact_path"
        not in inspect.signature(
            producer._run_installed_agent_evaluation,  # noqa: SLF001
        ).parameters
    )
    assert not hasattr(producer, "_validate_installed_agent_evaluation")


def test_forged_agent_command_receipt_cannot_authenticate_a_copied_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _agent_evaluation_runtime_env(monkeypatch, tmp_path)
    report = _agent_evaluation_report(candidate_commit="1" * 40)
    execute = _fake_agent_evaluation_command(report)

    def forged(*args: object, **kwargs: object) -> CommandResult:
        result = execute(*args, **kwargs)
        return CommandResult(
            receipt=result.receipt.model_copy(update={"name": "copied_report"}),
            stdout=result.stdout,
            stderr=result.stderr,
        )

    monkeypatch.setattr(producer, "run_command", forged)
    with pytest.raises(
        producer.ProofProducerError,
        match="installed_agent_evaluation_command_untrusted",
    ):
        producer._run_installed_agent_evaluation(  # noqa: SLF001
            candidate_commit="1" * 40,
            installed_cache_sha256="2" * 64,
        )


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (
            _agent_evaluation_report(candidate_commit="3" * 40),
            "installed_agent_evaluation_candidate_mismatch",
        ),
        (
            _agent_evaluation_report(candidate_commit="1" * 40, include_all_tools=False),
            "installed_agent_evaluation_tool_coverage_missing",
        ),
        (
            _agent_evaluation_report(
                candidate_commit="1" * 40,
                execution_mode="manifest_validation",
            ),
            "installed_agent_evaluation_not_passed",
        ),
    ],
    ids=("mismatched", "incomplete", "fixture_only"),
)
def test_process_agent_evaluation_rejects_mismatched_incomplete_or_fixture_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report: EvalRunReport,
    reason: str,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _agent_evaluation_runtime_env(monkeypatch, tmp_path)
    monkeypatch.setattr(producer, "run_command", _fake_agent_evaluation_command(report))

    with pytest.raises(producer.ProofProducerError, match=reason):
        producer._run_installed_agent_evaluation(  # noqa: SLF001
            candidate_commit="1" * 40,
            installed_cache_sha256="2" * 64,
        )


def test_authentic_process_agent_evaluation_issues_exact_tool_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _agent_evaluation_runtime_env(monkeypatch, tmp_path)
    report = _agent_evaluation_report(candidate_commit="1" * 40)
    monkeypatch.setattr(producer, "run_command", _fake_agent_evaluation_command(report))

    receipts = producer._run_installed_agent_evaluation(  # noqa: SLF001
        candidate_commit="1" * 40,
        installed_cache_sha256="2" * 64,
    )

    assert tuple(receipt.tool_id for receipt in receipts) == (
        load_analysis_kind_catalog().skill_scenario_tools
    )
    assert all(receipt.evaluation_state == "passed" for receipt in receipts)


def test_codex_native_process_evaluation_issues_exact_tool_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    codex_home = tmp_path / "codex-home"
    source_repo = tmp_path / "source-repo"
    for path in (codex_home, source_repo):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("SAXO_ANALYTICS_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("SAXO_ANALYTICS_SOURCE_REPO", str(source_repo))
    monkeypatch.delenv("SAXO_ANALYTICS_CLAUDE_CACHE_ROOT", raising=False)
    monkeypatch.delenv("SAXO_ANALYTICS_CLAUDE_HOME", raising=False)
    report = _codex_native_agent_evaluation_report(candidate_commit="1" * 40)
    execute = _fake_agent_evaluation_command(report)
    observed_commands: list[tuple[str, ...]] = []

    def inspect_command(*args: object, **kwargs: object) -> CommandResult:
        command = cast("tuple[str, ...]", args[1])
        observed_commands.append(command)
        return execute(*args, **kwargs)

    monkeypatch.setattr(producer, "run_command", inspect_command)
    receipts = producer._run_installed_agent_evaluation(  # noqa: SLF001
        candidate_commit="1" * 40,
        installed_cache_sha256="2" * 64,
        harness_policy="codex_native_v1",
    )

    assert tuple(receipt.tool_id for receipt in receipts) == (
        load_analysis_kind_catalog().skill_scenario_tools
    )
    assert len(observed_commands) == 1
    assert all("claude" not in value.lower() for value in observed_commands[0])


def test_agent_evaluation_uses_the_verified_source_clone_not_the_plugin_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    source_repo = _agent_evaluation_runtime_env(monkeypatch, tmp_path)
    report = _agent_evaluation_report(candidate_commit="1" * 40)
    execute = _fake_agent_evaluation_command(report)
    observed_source: list[Path] = []

    def inspect_command(*args: object, **kwargs: object) -> CommandResult:
        argv = cast("tuple[str, ...]", args[1])
        observed_source.append(Path(argv[argv.index("--source-repo") + 1]).resolve())
        return execute(*args, **kwargs)

    monkeypatch.setattr(producer, "run_command", inspect_command)

    producer._run_installed_agent_evaluation(  # noqa: SLF001
        candidate_commit="1" * 40,
        installed_cache_sha256="2" * 64,
    )

    assert observed_source == [source_repo.resolve()]
    assert observed_source[0] != Path.cwd().resolve()


def test_installed_producer_child_requires_an_isolated_sim_auth_lease() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    source = inspect.getsource(producer._execute_installed_child)  # noqa: SLF001
    command_source = inspect.getsource(producer._producer_command)  # noqa: SLF001
    agent_command_source = inspect.getsource(
        producer._agent_evaluation_command,  # noqa: SLF001
    )

    assert "prepare_eval_isolated_runtime" in source
    assert "bind_eval_runtime_account_allowlist" not in source
    assert "require_matrix_runtime_cleanup" in source
    assert "runtime.env" in source
    assert "_run_mcp_account_and_fixture_preflight" in inspect.getsource(
        import_module("saxo_bank_mcp.qa_sim_tool_matrix"),
    )
    assert "SAXO_MCP_TOKEN_CACHE_PATH" not in command_source
    assert "SAXO_MCP_SIM_CREDENTIAL_FILE" not in command_source
    assert "SAXO_MCP_LIVE_TOKEN_CACHE_PATH" not in source
    assert "ephemeral-owner-only-copy" in agent_command_source
    assert "source-codex-home" in agent_command_source
    assert "source-claude-home" in agent_command_source


def test_matrix_environment_never_discovers_accounts_with_direct_http() -> None:
    matrix_env = import_module("saxo_bank_mcp.agent_skill_matrix_env")
    source = inspect.getsource(matrix_env)

    assert "discover_exactly_one_active_sim_account" not in source
    assert "client.get(" not in source
    assert '"port/v1/accounts/me"' not in source


def test_public_or_fixture_authored_proof_bundle_cannot_reach_private_validator() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    _catalog, contracts, bundle = _complete_bundle()
    active_contracts = tuple(
        contract.model_copy(
            update={"proof_activation_state": "active", "quarantine_reason": None},
        )
        for contract in contracts
    )

    with pytest.raises(producer.ProofProducerError, match="trusted_producer_provenance_missing"):
        producer._validate_executed_bundle(  # noqa: SLF001
            bundle,
            contracts=active_contracts,
            candidate_commit=bundle.candidate_commit,
            authority=object(),
        )


def test_fixture_install_cannot_acquire_proof_producer_authority() -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    install_models = import_module("saxo_bank_mcp.agent_skill_install_models")
    fixture = install_models.FixtureSupportReport.model_construct(
        execution_mode="fixture_support",
    )

    with pytest.raises(producer.ProofProducerError, match="fixture_support_not_production"):
        producer.run_verified_installed_producer(
            fixture,
            candidate_commit="1" * 40,
        )


def test_copied_installed_producer_json_has_no_process_authority(tmp_path: Path) -> None:
    producer = import_module("saxo_bank_mcp.qa_analytics_proof_producer")
    copied = producer.InstalledProofProducerResult(
        candidate_commit="1" * 40,
        installed_cache_sha256="2" * 64,
        producer_module_sha256="3" * 64,
        catalog_sha256="4" * 64,
        contract_sha256="5" * 64,
        status="blocked",
        execution_performed=False,
        process_local_activation=False,
        executed_receipt_count=0,
        proof_execution_sha256="6" * 64,
        validation_errors=("installed_sim_requirements_unavailable",),
    )
    copied_path = tmp_path / "copied-producer.json"
    copied_path.write_text(copied.model_dump_json(), encoding="utf-8")
    parameters = inspect.signature(producer.run_verified_installed_producer).parameters

    assert "receipt_path" not in parameters
    assert "result_path" not in parameters
    assert "evidence_path" not in parameters
    with pytest.raises(producer.ProofProducerError, match="trusted_producer_provenance_missing"):
        producer._validate_process_owned_result(  # noqa: SLF001
            object(),
            command=("uv",),
            cache_root=tmp_path,
            candidate_commit="1" * 40,
            installed_cache_sha256="2" * 64,
            producer_module_sha256="3" * 64,
            authority=object(),
        )
    assert copied_path.is_file()
