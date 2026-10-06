"""Real MCP calculation job lifecycle over authenticated, offline source material."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from test_analytics_runtime_models import (
    _backtest_fixture,  # pyright: ignore[reportPrivateUsage]
    _optimization_fixture,  # pyright: ignore[reportPrivateUsage]
    _pretrade_fixture,  # pyright: ignore[reportPrivateUsage]
)

import saxo_bank_mcp.analytics_release as release
import saxo_bank_mcp.analytics_sync as sync
import saxo_bank_mcp.mcp_analytics_tools as analytics_tools
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_instrument_identity import (
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_jobs import JobConclusion, JobExecutionContext, JobStatus
from saxo_bank_mcp.analytics_market_data import (
    ChartInterval,
    normalize_price_series,
    normalize_quote,
)
from saxo_bank_mcp.analytics_models import AnalysisResult, QualityState
from saxo_bank_mcp.analytics_provenance import AnalysisReplayRefused, replay_analysis
from saxo_bank_mcp.analytics_runtime import PROOF_CHECKS, RECIPE_KINDS, execute_analysis
from saxo_bank_mcp.analytics_source_contracts import (
    SourceJsonValue,
    SourcePage,
    compare_source_schema,
    freeze_source_rows,
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
    source_page_fingerprint,
    source_quality_proof,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StorageDataType, StorageScope
from saxo_bank_mcp.server import create_mcp_server

_ACCOUNT = "aa_00000000000040008000000000000001"
_DATASET = "ds_00000000000040008000000000000001"
_MODEL_DATASET = "ds_00000000000040008000000000000002"
_SOURCE_REVISION = "fixture:production_jobs"
_CALIBRATION_OBSERVATIONS = 40
_STARTING_VALUE = 1000.0
_EXPECTED_GOAL_PROBABILITY = 1.0
_WORK_UNITS = 100_000
_WAIT_SECONDS = 10.0
_SEED = 17
_AS_OF = datetime(2026, 10, 4, tzinfo=UTC)
_CHART_START = datetime(2026, 6, 23, tzinfo=UTC)


def _release_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin only the active release receipt while other workers edit shared files."""
    code_hash = release.runtime_code_sha256()
    monkeypatch.setattr(release, "runtime_code_sha256", lambda: code_hash)
    receipt = {
        "code_sha256": code_hash,
        "definition_catalog_sha256": release.load_metric_definition_catalog().fingerprint_sha256,
        "source_catalog_sha256": source_contract_catalog_sha256(),
        "recipe_kinds": list(RECIPE_KINDS),
        "checks_passed": list(PROOF_CHECKS),
        "suite_passed": True,
    }
    monkeypatch.setattr(release, "_release_document", lambda: receipt)


def _persist_calibration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Use actual immutable page and dataset storage, including authenticated fingerprints."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB", "1")
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = load_analytics_config(os.environ)
    contracts = source_contracts_by_id()
    rows_by_contract: dict[str, list[dict[str, object]]] = {
        "balances_v1": [{"Currency": "USD", "TotalValue": 10_000.0}],
        "performance_timeseries_v4": [
            {
                "TimeWeighted": {
                    "Accumulated": [
                        {
                            "Date": (
                                _AS_OF - timedelta(days=_CALIBRATION_OBSERVATIONS - index)
                            ).isoformat(),
                            "Value": float(index),
                        }
                        for index in range(_CALIBRATION_OBSERVATIONS)
                    ]
                }
            }
        ],
    }
    store = AnalyticsStore.open(config)
    try:
        pages = tuple(
            store.put_source_page(
                source_kind="balances" if contract_id == "balances_v1" else "performance",
                page_key=f"fixture:calibration:{contract_id}",
                source_revision=_SOURCE_REVISION,
                contract_name=contract_id,
                contract_sha256=source_contract_fingerprint(contracts[contract_id]),
                payload={
                    "contract_id": contract_id,
                    "rows": rows,
                    "sync_metadata": {"entitlement_state": "available", "delayed_by_minutes": 0},
                },
                row_count=len(rows),
                source_timestamp=_AS_OF,
                account_scope=_ACCOUNT,
                instrument_handle=None,
            )
            for contract_id, rows in rows_by_contract.items()
        )
        store.create_dataset(
            dataset_id=_DATASET,
            account_scope=_ACCOUNT,
            source_scope="saxo_openapi",
            source_revision=_SOURCE_REVISION,
            source_page_ids=tuple(page.page_id for page in pages),
            created_at=_AS_OF,
            coverage_start=_AS_OF - timedelta(days=_CALIBRATION_OBSERVATIONS),
            coverage_end=_AS_OF,
            quality_state=QualityState.COMPLETE,
        )
        authenticated = store.get_authenticated_dataset_material(_DATASET)
        assert {page.contract_name for page in authenticated.pages} == set(rows_by_contract)
    finally:
        store.close()
    _release_receipt(monkeypatch)


