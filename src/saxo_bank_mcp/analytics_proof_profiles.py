from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    MetricDefinitionCatalog,
)
from saxo_bank_mcp.analytics_source_contracts import (
    SourceField,
    SourceValueSchema,
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)

_CATALOG_RESOURCE: Final = "_analytics_proof_profiles/proof_profiles.json"
_CATALOG_SOURCE_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data/analytics/proof_profiles.json"
)
_SAFE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_VERSION_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_FIELD_PATH_PATTERN: Final = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$",
)
_QUARANTINE_LOCK: Final = RLock()
_RUNTIME_QUARANTINES: dict[str, str] = {}
_MAX_REASON_CODE_LENGTH: Final = 127
_PUBLIC_QUARANTINE_REASONS: Final = frozenset(
    {
        "reconciliation_mismatch",
        "schema_drift",
    },
)


class ProfileActivationState(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"


class ProofState(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    QUARANTINED = "quarantined"
    REFUSED = "refused"


class ProofProfileError(RuntimeError):
    """Raised when a checked-in proof-profile catalog is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class EngineProofBinding(_StrictModel):
    engine_name: str
    engine_version: str
    code_commit: str

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.engine_name, "engine name")
        _require_version(self.engine_version, "engine version")
        _require_version(self.code_commit, "engine commit")
        return self


class SourceContractProofBinding(_StrictModel):
    contract_id: str
    contract_sha256: str
    field_paths: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.contract_id, "source contract identifier")
        _require_sha256(self.contract_sha256, "source contract fingerprint")
        if len(set(self.field_paths)) != len(self.field_paths):
            raise ValueError("source field paths must be unique")
        if any(_FIELD_PATH_PATTERN.fullmatch(path) is None for path in self.field_paths):
            raise ValueError("source field path is invalid")
        return self


class ProofProfile(_StrictModel):
    proof_profile_id: str
    profile_version: str
    activation_state: ProfileActivationState
    quarantine_reason: str | None
    analysis_kind: str
    schema_version: Literal["1"]
    metric_definitions: tuple[MetricDefinitionBinding, ...]
    source_contracts: tuple[SourceContractProofBinding, ...]
    source_revision: str | None
    engines: tuple[EngineProofBinding, ...]
    artifact_template_ids: tuple[str, ...]
    definition_catalog_sha256: str
    source_catalog_sha256: str
    valid_until: datetime | None

    @model_validator(mode="after")
    def _validate_profile(self) -> Self:
        _require_safe_name(self.proof_profile_id, "proof profile identifier")
        _require_version(self.profile_version, "proof profile version")
        _require_safe_name(self.analysis_kind, "analysis kind")
        _require_sha256(self.definition_catalog_sha256, "definition catalog fingerprint")
        _require_sha256(self.source_catalog_sha256, "source catalog fingerprint")
        _require_unique(
            (binding.metric_id for binding in self.metric_definitions),
            "metric bindings",
        )
        _require_unique(
            (binding.contract_id for binding in self.source_contracts),
            "source bindings",
        )
        _require_unique((binding.engine_name for binding in self.engines), "engine bindings")
        _require_unique(self.artifact_template_ids, "artifact template identifiers")
        for artifact_id in self.artifact_template_ids:
            _require_safe_name(artifact_id, "artifact template identifier")
        if self.activation_state is ProfileActivationState.ACTIVE:
            if self.quarantine_reason is not None or not self.engines:
                raise ValueError("active proof profiles require engines and no quarantine")
        elif self.quarantine_reason is None:
            raise ValueError("quarantined proof profiles require a reason code")
        if self.quarantine_reason is not None:
            _require_safe_name(self.quarantine_reason, "quarantine reason")
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() != timedelta(0)
        ):
            raise ValueError("proof expiry must use UTC")
        return self


class ProofProfileCatalog(_StrictModel):
    schema_version: Literal["1"]
    catalog_version: str
    definition_catalog_sha256: str
    source_catalog_sha256: str
    production_metric_ids: tuple[str, ...]
    production_analysis_kinds: tuple[str, ...]
    production_artifact_template_ids: tuple[str, ...]
    source_field_coverage: tuple[SourceContractProofBinding, ...]
    profiles: tuple[ProofProfile, ...]

    @model_validator(mode="after")
    def _validate_catalog(self) -> Self:
        _require_version(self.catalog_version, "proof catalog version")
        _require_sha256(self.definition_catalog_sha256, "definition catalog fingerprint")
        _require_sha256(self.source_catalog_sha256, "source catalog fingerprint")
        for values, label in (
            (self.production_metric_ids, "production metric identifiers"),
            (self.production_analysis_kinds, "production analysis kinds"),
            (self.production_artifact_template_ids, "artifact template identifiers"),
        ):
            _require_unique(values, label)
            for value in values:
                _require_safe_name(value, label)
        _require_unique(
            (binding.contract_id for binding in self.source_field_coverage),
            "source coverage bindings",
        )
        _require_unique(
            (profile.proof_profile_id for profile in self.profiles),
            "proof profile identifiers",
        )
        _require_unique(
            (profile.analysis_kind for profile in self.profiles),
            "proof profile analysis kinds",
        )
        return self


class ProofStatus(_StrictModel):
    state: ProofState
    reason_code: str
    proof_profile_id: str | None


class CoverageMatrix(_StrictModel):
    complete: bool
    missing_metric_ids: tuple[str, ...]
    missing_analysis_kinds: tuple[str, ...]
    missing_artifact_template_ids: tuple[str, ...]
    missing_source_fields: tuple[str, ...]


class CoverageError(RuntimeError):
    def __init__(self, matrix: CoverageMatrix) -> None:
        """Retain the value-free matrix for deterministic caller handling."""
        super().__init__("analytics proof coverage is incomplete")
        self.matrix = matrix


class _ArtifactBinding(_StrictModel):
    analysis_kind: str
    template_ids: tuple[str, ...] = Field(min_length=1)


class _ProofCatalogDocument(_StrictModel):
    schema_version: Literal["1"]
    catalog_version: str
    definition_catalog_sha256: str
    source_catalog_sha256: str
    production_analysis_kinds: tuple[str, ...]
    source_contract_sha256s: Mapping[str, str]
    artifact_bindings: tuple[_ArtifactBinding, ...]
    engine: EngineProofBinding
    activation_state: Literal[ProfileActivationState.QUARANTINED]
    quarantine_reason: Literal["implementation_pending"]


class ProofRegistry:
    """Evaluate exact proof bindings without mutating persisted results."""

    def __init__(
        self,
        *,
        definitions: MetricDefinitionCatalog,
        catalog: ProofProfileCatalog,
    ) -> None:
        """Index immutable definition and profile catalogs by analysis kind."""
        self.definitions = definitions
        self.catalog = catalog
        self._profiles = MappingProxyType(
            {profile.analysis_kind: profile for profile in catalog.profiles},
        )

    def status(  # noqa: C901, PLR0911, PLR0912, PLR0913
        self,
        analysis_kind: str,
        schema_version: str,
        source_contracts: Mapping[str, str],
        *,
        source_revision: str | None = None,
        engine_versions: Mapping[str, tuple[str, str]] | None = None,
        at: datetime | None = None,
    ) -> ProofStatus:
        profile = self._profiles.get(analysis_kind)
        if profile is None:
            return _status(ProofState.REFUSED, "missing_proof_profile", None)
        with _QUARANTINE_LOCK:
            runtime_reason = _RUNTIME_QUARANTINES.get(analysis_kind)
        if runtime_reason is not None:
            return _status(ProofState.QUARANTINED, runtime_reason, profile)
        if profile.activation_state is ProfileActivationState.QUARANTINED:
            return _status(
                ProofState.QUARANTINED,
                profile.quarantine_reason or "proof_quarantined",
                profile,
            )
        if schema_version != profile.schema_version:
            return _status(ProofState.STALE, "schema_version_changed", profile)
        if (
            self.definitions.fingerprint_sha256 != profile.definition_catalog_sha256
            or self.definitions.fingerprint_sha256 != self.catalog.definition_catalog_sha256
        ):
            return _status(ProofState.STALE, "definition_catalog_changed", profile)
        current_definitions = self.definitions.by_id()
        if any(
            binding.metric_id not in current_definitions
            or current_definitions[binding.metric_id].definition_version
            != binding.definition_version
            for binding in profile.metric_definitions
        ):
            return _status(ProofState.STALE, "metric_definition_changed", profile)
        if self.catalog.source_catalog_sha256 != profile.source_catalog_sha256:
            return _status(ProofState.STALE, "source_catalog_changed", profile)
        if self.catalog.source_catalog_sha256 != source_contract_catalog_sha256():
            return _status(ProofState.STALE, "source_catalog_changed", profile)
        expected_sources = {
            binding.contract_id: binding.contract_sha256 for binding in profile.source_contracts
        }
        if dict(source_contracts) != expected_sources:
            return _status(ProofState.STALE, "source_contract_changed", profile)
        if profile.source_revision != source_revision:
            return _status(ProofState.STALE, "source_revision_changed", profile)
        supplied_engines = dict(engine_versions or {})
        expected_engines = {
            binding.engine_name: (binding.engine_version, binding.code_commit)
            for binding in profile.engines
        }
        if supplied_engines != expected_engines:
            return _status(ProofState.STALE, "engine_binding_changed", profile)
        checked_at = at or datetime.now(UTC)
        if checked_at.tzinfo is None or checked_at.utcoffset() != datetime.now(UTC).utcoffset():
            return _status(ProofState.REFUSED, "non_utc_proof_time", profile)
        if profile.valid_until is not None and checked_at > profile.valid_until:
            return _status(ProofState.STALE, "proof_expired", profile)
        return _status(ProofState.ACTIVE, "active", profile)


def quarantine_analysis_kind(kind: str, reason: str) -> None:
    """Fail closed for a kind while retaining only a bounded reason code."""
    _require_safe_name(kind, "analysis kind")
    reason_code = re.sub(r"[^a-z0-9]+", "_", reason.strip().lower()).strip("_")
    if (
        not reason_code
        or len(reason_code) > _MAX_REASON_CODE_LENGTH
        or reason_code not in _PUBLIC_QUARANTINE_REASONS
    ):
        reason_code = "runtime_quarantine"
    _require_safe_name(reason_code, "quarantine reason")
    with _QUARANTINE_LOCK:
        _RUNTIME_QUARANTINES[kind] = reason_code


def generate_coverage_matrix(
    *,
    definitions: MetricDefinitionCatalog,
    catalog: ProofProfileCatalog,
) -> CoverageMatrix:
    """Generate the production coverage matrix and fail on every missing surface."""
    defined = set(definitions.by_id())
    bound_metrics = {
        binding.metric_id for profile in catalog.profiles for binding in profile.metric_definitions
    }
    missing_metrics = sorted(
        (set(definitions.production_metric_ids) - defined)
        | (set(definitions.production_metric_ids) - bound_metrics)
        | (set(catalog.production_metric_ids) - bound_metrics)
        | {
            definition.metric_id
            for definition in definitions.definitions
            for kind in definition.analysis_kinds
            if not any(
                profile.analysis_kind == kind
                and any(
                    binding.metric_id == definition.metric_id
                    for binding in profile.metric_definitions
                )
                for profile in catalog.profiles
            )
        },
    )
    profiled_kinds = {profile.analysis_kind for profile in catalog.profiles}
    missing_kinds = sorted(set(catalog.production_analysis_kinds) - profiled_kinds)
    bound_artifacts = {
        artifact_id for profile in catalog.profiles for artifact_id in profile.artifact_template_ids
    }
    missing_artifacts = sorted(
        set(catalog.production_artifact_template_ids) - bound_artifacts,
    )
    supplied_fields = {
        f"{binding.contract_id}.{path}"
        for binding in catalog.source_field_coverage
        for path in binding.field_paths
    }
    required_fields = {
        f"{contract.contract_id}.{path}"
        for contract in source_contracts_by_id().values()
        for path in _source_field_paths(contract.fields)
    }
    missing_fields = sorted(required_fields - supplied_fields)
    matrix = CoverageMatrix(
        complete=not (missing_metrics or missing_kinds or missing_artifacts or missing_fields),
        missing_metric_ids=tuple(missing_metrics),
        missing_analysis_kinds=tuple(missing_kinds),
        missing_artifact_template_ids=tuple(missing_artifacts),
        missing_source_fields=tuple(missing_fields),
    )
    if not matrix.complete:
        raise CoverageError(matrix)
    return matrix


def load_proof_profile_catalog(
    *,
    definitions: MetricDefinitionCatalog,
    path: Path | None = None,
) -> ProofProfileCatalog:
    """Load checked-in quarantined registrations bound to exact source contracts."""
    try:
        document = _ProofCatalogDocument.model_validate_json(
            _read_catalog(path),
            strict=True,
        )
    except ValidationError as error:
        raise ProofProfileError("proof profile catalog is invalid") from error
    if document.definition_catalog_sha256 != definitions.fingerprint_sha256:
        raise ProofProfileError("proof profiles bind a different metric catalog")
    current_source_sha = source_contract_catalog_sha256()
    if document.source_catalog_sha256 != current_source_sha:
        raise ProofProfileError("proof profiles bind a different source catalog")
    contracts = source_contracts_by_id()
    current_contract_shas = {
        contract_id: source_contract_fingerprint(contract)
        for contract_id, contract in contracts.items()
    }
    if dict(document.source_contract_sha256s) != current_contract_shas:
        raise ProofProfileError("proof profiles bind different source contracts")
    definition_kinds = {
        kind for definition in definitions.definitions for kind in definition.analysis_kinds
    }
    source_kinds = {
        kind for contract in contracts.values() for kind in contract.dependent_analysis_kinds
    }
    if set(document.production_analysis_kinds) != definition_kinds | source_kinds:
        raise ProofProfileError("proof profile analysis-kind inventory is incomplete")
    artifacts_by_kind: dict[str, tuple[str, ...]] = {}
    for binding in document.artifact_bindings:
        if binding.analysis_kind in artifacts_by_kind:
            raise ProofProfileError("artifact analysis-kind bindings must be unique")
        artifacts_by_kind[binding.analysis_kind] = binding.template_ids
    profiles: list[ProofProfile] = []
    for kind in document.production_analysis_kinds:
        metric_bindings = tuple(
            MetricDefinitionBinding(
                metric_id=definition.metric_id,
                definition_version=definition.definition_version,
            )
            for definition in definitions.definitions
            if kind in definition.analysis_kinds
        )
        source_bindings = tuple(
            SourceContractProofBinding(
                contract_id=contract.contract_id,
                contract_sha256=current_contract_shas[contract.contract_id],
                field_paths=_source_field_paths(contract.fields),
            )
            for contract in contracts.values()
            if kind in contract.dependent_analysis_kinds
        )
        profiles.append(
            ProofProfile(
                proof_profile_id=f"vp_{kind}_v1",
                profile_version="1",
                activation_state=document.activation_state,
                quarantine_reason=document.quarantine_reason,
                analysis_kind=kind,
                schema_version="1",
                metric_definitions=metric_bindings,
                source_contracts=source_bindings,
                source_revision=None,
                engines=(document.engine,),
                artifact_template_ids=artifacts_by_kind.get(kind, ()),
                definition_catalog_sha256=document.definition_catalog_sha256,
                source_catalog_sha256=document.source_catalog_sha256,
                valid_until=None,
            ),
        )
    source_coverage = tuple(
        SourceContractProofBinding(
            contract_id=contract.contract_id,
            contract_sha256=current_contract_shas[contract.contract_id],
            field_paths=_source_field_paths(contract.fields),
        )
        for contract in contracts.values()
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version=document.catalog_version,
        definition_catalog_sha256=document.definition_catalog_sha256,
        source_catalog_sha256=document.source_catalog_sha256,
        production_metric_ids=definitions.production_metric_ids,
        production_analysis_kinds=document.production_analysis_kinds,
        production_artifact_template_ids=tuple(
            template_id
            for binding in document.artifact_bindings
            for template_id in binding.template_ids
        ),
        source_field_coverage=source_coverage,
        profiles=tuple(profiles),
    )
    generate_coverage_matrix(definitions=definitions, catalog=catalog)
    return catalog


def _source_field_paths(fields: Sequence[SourceField]) -> tuple[str, ...]:
    paths: list[str] = []

    def visit_schema(schema: SourceValueSchema, prefix: str) -> None:
        for child in schema.properties:
            visit(child, prefix)
        if schema.items is not None:
            visit_schema(schema.items, prefix)

    def visit(field: SourceField, prefix: str) -> None:
        path = f"{prefix}.{field.name}" if prefix else field.name
        paths.append(path)
        visit_schema(field, path)

    for source_field in fields:
        visit(source_field, "")
    return tuple(paths)


def _status(
    state: ProofState,
    reason_code: str,
    profile: ProofProfile | None,
) -> ProofStatus:
    return ProofStatus(
        state=state,
        reason_code=reason_code,
        proof_profile_id=None if profile is None else profile.proof_profile_id,
    )


def _read_catalog(path: Path | None) -> str:
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProofProfileError("proof profile catalog cannot be read") from error
    if _CATALOG_SOURCE_PATH.is_file():
        return _CATALOG_SOURCE_PATH.read_text(encoding="utf-8")
    try:
        return (
            files("saxo_bank_mcp")
            .joinpath(_CATALOG_RESOURCE)
            .read_text(
                encoding="utf-8",
            )
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
        raise ProofProfileError("proof profile catalog cannot be read") from error


def _require_safe_name(value: str, label: str) -> None:
    if _SAFE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_version(value: str, label: str) -> None:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_sha256(value: str, label: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_unique(values: Iterable[str], label: str) -> None:
    material = tuple(values)
    if len(set(material)) != len(material):
        raise ValueError(f"{label} must be unique")
