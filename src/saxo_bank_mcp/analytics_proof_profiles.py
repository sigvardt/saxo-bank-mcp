from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    AnalyticsConfigError,
    prepare_owner_only_path,
)
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
_QUARANTINE_STORE_NAME: Final = "proof-quarantines.json"
_QUARANTINE_LOCK_NAME: Final = "proof-quarantines.lock"
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
            if (
                self.quarantine_reason is not None
                or not self.source_contracts
                or self.source_revision is None
                or not self.engines
                or self.valid_until is None
            ):
                raise ValueError(
                    "active proof profiles require exact sources, a source revision, "
                    "engines, expiry, and no quarantine",
                )
        elif self.quarantine_reason is None:
            raise ValueError("quarantined proof profiles require a reason code")
        if self.quarantine_reason is not None:
            _require_safe_name(self.quarantine_reason, "quarantine reason")
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() != timedelta(0)
        ):
            raise ValueError("proof expiry must use UTC")
        return self


class ArtifactOwnerBinding(_StrictModel):
    template_id: str
    analysis_kind: str

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.template_id, "artifact template identifier")
        _require_safe_name(self.analysis_kind, "artifact analysis kind")
        return self


class ProofProfileCatalog(_StrictModel):
    schema_version: Literal["1"]
    catalog_version: str
    definition_catalog_sha256: str
    source_catalog_sha256: str
    production_metric_ids: tuple[str, ...]
    production_analysis_kinds: tuple[str, ...]
    production_artifact_template_ids: tuple[str, ...]
    artifact_owners: tuple[ArtifactOwnerBinding, ...] = ()
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
        _require_unique(
            (binding.template_id for binding in self.artifact_owners),
            "artifact owner bindings",
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
    inactive_metric_ids: tuple[str, ...] = ()
    inactive_analysis_kinds: tuple[str, ...] = ()
    inactive_artifact_template_ids: tuple[str, ...] = ()
    inactive_source_fields: tuple[str, ...] = ()
    misassigned_artifact_template_ids: tuple[str, ...] = ()


class CoverageError(RuntimeError):
    def __init__(self, matrix: CoverageMatrix) -> None:
        """Retain the value-free matrix for deterministic caller handling."""
        super().__init__("analytics proof coverage is incomplete")
        self.matrix = matrix


class _ArtifactBinding(_StrictModel):
    analysis_kind: str
    template_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.analysis_kind, "artifact analysis kind")
        _require_unique(self.template_ids, "artifact template identifiers")
        for template_id in self.template_ids:
            _require_safe_name(template_id, "artifact template identifier")
        return self


class _RuntimeQuarantine(_StrictModel):
    analysis_kind: str
    source_revision: str
    reason_code: str

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.analysis_kind, "analysis kind")
        _require_version(self.source_revision, "source revision")
        _require_safe_name(self.reason_code, "quarantine reason")
        if self.reason_code not in _PUBLIC_QUARANTINE_REASONS | {"runtime_quarantine"}:
            raise ValueError("quarantine reason is not public")
        return self


class _RuntimeQuarantineDocument(_StrictModel):
    schema_version: Literal["1"]
    quarantines: tuple[_RuntimeQuarantine, ...]

    @model_validator(mode="after")
    def _validate_document(self) -> Self:
        keys = tuple(f"{item.analysis_kind}\0{item.source_revision}" for item in self.quarantines)
        _require_unique(keys, "runtime quarantines")
        return self


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
        config: AnalyticsConfig | None = None,
    ) -> None:
        """Index immutable definition and profile catalogs by analysis kind."""
        self.definitions = definitions
        self.catalog = catalog
        self.config = (
            None if config is None else AnalyticsConfig.model_validate(config, strict=True)
        )
        self._profiles = MappingProxyType(
            {profile.analysis_kind: profile for profile in catalog.profiles},
        )

    def profile(self, analysis_kind: str) -> ProofProfile | None:
        """Return the exact immutable registration for one analysis kind."""
        return self._profiles.get(analysis_kind)

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
        runtime_reason = _runtime_quarantine_reason(
            analysis_kind,
            source_revision,
            self.config,
        )
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
        profile_source_fields = {
            binding.contract_id: frozenset(binding.field_paths)
            for binding in profile.source_contracts
        }
        if any(
            input_binding.source_contract_id is not None
            and (
                input_binding.source_contract_id not in profile_source_fields
                or not set(input_binding.field_paths).issubset(
                    profile_source_fields[input_binding.source_contract_id],
                )
            )
            for metric_binding in profile.metric_definitions
            for input_binding in current_definitions[metric_binding.metric_id].input_bindings
        ):
            return _status(
                ProofState.STALE,
                "metric_source_binding_changed",
                profile,
            )
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
        if profile.valid_until is not None and checked_at >= profile.valid_until:
            return _status(ProofState.STALE, "proof_expired", profile)
        return _status(ProofState.ACTIVE, "active", profile)


