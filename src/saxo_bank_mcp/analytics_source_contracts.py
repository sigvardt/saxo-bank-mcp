from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Self, cast
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from saxo_bank_mcp.endpoint_registry import find_registered_endpoint

type SourceJsonValue = (
    str | int | float | bool | None | list[SourceJsonValue] | dict[str, SourceJsonValue]
)
type FrozenSourceJsonValue = (
    str
    | int
    | float
    | bool
    | None
    | tuple[FrozenSourceJsonValue, ...]
    | Mapping[str, FrozenSourceJsonValue]
)
type FrozenUnknownEnumValues = Mapping[str, tuple[str, ...]]

_REPOSITORY_CONTRACT_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data/analytics/source_contracts.json"
)
_CONTRACT_RESOURCE_DIR: Final = "_analytics_source_contracts"
_CONTRACT_RESOURCE_NAME: Final = "source_contracts.json"
_SAFE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_FIELD_PATH_PATTERN: Final = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$"
)
_QUERY_NAME_PATTERN: Final = re.compile(r"^\$?[A-Za-z][A-Za-z0-9]*$")
_PATH_PLACEHOLDER_PATTERN: Final = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_CAPTURE_REVISION_PATTERN: Final = re.compile(r"^capture:[a-f0-9]{32}$")
_ACCOUNT_SELECTOR_NAMES: Final = frozenset(
    {"AccountKey", "AccountKeys", "ClientKey"},
)
_INSTRUMENT_SELECTOR_NAMES: Final = frozenset(
    {"AssetType", "AssetTypes", "OptionRootId", "Uic", "Uics"},
)


class SourceValueType(StrEnum):
    """JSON value classes used by frozen Saxo task fields."""

    ANY = "any"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"
    DATE = "date"
    TIMESTAMP = "timestamp"
    STRING_OR_INTEGER = "string_or_integer"


class SourceResponseShape(StrEnum):
    """Supported structural Saxo response envelopes."""

    DATA_ARRAY = "data_array"
    OBJECT = "object"


class SourceObjectMode(StrEnum):
    """Whether an object has a closed schema or an explicit opaque-map escape."""

    CLOSED = "closed"
    OPAQUE_MAP = "opaque_map"


class SourceEnvelopeRowLocation(StrEnum):
    """Where source rows live inside the response envelope."""

    DATA = "data"
    ROOT = "root"


class SourceCursorValueType(StrEnum):
    """Validated value classes for returned pagination cursors."""

    INTEGER = "integer"
    TOKEN = "token"  # noqa: S105


class SourceCursorField(BaseModel):
    """One returned cursor field with explicit safe bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Literal["$skip", "$skiptoken", "$top"]
    value_type: SourceCursorValueType
    minimum: int | None = None
    maximum: int | None = None
    min_length: int | None = Field(default=None, ge=1, le=4_096)
    max_length: int | None = Field(default=None, ge=1, le=4_096)

    @model_validator(mode="after")
    def validate_cursor_bounds(self) -> Self:
        if self.value_type is SourceCursorValueType.INTEGER:
            if (
                self.minimum is None
                or self.maximum is None
                or self.minimum > self.maximum
                or self.min_length is not None
                or self.max_length is not None
            ):
                raise PydanticCustomError(
                    "integer_cursor_bounds_invalid",
                    "integer cursor fields require only ordered numeric bounds",
                )
        elif (
            self.minimum is not None
            or self.maximum is not None
            or self.min_length is None
            or self.max_length is None
            or self.min_length > self.max_length
        ):
            raise PydanticCustomError(
                "token_cursor_bounds_invalid",
                "token cursor fields require only ordered length bounds",
            )
        return self


class SourcePagination(BaseModel):
    """Allowed cursor fields and exact caller/returned combinations."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cursor_fields: tuple[SourceCursorField, ...]
    valid_combinations: tuple[tuple[str, ...], ...]
    returned_combinations: tuple[tuple[str, ...], ...]

    @model_validator(mode="after")
    def validate_cursor_contract(self) -> Self:
        field_names = tuple(field.name for field in self.cursor_fields)
        if not field_names or len(set(field_names)) != len(field_names):
            raise PydanticCustomError(
                "pagination_cursor_fields_invalid",
                "pagination cursor fields must be non-empty and unique",
            )
        field_order = {name: index for index, name in enumerate(field_names)}
        for combinations in (self.valid_combinations, self.returned_combinations):
            if not combinations or len(set(combinations)) != len(combinations):
                raise PydanticCustomError(
                    "pagination_cursor_combinations_invalid",
                    "pagination cursor combinations must be non-empty and unique",
                )
            for combination in combinations:
                if (
                    not combination
                    or len(set(combination)) != len(combination)
                    or any(name not in field_order for name in combination)
                    or tuple(sorted(combination, key=field_order.__getitem__)) != combination
                ):
                    raise PydanticCustomError(
                        "pagination_cursor_combination_invalid",
                        "pagination cursor combination must use declared fields "
                        "in declaration order",
                    )
        return self


