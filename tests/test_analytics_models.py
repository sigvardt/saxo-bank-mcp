from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import RFC_4122, UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_errors import AnalyticsPrivacyError
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisOutput,
    AnalysisProvenance,
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    AnalyticsDegradation,
    AnalyticsRefusal,
    ArtifactSummary,
    DataCoverage,
    DataQuality,
    DatasetSummary,
    HandleKind,
    InstrumentAnalysisRequest,
    JobSummary,
    MarketAnalysisRequest,
    MetricClass,
    MetricValue,
    PortfolioAnalysisRequest,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    ValueUnitClass,
    VisibilityMode,
    new_safe_handle,
    validate_public_evidence,
)

_SCHEMA_PATH = (
    Path(__file__).parents[1] / "data" / "analytics" / "output_schema_v1.json"
)
_AS_OF = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
_PROOF_PROFILE_ID = "vp_fixture_v1"
_SOURCE_REVISION = "revision_fixture_v1"
_SOURCE_CONTRACT_SHA256 = "c" * 64
_COMMIT = "a" * 40
_SHA256 = "b" * 64
_REQUIRED_PROOF_CHECKS = (
    "golden",
    "property",
    "independent_reference",
    "saxo_reconciliation",
    "sim_end_to_end",
)
_HANDLES_PER_KIND = 2
_EXPECTED_HANDLE_COUNT = len(HandleKind) * _HANDLES_PER_KIND
_UUID_VERSION = 4
_ACCOUNT_ALIAS = "aa_00000000000040008000000000000000"


def _coverage(state: QualityState = QualityState.COMPLETE) -> DataCoverage:
    missing_rows = 0 if state is QualityState.COMPLETE else 2
    return DataCoverage(
        state=state,
        start_at=_AS_OF - timedelta(days=2),
        end_at=_AS_OF,
        row_count=3,
        expected_row_count=3 + missing_rows,
        missing_row_count=missing_rows,
    )


def _quality(state: QualityState = QualityState.COMPLETE) -> DataQuality:
    return DataQuality(
        state=state,
        coverage=_coverage(state),
        checked_at=_AS_OF,
        warnings=(),
    )


def _metric(
    *,
    metric_class: MetricClass = MetricClass.CALCULATED_VERIFIED,
    proof_profile_id: str = _PROOF_PROFILE_ID,
) -> MetricValue:
    return MetricValue(
        metric_id="total_return",
        value=0.125,
        unit="ratio",
        unit_class=ValueUnitClass.RATIO,
        currency=None,
        metric_class=metric_class,
        source_timestamp=_AS_OF - timedelta(minutes=1),
        proof_profile_id=proof_profile_id,
    )


def _provenance(dataset_id: str) -> AnalysisProvenance:
    source_binding = ProofSourceBinding(
        source_scope="saxo_openapi",
        source_revision=_SOURCE_REVISION,
        source_contract_sha256=_SOURCE_CONTRACT_SHA256,
    )
    engine_binding = ProofEngineBinding(
        engine_name="saxo-analytics",
        engine_version="1",
        code_commit=_COMMIT,
    )
    return AnalysisProvenance(
        dataset_id=dataset_id,
        source_scope="saxo_openapi",
        source_revision=_SOURCE_REVISION,
        source_contract_sha256=_SOURCE_CONTRACT_SHA256,
        source_timestamp=_AS_OF - timedelta(minutes=1),
        proof_receipts=(
            ActiveProofReceipt(
                state="active",
                analysis_kind="bounded_market_overview",
                schema_version="1",
                source_binding=source_binding,
                engine_binding=engine_binding,
                proof_profile_id=_PROOF_PROFILE_ID,
                checks_passed=_REQUIRED_PROOF_CHECKS,
            ),
        ),
        engine_name="saxo-analytics",
        engine_version="1",
        code_commit=_COMMIT,
    )


def _active_proof_receipt_payload() -> dict[str, Any]:
    return {
        "state": "active",
        "analysis_kind": "bounded_market_overview",
        "schema_version": "1",
        "source_binding": {
            "source_scope": "saxo_openapi",
            "source_revision": _SOURCE_REVISION,
            "source_contract_sha256": _SOURCE_CONTRACT_SHA256,
        },
        "engine_binding": {
            "engine_name": "saxo-analytics",
            "engine_version": "1",
            "code_commit": _COMMIT,
        },
        "proof_profile_id": _PROOF_PROFILE_ID,
        "checks_passed": _REQUIRED_PROOF_CHECKS,
    }


def _result_payload_with_active_proof_receipt() -> dict[str, Any]:
    payload = _result().model_dump()
    provenance = payload["provenance"]
    provenance["source_contract_sha256"] = _SOURCE_CONTRACT_SHA256
    provenance["proof_receipts"] = (_active_proof_receipt_payload(),)
    return payload


