"""Advanced calculations through the authenticated store and normal public routes."""

# pyright: reportPrivateUsage=false
# ruff: noqa: SLF001, PLR2004 - real ingestion fixtures and explicit arithmetic references.

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client
from pydantic import BaseModel
from test_analytics_production_jobs import _release_receipt
from test_analytics_runtime_models import (
    _ACCOUNT,
    _AS_OF,
    _DATASET,
    _HANDLE,
    _OPTION_HANDLE,
    _SCENARIOS,
    _SECOND_HANDLE,
    _backtest_fixture,
    _futures_fixture,
    _fx_fixture,
    _optimization_fixture,
    _option_choices,
    _option_fixture,
    _pretrade_fixture,
    _quote,
    _scenario_fixture,
    _simulation_fixture,
    _sizing_sources,
    _surface_fixture,
)

import saxo_bank_mcp.analytics_release as release
import saxo_bank_mcp.analytics_sync as sync
import saxo_bank_mcp.mcp_analytics_tools as tools
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_instrument_identity import (
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_market_data import (
    normalize_option_chain,
    normalize_price_series,
    normalize_quote,
)
from saxo_bank_mcp.analytics_metric_definitions import MetricDefinitionCatalog
from saxo_bank_mcp.analytics_models import AnalysisResult, QualityState
from saxo_bank_mcp.analytics_proof_profiles import ProofProfileCatalog
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_runtime import execute_analysis
from saxo_bank_mcp.analytics_runtime_inputs import AnalyticsExecutionError
from saxo_bank_mcp.analytics_runtime_models import REQUIRED_METRICS, SUPPORTED_KINDS
from saxo_bank_mcp.analytics_source_contracts import (
    SourceJsonValue,
    SourcePage,
    compare_source_schema,
    freeze_source_rows,
    source_contract_fingerprint,
    source_contracts_by_id,
    source_page_fingerprint,
    source_quality_proof,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StorageDataType, StorageScope
from saxo_bank_mcp.analytics_sync import (
    DatasetRow,
    OptionReferenceDatasetRow,
    PriceBarDatasetRow,
    QuoteDatasetRow,
)
from saxo_bank_mcp.server import create_mcp_server

_MODEL_DATASET = "ds_00000000000040008000000000000002"
_BOND = instrument_handle_for_saxo_identity("Bond", 40)


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB", "1")
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    _release_receipt(monkeypatch)


def _source_dataset(
    sources: dict[str, list[dict[str, object]]],
    dataset_id: str = _DATASET,
    *,
    captured_at: datetime = _AS_OF,
) -> str:
    config = load_analytics_config(os.environ)
    contracts = source_contracts_by_id()
    store = AnalyticsStore.open(config)
    try:
        pages = tuple(
            store.put_source_page(
                source_kind=contracts[name].source_kind,
                page_key=f"advanced:{dataset_id}:{name}",
                source_revision=f"fixture:{dataset_id}",
                contract_name=name,
                contract_sha256=source_contract_fingerprint(contracts[name]),
                payload={"rows": rows},
                row_count=len(rows),
                source_timestamp=captured_at,
                account_scope=_ACCOUNT,
                instrument_handle=None,
            )
            for name, rows in sources.items()
            if name not in {"chart_v3", "info_price_v1", "options_chain_reference_v1"}
        )
        store.create_dataset(
            dataset_id=dataset_id,
            account_scope=_ACCOUNT,
            source_scope="saxo_openapi",
            source_revision=f"fixture:{dataset_id}",
            source_page_ids=tuple(page.page_id for page in pages),
            created_at=captured_at,
            coverage_start=captured_at,
            coverage_end=captured_at,
            quality_state=QualityState.COMPLETE,
        )
        store.get_authenticated_dataset_material(dataset_id)
    finally:
        store.close()
    return dataset_id


def _page(name: str, rows: list[dict[str, SourceJsonValue]], handle: str) -> SourcePage:
    contract = source_contracts_by_id()[name]
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    body = (
        rows[0]
        if name in {"info_price_v1", "options_chain_reference_v1"}
        else {"Data": rows, "DataVersion": 1}
    )
    comparison = compare_source_schema(contract, body)
    frozen = freeze_source_rows(rows)
    return SourcePage(
        contract_id=name,
        operation_id=contract.operation_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_kind=contract.source_kind,
        capture_revision=f"capture:{digest[:32]}",
        source_timestamp=_AS_OF,
        account_scope="aggregate",
        instrument_scope_sha256=hashlib.sha256(handle.encode()).hexdigest(),
        request_fingerprint_sha256=digest,
        page_number=1,
        rows=frozen,
        row_count=len(rows),
        data_version=1 if name == "chart_v3" else None,
        source_revision="fixture:advanced",
        page_fingerprint_sha256=source_page_fingerprint(rows),
        schema_comparison=comparison,
        source_quality=source_quality_proof(contract, frozen, comparison),
    )


def _quote_dataset(row: dict[str, object]) -> str:
    config = load_analytics_config(os.environ)
    handle = instrument_handle_for_saxo_identity(
        cast("str", row["AssetType"]), cast("int", row["Uic"])
    )
    _register(cast("str", row["AssetType"]), cast("int", row["Uic"]))
    source = cast("dict[str, SourceJsonValue]", row)
    page = _page("info_price_v1", [source], handle)
    quote = normalize_quote(
        row=row,
        instrument_handle=handle,
        captured_at=_AS_OF,
        evaluated_at=_AS_OF,
        max_age=timedelta(minutes=5),
    )
    fingerprint = sync._quote_row_fingerprint(
        quote, quality_state=QualityState.COMPLETE, entitlement_state="available", warnings=()
    )
    correction = {"capture_kind": "point", "captured_at": _AS_OF.isoformat()}
    fingerprints = sync._capture_fingerprints((page,), fingerprint, correction)
    _, dataset_id = sync._persist_market_capture(
        config=config,
        pages=(page,),
        instrument_handle=handle,
        coverage_start=_AS_OF,
        coverage_end=_AS_OF,
        quality_state=QualityState.COMPLETE,
        sync_metadata={
            "capture_revision": page.capture_revision,
            "captured_at": _AS_OF.isoformat(),
            "correction_state": correction,
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
        persist_normalized=lambda connection, stored: sync._persist_normalized_quote(
            connection=connection,
            pages=(page,),
            stored_page_ids=stored,
            quote=quote,
            row_fingerprint_sha256=fingerprint,
        ),
    )
    assert sync.get_dataset(dataset_id, 1, 100, config=config).rows
    return dataset_id


def _register(asset_type: str, identifier: int) -> None:
    config = load_analytics_config(os.environ)
    metadata = json.dumps(
        {"asset_type": asset_type, "identifier": identifier, "display_label": "Offline fixture"},
        sort_keys=True,
        separators=(",", ":"),
    )
    store = AnalyticsStore.open(config)
    try:
        with store.market_ingestion_transaction(0) as connection:
            put_saxo_instrument_identity(
                connection,
                asset_type=asset_type,
                uic=identifier,
                safe_label="Offline fixture",
                source_revision="fixture:advanced",
                source_timestamp=_AS_OF,
                fingerprint_sha256=hashlib.sha256(metadata.encode()).hexdigest(),
                metadata_json=metadata,
                update_existing=False,
            )
    finally:
        store.close()


def _chart_dataset(rows: tuple[PriceBarDatasetRow, ...], asset_type: str, identifier: int) -> str:
    config = load_analytics_config(os.environ)
    _register(asset_type, identifier)
    handle = rows[0].instrument_handle
    source = [
        {
            "Time": row.bar_time.isoformat(),
            "OpenBid": row.open_value,
            "HighBid": row.high_value,
            "LowBid": row.low_value,
            "CloseBid": row.close_value,
            "Volume": row.volume_value,
            "PriceType": "RealTime",
        }
        for row in rows
    ]
    page = _page("chart_v3", cast("list[dict[str, SourceJsonValue]]", source), handle)
    start, end = rows[0].bar_time, rows[-1].bar_time
    normalized = normalize_price_series(
        rows=source, instrument_handle=handle, interval=rows[0].interval, start=start, end=end
    )
    correction: dict[str, object] = {"data_version": 1}
    fingerprints = sync._capture_fingerprints((page,), normalized.fingerprint_sha256, correction)
    _, dataset_id = sync._persist_chart_capture(
        config=config,
        pages=(page,),
        instrument_handle=handle,
        interval=rows[0].interval,
        start=start,
        end=end,
        quality_state=QualityState.COMPLETE,
        fingerprints=fingerprints,
        correction_state=correction,
        prior_page_ids=(),
        retained_page_ids=(),
        normalized_series=normalized,
        coverage=(start, end),
        visible_bar_revisions={bar.bar_time: page.capture_revision for bar in normalized.bars},
        invalidate_prior_analyses=False,
    )
    assert sync.get_dataset(dataset_id, 1, 100, config=config).rows
    return dataset_id


def _option_dataset(
    rows: tuple[OptionReferenceDatasetRow, ...], identifiers: dict[str, tuple[str, int]]
) -> str:
    config = load_analytics_config(os.environ)
    underlying = rows[0].underlying_handle
    underlying_type, underlying_uic = identifiers[underlying]
    option_type = identifiers[rows[0].instrument_handle][0]
    expiry = rows[0].expiry
    source: dict[str, SourceJsonValue] = {
        "OptionRootId": 800,
        "AssetType": option_type,
        "UnderlyingAssetType": underlying_type,
        "CurrencyCode": "USD",
        "OptionSpace": [
            {
                "Expiry": expiry.isoformat(),
                "SpecificOptions": [
                    {
                        "Uic": identifiers[row.instrument_handle][1],
                        "UnderlyingUic": underlying_uic,
                        "StrikePrice": row.strike_value,
                        "PutCall": row.put_call,
                    }
                    for row in rows
                ],
            }
        ],
    }
    page = _page("options_chain_reference_v1", [source], underlying)
    normalized = normalize_option_chain(row=source, expected_root_id=800, expiry=expiry)
    assert {option.source_identifier for option in normalized.options} == {
        identifiers[row.instrument_handle][1] for row in rows
    }
    correction = {"capture_kind": "point", "captured_at": _AS_OF.isoformat()}
    fingerprints = sync._capture_fingerprints((page,), normalized.fingerprint_sha256, correction)
    _, dataset_id = sync._persist_market_capture(
        config=config,
        pages=(page,),
        instrument_handle=underlying,
        coverage_start=_AS_OF,
        coverage_end=_AS_OF,
        quality_state=QualityState.COMPLETE,
        sync_metadata={
            "capture_revision": page.capture_revision,
            "captured_at": _AS_OF.isoformat(),
            "correction_state": correction,
            "data_kind": "option_chain",
            "entitlement_state": "available",
            "fingerprints": fingerprints.model_dump(mode="json"),
            "instrument_handle": underlying,
            "normalized_rows": [option.model_dump(mode="json") for option in normalized.options],
            "warnings": [],
        },
        normalized_bytes=1,
        persist_normalized=lambda connection, stored: sync._persist_normalized_options(
            connection=connection,
            pages=(page,),
            stored_page_ids=stored,
            underlying_handle=underlying,
            chain=normalized,
        ),
    )
    stored = sync.get_dataset(dataset_id, 1, 100, config=config).rows
    assert {row.instrument_handle for row in stored} == {row.instrument_handle for row in rows}
    return dataset_id


def _persist_inputs(
    sources: dict[str, list[dict[str, object]]], rows: tuple[DatasetRow, ...] = ()
) -> tuple[str, ...]:
    """Persist source pages and normalize every requested market row through real owners."""
    identifiers: dict[str, tuple[str, int]] = {}
    for name in ("reference_instruments_v1", "reference_instrument_details_v1", "info_price_v1"):
        for row in sources.get(name, []):
            asset_type = cast("str", row["AssetType"])
            uic = cast("int", row.get("Uic", row.get("Identifier")))
            identifiers[instrument_handle_for_saxo_identity(asset_type, uic)] = (asset_type, uic)
    datasets = [_source_dataset(sources)]
    quotes = tuple(row for row in rows if isinstance(row, QuoteDatasetRow))
    if not quotes:
        quotes = tuple(
            _quote(
                instrument_handle_for_saxo_identity(
                    cast("str", row["AssetType"]), cast("int", row["Uic"])
                ),
                cast("float", cast("dict[str, object]", row["Quote"])["Mid"]),
            )
            for row in sources.get("info_price_v1", [])
        )
    for quote in quotes:
        asset_type, uic = identifiers[quote.instrument_handle]
        matching = next(
            row
            for row in sources["info_price_v1"]
            if row["AssetType"] == asset_type and row["Uic"] == uic
        )
        source = {
            **matching,
            "PriceTypeBid": "Tradable",
            "PriceTypeAsk": "Tradable",
            "Quote": {
                "Bid": quote.bid_value,
                "Ask": quote.ask_value,
                "Mid": quote.mid_value,
                "DelayedByMinutes": 0,
                "PriceType": "Tradable",
            },
        }
        datasets.append(_quote_dataset(source))
    bar_handles = {row.instrument_handle for row in rows if isinstance(row, PriceBarDatasetRow)}
    for handle in sorted(bar_handles):
        bars = tuple(
            row
            for row in rows
            if isinstance(row, PriceBarDatasetRow) and row.instrument_handle == handle
        )
        datasets.append(_chart_dataset(bars, *identifiers[handle]))
    option_expiries = {row.expiry for row in rows if isinstance(row, OptionReferenceDatasetRow)}
    for expiry in sorted(option_expiries):
        references = tuple(
            row
            for row in rows
            if isinstance(row, OptionReferenceDatasetRow) and row.expiry == expiry
        )
        datasets.append(_option_dataset(references, identifiers))
    return tuple(datasets)


def _json_value(value: object) -> str:
    if isinstance(value, date | datetime | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported offline fixture type {type(value).__name__}")


def _request(
    kind: str, datasets: tuple[str, ...], handles: tuple[str, ...], arguments: dict[str, object]
) -> BaseModel:
    values: dict[str, object] = {
        "analysis_kind": kind,
        "dataset_id": datasets[0],
        "supporting_dataset_ids": datasets[1:],
        **arguments,
    }
    request_type: type[BaseModel]
    if kind in {"monte_carlo", "goal_model", "pretrade_impact"}:
        values.pop("dataset_id")
        values.pop("supporting_dataset_ids")
        values["dataset_ids"] = datasets
        request_type = tools.StoredGoalToolRequest
        if kind == "pretrade_impact":
            values["instrument_handles"] = handles
            request_type = tools.StoredPretradeToolRequest
    elif kind == "position_sizing":
        values["instrument_handle"] = handles[0]
        request_type = tools.StoredPositionSizingToolRequest
    elif kind == "bounded_backtest":
        options = cast("dict[str, object]", arguments["options"])
        backtest = cast("dict[str, object]", options["backtest"])
        values.update(
            {
                "instrument_handle": handles[0],
                "strategy": backtest["strategy"],
                "starting_equity": backtest["starting_equity"],
            }
        )
        request_type = tools.StoredBacktestToolRequest
    elif kind in {"portfolio_minimum_variance", "portfolio_risk_parity"}:
        values["objective"] = (
            "minimum_variance" if kind == "portfolio_minimum_variance" else "risk_parity"
        )
        request_type = tools.StoredOptimizationToolRequest
    elif kind in _SCENARIOS and kind != "derivatives_scenario":
        scenario = cast(
            "dict[str, object]", cast("dict[str, object]", arguments["options"])["scenario"]
        )
        shocks = cast("tuple[dict[str, object], ...]", scenario["shocks"])
        values.update(
            {
                "shocks": [
                    {
                        "instrument_handle": shock["instrument_handle"],
                        "price_shock_ratio": shock.get("price_shock_ratio", 0),
                        "rate_shock_basis_points": shock.get("rate_shock_basis_points", 0),
                        "volatility_shock_points": shock.get("volatility_shock_points", 0),
                    }
                    for shock in shocks
                ],
                "numeric_shocks_echoed_by_caller": True,
                "caller_accepted_numeric_shocks": True,
            }
        )
        request_type = tools.StoredScenarioToolRequest
    else:
        values["instrument_handles"] = handles
        request_type = tools.StoredDerivativesToolRequest
    return request_type.model_validate_json(json.dumps(values, default=_json_value))


def _case(
    kind: str,
) -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], tuple[str, ...], dict[str, object]
]:
    if kind in {
        "pretrade_impact",
        "monte_carlo",
        "goal_model",
        "portfolio_minimum_variance",
        "portfolio_risk_parity",
        "bounded_backtest",
        *_SCENARIOS,
    }:
        return _portfolio_case(kind)
    if kind == "position_sizing":
        return (
            _sizing_sources(),
            (_quote(_HANDLE, 100.0),),
            (_HANDLE,),
            {
                "method": "stop_distance",
                "maximum_loss": 100.0,
                "risk_budget_confirmed": True,
                "stop_price": 90.0,
                "options": {
                    "sizing": {
                        "maximum_weight": 0.5,
                        "reserved_buffer": 0.0,
                        "margin_requirement_rate": 1.0,
                    }
                },
            },
        )
    if kind == "futures_curve":
        sources, rows, arguments = _futures_fixture()
        handles = tuple(row.instrument_handle for row in rows if row.instrument_handle != _HANDLE)
        return (sources, rows, handles, arguments)
    if kind == "fx_forward_carry":
        sources, rows, arguments, handle = _fx_fixture()
        return (sources, rows, (handle,), arguments)
    if kind == "iv_surface":
        sources, rows, arguments = _surface_fixture()
        return (sources, rows, (_HANDLE,), arguments)
    sources, rows = _option_fixture()
    choices = _option_choices()
    choices.update(
        {
            "legs": ({"instrument_handle": _OPTION_HANDLE, "quantity": -2.0},),
            "net_premium": -1200.0,
            "eligible_costs": 2.0,
            "expiry_reference_prices": (120.0, 100.0),
        }
    )
    return (sources, rows, (_OPTION_HANDLE,), {"options": {"option": choices}})


def _portfolio_case(
    kind: str,
) -> tuple[
    dict[str, list[dict[str, object]]], tuple[DatasetRow, ...], tuple[str, ...], dict[str, object]
]:
    if kind == "pretrade_impact":
        sources, arguments = _pretrade_fixture()
        return (sources, (_quote(_HANDLE, 100.0),), (_HANDLE,), arguments)
    if kind in {"monte_carlo", "goal_model"}:
        sources, arguments = _simulation_fixture()
        return (sources, (), (), arguments)
    if kind in {"portfolio_minimum_variance", "portfolio_risk_parity"}:
        sources, rows, arguments = _optimization_fixture()
        return (sources, rows, (_HANDLE, _SECOND_HANDLE), arguments)
    if kind in _SCENARIOS:
        sources, rows, arguments, handle = _scenario_fixture(kind)
        if handle == _HANDLE:
            rows = tuple(
                row
                for row in rows
                if isinstance(row, PriceBarDatasetRow) or row.instrument_handle == _HANDLE
            )
        return (sources, rows, (handle,), arguments)
    sources, rows, arguments = _backtest_fixture()
    return (sources, rows, (_HANDLE,), arguments)


def _calculate(request: BaseModel) -> AnalysisResult:
    config = load_analytics_config(os.environ)
    registry = release.load_production_registry(config)
    store = AnalyticsStore.open(config)
    try:
        kind = cast("str", request.model_dump()["analysis_kind"])
        tool_name = {
            "position_sizing": "saxo_size_position",
            "pretrade_impact": "saxo_derive_trade_proposal",
            "monte_carlo": "saxo_manage_analysis_job",
            "goal_model": "saxo_manage_analysis_job",
            "portfolio_minimum_variance": "saxo_optimize_portfolio",
            "portfolio_risk_parity": "saxo_optimize_portfolio",
            "bounded_backtest": "saxo_backtest_strategy",
            **dict.fromkeys(_SCENARIOS, "saxo_run_scenario"),
        }.get(kind, "saxo_model_derivatives")
        result = execute_analysis(
            tool_name=tool_name,
            request=request,
            config=config,
            store=store,
            registry=registry,
        )
    finally:
        store.close()
    assert replay_analysis(result.analysis_id, config=config, registry=registry) == result
    return result


@pytest.mark.parametrize("kind", sorted(SUPPORTED_KINDS))
def test_each_advanced_kind_calculates_and_replays_from_real_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    _setup(monkeypatch, tmp_path)
    sources, rows, handles, arguments = _case(kind)
    if kind == "pretrade_impact":
        calibration, origin_arguments = _simulation_fixture()
        _source_dataset(calibration, _MODEL_DATASET)
        origin = _calculate(_request("monte_carlo", (_MODEL_DATASET,), (), origin_arguments))
        cast("dict[str, object]", cast("dict[str, object]", arguments["options"])["pretrade"])[
            "origin_analysis_id"
        ] = origin.analysis_id
    datasets = _persist_inputs(sources, rows)
    if kind in {"scenario_rate", "scenario_volatility", "derivatives_scenario"}:
        origin = _calculate(
            _request(
                "derivatives_model",
                datasets,
                (_OPTION_HANDLE,),
                {"options": {"option": _option_choices()}},
            )
        )
        scenario = cast(
            "dict[str, object]", cast("dict[str, object]", arguments["options"])["scenario"]
        )
        cast("tuple[dict[str, object], ...]", scenario["shocks"])[0]["model_analysis_id"] = (
            origin.analysis_id
        )
    request = _request(kind, datasets, handles, arguments)
    result = _calculate(request)
    assert _calculate(request) == result
    values = {metric.metric_id: metric.value for metric in result.metrics}
    assert set(REQUIRED_METRICS[kind]) <= values.keys()
    assert result.account_scope == _ACCOUNT
    assert result.tables
    assert {
        dependency.dataset_id for dependency in result.provenance.input_dataset_dependencies
    } == set(datasets)
    assert all(math.isfinite(value) for value in values.values())
    _assert_domain_values(kind, result)


def _assert_domain_values(kind: str, result: AnalysisResult) -> None:
    values = {metric.metric_id: metric.value for metric in result.metrics}
    if kind == "position_sizing":
        assert values["position_size"] == 10.0
    elif kind == "pretrade_impact":
        assert values["instrument_exposure"] == 1100.0
        assert values["estimated_transaction_cost"] == 1.0
    elif kind == "option_payoff":
        assert values["payoff_at_expiry"] == -2802.0
    elif kind == "futures_curve":
        assert values["futures_basis"] == 2.0
        assert abs(values["futures_roll"]) == 41.0
    elif kind == "fx_forward_carry":
        assert values["fx_forward"] == pytest.approx(1.1 * 1.04 / 1.02)
    elif kind in {"monte_carlo", "goal_model"}:
        assert values["goal_probability"] == 1.0
        assert values["ruin_probability"] == 0.0
        assert values["sensitivity_upper"] > values["sensitivity_lower"] > 1000.0
    elif kind in {"scenario_historical", "scenario_custom", "scenario_currency"}:
        assert result.metrics[0].value == pytest.approx(-100.0)
    elif kind in {"portfolio_minimum_variance", "portfolio_risk_parity"}:
        weights = [
            cell.value
            for row in result.tables[0].rows
            for cell in row.cells
            if cell.field == "target_weight"
        ]
        assert sum(cast("list[float]", weights)) == pytest.approx(1.0)
        assert all(cast("float", weight) >= 0 for weight in weights)
    elif kind == "bounded_backtest":
        assert "broker_execution_validation_not_run" in {
            warning.code for warning in result.warnings
        }
        assert result.tables[1].rows
        assert result.tables[2].rows
    else:
        assert any(value != 0 for value in values.values())


def test_public_scenario_refuses_an_invalidated_prior_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)
    sources = _sizing_sources()
    sources["exposure_instruments_v1"] = [{"Uic": 40, "AssetType": "Bond"}]
    sources["positions_v1"] = [
        {
            "PositionBase": {"Uic": 40, "AssetType": "Bond", "Amount": 1.0},
            "PositionView": {
                "ExposureInBaseCurrency": 100.0,
                "ExposureCurrency": "USD",
                "MarketValueInBaseCurrency": 100.0,
            },
        }
    ]
    _source_dataset(sources)
    quote_id = _quote_dataset(
        {
            "Uic": 40,
            "AssetType": "Bond",
            "PriceTypeBid": "Tradable",
            "Quote": {"Bid": 99.0, "Ask": 101.0, "Mid": 100.0, "DelayedByMinutes": 0},
        }
    )
    config = load_analytics_config(os.environ)
    registry = release.load_production_registry(config)
    store = AnalyticsStore.open(config)
    try:
        model = execute_analysis(
            tool_name="saxo_analyze_instruments",
            request=tools.StoredInstrumentToolRequest.model_validate_json(
                json.dumps(
                    {
                        "analysis_kind": "fixed_income",
                        "dataset_ids": [_DATASET, quote_id],
                        "instrument_handles": [_BOND],
                        "options": {
                            "fixed_income_model": {
                                "cash_flows": [{"years_from_settlement": 1.0, "amount": 110.0}],
                                "compounding_frequency": 1,
                                "day_count_basis": "actual_365",
                                "quote_price_scale_assumption": 1.0,
                                "accrued_interest_assumption": 0.0,
                            }
                        },
                    }
                )
            ),
            config=config,
            store=store,
            registry=registry,
        )
    finally:
        store.close()
    assert replay_analysis(model.analysis_id, config=config, registry=registry) == model
    request = {
        "analysis_kind": "scenario_rate",
        "dataset_id": _DATASET,
        "shocks": [
            {"instrument_handle": _BOND, "price_shock_ratio": "0", "rate_shock_basis_points": "100"}
        ],
        "numeric_shocks_echoed_by_caller": True,
        "caller_accepted_numeric_shocks": True,
        "options": {
            "scenario": {
                "shocks": [
                    {
                        "instrument_handle": _BOND,
                        "rate_shock_basis_points": 100.0,
                        "model_analysis_id": model.analysis_id,
                    }
                ]
            }
        },
    }

    async def run() -> None:
        async with Client(create_mcp_server()) as client:
            positive = await client.call_tool("saxo_run_scenario", {"request": request})
            assert not positive.is_error
            assert positive.structured_content is not None
            assert positive.structured_content["status"] in {"verified", "degraded"}, (
                positive.structured_content.get("reason_code")
            )
            current = AnalyticsStore.open(config)
            try:
                with current.market_ingestion_transaction(0) as connection:
                    connection.execute(
                        "UPDATE analyses SET status = 'invalidated' WHERE analysis_id = ?",
                        (model.analysis_id,),
                    )
            finally:
                current.close()
            refused = await client.call_tool("saxo_run_scenario", {"request": request})
            assert not refused.is_error
            assert refused.structured_content is not None
            assert refused.structured_content["status"] == "refused"
            assert refused.structured_content["reason_code"] == "analysis_invalidated"
            assert refused.structured_content["network_call_made"] is False
            assert refused.structured_content["analysis_id"] is None
            assert refused.structured_content["result"]["status"] == "refused"
            assert "metrics" not in refused.structured_content["result"]

    asyncio.run(run())


@pytest.mark.parametrize("age_minutes", [5, 6])
def test_current_model_uses_quote_age_at_analysis_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, age_minutes: int
) -> None:
    _setup(monkeypatch, tmp_path)
    sources, _, handles, arguments = _case("position_sizing")
    _source_dataset(sources, captured_at=_AS_OF + timedelta(minutes=age_minutes))
    quote_id = _quote_dataset(
        {
            "Uic": 1,
            "AssetType": "Stock",
            "PriceTypeBid": "Tradable",
            "PriceTypeAsk": "Tradable",
            "Quote": {
                "Bid": 100.0,
                "Ask": 100.0,
                "Mid": 100.0,
                "DelayedByMinutes": 0,
                "PriceType": "Tradable",
            },
        }
    )
    request = _request("position_sizing", (_DATASET, quote_id), handles, arguments)
    if age_minutes == 5:
        result = _calculate(request)
        assert (
            next(metric.value for metric in result.metrics if metric.metric_id == "position_size")
            == 10.0
        )
        assert result.as_of == _AS_OF + timedelta(minutes=5)
    else:
        with pytest.raises(AnalyticsExecutionError, match="current_quote_unavailable"):
            _calculate(request)


