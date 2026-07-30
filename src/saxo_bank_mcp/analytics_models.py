from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from saxo_bank_mcp.analytics_errors import AnalyticsPrivacyError

_SCHEMA_VERSION: Final = "1"
_HANDLE_RANDOM_BYTES: Final = 24
_MIN_HANDLE_PAYLOAD_CHARACTERS: Final = 4
_HANDLE_PAYLOAD_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{32}$")
_CURRENCY_UNITS: Final = frozenset({"currency", "currency_per_unit", "price"})
_PUBLIC_VISIBILITY_MODES: Final = frozenset(
    {
        "redacted_preview",
        "fingerprint_only",
        "public_evidence",
    },
)
_PRIVATE_VALUE_MESSAGE: Final = "public evidence contains a forbidden value class"
_PRIVATE_DELIVERY_MESSAGE: Final = "public evidence rejects the private delivery mode"
_NUMERIC_EVIDENCE_MESSAGE: Final = "public evidence cannot contain material numeric claims"
_SENSITIVE_FIELD_NAMES: Final = frozenset(
    {
        "accountid",
        "accountkey",
        "accountnumber",
        "clientid",
        "clientkey",
        "displayname",
        "orderid",
        "orderids",
        "accesstoken",
        "refreshtoken",
        "previewtoken",
        "disclaimertoken",
        "authorization",
        "deletionpreviewtoken",
    },
)
_SENSITIVE_ASSIGNMENT_PATTERN: Final = re.compile(
    r"""(?ix)
    \b(?:
        account[\s_-]?(?:id|key|number)
        |client[\s_-]?(?:id|key)
        |order[\s_-]?ids?
        |display[\s_-]?name
        |access[\s_-]?token
        |refresh[\s_-]?token
        |preview[\s_-]?token
        |disclaimer[\s_-]?token
    )\b
    \s*(?:=|:|\bis\b)\s*
    ["']?[^\s,"'};]{3,}
    """,
)
_AUTHORIZATION_PATTERN: Final = re.compile(
    r"(?i)\bauthorization\s*(?::|=)?\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}",
)
_JWT_PATTERN: Final = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
)
_URL_PATTERN: Final = re.compile(r"(?i)\b(?:https?|file)://[^\s<>'\"]+")
_LOCAL_PATH_PATTERN: Final = re.compile(
    r"""(?ix)
    (?:
        /(?:Users|Volumes|private|home|tmp|var/folders)/[^\s<>'"]*
        |[A-Z]:\\(?:Users|Temp|Documents)\\[^\s<>'"]*
    )
    """,
)
_RAW_ACCOUNT_SCOPE_PATTERNS: Final = (
    re.compile(r"^[0-9]{6,}$"),
    re.compile(
        r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    ),
    re.compile(r"(?i)^account[_-][A-Za-z0-9_-]{8,}$"),
)


class AnalysisStatus(StrEnum):
    VERIFIED = "verified"
    DEGRADED = "degraded"
    REFUSED = "refused"
    UNVERIFIED = "unverified"


class MetricClass(StrEnum):
    BROKER_REPORTED = "broker_reported"
    CALCULATED_VERIFIED = "calculated_verified"
    MODEL_OUTPUT = "model_output"
    APPROXIMATION = "approximation"
    UNAVAILABLE = "unavailable"


class VisibilityMode(StrEnum):
    PRIVATE_USER_RESULT = "private_user_result"
    INLINE_PRIVATE = "inline_private"
    LOCAL_RESOURCE_LINK = "local_resource_link"
    REDACTED_PREVIEW = "redacted_preview"
    FINGERPRINT_ONLY = "fingerprint_only"
    PUBLIC_EVIDENCE = "public_evidence"


class QualityState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    MISSING = "missing"
    INVALID = "invalid"