def _result(
    *,
    visibility: VisibilityMode = VisibilityMode.PRIVATE_USER_RESULT,
) -> AnalysisResult:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    return AnalysisResult(
        schema_version="1",
        status=AnalysisStatus.VERIFIED,
        tool_name="saxo_analyze_market",
        analysis_id=new_safe_handle(HandleKind.ANALYSIS_ID),
        analysis_kind="bounded_market_overview",
        request=MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="bounded_market_overview",
            dataset_id=dataset_id,
        ),
        visibility=visibility,
        account_scope="selected SIM account",
        as_of=_AS_OF,
        valid_until=_AS_OF + timedelta(minutes=5),
        metrics=(_metric(),),
        warnings=(),
        data_quality=_quality(),
        provenance=_provenance(dataset_id),
        assumptions=(),
        is_not_advice=True,
        is_not_forecast=True,
        model_distribution_only=False,
        verifies=("source-bound metric calculation",),
        does_not_verify=("future performance",),
        replayable=True,
        next_actions=(),
    )


def _refusal_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "status": AnalysisStatus.REFUSED,
        "tool_name": "saxo_analyze_portfolio",
        "analysis_kind": "tax_lot_export",
        "visibility": VisibilityMode.PUBLIC_EVIDENCE,
        "as_of": _AS_OF,
        "reason_code": "authoritative_basis_unavailable",
        "reason": "The required source basis is unavailable.",
        "next_action": "Use a supported analysis kind.",
        "warnings": (),
        "verifies": (),
        "does_not_verify": ("authoritative tax lots",),
    }


def _refusal() -> AnalyticsRefusal:
    return AnalyticsRefusal.model_validate(_refusal_payload())


def _degradation_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "status": AnalysisStatus.DEGRADED,
        "tool_name": "saxo_analyze_market",
        "analysis_kind": "bounded_market_overview",
        "analysis_id": new_safe_handle(HandleKind.ANALYSIS_ID),
        "dataset_id": new_safe_handle(HandleKind.DATASET_ID),
        "visibility": VisibilityMode.PUBLIC_EVIDENCE,
        "as_of": _AS_OF,
        "reason_code": "partial_source_coverage",
        "reason": "Only part of the requested source range is available.",
        "quality": _quality(QualityState.PARTIAL),
        "warnings": (),
        "next_action": "Use the reported coverage or narrow the date range.",
        "verifies": ("available source coverage",),
        "does_not_verify": ("the missing source range",),
    }


@pytest.mark.parametrize(
    ("enum_type", "expected"),
    [
        (
            AnalysisStatus,
            {
                "VERIFIED": "verified",
                "DEGRADED": "degraded",
                "REFUSED": "refused",
                "UNVERIFIED": "unverified",
            },
        ),
        (
            MetricClass,
            {
                "BROKER_REPORTED": "broker_reported",
                "CALCULATED_VERIFIED": "calculated_verified",
                "MODEL_OUTPUT": "model_output",
                "APPROXIMATION": "approximation",
                "UNAVAILABLE": "unavailable",
            },
        ),
        (
            VisibilityMode,
            {
                "PRIVATE_USER_RESULT": "private_user_result",
                "INLINE_PRIVATE": "inline_private",
                "LOCAL_RESOURCE_LINK": "local_resource_link",
                "REDACTED_PREVIEW": "redacted_preview",
                "FINGERPRINT_ONLY": "fingerprint_only",
                "PUBLIC_EVIDENCE": "public_evidence",
            },
        ),
        (
            QualityState,
            {
                "COMPLETE": "complete",
                "PARTIAL": "partial",
                "STALE": "stale",
                "MISSING": "missing",
                "INVALID": "invalid",
            },
        ),
        (
            HandleKind,
            {
                "INSTRUMENT_HANDLE": "instrument_handle",
                "UNIVERSE_ID": "universe_id",
                "DATASET_ID": "dataset_id",
                "PORTFOLIO_SNAPSHOT_ID": "portfolio_snapshot_id",
                "ANALYSIS_ID": "analysis_id",
                "ARTIFACT_ID": "artifact_id",
                "JOB_ID": "job_id",
                "DELETION_PREVIEW_TOKEN": "deletion_preview_token",
            },
        ),
    ],
)
def test_enum_protocol_values_are_exact_and_round_trip(
    enum_type: type[
        AnalysisStatus | MetricClass | VisibilityMode | QualityState | HandleKind
    ],
    expected: dict[str, str],
) -> None:
    adapter = TypeAdapter(enum_type)

    assert {member.name: member.value for member in enum_type} == expected
    for member in enum_type:
        assert adapter.validate_json(adapter.dump_json(member)) is member


