"""Thin typed FastMCP adapters over the existing analytics domain services."""

from __future__ import annotations

import asyncio
import base64
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from typing import Annotated, Final, Literal, cast

import mcp.types as mt
from fastmcp.tools import ToolResult
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import to_jsonable_python

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    AnalyticsConfigError,
    load_analytics_config,
)
from saxo_bank_mcp.analytics_export import (
    StoredReportExportRequest,
    StoredTableExportRequest,
    export_analysis,
)
from saxo_bank_mcp.analytics_jobs import (
    AnalyticsJobManager,
    JobCapacityError,
    JobConclusion,
    JobError,
    JobExecutionContext,
    JobHandler,
    JobKind,
    JobNotFoundError,
    JobParameter,
    JobRequest,
    JobStateError,
)
from saxo_bank_mcp.analytics_market import (
    SavedCondition,
)
from saxo_bank_mcp.analytics_metric_definitions import load_metric_definition_catalog
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    AnalysisResult,
    AnalyticsDegradation,
    AnalyticsRefusal,
    DatasetId,
    InstrumentHandle,
    JobId,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioAnalyticsError,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    ProofProfileError,
    ProofRegistry,
    load_proof_profile_catalog,
)
from saxo_bank_mcp.analytics_provenance import AnalysisReplayRefused, replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider, SourceProviderError
from saxo_bank_mcp.analytics_query import (
    BreakdownDimension,
    BreakdownMetric,
    PortfolioAnalysisKind,
    PortfolioCapability,
    PortfolioEventType,
    PortfolioMetric,
    PortfolioQueryError,
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
)
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
from saxo_bank_mcp.analytics_strategy_schema import StrategyDefinition
from saxo_bank_mcp.analytics_sync import (
    DatasetNotFoundError,
    SyncError,
    SyncLimitError,
    SyncResearchRequest,
    get_dataset,
    sync_research_data,
)
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
_MIN_REPORT_VIEWPORT_WIDTH: Final = 320
_MAX_REPORT_VIEWPORT_WIDTH: Final = 2560
_job_runtime: tuple[str, AnalyticsStore, AnalyticsJobManager] | None = None


class _StrictToolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=False,
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


class VerifiedAnalysisToolResponse(_StrictToolModel):
    status: Literal["verified"] = "verified"
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    analysis_id: AnalysisId
    result: AnalysisResult
    warnings: tuple[str, ...] = ()
    next_tool: Literal["saxo_explain_analysis"] = "saxo_explain_analysis"
    next_action: str = Field(min_length=1, max_length=500)
    network_call_made: Literal[False] = False
    local_state_changed: Literal[True] = True
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class DegradedAnalysisToolResponse(_StrictToolModel):
    status: Literal["degraded"] = "degraded"
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    analysis_id: AnalysisId
    result: AnalyticsDegradation
    warnings: tuple[str, ...]
    next_tool: Literal["saxo_explain_analysis"] = "saxo_explain_analysis"
    next_action: str = Field(min_length=1, max_length=500)
    network_call_made: Literal[False] = False
    local_state_changed: Literal[True] = True
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class RefusedAnalysisToolResponse(_StrictToolModel):
    status: Literal["refused"] = "refused"
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    analysis_id: None = None
    result: AnalyticsRefusal
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    message: str = Field(min_length=1, max_length=500)
    warnings: tuple[str, ...] = ()
    next_tool: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    next_action: str = Field(min_length=1, max_length=500)
    network_call_made: Literal[False] = False
    local_state_changed: Literal[False] = False
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


type CanonicalAnalysisToolResponse = Annotated[
    VerifiedAnalysisToolResponse | DegradedAnalysisToolResponse | RefusedAnalysisToolResponse,
    Field(discriminator="status"),
]


