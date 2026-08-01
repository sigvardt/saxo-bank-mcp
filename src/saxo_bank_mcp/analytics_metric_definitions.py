from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.analytics_models import MetricClass, ValueUnitClass

_CATALOG_RESOURCE: Final = "_analytics_metric_definitions/metric_definitions.json"
_CATALOG_SOURCE_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data/analytics/metric_definitions.json"
)
_SAFE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_VERSION_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")

type ToleranceMode = Literal[
    "exact",
    "absolute_relative",
    "currency_minor_unit",
    "model_calibration",
]


class MetricDefinitionError(RuntimeError):
    """Raised when the checked-in metric-definition catalog is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class FxTreatment(_StrictModel):
    """Exact Saxo-only currency conversion rule for one metric family."""

    source: str = Field(min_length=1, max_length=1_000)
    direction: str = Field(min_length=1, max_length=1_000)
    timestamp: str = Field(min_length=1, max_length=1_000)


class NumericTolerance(_StrictModel):
    """Machine-readable comparison tolerance with no global fallback."""

    mode: ToleranceMode
    absolute: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    relative: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency_minor_unit_fraction: float | None = Field(
        default=None,
        gt=0,
        le=1,
        allow_inf_nan=False,
    )
    rounding: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def _validate_mode(self) -> Self:
        if self.mode == "exact" and (
            self.absolute not in {None, 0.0}
            or self.relative not in {None, 0.0}
            or self.currency_minor_unit_fraction is not None
        ):
            raise ValueError("exact tolerance cannot widen equality")
        if self.mode in {"absolute_relative", "model_calibration"} and (
            self.absolute is None or self.relative is None
        ):
            raise ValueError("floating tolerances require absolute and relative bounds")
        if self.mode == "currency_minor_unit" and self.currency_minor_unit_fraction is None:
            raise ValueError("currency tolerance requires a minor-unit fraction")
        return self


class MetricDefinitionBinding(_StrictModel):
    """One proof-profile binding to an exact metric-definition version."""

    metric_id: str
    definition_version: str

    @model_validator(mode="after")
    def _validate_names(self) -> Self:
        _require_safe_name(self.metric_id, "metric identifier")
        _require_version(self.definition_version, "metric definition version")
        return self


class MetricDefinition(_StrictModel):
    """Complete versioned meaning of one material analytics metric."""

    metric_id: str
    definition_version: str
    meaning: str = Field(min_length=1, max_length=2_000)
    formula: str = Field(min_length=1, max_length=4_000)
    input_units: tuple[str, ...] = Field(min_length=1)
    output_unit: str = Field(min_length=1, max_length=128)
    unit_class: ValueUnitClass
    default_metric_class: MetricClass
    sign_convention: str = Field(min_length=1, max_length=2_000)
    timing_convention: str = Field(min_length=1, max_length=2_000)
    cash_flow_treatment: str = Field(min_length=1, max_length=2_000)
    fx_treatment: FxTreatment
    missing_data_policy: str = Field(min_length=1, max_length=2_000)
    minimum_sample_count: int = Field(ge=0)
    minimum_coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    tolerance: NumericTolerance
    independent_reference: str = Field(min_length=1, max_length=2_000)
    broker_reconciliation: str = Field(min_length=1, max_length=2_000)
    analysis_kinds: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_definition(self) -> Self:
        _require_safe_name(self.metric_id, "metric identifier")
        _require_version(self.definition_version, "metric definition version")
        if len(set(self.input_units)) != len(self.input_units):
            raise ValueError("metric input units must be unique")
        if any(not value.strip() for value in self.input_units):
            raise ValueError("metric input units must be non-empty")
        if len(set(self.analysis_kinds)) != len(self.analysis_kinds):
            raise ValueError("metric analysis kinds must be unique")
        for kind in self.analysis_kinds:
            _require_safe_name(kind, "analysis kind")
        return self


class MetricDefinitionCatalog(_StrictModel):
    """Expanded source-controlled catalog used by proof registration."""

    schema_version: Literal["1"]
    catalog_version: str
    production_metric_ids: tuple[str, ...] = Field(min_length=1)
    definitions: tuple[MetricDefinition, ...]
    fingerprint_sha256: str

    @model_validator(mode="after")
    def _validate_catalog(self) -> Self:
        _require_version(self.catalog_version, "metric catalog version")
        for metric_id in self.production_metric_ids:
            _require_safe_name(metric_id, "production metric identifier")
        if len(set(self.production_metric_ids)) != len(self.production_metric_ids):
            raise ValueError("production metric identifiers must be unique")
        definition_ids = tuple(definition.metric_id for definition in self.definitions)
        if len(set(definition_ids)) != len(definition_ids):
            raise ValueError("metric definition identifiers must be unique")
        if set(definition_ids) != set(self.production_metric_ids):
            raise ValueError("production metrics and definitions must match exactly")
        if _SHA256_PATTERN.fullmatch(self.fingerprint_sha256) is None:
            raise ValueError("metric catalog fingerprint is invalid")
        expected = _catalog_fingerprint(
            schema_version=self.schema_version,
            catalog_version=self.catalog_version,
            production_metric_ids=self.production_metric_ids,
            definitions=self.definitions,
        )
        if self.fingerprint_sha256 != expected:
            raise ValueError("metric catalog fingerprint does not match its definitions")
        return self

    @classmethod
    def from_definitions(
        cls,
        *,
        catalog_version: str,
        production_metric_ids: Sequence[str],
        definitions: Sequence[MetricDefinition],
    ) -> Self:
        """Build a catalog whose fingerprint changes on every material definition edit."""
        production = tuple(production_metric_ids)
        expanded = tuple(definitions)
        return cls(
            schema_version="1",
            catalog_version=catalog_version,
            production_metric_ids=production,
            definitions=expanded,
            fingerprint_sha256=_catalog_fingerprint(
                schema_version="1",
                catalog_version=catalog_version,
                production_metric_ids=production,
                definitions=expanded,
            ),
        )

    def by_id(self) -> Mapping[str, MetricDefinition]:
        """Return an immutable lookup without changing catalog order."""
        return MappingProxyType(
            {definition.metric_id: definition for definition in self.definitions},
        )


class _MetricPolicy(_StrictModel):
    policy_id: str
    input_units: tuple[str, ...] = Field(min_length=1)
    output_unit: str
    unit_class: ValueUnitClass
    default_metric_class: MetricClass
    sign_convention: str
    timing_convention: str
    cash_flow_treatment: str
    fx_treatment: FxTreatment
    missing_data_policy: str
    minimum_sample_count: int = Field(ge=0)
    minimum_coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    tolerance: NumericTolerance
    independent_reference: str
    broker_reconciliation: str

    @model_validator(mode="after")
    def _validate_policy_id(self) -> Self:
        _require_safe_name(self.policy_id, "metric policy identifier")
        return self


class _MetricEntry(_StrictModel):
    metric_id: str
    meaning: str
    formula: str
    analysis_kinds: tuple[str, ...] = ()
    output_unit: str | None = None
    unit_class: ValueUnitClass | None = None
    default_metric_class: MetricClass | None = None
    sign_convention: str | None = None
    timing_convention: str | None = None
    cash_flow_treatment: str | None = None
    fx_treatment: FxTreatment | None = None
    missing_data_policy: str | None = None
    minimum_sample_count: int | None = Field(default=None, ge=0)
    minimum_coverage_ratio: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    tolerance: NumericTolerance | None = None
    independent_reference: str | None = None
    broker_reconciliation: str | None = None


class _MetricGroup(_StrictModel):
    policy_id: str
    definition_version: str
    analysis_kinds: tuple[str, ...] = Field(min_length=1)
    metrics: tuple[_MetricEntry, ...] = Field(min_length=1)


class _MetricCatalogDocument(_StrictModel):
    schema_version: Literal["1"]
    catalog_version: str
    production_metric_ids: tuple[str, ...] = Field(min_length=1)
    policies: tuple[_MetricPolicy, ...] = Field(min_length=1)
    groups: tuple[_MetricGroup, ...] = Field(min_length=1)


def load_metric_definition_catalog(path: Path | None = None) -> MetricDefinitionCatalog:
    """Load and expand the fixed machine-readable metric-definition catalog."""
    raw = _read_catalog(path)
    try:
        document = _MetricCatalogDocument.model_validate_json(raw, strict=True)
    except ValidationError as error:
        raise MetricDefinitionError("metric definition catalog is invalid") from error
    policies = {policy.policy_id: policy for policy in document.policies}
    if len(policies) != len(document.policies):
        raise MetricDefinitionError("metric definition policies must be unique")
    definitions: list[MetricDefinition] = []
    for group in document.groups:
        policy = policies.get(group.policy_id)
        if policy is None:
            raise MetricDefinitionError("metric definition group names an absent policy")
        _require_version(group.definition_version, "metric definition version")
        for entry in group.metrics:
            analysis_kinds = entry.analysis_kinds or group.analysis_kinds
            definitions.append(
                MetricDefinition(
                    metric_id=entry.metric_id,
                    definition_version=group.definition_version,
                    meaning=entry.meaning,
                    formula=entry.formula,
                    input_units=policy.input_units,
                    output_unit=entry.output_unit or policy.output_unit,
                    unit_class=entry.unit_class or policy.unit_class,
                    default_metric_class=(
                        entry.default_metric_class or policy.default_metric_class
                    ),
                    sign_convention=entry.sign_convention or policy.sign_convention,
                    timing_convention=entry.timing_convention or policy.timing_convention,
                    cash_flow_treatment=(entry.cash_flow_treatment or policy.cash_flow_treatment),
                    fx_treatment=entry.fx_treatment or policy.fx_treatment,
                    missing_data_policy=(entry.missing_data_policy or policy.missing_data_policy),
                    minimum_sample_count=(
                        entry.minimum_sample_count
                        if entry.minimum_sample_count is not None
                        else policy.minimum_sample_count
                    ),
                    minimum_coverage_ratio=(
                        entry.minimum_coverage_ratio
                        if entry.minimum_coverage_ratio is not None
                        else policy.minimum_coverage_ratio
                    ),
                    tolerance=entry.tolerance or policy.tolerance,
                    independent_reference=(
                        entry.independent_reference or policy.independent_reference
                    ),
                    broker_reconciliation=(
                        entry.broker_reconciliation or policy.broker_reconciliation
                    ),
                    analysis_kinds=analysis_kinds,
                ),
            )
    try:
        return MetricDefinitionCatalog.from_definitions(
            catalog_version=document.catalog_version,
            production_metric_ids=document.production_metric_ids,
            definitions=definitions,
        )
    except ValidationError as error:
        raise MetricDefinitionError("metric definition catalog is invalid") from error


def _read_catalog(path: Path | None) -> str:
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise MetricDefinitionError("metric definition catalog cannot be read") from error
    if _CATALOG_SOURCE_PATH.is_file():
        return _CATALOG_SOURCE_PATH.read_text(encoding="utf-8")
    try:
        return files("saxo_bank_mcp").joinpath(_CATALOG_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
        raise MetricDefinitionError("metric definition catalog cannot be read") from error


def _catalog_fingerprint(
    *,
    schema_version: str,
    catalog_version: str,
    production_metric_ids: Sequence[str],
    definitions: Sequence[MetricDefinition],
) -> str:
    payload = {
        "catalog_version": catalog_version,
        "definitions": [definition.model_dump(mode="json") for definition in definitions],
        "production_metric_ids": list(production_metric_ids),
        "schema_version": schema_version,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_safe_name(value: str, label: str) -> None:
    if _SAFE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_version(value: str, label: str) -> None:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
