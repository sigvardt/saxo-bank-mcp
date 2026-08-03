"""Thin typed FastMCP adapters over the existing analytics domain services."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Annotated, Final, Literal, cast

import mcp.types as mt
from fastmcp.tools import ToolResult
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from saxo_bank_mcp.analytics_account_data import AccountScope
from saxo_bank_mcp.analytics_config import (
    AnalyticsConfig,
    AnalyticsConfigError,
    AnalyticsLimits,
    load_analytics_config,
)
from saxo_bank_mcp.analytics_execution import (
    BacktestExecutionParameters,
    DerivativesExecutionParameters,
    InstrumentExecutionParameters,
    OptimizationExecutionParameters,
    PortfolioExecutionParameters,
    PositionSizingExecutionParameters,
    ScenarioExecutionParameters,
    ScenarioExecutionShock,
    StoredAnalysisExecutionError,
    StoredExecutionParameters,
    execute_market_comparison,
    execute_pretrade_proposal,
    execute_stored_analysis,
    issue_stored_analysis_input,
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
    JobStatus,
    JobStatusCode,
)
from saxo_bank_mcp.analytics_market import (
    SavedCondition,
)
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinition,
    MetricDefinitionBinding,
    MetricDefinitionCatalog,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    AnalysisResult,
    AnalyticsDegradation,
    AnalyticsRefusal,
    DatasetId,
    InstrumentHandle,
    JobId,
    QualityState,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_portfolio import (
    PortfolioAnalyticsError,
)
from saxo_bank_mcp.analytics_portfolio_snapshots import (
    PortfolioSnapshotError,
    capture_portfolio_snapshot,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofProfileError,
    ProofRegistry,
    SourceContractProofBinding,
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
    ResolutionResult,
    ResolutionStatus,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_storage_tools import (
    DeletionPreviewResult,
    DeletionResult,
    StorageBoundaryError,
    StorageListing,
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
    AccountSnapshotDatasetSummary,
    AccountSnapshotSyncSpec,
    AnalysisInputDatasetSummary,
    AnalysisInputSyncSpec,
    DatasetNotFoundError,
    DatasetPage,
    IngestionFingerprints,
    SyncError,
    SyncLimitError,
    SyncResearchRequest,
    SyncResult,
    SyncStatus,
    get_dataset,
    sync_research_data,
)
from saxo_bank_mcp.analytics_universes import (
    ResearchUniverseStore,
    UniverseConflictError,
    UniverseError,
    UniverseNotFoundError,
    UniverseSummary,
    UniverseValidationError,
)
from saxo_bank_mcp.config import SaxoRuntimeConfig, resolve_sim_auth_settings
from saxo_bank_mcp.mcp_token_state import CachedTokenBlocked, cached_token_for_tool
from saxo_bank_mcp.process_scoped_selectors import resolve_bound_account_selector
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
_JOB_RUNTIME_LOCK: Final = RLock()
_PROCESS_PROOF_LOCK: Final = RLock()
_PROCESS_PROOF_AUTHORITY: Final = object()
_MIN_REPORT_VIEWPORT_WIDTH: Final = 320
_MAX_REPORT_VIEWPORT_WIDTH: Final = 2560
_job_runtime: tuple[str, AnalyticsStore, AnalyticsJobManager] | None = None
_process_proof_candidate: str | None = None
_process_proof_kinds: frozenset[str] = frozenset()
_process_proof_revisions: dict[str, str] = {}


class _StrictToolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=False,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class _OperationalToolResponse(_StrictToolModel):
    """Shared server-issued safety fields for typed operational results."""

    tool_name: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    warnings: tuple[str, ...] = ()
    next_tool: str = Field(pattern=r"^saxo_[a-z0-9_]{1,127}$")
    next_action: str = Field(min_length=1, max_length=500)
    network_call_made: bool | None = None
    local_state_changed: bool
    broker_write_made: Literal[False] = False
    approval_authority: Literal[False] = False
    execution_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class OperationalRefusalToolResponse(_OperationalToolResponse):
    status: Literal["refused"] = "refused"
    result: None = None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    message: str = Field(min_length=1, max_length=500)


class ProofCapabilitySummary(_StrictToolModel):
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    proof_profile_id: str = Field(pattern=r"^vp_[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    activation_state: Literal["active", "quarantined"]
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")


class AnalyticsCapabilitiesResult(_StrictToolModel):
    source_scope: Literal["saxo_openapi"] = "saxo_openapi"
    analytics_tool_ids: tuple[str, ...]
    installed_modules: tuple[str, ...]
    source_data_groups: tuple[str, ...]
    entitlement_state: Literal["not_checked"] = "not_checked"
    dataset_coverage_next_tool: Literal["saxo_list_analytics_storage"] = (
        "saxo_list_analytics_storage"
    )
    render_formats: tuple[Literal["png", "html"], ...]
    export_formats: tuple[Literal["csv", "parquet", "json", "html", "pdf"], ...]
    limits: AnalyticsLimits
    proof_catalog_version: str
    proof_profiles: tuple[ProofCapabilitySummary, ...]
    quarantined_analysis_kinds: tuple[str, ...]
    background_collector: Literal[False] = False
    broker_write_authority: Literal[False] = False
    disclaimer_response_available: Literal[False] = False


class CapabilitiesToolResponse(_OperationalToolResponse):
    status: Literal["passed"] = "passed"
    result: AnalyticsCapabilitiesResult


class ResolutionToolResponse(_OperationalToolResponse):
    status: Literal["resolved", "ambiguous", "unavailable"]
    result: ResolutionResult


class UniverseSummaryResult(_StrictToolModel):
    action: Literal["create", "update"]
    universe: UniverseSummary


class UniverseListResult(_StrictToolModel):
    action: Literal["list"] = "list"
    universes: tuple[UniverseSummary, ...]


class UniverseDeleteResult(_StrictToolModel):
    action: Literal["delete"] = "delete"
    universe_id: str = Field(pattern=r"^un_[0-9a-f]{32}$")
    deleted: Literal[True] = True


type UniverseToolResult = Annotated[
    UniverseSummaryResult | UniverseListResult | UniverseDeleteResult,
    Field(discriminator="action"),
]


class UniverseToolResponse(_OperationalToolResponse):
    status: Literal["passed"] = "passed"
    result: UniverseToolResult


class SyncToolResponse(_OperationalToolResponse):
    status: Literal["passed", "degraded"]
    result: SyncResult


class DatasetToolResponse(_OperationalToolResponse):
    status: Literal["passed"] = "passed"
    result: DatasetPage


class AnalysisExplanation(_StrictToolModel):
    analysis: AnalysisResult
    metric_definitions: tuple[MetricDefinition, ...]
    proof_profile: ProofProfile
    replay_verified: Literal[True] = True


class ExplainToolResponse(_OperationalToolResponse):
    status: Literal["passed"] = "passed"
    result: AnalysisExplanation


class JobToolResponse(_OperationalToolResponse):
    status: JobStatusCode
    result: JobStatus


class StorageListToolResponse(_OperationalToolResponse):
    status: Literal["passed"] = "passed"
    result: StorageListing


class DeletionPreviewToolResponse(_OperationalToolResponse):
    status: Literal["preview_ready"] = "preview_ready"
    result: DeletionPreviewResult


class DeletionToolResponse(_OperationalToolResponse):
    status: Literal["deleted"] = "deleted"
    result: DeletionResult


type CapabilitiesResponse = Annotated[
    CapabilitiesToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type ResolutionResponse = Annotated[
    ResolutionToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type UniverseResponse = Annotated[
    UniverseToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type SyncResponse = Annotated[
    SyncToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type DatasetResponse = Annotated[
    DatasetToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type ExplainResponse = Annotated[
    ExplainToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type JobResponse = Annotated[
    JobToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type StorageListResponse = Annotated[
    StorageListToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type DeletionPreviewResponse = Annotated[
    DeletionPreviewToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]
type DeletionResponse = Annotated[
    DeletionToolResponse | OperationalRefusalToolResponse,
    Field(discriminator="status"),
]


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
_OPERATIONAL_OUTPUT_ADAPTERS: Final[dict[str, TypeAdapter[object]]] = {
    "saxo_analytics_capabilities": TypeAdapter(CapabilitiesResponse),
    "saxo_resolve_research_universe": TypeAdapter(ResolutionResponse),
    "saxo_manage_research_universe": TypeAdapter(UniverseResponse),
    "saxo_sync_research_data": TypeAdapter(SyncResponse),
    "saxo_get_research_dataset": TypeAdapter(DatasetResponse),
    "saxo_explain_analysis": TypeAdapter(ExplainResponse),
    "saxo_manage_analysis_job": TypeAdapter(JobResponse),
    "saxo_list_analytics_storage": TypeAdapter(StorageListResponse),
    "saxo_preview_analytics_deletion": TypeAdapter(DeletionPreviewResponse),
    "saxo_delete_analytics_data": TypeAdapter(DeletionResponse),
}


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


def saxo_analytics_capabilities() -> CapabilitiesResponse:
    """Return local installed capabilities and proof maturity without source access."""
    tool = "saxo_analytics_capabilities"
    try:
        config = _analytics_config()
        definitions = load_metric_definition_catalog()
        catalog = load_proof_profile_catalog(definitions=definitions)
    except (AnalyticsConfigError, OSError, ProofProfileError, ValueError) as error:
        return _known_failure(tool, error, next_tool=tool)
    profiles = tuple(
        ProofCapabilitySummary(
            analysis_kind=profile.analysis_kind,
            proof_profile_id=profile.proof_profile_id,
            activation_state=profile.activation_state.value,
            reason_code=profile.quarantine_reason,
        )
        for profile in catalog.profiles
    )
    result = AnalyticsCapabilitiesResult(
        analytics_tool_ids=ANALYTICS_TOOL_IDS,
        installed_modules=(
            "discovery_and_data",
            "market_and_instrument_research",
            "portfolio_research",
            "decision_models",
            "derivatives_and_strategy_research",
            "artifacts_and_explanation",
            "bounded_jobs_and_storage",
        ),
        source_data_groups=(
            "instrument_reference",
            "price_bars",
            "quotes",
            "portfolio_snapshots",
            "transactions_and_closed_positions",
            "costs",
            "corporate_actions",
            "options_and_derivatives",
        ),
        render_formats=("png", "html"),
        export_formats=("csv", "parquet", "json", "html", "pdf"),
        limits=config.limits,
        proof_catalog_version=catalog.catalog_version,
        proof_profiles=profiles,
        quarantined_analysis_kinds=tuple(
            profile.analysis_kind
            for profile in catalog.profiles
            if profile.activation_state.value == "quarantined"
        ),
    )
    return CapabilitiesToolResponse(
        tool_name=tool,
        result=result,
        next_tool="saxo_resolve_research_universe",
        next_action="Resolve a bounded Saxo research universe before source synchronization.",
        network_call_made=False,
        local_state_changed=False,
    )


async def saxo_resolve_research_universe(
    query: str,
    asset_types: tuple[str, ...] = (),
    exchanges: tuple[str, ...] = (),
) -> ResolutionResponse:
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
        return ResolutionToolResponse(
            status="ambiguous",
            tool_name=tool,
            result=result,
            next_tool=tool,
            next_action="Retry with an explicit asset type or exchange from the returned matches.",
            network_call_made=True,
            local_state_changed=True,
        )
    if result.status is ResolutionStatus.UNAVAILABLE:
        return ResolutionToolResponse(
            status="unavailable",
            tool_name=tool,
            result=result,
            next_tool=tool,
            next_action="Refine the Saxo instrument query; do not substitute another provider.",
            network_call_made=True,
            local_state_changed=False,
        )
    return ResolutionToolResponse(
        status="resolved",
        tool_name=tool,
        result=result,
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
) -> UniverseResponse:
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
            result: UniverseToolResult = UniverseSummaryResult(
                action="create",
                universe=universes.create_universe(cast("str", name), handles),
            )
        elif action == "list":
            result = UniverseListResult(universes=universes.list_universes())
        elif action == "update":
            result = UniverseSummaryResult(
                action="update",
                universe=universes.update_universe(
                    cast("str", universe_id),
                    additions,
                    removals,
                    cast("str", expected_revision),
                ),
            )
        else:
            universes.delete_universe(
                cast("str", universe_id),
                cast("str", expected_revision),
            )
            result = UniverseDeleteResult(universe_id=cast("str", universe_id))
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
    return UniverseToolResponse(
        tool_name=tool,
        result=result,
        next_tool="saxo_sync_research_data",
        next_action="Synchronize the selected safe handles when current Saxo data is required.",
        network_call_made=False,
        local_state_changed=action != "list",
    )


async def saxo_sync_research_data(request: SyncResearchRequest) -> SyncResponse:
    """Run one bounded source sync through the existing Saxo-only provider."""
    tool = "saxo_sync_research_data"
    try:
        result = await _sync_research_request(request)
    except (
        AnalyticsConfigError,
        PortfolioSnapshotError,
        SourceProviderError,
        StoredAnalysisExecutionError,
        SyncError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        next_tool = "saxo_manage_analysis_job" if isinstance(error, SyncLimitError) else tool
        return _known_failure(tool, error, next_tool=next_tool)
    if result.status.value == "refused":
        return _refusal(
            tool,
            "source_sync_refused",
            "The bounded Saxo source synchronization produced no usable stored dataset.",
            next_tool=tool,
            next_action="Review the typed source scope and retry without substituting data.",
        )
    return SyncToolResponse(
        status="passed" if result.status.value == "complete" else "degraded",
        tool_name=tool,
        result=result,
        next_tool="saxo_get_research_dataset",
        next_action="Inspect dataset lineage and quality before calculation.",
        network_call_made=result.source_request_count > 0,
        local_state_changed=True,
    )


async def _sync_research_request(request: SyncResearchRequest) -> SyncResult:
    """Dispatch special server-owned inputs while retaining one bounded public tool."""
    config = _analytics_config()
    provider = SaxoAnalyticsProvider()
    market_items = tuple(
        item
        for item in request.items
        if not isinstance(item, AccountSnapshotSyncSpec | AnalysisInputSyncSpec)
    )
    results: list[SyncResult] = []
    if market_items:
        results.append(
            await sync_research_data(
                SyncResearchRequest(items=market_items),
                provider=provider,
                config=config,
            ),
        )
    for item in request.items:
        if isinstance(item, AccountSnapshotSyncSpec):
            results.append(
                await _capture_server_account_snapshot(item, provider=provider, config=config),
            )
        elif isinstance(item, AnalysisInputSyncSpec):
            results.append(_route_server_analysis_input(item, config=config))
    if not results:
        raise SyncError("research sync contains no executable item")
    statuses = {result.status for result in results}
    status = (
        SyncStatus.COMPLETE
        if statuses == {SyncStatus.COMPLETE}
        else SyncStatus.REFUSED
        if statuses == {SyncStatus.REFUSED}
        else SyncStatus.DEGRADED
    )
    return SyncResult(
        status=status,
        source_request_count=sum(result.source_request_count for result in results),
        datasets=tuple(dataset for result in results for dataset in result.datasets),
    )


async def _capture_server_account_snapshot(
    item: AccountSnapshotSyncSpec,
    *,
    provider: SaxoAnalyticsProvider,
    config: AnalyticsConfig,
) -> SyncResult:
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.requested_environment.value != "SIM":
        raise SyncError("account analytics capture requires current SIM runtime proof")
    settings = resolve_sim_auth_settings(require_redirect=False)
    cached = cached_token_for_tool("saxo_sync_research_data", settings.cache_path)
    if isinstance(cached, CachedTokenBlocked):
        raise SyncError("current SIM authentication is unavailable")
    binding = resolve_bound_account_selector(cached.token, item.safe_account_selector)
    if binding is None or not binding.client_key or not binding.currency:
        raise SyncError("current server-owned account context is unavailable")
    snapshot = await capture_portfolio_snapshot(
        AccountScope(
            alias=binding.account_alias,
            account_key=SecretStr(binding.account_key),
            client_key=SecretStr(binding.client_key),
        ),
        provider=provider,
        config=config,
    )
    return SyncResult(
        status=(SyncStatus.COMPLETE if snapshot.status == "complete" else SyncStatus.DEGRADED),
        source_request_count=snapshot.source_request_count,
        datasets=(
            AccountSnapshotDatasetSummary(
                dataset_id=snapshot.dataset_id,
                account_alias=snapshot.account_alias,
                eligible_analysis_kinds=(
                    "portfolio_performance",
                    "position_sizing",
                    "scenario_custom",
                    "portfolio_minimum_variance",
                    "pretrade_impact",
                ),
                quality_state=(
                    QualityState.COMPLETE if snapshot.status == "complete" else QualityState.PARTIAL
                ),
                coverage_start=snapshot.as_of,
                coverage_end=snapshot.as_of,
                row_count=(
                    snapshot.balance_row_count + snapshot.position_count + snapshot.order_count
                ),
                warnings=snapshot.warnings,
                fingerprints=snapshot.fingerprints,
            ),
        ),
    )


def _route_server_analysis_input(
    item: AnalysisInputSyncSpec,
    *,
    config: AnalyticsConfig,
) -> SyncResult:
    store = AnalyticsStore.open(config)
    try:
        issued = issue_stored_analysis_input(
            analysis_kind=item.analysis_kind,
            source_dataset_ids=item.source_dataset_ids,
            store=store,
        )
    finally:
        store.close()
    fingerprint = issued.fingerprint_sha256
    return SyncResult(
        status=(
            SyncStatus.COMPLETE if issued.quality_state.value == "complete" else SyncStatus.DEGRADED
        ),
        source_request_count=0,
        datasets=(
            AnalysisInputDatasetSummary.model_validate(
                {
                    "dataset_id": issued.dataset_id,
                    "analysis_kind": issued.analysis_kind,
                    "quality_state": issued.quality_state,
                    "coverage_start": issued.coverage_start,
                    "coverage_end": issued.coverage_end,
                    "row_count": issued.row_count,
                    "warnings": (),
                    "fingerprints": IngestionFingerprints(
                        raw_pages_sha256=fingerprint,
                        normalized_rows_sha256=fingerprint,
                        source_contract_sha256=fingerprint,
                        entitlements_sha256=hashlib.sha256(b"available").hexdigest(),
                        correction_state_sha256=hashlib.sha256(
                            json.dumps(
                                {"analysis_kind": issued.analysis_kind},
                                separators=(",", ":"),
                                sort_keys=True,
                            ).encode(),
                        ).hexdigest(),
                    ),
                },
            ),
        ),
    )


def saxo_get_research_dataset(
    dataset_id: DatasetId,
    page: int = 1,
    limit: int = 100,
) -> DatasetResponse:
    """Read one bounded local dataset page by opaque handle."""
    tool = "saxo_get_research_dataset"
    try:
        result = get_dataset(dataset_id, page, limit, config=_analytics_config())
    except (AnalyticsConfigError, DatasetNotFoundError, SyncError, StoreError, OSError) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return DatasetToolResponse(
        tool_name=tool,
        result=result,
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
        periods_per_year=request.periods_per_year,
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
        parameters=(
            InstrumentExecutionParameters(
                instrument_handles=request.instrument_handles,
                rolling_window=request.rolling_window,
                periods_per_year=request.periods_per_year,
                requested_return=request.requested_return,
            )
            if request.analysis_kind == "instrument_price_return"
            else None
        ),
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
        parameters=(
            PortfolioExecutionParameters()
            if request.analysis_kind == "portfolio_performance"
            else None
        ),
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
        parameters=PositionSizingExecutionParameters(
            instrument_handle=request.instrument_handle,
            method=request.method,
            maximum_loss=request.maximum_loss,
            risk_budget_confirmed=request.risk_budget_confirmed,
            stop_price=request.stop_price,
            volatility_multiple=request.volatility_multiple,
        ),
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
        parameters=ScenarioExecutionParameters(
            analysis_kind=request.analysis_kind,
            shocks=tuple(
                ScenarioExecutionShock(
                    instrument_handle=shock.instrument_handle,
                    price_shock_ratio=shock.price_shock_ratio,
                    volatility_shock_points=shock.volatility_shock_points,
                    rate_shock_basis_points=shock.rate_shock_basis_points,
                )
                for shock in request.shocks
            ),
            numeric_shocks_echoed_by_caller=request.numeric_shocks_echoed_by_caller,
            caller_accepted_numeric_shocks=request.caller_accepted_numeric_shocks,
        ),
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
        parameters=OptimizationExecutionParameters(
            analysis_kind=request.analysis_kind,
            objective=request.objective,
            objective_confirmed_by_caller=request.objective_confirmed_by_caller,
            constraints_confirmed_by_caller=request.constraints_confirmed_by_caller,
            short_policy=request.short_policy,
            maximum_turnover=request.maximum_turnover,
            maximum_transaction_cost_ratio=request.maximum_transaction_cost_ratio,
            maximum_margin_ratio=request.maximum_margin_ratio,
        ),
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
        parameters=(
            DerivativesExecutionParameters(
                instrument_handles=request.instrument_handles,
                volatility_assumption=request.volatility_assumption,
                rate_assumption=request.rate_assumption,
            )
            if request.analysis_kind == "derivatives_model"
            else None
        ),
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
        parameters=BacktestExecutionParameters(
            instrument_handle=request.instrument_handle,
            strategy=request.strategy,
            starting_equity=request.starting_equity,
        ),
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
    tool = "saxo_propose_trade_from_analysis"
    selected_visibility = VisibilityMode(visibility)
    if _server_environment() != "SIM":
        return _canonical_analysis_refusal(
            tool,
            "pretrade_impact",
            selected_visibility,
            "sim_pretrade_context_required",
            "A current server-owned SIM account context is required for this proposal.",
            next_tool="saxo_explain_analysis",
            next_action="Inspect the stored analysis without creating a broker request.",
        )
    store: AnalyticsStore | None = None
    try:
        config = _analytics_config()
        store = AnalyticsStore.open(config)
        matching_contexts = tuple(
            snapshot
            for snapshot in store.find_authenticated_snapshot_materials("pretrade_input")
            if snapshot.payload.get("origin_analysis_id") == analysis_id
            and snapshot.payload.get("instrument_handle") == instrument_handle
        )
        registry = _proof_registry(
            config,
            analysis_kind=("pretrade_impact" if len(matching_contexts) == 1 else None),
            source_revision=(
                matching_contexts[0].source_revision if len(matching_contexts) == 1 else None
            ),
        )
        result = replay_analysis(analysis_id, config=config, registry=registry)
        store.get_authenticated_dataset(result.provenance.dataset_id)
        bound_handles = tuple(getattr(result.request, "instrument_handles", ()))
        if not bound_handles or instrument_handle not in bound_handles:
            return _canonical_analysis_refusal(
                tool,
                "pretrade_impact",
                selected_visibility,
                "proposal_context_mismatch",
                "The explicit instrument choice is not bound to the replayed analysis.",
                next_tool="saxo_explain_analysis",
                next_action=(
                    "Inspect the stored analysis and make a separate explicit matching choice."
                ),
            )
        proposal = execute_pretrade_proposal(
            tool_name=tool,
            origin=result,
            instrument_handle=instrument_handle,
            side=side,
            quantity=quantity,
            proposal_price=proposal_price,
            maximum_loss=maximum_loss,
            holding_period_days=holding_period_days,
            visibility=selected_visibility,
            store=store,
            registry=registry,
        )
        return VerifiedAnalysisToolResponse(
            tool_name=tool,
            analysis_kind="pretrade_impact",
            analysis_id=proposal.analysis_id,
            result=proposal,
            warnings=_warning_codes(proposal),
            next_action="Explain this non-authorizing proposal analysis by its stored handle.",
        )
    except StoredAnalysisExecutionError as error:
        return _canonical_analysis_refusal(
            tool,
            "pretrade_impact",
            selected_visibility,
            error.reason_code,
            "The stored analysis and current server-owned context cannot prove this proposal.",
            next_tool="saxo_explain_analysis",
            next_action="Refresh or inspect the stored inputs without creating a broker request.",
        )
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
    finally:
        if store is not None:
            store.close()


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


def saxo_explain_analysis(analysis_id: AnalysisId) -> ExplainResponse:
    """Replay one verified result and attach its checked-in metric definitions."""
    tool = "saxo_explain_analysis"
    try:
        config = _analytics_config()
        registry = _proof_registry(config)
        definitions = registry.definitions
        result = replay_analysis(analysis_id, config=config, registry=registry)
        by_id = definitions.by_id()
        metric_definitions = tuple(
            by_id[metric.metric_id] for metric in result.metrics if metric.metric_id in by_id
        )
        profile = registry.profile(result.analysis_kind)
        if profile is None:
            return _refusal(
                tool,
                "proof_profile_unavailable",
                "The stored analysis proof profile is unavailable.",
                next_tool="saxo_sync_research_data",
                next_action="Refresh the exact stored analysis before explaining it.",
            )
        explanation = AnalysisExplanation(
            analysis=result,
            metric_definitions=metric_definitions,
            proof_profile=profile,
        )
    except (
        AnalysisReplayRefused,
        AnalyticsConfigError,
        ProofProfileError,
        StoreError,
        OSError,
        ValueError,
    ) as error:
        return _known_failure(tool, error, next_tool="saxo_sync_research_data")
    return ExplainToolResponse(
        tool_name=tool,
        result=explanation,
        next_tool="saxo_render_analysis",
        next_action="Render or export only if the stored visibility permits owner delivery.",
        network_call_made=False,
        local_state_changed=False,
    )


async def saxo_manage_analysis_job(
    action: ManageJobAction,
    request: AnalyticsJobToolRequest | None = None,
    job_id: JobId | None = None,
) -> JobResponse:
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
    return JobToolResponse(
        status=status.status_code,
        tool_name=tool,
        result=status,
        next_tool=next_tool,
        next_action=next_action,
        network_call_made=False,
        local_state_changed=True,
    )


def saxo_list_analytics_storage(scope: StorageScope) -> StorageListResponse:
    """List safe local storage metadata through the local-only boundary service."""
    tool = "saxo_list_analytics_storage"
    store: AnalyticsStore | None = None
    try:
        config = _analytics_config()
        store = AnalyticsStore.open(config)
        result = list_storage(
            scope,
            store=store,
            analytics_root=config.paths.analytics_root,
        )
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
    return StorageListToolResponse(
        tool_name=tool,
        result=result,
        next_tool="saxo_preview_analytics_deletion",
        next_action="Preview an exact scope before any local deletion.",
        network_call_made=False,
        local_state_changed=False,
    )


def saxo_preview_analytics_deletion(scope: StorageScope) -> DeletionPreviewResponse:
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
    return DeletionPreviewToolResponse(
        tool_name=tool,
        result=result,
        next_tool="saxo_delete_analytics_data",
        next_action="Use the returned token once before it expires, or preview again.",
        network_call_made=False,
        local_state_changed=True,
    )


def saxo_delete_analytics_data(token: str) -> DeletionResponse:
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
    return DeletionToolResponse(
        tool_name=tool,
        result=result,
        next_tool="saxo_list_analytics_storage",
        next_action="List local storage to inspect the remaining value-free metadata.",
        network_call_made=False,
        local_state_changed=True,
    )


def _stored_analysis_response(  # noqa: C901, PLR0911, PLR0913
    tool: str,
    analysis_kind: str,
    dataset_ids: Sequence[str],
    visibility: VisibilityMode,
    *,
    periods_per_year: float | None = None,
    parameters: StoredExecutionParameters | None = None,
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
        authenticated_datasets = tuple(
            store.get_authenticated_dataset(dataset_id) for dataset_id in dataset_ids
        )
        source_revisions = {dataset.source_revision for dataset in authenticated_datasets}
        if len(source_revisions) != 1:
            raise StoredAnalysisExecutionError(  # noqa: TRY301
                "multi_revision_proof_binding_unavailable"
            )
        registry = _proof_registry(
            config,
            analysis_kind=analysis_kind,
            source_revision=next(iter(source_revisions)),
        )
        profile = registry.profile(analysis_kind)
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
        if (
            tool == "saxo_analyze_market"
            and analysis_kind == "market_comparison"
            and periods_per_year is not None
        ):
            result = execute_market_comparison(
                tool_name=tool,
                dataset_ids=tuple(dataset_ids),
                periods_per_year=periods_per_year,
                visibility=visibility,
                config=config,
                store=store,
                registry=registry,
            )
            return VerifiedAnalysisToolResponse(
                tool_name=tool,
                analysis_kind=analysis_kind,
                analysis_id=result.analysis_id,
                result=result,
                warnings=_warning_codes(result),
                next_action="Explain, render, or export this exact stored analysis handle.",
            )
        if parameters is not None:
            if len(dataset_ids) != 1:
                raise StoredAnalysisExecutionError(  # noqa: TRY301
                    "single_primary_dataset_required"
                )
            result = execute_stored_analysis(
                tool_name=tool,
                dataset_id=dataset_ids[0],
                visibility=visibility,
                parameters=parameters,
                config=config,
                store=store,
                registry=registry,
            )
            return VerifiedAnalysisToolResponse(
                tool_name=tool,
                analysis_kind=analysis_kind,
                analysis_id=result.analysis_id,
                result=result,
                warnings=_warning_codes(result),
                next_action="Explain, render, or export this exact stored analysis handle.",
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
    except StoredAnalysisExecutionError as error:
        return _canonical_analysis_refusal(
            tool,
            analysis_kind,
            visibility,
            error.reason_code,
            "The authenticated stored input cannot satisfy the active proof-bound executor.",
            next_tool="saxo_sync_research_data",
            next_action="Refresh the exact stored input or select its supported bounded analysis.",
        )
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


def _refusal(
    tool: str,
    reason_code: str,
    message: str,
    *,
    next_tool: str,
    next_action: str,
) -> OperationalRefusalToolResponse:
    return OperationalRefusalToolResponse(
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
) -> OperationalRefusalToolResponse:
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


def _process_proof_session_authority() -> object:  # pyright: ignore[reportUnusedFunction]
    return _PROCESS_PROOF_AUTHORITY


def _begin_process_proof_session(  # pyright: ignore[reportUnusedFunction]
    candidate_commit: str,
    analysis_kinds: Sequence[str],
    *,
    authority: object,
) -> None:
    """Begin one private provisional proof session inside the installed producer."""
    global _process_proof_candidate, _process_proof_kinds  # noqa: PLW0603
    if (
        authority is not _PROCESS_PROOF_AUTHORITY
        or re.fullmatch(
            r"[a-f0-9]{40}",
            candidate_commit,
        )
        is None
    ):
        raise ValueError("process proof authority is unavailable")
    kinds = frozenset(analysis_kinds)
    if not kinds:
        raise ValueError("process proof session requires bounded analysis kinds")
    with _PROCESS_PROOF_LOCK:
        if _process_proof_candidate is not None:
            raise ValueError("process proof session is already active")
        _process_proof_candidate = candidate_commit
        _process_proof_kinds = kinds | {"bounded_backtest"}
        _process_proof_revisions.clear()


def _end_process_proof_session(  # pyright: ignore[reportUnusedFunction]
    *, authority: object
) -> None:
    global _process_proof_candidate, _process_proof_kinds  # noqa: PLW0603
    if authority is not _PROCESS_PROOF_AUTHORITY:
        raise ValueError("process proof authority is unavailable")
    with _PROCESS_PROOF_LOCK:
        _process_proof_candidate = None
        _process_proof_kinds = frozenset()
        _process_proof_revisions.clear()


def _proof_registry(
    config: AnalyticsConfig,
    *,
    analysis_kind: str | None = None,
    source_revision: str | None = None,
) -> ProofRegistry:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    with _PROCESS_PROOF_LOCK:
        candidate = _process_proof_candidate
        allowed = _process_proof_kinds
        revisions = dict(_process_proof_revisions)
        if candidate is not None and analysis_kind in allowed and source_revision is not None:
            prior = _process_proof_revisions.setdefault(analysis_kind, source_revision)
            if prior != source_revision:
                raise ProofProfileError("process proof source revision changed")
            revisions[analysis_kind] = source_revision
    if candidate is not None:
        catalog = _process_active_catalog(
            catalog,
            definitions=definitions,
            candidate_commit=candidate,
            allowed_kinds=allowed,
            source_revisions=revisions,
        )
    return ProofRegistry(
        definitions=definitions,
        catalog=catalog,
        config=config,
    )


def _process_active_catalog(
    catalog: ProofProfileCatalog,
    *,
    definitions: MetricDefinitionCatalog,
    candidate_commit: str,
    allowed_kinds: frozenset[str],
    source_revisions: dict[str, str],
) -> ProofProfileCatalog:
    profiles = list(catalog.profiles)
    if "bounded_backtest" in allowed_kinds and not any(
        profile.analysis_kind == "bounded_backtest" for profile in profiles
    ):
        metric = definitions.by_id()["total_return"]
        fields: dict[str, set[str]] = {}
        for binding in metric.input_bindings:
            if binding.source_contract_id is not None:
                fields.setdefault(binding.source_contract_id, set()).update(binding.field_paths)
        profiles.append(
            ProofProfile(
                proof_profile_id="vp_bounded_backtest_runtime_v1",
                profile_version="1",
                activation_state=ProfileActivationState.QUARANTINED,
                quarantine_reason="authenticated_ghost_required",
                analysis_kind="bounded_backtest",
                schema_version="1",
                metric_definitions=(
                    MetricDefinitionBinding(
                        metric_id=metric.metric_id,
                        definition_version=metric.definition_version,
                    ),
                ),
                source_contracts=tuple(
                    SourceContractProofBinding(
                        contract_id=contract_id,
                        contract_sha256=source_contract_fingerprint(
                            source_contracts_by_id()[contract_id]
                        ),
                        field_paths=tuple(sorted(field_paths)),
                    )
                    for contract_id, field_paths in sorted(fields.items())
                ),
                source_revision=None,
                engines=(),
                artifact_template_ids=(),
                definition_catalog_sha256=definitions.fingerprint_sha256,
                source_catalog_sha256=source_contract_catalog_sha256(),
                valid_until=None,
            ),
        )
    active_profiles: list[ProofProfile] = []
    for profile in profiles:
        revision = source_revisions.get(profile.analysis_kind)
        if profile.analysis_kind not in allowed_kinds or revision is None:
            active_profiles.append(profile)
            continue
        active_profiles.append(
            profile.model_copy(
                update={
                    "activation_state": ProfileActivationState.ACTIVE,
                    "quarantine_reason": None,
                    "source_revision": revision,
                    "engines": (
                        EngineProofBinding(
                            engine_name="saxo_analytics",
                            engine_version="task23-installed-proof",
                            code_commit=candidate_commit,
                        ),
                    ),
                    "valid_until": datetime.now(UTC) + timedelta(hours=1),
                }
            )
        )
    production_kinds = tuple(
        dict.fromkeys((*catalog.production_analysis_kinds, *(p.analysis_kind for p in profiles)))
    )
    production_metrics = tuple(
        dict.fromkeys(
            (
                *catalog.production_metric_ids,
                *(
                    binding.metric_id
                    for profile in profiles
                    for binding in profile.metric_definitions
                ),
            ),
        )
    )
    return catalog.model_copy(
        update={
            "production_analysis_kinds": production_kinds,
            "production_metric_ids": production_metrics,
            "profiles": tuple(active_profiles),
        }
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


def _job_field_refusal(
    reason_code: str,
    message: str,
) -> OperationalRefusalToolResponse:
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


def _artifact_failure_result(
    tool: str,
    response: OperationalRefusalToolResponse,
) -> ToolResult:
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
    adapter = _OPERATIONAL_OUTPUT_ADAPTERS.get(tool_id)
    if adapter is not None:
        schema = adapter.json_schema()
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
