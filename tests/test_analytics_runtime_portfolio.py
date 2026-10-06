# ruff: noqa: PLR2004 - literal financially meaningful fixture expectations
"""Source-to-recipe checks for portfolio amounts, timing, missingness and identity."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_models import QualityState
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipePayload,
    RecipeRequest,
    ResearchInputs,
)
from saxo_bank_mcp.analytics_runtime_portfolio import PortfolioOptions, execute_recipe
from saxo_bank_mcp.analytics_source_contracts import (
    SourceJsonValue,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    AuthenticatedDatasetMaterial,
    AuthenticatedSourceMaterial,
    StoredDataset,
)
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow

_ALIAS = "aa_00000000000040008000000000000012"
_DATASET = "ds_00000000000040008000000000000042"
_CHART_DATASET = "ds_00000000000040008000000000000043"
_START = datetime(2026, 2, 1, tzinfo=UTC)
_END = _START + timedelta(days=4)
_HANDLE = instrument_handle_for_saxo_identity("Stock", 123)
_BALANCE: dict[str, object] = {
    "Currency": "USD",
    "CashBalance": 250.0,
    "TotalValue": 1250.0,
    "TransactionsNotBooked": -10.0,
    "FundsReservedForSettlement": 20.0,
    "FundsAvailableForSettlement": 220.0,
    "SpendingPower": 200.0,
    "MarginAvailableForTrading": 150.0,
    "MarginUsedByCurrentPositions": -100.0,
    "NetEquityForMargin": 250.0,
    "MarginUtilizationPct": 40.0,
    "CalculationReliability": "Ok",
}
_POSITION: dict[str, object] = {
    "PositionId": "private-position-a",
    "PositionBase": {"Amount": 10.0, "AssetType": "Stock", "Uic": 123},
    "PositionView": {
        "CurrentPrice": 100.0,
        "MarketValueInBaseCurrency": 1000.0,
        "MarketValueOpenInBaseCurrency": 900.0,
        "ProfitLossOnTradeInBaseCurrency": 100.0,
        "ProfitLossCurrencyConversion": 9.0,
        "ExposureInBaseCurrency": 1000.0,
        "ExposureCurrency": "USD",
    },
}


def _series(field_values: list[float]) -> list[dict[str, object]]:
    return [
        {"Date": (_START + timedelta(days=index)).date().isoformat(), "Value": value}
        for index, value in enumerate(field_values)
    ]


def _source_rows() -> dict[str, list[dict[str, object]]]:
    return {
        "balances_v1": [dict(_BALANCE)],
        "positions_v1": [_POSITION],
        "orders_v1": [{"OrderId": "private-order-a", "AssetType": "Stock", "Status": "Working"}],
        "performance_timeseries_v4": [
            {
                "Balance": {
                    "AccountValue": _series([1000.0, 1020.0, 990.0, 1050.0, 1060.0]),
                    "CashTransfer": [],
                    "SecurityTransfer": [],
                },
                "TimeWeighted": {"Accumulated": _series([0.0, 0.02, -0.01, 0.05, 0.06])},
            }
        ],
        "transactions_v1": [],
        "bookings_v1": [
            {
                "BkAmountId": "private-dividend",
                "Date": "2026-02-03",
                "Amount": 20.0,
                "Currency": "USD",
                "BkAmountType": "Dividend",
            },
            {
                "BkAmountId": "private-withholding",
                "Date": "2026-02-03",
                "Amount": -3.0,
                "Currency": "USD",
                "BkAmountType": "Withholding Tax",
            },
            {
                "BkAmountId": "private-commission",
                "Date": "2026-02-02",
                "Amount": -2.0,
                "Currency": "USD",
                "BkAmountType": "Commission",
                "RelatedPositionId": "private-open",
            },
        ],
        "closed_positions_history_v1": [
            {
                "ClosePositionId": "private-close",
                "OpenPositionId": "private-open",
                "AssetType": "Stock",
                "InstrumentCurrency": "USD",
                "AccountCurrency": "USD",
                "InstrumentSymbol": "FIXTURE",
                "AmountOpen": 10.0,
                "AmountClose": 10.0,
                "OpenPrice": 90.0,
                "ClosePrice": 100.0,
                "TradeDateOpen": "2026-02-01",
                "TradeDateClose": "2026-02-03",
                "PnLAccountCurrency": 98.0,
                "TotalBookedOnOpeningLegAccountCurrency": -902.0,
                "TotalBookedOnClosingLegAccountCurrency": 1000.0,
            }
        ],
        "reference_instrument_details_v1": [
            {"Uic": 123, "AssetType": "Stock", "CurrencyCode": "USD"}
        ],
        "reference_instruments_v1": [
            {"Identifier": 123, "AssetType": "Stock", "Symbol": "FIXTURE"}
        ],
        "costs_v1": [
            {
                "HoldingPeriodInDays": 10,
                "Cost": {
                    "Long": {
                        "Currency": "USD",
                        "TotalCost": 9.0,
                        "TradingCost": {"Commissions": [{"Value": 2.0}], "Spread": {"Value": 7.0}},
                    }
                },
            }
        ],
        "corporate_action_events_v2": [
            {
                "EventId": "private-event",
                "Uic": 123,
                "AssetType": "Stock",
                "EventType": {"Code": "DVCA", "Name": "Cash Dividend"},
                "Ex": {"Date": "2026-02-03"},
                "Options": [
                    {
                        "OptionId": "private-option",
                        "Payment": {"Date": "2026-02-10"},
                        "IsDefault": True,
                    }
                ],
            }
        ],
        "corporate_action_holdings_v2": [
            {"EventId": "private-event", "AccountId": "private-account", "Amount": 10.0}
        ],
    }


def _material(
    rows: dict[str, list[dict[str, object]]],
    *,
    dataset_id: str = _DATASET,
    at: datetime = _END,
) -> AuthenticatedDatasetMaterial:
    contracts = source_contracts_by_id()
    pages = tuple(
        AuthenticatedSourceMaterial(
            page_id=f"sp_{index:032x}",
            contract_name=contract,
            contract_sha256=source_contract_fingerprint(contracts[contract]),
            source_kind=contracts[contract].source_kind,
            source_revision="fixture:portfolio",
            source_timestamp=at,
            fingerprint_sha256=f"{index + 1:064x}",
            account_scope=_ALIAS,
            instrument_handle=None,
            payload=cast("dict[str, SourceJsonValue]", {"rows": values}),
        )
        for index, (contract, values) in enumerate(rows.items())
    )
    return AuthenticatedDatasetMaterial(
        dataset=StoredDataset(
            dataset_id,
            "fixture:portfolio",
            "a" * 64,
            sum(len(values) for values in rows.values()),
            100,
            at,
            QualityState.COMPLETE,
        ),
        account_scope=_ALIAS,
        coverage_start=_START,
        coverage_end=at,
        pages=pages,
    )


def _inputs(
    tmp_path: Path, rows: dict[str, list[dict[str, object]]] | None = None
) -> ResearchInputs:
    source = _source_rows() if rows is None else rows
    material = _material(source)
    bars = tuple(
        PriceBarDatasetRow(
            instrument_handle=_HANDLE,
            bar_time=_START + timedelta(days=index),
            interval=ChartInterval.ONE_DAY,
            open_value=price,
            high_value=price,
            low_value=price,
            close_value=price,
            volume_value=10.0,
            adjusted=False,
        )
        for index, price in enumerate([100.0, 101.0, 99.0, 103.0, 105.0])
    )
    chart = _material({"chart_v3": []}, dataset_id=_CHART_DATASET)
    chart = AuthenticatedDatasetMaterial(
        chart.dataset,
        chart.account_scope,
        chart.coverage_start,
        chart.coverage_end,
        tuple(
            AuthenticatedSourceMaterial(
                page_id="sp_00000000000040008000000000009999",
                contract_name=page.contract_name,
                contract_sha256=page.contract_sha256,
                source_kind=page.source_kind,
                source_revision=page.source_revision,
                source_timestamp=page.source_timestamp,
                fingerprint_sha256=page.fingerprint_sha256,
                account_scope=page.account_scope,
                instrument_handle=_HANDLE,
                payload={
                    "rows": [],
                    "sync_metadata": {"data_kind": "price_bars", "missing_interval_count": 0},
                },
            )
            for page in chart.pages
        ),
    )
    return ResearchInputs(
        load_analytics_config({"XDG_STATE_HOME": str(tmp_path)}),
        cast("AnalyticsStore", None),
        (material, chart),
        {_CHART_DATASET: bars},
    )


def _run(kind: str, inputs: ResearchInputs, **options: object) -> RecipePayload:
    return execute_recipe(RecipeRequest(kind, (_DATASET,), arguments={"options": options}), inputs)


def _values(payload: RecipePayload) -> dict[str, float]:
    return {metric.metric_id: metric.value for metric in payload.metrics}


@pytest.mark.parametrize(
    "kind",
    [
        "portfolio_overview",
        "cash_and_settlement",
        "portfolio_margin",
        "portfolio_exposure",
        "portfolio_performance",
        "portfolio_risk",
        "portfolio_attribution",
        "income_calendar",
        "corporate_action_center",
        "cost_xray",
        "regulatory_cost_report",
        "trading_mirror",
        "execution_quality",
        "tax_lot_export",
    ],
)
def test_complete_sources_produce_substantive_portfolio_reports(tmp_path: Path, kind: str) -> None:
    payload = _run(kind, _inputs(tmp_path))
    assert payload.metrics or payload.tables
    assert payload.verifies
    assert all(
        cell.unit is not None
        for table in payload.tables
        for row in table.rows
        for cell in row.cells
        if type(cell.value) in {int, float}
    )
    assert all(metric.value == metric.value for metric in payload.metrics)
    encoded = str(payload)
    assert "private-" not in encoded
    if kind not in {"portfolio_margin", "cash_and_settlement"}:
        assert payload.tables


def test_overview_cash_and_margin_keep_money_classes_distinct(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    overview = _values(_run("portfolio_overview", inputs))
    cash = _values(_run("cash_and_settlement", inputs))
    margin = _values(_run("portfolio_margin", inputs))
    attribution = _values(_run("portfolio_attribution", inputs))
    assert overview["account_value"] == 1250.0
    assert overview["cash_balance"] == 250.0
    assert cash["settled_cash"] == 220.0
    assert cash["unsettled_cash"] == -10.0
    assert margin["margin_headroom"] == 150.0
    assert margin["margin_utilization"] == 0.4
    assert attribution["local_asset_contribution"] == pytest.approx(100 / 9)
    assert attribution["currency_contribution"] == 1.0


def test_missing_optional_cash_is_unavailable_and_bad_reconciliation_refuses(
    tmp_path: Path,
) -> None:
    rows = _source_rows()
    del rows["balances_v1"][0]["SpendingPower"]
    result = _run("cash_and_settlement", _inputs(tmp_path, rows))
    assert "buying_power" not in _values(result)
    assert "SpendingPower" in result.unavailable_fields
    rows["balances_v1"][0]["FundsAvailableForSettlement"] = 221.0
    with pytest.raises(AnalyticsExecutionError, match="cash_settlement_reconciliation_failed"):
        _run("cash_and_settlement", _inputs(tmp_path, rows))


def test_broker_return_series_prevents_deposit_from_becoming_investment_return(
    tmp_path: Path,
) -> None:
    rows = _source_rows()
    performance = rows["performance_timeseries_v4"][0]
    performance["Balance"] = {
        "AccountValue": _series([1000.0, 1020.0, 1090.0, 1150.0, 1160.0]),
        "CashTransfer": [{"Date": "2026-02-03", "Value": 100.0}],
    }
    result = _run("portfolio_performance", _inputs(tmp_path, rows))
    assert _values(result)["time_weighted_return"] == pytest.approx(0.06)
    del performance["TimeWeighted"]
    with pytest.raises(AnalyticsExecutionError, match="source_object_field_unavailable"):
        _run("portfolio_risk", _inputs(tmp_path, rows))


def test_income_quantity_cost_illustration_and_charges_are_not_interchangeable(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    income = _values(_run("income_calendar", inputs))
    cost = _values(_run("cost_xray", inputs))
    assert income["income_amount"] == 20.0
    assert income["dividend_amount"] == 20.0
    assert cost["total_cost"] == 2.0
    assert cost["commission_cost"] == 2.0
    action = _run("corporate_action_center", inputs)
    assert "dividend_amount" not in _values(action)
    assert action.tables[0].rows[0].cells[0].value == 10.0
    estimated = _values(_run("regulatory_cost_report", inputs, cost_report_basis="ex_ante"))
    assert estimated["regulatory_ex_ante_cost"] == 9.0


def test_corporate_calendar_uses_nested_dates_and_distinguishes_empty_capture(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    report = _run("corporate_action_center", inputs)
    assert report.tables[0].rows[0].instrument_handle == _HANDLE
    assert report.tables[0].rows[0].at == _START + timedelta(days=2)
    assert report.tables[1].rows[0].at == datetime(2026, 2, 10, tzinfo=UTC)
    assert "private-" not in str(report)
    rows = _source_rows()
    rows["corporate_action_events_v2"] = []
    rows["corporate_action_holdings_v2"] = []
    empty = _run("corporate_action_center", _inputs(tmp_path, rows))
    assert empty.tables[0].rows == ()
    assert "no matching events" in empty.verifies[0]
    rows.pop("corporate_action_events_v2")
    with pytest.raises(AnalyticsExecutionError, match="corporate_action_capture_required"):
        _run("corporate_action_center", _inputs(tmp_path, rows))


def test_bookings_deduplicate_identical_rows_and_refuse_conflicting_revisions(
    tmp_path: Path,
) -> None:
    rows = _source_rows()
    rows["bookings_v1"].append(dict(rows["bookings_v1"][0]))
    assert _values(_run("income_calendar", _inputs(tmp_path, rows)))["income_amount"] == 20.0
    rows["bookings_v1"][-1]["Amount"] = 21.0
    with pytest.raises(AnalyticsExecutionError, match="portfolio_source_revision_ambiguous"):
        _run("income_calendar", _inputs(tmp_path, rows))


def test_missing_currency_and_cost_refunds_never_receive_inferred_money_basis(
    tmp_path: Path,
) -> None:
    rows = _source_rows()
    del rows["bookings_v1"][0]["Currency"]
    with pytest.raises(
        AnalyticsExecutionError, match="portfolio_booking_currency_basis_unavailable"
    ):
        _run("income_calendar", _inputs(tmp_path, rows))
    rows = _source_rows()
    rows["bookings_v1"][-1]["BkAmountType"] = "Commission Refund"
    with pytest.raises(
        AnalyticsExecutionError, match="portfolio_cost_refund_semantics_unavailable"
    ):
        _run("cost_xray", _inputs(tmp_path, rows))


def test_comparison_requires_chosen_calendar_aligned_proxy(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = _run("portfolio_comparison", inputs, benchmark_handle=_HANDLE)
    assert _values(result)["active_return"] == pytest.approx(0.01)
    assert "benchmark_proxy_price_return_not_official" in result.warnings
    with pytest.raises(AnalyticsExecutionError, match="portfolio_benchmark_choice_required"):
        _run("portfolio_comparison", inputs)


def test_trade_review_uses_classified_costs_and_never_total_booked_principal(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    result = _run("trading_mirror", inputs)
    assert _values(result)["win_rate"] == 100.0
    assert "turnover" not in _values(result)
    assert "turnover" in result.unavailable_fields
    cells = {cell.field: cell.value for cell in result.tables[0].rows[0].cells}
    assert cells["gross_pnl"] == 100.0
    assert cells["net_pnl"] == 98.0
    assert cells["gross_traded_notional"] == 1900.0
    assert "individual_fills" in result.unavailable_fields
    assert "arrival_price" in result.unavailable_fields


def test_execution_quality_does_not_call_a_current_quote_historical_arrival(tmp_path: Path) -> None:
    result = _run("execution_quality", _inputs(tmp_path))
    assert "vwap" not in _values(result)
    assert result.tables[1].rows[0].cells[0].value == 90.0
    assert {"vwap", "arrival_price", "slippage", "spread"} <= set(result.unavailable_fields)


def test_time_machine_reprices_baseline_holding_and_freezes_cash(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    position = dict(_POSITION)
    position["PositionView"] = {
        **cast("dict[str, object]", _POSITION["PositionView"]),
        "CurrentPrice": 100.0,
    }
    old = _material({"balances_v1": [dict(_BALANCE)], "positions_v1": [position]}, at=_START)
    # Source page identities are immutable and distinct across snapshots.
    old = AuthenticatedDatasetMaterial(
        old.dataset,
        old.account_scope,
        old.coverage_start,
        old.coverage_end,
        tuple(
            AuthenticatedSourceMaterial(
                page_id=f"sp_{100 + index:032x}",
                contract_name=page.contract_name,
                contract_sha256=page.contract_sha256,
                source_kind=page.source_kind,
                source_revision=page.source_revision,
                source_timestamp=page.source_timestamp,
                fingerprint_sha256=page.fingerprint_sha256,
                account_scope=page.account_scope,
                instrument_handle=page.instrument_handle,
                payload=page.payload,
            )
            for index, page in enumerate(old.pages)
        ),
    )
    rows = _source_rows()
    rows.pop("corporate_action_events_v2")
    rows.pop("corporate_action_holdings_v2")
    current = _material(rows)
    inputs.materials = (old, current, inputs.materials[1])
    result = _run("portfolio_time_machine", inputs)
    assert _values(result)["do_nothing_counterfactual"] == -50.0
    assert result.tables[1].rows[0].cells[0].value == 1300.0
    assert "counterfactual_constant_cash_no_income_or_financing" in result.warnings


def test_query_routes_to_same_executor_and_tax_basis_refuses(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = execute_recipe(
        RecipeRequest(
            "portfolio_query",
            (_DATASET,),
            arguments={
                "query_intent": {"intent": "metric", "metric": "cash_balance"},
            },
        ),
        inputs,
    )
    assert _values(result)["cash_balance"] == 250.0
    with pytest.raises(AnalyticsExecutionError, match="authoritative_tax_lot_basis_unavailable"):
        _run("tax_lot_export", inputs, tax_lot_export_mode="authoritative_tax_lots")


def test_tax_reconciliation_exports_source_legs_without_tax_authority(tmp_path: Path) -> None:
    rows = _source_rows()
    result = _run(
        "tax_lot_export",
        _inputs(
            tmp_path,
            {key: rows[key] for key in ("closed_positions_history_v1", "reference_instruments_v1")},
        ),
    )
    assert not result.metrics
    assert result.tables[0].title == "Closed trade activity for tax reconciliation"
    opening, closing = result.tables[0].rows
    assert opening.at == _START
    assert closing.at == _START + timedelta(days=2)
    assert opening.instrument_handle == closing.instrument_handle == _HANDLE
    assert [(cell.field, cell.value) for cell in opening.cells] == [
        ("quantity", 10.0),
        ("price", 90.0),
    ]
    assert [(cell.field, cell.value) for cell in closing.cells] == [
        ("quantity", 10.0),
        ("price", 100.0),
    ]
    assert opening.cells[0].currency is None
    assert opening.cells[1].currency == "USD"
    assert {"tax_cost_basis", "lot_matching", "tax_treatment"} <= set(result.unavailable_fields)
    assert "private-" not in str(result)


def test_tax_trade_activity_period_and_currency_are_source_bound(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = _run("tax_lot_export", inputs, start_at=_START + timedelta(days=2), end_at=_END)
    assert result.tables[0].rows[0].at == _START
    with pytest.raises(AnalyticsExecutionError, match="closed_trade_history_unavailable_in_period"):
        _run("tax_lot_export", inputs, start_at=_END, end_at=_END)
    rows = _source_rows()
    del rows["closed_positions_history_v1"][0]["InstrumentCurrency"]
    with pytest.raises(AnalyticsExecutionError, match="source_text_field_unavailable"):
        _run("tax_lot_export", _inputs(tmp_path, rows))


def test_trade_period_keeps_entry_costs_and_same_day_dates_do_not_fake_intraday_precision(
    tmp_path: Path,
) -> None:
    result = _run(
        "trading_mirror", _inputs(tmp_path), start_at=_START + timedelta(days=2), end_at=_END
    )
    cells = {cell.field: cell.value for cell in result.tables[0].rows[0].cells}
    assert cells["net_pnl"] == 98.0
    assert cells["holding_calendar_days"] == 2
    with pytest.raises(AnalyticsExecutionError, match="closed_trade_history_unavailable_in_period"):
        _run("execution_quality", _inputs(tmp_path), start_at=_END, end_at=_END)
    rows = _source_rows()
    rows["closed_positions_history_v1"][0]["TradeDateOpen"] = "2026-02-03"
    same_day = _run("trading_mirror", _inputs(tmp_path, rows))
    cells = {cell.field: cell.value for cell in same_day.tables[0].rows[0].cells}
    assert cells["holding_calendar_days"] == 0
    assert "holding_hours" not in cells


def test_options_reject_source_amounts_and_unordered_periods() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        PortfolioOptions.model_validate({"account_value": 100.0})
    with pytest.raises(ValueError, match="ordered"):
        PortfolioOptions(start_at=_END, end_at=_START)


def test_price_proxy_currency_and_requested_return_period_must_match(tmp_path: Path) -> None:
    rows = _source_rows()
    rows["reference_instrument_details_v1"][0]["CurrencyCode"] = "EUR"
    with pytest.raises(
        AnalyticsExecutionError, match="portfolio_benchmark_currency_basis_unavailable"
    ):
        _run("portfolio_comparison", _inputs(tmp_path, rows), benchmark_handle=_HANDLE)
    with pytest.raises(AnalyticsExecutionError, match="portfolio_requested_period_incomplete"):
        _run(
            "portfolio_performance",
            _inputs(tmp_path),
            start_at=_START - timedelta(days=1),
            end_at=_END,
        )


@pytest.mark.parametrize(
    "quality", [QualityState.MISSING, QualityState.INVALID, QualityState.STALE]
)
def test_explicit_unusable_source_quality_refuses_current_account_report(
    tmp_path: Path,
    quality: QualityState,
) -> None:
    inputs = _inputs(tmp_path)
    material = inputs.materials[0]
    inputs.materials = (
        replace(material, dataset=replace(material.dataset, quality_state=quality)),
    )
    with pytest.raises(AnalyticsExecutionError, match="portfolio_source_data_unusable"):
        _run("portfolio_overview", inputs)


def test_interest_financing_is_a_cost_and_date_precision_never_proves_arrival(
    tmp_path: Path,
) -> None:
    rows = _source_rows()
    rows["bookings_v1"].append(
        {
            "BkAmountId": "private-financing",
            "Date": "2026-02-02",
            "Amount": -4.0,
            "Currency": "USD",
            "BkAmountType": "Financing Interest",
        }
    )
    inputs = _inputs(tmp_path, rows)
    assert _values(_run("income_calendar", inputs))["income_amount"] == 20.0
    assert _values(_run("cost_xray", inputs))["financing_cost"] == 4.0
    rows = _source_rows()
    rows["closed_positions_history_v1"][0]["TradeDateOpen"] = "2026-02-01T00:00:00"
    result = _run("trading_mirror", _inputs(tmp_path, rows))
    assert "arrival_price" in result.unavailable_fields


def test_terminal_total_loss_is_a_valid_return_and_drawdown(tmp_path: Path) -> None:
    rows = _source_rows()
    rows["performance_timeseries_v4"][0] = {
        "Balance": {
            "AccountValue": _series([1000.0, 1020.0, 990.0, 1050.0, 0.0]),
            "CashTransfer": [],
        },
        "TimeWeighted": {"Accumulated": _series([0.0, 0.02, -0.01, 0.05, -1.0])},
    }
    inputs = _inputs(tmp_path, rows)
    result = _run("portfolio_performance", inputs)
    assert _values(result)["time_weighted_return"] == -1.0
    assert "cagr" in result.unavailable_fields
    risk = _run("portfolio_risk", inputs)
    assert _values(risk)["maximum_drawdown"] == -1.0
    assert _values(risk)["expected_shortfall"] == 1.0