class HandleKind(StrEnum):
    INSTRUMENT_HANDLE = "instrument_handle"
    UNIVERSE_ID = "universe_id"
    DATASET_ID = "dataset_id"
    PORTFOLIO_SNAPSHOT_ID = "portfolio_snapshot_id"
    ANALYSIS_ID = "analysis_id"
    ARTIFACT_ID = "artifact_id"
    JOB_ID = "job_id"
    DELETION_PREVIEW_TOKEN = "deletion_preview_token"  # noqa: S105


_HANDLE_PREFIXES: Final = MappingProxyType(
    {
        HandleKind.INSTRUMENT_HANDLE: "ih",
        HandleKind.UNIVERSE_ID: "un",
        HandleKind.DATASET_ID: "ds",
        HandleKind.PORTFOLIO_SNAPSHOT_ID: "ps",
        HandleKind.ANALYSIS_ID: "an",
        HandleKind.ARTIFACT_ID: "ar",
        HandleKind.JOB_ID: "jb",
        HandleKind.DELETION_PREVIEW_TOKEN: "dp",
    },
)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PydanticCustomError(
            "analytics_utc_timestamp",
            "analytics timestamp must use UTC",
        )
    return value.astimezone(UTC)


type UtcDateTime = Annotated[datetime, AfterValidator(_require_utc)]
type NonEmptyText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1_000),
]


def _require_safe_account_scope(value: str) -> str:
    if any(pattern.fullmatch(value) is not None for pattern in _RAW_ACCOUNT_SCOPE_PATTERNS):
        raise PydanticCustomError(
            "analytics_safe_account_alias",
            "account scope must use a safe account alias",
        )
    return value


type SafeAccountScope = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1_000),
    AfterValidator(_require_safe_account_scope),
]
type ContractName = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
        min_length=1,
        max_length=128,
    ),
]
type ProofProfileId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^vp_[A-Za-z0-9][A-Za-z0-9._-]{2,127}$",
    ),
]


def _require_handle(value: str, *, kind: HandleKind) -> str:
    prefix = _HANDLE_PREFIXES[kind]
    expected_prefix = f"{prefix}_"
    if not value.startswith(expected_prefix):
        raise PydanticCustomError(
            "analytics_handle_kind",
            "analytics handle has the wrong kind",
        )
    payload = value[len(expected_prefix) :]
    if (
        _HANDLE_PAYLOAD_PATTERN.fullmatch(payload) is None
        or len(set(payload)) < _MIN_HANDLE_PAYLOAD_CHARACTERS
    ):
        raise PydanticCustomError(
            "analytics_opaque_handle",
            "analytics handle must be an opaque handle",
        )
    return value


type InstrumentHandle = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ih_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.INSTRUMENT_HANDLE)),
]
type UniverseId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^un_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.UNIVERSE_ID)),
]
type DatasetId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ds_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.DATASET_ID)),
]
type PortfolioSnapshotId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ps_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.PORTFOLIO_SNAPSHOT_ID)),
]
type AnalysisId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^an_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.ANALYSIS_ID)),
]
type ArtifactId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ar_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.ARTIFACT_ID)),
]
type JobId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^jb_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.JOB_ID)),
]
type DeletionPreviewToken = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^dp_[A-Za-z0-9_-]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.DELETION_PREVIEW_TOKEN)),
]


class _StrictAnalyticsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class _EvidenceAwareModel(_StrictAnalyticsModel):
    visibility: VisibilityMode

    @model_validator(mode="after")
    def _reject_private_public_evidence(self) -> Self:
        if self.visibility.value not in _PUBLIC_VISIBILITY_MODES:
            return self
        payload = self.model_dump(mode="json")
        if payload.get("metrics"):
            raise PydanticCustomError(
                "analytics_public_numeric_claim",
                _NUMERIC_EVIDENCE_MESSAGE,
            )
        if _contains_forbidden_public_value(payload):
            raise PydanticCustomError(
                "analytics_public_evidence_privacy",
                _PRIVATE_VALUE_MESSAGE,
            )
        return self