def test_analysis_requests_are_discriminated_and_round_trip() -> None:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    requests: tuple[
        MarketAnalysisRequest | InstrumentAnalysisRequest | PortfolioAnalysisRequest,
        ...,
    ] = (
        MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="bounded_market_overview",
            dataset_id=dataset_id,
        ),
        InstrumentAnalysisRequest(
            request_kind="instrument",
            analysis_kind="price_return",
            dataset_id=dataset_id,
            instrument_handles=(
                new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
            ),
        ),
        PortfolioAnalysisRequest(
            request_kind="portfolio",
            analysis_kind="overview",
            dataset_id=dataset_id,
            portfolio_snapshot_id=new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID),
        ),
    )
    adapter = TypeAdapter(AnalysisRequest)

    assert tuple(
        type(adapter.validate_json(adapter.dump_json(request))) for request in requests
    ) == (
        MarketAnalysisRequest,
        InstrumentAnalysisRequest,
        PortfolioAnalysisRequest,
    )
    for request in requests:
        assert adapter.validate_json(adapter.dump_json(request)) == request


def test_analysis_request_rejects_an_unknown_discriminator() -> None:
    payload = {
        "request_kind": "unknown",
        "analysis_kind": "overview",
        "dataset_id": new_safe_handle(HandleKind.DATASET_ID),
    }

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        TypeAdapter(AnalysisRequest).validate_python(payload)


def test_safe_handles_use_kind_prefixes_and_random_uuid4_payloads() -> None:
    expected_prefixes = {
        HandleKind.INSTRUMENT_HANDLE: "ih",
        HandleKind.UNIVERSE_ID: "un",
        HandleKind.DATASET_ID: "ds",
        HandleKind.PORTFOLIO_SNAPSHOT_ID: "ps",
        HandleKind.ANALYSIS_ID: "an",
        HandleKind.ARTIFACT_ID: "ar",
        HandleKind.JOB_ID: "jb",
        HandleKind.DELETION_PREVIEW_TOKEN: "dp",
    }
    handles = {
        kind: (new_safe_handle(kind), new_safe_handle(kind)) for kind in HandleKind
    }

    assert (
        len({handle for pair in handles.values() for handle in pair})
        == _EXPECTED_HANDLE_COUNT
    )
    for kind, pair in handles.items():
        for handle in pair:
            prefix, payload = handle.split("_", maxsplit=1)
            opaque_uuid = UUID(hex=payload)
            assert prefix == expected_prefixes[kind]
            assert re.fullmatch(r"[0-9a-f]{32}", payload)
            assert opaque_uuid.version == _UUID_VERSION
            assert opaque_uuid.variant == RFC_4122


def test_models_reject_predictable_or_wrong_kind_handles() -> None:
    predictable = "ds_" + ("a" * 32)
    wrong_kind = new_safe_handle(HandleKind.ANALYSIS_ID)

    with pytest.raises(ValidationError):
        MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="overview",
            dataset_id=predictable,
        )
    with pytest.raises(ValidationError):
        MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="overview",
            dataset_id=wrong_kind,
        )