@pytest.mark.parametrize(
    ("kind", "metric_id", "expected"),
    [("option_payoff", "payoff_at_expiry", -2802.0), ("futures_curve", "futures_roll", -41.0)],
)
def test_contract_currency_outputs_replay_for_another_account_currency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, metric_id: str, expected: float
) -> None:
    _setup(monkeypatch, tmp_path)
    sources, rows, handles, arguments = _case(kind)
    sources["balances_v1"][0]["Currency"] = "DKK"
    datasets = _persist_inputs(sources, rows)
    result = _calculate(_request(kind, datasets, handles, arguments))
    metric = next(metric for metric in result.metrics if metric.metric_id == metric_id)
    assert result.request.parameters.reporting_currency == "DKK"
    assert metric.currency == "USD"
    assert metric.value == pytest.approx(expected)
    assert metric.unit == "price_currency"


def test_runtime_refuses_misclassified_native_money_before_persisting_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    definitions = release.load_metric_definition_catalog()
    mismatched = MetricDefinitionCatalog.from_definitions(
        catalog_version=definitions.catalog_version,
        production_metric_ids=definitions.production_metric_ids,
        definitions=tuple(
            definition.model_copy(update={"output_unit": "reporting_currency"})
            if definition.metric_id == "futures_roll"
            else definition
            for definition in definitions.definitions
        ),
    )
    legacy = release.load_proof_profile_catalog(definitions=definitions)
    fixture_catalog = legacy.model_copy(
        update={
            "definition_catalog_sha256": mismatched.fingerprint_sha256,
            "profiles": tuple(
                profile.model_copy(
                    update={"definition_catalog_sha256": mismatched.fingerprint_sha256}
                )
                for profile in legacy.profiles
            ),
        }
    )

    def configured_profiles(*, definitions: MetricDefinitionCatalog) -> ProofProfileCatalog:
        assert definitions.fingerprint_sha256 == mismatched.fingerprint_sha256
        return fixture_catalog

    monkeypatch.setattr(release, "load_metric_definition_catalog", lambda: mismatched)
    monkeypatch.setattr(release, "load_proof_profile_catalog", configured_profiles)
    _setup(monkeypatch, tmp_path)
    sources, rows, handles, arguments = _case("futures_curve")
    sources["balances_v1"][0]["Currency"] = "DKK"
    datasets = _persist_inputs(sources, rows)
    with pytest.raises(AnalyticsExecutionError, match="analysis_currency_mismatch"):
        _calculate(_request("futures_curve", datasets, handles, arguments))
    store = AnalyticsStore.open(load_analytics_config(os.environ))
    try:
        assert store.list_storage(StorageScope(data_types=(StorageDataType.ANALYSES,))) == ()
    finally:
        store.close()
