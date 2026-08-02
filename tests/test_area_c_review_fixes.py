# ruff: noqa: PLR2004

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx2
import pytest
from pydantic import SecretStr

import saxo_bank_mcp.analytics_sync as analytics_sync_module
from saxo_bank_mcp.analytics_account_data import AccountScope, new_account_alias
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import HandleKind, VisibilityMode, new_safe_handle
from saxo_bank_mcp.analytics_portfolio_snapshots import capture_portfolio_snapshot
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_resolver import InstrumentResolver, ResolutionError
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_sync import sync_price_bars
from saxo_bank_mcp.analytics_universes import ResearchUniverseStore, UniverseValidationError
from saxo_bank_mcp.endpoint_registry import EndpointOperation

_FIRST_CAPTURE = datetime(2026, 8, 2, 8, tzinfo=UTC)
_SECOND_CAPTURE = datetime(2026, 8, 2, 9, tzinfo=UTC)
_THIRD_CAPTURE = datetime(2026, 8, 2, 10, tzinfo=UTC)
_START = datetime(2026, 8, 2, 7, tzinfo=UTC)
_END = datetime(2026, 8, 2, 7, 1, tzinfo=UTC)


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


def _scope() -> AccountScope:
    return AccountScope(
        alias=new_account_alias(),
        account_key=SecretStr("mocked-account-key"),
        client_key=SecretStr("mocked-client-key"),
    )


def _reference_payload(
    identifier: int,
    *,
    symbol: str,
    asset_type: str = "Stock",
) -> Mapping[str, object]:
    return {
        "Data": [
            {
                "Identifier": identifier,
                "AssetType": asset_type,
                "Description": f"{symbol} fixture instrument",
                "Symbol": symbol,
                "ExchangeId": "XNAS",
            },
        ],
    }


async def _resolve(
    config: AnalyticsConfig,
    identifier: int,
    *,
    symbol: str,
) -> str:
    resolver = InstrumentResolver(
        SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor((_reference_payload(identifier, symbol=symbol),)),
        ),
        config,
    )
    result = await resolver.resolve_instruments(symbol, (), ())
    return result.matches[0].instrument_handle


def _snapshot_payloads(
    *,
    position_id: str,
    identifier: int | None,
) -> tuple[Mapping[str, object], ...]:
    position_base: dict[str, object] = {
        "Amount": 2.0,
        "AssetType": "Stock",
        "OpenPrice": 100.0,
    }
    if identifier is not None:
        position_base["Uic"] = identifier
    return (
        {
            "CashBalance": 5_000.0,
            "Currency": "DKK",
            "TotalValue": 5_220.0,
        },
        {
            "Data": [
                {
                    "PositionBase": position_base,
                    "PositionId": position_id,
                    "PositionView": {
                        "CurrentPrice": 110.0,
                        "Exposure": 220.0,
                        "ProfitLossOnTrade": 20.0,
                    },
                },
            ],
        },
        {"Data": []},
    )


def _chart_payload(close_values: tuple[float, float], *, data_version: int) -> Mapping[str, object]:
    return {
        "Data": [
            {
                "CloseBid": value,
                "Time": f"2026-08-02T09:0{minute}:00+02:00",
                "Volume": 1,
            }
            for minute, value in enumerate(close_values)
        ],
        "DataVersion": data_version,
    }


async def _sync_chart(
    config: AnalyticsConfig,
    instrument_handle: str,
    payload: Mapping[str, object],
    captured_at: datetime,
) -> str:
    result = await sync_price_bars(
        instrument_handle,
        ChartInterval.ONE_MINUTE,
        _START,
        _END,
        provider=SaxoAnalyticsProvider(request_executor=_PayloadExecutor((payload,))),
        config=config,
        clock=lambda: captured_at,
    )
    return result.datasets[0].dataset_id