class DataCoverage(_StrictAnalyticsModel):
    state: QualityState
    start_at: UtcDateTime
    end_at: UtcDateTime
    row_count: int = Field(ge=0)
    expected_row_count: int | None = Field(ge=0)
    missing_row_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_coverage(self) -> Self:
        if self.end_at < self.start_at:
            raise PydanticCustomError(
                "analytics_coverage_range",
                "coverage end must not precede coverage start",
            )
        if self.expected_row_count is not None and (
            self.row_count + self.missing_row_count != self.expected_row_count
        ):
            raise PydanticCustomError(
                "analytics_coverage_count",
                "coverage row counts must match the expected count",
            )
        if self.state is QualityState.COMPLETE and self.missing_row_count != 0:
            raise PydanticCustomError(
                "analytics_complete_coverage",
                "complete coverage cannot contain missing rows",
            )
        return self


class AnalysisWarning(_StrictAnalyticsModel):
    code: ContractName
    message: NonEmptyText
    quality_state: QualityState | None = None
    next_action: NonEmptyText | None = None


class DataQuality(_StrictAnalyticsModel):
    state: QualityState
    coverage: DataCoverage
    checked_at: UtcDateTime
    warnings: tuple[AnalysisWarning, ...]

    @model_validator(mode="after")
    def _validate_complete_quality(self) -> Self:
        if (
            self.state is QualityState.COMPLETE
            and self.coverage.state is not QualityState.COMPLETE
        ):
            raise PydanticCustomError(
                "analytics_complete_quality",
                "complete quality requires complete coverage",
            )
        return self


class MetricValue(_StrictAnalyticsModel):
    metric_id: ContractName
    value: float = Field(allow_inf_nan=False)
    unit: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^[a-z][a-z0-9_/%.-]{0,63}$",
        ),
    ]
    currency: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[A-Z]{3}$"),
    ] | None
    metric_class: MetricClass
    source_timestamp: UtcDateTime
    proof_profile_id: ProofProfileId

    @model_validator(mode="after")
    def _validate_numeric_claim(self) -> Self:
        if self.unit in _CURRENCY_UNITS and self.currency is None:
            raise PydanticCustomError(
                "analytics_currency_required",
                "currency is required for a money unit",
            )
        if self.metric_class is MetricClass.UNAVAILABLE:
            raise PydanticCustomError(
                "analytics_unavailable_value",
                "an unavailable metric cannot contain a numeric value",
            )
        return self


class AnalysisProvenance(_StrictAnalyticsModel):
    dataset_id: DatasetId
    source_scope: Literal["saxo_openapi"]
    source_revision: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        ),
    ]
    source_timestamp: UtcDateTime
    proof_profile_ids: tuple[ProofProfileId, ...] = Field(min_length=1)
    checks_passed: tuple[ContractName, ...] = Field(min_length=1)
    engine_name: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^[a-z][a-z0-9_-]{0,127}$",
        ),
    ]
    engine_version: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        ),
    ]
    code_commit: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{7,64}$"),
    ]

    @model_validator(mode="after")
    def _validate_proof_lists(self) -> Self:
        if len(set(self.proof_profile_ids)) != len(self.proof_profile_ids):
            raise PydanticCustomError(
                "analytics_duplicate_proof_profile",
                "proof profile identifiers must be unique",
            )
        if len(set(self.checks_passed)) != len(self.checks_passed):
            raise PydanticCustomError(
                "analytics_duplicate_proof_check",
                "proof checks must be unique",
            )
        return self


class _AnalysisRequestBase(_StrictAnalyticsModel):
    analysis_kind: ContractName
    dataset_id: DatasetId


class MarketAnalysisRequest(_AnalysisRequestBase):
    request_kind: Literal["market"]


class InstrumentAnalysisRequest(_AnalysisRequestBase):
    request_kind: Literal["instrument"]
    instrument_handles: tuple[InstrumentHandle, ...] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def _validate_unique_instruments(self) -> Self:
        if len(set(self.instrument_handles)) != len(self.instrument_handles):
            raise PydanticCustomError(
                "analytics_duplicate_instrument",
                "instrument handles must be unique",
            )
        return self