def _request(*, path_count: int, horizon_periods: int, seed: int = _SEED) -> dict[str, object]:
    return {
        "job_kind": "monte_carlo",
        "total_work_units": _WORK_UNITS,
        "analysis_request": {
            "analysis_kind": "monte_carlo",
            "dataset_ids": [_DATASET],
            "options": {
                "simulation": {
                    "random_seed": seed,
                    "path_count": path_count,
                    "block_length": 2,
                    "horizon_periods": horizon_periods,
                    "horizon_years": 1.0,
                    "starting_value": _STARTING_VALUE,
                    "explicit_goal": _STARTING_VALUE,
                    "explicit_ruin_threshold": 1.0,
                    "cash_flows": [
                        {"period_index": period, "contribution": "0", "withdrawal": "0"}
                        for period in range(1, horizon_periods + 1)
                    ],
                    "annual_inflation_assumption": 0.02,
                }
            },
        },
    }


def _persist_chart(uic: int, prices: tuple[float, ...]) -> str:
    """Use the normal chart ingestion owner without constructing a network executor."""
    config = load_analytics_config(os.environ)
    handle = instrument_handle_for_saxo_identity("Stock", uic)
    metadata = {"asset_type": "Stock", "identifier": uic, "display_label": "Fixture stock"}
    metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    store = AnalyticsStore.open(config)
    try:
        with store.market_ingestion_transaction(0) as connection:
            put_saxo_instrument_identity(
                connection,
                asset_type="Stock",
                uic=uic,
                safe_label="Fixture stock",
                source_revision=_SOURCE_REVISION,
                source_timestamp=_AS_OF,
                fingerprint_sha256=hashlib.sha256(metadata_json.encode()).hexdigest(),
                metadata_json=metadata_json,
                update_existing=False,
            )
    finally:
        store.close()
    rows: list[dict[str, SourceJsonValue]] = [
        {
            "Time": (_CHART_START + timedelta(days=index)).isoformat(),
            "OpenBid": price,
            "HighBid": price,
            "LowBid": price,
            "CloseBid": price,
            "Volume": 100.0,
            "PriceType": "RealTime",
        }
        for index, price in enumerate(prices)
    ]
    end = _CHART_START + timedelta(days=len(prices) - 1)
    contract = source_contracts_by_id()["chart_v3"]
    comparison = compare_source_schema(contract, {"Data": rows, "DataVersion": 1})
    frozen = freeze_source_rows(rows)
    capture_revision = f"capture:{uic:032x}"
    page = SourcePage(
        contract_id=contract.contract_id,
        operation_id=contract.operation_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_kind=contract.source_kind,
        capture_revision=capture_revision,
        source_timestamp=_AS_OF,
        account_scope="aggregate",
        instrument_scope_sha256=hashlib.sha256(handle.encode()).hexdigest(),
        request_fingerprint_sha256=hashlib.sha256(f"chart:{uic}".encode()).hexdigest(),
        page_number=1,
        rows=frozen,
        row_count=len(rows),
        data_version=1,
        source_revision="data_version:1",
        page_fingerprint_sha256=source_page_fingerprint(rows),
        schema_comparison=comparison,
        source_quality=source_quality_proof(contract, frozen, comparison),
    )
    normalized = normalize_price_series(
        rows=rows,
        instrument_handle=handle,
        interval=ChartInterval.ONE_DAY,
        start=_CHART_START,
        end=end,
    )
    correction_state: dict[str, object] = {"data_version": 1}
    fingerprints = sync._capture_fingerprints(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        (page,), normalized.fingerprint_sha256, correction_state
    )
    _, dataset_id = sync._persist_chart_capture(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        config=config,
        pages=(page,),
        instrument_handle=handle,
        interval=ChartInterval.ONE_DAY,
        start=_CHART_START,
        end=end,
        quality_state=QualityState.COMPLETE,
        fingerprints=fingerprints,
        correction_state=correction_state,
        prior_page_ids=(),
        retained_page_ids=(),
        normalized_series=normalized,
        coverage=(_CHART_START, end),
        visible_bar_revisions={bar.bar_time: capture_revision for bar in normalized.bars},
        invalidate_prior_analyses=False,
    )
    assert len(sync.get_dataset(dataset_id, 1, 100, config=config).rows) == len(prices)
    return dataset_id