class SourceValueSchema(BaseModel):
    """Recursive closed shape for one source value."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    value_type: SourceValueType
    nullable: bool = True
    enum_values: tuple[str, ...] = ()
    object_mode: SourceObjectMode | None = None
    properties: tuple[SourceField, ...] = ()
    items: SourceValueSchema | None = None

    @field_validator("enum_values")
    @classmethod
    def validate_enum_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item for item in value):
            raise ValueError("source field enum values must be unique and non-empty")
        return value

    @model_validator(mode="after")
    def validate_value_schema(self) -> Self:
        if self.enum_values and self.value_type is not SourceValueType.STRING:
            raise PydanticCustomError(
                "enum_type_invalid",
                "source enum fields must use the string value type",
            )
        property_names = tuple(source_field.name for source_field in self.properties)
        if len(set(property_names)) != len(property_names):
            raise PydanticCustomError(
                "source_properties_invalid",
                "closed source object properties must be unique",
            )
        if self.value_type is SourceValueType.OBJECT:
            if self.object_mode is None or self.items is not None:
                raise PydanticCustomError(
                    "source_object_schema_invalid",
                    "source objects require an explicit object mode and no array items",
                )
            if self.object_mode is SourceObjectMode.CLOSED and not self.properties:
                raise PydanticCustomError(
                    "closed_source_object_empty",
                    "closed source objects require declared properties",
                )
            if self.object_mode is SourceObjectMode.OPAQUE_MAP and self.properties:
                raise PydanticCustomError(
                    "opaque_source_object_has_properties",
                    "opaque source maps cannot also declare closed properties",
                )
        elif self.value_type is SourceValueType.ARRAY:
            if self.items is None or self.object_mode is not None or self.properties:
                raise PydanticCustomError(
                    "source_array_schema_invalid",
                    "source arrays require only an explicit item schema",
                )
        elif self.object_mode is not None or self.properties or self.items is not None:
            raise PydanticCustomError(
                "source_scalar_schema_invalid",
                "scalar source values cannot declare object or array shape",
            )
        return self


class SourceField(SourceValueSchema):
    """One named source field and its compatibility behavior."""

    name: str
    required: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _FIELD_PATH_PATTERN.fullmatch(value) is None or "." in value:
            raise ValueError("source field name is invalid")
        return value

    @model_validator(mode="after")
    def validate_field_contract(self) -> Self:
        if self.required and self.nullable:
            raise PydanticCustomError(
                "required_nullable_field",
                "required task fields must fail closed on null",
            )
        return self


SourceValueSchema.model_rebuild()


class SourceEnvelopeField(BaseModel):
    """One explicitly declared structural response-envelope field."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str
    value_type: SourceValueType
    required: bool = False
    nullable: bool = False
    items: Literal["source_row"] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _FIELD_PATH_PATTERN.fullmatch(value) is None or "." in value:
            raise ValueError("source envelope field name is invalid")
        return value

    @model_validator(mode="after")
    def validate_envelope_field(self) -> Self:
        if (self.items is None) == (self.value_type is SourceValueType.ARRAY):
            raise PydanticCustomError(
                "source_envelope_items_invalid",
                "only envelope arrays require source-row items",
            )
        if self.required and self.nullable:
            raise PydanticCustomError(
                "required_nullable_envelope_field",
                "required envelope fields must fail closed on null",
            )
        return self


