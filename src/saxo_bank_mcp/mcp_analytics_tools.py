"""Thin typed FastMCP adapters over the existing analytics domain services."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping, Sequence
from threading import RLock
from typing import Annotated, Final, Literal, cast

import mcp.types as mt
from fastmcp.tools import ToolResult
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import to_jsonable_python

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_attribution import (
    AttributionDataset,
    analyze_portfolio_attribution,
)
from saxo_bank_mcp.analytics_backtest import BacktestRequest, run_backtest
from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    AnalyticsConfigError,
    load_analytics_config,
)
from saxo_bank_mcp.analytics_costs import CostDataset, analyze_cost_xray
from saxo_bank_mcp.analytics_derivatives import (
    DerivativeDataset,
    FuturesCurveRequest,
    FxForwardRequest,
    IvSurfaceRequest,
    LifecycleRadarRequest,
    SaxoGreekSnapshot,
    analyze_futures_curve,
    analyze_fx_forward,
    analyze_iv_surface,
    analyze_lifecycle_radar,
    analyze_option_model,
    analyze_option_strategy,
)
from saxo_bank_mcp.analytics_export import (
    StoredReportExportRequest,
    StoredTableExportRequest,
    export_analysis,
)
from saxo_bank_mcp.analytics_exposure import ExposureDataset, analyze_portfolio_exposure
from saxo_bank_mcp.analytics_income import (
    CorporateActionClaimDataset,
    IncomeDataset,
    analyze_income,
    authoritative_corporate_action_claim,
)
from saxo_bank_mcp.analytics_instruments import (
    PriceSeriesDataset,
    QuoteResearchDataset,
    ResearchRefusal,
    analyze_instrument_prices,
    analyze_quote,
    build_instrument_dossier,
)
from saxo_bank_mcp.analytics_jobs import (
    AnalyticsJobManager,
    JobCapacityError,
    JobError,
    JobNotFoundError,
    JobRequest,
    JobStateError,
)
from saxo_bank_mcp.analytics_liquidity import (
    LiquidityDataset,
    analyze_cash_and_settlement,
)
from saxo_bank_mcp.analytics_market import (
    BoundedResearchUniverse,
    MarketDepthDataset,
    SavedCondition,
    WrapperCostDataset,
    analyze_bounded_market,
    analyze_entitled_depth,
    check_saved_conditions,
    compare_wrappers,
    prepare_bounded_session,
)
from saxo_bank_mcp.analytics_metric_definitions import load_metric_definition_catalog
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    DatasetId,
    InstrumentHandle,
    JobId,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_optimization import OptimizationRequest, optimize_portfolio
from saxo_bank_mcp.analytics_options import OptionModelInput, OptionStrategyRequest
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioAnalyticsError,
    PortfolioPeriodDataset,
    analyze_multi_account_portfolios,
    analyze_portfolio_truth,
    authoritative_tax_lot_export,
)
from saxo_bank_mcp.analytics_position_sizing import PositionSizingRequest, size_position
from saxo_bank_mcp.analytics_pretrade import TradeProposal, build_pretrade_impact
from saxo_bank_mcp.analytics_proof_profiles import (
    ProofProfileError,
    ProofRegistry,
    load_proof_profile_catalog,
)
from saxo_bank_mcp.analytics_provenance import AnalysisReplayRefused, replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider, SourceProviderError
from saxo_bank_mcp.analytics_query import (
    PortfolioQueryError,
    PortfolioQueryIntent,
    parse_portfolio_query,
    portfolio_query_catalog,
)
from saxo_bank_mcp.analytics_render import (
    ArtifactBindingRegistry,
    ArtifactRefusal,
    ArtifactResourceLink,
    InlineArtifact,
    StoredRenderRequest,
    render_analysis,
)
from saxo_bank_mcp.analytics_resolver import (
    InstrumentResolver,
    ResolutionError,
    ResolutionStatus,
    ResolvedInstrument,
)
from saxo_bank_mcp.analytics_scenarios import PortfolioScenarioRequest, run_portfolio_scenario
from saxo_bank_mcp.analytics_storage_tools import (
    StorageBoundaryError,
    delete_analytics_data,
    list_storage,
    preview_deletion,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    DeletionTokenError,
    StorageScope,
    StoreBusyError,
    StoreError,
    StoreNotFoundError,
    StoreQuotaError,
    StoreValidationError,
)
from saxo_bank_mcp.analytics_sync import (
    DatasetNotFoundError,
    SyncError,
    SyncLimitError,
    SyncResearchRequest,
    get_dataset,
    sync_research_data,
)
from saxo_bank_mcp.analytics_trade_review import TradeReviewDataset, analyze_trading_mirror
from saxo_bank_mcp.analytics_universes import (
    ResearchUniverseStore,
    UniverseConflictError,
    UniverseError,
    UniverseNotFoundError,
    UniverseValidationError,
)
from saxo_bank_mcp.server_tool_ids import ANALYTICS_TOOL_IDS

type AnalysisVisibility = Literal[
    VisibilityMode.PUBLIC_EVIDENCE,
    VisibilityMode.FINGERPRINT_ONLY,
    VisibilityMode.REDACTED_PREVIEW,
    VisibilityMode.PRIVATE_USER_RESULT,
]
type ManageUniverseAction = Literal["create", "list", "update", "delete"]
type ManageJobAction = Literal["start", "check", "cancel"]
type ExportKind = Literal["table", "report"]
type AnalyticsExportFormat = Literal["csv", "parquet", "json", "html", "pdf"]

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_RESULT_ADAPTER: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(
    dict[str, JsonValue],
)
_JOB_RUNTIME_LOCK: Final = RLock()
_job_runtime: tuple[str, AnalyticsStore, AnalyticsJobManager] | None = None


class _StrictToolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class AnalyticsToolResponse(_StrictToolModel):
    """Common value-bounded tool envelope with explicit recovery and no write authority."""

    status: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    result: dict[str, JsonValue] | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    message: str | None = Field(default=None, min_length=1, max_length=500)
    warnings: tuple[str, ...] = ()
    next_tool: str | None = Field(default=None, pattern=r"^saxo_[a-z0-9_]{1,127}$")
    next_action: str | None = Field(default=None, min_length=1, max_length=500)
    network_call_made: bool | None = None
    local_state_changed: bool
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class BoundedMarketToolRequest(_StrictToolModel):
    analysis_kind: Literal["bounded_market"] = "bounded_market"
    universe: BoundedResearchUniverse
    periods_per_year: float = Field(default=252.0, gt=0, allow_inf_nan=False)


class MarketDepthToolRequest(_StrictToolModel):
    analysis_kind: Literal["depth"] = "depth"
    dataset: MarketDepthDataset


class WrapperComparisonToolRequest(_StrictToolModel):
    analysis_kind: Literal["wrapper_comparison"] = "wrapper_comparison"
    datasets: tuple[WrapperCostDataset, ...] = Field(min_length=1, max_length=25)


class SavedConditionsToolRequest(_StrictToolModel):
    analysis_kind: Literal["saved_conditions"] = "saved_conditions"
    conditions: tuple[SavedCondition, ...] = Field(min_length=1, max_length=100)
    universe: BoundedResearchUniverse
    quotes: tuple[QuoteResearchDataset, ...] = Field(default=(), max_length=25)


class SessionPreparationToolRequest(_StrictToolModel):
    analysis_kind: Literal["session_cockpit"] = "session_cockpit"
    universe: BoundedResearchUniverse
    quotes: tuple[QuoteResearchDataset, ...] = Field(default=(), max_length=25)
    periods_per_year: float = Field(default=252.0, gt=0, allow_inf_nan=False)


type MarketToolRequest = Annotated[
    BoundedMarketToolRequest
    | MarketDepthToolRequest
    | WrapperComparisonToolRequest
    | SavedConditionsToolRequest
    | SessionPreparationToolRequest,
    Field(discriminator="analysis_kind"),
]


class InstrumentPriceToolRequest(_StrictToolModel):
    analysis_kind: Literal["price"] = "price"
    dataset: PriceSeriesDataset
    rolling_window: int = Field(default=20, ge=2)
    periods_per_year: float = Field(default=252.0, gt=0, allow_inf_nan=False)
    requested_return: Literal["price_return", "adjusted_price_return", "total_return"] = (
        "price_return"
    )


class InstrumentQuoteToolRequest(_StrictToolModel):
    analysis_kind: Literal["quote"] = "quote"
    dataset: QuoteResearchDataset


class InstrumentDossierToolRequest(_StrictToolModel):
    analysis_kind: Literal["dossier"] = "dossier"
    instrument: ResolvedInstrument
    price_dataset: PriceSeriesDataset
    quote_dataset: QuoteResearchDataset | None = None
    rolling_window: int = Field(default=20, ge=2)
    periods_per_year: float = Field(default=252.0, gt=0, allow_inf_nan=False)


type InstrumentToolRequest = Annotated[
    InstrumentPriceToolRequest | InstrumentQuoteToolRequest | InstrumentDossierToolRequest,
    Field(discriminator="analysis_kind"),
]


class PortfolioTruthToolRequest(_StrictToolModel):
    analysis_kind: Literal["performance"] = "performance"
    dataset: PortfolioPeriodDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class MultiAccountPortfolioToolRequest(_StrictToolModel):
    analysis_kind: Literal["multi_account"] = "multi_account"
    datasets: tuple[PortfolioPeriodDataset, ...] = Field(min_length=1, max_length=25)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class TaxLotToolRequest(_StrictToolModel):
    analysis_kind: Literal["tax_lot_export"] = "tax_lot_export"
    dataset: PortfolioPeriodDataset


class AttributionToolRequest(_StrictToolModel):
    analysis_kind: Literal["attribution"] = "attribution"
    dataset: AttributionDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class ExposureToolRequest(_StrictToolModel):
    analysis_kind: Literal["exposure"] = "exposure"
    dataset: ExposureDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class IncomeToolRequest(_StrictToolModel):
    analysis_kind: Literal["income"] = "income"
    dataset: IncomeDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class CorporateActionToolRequest(_StrictToolModel):
    analysis_kind: Literal["corporate_action"] = "corporate_action"
    dataset: CorporateActionClaimDataset


class LiquidityToolRequest(_StrictToolModel):
    analysis_kind: Literal["cash_and_settlement"] = "cash_and_settlement"
    dataset: LiquidityDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class CostToolRequest(_StrictToolModel):
    analysis_kind: Literal["costs"] = "costs"
    dataset: CostDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class TradeReviewToolRequest(_StrictToolModel):
    analysis_kind: Literal["trade_review"] = "trade_review"
    dataset: TradeReviewDataset
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class PortfolioIntentToolRequest(_StrictToolModel):
    analysis_kind: Literal["query"] = "query"
    intent: PortfolioQueryIntent


class PortfolioQueryCatalogToolRequest(_StrictToolModel):
    analysis_kind: Literal["query_catalog"] = "query_catalog"


type PortfolioToolRequest = Annotated[
    PortfolioTruthToolRequest
    | MultiAccountPortfolioToolRequest
    | TaxLotToolRequest
    | AttributionToolRequest
    | ExposureToolRequest
    | IncomeToolRequest
    | CorporateActionToolRequest
    | LiquidityToolRequest
    | CostToolRequest
    | TradeReviewToolRequest
    | PortfolioIntentToolRequest
    | PortfolioQueryCatalogToolRequest,
    Field(discriminator="analysis_kind"),
]


class OptionModelToolRequest(_StrictToolModel):
    analysis_kind: Literal["option_model"] = "option_model"
    dataset: DerivativeDataset
    model_input: OptionModelInput
    saxo_greeks: SaxoGreekSnapshot | None = None
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class OptionStrategyToolRequest(_StrictToolModel):
    analysis_kind: Literal["option_strategy"] = "option_strategy"
    dataset: DerivativeDataset
    request: OptionStrategyRequest
    expiry_reference_prices: tuple[float, ...] = Field(min_length=1, max_length=500)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class IvSurfaceToolRequest(_StrictToolModel):
    analysis_kind: Literal["iv_surface"] = "iv_surface"
    dataset: DerivativeDataset
    request: IvSurfaceRequest
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class LifecycleRadarToolRequest(_StrictToolModel):
    analysis_kind: Literal["lifecycle_radar"] = "lifecycle_radar"
    dataset: DerivativeDataset
    request: LifecycleRadarRequest
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class FuturesCurveToolRequest(_StrictToolModel):
    analysis_kind: Literal["futures_curve"] = "futures_curve"
    dataset: DerivativeDataset
    request: FuturesCurveRequest
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class FxForwardToolRequest(_StrictToolModel):
    analysis_kind: Literal["fx_forward"] = "fx_forward"
    dataset: DerivativeDataset
    request: FxForwardRequest
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


type DerivativesToolRequest = Annotated[
    OptionModelToolRequest
    | OptionStrategyToolRequest
    | IvSurfaceToolRequest
    | LifecycleRadarToolRequest
    | FuturesCurveToolRequest
    | FxForwardToolRequest,
    Field(discriminator="analysis_kind"),
]


def saxo_analytics_capabilities() -> AnalyticsToolResponse:
    """Return local installed capabilities and proof maturity without source access."""
    tool = "saxo_analytics_capabilities"
    try:
        config = _analytics_config()
        definitions = load_metric_definition_catalog()
        catalog = load_proof_profile_catalog(definitions=definitions)
    except (AnalyticsConfigError, OSError, ProofProfileError, ValueError) as error:
        return _known_failure(tool, error, next_tool=tool)
    profiles = tuple(
        {
            "analysis_kind": profile.analysis_kind,
            "proof_profile_id": profile.proof_profile_id,
            "activation_state": profile.activation_state.value,
            "reason_code": profile.quarantine_reason,
        }
        for profile in catalog.profiles
    )
    return _response(
        tool,
        {
            "source_scope": "saxo_openapi",
            "analytics_tool_ids": ANALYTICS_TOOL_IDS,
            "installed_modules": (
                "discovery_and_data",
                "market_and_instrument_research",
                "portfolio_research",
                "decision_models",
                "derivatives_and_strategy_research",
                "artifacts_and_explanation",
                "bounded_jobs_and_storage",
            ),
            "source_data_groups": (
                "instrument_reference",
                "price_bars",
                "quotes",
                "portfolio_snapshots",
                "transactions_and_closed_positions",
                "costs",
                "corporate_actions",
                "options_and_derivatives",
            ),
            "entitlement_state": "not_checked",
            "dataset_coverage_next_tool": "saxo_list_analytics_storage",
            "render_formats": ("png", "html"),
            "export_formats": ("csv", "parquet", "json", "html", "pdf"),
            "limits": config.limits.model_dump(mode="json"),
            "proof_catalog_version": catalog.catalog_version,
            "proof_profiles": profiles,
            "quarantined_analysis_kinds": tuple(
                profile.analysis_kind
                for profile in catalog.profiles
                if profile.activation_state.value == "quarantined"
            ),
            "background_collector": False,
            "broker_write_authority": False,
            "disclaimer_response_available": False,
        },
        status="passed",
        next_tool="saxo_resolve_research_universe",
        next_action="Resolve a bounded Saxo research universe before source synchronization.",
        network_call_made=False,
        local_state_changed=False,
    )


async def saxo_resolve_research_universe(
    query: str,
    asset_types: tuple[str, ...] = (),
    exchanges: tuple[str, ...] = (),
) -> AnalyticsToolResponse:
    """Resolve a natural-language query into safe handles and explicit ambiguity."""
    tool = "saxo_resolve_research_universe"
    try:
        resolver = InstrumentResolver(SaxoAnalyticsProvider(), _analytics_config())
        result = await resolver.resolve_instruments(query, asset_types, exchanges)
    except (
        AnalyticsConfigError,
        ResolutionError,
        SourceProviderError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool="saxo_analytics_capabilities")
    if result.status is ResolutionStatus.AMBIGUOUS:
        return _response(
            tool,
            result,
            status="ambiguous",
            next_tool=tool,
            next_action="Retry with an explicit asset type or exchange from the returned matches.",
            network_call_made=True,
            local_state_changed=True,
        )
    if result.status is ResolutionStatus.UNAVAILABLE:
        return _response(
            tool,
            result,
            status="unavailable",
            next_tool=tool,
            next_action="Refine the Saxo instrument query; do not substitute another provider.",
            network_call_made=True,
            local_state_changed=False,
        )
    return _response(
        tool,
        result,
        status="resolved",
        next_tool="saxo_manage_research_universe",
        next_action="Save the selected safe handles or synchronize them directly.",
        network_call_made=True,
        local_state_changed=True,
    )


def saxo_manage_research_universe(  # noqa: PLR0913 - exact bounded transition fields
    action: ManageUniverseAction,
    name: str | None = None,
    handles: tuple[InstrumentHandle, ...] = (),
    universe_id: str | None = None,
    additions: tuple[InstrumentHandle, ...] = (),
    removals: tuple[InstrumentHandle, ...] = (),
    expected_revision: str | None = None,
) -> AnalyticsToolResponse:
    """Apply one exact owner-local universe transition."""
    tool = "saxo_manage_research_universe"
    missing = _universe_missing_field(action, name, universe_id, expected_revision)
    if missing is not None:
        return _refusal(
            tool,
            "universe_request_incomplete",
            f"The {missing} field is required for this universe action.",
            next_tool=tool,
            next_action="Retry the same action with its required revision-bound fields.",
        )
    try:
        universes = ResearchUniverseStore(_analytics_config())
        if action == "create":
            result: object = universes.create_universe(cast("str", name), handles)
        elif action == "list":
            result = {"universes": universes.list_universes()}
        elif action == "update":
            result = universes.update_universe(
                cast("str", universe_id),
                additions,
                removals,
                cast("str", expected_revision),
            )
        else:
            universes.delete_universe(
                cast("str", universe_id),
                cast("str", expected_revision),
            )
            result = {"deleted": True, "universe_id": universe_id}
    except (
        AnalyticsConfigError,
        UniverseConflictError,
        UniverseNotFoundError,
        UniverseValidationError,
        UniverseError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool=tool)
    return _response(
        tool,
        result,
        status="passed",
        next_tool="saxo_sync_research_data",
        next_action="Synchronize the selected safe handles when current Saxo data is required.",
        network_call_made=False,
        local_state_changed=action != "list",
    )


async def saxo_sync_research_data(request: SyncResearchRequest) -> AnalyticsToolResponse:
    """Run one bounded source sync through the existing Saxo-only provider."""
    tool = "saxo_sync_research_data"
    try:
        result = await sync_research_data(
            request,
            provider=SaxoAnalyticsProvider(),
            config=_analytics_config(),
        )
    except (AnalyticsConfigError, SourceProviderError, SyncError, StoreError, OSError) as error:
        next_tool = "saxo_manage_analysis_job" if isinstance(error, SyncLimitError) else tool
        return _known_failure(tool, error, next_tool=next_tool)
    return _response(
        tool,
        result,
        next_tool="saxo_get_research_dataset",
        next_action="Inspect dataset lineage and quality before calculation.",
        network_call_made=result.source_request_count > 0,
        local_state_changed=True,
    )


def saxo_get_research_dataset(
    dataset_id: DatasetId,
    page: int = 1,
    limit: int = 100,
) -> AnalyticsToolResponse:
    """Read one bounded local dataset page by opaque handle."""
    tool = "saxo_get_research_dataset"
    try:
        result = get_dataset(dataset_id, page, limit, config=_analytics_config())
    except (AnalyticsConfigError, DatasetNotFoundError, SyncError, StoreError, OSError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _response(
        tool,
        result,
        status="passed",
        next_tool="saxo_analyze_instruments",
        next_action="Use a typed analysis tool that matches this dataset kind.",
        network_call_made=False,
        local_state_changed=False,
    )


def saxo_analyze_market(request: MarketToolRequest) -> AnalyticsToolResponse:
    """Dispatch one typed market request without duplicating domain calculations."""
    tool = "saxo_analyze_market"
    try:
        if isinstance(request, BoundedMarketToolRequest):
            result = analyze_bounded_market(
                request.universe,
                periods_per_year=request.periods_per_year,
            )
        elif isinstance(request, MarketDepthToolRequest):
            result = analyze_entitled_depth(request.dataset)
        elif isinstance(request, WrapperComparisonToolRequest):
            result = compare_wrappers(request.datasets)
        elif isinstance(request, SavedConditionsToolRequest):
            result = check_saved_conditions(
                request.conditions,
                request.universe,
                quotes=request.quotes,
            )
        else:
            result = prepare_bounded_session(
                request.universe,
                quotes=request.quotes,
                periods_per_year=request.periods_per_year,
            )
    except (ArithmeticError, PortfolioAnalyticsError, ValueError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _domain_response(tool, result, next_tool="saxo_render_analysis")


def saxo_analyze_instruments(request: InstrumentToolRequest) -> AnalyticsToolResponse:
    """Dispatch one typed instrument request to the existing domain services."""
    tool = "saxo_analyze_instruments"
    try:
        if isinstance(request, InstrumentPriceToolRequest):
            result = analyze_instrument_prices(
                request.dataset,
                rolling_window=request.rolling_window,
                periods_per_year=request.periods_per_year,
                requested_return=request.requested_return,
            )
        elif isinstance(request, InstrumentQuoteToolRequest):
            result = analyze_quote(request.dataset)
        else:
            result = build_instrument_dossier(
                request.instrument,
                request.price_dataset,
                quote_dataset=request.quote_dataset,
                rolling_window=request.rolling_window,
                periods_per_year=request.periods_per_year,
            )
    except (ArithmeticError, ValueError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _domain_response(tool, result, next_tool="saxo_render_analysis")


def saxo_analyze_portfolio(  # noqa: C901, PLR0912 - typed domain dispatch
    request: PortfolioToolRequest,
) -> AnalyticsToolResponse:
    """Dispatch one typed portfolio request to existing accounting domain services."""
    tool = "saxo_analyze_portfolio"
    delivery = _portfolio_request_delivery(request)
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    visibility, trusted = delivery
    try:
        if isinstance(request, PortfolioTruthToolRequest):
            result = analyze_portfolio_truth(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, MultiAccountPortfolioToolRequest):
            result = analyze_multi_account_portfolios(
                request.datasets,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, TaxLotToolRequest):
            result = authoritative_tax_lot_export(request.dataset)
        elif isinstance(request, AttributionToolRequest):
            result = analyze_portfolio_attribution(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, ExposureToolRequest):
            result = analyze_portfolio_exposure(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, IncomeToolRequest):
            result = analyze_income(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, CorporateActionToolRequest):
            result = authoritative_corporate_action_claim(request.dataset)
        elif isinstance(request, LiquidityToolRequest):
            result = analyze_cash_and_settlement(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, CostToolRequest):
            result = analyze_cost_xray(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, TradeReviewToolRequest):
            result = analyze_trading_mirror(
                request.dataset,
                visibility=visibility,
                trusted_local_host=trusted,
            )
        elif isinstance(request, PortfolioIntentToolRequest):
            result = parse_portfolio_query(request.intent.model_dump(mode="python"))
        else:
            result = portfolio_query_catalog()
    except (ArithmeticError, PortfolioAnalyticsError, PortfolioQueryError, ValueError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _domain_response(tool, result, next_tool="saxo_render_analysis")


def saxo_size_position(
    request: PositionSizingRequest,
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY,
) -> AnalyticsToolResponse:
    """Call the explicit-risk sizing service with server-derived delivery trust."""
    tool = "saxo_size_position"
    delivery = _delivery_context(tool, VisibilityMode(visibility))
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    try:
        result = size_position(
            request,
            visibility=VisibilityMode(visibility),
            trusted_local_host=delivery,
        )
    except (ArithmeticError, PortfolioAnalyticsError, ValueError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _domain_response(tool, result, next_tool="saxo_run_scenario")


def saxo_run_scenario(
    request: PortfolioScenarioRequest,
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY,
) -> AnalyticsToolResponse:
    """Call the explicit numerical scenario service only."""
    tool = "saxo_run_scenario"
    delivery = _delivery_context(tool, VisibilityMode(visibility))
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    try:
        result = run_portfolio_scenario(
            request,
            visibility=VisibilityMode(visibility),
            trusted_local_host=delivery,
        )
    except (ArithmeticError, PortfolioAnalyticsError, ValueError) as error:
        return _known_failure(tool, error, next_tool=tool)
    return _domain_response(tool, result, next_tool="saxo_render_analysis")


def saxo_optimize_portfolio(
    request: OptimizationRequest,
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY,
) -> AnalyticsToolResponse:
    """Call the bounded optimizer without adding objective or risk choices."""
    tool = "saxo_optimize_portfolio"
    delivery = _delivery_context(tool, VisibilityMode(visibility))
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    try:
        result = optimize_portfolio(
            request,
            visibility=VisibilityMode(visibility),
            trusted_local_host=delivery,
        )
    except (ArithmeticError, PortfolioAnalyticsError, ValueError) as error:
        return _known_failure(tool, error, next_tool=tool)
    return _domain_response(tool, result, next_tool="saxo_render_analysis")


def saxo_model_derivatives(request: DerivativesToolRequest) -> AnalyticsToolResponse:
    """Dispatch one explicitly supported derivative model."""
    tool = "saxo_model_derivatives"
    delivery = _delivery_context(tool, VisibilityMode(request.visibility))
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    visibility = VisibilityMode(request.visibility)
    try:
        if isinstance(request, OptionModelToolRequest):
            result = analyze_option_model(
                request.dataset,
                request.model_input,
                visibility=visibility,
                trusted_local_host=delivery,
                saxo_greeks=request.saxo_greeks,
            )
        elif isinstance(request, OptionStrategyToolRequest):
            result = analyze_option_strategy(
                request.dataset,
                request.request,
                expiry_reference_prices=request.expiry_reference_prices,
                visibility=visibility,
                trusted_local_host=delivery,
            )
        elif isinstance(request, IvSurfaceToolRequest):
            result = analyze_iv_surface(
                request.dataset,
                request.request,
                visibility=visibility,
                trusted_local_host=delivery,
            )
        elif isinstance(request, LifecycleRadarToolRequest):
            result = analyze_lifecycle_radar(
                request.dataset,
                request.request,
                visibility=visibility,
                trusted_local_host=delivery,
            )
        elif isinstance(request, FuturesCurveToolRequest):
            result = analyze_futures_curve(
                request.dataset,
                request.request,
                visibility=visibility,
                trusted_local_host=delivery,
            )
        else:
            result = analyze_fx_forward(
                request.dataset,
                request.request,
                visibility=visibility,
                trusted_local_host=delivery,
            )
    except (ArithmeticError, PortfolioAnalyticsError, ValueError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _domain_response(tool, result, next_tool="saxo_render_analysis")


def saxo_backtest_strategy(
    request: BacktestRequest,
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY,
) -> AnalyticsToolResponse:
    """Call the bounded declarative research engine without execution authority."""
    tool = "saxo_backtest_strategy"
    delivery = _delivery_context(tool, VisibilityMode(visibility))
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    try:
        result = run_backtest(
            request,
            visibility=VisibilityMode(visibility),
            trusted_local_host=delivery,
        )
    except (ArithmeticError, PortfolioAnalyticsError, ValueError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _domain_response(tool, result, next_tool="saxo_explain_analysis")


def saxo_propose_trade_from_analysis(
    analysis_id: AnalysisId,
    proposal: TradeProposal,
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY,
) -> AnalyticsToolResponse:
    """Return only the existing non-authorizing typed precheck input."""
    tool = "saxo_propose_trade_from_analysis"
    delivery = _delivery_context(tool, VisibilityMode(visibility))
    if isinstance(delivery, AnalyticsToolResponse):
        return delivery
    try:
        config = _analytics_config()
        replay_analysis(analysis_id, config=config, registry=_proof_registry(config))
        result = build_pretrade_impact(
            analysis_id,
            proposal,
            visibility=VisibilityMode(visibility),
            trusted_local_host=delivery,
        )
    except (
        AnalysisReplayRefused,
        AnalyticsConfigError,
        PortfolioAnalyticsError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool="saxo_explain_analysis")
    return _domain_response(tool, result, next_tool="saxo_create_order_preview")


def saxo_render_analysis(
    analysis_id: AnalysisId,
    template_id: str,
    output_format: Literal["png", "html"] = "png",
    width: int = 1200,
    height: int = 675,
) -> ToolResult:
    """Issue a server-owned binding and render one proof-replayed stored analysis."""
    tool = "saxo_render_analysis"
    store: AnalyticsStore | None = None
    try:
        config = _analytics_config()
        store = AnalyticsStore.open(config)
        bindings = ArtifactBindingRegistry(
            config=config,
            proof_registry=_proof_registry(config),
        )
        issued = bindings.issue(analysis_id)
        delivery = render_analysis(
            StoredRenderRequest(
                binding_id=issued.binding_id,
                template_id=template_id,
                output_format="png" if output_format == "png" else "plotly_html",
                width=width,
                height=height,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
        return _artifact_result(tool, delivery)
    except (
        AnalysisReplayRefused,
        AnalyticsConfigError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _tool_result(_known_failure(tool, error, next_tool="saxo_explain_analysis"))
    finally:
        if store is not None:
            store.close()


def saxo_export_analysis(
    analysis_id: AnalysisId,
    export_kind: ExportKind,
    output_format: AnalyticsExportFormat,
    template_id: str | None = None,
    viewport_width: int = 1280,
) -> ToolResult:
    """Issue a server-owned binding and export exact proof-replayed values."""
    tool = "saxo_export_analysis"
    if export_kind == "table" and output_format not in {"csv", "parquet", "json", "html"}:
        return _tool_result(
            _refusal(
                tool,
                "export_format_unsupported",
                "Table exports support CSV, Parquet, JSON, or HTML.",
                next_tool=tool,
                next_action="Retry with a supported table format.",
            ),
        )
    if export_kind == "report" and (output_format not in {"html", "pdf"} or template_id is None):
        return _tool_result(
            _refusal(
                tool,
                "export_request_incomplete",
                "Report export requires an approved template and HTML or PDF format.",
                next_tool=tool,
                next_action="Retry with template_id and a supported report format.",
            ),
        )
    store: AnalyticsStore | None = None
    try:
        config = _analytics_config()
        store = AnalyticsStore.open(config)
        bindings = ArtifactBindingRegistry(
            config=config,
            proof_registry=_proof_registry(config),
        )
        issued = bindings.issue(analysis_id)
        if export_kind == "table":
            request = StoredTableExportRequest(
                binding_id=issued.binding_id,
                output_format=cast("Literal['csv', 'parquet', 'json', 'html']", output_format),
            )
        else:
            request = StoredReportExportRequest(
                binding_id=issued.binding_id,
                template_id=cast("str", template_id),
                output_format=cast("Literal['html', 'pdf']", output_format),
                viewport_width=viewport_width,
            )
        return _artifact_result(
            tool,
            export_analysis(
                request,
                config=config,
                store=store,
                bindings=bindings,
            ),
        )
    except (
        AnalysisReplayRefused,
        AnalyticsConfigError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _tool_result(_known_failure(tool, error, next_tool="saxo_explain_analysis"))
    finally:
        if store is not None:
            store.close()


def saxo_explain_analysis(analysis_id: AnalysisId) -> AnalyticsToolResponse:
    """Replay one verified result and attach its checked-in metric definitions."""
    tool = "saxo_explain_analysis"
    try:
        config = _analytics_config()
        definitions = load_metric_definition_catalog()
        registry = ProofRegistry(
            definitions=definitions,
            catalog=load_proof_profile_catalog(definitions=definitions),
            config=config,
        )
        result = replay_analysis(analysis_id, config=config, registry=registry)
        by_id = definitions.by_id()
        metric_definitions = tuple(
            by_id[metric.metric_id] for metric in result.metrics if metric.metric_id in by_id
        )
        profile = registry.profile(result.analysis_kind)
        explanation = {
            "analysis": result,
            "metric_definitions": metric_definitions,
            "proof_profile": profile,
            "replay_verified": True,
        }
    except (
        AnalysisReplayRefused,
        AnalyticsConfigError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return _response(
        tool,
        explanation,
        status="passed",
        next_tool="saxo_render_analysis",
        next_action="Render or export only if the stored visibility permits owner delivery.",
        network_call_made=False,
        local_state_changed=False,
    )


async def saxo_manage_analysis_job(
    action: ManageJobAction,
    request: JobRequest | None = None,
    job_id: JobId | None = None,
) -> AnalyticsToolResponse:
    """Apply one bounded in-process job transition."""
    tool = "saxo_manage_analysis_job"
    if action == "start" and request is None:
        return _job_field_refusal("job_request_required", "A typed job request is required.")
    if action in {"check", "cancel"} and job_id is None:
        return _job_field_refusal("job_id_required", "A safe job handle is required.")
    try:
        manager = _job_manager_for_config(_analytics_config())
        if action == "start":
            status = await manager.start_job(cast("JobRequest", request))
        elif action == "check":
            status = await manager.get_job(cast("str", job_id))
        else:
            status = await manager.cancel_job(cast("str", job_id))
    except (
        AnalyticsConfigError,
        JobCapacityError,
        JobNotFoundError,
        JobStateError,
        JobError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool=tool)
    next_tool, next_action = _job_next_action(
        status.state,
        restart_allowed=status.restart_allowed,
    )
    return _response(
        tool,
        status,
        status=status.state,
        next_tool=next_tool,
        next_action=next_action,
        network_call_made=False,
        local_state_changed=True,
    )


def saxo_list_analytics_storage(scope: StorageScope) -> AnalyticsToolResponse:
    """List safe local storage metadata through the local-only boundary service."""
    tool = "saxo_list_analytics_storage"
    store: AnalyticsStore | None = None
    try:
        store = AnalyticsStore.open(_analytics_config())
        result = list_storage(scope, store=store)
    except (
        AnalyticsConfigError,
        StorageBoundaryError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool=tool)
    finally:
        if store is not None:
            store.close()
    return _response(
        tool,
        result,
        status="passed",
        next_tool="saxo_preview_analytics_deletion",
        next_action="Preview an exact scope before any local deletion.",
        network_call_made=False,
        local_state_changed=False,
    )


def saxo_preview_analytics_deletion(scope: StorageScope) -> AnalyticsToolResponse:
    """Preview an exact local dependency closure and issue one bounded token."""
    tool = "saxo_preview_analytics_deletion"
    store: AnalyticsStore | None = None
    try:
        store = AnalyticsStore.open(_analytics_config())
        result = preview_deletion(scope, store=store)
    except (
        AnalyticsConfigError,
        StorageBoundaryError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool="saxo_list_analytics_storage")
    finally:
        if store is not None:
            store.close()
    return _response(
        tool,
        result,
        status="preview_ready",
        next_tool="saxo_delete_analytics_data",
        next_action="Use the returned token once before it expires, or preview again.",
        network_call_made=False,
        local_state_changed=True,
    )


def saxo_delete_analytics_data(token: str) -> AnalyticsToolResponse:
    """Consume one revision-bound token through the local-only deletion service."""
    tool = "saxo_delete_analytics_data"
    store: AnalyticsStore | None = None
    try:
        store = AnalyticsStore.open(_analytics_config())
        result = delete_analytics_data(token, store=store)
    except (
        AnalyticsConfigError,
        DeletionTokenError,
        StorageBoundaryError,
        StoreBusyError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool="saxo_preview_analytics_deletion")
    finally:
        if store is not None:
            store.close()
    return _response(
        tool,
        result,
        status="deleted",
        next_tool="saxo_list_analytics_storage",
        next_action="List local storage to inspect the remaining value-free metadata.",
        network_call_made=False,
        local_state_changed=True,
    )


def _domain_response(
    tool: str,
    result: object,
    *,
    next_tool: str,
) -> AnalyticsToolResponse:
    if isinstance(result, ResearchRefusal):
        return _response(
            tool,
            result,
            status="refused",
            reason_code=result.reason_code,
            message=result.reason,
            next_tool=_refusal_next_tool(result.reason_code, next_tool),
            next_action=_refusal_next_action(result.reason_code),
            network_call_made=False,
            local_state_changed=False,
        )
    return _response(
        tool,
        result,
        next_tool=next_tool,
        next_action=f"Continue with {next_tool} only if that output is needed.",
        network_call_made=False,
        local_state_changed=False,
    )


def _response(  # noqa: PLR0913 - shared structured response fields
    tool: str,
    result: object,
    *,
    status: str | None = None,
    reason_code: str | None = None,
    message: str | None = None,
    next_tool: str | None,
    next_action: str | None,
    network_call_made: bool | None,
    local_state_changed: bool,
) -> AnalyticsToolResponse:
    payload = _payload(result)
    resolved_status = status or _status_value(getattr(result, "status", None)) or "passed"
    return AnalyticsToolResponse(
        status=resolved_status,
        tool_name=tool,
        result=payload,
        reason_code=reason_code,
        message=message,
        warnings=_warning_codes(result),
        next_tool=next_tool,
        next_action=next_action,
        network_call_made=network_call_made,
        local_state_changed=local_state_changed,
    )


def _refusal(
    tool: str,
    reason_code: str,
    message: str,
    *,
    next_tool: str,
    next_action: str,
) -> AnalyticsToolResponse:
    return AnalyticsToolResponse(
        status="refused",
        tool_name=tool,
        reason_code=reason_code,
        message=message,
        next_tool=next_tool,
        next_action=next_action,
        network_call_made=None,
        local_state_changed=False,
    )


def _known_failure(
    tool: str,
    error: Exception,
    *,
    next_tool: str,
) -> AnalyticsToolResponse:
    reason_code, message = _known_failure_details(error)
    return _refusal(
        tool,
        reason_code,
        message,
        next_tool=next_tool,
        next_action=f"Call {next_tool} and follow its value-free recovery fields.",
    )


_KNOWN_FAILURE_DETAILS: Final[tuple[tuple[type[Exception], str, str], ...]] = (
    (
        ResolutionError,
        "instrument_resolution_failed",
        "Instrument resolution could not complete safely.",
    ),
    (SyncLimitError, "bounded_job_required", "The request exceeds the synchronous limit."),
    (DatasetNotFoundError, "dataset_not_found", "The safe dataset handle is unavailable."),
    (
        StoreQuotaError,
        "analytics_store_quota_exceeded",
        "The owner-only analytics quota is exhausted.",
    ),
    (
        DeletionTokenError,
        "deletion_preview_invalid",
        "The deletion token is invalid, expired, or stale.",
    ),
    (StoreBusyError, "analytics_store_busy", "The owner-only analytics writer is busy."),
    (
        StoreNotFoundError,
        "analytics_object_not_found",
        "The requested safe analytics handle was not found.",
    ),
    (
        StoreValidationError,
        "analytics_storage_request_invalid",
        "The local storage request is invalid.",
    ),
    (
        UniverseConflictError,
        "universe_revision_conflict",
        "The universe changed; fetch its current revision.",
    ),
    (
        UniverseNotFoundError,
        "universe_not_found",
        "The safe universe handle is unavailable or expired.",
    ),
    (
        UniverseValidationError,
        "universe_request_invalid",
        "The universe request does not satisfy its contract.",
    ),
    (JobCapacityError, "analytics_job_capacity_reached", "Four analytics jobs are active."),
    (
        JobNotFoundError,
        "analytics_job_not_found",
        "The safe job handle is unavailable or expired.",
    ),
    (
        JobStateError,
        "analytics_job_transition_refused",
        "The requested job transition is unavailable.",
    ),
    (
        AnalysisReplayRefused,
        "analysis_replay_refused",
        "The stored analysis is stale, invalidated, or unverified.",
    ),
    (
        PortfolioAnalyticsError,
        "private_delivery_refused",
        "The requested private delivery boundary is unavailable.",
    ),
    (
        ProofProfileError,
        "proof_profile_unavailable",
        "The checked-in analytics proof profile is unavailable.",
    ),
    (
        AnalyticsConfigError,
        "analytics_configuration_refused",
        "The owner-only analytics configuration is unsafe.",
    ),
    (
        StorageBoundaryError,
        "local_storage_boundary_failed",
        "The local-only operation observed an unsafe request.",
    ),
    (
        OSError,
        "analytics_local_state_unavailable",
        "Owner-only local analytics state is unavailable.",
    ),
    (
        JobError,
        "analytics_operation_refused",
        "The analytics domain service refused the operation.",
    ),
    (
        StoreError,
        "analytics_operation_refused",
        "The analytics domain service refused the operation.",
    ),
    (
        SyncError,
        "analytics_operation_refused",
        "The analytics domain service refused the operation.",
    ),
    (
        UniverseError,
        "analytics_operation_refused",
        "The analytics domain service refused the operation.",
    ),
    (
        PortfolioQueryError,
        "analytics_operation_refused",
        "The analytics domain service refused the operation.",
    ),
)


def _known_failure_details(error: Exception) -> tuple[str, str]:
    if isinstance(error, SourceProviderError):
        code = error.code if _SAFE_CODE.fullmatch(error.code) else "source_read_failed"
        return code, "The registered Saxo source read could not complete safely."
    for error_type, reason_code, message in _KNOWN_FAILURE_DETAILS:
        if isinstance(error, error_type):
            return reason_code, message
    return "analytics_request_invalid", "The typed analytics request is invalid."


def _delivery_context(
    tool: str,
    visibility: VisibilityMode,
) -> bool | AnalyticsToolResponse:
    allowed = {
        VisibilityMode.PUBLIC_EVIDENCE,
        VisibilityMode.FINGERPRINT_ONLY,
        VisibilityMode.REDACTED_PREVIEW,
        VisibilityMode.PRIVATE_USER_RESULT,
    }
    if visibility not in allowed:
        return _refusal(
            tool,
            "analytics_visibility_unsupported",
            "Use public evidence, fingerprint-only, redacted preview, or private owner output.",
            next_tool="saxo_export_analysis",
            next_action="Use bound export for owner-only resource-link delivery.",
        )
    if visibility is not VisibilityMode.PRIVATE_USER_RESULT:
        return False
    environment = os.environ.get("SAXO_MCP_ENVIRONMENT", "SIM").strip().upper()
    if environment == "SIM":
        return True
    return _refusal(
        tool,
        "inline_private_not_enabled",
        "Private LIVE values require owner-only bound artifact delivery.",
        next_tool="saxo_export_analysis",
        next_action="Persist and export a verified analysis through owner-only bound delivery.",
    )


def _portfolio_request_delivery(
    request: PortfolioToolRequest,
) -> tuple[VisibilityMode, bool] | AnalyticsToolResponse:
    raw = getattr(request, "visibility", VisibilityMode.FINGERPRINT_ONLY)
    visibility = VisibilityMode(raw)
    trusted = _delivery_context("saxo_analyze_portfolio", visibility)
    if isinstance(trusted, AnalyticsToolResponse):
        return trusted
    return visibility, trusted


def _proof_registry(config: AnalyticsConfig) -> ProofRegistry:
    definitions = load_metric_definition_catalog()
    return ProofRegistry(
        definitions=definitions,
        catalog=load_proof_profile_catalog(definitions=definitions),
        config=config,
    )


def _analytics_config() -> AnalyticsConfig:
    return load_analytics_config(os.environ)


def _job_manager_for_config(config: AnalyticsConfig) -> AnalyticsJobManager:
    global _job_runtime  # noqa: PLW0603
    key = str(config.paths.store_path)
    with _JOB_RUNTIME_LOCK:
        if _job_runtime is not None:
            existing_key, _store, manager = _job_runtime
            if existing_key != key:
                raise JobStateError("analytics job runtime configuration changed")
            return manager
        store = AnalyticsStore.open(config)
        try:
            manager = AnalyticsJobManager(store=store, config=config, handlers={})
        except BaseException:
            store.close()
            raise
        _job_runtime = (key, store, manager)
        return manager


def _job_field_refusal(reason_code: str, message: str) -> AnalyticsToolResponse:
    return _refusal(
        "saxo_manage_analysis_job",
        reason_code,
        message,
        next_tool="saxo_manage_analysis_job",
        next_action="Retry with the exact typed field required by the selected action.",
    )


def _job_next_action(state: str, *, restart_allowed: bool) -> tuple[str, str]:
    if state in {"queued", "running"}:
        return (
            "saxo_manage_analysis_job",
            "Check this job again by its safe handle; partial conclusions remain unavailable.",
        )
    if state == "completed":
        return "saxo_explain_analysis", "Explain or render the completed safe analysis handle."
    if restart_allowed:
        return (
            "saxo_manage_analysis_job",
            "Start the same deterministic request with restart_interrupted enabled.",
        )
    return "saxo_analytics_capabilities", "Inspect the bounded job limitation before retrying."


def _universe_missing_field(
    action: ManageUniverseAction,
    name: str | None,
    universe_id: str | None,
    expected_revision: str | None,
) -> str | None:
    if action == "create" and not name:
        return "name"
    if action in {"update", "delete"} and not universe_id:
        return "universe_id"
    if action in {"update", "delete"} and not expected_revision:
        return "expected_revision"
    return None


def _refusal_next_tool(reason_code: str, default: str) -> str:
    if "source" in reason_code or "quality" in reason_code or "stale" in reason_code:
        return "saxo_sync_research_data"
    if "risk_budget" in reason_code or "shock" in reason_code:
        return default
    if "proof" in reason_code or "replay" in reason_code:
        return "saxo_explain_analysis"
    return default


def _refusal_next_action(reason_code: str) -> str:
    if "source" in reason_code or "quality" in reason_code or "stale" in reason_code:
        return "Synchronize the exact missing or stale Saxo dataset, then retry."
    if "risk_budget" in reason_code:
        return "Supply and confirm an explicit numeric risk budget, then retry."
    if "shock" in reason_code:
        return "Echo and accept the explicit numeric shock map, then retry."
    return "Follow the named next tool without substituting data or calculations."


def _status_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) and _SAFE_CODE.fullmatch(raw) else None


def _warning_codes(value: object) -> tuple[str, ...]:
    warnings: set[str] = set()
    for item in cast("Sequence[object]", getattr(value, "warnings", ())):
        code = getattr(item, "code", item)
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            warnings.add(code)
    quality = getattr(value, "data_quality", None)
    for item in cast("Sequence[object]", getattr(quality, "warnings", ())):
        code = getattr(item, "code", item)
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            warnings.add(code)
    return tuple(sorted(warnings))


def _payload(value: object) -> dict[str, JsonValue]:
    converted = cast("object", to_jsonable_python(value))
    if isinstance(converted, Mapping):
        payload = dict(cast("Mapping[str, object]", converted))
    else:
        payload = {"value": converted}
    return _RESULT_ADAPTER.validate_python(payload)


def _artifact_result(
    tool: str,
    delivery: InlineArtifact | ArtifactResourceLink | ArtifactRefusal,
) -> ToolResult:
    if isinstance(delivery, ArtifactRefusal):
        response = _refusal(
            tool,
            delivery.reason_code,
            delivery.reason,
            next_tool="saxo_explain_analysis",
            next_action=delivery.next_action,
        )
        return _tool_result(response)
    structured = delivery.model_dump(mode="json", exclude={"content"})
    structured.update(
        {
            "tool_name": tool,
            "broker_write_made": False,
            "approval_authority": False,
            "execution_authority": False,
            "disclaimer_response_available": False,
        },
    )
    if isinstance(delivery, ArtifactResourceLink):
        content: list[mt.ContentBlock] = [
            cast(
                "mt.ContentBlock",
                mt.ResourceLink(
                    type="resource_link",
                    name=delivery.artifact_id,
                    uri=AnyUrl(delivery.resource_uri),
                    description="Owner-only Saxo analytics artifact",
                    mimeType=delivery.media_type,
                    size=delivery.byte_count,
                ),
            ),
        ]
    elif delivery.media_type.startswith("image/"):
        content = [
            mt.ImageContent(
                type="image",
                data=base64.b64encode(delivery.content).decode("ascii"),
                mimeType=delivery.media_type,
            ),
        ]
    else:
        content = [
            mt.EmbeddedResource(
                type="resource",
                resource=mt.BlobResourceContents(
                    uri=AnyUrl(f"saxo-analytics://inline/{delivery.artifact_id}"),
                    mimeType=delivery.media_type,
                    blob=base64.b64encode(delivery.content).decode("ascii"),
                ),
            ),
        ]
    return ToolResult(content=content, structured_content=structured, is_error=False)


def _tool_result(response: AnalyticsToolResponse) -> ToolResult:
    return ToolResult(
        structured_content=response.model_dump(mode="json"),
        is_error=False,
    )


ANALYTICS_TOOL_FUNCTIONS: Final[tuple[object, ...]] = (
    saxo_analytics_capabilities,
    saxo_resolve_research_universe,
    saxo_manage_research_universe,
    saxo_sync_research_data,
    saxo_get_research_dataset,
    saxo_analyze_market,
    saxo_analyze_instruments,
    saxo_analyze_portfolio,
    saxo_size_position,
    saxo_run_scenario,
    saxo_optimize_portfolio,
    saxo_model_derivatives,
    saxo_backtest_strategy,
    saxo_propose_trade_from_analysis,
    saxo_render_analysis,
    saxo_export_analysis,
    saxo_explain_analysis,
    saxo_manage_analysis_job,
    saxo_list_analytics_storage,
    saxo_preview_analytics_deletion,
    saxo_delete_analytics_data,
)
