from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from saxo_bank_mcp.analytics_models import InstrumentHandle, SafeAccountScope, UtcDateTime

type PortfolioMetric = Literal[
    "portfolio_value",
    "cash_balance",
    "total_return",
    "time_weighted_return",
    "money_weighted_return",
    "volatility",
    "maximum_drawdown",
    "gross_exposure",
    "net_exposure",
    "income",
    "total_cost",
    "spending_power",
    "margin_available",
    "settlement_cash",
    "fx_drag",
]
type BreakdownMetric = Literal["exposure", "attribution", "income", "cost"]
type BreakdownDimension = Literal["asset_type", "currency", "instrument", "account"]
type PortfolioEventType = Literal[
    "transactions",
    "income",
    "corporate_actions",
    "settlement",
    "dividends",
]
type PortfolioCapability = Literal[
    "tax_lot_export",
    "corporate_action_center",
    "regulatory_cost_report",
]
type PortfolioAnalysisKind = Literal[
    "portfolio_overview",
    "portfolio_performance",
    "portfolio_risk",
    "portfolio_exposure",
    "portfolio_attribution",
    "portfolio_income",
    "portfolio_liquidity",
    "portfolio_margin",
    "cash_and_settlement",
    "income_calendar",
    "tax_lot_export",
    "regulatory_cost_report",
    "multi_account",
    "full_tearsheet",
    "portfolio_briefing",
    "portfolio_time_machine",
    "settlement_radar",
    "fx_drag_decomposition",
    "corporate_action_center",
    "model_disagreement_radar",
]

_APPROVED_METRICS: Final = (
    "portfolio_value",
    "cash_balance",
    "total_return",
    "time_weighted_return",
    "money_weighted_return",
    "volatility",
    "maximum_drawdown",
    "gross_exposure",
    "net_exposure",
    "income",
    "total_cost",
    "spending_power",
    "margin_available",
    "settlement_cash",
    "fx_drag",
)
_APPROVED_ANALYSES: Final = (
    "portfolio_overview",
    "portfolio_performance",
    "portfolio_risk",
    "portfolio_exposure",
    "portfolio_attribution",
    "portfolio_income",
    "portfolio_liquidity",
    "portfolio_margin",
    "cash_and_settlement",
    "income_calendar",
    "tax_lot_export",
    "regulatory_cost_report",
    "multi_account",
    "full_tearsheet",
    "portfolio_briefing",
    "portfolio_time_machine",
    "settlement_radar",
    "fx_drag_decomposition",
    "corporate_action_center",
    "model_disagreement_radar",
)


class PortfolioQueryError(ValueError):
    """Raised without echoing an unsafe caller payload."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class MetricQueryIntent(_StrictModel):
    """One approved portfolio metric over an optional exact UTC period."""

    intent: Literal["metric"] = "metric"
    metric: PortfolioMetric
    account_alias: SafeAccountScope
    start_at: UtcDateTime | None = None
    end_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def validate_period(self) -> MetricQueryIntent:
        _validate_period(self.start_at, self.end_at)
        return self


class BreakdownQueryIntent(_StrictModel):
    """One approved metric grouped by one approved portfolio dimension."""

    intent: Literal["breakdown"] = "breakdown"
    metric: BreakdownMetric
    dimension: BreakdownDimension
    account_alias: SafeAccountScope
    as_of: UtcDateTime


class EventQueryIntent(_StrictModel):
    """One approved event family over a bounded UTC period."""

    intent: Literal["events"] = "events"
    event_type: PortfolioEventType
    account_alias: SafeAccountScope
    instrument_handle: InstrumentHandle | None = None
    start_at: UtcDateTime
    end_at: UtcDateTime

    @model_validator(mode="after")
    def validate_period(self) -> EventQueryIntent:
        _validate_period(self.start_at, self.end_at)
        return self


class CapabilityQueryIntent(_StrictModel):
    """Capability lookup that does not assert unavailable authority."""

    intent: Literal["capability"] = "capability"
    capability: PortfolioCapability
    account_alias: SafeAccountScope


class AnalysisQueryIntent(_StrictModel):
    """One curated Task 13 portfolio analysis request."""

    intent: Literal["analysis"] = "analysis"
    analysis_kind: PortfolioAnalysisKind
    account_alias: SafeAccountScope
    as_of: UtcDateTime


class PortfolioQueryCatalog(_StrictModel):
    """Value-free inventory of the fixed query vocabulary."""

    schema_version: Literal["1"] = "1"
    metrics: tuple[str, ...]
    analysis_kinds: tuple[str, ...]
    arbitrary_code_allowed: Literal[False] = False
    local_paths_allowed: Literal[False] = False
    network_requests_allowed: Literal[False] = False


type PortfolioQueryIntent = Annotated[
    MetricQueryIntent
    | BreakdownQueryIntent
    | EventQueryIntent
    | CapabilityQueryIntent
    | AnalysisQueryIntent,
    Field(discriminator="intent"),
]

_QUERY_ADAPTER: Final[TypeAdapter[PortfolioQueryIntent]] = TypeAdapter(PortfolioQueryIntent)
_QUERY_CATALOG: Final = PortfolioQueryCatalog(
    metrics=_APPROVED_METRICS,
    analysis_kinds=_APPROVED_ANALYSES,
)


def parse_portfolio_query(payload: Mapping[str, object]) -> PortfolioQueryIntent:
    """Accept only the discriminated typed catalog and never evaluate caller text."""
    try:
        return _QUERY_ADAPTER.validate_python(payload, strict=True)
    except ValidationError as error:
        raise PortfolioQueryError(
            "portfolio query must use an approved typed intent",
        ) from error


def portfolio_query_catalog() -> PortfolioQueryCatalog:
    """Return the immutable approved metric and analysis vocabulary."""
    return _QUERY_CATALOG


def _validate_period(start_at: datetime | None, end_at: datetime | None) -> None:
    if (start_at is None) != (end_at is None):
        raise ValueError("query period requires both start and end")
    if start_at is not None and end_at is not None and end_at < start_at:
        raise ValueError("query period end must not precede its start")


__all__ = (
    "AnalysisQueryIntent",
    "BreakdownQueryIntent",
    "CapabilityQueryIntent",
    "EventQueryIntent",
    "MetricQueryIntent",
    "PortfolioQueryCatalog",
    "PortfolioQueryError",
    "parse_portfolio_query",
    "portfolio_query_catalog",
)