def test_models_reject_encoded_identifier_handle_payloads() -> None:
    encoded_identifier = "ds_QWNjb3VudElkPTEyMzQ1Njc4OTAxMjM0"

    with pytest.raises(ValidationError):
        MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="overview",
            dataset_id=encoded_identifier,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 7, 30, 8, 0),  # noqa: DTZ001
        datetime(2026, 7, 30, 9, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_material_metric_rejects_non_utc_timestamps(timestamp: datetime) -> None:
    payload = _metric().model_dump()
    payload["source_timestamp"] = timestamp

    with pytest.raises(ValidationError, match="UTC"):
        MetricValue.model_validate(payload)


def test_utc_timestamps_round_trip_as_zulu_json() -> None:
    metric = _metric()

    payload = metric.model_dump_json()

    assert '"source_timestamp":"2026-07-30T07:59:00Z"' in payload
    assert MetricValue.model_validate_json(payload) == metric


@pytest.mark.parametrize(
    "missing_field",
    [
        "unit",
        "unit_class",
        "currency",
        "metric_class",
        "source_timestamp",
        "proof_profile_id",
    ],
)
def test_material_numeric_claim_requires_explicit_typed_context(
    missing_field: str,
) -> None:
    payload = _metric().model_dump()
    del payload[missing_field]

    with pytest.raises(ValidationError):
        MetricValue.model_validate(payload)


def test_money_claim_requires_an_iso_currency() -> None:
    payload = _metric().model_dump()
    payload.update(
        unit="currency",
        unit_class=ValueUnitClass.MONETARY,
        currency=None,
    )

    with pytest.raises(ValidationError, match="currency is required"):
        MetricValue.model_validate(payload)

    payload["currency"] = "usd"
    with pytest.raises(ValidationError):
        MetricValue.model_validate(payload)

    payload["currency"] = "USD"
    metric = MetricValue.model_validate(payload)
    assert metric.currency == "USD"


def test_money_claim_rejects_unknown_iso_currency_code() -> None:
    payload = _metric().model_dump()
    payload.update(
        unit="currency",
        unit_class=ValueUnitClass.MONETARY,
        currency="ZZZ",
    )

    with pytest.raises(ValidationError, match="ISO 4217"):
        MetricValue.model_validate(payload)


def test_money_claim_schema_publishes_closed_iso_currency_codes() -> None:
    schema = TypeAdapter(MetricValue).json_schema()
    currency_schema = schema["properties"]["currency"]
    currency_reference = next(
        branch["$ref"]
        for branch in currency_schema["anyOf"]
        if "$ref" in branch
    )
    currency_definition = currency_reference.rsplit("/", maxsplit=1)[-1]
    published_codes = schema["$defs"][currency_definition]["enum"]

    assert "USD" in published_codes
    assert "ZZZ" not in published_codes


def test_value_unit_class_protocol_values_are_exact_and_round_trip() -> None:
    enum_type = ValueUnitClass
    expected = {
        "MONETARY": "monetary",
        "RATIO": "ratio",
        "PERCENTAGE": "percentage",
        "COUNT": "count",
        "DURATION": "duration",
        "QUANTITY": "quantity",
    }
    adapter = TypeAdapter(enum_type)

    assert {member.name: member.value for member in enum_type} == expected
    for member in enum_type:
        assert adapter.validate_json(adapter.dump_json(member)) is member


def test_non_monetary_metric_requires_an_explicit_unit_class() -> None:
    payload = _metric().model_dump()
    del payload["unit_class"]

    with pytest.raises(ValidationError):
        MetricValue.model_validate(payload)

    assert _metric().unit_class is ValueUnitClass.RATIO


@pytest.mark.parametrize("unit", ["price_per_share", "cash_amount", "fee"])
def test_monetary_unit_class_requires_currency_for_novel_unit_names(
    unit: str,
) -> None:
    payload = _metric().model_dump()
    payload.update(
        unit=unit,
        unit_class=ValueUnitClass.MONETARY,
        currency=None,
    )

    with pytest.raises(ValidationError, match="currency is required"):
        MetricValue.model_validate(payload)


@pytest.mark.parametrize("unit", ["price_per_share", "cash_amount", "fee"])
def test_non_monetary_unit_class_cannot_disguise_novel_money_units(
    unit: str,
) -> None:
    payload = _metric().model_dump()
    payload.update(unit=unit, unit_class=ValueUnitClass.RATIO, currency=None)

    with pytest.raises(ValidationError, match="not allowed for the selected unit class"):
        MetricValue.model_validate(payload)


def test_non_currency_unit_class_rejects_currency() -> None:
    payload = _metric().model_dump()
    payload.update(unit_class=ValueUnitClass.RATIO, currency="USD")

    with pytest.raises(ValidationError, match="currency is forbidden"):
        MetricValue.model_validate(payload)


@pytest.mark.parametrize("value", [True, float("inf"), float("nan")])
def test_material_numeric_claim_rejects_non_finite_or_boolean_values(
    value: object,
) -> None:
    payload = _metric().model_dump()
    payload["value"] = value

    with pytest.raises(ValidationError):
        MetricValue.model_validate(payload)


def test_verified_result_requires_source_bound_metric_proof() -> None:
    payload = _result().model_dump()
    payload["metrics"][0]["proof_profile_id"] = "vp_unlisted_v1"

    with pytest.raises(ValidationError, match="proof profile"):
        AnalysisResult.model_validate(payload)


def test_verified_result_rejects_self_declared_fake_matching_proof() -> None:
    payload = _result().model_dump()
    provenance = payload["provenance"]
    del provenance["proof_receipts"]
    provenance["proof_profile_ids"] = (_PROOF_PROFILE_ID,)
    provenance["checks_passed"] = ("golden", "made_up_check")

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


def test_verified_result_accepts_exact_active_proof_receipt_binding() -> None:
    payload = _result_payload_with_active_proof_receipt()

    result = AnalysisResult.model_validate(payload)

    assert result.provenance.proof_receipts[0].state == "active"


def test_verified_result_requires_an_active_proof_receipt() -> None:
    payload = _result_payload_with_active_proof_receipt()
    del payload["provenance"]["proof_receipts"]

    with pytest.raises(ValidationError, match="proof_receipts"):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (("state",), "inactive", "active"),
        (("analysis_kind",), "different_analysis", "analysis kind"),
        (("schema_version",), "2", "schema_version"),
        (
            ("source_binding", "source_revision"),
            "different_revision",
            "source binding",
        ),
        (
            ("source_binding", "source_contract_sha256"),
            "d" * 64,
            "source binding",
        ),
        (("engine_binding", "engine_name"), "other-engine", "engine binding"),
        (("engine_binding", "engine_version"), "2", "engine binding"),
        (("engine_binding", "code_commit"), "e" * 40, "engine binding"),
        (("proof_profile_id",), "vp_different_v1", "proof profile"),
    ],
)
def test_verified_result_rejects_mismatched_active_proof_receipt_bindings(
    path: tuple[str, ...],
    replacement: str,
    message: str,
) -> None:
    payload = _result_payload_with_active_proof_receipt()
    target = payload["provenance"]["proof_receipts"][0]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement

    with pytest.raises(ValidationError, match=message):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    "checks_passed",
    [
        ("golden", "property"),
        (*_REQUIRED_PROOF_CHECKS, "made_up_check"),
        (
            "golden",
            "property",
            "independent_reference",
            "saxo_reconciliation",
            "golden",
        ),
    ],
)
def test_active_proof_receipt_rejects_missing_or_made_up_checks(
    checks_passed: tuple[str, ...],
) -> None:
    payload = _active_proof_receipt_payload()
    payload["checks_passed"] = checks_passed

    with pytest.raises(ValidationError, match="proof checks"):
        ActiveProofReceipt.model_validate(payload)


