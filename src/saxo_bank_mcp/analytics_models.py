from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self, cast
from uuid import RFC_4122, UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from saxo_bank_mcp.analytics_errors import AnalyticsPrivacyError

_SCHEMA_VERSION: Final = "1"
_OPAQUE_HANDLE_UUID_VERSION: Final = 4
_HANDLE_PAYLOAD_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
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
_SENSITIVE_FIELD_PATTERN_TEXT: Final = r"""(?:
    account[\s_-]?(?:id|key|number)
    |client[\s_-]?(?:id|key)
    |order[\s_-]?ids?
    |display[\s_-]?name
    |access[\s_-]?token
    |refresh[\s_-]?token
    |deletion[\s_-]?preview[\s_-]?token
    |preview[\s_-]?token
    |disclaimer[\s_-]?token
)"""
_SENSITIVE_ASSIGNMENT_RHS_PATTERN_TEXT: Final = r"""(?P<value>
    ["'][^"'\r\n]*["']
    |[^\r\n;.!?]+
)"""
_SENSITIVE_ASSIGNMENT_PATTERN: Final = re.compile(
    r"(?ix)\b"
    + _SENSITIVE_FIELD_PATTERN_TEXT
    + r"""\b
    ["']?\s*(?:=|:)\s*
    """
    + _SENSITIVE_ASSIGNMENT_RHS_PATTERN_TEXT,
)
_SENSITIVE_IS_ASSIGNMENT_PATTERN: Final = re.compile(
    r"(?ix)\b"
    + _SENSITIVE_FIELD_PATTERN_TEXT
    + r"""\b
    ["']?\s+\bis\b\s+
    """
    + _SENSITIVE_ASSIGNMENT_RHS_PATTERN_TEXT,
)
_DISPLAY_NAME_IS_ASSIGNMENT_PATTERN: Final = re.compile(
    r"""(?ix)\bdisplay[\s_-]?name\b
    ["']?\s+\bis\b\s+
    ["']?(?P<value>[^\r\n.;]+)
    """,
)
_SAFE_SENSITIVE_VALUE_SENTINELS: Final = frozenset(
    {
        "excluded",
        "none",
        "not_applicable",
        "not_available",
        "not_provided",
        "null",
        "omitted",
        "redacted",
        "unavailable",
        "unknown",
    }
)
_AUTHORIZATION_PATTERN: Final = re.compile(
    r"(?i)\bauthorization\s*(?::|=)?\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}",
)
_JWT_PATTERN: Final = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
)
_RAW_ACCOUNT_IDENTIFIER_PATTERN: Final = re.compile(
    r"""(?ix)
    (?<![A-Z0-9])
    raw[\s_-]+account[\s_-]+identifier[\s_-]+
    [A-Z0-9][A-Z0-9_-]{5,}
    (?![A-Z0-9_-])
    """,
)
_DELETION_PREVIEW_TOKEN_PATTERN: Final = re.compile(
    r"(?<![A-Za-z0-9_])dp_[0-9a-f]{32}(?![A-Za-z0-9_])",
)
_URL_PATTERN: Final = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>'\"]+",
)
_LOCAL_PATH_PATTERN: Final = re.compile(
    r"""(?ix)
    (?:
        (?<![A-Z0-9._~:/-])/(?!/)[^\s<>'"]+
        |(?<![A-Z0-9])[A-Z]:[\\/][^\s<>'"]+
        |(?<![\\/])\\\\[A-Z0-9._$-]+[\\/][^\s<>'"]+
        |(?<![A-Z0-9._~:/-])~/(?!/)[^\s<>'"]+
    )
    """,
)
_ISO_4217_CURRENCY_CODES: Final[frozenset[str]] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND
    BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU
    CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
    GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA
    MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD
    OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
    SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD
    TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU
    XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW
    ZWG ZWL
    """.split()  # noqa: SIM905
)
_CURRENCY_NAMES: Final[frozenset[str]] = frozenset(
    """
    baht dinar dinars dirham dirhams dollar dollars dong euro euros forint
    forints franc francs hryvnia hryvnias krona krone kroner kronor koruna
    lei leu lev leva lira naira peso pesos pound pounds rand reais renminbi
    rial rials ringgit riyal riyals rouble roubles ruble rubles rupee rupees
    shekel shekels shilling shillings sterling taka tenge won yen yuan zloty
    """.split()  # noqa: SIM905
)
_CURRENCY_CODE_PATTERN_TEXT: Final = "|".join(sorted(_ISO_4217_CURRENCY_CODES))
_CURRENCY_NAME_PATTERN_TEXT: Final = "|".join(sorted(_CURRENCY_NAMES))
_MONEY_AMOUNT_PATTERN_TEXT: Final = (
    r"[-+]?(?:\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\.\d+)?"
)
_DIRECT_MONETARY_VALUE_PATTERN: Final = re.compile(
    rf"""(?x)
    (?:
        \b(?:{_CURRENCY_CODE_PATTERN_TEXT})\b\s*{_MONEY_AMOUNT_PATTERN_TEXT}
        |
        {_MONEY_AMOUNT_PATTERN_TEXT}\s*
        \b(?:{_CURRENCY_CODE_PATTERN_TEXT})\b
        |
        {_MONEY_AMOUNT_PATTERN_TEXT}\s*
        \b(?i:(?:{_CURRENCY_NAME_PATTERN_TEXT}))\b
        |
        [€£$¥₹]\s*{_MONEY_AMOUNT_PATTERN_TEXT}
        |
        {_MONEY_AMOUNT_PATTERN_TEXT}\s*[€£$¥₹]
    )
    """,
)
_FINANCIAL_CONTEXT_PATTERN_TEXT: Final = r"""(?:
    (?:account|cash|market|portfolio|position)\s+(?:balance|value)
    |amount
    |balance
    |cost
    |fee
    |loss
    |notional
    |p&l
    |pnl
    |price
    |proceeds
    |profit
)"""
_FINANCIAL_LABEL_BINDING_PATTERN_TEXT: Final = (
    r"(?:\s*[:=]\s*|\s+(?:is|was)\s+|\s+)"
)
_CONTEXTUAL_CURRENCY_NAME_VALUE_PATTERN: Final = re.compile(
    rf"""(?ix)
    \b{_FINANCIAL_CONTEXT_PATTERN_TEXT}\b
    {_FINANCIAL_LABEL_BINDING_PATTERN_TEXT}
    \b(?:{_CURRENCY_NAME_PATTERN_TEXT})\b
    \s*{_MONEY_AMOUNT_PATTERN_TEXT}
    """,
)
_PERCENTAGE_VALUE_PATTERN: Final = re.compile(
    r"""(?ix)
    (?<![A-Z0-9])
    [-+]?(?:\d+(?:[.,]\d+)?)
    \s*(?:%|\bpercent(?:age)?(?:\s+points?)?\b)
    """,
)
_AGGREGATE_ACCOUNT_SCOPES: Final = frozenset(
    {
        "aggregate",
        "selected SIM account",
    },
)
_ACCOUNT_ALIAS_PATTERN_TEXT: Final = (
    r"^aa_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$"
)
_ACCOUNT_ALIAS_PATTERN: Final = re.compile(_ACCOUNT_ALIAS_PATTERN_TEXT)


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


class ValueUnitClass(StrEnum):
    MONETARY = "monetary"
    RATIO = "ratio"
    PERCENTAGE = "percentage"
    COUNT = "count"
    DURATION = "duration"
    QUANTITY = "quantity"


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


_NON_MONETARY_UNITS: Final = MappingProxyType(
    {
        ValueUnitClass.RATIO: frozenset({"ratio"}),
        ValueUnitClass.PERCENTAGE: frozenset(
            {"percent", "percentage_points", "basis_points"}
        ),
        ValueUnitClass.COUNT: frozenset({"count"}),
        ValueUnitClass.DURATION: frozenset(
            {"seconds", "minutes", "hours", "days", "years"}
        ),
        ValueUnitClass.QUANTITY: frozenset({"shares", "contracts", "units"}),
    }
)
_REQUIRED_PROOF_CHECKS: Final = frozenset(
    {
        "golden",
        "property",
        "independent_reference",
        "saxo_reconciliation",
        "sim_end_to_end",
    }
)


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
    if value in _AGGREGATE_ACCOUNT_SCOPES:
        return value
    if _ACCOUNT_ALIAS_PATTERN.fullmatch(value) is not None:
        opaque_uuid = UUID(hex=value.removeprefix("aa_"))
        if (
            opaque_uuid.version == _OPAQUE_HANDLE_UUID_VERSION
            and opaque_uuid.variant == RFC_4122
        ):
            return value
    raise PydanticCustomError(
        "analytics_safe_account_alias",
        "account scope must use a safe account alias or aggregate",
    )


type SafeAccountScope = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1_000),
    AfterValidator(_require_safe_account_scope),
    WithJsonSchema(
        {
            "anyOf": [
                {
                    "enum": sorted(_AGGREGATE_ACCOUNT_SCOPES),
                    "type": "string",
                },
                {
                    "pattern": _ACCOUNT_ALIAS_PATTERN_TEXT,
                    "type": "string",
                },
            ],
        },
    ),
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
type SourceRevision = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
type Sha256Fingerprint = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
]
type EngineName = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9_-]{0,127}$",
    ),
]
type EngineVersion = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    ),
]
type CodeCommit = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-f0-9]{7,64}$"),
]
type ProofCheck = Literal[
    "golden",
    "property",
    "independent_reference",
    "saxo_reconciliation",
    "sim_end_to_end",
]


def _require_iso_4217_currency(value: str) -> str:
    if value not in _ISO_4217_CURRENCY_CODES:
        raise PydanticCustomError(
            "analytics_iso_4217_currency",
            "currency must be a supported ISO 4217 code",
        )
    return value


type IsoCurrencyCode = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z]{3}$"),
    AfterValidator(_require_iso_4217_currency),
    WithJsonSchema(
        {
            "enum": sorted(_ISO_4217_CURRENCY_CODES),
            "type": "string",
        },
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
    if _HANDLE_PAYLOAD_PATTERN.fullmatch(payload) is None:
        raise PydanticCustomError(
            "analytics_opaque_handle",
            "analytics handle must be an opaque handle",
        )
    opaque_uuid = UUID(hex=payload)
    if (
        opaque_uuid.version != _OPAQUE_HANDLE_UUID_VERSION
        or opaque_uuid.variant != RFC_4122
    ):
        raise PydanticCustomError(
            "analytics_opaque_handle",
            "analytics handle must be an opaque handle",
        )
    return value


type InstrumentHandle = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ih_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.INSTRUMENT_HANDLE)),
]
type UniverseId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^un_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.UNIVERSE_ID)),
]
type DatasetId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ds_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.DATASET_ID)),
]
type PortfolioSnapshotId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ps_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.PORTFOLIO_SNAPSHOT_ID)),
]
type AnalysisId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^an_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.ANALYSIS_ID)),
]
type ArtifactId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^ar_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.ARTIFACT_ID)),
]
type JobId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^jb_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.JOB_ID)),
]
type DeletionPreviewToken = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^dp_[0-9a-f]{32}$"),
    AfterValidator(partial(_require_handle, kind=HandleKind.DELETION_PREVIEW_TOKEN)),
]


class _StrictAnalyticsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
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
    unit_class: ValueUnitClass
    currency: IsoCurrencyCode | None
    metric_class: MetricClass
    source_timestamp: UtcDateTime
    proof_profile_id: ProofProfileId

    @model_validator(mode="after")
    def _validate_numeric_claim(self) -> Self:
        if self.unit_class is ValueUnitClass.MONETARY and self.currency is None:
            raise PydanticCustomError(
                "analytics_currency_required",
                "currency is required for a money unit",
            )
        if self.unit_class is not ValueUnitClass.MONETARY:
            if self.currency is not None:
                raise PydanticCustomError(
                    "analytics_currency_forbidden",
                    "currency is forbidden for a non-monetary unit class",
                )
            allowed_units = _NON_MONETARY_UNITS[self.unit_class]
            if self.unit not in allowed_units:
                raise PydanticCustomError(
                    "analytics_unit_class_mismatch",
                    "unit is not allowed for the selected unit class",
                )
        if self.metric_class is MetricClass.UNAVAILABLE:
            raise PydanticCustomError(
                "analytics_unavailable_value",
                "an unavailable metric cannot contain a numeric value",
            )
        return self


class ProofSourceBinding(_StrictAnalyticsModel):
    source_scope: Literal["saxo_openapi"]
    source_revision: SourceRevision
    source_contract_sha256: Sha256Fingerprint


class ProofEngineBinding(_StrictAnalyticsModel):
    engine_name: EngineName
    engine_version: EngineVersion
    code_commit: CodeCommit


class ActiveProofReceipt(_StrictAnalyticsModel):
    state: Literal["active"]
    analysis_kind: ContractName
    schema_version: Literal["1"]
    source_binding: ProofSourceBinding
    engine_binding: ProofEngineBinding
    proof_profile_id: ProofProfileId
    checks_passed: tuple[ProofCheck, ...] = Field(
        min_length=5,
        max_length=5,
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("checks_passed", mode="before")
    @classmethod
    def _validate_required_checks(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise PydanticCustomError(
                "analytics_proof_checks",
                "active proof receipt requires the exact Task 3 proof checks",
            )
        supplied_checks = cast("list[object] | tuple[object, ...]", value)
        if (
            len(supplied_checks) != len(_REQUIRED_PROOF_CHECKS)
            or any(check not in _REQUIRED_PROOF_CHECKS for check in supplied_checks)
            or any(check not in supplied_checks for check in _REQUIRED_PROOF_CHECKS)
        ):
            raise PydanticCustomError(
                "analytics_proof_checks",
                "active proof receipt requires the exact Task 3 proof checks",
            )
        return tuple(supplied_checks)


class AnalysisProvenance(_StrictAnalyticsModel):
    dataset_id: DatasetId
    source_scope: Literal["saxo_openapi"]
    source_revision: SourceRevision
    source_contract_sha256: Sha256Fingerprint
    source_timestamp: UtcDateTime
    proof_receipts: tuple[ActiveProofReceipt, ...] = Field(min_length=1)
    engine_name: EngineName
    engine_version: EngineVersion
    code_commit: CodeCommit

    @model_validator(mode="after")
    def _validate_proof_lists(self) -> Self:
        proof_profile_ids = tuple(
            receipt.proof_profile_id for receipt in self.proof_receipts
        )
        if len(set(proof_profile_ids)) != len(proof_profile_ids):
            raise PydanticCustomError(
                "analytics_duplicate_proof_profile",
                "proof profile identifiers must be unique",
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

    def _validate_request_binding(self) -> None:
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

    def _validate_timestamps(self) -> None:
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
        if self.data_quality.coverage.end_at > self.as_of:
            raise PydanticCustomError(
                "analytics_future_coverage",
                "source coverage must not end after as_of",
            )

    def _validate_verified_values(self) -> None:
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

    def _validate_proof_receipt_bindings(self) -> None:
        expected_source_binding = ProofSourceBinding(
            source_scope=self.provenance.source_scope,
            source_revision=self.provenance.source_revision,
            source_contract_sha256=self.provenance.source_contract_sha256,
        )
        expected_engine_binding = ProofEngineBinding(
            engine_name=self.provenance.engine_name,
            engine_version=self.provenance.engine_version,
            code_commit=self.provenance.code_commit,
        )
        for receipt in self.provenance.proof_receipts:
            if receipt.analysis_kind != self.analysis_kind:
                raise PydanticCustomError(
                    "analytics_proof_analysis_kind",
                    "proof receipt analysis kind must match the result",
                )
            if receipt.schema_version != self.schema_version:
                raise PydanticCustomError(
                    "analytics_proof_schema_version",
                    "proof receipt schema version must match the result",
                )
            if receipt.source_binding != expected_source_binding:
                raise PydanticCustomError(
                    "analytics_proof_source_binding",
                    "proof receipt source binding must match provenance",
                )
            if receipt.engine_binding != expected_engine_binding:
                raise PydanticCustomError(
                    "analytics_proof_engine_binding",
                    "proof receipt engine binding must match provenance",
                )
        proof_profiles = {
            receipt.proof_profile_id for receipt in self.provenance.proof_receipts
        }
        if any(
            metric.proof_profile_id not in proof_profiles for metric in self.metrics
        ):
            raise PydanticCustomError(
                "analytics_metric_proof_profile",
                "every verified metric requires a listed proof profile",
            )

    @model_validator(mode="after")
    def _validate_verified_result(self) -> Self:
        self._validate_request_binding()
        self._validate_timestamps()
        self._validate_verified_values()
        self._validate_proof_receipt_bindings()
        return self


class DatasetSummary(_EvidenceAwareModel):
    schema_version: Literal["1"] = _SCHEMA_VERSION
    dataset_id: DatasetId
    account_scope: SafeAccountScope
    source_scope: Literal["saxo_openapi"]
    source_revision: SourceRevision
    fingerprint_sha256: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
    ]
    created_at: UtcDateTime
    coverage: DataCoverage
    data_quality: DataQuality

    @model_validator(mode="after")
    def _validate_coverage_contract(self) -> Self:
        if self.coverage != self.data_quality.coverage:
            raise PydanticCustomError(
                "analytics_dataset_coverage",
                "dataset coverage must match data-quality coverage",
            )
        return self


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
    return f"{_HANDLE_PREFIXES[kind]}_{uuid4().hex}"


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


def _is_safe_sensitive_value(value: str) -> bool:
    candidate = value.strip().strip("\"'").rstrip(".!?")
    normalized = re.sub(r"\s+", "_", candidate.casefold().replace("-", "_"))
    return normalized in _SAFE_SENSITIVE_VALUE_SENTINELS


def _string_contains_forbidden_public_value(value: str) -> bool:
    if any(
        pattern.search(value) is not None
        for pattern in (
            _AUTHORIZATION_PATTERN,
            _JWT_PATTERN,
            _RAW_ACCOUNT_IDENTIFIER_PATTERN,
            _DELETION_PREVIEW_TOKEN_PATTERN,
            _URL_PATTERN,
            _LOCAL_PATH_PATTERN,
            _DIRECT_MONETARY_VALUE_PATTERN,
            _CONTEXTUAL_CURRENCY_NAME_VALUE_PATTERN,
            _PERCENTAGE_VALUE_PATTERN,
        )
    ):
        return True
    if any(
        not _is_safe_sensitive_value(match.group("value"))
        for match in _SENSITIVE_ASSIGNMENT_PATTERN.finditer(value)
    ):
        return True
    if any(
        not _is_safe_sensitive_value(match.group("value"))
        for match in _DISPLAY_NAME_IS_ASSIGNMENT_PATTERN.finditer(value)
    ):
        return True
    return any(
        not _is_safe_sensitive_value(match.group("value"))
        for match in _SENSITIVE_IS_ASSIGNMENT_PATTERN.finditer(value)
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
    return isinstance(value, str) and _string_contains_forbidden_public_value(
        value
    )


__all__ = (
    "ActiveProofReceipt",
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
    "ProofEngineBinding",
    "ProofSourceBinding",
    "QualityState",
    "ValueUnitClass",
    "VisibilityMode",
    "new_safe_handle",
    "validate_public_evidence",
)
