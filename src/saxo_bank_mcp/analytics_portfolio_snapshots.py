from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast
from uuid import RFC_4122, UUID

import duckdb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from saxo_bank_mcp.analytics_account_data import (
    AccountScope,
    AccountSyncValidationError,
    assert_persisted_account_scope_binding,
    bind_account_scope,
    conservative_ingestion_reservation,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instrument_identity import (
    InstrumentIdentityError,
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_models import (
    DatasetId,
    HandleKind,
    InstrumentHandle,
    PortfolioSnapshotId,
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

_BALANCES_CONTRACT: Final = "balances_v1"
_POSITIONS_CONTRACT: Final = "positions_v1"
_ORDERS_CONTRACT: Final = "orders_v1"
_SNAPSHOT_CONTRACTS: Final = (
    _BALANCES_CONTRACT,
    _POSITIONS_CONTRACT,
    _ORDERS_CONTRACT,
)
_NORMALIZED_ROW_ESTIMATE_BYTES: Final = 256
_SOURCE_PAGE_SIZE: Final = 500
_OPAQUE_UUID_VERSION: Final = 4
_MAX_SOURCE_TEXT_LENGTH: Final = 1_000
_JSON_OBJECT_ADAPTER: Final[TypeAdapter[dict[str, SourceJsonValue]]] = TypeAdapter(
    dict[str, SourceJsonValue],
)

type Clock = Callable[[], datetime]


class PortfolioSnapshotError(RuntimeError):
    """Base error for on-demand owner portfolio snapshots."""


class PortfolioSnapshotValidationError(PortfolioSnapshotError):
    """Raised before source access for an invalid or unsafe snapshot request."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class PrivatePositionValue(_StrictModel):
    """Private current-position values with a safe position alias."""

    position_alias: str = Field(pattern=r"^pa_[0-9a-f]{32}$")
    instrument_handle: InstrumentHandle | None
    amount_value: float | None = Field(allow_inf_nan=False)
    open_price_value: float | None = Field(allow_inf_nan=False)
    current_price_value: float | None = Field(allow_inf_nan=False)
    exposure_value: float | None = Field(allow_inf_nan=False)
    profit_loss_value: float | None = Field(allow_inf_nan=False)


class PrivatePortfolioValues(_StrictModel):
    """Private account values released only to the trusted local owner host."""

    currency: str = Field(min_length=1, max_length=16)
    cash_balance: float = Field(allow_inf_nan=False)
    cash_available_for_trading: float | None = Field(allow_inf_nan=False)
    unsettled_cash: float | None = Field(allow_inf_nan=False)
    funds_available_for_settlement: float | None = Field(allow_inf_nan=False)
    funds_reserved_for_settlement: float | None = Field(allow_inf_nan=False)
    financing_accruals: float | None = Field(allow_inf_nan=False)
    fee_value: float | None = Field(allow_inf_nan=False)
    tax_value: float | None = Field(allow_inf_nan=False)
    deposit_value: float | None = Field(allow_inf_nan=False)
    withdrawal_value: float | None = Field(allow_inf_nan=False)
    total_value: float | None = Field(allow_inf_nan=False)
    positions: tuple[PrivatePositionValue, ...]
    fx_timestamp: UtcDateTime | None = None
    tax_lot_basis_available: Literal[False] = False


class PortfolioSnapshot(_StrictModel):
    """Immutable snapshot handle with private values omitted by default."""

    status: Literal["complete", "degraded"]
    snapshot_id: PortfolioSnapshotId
    dataset_id: DatasetId
    account_alias: SafeAccountScope
    as_of: UtcDateTime
    source_request_count: int = Field(ge=0)
    balance_row_count: int = Field(ge=0)
    position_count: int = Field(ge=0)
    order_count: int = Field(ge=0)
    position_handles: tuple[InstrumentHandle, ...]
    invalidated_analysis_count: int = Field(ge=0)
    warnings: tuple[str, ...]
    fingerprints: IngestionFingerprints
    visibility: VisibilityMode
    private_values: PrivatePortfolioValues | None

    @model_validator(mode="after")
    def require_private_delivery_boundary(self) -> PortfolioSnapshot:
        is_private = self.visibility is VisibilityMode.PRIVATE_USER_RESULT
        if is_private != (self.private_values is not None):
            raise ValueError("private portfolio values do not match result visibility")
        return self


@dataclass(frozen=True, slots=True)
class _Position:
    position_alias: str
    instrument_handle: str | None
    asset_type: str
    identifier: int | None
    amount_value: float | None
    open_price_value: float | None
    current_price_value: float | None
    exposure_value: float | None
    profit_loss_value: float | None

    def private_value(self) -> PrivatePositionValue:
        return PrivatePositionValue(
            position_alias=self.position_alias,
            instrument_handle=self.instrument_handle,
            amount_value=self.amount_value,
            open_price_value=self.open_price_value,
            current_price_value=self.current_price_value,
            exposure_value=self.exposure_value,
            profit_loss_value=self.profit_loss_value,
        )

    def stored_value(self) -> dict[str, SourceJsonValue]:
        return {
            "amount_value": self.amount_value,
            "current_price_value": self.current_price_value,
            "exposure_value": self.exposure_value,
            "instrument_handle": self.instrument_handle,
            "open_price_value": self.open_price_value,
            "position_alias": self.position_alias,
            "profit_loss_value": self.profit_loss_value,
        }


@dataclass(frozen=True, slots=True)
class _SnapshotValues:
    currency: str
    cash_balance: float
    cash_available_for_trading: float | None
    unsettled_cash: float | None
    funds_available_for_settlement: float | None
    funds_reserved_for_settlement: float | None
    financing_accruals: float | None
    fee_value: float | None
    tax_value: float | None
    deposit_value: float | None
    withdrawal_value: float | None
    total_value: float | None
    positions: tuple[_Position, ...]
    order_identity_sha256s: tuple[str, ...]

    def private_values(self, response_rows: int) -> PrivatePortfolioValues:
        return PrivatePortfolioValues(
            currency=self.currency,
            cash_balance=self.cash_balance,
            cash_available_for_trading=self.cash_available_for_trading,
            unsettled_cash=self.unsettled_cash,
            funds_available_for_settlement=self.funds_available_for_settlement,
            funds_reserved_for_settlement=self.funds_reserved_for_settlement,
            financing_accruals=self.financing_accruals,
            fee_value=self.fee_value,
            tax_value=self.tax_value,
            deposit_value=self.deposit_value,
            withdrawal_value=self.withdrawal_value,
            total_value=self.total_value,
            positions=tuple(
                position.private_value() for position in self.positions[:response_rows]
            ),
            fx_timestamp=None,
            tax_lot_basis_available=False,
        )

    def stored_payload(self, material_fingerprint_sha256: str) -> dict[str, SourceJsonValue]:
        return {
            "cash_available_for_trading": self.cash_available_for_trading,
            "cash_balance": self.cash_balance,
            "currency": self.currency,
            "deposit_value": self.deposit_value,
            "fee_value": self.fee_value,
            "financing_accruals": self.financing_accruals,
            "funds_available_for_settlement": self.funds_available_for_settlement,
            "funds_reserved_for_settlement": self.funds_reserved_for_settlement,
            "fx_timestamp": None,
            "material_fingerprint_sha256": material_fingerprint_sha256,
            "order_identity_sha256s": list(self.order_identity_sha256s),
            "positions": [position.stored_value() for position in self.positions],
            "tax_lot_basis_available": False,
            "tax_value": self.tax_value,
            "total_value": self.total_value,
            "unsettled_cash": self.unsettled_cash,
            "withdrawal_value": self.withdrawal_value,
        }


async def capture_portfolio_snapshot(  # noqa: PLR0913
    scope: AccountScope,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
    visibility: VisibilityMode = VisibilityMode.FINGERPRINT_ONLY,
    trusted_local_host: bool = False,
    clock: Clock = lambda: datetime.now(UTC),
    request_budget: SourceRequestBudget | None = None,
) -> PortfolioSnapshot:
    """Capture one immutable balances, positions, and orders snapshot on demand."""
    validated_scope = _preflight(
        scope,
        provider,
        visibility,
        trusted_local_host=trusted_local_host,
    )
    try:
        assert_persisted_account_scope_binding(config, validated_scope)
    except AccountSyncValidationError as error:
        raise PortfolioSnapshotValidationError(str(error)) from error
    budget = _source_budget(config, request_budget)
    request_count_start = budget.used
    captured_at = _require_utc_clock(clock())
    requests = _snapshot_requests(validated_scope)
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
        raise PortfolioSnapshotValidationError("snapshot row count exceeds its fixed limit")
    values = _normalize_snapshot(pages_by_contract, validated_scope.alias)
    material_fingerprint = _material_fingerprint(pages_by_contract)
    warnings = _snapshot_warnings(values, envelope.pages)
    identity_unavailable = any(position.instrument_handle is None for position in values.positions)
    truncated = len(values.positions) > config.limits.response_rows
    if truncated:
        warnings = tuple(
            sorted({*warnings, "position_result_truncated_to_response_limit"}),
        )
    snapshot_id, dataset_id, invalidated = _persist_snapshot(
        config=config,
        scope=validated_scope,
        pages=envelope.pages,
        values=values,
        material_fingerprint_sha256=material_fingerprint,
        captured_at=captured_at,
    )
    fingerprints = _snapshot_fingerprints(
        envelope.pages,
        material_fingerprint,
    )
    return PortfolioSnapshot(
        status=(
            "degraded"
            if truncated
            or identity_unavailable
            or any(page.source_quality.state == "limited" for page in envelope.pages)
            else "complete"
        ),
        snapshot_id=snapshot_id,
        dataset_id=dataset_id,
        account_alias=validated_scope.alias,
        as_of=captured_at,
        source_request_count=budget.used - request_count_start,
        balance_row_count=sum(page.row_count for page in pages_by_contract[_BALANCES_CONTRACT]),
        position_count=len(values.positions),
        order_count=len(values.order_identity_sha256s),
        position_handles=tuple(
            position.instrument_handle
            for position in values.positions
            if position.instrument_handle is not None
        )[: config.limits.response_rows],
        invalidated_analysis_count=invalidated,
        warnings=warnings,
        fingerprints=fingerprints,
        visibility=visibility,
        private_values=(
            values.private_values(config.limits.response_rows)
            if visibility is VisibilityMode.PRIVATE_USER_RESULT
            else None
        ),
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
        raise PortfolioSnapshotValidationError("account scope is invalid") from error
    if type(provider) is not SaxoAnalyticsProvider:
        raise TypeError("portfolio snapshots require SaxoAnalyticsProvider")
    if visibility is VisibilityMode.PRIVATE_USER_RESULT and not trusted_local_host:
        raise PortfolioSnapshotValidationError(
            "private_user_result requires the trusted local host",
        )
    return validated_scope


def _source_budget(
    config: AnalyticsConfig,
    supplied: SourceRequestBudget | None,
) -> SourceRequestBudget:
    if supplied is None:
        return SourceRequestBudget(config.limits.sync_instruments)
    if type(supplied) is not SourceRequestBudget:
        raise PortfolioSnapshotValidationError("source request budget is invalid")
    if supplied.limit != config.limits.sync_instruments:
        raise PortfolioSnapshotValidationError(
            "source request budget must use the fixed sync limit",
        )
    return supplied


def _snapshot_requests(scope: AccountScope) -> dict[str, dict[str, object]]:
    account_key = scope.account_key.get_secret_value()
    client_key = scope.client_key.get_secret_value()
    return {
        _BALANCES_CONTRACT: {
            "AccountKey": account_key,
            "ClientKey": client_key,
        },
        _POSITIONS_CONTRACT: {
            "$top": _SOURCE_PAGE_SIZE,
            "AccountKey": account_key,
            "ClientKey": client_key,
        },
        _ORDERS_CONTRACT: {
            "$top": _SOURCE_PAGE_SIZE,
            "AccountKey": account_key,
            "ClientKey": client_key,
        },
    }


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
        raise PortfolioSnapshotValidationError(
            "portfolio snapshot source budget was exhausted",
        ) from error


def _normalize_snapshot(
    pages_by_contract: Mapping[str, tuple[SourcePage, ...]],
    alias: str,
) -> _SnapshotValues:
    balance_rows = tuple(row for page in pages_by_contract[_BALANCES_CONTRACT] for row in page.rows)
    if len(balance_rows) != 1:
        raise PortfolioSnapshotError("snapshot requires exactly one balances row")
    balance = balance_rows[0]
    detail = _optional_mapping(balance.get("TransactionsNotBookedDetail"))
    positions = tuple(
        sorted(
            (
                _normalize_position(row, alias)
                for page in pages_by_contract[_POSITIONS_CONTRACT]
                for row in page.rows
            ),
            key=lambda position: (
                position.instrument_handle or "",
                position.position_alias,
            ),
        ),
    )
    order_hashes = tuple(
        sorted(
            _fingerprint(
                {
                    "account_alias": alias,
                    "order_id": _required_text(row.get("OrderId")),
                },
            )
            for page in pages_by_contract[_ORDERS_CONTRACT]
            for row in page.rows
        ),
    )
    return _SnapshotValues(
        currency=_required_text(balance.get("Currency")),
        cash_balance=_required_number(balance.get("CashBalance")),
        cash_available_for_trading=_optional_number(
            balance.get("CashAvailableForTrading"),
        ),
        unsettled_cash=_optional_number(balance.get("TransactionsNotBooked")),
        funds_available_for_settlement=_optional_number(
            balance.get("FundsAvailableForSettlement"),
        ),
        funds_reserved_for_settlement=_optional_number(
            balance.get("FundsReservedForSettlement"),
        ),
        financing_accruals=_optional_number(balance.get("FinancingAccruals")),
        fee_value=_sum_optional(
            detail,
            (
                "AdditionalTransactionCost",
                "Commission",
                "ExchangeFee",
                "ExternalCharges",
                "IpoSubscriptionFee",
            ),
        ),
        tax_value=_optional_number(detail.get("StampDuty")),
        deposit_value=_optional_number(detail.get("CashDeposit")),
        withdrawal_value=_optional_number(detail.get("CashWithdrawal")),
        total_value=_optional_number(balance.get("TotalValue")),
        positions=positions,
        order_identity_sha256s=order_hashes,
    )


def _normalize_position(
    row: Mapping[str, FrozenSourceJsonValue],
    alias: str,
) -> _Position:
    position_id = _required_text(row.get("PositionId"))
    base = _optional_mapping(row.get("PositionBase"))
    view = _optional_mapping(row.get("PositionView"))
    asset_type = _optional_text(base.get("AssetType")) or "Unknown"
    identifier = _optional_integer(base.get("Uic"))
    return _Position(
        position_alias=_opaque_position_alias(
            {"account_alias": alias, "position_id": position_id},
        ),
        instrument_handle=(
            None
            if identifier is None
            else instrument_handle_for_saxo_identity(asset_type, identifier)
        ),
        asset_type=asset_type,
        identifier=identifier,
        amount_value=_optional_number(base.get("Amount")),
        open_price_value=_optional_number(base.get("OpenPrice")),
        current_price_value=_optional_number(view.get("CurrentPrice")),
        exposure_value=_optional_number(view.get("Exposure")),
        profit_loss_value=_optional_number(view.get("ProfitLossOnTrade")),
    )


def _snapshot_warnings(
    values: _SnapshotValues,
    pages: Sequence[SourcePage],
) -> tuple[str, ...]:
    warnings = {
        "fx_conversion_timestamp_unavailable",
        "tax_lot_basis_unavailable",
    }
    if values.unsettled_cash not in {None, 0.0}:
        warnings.add("unsettled_cash_present")
    if any(page.source_quality.state == "limited" for page in pages):
        warnings.add("source_quality_limited")
    if any(position.instrument_handle is None for position in values.positions):
        warnings.add("position_instrument_unavailable")
    return tuple(sorted(warnings))


def _material_fingerprint(
    pages_by_contract: Mapping[str, tuple[SourcePage, ...]],
) -> str:
    return _fingerprint(
        {
            contract_id: sorted(
                _fingerprint(_thaw_mapping(row))
                for page in pages_by_contract[contract_id]
                for row in page.rows
            )
            for contract_id in _SNAPSHOT_CONTRACTS
        },
    )


def _persist_snapshot(  # noqa: PLR0913
    *,
    config: AnalyticsConfig,
    scope: AccountScope,
    pages: Sequence[SourcePage],
    values: _SnapshotValues,
    material_fingerprint_sha256: str,
    captured_at: datetime,
) -> tuple[str, str, int]:
    if not pages:
        raise PortfolioSnapshotError("portfolio capture contains no source page")
    alias = scope.alias
    payload = values.stored_payload(material_fingerprint_sha256)
    payload_bytes = len(_canonical_json(payload).encode())
    reservation = conservative_ingestion_reservation(
        raw_payload_bytes=(
            payload_bytes + sum(len(page.model_dump_json().encode()) for page in pages)
        ),
        normalized_rows=sum(page.row_count for page in pages),
        source_pages=len(pages),
        datasets=1,
        snapshots=1,
    )
    try:
        AnalyticsStore.ensure_owner_capacity(config, reservation)
    except StoreQuotaError as error:
        raise PortfolioSnapshotValidationError(
            "analytics store quota refuses portfolio snapshot",
        ) from error
    store = AnalyticsStore.open(config)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    snapshot_id = new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID)
    invalidated = 0
    try:
        try:
            with store.market_ingestion_transaction(reservation) as connection:
                try:
                    bind_account_scope(connection, scope)
                except AccountSyncValidationError as error:
                    raise PortfolioSnapshotValidationError(str(error)) from error
                page_ids = _persist_source_pages(store, pages, alias)
                for position in values.positions:
                    _put_safe_position(
                        connection,
                        position,
                        source_revision=pages[0].capture_revision,
                        source_timestamp=captured_at,
                    )
                previous_material = _latest_material_fingerprint(connection, alias)
                if (
                    previous_material is not None
                    and previous_material != material_fingerprint_sha256
                ):
                    invalidated = _invalidate_snapshot_dependents(connection, alias)
                store.create_dataset(
                    dataset_id=dataset_id,
                    account_scope=alias,
                    source_scope="saxo_openapi",
                    source_revision=pages[0].capture_revision,
                    source_page_ids=page_ids,
                    created_at=captured_at,
                    coverage_start=captured_at,
                    coverage_end=captured_at,
                    quality_state=_dataset_quality(pages),
                )
                store.create_snapshot(
                    snapshot_id=snapshot_id,
                    dataset_id=dataset_id,
                    snapshot_kind="portfolio",
                    account_scope=alias,
                    source_revision=pages[0].capture_revision,
                    as_of=captured_at,
                    payload=payload,
                )
        except StoreQuotaError as error:
            raise PortfolioSnapshotValidationError(
                "analytics store quota refuses portfolio snapshot",
            ) from error
    finally:
        store.close()
    return snapshot_id, dataset_id, invalidated


def _persist_source_pages(
    store: AnalyticsStore,
    pages: Sequence[SourcePage],
    alias: str,
) -> tuple[str, ...]:
    page_ids: list[str] = []
    for page in pages:
        serialized = page.model_dump(mode="json")
        source_payload = _JSON_OBJECT_ADAPTER.validate_python(serialized, strict=True)
        rows = source_payload.get("rows")
        if not isinstance(rows, list):
            raise PortfolioSnapshotError("validated source page rows are unavailable")
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
            instrument_handle=None,
            instrument_scope_sha256=page.instrument_scope_sha256,
        )
        page_ids.append(stored.page_id)
    return tuple(page_ids)


def _dataset_quality(pages: Sequence[SourcePage]) -> QualityState:
    return (
        QualityState.PARTIAL
        if any(page.source_quality.state == "limited" for page in pages)
        else QualityState.COMPLETE
    )


def _put_safe_position(
    connection: duckdb.DuckDBPyConnection,
    position: _Position,
    *,
    source_revision: str,
    source_timestamp: datetime,
) -> None:
    if position.identifier is None or position.instrument_handle is None:
        return
    metadata: dict[str, SourceJsonValue] = {
        "aliases": [],
        "asset_type": position.asset_type,
        "display_label": f"{position.asset_type} position",
        "exchange": None,
        "identifier": position.identifier,
        "symbol": None,
    }
    metadata_json = _canonical_json(metadata)
    try:
        write = put_saxo_instrument_identity(
            connection,
            asset_type=position.asset_type,
            uic=position.identifier,
            safe_label=f"{position.asset_type} position",
            source_revision=source_revision,
            source_timestamp=source_timestamp,
            fingerprint_sha256=_fingerprint(metadata),
            metadata_json=metadata_json,
            update_existing=False,
        )
    except InstrumentIdentityError as error:
        raise PortfolioSnapshotError(str(error)) from error
    if write.instrument_handle != position.instrument_handle:
        raise PortfolioSnapshotError("portfolio instrument identity is inconsistent")


def _latest_material_fingerprint(
    connection: duckdb.DuckDBPyConnection,
    alias: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT payload_json
        FROM account_snapshots
        WHERE account_scope = ? AND snapshot_kind = 'portfolio'
        ORDER BY as_of DESC, created_order DESC NULLS LAST, rowid DESC
        LIMIT 1
        """,
        (alias,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    try:
        payload = _JSON_OBJECT_ADAPTER.validate_json(row[0], strict=True)
    except ValidationError as error:
        raise PortfolioSnapshotError("stored portfolio snapshot is invalid") from error
    fingerprint = payload.get("material_fingerprint_sha256")
    if not isinstance(fingerprint, str):
        raise PortfolioSnapshotError("stored portfolio material fingerprint is invalid")
    return fingerprint


def _invalidate_snapshot_dependents(
    connection: duckdb.DuckDBPyConnection,
    alias: str,
) -> int:
    dependent = tuple(
        sorted(
            {
                analysis_kind
                for contract_id in _SNAPSHOT_CONTRACTS
                for analysis_kind in source_contracts_by_id()[contract_id].dependent_analysis_kinds
            },
        ),
    )
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


def _snapshot_fingerprints(
    pages: Sequence[SourcePage],
    material_fingerprint_sha256: str,
) -> IngestionFingerprints:
    return IngestionFingerprints(
        raw_pages_sha256=_fingerprint(
            sorted(page.page_fingerprint_sha256 for page in pages),
        ),
        normalized_rows_sha256=material_fingerprint_sha256,
        source_contract_sha256=_fingerprint(
            sorted({page.contract_sha256 for page in pages}),
        ),
        entitlements_sha256=_fingerprint(
            [page.source_quality.model_dump(mode="json") for page in pages],
        ),
        correction_state_sha256=_fingerprint(
            {"material_fingerprint_sha256": material_fingerprint_sha256},
        ),
    )


def _sum_optional(
    values: Mapping[str, FrozenSourceJsonValue],
    fields: Sequence[str],
) -> float | None:
    observed = tuple(
        value for field in fields if (value := _optional_number(values.get(field))) is not None
    )
    return sum(observed) if observed else None


def _required_text(value: object) -> str:
    text = _optional_text(value)
    if text is None:
        raise PortfolioSnapshotError("validated snapshot source text is unavailable")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_SOURCE_TEXT_LENGTH:
        raise PortfolioSnapshotError("validated snapshot source text is invalid")
    return value


def _required_number(value: object) -> float:
    number = _optional_number(value)
    if number is None:
        raise PortfolioSnapshotError("validated snapshot source number is unavailable")
    return number


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PortfolioSnapshotError("validated snapshot source number is invalid")
    number = float(value)
    if not math.isfinite(number):
        raise PortfolioSnapshotError("validated snapshot source number is invalid")
    return number


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PortfolioSnapshotError("validated snapshot source integer is invalid")
    return value


def _optional_mapping(value: object) -> Mapping[str, FrozenSourceJsonValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PortfolioSnapshotError("validated snapshot source object is invalid")
    return cast("Mapping[str, FrozenSourceJsonValue]", value)


def _require_utc_clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PortfolioSnapshotValidationError("portfolio capture clock must use UTC")
    return value.astimezone(UTC)


def _opaque_position_alias(material: Mapping[str, object]) -> str:
    digest = bytearray(hashlib.sha256(_canonical_json(dict(material)).encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    opaque_uuid = UUID(bytes=bytes(digest))
    if opaque_uuid.version != _OPAQUE_UUID_VERSION or opaque_uuid.variant != RFC_4122:
        raise PortfolioSnapshotError("opaque portfolio handle construction failed")
    return f"pa_{opaque_uuid.hex}"


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
        raise PortfolioSnapshotError("portfolio data is not finite canonical JSON") from error


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


__all__ = (
    "PortfolioSnapshot",
    "PortfolioSnapshotError",
    "PortfolioSnapshotValidationError",
    "PrivatePortfolioValues",
    "PrivatePositionValue",
    "capture_portfolio_snapshot",
)
