# ruff: noqa: PLR2004

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import duckdb
import httpx2
import pytest
from analytics_legacy_alias_support import migrate_v2_store_with_unbound_alias
from pydantic import SecretStr

import saxo_bank_mcp.analytics_portfolio_snapshots as snapshot_module
from saxo_bank_mcp.analytics_account_data import (
    AccountScope,
    bind_account_scope,
    new_account_alias,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_models import HandleKind, VisibilityMode, new_safe_handle
from saxo_bank_mcp.analytics_portfolio_snapshots import (
    PortfolioSnapshotValidationError,
    capture_portfolio_snapshot,
)
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.endpoint_registry import EndpointOperation

_CAPTURED_AT = datetime(2026, 8, 1, 8, tzinfo=UTC)
_LATER = datetime(2026, 8, 1, 9, tzinfo=UTC)
_LATEST = datetime(2026, 8, 1, 10, tzinfo=UTC)
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


def _all_local_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _scope() -> AccountScope:
    return AccountScope(
        alias=new_account_alias(),
        account_key=SecretStr("mocked-account-key"),
        client_key=SecretStr("mocked-client-key"),
    )


def _snapshot_payloads(
    cash_balance: float,
    *,
    position_id: str = "synthetic-position",
) -> tuple[Mapping[str, object], ...]:
    return (
        {
            "CashAvailableForTrading": cash_balance - 100.0,
            "CashBalance": cash_balance,
            "Currency": "DKK",
            "FinancingAccruals": -4.0,
            "FundsAvailableForSettlement": cash_balance - 50.0,
            "FundsReservedForSettlement": 25.0,
            "TotalValue": cash_balance + 500.0,
            "TransactionsNotBooked": 12.5,
            "TransactionsNotBookedDetail": {
                "CashDeposit": 100.0,
                "CashWithdrawal": -20.0,
                "Commission": -2.0,
                "ExchangeFee": -1.0,
                "ExternalCharges": -0.5,
                "StampDuty": -3.0,
            },
        },
        {
            "Data": [
                {
                    "PositionBase": {
                        "Amount": 2.0,
                        "AssetType": "Stock",
                        "ExecutionTimeOpen": "2026-07-01T08:00:00Z",
                        "OpenPrice": 100.0,
                        "Uic": 1001,
                    },
                    "PositionId": position_id,
                    "PositionView": {
                        "CurrentPrice": 110.0,
                        "Exposure": 220.0,
                        "ProfitLossOnTrade": 20.0,
                    },
                },
            ],
        },
        {
            "Data": [
                {
                    "AssetType": "Stock",
                    "OrderId": "synthetic-order",
                    "Status": "Working",
                },
            ],
        },
    )


def _position_payload(index: int) -> Mapping[str, object]:
    return {
        "PositionBase": {
            "Amount": 2.0,
            "AssetType": "Stock",
            "ExecutionTimeOpen": "2026-07-01T08:00:00Z",
            "OpenPrice": 100.0,
            "Uic": 1000 + index,
        },
        "PositionId": f"synthetic-position-{index}",
        "PositionView": {
            "CurrentPrice": 110.0,
            "Exposure": 220.0,
            "ProfitLossOnTrade": 20.0,
        },
    }


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
                "c" * 64,
            ),
        )
    finally:
        connection.close()
    return analysis_id