class InlineArtifactToolResponse(_StrictToolModel):
    status: Literal["inline"] = "inline"
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    artifact_id: str = Field(pattern=r"^ar_[0-9a-f]{32}$")
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantics_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    visible_stamps: tuple[str, ...]
    next_tool: Literal["saxo_explain_analysis"] = "saxo_explain_analysis"
    next_action: str = "Use this bounded inline owner result or request another format."
    network_call_made: Literal[False] = False
    local_state_changed: Literal[True] = True
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class ResourceArtifactToolResponse(_StrictToolModel):
    status: Literal["resource_link"] = "resource_link"
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    artifact_id: str = Field(pattern=r"^ar_[0-9a-f]{32}$")
    resource_uri: str = Field(pattern=r"^saxo-analytics://artifacts/ar_[0-9a-f]{32}$")
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantics_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner_only: Literal[True] = True
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    visible_stamps: tuple[str, ...]
    next_tool: Literal["saxo_explain_analysis"] = "saxo_explain_analysis"
    next_action: str = "Read the owner-only resource by its opaque resource link."
    network_call_made: Literal[False] = False
    local_state_changed: Literal[True] = True
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class RefusedArtifactToolResponse(_StrictToolModel):
    status: Literal["refused"] = "refused"
    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    message: str = Field(min_length=1, max_length=500)
    next_tool: Literal["saxo_explain_analysis"] = "saxo_explain_analysis"
    next_action: str = Field(min_length=1, max_length=500)
    network_call_made: Literal[False] = False
    local_state_changed: Literal[False] = False
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


type ArtifactToolResponse = Annotated[
    InlineArtifactToolResponse | ResourceArtifactToolResponse | RefusedArtifactToolResponse,
    Field(discriminator="status"),
]


_CANONICAL_ANALYSIS_OUTPUT_ADAPTER: Final[TypeAdapter[CanonicalAnalysisToolResponse]] = TypeAdapter(
    CanonicalAnalysisToolResponse
)
_ARTIFACT_OUTPUT_ADAPTER: Final[TypeAdapter[ArtifactToolResponse]] = TypeAdapter(
    ArtifactToolResponse
)


class StoredMarketToolRequest(_StrictToolModel):
    analysis_kind: Literal[
        "market_comparison",
        "market_microstructure",
        "wrapper_comparison",
        "saved_condition_checks",
        "session_cockpit",
    ]
    dataset_ids: tuple[DatasetId, ...] = Field(min_length=1, max_length=25)
    periods_per_year: float = Field(default=252.0, gt=0, allow_inf_nan=False)
    conditions: tuple[SavedCondition, ...] = Field(default=(), max_length=100)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class StoredInstrumentToolRequest(_StrictToolModel):
    analysis_kind: Literal[
        "instrument_price_return",
        "instrument_quote",
        "instrument_dossier",
    ]
    dataset_ids: tuple[DatasetId, ...] = Field(min_length=1, max_length=2)
    instrument_handles: tuple[InstrumentHandle, ...] = Field(min_length=1, max_length=25)
    rolling_window: int = Field(default=20, ge=2, le=1000)
    periods_per_year: float = Field(default=252.0, gt=0, allow_inf_nan=False)
    requested_return: Literal["price_return", "adjusted_price_return", "total_return"] = (
        "price_return"
    )
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class StoredMetricQueryChoice(_StrictToolModel):
    intent: Literal["metric"] = "metric"
    metric: PortfolioMetric


class StoredBreakdownQueryChoice(_StrictToolModel):
    intent: Literal["breakdown"] = "breakdown"
    metric: BreakdownMetric
    dimension: BreakdownDimension


class StoredEventQueryChoice(_StrictToolModel):
    intent: Literal["events"] = "events"
    event_type: PortfolioEventType
    instrument_handle: InstrumentHandle | None = None


class StoredCapabilityQueryChoice(_StrictToolModel):
    intent: Literal["capability"] = "capability"
    capability: PortfolioCapability


class StoredAnalysisQueryChoice(_StrictToolModel):
    intent: Literal["analysis"] = "analysis"
    requested_analysis: PortfolioAnalysisKind


type StoredPortfolioQueryChoice = Annotated[
    StoredMetricQueryChoice
    | StoredBreakdownQueryChoice
    | StoredEventQueryChoice
    | StoredCapabilityQueryChoice
    | StoredAnalysisQueryChoice,
    Field(discriminator="intent"),
]