def _seed_analysis(
    config: AnalyticsConfig,
    *,
    dataset_id: str,
    analysis_kind: str,
) -> str:
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        dataset = connection.execute(
            "SELECT account_scope, source_revision FROM datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        assert dataset is not None
        connection.execute(
            """
            INSERT INTO analyses (
                analysis_id, dataset_id, account_scope, analysis_kind, status,
                source_revision, as_of, created_at, byte_count,
                fingerprint_sha256, result_json
            )
            VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, 2, ?, '{}')
            """,
            (
                analysis_id,
                dataset_id,
                str(dataset[0]),
                analysis_kind,
                str(dataset[1]),
                _FIRST_CAPTURE,
                _FIRST_CAPTURE,
                "c" * 64,
            ),
        )
    finally:
        connection.close()
    return analysis_id


def _analysis_statuses(config: AnalyticsConfig, analysis_ids: Sequence[str]) -> dict[str, str]:
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        return {
            str(analysis_id): str(status)
            for analysis_id, status in connection.execute(
                "SELECT analysis_id, status FROM analyses WHERE analysis_id = ANY(?)",
                (list(analysis_ids),),
            ).fetchall()
        }
    finally:
        connection.close()


@pytest.mark.anyio
async def test_resolver_and_portfolio_ingestion_share_one_instrument_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    resolved_handle = await _resolve(config, 1001, symbol="FIX")

    first = await capture_portfolio_snapshot(
        _scope(),
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                _snapshot_payloads(position_id="first-position", identifier=1001),
            ),
        ),
        config=config,
        clock=lambda: _FIRST_CAPTURE,
    )
    second = await capture_portfolio_snapshot(
        _scope(),
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                _snapshot_payloads(position_id="second-position", identifier=1001),
            ),
        ),
        config=config,
        clock=lambda: _SECOND_CAPTURE,
    )

    assert first.position_handles == second.position_handles == (resolved_handle,)
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT instrument_handle, safe_label, metadata_json FROM safe_instruments",
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    assert rows[0][0] == resolved_handle
    assert rows[0][1] == "FIX · Stock · XNAS"
    assert json.loads(str(rows[0][2]))["symbol"] == "FIX"


@pytest.mark.anyio
async def test_duplicate_noncanonical_rows_for_one_saxo_identity_fail_closed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    store.close()
    metadata = {
        "aliases": ["fix"],
        "asset_type": "Stock",
        "display_label": "FIX · Stock · XNAS",
        "exchange": "XNAS",
        "identifier": 1001,
        "symbol": "FIX",
    }
    metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    fingerprint = hashlib.sha256(metadata_json.encode()).hexdigest()
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.executemany(
            """
            INSERT INTO safe_instruments (
                instrument_handle, asset_type, safe_label, source_revision,
                source_timestamp, fingerprint_sha256, metadata_json
            )
            VALUES (?, 'Stock', 'FIX · Stock · XNAS', 'fixture', ?, ?, ?)
            """,
            [
                (
                    new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                    _FIRST_CAPTURE,
                    fingerprint,
                    metadata_json,
                ),
                (
                    new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                    _FIRST_CAPTURE,
                    fingerprint,
                    metadata_json,
                ),
            ],
        )
    finally:
        connection.close()
    executor = _PayloadExecutor((_reference_payload(1001, symbol="FIX"),))
    resolver = InstrumentResolver(
        SaxoAnalyticsProvider(request_executor=executor),
        config,
    )

    with pytest.raises(ResolutionError, match="identity"):
        await resolver.resolve_instruments("FIX", (), ())

    assert executor.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("corruption", ["handle", "fingerprint"])
async def test_catalog_load_refuses_noncanonical_identity_before_unavailable_result(
    tmp_path: Path,
    corruption: str,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    store.close()
    metadata = {
        "aliases": ["fix"],
        "asset_type": "Stock",
        "display_label": "FIX · Stock · XNAS",
        "exchange": "XNAS",
        "identifier": 1001,
        "symbol": "FIX",
    }
    metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    canonical_handle = instrument_handle_for_saxo_identity("Stock", 1001)
    canonical_fingerprint = hashlib.sha256(metadata_json.encode()).hexdigest()
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            """
            INSERT INTO safe_instruments (
                instrument_handle, asset_type, safe_label, source_revision,
                source_timestamp, fingerprint_sha256, metadata_json
            )
            VALUES (?, 'Stock', 'FIX · Stock · XNAS', 'fixture', ?, ?, ?)
            """,
            (
                (
                    new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
                    if corruption == "handle"
                    else canonical_handle
                ),
                _FIRST_CAPTURE,
                "f" * 64 if corruption == "fingerprint" else canonical_fingerprint,
                metadata_json,
            ),
        )
    finally:
        connection.close()
    executor = _PayloadExecutor(({"Data": []},))
    resolver = InstrumentResolver(
        SaxoAnalyticsProvider(request_executor=executor),
        config,
    )

    with pytest.raises(ResolutionError, match="identity"):
        await resolver.resolve_instruments("FIX", (), ())

    assert executor.calls == []