class SourceResponseEnvelope(BaseModel):
    """Closed structural envelope around root or `Data` source rows."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    row_location: SourceEnvelopeRowLocation
    structural_fields: tuple[SourceEnvelopeField, ...]

    @model_validator(mode="after")
    def validate_response_envelope(self) -> Self:
        field_names = tuple(source_field.name for source_field in self.structural_fields)
        if len(set(field_names)) != len(field_names):
            raise PydanticCustomError(
                "source_envelope_fields_invalid",
                "source envelope fields must be unique",
            )
        if self.row_location is SourceEnvelopeRowLocation.ROOT:
            if self.structural_fields:
                raise PydanticCustomError(
                    "root_source_envelope_has_structural_fields",
                    "root source envelopes use the contract fields directly",
                )
        else:
            data_fields = tuple(
                source_field
                for source_field in self.structural_fields
                if source_field.name == "Data"
            )
            if (
                len(data_fields) != 1
                or not data_fields[0].required
                or data_fields[0].items != "source_row"
            ):
                raise PydanticCustomError(
                    "data_source_envelope_invalid",
                    "data source envelopes require one source-row Data array",
                )
        return self


class SourceContract(BaseModel):
    """Frozen endpoint, request, schema, and quarantine contract for one source."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    contract_id: str
    schema_version: str = "1"
    source_kind: str
    operation_id: str
    method: Literal["GET"] = "GET"
    path_template: str
    response_shape: SourceResponseShape
    path_parameters: tuple[str, ...] = ()
    query_parameters: tuple[str, ...] = ()
    response_envelope: SourceResponseEnvelope
    fields: tuple[SourceField, ...]
    stable_key_fields: tuple[str, ...] = ()
    revision_fields: tuple[str, ...] = ()
    dependent_analysis_kinds: tuple[str, ...]
    pagination: SourcePagination | None
    page_limit: int = Field(default=100, ge=1, le=1_000)
    retry_attempts: int = Field(default=3, ge=1, le=3)

    @field_validator("contract_id", "source_kind")
    @classmethod
    def validate_safe_name(cls, value: str) -> str:
        if _SAFE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("source contract name is invalid")
        return value

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1":
            raise ValueError("unsupported source contract schema version")
        return value

    @field_validator("path_template")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme
            or parsed.netloc
            or not value.startswith("/")
            or value.startswith("//")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("source contract path must be a relative path template")
        return value

    @field_validator("path_parameters", "query_parameters")
    @classmethod
    def validate_request_parameter_names(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("source request parameters must be unique")
        if any(_QUERY_NAME_PATTERN.fullmatch(item) is None for item in value):
            raise ValueError("source request parameter name is invalid")
        return value

    @field_validator("dependent_analysis_kinds")
    @classmethod
    def validate_analysis_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("dependent analysis kinds must be non-empty and unique")
        if any(_SAFE_NAME_PATTERN.fullmatch(item) is None for item in value):
            raise ValueError("dependent analysis kind is invalid")
        return value

    @model_validator(mode="after")
    def validate_registered_source(self) -> Self:
        expected_row_location = (
            SourceEnvelopeRowLocation.DATA
            if self.response_shape is SourceResponseShape.DATA_ARRAY
            else SourceEnvelopeRowLocation.ROOT
        )
        if self.response_envelope.row_location is not expected_row_location:
            raise PydanticCustomError(
                "source_response_envelope_mismatch",
                "source response envelope does not match its response shape",
            )
        registered = find_registered_endpoint(self.method, self.path_template)
        if (
            registered is None
            or registered.operation.operation_id != self.operation_id
            or registered.operation.path_template != self.path_template
            or registered.operation.method != "GET"
            or registered.operation.status != "implemented"
            or registered.operation.read_write_class != "read"
        ):
            raise PydanticCustomError(
                "source_route_not_registered_read",
                "source contract route is not a registered implemented GET/read operation",
            )
        placeholders = tuple(_PATH_PLACEHOLDER_PATTERN.findall(self.path_template))
        if placeholders != self.path_parameters:
            raise PydanticCustomError(
                "path_parameter_mismatch",
                "source path parameters do not match its route template",
            )
        operation_query_parameters = frozenset(
            match.group(1)
            for match in re.finditer(
                r"(?:^|&)(\$?[A-Za-z][A-Za-z0-9]*)=\{[^{}]+\}",
                registered.operation.query_template,
            )
        )
        if not set(self.query_parameters) <= operation_query_parameters:
            raise PydanticCustomError(
                "query_parameter_not_registered",
                "source query parameter is not registered for the operation",
            )
        if set(self.path_parameters) & set(self.query_parameters):
            raise PydanticCustomError(
                "request_parameter_collision",
                "source path and query parameter names overlap",
            )
        field_names = tuple(field.name for field in self.fields)
        if not field_names or len(set(field_names)) != len(field_names):
            raise PydanticCustomError(
                "source_fields_invalid",
                "source fields must be non-empty and unique",
            )
        if not set(self.stable_key_fields) <= set(field_names):
            raise PydanticCustomError(
                "stable_key_field_missing",
                "stable key field is not defined by the source contract",
            )
        if not set(self.revision_fields) <= set(field_names) | {"DataVersion"}:
            raise PydanticCustomError(
                "revision_field_missing",
                "revision field is not defined by the source contract",
            )
        return self


class SourceContractCatalog(BaseModel):
    """Versioned checked-in source-contract catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"]
    contracts: tuple[SourceContract, ...]

    @model_validator(mode="after")
    def validate_unique_contracts(self) -> Self:
        contract_ids = tuple(contract.contract_id for contract in self.contracts)
        if not contract_ids or len(set(contract_ids)) != len(contract_ids):
            raise PydanticCustomError(
                "source_contract_ids_invalid",
                "source contract IDs must be non-empty and unique",
            )
        return self


class SchemaComparison(BaseModel):
    """Value-free compatibility result for one Saxo response."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        validate_default=True,
    )

    compatible: bool
    structural_errors: tuple[str, ...] = ()
    missing_required_fields: tuple[str, ...] = ()
    null_required_fields: tuple[str, ...] = ()
    required_type_mismatches: tuple[str, ...] = ()
    missing_optional_fields: tuple[str, ...] = ()
    null_optional_fields: tuple[str, ...] = ()
    optional_type_mismatches: tuple[str, ...] = ()
    additive_fields: tuple[str, ...] = ()
    unknown_enum_values: FrozenUnknownEnumValues = Field(default_factory=dict)
    quarantined_analysis_kinds: tuple[str, ...] = ()

    @field_validator("unknown_enum_values")
    @classmethod
    def freeze_unknown_enum_values(
        cls,
        value: FrozenUnknownEnumValues,
    ) -> FrozenUnknownEnumValues:
        return MappingProxyType(
            {field_name: tuple(enum_values) for field_name, enum_values in sorted(value.items())}
        )

    @field_serializer("unknown_enum_values")
    def serialize_unknown_enum_values(
        self,
        value: FrozenUnknownEnumValues,
    ) -> dict[str, tuple[str, ...]]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def validate_compatibility(self) -> Self:
        has_breaking_drift = any(
            (
                self.structural_errors,
                self.missing_required_fields,
                self.null_required_fields,
                self.required_type_mismatches,
                self.optional_type_mismatches,
                self.additive_fields,
                self.unknown_enum_values,
            )
        )
        if self.compatible == has_breaking_drift:
            raise PydanticCustomError(
                "schema_comparison_state_invalid",
                "schema comparison compatibility does not match required-field drift",
            )
        if self.compatible and self.quarantined_analysis_kinds:
            raise PydanticCustomError(
                "compatible_schema_quarantined",
                "compatible source schema cannot quarantine analysis kinds",
            )
        if not self.compatible and not self.quarantined_analysis_kinds:
            raise PydanticCustomError(
                "breaking_schema_not_quarantined",
                "breaking source schema must quarantine dependent analysis kinds",
            )
        return self