class StoredPortfolioToolRequest(_StrictToolModel):
    analysis_kind: Literal[
        "portfolio_performance",
        "portfolio_comparison",
        "tax_lot_export",
        "portfolio_attribution",
        "portfolio_exposure",
        "income_calendar",
        "corporate_action_center",
        "cash_and_settlement",
        "cost_xray",
        "trading_mirror",
        "portfolio_query",
    ]
    dataset_ids: tuple[DatasetId, ...] = Field(min_length=1, max_length=25)
    query_intent: StoredPortfolioQueryChoice | None = None
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class StoredPositionSizingToolRequest(_StrictToolModel):
    analysis_kind: Literal["position_sizing"] = "position_sizing"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    method: Literal["stop_distance", "volatility"]
    maximum_loss: Decimal = Field(gt=0, allow_inf_nan=False)
    risk_budget_confirmed: bool
    stop_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volatility_multiple: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class ExplicitScenarioShock(_StrictToolModel):
    instrument_handle: InstrumentHandle
    price_shock_ratio: Decimal = Field(ge=-1, allow_inf_nan=False)
    volatility_shock_points: Decimal = Field(default=Decimal(0), allow_inf_nan=False)
    rate_shock_basis_points: Decimal = Field(default=Decimal(0), allow_inf_nan=False)


class StoredScenarioToolRequest(_StrictToolModel):
    analysis_kind: Literal[
        "scenario_historical",
        "scenario_custom",
        "scenario_currency",
        "scenario_volatility",
        "scenario_rate",
        "scenario_margin",
        "scenario_combined",
    ]
    dataset_id: DatasetId
    shocks: tuple[ExplicitScenarioShock, ...] = Field(min_length=1, max_length=100)
    numeric_shocks_echoed_by_caller: bool
    caller_accepted_numeric_shocks: bool
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class StoredOptimizationToolRequest(_StrictToolModel):
    analysis_kind: Literal["portfolio_minimum_variance", "portfolio_risk_parity"]
    dataset_id: DatasetId
    objective: Literal["minimum_variance", "risk_parity"]
    objective_confirmed_by_caller: bool
    constraints_confirmed_by_caller: bool
    short_policy: Literal["long_only", "bounded_short"]
    maximum_turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_transaction_cost_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    maximum_margin_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class StoredDerivativesToolRequest(_StrictToolModel):
    analysis_kind: Literal[
        "derivatives_model",
        "option_payoff",
        "iv_surface",
        "derivatives_scenario",
        "futures_curve",
        "fx_forward_carry",
    ]
    dataset_id: DatasetId
    instrument_handles: tuple[InstrumentHandle, ...] = Field(min_length=1, max_length=25)
    volatility_assumption: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    rate_assumption: Decimal | None = Field(default=None, allow_inf_nan=False)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class StoredBacktestToolRequest(_StrictToolModel):
    analysis_kind: Literal["bounded_backtest"] = "bounded_backtest"
    dataset_id: DatasetId
    instrument_handle: InstrumentHandle
    strategy: StrategyDefinition
    starting_equity: float = Field(gt=0, allow_inf_nan=False)
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY


class AnalyticsJobParameter(_StrictToolModel):
    name: str = Field(min_length=1, max_length=64)
    value: str | int | float | bool | None


class AnalyticsJobToolRequest(_StrictToolModel):
    job_kind: JobKind
    dataset_ids: tuple[DatasetId, ...] = Field(default=(), max_length=100)
    analysis_ids: tuple[AnalysisId, ...] = Field(default=(), max_length=100)
    instrument_handles: tuple[InstrumentHandle, ...] = Field(default=(), max_length=100)
    parameters: tuple[AnalyticsJobParameter, ...] = Field(default=(), max_length=100)
    total_work_units: int = Field(ge=1, le=5_000_000)
    restart_interrupted: bool = False

    def to_domain(self) -> JobRequest:
        return JobRequest(
            job_kind=self.job_kind,
            dataset_ids=self.dataset_ids,
            analysis_ids=self.analysis_ids,
            instrument_handles=self.instrument_handles,
            parameters=tuple(
                JobParameter(name=parameter.name, value=parameter.value)
                for parameter in self.parameters
            ),
            total_work_units=self.total_work_units,
            restart_interrupted=self.restart_interrupted,
        )


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


