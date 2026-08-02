from __future__ import annotations

from datetime import UTC, datetime

import pytest

from saxo_bank_mcp.analytics_query import (
    AnalysisQueryIntent,
    BreakdownQueryIntent,
    CapabilityQueryIntent,
    EventQueryIntent,
    MetricQueryIntent,
    PortfolioQueryError,
    parse_portfolio_query,
)

_ALIAS = "aa_00000000000040008000000000000008"
_HANDLE = "ih_00000000000040008000000000000038"
_START = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {
                "intent": "metric",
                "metric": "total_return",
                "account_alias": _ALIAS,
                "start_at": _START,
                "end_at": _END,
            },
            MetricQueryIntent,
        ),
        (
            {
                "intent": "breakdown",
                "metric": "exposure",
                "dimension": "currency",
                "account_alias": _ALIAS,
                "as_of": _END,
            },
            BreakdownQueryIntent,
        ),
        (
            {
                "intent": "events",
                "event_type": "income",
                "account_alias": _ALIAS,
                "instrument_handle": _HANDLE,
                "start_at": _START,
                "end_at": _END,
            },
            EventQueryIntent,
        ),
        (
            {
                "intent": "capability",
                "capability": "tax_lot_export",
                "account_alias": _ALIAS,
            },
            CapabilityQueryIntent,
        ),
        (
            {
                "intent": "analysis",
                "analysis_kind": "portfolio_time_machine",
                "account_alias": _ALIAS,
                "as_of": _END,
            },
            AnalysisQueryIntent,
        ),
    ],
)
def test_typed_approved_portfolio_query_intents_round_trip(
    payload: dict[str, object],
    expected_type: type[object],
) -> None:
    parsed = parse_portfolio_query(payload)

    assert isinstance(parsed, expected_type)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "intent": "metric",
            "metric": "total_return",
            "account_alias": _ALIAS,
            "sql": "SELECT * FROM positions",
        },
        {
            "intent": "metric",
            "metric": "__import__('os').system('id')",
            "account_alias": _ALIAS,
        },
        {
            "intent": "metric",
            "metric": "portfolio_value + cash_balance",
            "account_alias": _ALIAS,
        },
        {
            "intent": "metric",
            "metric": "portfolio_value",
            "account_alias": "/private/tmp/account.json",
        },
        {
            "intent": "metric",
            "metric": "portfolio_value",
            "account_alias": _ALIAS,
            "path": "~/portfolio.csv",
        },
        {
            "intent": "metric",
            "metric": "portfolio_value",
            "account_alias": _ALIAS,
            "url": "https://example.invalid/account",
        },
        {
            "intent": "metric",
            "metric": "portfolio_value",
            "account_alias": _ALIAS,
            "python": "open('/tmp/value').read()",
        },
        {
            "intent": "metric",
            "metric": "portfolio_value",
            "account_alias": _ALIAS,
            "expression": "positions[0].value",
        },
    ],
)
def test_query_catalog_rejects_sql_python_expressions_paths_and_network(
    payload: dict[str, object],
) -> None:
    with pytest.raises(PortfolioQueryError, match="approved typed intent"):
        parse_portfolio_query(payload)
