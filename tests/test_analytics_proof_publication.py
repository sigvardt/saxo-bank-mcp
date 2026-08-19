from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.qa_analytics_proof_failure import (
    CodexNativeBootstrapVerification,
    CodexNativeVerifiedChildFailure,
    verify_child_failure_envelope,
)
from saxo_bank_mcp.qa_analytics_proof_publication import (
    CodexNativeBoundaryFailureReceipt,
    CodexNativeProofPublication,
    build_codex_native_boundary_failure,
    build_codex_native_candidate_runner_receipt,
    build_codex_native_proof_publication,
    verify_codex_native_candidate_runner_receipt,
    verify_codex_native_proof_publication,
)

CANDIDATE = "1" * 40
CONTRACT_SHA256 = "5" * 64
ANALYSIS_KIND_COUNT = 54
EVIDENCE_RECEIPT_COUNT = 221


def _publication() -> CodexNativeProofPublication:
    result = build_codex_native_boundary_failure(
        candidate_commit=CANDIDATE,
        reason="proof_producer_native_boundary_failed",
    )
    return build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=ANALYSIS_KIND_COUNT,
        evidence_receipt_count=EVIDENCE_RECEIPT_COUNT,
        contract_sha256=CONTRACT_SHA256,
        result_kind="boundary_failure",
        result=result,
    )


def test_native_publication_round_trip_authenticates_every_top_level_field() -> None:
    publication = _publication()

    verified = verify_codex_native_proof_publication(publication.model_dump_json())

    assert verified == publication
    assert set(publication.model_dump(mode="json")) == {
        "schema_version",
        "receipt_kind",
        "harness_policy",
        "candidate_commit",
        "analysis_kind_count",
        "evidence_receipt_count",
        "contract_sha256",
        "result_kind",
        "result",
        "redacted_publication",
        "publication_sha256",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analysis_kind_count", ANALYSIS_KIND_COUNT - 1),
        ("evidence_receipt_count", EVIDENCE_RECEIPT_COUNT - 1),
        ("contract_sha256", "a" * 64),
        ("candidate_commit", "b" * 40),
    ],
)
def test_native_publication_rejects_tampered_top_level_material(
    field: str,
    value: object,
) -> None:
    payload = _publication().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError):
        verify_codex_native_proof_publication(json.dumps(payload))


def test_native_publication_rejects_tampered_nested_receipt() -> None:
    payload = _publication().model_dump(mode="json")
    assert isinstance(payload["result"], dict)
    payload["result"]["reason"] = "proof_tampered_reason"

    with pytest.raises(ValidationError):
        verify_codex_native_proof_publication(json.dumps(payload))


def test_native_publication_rejects_unsigned_extra_field() -> None:
    payload = _publication().model_dump(mode="json")
    payload["unsigned_summary"] = "passed"

    with pytest.raises(ValidationError):
        verify_codex_native_proof_publication(json.dumps(payload))


def test_legacy_runner_digest_cannot_authenticate_new_cleanup_claims() -> None:
    receipt = build_codex_native_candidate_runner_receipt(
        candidate_commit=CANDIDATE,
        candidate_tree="2" * 40,
        phase="entry",
        spawned=False,
        exit_code=None,
        command_sha256="3" * 64,
        command_schema_sha256="4" * 64,
        result_sha256=None,
        cleanup_status="not_started",
    )
    payload = receipt.model_dump(mode="json")
    legacy_material = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "receipt_sha256",
            "cleanup_identity_evidence_status",
            "cleanup_identity_receipt_sha256",
            "cleanup_unknown_reason",
        }
    }
    payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            legacy_material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    payload["cleanup_identity_evidence_status"] = "observation-unknown"
    payload["cleanup_identity_receipt_sha256"] = "d" * 64
    payload["cleanup_unknown_reason"] = "watcher_drain_unknown"

    with pytest.raises(ValidationError):
        verify_codex_native_candidate_runner_receipt(json.dumps(payload))


