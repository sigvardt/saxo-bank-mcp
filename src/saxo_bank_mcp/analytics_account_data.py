from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast
from uuid import RFC_4122, UUID, uuid4

import duckdb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import (
    DatasetId,
    HandleKind,
    InstrumentHandle,
    QualityState,
    SafeAccountScope,
    UtcDateTime,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_provider import (
    SaxoAnalyticsProvider,
    SourceRequestBudget,
    SourceRequestBudgetError,
)
from saxo_bank_mcp.analytics_source_contracts import (
    FrozenSourceJsonValue,
    SourceCaptureContext,
    SourceJsonValue,
    SourcePage,
    build_source_capture_context,
    build_source_capture_envelope,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreQuotaError
from saxo_bank_mcp.analytics_sync import IngestionFingerprints

_TRANSACTIONS_CONTRACT: Final = "transactions_v1"
_BOOKINGS_CONTRACT: Final = "bookings_v1"
_CLOSED_POSITIONS_CONTRACT: Final = "closed_positions_history_v1"
_COSTS_CONTRACT: Final = "costs_v1"
_NORMALIZED_ROW_ESTIMATE_BYTES: Final = 512
_SOURCE_PAGE_SIZE: Final = 500
_ACCOUNT_ALIAS_VERSION: Final = 4
_MAX_SOURCE_TEXT_LENGTH: Final = 1_000
_DUCKDB_ALLOCATION_RESERVE_BYTES: Final = 16 * 1024 * 1024
_DUCKDB_BLOCK_BYTES: Final = 256 * 1024
_SOURCE_PAGE_INDEX_OVERHEAD_BYTES: Final = 64 * 1024
_DATASET_INDEX_OVERHEAD_BYTES: Final = 64 * 1024
_ACCOUNT_BINDING_INDEX_OVERHEAD_BYTES: Final = 64 * 1024
_SNAPSHOT_INDEX_OVERHEAD_BYTES: Final = 64 * 1024
_JSON_OBJECT_ADAPTER: Final[TypeAdapter[dict[str, SourceJsonValue]]] = TypeAdapter(
    dict[str, SourceJsonValue],
)
_DATA_KINDS: Final = frozenset(
    {"transactions", "bookings", "closed_positions", "costs"},
)

type Clock = Callable[[], datetime]
type AccountDataKind = Literal["transactions", "bookings", "closed_positions", "costs"]


class AccountSyncError(RuntimeError):
    """Base error for owner-requested account-data ingestion."""


class AccountSyncValidationError(AccountSyncError):
    """Raised before source access for an invalid or unsafe request."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class AccountScope(_StrictModel):
    """Private Saxo selectors bound to one opaque public account alias."""

    alias: SafeAccountScope
    account_key: SecretStr = Field(min_length=1, max_length=256)
    client_key: SecretStr = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def require_opaque_alias(self) -> AccountScope:
        if not self.alias.startswith("aa_"):
            raise ValueError("account scope must use a safe account alias")
        return self


class AccountCategoryCount(_StrictModel):
    """Value-free count for one normalized account-data category."""

    category: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    row_count: int = Field(ge=0)


class AccountDatasetSummary(_StrictModel):
    """Safe handle-only summary for one owner-only account dataset."""

    dataset_id: DatasetId
    account_alias: SafeAccountScope
    data_kind: AccountDataKind
    instrument_handle: InstrumentHandle | None = None
    quality_state: QualityState
    coverage_start: UtcDateTime
    coverage_end: UtcDateTime
    row_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    correction_count: int = Field(ge=0)
    category_counts: tuple[AccountCategoryCount, ...]
    warnings: tuple[str, ...]
    fingerprints: IngestionFingerprints


class PrivateAccountRecord(_StrictModel):
    """Private normalized values released only to the trusted local owner host."""

    data_kind: AccountDataKind
    category: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    effective_at: UtcDateTime | None
    amount_value: float | None = Field(allow_inf_nan=False)
    fee_value: float | None = Field(default=None, allow_inf_nan=False)
    financing_value: float | None = Field(default=None, allow_inf_nan=False)
    tax_value: float | None = Field(default=None, allow_inf_nan=False)
    currency: str | None = Field(default=None, max_length=16)
    instrument_handle: InstrumentHandle | None = None
    fx_timestamp: UtcDateTime | None = None
    tax_lot_basis_available: Literal[False] = False


class AccountSyncResult(_StrictModel):
    """On-demand account sync result with no public raw values or identifiers."""

    status: Literal["complete", "degraded"]
    account_alias: SafeAccountScope
    source_request_count: int = Field(ge=0)
    datasets: tuple[AccountDatasetSummary, ...]
    invalidated_analysis_count: int = Field(ge=0)
    visibility: VisibilityMode
    private_records: tuple[PrivateAccountRecord, ...] | None

    @model_validator(mode="after")
    def require_private_delivery_boundary(self) -> AccountSyncResult:
        is_private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if is_private != (self.private_records is not None):
            raise ValueError("private account records do not match result visibility")
        return self


SyncResult = AccountSyncResult


@dataclass(frozen=True, slots=True)
class _NormalizedRecord:
    data_kind: AccountDataKind
    category: str
    source_identity_sha256: str
    source_row_sha256: str
    effective_at: datetime | None
    amount_value: float | None
    currency: str | None
    instrument_handle: str | None
    page_number: int
    fee_value: float | None = None
    financing_value: float | None = None
    tax_value: float | None = None

    @property
    def public_payload(self) -> dict[str, SourceJsonValue]:
        return {
            "category": self.category,
            "fx_timestamp": None,
            "source_identity_sha256": self.source_identity_sha256,
            "source_row_sha256": self.source_row_sha256,
            "tax_lot_basis_available": False,
        }

    def private_record(self) -> PrivateAccountRecord:
        return PrivateAccountRecord(
            data_kind=self.data_kind,
            category=self.category,
            effective_at=self.effective_at,
            amount_value=self.amount_value,
            fee_value=self.fee_value,
            financing_value=self.financing_value,
            tax_value=self.tax_value,
            currency=self.currency,
            instrument_handle=self.instrument_handle,
            fx_timestamp=None,
            tax_lot_basis_available=False,
        )


@dataclass(frozen=True, slots=True)
class _PreparedDataset:
    contract_id: str
    data_kind: AccountDataKind
    pages: tuple[SourcePage, ...]
    records: tuple[_NormalizedRecord, ...]
    coverage_start: datetime
    coverage_end: datetime
    warnings: tuple[str, ...]
    instrument_handle: str | None = None


@dataclass(frozen=True, slots=True)
class _InstrumentSelector:
    handle: str
    identifier: int
    asset_type: str


def new_account_alias() -> str:
    """Create one opaque UUID4 alias safe for public account scoping."""
    return f"aa_{uuid4().hex}"


async def sync_account_history(  # noqa: PLR0913
    scope: AccountScope,
    start: datetime,
    end: datetime,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode = VisibilityMode.FINGERPRINT_ONLY,
    trusted_local_host: bool = False,
    clock: Clock = lambda: datetime.now(UTC),
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Fetch transactions, bookings, and closed positions in one bounded capture."""
    return await _sync_history_contracts(
        scope,
        start,
        end,
        contract_ids=(
            _TRANSACTIONS_CONTRACT,
            _BOOKINGS_CONTRACT,
            _CLOSED_POSITIONS_CONTRACT,
        ),
        provider=provider,
        config=config,
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        clock=clock,
        request_budget=request_budget,
    )


async def sync_transactions(  # noqa: PLR0913
    scope: AccountScope,
    start: datetime,
    end: datetime,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode = VisibilityMode.FINGERPRINT_ONLY,
    trusted_local_host: bool = False,
    clock: Clock = lambda: datetime.now(UTC),
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Fetch and persist one bounded transaction history on demand."""
    return await _sync_history_contracts(
        scope,
        start,
        end,
        contract_ids=(_TRANSACTIONS_CONTRACT,),
        provider=provider,
        config=config,
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        clock=clock,
        request_budget=request_budget,
    )


async def sync_bookings(  # noqa: PLR0913
    scope: AccountScope,
    start: datetime,
    end: datetime,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode = VisibilityMode.FINGERPRINT_ONLY,
    trusted_local_host: bool = False,
    clock: Clock = lambda: datetime.now(UTC),
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Fetch and persist one bounded booking history on demand."""
    return await _sync_history_contracts(
        scope,
        start,
        end,
        contract_ids=(_BOOKINGS_CONTRACT,),
        provider=provider,
        config=config,
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        clock=clock,
        request_budget=request_budget,
    )


async def sync_closed_positions(  # noqa: PLR0913
    scope: AccountScope,
    start: datetime,
    end: datetime,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode = VisibilityMode.FINGERPRINT_ONLY,
    trusted_local_host: bool = False,
    clock: Clock = lambda: datetime.now(UTC),
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Fetch and persist one bounded closed-position history on demand."""
    return await _sync_history_contracts(
        scope,
        start,
        end,
        contract_ids=(_CLOSED_POSITIONS_CONTRACT,),
        provider=provider,
        config=config,
        visibility=visibility,
        trusted_local_host=trusted_local_host,
        clock=clock,
        request_budget=request_budget,
    )


async def sync_cost_sources(  # noqa: PLR0913
    scope: AccountScope,
    instruments: Sequence[str],
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode = VisibilityMode.FINGERPRINT_ONLY,
    trusted_local_host: bool = False,
    clock: Clock = lambda: datetime.now(UTC),
    request_budget: SourceRequestBudget | None = None,
) -> SyncResult:
    """Fetch current Saxo cost sources for explicit safe instrument handles."""
    validated_scope = _preflight(
        scope,
        provider,
        visibility,
        trusted_local_host=trusted_local_host,
    )
    assert_persisted_account_scope_binding(config, validated_scope)
    handles = tuple(instruments)
    if not handles or len(handles) > config.limits.sync_instruments:
        raise AccountSyncValidationError("cost sync instrument count exceeds its fixed limit")
    if len(set(handles)) != len(handles):
        raise AccountSyncValidationError("cost sync instruments must be unique")
    selectors = _instrument_selectors(config, handles)
    budget = _source_budget(config, request_budget)
    request_count_start = budget.used
    captured_at = _require_utc_clock(clock())
    prepared: list[_PreparedDataset] = []
    for selector in selectors:
        request: dict[str, object] = {
            "AccountKey": validated_scope.account_key.get_secret_value(),
            "AssetType": selector.asset_type,
            "Uic": selector.identifier,
        }
        capture = build_source_capture_context(
            {_COSTS_CONTRACT: request},
            captured_at=captured_at,
        )
        pages = await _fetch_pages(
            provider,
            _COSTS_CONTRACT,
            request,
            capture,
            budget,
        )
        envelope = build_source_capture_envelope(capture, pages)
        records = _normalize_costs(
            envelope.pages,
            validated_scope.alias,
            selector.handle,
        )
        prepared.append(
            _PreparedDataset(
                contract_id=_COSTS_CONTRACT,
                data_kind="costs",
                pages=envelope.pages,
                records=records,
                coverage_start=captured_at,
                coverage_end=captured_at,
                warnings=_record_warnings(records, envelope.pages),
                instrument_handle=selector.handle,
            ),
        )
    summaries, invalidated = _persist_prepared(
        config,
        validated_scope,
        tuple(prepared),
    )
    return _result(
        alias=validated_scope.alias,
        summaries=summaries,
        invalidated=invalidated,
        request_count=budget.used - request_count_start,
        visibility=visibility,
        prepared=prepared,
        response_rows=config.limits.response_rows,
    )


async def _sync_history_contracts(  # noqa: PLR0913
    scope: AccountScope,
    start: datetime,
    end: datetime,
    *,
    contract_ids: Sequence[str],
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode,
    trusted_local_host: bool,
    clock: Clock,
    request_budget: SourceRequestBudget | None,
) -> SyncResult:
    validated_scope = _preflight(
        scope,
        provider,
        visibility,
        trusted_local_host=trusted_local_host,
    )
    assert_persisted_account_scope_binding(config, validated_scope)
    _require_utc_range(start, end)
    budget = _source_budget(config, request_budget)
    request_count_start = budget.used
    captured_at = _require_utc_clock(clock())
    requests = {
        contract_id: _history_request(contract_id, validated_scope, start, end)
        for contract_id in contract_ids
    }
    capture = build_source_capture_context(requests, captured_at=captured_at)
    pages_by_contract: dict[str, tuple[SourcePage, ...]] = {}
    all_pages: list[SourcePage] = []
    for contract_id, request in requests.items():
        pages = await _fetch_pages(
            provider,
            contract_id,
            request,
            capture,
            budget,
        )
        pages_by_contract[contract_id] = pages
        all_pages.extend(pages)
    envelope = build_source_capture_envelope(capture, all_pages)
    if sum(page.row_count for page in envelope.pages) > config.limits.sync_rows:
        raise AccountSyncValidationError("account sync row count exceeds its fixed limit")
    prepared = tuple(
        _prepare_history_dataset(
            contract_id,
            pages_by_contract[contract_id],
            validated_scope.alias,
            start,
            end,
        )
        for contract_id in contract_ids
    )
    summaries, invalidated = _persist_prepared(
        config,
        validated_scope,
        prepared,
    )
    return _result(
        alias=validated_scope.alias,
        summaries=summaries,
        invalidated=invalidated,
        request_count=budget.used - request_count_start,
        visibility=visibility,
        prepared=prepared,
        response_rows=config.limits.response_rows,
    )


def _preflight(
    scope: AccountScope,
    provider: SaxoAnalyticsProvider,
    visibility: VisibilityMode,
    *,
    trusted_local_host: bool,
) -> AccountScope:
    try:
        validated_scope = AccountScope.model_validate(scope)
    except ValidationError as error:
        raise AccountSyncValidationError("account scope is invalid") from error
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("account sync requires SaxoAnalyticsProvider")
    if visibility is VisibilityMode.PRIVATE_USER_RESULT and not trusted_local_host:
        raise AccountSyncValidationError(
            "private_user_result requires the trusted local host",
        )
    return validated_scope


def _selector_fingerprints(scope: AccountScope) -> tuple[str, str]:
    account_selector = scope.account_key.get_secret_value().encode()
    client_selector = scope.client_key.get_secret_value().encode()
    return (
        hashlib.sha256(b"saxo-analytics-account-selector-v1\0" + account_selector).hexdigest(),
        hashlib.sha256(b"saxo-analytics-client-selector-v1\0" + client_selector).hexdigest(),
    )


def assert_persisted_account_scope_binding(
    config: AnalyticsConfig,
    scope: AccountScope,
) -> None:
    store_path = config.paths.store_path
    if not store_path.is_file() or store_path.stat().st_size == 0:
        return
    connection = duckdb.connect(str(store_path), read_only=True)
    try:
        try:
            row = connection.execute(
                """
                SELECT account_selector_sha256, client_selector_sha256
                FROM account_scope_bindings
                WHERE account_scope = ?
                """,
                (scope.alias,),
            ).fetchone()
        except duckdb.CatalogException:
            return
    finally:
        connection.close()
    if row is None:
        return
    expected_account, expected_client = _selector_fingerprints(scope)
    if (
        not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not hmac.compare_digest(row[0], expected_account)
        or not hmac.compare_digest(row[1], expected_client)
    ):
        raise AccountSyncValidationError(
            "account alias binding does not match the supplied selectors",
        )


def bind_account_scope(
    connection: duckdb.DuckDBPyConnection,
    scope: AccountScope,
) -> None:
    expected_account, expected_client = _selector_fingerprints(scope)
    row = connection.execute(
        """
        SELECT account_selector_sha256, client_selector_sha256
        FROM account_scope_bindings
        WHERE account_scope = ?
        """,
        (scope.alias,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO account_scope_bindings (
                account_scope, account_selector_sha256, client_selector_sha256
            )
            VALUES (?, ?, ?)
            """,
            (scope.alias, expected_account, expected_client),
        )
        return
    if (
        not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not hmac.compare_digest(row[0], expected_account)
        or not hmac.compare_digest(row[1], expected_client)
    ):
        raise AccountSyncValidationError(
            "account alias binding does not match the supplied selectors",
        )


def conservative_ingestion_reservation(
    *,
    raw_payload_bytes: int,
    normalized_rows: int,
    source_pages: int,
    datasets: int,
    snapshots: int = 0,
) -> int:
    estimated = (
        _DUCKDB_ALLOCATION_RESERVE_BYTES
        + raw_payload_bytes
        + (normalized_rows * _NORMALIZED_ROW_ESTIMATE_BYTES)
        + (source_pages * _SOURCE_PAGE_INDEX_OVERHEAD_BYTES)
        + (datasets * _DATASET_INDEX_OVERHEAD_BYTES)
        + _ACCOUNT_BINDING_INDEX_OVERHEAD_BYTES
        + (snapshots * _SNAPSHOT_INDEX_OVERHEAD_BYTES)
    )
    return math.ceil(estimated / _DUCKDB_BLOCK_BYTES) * _DUCKDB_BLOCK_BYTES


def _source_budget(
    config: AnalyticsConfig,
    supplied: SourceRequestBudget | None,
) -> SourceRequestBudget:
    if supplied is None:
        return SourceRequestBudget(config.limits.sync_instruments)
    if type(supplied) is not SourceRequestBudget:
        raise AccountSyncValidationError("source request budget is invalid")
    if supplied.limit != config.limits.sync_instruments:
        raise AccountSyncValidationError("source request budget must use the fixed sync limit")
    return supplied


async def _fetch_pages(
    provider: SaxoAnalyticsProvider,
    contract_id: str,
    request: Mapping[str, object],
    capture: SourceCaptureContext,
    budget: SourceRequestBudget,
) -> tuple[SourcePage, ...]:
    try:
        return tuple(
            [
                page
                async for page in provider.fetch(
                    contract_id,
                    request,
                    capture=capture,
                    budget=budget,
                )
            ],
        )
    except SourceRequestBudgetError as error:
        raise AccountSyncValidationError("account sync source budget was exhausted") from error


def _history_request(
    contract_id: str,
    scope: AccountScope,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    account_key = scope.account_key.get_secret_value()
    client_key = scope.client_key.get_secret_value()
    if contract_id == _TRANSACTIONS_CONTRACT:
        return {
            "$top": _SOURCE_PAGE_SIZE,
            "AccountKeys": (account_key,),
            "ClientKey": client_key,
            "FromDate": start.isoformat(),
            "ToDate": end.isoformat(),
        }
    if contract_id == _BOOKINGS_CONTRACT:
        return {
            "$top": _SOURCE_PAGE_SIZE,
            "AccountKey": account_key,
            "ClientKey": client_key,
            "FromDate": start.isoformat(),
            "ToDate": end.isoformat(),
        }
    if contract_id == _CLOSED_POSITIONS_CONTRACT:
        return {
            "$top": _SOURCE_PAGE_SIZE,
            "AccountKey": account_key,
            "ClientKey": client_key,
            "FromDate": start.date().isoformat(),
            "ToDate": end.date().isoformat(),
        }
    raise AccountSyncValidationError("account history contract is unsupported")


def _prepare_history_dataset(
    contract_id: str,
    pages: tuple[SourcePage, ...],
    alias: str,
    start: datetime,
    end: datetime,
) -> _PreparedDataset:
    dataset_warnings: tuple[str, ...] | None = None
    if contract_id == _TRANSACTIONS_CONTRACT:
        records = _normalize_transactions(pages, alias)
        data_kind: AccountDataKind = "transactions"
    elif contract_id == _BOOKINGS_CONTRACT:
        records = _normalize_bookings(pages, alias)
        data_kind = "bookings"
    elif contract_id == _CLOSED_POSITIONS_CONTRACT:
        observed_records = _normalize_closed_positions(pages, alias)
        warnings = set(_record_warnings(observed_records, pages))
        if any(
            record.effective_at is not None and not start <= record.effective_at <= end
            for record in observed_records
        ):
            warnings.add("closed_position_rows_outside_requested_utc_range")
        records = tuple(
            record
            for record in observed_records
            if record.effective_at is not None and start <= record.effective_at <= end
        )
        dataset_warnings = tuple(sorted(warnings))
        data_kind = "closed_positions"
    else:
        raise AccountSyncValidationError("account history contract is unsupported")
    return _PreparedDataset(
        contract_id=contract_id,
        data_kind=data_kind,
        pages=pages,
        records=records,
        coverage_start=start,
        coverage_end=end,
        warnings=(dataset_warnings or _record_warnings(records, pages)),
    )


def _normalize_transactions(
    pages: Sequence[SourcePage],
    alias: str,
) -> tuple[_NormalizedRecord, ...]:
    records: list[_NormalizedRecord] = []
    for page in pages:
        for row in page.rows:
            source_id = _required_text(row.get("TransactionId"))
            transaction_type = _required_text(row.get("TransactionType"))
            amount = _optional_number(row.get("Amount"))
            records.append(
                _NormalizedRecord(
                    data_kind="transactions",
                    category=_transaction_category(transaction_type, amount),
                    source_identity_sha256=_identity_fingerprint(alias, source_id),
                    source_row_sha256=_fingerprint(_thaw_mapping(row)),
                    effective_at=_required_timestamp(row.get("ExecutionTime")),
                    amount_value=amount,
                    currency=None,
                    instrument_handle=None,
                    page_number=page.page_number,
                    fee_value=amount if transaction_type == "Fee" else None,
                ),
            )
    return tuple(records)


def _normalize_bookings(
    pages: Sequence[SourcePage],
    alias: str,
) -> tuple[_NormalizedRecord, ...]:
    records: list[_NormalizedRecord] = []
    for page in pages:
        for row in page.rows:
            source_id = _required_text(row.get("BookingId"))
            records.append(
                _NormalizedRecord(
                    data_kind="bookings",
                    category="booking",
                    source_identity_sha256=_identity_fingerprint(alias, source_id),
                    source_row_sha256=_fingerprint(_thaw_mapping(row)),
                    effective_at=_required_timestamp(row.get("BookingDate")),
                    amount_value=_optional_number(row.get("Amount")),
                    currency=None,
                    instrument_handle=None,
                    page_number=page.page_number,
                ),
            )
    return tuple(records)


def _normalize_closed_positions(
    pages: Sequence[SourcePage],
    alias: str,
) -> tuple[_NormalizedRecord, ...]:
    records: list[_NormalizedRecord] = []
    for page in pages:
        for row in page.rows:
            source_id = _required_text(row.get("ClosedPositionId"))
            closing = _optional_mapping(row.get("ClosingPosition"))
            closed = _optional_mapping(row.get("ClosedPosition"))
            asset_type = _optional_text(closing.get("AssetType")) or "Unknown"
            identifier = _optional_integer(closing.get("Uic"))
            instrument_handle = (
                None
                if identifier is None
                else _opaque_handle(
                    "ih",
                    {
                        "account_alias": alias,
                        "asset_type": asset_type,
                        "identifier": identifier,
                    },
                )
            )
            closed_at_value = closed.get("ExecutionTimeClose")
            closed_at = None if closed_at_value is None else _required_timestamp(closed_at_value)
            records.append(
                _NormalizedRecord(
                    data_kind="closed_positions",
                    category="closed_position",
                    source_identity_sha256=_identity_fingerprint(alias, source_id),
                    source_row_sha256=_fingerprint(_thaw_mapping(row)),
                    effective_at=closed_at,
                    amount_value=_optional_number(closed.get("ClosedProfitLoss")),
                    currency=None,
                    instrument_handle=instrument_handle,
                    page_number=page.page_number,
                ),
            )
    return tuple(records)


def _normalize_costs(
    pages: Sequence[SourcePage],
    alias: str,
    instrument_handle: str,
) -> tuple[_NormalizedRecord, ...]:
    records: list[_NormalizedRecord] = []
    for page in pages:
        for row in page.rows:
            cost = _optional_mapping(row.get("Cost"))
            total = _optional_number(cost.get("TotalCost"))
            commission = _optional_number(cost.get("Commission"))
            tax = _optional_number(cost.get("StampDuty"))
            source_id = _fingerprint(
                {"account_alias": alias, "instrument_handle": instrument_handle},
            )
            records.append(
                _NormalizedRecord(
                    data_kind="costs",
                    category="cost",
                    source_identity_sha256=source_id,
                    source_row_sha256=_fingerprint(_thaw_mapping(row)),
                    effective_at=page.source_timestamp,
                    amount_value=total,
                    currency=_optional_text(row.get("Currency")),
                    instrument_handle=instrument_handle,
                    page_number=page.page_number,
                    fee_value=commission,
                    tax_value=tax,
                ),
            )
    return tuple(records)


def _transaction_category(transaction_type: str, amount: float | None) -> str:
    if transaction_type == "CashTransfer":
        if amount is not None and amount > 0:
            return "deposit"
        if amount is not None and amount < 0:
            return "withdrawal"
        return "cash_transfer"
    if transaction_type == "CorporateAction":
        return (
            "dividend_or_corporate_action_income"
            if amount is not None and amount > 0
            else "corporate_action"
        )
    if transaction_type == "Fee":
        return "fee"
    return "trade"


def _record_warnings(
    records: Sequence[_NormalizedRecord],
    pages: Sequence[SourcePage],
) -> tuple[str, ...]:
    warnings = {
        "fx_conversion_timestamp_unavailable",
        "tax_lot_basis_unavailable",
    }
    if any(record.currency is None for record in records):
        warnings.add("currency_unavailable")
    if any(
        record.data_kind == "closed_positions" and record.effective_at is None for record in records
    ):
        warnings.add("closed_position_time_unavailable")
    if any(
        record.data_kind == "closed_positions" and record.instrument_handle is None
        for record in records
    ):
        warnings.add("closed_position_instrument_unavailable")
    if any(record.category.startswith("dividend_or_") for record in records):
        warnings.add("corporate_action_income_classification_ambiguous")
    if any(record.category in {"deposit", "withdrawal"} for record in records):
        warnings.add("cash_transfer_direction_inferred_from_sign")
    if any(page.source_quality.state == "limited" for page in pages):
        warnings.add("source_quality_limited")
    return tuple(sorted(warnings))


def _persist_prepared(
    config: AnalyticsConfig,
    scope: AccountScope,
    prepared: tuple[_PreparedDataset, ...],
) -> tuple[tuple[AccountDatasetSummary, ...], int]:
    alias = scope.alias
    normalized_count = sum(len(item.records) for item in prepared)
    pages = tuple(page for item in prepared for page in item.pages)
    reservation = conservative_ingestion_reservation(
        raw_payload_bytes=sum(len(page.model_dump_json().encode()) for page in pages),
        normalized_rows=normalized_count,
        source_pages=len(pages),
        datasets=len(prepared),
    )
    try:
        AnalyticsStore.ensure_owner_capacity(config, reservation)
    except StoreQuotaError as error:
        raise AccountSyncValidationError(
            "analytics store quota refuses account ingestion",
        ) from error
    store = AnalyticsStore.open(config)
    summaries: list[AccountDatasetSummary] = []
    invalidated_total = 0
    try:
        try:
            with store.market_ingestion_transaction(
                reservation,
            ) as connection:
                bind_account_scope(connection, scope)
                for item in prepared:
                    summary, invalidated = _persist_one_dataset(
                        store,
                        connection,
                        alias,
                        item,
                    )
                    summaries.append(summary)
                    invalidated_total += invalidated
        except StoreQuotaError as error:
            raise AccountSyncValidationError(
                "analytics store quota refuses account ingestion",
            ) from error
    finally:
        store.close()
    return tuple(summaries), invalidated_total


def _persist_one_dataset(
    store: AnalyticsStore,
    connection: duckdb.DuckDBPyConnection,
    alias: str,
    prepared: _PreparedDataset,
) -> tuple[AccountDatasetSummary, int]:
    if not prepared.pages:
        raise AccountSyncError("account source capture contains no page")
    page_ids: dict[int, str] = {}
    for page in prepared.pages:
        serialized = page.model_dump(mode="json")
        source_payload = _JSON_OBJECT_ADAPTER.validate_python(serialized, strict=True)
        rows = source_payload.get("rows")
        if not isinstance(rows, list):
            raise AccountSyncError("validated source page rows are unavailable")
        stored = store.put_source_page(
            source_kind=page.source_kind,
            page_key=(
                f"{page.contract_id}:{page.page_number}:"
                f"{page.capture_revision.removeprefix('capture:')}"
            ),
            source_revision=page.capture_revision,
            source_native_revision=page.source_revision,
            contract_name=page.contract_id,
            contract_sha256=page.contract_sha256,
            payload={
                "contract_id": page.contract_id,
                "data_version": page.data_version,
                "page_fingerprint_sha256": page.page_fingerprint_sha256,
                "page_number": page.page_number,
                "request_fingerprint_sha256": page.request_fingerprint_sha256,
                "rows": rows,
                "source_native_revision": page.source_revision,
                "source_quality": page.source_quality.model_dump(mode="json"),
            },
            row_count=page.row_count,
            source_timestamp=page.source_timestamp,
            account_scope=alias,
            instrument_handle=prepared.instrument_handle,
            instrument_scope_sha256=page.instrument_scope_sha256,
        )
        page_ids[page.page_number] = stored.page_id
    existing = _existing_record_versions(connection, prepared.data_kind, alias)
    duplicate_count = 0
    correction_count = 0
    inserted_count = 0
    for record in prepared.records:
        versions = existing.setdefault(record.source_identity_sha256, set())
        if record.source_row_sha256 in versions:
            duplicate_count += 1
            continue
        is_correction = bool(versions)
        _ensure_closed_position_instrument(
            connection,
            prepared,
            record,
        )
        if not _insert_record(connection, alias, page_ids[record.page_number], record):
            continue
        inserted_count += 1
        if is_correction:
            correction_count += 1
        versions.add(record.source_row_sha256)
    invalidated = (
        _invalidate_dependent_analyses(
            connection,
            alias,
            prepared.contract_id,
        )
        if inserted_count
        else 0
    )
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    quality = (
        QualityState.PARTIAL
        if any(page.source_quality.state == "limited" for page in prepared.pages)
        else QualityState.COMPLETE
    )
    stored_dataset = store.create_dataset(
        dataset_id=dataset_id,
        account_scope=alias,
        source_scope="saxo_openapi",
        source_revision=prepared.pages[0].capture_revision,
        source_page_ids=tuple(page_ids.values()),
        created_at=prepared.pages[0].source_timestamp,
        coverage_start=prepared.coverage_start,
        coverage_end=prepared.coverage_end,
        quality_state=quality,
    )
    counts: dict[str, int] = {}
    for record in prepared.records:
        counts[record.category] = counts.get(record.category, 0) + 1
    fingerprints = _ingestion_fingerprints(
        prepared,
        duplicate_count,
        correction_count,
    )
    return (
        AccountDatasetSummary(
            dataset_id=stored_dataset.dataset_id,
            account_alias=alias,
            data_kind=prepared.data_kind,
            instrument_handle=prepared.instrument_handle,
            quality_state=stored_dataset.quality_state,
            coverage_start=prepared.coverage_start,
            coverage_end=prepared.coverage_end,
            row_count=len(prepared.records),
            duplicate_count=duplicate_count,
            correction_count=correction_count,
            category_counts=tuple(
                AccountCategoryCount(category=category, row_count=count)
                for category, count in sorted(counts.items())
            ),
            warnings=prepared.warnings,
            fingerprints=fingerprints,
        ),
        invalidated,
    )


def _existing_record_versions(
    connection: duckdb.DuckDBPyConnection,
    data_kind: AccountDataKind,
    alias: str,
) -> dict[str, set[str]]:
    if data_kind not in _DATA_KINDS:
        raise AccountSyncError("normalized account data kind is unsupported")
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            f"SELECT fingerprint_sha256, payload_json FROM {data_kind} "  # noqa: S608
            "WHERE account_scope = ?",
            (alias,),
        ).fetchall(),
    )
    versions: dict[str, set[str]] = {}
    for fingerprint, payload_json in rows:
        if not isinstance(fingerprint, str) or not isinstance(payload_json, str):
            raise AccountSyncError("normalized account data is invalid")
        try:
            payload = _JSON_OBJECT_ADAPTER.validate_json(payload_json, strict=True)
        except ValidationError as error:
            raise AccountSyncError("normalized account payload is invalid") from error
        identity = payload.get("source_identity_sha256")
        if not isinstance(identity, str):
            raise AccountSyncError("normalized account identity is invalid")
        versions.setdefault(identity, set()).add(fingerprint)
    return versions


def _insert_record(
    connection: duckdb.DuckDBPyConnection,
    alias: str,
    page_id: str,
    record: _NormalizedRecord,
) -> bool:
    if record.effective_at is None or (
        record.data_kind == "closed_positions" and record.instrument_handle is None
    ):
        return False
    record_id = _opaque_row_id(
        record.data_kind,
        alias,
        record.source_identity_sha256,
        record.source_row_sha256,
    )
    payload_json = _canonical_json(record.public_payload)
    if record.data_kind == "transactions":
        connection.execute(
            """
            INSERT INTO transactions (
                transaction_id, page_id, account_scope, instrument_handle,
                source_revision, effective_at, amount_value, currency,
                fingerprint_sha256, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (transaction_id) DO NOTHING
            """,
            (
                record_id,
                page_id,
                alias,
                record.instrument_handle,
                f"row:{record.source_row_sha256}",
                record.effective_at,
                record.amount_value,
                record.currency,
                record.source_row_sha256,
                payload_json,
            ),
        )
    elif record.data_kind == "bookings":
        connection.execute(
            """
            INSERT INTO bookings (
                booking_id, page_id, account_scope, instrument_handle,
                source_revision, booked_at, amount_value, currency,
                fingerprint_sha256, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (booking_id) DO NOTHING
            """,
            (
                record_id,
                page_id,
                alias,
                record.instrument_handle,
                f"row:{record.source_row_sha256}",
                record.effective_at,
                record.amount_value,
                record.currency,
                record.source_row_sha256,
                payload_json,
            ),
        )
    elif record.data_kind == "closed_positions":
        connection.execute(
            """
            INSERT INTO closed_positions (
                closed_position_id, page_id, account_scope, instrument_handle,
                source_revision, closed_at, amount_value, currency,
                fingerprint_sha256, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (closed_position_id) DO NOTHING
            """,
            (
                record_id,
                page_id,
                alias,
                record.instrument_handle,
                f"row:{record.source_row_sha256}",
                record.effective_at,
                record.amount_value,
                record.currency,
                record.source_row_sha256,
                payload_json,
            ),
        )
    elif record.data_kind == "costs":
        connection.execute(
            """
            INSERT INTO costs (
                cost_id, page_id, account_scope, instrument_handle,
                source_revision, effective_at, amount_value, currency,
                fingerprint_sha256, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cost_id) DO NOTHING
            """,
            (
                record_id,
                page_id,
                alias,
                record.instrument_handle,
                f"row:{record.source_row_sha256}",
                record.effective_at,
                record.amount_value,
                record.currency,
                record.source_row_sha256,
                payload_json,
            ),
        )
    else:
        raise AccountSyncError("normalized account data kind is unsupported")
    return True


def _ensure_closed_position_instrument(
    connection: duckdb.DuckDBPyConnection,
    prepared: _PreparedDataset,
    record: _NormalizedRecord,
) -> None:
    if record.data_kind != "closed_positions" or record.instrument_handle is None:
        return
    source_row = next(
        (
            row
            for page in prepared.pages
            if page.page_number == record.page_number
            for row in page.rows
            if _fingerprint(_thaw_mapping(row)) == record.source_row_sha256
        ),
        None,
    )
    if source_row is None:
        raise AccountSyncError("closed-position source row is unavailable")
    closing = _optional_mapping(source_row.get("ClosingPosition"))
    asset_type = _optional_text(closing.get("AssetType")) or "Unknown"
    identifier = _optional_integer(closing.get("Uic"))
    if identifier is None:
        raise AccountSyncError("closed-position source instrument is unavailable")
    metadata: dict[str, SourceJsonValue] = {
        "aliases": [],
        "asset_type": asset_type,
        "display_label": f"{asset_type} position",
        "exchange": None,
        "identifier": identifier,
        "symbol": None,
    }
    metadata_json = _canonical_json(metadata)
    connection.execute(
        """
        INSERT INTO safe_instruments (
            instrument_handle, asset_type, safe_label, source_revision,
            source_timestamp, fingerprint_sha256, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (instrument_handle) DO NOTHING
        """,
        (
            record.instrument_handle,
            asset_type,
            f"{asset_type} position",
            f"row:{record.source_row_sha256}",
            prepared.pages[0].source_timestamp,
            _fingerprint(metadata),
            metadata_json,
        ),
    )


def _invalidate_dependent_analyses(
    connection: duckdb.DuckDBPyConnection,
    alias: str,
    contract_id: str,
) -> int:
    contract = source_contracts_by_id()[contract_id]
    dependent = tuple(contract.dependent_analysis_kinds)
    if not dependent:
        return 0
    rows = cast(
        "list[tuple[object, ...]]",
        connection.execute(
            """
            SELECT analysis_id
            FROM analyses
            WHERE account_scope = ?
              AND analysis_kind = ANY(?)
              AND status != 'invalidated'
            """,
            (alias, list(dependent)),
        ).fetchall(),
    )
    analysis_ids = [row[0] for row in rows if isinstance(row[0], str)]
    if analysis_ids:
        connection.execute(
            "UPDATE analyses SET status = 'invalidated' WHERE analysis_id = ANY(?)",
            (analysis_ids,),
        )
    return len(analysis_ids)


def _ingestion_fingerprints(
    prepared: _PreparedDataset,
    duplicate_count: int,
    correction_count: int,
) -> IngestionFingerprints:
    return IngestionFingerprints(
        raw_pages_sha256=_fingerprint(
            [page.page_fingerprint_sha256 for page in prepared.pages],
        ),
        normalized_rows_sha256=_fingerprint(
            sorted(record.source_row_sha256 for record in prepared.records),
        ),
        source_contract_sha256=_fingerprint(
            sorted({page.contract_sha256 for page in prepared.pages}),
        ),
        entitlements_sha256=_fingerprint(
            [page.source_quality.model_dump(mode="json") for page in prepared.pages],
        ),
        correction_state_sha256=_fingerprint(
            {
                "correction_count": correction_count,
                "duplicate_count": duplicate_count,
                "source_identity_sha256s": sorted(
                    record.source_identity_sha256 for record in prepared.records
                ),
            },
        ),
    )


def _result(  # noqa: PLR0913
    *,
    alias: str,
    summaries: tuple[AccountDatasetSummary, ...],
    invalidated: int,
    request_count: int,
    visibility: VisibilityMode,
    prepared: Sequence[_PreparedDataset],
    response_rows: int,
) -> SyncResult:
    all_private_records = tuple(
        record.private_record() for item in prepared for record in item.records
    )
    truncated = (
        visibility is VisibilityMode.PRIVATE_USER_RESULT
        and len(all_private_records) > response_rows
    )
    if truncated:
        summaries = tuple(
            summary.model_copy(
                update={
                    "warnings": tuple(
                        sorted(
                            {
                                *summary.warnings,
                                "private_result_truncated_to_response_limit",
                            },
                        ),
                    ),
                },
            )
            for summary in summaries
        )
    private_records = (
        all_private_records[:response_rows]
        if visibility is VisibilityMode.PRIVATE_USER_RESULT
        else None
    )
    return AccountSyncResult(
        status=(
            "degraded"
            if truncated
            or any(summary.quality_state is not QualityState.COMPLETE for summary in summaries)
            else "complete"
        ),
        account_alias=alias,
        source_request_count=request_count,
        datasets=summaries,
        invalidated_analysis_count=invalidated,
        visibility=visibility,
        private_records=private_records,
    )


def _instrument_selectors(
    config: AnalyticsConfig,
    handles: Sequence[str],
) -> tuple[_InstrumentSelector, ...]:
    store = AnalyticsStore.open(config)
    store.close()
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        rows = cast(
            "list[tuple[object, ...]]",
            connection.execute(
                """
                SELECT instrument_handle, metadata_json
                FROM safe_instruments
                WHERE instrument_handle = ANY(?)
                """,
                (list(handles),),
            ).fetchall(),
        )
    finally:
        connection.close()
    by_handle: dict[str, _InstrumentSelector] = {}
    for handle, metadata_json in rows:
        if not isinstance(handle, str) or not isinstance(metadata_json, str):
            raise AccountSyncError("stored instrument metadata is invalid")
        try:
            metadata = _JSON_OBJECT_ADAPTER.validate_json(metadata_json, strict=True)
        except ValidationError as error:
            raise AccountSyncError("stored instrument metadata is invalid") from error
        identifier = _optional_integer(metadata.get("identifier"))
        asset_type = _optional_text(metadata.get("asset_type"))
        if identifier is None or asset_type is None:
            raise AccountSyncError("stored instrument selector is unavailable")
        by_handle[handle] = _InstrumentSelector(handle, identifier, asset_type)
    if set(by_handle) != set(handles):
        raise AccountSyncValidationError("cost sync instrument handle is unavailable")
    return tuple(by_handle[handle] for handle in handles)


def _identity_fingerprint(alias: str, source_id: str) -> str:
    return _fingerprint({"account_alias": alias, "source_id": source_id})


def _opaque_row_id(
    data_kind: AccountDataKind,
    alias: str,
    source_identity_sha256: str,
    source_row_sha256: str,
) -> str:
    prefix = {
        "transactions": "tx",
        "bookings": "bk",
        "closed_positions": "cp",
        "costs": "co",
    }[data_kind]
    return f"{prefix}_{_fingerprint([alias, source_identity_sha256, source_row_sha256])}"


def _opaque_handle(prefix: str, material: Mapping[str, object]) -> str:
    digest = bytearray(hashlib.sha256(_canonical_json(dict(material)).encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    opaque_uuid = UUID(bytes=bytes(digest))
    if opaque_uuid.version != _ACCOUNT_ALIAS_VERSION or opaque_uuid.variant != RFC_4122:
        raise AccountSyncError("opaque account-data handle construction failed")
    return f"{prefix}_{opaque_uuid.hex}"


def _required_text(value: object) -> str:
    text = _optional_text(value)
    if text is None:
        raise AccountSyncError("validated account source text is unavailable")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_SOURCE_TEXT_LENGTH:
        raise AccountSyncError("validated account source text is invalid")
    return value


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AccountSyncError("validated account source number is invalid")
    number = float(value)
    if not math.isfinite(number):
        raise AccountSyncError("validated account source number is invalid")
    return number


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountSyncError("validated account source integer is invalid")
    return value


def _optional_mapping(value: object) -> Mapping[str, FrozenSourceJsonValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AccountSyncError("validated account source object is invalid")
    return cast("Mapping[str, FrozenSourceJsonValue]", value)


def _required_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AccountSyncError("validated account source timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise AccountSyncError("validated account source timestamp is invalid") from error
    if timestamp.tzinfo is None:
        raise AccountSyncError("validated account source timestamp has no offset")
    return timestamp.astimezone(UTC)


def _require_utc_range(start: datetime, end: datetime) -> None:
    if (
        start.tzinfo is None
        or start.utcoffset() != timedelta(0)
        or end.tzinfo is None
        or end.utcoffset() != timedelta(0)
        or end < start
    ):
        raise AccountSyncValidationError("account sync requires an ordered UTC range")


def _require_utc_clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AccountSyncValidationError("account capture clock must use UTC")
    return value.astimezone(UTC)


def _thaw_mapping(
    value: Mapping[str, FrozenSourceJsonValue],
) -> dict[str, SourceJsonValue]:
    return {key: _thaw(item) for key, item in value.items()}


def _thaw(value: FrozenSourceJsonValue) -> SourceJsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, FrozenSourceJsonValue]", value)
        return {key: _thaw(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise AccountSyncError("account data is not finite canonical JSON") from error


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


__all__ = (
    "AccountDatasetSummary",
    "AccountScope",
    "AccountSyncError",
    "AccountSyncResult",
    "AccountSyncValidationError",
    "PrivateAccountRecord",
    "SyncResult",
    "new_account_alias",
    "sync_account_history",
    "sync_bookings",
    "sync_closed_positions",
    "sync_cost_sources",
    "sync_transactions",
)
