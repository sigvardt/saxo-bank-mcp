"""Every stock/market recipe reaches a stored, replayable normal MCP result."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from fastmcp import Client
from test_analytics_production_runtime import _checked_release
from test_analytics_sync import _PayloadExecutor

import saxo_bank_mcp.mcp_analytics_tools as api
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import AnalysisResult
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_reference_data import capture_instrument_details
from saxo_bank_mcp.analytics_release import load_production_registry
from saxo_bank_mcp.analytics_resolver import InstrumentResolver
from saxo_bank_mcp.analytics_runtime_market import SUPPORTED_KINDS
from saxo_bank_mcp.analytics_source_contracts import (
    build_source_capture_context,
    build_source_capture_envelope,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_sync import capture_quote, sync_price_bars
from saxo_bank_mcp.server import create_mcp_server

_AT = datetime(2026, 9, 1, 12, tzinfo=UTC)
_START = _AT - timedelta(minutes=10)


async def _capture(config: AnalyticsConfig) -> tuple[dict[int, str], dict[int, list[str]]]:
    handles: dict[int, str] = {}
    datasets: dict[int, list[str]] = {}
    for identifier, asset_type in ((1, "Stock"), (2, "Stock"), (3, "Bond")):
        reference = {
            "Data": [
                {
                    "Identifier": identifier,
                    "AssetType": asset_type,
                    "CurrencyCode": "USD",
                    "Description": "Synthetic instrument",
                    "Symbol": f"SYN{identifier}",
                    "ExchangeId": "XSYN",
                }
            ]
        }
        provider = SaxoAnalyticsProvider(request_executor=_PayloadExecutor((reference,)))
        resolution = await InstrumentResolver(provider, config).resolve_instruments(
            f"SYN{identifier}", (), ()
        )
        handle = resolution.matches[0].instrument_handle
        handles[identifier] = handle
        datasets[identifier] = []
        quote = {
            "Uic": identifier,
            "AssetType": asset_type,
            "PriceTypeBid": "RealTime",
            "PriceTypeAsk": "RealTime",
            "Quote": {
                "Bid": 99.0,
                "Ask": 101.0,
                "Mid": 100.0,
                "PriceType": "RealTime",
                "DelayedByMinutes": 0,
            },
        }
        provider = SaxoAnalyticsProvider(request_executor=_PayloadExecutor((quote,)))
        captured = await capture_quote(handle, provider=provider, config=config, clock=lambda: _AT)
        datasets[identifier].append(captured.datasets[0].dataset_id)
        details = {
            "Data": [
                {
                    "Uic": identifier,
                    "AssetType": asset_type,
                    "CurrencyCode": "USD",
                    "LotSize": 1.0,
                    "TradingStatus": "Tradable",
                }
            ]
        }
        provider = SaxoAnalyticsProvider(request_executor=_PayloadExecutor((details,)))
        captured = await capture_instrument_details(handle, provider=provider, config=config)
        datasets[identifier].append(captured.datasets[0].dataset_id)
        if asset_type == "Bond":
            continue
        closes = (100.0, 110.0, 99.0, 120.0) if identifier == 1 else (200.0, 190.0, 220.0, 210.0)
        bars = {
            "DataVersion": 1,
            "Data": [
                {
                    "Time": (_START + timedelta(minutes=index)).isoformat(),
                    "OpenBid": close - 1.0,
                    "HighBid": close + 2.0,
                    "LowBid": close - 2.0,
                    "CloseBid": close,
                    "Volume": 10.0 + index,
                    "PriceType": "RealTime",
                }
                for index, close in enumerate(closes)
            ],
        }
        provider = SaxoAnalyticsProvider(request_executor=_PayloadExecutor((bars,)))
        captured = await sync_price_bars(
            handle,
            ChartInterval.ONE_MINUTE,
            _START,
            _START + timedelta(minutes=3),
            provider=provider,
            config=config,
            clock=lambda: _AT,
        )
        datasets[identifier].append(captured.datasets[0].dataset_id)
        request = {
            "AccountKey": "ak",
            "Uic": identifier,
            "AssetType": asset_type,
            "Amount": 1.0,
            "Price": 100.0,
            "HoldingPeriodInDays": 30,
        }
        cost: dict[str, object] = {
            **request,
            "Currency": "USD",
            "AccountCurrency": "USD",
            "AccountID": "ab",
            "Instrument": "Synthetic instrument",
            "CostCalculationAssumptions": [],
            "Cost": {
                "Long": {
                    "Currency": "USD",
                    "TotalCost": 3.0 if identifier == 1 else 5.0,
                    "TradingCost": {"Commissions": [], "Spread": {"Value": 0.5}},
                }
            },
        }
        provider = SaxoAnalyticsProvider(request_executor=_PayloadExecutor((cost,)))
        capture = build_source_capture_context({"costs_v1": request}, captured_at=_AT)
        pages = tuple([page async for page in provider.fetch("costs_v1", request, capture=capture)])
        store = AnalyticsStore.open(config)
        try:
            captured_source = store.ingest_source_capture(
                build_source_capture_envelope(capture, pages)
            )
            datasets[identifier].append(captured_source.dataset.dataset_id)
        finally:
            store.close()
    return handles, datasets


@pytest.mark.parametrize("kind", sorted(SUPPORTED_KINDS))
def test_all_market_recipes_have_normal_saved_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    config = api._analytics_config()  # noqa: SLF001 - the normal server configuration
    handles, datasets = asyncio.run(_capture(config))
    _checked_release(monkeypatch)
    comparison = {
        "market_comparison",
        "market_correlation_regime",
        "market_volatility_dispersion",
        "multi_instrument_comparison",
        "wrapper_comparison",
    }
    chosen = (1, 2) if kind in comparison else (3,) if kind == "fixed_income" else (1,)
    selected = [identifier for index in chosen for identifier in datasets[index]]
    options: dict[str, object] = {
        "risk_free_period_return_assumption": 0.0,
        "indicator_parameters": {
            "moving_average_window": 2,
            "rsi_period": 2,
            "macd_fast_period": 2,
            "macd_slow_period": 3,
            "macd_signal_period": 2,
            "atr_period": 2,
            "bollinger_window": 2,
            "bollinger_width": 2.0,
            "momentum_lookback": 1,
            "periods_per_year": 252.0,
            "level_wing": 1,
        },
    }
    if kind == "fixed_income":
        options["fixed_income_model"] = {
            "cash_flows": [{"years_from_settlement": 1.0, "amount": 110.0}],
            "compounding_frequency": 1,
            "day_count_basis": "ACT/365",
            "quote_price_scale_assumption": 1.0,
            "accrued_interest_assumption": 0.0,
        }
    request: dict[str, object] = {
        "analysis_kind": kind,
        "dataset_ids": selected,
        "options": options,
    }
    if kind == "saved_condition_checks":
        request["conditions"] = [
            {
                "condition_id": "above_110",
                "instrument_handle": handles[1],
                "dataset_id": datasets[1][2],
                "kind": "price_above",
                "threshold": 110.0,
            }
        ]
    tool = "saxo_analyze_market"
    if kind in get_args(api.StoredInstrumentToolRequest.model_fields["analysis_kind"].annotation):
        request["instrument_handles"] = [handles[index] for index in chosen]
        request["rolling_window"] = 2
        tool = "saxo_analyze_instruments"

    async def call() -> AnalysisResult:
        async with Client(create_mcp_server()) as client:
            response = await client.call_tool(tool, {"request": request})
        assert not response.is_error
        body = response.structured_content
        assert isinstance(body, dict)
        assert body["status"] in {"verified", "degraded"}, (
            body.get("reason_code"),
            body.get("message"),
        )
        return AnalysisResult.model_validate_json(json.dumps(body["result"]))

    result = asyncio.run(call())
    assert result.tables
    assert (
        replay_analysis(
            result.analysis_id, config=config, registry=load_production_registry(config)
        )
        == result
    )
    metrics = {metric.metric_id: metric.value for metric in result.metrics}
    if kind in {"instrument_price_return", "instrument_risk", "instrument_dossier"}:
        assert metrics["price_return"] == pytest.approx(0.2)
    if kind in {"technical_indicators", "instrument_price_volume"}:
        assert metrics["moving_average"] == pytest.approx(109.5)
    if kind == "wrapper_comparison":
        assert metrics["wrapper_cost_difference"] == pytest.approx(2.0)
    if kind == "fixed_income":
        assert metrics["yield_to_maturity"] == pytest.approx(0.1)