def saxo_analyze_market(
    request: StoredMarketToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Authenticate stored Saxo material and refuse until its proof profile is active."""
    return _stored_analysis_response(
        "saxo_analyze_market",
        request.analysis_kind,
        request.dataset_ids,
        VisibilityMode(request.visibility),
    )


def saxo_analyze_instruments(
    request: StoredInstrumentToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Accept only opaque stored datasets and bounded calculation parameters."""
    return _stored_analysis_response(
        "saxo_analyze_instruments",
        request.analysis_kind,
        request.dataset_ids,
        VisibilityMode(request.visibility),
    )


def saxo_analyze_portfolio(
    request: StoredPortfolioToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Authenticate stored account datasets without accepting caller-built account facts."""
    return _stored_analysis_response(
        "saxo_analyze_portfolio",
        request.analysis_kind,
        request.dataset_ids,
        VisibilityMode(request.visibility),
    )


def saxo_size_position(
    request: StoredPositionSizingToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Bind caller-selected risk budget to authenticated stored account material."""
    return _stored_analysis_response(
        "saxo_size_position",
        request.analysis_kind,
        (request.dataset_id,),
        VisibilityMode(request.visibility),
    )


def saxo_run_scenario(
    request: StoredScenarioToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Bind explicit accepted shocks to authenticated stored portfolio material."""
    if not request.numeric_shocks_echoed_by_caller or not request.caller_accepted_numeric_shocks:
        return _canonical_analysis_refusal(
            "saxo_run_scenario",
            request.analysis_kind,
            VisibilityMode(request.visibility),
            "numeric_shocks_not_accepted",
            "Explicit numeric shocks must be echoed and accepted before calculation.",
            next_tool="saxo_run_scenario",
            next_action="Echo and accept the exact numeric shock map, then retry.",
        )
    return _stored_analysis_response(
        "saxo_run_scenario",
        request.analysis_kind,
        (request.dataset_id,),
        VisibilityMode(request.visibility),
    )


def saxo_optimize_portfolio(
    request: StoredOptimizationToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Bind caller-selected objective and constraints to authenticated stored inputs."""
    if not request.objective_confirmed_by_caller or not request.constraints_confirmed_by_caller:
        return _canonical_analysis_refusal(
            "saxo_optimize_portfolio",
            request.analysis_kind,
            VisibilityMode(request.visibility),
            "optimizer_choices_not_confirmed",
            "The objective and bounded constraints require explicit caller confirmation.",
            next_tool="saxo_optimize_portfolio",
            next_action="Confirm the exact objective and constraints, then retry.",
        )
    return _stored_analysis_response(
        "saxo_optimize_portfolio",
        request.analysis_kind,
        (request.dataset_id,),
        VisibilityMode(request.visibility),
    )


def saxo_model_derivatives(
    request: StoredDerivativesToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Bind model assumptions to authenticated stored derivative records."""
    return _stored_analysis_response(
        "saxo_model_derivatives",
        request.analysis_kind,
        (request.dataset_id,),
        VisibilityMode(request.visibility),
    )


def saxo_backtest_strategy(
    request: StoredBacktestToolRequest,
) -> CanonicalAnalysisToolResponse:
    """Bind a declarative strategy to authenticated stored bars only."""
    return _stored_analysis_response(
        "saxo_backtest_strategy",
        request.analysis_kind,
        (request.dataset_id,),
        VisibilityMode(request.visibility),
    )


def saxo_propose_trade_from_analysis(  # noqa: PLR0913 - explicit user choices only
    analysis_id: AnalysisId,
    instrument_handle: InstrumentHandle,
    side: Literal["buy", "sell"],
    quantity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)],
    proposal_price: Annotated[
        Decimal | None,
        Field(default=None, gt=0, allow_inf_nan=False),
    ] = None,
    maximum_loss: Annotated[Decimal | None, Field(default=None, gt=0, allow_inf_nan=False)] = None,
    holding_period_days: Annotated[int, Field(default=0, ge=0, le=36500)] = 0,
    visibility: AnalysisVisibility = VisibilityMode.FINGERPRINT_ONLY,
) -> CanonicalAnalysisToolResponse:
    """Replay one stored analysis and accept only explicit user trade choices."""
    del side, quantity, proposal_price, maximum_loss, holding_period_days
    tool = "saxo_propose_trade_from_analysis"
    selected_visibility = VisibilityMode(visibility)
    try:
        config = _analytics_config()
        result = replay_analysis(analysis_id, config=config, registry=_proof_registry(config))
        store = AnalyticsStore.open(config)
        try:
            store.get_authenticated_dataset(result.provenance.dataset_id)
        finally:
            store.close()
    except (
        AnalysisReplayRefused,
        AnalyticsConfigError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ):
        return _canonical_analysis_refusal(
            tool,
            "pretrade_impact",
            selected_visibility,
            "analysis_replay_refused",
            "The stored analysis is stale, unavailable, or no longer proof-verified.",
            next_tool="saxo_explain_analysis",
            next_action="Explain or refresh the stored analysis before any new proposal request.",
        )
    bound_handles = tuple(getattr(result.request, "instrument_handles", ()))
    if not bound_handles or instrument_handle not in bound_handles:
        return _canonical_analysis_refusal(
            tool,
            "pretrade_impact",
            selected_visibility,
            "proposal_context_mismatch",
            "The explicit instrument choice is not bound to the replayed analysis.",
            next_tool="saxo_explain_analysis",
            next_action="Inspect the stored analysis and make a separate explicit matching choice.",
        )
    return _canonical_analysis_refusal(
        tool,
        "pretrade_impact",
        selected_visibility,
        "pretrade_context_unavailable",
        "The replayed analysis lacks a complete current server-owned pretrade context.",
        next_tool="saxo_explain_analysis",
        next_action=(
            "Refresh the required stored account and market sources; a broker preview requires a "
            "separate later user request."
        ),
    )


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
        return _artifact_failure_result(
            tool,
            _known_failure(tool, error, next_tool="saxo_explain_analysis"),
        )
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
        return _artifact_failure_result(
            tool,
            _refusal(
                tool,
                "export_format_unsupported",
                "Table exports support CSV, Parquet, JSON, or HTML.",
                next_tool=tool,
                next_action="Retry with a supported table format.",
            ),
        )
    if export_kind == "report" and (output_format not in {"html", "pdf"} or template_id is None):
        return _artifact_failure_result(
            tool,
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
        return _artifact_failure_result(
            tool,
            _known_failure(tool, error, next_tool="saxo_explain_analysis"),
        )
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
    request: AnalyticsJobToolRequest | None = None,
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
            status = await manager.start_job(cast("AnalyticsJobToolRequest", request).to_domain())
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


def _stored_analysis_response(
    tool: str,
    analysis_kind: str,
    dataset_ids: Sequence[str],
    visibility: VisibilityMode,
) -> CanonicalAnalysisToolResponse:
    """Authenticate exact stored lineage and fail closed before any unproved calculation."""
    if visibility is VisibilityMode.PRIVATE_USER_RESULT and _server_environment() == "LIVE":
        return _canonical_analysis_refusal(
            tool,
            analysis_kind,
            visibility,
            "inline_private_not_enabled",
            "Private LIVE values require owner-only proof-bound artifact delivery.",
            next_tool="saxo_export_analysis",
            next_action="Export a verified stored analysis through owner-only bound delivery.",
        )
    store: AnalyticsStore | None = None
    try:
        config = _analytics_config()
        store = AnalyticsStore.open(config)
        for dataset_id in dataset_ids:
            store.get_authenticated_dataset(dataset_id)
        profile = _proof_registry(config).profile(analysis_kind)
    except (
        AnalyticsConfigError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        reason_code, message = _known_failure_details(error)
        return _canonical_analysis_refusal(
            tool,
            analysis_kind,
            visibility,
            reason_code,
            message,
            next_tool="saxo_sync_research_data",
            next_action=(
                "Synchronize the exact stored source coverage, then retry by its safe handle."
            ),
        )
    finally:
        if store is not None:
            store.close()
    if profile is None:
        return _canonical_analysis_refusal(
            tool,
            analysis_kind,
            visibility,
            "missing_proof_profile",
            "No checked-in proof profile covers this stored analysis kind.",
            next_tool="saxo_analytics_capabilities",
            next_action="Inspect the installed proof state; do not substitute a calculation.",
        )
    if profile.activation_state.value != "active":
        return _canonical_analysis_refusal(
            tool,
            analysis_kind,
            visibility,
            profile.quarantine_reason or "proof_quarantined",
            "The checked-in proof profile is not active, so no analytical claim was produced.",
            next_tool="saxo_analytics_capabilities",
            next_action="Inspect proof maturity and wait for an active checked-in profile.",
        )
    return _canonical_analysis_refusal(
        tool,
        analysis_kind,
        visibility,
        "proof_bound_executor_unavailable",
        "No proof-bound stored-input executor is registered for this exact analysis request.",
        next_tool="saxo_analytics_capabilities",
        next_action=(
            "Use only an installed proof-bound analysis kind; do not provide source values."
        ),
    )


def _canonical_analysis_refusal(  # noqa: PLR0913 - explicit safe recovery envelope
    tool: str,
    analysis_kind: str,
    visibility: VisibilityMode,
    reason_code: str,
    message: str,
    *,
    next_tool: str,
    next_action: str,
) -> RefusedAnalysisToolResponse:
    refusal = AnalyticsRefusal(
        visibility=visibility,
        tool_name=tool,
        analysis_kind=analysis_kind,
        as_of=datetime.now(UTC),
        reason_code=reason_code,
        reason=message,
        next_action=next_action,
        warnings=(),
        verifies=("No unproved Saxo source or analytical value was returned.",),
        does_not_verify=("The requested analytical conclusion.",),
    )
    return RefusedAnalysisToolResponse(
        tool_name=tool,
        analysis_kind=analysis_kind,
        result=refusal,
        reason_code=reason_code,
        message=message,
        next_tool=next_tool,
        next_action=next_action,
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


def _server_environment() -> str:
    return os.environ.get("SAXO_MCP_ENVIRONMENT", "SIM").strip().upper()


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
            manager = AnalyticsJobManager(
                store=store,
                config=config,
                handlers=_analytics_job_handlers(config, store),
            )
        except BaseException:
            store.close()
            raise
        _job_runtime = (key, store, manager)
        return manager


def _analytics_job_handlers(
    config: AnalyticsConfig,
    store: AnalyticsStore,
) -> dict[JobKind, JobHandler]:
    async def verify_persisted_job_input(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        expected_instruments = 1 if request.job_kind == "backtest" else 0
        if (
            len(request.dataset_ids) != 1
            or request.analysis_ids
            or len(request.instrument_handles) != expected_instruments
        ):
            raise JobStateError("analytics job persisted-input shape is invalid")
        store.get_authenticated_dataset(request.dataset_ids[0])
        await context.report_progress(context.total_work_units)
        raise JobStateError("proof-bound analytics job executor is unavailable")

    async def generate_report(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        if request.dataset_ids or request.instrument_handles or len(request.analysis_ids) != 1:
            raise JobStateError("report generation requires one stored analysis handle")
        parameters = {parameter.name: parameter.value for parameter in request.parameters}
        template_id = parameters.get("template_id")
        output_format = parameters.get("output_format")
        viewport_width = parameters.get("viewport_width", 1280)
        if (
            not isinstance(template_id, str)
            or output_format not in {"html", "pdf"}
            or type(viewport_width) is not int
            or not _MIN_REPORT_VIEWPORT_WIDTH <= viewport_width <= _MAX_REPORT_VIEWPORT_WIDTH
        ):
            raise JobStateError("report generation parameters are invalid")
        bindings = ArtifactBindingRegistry(
            config=config,
            proof_registry=_proof_registry(config),
        )
        issued = bindings.issue(request.analysis_ids[0])
        delivery = export_analysis(
            StoredReportExportRequest(
                binding_id=issued.binding_id,
                template_id=template_id,
                output_format=cast("Literal['html', 'pdf']", output_format),
                viewport_width=viewport_width,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
        if isinstance(delivery, ArtifactRefusal):
            raise JobStateError("proof-bound report generation was refused")
        await context.report_progress(context.total_work_units)
        return JobConclusion(analysis_id=None, artifact_ids=(delivery.artifact_id,))

    return {
        "monte_carlo": verify_persisted_job_input,
        "optimization": verify_persisted_job_input,
        "backtest": verify_persisted_job_input,
        "report_generation": generate_report,
    }


async def shutdown_analytics_runtime() -> None:
    """Stop process-owned jobs, close their store, and reset the singleton exactly once."""
    with _JOB_RUNTIME_LOCK:
        runtime = _job_runtime
    if runtime is None:
        return
    cleanup = asyncio.create_task(
        _shutdown_analytics_runtime(runtime),
        name="analytics-runtime-shutdown",
    )
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def _shutdown_analytics_runtime(
    runtime: tuple[str, AnalyticsStore, AnalyticsJobManager],
) -> None:
    global _job_runtime  # noqa: PLW0603
    _key, store, manager = runtime
    try:
        await manager.shutdown()
    finally:
        try:
            store.close()
        finally:
            with _JOB_RUNTIME_LOCK:
                if _job_runtime is runtime:
                    _job_runtime = None


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
        response = RefusedArtifactToolResponse(
            tool_name=tool,
            reason_code=delivery.reason_code,
            message=delivery.reason,
            next_action=delivery.next_action,
        )
        return ToolResult(
            structured_content=response.model_dump(mode="json"),
            is_error=False,
        )
    structured = delivery.model_dump(mode="json", exclude={"content"})
    structured.update(
        {
            "tool_name": tool,
            "next_action": (
                "Read the owner-only resource by its opaque resource link."
                if isinstance(delivery, ArtifactResourceLink)
                else "Use this bounded inline owner result or request another format."
            ),
            "network_call_made": False,
            "local_state_changed": True,
            "broker_write_made": False,
            "approval_authority": False,
            "execution_authority": False,
            "disclaimer_response_available": False,
        },
    )
    if isinstance(delivery, ArtifactResourceLink):
        structured = ResourceArtifactToolResponse.model_validate(structured).model_dump(mode="json")
    else:
        structured = InlineArtifactToolResponse.model_validate(structured).model_dump(mode="json")
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


def _artifact_failure_result(tool: str, response: AnalyticsToolResponse) -> ToolResult:
    refusal = RefusedArtifactToolResponse(
        tool_name=tool,
        reason_code=response.reason_code or "artifact_delivery_refused",
        message=response.message or "The proof-bound artifact delivery was refused.",
        next_action=response.next_action or "Explain the stored analysis before retrying.",
    )
    return ToolResult(
        structured_content=refusal.model_dump(mode="json"),
        is_error=False,
    )


_CANONICAL_ANALYSIS_TOOLS: Final = frozenset(
    {
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
        "saxo_propose_trade_from_analysis",
    }
)
_ARTIFACT_TOOLS: Final = frozenset({"saxo_render_analysis", "saxo_export_analysis"})


def analytics_tool_output_schema(tool_id: str) -> dict[str, object] | None:
    """Return an explicit structured schema where ToolResult inference is insufficient."""
    if tool_id in _CANONICAL_ANALYSIS_TOOLS:
        schema = _CANONICAL_ANALYSIS_OUTPUT_ADAPTER.json_schema()
        schema["type"] = "object"
        return schema
    if tool_id in _ARTIFACT_TOOLS:
        schema = _ARTIFACT_OUTPUT_ADAPTER.json_schema()
        schema["type"] = "object"
        return schema
    return None


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
