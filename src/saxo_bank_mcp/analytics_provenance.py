from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast
from uuid import RFC_4122, UUID

import duckdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_metric_definitions import MetricDefinition
from saxo_bank_mcp.analytics_models import (
    AnalysisResult,
    MetricValue,
    ValueUnitClass,
)
from saxo_bank_mcp.analytics_proof_profiles import ProofProfile, ProofRegistry, ProofState

_ANALYSIS_ID_DOMAIN: Final = b"saxo-bank-mcp:analysis-id:v1\x00"
_ANALYSIS_INPUT_DOMAIN: Final = b"saxo-bank-mcp:analysis-input:v1\x00"
_ANALYSIS_PARAMETERS_DOMAIN: Final = b"saxo-bank-mcp:analysis-parameters:v1\x00"
_ANALYSIS_ENGINE_DOMAIN: Final = b"saxo-bank-mcp:analysis-engine:v1\x00"
_ANALYSIS_SEED_DOMAIN: Final = b"saxo-bank-mcp:analysis-seed:v1\x00"
_OWNER_FILE_MODE: Final = 0o600
_OWNER_DIRECTORY_MODE: Final = 0o700
_ANALYSIS_ID_LENGTH: Final = 35
_OPAQUE_UUID_VERSION: Final = 4
_SHA256_LENGTH: Final = 64
_IDENTITY_INPUT_KEYS: Final = frozenset(
    {
        "analysis_parameters_sha256",
        "dataset_fingerprint_sha256",
        "dataset_id",
        "source_revision",
        "tool_name",
    },
)
_ENGINE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_ENGINE_VERSION_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CODE_COMMIT_PATTERN: Final = re.compile(r"^[a-f0-9]{7,64}$")
_SOURCE_REVISION_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONNECTION_CONFIG: Final = MappingProxyType(
    {
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "enable_external_access": "false",
    },
)


class AnalysisReplayRefused(RuntimeError):  # noqa: N818
    """Value-free replay refusal suitable for agent-visible handling."""

    def __init__(self, reason_code: str) -> None:
        """Retain only a stable reason code, never raw private context."""
        super().__init__("analytics replay refused")
        self.reason_code = reason_code


