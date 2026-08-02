from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast
from uuid import UUID

import duckdb
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_models import InstrumentHandle

_HANDLE_DOMAIN: Final = b"saxo-bank-mcp:saxo-instrument-identity:v1\x00"
_MAX_ASSET_TYPE_LENGTH: Final = 64
_INSTRUMENT_HANDLE_ADAPTER: Final[TypeAdapter[InstrumentHandle]] = TypeAdapter(
    InstrumentHandle,
)


class InstrumentIdentityError(RuntimeError):
    """Raised when one Saxo identity cannot be bound without replacement."""


@dataclass(frozen=True, slots=True)
class InstrumentIdentityWrite:
    instrument_handle: InstrumentHandle
    changed: bool


@dataclass(frozen=True, slots=True)
class _StoredIdentity:
    instrument_handle: InstrumentHandle
    asset_type: str
    uic: int


def _validated_identity(asset_type: object, uic: object) -> tuple[str, int]:
    if (
        not isinstance(asset_type, str)
        or not asset_type
        or len(asset_type) > _MAX_ASSET_TYPE_LENGTH
        or asset_type != asset_type.strip()
        or any(not character.isprintable() for character in asset_type)
    ):
        raise InstrumentIdentityError("Saxo instrument asset type is invalid")
    if isinstance(uic, bool) or not isinstance(uic, int) or uic < 0:
        raise InstrumentIdentityError("Saxo instrument UIC is invalid")
    return asset_type, uic


def instrument_handle_for_saxo_identity(asset_type: str, uic: int) -> InstrumentHandle:
    """Return the one opaque handle for an exact Saxo AssetType and UIC pair."""
    validated_asset_type, validated_uic = _validated_identity(asset_type, uic)
    material = json.dumps(
        {"asset_type": validated_asset_type, "uic": validated_uic},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = bytearray(hashlib.sha256(_HANDLE_DOMAIN + material).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    value = f"ih_{UUID(bytes=bytes(digest)).hex}"
    try:
        return _INSTRUMENT_HANDLE_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:  # pragma: no cover - construction invariant
        raise InstrumentIdentityError("Saxo instrument handle construction failed") from error


def _metadata_identity(metadata_json: object) -> tuple[str, int, dict[str, object]]:
    if not isinstance(metadata_json, str):
        raise InstrumentIdentityError("stored Saxo instrument metadata is invalid")
    try:
        loaded = json.loads(metadata_json)
    except (TypeError, ValueError) as error:
        raise InstrumentIdentityError("stored Saxo instrument metadata is invalid") from error
    if not isinstance(loaded, dict):
        raise InstrumentIdentityError("stored Saxo instrument metadata is invalid")
    metadata = cast("dict[str, object]", loaded)
    asset_type, uic = _validated_identity(
        metadata.get("asset_type"),
        metadata.get("identifier"),
    )
    return asset_type, uic, metadata


def _stored_identities(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[_StoredIdentity, ...]:
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            "SELECT instrument_handle, asset_type, safe_label, fingerprint_sha256, metadata_json "
            "FROM safe_instruments",
        ).fetchall(),
    )
    identities: list[_StoredIdentity] = []
    seen: set[tuple[str, int]] = set()
    for raw_handle, raw_asset_type, raw_label, raw_fingerprint, metadata_json in rows:
        try:
            handle = _INSTRUMENT_HANDLE_ADAPTER.validate_python(raw_handle, strict=True)
        except ValidationError as error:
            raise InstrumentIdentityError("stored Saxo instrument handle is invalid") from error
        asset_type, uic, metadata = _metadata_identity(metadata_json)
        if (
            raw_asset_type != asset_type
            or raw_label != metadata.get("display_label")
            or not isinstance(raw_fingerprint, str)
            or raw_fingerprint != hashlib.sha256(str(metadata_json).encode()).hexdigest()
        ):
            raise InstrumentIdentityError("stored Saxo instrument identity is invalid")
        identity = (asset_type, uic)
        if identity in seen:
            raise InstrumentIdentityError("stored Saxo instrument identity is duplicated")
        seen.add(identity)
        identities.append(
            _StoredIdentity(
                instrument_handle=handle,
                asset_type=asset_type,
                uic=uic,
            ),
        )
    return tuple(identities)


def put_saxo_instrument_identity(  # noqa: PLR0913
    connection: duckdb.DuckDBPyConnection,
    *,
    asset_type: str,
    uic: int,
    safe_label: str,
    source_revision: str,
    source_timestamp: datetime,
    fingerprint_sha256: str,
    metadata_json: str,
    update_existing: bool,
) -> InstrumentIdentityWrite:
    """Insert or enrich one Saxo identity without replacing another listing."""
    validated_asset_type, validated_uic = _validated_identity(asset_type, uic)
    metadata_asset_type, metadata_uic, metadata = _metadata_identity(metadata_json)
    if (
        (metadata_asset_type, metadata_uic) != (validated_asset_type, validated_uic)
        or metadata.get("display_label") != safe_label
        or hashlib.sha256(metadata_json.encode()).hexdigest() != fingerprint_sha256
    ):
        raise InstrumentIdentityError("new Saxo instrument identity is inconsistent")
    expected_handle = instrument_handle_for_saxo_identity(
        validated_asset_type,
        validated_uic,
    )
    identities = _stored_identities(connection)
    existing = next(
        (
            identity
            for identity in identities
            if (identity.asset_type, identity.uic) == (validated_asset_type, validated_uic)
        ),
        None,
    )
    if existing is not None:
        if existing.instrument_handle != expected_handle:
            raise InstrumentIdentityError(
                "stored Saxo instrument identity uses a noncanonical handle",
            )
        if not update_existing:
            return InstrumentIdentityWrite(expected_handle, changed=False)
        current = connection.execute(
            """
            SELECT safe_label, fingerprint_sha256, metadata_json
            FROM safe_instruments
            WHERE instrument_handle = ?
            """,
            (expected_handle,),
        ).fetchone()
        desired = (
            safe_label,
            fingerprint_sha256,
            metadata_json,
        )
        if current == desired:
            return InstrumentIdentityWrite(expected_handle, changed=False)
        connection.execute(
            """
            UPDATE safe_instruments
            SET asset_type = ?, safe_label = ?, source_revision = ?,
                source_timestamp = ?, fingerprint_sha256 = ?, metadata_json = ?
            WHERE instrument_handle = ?
            """,
            (
                validated_asset_type,
                safe_label,
                source_revision,
                source_timestamp,
                fingerprint_sha256,
                metadata_json,
                expected_handle,
            ),
        )
        return InstrumentIdentityWrite(expected_handle, changed=True)
    if any(identity.instrument_handle == expected_handle for identity in identities):
        raise InstrumentIdentityError("Saxo instrument handle identifies another listing")
    connection.execute(
        """
        INSERT INTO safe_instruments (
            instrument_handle, asset_type, safe_label, source_revision,
            source_timestamp, fingerprint_sha256, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            expected_handle,
            validated_asset_type,
            safe_label,
            source_revision,
            source_timestamp,
            fingerprint_sha256,
            metadata_json,
        ),
    )
    return InstrumentIdentityWrite(expected_handle, changed=True)


__all__ = (
    "InstrumentIdentityError",
    "InstrumentIdentityWrite",
    "instrument_handle_for_saxo_identity",
    "put_saxo_instrument_identity",
)