class PortfolioAnalysisRequest(_AnalysisRequestBase):
    request_kind: Literal["portfolio"]
    portfolio_snapshot_id: PortfolioSnapshotId


type AnalysisRequest = Annotated[
    MarketAnalysisRequest | InstrumentAnalysisRequest | PortfolioAnalysisRequest,
    Field(discriminator="request_kind"),
]


class AnalysisResult(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    status: Literal[AnalysisStatus.VERIFIED] = AnalysisStatus.VERIFIED
    tool_name: ContractName
    analysis_id: AnalysisId
    analysis_kind: ContractName
    request: AnalysisRequest
    account_scope: SafeAccountScope
    as_of: UtcDateTime
    valid_until: UtcDateTime
    metrics: tuple[MetricValue, ...] = Field(min_length=1)
    warnings: tuple[AnalysisWarning, ...]
    data_quality: DataQuality
    provenance: AnalysisProvenance
    assumptions: tuple[NonEmptyText, ...]
    is_not_advice: Literal[True]
    is_not_forecast: bool
    model_distribution_only: bool
    verifies: tuple[NonEmptyText, ...]
    does_not_verify: tuple[NonEmptyText, ...]
    replayable: bool
    next_actions: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def _validate_verified_result(self) -> Self:
        if self.analysis_kind != self.request.analysis_kind:
            raise PydanticCustomError(
                "analytics_request_kind_mismatch",
                "result and request analysis kinds must match",
            )
        if self.request.dataset_id != self.provenance.dataset_id:
            raise PydanticCustomError(
                "analytics_dataset_mismatch",
                "request and provenance dataset handles must match",
            )
        if self.valid_until <= self.as_of:
            raise PydanticCustomError(
                "analytics_validity_range",
                "valid_until must follow as_of",
            )
        if self.provenance.source_timestamp > self.as_of or any(
            metric.source_timestamp > self.as_of for metric in self.metrics
        ):
            raise PydanticCustomError(
                "analytics_future_source",
                "source timestamps must not follow as_of",
            )
        if self.data_quality.state is not QualityState.COMPLETE:
            raise PydanticCustomError(
                "analytics_verified_quality",
                "verified results require complete data quality",
            )
        if any(
            metric.metric_class in {MetricClass.APPROXIMATION, MetricClass.UNAVAILABLE}
            for metric in self.metrics
        ):
            raise PydanticCustomError(
                "analytics_verified_metric_class",
                "verified results cannot contain approximate or unavailable metrics",
            )
        proof_profiles = set(self.provenance.proof_profile_ids)
        if any(
            metric.proof_profile_id not in proof_profiles for metric in self.metrics
        ):
            raise PydanticCustomError(
                "analytics_metric_proof_profile",
                "every verified metric requires a listed proof profile",
            )
        return self


class DatasetSummary(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    dataset_id: DatasetId
    account_scope: SafeAccountScope
    source_scope: Literal["saxo_openapi"]
    source_revision: NonEmptyText
    fingerprint_sha256: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
    ]
    created_at: UtcDateTime
    coverage: DataCoverage
    data_quality: DataQuality


class ArtifactSummary(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    artifact_id: ArtifactId
    analysis_id: AnalysisId
    media_type: Annotated[
        str,
        StringConstraints(
            strict=True,
            pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$",
        ),
    ]
    byte_count: int = Field(ge=0)
    sha256: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
    ]
    created_at: UtcDateTime
    description: NonEmptyText


type JobState = Literal["queued", "running", "completed", "failed", "cancelled"]


class JobSummary(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    job_id: JobId
    state: JobState
    request_fingerprint: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
    ]
    created_at: UtcDateTime
    updated_at: UtcDateTime
    analysis_id: AnalysisId | None
    artifact_ids: tuple[ArtifactId, ...]
    message: NonEmptyText | None

    @model_validator(mode="after")
    def _validate_job_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise PydanticCustomError(
                "analytics_job_timestamp",
                "job update must not precede creation",
            )
        return self


class AnalyticsRefusal(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    status: Literal[AnalysisStatus.REFUSED] = AnalysisStatus.REFUSED
    tool_name: ContractName
    analysis_kind: ContractName
    as_of: UtcDateTime
    reason_code: ContractName
    reason: NonEmptyText
    next_action: NonEmptyText
    warnings: tuple[AnalysisWarning, ...]
    verifies: tuple[NonEmptyText, ...]
    does_not_verify: tuple[NonEmptyText, ...]


class AnalyticsDegradation(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    status: Literal[AnalysisStatus.DEGRADED] = AnalysisStatus.DEGRADED
    tool_name: ContractName
    analysis_kind: ContractName
    analysis_id: AnalysisId
    dataset_id: DatasetId
    as_of: UtcDateTime
    reason_code: ContractName
    reason: NonEmptyText
    quality: DataQuality
    warnings: tuple[AnalysisWarning, ...]
    next_action: NonEmptyText
    verifies: tuple[NonEmptyText, ...]
    does_not_verify: tuple[NonEmptyText, ...]


type AnalysisOutput = (
    AnalysisResult
    | DatasetSummary
    | ArtifactSummary
    | JobSummary
    | AnalyticsRefusal
    | AnalyticsDegradation
)
type PublicEvidenceValue = (
    str
    | int
    | float
    | bool
    | None
    | list[PublicEvidenceValue]
    | dict[str, PublicEvidenceValue]
)


def new_safe_handle(kind: HandleKind) -> str:
    """Return a fresh opaque handle that exposes only its analytics kind."""
    return f"{_HANDLE_PREFIXES[kind]}_{secrets.token_urlsafe(_HANDLE_RANDOM_BYTES)}"


def validate_public_evidence(result: AnalysisOutput) -> None:
    """Reject private delivery modes, material values, and unsafe public strings."""
    if result.visibility.value not in _PUBLIC_VISIBILITY_MODES:
        raise AnalyticsPrivacyError(
            "private_delivery_mode",
            _PRIVATE_DELIVERY_MESSAGE,
        )
    payload = cast(
        "dict[str, PublicEvidenceValue]",
        result.model_dump(mode="json"),
    )
    if payload.get("metrics"):
        raise AnalyticsPrivacyError(
            "material_numeric_claim",
            _NUMERIC_EVIDENCE_MESSAGE,
        )
    if _contains_forbidden_public_value(payload):
        raise AnalyticsPrivacyError(
            "forbidden_public_value",
            _PRIVATE_VALUE_MESSAGE,
        )


def _contains_forbidden_public_value(value: PublicEvidenceValue) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _SENSITIVE_FIELD_NAMES and child not in {
                None,
                "",
                "<redacted>",
            }:
                return True
            if _contains_forbidden_public_value(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_public_value(child) for child in value)
    if not isinstance(value, str):
        return False
    return any(
        pattern.search(value) is not None
        for pattern in (
            _SENSITIVE_ASSIGNMENT_PATTERN,
            _AUTHORIZATION_PATTERN,
            _JWT_PATTERN,
            _URL_PATTERN,
            _LOCAL_PATH_PATTERN,
        )
    )


__all__ = (
    "AnalysisOutput",
    "AnalysisProvenance",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisStatus",
    "AnalysisWarning",
    "AnalyticsDegradation",
    "AnalyticsRefusal",
    "ArtifactSummary",
    "DataCoverage",
    "DataQuality",
    "DatasetSummary",
    "HandleKind",
    "InstrumentAnalysisRequest",
    "JobSummary",
    "MarketAnalysisRequest",
    "MetricClass",
    "MetricValue",
    "PortfolioAnalysisRequest",
    "QualityState",
    "VisibilityMode",
    "new_safe_handle",
    "validate_public_evidence",
)
