from __future__ import annotations

import fcntl
import hashlib
import os
import re
import unicodedata
from collections.abc import AsyncIterator, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, cast

import duckdb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_migrations import store_writer_lock_path
from saxo_bank_mcp.analytics_models import HandleKind, InstrumentHandle, new_safe_handle
from saxo_bank_mcp.analytics_store import AnalyticsStore

_REFERENCE_CONTRACT: Final = "reference_instruments_v1"
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


class InstrumentSourcePage(Protocol):
    @property
    def rows(self) -> tuple[Mapping[str, object], ...]: ...

    @property
    def source_revision(self) -> str: ...

    @property
    def source_timestamp(self) -> datetime: ...


class InstrumentSource(Protocol):
    def fetch(
        self,
        contract_id: str,
        request: Mapping[str, object],
    ) -> AsyncIterator[InstrumentSourcePage]: ...


class _SourceInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

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
                    SELECT instrument_handle, metadata_json
                    FROM safe_instruments
                    ORDER BY instrument_handle
                    """,
                ).fetchall(),
            )
        finally:
            connection.close()
        entries: list[_CatalogEntry] = []
        for handle, metadata_json in rows:
            if not isinstance(handle, str) or not isinstance(metadata_json, str):
                raise ResolutionError("stored instrument metadata is invalid")
            try:
                metadata = _StoredInstrumentMetadata.model_validate_json(
                    metadata_json,
                    strict=True,
                )
            except ValidationError as error:
                raise ResolutionError("stored instrument metadata is invalid") from error
            entries.append(_CatalogEntry(instrument_handle=handle, metadata=metadata))
        return tuple(entries)

    def upsert(
        self,
        instruments: Sequence[_SourceInstrument],
        *,
        source_revision: str,
        source_timestamp: datetime,
    ) -> tuple[ResolvedInstrument, ...]:
        with _write_transaction(self._config) as connection:
            existing = self._entries_in_connection(connection)
            by_identity = {entry.identity: entry for entry in existing}
            results: list[ResolvedInstrument] = []
            changed = False
            for instrument in instruments:
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
                "SELECT instrument_handle, metadata_json FROM safe_instruments",
            ).fetchall(),
        )
        entries: list[_CatalogEntry] = []
        for handle, metadata_json in rows:
            if not isinstance(handle, str) or not isinstance(metadata_json, str):
                raise ResolutionError("stored instrument metadata is invalid")
            try:
                metadata = _StoredInstrumentMetadata.model_validate_json(
                    metadata_json,
                    strict=True,
                )
            except ValidationError as error:
                raise ResolutionError("stored instrument metadata is invalid") from error
            entries.append(_CatalogEntry(instrument_handle=handle, metadata=metadata))
        return tuple(entries)

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
        new_aliases = {
            _normalize_alias(value)
            for value in (instrument.symbol, instrument.description)
            if value and value.strip()
        }
        if current is not None:
            new_aliases.update(current.metadata.aliases)
        metadata = _StoredInstrumentMetadata(
            identifier=instrument.identifier,
            asset_type=instrument.asset_type,
            symbol=safe_symbol,
            exchange=safe_exchange,
            display_label=label,
            aliases=tuple(sorted(new_aliases)),
        )
        metadata_json = metadata.model_dump_json()
        fingerprint = _sha256(metadata_json)
        handle = (
            new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
            if current is None
            else current.instrument_handle
        )
        renamed = current is not None and current.metadata != metadata
        changed = current is None or renamed
        if changed:
            incoming_bytes = len(metadata_json.encode())
            current_bytes = self._config.paths.store_path.stat().st_size
            if not self._config.limits.can_accept_ingestion(current_bytes, incoming_bytes):
                raise ResolutionError("analytics store quota refuses the instrument write")
            connection.execute(
                """
                INSERT OR REPLACE INTO safe_instruments (
                    instrument_handle,
                    asset_type,
                    safe_label,
                    source_revision,
                    source_timestamp,
                    fingerprint_sha256,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle,
                    instrument.asset_type,
                    label,
                    source_revision,
                    source_timestamp,
                    fingerprint,
                    metadata_json,
                ),
            )
        return (
            ResolvedInstrument(
                instrument_handle=handle,
                display_label=label,
                symbol=safe_symbol,
                asset_type=instrument.asset_type,
                exchange=safe_exchange,
                state=InstrumentState.RENAMED if renamed else InstrumentState.CURRENT,
            ),
            changed,
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
    source: InstrumentSource,
    request: Mapping[str, object],
) -> tuple[tuple[_SourceInstrument, ...], str | None, datetime | None]:
    pages = [
        page
        async for page in source.fetch(
            _REFERENCE_CONTRACT,
            request,
        )
    ]
    rows = _parse_source_rows(tuple(row for page in pages for row in page.rows))
    if not pages:
        return rows, None, None
    revisions = {page.source_revision for page in pages}
    timestamps = {page.source_timestamp for page in pages}
    if len(revisions) != 1 or len(timestamps) != 1:
        raise ResolutionError("Saxo instrument source pages mix revisions")
    return rows, next(iter(revisions)), next(iter(timestamps))


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

    def __init__(self, source: InstrumentSource, config: AnalyticsConfig) -> None:
        """Bind the Saxo-only source and owner-local instrument catalog."""
        self._source = source
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
        prior = self._catalog.entries_for_alias(clean_query)
        rows, source_revision, source_timestamp = await _fetch_instruments(
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
        if source_revision is not None and source_timestamp is not None:
            current = self._catalog.upsert(
                selected,
                source_revision=source_revision,
                source_timestamp=source_timestamp,
            )
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
    "InstrumentSource",
    "InstrumentSourcePage",
    "InstrumentState",
    "ResolutionError",
    "ResolutionIssue",
    "ResolutionIssueCode",
    "ResolutionResult",
    "ResolutionStatus",
    "ResolvedInstrument",
)