def test_legacy_boundary_digest_cannot_authenticate_new_cleanup_claims() -> None:
    result = build_codex_native_boundary_failure(
        candidate_commit=CANDIDATE,
        reason="proof_producer_native_boundary_failed",
    )
    payload = result.model_dump(mode="json")
    legacy_material = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "boundary_receipt_sha256",
            "candidate_runner_cleanup_evidence_status",
            "candidate_runner_cleanup_receipt_sha256",
            "candidate_runner_cleanup_unknown_reason",
        }
    }
    payload["boundary_receipt_sha256"] = hashlib.sha256(
        json.dumps(
            legacy_material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    payload["candidate_runner_cleanup_evidence_status"] = "observation-unknown"
    payload["candidate_runner_cleanup_receipt_sha256"] = "d" * 64
    payload["candidate_runner_cleanup_unknown_reason"] = "watcher_drain_unknown"

    with pytest.raises(ValidationError):
        CodexNativeBoundaryFailureReceipt.model_validate(payload)


def test_spawned_runner_unknown_cleanup_requires_typed_unknown_evidence() -> None:
    with pytest.raises(ValidationError):
        build_codex_native_candidate_runner_receipt(
            candidate_commit=CANDIDATE,
            candidate_tree="2" * 40,
            phase="exit",
            spawned=True,
            exit_code=1,
            command_sha256="3" * 64,
            command_schema_sha256="4" * 64,
            result_sha256=None,
            cleanup_status="unknown",
        )


def test_outer_unknown_candidate_cleanup_requires_typed_unknown_evidence() -> None:
    with pytest.raises(ValidationError):
        build_codex_native_boundary_failure(
            candidate_commit=CANDIDATE,
            reason="proof_candidate_runner_cleanup_failed",
            boundary_phase="candidate_runner",
            candidate_runner_receipt_sha256="d" * 64,
            candidate_runner_cleanup_status="unknown",
        )


def test_legacy_verified_failure_without_eval_summary_remains_verifiable() -> None:
    bootstrap = CodexNativeBootstrapVerification(
        status="missing",
        envelope=None,
    )
    verified = verify_child_failure_envelope(
        raw_stdout="",
        candidate_commit=CANDIDATE,
        installed_cache_sha256="2" * 64,
        producer_module_sha256="3" * 64,
        catalog_sha256="4" * 64,
        contract_sha256=CONTRACT_SHA256,
        child_exit_code=1,
        command_timed_out=False,
        command_cleanup_attempted=True,
        command_stdout_sha256="0" * 64,
        command_stderr_sha256="0" * 64,
        remaining_process_count=0,
        remaining_process_group_count=0,
        runtime_cleanup_status="complete",
        bootstrap_verification=bootstrap,
    )
    publication = build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=ANALYSIS_KIND_COUNT,
        evidence_receipt_count=EVIDENCE_RECEIPT_COUNT,
        contract_sha256=CONTRACT_SHA256,
        result_kind="verified_child_failure",
        result=verified,
    )
    payload = publication.model_dump(mode="json")
    assert isinstance(payload["result"], dict)
    result = cast("dict[str, Any]", payload["result"])
    result.pop("agent_evaluation_failure_summary")
    material = {key: value for key, value in payload.items() if key != "publication_sha256"}
    payload["publication_sha256"] = hashlib.sha256(
        json.dumps(
            material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()

    parsed = verify_codex_native_proof_publication(json.dumps(payload))

    assert parsed.result_kind == "verified_child_failure"
    assert isinstance(parsed.result, CodexNativeVerifiedChildFailure)
    assert parsed.result.agent_evaluation_failure_summary is None


def test_native_publication_rejects_nested_candidate_mismatch() -> None:
    result = build_codex_native_boundary_failure(
        candidate_commit="a" * 40,
        reason="proof_producer_native_boundary_failed",
    )

    with pytest.raises(ValidationError):
        build_codex_native_proof_publication(
            candidate_commit=CANDIDATE,
            analysis_kind_count=ANALYSIS_KIND_COUNT,
            evidence_receipt_count=EVIDENCE_RECEIPT_COUNT,
            contract_sha256=CONTRACT_SHA256,
            result_kind="boundary_failure",
            result=result,
        )


def test_native_boundary_receipt_authenticates_local_phase_command_and_cleanup() -> None:
    result = build_codex_native_boundary_failure(
        candidate_commit=CANDIDATE,
        reason="proof_bootstrap_receipt_invalid",
        boundary_phase="bootstrap_verification",
        command_state="completed",
        cleanup_status="complete",
        runtime_consumption_intent_sha256="6" * 64,
        runtime_cleanup_receipt_sha256="7" * 64,
    )

    publication = build_codex_native_proof_publication(
        candidate_commit=CANDIDATE,
        analysis_kind_count=ANALYSIS_KIND_COUNT,
        evidence_receipt_count=EVIDENCE_RECEIPT_COUNT,
        contract_sha256=CONTRACT_SHA256,
        result_kind="boundary_failure",
        result=result,
    )
    verified = verify_codex_native_proof_publication(publication.model_dump_json())

    assert isinstance(verified.result, CodexNativeBoundaryFailureReceipt)
    boundary = verified.result
    assert boundary.boundary_phase == "bootstrap_verification"
    assert boundary.command_state == "completed"
    assert boundary.cleanup_status == "complete"
    assert boundary.outer_runtime_cleanup_status == "complete"
    assert boundary.runtime_consumption_intent_sha256 == "6" * 64
    assert boundary.runtime_cleanup_receipt_sha256 == "7" * 64
    assert boundary.reason == "proof_bootstrap_receipt_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("boundary_phase", "runtime_preparation"),
        ("command_state", "not_started"),
        ("cleanup_status", "failed"),
        ("runtime_cleanup_receipt_sha256", "8" * 64),
    ],
)
def test_native_boundary_receipt_rejects_tampered_local_discriminators(
    field: str,
    value: object,
) -> None:
    result = build_codex_native_boundary_failure(
        candidate_commit=CANDIDATE,
        reason="proof_bootstrap_receipt_invalid",
        boundary_phase="bootstrap_verification",
        command_state="completed",
        cleanup_status="complete",
        runtime_consumption_intent_sha256="6" * 64,
        runtime_cleanup_receipt_sha256="7" * 64,
    )
    payload = result.model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError):
        type(result).model_validate(payload)


def test_native_boundary_receipt_can_prove_retained_cleanup_with_other_cleanup_failed() -> None:
    result = build_codex_native_boundary_failure(
        candidate_commit=CANDIDATE,
        reason="proof_sim_auth_lease_cleanup_failed",
        boundary_phase="isolated_runtime_cleanup",
        command_state="completed",
        cleanup_status="failed",
        runtime_consumption_intent_sha256="6" * 64,
        runtime_cleanup_receipt_sha256="7" * 64,
    )

    verified = type(result).model_validate_json(result.model_dump_json())

    assert verified.cleanup_status == "failed"
    assert verified.outer_runtime_cleanup_status == "failed"
    assert verified.runtime_cleanup_receipt_sha256 == "7" * 64