class SourceCaptureContext(BaseModel):
    """One immutable provider capture shared by every contract and page."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    capture_revision: str
    captured_at: datetime
    account_scope: Literal["aggregate", "selected SIM account"]
    instrument_scope_sha256: str
    request_fingerprints: Mapping[str, str]

    @field_validator("capture_revision")
    @classmethod
    def validate_capture_revision(cls, value: str) -> str:
        if _CAPTURE_REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError("source capture revision is invalid")
        return value

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("source capture timestamp must use UTC")
        return value

    @field_validator("instrument_scope_sha256")
    @classmethod
    def validate_instrument_scope(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("instrument scope must be lowercase SHA-256")
        return value

    @field_validator("request_fingerprints")
    @classmethod
    def freeze_request_fingerprints(
        cls,
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        if not value or any(
            _SAFE_NAME_PATTERN.fullmatch(contract_id) is None
            or _SHA256_PATTERN.fullmatch(fingerprint) is None
            for contract_id, fingerprint in value.items()
        ):
            raise ValueError("source capture request fingerprints are invalid")
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("request_fingerprints")
    def serialize_request_fingerprints(
        self,
        value: Mapping[str, str],
    ) -> dict[str, str]:
        return dict(value)


class SourcePage(BaseModel):
    """One validated source page with preserved row and pagination order."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    contract_id: str
    operation_id: str
    contract_sha256: str
    source_kind: str
    capture_revision: str
    source_timestamp: datetime
    account_scope: Literal["aggregate", "selected SIM account"]
    instrument_scope_sha256: str
    request_fingerprint_sha256: str
    page_number: int = Field(ge=1)
    rows: tuple[Mapping[str, FrozenSourceJsonValue], ...]
    row_count: int = Field(ge=0)
    next_link: str | None = None
    data_version: str | int | None = None
    source_revision: str
    page_fingerprint_sha256: str
    schema_comparison: SchemaComparison

    @field_validator("contract_id")
    @classmethod
    def validate_contract_id(cls, value: str) -> str:
        if _SAFE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("source page contract ID is invalid")
        return value

    @field_validator("source_kind")
    @classmethod
    def validate_source_kind(cls, value: str) -> str:
        if _SAFE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("source page kind is invalid")
        return value

    @field_validator(
        "contract_sha256",
        "instrument_scope_sha256",
        "request_fingerprint_sha256",
    )
    @classmethod
    def validate_source_fingerprint(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("source page fingerprint must be lowercase SHA-256")
        return value

    @field_validator("capture_revision")
    @classmethod
    def validate_capture_revision(cls, value: str) -> str:
        if _CAPTURE_REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError("source page capture revision is invalid")
        return value

    @field_validator("source_timestamp")
    @classmethod
    def validate_source_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("source page timestamp must use UTC")
        return value

    @field_validator("next_link")
    @classmethod
    def validate_next_link(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme
            or parsed.netloc
            or not value.startswith("/")
            or value.startswith("//")
            or not parsed.path
        ):
            raise ValueError("source page next link must be relative")
        return value

    @field_validator("page_fingerprint_sha256")
    @classmethod
    def validate_page_fingerprint(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("source page fingerprint must be lowercase SHA-256")
        return value

    @field_validator("rows", mode="before")
    @classmethod
    def normalize_row_arrays(cls, value: object) -> object:
        return _tuplify_source_arrays(value)

    @field_validator("rows")
    @classmethod
    def freeze_rows(
        cls,
        value: tuple[Mapping[str, FrozenSourceJsonValue], ...],
    ) -> tuple[Mapping[str, FrozenSourceJsonValue], ...]:
        return tuple(_freeze_validated_source_object(row) for row in value)

    @field_serializer("rows")
    def serialize_rows(
        self,
        value: tuple[Mapping[str, FrozenSourceJsonValue], ...],
    ) -> tuple[dict[str, SourceJsonValue], ...]:
        return tuple(_thaw_source_object(row) for row in value)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.row_count != len(self.rows):
            raise PydanticCustomError(
                "source_page_row_count_mismatch",
                "source page row count does not match its rows",
            )
        if not self.schema_comparison.compatible:
            raise PydanticCustomError(
                "source_page_schema_quarantined",
                "source page cannot contain quarantined schema data",
            )
        contract = source_contracts_by_id().get(self.contract_id)
        if (
            contract is None
            or self.operation_id != contract.operation_id
            or self.source_kind != contract.source_kind
            or self.contract_sha256 != source_contract_fingerprint(contract)
        ):
            raise PydanticCustomError(
                "source_page_contract_mismatch",
                "source page metadata does not match its frozen contract",
            )
        return self


@dataclass(slots=True)
class _ObservedSchema:
    structural_errors: set[str] = field(default_factory=set)
    missing_required_fields: set[str] = field(default_factory=set)
    null_required_fields: set[str] = field(default_factory=set)
    required_type_mismatches: set[str] = field(default_factory=set)
    missing_optional_fields: set[str] = field(default_factory=set)
    null_optional_fields: set[str] = field(default_factory=set)
    optional_type_mismatches: set[str] = field(default_factory=set)
    additive_fields: set[str] = field(default_factory=set)
    unknown_enum_values: dict[str, set[str]] = field(default_factory=dict)


@cache
def load_source_contract_catalog(
    path: Path | None = None,
) -> SourceContractCatalog:
    """Load and validate the frozen checked-in source catalog."""
    return SourceContractCatalog.model_validate_json(
        _read_source_contract_catalog(path),
        strict=True,
    )


def _read_source_contract_catalog(path: Path | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    resource = files("saxo_bank_mcp").joinpath(
        _CONTRACT_RESOURCE_DIR,
        _CONTRACT_RESOURCE_NAME,
    )
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    return _REPOSITORY_CONTRACT_PATH.read_text(encoding="utf-8")


def source_contract_catalog_sha256(path: Path | None = None) -> str:
    """Fingerprint the exact installed or explicitly selected contract catalog."""
    return hashlib.sha256(_read_source_contract_catalog(path).encode()).hexdigest()


def source_contract_fingerprint(contract: SourceContract) -> str:
    """Fingerprint one complete frozen contract without retaining source values."""
    material = json.dumps(
        contract.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


@cache
def source_contracts_by_id() -> Mapping[str, SourceContract]:
    """Return immutable source contracts keyed by their opaque local ID."""
    return MappingProxyType(
        {contract.contract_id: contract for contract in load_source_contract_catalog().contracts}
    )


def build_source_capture_context(
    requests: Mapping[str, Mapping[str, object]],
    *,
    captured_at: datetime | None = None,
) -> SourceCaptureContext:
    """Bind a value-free capture identity to an exact frozen request set."""
    contracts = source_contracts_by_id()
    if not requests or any(contract_id not in contracts for contract_id in requests):
        raise ValueError("source capture names an unknown contract")
    capture_time = captured_at or datetime.now(UTC)
    if capture_time.tzinfo is None or capture_time.utcoffset() != timedelta(0):
        raise ValueError("source capture timestamp must use UTC")
    request_fingerprints = {
        contract_id: _source_request_fingerprint(contract_id, request)
        for contract_id, request in sorted(requests.items())
    }
    has_account_scope = any(
        key in _ACCOUNT_SELECTOR_NAMES and _selector_present(value)
        for request in requests.values()
        for key, value in request.items()
    )
    instrument_material = {
        contract_id: {
            key: value
            for key, value in sorted(request.items())
            if key in _INSTRUMENT_SELECTOR_NAMES
        }
        for contract_id, request in sorted(requests.items())
    }
    return SourceCaptureContext(
        capture_revision=f"capture:{uuid4().hex}",
        captured_at=capture_time,
        account_scope="selected SIM account" if has_account_scope else "aggregate",
        instrument_scope_sha256=_json_fingerprint(instrument_material),
        request_fingerprints=request_fingerprints,
    )


def source_capture_request_fingerprint(
    capture: SourceCaptureContext,
    contract_id: str,
    request: Mapping[str, object],
) -> str:
    """Validate that a provider request belongs to its immutable capture."""
    fingerprint = _source_request_fingerprint(contract_id, request)
    if capture.request_fingerprints.get(contract_id) != fingerprint:
        raise ValueError("source request does not belong to the capture")
    return fingerprint


def _source_request_fingerprint(
    contract_id: str,
    request: Mapping[str, object],
) -> str:
    return _json_fingerprint(
        {
            "contract_id": contract_id,
            "request": dict(request),
        },
    )


def _json_fingerprint(value: object) -> str:
    material = json.dumps(
        value,
        allow_nan=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _selector_present(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, str):
        return len(cast("Sequence[object]", value)) > 0
    return value is not None


def compare_source_schema(
    contract: SourceContract,
    response: Mapping[str, object],
) -> SchemaComparison:
    """Compare a response structurally while tolerating forward-compatible variation."""
    observed = _ObservedSchema()
    next_link = response.get("__next")
    if next_link is not None and not isinstance(next_link, str):
        observed.structural_errors.add("next_link_not_string")
    _observe_response_envelope(contract, response, observed)
    rows = _response_rows(contract, response, observed.structural_errors)
    _observe_top_level_revisions(contract, response, observed)
    if observed.structural_errors:
        return _comparison(contract, observed)
    _observe_fields(contract, rows, observed)
    return _comparison(contract, observed)


def _observe_response_envelope(
    contract: SourceContract,
    response: Mapping[str, object],
    observed: _ObservedSchema,
) -> None:
    if contract.response_envelope.row_location is SourceEnvelopeRowLocation.ROOT:
        return
    envelope_fields = {
        source_field.name: source_field
        for source_field in contract.response_envelope.structural_fields
    }
    observed.additive_fields.update(key for key in response if key not in envelope_fields)
    for field_name, source_field in envelope_fields.items():
        if field_name not in response:
            if source_field.required:
                observed.missing_required_fields.add(field_name)
            continue
        value = response[field_name]
        if value is None:
            if source_field.nullable:
                observed.null_optional_fields.add(field_name)
            else:
                observed.structural_errors.add(f"envelope_field_null:{field_name}")
            continue
        if not _matches_source_type(value, source_field.value_type):
            observed.structural_errors.add(f"envelope_field_type_mismatch:{field_name}")


def _observe_top_level_revisions(
    contract: SourceContract,
    response: Mapping[str, object],
    observed: _ObservedSchema,
) -> None:
    row_field_names = {source_field.name for source_field in contract.fields}
    for revision_field in contract.revision_fields:
        if revision_field in row_field_names:
            continue
        if revision_field not in response:
            observed.missing_required_fields.add(revision_field)
            continue
        value = response[revision_field]
        if value is None:
            observed.null_required_fields.add(revision_field)
        elif revision_field == "DataVersion" and (
            isinstance(value, bool) or not isinstance(value, str | int)
        ):
            observed.required_type_mismatches.add(revision_field)


def _observe_fields(
    contract: SourceContract,
    rows: Sequence[Mapping[str, object]],
    observed: _ObservedSchema,
) -> None:
    for row in rows:
        _observe_closed_object(row, contract.fields, "", observed)


def _observe_closed_object(
    value: Mapping[str, object],
    properties: tuple[SourceField, ...],
    path_prefix: str,
    observed: _ObservedSchema,
) -> None:
    properties_by_name = {source_field.name: source_field for source_field in properties}
    observed.additive_fields.update(
        _field_path(path_prefix, key) for key in value if key not in properties_by_name
    )
    for source_field in properties:
        field_path = _field_path(path_prefix, source_field.name)
        _observe_field(value, source_field, field_path, observed)


def _observe_field(
    parent: Mapping[str, object],
    source_field: SourceField,
    field_path: str,
    observed: _ObservedSchema,
) -> None:
    if source_field.name not in parent:
        target = (
            observed.missing_required_fields
            if source_field.required
            else observed.missing_optional_fields
        )
        target.add(field_path)
        return
    value = parent[source_field.name]
    if value is None:
        target = (
            observed.null_required_fields
            if source_field.required
            else observed.null_optional_fields
        )
        target.add(field_path)
        return
    if not _matches_source_type(value, source_field.value_type):
        target = (
            observed.required_type_mismatches
            if source_field.required
            else observed.optional_type_mismatches
        )
        target.add(field_path)
        return
    if (
        source_field.enum_values
        and isinstance(value, str)
        and value not in source_field.enum_values
    ):
        observed.unknown_enum_values.setdefault(field_path, set()).add(value)
    _observe_nested_value(
        value,
        source_field,
        field_path,
        observed,
        required=source_field.required,
    )


def _observe_nested_value(
    value: object,
    schema: SourceValueSchema,
    field_path: str,
    observed: _ObservedSchema,
    *,
    required: bool,
) -> None:
    if schema.value_type is SourceValueType.OBJECT:
        _observe_nested_object(
            value,
            schema,
            field_path,
            observed,
            required=required,
        )
        return
    if schema.value_type is not SourceValueType.ARRAY or schema.items is None:
        return
    _observe_nested_array(
        value,
        schema.items,
        field_path,
        observed,
        required=required,
    )


def _observe_nested_object(
    value: object,
    schema: SourceValueSchema,
    field_path: str,
    observed: _ObservedSchema,
    *,
    required: bool,
) -> None:
    if schema.object_mode is SourceObjectMode.OPAQUE_MAP or not isinstance(
        value,
        Mapping,
    ):
        return
    untyped = cast("Mapping[object, object]", value)
    if any(not isinstance(key, str) for key in untyped):
        _type_mismatch_target(observed, required=required).add(field_path)
        return
    string_mapping = {key: item for key, item in untyped.items() if isinstance(key, str)}
    _observe_closed_object(
        string_mapping,
        schema.properties,
        field_path,
        observed,
    )


def _observe_nested_array(
    value: object,
    item_schema: SourceValueSchema,
    field_path: str,
    observed: _ObservedSchema,
    *,
    required: bool,
) -> None:
    if not isinstance(value, list):
        return
    item_path = f"{field_path}[]"
    for item in cast("list[object]", value):
        if item is None:
            if not item_schema.nullable:
                _type_mismatch_target(observed, required=required).add(item_path)
            continue
        if not _matches_source_type(item, item_schema.value_type):
            _type_mismatch_target(observed, required=required).add(item_path)
            continue
        if (
            item_schema.enum_values
            and isinstance(item, str)
            and item not in item_schema.enum_values
        ):
            observed.unknown_enum_values.setdefault(item_path, set()).add(item)
        _observe_nested_value(
            item,
            item_schema,
            item_path,
            observed,
            required=required,
        )


def _type_mismatch_target(
    observed: _ObservedSchema,
    *,
    required: bool,
) -> set[str]:
    return observed.required_type_mismatches if required else observed.optional_type_mismatches


def _field_path(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def source_page_fingerprint(rows: Sequence[Mapping[str, object]]) -> str:
    """Fingerprint returned row order and values without exposing them."""
    material = json.dumps(
        list(rows),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def freeze_source_rows(
    rows: Sequence[Mapping[str, SourceJsonValue]],
) -> tuple[Mapping[str, FrozenSourceJsonValue], ...]:
    """Deep-freeze validated source rows before exposing their fingerprint."""
    return tuple(_freeze_source_object(row) for row in rows)


def _response_rows(
    contract: SourceContract,
    response: Mapping[str, object],
    structural_errors: set[str],
) -> tuple[Mapping[str, object], ...]:
    if contract.response_shape is SourceResponseShape.OBJECT:
        return (response,)
    if "Data" not in response:
        structural_errors.add("data_field_missing")
        return ()
    data = response["Data"]
    if not isinstance(data, list):
        structural_errors.add("data_field_not_array")
        return ()
    rows: list[Mapping[str, object]] = []
    for item in cast("list[object]", data):
        if not isinstance(item, dict):
            structural_errors.add("data_row_not_object")
            continue
        untyped_item = cast("dict[object, object]", item)
        if any(not isinstance(key, str) for key in untyped_item):
            structural_errors.add("data_row_not_object")
            continue
        rows.append({key: value for key, value in untyped_item.items() if isinstance(key, str)})
    return tuple(rows)


def _matches_source_type(value: object, value_type: SourceValueType) -> bool:
    if value_type is SourceValueType.ANY:
        matches = True
    elif value_type is SourceValueType.STRING:
        matches = isinstance(value, str)
    elif value_type is SourceValueType.INTEGER:
        matches = isinstance(value, int) and not isinstance(value, bool)
    elif value_type is SourceValueType.NUMBER:
        matches = isinstance(value, int | float) and not isinstance(value, bool)
    elif value_type is SourceValueType.BOOLEAN:
        matches = isinstance(value, bool)
    elif value_type is SourceValueType.OBJECT:
        matches = isinstance(value, Mapping)
    elif value_type is SourceValueType.ARRAY:
        matches = isinstance(value, list)
    elif value_type is SourceValueType.DATE:
        matches = isinstance(value, str) and _is_date(value)
    elif value_type is SourceValueType.TIMESTAMP:
        matches = isinstance(value, str) and _is_timestamp(value)
    else:
        matches = isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
    return matches


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _freeze_source_object(
    value: Mapping[str, SourceJsonValue],
) -> Mapping[str, FrozenSourceJsonValue]:
    return MappingProxyType({key: _freeze_source_value(item) for key, item in value.items()})


def _freeze_source_value(value: SourceJsonValue) -> FrozenSourceJsonValue:
    if isinstance(value, dict):
        return _freeze_source_object(value)
    if isinstance(value, list):
        return tuple(_freeze_source_value(item) for item in value)
    return value


def _freeze_validated_source_object(
    value: Mapping[str, FrozenSourceJsonValue],
) -> Mapping[str, FrozenSourceJsonValue]:
    return MappingProxyType(
        {key: _freeze_validated_source_value(item) for key, item in value.items()}
    )


def _freeze_validated_source_value(
    value: FrozenSourceJsonValue,
) -> FrozenSourceJsonValue:
    if isinstance(value, Mapping):
        return _freeze_validated_source_object(value)
    if isinstance(value, tuple):
        return tuple(_freeze_validated_source_value(item) for item in value)
    return value


def _thaw_source_object(
    value: Mapping[str, FrozenSourceJsonValue],
) -> dict[str, SourceJsonValue]:
    return {key: _thaw_source_value(item) for key, item in value.items()}


def _thaw_source_value(value: FrozenSourceJsonValue) -> SourceJsonValue:
    if isinstance(value, Mapping):
        return _thaw_source_object(value)
    if isinstance(value, tuple):
        return [_thaw_source_value(item) for item in value]
    return value


def _tuplify_source_arrays(value: object) -> object:
    if isinstance(value, Mapping):
        untyped = cast("Mapping[object, object]", value)
        return {key: _tuplify_source_arrays(item) for key, item in untyped.items()}
    if isinstance(value, list | tuple):
        sequence = cast("Sequence[object]", value)
        return tuple(_tuplify_source_arrays(item) for item in sequence)
    return value


def _comparison(
    contract: SourceContract,
    observed: _ObservedSchema,
) -> SchemaComparison:
    compatible = not any(
        (
            observed.structural_errors,
            observed.missing_required_fields,
            observed.null_required_fields,
            observed.required_type_mismatches,
            observed.optional_type_mismatches,
            observed.additive_fields,
            observed.unknown_enum_values,
        )
    )
    return SchemaComparison(
        compatible=compatible,
        structural_errors=tuple(sorted(observed.structural_errors)),
        missing_required_fields=tuple(sorted(observed.missing_required_fields)),
        null_required_fields=tuple(sorted(observed.null_required_fields)),
        required_type_mismatches=tuple(sorted(observed.required_type_mismatches)),
        missing_optional_fields=tuple(sorted(observed.missing_optional_fields)),
        null_optional_fields=tuple(sorted(observed.null_optional_fields)),
        optional_type_mismatches=tuple(sorted(observed.optional_type_mismatches)),
        additive_fields=tuple(sorted(observed.additive_fields)),
        unknown_enum_values={
            key: tuple(sorted(values))
            for key, values in sorted(observed.unknown_enum_values.items())
        },
        quarantined_analysis_kinds=(() if compatible else contract.dependent_analysis_kinds),
    )