def _persist_model_sources(
    sources: dict[str, list[dict[str, object]]], *, dataset_id: str = _DATASET
) -> None:
    config = load_analytics_config(os.environ)
    contracts = source_contracts_by_id()
    store = AnalyticsStore.open(config)
    try:
        pages = tuple(
            store.put_source_page(
                source_kind=contracts[contract_id].source_kind,
                page_key=f"fixture:{contract_id}",
                source_revision=_SOURCE_REVISION,
                contract_name=contract_id,
                contract_sha256=source_contract_fingerprint(contracts[contract_id]),
                payload={"rows": rows},
                row_count=len(rows),
                source_timestamp=_AS_OF,
                account_scope=_ACCOUNT,
                instrument_handle=None,
            )
            for contract_id, rows in sources.items()
            if contract_id != "chart_v3"
        )
        store.create_dataset(
            dataset_id=dataset_id,
            account_scope=_ACCOUNT,
            source_scope="saxo_openapi",
            source_revision=_SOURCE_REVISION,
            source_page_ids=tuple(page.page_id for page in pages),
            created_at=_AS_OF,
            coverage_start=_CHART_START,
            coverage_end=_AS_OF,
            quality_state=QualityState.COMPLETE,
        )
        store.get_authenticated_dataset_material(dataset_id)
    finally:
        store.close()


def _persist_quote() -> str:
    config = load_analytics_config(os.environ)
    handle = instrument_handle_for_saxo_identity("Stock", 1)
    row: dict[str, SourceJsonValue] = {
        "Uic": 1,
        "AssetType": "Stock",
        "PriceTypeBid": "Tradable",
        "PriceTypeAsk": "Tradable",
        "Quote": {
            "Bid": 99.0,
            "Ask": 101.0,
            "Mid": 100.0,
            "DelayedByMinutes": 0,
            "PriceType": "Tradable",
        },
    }
    contract = source_contracts_by_id()["info_price_v1"]
    comparison = compare_source_schema(contract, row)
    frozen = freeze_source_rows((row,))
    capture_revision = f"capture:{1001:032x}"
    page = SourcePage(
        contract_id=contract.contract_id,
        operation_id=contract.operation_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_kind=contract.source_kind,
        capture_revision=capture_revision,
        source_timestamp=_AS_OF,
        account_scope="aggregate",
        instrument_scope_sha256=hashlib.sha256(handle.encode()).hexdigest(),
        request_fingerprint_sha256=hashlib.sha256(b"quote").hexdigest(),
        page_number=1,
        rows=frozen,
        row_count=1,
        source_revision="fixture:quote",
        page_fingerprint_sha256=source_page_fingerprint((row,)),
        schema_comparison=comparison,
        source_quality=source_quality_proof(contract, frozen, comparison),
    )
    assert page.source_quality.state == "complete"
    quote = normalize_quote(
        row=row,
        instrument_handle=handle,
        captured_at=_AS_OF,
        evaluated_at=_AS_OF,
        max_age=timedelta(minutes=5),
    )
    row_fingerprint = sync._quote_row_fingerprint(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        quote, quality_state=QualityState.COMPLETE, entitlement_state="available", warnings=()
    )
    correction_state = {"capture_kind": "point", "captured_at": _AS_OF.isoformat()}
    fingerprints = sync._capture_fingerprints(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        (page,), row_fingerprint, correction_state
    )
    _, dataset_id = sync._persist_market_capture(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        config=config,
        pages=(page,),
        instrument_handle=handle,
        coverage_start=_AS_OF,
        coverage_end=_AS_OF,
        quality_state=QualityState.COMPLETE,
        sync_metadata={
            "capture_revision": capture_revision,
            "captured_at": _AS_OF.isoformat(),
            "correction_state": correction_state,
            "data_kind": "quote",
            "delayed_by_minutes": 0,
            "entitlement_state": "available",
            "fingerprints": fingerprints.model_dump(mode="json"),
            "freshness": "fresh",
            "instrument_handle": handle,
            "quality_state": "complete",
            "warnings": [],
        },
        normalized_bytes=1,
        persist_normalized=lambda connection, stored: sync._persist_normalized_quote(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            connection=connection,
            pages=(page,),
            stored_page_ids=stored,
            quote=quote,
            row_fingerprint_sha256=row_fingerprint,
        ),
    )
    assert sync.get_dataset(dataset_id, 1, 100, config=config).rows
    return dataset_id