class AnalysisIdentityBinding(BaseModel):
    """Opaque deterministic identity plus value-free component fingerprints."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    analysis_id: str = Field(pattern=r"^an_[0-9a-f]{32}$")
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    seed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def build_analysis_identity(
    inputs: Mapping[str, object],
    engine_versions: Mapping[str, object],
    seed: int | None,
) -> AnalysisIdentityBinding:
    """Bind only replay-safe identifiers, exact engines, and explicit seed state."""
    _validate_seed(seed)
    normalized_inputs = _normalized_identity_inputs(inputs)
    normalized_engines = _normalized_engine_versions(engine_versions)
    input_sha256 = _canonical_component_sha256(
        _ANALYSIS_INPUT_DOMAIN,
        normalized_inputs,
    )
    engine_sha256 = _canonical_component_sha256(
        _ANALYSIS_ENGINE_DOMAIN,
        normalized_engines,
    )
    seed_sha256 = _canonical_component_sha256(
        _ANALYSIS_SEED_DOMAIN,
        {"random_seed": seed, "seed_state": "none" if seed is None else "explicit"},
    )
    identity_material = {
        "engine_sha256": engine_sha256,
        "input_sha256": input_sha256,
        "seed_sha256": seed_sha256,
    }
    encoded = _canonical_json(identity_material)
    raw = bytearray(hashlib.sha256(_ANALYSIS_ID_DOMAIN + encoded).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return AnalysisIdentityBinding(
        analysis_id=f"an_{UUID(bytes=bytes(raw)).hex}",
        input_sha256=input_sha256,
        engine_sha256=engine_sha256,
        seed_sha256=seed_sha256,
    )


def build_analysis_id(
    inputs: Mapping[str, object],
    engine_versions: Mapping[str, object],
    seed: int | None,
) -> str:
    """Build an opaque deterministic handle from replay-safe identity material."""
    return build_analysis_identity(inputs, engine_versions, seed).analysis_id


def _validate_seed(seed: int | None) -> None:
    if seed is not None and (type(seed) is not int or seed < 0 or seed >= 2**64):
        raise ValueError("analysis seed must be an unsigned 64-bit integer or null")


def _normalized_identity_inputs(inputs: Mapping[str, object]) -> dict[str, str]:
    if not inputs:
        raise ValueError("analysis identity inputs must contain exact replay-safe fields")
    if frozenset(inputs) != _IDENTITY_INPUT_KEYS:
        raise ValueError("analysis identity input fields are invalid")
    fingerprint = inputs["dataset_fingerprint_sha256"]
    dataset_id = inputs["dataset_id"]
    source_revision = inputs["source_revision"]
    tool_name = inputs["tool_name"]
    analysis_parameters_sha256 = inputs["analysis_parameters_sha256"]
    if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
        raise ValueError("analysis identity input fingerprint is invalid")
    if not isinstance(dataset_id, str) or not _is_opaque_dataset_id(dataset_id):
        raise ValueError("analysis identity input dataset handle is invalid")
    if (
        not isinstance(source_revision, str)
        or _SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None
    ):
        raise ValueError("analysis identity input source revision is invalid")
    if not isinstance(tool_name, str) or _ENGINE_NAME_PATTERN.fullmatch(tool_name) is None:
        raise ValueError("analysis identity input tool name is invalid")
    if (
        not isinstance(analysis_parameters_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", analysis_parameters_sha256) is None
    ):
        raise ValueError("analysis identity parameter fingerprint is invalid")
    return {
        "analysis_parameters_sha256": analysis_parameters_sha256,
        "dataset_fingerprint_sha256": fingerprint,
        "dataset_id": dataset_id,
        "source_revision": source_revision,
        "tool_name": tool_name,
    }


def analysis_parameters_sha256(result: AnalysisResult) -> str:
    """Fingerprint the complete canonical safe request and analysis parameters."""
    return _canonical_component_sha256(
        _ANALYSIS_PARAMETERS_DOMAIN,
        {
            "account_scope": result.account_scope,
            "analysis_kind": result.analysis_kind,
            "as_of": result.as_of.isoformat(),
            "request": result.request.model_dump(mode="json"),
            "schema_version": result.schema_version,
        },
    )


def _normalized_engine_versions(
    engine_versions: Mapping[str, object],
) -> dict[str, dict[str, str]]:
    if not engine_versions:
        raise ValueError("analysis engine bindings must be non-empty")
    normalized: dict[str, dict[str, str]] = {}
    for engine_name, raw_binding in engine_versions.items():
        if _ENGINE_NAME_PATTERN.fullmatch(engine_name) is None:
            raise ValueError("analysis engine name is invalid")
        if not isinstance(raw_binding, Mapping):
            raise TypeError("analysis engine binding is invalid")
        binding = cast("Mapping[object, object]", raw_binding)
        if frozenset(binding) != frozenset({"code_commit", "version"}):
            raise ValueError("analysis engine binding is invalid")
        version = binding["version"]
        code_commit = binding["code_commit"]
        if (
            not isinstance(version, str)
            or _ENGINE_VERSION_PATTERN.fullmatch(version) is None
            or not isinstance(code_commit, str)
            or _CODE_COMMIT_PATTERN.fullmatch(code_commit) is None
        ):
            raise ValueError("analysis engine binding is invalid")
        normalized[engine_name] = {
            "code_commit": code_commit,
            "version": version,
        }
    return normalized


def _is_opaque_dataset_id(value: str) -> bool:
    if not value.startswith("ds_") or len(value) != _ANALYSIS_ID_LENGTH:
        return False
    try:
        opaque = UUID(hex=value.removeprefix("ds_"))
    except ValueError:
        return False
    return opaque.version == _OPAQUE_UUID_VERSION and opaque.variant == RFC_4122


def _canonical_component_sha256(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + _canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def replay_analysis(  # noqa: C901, PLR0912, PLR0915
    analysis_id: str,
    *,
    config: AnalyticsConfig,
    registry: ProofRegistry,
    at: datetime | None = None,
) -> AnalysisResult:
    """Read an owner-only result only while all persisted proof bindings remain active."""
    _require_analysis_id(analysis_id)
    checked_at = at or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() != datetime.now(UTC).utcoffset():
        raise AnalysisReplayRefused("non_utc_replay_time")
    store_path = _owner_only_store_path(config)
    try:
        connection = duckdb.connect(
            str(store_path),
            read_only=True,
            config=dict(_CONNECTION_CONFIG),
        )
    except duckdb.Error as error:
        raise AnalysisReplayRefused("store_unreadable") from error
    try:
        row = connection.execute(
            """
            SELECT dataset_id, analysis_kind, status, source_revision,
                   byte_count, fingerprint_sha256, result_json
            FROM analyses
            WHERE analysis_id = ?
            """,
            (analysis_id,),
        ).fetchone()
        if row is None:
            raise AnalysisReplayRefused("analysis_not_found")
        dataset_id = _stored_str(row[0])
        analysis_kind = _stored_str(row[1])
        status = _stored_str(row[2])
        source_revision = _stored_str(row[3])
        byte_count = _stored_int(row[4])
        fingerprint = _stored_str(row[5])
        result_json = _stored_str(row[6])
        if status == "invalidated":
            raise AnalysisReplayRefused("analysis_invalidated")
        if status != "verified":
            raise AnalysisReplayRefused("analysis_not_verified")
        _verify_payload(result_json, byte_count, fingerprint)
        try:
            result = AnalysisResult.model_validate_json(result_json, strict=True)
        except ValidationError as error:
            raise AnalysisReplayRefused("analysis_payload_invalid") from error
        if (
            result.analysis_id != analysis_id
            or result.analysis_kind != analysis_kind
            or result.provenance.dataset_id != dataset_id
            or result.provenance.source_revision != source_revision
        ):
            raise AnalysisReplayRefused("analysis_binding_mismatch")
        if not result.replayable:
            raise AnalysisReplayRefused("analysis_not_replayable")
        if checked_at >= result.valid_until:
            raise AnalysisReplayRefused("analysis_result_expired")
        dataset_row = connection.execute(
            """
            SELECT source_scope, source_revision, quality_state, fingerprint_sha256
            FROM datasets
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if dataset_row is None:
            raise AnalysisReplayRefused("dataset_not_found")
        dataset_scope = _stored_str(dataset_row[0])
        dataset_revision = _stored_str(dataset_row[1])
        dataset_quality = _stored_str(dataset_row[2])
        dataset_fingerprint = _stored_str(dataset_row[3])
        if dataset_scope != "saxo_openapi" or dataset_quality != "complete":
            raise AnalysisReplayRefused("dataset_not_verified")
        _require_sha256(dataset_fingerprint, "dataset_fingerprint_invalid")
        if dataset_revision != source_revision:
            raise AnalysisReplayRefused("source_revision_changed")
        contract_rows = connection.execute(
            """
            SELECT DISTINCT sc.contract_name, sc.contract_sha256
            FROM dataset_source_pages AS dsp
            JOIN source_pages AS sp ON sp.page_id = dsp.page_id
            JOIN source_contracts AS sc ON sc.contract_id = sp.contract_id
            WHERE dsp.dataset_id = ?
            ORDER BY sc.contract_name
            """,
            (dataset_id,),
        ).fetchall()
        source_contracts = {
            _stored_str(contract_row[0]): _stored_str(contract_row[1])
            for contract_row in contract_rows
        }
        if not source_contracts or len(source_contracts) != len(contract_rows):
            raise AnalysisReplayRefused("source_contract_missing")
        for contract_sha256 in source_contracts.values():
            _require_sha256(contract_sha256, "source_contract_invalid")
        _verify_result_source_bindings(
            result,
            source_revision=source_revision,
            source_contract_sha256s=frozenset(source_contracts.values()),
        )
        engine_versions = _result_engine_bindings(result)
        proof_status = registry.status(
            result.analysis_kind,
            result.schema_version,
            source_contracts,
            source_revision=result.provenance.source_revision,
            engine_versions=engine_versions,
            at=checked_at,
        )
        if proof_status.state is not ProofState.ACTIVE:
            raise AnalysisReplayRefused(proof_status.reason_code)
        profile = registry.profile(result.analysis_kind)
        if (
            profile is None
            or proof_status.proof_profile_id is None
            or len(result.provenance.proof_receipts) != 1
            or result.provenance.proof_receipts[0].proof_profile_id != proof_status.proof_profile_id
        ):
            raise AnalysisReplayRefused("proof_receipt_changed")
        _verify_result_metric_bindings(result, registry=registry, profile=profile)
        parameters_sha256 = analysis_parameters_sha256(result)
        if parameters_sha256 != result.provenance.analysis_parameters_sha256:
            raise AnalysisReplayRefused("analysis_identity_changed")
        identity_engines = {
            engine_name: {
                "version": version,
                "code_commit": code_commit,
            }
            for engine_name, (version, code_commit) in engine_versions.items()
        }
        try:
            identity = build_analysis_identity(
                {
                    "analysis_parameters_sha256": parameters_sha256,
                    "dataset_fingerprint_sha256": dataset_fingerprint,
                    "dataset_id": dataset_id,
                    "source_revision": source_revision,
                    "tool_name": result.tool_name,
                },
                identity_engines,
                result.provenance.random_seed,
            )
        except ValueError as error:
            raise AnalysisReplayRefused("analysis_identity_changed") from error
        if (
            identity.analysis_id != result.analysis_id
            or identity.input_sha256 != result.provenance.analysis_input_sha256
            or identity.engine_sha256 != result.provenance.analysis_engine_sha256
            or identity.seed_sha256 != result.provenance.analysis_seed_sha256
        ):
            raise AnalysisReplayRefused("analysis_identity_changed")
        return result  # noqa: TRY300
    except duckdb.Error as error:
        raise AnalysisReplayRefused("store_unreadable") from error
    finally:
        connection.close()


