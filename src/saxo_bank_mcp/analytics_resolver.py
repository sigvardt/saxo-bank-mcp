from __future__ import annotations

import fcntl
import hashlib
import os
import re
import unicodedata
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

import duckdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instrument_identity import (
    InstrumentIdentityError,
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_migrations import store_writer_lock_path
from saxo_bank_mcp.analytics_models import InstrumentHandle
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_source_contracts import (
    SourcePage,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreQuotaError

_REFERENCE_CONTRACT: Final = "reference_instruments_v1"
_REFERENCE_RECEIPT: Final = source_contracts_by_id()[_REFERENCE_CONTRACT]
_REFERENCE_CONTRACT_SHA256: Final = source_contract_fingerprint(_REFERENCE_RECEIPT)
_CATALOG_ROW_COLUMN_COUNT: Final = 3
_MAX_QUERY_LENGTH: Final = 200
_MAX_FILTERS: Final = 25
_SAFE_FILTER_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:+ -]{1,64}$")
_CONNECTION_CONFIG: Final = MappingProxyType(
    {
        "allow_unsigned_extensions": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "enable_external_access": "false",
    },
)


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


class InstrumentState(StrEnum):
    CURRENT = "current"
    RENAMED = "renamed"
    UNAVAILABLE = "unavailable"


class ResolutionIssueCode(StrEnum):
    MULTIPLE_LISTINGS = "multiple_listings"
    ASSET_TYPE_COLLISION = "asset_type_collision"
    MISSING_EXCHANGE = "missing_exchange"
    PREVIOUSLY_RESOLVED_UNAVAILABLE = "previously_resolved_unavailable"
    NO_SILENT_REPLACEMENT = "no_silent_replacement"
    NOT_FOUND = "not_found"


class ResolutionError(RuntimeError):
    """Raised when instrument resolution cannot safely complete."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class ResolvedInstrument(_StrictModel):
    instrument_handle: InstrumentHandle
    display_label: str = Field(min_length=1, max_length=220)
    symbol: str | None = Field(max_length=64)
    asset_type: str = Field(min_length=1, max_length=64)
    exchange: str | None = Field(max_length=64)
    state: InstrumentState


class ResolutionIssue(_StrictModel):
    code: ResolutionIssueCode
    message: str = Field(min_length=1, max_length=240)


class ResolutionResult(_StrictModel):
    query: str = Field(min_length=1, max_length=_MAX_QUERY_LENGTH)
    status: ResolutionStatus
    matches: tuple[ResolvedInstrument, ...]
    issues: tuple[ResolutionIssue, ...]


class _SourceInstrument(BaseModel):
    # The provider has already validated every declared source field. Resolver identity needs only
    # this bounded projection and ignores other contract-declared reference metadata.
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    identifier: int = Field(alias="Identifier", ge=0)
    asset_type: str = Field(alias="AssetType", min_length=1, max_length=64)
    description: str | None = Field(alias="Description", max_length=500)
    symbol: str | None = Field(alias="Symbol", max_length=100)
    exchange: str | None = Field(alias="ExchangeId", max_length=100)


class _StoredInstrumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identifier: int = Field(ge=0)
    asset_type: str
    symbol: str | None
    exchange: str | None
    display_label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    instrument_handle: str
    metadata: _StoredInstrumentMetadata

    @property
    def identity(self) -> tuple[str, int]:
        return (self.metadata.asset_type, self.metadata.identifier)


def _unique_catalog_entries(
    entries: Sequence[_CatalogEntry],
) -> tuple[_CatalogEntry, ...]:
    identities: set[tuple[str, int]] = set()
    for entry in entries:
        if entry.identity in identities:
            raise ResolutionError("stored Saxo instrument identity is duplicated")
        identities.add(entry.identity)
    return tuple(entries)


def _authenticated_catalog_entry(row: tuple[object, ...]) -> _CatalogEntry:
    if (
        len(row) != _CATALOG_ROW_COLUMN_COUNT
        or not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not isinstance(row[2], str)
        or hashlib.sha256(row[2].encode()).hexdigest() != row[1]
    ):
        raise ResolutionError("stored instrument identity is invalid")
    try:
        metadata = _StoredInstrumentMetadata.model_validate_json(row[2], strict=True)
        canonical_handle = instrument_handle_for_saxo_identity(
            metadata.asset_type,
            metadata.identifier,
        )
    except (InstrumentIdentityError, ValidationError) as error:
        raise ResolutionError("stored instrument identity is invalid") from error
    if row[0] != canonical_handle:
        raise ResolutionError("stored instrument identity is invalid")
    return _CatalogEntry(instrument_handle=canonical_handle, metadata=metadata)


def _unique_source_instruments(
    instruments: Sequence[_SourceInstrument],
) -> tuple[_SourceInstrument, ...]:
    by_identity: dict[tuple[str, int], _SourceInstrument] = {}
    for instrument in instruments:
        identity = (instrument.asset_type, instrument.identifier)
        current = by_identity.get(identity)
        if current is not None and current != instrument:
            raise ResolutionError("Saxo returned conflicting rows for one instrument identity")
        by_identity[identity] = instrument
    return tuple(by_identity.values())


@contextmanager
def _writer_lock(config: AnalyticsConfig) -> Generator[None]:
    lock_path = store_writer_lock_path(config.paths.store_path)
    descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ResolutionError("another analytics writer owns the writer lock") from error
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _connect(config: AnalyticsConfig) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        str(config.paths.store_path),
        read_only=False,
        config=dict(_CONNECTION_CONFIG),
    )


@contextmanager
def _write_transaction(
    config: AnalyticsConfig,
) -> Generator[duckdb.DuckDBPyConnection]:
    with _writer_lock(config):
        connection = _connect(config)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                yield connection
            except BaseException:
                with suppress(duckdb.Error):
                    connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.casefold().split())


def _safe_component(value: str | None, *, fallback: str) -> str:
    if value is None:
        return fallback
    normalized = unicodedata.normalize("NFKC", value)
    printable = "".join(character for character in normalized if character.isprintable())
    collapsed = " ".join(printable.replace("·", " ").split()).strip()
    return collapsed[:64] or fallback


def _without_identifier(value: str, identifier: int) -> str:
    return re.sub(rf"(?<!\d){re.escape(str(identifier))}(?!\d)", "", value).strip()


def _display_label(instrument: _SourceInstrument) -> tuple[str | None, str | None, str]:
    symbol = _safe_component(instrument.symbol, fallback="") or None
    exchange = _safe_component(instrument.exchange, fallback="") or None
    safe_symbol = _without_identifier(symbol or "", instrument.identifier) or "Unnamed"
    safe_exchange = _without_identifier(exchange or "", instrument.identifier) or None
    asset_type = _safe_component(instrument.asset_type, fallback="Instrument")
    label = f"{safe_symbol} · {asset_type} · {safe_exchange or 'Exchange unavailable'}"
    return (None if safe_symbol == "Unnamed" else safe_symbol, safe_exchange, label)


def _stored_metadata(
    instrument: _SourceInstrument,
    current: _CatalogEntry | None,
) -> _StoredInstrumentMetadata:
    safe_symbol, safe_exchange, label = _display_label(instrument)
    new_aliases = {
        _normalize_alias(value)
        for value in (instrument.symbol, instrument.description)
        if value and value.strip()
    }
    if current is not None:
        new_aliases.update(current.metadata.aliases)
    return _StoredInstrumentMetadata(
        identifier=instrument.identifier,
        asset_type=instrument.asset_type,
        symbol=safe_symbol,
        exchange=safe_exchange,
        display_label=label,
        aliases=tuple(sorted(new_aliases)),
    )


class _InstrumentCatalog:
    def __init__(self, config: AnalyticsConfig) -> None:
        self._config = config
        store = AnalyticsStore.open(config)
        store.close()

    def entries_for_alias(self, query: str) -> tuple[_CatalogEntry, ...]:
        normalized = _normalize_alias(query)
        return tuple(entry for entry in self._entries() if normalized in entry.metadata.aliases)

    def _entries(self) -> tuple[_CatalogEntry, ...]:
        connection = _connect(self._config)
        try:
            rows = cast(
                "list[tuple[object, ...]]",
                connection.execute(
                    """
                    SELECT instrument_handle, fingerprint_sha256, metadata_json
                    FROM safe_instruments
                    ORDER BY instrument_handle
                    """,
                ).fetchall(),
            )
        finally:
            connection.close()
        entries = [_authenticated_catalog_entry(row) for row in rows]
        return _unique_catalog_entries(entries)

    def upsert(
        self,
        instruments: Sequence[_SourceInstrument],
        *,
        source_revision: str,
        source_timestamp: datetime,
    ) -> tuple[ResolvedInstrument, ...]:
        unique_instruments = _unique_source_instruments(instruments)
        with _write_transaction(self._config) as connection:
            existing = self._entries_in_connection(connection)
            by_identity = {entry.identity: entry for entry in existing}
            incoming_bytes = 0
            for instrument in unique_instruments:
                current = by_identity.get(
                    (instrument.asset_type, instrument.identifier),
                )
                metadata = _stored_metadata(instrument, current)
                if current is None or metadata != current.metadata:
                    incoming_bytes += len(metadata.model_dump_json().encode())
            if incoming_bytes:
                try:
                    AnalyticsStore.ensure_owner_capacity(
                        self._config,
                        incoming_bytes,
                    )
                except StoreQuotaError as error:
                    raise ResolutionError(
                        "analytics store quota refuses the instrument write",
                    ) from error
            results: list[ResolvedInstrument] = []
            changed = False
            for instrument in unique_instruments:
                current = by_identity.get((instrument.asset_type, instrument.identifier))
                result, did_change = self._upsert_one(
                    connection,
                    instrument,
                    current,
                    source_revision=source_revision,
                    source_timestamp=source_timestamp,
                )
                results.append(result)
                changed = changed or did_change
            if changed:
                connection.execute(
                    "UPDATE store_metadata SET revision = revision + 1 WHERE singleton = TRUE",
                )
            return tuple(results)

    @staticmethod
    def _entries_in_connection(
        connection: duckdb.DuckDBPyConnection,
    ) -> tuple[_CatalogEntry, ...]:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                "SELECT instrument_handle, fingerprint_sha256, metadata_json FROM safe_instruments",
            ).fetchall(),
        )
        entries = [_authenticated_catalog_entry(row) for row in rows]
        return _unique_catalog_entries(entries)

    def _upsert_one(
        self,
        connection: duckdb.DuckDBPyConnection,
        instrument: _SourceInstrument,
        current: _CatalogEntry | None,
        *,
        source_revision: str,
        source_timestamp: datetime,
    ) -> tuple[ResolvedInstrument, bool]:
        safe_symbol, safe_exchange, label = _display_label(instrument)
        metadata = _stored_metadata(instrument, current)
        metadata_json = metadata.model_dump_json()
        fingerprint = _sha256(metadata_json)
        renamed = current is not None and current.metadata != metadata
        try:
            write = put_saxo_instrument_identity(
                connection,
                asset_type=instrument.asset_type,
                uic=instrument.identifier,
                safe_label=label,
                source_revision=source_revision,
                source_timestamp=source_timestamp,
                fingerprint_sha256=fingerprint,
                metadata_json=metadata_json,
                update_existing=True,
            )
        except InstrumentIdentityError as error:
            raise ResolutionError(str(error)) from error
        return (
            ResolvedInstrument(
                instrument_handle=write.instrument_handle,
                display_label=label,
                symbol=safe_symbol,
                asset_type=instrument.asset_type,
                exchange=safe_exchange,
                state=InstrumentState.RENAMED if renamed else InstrumentState.CURRENT,
            ),
            write.changed,
        )


def _normalized_filters(values: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(value.strip() for value in values))
    if len(normalized) > _MAX_FILTERS or any(
        _SAFE_FILTER_PATTERN.fullmatch(value) is None for value in normalized
    ):
        raise ValueError(f"{field_name} filters are invalid")
    return normalized


def _parse_source_rows(rows: Sequence[Mapping[str, object]]) -> tuple[_SourceInstrument, ...]:
    parsed: list[_SourceInstrument] = []
    for row in rows:
        try:
            parsed.append(_SourceInstrument.model_validate(dict(row), strict=True))
        except ValidationError as error:
            raise ResolutionError("Saxo instrument source row is invalid") from error
    return tuple(parsed)


def _exact_matches(
    query: str,
    instruments: Sequence[_SourceInstrument],
) -> tuple[_SourceInstrument, ...]:
    normalized_query = _normalize_alias(query)
    exact = tuple(
        instrument
        for instrument in instruments
        if normalized_query
        in {
            _normalize_alias(value)
            for value in (instrument.symbol, instrument.description)
            if value
        }
    )
    return exact or tuple(instruments)


def _source_request(query: str, asset_types: tuple[str, ...]) -> dict[str, object]:
    request: dict[str, object] = {
        "$top": 100,
        "IncludeNonTradable": True,
        "Keywords": query,
    }
    if asset_types:
        request["AssetTypes"] = asset_types
    return request


async def _fetch_instruments(
    source: SaxoAnalyticsProvider,
    request: Mapping[str, object],
) -> tuple[tuple[_SourceInstrument, ...], tuple[SourcePage, ...]]:
    pages = [
        page
        async for page in source.fetch(
            _REFERENCE_CONTRACT,
            request,
        )
    ]
    if any(
        page.contract_id != _REFERENCE_CONTRACT
        or page.source_kind != "reference_instruments"
        or page.operation_id != _REFERENCE_RECEIPT.operation_id
        or page.contract_sha256 != _REFERENCE_CONTRACT_SHA256
        for page in pages
    ):
        raise ResolutionError("Saxo instrument source receipt is invalid")
    rows = _parse_source_rows(tuple(row for page in pages for row in page.rows))
    if not pages:
        return rows, ()
    revisions = {page.source_revision for page in pages}
    timestamps = {page.source_timestamp for page in pages}
    if len(revisions) != 1 or len(timestamps) != 1:
        raise ResolutionError("Saxo instrument source pages mix revisions")
    return rows, tuple(pages)


def _persist_reference_pages(
    config: AnalyticsConfig,
    pages: Sequence[SourcePage],
    instruments: Sequence[_SourceInstrument],
) -> None:
    """Persist exact resolver rows against their server-issued safe handles."""
    if not pages or not instruments:
        return
    rows_by_identity: dict[tuple[str, int], tuple[SourcePage, Mapping[str, object]]] = {}
    for page in pages:
        for row in page.rows:
            parsed = _SourceInstrument.model_validate(dict(row), strict=True)
            identity = (parsed.asset_type, parsed.identifier)
            existing = rows_by_identity.get(identity)
            if existing is not None and dict(existing[1]) != dict(row):
                raise ResolutionError("Saxo returned conflicting resolver source rows")
            rows_by_identity[identity] = (page, row)
    store = AnalyticsStore.open(config)
    try:
        with store.transaction():
            for instrument in instruments:
                identity = (instrument.asset_type, instrument.identifier)
                source = rows_by_identity.get(identity)
                if source is None:
                    raise ResolutionError("resolved instrument source row is unavailable")
                page, row = source
                handle = instrument_handle_for_saxo_identity(*identity)
                store.put_source_page(
                    source_kind=page.source_kind,
                    page_key=(
                        f"{page.contract_id}:{page.page_number}:"
                        f"{page.capture_revision.removeprefix('capture:')}:{handle}"
                    ),
                    source_revision=page.capture_revision,
                    source_native_revision=page.source_revision,
                    contract_name=page.contract_id,
                    contract_sha256=page.contract_sha256,
                    payload={
                        "contract_id": page.contract_id,
                        "data_version": page.data_version,
                        "page_number": page.page_number,
                        "request_fingerprint_sha256": page.request_fingerprint_sha256,
                        "rows": [dict(row)],
                        "source_native_revision": page.source_revision,
                        "source_quality": page.source_quality.model_dump(mode="json"),
                    },
                    row_count=1,
                    source_timestamp=page.source_timestamp,
                    account_scope=page.account_scope,
                    instrument_handle=handle,
                )
    finally:
        store.close()


def _resolution_issues(
    current: tuple[ResolvedInstrument, ...],
    unavailable: tuple[ResolvedInstrument, ...],
) -> tuple[ResolutionIssue, ...]:
    if unavailable:
        issues = [
            ResolutionIssue(
                code=ResolutionIssueCode.PREVIOUSLY_RESOLVED_UNAVAILABLE,
                message="A previously resolved Saxo instrument is no longer returned.",
            ),
        ]
        if current:
            issues.append(
                ResolutionIssue(
                    code=ResolutionIssueCode.NO_SILENT_REPLACEMENT,
                    message="A different Saxo listing requires an explicit selection.",
                ),
            )
        return tuple(issues)
    if len(current) > 1:
        issue_code = (
            ResolutionIssueCode.ASSET_TYPE_COLLISION
            if len({match.asset_type for match in current}) > 1
            else ResolutionIssueCode.MULTIPLE_LISTINGS
        )
        return (
            ResolutionIssue(
                code=issue_code,
                message="Several materially different Saxo instruments match the query.",
            ),
        )
    if current and current[0].exchange is None:
        return (
            ResolutionIssue(
                code=ResolutionIssueCode.MISSING_EXCHANGE,
                message="Saxo did not supply the exchange needed for a unique selection.",
            ),
        )
    if not current:
        return (
            ResolutionIssue(
                code=ResolutionIssueCode.NOT_FOUND,
                message="Saxo returned no matching instrument.",
            ),
        )
    return ()


def _resolution_status(
    current: tuple[ResolvedInstrument, ...],
    unavailable: tuple[ResolvedInstrument, ...],
) -> ResolutionStatus:
    if unavailable and not current:
        return ResolutionStatus.UNAVAILABLE
    if unavailable or len(current) > 1 or (current and current[0].exchange is None):
        return ResolutionStatus.AMBIGUOUS
    return ResolutionStatus.RESOLVED if current else ResolutionStatus.UNAVAILABLE


class InstrumentResolver:
    """Resolve Saxo reference rows into stable owner-local safe handles."""

    def __init__(self, source: SaxoAnalyticsProvider, config: AnalyticsConfig) -> None:
        """Bind the Saxo-only source and owner-local instrument catalog."""
        if type(source) is not SaxoAnalyticsProvider:
            raise TypeError("instrument resolver requires SaxoAnalyticsProvider")
        self._source = source
        self._config = config
        self._catalog = _InstrumentCatalog(config)

    async def resolve_instruments(
        self,
        query: str,
        asset_types: Sequence[str],
        exchanges: Sequence[str],
    ) -> ResolutionResult:
        clean_query = " ".join(query.split()).strip()
        if not clean_query or len(clean_query) > _MAX_QUERY_LENGTH:
            raise ValueError("instrument query is empty or too long")
        selected_asset_types = _normalized_filters(asset_types, field_name="asset type")
        selected_exchanges = _normalized_filters(exchanges, field_name="exchange")
        prior = tuple(
            entry
            for entry in self._catalog.entries_for_alias(clean_query)
            if (not selected_asset_types or entry.metadata.asset_type in selected_asset_types)
            and (not selected_exchanges or entry.metadata.exchange in selected_exchanges)
        )
        rows, source_pages = await _fetch_instruments(
            self._source,
            _source_request(clean_query, selected_asset_types),
        )
        filtered = tuple(
            instrument
            for instrument in rows
            if (not selected_asset_types or instrument.asset_type in selected_asset_types)
            and (not selected_exchanges or instrument.exchange in selected_exchanges)
        )
        selected = _exact_matches(clean_query, filtered)
        if source_pages:
            source_revision = source_pages[0].source_revision
            source_timestamp = source_pages[0].source_timestamp
            current = self._catalog.upsert(
                selected,
                source_revision=source_revision,
                source_timestamp=source_timestamp,
            )
            _persist_reference_pages(self._config, source_pages, selected)
        else:
            current = ()

        current_identities = {
            (instrument.asset_type, instrument.identifier) for instrument in selected
        }
        unavailable = tuple(
            ResolvedInstrument(
                instrument_handle=entry.instrument_handle,
                display_label=entry.metadata.display_label,
                symbol=entry.metadata.symbol,
                asset_type=entry.metadata.asset_type,
                exchange=entry.metadata.exchange,
                state=InstrumentState.UNAVAILABLE,
            )
            for entry in prior
            if entry.identity not in current_identities
        )
        matches = (*current, *unavailable)
        return ResolutionResult(
            query=clean_query,
            status=_resolution_status(current, unavailable),
            matches=matches,
            issues=_resolution_issues(current, unavailable),
        )


__all__ = (
    "InstrumentResolver",
    "InstrumentState",
    "ResolutionError",
    "ResolutionIssue",
    "ResolutionIssueCode",
    "ResolutionResult",
    "ResolutionStatus",
    "ResolvedInstrument",
)