async def _call_job(client: Client[FastMCPTransport], arguments: dict[str, object]) -> JobStatus:
    response = await client.call_tool("saxo_manage_analysis_job", arguments)
    assert not response.is_error
    assert response.structured_content is not None
    body = response.structured_content
    assert body["network_call_made"] is False
    assert body["status"] in {
        "job_queued",
        "job_running",
        "job_completed",
        "job_cancelled",
        "job_failed",
    }, body
    return JobStatus.model_validate_json(json.dumps(body["result"]))


async def _wait_for(
    client: Client[FastMCPTransport], job_id: str, *, progress: bool = False
) -> tuple[JobStatus, tuple[JobStatus, ...]]:
    async with asyncio.timeout(_WAIT_SECONDS):
        observations: list[JobStatus] = []
        while True:
            current = await _call_job(client, {"action": "check", "job_id": job_id})
            observations.append(current)
            if current.state in {"completed", "failed", "cancelled"} or (
                progress and current.progress.completed_units > 0
            ):
                return current, tuple(observations)
            await asyncio.sleep(0.01)


def test_real_mcp_job_computes_and_replays_a_scoped_conclusion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _persist_calibration(monkeypatch, tmp_path)

    async def run() -> tuple[JobStatus, tuple[JobStatus, ...]]:
        try:
            async with Client(create_mcp_server()) as client:
                request = _request(path_count=1000, horizon_periods=2)
                started = await _call_job(client, {"action": "start", "request": request})
                assert not started.conclusion_available
                assert started.analysis_id is None
                terminal, observations = await _wait_for(client, started.job_id)
                duplicate = await _call_job(client, {"action": "start", "request": request})
                assert duplicate.job_id == terminal.job_id
                assert duplicate.analysis_id == terminal.analysis_id
                late_cancel = await _call_job(
                    client, {"action": "cancel", "job_id": terminal.job_id}
                )
                assert late_cancel.state == "completed"
                assert late_cancel.analysis_id == terminal.analysis_id
                return terminal, (started, *observations)
        finally:
            await analytics_tools.shutdown_analytics_runtime()

    terminal, observations = asyncio.run(run())
    assert terminal.state == "completed"
    assert terminal.conclusion_available
    assert terminal.analysis_id is not None
    assert terminal.progress.completed_units == _WORK_UNITS
    assert terminal.progress.remaining_units == 0
    assert tuple(status.progress.completed_units for status in observations) == tuple(
        sorted(status.progress.completed_units for status in observations)
    )
    for status in observations:
        if status.state != "completed":
            assert status.analysis_id is None
            assert not status.conclusion_available
    config = load_analytics_config(os.environ)
    result: AnalysisResult = replay_analysis(
        terminal.analysis_id, config=config, registry=release.load_production_registry(config)
    )
    assert result.provenance.dataset_id == _DATASET
    assert result.account_scope == _ACCOUNT
    assert result.provenance.random_seed == _SEED
    metrics = {metric.metric_id: metric.value for metric in result.metrics}
    assert metrics["goal_probability"] == _EXPECTED_GOAL_PROBABILITY
    assert metrics["sensitivity_upper"] > metrics["sensitivity_lower"] > _STARTING_VALUE
    assert result.tables
    store = AnalyticsStore.open(config)
    try:
        analyses = store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,)))
        assert {entry.object_id for entry in analyses} == {terminal.analysis_id}
        assert store.get_authenticated_dataset_material(_DATASET).dataset.source_revision == (
            _SOURCE_REVISION
        )
    finally:
        store.close()


