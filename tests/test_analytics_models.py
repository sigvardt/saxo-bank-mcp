from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_errors import AnalyticsPrivacyError
from saxo_bank_mcp.analytics_models import (
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
    QualityState,
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
_COMMIT = "a" * 40
_SHA256 = "b" * 64
_HANDLES_PER_KIND = 2
_EXPECTED_HANDLE_COUNT = len(HandleKind) * _HANDLES_PER_KIND
_HANDLE_RANDOM_BYTES = 24


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
        currency=None,
        metric_class=metric_class,
        source_timestamp=_AS_OF - timedelta(minutes=1),
        proof_profile_id=proof_profile_id,
    )


def _provenance(dataset_id: str) -> AnalysisProvenance:
    return AnalysisProvenance(
        dataset_id=dataset_id,
        source_scope="saxo_openapi",
        source_revision=_SOURCE_REVISION,
        source_timestamp=_AS_OF - timedelta(minutes=1),
        proof_profile_ids=(_PROOF_PROFILE_ID,),
        checks_passed=("golden", "independent_reference", "sim_end_to_end"),
        engine_name="saxo-analytics",
        engine_version="1",
        code_commit=_COMMIT,
    )


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


def test_safe_handles_use_kind_prefixes_and_random_192_bit_payloads() -> None:
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
            decoded = base64.urlsafe_b64decode(payload + "==")
            assert prefix == expected_prefixes[kind]
            assert re.fullmatch(r"[A-Za-z0-9_-]{32}", payload)
            assert len(decoded) == _HANDLE_RANDOM_BYTES


def test_models_reject_predictable_or_wrong_kind_handles() -> None:
    predictable = "ds_" + ("a" * 32)
    wrong_kind = new_safe_handle(HandleKind.ANALYSIS_ID)

    with pytest.raises(ValidationError, match="opaque handle"):
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
    ["unit", "currency", "metric_class", "source_timestamp", "proof_profile_id"],
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
    payload.update(unit="currency", currency=None)

    with pytest.raises(ValidationError, match="currency is required"):
        MetricValue.model_validate(payload)

    payload["currency"] = "usd"
    with pytest.raises(ValidationError):
        MetricValue.model_validate(payload)

    payload["currency"] = "USD"
    metric = MetricValue.model_validate(payload)
    assert metric.currency == "USD"


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


def test_public_evidence_allows_safe_ordinary_privacy_prose() -> None:
    payload = _refusal_payload()
    payload["reason"] = (
        "Raw account IDs, client keys, order IDs, URLs, local paths, tokens, "
        "and DisplayName are excluded from this evidence."
    )
    refusal = AnalyticsRefusal.model_validate(payload)

    assert validate_public_evidence(refusal) is None


@pytest.mark.parametrize(
    "raw_scope",
    [
        "123456789012",
        "00000000-0000-4000-8000-000000000000",
        "account_" + ("x" * 20),
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
            schema_version="1",
            status=AnalysisStatus.DEGRADED,
            tool_name="saxo_analyze_market",
            analysis_kind="bounded_market_overview",
            analysis_id=analysis_id,
            dataset_id=dataset_id,
            visibility=VisibilityMode.PUBLIC_EVIDENCE,
            as_of=now,
            reason_code="partial_source_coverage",
            reason="Only part of the requested source range is available.",
            quality=_quality(QualityState.PARTIAL),
            warnings=(),
            next_action="Use the reported coverage or narrow the date range.",
            verifies=("available source coverage",),
            does_not_verify=("the missing source range",),
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
