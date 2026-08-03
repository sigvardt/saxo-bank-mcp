"""Proof-bound execution over authenticated owner-local analytics inputs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from statistics import fmean
from typing import Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.analytics_backtest import (
    BacktestDataset,
    BacktestRequest,
    HistoricalBar,
    run_backtest,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_costs import (
    CostComponents,
    NamedCostDifference,
    SaxoCostIllustration,
)
from saxo_bank_mcp.analytics_derivatives import (
    DerivativeDataset,
    OptionAnalyticsValues,
    SaxoGreekSnapshot,
    analyze_option_model,
)
from saxo_bank_mcp.analytics_fx import FxQuote
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_instruments import (
    PriceSeriesDataset,
    ResearchRefusal,
    ResearchStatus,
    analyze_instrument_prices,
)
from saxo_bank_mcp.analytics_market import (
    BoundedMarketResearch,
    BoundedResearchUniverse,
    analyze_bounded_market,
)
from saxo_bank_mcp.analytics_metrics import covariance, simple_returns
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisCalendar,
    AnalysisParameterBinding,
    AnalysisProvenance,
    AnalysisResult,
    AnalysisWarning,
    DataCoverage,
    DataQuality,
    FxConversionMethod,
    FxSource,
    HandleKind,
    InstrumentAnalysisRequest,
    MarketAnalysisRequest,
    MetricCurrencyBinding,
    MetricValue,
    ModelScalarUnit,
    NamedModelAssumption,
    NamedModelParameter,
    PortfolioAnalysisRequest,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    ValueUnitClass,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_optimization import (
    CovariancePerturbation,
    OptimizationAsset,
    OptimizationDataset,
    OptimizationRequest,
    SolverSettings,
    optimize_portfolio,
)
from saxo_bank_mcp.analytics_options import OptionContract, OptionModelInput
from saxo_bank_mcp.analytics_portfolio import (
    LedgerEntry,
    LedgerKind,
    PortfolioPeriodDataset,
    SaxoPerformanceTotals,
    SaxoSourceBinding,
    analyze_portfolio_truth,
)
from saxo_bank_mcp.analytics_position_sizing import (
    PositionSizingRequest,
    size_position,
)
from saxo_bank_mcp.analytics_pretrade import (
    PrivatePreTradeValues,
    TradeProposal,
    build_pretrade_impact,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    ProfileActivationState,
    ProofProfile,
    ProofRegistry,
    ProofState,
)
from saxo_bank_mcp.analytics_provenance import (
    build_analysis_identity,
    build_analysis_parameters_sha256,
)
from saxo_bank_mcp.analytics_scenarios import (
    CurrencyShock,
    PortfolioScenarioRequest,
    PrivateScenarioValues,
    ScenarioComponent,
    ScenarioShock,
    run_portfolio_scenario,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    AuthenticatedDatasetMaterial,
    AuthenticatedSnapshotMaterial,
    AuthenticatedSourceMaterial,
    StoredDataset,
    StoreNotFoundError,
)
from saxo_bank_mcp.analytics_strategy_schema import (
    StrategyDefinition,
    strategy_definition_fingerprint,
)
from saxo_bank_mcp.analytics_sync import (
    OptionReferenceDatasetRow,
    PriceBarDatasetRow,
    QuoteDatasetRow,
    SyncError,
    get_dataset,
)
from saxo_bank_mcp.analytics_trade_review import (
    DecisionBarReference,
    DecisionPointQuote,
)

_ENGINE_NAME: Final = "saxo_analytics"
_MARKET_COMPARISON_KIND: Final = "market_comparison"
_PRICE_RETURN_METRIC: Final = "price_return"
_CHART_CONTRACT: Final = "chart_v3"
_MINIMUM_PRICE_OBSERVATIONS: Final = 2
_MINIMUM_PORTFOLIO_VALUATIONS: Final = 2
_MINIMUM_OPTIMIZATION_ASSETS: Final = 2
_MINIMUM_OPTIMIZATION_PRICE_OBSERVATIONS: Final = 31
_PROOF_CHECKS: Final = (
    "golden",
    "property",
    "independent_reference",
    "saxo_reconciliation",
    "sim_end_to_end",
)
_PORTFOLIO_CONTEXT_KIND: Final = "portfolio_performance_input"
_SIZING_CONTEXT_KIND: Final = "position_sizing_input"
_SCENARIO_CONTEXT_KIND: Final = "scenario_input"
_OPTIMIZATION_CONTEXT_KIND: Final = "optimization_input"
_DERIVATIVES_CONTEXT_KIND: Final = "derivatives_input"
_BACKTEST_CONTEXT_KIND: Final = "backtest_input"
_PRETRADE_CONTEXT_KIND: Final = "pretrade_input"

_CONTEXT_KIND_BY_ANALYSIS: Final[dict[str, str]] = {
    "portfolio_performance": _PORTFOLIO_CONTEXT_KIND,
    "position_sizing": _SIZING_CONTEXT_KIND,
    "scenario_custom": _SCENARIO_CONTEXT_KIND,
    "portfolio_minimum_variance": _OPTIMIZATION_CONTEXT_KIND,
    "derivatives_model": _DERIVATIVES_CONTEXT_KIND,
    "bounded_backtest": _BACKTEST_CONTEXT_KIND,
    "pretrade_impact": _PRETRADE_CONTEXT_KIND,
}

_REQUEST_FINGERPRINT_CHUNKS: Final = 16


class StoredAnalysisExecutionError(RuntimeError):
    """Value-free refusal raised before an unproved analysis can be persisted."""

    def __init__(self, reason_code: str) -> None:
        """Retain only a stable public reason code."""
        super().__init__("stored analysis execution refused")
        self.reason_code = reason_code


class _StrictExecutionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class InstrumentExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal["instrument_price_return"] = "instrument_price_return"
    instrument_handles: tuple[str, ...] = Field(min_length=1, max_length=25)
    rolling_window: int = Field(ge=2, le=1000)
    periods_per_year: float = Field(gt=0, allow_inf_nan=False)
    requested_return: Literal["price_return", "adjusted_price_return", "total_return"]


class PortfolioExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal["portfolio_performance"] = "portfolio_performance"


class PositionSizingExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal["position_sizing"] = "position_sizing"
    instrument_handle: str
    method: Literal["stop_distance", "volatility"]
    maximum_loss: Decimal = Field(gt=0, allow_inf_nan=False)
    risk_budget_confirmed: bool
    stop_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volatility_multiple: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)


class ScenarioExecutionShock(_StrictExecutionModel):
    instrument_handle: str
    price_shock_ratio: Decimal = Field(ge=-1, allow_inf_nan=False)
    volatility_shock_points: Decimal = Field(allow_inf_nan=False)
    rate_shock_basis_points: Decimal = Field(allow_inf_nan=False)


class ScenarioExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal[
        "margin_fire_drill",
        "portfolio_scenario",
        "scenario_historical",
        "scenario_custom",
        "scenario_currency",
        "scenario_volatility",
        "scenario_rate",
        "scenario_margin",
        "scenario_combined",
    ]
    shocks: tuple[ScenarioExecutionShock, ...] = Field(min_length=1, max_length=100)
    numeric_shocks_echoed_by_caller: bool
    caller_accepted_numeric_shocks: bool


class OptimizationExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal["portfolio_minimum_variance", "portfolio_risk_parity"]
    objective: Literal["minimum_variance", "risk_parity"]
    objective_confirmed_by_caller: bool
    constraints_confirmed_by_caller: bool
    short_policy: Literal["long_only", "bounded_short"]
    maximum_turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_transaction_cost_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_margin_ratio: Decimal = Field(ge=0, allow_inf_nan=False)


class DerivativesExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal["derivatives_model"] = "derivatives_model"
    instrument_handles: tuple[str, ...] = Field(min_length=1, max_length=25)
    volatility_assumption: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    rate_assumption: Decimal | None = Field(default=None, allow_inf_nan=False)


class BacktestExecutionParameters(_StrictExecutionModel):
    analysis_kind: Literal["bounded_backtest"] = "bounded_backtest"
    instrument_handle: str
    strategy: StrategyDefinition
    starting_equity: float = Field(gt=0, allow_inf_nan=False)


class AuthenticatedBacktestExecutionProof(_StrictExecutionModel):
    """Sealed lifecycle binding held only by one installed matrix process session."""

    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    dataset_id: str = Field(pattern=r"^ds_[a-f0-9]{32}$")
    account_alias: str = Field(
        pattern=r"^(?:aa_[a-f0-9]{32}|aggregate|selected SIM account)$",
    )
    instrument_handle: str = Field(pattern=r"^ih_[a-f0-9]{32}$")
    strategy_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fill_model: Literal["next_bar_open"]
    ledger_provenance_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    lifecycle_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment: Literal["SIM"] = "SIM"
    cleanup_state: Literal["proved_equal"] = "proved_equal"


type StoredExecutionParameters = (
    InstrumentExecutionParameters
    | PortfolioExecutionParameters
    | PositionSizingExecutionParameters
    | ScenarioExecutionParameters
    | OptimizationExecutionParameters
    | DerivativesExecutionParameters
    | BacktestExecutionParameters
)


class _StoredContextBase(_StrictExecutionModel):
    schema_version: Literal["1"] = "1"
    supporting_dataset_ids: tuple[str, ...] = Field(default=(), max_length=25)

    @model_validator(mode="after")
    def _validate_supporting_datasets(self) -> Self:
        if len(self.supporting_dataset_ids) != len(set(self.supporting_dataset_ids)):
            raise ValueError("supporting dataset handles must be unique")
        return self


class StoredPortfolioExecutionContext(_StoredContextBase):
    analysis_kind: Literal["portfolio_performance"] = "portfolio_performance"
    dataset: PortfolioPeriodDataset


class StoredPositionSizingExecutionContext(_StoredContextBase):
    analysis_kind: Literal["position_sizing"] = "position_sizing"
    request: PositionSizingRequest


class StoredScenarioExecutionContext(_StoredContextBase):
    analysis_kind: Literal[
        "scenario_historical",
        "scenario_custom",
        "scenario_currency",
        "scenario_volatility",
        "scenario_rate",
        "scenario_margin",
        "scenario_combined",
    ]
    request: PortfolioScenarioRequest


class StoredOptimizationExecutionContext(_StoredContextBase):
    analysis_kind: Literal["portfolio_minimum_variance", "portfolio_risk_parity"]
    request: OptimizationRequest


class StoredDerivativesExecutionContext(_StoredContextBase):
    analysis_kind: Literal["derivatives_model"] = "derivatives_model"
    dataset: DerivativeDataset
    model_input: OptionModelInput
    saxo_greeks: SaxoGreekSnapshot | None = None


class StoredBacktestExecutionContext(_StoredContextBase):
    analysis_kind: Literal["bounded_backtest"] = "bounded_backtest"
    account_alias: str
    instrument_handle: str
    missing_interval_count: int = Field(ge=0)
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]


class StoredPretradeExecutionContext(_StoredContextBase):
    analysis_kind: Literal["pretrade_impact"] = "pretrade_impact"
    origin_analysis_id: str
    dataset_id: str
    account_alias: str
    instrument_handle: str
    as_of: datetime
    reference_price: Decimal = Field(gt=0, allow_inf_nan=False)
    contract_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)
    instrument_currency: str
    reporting_currency: str
    current_position_quantity: Decimal = Field(allow_inf_nan=False)
    current_position_exposure: Decimal = Field(allow_inf_nan=False)
    portfolio_value: Decimal = Field(gt=0, allow_inf_nan=False)
    current_currency_exposure: Decimal = Field(allow_inf_nan=False)
    buying_power_available: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_available: Decimal = Field(ge=0, allow_inf_nan=False)
    buy_cash_required_per_unit: Decimal = Field(ge=0, allow_inf_nan=False)
    sell_cash_required_per_unit: Decimal = Field(ge=0, allow_inf_nan=False)
    margin_required_per_unit: Decimal = Field(ge=0, allow_inf_nan=False)
    unit_cost_estimate: CostComponents
    saxo_illustration: SaxoCostIllustration | None = None
    named_cost_difference: NamedCostDifference | None = None
    decision_quote: DecisionPointQuote | None = None
    decision_bar: DecisionBarReference | None = None
    fx_quotes: tuple[FxQuote, ...] = ()
    cost_holding_period_days: int = Field(ge=0, le=36500)
    source_bindings: tuple[SaxoSourceBinding, ...]
    quality_state: QualityState
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_context(self) -> Self:
        if self.instrument_currency != self.reporting_currency:
            raise ValueError("stored pretrade context requires a proved same-currency basis")
        expected_turnover = self.reference_price * self.contract_multiplier
        if self.unit_cost_estimate.turnover != expected_turnover:
            raise ValueError("unit pretrade turnover must match its stored reference notional")
        if bool(self.missing_fields) != (self.quality_state is QualityState.PARTIAL):
            raise ValueError("partial pretrade context must name missing fields")
        return self


_CONTEXT_MODEL_BY_ANALYSIS: Final[dict[str, type[BaseModel]]] = {
    "portfolio_performance": StoredPortfolioExecutionContext,
    "position_sizing": StoredPositionSizingExecutionContext,
    "scenario_custom": StoredScenarioExecutionContext,
    "portfolio_minimum_variance": StoredOptimizationExecutionContext,
    "derivatives_model": StoredDerivativesExecutionContext,
    "bounded_backtest": StoredBacktestExecutionContext,
    "pretrade_impact": StoredPretradeExecutionContext,
}


@dataclass(frozen=True, slots=True)
class IssuedStoredAnalysisInput:
    """Value-free proof that one exact typed context is already server-owned."""

    dataset_id: str
    analysis_kind: str
    quality_state: QualityState
    coverage_start: datetime
    coverage_end: datetime
    row_count: int
    fingerprint_sha256: str


def issue_stored_analysis_input(
    *,
    analysis_kind: str,
    source_dataset_ids: Sequence[str],
    config: AnalyticsConfig,
    store: AnalyticsStore,
    origin_analysis: AnalysisResult | None = None,
) -> IssuedStoredAnalysisInput:
    """Authenticate and route one existing typed input without accepting source facts.

    Context payloads can only have entered through server-owned capture code. This boundary
    accepts opaque dataset handles, authenticates every bound source page and snapshot, and
    refuses a widened or mismatched source set.
    """
    context_kind = _CONTEXT_KIND_BY_ANALYSIS.get(analysis_kind)
    model_type = _CONTEXT_MODEL_BY_ANALYSIS.get(analysis_kind)
    if context_kind is None or model_type is None:
        raise StoredAnalysisExecutionError("analysis_input_kind_unsupported")
    dataset_ids = tuple(source_dataset_ids)
    if not dataset_ids or len(dataset_ids) != len(set(dataset_ids)):
        raise StoredAnalysisExecutionError("analysis_input_source_scope_invalid")
    try:
        primary = store.get_authenticated_dataset_material(dataset_ids[0])
        snapshot = store.get_authenticated_snapshot_material(dataset_ids[0], context_kind)
    except StoreNotFoundError:
        primary, snapshot = _derive_stored_execution_context(
            analysis_kind=analysis_kind,
            source_dataset_ids=dataset_ids,
            config=config,
            store=store,
            origin_analysis=origin_analysis,
        )
        dataset_ids = (primary.dataset.dataset_id,)
    context = _parse_context(snapshot, model_type)
    supporting = tuple(getattr(context, "supporting_dataset_ids", ()))
    if dataset_ids != (dataset_ids[0], *supporting):
        raise StoredAnalysisExecutionError("analysis_input_source_scope_mismatch")
    materials = (primary, *(store.get_authenticated_dataset_material(item) for item in supporting))
    if any(material.account_scope != primary.account_scope for material in materials):
        raise StoredAnalysisExecutionError("analysis_input_account_scope_mismatch")
    fingerprint_sha256 = hashlib.sha256(
        json.dumps(
            {
                "analysis_kind": analysis_kind,
                "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
                "dataset_fingerprints": [
                    material.dataset.fingerprint_sha256 for material in materials
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    return IssuedStoredAnalysisInput(
        dataset_id=primary.dataset.dataset_id,
        analysis_kind=analysis_kind,
        quality_state=primary.dataset.quality_state,
        coverage_start=primary.coverage_start,
        coverage_end=primary.coverage_end,
        row_count=primary.dataset.row_count,
        fingerprint_sha256=fingerprint_sha256,
    )


def _derive_stored_execution_context(
    *,
    analysis_kind: str,
    source_dataset_ids: tuple[str, ...],
    config: AnalyticsConfig,
    store: AnalyticsStore,
    origin_analysis: AnalysisResult | None,
) -> tuple[AuthenticatedDatasetMaterial, AuthenticatedSnapshotMaterial]:
    """Create one server-owned typed context from authenticated Saxo pages only."""
    materials = tuple(
        store.get_authenticated_dataset_material(dataset_id)
        for dataset_id in sorted(source_dataset_ids)
    )
    account_scopes = {
        material.account_scope for material in materials if material.account_scope != "aggregate"
    }
    if len(account_scopes) > 1:
        raise StoredAnalysisExecutionError("analysis_input_account_scope_mismatch")
    account_scope = next(iter(account_scopes), "aggregate")
    if any(material.dataset.quality_state is not QualityState.COMPLETE for material in materials):
        raise StoredAnalysisExecutionError("verified_coverage_unavailable")
    builders = {
        "portfolio_performance": _build_portfolio_context,
        "position_sizing": _build_position_sizing_context,
        "scenario_custom": _build_scenario_context,
        "portfolio_minimum_variance": _build_optimization_context,
        "derivatives_model": _build_derivatives_context,
        "bounded_backtest": _build_backtest_context,
        "pretrade_impact": _build_pretrade_context,
    }
    builder = builders.get(analysis_kind)
    if builder is None:
        _ = config
        raise StoredAnalysisExecutionError("analysis_source_context_unavailable")
    required_contracts = {
        "portfolio_performance": frozenset(
            {
                "performance_summary_v4",
                "performance_timeseries_v4",
                "transactions_v1",
                "bookings_v1",
                "balances_v1",
            },
        ),
        "position_sizing": frozenset({"balances_v1", "positions_v1", "costs_v1"}),
        "scenario_custom": frozenset(
            {"balances_v1", "positions_v1", "exposure_instruments_v1"},
        ),
        "portfolio_minimum_variance": frozenset(
            {
                "chart_v3",
                "positions_v1",
                "exposure_instruments_v1",
                "balances_v1",
                "costs_v1",
            },
        ),
        "derivatives_model": frozenset(
            {"options_chain_reference_v1", "info_price_v1", "balances_v1"},
        ),
        "bounded_backtest": frozenset({"chart_v3", "reference_instruments_v1"}),
        "pretrade_impact": frozenset(
            {"balances_v1", "positions_v1", "costs_v1", "info_price_v1"},
        ),
    }[analysis_kind]
    external_pages: tuple[AuthenticatedSourceMaterial, ...] = ()
    if analysis_kind == "bounded_backtest":
        chart_handles = {
            row.instrument_handle
            for source_dataset_id in source_dataset_ids
            for row in _dataset_rows_or_empty(source_dataset_id, config=config)
            if isinstance(row, PriceBarDatasetRow)
        }
        if len(chart_handles) != 1:
            raise StoredAnalysisExecutionError("backtest_chart_scope_ambiguous")
        reference_pages = store.find_authenticated_source_materials(
            contract_name="reference_instruments_v1",
            instrument_handle=next(iter(chart_handles)),
        )
        if reference_pages:
            external_pages = (max(reference_pages, key=lambda page: page.source_timestamp),)
    pages = (
        *(page for material in materials for page in material.pages),
        *external_pages,
    )
    contract_ids = {page.contract_name for page in pages}
    if not required_contracts <= contract_ids:
        raise StoredAnalysisExecutionError("analysis_source_contracts_incomplete")
    current = max(
        materials,
        key=lambda material: (material.dataset.created_at, material.dataset.dataset_id),
    )
    current_page_ids = tuple(page.page_id for page in current.pages)
    lineage_page_ids = tuple(
        page.page_id for material in materials if material is not current for page in material.pages
    ) + tuple(page.page_id for page in external_pages)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    snapshot_id = new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID)
    created_at = max(
        tuple(material.dataset.created_at for material in materials)
        + tuple(page.source_timestamp for page in external_pages),
    )
    coverage_start = min(material.coverage_start for material in materials)
    coverage_end = max(material.coverage_end for material in materials)
    context = builder(
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        account_alias=account_scope,
        as_of=created_at,
        pages=pages,
        bindings=_canonical_source_bindings_from_pages(pages),
        config=config,
        source_dataset_ids=source_dataset_ids,
        origin_analysis=origin_analysis,
    )
    context_kind = _CONTEXT_KIND_BY_ANALYSIS[analysis_kind]
    aggregate_lineage_contract_ids = {
        "portfolio_minimum_variance": ("chart_v3",),
        "derivatives_model": ("info_price_v1", "options_chain_reference_v1"),
        "pretrade_impact": ("info_price_v1",),
    }.get(analysis_kind, ())
    try:
        with store.transaction():
            store.create_dataset(
                dataset_id=dataset_id,
                account_scope=account_scope,
                source_scope="saxo_openapi",
                source_revision=current.dataset.source_revision,
                source_page_ids=current_page_ids,
                lineage_source_page_ids=lineage_page_ids,
                aggregate_lineage_contract_ids=aggregate_lineage_contract_ids,
                created_at=created_at,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                quality_state=QualityState.COMPLETE,
            )
            store.create_snapshot(
                snapshot_id=snapshot_id,
                dataset_id=dataset_id,
                snapshot_kind=context_kind,
                account_scope=account_scope,
                source_revision=current.dataset.source_revision,
                as_of=created_at,
                payload=context.model_dump(mode="json"),
            )
    except StoredAnalysisExecutionError:
        raise
    except (ArithmeticError, ValueError) as error:
        raise StoredAnalysisExecutionError("analysis_source_context_invalid") from error
    return (
        store.get_authenticated_dataset_material(dataset_id),
        store.get_authenticated_snapshot_material(dataset_id, context_kind),
    )


def _build_portfolio_context(  # noqa: PLR0913
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredPortfolioExecutionContext:
    _ = config, source_dataset_ids, origin_analysis
    summary = _single_source_row(pages, "performance_summary_v4")
    balance = _single_source_row(pages, "balances_v1")
    series = tuple(
        sorted(
            _source_rows(pages, "performance_timeseries_v4"),
            key=lambda row: _utc_value(row, "Date"),
        ),
    )
    if len(series) < _MINIMUM_PORTFOLIO_VALUATIONS:
        raise StoredAnalysisExecutionError("portfolio_boundary_valuations_unavailable")
    if _source_rows(pages, "transactions_v1") or _source_rows(pages, "bookings_v1"):
        raise StoredAnalysisExecutionError("portfolio_flow_boundary_valuations_unavailable")
    start_at = _utc_value(series[0], "Date")
    end_at = _utc_value(series[-1], "Date")
    opening = _decimal_value(series[0], "EndOfDayBalance", positive=True)
    closing = _decimal_value(series[-1], "EndOfDayBalance", nonnegative=True)
    summary_closing = _decimal_value(summary, "AccountValue", nonnegative=True)
    if closing != summary_closing or end_at > as_of:
        raise StoredAnalysisExecutionError("portfolio_performance_reconciliation_unavailable")
    profit_loss = closing - opening
    reported_return = summary.get("TimeWeightedReturn")
    if reported_return is not None:
        calculated_return = profit_loss / opening * Decimal(100)
        if abs(_decimal_value(summary, "TimeWeightedReturn") - calculated_return) > Decimal(
            "0.00000001"
        ):
            raise StoredAnalysisExecutionError("portfolio_performance_reconciliation_unavailable")
    return StoredPortfolioExecutionContext(
        dataset=PortfolioPeriodDataset(
            dataset_id=dataset_id,
            snapshot_id=snapshot_id,
            account_alias=account_alias,
            start_at=start_at,
            end_at=end_at,
            reporting_currency=_text_value(balance, "Currency"),
            opening_value=opening,
            closing_value=closing,
            ledger_entries=(
                LedgerEntry(
                    account_alias=account_alias,
                    event_key_sha256=_sha256_json(
                        {
                            "analysis_kind": "portfolio_performance",
                            "source_pages": sorted(page.page_id for page in pages),
                        },
                    ),
                    revision=1,
                    occurred_at=end_at,
                    kind=LedgerKind.TRADING_PNL,
                    amount=profit_loss,
                    currency=_text_value(balance, "Currency"),
                ),
            ),
            source_bindings=bindings,
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            warnings=(),
            benchmark=None,
            saxo_totals=SaxoPerformanceTotals(
                closing_value=summary_closing,
                total_profit_loss=profit_loss,
                currency=_text_value(balance, "Currency"),
            ),
            named_differences=(),
        ),
    )


def _build_scenario_context(  # noqa: PLR0913
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredScenarioExecutionContext:
    _ = config, source_dataset_ids, origin_analysis
    balance = _single_source_row(pages, "balances_v1")
    reporting_currency = _text_value(balance, "Currency")
    exposures = {
        (_text_value(row, "AssetType"), _integer_value(row, "Uic")): row
        for row in _source_rows(pages, "exposure_instruments_v1")
    }
    components: list[ScenarioComponent] = []
    shocks: list[ScenarioShock] = []
    for position in _source_rows(pages, "positions_v1"):
        base = _mapping_value(position, "PositionBase")
        view = _mapping_value(position, "PositionView")
        asset_type = _text_value(base, "AssetType")
        uic = _integer_value(base, "Uic")
        exposure_row = exposures.get((asset_type, uic))
        if exposure_row is None or exposure_row.get("Currency") not in {
            None,
            reporting_currency,
        }:
            raise StoredAnalysisExecutionError("scenario_exposure_scope_incomplete")
        handle = instrument_handle_for_saxo_identity(asset_type, uic)
        current_value = _decimal_value(view, "Exposure")
        if current_value == 0:
            raise StoredAnalysisExecutionError("scenario_zero_exposure_unavailable")
        components.append(
            ScenarioComponent(
                account_alias=account_alias,
                instrument_handle=handle,
                branch_id="linear",
                current_value=current_value,
                currency=reporting_currency,
                current_margin_requirement=Decimal(0),
                model_analysis_id=None,
            ),
        )
        shocks.append(
            ScenarioShock(
                instrument_handle=handle,
                price_shock_ratio=Decimal(0),
                volatility_shock_points=Decimal(0),
                rate_shock_basis_points=Decimal(0),
                cash_flow_shock=Decimal(0),
                repriced_value_at_base_fx=None,
                stressed_margin_requirement=Decimal(0),
            ),
        )
    if not components:
        raise StoredAnalysisExecutionError("scenario_exposure_scope_incomplete")
    return StoredScenarioExecutionContext(
        analysis_kind="scenario_custom",
        request=PortfolioScenarioRequest(
            dataset_id=dataset_id,
            snapshot_id=snapshot_id,
            account_alias=account_alias,
            as_of=as_of,
            reporting_currency=reporting_currency,
            scenario_type="equity",
            input_mode="numeric",
            narrative_fingerprint_sha256=None,
            numeric_shocks_echoed_by_caller=False,
            caller_accepted_numeric_shocks=False,
            echoed_shock_map_sha256=None,
            accepted_shock_map_sha256=None,
            historical_start_at=None,
            historical_end_at=None,
            components=tuple(components),
            component_shocks=tuple(shocks),
            currency_shocks=(CurrencyShock(currency=reporting_currency, shock_ratio=Decimal(0)),),
            current_margin_headroom=_decimal_value(
                balance,
                "MarginAvailableForTrading",
                nonnegative=True,
            ),
            source_bindings=bindings,
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            warnings=(),
        ),
    )


def _build_optimization_context(  # noqa: C901, PLR0913
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredOptimizationExecutionContext:
    _ = origin_analysis
    balance = _single_source_row(pages, "balances_v1")
    reporting_currency = _text_value(balance, "Currency")
    positions: dict[str, Decimal] = {}
    asset_types: dict[str, str] = {}
    for position in _source_rows(pages, "positions_v1"):
        base = _mapping_value(position, "PositionBase")
        view = _mapping_value(position, "PositionView")
        asset_type = _text_value(base, "AssetType")
        handle = instrument_handle_for_saxo_identity(asset_type, _integer_value(base, "Uic"))
        exposure = _decimal_value(view, "Exposure", positive=True)
        positions[handle] = exposure
        asset_types[handle] = asset_type
    if len(positions) < _MINIMUM_OPTIMIZATION_ASSETS:
        raise StoredAnalysisExecutionError("optimization_minimum_asset_count_unavailable")
    price_rows: dict[str, dict[datetime, float]] = {}
    for source_dataset_id in source_dataset_ids:
        for row in _dataset_rows_or_empty(source_dataset_id, config=config):
            if isinstance(row, PriceBarDatasetRow) and row.instrument_handle in positions:
                price_rows.setdefault(row.instrument_handle, {})[row.bar_time] = row.close_value
    if set(price_rows) != set(positions):
        raise StoredAnalysisExecutionError("optimization_price_history_incomplete")
    time_sets: list[set[datetime]] = [set(rows) for rows in price_rows.values()]
    common_times: list[datetime] = sorted(
        time_sets[0].intersection(*time_sets[1:]),
    )
    if len(common_times) < _MINIMUM_OPTIMIZATION_PRICE_OBSERVATIONS:
        raise StoredAnalysisExecutionError("optimization_minimum_sample_count_unavailable")
    handles = tuple(sorted(positions))
    returns = {
        handle: simple_returns([price_rows[handle][at] for at in common_times])
        for handle in handles
    }
    covariance_matrix = tuple(
        tuple(Decimal(str(covariance(returns[left], returns[right]))) for right in handles)
        for left in handles
    )
    gross = sum(positions.values(), Decimal(0))
    cost_by_handle: dict[str, Decimal] = {}
    for page in pages:
        if page.contract_name != "costs_v1" or page.instrument_handle is None:
            continue
        rows = _source_rows((page,), "costs_v1")
        if len(rows) != 1:
            raise StoredAnalysisExecutionError("optimization_cost_scope_incomplete")
        cost_by_handle[page.instrument_handle] = _decimal_value(
            _mapping_value(rows[0], "Cost"),
            "TotalCost",
            nonnegative=True,
        )
    if set(cost_by_handle) != set(handles):
        raise StoredAnalysisExecutionError("optimization_cost_scope_incomplete")
    assets = tuple(
        OptimizationAsset(
            account_alias=account_alias,
            instrument_handle=handle,
            asset_class="equity" if asset_types[handle] == "Stock" else "other",
            currency=reporting_currency,
            current_weight=positions[handle] / gross,
            expected_return=Decimal(str(fmean(returns[handle]))),
            lower_bound=Decimal(0),
            upper_bound=Decimal(1),
            transaction_cost_rate=cost_by_handle[handle] / positions[handle],
            margin_requirement_rate=Decimal(1),
            minimum_trade_weight=Decimal(0),
            excluded=False,
        )
        for handle in handles
    )
    dataset = OptimizationDataset(
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        account_alias=account_alias,
        estimation_start_at=common_times[0],
        estimation_end_at=common_times[-1],
        as_of=as_of,
        reporting_currency=reporting_currency,
        assets=assets,
        covariance_matrix=covariance_matrix,
        sample_count=len(common_times) - 1,
        return_model="historical_arithmetic",
        covariance_model="sample_covariance",
        source_bindings=bindings,
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )
    return StoredOptimizationExecutionContext(
        analysis_kind="portfolio_minimum_variance",
        request=OptimizationRequest(
            dataset=dataset,
            objective="minimum_variance",
            objective_confirmed_by_caller=False,
            constraints_confirmed_by_caller=False,
            short_policy="long_only",
            asset_class_constraints=(),
            currency_constraints=(),
            maximum_turnover=Decimal(2),
            maximum_transaction_cost_ratio=Decimal(2),
            maximum_margin_ratio=Decimal(1),
            perturbations=(
                CovariancePerturbation(
                    perturbation_id="observed_covariance",
                    covariance_matrix=covariance_matrix,
                ),
            ),
            solver_settings=SolverSettings(
                method="SLSQP",
                maximum_iterations=1000,
                objective_tolerance=Decimal("0.00000001"),
                feasibility_tolerance=Decimal("0.00000001"),
                kkt_tolerance=Decimal("0.00001"),
            ),
            lexicographic_tie_break_rule="asset_order_within_objective_tolerance",
            concentration_warning_threshold=Decimal("0.95"),
            condition_number_warning_threshold=Decimal(1000000),
            stability_warning_threshold=Decimal("0.05"),
            stability_refusal_threshold=Decimal("0.25"),
        ),
    )


def _build_derivatives_context(  # noqa: PLR0913
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredDerivativesExecutionContext:
    _ = snapshot_id, origin_analysis
    balance = _single_source_row(pages, "balances_v1")
    options: list[OptionReferenceDatasetRow] = []
    quotes: list[QuoteDatasetRow] = []
    for source_dataset_id in source_dataset_ids:
        rows = _dataset_rows_or_empty(source_dataset_id, config=config)
        options.extend(row for row in rows if isinstance(row, OptionReferenceDatasetRow))
        quotes.extend(row for row in rows if isinstance(row, QuoteDatasetRow))
    if len(options) != 1 or len(quotes) != 1:
        raise StoredAnalysisExecutionError("derivatives_source_scope_ambiguous")
    option = options[0]
    quote = quotes[0]
    if option.underlying_handle != quote.instrument_handle or quote.freshness != "fresh":
        raise StoredAnalysisExecutionError("derivatives_quote_scope_mismatch")
    reference_price = quote.mid_value
    if reference_price is None and quote.bid_value is not None and quote.ask_value is not None:
        reference_price = (quote.bid_value + quote.ask_value) / 2
    if reference_price is None or reference_price <= 0 or option.strike_value <= 0:
        raise StoredAnalysisExecutionError("derivatives_reference_price_unavailable")
    expiry_at = datetime.combine(option.expiry, datetime.min.time(), tzinfo=UTC)
    seconds = (expiry_at - as_of).total_seconds()
    if seconds <= 0:
        raise StoredAnalysisExecutionError("derivatives_expiry_unavailable")
    currency = option.currency or _text_value(balance, "Currency")
    contract = OptionContract(
        dataset_id=dataset_id,
        option_handle=option.instrument_handle,
        underlying_handle=option.underlying_handle,
        contract_currency=currency,
        pricing_model="black_scholes",
        reference_kind="spot",
        option_type=option.put_call,
        exercise_style="european",
        payoff_style="vanilla",
        rate_model="constant",
        reference_price=reference_price,
        strike=option.strike_value,
        time_to_expiry_years=seconds / (365 * 24 * 60 * 60),
        risk_free_rate=0,
        dividend_yield=0,
        days_per_year=365,
    )
    return StoredDerivativesExecutionContext(
        dataset=DerivativeDataset(
            dataset_id=dataset_id,
            account_alias=account_alias,
            as_of=as_of,
            instrument_handles=(option.instrument_handle, option.underlying_handle),
            source_bindings=bindings,
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            warnings=(),
        ),
        model_input=OptionModelInput(contract=contract, volatility=0),
        saxo_greeks=None,
    )


def _build_backtest_context(  # noqa: PLR0913
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredBacktestExecutionContext:
    _ = dataset_id, snapshot_id, as_of, bindings, origin_analysis
    chart_handles = {
        row.instrument_handle
        for source_dataset_id in source_dataset_ids
        for row in _dataset_rows_or_empty(source_dataset_id, config=config)
        if isinstance(row, PriceBarDatasetRow)
    }
    if len(chart_handles) != 1:
        raise StoredAnalysisExecutionError("backtest_chart_scope_ambiguous")
    reference_handles = {
        page.instrument_handle
        for page in pages
        if page.contract_name == "reference_instruments_v1" and page.instrument_handle is not None
    }
    if reference_handles and reference_handles != chart_handles:
        raise StoredAnalysisExecutionError("backtest_reference_scope_mismatch")
    return StoredBacktestExecutionContext(
        account_alias=account_alias,
        instrument_handle=next(iter(chart_handles)),
        missing_interval_count=0,
        missing_fields=(),
        warnings=(),
    )


def _build_pretrade_context(  # noqa: C901, PLR0912, PLR0913, PLR0915
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredPretradeExecutionContext:
    _ = snapshot_id, source_dataset_ids
    if origin_analysis is None or not isinstance(
        origin_analysis.request,
        InstrumentAnalysisRequest,
    ):
        raise StoredAnalysisExecutionError("pretrade_origin_analysis_required")
    if origin_analysis.account_scope not in {account_alias, "aggregate"}:
        raise StoredAnalysisExecutionError("pretrade_origin_account_scope_mismatch")
    if len(origin_analysis.request.instrument_handles) != 1:
        raise StoredAnalysisExecutionError("pretrade_origin_scope_ambiguous")
    instrument_handle = origin_analysis.request.instrument_handles[0]
    info_pages = tuple(
        page
        for page in pages
        if page.contract_name == "info_price_v1" and page.instrument_handle == instrument_handle
    )
    raw_quote_rows = tuple(
        row for page in info_pages for row in _source_rows((page,), "info_price_v1")
    )
    if len(raw_quote_rows) != 1:
        raise StoredAnalysisExecutionError("pretrade_quote_unavailable")
    if origin_analysis.account_scope == "aggregate":
        origin_store = AnalyticsStore.open(config)
        try:
            origin_material = origin_store.get_authenticated_dataset_material(
                origin_analysis.provenance.dataset_id,
            )
        finally:
            origin_store.close()
        if origin_material.account_scope != "aggregate" or instrument_handle not in {
            page.instrument_handle for page in origin_material.pages
        }:
            raise StoredAnalysisExecutionError("pretrade_origin_lineage_mismatch")
    raw_quote = _mapping_value(raw_quote_rows[0], "Quote")
    price_type = _text_value(raw_quote, "PriceType")
    if price_type.casefold() in {"noaccess", "no_access"}:
        raise StoredAnalysisExecutionError("pretrade_quote_unavailable")
    bid_value = _decimal_value(raw_quote, "Bid", positive=True)
    ask_value = _decimal_value(raw_quote, "Ask", positive=True)
    if ask_value < bid_value:
        raise StoredAnalysisExecutionError("pretrade_quote_unavailable")
    balance = _single_source_row(pages, "balances_v1")
    reporting_currency = _text_value(balance, "Currency")
    cost_pages = tuple(
        page
        for page in pages
        if page.contract_name == "costs_v1" and page.instrument_handle == instrument_handle
    )
    if len(cost_pages) != 1:
        raise StoredAnalysisExecutionError("pretrade_cost_scope_ambiguous")
    cost_row = _single_source_row(cost_pages, "costs_v1")
    if cost_row.get("Currency") not in {None, reporting_currency}:
        raise StoredAnalysisExecutionError("pretrade_currency_mismatch")
    raw_cost = _mapping_value(cost_row, "Cost")
    commission = _optional_decimal_value(
        raw_cost,
        "Commission",
        default=Decimal(0),
        nonnegative=True,
    )
    stamp = _optional_decimal_value(
        raw_cost,
        "StampDuty",
        default=Decimal(0),
        nonnegative=True,
    )
    total_cost = _decimal_value(raw_cost, "TotalCost", nonnegative=True)
    residual = total_cost - commission - stamp
    if residual < 0:
        raise StoredAnalysisExecutionError("pretrade_cost_reconciliation_unavailable")
    reference_price = (bid_value + ask_value) / Decimal(2)
    current_quantity = Decimal(0)
    current_exposure = Decimal(0)
    for position in _source_rows(pages, "positions_v1"):
        base = _mapping_value(position, "PositionBase")
        asset_type = _text_value(base, "AssetType")
        handle = instrument_handle_for_saxo_identity(asset_type, _integer_value(base, "Uic"))
        if handle != instrument_handle:
            continue
        current_quantity = _optional_decimal_value(
            base,
            "Amount",
            default=Decimal(0),
        )
        view = position.get("PositionView")
        if isinstance(view, Mapping):
            current_exposure = _optional_decimal_value(
                cast("Mapping[str, object]", view),
                "Exposure",
                default=Decimal(0),
            )
    info_binding = next(binding for binding in bindings if binding.contract_id == "info_price_v1")
    cost_components = CostComponents(
        commission=commission,
        spread=residual,
        fx_conversion=Decimal(0),
        financing=Decimal(0),
        borrow=Decimal(0),
        custody=Decimal(0),
        tax=stamp,
        turnover=reference_price,
        total_cost=total_cost,
    )
    return StoredPretradeExecutionContext(
        origin_analysis_id=origin_analysis.analysis_id,
        dataset_id=dataset_id,
        account_alias=account_alias,
        instrument_handle=instrument_handle,
        as_of=as_of,
        reference_price=reference_price,
        contract_multiplier=Decimal(1),
        instrument_currency=reporting_currency,
        reporting_currency=reporting_currency,
        current_position_quantity=current_quantity,
        current_position_exposure=current_exposure,
        portfolio_value=_decimal_value(balance, "TotalValue", positive=True),
        current_currency_exposure=current_exposure,
        buying_power_available=_decimal_value(
            balance,
            "CashAvailableForTrading",
            nonnegative=True,
        ),
        margin_available=_decimal_value(
            balance,
            "MarginAvailableForTrading",
            nonnegative=True,
        ),
        buy_cash_required_per_unit=reference_price,
        sell_cash_required_per_unit=Decimal(0),
        margin_required_per_unit=reference_price,
        unit_cost_estimate=cost_components,
        saxo_illustration=SaxoCostIllustration(
            commission=commission,
            stamp_duty=stamp,
            total_cost=total_cost,
            currency=reporting_currency,
        ),
        named_cost_difference=None,
        decision_quote=DecisionPointQuote(
            dataset_id=dataset_id,
            instrument_handle=instrument_handle,
            captured_at=info_pages[0].source_timestamp,
            bid=bid_value,
            ask=ask_value,
            price_type=price_type,
            delayed_by_minutes=(
                None
                if raw_quote.get("DelayedByMinutes") is None
                else _integer_value(raw_quote, "DelayedByMinutes")
            ),
            quality_state=QualityState.COMPLETE,
            entitlement_state="available",
            source_binding=info_binding,
            captured_by_mcp=True,
            warnings=(),
        ),
        decision_bar=None,
        fx_quotes=(),
        cost_holding_period_days=(
            0
            if cost_row.get("HoldingPeriodInDays") is None
            else _integer_value(cost_row, "HoldingPeriodInDays")
        ),
        source_bindings=bindings,
        quality_state=QualityState.COMPLETE,
        missing_fields=(),
        warnings=(),
    )


def _build_position_sizing_context(  # noqa: PLR0913
    *,
    dataset_id: str,
    snapshot_id: str,
    account_alias: str,
    as_of: datetime,
    pages: tuple[AuthenticatedSourceMaterial, ...],
    bindings: tuple[SaxoSourceBinding, ...],
    config: AnalyticsConfig,
    source_dataset_ids: tuple[str, ...],
    origin_analysis: AnalysisResult | None,
) -> StoredPositionSizingExecutionContext:
    _ = snapshot_id, as_of, config, source_dataset_ids, origin_analysis
    balance = _single_source_row(pages, "balances_v1")
    positions_by_handle: dict[str, Mapping[str, object]] = {}
    for position in _source_rows(pages, "positions_v1"):
        position_base = _mapping_value(position, "PositionBase")
        position_handle = instrument_handle_for_saxo_identity(
            _text_value(position_base, "AssetType"),
            _integer_value(position_base, "Uic"),
        )
        positions_by_handle[position_handle] = position
    costs_by_handle: dict[str, Mapping[str, object]] = {}
    for page in pages:
        if page.contract_name != "costs_v1" or page.instrument_handle is None:
            continue
        rows = _source_rows((page,), "costs_v1")
        if len(rows) != 1:
            raise StoredAnalysisExecutionError("position_sizing_source_scope_ambiguous")
        costs_by_handle[page.instrument_handle] = rows[0]
    selected_handles = set(positions_by_handle) & set(costs_by_handle)
    if len(selected_handles) != 1:
        raise StoredAnalysisExecutionError("position_sizing_source_scope_ambiguous")
    selected_handle = next(iter(selected_handles))
    selected_position = positions_by_handle[selected_handle]
    selected_cost = costs_by_handle[selected_handle]
    base = _mapping_value(selected_position, "PositionBase")
    view = _mapping_value(selected_position, "PositionView")
    asset_type = _text_value(base, "AssetType")
    uic = _integer_value(base, "Uic")
    if asset_type != "Stock":
        raise StoredAnalysisExecutionError("position_sizing_asset_constraints_unavailable")
    instrument_handle = instrument_handle_for_saxo_identity(asset_type, uic)
    if selected_handle != instrument_handle:
        raise StoredAnalysisExecutionError("position_sizing_instrument_scope_mismatch")
    cost = _mapping_value(selected_cost, "Cost")
    reporting_currency = _text_value(balance, "Currency")
    if selected_cost.get("Currency") not in {None, reporting_currency}:
        raise StoredAnalysisExecutionError("position_sizing_currency_mismatch")
    return StoredPositionSizingExecutionContext(
        request=PositionSizingRequest(
            dataset_id=dataset_id,
            account_alias=account_alias,
            instrument_handle=instrument_handle,
            reporting_currency=reporting_currency,
            method="stop_distance",
            maximum_loss=None,
            risk_budget_confirmed=False,
            entry_price=_decimal_value(view, "CurrentPrice", positive=True),
            stop_price=None,
            volatility_measure=None,
            volatility_multiplier=None,
            value_per_price_unit=Decimal(1),
            lot_size=Decimal(1),
            portfolio_value=_decimal_value(balance, "TotalValue", positive=True),
            maximum_weight=Decimal(1),
            buying_power=_decimal_value(
                balance,
                "CashAvailableForTrading",
                nonnegative=True,
            ),
            reserved_buffer=_optional_decimal_value(
                balance,
                "CashBlocked",
                default=Decimal(0),
                nonnegative=True,
            ),
            estimated_transaction_cost=_decimal_value(
                cost,
                "TotalCost",
                nonnegative=True,
            ),
            margin_headroom=_decimal_value(
                balance,
                "MarginAvailableForTrading",
                nonnegative=True,
            ),
            margin_requirement_per_money_unit=Decimal(1),
            source_bindings=bindings,
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            warnings=(),
        ),
    )


def _source_rows(
    pages: Sequence[AuthenticatedSourceMaterial],
    contract_id: str,
) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    for page in pages:
        if page.contract_name != contract_id:
            continue
        raw_rows = page.payload.get("rows")
        if not isinstance(raw_rows, list) or any(not isinstance(row, Mapping) for row in raw_rows):
            raise StoredAnalysisExecutionError("analysis_source_rows_invalid")
        rows.extend(cast("list[Mapping[str, object]]", raw_rows))
    return tuple(rows)


def _dataset_rows_or_empty(
    dataset_id: str,
    *,
    config: AnalyticsConfig,
) -> tuple[PriceBarDatasetRow | QuoteDatasetRow | OptionReferenceDatasetRow, ...]:
    try:
        return get_dataset(
            dataset_id,
            1,
            config.limits.response_rows,
            config=config,
        ).rows
    except SyncError:
        return ()


def _single_source_row(
    pages: Sequence[AuthenticatedSourceMaterial],
    contract_id: str,
) -> Mapping[str, object]:
    rows = _source_rows(pages, contract_id)
    if len(rows) != 1:
        raise StoredAnalysisExecutionError("analysis_source_scope_ambiguous")
    return rows[0]


def _mapping_value(row: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = row.get(name)
    if not isinstance(value, Mapping):
        raise StoredAnalysisExecutionError("analysis_source_field_missing")
    return cast("Mapping[str, object]", value)


def _text_value(row: Mapping[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise StoredAnalysisExecutionError("analysis_source_field_missing")
    return value


def _utc_value(row: Mapping[str, object], name: str) -> datetime:
    value = row.get(name)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise StoredAnalysisExecutionError("analysis_source_field_invalid") from error
    else:
        raise StoredAnalysisExecutionError("analysis_source_field_missing")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.utcoffset() != datetime.now(UTC).utcoffset():
        raise StoredAnalysisExecutionError("analysis_source_field_invalid")
    return parsed.astimezone(UTC)


def _integer_value(row: Mapping[str, object], name: str) -> int:
    value = row.get(name)
    if type(value) is not int:
        raise StoredAnalysisExecutionError("analysis_source_field_missing")
    return value


def _decimal_value(
    row: Mapping[str, object],
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise StoredAnalysisExecutionError("analysis_source_field_missing")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or (positive and parsed <= 0) or (nonnegative and parsed < 0):
        raise StoredAnalysisExecutionError("analysis_source_field_invalid")
    return parsed


def _optional_decimal_value(
    row: Mapping[str, object],
    name: str,
    *,
    default: Decimal,
    nonnegative: bool = False,
) -> Decimal:
    return default if row.get(name) is None else _decimal_value(row, name, nonnegative=nonnegative)


def execute_market_comparison(  # noqa: PLR0913
    *,
    tool_name: str,
    dataset_ids: tuple[str, ...],
    periods_per_year: float,
    visibility: VisibilityMode,
    config: AnalyticsConfig,
    store: AnalyticsStore,
    registry: ProofRegistry,
) -> AnalysisResult:
    """Run and persist one exact profile-bound price-return comparison."""
    if len(dataset_ids) != 1:
        raise StoredAnalysisExecutionError("multi_dataset_proof_binding_unavailable")
    if visibility is not VisibilityMode.PRIVATE_USER_RESULT:
        raise StoredAnalysisExecutionError("private_result_required")
    profile = _active_profile(registry, _MARKET_COMPARISON_KIND)
    if _PRICE_RETURN_METRIC not in {binding.metric_id for binding in profile.metric_definitions}:
        raise StoredAnalysisExecutionError("proof_metric_executor_unavailable")
    dataset = store.get_authenticated_dataset(dataset_ids[0])
    series = _stored_price_series(dataset, config=config)
    domain_result = analyze_bounded_market(
        BoundedResearchUniverse(scope="explicit", series=(series,)),
        periods_per_year=periods_per_year,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    result = _market_result(
        tool_name=tool_name,
        dataset=dataset,
        series=series,
        domain_result=domain_result,
        periods_per_year=periods_per_year,
        visibility=visibility,
        profile=profile,
        registry=registry,
    )
    store.put_analysis(result)
    return result


def execute_stored_analysis(  # noqa: PLR0911, PLR0913
    *,
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: StoredExecutionParameters,
    config: AnalyticsConfig,
    store: AnalyticsStore,
    registry: ProofRegistry,
    backtest_proof: AuthenticatedBacktestExecutionProof | None = None,
) -> AnalysisResult:
    """Dispatch one typed stored-input request to its existing domain engine."""
    if visibility is not VisibilityMode.PRIVATE_USER_RESULT:
        raise StoredAnalysisExecutionError("private_result_required")
    profile = _active_profile(registry, parameters.analysis_kind)
    if isinstance(parameters, InstrumentExecutionParameters):
        return _execute_instrument(
            tool_name,
            dataset_id,
            visibility,
            parameters,
            config,
            store,
            registry,
            profile,
        )
    if isinstance(parameters, PortfolioExecutionParameters):
        return _execute_portfolio(
            tool_name,
            dataset_id,
            visibility,
            parameters,
            store,
            registry,
            profile,
        )
    if isinstance(parameters, PositionSizingExecutionParameters):
        return _execute_sizing(
            tool_name,
            dataset_id,
            visibility,
            parameters,
            store,
            registry,
            profile,
        )
    if isinstance(parameters, ScenarioExecutionParameters):
        return _execute_scenario(
            tool_name,
            dataset_id,
            visibility,
            parameters,
            store,
            registry,
            profile,
        )
    if isinstance(parameters, OptimizationExecutionParameters):
        return _execute_optimization(
            tool_name,
            dataset_id,
            visibility,
            parameters,
            store,
            registry,
            profile,
        )
    if isinstance(parameters, DerivativesExecutionParameters):
        return _execute_derivatives(
            tool_name,
            dataset_id,
            visibility,
            parameters,
            store,
            registry,
            profile,
        )
    return _execute_backtest(
        tool_name,
        dataset_id,
        visibility,
        parameters,
        config,
        store,
        registry,
        profile,
        backtest_proof,
    )


def execute_pretrade_proposal(  # noqa: C901, PLR0913
    *,
    tool_name: str,
    origin: AnalysisResult,
    instrument_handle: str,
    side: Literal["buy", "sell"],
    quantity: Decimal,
    proposal_price: Decimal | None,
    maximum_loss: Decimal | None,
    holding_period_days: int,
    visibility: VisibilityMode,
    store: AnalyticsStore,
    registry: ProofRegistry,
) -> AnalysisResult:
    """Use one current server-owned account context to produce a non-authorizing proposal."""
    if proposal_price is None or maximum_loss is None:
        raise StoredAnalysisExecutionError("explicit_pretrade_inputs_required")
    if visibility is not VisibilityMode.PRIVATE_USER_RESULT:
        raise StoredAnalysisExecutionError("private_result_required")
    profile = _active_profile(registry, "pretrade_impact")
    matches: list[tuple[AuthenticatedSnapshotMaterial, StoredPretradeExecutionContext]] = []
    for snapshot in store.find_authenticated_snapshot_materials(_PRETRADE_CONTEXT_KIND):
        context = _parse_context(snapshot, StoredPretradeExecutionContext)
        if (
            context.origin_analysis_id == origin.analysis_id
            and context.instrument_handle == instrument_handle
        ):
            matches.append((snapshot, context))
    if not matches:
        raise StoredAnalysisExecutionError("pretrade_context_unavailable")
    if len(matches) != 1:
        raise StoredAnalysisExecutionError("pretrade_context_ambiguous")
    snapshot, context = matches[0]
    if (
        context.dataset_id != snapshot.snapshot.dataset_id
        or context.account_alias != snapshot.account_scope
        or context.as_of != snapshot.snapshot.as_of
        or context.as_of < origin.as_of
        or proposal_price != context.reference_price
        or holding_period_days != context.cost_holding_period_days
    ):
        raise StoredAnalysisExecutionError("proposal_context_mismatch")
    primary, supporting = _context_materials(
        store,
        context.dataset_id,
        context.supporting_dataset_ids,
    )
    bindings = _canonical_source_bindings((primary, *supporting))
    source_request = _model_with_updates(
        context,
        StoredPretradeExecutionContext,
        source_bindings=bindings,
        quality_state=primary.dataset.quality_state,
    )
    scaled_costs = _scaled_cost_components(source_request.unit_cost_estimate, quantity)
    proposal = TradeProposal(
        origin_analysis_id=origin.analysis_id,
        dataset_id=source_request.dataset_id,
        account_alias=source_request.account_alias,
        instrument_handle=source_request.instrument_handle,
        decision_at=source_request.as_of,
        side=side,
        quantity=quantity,
        proposal_price=proposal_price,
        contract_multiplier=source_request.contract_multiplier,
        instrument_currency=source_request.instrument_currency,
        reporting_currency=source_request.reporting_currency,
        current_position_quantity=source_request.current_position_quantity,
        current_position_exposure=source_request.current_position_exposure,
        portfolio_value=source_request.portfolio_value,
        current_currency_exposure=source_request.current_currency_exposure,
        buying_power_available=source_request.buying_power_available,
        estimated_cash_required=(
            source_request.buy_cash_required_per_unit
            if side == "buy"
            else source_request.sell_cash_required_per_unit
        )
        * quantity,
        margin_available=source_request.margin_available,
        estimated_margin_required=source_request.margin_required_per_unit * quantity,
        holding_period_days=holding_period_days,
        cost_estimate=scaled_costs,
        saxo_illustration=source_request.saxo_illustration,
        named_cost_difference=source_request.named_cost_difference,
        decision_quote=source_request.decision_quote,
        decision_bar=source_request.decision_bar,
        fx_quotes=source_request.fx_quotes,
        source_bindings=bindings,
        quality_state=source_request.quality_state,
        missing_fields=source_request.missing_fields,
        warnings=source_request.warnings,
    )
    domain_result = build_pretrade_impact(
        origin.analysis_id,
        proposal,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    if domain_result.status is ResearchStatus.REDUCED:
        raise StoredAnalysisExecutionError("pretrade_verified_basis_unavailable")
    values = domain_result.private_values
    if not isinstance(values, PrivatePreTradeValues):
        raise StoredAnalysisExecutionError("private_result_required")
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind="pretrade_impact",
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=(instrument_handle,),
        as_of=source_request.as_of,
        start_at=source_request.as_of,
        end_at=source_request.as_of,
        reporting_currency=source_request.reporting_currency,
        metric_claims=(
            _MetricClaim("estimated_transaction_cost", values.costs.total_cost),
            _MetricClaim("maximum_loss", maximum_loss),
        ),
        warnings=domain_result.warnings,
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "holding_period_days": holding_period_days,
            "instrument_handle": instrument_handle,
            "maximum_loss": str(maximum_loss),
            "origin_analysis_id": origin.analysis_id,
            "proposal_price": str(proposal_price),
            "quantity": str(quantity),
            "side": side,
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=("The exact stored pre-trade impact for the explicit proposal choices.",),
        does_not_verify=(
            "Broker preview acceptance.",
            "Order approval or execution.",
            "Future market values.",
        ),
    )
    store.put_analysis(result)
    return result


def _active_profile(registry: ProofRegistry, analysis_kind: str) -> ProofProfile:
    profile = registry.profile(analysis_kind)
    if profile is None:
        raise StoredAnalysisExecutionError("missing_proof_profile")
    if profile.activation_state is not ProfileActivationState.ACTIVE:
        raise StoredAnalysisExecutionError(profile.quarantine_reason or "proof_quarantined")
    return profile


def _execute_instrument(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: InstrumentExecutionParameters,
    config: AnalyticsConfig,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> AnalysisResult:
    if len(parameters.instrument_handles) != 1:
        raise StoredAnalysisExecutionError("multi_instrument_executor_unavailable")
    primary = store.get_authenticated_dataset_material(dataset_id)
    series = _stored_price_series(primary.dataset, config=config)
    if parameters.instrument_handles != (series.instrument_handle,):
        raise StoredAnalysisExecutionError("instrument_dataset_scope_mismatch")
    domain_result = analyze_instrument_prices(
        series,
        rolling_window=parameters.rolling_window,
        periods_per_year=parameters.periods_per_year,
        requested_return=parameters.requested_return,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=None,
        instrument_handles=parameters.instrument_handles,
        as_of=primary.dataset.created_at,
        start_at=domain_result.start_at,
        end_at=domain_result.end_at,
        reporting_currency="XXX",
        metric_claims=(_MetricClaim("price_return", domain_result.price_return),),
        warnings=domain_result.warnings,
        request_material=parameters,
        profile=profile,
        registry=registry,
        verifies=domain_result.verifies,
        does_not_verify=domain_result.does_not_verify,
    )
    store.put_analysis(result)
    return result


def _execute_portfolio(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: PortfolioExecutionParameters,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> AnalysisResult:
    snapshot = store.get_authenticated_snapshot_material(dataset_id, _PORTFOLIO_CONTEXT_KIND)
    context = _parse_context(snapshot, StoredPortfolioExecutionContext)
    primary, supporting = _context_materials(
        store,
        dataset_id,
        context.supporting_dataset_ids,
    )
    base = context.dataset
    if (
        base.dataset_id != dataset_id
        or base.snapshot_id != snapshot.snapshot.snapshot_id
        or base.account_alias != primary.account_scope
        or base.end_at != snapshot.snapshot.as_of
    ):
        raise StoredAnalysisExecutionError("stored_portfolio_context_mismatch")
    dataset = _model_with_updates(
        base,
        PortfolioPeriodDataset,
        source_bindings=_canonical_source_bindings((primary, *supporting)),
        quality_state=primary.dataset.quality_state,
    )
    domain_result = analyze_portfolio_truth(
        dataset,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    values = domain_result.private_values
    if values is None:
        raise StoredAnalysisExecutionError("private_result_required")
    if values.period_return_percentage is None:
        raise StoredAnalysisExecutionError("twr_boundary_valuations_unavailable")
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=(),
        as_of=dataset.end_at,
        start_at=dataset.start_at,
        end_at=dataset.end_at,
        reporting_currency=dataset.reporting_currency,
        metric_claims=(
            _MetricClaim("time_weighted_return", values.period_return_percentage / Decimal(100)),
        ),
        warnings=domain_result.warnings,
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "parameters": parameters.model_dump(mode="json"),
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=("The reconciled no-external-flow period return at the exact cutoff.",),
        does_not_verify=("Return periods with unbound external-flow boundary valuations.",),
    )
    store.put_analysis(result)
    return result


def _execute_sizing(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: PositionSizingExecutionParameters,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> AnalysisResult:
    snapshot = store.get_authenticated_snapshot_material(dataset_id, _SIZING_CONTEXT_KIND)
    context = _parse_context(snapshot, StoredPositionSizingExecutionContext)
    primary, supporting = _context_materials(
        store,
        dataset_id,
        context.supporting_dataset_ids,
    )
    base = context.request
    if (
        base.dataset_id != dataset_id
        or base.account_alias != primary.account_scope
        or base.instrument_handle != parameters.instrument_handle
    ):
        raise StoredAnalysisExecutionError("stored_sizing_context_mismatch")
    request = _model_with_updates(
        base,
        PositionSizingRequest,
        method=parameters.method,
        maximum_loss=parameters.maximum_loss,
        risk_budget_confirmed=parameters.risk_budget_confirmed,
        stop_price=parameters.stop_price,
        volatility_multiplier=parameters.volatility_multiple,
        source_bindings=_canonical_source_bindings((primary, *supporting)),
        quality_state=primary.dataset.quality_state,
    )
    domain_result = size_position(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    values = domain_result.private_values
    if values is None:
        raise StoredAnalysisExecutionError("private_result_required")
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=(parameters.instrument_handle,),
        as_of=snapshot.snapshot.as_of,
        start_at=snapshot.snapshot.as_of,
        end_at=snapshot.snapshot.as_of,
        reporting_currency=request.reporting_currency,
        metric_claims=(_MetricClaim("position_size", values.quantity),),
        warnings=domain_result.warnings,
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "parameters": parameters.model_dump(mode="json"),
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=("The lot-aligned size under the caller-supplied risk budget.",),
        does_not_verify=("A preferred risk budget.", "Order approval or execution."),
    )
    store.put_analysis(result)
    return result


def _execute_scenario(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: ScenarioExecutionParameters,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> AnalysisResult:
    if parameters.analysis_kind == "scenario_historical":
        raise StoredAnalysisExecutionError("historical_replay_observations_unbound")
    snapshot = store.get_authenticated_snapshot_material(dataset_id, _SCENARIO_CONTEXT_KIND)
    context = _parse_context(snapshot, StoredScenarioExecutionContext)
    primary, supporting = _context_materials(
        store,
        dataset_id,
        context.supporting_dataset_ids,
    )
    base = context.request
    if (
        base.dataset_id != dataset_id
        or base.snapshot_id != snapshot.snapshot.snapshot_id
        or base.account_alias != primary.account_scope
    ):
        raise StoredAnalysisExecutionError("stored_scenario_context_mismatch")
    supplied = {shock.instrument_handle: shock for shock in parameters.shocks}
    expected = {component.instrument_handle for component in base.components}
    if set(supplied) != expected:
        raise StoredAnalysisExecutionError("scenario_shock_map_incomplete")
    component_shocks = tuple(
        _scenario_shock_from_stored(existing, supplied[existing.instrument_handle])
        for existing in base.component_shocks
    )
    request = _model_with_updates(
        base,
        PortfolioScenarioRequest,
        scenario_type=_scenario_type(parameters.analysis_kind),
        input_mode="numeric",
        narrative_fingerprint_sha256=None,
        numeric_shocks_echoed_by_caller=False,
        caller_accepted_numeric_shocks=False,
        echoed_shock_map_sha256=None,
        accepted_shock_map_sha256=None,
        historical_start_at=None,
        historical_end_at=None,
        component_shocks=component_shocks,
        source_bindings=_canonical_source_bindings((primary, *supporting)),
        quality_state=primary.dataset.quality_state,
    )
    domain_result = run_portfolio_scenario(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    values = domain_result.private_values
    if values is None:
        raise StoredAnalysisExecutionError("private_result_required")
    metric_claim = _scenario_metric_claim(parameters.analysis_kind, values)
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=tuple(sorted(expected)),
        as_of=request.as_of,
        start_at=request.as_of,
        end_at=request.as_of,
        reporting_currency=request.reporting_currency,
        metric_claims=(metric_claim,),
        warnings=domain_result.warnings,
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "parameters": parameters.model_dump(mode="json"),
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=("The exact arithmetic effect of the accepted explicit shock map.",),
        does_not_verify=("A forecast or probability of the scenario.",),
    )
    store.put_analysis(result)
    return result


def _execute_optimization(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: OptimizationExecutionParameters,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> AnalysisResult:
    snapshot = store.get_authenticated_snapshot_material(dataset_id, _OPTIMIZATION_CONTEXT_KIND)
    context = _parse_context(snapshot, StoredOptimizationExecutionContext)
    primary, supporting = _context_materials(
        store,
        dataset_id,
        context.supporting_dataset_ids,
    )
    base = context.request
    base_dataset = base.dataset
    if (
        base_dataset.dataset_id != dataset_id
        or base_dataset.snapshot_id != snapshot.snapshot.snapshot_id
        or base_dataset.account_alias != primary.account_scope
        or parameters.analysis_kind != f"portfolio_{parameters.objective}"
    ):
        raise StoredAnalysisExecutionError("stored_optimization_context_mismatch")
    dataset = _model_with_updates(
        base_dataset,
        type(base_dataset),
        source_bindings=_canonical_source_bindings((primary, *supporting)),
        quality_state=primary.dataset.quality_state,
    )
    request = _model_with_updates(
        base,
        OptimizationRequest,
        dataset=dataset,
        objective=parameters.objective,
        objective_confirmed_by_caller=parameters.objective_confirmed_by_caller,
        constraints_confirmed_by_caller=parameters.constraints_confirmed_by_caller,
        short_policy=parameters.short_policy,
        maximum_turnover=parameters.maximum_turnover,
        maximum_transaction_cost_ratio=parameters.maximum_transaction_cost_ratio,
        maximum_margin_ratio=parameters.maximum_margin_ratio,
    )
    domain_result = optimize_portfolio(
        request,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    values = domain_result.private_values
    if values is None:
        raise StoredAnalysisExecutionError("private_result_required")
    metric_claim = (
        _MetricClaim("minimum_variance_objective", values.objective_value)
        if parameters.objective == "minimum_variance"
        else _MetricClaim(
            "risk_parity_contribution",
            values.diagnostics.maximum_risk_contribution_deviation or Decimal(0),
        )
    )
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=tuple(asset.instrument_handle for asset in request.dataset.assets),
        as_of=request.dataset.as_of,
        start_at=request.dataset.estimation_start_at,
        end_at=request.dataset.estimation_end_at,
        reporting_currency=request.dataset.reporting_currency,
        metric_claims=(metric_claim,),
        warnings=domain_result.warnings,
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "parameters": parameters.model_dump(mode="json"),
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=("The caller-selected constrained objective and feasibility diagnostics.",),
        does_not_verify=(
            "A preferred objective or risk tolerance.",
            "Any order or execution instruction.",
        ),
    )
    store.put_analysis(result)
    return result


def _execute_derivatives(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: DerivativesExecutionParameters,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
) -> AnalysisResult:
    if parameters.volatility_assumption is None or parameters.rate_assumption is None:
        raise StoredAnalysisExecutionError("explicit_derivatives_assumptions_required")
    snapshot = store.get_authenticated_snapshot_material(dataset_id, _DERIVATIVES_CONTEXT_KIND)
    context = _parse_context(snapshot, StoredDerivativesExecutionContext)
    primary, supporting = _context_materials(
        store,
        dataset_id,
        context.supporting_dataset_ids,
    )
    base_dataset = context.dataset
    contract = context.model_input.contract
    expected_handles = (contract.option_handle, contract.underlying_handle)
    if (
        base_dataset.dataset_id != dataset_id
        or base_dataset.account_alias != primary.account_scope
        or set(parameters.instrument_handles) != set(expected_handles)
    ):
        raise StoredAnalysisExecutionError("stored_derivatives_context_mismatch")
    dataset = _model_with_updates(
        base_dataset,
        DerivativeDataset,
        source_bindings=_canonical_source_bindings((primary, *supporting)),
        quality_state=primary.dataset.quality_state,
    )
    contract_updates: dict[str, object] = {}
    contract_updates["risk_free_rate"] = float(parameters.rate_assumption)
    updated_contract = _model_with_updates(
        contract,
        type(contract),
        **contract_updates,
    )
    model_input = _model_with_updates(
        context.model_input,
        OptionModelInput,
        contract=updated_contract,
        volatility=float(parameters.volatility_assumption),
    )
    domain_result = analyze_option_model(
        dataset,
        model_input,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
        saxo_greeks=context.saxo_greeks,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    if domain_result.status is ResearchStatus.REDUCED and set(domain_result.warnings) != {
        "saxo_greeks_not_compared"
    }:
        raise StoredAnalysisExecutionError("saxo_greek_reconciliation_unavailable")
    values = domain_result.private_values
    if not isinstance(values, OptionAnalyticsValues):
        raise StoredAnalysisExecutionError("private_result_required")
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=expected_handles,
        as_of=dataset.as_of,
        start_at=dataset.as_of,
        end_at=dataset.as_of,
        reporting_currency=contract.contract_currency,
        metric_claims=(_MetricClaim("theoretical_option_value", values.model.value),),
        warnings=domain_result.warnings,
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "parameters": parameters.model_dump(mode="json"),
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=("The declared European option model under the explicit assumptions.",),
        does_not_verify=("A broker quote, forecast, recommendation, or American exercise.",),
        is_not_forecast=True,
    )
    store.put_analysis(result)
    return result


def _execute_backtest(  # noqa: PLR0913
    tool_name: str,
    dataset_id: str,
    visibility: VisibilityMode,
    parameters: BacktestExecutionParameters,
    config: AnalyticsConfig,
    store: AnalyticsStore,
    registry: ProofRegistry,
    profile: ProofProfile,
    backtest_proof: AuthenticatedBacktestExecutionProof | None,
) -> AnalysisResult:
    if backtest_proof is None:
        raise StoredAnalysisExecutionError("backtest_sim_proof_unavailable")
    snapshot = store.get_authenticated_snapshot_material(dataset_id, _BACKTEST_CONTEXT_KIND)
    context = _parse_context(snapshot, StoredBacktestExecutionContext)
    primary, supporting = _context_materials(
        store,
        dataset_id,
        context.supporting_dataset_ids,
    )
    series = _stored_price_series(primary.dataset, config=config)
    if (
        context.instrument_handle != parameters.instrument_handle
        or series.instrument_handle != parameters.instrument_handle
        or context.account_alias != primary.account_scope
        or backtest_proof.dataset_id != dataset_id
        or backtest_proof.account_alias != context.account_alias
        or backtest_proof.instrument_handle != context.instrument_handle
        or backtest_proof.strategy_fingerprint_sha256
        != strategy_definition_fingerprint(parameters.strategy)
        or backtest_proof.fill_model != parameters.strategy.rebalancing.fill_timing
    ):
        raise StoredAnalysisExecutionError("stored_backtest_context_mismatch")
    dataset = BacktestDataset(
        dataset_id=dataset_id,
        account_alias=context.account_alias,
        instrument_handle=context.instrument_handle,
        as_of=primary.dataset.created_at,
        bars=tuple(
            HistoricalBar(
                instrument_handle=row.instrument_handle,
                at=row.bar_time,
                open_price=row.open_value,
                close_price=row.close_value,
                lifecycle_state="active",
            )
            for row in series.bars
        ),
        source_bindings=_canonical_source_bindings((primary, *supporting)),
        quality_state=primary.dataset.quality_state,
        missing_interval_count=context.missing_interval_count,
        missing_fields=context.missing_fields,
        warnings=context.warnings,
        universe_scope="single_instrument",
    )
    domain_result = run_backtest(
        BacktestRequest(
            dataset=dataset,
            strategy=parameters.strategy,
            starting_equity=parameters.starting_equity,
        ),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        trusted_local_host=True,
    )
    if isinstance(domain_result, ResearchRefusal):
        raise StoredAnalysisExecutionError(domain_result.reason_code)
    values = domain_result.private_values
    if values is None:
        raise StoredAnalysisExecutionError("private_result_required")
    result = _build_proof_result(
        tool_name=tool_name,
        analysis_kind=parameters.analysis_kind,
        visibility=visibility,
        primary=primary,
        snapshot_id=snapshot.snapshot.snapshot_id,
        instrument_handles=(parameters.instrument_handle,),
        as_of=dataset.as_of,
        start_at=dataset.bars[0].at,
        end_at=dataset.bars[-1].at,
        reporting_currency=parameters.strategy.transaction_costs.currency,
        metric_claims=(_MetricClaim("total_return", values.total_return_ratio),),
        warnings=(
            *(
                warning
                for warning in domain_result.warnings
                if warning != "ghost_workflow_not_executed"
            ),
            *domain_result.overfit_warnings,
        ),
        request_material={
            "context_fingerprint_sha256": snapshot.snapshot.fingerprint_sha256,
            "parameters": parameters.model_dump(mode="json"),
            "supporting_dataset_fingerprints": [
                material.dataset.fingerprint_sha256 for material in supporting
            ],
        },
        profile=profile,
        registry=registry,
        verifies=(
            "The deterministic bounded backtest accounting under the declared strategy.",
            "Equivalent controlled SIM ghost lifecycle and cleanup for this exact strategy.",
        ),
        does_not_verify=("Future returns, advice, or execution authority.",),
        is_not_forecast=True,
    )
    store.put_analysis(result)
    return result


@dataclass(frozen=True, slots=True)
class _MetricClaim:
    metric_id: str
    value: float | Decimal


def _parse_context[ModelT: BaseModel](
    snapshot: AuthenticatedSnapshotMaterial,
    model_type: type[ModelT],
) -> ModelT:
    try:
        return model_type.model_validate_json(
            json.dumps(snapshot.payload, separators=(",", ":"), sort_keys=True),
        )
    except ValueError as error:
        raise StoredAnalysisExecutionError("stored_execution_context_invalid") from error


def _model_with_updates[ModelT: BaseModel](
    model: BaseModel,
    model_type: type[ModelT],
    **updates: object,
) -> ModelT:
    payload = model.model_dump(mode="json")
    payload.update({name: _jsonable(value) for name, value in updates.items()})
    try:
        return model_type.model_validate_json(
            json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True),
        )
    except ValueError as error:
        raise StoredAnalysisExecutionError("stored_execution_context_invalid") from error


def _jsonable(value: object) -> object:  # noqa: PLR0911
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast("Sequence[object]", value)
        return [_jsonable(item) for item in sequence]
    return value


def _context_materials(
    store: AnalyticsStore,
    primary_dataset_id: str,
    supporting_dataset_ids: Sequence[str],
) -> tuple[AuthenticatedDatasetMaterial, tuple[AuthenticatedDatasetMaterial, ...]]:
    if primary_dataset_id in supporting_dataset_ids:
        raise StoredAnalysisExecutionError("supporting_dataset_scope_invalid")
    primary = store.get_authenticated_dataset_material(primary_dataset_id)
    supporting = tuple(
        store.get_authenticated_dataset_material(dataset_id)
        for dataset_id in supporting_dataset_ids
    )
    if any(
        material.dataset.quality_state is not QualityState.COMPLETE
        for material in (primary, *supporting)
    ):
        raise StoredAnalysisExecutionError("verified_coverage_unavailable")
    primary_page_ids = {page.page_id for page in primary.pages}
    if any(
        material.account_scope != primary.account_scope
        or not {page.page_id for page in material.pages} <= primary_page_ids
        for material in supporting
    ):
        raise StoredAnalysisExecutionError("supporting_dataset_not_proof_bound")
    return primary, supporting


def _canonical_source_bindings(
    materials: Sequence[AuthenticatedDatasetMaterial],
) -> tuple[SaxoSourceBinding, ...]:
    return _canonical_source_bindings_from_pages(
        tuple(page for material in materials for page in material.pages),
    )


def _canonical_source_bindings_from_pages(  # noqa: C901
    pages: Sequence[AuthenticatedSourceMaterial],
) -> tuple[SaxoSourceBinding, ...]:
    pages_by_contract: dict[str, dict[str, AuthenticatedSourceMaterial]] = {}
    for page in pages:
        pages_by_contract.setdefault(page.contract_name, {})[page.page_id] = page
    bindings: list[SaxoSourceBinding] = []
    contracts = source_contracts_by_id()
    for contract_id in sorted(pages_by_contract):
        typed_pages = tuple(pages_by_contract[contract_id].values())
        contract = contracts.get(contract_id)
        if contract is None:
            raise StoredAnalysisExecutionError("source_binding_invalid")
        revisions = {page.source_revision for page in typed_pages}
        shas = {page.contract_sha256 for page in typed_pages}
        if len(revisions) != 1 or shas != {source_contract_fingerprint(contract)}:
            raise StoredAnalysisExecutionError("source_binding_ambiguous")
        page_fingerprints = sorted(page.fingerprint_sha256 for page in typed_pages)
        capture_fingerprint = (
            page_fingerprints[0]
            if len(page_fingerprints) == 1
            else _sha256_json({"page_fingerprints": page_fingerprints})
        )
        entitlement: Literal["available", "partial", "denied"] = "available"
        quality = QualityState.COMPLETE
        for page in typed_pages:
            source_quality = page.payload.get("source_quality")
            if isinstance(source_quality, Mapping) and source_quality.get("state") == "limited":
                quality = QualityState.PARTIAL
                if source_quality.get("entitlement_limited_fields"):
                    entitlement = "partial"
            raw_entitlement = page.payload.get("entitlement")
            if isinstance(raw_entitlement, Mapping):
                state = raw_entitlement.get("state")
                if state == "denied":
                    entitlement = "denied"
                elif state == "partial" and entitlement != "denied":
                    entitlement = "partial"
        bindings.append(
            SaxoSourceBinding(
                contract_id=contract_id,
                contract_sha256=source_contract_fingerprint(contract),
                source_revision=next(iter(revisions)),
                capture_fingerprint_sha256=capture_fingerprint,
                quality_state=quality,
                entitlement_state=entitlement,
            ),
        )
    return tuple(bindings)


def _scenario_shock_from_stored(
    stored: ScenarioShock,
    supplied: ScenarioExecutionShock,
) -> ScenarioShock:
    return _model_with_updates(
        stored,
        ScenarioShock,
        price_shock_ratio=supplied.price_shock_ratio,
        volatility_shock_points=supplied.volatility_shock_points,
        rate_shock_basis_points=supplied.rate_shock_basis_points,
    )


def _scenario_type(analysis_kind: str) -> str:
    by_kind = {
        "margin_fire_drill": "margin",
        "portfolio_scenario": "equity",
        "scenario_custom": "equity",
        "scenario_currency": "currency",
        "scenario_volatility": "volatility",
        "scenario_rate": "rate",
        "scenario_margin": "margin",
        "scenario_combined": "combined",
    }
    scenario_type = by_kind.get(analysis_kind)
    if scenario_type is None:
        raise StoredAnalysisExecutionError("scenario_kind_unsupported")
    return scenario_type


def _scenario_metric_claim(
    analysis_kind: str,
    values: PrivateScenarioValues,
) -> _MetricClaim:
    metric_ids = {
        "margin_fire_drill": "combined_scenario_effect",
        "portfolio_scenario": "custom_shock_effect",
        "scenario_custom": "custom_shock_effect",
        "scenario_currency": "currency_shock_effect",
        "scenario_volatility": "volatility_shock_effect",
        "scenario_rate": "rate_shock_effect",
        "scenario_combined": "combined_scenario_effect",
    }
    metric_id = metric_ids.get(analysis_kind)
    if metric_id is None:
        raise StoredAnalysisExecutionError("scenario_metric_binding_unavailable")
    return _MetricClaim(metric_id, values.total_effect)


def _scaled_cost_components(costs: CostComponents, quantity: Decimal) -> CostComponents:
    return CostComponents(
        commission=costs.commission * quantity,
        spread=costs.spread * quantity,
        fx_conversion=costs.fx_conversion * quantity,
        financing=costs.financing * quantity,
        borrow=costs.borrow * quantity,
        custody=costs.custody * quantity,
        tax=costs.tax * quantity,
        turnover=costs.turnover * quantity,
        total_cost=costs.total_cost * quantity,
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def _build_proof_result(  # noqa: C901, PLR0912, PLR0913, PLR0915
    *,
    tool_name: str,
    analysis_kind: str,
    visibility: VisibilityMode,
    primary: AuthenticatedDatasetMaterial,
    snapshot_id: str | None,
    instrument_handles: tuple[str, ...],
    as_of: datetime,
    start_at: datetime,
    end_at: datetime,
    reporting_currency: str,
    metric_claims: tuple[_MetricClaim, ...],
    warnings: Sequence[str],
    request_material: BaseModel | Mapping[str, object],
    profile: ProofProfile,
    registry: ProofRegistry,
    verifies: tuple[str, ...],
    does_not_verify: tuple[str, ...],
    is_not_forecast: bool = True,
) -> AnalysisResult:
    dataset = primary.dataset
    if dataset.quality_state is not QualityState.COMPLETE or start_at > end_at or end_at > as_of:
        raise StoredAnalysisExecutionError("verified_coverage_unavailable")
    source_timestamp = max(page.source_timestamp for page in primary.pages)
    if source_timestamp > as_of:
        raise StoredAnalysisExecutionError("future_source_observation")
    valid_until = profile.valid_until
    if valid_until is None or valid_until <= datetime.now(UTC):
        raise StoredAnalysisExecutionError("proof_expired")
    engines = tuple(profile.engines)
    if len(engines) != 1 or engines[0].engine_name != _ENGINE_NAME:
        raise StoredAnalysisExecutionError("proof_engine_executor_unavailable")
    engine = engines[0]
    authenticated_source_contracts: dict[str, str] = {}
    for page in primary.pages:
        existing = authenticated_source_contracts.setdefault(
            page.contract_name,
            page.contract_sha256,
        )
        if existing != page.contract_sha256:
            raise StoredAnalysisExecutionError("source_binding_ambiguous")
    if not authenticated_source_contracts:
        raise StoredAnalysisExecutionError("source_contract_missing")
    declared_contract_ids = {binding.contract_id for binding in profile.source_contracts}
    proof_source_contracts = {
        contract_id: contract_sha256
        for contract_id, contract_sha256 in authenticated_source_contracts.items()
        if contract_id in declared_contract_ids
    }
    if set(proof_source_contracts) != declared_contract_ids:
        raise StoredAnalysisExecutionError("proof_source_contract_missing")
    proof_status = registry.status(
        analysis_kind,
        "1",
        proof_source_contracts,
        source_revision=dataset.source_revision,
        engine_versions={
            engine.engine_name: (engine.engine_version, engine.code_commit),
        },
        at=datetime.now(UTC),
    )
    if proof_status.state is not ProofState.ACTIVE:
        raise StoredAnalysisExecutionError(proof_status.reason_code)
    profile_metric_ids = {binding.metric_id for binding in profile.metric_definitions}
    if not metric_claims or any(
        claim.metric_id not in profile_metric_ids for claim in metric_claims
    ):
        raise StoredAnalysisExecutionError("proof_metric_executor_unavailable")
    material = (
        request_material.model_dump(mode="json")
        if isinstance(request_material, BaseModel)
        else _jsonable(request_material)
    )
    parameters = AnalysisParameterBinding(
        start_at=start_at,
        end_at=end_at,
        as_of=as_of,
        benchmark_handle=None,
        benchmark_fingerprint_sha256=None,
        fx_method=FxConversionMethod.NOT_APPLICABLE,
        fx_source=FxSource.NOT_APPLICABLE,
        fx_timestamp=None,
        calendar=AnalysisCalendar.CALENDAR_DAYS,
        reporting_currency=reporting_currency,
        metric_currency_bindings=tuple(
            sorted(
                (
                    MetricCurrencyBinding(
                        metric_id=claim.metric_id,
                        currency=reporting_currency,
                    )
                    for claim in metric_claims
                    if registry.definitions.by_id()[claim.metric_id].output_unit == "price_currency"
                ),
                key=lambda binding: binding.metric_id,
            ),
        ),
        model_parameters=_fingerprint_parameters(material),
    )
    request: MarketAnalysisRequest | InstrumentAnalysisRequest | PortfolioAnalysisRequest
    if snapshot_id is not None:
        request = PortfolioAnalysisRequest(
            request_kind="portfolio",
            analysis_kind=analysis_kind,
            dataset_id=dataset.dataset_id,
            portfolio_snapshot_id=snapshot_id,
            parameters=parameters,
        )
    elif instrument_handles:
        request = InstrumentAnalysisRequest(
            request_kind="instrument",
            analysis_kind=analysis_kind,
            dataset_id=dataset.dataset_id,
            instrument_handles=instrument_handles,
            parameters=parameters,
        )
    else:
        request = MarketAnalysisRequest(
            request_kind="market",
            analysis_kind=analysis_kind,
            dataset_id=dataset.dataset_id,
            parameters=parameters,
        )
    assumptions: tuple[NamedModelAssumption, ...] = ()
    parameter_sha256 = build_analysis_parameters_sha256(
        account_scope=primary.account_scope,
        analysis_kind=analysis_kind,
        assumptions=assumptions,
        as_of=as_of,
        request=request,
    )
    engine_versions = {
        engine.engine_name: {
            "version": engine.engine_version,
            "code_commit": engine.code_commit,
        },
    }
    identity = build_analysis_identity(
        {
            "dataset_fingerprint_sha256": dataset.fingerprint_sha256,
            "dataset_id": dataset.dataset_id,
            "source_revision": dataset.source_revision,
            "tool_name": tool_name,
            "analysis_parameters_sha256": parameter_sha256,
        },
        engine_versions,
        None,
    )
    contract_shas = tuple(sorted(authenticated_source_contracts.values()))
    if len(contract_shas) == 1:
        primary_contract_sha = contract_shas[0]
        contract_sha_set: tuple[str, ...] = ()
    else:
        primary_contract_sha = _sha256_json(
            {"source_contract_sha256s": list(contract_shas)},
        )
        contract_sha_set = contract_shas
    source_binding = ProofSourceBinding(
        source_scope="saxo_openapi",
        source_revision=dataset.source_revision,
        source_contract_sha256=primary_contract_sha,
        source_contract_sha256s=contract_sha_set,
    )
    engine_binding = ProofEngineBinding(
        engine_name=engine.engine_name,
        engine_version=engine.engine_version,
        code_commit=engine.code_commit,
    )
    warning_models = tuple(
        AnalysisWarning(
            code=code,
            message="The exact verified metric preserves this named bounded limitation.",
            quality_state=QualityState.COMPLETE,
            next_action="Inspect this same stored analysis before using its bounded result.",
        )
        for code in sorted(set(warnings))
    )
    definitions = registry.definitions.by_id()
    metrics = tuple(
        MetricValue(
            metric_id=claim.metric_id,
            value=float(claim.value),
            unit=definitions[claim.metric_id].output_unit,
            unit_class=definitions[claim.metric_id].unit_class,
            currency=(
                reporting_currency
                if definitions[claim.metric_id].unit_class is ValueUnitClass.MONETARY
                else None
            ),
            metric_class=definitions[claim.metric_id].default_metric_class,
            source_timestamp=source_timestamp,
            proof_profile_id=profile.proof_profile_id,
        )
        for claim in metric_claims
    )
    result = AnalysisResult(
        visibility=visibility,
        tool_name=tool_name,
        analysis_id=identity.analysis_id,
        analysis_kind=analysis_kind,
        request=request,
        account_scope=primary.account_scope,
        as_of=as_of,
        valid_until=valid_until,
        metrics=metrics,
        warnings=warning_models,
        data_quality=DataQuality(
            state=QualityState.COMPLETE,
            coverage=DataCoverage(
                state=QualityState.COMPLETE,
                start_at=primary.coverage_start,
                end_at=primary.coverage_end,
                row_count=dataset.row_count,
                expected_row_count=dataset.row_count,
                missing_row_count=0,
            ),
            checked_at=as_of,
            warnings=warning_models,
        ),
        provenance=AnalysisProvenance(
            dataset_id=dataset.dataset_id,
            source_scope="saxo_openapi",
            source_revision=dataset.source_revision,
            source_contract_sha256=primary_contract_sha,
            source_contract_sha256s=contract_sha_set,
            source_timestamp=source_timestamp,
            proof_receipts=(
                ActiveProofReceipt(
                    state="active",
                    analysis_kind=analysis_kind,
                    schema_version="1",
                    source_binding=source_binding,
                    engine_binding=engine_binding,
                    proof_profile_id=profile.proof_profile_id,
                    checks_passed=_PROOF_CHECKS,
                ),
            ),
            engine_name=engine.engine_name,
            engine_version=engine.engine_version,
            code_commit=engine.code_commit,
            analysis_input_sha256=identity.input_sha256,
            analysis_parameters_sha256=parameter_sha256,
            analysis_engine_sha256=identity.engine_sha256,
            analysis_seed_sha256=identity.seed_sha256,
            random_seed=None,
        ),
        assumptions=assumptions,
        is_not_advice=True,
        is_not_forecast=is_not_forecast,
        model_distribution_only=False,
        verifies=verifies,
        does_not_verify=does_not_verify,
        replayable=True,
        next_actions=("Explain, render, or export this same stored analysis handle.",),
    )
    if not all(math.isfinite(metric.value) for metric in result.metrics):
        raise StoredAnalysisExecutionError("analytics_measure_undefined")
    return result


def _fingerprint_parameters(material: object) -> tuple[NamedModelParameter, ...]:
    digest = _sha256_json(material)
    chunks = tuple(int(digest[index : index + 4], 16) for index in range(0, len(digest), 4))
    if len(chunks) != _REQUEST_FINGERPRINT_CHUNKS:
        raise StoredAnalysisExecutionError("analysis_parameter_binding_failed")
    return tuple(
        NamedModelParameter(
            name=f"request_fingerprint_{index:02d}",
            value=float(value),
            unit=ModelScalarUnit.DIMENSIONLESS,
        )
        for index, value in enumerate(chunks)
    )


def _stored_price_series(
    dataset: StoredDataset,
    *,
    config: AnalyticsConfig,
) -> PriceSeriesDataset:
    page_number = 1
    rows: list[PriceBarDatasetRow] = []
    expected_total: int | None = None
    while True:
        page = get_dataset(
            dataset.dataset_id,
            page_number,
            config.limits.response_rows,
            config=config,
        )
        if expected_total is None:
            expected_total = page.total_rows
        if page.total_rows != expected_total or any(
            not isinstance(row, PriceBarDatasetRow) for row in page.rows
        ):
            raise StoredAnalysisExecutionError("stored_price_dataset_invalid")
        rows.extend(row for row in page.rows if isinstance(row, PriceBarDatasetRow))
        if page.next_page is None:
            break
        if page.next_page != page_number + 1:
            raise StoredAnalysisExecutionError("stored_price_dataset_invalid")
        page_number = page.next_page
    if expected_total != len(rows) or not rows:
        raise StoredAnalysisExecutionError("stored_price_dataset_invalid")
    handles = {row.instrument_handle for row in rows}
    if len(handles) != 1:
        raise StoredAnalysisExecutionError("stored_price_dataset_invalid")
    return PriceSeriesDataset(
        dataset_id=dataset.dataset_id,
        instrument_handle=next(iter(handles)),
        bars=tuple(rows),
        quality_state=dataset.quality_state,
        missing_interval_count=0,
        return_series_label="price_return",
        adjustment_status="unadjusted",
        warnings=(),
    )


def _market_result(  # noqa: PLR0913
    *,
    tool_name: str,
    dataset: StoredDataset,
    series: PriceSeriesDataset,
    domain_result: BoundedMarketResearch,
    periods_per_year: float,
    visibility: VisibilityMode,
    profile: ProofProfile,
    registry: ProofRegistry,
) -> AnalysisResult:
    if (
        dataset.quality_state is not QualityState.COMPLETE
        or len(series.bars) < _MINIMUM_PRICE_OBSERVATIONS
    ):
        raise StoredAnalysisExecutionError("verified_coverage_unavailable")
    if len(domain_result.comparisons) != 1:
        raise StoredAnalysisExecutionError("market_comparison_result_invalid")
    source_timestamp = series.bars[-1].bar_time
    as_of = dataset.created_at
    if source_timestamp > as_of:
        raise StoredAnalysisExecutionError("future_source_observation")
    valid_until = profile.valid_until
    if valid_until is None or valid_until <= as_of:
        raise StoredAnalysisExecutionError("proof_expired")
    engines = tuple(profile.engines)
    if len(engines) != 1 or engines[0].engine_name != _ENGINE_NAME:
        raise StoredAnalysisExecutionError("proof_engine_executor_unavailable")
    engine = engines[0]
    chart = source_contracts_by_id()[_CHART_CONTRACT]
    contract_sha256 = source_contract_fingerprint(chart)
    source_contracts = {_CHART_CONTRACT: contract_sha256}
    proof_status = registry.status(
        _MARKET_COMPARISON_KIND,
        "1",
        source_contracts,
        source_revision=dataset.source_revision,
        engine_versions={
            engine.engine_name: (engine.engine_version, engine.code_commit),
        },
        at=datetime.now(UTC),
    )
    if proof_status.state is not ProofState.ACTIVE:
        raise StoredAnalysisExecutionError(proof_status.reason_code)
    parameters = AnalysisParameterBinding(
        start_at=series.bars[0].bar_time,
        end_at=series.bars[-1].bar_time,
        as_of=as_of,
        benchmark_handle=None,
        benchmark_fingerprint_sha256=None,
        fx_method=FxConversionMethod.NOT_APPLICABLE,
        fx_source=FxSource.NOT_APPLICABLE,
        fx_timestamp=None,
        calendar=AnalysisCalendar.TRADING_DAYS,
        reporting_currency="XXX",
        metric_currency_bindings=(),
        model_parameters=(
            NamedModelParameter(
                name="periods_per_year",
                value=periods_per_year,
                unit=ModelScalarUnit.COUNT,
            ),
        ),
    )
    request = MarketAnalysisRequest(
        request_kind="market",
        analysis_kind=_MARKET_COMPARISON_KIND,
        dataset_id=dataset.dataset_id,
        parameters=parameters,
    )
    assumptions = ()
    parameter_sha256 = build_analysis_parameters_sha256(
        account_scope="aggregate",
        analysis_kind=_MARKET_COMPARISON_KIND,
        assumptions=assumptions,
        as_of=as_of,
        request=request,
    )
    engine_versions = {
        engine.engine_name: {
            "version": engine.engine_version,
            "code_commit": engine.code_commit,
        },
    }
    identity = build_analysis_identity(
        {
            "dataset_fingerprint_sha256": dataset.fingerprint_sha256,
            "dataset_id": dataset.dataset_id,
            "source_revision": dataset.source_revision,
            "tool_name": tool_name,
            "analysis_parameters_sha256": parameter_sha256,
        },
        engine_versions,
        None,
    )
    source_binding = ProofSourceBinding(
        source_scope="saxo_openapi",
        source_revision=dataset.source_revision,
        source_contract_sha256=contract_sha256,
    )
    engine_binding = ProofEngineBinding(
        engine_name=engine.engine_name,
        engine_version=engine.engine_version,
        code_commit=engine.code_commit,
    )
    warnings = tuple(
        AnalysisWarning(
            code=code,
            message="The exact verified metric preserves this bounded source limitation.",
            quality_state=QualityState.COMPLETE,
            next_action="Use the named bounded scope; do not infer whole-market coverage.",
        )
        for code in sorted({"bounded_universe_only", *domain_result.warnings})
    )
    definition = registry.definitions.by_id()[_PRICE_RETURN_METRIC]
    metric = MetricValue(
        metric_id=_PRICE_RETURN_METRIC,
        value=domain_result.comparisons[0].price_return,
        unit=definition.output_unit,
        unit_class=definition.unit_class,
        currency=None,
        metric_class=definition.default_metric_class,
        source_timestamp=source_timestamp,
        proof_profile_id=profile.proof_profile_id,
    )
    return AnalysisResult(
        visibility=visibility,
        tool_name=tool_name,
        analysis_id=identity.analysis_id,
        analysis_kind=_MARKET_COMPARISON_KIND,
        request=request,
        account_scope="aggregate",
        as_of=as_of,
        valid_until=valid_until,
        metrics=(metric,),
        warnings=warnings,
        data_quality=DataQuality(
            state=QualityState.COMPLETE,
            coverage=DataCoverage(
                state=QualityState.COMPLETE,
                start_at=series.bars[0].bar_time,
                end_at=series.bars[-1].bar_time,
                row_count=len(series.bars),
                expected_row_count=len(series.bars),
                missing_row_count=0,
            ),
            checked_at=as_of,
            warnings=warnings,
        ),
        provenance=AnalysisProvenance(
            dataset_id=dataset.dataset_id,
            source_scope="saxo_openapi",
            source_revision=dataset.source_revision,
            source_contract_sha256=contract_sha256,
            source_timestamp=as_of,
            proof_receipts=(
                ActiveProofReceipt(
                    state="active",
                    analysis_kind=_MARKET_COMPARISON_KIND,
                    schema_version="1",
                    source_binding=source_binding,
                    engine_binding=engine_binding,
                    proof_profile_id=profile.proof_profile_id,
                    checks_passed=_PROOF_CHECKS,
                ),
            ),
            engine_name=engine.engine_name,
            engine_version=engine.engine_version,
            code_commit=engine.code_commit,
            analysis_input_sha256=identity.input_sha256,
            analysis_parameters_sha256=parameter_sha256,
            analysis_engine_sha256=identity.engine_sha256,
            analysis_seed_sha256=identity.seed_sha256,
            random_seed=None,
        ),
        assumptions=assumptions,
        is_not_advice=True,
        is_not_forecast=True,
        model_distribution_only=False,
        verifies=("The exact unadjusted price return over the authenticated stored dataset.",),
        does_not_verify=("Whole-market coverage.", "Future returns."),
        replayable=True,
        next_actions=("Explain or render this same stored analysis handle.",),
    )


__all__ = (
    "AuthenticatedBacktestExecutionProof",
    "BacktestExecutionParameters",
    "DerivativesExecutionParameters",
    "InstrumentExecutionParameters",
    "OptimizationExecutionParameters",
    "PortfolioExecutionParameters",
    "PositionSizingExecutionParameters",
    "ScenarioExecutionParameters",
    "ScenarioExecutionShock",
    "StoredAnalysisExecutionError",
    "StoredBacktestExecutionContext",
    "StoredDerivativesExecutionContext",
    "StoredExecutionParameters",
    "StoredOptimizationExecutionContext",
    "StoredPortfolioExecutionContext",
    "StoredPositionSizingExecutionContext",
    "StoredPretradeExecutionContext",
    "StoredScenarioExecutionContext",
    "execute_market_comparison",
    "execute_pretrade_proposal",
    "execute_stored_analysis",
)