def quarantine_store_path(config: AnalyticsConfig) -> Path:
    """Return the owner-only persistent runtime-quarantine store."""
    validated = AnalyticsConfig.model_validate(config, strict=True)
    return prepare_owner_only_path(
        validated.paths.analytics_root / _QUARANTINE_STORE_NAME,
    )


def quarantine_analysis_kind(
    kind: str,
    reason: str,
    *,
    source_revision: str,
    config: AnalyticsConfig,
) -> None:
    """Persist an exact kind/revision quarantine with only a public reason code."""
    _require_safe_name(kind, "analysis kind")
    _require_version(source_revision, "source revision")
    reason_code = re.sub(r"[^a-z0-9]+", "_", reason.strip().lower()).strip("_")
    if (
        not reason_code
        or len(reason_code) > _MAX_REASON_CODE_LENGTH
        or reason_code not in _PUBLIC_QUARANTINE_REASONS
    ):
        reason_code = "runtime_quarantine"
    _require_safe_name(reason_code, "quarantine reason")
    validated = AnalyticsConfig.model_validate(config, strict=True)
    store_path = quarantine_store_path(validated)
    lock_path = prepare_owner_only_path(
        validated.paths.analytics_root / _QUARANTINE_LOCK_NAME,
    )
    with lock_path.open("r+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        document = _read_quarantine_document(store_path)
        by_key = {(item.analysis_kind, item.source_revision): item for item in document.quarantines}
        by_key[(kind, source_revision)] = _RuntimeQuarantine(
            analysis_kind=kind,
            source_revision=source_revision,
            reason_code=reason_code,
        )
        _write_quarantine_document(
            store_path,
            _RuntimeQuarantineDocument(
                schema_version="1",
                quarantines=tuple(by_key[key] for key in sorted(by_key)),
            ),
        )
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def generate_coverage_matrix(
    *,
    definitions: MetricDefinitionCatalog,
    catalog: ProofProfileCatalog,
) -> CoverageMatrix:
    """Require every declared production surface to have an active proof profile."""
    return _generate_coverage_matrix(
        definitions=definitions,
        catalog=catalog,
        require_active=True,
    )


def generate_declared_coverage_matrix(
    *,
    definitions: MetricDefinitionCatalog,
    catalog: ProofProfileCatalog,
) -> CoverageMatrix:
    """Require complete source-controlled registration, regardless of activation."""
    return _generate_coverage_matrix(
        definitions=definitions,
        catalog=catalog,
        require_active=False,
    )


def _generate_coverage_matrix(
    *,
    definitions: MetricDefinitionCatalog,
    catalog: ProofProfileCatalog,
    require_active: bool,
) -> CoverageMatrix:
    defined = set(definitions.by_id())
    declared_metrics = {
        binding.metric_id for profile in catalog.profiles for binding in profile.metric_definitions
    }
    profiles_by_kind = {profile.analysis_kind: profile for profile in catalog.profiles}
    missing_metrics = sorted(
        (set(definitions.production_metric_ids) - defined)
        | (set(definitions.production_metric_ids) - declared_metrics)
        | (set(catalog.production_metric_ids) - declared_metrics)
        | {
            definition.metric_id
            for definition in definitions.definitions
            for kind in definition.analysis_kinds
            if (
                kind not in profiles_by_kind
                or not any(
                    binding.metric_id == definition.metric_id
                    for binding in profiles_by_kind[kind].metric_definitions
                )
            )
        },
    )
    profiled_kinds = set(profiles_by_kind)
    missing_kinds = sorted(set(catalog.production_analysis_kinds) - profiled_kinds)
    artifact_assignees: dict[str, set[str]] = {}
    for profile in catalog.profiles:
        for artifact_id in profile.artifact_template_ids:
            artifact_assignees.setdefault(artifact_id, set()).add(profile.analysis_kind)
    bound_artifacts = set(artifact_assignees)
    missing_artifacts = sorted(
        set(catalog.production_artifact_template_ids) - bound_artifacts,
    )
    expected_artifact_owners = {
        binding.template_id: binding.analysis_kind for binding in catalog.artifact_owners
    }
    misassigned_artifacts = sorted(
        artifact_id
        for artifact_id, expected_kind in expected_artifact_owners.items()
        if artifact_assignees.get(artifact_id)
        and artifact_assignees[artifact_id] != {expected_kind}
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
    inactive_metrics: list[str] = []
    inactive_kinds: list[str] = []
    inactive_artifacts: list[str] = []
    inactive_fields: list[str] = []
    if require_active:
        active_profiles = tuple(
            profile
            for profile in catalog.profiles
            if profile.activation_state is ProfileActivationState.ACTIVE
        )
        active_by_kind = {profile.analysis_kind: profile for profile in active_profiles}
        inactive_kinds = sorted(
            set(catalog.production_analysis_kinds) - set(active_by_kind),
        )
        inactive_metrics = sorted(
            {
                definition.metric_id
                for definition in definitions.definitions
                for kind in definition.analysis_kinds
                if kind in profiles_by_kind
                and (
                    kind not in active_by_kind
                    or not any(
                        binding.metric_id == definition.metric_id
                        for binding in active_by_kind[kind].metric_definitions
                    )
                )
            },
        )
        inactive_artifacts = sorted(
            artifact_id
            for artifact_id in catalog.production_artifact_template_ids
            if (
                expected_artifact_owners.get(artifact_id) not in active_by_kind
                or artifact_id
                not in active_by_kind[expected_artifact_owners[artifact_id]].artifact_template_ids
            )
        )
        active_source_fields = {
            f"{binding.contract_id}.{path}"
            for profile in active_profiles
            for binding in profile.source_contracts
            for path in binding.field_paths
        }
        inactive_fields = sorted(required_fields - active_source_fields)
    incomplete_items = (
        missing_metrics,
        missing_kinds,
        missing_artifacts,
        missing_fields,
        inactive_metrics,
        inactive_kinds,
        inactive_artifacts,
        inactive_fields,
        misassigned_artifacts,
    )
    matrix = CoverageMatrix(
        complete=not any(incomplete_items),
        missing_metric_ids=tuple(missing_metrics),
        missing_analysis_kinds=tuple(missing_kinds),
        missing_artifact_template_ids=tuple(missing_artifacts),
        missing_source_fields=tuple(missing_fields),
        inactive_metric_ids=tuple(inactive_metrics),
        inactive_analysis_kinds=tuple(inactive_kinds),
        inactive_artifact_template_ids=tuple(inactive_artifacts),
        inactive_source_fields=tuple(inactive_fields),
        misassigned_artifact_template_ids=tuple(misassigned_artifacts),
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
    artifact_owners: list[ArtifactOwnerBinding] = []
    for binding in document.artifact_bindings:
        if binding.analysis_kind in artifacts_by_kind:
            raise ProofProfileError("artifact analysis-kind bindings must be unique")
        artifacts_by_kind[binding.analysis_kind] = binding.template_ids
        artifact_owners.extend(
            ArtifactOwnerBinding(
                template_id=template_id,
                analysis_kind=binding.analysis_kind,
            )
            for template_id in binding.template_ids
        )
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
        artifact_owners=tuple(artifact_owners),
        source_field_coverage=source_coverage,
        profiles=tuple(profiles),
    )
    generate_declared_coverage_matrix(definitions=definitions, catalog=catalog)
    return catalog


def _runtime_quarantine_reason(
    analysis_kind: str,
    source_revision: str | None,
    config: AnalyticsConfig | None,
) -> str | None:
    if config is None or source_revision is None:
        return None
    try:
        document = _read_quarantine_document(quarantine_store_path(config))
    except (AnalyticsConfigError, OSError, ProofProfileError, ValidationError):
        return "runtime_quarantine"
    for quarantine in document.quarantines:
        if (
            quarantine.analysis_kind == analysis_kind
            and quarantine.source_revision == source_revision
        ):
            return quarantine.reason_code
    return None


def _read_quarantine_document(path: Path) -> _RuntimeQuarantineDocument:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProofProfileError("runtime quarantine store cannot be read") from error
    if not raw.strip():
        return _RuntimeQuarantineDocument(schema_version="1", quarantines=())
    try:
        return _RuntimeQuarantineDocument.model_validate_json(raw, strict=True)
    except ValidationError as error:
        raise ProofProfileError("runtime quarantine store is invalid") from error


def _write_quarantine_document(
    path: Path,
    document: _RuntimeQuarantineDocument,
) -> None:
    payload = json.dumps(
        document.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".proof-quarantines-",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as temporary_file:
            descriptor = -1
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(path)
        path.chmod(0o600)
        temporary_path = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise ProofProfileError("runtime quarantine store cannot be written") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()


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