@pytest.mark.anyio
async def test_public_snapshot_returns_only_handles_counts_aliases_and_fingerprints(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    result = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5000.0)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "complete"
    assert result.account_alias == scope.alias
    assert result.source_request_count == 3
    assert result.balance_row_count == 1
    assert result.position_count == 1
    assert result.order_count == 1
    assert result.private_values is None
    assert len(result.position_handles) == 1
    assert result.position_handles[0].startswith("ih_")
    assert all(_SHA256.fullmatch(value) for value in result.fingerprints.model_dump().values())
    public_json = result.model_dump_json()
    assert "5000.0" not in public_json
    assert "synthetic-position" not in public_json
    assert "synthetic-order" not in public_json
    assert scope.account_key.get_secret_value() not in public_json

    mode = stat.S_IMODE(config.paths.store_path.stat().st_mode)
    assert mode == 0o600
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM datasets),
                (SELECT count(*) FROM account_snapshots)
            """,
        ).fetchone()
        payload = connection.execute(
            "SELECT payload_json FROM account_snapshots",
        ).fetchone()
    finally:
        connection.close()
    assert counts == (3, 1, 1)
    assert payload is not None
    assert '"cash_balance":5000.0' in str(payload[0])


@pytest.mark.anyio
async def test_private_snapshot_exposes_unsettled_financing_tax_and_missing_basis_only_locally(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    refused_executor = _PayloadExecutor(_snapshot_payloads(5000.0))

    with pytest.raises(PortfolioSnapshotValidationError, match="trusted local host"):
        await capture_portfolio_snapshot(
            scope,
            provider=SaxoAnalyticsProvider(request_executor=refused_executor),
            config=config,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
            trusted_local_host=False,
            clock=lambda: _CAPTURED_AT,
        )

    assert refused_executor.calls == []
    result = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5000.0)),
        ),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.private_values is not None
    values = result.private_values
    assert values.cash_balance == 5000.0
    assert values.unsettled_cash == 12.5
    assert values.financing_accruals == -4.0
    assert values.fee_value == -3.5
    assert values.tax_value == -3.0
    assert values.deposit_value == 100.0
    assert values.withdrawal_value == -20.0
    assert values.fx_timestamp is None
    assert values.tax_lot_basis_available is False
    assert values.positions[0].amount_value == 2.0
    assert values.positions[0].profit_loss_value == 20.0
    assert set(result.warnings) >= {
        "fx_conversion_timestamp_unavailable",
        "tax_lot_basis_unavailable",
        "unsettled_cash_present",
    }


@pytest.mark.anyio
async def test_snapshot_rejects_rebound_account_alias_before_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    first = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5000.0)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    rebound = AccountScope(
        alias=scope.alias,
        account_key=scope.account_key,
        client_key=SecretStr("different-client-key"),
    )
    executor = _PayloadExecutor(_snapshot_payloads(5000.0))

    with pytest.raises(PortfolioSnapshotValidationError, match="account alias binding"):
        await capture_portfolio_snapshot(
            rebound,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _LATER,
        )

    assert executor.calls == []
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        counts = connection.execute(
            "SELECT count(*) FROM account_snapshots WHERE account_scope = ?",
            (scope.alias,),
        ).fetchone()
    finally:
        connection.close()
    assert counts == (1,)
    assert first.account_alias == scope.alias


@pytest.mark.anyio
async def test_snapshot_refuses_migrated_v2_unbound_alias_before_source_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    alias = new_account_alias()
    migrate_v2_store_with_unbound_alias(config, alias, data_kind="snapshot")
    scope = AccountScope(
        alias=alias,
        account_key=SecretStr("legacy-snapshot-account-key"),
        client_key=SecretStr("legacy-snapshot-client-key"),
    )
    executor = _PayloadExecutor(_snapshot_payloads(5000.0))

    with pytest.raises(PortfolioSnapshotValidationError) as raised:
        await capture_portfolio_snapshot(
            scope,
            provider=SaxoAnalyticsProvider(request_executor=executor),
            config=config,
            clock=lambda: _LATER,
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
                (SELECT count(*) FROM account_snapshots WHERE account_scope = ?),
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM datasets)
            """,
            (alias,),
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 1, 0, 0)


@pytest.mark.anyio
async def test_snapshot_caps_public_and_private_positions_across_source_pages(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    first_positions = tuple(
        _position_payload(index) for index in range(config.limits.response_rows)
    )
    final_position = _position_payload(config.limits.response_rows)
    next_query = urlencode(
        {
            "$skip": config.limits.response_rows,
            "$top": config.limits.response_rows,
            "AccountKey": scope.account_key.get_secret_value(),
            "ClientKey": scope.client_key.get_secret_value(),
        },
    )
    balance, _positions, _orders = _snapshot_payloads(5000.0)
    executor = _PayloadExecutor(
        (
            balance,
            {
                "Data": list(first_positions),
                "__next": f"/port/v1/positions?{next_query}",
            },
            {"Data": [final_position]},
            {"Data": []},
        ),
    )

    result = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _CAPTURED_AT,
    )

    assert result.status == "degraded"
    assert result.position_count == config.limits.response_rows + 1
    assert len(result.position_handles) == config.limits.response_rows
    assert result.private_values is not None
    assert len(result.private_values.positions) == config.limits.response_rows
    assert "position_result_truncated_to_response_limit" in result.warnings
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        stored_count = connection.execute("SELECT count(*) FROM safe_instruments").fetchone()
    finally:
        connection.close()
    assert stored_count == (config.limits.response_rows + 1,)


