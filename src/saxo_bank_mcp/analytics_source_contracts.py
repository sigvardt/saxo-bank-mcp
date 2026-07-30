from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from functools import cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Self, TypeGuard, cast
from urllib.parse import urlparse

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
_STRUCTURAL_FIELDS: Final = frozenset(
    {
        "Data",
        "DataVersion",
        "MaxRows",
        "__count",
        "__next",
    }
)
_MISSING: Final = object()


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


class SourceResponseShape(StrEnum):
    """Supported structural Saxo response envelopes."""

    DATA_ARRAY = "data_array"
    OBJECT = "object"


class SourceField(BaseModel):
    """One source field and its compatibility behavior."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str
    value_type: SourceValueType
    required: bool = False
    nullable: bool = True
    enum_values: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _FIELD_PATH_PATTERN.fullmatch(value) is None:
            raise ValueError("source field name is invalid")
        return value

    @field_validator("enum_values")
    @classmethod
    def validate_enum_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item for item in value):
            raise ValueError("source field enum values must be unique and non-empty")
        return value

    @model_validator(mode="after")
    def validate_field_contract(self) -> Self:
        if self.enum_values and self.value_type is not SourceValueType.STRING:
            raise PydanticCustomError(
                "enum_type_invalid",
                "source enum fields must use the string value type",
            )
        if self.required and self.nullable:
            raise PydanticCustomError(
                "required_nullable_field",
                "required task fields must fail closed on null",
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
    fields: tuple[SourceField, ...]
    stable_key_fields: tuple[str, ...] = ()
    revision_fields: tuple[str, ...] = ()
    dependent_analysis_kinds: tuple[str, ...]
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

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    compatible: bool
    structural_errors: tuple[str, ...] = ()
    missing_required_fields: tuple[str, ...] = ()
    null_required_fields: tuple[str, ...] = ()
    required_type_mismatches: tuple[str, ...] = ()
    missing_optional_fields: tuple[str, ...] = ()
    null_optional_fields: tuple[str, ...] = ()
    optional_type_mismatches: tuple[str, ...] = ()
    additive_fields: tuple[str, ...] = ()
    unknown_enum_values: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    quarantined_analysis_kinds: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_compatibility(self) -> Self:
        has_breaking_drift = any(
            (
                self.structural_errors,
                self.missing_required_fields,
                self.null_required_fields,
                self.required_type_mismatches,
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


class SourcePage(BaseModel):
    """One validated source page with preserved row and pagination order."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    contract_id: str
    operation_id: str
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


@cache
def source_contracts_by_id() -> Mapping[str, SourceContract]:
    """Return immutable source contracts keyed by their opaque local ID."""
    return MappingProxyType(
        {contract.contract_id: contract for contract in load_source_contract_catalog().contracts}
    )


def compare_source_schema(
    contract: SourceContract,
    response: Mapping[str, object],
) -> SchemaComparison:
    """Compare a response structurally while tolerating forward-compatible variation."""
    observed = _ObservedSchema()
    next_link = response.get("__next")
    if next_link is not None and not isinstance(next_link, str):
        observed.structural_errors.add("next_link_not_string")
    rows = _response_rows(contract, response, observed.structural_errors)
    _observe_top_level_revisions(contract, response, observed)
    if observed.structural_errors:
        return _comparison(contract, observed)
    _observe_fields(contract, rows, observed)
    _observe_additive_fields(contract, rows, observed)
    return _comparison(contract, observed)


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
        for source_field in contract.fields:
            _observe_field(row, source_field, observed)


def _observe_field(
    row: Mapping[str, object],
    source_field: SourceField,
    observed: _ObservedSchema,
) -> None:
    value = _value_at_path(row, source_field.name)
    if value is _MISSING:
        target = (
            observed.missing_required_fields
            if source_field.required
            else observed.missing_optional_fields
        )
        target.add(source_field.name)
        return
    if value is None:
        target = (
            observed.null_required_fields
            if source_field.required
            else observed.null_optional_fields
        )
        target.add(source_field.name)
        return
    if not _matches_source_type(value, source_field.value_type):
        target = (
            observed.required_type_mismatches
            if source_field.required
            else observed.optional_type_mismatches
        )
        target.add(source_field.name)
        return
    if (
        source_field.enum_values
        and isinstance(value, str)
        and value not in source_field.enum_values
    ):
        observed.unknown_enum_values.setdefault(source_field.name, set()).add(value)


def _observe_additive_fields(
    contract: SourceContract,
    rows: Sequence[Mapping[str, object]],
    observed: _ObservedSchema,
) -> None:
    known_top_level_fields = {field.name.split(".", maxsplit=1)[0] for field in contract.fields}
    observed.additive_fields.update(
        {
            key
            for row in rows
            for key in row
            if key not in known_top_level_fields and key not in _STRUCTURAL_FIELDS
        }
    )


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


def _value_at_path(row: Mapping[str, object], field_path: str) -> object:
    current: object = row
    for part in field_path.split("."):
        if not _is_string_object_dict(current):
            return _MISSING
        if part not in current:
            return _MISSING
        current = current[part]
    return current


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    untyped = cast("dict[object, object]", value)
    return all(isinstance(key, str) for key in untyped)


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
    else:
        matches = isinstance(value, str) and _is_timestamp(value)
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