def test_active_proof_receipt_schema_closes_and_deduplicates_checks() -> None:
    schema = TypeAdapter(ActiveProofReceipt).json_schema()
    checks_schema = schema["properties"]["checks_passed"]
    proof_check_definition = checks_schema["items"]["$ref"].rsplit("/", maxsplit=1)[-1]

    assert checks_schema["minItems"] == len(_REQUIRED_PROOF_CHECKS)
    assert checks_schema["maxItems"] == len(_REQUIRED_PROOF_CHECKS)
    assert checks_schema["uniqueItems"] is True
    assert schema["$defs"][proof_check_definition]["enum"] == list(
        _REQUIRED_PROOF_CHECKS,
    )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("state", "inactive"),
        ("checks_passed", ("golden", "made_up_check")),
    ],
)
def test_verified_result_rejects_forged_proof_receipt_instances(
    field_name: str,
    replacement: object,
) -> None:
    payload = _result().model_dump()
    receipt = ActiveProofReceipt.model_validate(_active_proof_receipt_payload())
    forged_receipt = receipt.model_copy(update={field_name: replacement})
    payload["provenance"]["proof_receipts"] = (forged_receipt,)

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("status",), AnalysisStatus.DEGRADED),
        (("data_quality", "state"), QualityState.PARTIAL),
        (("metrics", 0, "metric_class"), MetricClass.APPROXIMATION),
    ],
)
def test_verified_result_rejects_non_verified_state(
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    payload = _result().model_dump()
    target: Any = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


def test_verified_result_rejects_request_and_provenance_mismatch() -> None:
    payload = _result().model_dump()
    payload["request"]["dataset_id"] = new_safe_handle(HandleKind.DATASET_ID)

    with pytest.raises(ValidationError, match="dataset"):
        AnalysisResult.model_validate(payload)


def test_verified_result_rejects_incomplete_coverage_hidden_by_quality_state() -> None:
    payload = _result().model_dump()
    coverage = payload["data_quality"]["coverage"]
    coverage["state"] = QualityState.PARTIAL
    coverage["expected_row_count"] = 5
    coverage["missing_row_count"] = 2

    with pytest.raises(ValidationError, match="complete quality"):
        AnalysisResult.model_validate(payload)


def test_verified_result_rejects_coverage_ending_after_as_of() -> None:
    payload = _result().model_dump()
    payload["data_quality"]["coverage"]["end_at"] = _AS_OF + timedelta(seconds=1)

    with pytest.raises(ValidationError, match="coverage"):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    ("model_type", "extra_field"),
    [
        (AnalyticsRefusal, {"metrics": []}),
        (AnalyticsDegradation, {"value": 1.0}),
    ],
)
def test_refusal_and_degradation_are_structurally_value_free(
    model_type: type[AnalyticsRefusal | AnalyticsDegradation],
    extra_field: dict[str, object],
) -> None:
    payload = (
        _refusal_payload()
        if model_type is AnalyticsRefusal
        else {
            **_degradation_payload(),
        }
    )
    payload.update(extra_field)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model_type.model_validate(payload)


def _unsafe_public_texts() -> tuple[str, ...]:
    assignment_value = "synthetic_" + ("x" * 16)
    return (
        f"AccountId={assignment_value}",
        f"ClientKey={assignment_value}",
        f"OrderId={assignment_value}",
        "https:" + "//example.invalid/private",
        "/" + "Users/example/private/result.json",
        "access_" + f"token={assignment_value}",
        "Display" + "Name=Example Person",
        '{"ClientKey":"synthetic_1234567890"}',
        "/srv/analytics/result.json",
        "/etc/passwd",
        r"C:\Work\result.json",
    )