def _owner_only_store_path(config: AnalyticsConfig) -> Path:
    validated = AnalyticsConfig.model_validate(config)
    store_path = validated.paths.store_path
    try:
        store_stat = store_path.lstat()
    except OSError as error:
        raise AnalysisReplayRefused("owner_only_store_required") from error
    if (
        stat.S_ISLNK(store_stat.st_mode)
        or not stat.S_ISREG(store_stat.st_mode)
        or stat.S_IMODE(store_stat.st_mode) != _OWNER_FILE_MODE
        or store_stat.st_uid != os.getuid()
    ):
        raise AnalysisReplayRefused("owner_only_store_required")
    for directory in (
        validated.paths.state_root,
        validated.paths.analytics_root,
        validated.paths.artifacts_dir,
    ):
        try:
            directory_stat = directory.lstat()
        except OSError as error:
            raise AnalysisReplayRefused("owner_only_store_required") from error
        if (
            stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != _OWNER_DIRECTORY_MODE
            or directory_stat.st_uid != os.getuid()
        ):
            raise AnalysisReplayRefused("owner_only_store_required")
    return store_path


def _verify_payload(result_json: str, byte_count: int, fingerprint: str) -> None:
    encoded = result_json.encode()
    if len(encoded) != byte_count:
        raise AnalysisReplayRefused("analysis_payload_changed")
    _require_sha256(fingerprint, "analysis_fingerprint_invalid")
    if hashlib.sha256(encoded).hexdigest() != fingerprint:
        raise AnalysisReplayRefused("analysis_payload_changed")
    try:
        parsed = json.loads(result_json)
        canonical = json.dumps(
            parsed,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise AnalysisReplayRefused("analysis_payload_invalid") from error
    if canonical != result_json:
        raise AnalysisReplayRefused("analysis_payload_not_canonical")


def _result_engine_bindings(result: AnalysisResult) -> dict[str, tuple[str, str]]:
    bindings = {
        receipt.engine_binding.engine_name: (
            receipt.engine_binding.engine_version,
            receipt.engine_binding.code_commit,
        )
        for receipt in result.provenance.proof_receipts
    }
    provenance_binding = (
        result.provenance.engine_version,
        result.provenance.code_commit,
    )
    if bindings.get(result.provenance.engine_name) != provenance_binding:
        raise AnalysisReplayRefused("engine_binding_mismatch")
    return bindings


def _verify_result_metric_bindings(
    result: AnalysisResult,
    *,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> None:
    definitions = registry.definitions.by_id()
    result_metric_ids = tuple(metric.metric_id for metric in result.metrics)
    if len(set(result_metric_ids)) != len(result_metric_ids):
        raise AnalysisReplayRefused("metric_not_bound")
    for metric_id in result_metric_ids:
        if metric_id not in definitions:
            raise AnalysisReplayRefused("metric_definition_missing")
    required_metric_ids = {binding.metric_id for binding in profile.metric_definitions}
    if any(metric_id not in required_metric_ids for metric_id in result_metric_ids):
        raise AnalysisReplayRefused("metric_not_bound")
    if set(result_metric_ids) != required_metric_ids:
        raise AnalysisReplayRefused("required_metric_missing")
    for metric in result.metrics:
        definition = definitions[metric.metric_id]
        _verify_metric_semantics(metric, definition)
        if metric.proof_profile_id != profile.proof_profile_id:
            raise AnalysisReplayRefused("proof_receipt_changed")


def _verify_metric_semantics(
    metric: MetricValue,
    definition: MetricDefinition,
) -> None:
    if metric.metric_class is not definition.default_metric_class:
        raise AnalysisReplayRefused("metric_class_mismatch")
    if metric.unit_class is not definition.unit_class:
        raise AnalysisReplayRefused("metric_unit_class_mismatch")
    if metric.unit != definition.output_unit:
        raise AnalysisReplayRefused("metric_unit_mismatch")
    monetary = definition.unit_class is ValueUnitClass.MONETARY
    allowed_monetary_unit = definition.output_unit in {
        "price_currency",
        "reporting_currency",
    }
    if (monetary and (metric.currency is None or not allowed_monetary_unit)) or (
        not monetary and metric.currency is not None
    ):
        raise AnalysisReplayRefused("metric_currency_mismatch")


def _verify_result_source_bindings(
    result: AnalysisResult,
    *,
    source_revision: str,
    source_contract_sha256s: frozenset[str],
) -> None:
    provenance_hashes = frozenset(
        (
            result.provenance.source_contract_sha256,
            *result.provenance.source_contract_sha256s,
        ),
    )
    if provenance_hashes != source_contract_sha256s:
        raise AnalysisReplayRefused("source_binding_mismatch")
    for receipt in result.provenance.proof_receipts:
        receipt_hashes = frozenset(
            (
                receipt.source_binding.source_contract_sha256,
                *receipt.source_binding.source_contract_sha256s,
            ),
        )
        if (
            receipt.analysis_kind != result.analysis_kind
            or receipt.schema_version != result.schema_version
            or receipt.source_binding.source_revision != source_revision
            or receipt_hashes != source_contract_sha256s
        ):
            raise AnalysisReplayRefused("source_binding_mismatch")


def _require_analysis_id(value: str) -> None:
    if not value.startswith("an_") or len(value) != _ANALYSIS_ID_LENGTH:
        raise AnalysisReplayRefused("analysis_id_invalid")
    try:
        opaque = UUID(hex=value.removeprefix("an_"))
    except ValueError as error:
        raise AnalysisReplayRefused("analysis_id_invalid") from error
    if opaque.version != _OPAQUE_UUID_VERSION or opaque.variant != RFC_4122:
        raise AnalysisReplayRefused("analysis_id_invalid")


def _require_sha256(value: str, reason_code: str) -> None:
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise AnalysisReplayRefused(reason_code)


def _stored_str(value: object) -> str:
    if not isinstance(value, str):
        raise AnalysisReplayRefused("store_row_invalid")
    return value


def _stored_int(value: object) -> int:
    if not isinstance(value, int):
        raise AnalysisReplayRefused("store_row_invalid")
    return value
