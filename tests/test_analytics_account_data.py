# ruff: noqa: PLR2004

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import duckdb
import httpx2
import pytest
from analytics_legacy_alias_support import migrate_v2_store_with_unbound_alias
from pydantic import SecretStr, ValidationError

from saxo_bank_mcp.analytics_account_data import (
    AccountScope,
    AccountSyncResult,
    AccountSyncValidationError,
    new_account_alias,
    sync_account_analysis_sources,
    sync_account_history,
    sync_closed_positions,
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
    def __init__(
        self,
        payloads: Sequence[Mapping[str, object] | list[Mapping[str, object]]],
    ) -> None:
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


def _all_local_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


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


def _closed_position(
    position_id: str,
    *,
    closed_at: str | None,
    uic: int | None,
) -> Mapping[str, object]:
    closed: dict[str, object] = {"ClosedProfitLoss": 8.0}
    if closed_at is not None:
        closed["ExecutionTimeClose"] = closed_at
    closing: dict[str, object] = {"Amount": 1.0, "AssetType": "Stock"}
    if uic is not None:
        closing["Uic"] = uic
    result: dict[str, object] = {
        "Amount": 1.0,
        "AssetType": "Stock",
        "ClosePositionId": position_id,
        "ClosedPosition": closed,
        "ClosedPositionId": position_id,
        "ClosingPosition": closing,
        "PnLAccountCurrency": 8.0,
    }
    if closed_at is not None:
        result["TradeDateClose"] = closed_at
    return result


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
async def test_new_transaction_invalidates_dependents_but_its_duplicate_does_not(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    original = _transaction("synthetic-existing", "Trade", -10.0)
    added = _transaction("synthetic-added", "Fee", -2.0)
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
    changed_analysis = _seed_analysis(
        config,
        dataset_id=first.datasets[0].dataset_id,
        account_alias=scope.alias,
        analysis_kind="portfolio_performance",
    )

    changed = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor((_transactions(original, added),)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT + timedelta(minutes=1),
    )
    duplicate_analysis = _seed_analysis(
        config,
        dataset_id=changed.datasets[0].dataset_id,
        account_alias=scope.alias,
        analysis_kind="portfolio_performance",
    )
    duplicate = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor((_transactions(original, added),)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT + timedelta(minutes=2),
    )

    assert changed.datasets[0].correction_count == 0
    assert changed.invalidated_analysis_count == 1
    assert duplicate.datasets[0].duplicate_count == 2
    assert duplicate.invalidated_analysis_count == 0
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        statuses = dict(
            connection.execute(
                "SELECT analysis_id, status FROM analyses WHERE analysis_id = ANY(?)",
                ([changed_analysis, duplicate_analysis],),
            ).fetchall(),
        )
    finally:
        connection.close()
    assert statuses == {
        changed_analysis: "invalidated",
        duplicate_analysis: "verified",
    }


@pytest.mark.anyio
async def test_newly_valid_closed_position_invalidates_dependent_analysis(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    unavailable = _closed_position(
        "synthetic-late-valid",
        closed_at="2026-07-17T10:00:00Z",
        uic=None,
    )
    first = await sync_closed_positions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(({"Data": [unavailable]},)),
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

    valid = _closed_position(
        "synthetic-late-valid",
        closed_at="2026-07-17T10:00:00Z",
        uic=1001,
    )
    changed = await sync_closed_positions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(({"Data": [valid]},)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT + timedelta(minutes=1),
    )

    assert changed.datasets[0].correction_count == 0
    assert changed.invalidated_analysis_count == 1
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        normalized = connection.execute("SELECT count(*) FROM closed_positions").fetchone()
        status = connection.execute(
            "SELECT status FROM analyses WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
    finally:
        connection.close()
    assert normalized == (1,)
    assert status == ("invalidated",)


@pytest.mark.anyio
async def test_account_alias_binding_rejects_changed_account_or_client_before_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(request_executor=_PayloadExecutor((_transactions(),))),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        binding = connection.execute(
            """
            SELECT account_selector_sha256, client_selector_sha256
            FROM account_scope_bindings
            WHERE account_scope = ?
            """,
            (scope.alias,),
        ).fetchone()
        counts_before = connection.execute(
            "SELECT (SELECT count(*) FROM source_pages), (SELECT count(*) FROM datasets)",
        ).fetchone()
    finally:
        connection.close()
    assert binding is not None
    assert all(isinstance(value, str) and _SHA256.fullmatch(value) for value in binding)
    assert scope.account_key.get_secret_value() not in str(binding)
    assert scope.client_key.get_secret_value() not in str(binding)

    changed_scopes = (
        AccountScope(
            alias=scope.alias,
            account_key=SecretStr("changed-account-key"),
            client_key=scope.client_key,
        ),
        AccountScope(
            alias=scope.alias,
            account_key=scope.account_key,
            client_key=SecretStr("changed-client-key"),
        ),
    )
    for changed_scope in changed_scopes:
        executor = _PayloadExecutor((_transactions(),))
        with pytest.raises(AccountSyncValidationError, match="account alias binding"):
            await sync_transactions(
                changed_scope,
                _START,
                _END,
                provider=SaxoAnalyticsProvider(request_executor=executor),
                config=config,
                clock=lambda: _CAPTURED_AT + timedelta(minutes=1),
            )
        assert executor.calls == []

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts_after = connection.execute(
            "SELECT (SELECT count(*) FROM source_pages), (SELECT count(*) FROM datasets)",
        ).fetchone()
    finally:
        connection.close()
    assert counts_after == counts_before


@pytest.mark.anyio
async def test_migrated_v2_alias_refuses_wrong_first_selectors_before_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    alias = new_account_alias()
    migrate_v2_store_with_unbound_alias(config, alias, data_kind="transaction")
    scope = AccountScope(
        alias=alias,
        account_key=SecretStr("wrong-first-account-key"),
        client_key=SecretStr("wrong-first-client-key"),
    )
    executor = _PayloadExecutor((_transactions(),))

    with pytest.raises(AccountSyncValidationError) as raised:
        await sync_transactions(
            scope,
            _START,
            _END,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    message = str(raised.value)
    assert "explicit deletion and reimport or an approved rebinding workflow" in message
    assert alias not in message
    assert scope.account_key.get_secret_value() not in message
    assert scope.client_key.get_secret_value() not in message
    assert executor.calls == []
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM account_scope_bindings),
                (SELECT count(*) FROM transactions WHERE account_scope = ?),
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM datasets)
            """,
            (alias,),
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 1, 0, 0)


@pytest.mark.anyio
async def test_migrated_v2_alias_refuses_original_first_selectors_before_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    alias = new_account_alias()
    migrate_v2_store_with_unbound_alias(config, alias, data_kind="transaction")
    scope = _scope(alias)
    executor = _PayloadExecutor((_transactions(),))

    with pytest.raises(AccountSyncValidationError) as raised:
        await sync_transactions(
            scope,
            _START,
            _END,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    message = str(raised.value)
    assert "explicit deletion and reimport or an approved rebinding workflow" in message
    assert alias not in message
    assert scope.account_key.get_secret_value() not in message
    assert scope.client_key.get_secret_value() not in message
    assert executor.calls == []
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM account_scope_bindings),
                (SELECT count(*) FROM transactions WHERE account_scope = ?),
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM datasets)
            """,
            (alias,),
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 1, 0, 0)


@pytest.mark.anyio
async def test_new_alias_binds_when_migrated_store_has_an_unbound_legacy_alias(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    legacy_alias = new_account_alias()
    migrate_v2_store_with_unbound_alias(config, legacy_alias, data_kind="transaction")
    scope = _scope()
    executor = _PayloadExecutor((_transactions(),))

    result = await sync_transactions(
        scope,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "complete"
    assert len(executor.calls) == 1
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        bindings = connection.execute(
            "SELECT account_scope FROM account_scope_bindings ORDER BY account_scope",
        ).fetchall()
    finally:
        connection.close()
    assert bindings == [(scope.alias,)]
    assert scope.alias != legacy_alias


@pytest.mark.anyio
async def test_private_account_result_caps_records_across_all_source_pages(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    first_rows = tuple(
        _transaction(
            f"synthetic-page-one-{index}",
            "Trade",
            float(-index),
            execution_time=(
                datetime(2026, 7, 1, tzinfo=UTC) + timedelta(seconds=index)
            ).isoformat(),
        )
        for index in range(config.limits.response_rows)
    )
    second_row = _transaction(
        "synthetic-page-two",
        "Fee",
        -1.0,
        execution_time="2026-07-02T00:00:00Z",
    )
    next_query = urlencode(
        {
            "$skip": config.limits.response_rows,
            "$top": config.limits.response_rows,
            "AccountKeys": scope.account_key.get_secret_value(),
            "ClientKey": scope.client_key.get_secret_value(),
            "FromDate": _START.date().isoformat(),
            "ToDate": _END.date().isoformat(),
        },
    )
    executor = _PayloadExecutor(
        (
            {"Data": list(first_rows), "__next": f"/hist/v1/transactions?{next_query}"},
            {"Data": [second_row]},
        ),
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

    assert result.status == "degraded"
    assert result.private_records is not None
    assert len(result.private_records) == config.limits.response_rows
    assert result.datasets[0].row_count == config.limits.response_rows + 1
    assert "private_result_truncated_to_response_limit" in result.datasets[0].warnings
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        stored_count = connection.execute("SELECT count(*) FROM transactions").fetchone()
    finally:
        connection.close()
    assert stored_count == (config.limits.response_rows + 1,)


@pytest.mark.anyio
async def test_account_ingestion_reserves_duckdb_and_index_overhead_before_writes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    store.close()
    reserved = config.paths.artifacts_dir / "reserved.bin"
    reserved.touch(mode=0o600)
    current_bytes = _all_local_bytes(config.paths.analytics_root)
    with reserved.open("r+b") as handle:
        handle.truncate(config.limits.store_quota_bytes - current_bytes - 1024 * 1024)
    scope = _scope()

    with pytest.raises(AccountSyncValidationError, match="store quota"):
        await sync_transactions(
            scope,
            _START,
            _END,
            provider=SaxoAnalyticsProvider(
                request_executor=_PayloadExecutor(
                    (_transactions(_transaction("synthetic-quota", "Trade", -1.0)),),
                ),
            ),
            config=config,
            clock=lambda: _CAPTURED_AT,
        )

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM transactions),
                (SELECT count(*) FROM datasets),
                (SELECT count(*) FROM account_scope_bindings)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 0, 0, 0)
    assert _all_local_bytes(config.paths.analytics_root) <= config.limits.store_quota_bytes


@pytest.mark.anyio
async def test_closed_positions_filter_date_granularity_to_exact_utc_coverage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    start = datetime(2026, 7, 1, 12, tzinfo=UTC)
    end = datetime(2026, 7, 2, 12, tzinfo=UTC)
    rows = (
        _closed_position("synthetic-before", closed_at="2026-07-01T11:59:59Z", uic=1001),
        _closed_position("synthetic-inside", closed_at="2026-07-01T12:00:00Z", uic=1002),
        _closed_position("synthetic-after", closed_at="2026-07-02T12:00:01Z", uic=1003),
        _closed_position("synthetic-unknown-time", closed_at=None, uic=1004),
    )

    result = await sync_closed_positions(
        scope,
        start,
        end,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(({"Data": list(rows)},)),
        ),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    summary = result.datasets[0]
    assert summary.coverage_start == start
    assert summary.coverage_end == end
    assert summary.row_count == 1
    assert result.private_records is not None
    assert tuple(record.effective_at for record in result.private_records) == (start,)
    assert set(summary.warnings) >= {
        "closed_position_rows_outside_requested_utc_range",
        "closed_position_time_unavailable",
    }
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        normalized = connection.execute("SELECT count(*) FROM closed_positions").fetchone()
        raw = connection.execute("SELECT row_count FROM source_pages").fetchone()
    finally:
        connection.close()
    assert normalized == (1,)
    assert raw == (4,)


@pytest.mark.anyio
async def test_current_flat_closed_position_normalizes_without_inventing_instrument_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    result = await sync_closed_positions(
        _scope(),
        _START,
        _END,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                (
                    {
                        "Data": [
                            {
                                "Amount": 1.0,
                                "AssetType": "Stock",
                                "ClosePositionId": "synthetic-current-close",
                                "PnLAccountCurrency": 8.0,
                                "TradeDateClose": "2026-07-17",
                            },
                        ],
                    },
                ),
            ),
        ),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.datasets[0].row_count == 1
    assert "closed_position_instrument_unavailable" in result.datasets[0].warnings
    assert result.private_records is not None
    assert result.private_records[0].effective_at == datetime(
        2026,
        7,
        17,
        tzinfo=UTC,
    )
    assert result.private_records[0].instrument_handle is None


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
                        "BkAmountId": "synthetic-booking",
                        "Date": "2026-07-16",
                    },
                ],
            },
            {
                "Data": [
                    {
                        "Amount": 1.0,
                        "AssetType": "Stock",
                        "ClosePositionId": "synthetic-closed-position",
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
                        "PnLAccountCurrency": 8.0,
                        "TradeDateClose": "2026-07-17T10:00:00Z",
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
    assert executor.calls[0][2]["FromDate"] == _START.date().isoformat()
    assert executor.calls[0][2]["ToDate"] == _END.date().isoformat()
    assert executor.calls[1][2]["FromDate"] == _START.date().isoformat()
    assert executor.calls[1][2]["ToDate"] == _END.date().isoformat()
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
    cost_payload = cast(
        "Mapping[str, object]",
        {
            "AccountCurrency": "DKK",
            "AccountID": "synthetic-account",
            "Amount": 1.0,
            "AssetType": "Stock",
            "Cost": {
                "Long": {
                    "BuySell": "Buy",
                    "Currency": "DKK",
                    "TotalCost": 3.0,
                    "TotalCostPct": 0.03,
                    "TradingCost": {
                        "Commissions": [
                            {"Pct": 0.02, "Rule": {}, "Value": 2.0},
                        ],
                        "Spread": {
                            "DisplayDecimals": 2,
                            "Pct": 0.01,
                            "Rule": {"Value": 0.01},
                            "Value": 1.0,
                        },
                    },
                },
            },
            "CostCalculationAssumptions": [],
            "HoldingPeriodInDays": 1,
            "Instrument": "Synthetic instrument",
            "Price": 50.0,
            "Uic": 1,
        },
    )
    executor = _PayloadExecutor((cost_payload,))

    result = await sync_cost_sources(
        scope,
        (handle,),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        reference_prices={handle: 50.0},
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.source_request_count == 1
    assert executor.calls[0][2]["Amount"] == "1"
    assert executor.calls[0][2]["HoldingPeriodInDays"] == "1"
    assert float(executor.calls[0][2]["Price"]) == 50.0
    assert "TradeContext" not in executor.calls[0][2]
    assert result.private_records is not None
    record = result.private_records[0]
    assert record.amount_value == 3.0
    assert record.fee_value == 2.0
    assert record.tax_value is None
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


@pytest.mark.anyio
async def test_account_analysis_sources_capture_exact_performance_and_exposure_lineage(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    handle = _seed_instrument(config)
    executor = _PayloadExecutor(
        (
            {"AccountValue": 1000.0, "AccumulatedProfitLoss": 10.0},
            {
                "Balance": {
                    "AccountValue": [
                        {"Date": "2026-07-31", "Value": 990.0},
                        {"Date": "2026-08-01", "Value": 1000.0},
                    ],
                    "CashTransfer": [],
                },
                "TimeWeighted": {
                    "Accumulated": [
                        {"Date": "2026-07-31", "Value": 0.0},
                        {"Date": "2026-08-01", "Value": 1.0},
                    ],
                },
            },
            [
                {
                    "Amount": 1.0,
                    "AssetType": "Stock",
                    "Currency": "DKK",
                    "Uic": 1001,
                },
            ],
        ),
    )

    summaries, request_count = await sync_account_analysis_sources(
        scope,
        ("portfolio_performance", "scenario_custom", "pretrade_impact"),
        (handle,),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert request_count == 3
    assert {item.contract_id for item in summaries} == {
        "exposure_instruments_v1",
        "performance_summary_v4",
        "performance_timeseries_v4",
    }
    assert all(item.account_alias == scope.alias for item in summaries)
    assert all(item.quality_state.value == "complete" for item in summaries)
    assert [call[0] for call in executor.calls] == [
        "get.hist.v4.performance.summary",
        "get.hist.v4.performance.timeseries",
        "get.port.v1.exposure.instruments",
    ]