def test_real_mcp_job_progress_cancellation_never_persists_partial_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _persist_calibration(monkeypatch, tmp_path)

    async def run() -> tuple[JobStatus, JobStatus]:
        try:
            async with Client(create_mcp_server()) as client:
                started = await _call_job(
                    client,
                    {
                        "action": "start",
                        "request": _request(path_count=100_000, horizon_periods=20),
                    },
                )
                active, observations = await _wait_for(client, started.job_id, progress=True)
                assert active.state == "running"
                assert 0 < active.progress.completed_units < active.progress.total_units
                assert all(status.analysis_id is None for status in observations)
                cancelled = await _call_job(client, {"action": "cancel", "job_id": started.job_id})
                readback = await _call_job(client, {"action": "check", "job_id": started.job_id})
                assert readback == cancelled
                return active, cancelled
        finally:
            await analytics_tools.shutdown_analytics_runtime()

    active, cancelled = asyncio.run(run())
    assert cancelled.state == "cancelled"
    assert cancelled.request_fingerprint == active.request_fingerprint
    assert active.progress.completed_units <= cancelled.progress.completed_units < _WORK_UNITS
    assert cancelled.analysis_id is None
    assert not cancelled.artifact_ids
    assert not cancelled.conclusion_available
    store = AnalyticsStore.open(load_analytics_config(os.environ))
    try:
        assert not store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,)))
        assert store.get_authenticated_dataset_material(_DATASET).dataset.quality_state is (
            QualityState.COMPLETE
        )
    finally:
        store.close()


def test_job_recipe_dataset_mismatch_is_rejected_before_work() -> None:
    request = _request(path_count=100, horizon_periods=2)
    request["dataset_ids"] = ["ds_00000000000040008000000000000002"]
    parsed = analytics_tools.AnalyticsJobToolRequest.model_validate_json(json.dumps(request))
    with pytest.raises(ValueError, match="dataset handles must match"):
        parsed.to_domain()


def test_cancel_at_result_commit_preserves_an_existing_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _persist_calibration(monkeypatch, tmp_path)

    async def run() -> tuple[JobStatus, JobStatus]:
        computed = asyncio.Event()
        release_commit = asyncio.Event()
        original_commit = JobExecutionContext.commit_analysis

        async def hold_commit(
            context: JobExecutionContext, result: AnalysisResult
        ) -> JobConclusion:
            computed.set()
            await release_commit.wait()
            return await original_commit(context, result)

        try:
            async with Client(create_mcp_server()) as client:
                request = _request(path_count=100, horizon_periods=2)
                first = await _call_job(client, {"action": "start", "request": request})
                completed, _ = await _wait_for(client, first.job_id)
                assert completed.state == "completed"
                monkeypatch.setattr(JobExecutionContext, "commit_analysis", hold_commit)
                request["total_work_units"] = _WORK_UNITS + 1
                second = await _call_job(client, {"action": "start", "request": request})
                assert second.job_id != first.job_id
                async with asyncio.timeout(_WAIT_SECONDS):
                    await computed.wait()
                stopped = await _call_job(client, {"action": "cancel", "job_id": second.job_id})
                release_commit.set()
                return completed, stopped
        finally:
            release_commit.set()
            await analytics_tools.shutdown_analytics_runtime()

    existing, cancelled = asyncio.run(run())
    assert cancelled.state == "cancelled"
    assert cancelled.analysis_id is None
    assert existing.analysis_id is not None
    store = AnalyticsStore.open(load_analytics_config(os.environ))
    try:
        analyses = store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,)))
        assert {entry.object_id for entry in analyses} == {existing.analysis_id}
    finally:
        store.close()
    assert (
        replay_analysis(
            existing.analysis_id,
            config=load_analytics_config(os.environ),
            registry=release.load_production_registry(load_analytics_config(os.environ)),
        ).analysis_id
        == existing.analysis_id
    )


def test_job_commit_storage_failure_rolls_back_analysis_and_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _persist_calibration(monkeypatch, tmp_path)
    original_put = AnalyticsStore.put_analysis

    def fail_after_insert(store: AnalyticsStore, result: AnalysisResult) -> NoReturn:
        original_put(store, result)
        raise RuntimeError("fixture_result_commit_failed")

    monkeypatch.setattr(AnalyticsStore, "put_analysis", fail_after_insert)

    async def run() -> JobStatus:
        try:
            async with Client(create_mcp_server()) as client:
                started = await _call_job(
                    client,
                    {"action": "start", "request": _request(path_count=100, horizon_periods=2)},
                )
                terminal, _ = await _wait_for(client, started.job_id)
                return terminal
        finally:
            await analytics_tools.shutdown_analytics_runtime()

    failed = asyncio.run(run())
    assert failed.state == "failed"
    assert failed.analysis_id is None
    assert not failed.conclusion_available
    store = AnalyticsStore.open(load_analytics_config(os.environ))
    try:
        assert not store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,)))
        assert store.get_authenticated_dataset_material(_DATASET).dataset.quality_state is (
            QualityState.COMPLETE
        )
    finally:
        store.close()


