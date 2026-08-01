from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast
from uuid import RFC_4122, UUID

import duckdb
from pydantic import ValidationError

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import AnalysisResult
from saxo_bank_mcp.analytics_proof_profiles import ProofRegistry, ProofState

_ANALYSIS_ID_DOMAIN: Final = b"saxo-bank-mcp:analysis-id:v1\x00"
_OWNER_FILE_MODE: Final = 0o600
_OWNER_DIRECTORY_MODE: Final = 0o700
_ANALYSIS_ID_LENGTH: Final = 35
_OPAQUE_UUID_VERSION: Final = 4
_SHA256_LENGTH: Final = 64
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


def build_analysis_id(
    inputs: Mapping[str, object],
    engine_versions: Mapping[str, object],
    seed: int | None,
) -> str:
    """Build an opaque deterministic handle from canonical private input material."""
    if seed is not None and (type(seed) is not int or seed < 0 or seed >= 2**64):
        raise ValueError("analysis seed must be an unsigned 64-bit integer or null")
    material = {
        "engine_versions": _finite_json_value(engine_versions),
        "inputs": _finite_json_value(inputs),
        "seed": seed,
    }
    try:
        encoded = json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("analysis identity material must be finite JSON") from error
    raw = bytearray(hashlib.sha256(_ANALYSIS_ID_DOMAIN + encoded).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return f"an_{UUID(bytes=bytes(raw)).hex}"


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
        if checked_at > result.valid_until:
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
        if proof_status.proof_profile_id is None or any(
            receipt.proof_profile_id != proof_status.proof_profile_id
            for receipt in result.provenance.proof_receipts
        ):
            raise AnalysisReplayRefused("proof_receipt_changed")
        return result  # noqa: TRY300
    except duckdb.Error as error:
        raise AnalysisReplayRefused("store_unreadable") from error
    finally:
        connection.close()


def _finite_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("analysis identity material must be finite JSON")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in cast("Mapping[object, object]", value).items():
            if not isinstance(key, str):
                raise TypeError("analysis identity mapping keys must be strings")
            normalized[key] = _finite_json_value(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_finite_json_value(item) for item in cast("Sequence[object]", value)]
    raise ValueError("analysis identity material must be finite JSON")


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