@pytest.mark.anyio
async def test_null_uic_position_keeps_private_values_without_advertising_an_instrument(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    result = await capture_portfolio_snapshot(
        _scope(),
        provider=SaxoAnalyticsProvider(
            request_executor=_PayloadExecutor(
                _snapshot_payloads(position_id="private-position", identifier=None),
            ),
        ),
        config=config,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        clock=lambda: _FIRST_CAPTURE,
    )

    assert result.position_count == 1
    assert result.position_handles == ()
    assert "position_instrument_unavailable" in result.warnings
    assert result.private_values is not None
    private_position = result.private_values.positions[0]
    assert private_position.instrument_handle is None
    assert re.fullmatch(r"pa_[0-9a-f]{32}", private_position.position_alias)
    assert private_position.amount_value == 2.0

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        safe_instrument_count = connection.execute(
            "SELECT count(*) FROM safe_instruments",
        ).fetchone()
        payload_row = connection.execute(
            "SELECT payload_json FROM account_snapshots",
        ).fetchone()
    finally:
        connection.close()
    assert safe_instrument_count == (0,)
    assert payload_row is not None
    stored_position = json.loads(str(payload_row[0]))["positions"][0]
    assert stored_position["instrument_handle"] is None
    assert stored_position["position_alias"] == private_position.position_alias

    with pytest.raises(UniverseValidationError, match="instrument handle"):
        ResearchUniverseStore(config).create_universe(
            "Unavailable identity",
            (private_position.position_alias,),
        )


@pytest.mark.anyio
async def test_chart_correction_invalidates_only_dependent_analyses_in_its_scope(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first_handle = await _resolve(config, 1001, symbol="FIRST")
    second_handle = await _resolve(config, 2002, symbol="SECOND")
    first_dataset = await _sync_chart(
        config,
        first_handle,
        _chart_payload((100.0, 101.0), data_version=1),
        _FIRST_CAPTURE,
    )
    second_dataset = await _sync_chart(
        config,
        second_handle,
        _chart_payload((200.0, 201.0), data_version=1),
        _FIRST_CAPTURE,
    )
    dependent = _seed_analysis(
        config,
        dataset_id=first_dataset,
        analysis_kind="instrument_price_return",
    )
    unrelated = _seed_analysis(
        config,
        dataset_id=first_dataset,
        analysis_kind="portfolio_overview",
    )
    other_instrument = _seed_analysis(
        config,
        dataset_id=second_dataset,
        analysis_kind="instrument_price_return",
    )

    await _sync_chart(
        config,
        first_handle,
        _chart_payload((100.0, 101.0), data_version=2),
        _SECOND_CAPTURE,
    )
    assert _analysis_statuses(config, (dependent, unrelated, other_instrument)) == {
        dependent: "verified",
        unrelated: "verified",
        other_instrument: "verified",
    }

    await _sync_chart(
        config,
        first_handle,
        _chart_payload((100.0, 111.0), data_version=3),
        _THIRD_CAPTURE,
    )
    assert _analysis_statuses(config, (dependent, unrelated, other_instrument)) == {
        dependent: "invalidated",
        unrelated: "verified",
        other_instrument: "verified",
    }


@pytest.mark.anyio
async def test_chart_invalidation_failure_rolls_back_the_material_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    handle = await _resolve(config, 1001, symbol="FIX")
    dataset_id = await _sync_chart(
        config,
        handle,
        _chart_payload((100.0, 101.0), data_version=1),
        _FIRST_CAPTURE,
    )
    analysis_id = _seed_analysis(
        config,
        dataset_id=dataset_id,
        analysis_kind="instrument_price_return",
    )
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        before = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM price_bars),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()

    def fail_invalidation(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("injected invalidation failure")

    monkeypatch.setattr(
        analytics_sync_module,
        "_invalidate_chart_dependent_analyses",
        fail_invalidation,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="injected invalidation failure"):
        await _sync_chart(
            config,
            handle,
            _chart_payload((100.0, 111.0), data_version=2),
            _SECOND_CAPTURE,
        )

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        after = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM source_pages),
                (SELECT count(*) FROM price_bars),
                (SELECT count(*) FROM datasets)
            """,
        ).fetchone()
    finally:
        connection.close()
    assert after == before
    assert _analysis_statuses(config, (analysis_id,)) == {analysis_id: "verified"}