def test_job_recipe_seed_is_part_of_the_persisted_request_identity() -> None:
    first = analytics_tools.AnalyticsJobToolRequest.model_validate_json(
        json.dumps(_request(path_count=100, horizon_periods=2))
    ).to_domain()
    second = analytics_tools.AnalyticsJobToolRequest.model_validate_json(
        json.dumps(_request(path_count=100, horizon_periods=2, seed=18))
    ).to_domain()
    assert first.recipe_request_json != second.recipe_request_json
    first_recipe = cast("dict[str, object]", json.loads(first.recipe_request_json or "{}"))
    assert first_recipe["dataset_ids"] == [_DATASET]


@pytest.mark.parametrize("job_kind", ["optimization", "backtest"])
def test_real_mcp_job_targets_compute_chart_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_kind: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB", "1")
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    if job_kind == "optimization":
        sources, _, arguments = _optimization_fixture()
        del sources["info_price_v1"]
        charts = [
            _persist_chart(1, tuple(100.0 + index + index % 3 for index in range(40))),
            _persist_chart(2, tuple(100.0 + index * 0.5 + index % 5 for index in range(40))),
        ]
        kind = "portfolio_minimum_variance"
        arguments["objective"] = "minimum_variance"
    else:
        sources, _, arguments = _backtest_fixture()
        charts = [_persist_chart(1, (10.0, 10.0, 12.0, 13.0, 14.0, 10.0, 9.0, 9.0, 10.0))]
        kind = "bounded_backtest"
        options = cast("dict[str, object]", arguments["options"])
        backtest = cast("dict[str, object]", options["backtest"])
        arguments.update(
            {
                "strategy": backtest["strategy"],
                "starting_equity": backtest["starting_equity"],
                "instrument_handle": instrument_handle_for_saxo_identity("Stock", 1),
            }
        )
    _persist_model_sources(sources)
    _release_receipt(monkeypatch)
    arguments.update(
        {"analysis_kind": kind, "dataset_id": _DATASET, "supporting_dataset_ids": charts}
    )
    request: dict[str, object] = {
        "job_kind": job_kind,
        "total_work_units": _WORK_UNITS,
        "analysis_request": json.loads(json.dumps(arguments, default=str)),
    }

    async def run() -> JobStatus:
        try:
            async with Client(create_mcp_server()) as client:
                started = await _call_job(client, {"action": "start", "request": request})
                terminal, _ = await _wait_for(client, started.job_id)
                return terminal
        finally:
            await analytics_tools.shutdown_analytics_runtime()

    terminal = asyncio.run(run())
    assert terminal.state == "completed"
    assert terminal.analysis_id is not None
    config = load_analytics_config(os.environ)
    result = replay_analysis(
        terminal.analysis_id, config=config, registry=release.load_production_registry(config)
    )
    assert result.analysis_kind == kind
    assert result.account_scope == _ACCOUNT
    assert result.tables[0].rows
    if job_kind == "optimization":
        weights = [
            cast("float", cell.value)
            for row in result.tables[0].rows
            for cell in row.cells
            if cell.field == "target_weight"
        ]
        assert sum(weights) == pytest.approx(1.0)
    else:
        assert result.tables[1].rows
        assert "equivalent_broker_execution_validation" in result.unavailable_fields