@pytest.mark.anyio
async def test_snapshot_reserves_shared_store_overhead_before_any_write(
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

    with pytest.raises(PortfolioSnapshotValidationError, match="store quota"):
        await capture_portfolio_snapshot(
            _scope(),
            provider=SaxoAnalyticsProvider(
                request_executor=_PayloadExecutor(_snapshot_payloads(5000.0)),
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
                (SELECT count(*) FROM safe_instruments),
                (SELECT count(*) FROM datasets),
                (SELECT count(*) FROM account_snapshots),
                (SELECT count(*) FROM account_scope_bindings)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert counts == (0, 0, 0, 0, 0)
    assert _all_local_bytes(config.paths.analytics_root) <= config.limits.store_quota_bytes


@pytest.mark.anyio
async def test_snapshots_are_immutable_and_only_material_revisions_invalidate_dependents(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    first = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5000.0)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    dependent_id = _seed_analysis(
        config,
        dataset_id=first.dataset_id,
        account_alias=scope.alias,
        analysis_kind="portfolio_overview",
    )
    unrelated_id = _seed_analysis(
        config,
        dataset_id=first.dataset_id,
        account_alias=scope.alias,
        analysis_kind="market_drawdown",
    )

    revised = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5001.0)),
        ),
        config=config,
        clock=lambda: _LATER,
    )
    repeat_dependent_id = _seed_analysis(
        config,
        dataset_id=revised.dataset_id,
        account_alias=scope.alias,
        analysis_kind="portfolio_overview",
    )
    repeated = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5001.0)),
        ),
        config=config,
        clock=lambda: _LATEST,
    )

    assert len({first.snapshot_id, revised.snapshot_id, repeated.snapshot_id}) == 3
    assert first.position_handles == revised.position_handles == repeated.position_handles
    assert revised.invalidated_analysis_count == 1
    assert repeated.invalidated_analysis_count == 0
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        statuses = dict(
            connection.execute(
                """
                SELECT analysis_id, status
                FROM analyses
                WHERE analysis_id = ANY(?)
                """,
                ([dependent_id, unrelated_id, repeat_dependent_id],),
            ).fetchall(),
        )
        snapshots = connection.execute(
            "SELECT payload_json FROM account_snapshots ORDER BY as_of",
        ).fetchall()
    finally:
        connection.close()
    assert statuses == {
        dependent_id: "invalidated",
        unrelated_id: "verified",
        repeat_dependent_id: "verified",
    }
    assert len(snapshots) == 3
    assert '"cash_balance":5000.0' in str(snapshots[0][0])
    assert '"cash_balance":5001.0' in str(snapshots[1][0])


@pytest.mark.anyio
async def test_equal_as_of_recency_uses_committed_order_instead_of_snapshot_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    scope = _scope()
    store = AnalyticsStore.open(config)
    store.close()
    high_snapshot_id = "ps_ffffffffffff4fff8fffffffffffffff"
    low_snapshot_id = "ps_00000000000040008000000000000000"
    repeat_snapshot_id = "ps_11111111111141118111111111111111"
    legacy_payload = json.dumps(
        {"material_fingerprint_sha256": "a" * 64},
        separators=(",", ":"),
        sort_keys=True,
    )
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        bind_account_scope(connection, scope)
        connection.execute(
            """
            INSERT INTO account_snapshots (
                snapshot_id, dataset_id, page_id, snapshot_kind, account_scope,
                source_revision, as_of, byte_count, fingerprint_sha256, payload_json
            )
            VALUES (?, ?, NULL, 'portfolio', ?, 'legacy-revision', ?, ?, ?, ?)
            """,
            (
                high_snapshot_id,
                "ds_22222222222242228222222222222222",
                scope.alias,
                _CAPTURED_AT,
                len(legacy_payload.encode()),
                "b" * 64,
                legacy_payload,
            ),
        )
    finally:
        connection.close()

    original_new_handle = snapshot_module.new_safe_handle
    snapshot_ids = iter((low_snapshot_id, repeat_snapshot_id))

    def controlled_handle(kind: HandleKind) -> str:
        if kind is HandleKind.PORTFOLIO_SNAPSHOT_ID:
            return next(snapshot_ids)
        return original_new_handle(kind)

    monkeypatch.setattr(snapshot_module, "new_safe_handle", controlled_handle)
    current = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5001.0)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    analysis_id = _seed_analysis(
        config,
        dataset_id=current.dataset_id,
        account_alias=scope.alias,
        analysis_kind="portfolio_overview",
    )

    repeated = await capture_portfolio_snapshot(
        scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5001.0)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )

    assert repeated.invalidated_analysis_count == 0
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        status = connection.execute(
            "SELECT status FROM analyses WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
    finally:
        connection.close()
    assert status == ("verified",)


@pytest.mark.anyio
async def test_material_change_in_another_account_does_not_cross_alias_boundary(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first_scope = _scope()
    second_scope = _scope()
    first = await capture_portfolio_snapshot(
        first_scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(5000.0)),
        ),
        config=config,
        clock=lambda: _CAPTURED_AT,
    )
    first_analysis_id = _seed_analysis(
        config,
        dataset_id=first.dataset_id,
        account_alias=first_scope.alias,
        analysis_kind="portfolio_overview",
    )
    second = await capture_portfolio_snapshot(
        second_scope,
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(_snapshot_payloads(6000.0)),
        ),
        config=config,
        clock=lambda: _LATER,
    )

    assert second.invalidated_analysis_count == 0
    assert first.account_alias != second.account_alias
    assert first.position_handles == second.position_handles
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        status = connection.execute(
            "SELECT status FROM analyses WHERE analysis_id = ?",
            (first_analysis_id,),
        ).fetchone()
    finally:
        connection.close()
    assert status == ("verified",)