@pytest.mark.parametrize("unsafe_text", _unsafe_public_texts())
def test_public_evidence_rejects_private_identifiers_and_locations(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text

    with pytest.raises(ValidationError) as error:
        AnalyticsRefusal.model_validate(payload)

    assert "public evidence contains a forbidden value class" in str(error.value)
    assert unsafe_text not in str(error.value)


@pytest.mark.parametrize("unsafe_text", _unsafe_public_texts())
def test_public_evidence_validator_rejects_models_built_without_validation(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text
    forged = AnalyticsRefusal.model_construct(**payload)

    with pytest.raises(AnalyticsPrivacyError) as error:
        validate_public_evidence(forged)

    assert unsafe_text not in str(error.value)


@pytest.mark.parametrize(
    "safe_text",
    [
        "The client key is unavailable.",
        "ClientKey is unavailable.",
        '"ClientKey" is "unavailable".',
        "AccountId: unavailable.",
        "ClientKey=redacted",
        "The client_key is not available.",
        "ClientKey is not_available.",
        '"ClientKey" is "not-provided".',
        "The local directory is unavailable.",
        (
            "Raw account IDs, client keys, order IDs, URLs, local paths, tokens, "
            "and DisplayName are excluded from this evidence."
        ),
    ],
)
def test_public_evidence_allows_safe_ordinary_privacy_prose(safe_text: str) -> None:
    payload = _refusal_payload()
    payload["reason"] = safe_text
    refusal = AnalyticsRefusal.model_validate(payload)

    assert validate_public_evidence(refusal) is None


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "ClientKey is synthetic_1234567890",
        '"ClientKey" is "synthetic_1234567890"',
        "client key is synthetic_1234567890",
        "client_key is synthetic_1234567890",
        "client-key is synthetic_1234567890",
    ],
)
def test_public_evidence_rejects_identifier_like_is_assignments(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text

    with pytest.raises(ValidationError):
        AnalyticsRefusal.model_validate(payload)

    forged = AnalyticsRefusal.model_construct(**payload)
    with pytest.raises(AnalyticsPrivacyError):
        validate_public_evidence(forged)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "DisplayName is Jane Doe",
        "DeletionPreviewToken=dp_0123456789abcdef0123456789abcdef",
        "ftp://internal.example/private/result.json",
        "custom+private://internal.example/private/result.json",
    ],
)
def test_public_evidence_rejects_named_private_value_probes(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text

    with pytest.raises(ValidationError, match="forbidden value class"):
        AnalyticsRefusal.model_validate(payload)

    forged = AnalyticsRefusal.model_construct(**payload)
    with pytest.raises(AnalyticsPrivacyError, match="forbidden value class"):
        validate_public_evidence(forged)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        r"\\server\share\result.json",
        "~/analytics/result.json",
    ],
)
def test_public_evidence_rejects_unc_and_home_relative_paths(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text

    with pytest.raises(ValidationError):
        AnalyticsRefusal.model_validate(payload)

    forged = AnalyticsRefusal.model_construct(**payload)
    with pytest.raises(AnalyticsPrivacyError):
        validate_public_evidence(forged)


@pytest.mark.parametrize(
    ("model_type", "field_name", "unsafe_text"),
    [
        (AnalyticsRefusal, "reason", "Portfolio value: USD 12345.67."),
        (AnalyticsRefusal, "next_action", "Review EUR 999.00 before continuing."),
        (AnalyticsRefusal, "warnings", "Estimated fee: $123.45."),
        (AnalyticsDegradation, "reason", "Portfolio value: GBP 12345.67."),
        (AnalyticsDegradation, "next_action", "Review DKK 999.00 before continuing."),
        (AnalyticsDegradation, "warnings", "Estimated fee: €123.45."),
    ],
)
def test_public_refusals_and_degradations_reject_monetary_text(
    model_type: type[AnalyticsRefusal | AnalyticsDegradation],
    field_name: str,
    unsafe_text: str,
) -> None:
    payload = (
        _refusal_payload()
        if model_type is AnalyticsRefusal
        else _degradation_payload()
    )
    if field_name == "warnings":
        payload[field_name] = (
            {
                "code": "material_value",
                "message": unsafe_text,
                "quality_state": None,
                "next_action": None,
            },
        )
    else:
        payload[field_name] = unsafe_text

    with pytest.raises(ValidationError, match="forbidden value class"):
        model_type.model_validate(payload)


@pytest.mark.parametrize(
    "model_type",
    [AnalyticsRefusal, AnalyticsDegradation],
)
def test_public_refusals_and_degradations_reject_financial_percentages(
    model_type: type[AnalyticsRefusal | AnalyticsDegradation],
) -> None:
    payload = (
        _refusal_payload()
        if model_type is AnalyticsRefusal
        else _degradation_payload()
    )
    payload["reason"] = "Portfolio return: 12.5%."

    with pytest.raises(ValidationError, match="forbidden value class"):
        model_type.model_validate(payload)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Portfolio value: 12345.67 dollars.",
        "Portfolio value: dollars 12345.67.",
        "Portfolio value: won 12345.67.",
        "Portfolio value: 12345.67 WoN.",
        "Portfolio value: USD 12345.67.",
        "Portfolio value: 12345.67 USD.",
    ],
)
def test_public_evidence_rejects_currency_names_and_real_codes_on_either_side(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text

    with pytest.raises(ValidationError, match="forbidden value class"):
        AnalyticsRefusal.model_validate(payload)


@pytest.mark.parametrize(
    "safe_text",
    [
        "All 123 rows were available.",
        "Won 123 tests during validation.",
        "Dollars 123 examples were listed.",
    ],
)
def test_public_evidence_allows_currency_word_collisions_without_financial_context(
    safe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = safe_text

    output = AnalyticsRefusal.model_validate(payload)

    assert validate_public_evidence(output) is None


def test_public_evidence_allows_financial_domain_prose_without_bound_money(
) -> None:
    payload = _refusal_payload()
    payload["reason"] = "The cost model won 123 tests."

    output = AnalyticsRefusal.model_validate(payload)

    assert validate_public_evidence(output) is None


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Portfolio value: won 12345.67.",
        "Balance is won 123.",
    ],
)
def test_public_evidence_rejects_currency_name_amount_bound_to_financial_label(
    unsafe_text: str,
) -> None:
    payload = _refusal_payload()
    payload["reason"] = unsafe_text

    with pytest.raises(ValidationError, match="forbidden value class"):
        AnalyticsRefusal.model_validate(payload)


