# ruff: noqa: PLR2004

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from saxo_bank_mcp.analytics_account_data import (
    AccountScope,
    AccountSyncResult,
    AccountSyncValidationError,
    new_account_alias,
    sync_account_history,
    sync_cost_sources,
    sync_transactions,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_models import HandleKind, VisibilityMode, new_safe_handle
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.endpoint_registry import EndpointOperation

_START = datetime(2026, 7, 1, tzinfo=UTC)
_END = datetime(2026, 7, 31, 23, 59, tzinfo=UTC)
_CAPTURED_AT = datetime(2026, 8, 1, 8, tzinfo=UTC)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class _PayloadExecutor:
    def __init__(self, payloads: Sequence[Mapping[str, object]]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        self.calls.append((operation.operation_id, request_target, dict(params)))
        payload = self._payloads.pop(0)
        return httpx2.Response(
            200,
            content=json.dumps(payload).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


def _scope(alias: str | None = None) -> AccountScope:
    return AccountScope(
        alias=alias or new_account_alias(),
        account_key=SecretStr("mocked-account-key"),
        client_key=SecretStr("mocked-client-key"),
    )


def _transactions(*rows: Mapping[str, object]) -> Mapping[str, object]:
    return {"Data": list(rows)}


def _transaction(
    transaction_id: str,
    transaction_type: str,
    amount: float,
    *,
    execution_time: str = "2026-07-15T10:00:00Z",
) -> Mapping[str, object]:
    return {
        "Amount": amount,
        "ExecutionTime": execution_time,
        "TransactionId": transaction_id,
        "TransactionType": transaction_type,
    }


def _category_counts(result: AccountSyncResult) -> dict[str, int]:
    summary = result.datasets[0]
    return {item.category: item.row_count for item in summary.category_counts}


def _seed_analysis(
    config: AnalyticsConfig,
    *,
    dataset_id: str,
    account_alias: str,
    analysis_kind: str,
) -> str:
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            """
            INSERT INTO analyses (
                analysis_id,
                dataset_id,
                account_scope,
                analysis_kind,
                status,
                source_revision,
                as_of,
                created_at,
                byte_count,
                fingerprint_sha256,
                result_json
            )
            VALUES (?, ?, ?, ?, 'verified', 'fixture-revision', ?, ?, 2, ?, '{}')
            """,
            (
                analysis_id,
                dataset_id,
                account_alias,
                analysis_kind,
                _CAPTURED_AT,
                _CAPTURED_AT,
                "a" * 64,
            ),
        )
    finally:
        connection.close()
    return analysis_id


@pytest.mark.anyio
async def test_transactions_classify_cash_flows_income_fees_and_partial_fills(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    executor = _PayloadExecutor(
        (
            _transactions(
                _transaction("synthetic-deposit", "CashTransfer", 1000.0),
                _transaction("synthetic-withdrawal", "CashTransfer", -200.0),
                _transaction("synthetic-fee", "Fee", -3.0),
                _transaction("synthetic-dividend", "CorporateAction", 7.0),
                _transaction("synthetic-fill-a", "Trade", -10.0),
                _transaction("synthetic-fill-b", "Trade", -20.0),
            ),
        ),
    )

    result = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "complete"
    assert result.account_alias == scope.alias
    assert result.source_request_count == 1
    assert result.private_records is None
    assert _category_counts(result) == {
        "deposit": 1,
        "dividend_or_corporate_action_income": 1,
        "fee": 1,
        "trade": 2,
        "withdrawal": 1,
    }
    summary = result.datasets[0]
    assert summary.row_count == 6
    assert summary.correction_count == 0
    assert summary.duplicate_count == 0
    assert all(_SHA256.fullmatch(value) for value in summary.fingerprints.model_dump().values())
    public_json = result.model_dump_json()
    assert "1000.0" not in public_json
    assert "synthetic-deposit" not in public_json
    assert scope.account_key.get_secret_value() not in public_json
    assert scope.client_key.get_secret_value() not in public_json

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT transaction_id, payload_json FROM transactions ORDER BY transaction_id",
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 6
    assert sum('"category":"trade"' in str(row[1]) for row in rows) == 2
    assert all("synthetic-" not in str(row[0]) for row in rows)


@pytest.mark.anyio
async def test_private_transaction_values_require_the_trusted_local_host(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    refused_executor = _PayloadExecutor((_transactions(),))

    with pytest.raises(AccountSyncValidationError, match="trusted local host"):
        await sync_transactions(
            scope,
            _START,
            _END,
            provider=SaxoAnalyticsProvider(request_executor=refused_executor),
            config=config,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=False,
            clock=lambda: _CAPTURED_AT,
        )

    assert refused_executor.calls == []
    executor = _PayloadExecutor(
        (_transactions(_transaction("synthetic-private", "Fee", -4.5)),),
    )
    result = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.private_records is not None
    record = result.private_records[0]
    assert record.amount_value == -4.5
    assert record.fx_timestamp is None
    assert record.tax_lot_basis_available is False
    assert set(result.datasets[0].warnings) >= {
        "currency_unavailable",
        "fx_conversion_timestamp_unavailable",
        "tax_lot_basis_unavailable",
    }


@pytest.mark.anyio
async def test_duplicate_retry_is_a_no_op_but_correction_is_retained_and_invalidates(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    original = _transaction("synthetic-corrected", "CashTransfer", 10.0)

    first = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor((_transactions(original),)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    analysis_id = _seed_analysis(
        config,
        dataset_id=first.datasets[0].dataset_id,
        account_alias=scope.alias,
        analysis_kind="portfolio_performance",
    )
    duplicate = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor((_transactions(original),)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    corrected = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (_transactions(_transaction("synthetic-corrected", "CashTransfer", 11.0)),),
            ),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert duplicate.datasets[0].duplicate_count == 1
    assert duplicate.datasets[0].correction_count == 0
    assert duplicate.invalidated_analysis_count == 0
    assert corrected.datasets[0].duplicate_count == 0
    assert corrected.datasets[0].correction_count == 1
    assert corrected.invalidated_analysis_count == 1
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        count = connection.execute(
            "SELECT count(*) FROM transactions WHERE account_scope = ?",
            (scope.alias,),
        ).fetchone()
        status = connection.execute(
            "SELECT status FROM analyses WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
    finally:
        connection.close()
    assert count == (2,)
    assert status == ("invalidated",)


@pytest.mark.anyio
async def test_same_source_identity_stays_isolated_between_account_aliases(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first_scope = _scope()
    second_scope = _scope()

    first = await sync_transactions(
        first_scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (_transactions(_transaction("synthetic-shared", "CashTransfer", 10.0)),),
            ),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    second = await sync_transactions(
        second_scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (_transactions(_transaction("synthetic-shared", "CashTransfer", -10.0)),),
            ),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert first.account_alias != second.account_alias
    assert first.datasets[0].correction_count == 0
    assert second.datasets[0].correction_count == 0
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT account_scope, count(*)
            FROM transactions
            GROUP BY account_scope
            ORDER BY account_scope
            """,
        ).fetchall()
    finally:
        connection.close()
    assert rows == sorted([(first_scope.alias, 1), (second_scope.alias, 1)])


@pytest.mark.anyio
async def test_account_history_syncs_transactions_bookings_and_closed_positions(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    executor = _PayloadExecutor(
        (
            _transactions(_transaction("synthetic-trade", "Trade", -25.0)),
            {
                "Data": [
                    {
                        "Amount": -2.0,
                        "BookingDate": "2026-07-16T10:00:00Z",
                        "BookingId": "synthetic-booking",
                    },
                ],
            },
            {
                "Data": [
                    {
                        "ClosedPosition": {
                            "ClosedProfitLoss": 8.0,
                            "ExecutionTimeClose": "2026-07-17T10:00:00Z",
                        },
                        "ClosedPositionId": "synthetic-closed-position",
                        "ClosingPosition": {
                            "Amount": 1.0,
                            "AssetType": "Stock",
                            "Uic": 1001,
                        },
                    },
                ],
            },
        ),
    )

    result = await sync_account_history(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.source_request_count == 3
    assert [item.data_kind for item in result.datasets] == [
        "transactions",
        "bookings",
        "closed_positions",
    ]
    assert [item.row_count for item in result.datasets] == [1, 1, 1]
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM transactions),
                (SELECT count(*) FROM bookings),
                (SELECT count(*) FROM closed_positions),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (1, 1, 1, 3)


def _seed_instrument(config: AnalyticsConfig) -> str:
    handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    store = AnalyticsStore.open(config)
    store.close()
    metadata = {
        "aliases": ["fixture"],
        "asset_type": "Stock",
        "display_label": "Fixture instrument",
        "exchange": "XNAS",
        "identifier": 1001,
        "symbol": "FIX",
    }
    metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            """
            INSERT INTO safe_instruments (
                instrument_handle,
                asset_type,
                safe_label,
                source_revision,
                source_timestamp,
                fingerprint_sha256,
                metadata_json
            )
            VALUES (?, 'Stock', 'Fixture instrument', 'fixture-revision', ?, ?, ?)
            """,
            (handle, _CAPTURED_AT, "b" * 64, metadata_json),
        )
    finally:
        connection.close()
    return handle


@pytest.mark.anyio
async def test_cost_sources_preserve_fee_tax_and_missing_fx_time_privately(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    handle = _seed_instrument(config)
    executor = _PayloadExecutor(
        (
            {
                "Cost": {"Commission": 2.0, "StampDuty": 1.0, "TotalCost": 3.0},
                "Currency": "DKK",
                "HoldingPeriodInDays": 1,
            },
        ),
    )

    result = await sync_cost_sources(
        scope,
        (handle,),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.source_request_count == 1
    assert result.private_records is not None
    record = result.private_records[0]
    assert record.amount_value == 3.0
    assert record.fee_value == 2.0
    assert record.tax_value == 1.0
    assert record.currency == "DKK"
    assert record.fx_timestamp is None
    assert record.tax_lot_basis_available is False


def test_account_alias_is_opaque_and_selectors_are_secret() -> None:
    scope = _scope()

    assert re.fullmatch(r"aa_[0-9a-f]{32}", scope.alias)
    assert scope.account_key.get_secret_value() not in repr(scope)
    assert scope.client_key.get_secret_value() not in repr(scope)
    with pytest.raises(ValidationError, match="safe account alias"):
        AccountScope(
            alias="raw-account-name",
            account_key=SecretStr("mocked-account-key"),
            client_key=SecretStr("mocked-client-key"),
        )