def test_composite_replay_checks_original_dataset_invalidation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _persist_calibration(monkeypatch, tmp_path)
    config = load_analytics_config(os.environ)
    registry = release.load_production_registry(config)
    store = AnalyticsStore.open(config)
    performance_id = _MODEL_DATASET
    balances_id = "ds_00000000000040008000000000000003"
    try:
        original = store.get_authenticated_dataset_material(_DATASET)
        for identifier, contract_id in (
            (performance_id, "performance_timeseries_v4"),
            (balances_id, "balances_v1"),
        ):
            store.create_dataset(
                dataset_id=identifier,
                account_scope=_ACCOUNT,
                source_scope="saxo_openapi",
                source_revision=_SOURCE_REVISION,
                source_page_ids=tuple(
                    page.page_id for page in original.pages if page.contract_name == contract_id
                ),
                created_at=_AS_OF,
                coverage_start=original.coverage_start,
                coverage_end=original.coverage_end,
                quality_state=QualityState.COMPLETE,
            )
        request = cast(
            "dict[str, object]", _request(path_count=100, horizon_periods=2)["analysis_request"]
        )
        request["dataset_ids"] = [performance_id, balances_id]
        typed = analytics_tools.StoredGoalToolRequest.model_validate_json(json.dumps(request))
        result = execute_analysis(
            tool_name="saxo_manage_analysis_job",
            request=typed,
            config=config,
            store=store,
            registry=registry,
        )
        assert result.provenance.dataset_id not in {performance_id, balances_id}
        assert {
            dependency.dataset_id for dependency in result.provenance.input_dataset_dependencies
        } == {performance_id, balances_id}
        assert replay_analysis(result.analysis_id, config=config, registry=registry) == result
        with store.market_ingestion_transaction(0) as connection:
            connection.execute(
                "UPDATE datasets SET quality_state = 'invalid' WHERE dataset_id = ?",
                (performance_id,),
            )
        assert store.get_authenticated_dataset_material(
            result.provenance.dataset_id
        ).dataset.quality_state is (QualityState.COMPLETE)
        with pytest.raises(AnalysisReplayRefused) as refused:
            replay_analysis(result.analysis_id, config=config, registry=registry)
        assert refused.value.reason_code in {
            "input_dataset_invalidated",
            "input_dataset_integrity_changed",
        }
    finally:
        store.close()


def test_pretrade_child_replay_checks_invalidated_prior_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _persist_calibration(monkeypatch, tmp_path)
    config = load_analytics_config(os.environ)
    registry = release.load_production_registry(config)
    store = AnalyticsStore.open(config)
    try:
        request = _request(path_count=100, horizon_periods=2)["analysis_request"]
        origin = execute_analysis(
            tool_name="saxo_manage_analysis_job",
            request=analytics_tools.StoredGoalToolRequest.model_validate_json(json.dumps(request)),
            config=config,
            store=store,
            registry=registry,
        )
    finally:
        store.close()
    _persist_chart(1, (100.0, 100.0))
    quote_id = _persist_quote()
    sources, arguments = _pretrade_fixture()
    del sources["info_price_v1"]
    _persist_model_sources(sources, dataset_id=_MODEL_DATASET)
    options = cast("dict[str, object]", arguments["options"])
    pretrade = cast("dict[str, object]", options["pretrade"])
    pretrade["origin_analysis_id"] = origin.analysis_id
    arguments.update(
        {
            "dataset_ids": [_MODEL_DATASET, quote_id],
            "instrument_handles": [instrument_handle_for_saxo_identity("Stock", 1)],
        }
    )
    typed = analytics_tools.StoredPretradeToolRequest.model_validate_json(
        json.dumps(arguments, default=str)
    )
    store = AnalyticsStore.open(config)
    try:
        child = execute_analysis(
            tool_name="saxo_propose_trade_from_analysis",
            request=typed,
            config=config,
            store=store,
            registry=registry,
        )
        assert {
            dependency.analysis_id for dependency in child.provenance.analysis_dependencies
        } == {origin.analysis_id}
        assert replay_analysis(child.analysis_id, config=config, registry=registry) == child
        with store.market_ingestion_transaction(0) as connection:
            connection.execute(
                "UPDATE analyses SET status = 'invalidated' WHERE analysis_id = ?",
                (origin.analysis_id,),
            )
        with pytest.raises(AnalysisReplayRefused) as refused:
            replay_analysis(child.analysis_id, config=config, registry=registry)
        assert refused.value.reason_code == "analysis_invalidated"
    finally:
        store.close()
    explanation = analytics_tools.saxo_explain_analysis(child.analysis_id)
    assert explanation.status == "refused"
    exported = analytics_tools.saxo_export_analysis(
        child.analysis_id, "table", "json", table_id=child.tables[0].table_id
    )
    assert exported.structured_content is not None
    assert exported.structured_content["status"] == "refused"
    assert exported.structured_content["network_call_made"] is False