def test_public_evidence_allows_non_currency_uppercase_count_labels() -> None:
    payload = _refusal_payload()
    payload["reason"] = "SIM 123 rows were available."

    output = AnalyticsRefusal.model_validate(payload)

    assert validate_public_evidence(output) is None


@pytest.mark.parametrize(
    "model_type",
    [AnalyticsRefusal, AnalyticsDegradation],
)
def test_public_value_free_counts_and_times_remain_allowed(
    model_type: type[AnalyticsRefusal | AnalyticsDegradation],
) -> None:
    payload = (
        _refusal_payload()
        if model_type is AnalyticsRefusal
        else _degradation_payload()
    )
    payload["reason"] = "Rows available: 123. Cutoff time: 12:30 UTC."

    output = model_type.model_validate(payload)

    assert validate_public_evidence(output) is None


@pytest.mark.parametrize(
    "raw_scope",
    [
        "123456789012",
        "00000000-0000-4000-8000-000000000000",
        "account_" + ("x" * 20),
        "raw-account-identifier-7654321",
        "aa_00000000000010008000000000000000",
    ],
)
def test_public_dataset_summary_rejects_raw_account_scope(raw_scope: str) -> None:
    with pytest.raises(ValidationError, match="safe account alias"):
        DatasetSummary(
            schema_version="1",
            dataset_id=new_safe_handle(HandleKind.DATASET_ID),
            visibility=VisibilityMode.FINGERPRINT_ONLY,
            account_scope=raw_scope,
            source_scope="saxo_openapi",
            source_revision=_SOURCE_REVISION,
            fingerprint_sha256=_SHA256,
            created_at=_AS_OF,
            coverage=_coverage(),
            data_quality=_quality(),
        )


@pytest.mark.parametrize(
    "account_scope",
    ["aggregate", "selected SIM account", _ACCOUNT_ALIAS],
)
def test_account_scope_accepts_closed_aggregate_or_uuid4_alias(
    account_scope: str,
) -> None:
    summary = DatasetSummary(
        schema_version="1",
        dataset_id=new_safe_handle(HandleKind.DATASET_ID),
        visibility=VisibilityMode.FINGERPRINT_ONLY,
        account_scope=account_scope,
        source_scope="saxo_openapi",
        source_revision=_SOURCE_REVISION,
        fingerprint_sha256=_SHA256,
        created_at=_AS_OF,
        coverage=_coverage(),
        data_quality=_quality(),
    )

    assert summary.account_scope == account_scope


def test_account_scope_schema_exposes_only_aggregate_or_opaque_alias() -> None:
    schema = TypeAdapter(DatasetSummary).json_schema()
    account_scope_reference = schema["properties"]["account_scope"]["$ref"]
    account_scope_definition = account_scope_reference.rsplit("/", maxsplit=1)[-1]

    assert schema["$defs"][account_scope_definition] == {
        "anyOf": [
            {
                "enum": ["aggregate", "selected SIM account"],
                "type": "string",
            },
            {
                "pattern": r"^aa_[0-9a-f]{32}$",
                "type": "string",
            },
        ],
    }


