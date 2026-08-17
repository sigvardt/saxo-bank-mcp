from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.qa_analytics_proof_publication import (
    CodexNativeProofPublication,
    build_codex_native_boundary_failure,
    build_codex_native_proof_publication,
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