def test_dataset_summary_uses_validated_source_revision_contract() -> None:
    with pytest.raises(ValidationError):
        DatasetSummary(
            schema_version="1",
            dataset_id=new_safe_handle(HandleKind.DATASET_ID),
            visibility=VisibilityMode.FINGERPRINT_ONLY,
            account_scope="aggregate",
            source_scope="saxo_openapi",
            source_revision="revision with spaces",
            fingerprint_sha256=_SHA256,
            created_at=_AS_OF,
            coverage=_coverage(),
            data_quality=_quality(),
        )


def test_dataset_summary_rejects_mismatched_coverage_contracts() -> None:
    quality_payload = _quality().model_dump()
    quality_payload["coverage"]["start_at"] = _AS_OF - timedelta(days=1)

    with pytest.raises(ValidationError, match="coverage"):
        DatasetSummary(
            schema_version="1",
            dataset_id=new_safe_handle(HandleKind.DATASET_ID),
            visibility=VisibilityMode.FINGERPRINT_ONLY,
            account_scope="aggregate",
            source_scope="saxo_openapi",
            source_revision=_SOURCE_REVISION,
            fingerprint_sha256=_SHA256,
            created_at=_AS_OF,
            coverage=_coverage(),
            data_quality=DataQuality.model_validate(quality_payload),
        )


def test_private_delivery_cannot_be_published_as_public_evidence() -> None:
    result = _result()

    with pytest.raises(AnalyticsPrivacyError, match="delivery mode"):
        validate_public_evidence(result)


def test_material_numeric_results_cannot_be_constructed_as_public_evidence() -> None:
    with pytest.raises(ValidationError, match="material numeric claims"):
        _result(visibility=VisibilityMode.PUBLIC_EVIDENCE)


def test_output_models_round_trip_through_the_shared_union() -> None:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    now = _AS_OF
    outputs: tuple[
        AnalysisResult
        | DatasetSummary
        | ArtifactSummary
        | JobSummary
        | AnalyticsRefusal
        | AnalyticsDegradation,
        ...,
    ] = (
        _result(),
        DatasetSummary(
            schema_version="1",
            dataset_id=dataset_id,
            visibility=VisibilityMode.FINGERPRINT_ONLY,
            account_scope="selected SIM account",
            source_scope="saxo_openapi",
            source_revision=_SOURCE_REVISION,
            fingerprint_sha256=_SHA256,
            created_at=now,
            coverage=_coverage(),
            data_quality=_quality(),
        ),
        ArtifactSummary(
            schema_version="1",
            artifact_id=new_safe_handle(HandleKind.ARTIFACT_ID),
            analysis_id=analysis_id,
            visibility=VisibilityMode.REDACTED_PREVIEW,
            media_type="image/png",
            byte_count=1024,
            sha256=_SHA256,
            created_at=now,
            description="Redacted chart preview.",
        ),
        JobSummary(
            schema_version="1",
            job_id=new_safe_handle(HandleKind.JOB_ID),
            state="completed",
            visibility=VisibilityMode.FINGERPRINT_ONLY,
            request_fingerprint=_SHA256,
            created_at=now - timedelta(minutes=1),
            updated_at=now,
            analysis_id=analysis_id,
            artifact_ids=(),
            message="The bounded local job completed.",
        ),
        _refusal(),
        AnalyticsDegradation(
            **_degradation_payload(),
        ),
    )
    adapter = TypeAdapter(AnalysisOutput)

    for output in outputs:
        assert adapter.validate_json(adapter.dump_json(output)) == output


def _walk_schema(value: object) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(_walk_schema(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_walk_schema(child))
    return nodes


def test_generated_schema_forbids_extra_fields_and_freezes_request_discriminator() -> None:
    schema = TypeAdapter(AnalysisOutput).json_schema()
    nodes = _walk_schema(schema)
    object_schemas = [node for node in nodes if node.get("type") == "object"]
    discriminators = [node["discriminator"] for node in nodes if "discriminator" in node]

    assert object_schemas
    assert all(node.get("additionalProperties") is False for node in object_schemas)
    assert {
        "propertyName": "request_kind",
        "mapping": {
            "instrument": "#/$defs/InstrumentAnalysisRequest",
            "market": "#/$defs/MarketAnalysisRequest",
            "portfolio": "#/$defs/PortfolioAnalysisRequest",
        },
    } in discriminators


def test_checked_in_output_schema_is_exactly_stable() -> None:
    generated = (
        json.dumps(
            TypeAdapter(AnalysisOutput).json_schema(),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    assert _SCHEMA_PATH.read_text(encoding="utf-8") == generated
