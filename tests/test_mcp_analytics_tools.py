# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import httpx2
import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel

import saxo_bank_mcp.mcp_analytics_tools as tools_module
import saxo_bank_mcp.qa_sim_tool_matrix as matrix_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_costs import CostComponents, SaxoCostIllustration
from saxo_bank_mcp.analytics_derivatives import DerivativeDataset
from saxo_bank_mcp.analytics_execution import (
    StoredDerivativesExecutionContext,
    StoredOptimizationExecutionContext,
    StoredPortfolioExecutionContext,
    StoredPositionSizingExecutionContext,
    StoredPretradeExecutionContext,
    StoredScenarioExecutionContext,
)
from saxo_bank_mcp.analytics_ghost_portfolio import (
    GhostLifecycleEvidence,
    GhostStateFingerprint,
)
from saxo_bank_mcp.analytics_instrument_identity import (
    instrument_handle_for_saxo_identity,
    put_saxo_instrument_identity,
)
from saxo_bank_mcp.analytics_jobs import (
    AnalyticsJobManager,
    JobProgress,
    JobRequest,
    JobStatus,
)
from saxo_bank_mcp.analytics_market_data import ChartInterval
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_models import (
    HandleKind,
    QualityState,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_optimization import (
    CovariancePerturbation,
    OptimizationAsset,
    OptimizationDataset,
    OptimizationRequest,
    SolverSettings,
)
from saxo_bank_mcp.analytics_options import OptionContract, OptionModelInput
from saxo_bank_mcp.analytics_portfolio import (
    LedgerEntry,
    LedgerKind,
    PortfolioPeriodDataset,
    SaxoPerformanceTotals,
    SaxoSourceBinding,
)
from saxo_bank_mcp.analytics_position_sizing import PositionSizingRequest
from saxo_bank_mcp.analytics_proof_profiles import (
    ArtifactOwnerBinding,
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofRegistry,
    SourceContractProofBinding,
)
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_resolver import (
    InstrumentState,
    ResolutionError,
    ResolutionIssue,
    ResolutionIssueCode,
    ResolutionResult,
    ResolutionStatus,
    ResolvedInstrument,
)
from saxo_bank_mcp.analytics_scenarios import (
    CurrencyShock,
    PortfolioScenarioRequest,
    ScenarioComponent,
    ScenarioShock,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_sync import (
    AccountAnalyticsSyncSpec,
    AnalysisInputKind,
    DatasetPage,
    IngestionFingerprints,
    sync_price_bars,
)
from saxo_bank_mcp.analytics_tool_descriptions import ANALYTICS_TOOL_DESCRIPTIONS
from saxo_bank_mcp.analytics_trade_review import DecisionPointQuote
from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.server import create_mcp_server
from saxo_bank_mcp.server_tool_ids import (
    ALL_LOGICAL_TOOL_IDS,
    ANALYTICS_TOOL_IDS,
    EXPECTED_TOOL_COUNT,
)

EXACT_ANALYTICS_TOOL_IDS: Final[tuple[str, ...]] = (
    "saxo_analytics_capabilities",
    "saxo_resolve_research_universe",
    "saxo_manage_research_universe",
    "saxo_sync_research_data",
    "saxo_get_research_dataset",
    "saxo_analyze_market",
    "saxo_analyze_instruments",
    "saxo_analyze_portfolio",
    "saxo_size_position",
    "saxo_run_scenario",
    "saxo_optimize_portfolio",
    "saxo_model_derivatives",
    "saxo_backtest_strategy",
    "saxo_propose_trade_from_analysis",
    "saxo_render_analysis",
    "saxo_export_analysis",
    "saxo_explain_analysis",
    "saxo_manage_analysis_job",
    "saxo_list_analytics_storage",
    "saxo_preview_analytics_deletion",
    "saxo_delete_analytics_data",
)
_EXPECTED_TOOL_COUNT: Final = 60
_NOW = datetime(2026, 8, 3, 10, tzinfo=UTC)
_ACCOUNT_ALIAS = "aa_00000000000040008000000000000099"
_CANDIDATE_COMMIT = "a" * 40


class _ChartFixtureExecutor:
    """Return one local synthetic chart response without opening a socket."""

    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        del operation, request_target, params
        self.call_count += 1
        return httpx2.Response(
            200,
            content=json.dumps(
                {
                    "Data": [
                        {
                            "CloseBid": 100.0,
                            "OpenBid": 100.0,
                            "HighBid": 100.0,
                            "LowBid": 100.0,
                            "PriceType": "RealTime",
                            "Time": "2026-08-03T09:00:00Z",
                            "Volume": 10,
                        },
                        {
                            "CloseBid": 105.0,
                            "OpenBid": 100.0,
                            "HighBid": 105.0,
                            "LowBid": 100.0,
                            "PriceType": "RealTime",
                            "Time": "2026-08-03T09:01:00Z",
                            "Volume": 11,
                        },
                        {
                            "CloseBid": 110.0,
                            "OpenBid": 105.0,
                            "HighBid": 110.0,
                            "LowBid": 105.0,
                            "PriceType": "RealTime",
                            "Time": "2026-08-03T09:02:00Z",
                            "Volume": 12,
                        },
                    ],
                    "DataVersion": 1,
                }
            ).encode(),
            request=httpx2.Request("GET", "https://unit.test/registered"),
        )


def _seed_chart_instrument(config: AnalyticsConfig) -> str:
    handle = instrument_handle_for_saxo_identity("Stock", 1)
    metadata: dict[str, object] = {
        "aliases": [],
        "asset_type": "Stock",
        "display_label": "Synthetic instrument",
        "exchange": None,
        "identifier": 1,
        "symbol": None,
    }
    metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    store = AnalyticsStore.open(config)
    try:
        with store.market_ingestion_transaction(0) as connection:
            write = put_saxo_instrument_identity(
                connection,
                asset_type="Stock",
                uic=1,
                safe_label="Synthetic instrument",
                source_revision="fixture:instrument",
                source_timestamp=_NOW,
                fingerprint_sha256=hashlib.sha256(metadata_json.encode()).hexdigest(),
                metadata_json=metadata_json,
                update_existing=False,
            )
        reference_contract = source_contracts_by_id()["reference_instruments_v1"]
        store.put_source_page(
            source_kind="reference_instruments",
            page_key=f"fixture:reference:{handle}",
            source_revision="capture:fixture-reference",
            source_native_revision="fixture:instrument",
            contract_name="reference_instruments_v1",
            contract_sha256=source_contract_fingerprint(reference_contract),
            payload={
                "contract_id": "reference_instruments_v1",
                "rows": [
                    {
                        "AssetType": "Stock",
                        "Description": "Synthetic instrument",
                        "ExchangeId": None,
                        "Identifier": 1,
                        "Symbol": None,
                    },
                ],
            },
            row_count=1,
            source_timestamp=_NOW,
            account_scope="aggregate",
            instrument_handle=handle,
        )
    finally:
        store.close()
    assert write.instrument_handle == handle
    return handle


async def _synced_chart_fixture(
    config: AnalyticsConfig,
) -> tuple[str, str, str, _ChartFixtureExecutor]:
    handle = _seed_chart_instrument(config)
    executor = _ChartFixtureExecutor()
    sync = await sync_price_bars(
        handle,
        ChartInterval.ONE_MINUTE,
        _NOW - timedelta(hours=1),
        _NOW - timedelta(minutes=58),
        provider=SaxoAnalyticsProvider(request_executor=executor),
        config=config,
        clock=lambda: _NOW,
    )
    dataset_id = sync.datasets[0].dataset_id
    store = AnalyticsStore.open(config)
    try:
        source_revision = store.get_authenticated_dataset(dataset_id).source_revision
    finally:
        store.close()
    return handle, dataset_id, source_revision, executor


def _source_binding(contract_id: str, source_revision: str) -> SaxoSourceBinding:
    contract = source_contracts_by_id()[contract_id]
    return SaxoSourceBinding(
        contract_id=contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        source_revision=source_revision,
        capture_fingerprint_sha256=hashlib.sha256(
            f"{contract_id}:{source_revision}".encode(),
        ).hexdigest(),
        quality_state=QualityState.COMPLETE,
        entitlement_state="available",
    )


def _persist_typed_context(
    config: AnalyticsConfig,
    *,
    context_kind: str,
    contract_ids: tuple[str, ...],
    at: datetime,
    build_context: Callable[
        [str, str, str, tuple[SaxoSourceBinding, ...]],
        BaseModel,
    ],
) -> tuple[str, str, str]:
    """Persist one synthetic Saxo-shaped context through the real owner-local store boundary."""
    source_revision = f"capture:{context_kind}"
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    snapshot_id = new_safe_handle(HandleKind.PORTFOLIO_SNAPSHOT_ID)
    store = AnalyticsStore.open(config)
    try:

        def source_payload(contract_id: str) -> dict[str, object]:
            if contract_id == "info_price_v1":
                return {
                    "contract_id": contract_id,
                    "rows": [
                        {
                            "Uic": 1,
                            "AssetType": "Stock",
                            "Quote": {
                                "Ask": 100,
                                "Bid": 100,
                                "DelayedByMinutes": 0,
                                "Mid": 100,
                                "PriceType": "RealTime",
                            },
                            "PriceTypeAsk": "RealTime",
                            "PriceTypeBid": "RealTime",
                        }
                    ],
                    "source_quality": {
                        "state": "complete",
                        "entitlement_limited_fields": [],
                        "delayed_fields": [],
                        "missing_fields": [],
                    },
                }
            return {
                "contract_id": contract_id,
                "rows": [{"schema": "synthetic_saxo_contract_fixture"}],
            }

        page_ids = tuple(
            store.put_source_page(
                source_kind=source_contracts_by_id()[contract_id].source_kind,
                page_key=f"{context_kind}:{contract_id}",
                source_revision=source_revision,
                contract_name=contract_id,
                contract_sha256=source_contract_fingerprint(
                    source_contracts_by_id()[contract_id],
                ),
                payload=source_payload(contract_id),
                row_count=1,
                source_timestamp=at,
                account_scope=_ACCOUNT_ALIAS,
                instrument_handle=None,
            ).page_id
            for contract_id in contract_ids
        )
        store.create_dataset(
            dataset_id=dataset_id,
            account_scope=_ACCOUNT_ALIAS,
            source_scope="saxo_openapi",
            source_revision=source_revision,
            source_page_ids=page_ids,
            created_at=at,
            coverage_start=at,
            coverage_end=at,
            quality_state=QualityState.COMPLETE,
        )
        material = store.get_authenticated_dataset_material(dataset_id)
        bindings = tuple(
            SaxoSourceBinding(
                contract_id=page.contract_name,
                contract_sha256=page.contract_sha256,
                source_revision=page.source_revision,
                capture_fingerprint_sha256=page.fingerprint_sha256,
                quality_state=QualityState.COMPLETE,
                entitlement_state="available",
            )
            for page in material.pages
        )
        context = build_context(dataset_id, snapshot_id, source_revision, bindings)
        store.create_snapshot(
            snapshot_id=snapshot_id,
            dataset_id=dataset_id,
            snapshot_kind=context_kind,
            account_scope=_ACCOUNT_ALIAS,
            source_revision=source_revision,
            as_of=at,
            payload=context.model_dump(mode="json"),
        )
    finally:
        store.close()
    return dataset_id, snapshot_id, source_revision


def _persist_position_sizing_source_pages(
    config: AnalyticsConfig,
    *,
    instrument_handle: str | None = None,
) -> tuple[str, str]:
    """Persist only validated Saxo-shaped pages; no typed execution snapshot."""
    handle = instrument_handle or _seed_chart_instrument(config)
    source_revision = "capture:position-sizing-source"
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    payloads: dict[str, dict[str, object]] = {
        "balances_v1": {
            "contract_id": "balances_v1",
            "rows": [
                {
                    "CashBalance": 1000,
                    "Currency": "USD",
                    "CashAvailableForTrading": 900,
                    "CashBlocked": 0,
                    "MarginAvailableForTrading": 800,
                    "TotalValue": 1200,
                }
            ],
        },
        "positions_v1": {
            "contract_id": "positions_v1",
            "rows": [
                {
                    "PositionId": "synthetic-position",
                    "PositionBase": {
                        "Amount": 2,
                        "AssetType": "Stock",
                        "OpenPrice": 90,
                        "Uic": 1,
                    },
                    "PositionView": {
                        "CurrentPrice": 100,
                        "Exposure": 200,
                    },
                }
            ],
        },
        "costs_v1": {
            "contract_id": "costs_v1",
            "rows": [
                {
                    "Cost": {"Commission": 1, "StampDuty": 0, "TotalCost": 1},
                    "Currency": "USD",
                    "HoldingPeriodInDays": 0,
                }
            ],
        },
    }
    store = AnalyticsStore.open(config)
    try:
        pages = tuple(
            store.put_source_page(
                source_kind=source_contracts_by_id()[contract_id].source_kind,
                page_key=f"position-sizing-source:{contract_id}",
                source_revision=source_revision,
                contract_name=contract_id,
                contract_sha256=source_contract_fingerprint(
                    source_contracts_by_id()[contract_id],
                ),
                payload=payload,
                row_count=1,
                source_timestamp=_NOW - timedelta(seconds=1),
                account_scope=_ACCOUNT_ALIAS,
                instrument_handle=(handle if contract_id == "costs_v1" else None),
            )
            for contract_id, payload in payloads.items()
        )
        store.create_dataset(
            dataset_id=dataset_id,
            account_scope=_ACCOUNT_ALIAS,
            source_scope="saxo_openapi",
            source_revision=source_revision,
            source_page_ids=tuple(page.page_id for page in pages),
            created_at=_NOW - timedelta(seconds=1),
            coverage_start=_NOW - timedelta(seconds=1),
            coverage_end=_NOW - timedelta(seconds=1),
            quality_state=QualityState.COMPLETE,
        )
    finally:
        store.close()
    return dataset_id, handle


def _persist_aggregate_quote_source_page(
    config: AnalyticsConfig,
    instrument_handle: str,
) -> str:
    """Persist one aggregate Saxo quote independently from private account facts."""
    source_revision = "capture:aggregate-quote-source"
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    payload: dict[str, object] = {
        "contract_id": "info_price_v1",
        "rows": [
            {
                "AssetType": "Stock",
                "PriceTypeAsk": "Tradable",
                "PriceTypeBid": "Tradable",
                "Quote": {
                    "Ask": 101,
                    "Bid": 99,
                    "DelayedByMinutes": 0,
                    "Mid": 100,
                    "PriceType": "Tradable",
                },
                "Uic": 1,
            },
        ],
        "source_quality": {
            "delayed_fields": [],
            "entitlement_limited_fields": [],
            "missing_fields": [],
            "state": "complete",
        },
    }
    store = AnalyticsStore.open(config)
    try:
        page = store.put_source_page(
            source_kind=source_contracts_by_id()["info_price_v1"].source_kind,
            page_key="aggregate-quote-source:info_price_v1",
            source_revision=source_revision,
            contract_name="info_price_v1",
            contract_sha256=source_contract_fingerprint(
                source_contracts_by_id()["info_price_v1"],
            ),
            payload=payload,
            row_count=1,
            source_timestamp=_NOW,
            account_scope="aggregate",
            instrument_handle=instrument_handle,
        )
        store.create_dataset(
            dataset_id=dataset_id,
            account_scope="aggregate",
            source_scope="saxo_openapi",
            source_revision=source_revision,
            source_page_ids=(page.page_id,),
            created_at=_NOW,
            coverage_start=_NOW,
            coverage_end=_NOW,
            quality_state=QualityState.COMPLETE,
        )
    finally:
        store.close()
    return dataset_id


@pytest.mark.anyio
async def test_analysis_input_is_derived_from_authenticated_saxo_source_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Production sync must not require a caller or test to mint a typed snapshot."""
    _state_env(monkeypatch, tmp_path)
    config = tools_module._analytics_config()  # noqa: SLF001
    source_dataset_id, handle = _persist_position_sizing_source_pages(config)

    issued = await tools_module.saxo_sync_research_data(
        tools_module.SyncResearchRequest(
            items=(
                tools_module.AnalysisInputSyncSpec(
                    analysis_kind="position_sizing",
                    source_dataset_ids=(source_dataset_id,),
                ),
            ),
        ),
    )

    assert issued.status == "passed", issued
    assert issued.result is not None
    derived_id = issued.result.datasets[0].dataset_id
    assert derived_id != source_dataset_id
    store = AnalyticsStore.open(config)
    try:
        snapshot = store.get_authenticated_snapshot_material(
            derived_id,
            "position_sizing_input",
        )
        context = StoredPositionSizingExecutionContext.model_validate_json(
            json.dumps(snapshot.payload),
        )
    finally:
        store.close()
    assert context.request.instrument_handle == handle
    assert context.request.entry_price == Decimal(100)
    assert context.request.portfolio_value == Decimal(1200)
    assert context.request.buying_power == Decimal(900)


def _active_market_registry(
    config: AnalyticsConfig,
    *,
    source_revision: str,
) -> ProofRegistry:
    return _active_test_registry(
        config,
        analysis_kind="market_comparison",
        metric_ids=("price_return",),
        source_revision=source_revision,
        artifact_template_ids=("relative_performance",),
    )


def _active_test_registry(
    config: AnalyticsConfig,
    *,
    analysis_kind: str,
    metric_ids: tuple[str, ...],
    source_revision: str,
    artifact_template_ids: tuple[str, ...] = (),
) -> ProofRegistry:
    return _active_test_registry_many(
        config,
        (
            (
                analysis_kind,
                metric_ids,
                source_revision,
                artifact_template_ids,
            ),
        ),
    )


def _active_test_registry_many(
    config: AnalyticsConfig,
    registrations: tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...],
) -> ProofRegistry:
    definitions = load_metric_definition_catalog()
    definitions_by_id = definitions.by_id()
    profile_sources: list[tuple[SourceContractProofBinding, ...]] = []
    catalog_source_fields: dict[str, set[str]] = {}
    for _analysis_kind, metric_ids, _source_revision, _templates in registrations:
        source_fields: dict[str, set[str]] = {}
        for metric_id in metric_ids:
            for binding in definitions_by_id[metric_id].input_bindings:
                if binding.source_contract_id is not None:
                    source_fields.setdefault(binding.source_contract_id, set()).update(
                        binding.field_paths
                    )
                    catalog_source_fields.setdefault(binding.source_contract_id, set()).update(
                        binding.field_paths
                    )
        profile_sources.append(
            tuple(
                SourceContractProofBinding(
                    contract_id=contract_id,
                    contract_sha256=source_contract_fingerprint(
                        source_contracts_by_id()[contract_id]
                    ),
                    field_paths=tuple(sorted(field_paths)),
                )
                for contract_id, field_paths in sorted(source_fields.items())
            )
        )
    catalog_sources = tuple(
        SourceContractProofBinding(
            contract_id=contract_id,
            contract_sha256=source_contract_fingerprint(source_contracts_by_id()[contract_id]),
            field_paths=tuple(sorted(field_paths)),
        )
        for contract_id, field_paths in sorted(catalog_source_fields.items())
    )
    profiles = tuple(
        ProofProfile(
            proof_profile_id=f"vp_{analysis_kind}_test_v1",
            profile_version="1",
            activation_state=ProfileActivationState.ACTIVE,
            quarantine_reason=None,
            analysis_kind=analysis_kind,
            schema_version="1",
            metric_definitions=tuple(
                MetricDefinitionBinding(
                    metric_id=definitions_by_id[metric_id].metric_id,
                    definition_version=definitions_by_id[metric_id].definition_version,
                )
                for metric_id in metric_ids
            ),
            source_contracts=sources,
            source_revision=source_revision,
            engines=(
                EngineProofBinding(
                    engine_name="saxo_analytics",
                    engine_version="1",
                    code_commit="abcdef0",
                ),
            ),
            artifact_template_ids=artifact_template_ids,
            definition_catalog_sha256=definitions.fingerprint_sha256,
            source_catalog_sha256=source_contract_catalog_sha256(),
            valid_until=datetime(2099, 1, 1, tzinfo=UTC),
        )
        for (
            analysis_kind,
            metric_ids,
            source_revision,
            artifact_template_ids,
        ), sources in zip(registrations, profile_sources, strict=True)
    )
    production_metric_ids = tuple(
        dict.fromkeys(
            metric_id
            for _kind, metrics, _revision, _templates in registrations
            for metric_id in metrics
        )
    )
    all_templates = tuple(
        template
        for _kind, _metrics, _revision, templates in registrations
        for template in templates
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version="area-g-test-1",
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        production_metric_ids=production_metric_ids,
        production_analysis_kinds=tuple(item[0] for item in registrations),
        production_artifact_template_ids=all_templates,
        artifact_owners=tuple(
            ArtifactOwnerBinding(template_id=template_id, analysis_kind=analysis_kind)
            for analysis_kind, _metrics, _revision, templates in registrations
            for template_id in templates
        ),
        source_field_coverage=catalog_sources,
        profiles=profiles,
    )
    return ProofRegistry(definitions=definitions, catalog=catalog, config=config)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB", "1")


def _bounded_strategy() -> tools_module.StrategyDefinition:
    return tools_module.StrategyDefinition.model_validate(
        {
            "entry": {
                "left": {"kind": "close", "window": 1},
                "comparison": "greater_than",
                "right_indicator": None,
                "threshold": 0.0,
            },
            "exit": {
                "left": {"kind": "close", "window": 1},
                "comparison": "less_than",
                "right_indicator": None,
                "threshold": 0.0,
            },
            "direction": "long",
            "sizing": {"kind": "fixed_weight", "target_weight": 0.5},
            "rebalancing": {
                "kind": "every_n_bars",
                "interval_bars": 5,
                "fill_timing": "next_bar_open",
            },
            "constraints": {
                "allow_long": True,
                "allow_short": False,
                "maximum_absolute_position_weight": 0.5,
                "maximum_gross_exposure": 1.0,
                "minimum_cash_weight": 0.5,
            },
            "transaction_costs": {
                "commission_basis_points": 0.0,
                "fixed_cost_per_fill": 0.0,
                "currency": "USD",
            },
            "slippage": {"kind": "none", "basis_points": 0.0},
            "evaluation_split": {
                "kind": "holdout",
                "train_end_at": datetime(2026, 1, 1, tzinfo=UTC),
                "holdout_start_at": datetime(2026, 1, 2, tzinfo=UTC),
            },
            "missing_bar_policy": "refuse",
            "delisting_policy": "terminal_close",
        },
    )


def test_exact_analytics_catalog_moves_server_from_39_to_60() -> None:
    assert ANALYTICS_TOOL_IDS == EXACT_ANALYTICS_TOOL_IDS
    assert len(ANALYTICS_TOOL_IDS) == len(set(ANALYTICS_TOOL_IDS)) == len(EXACT_ANALYTICS_TOOL_IDS)
    assert EXPECTED_TOOL_COUNT == _EXPECTED_TOOL_COUNT
    assert len(ALL_LOGICAL_TOOL_IDS) == EXPECTED_TOOL_COUNT
    assert set(ANALYTICS_TOOL_DESCRIPTIONS) == set(ANALYTICS_TOOL_IDS)


def test_installed_process_proof_overlay_is_candidate_bound_and_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    definitions = load_metric_definition_catalog()
    checked = tools_module.load_proof_profile_catalog(definitions=definitions)
    checked_profile = next(
        profile for profile in checked.profiles if profile.analysis_kind == "market_comparison"
    )
    assert checked_profile.activation_state is ProfileActivationState.QUARANTINED
    active_catalog = tools_module._process_active_catalog(  # noqa: SLF001
        checked,
        definitions=definitions,
        candidate_commit=_CANDIDATE_COMMIT,
        allowed_kinds=frozenset({"market_comparison"}),
        source_revisions={"market_comparison": "capture:installed-proof"},
    )
    active = next(
        profile
        for profile in active_catalog.profiles
        if profile.analysis_kind == "market_comparison"
    )
    assert active.activation_state is ProfileActivationState.ACTIVE
    assert active.source_revision == "capture:installed-proof"
    assert active.engines[0].code_commit == _CANDIDATE_COMMIT
    assert checked_profile.activation_state is ProfileActivationState.QUARANTINED


def test_process_proof_authority_is_not_retrievable_or_publicly_mintable() -> None:
    assert not hasattr(tools_module, "_process_proof_session_authority")
    assert not hasattr(tools_module, "_bind_observed_sim_ghost_lifecycle")
    assert not hasattr(tools_module, "_PROCESS_PROOF_RECORDER_SEAL")
    assert not hasattr(tools_module, "_InstalledMatrixProofRecorder")
    assert not hasattr(tools_module, "_receipt_issuer_authority")
    assert not hasattr(tools_module, "issue_authenticated_ghost_receipt_from_lifecycle")
    assert not hasattr(tools_module, "_PROCESS_PROOF_LOCK")
    assert not hasattr(tools_module, "_process_proof_candidate")
    assert not hasattr(tools_module, "_process_proof_kinds")
    assert not hasattr(tools_module, "_process_proof_revisions")
    assert not hasattr(tools_module, "_process_backtest_proofs")
    assert not hasattr(tools_module, "_request_installed_proof_session")
    assert not hasattr(tools_module, "_build_installed_proof_boundary")
    execution_module = import_module("saxo_bank_mcp.analytics_execution")
    assert not hasattr(execution_module, "AuthenticatedBacktestExecutionProof")
    assert (
        "backtest_proof"
        not in inspect.signature(
            execution_module.execute_stored_analysis,
        ).parameters
    )
    parameters = inspect.signature(
        tools_module._run_installed_matrix_proof_session,  # noqa: SLF001
    ).parameters
    assert tuple(parameters) == ("candidate_commit", "analysis_kinds")


def test_forged_lifespan_material_cannot_enter_the_process_proof_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    forged = SimpleNamespace()
    monkeypatch.setattr(
        tools_module,
        "get_context",
        lambda: SimpleNamespace(
            lifespan_context={"installed_analytics_proof_session": forged},
        ),
    )

    assert (
        tools_module._current_process_proof_registry(  # noqa: SLF001
            tools_module._analytics_config(),  # noqa: SLF001
            analysis_kind="bounded_backtest",
            source_revision="capture:forged",
        )
        is None
    )


@pytest.mark.anyio
async def test_installed_lifecycle_recorder_seals_exact_backtest_proof_locally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    handle, chart_dataset_id, _source_revision, _ = await _synced_chart_fixture(config)
    issued = await tools_module.saxo_sync_research_data(
        tools_module.SyncResearchRequest(
            items=(
                tools_module.AnalysisInputSyncSpec(
                    analysis_kind="bounded_backtest",
                    source_dataset_ids=(chart_dataset_id,),
                ),
            ),
        ),
    )
    assert issued.status == "passed"
    assert issued.result is not None
    dataset_id = issued.result.datasets[0].dataset_id
    strategy_payload = _bounded_strategy().model_dump(mode="json")
    strategy_payload["evaluation_split"] = {
        "kind": "holdout",
        "train_end_at": datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        "holdout_start_at": datetime(2026, 8, 3, 9, 1, tzinfo=UTC),
    }
    strategy = tools_module.StrategyDefinition.model_validate(strategy_payload)
    observed_inside = False
    copied_recorder: matrix_module._InstalledProofRecorder | None = None

    async def recorded_matrix(
        _fixtures: object,
        *,
        proof_recorder: matrix_module._InstalledProofRecorder,
        matrix_server: FastMCP,
    ) -> str:
        nonlocal copied_recorder, observed_inside
        copied_recorder = proof_recorder
        account_alias = proof_recorder.controlled_backtest_source_binding(
            dataset_id,
            handle,
            expected_uic=1,
            expected_asset_type="Stock",
        )
        state = GhostStateFingerprint(
            balance_fingerprint_sha256="1" * 64,
            orders_fingerprint_sha256="2" * 64,
            positions_fingerprint_sha256="3" * 64,
            trade_messages_fingerprint_sha256="4" * 64,
            order_count=0,
            position_count=0,
            trade_message_count=0,
        )
        evidence = GhostLifecycleEvidence(
            candidate_commit=_CANDIDATE_COMMIT,
            dataset_id=dataset_id,
            account_alias=account_alias,
            instrument_handle=handle,
            strategy_fingerprint_sha256=tools_module.strategy_definition_fingerprint(strategy),
            fill_model="next_bar_open",
            environment="SIM",
            session_capabilities_current=True,
            fixture_coverage_proved=True,
            preview_status="completed",
            place_status="completed",
            cancel_preview_status="completed",
            cancel_status="completed",
            preview_attempt_count=1,
            place_attempt_count=1,
            cancel_preview_attempt_count=1,
            cancel_attempt_count=1,
            orders_readback=True,
            positions_readback=True,
            trade_messages_readback=True,
            balances_fingerprint_readback=True,
            request_ledger_read_last=True,
            request_ledger_complete=True,
            live_event_count=0,
            live_mutation_count=0,
            non_sim_event_count=0,
            disclaimer_present=False,
            purchase_occurred=False,
            before=state,
            after=state,
        )
        proof_recorder.record_observed_ghost_lifecycle(
            evidence,
            ledger_provenance_sha256="5" * 64,
        )
        request = tools_module.StoredBacktestToolRequest(
            dataset_id=dataset_id,
            instrument_handle=handle,
            strategy=strategy,
            starting_equity=1000,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
        async with Client(matrix_server) as client:
            response = await client.call_tool(
                "saxo_backtest_strategy",
                {"request": request.model_dump(mode="json")},
            )
        assert response.structured_content is not None
        assert response.structured_content["status"] == "verified"
        assert response.structured_content["analysis_kind"] == "bounded_backtest"
        observed_inside = True
        return "recorded"

    monkeypatch.setattr(matrix_module, "_run_matrix", recorded_matrix)

    result = await tools_module._run_installed_matrix_proof_session(  # noqa: SLF001
        _CANDIDATE_COMMIT,
        ("bounded_backtest",),
    )

    assert result == "recorded"
    assert observed_inside is True
    assert copied_recorder is not None
    with pytest.raises(ValueError, match="process proof session is unavailable"):
        copied_recorder.candidate_commit()
    direct = tools_module.saxo_backtest_strategy(
        tools_module.StoredBacktestToolRequest(
            dataset_id=dataset_id,
            instrument_handle=handle,
            strategy=strategy,
            starting_equity=1000,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        ),
    )
    assert isinstance(direct, tools_module.RefusedAnalysisToolResponse)
    assert direct.reason_code in {"backtest_sim_proof_unavailable", "missing_proof_profile"}


@pytest.mark.anyio
async def test_server_account_analytics_capture_routes_every_required_source_family(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The MCP route composes current account, history, cost, and exposure captures."""
    _state_env(monkeypatch, tmp_path)
    fingerprint = IngestionFingerprints(
        raw_pages_sha256="1" * 64,
        normalized_rows_sha256="2" * 64,
        source_contract_sha256="3" * 64,
        entitlements_sha256="4" * 64,
        correction_state_sha256="5" * 64,
    )
    handle = instrument_handle_for_saxo_identity("Stock", 1)
    snapshot_id = new_safe_handle(HandleKind.DATASET_ID)
    calls: list[str] = []

    async def capture_snapshot(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("snapshot")
        return SimpleNamespace(
            account_alias=_ACCOUNT_ALIAS,
            as_of=_NOW,
            balance_row_count=1,
            dataset_id=snapshot_id,
            fingerprints=fingerprint,
            order_count=0,
            position_count=1,
            position_handles=(handle,),
            source_request_count=3,
            status="complete",
            warnings=(),
        )

    async def capture_history(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("history")
        return SimpleNamespace(
            datasets=tuple(
                SimpleNamespace(
                    account_alias=_ACCOUNT_ALIAS,
                    coverage_end=_NOW,
                    coverage_start=_NOW - timedelta(days=1),
                    data_kind=kind,
                    dataset_id=new_safe_handle(HandleKind.DATASET_ID),
                    fingerprints=fingerprint,
                    instrument_handle=None,
                    quality_state=QualityState.COMPLETE,
                    row_count=0,
                    warnings=(),
                )
                for kind in ("transactions", "bookings", "closed_positions")
            ),
            source_request_count=3,
        )

    async def capture_costs(*_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append("costs")
        return SimpleNamespace(
            datasets=(
                SimpleNamespace(
                    account_alias=_ACCOUNT_ALIAS,
                    coverage_end=_NOW,
                    coverage_start=_NOW,
                    dataset_id=new_safe_handle(HandleKind.DATASET_ID),
                    fingerprints=fingerprint,
                    instrument_handle=handle,
                    quality_state=QualityState.COMPLETE,
                    row_count=1,
                    warnings=(),
                ),
            ),
            source_request_count=1,
        )

    async def capture_supplemental(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[tuple[SimpleNamespace, ...], int]:
        calls.append("supplemental")
        return (
            tuple(
                SimpleNamespace(
                    account_alias=_ACCOUNT_ALIAS,
                    contract_id=contract_id,
                    coverage_end=_NOW,
                    coverage_start=_NOW,
                    dataset_id=new_safe_handle(HandleKind.DATASET_ID),
                    fingerprints=fingerprint,
                    instrument_handle=(
                        handle if contract_id == "exposure_instruments_v1" else None
                    ),
                    quality_state=QualityState.COMPLETE,
                    row_count=1,
                    warnings=(),
                )
                for contract_id in (
                    "performance_summary_v4",
                    "performance_timeseries_v4",
                    "exposure_instruments_v1",
                )
            ),
            3,
        )

    def server_scope(_selector: str) -> object:
        return object()

    monkeypatch.setattr(tools_module, "_server_account_scope", server_scope)
    monkeypatch.setattr(tools_module, "capture_portfolio_snapshot", capture_snapshot)
    monkeypatch.setattr(tools_module, "sync_account_history", capture_history)
    monkeypatch.setattr(tools_module, "sync_cost_sources", capture_costs)
    monkeypatch.setattr(tools_module, "sync_account_analysis_sources", capture_supplemental)
    result = await tools_module._capture_server_account_analytics(  # noqa: SLF001
        AccountAnalyticsSyncSpec(
            safe_account_selector="proc-acct-abcdefghijklmnopqrstuvwx",
            analysis_kinds=(
                "portfolio_performance",
                "position_sizing",
                "scenario_custom",
                "portfolio_minimum_variance",
                "derivatives_model",
                "pretrade_impact",
            ),
            instrument_handles=(handle,),
        ),
        provider=SaxoAnalyticsProvider(),
        config=tools_module._analytics_config(),  # noqa: SLF001
    )

    assert calls == ["snapshot", "history", "costs", "supplemental"]
    assert result.source_request_count == sum((3, 3, 1, 3))
    assert {getattr(item, "contract_id", None) for item in result.datasets} >= {
        "costs_v1",
        "exposure_instruments_v1",
        "performance_summary_v4",
        "performance_timeseries_v4",
        "transactions_v1",
        "bookings_v1",
        "closed_positions_history_v1",
    }


@pytest.mark.anyio
async def test_all_analytics_tools_register_once_with_schema_and_description() -> None:
    server = create_mcp_server()
    async with Client(server) as client:
        tools = list(await client.list_tools())

    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)) == EXPECTED_TOOL_COUNT
    assert set(names) == ALL_LOGICAL_TOOL_IDS
    for tool_id in ANALYTICS_TOOL_IDS:
        tool = next(item for item in tools if item.name == tool_id)
        assert tool.description == ANALYTICS_TOOL_DESCRIPTIONS[tool_id]
        assert tool.inputSchema["type"] == "object"
        assert tool.annotations is not None
    render = next(item for item in tools if item.name == "saxo_render_analysis")
    assert render.inputSchema["properties"]["output_format"]["enum"] == ["png", "html"]


@pytest.mark.anyio
async def test_analytics_schemas_expose_no_paths_raw_broker_ids_or_trust_switches() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        tools = list(await client.list_tools())

    forbidden = {
        "account_id",
        "account_key",
        "client_id",
        "client_key",
        "disclaimer_response",
        "path",
        "raw_account_id",
        "raw_client_id",
        "trusted_local_host",
        "uic",
        "url",
    }
    property_names = {name for tool in tools for name in _schema_property_names(tool.inputSchema)}
    assert forbidden.isdisjoint(property_names)


@pytest.mark.anyio
async def test_analysis_schemas_accept_only_handles_and_user_parameters() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    source_fact_fields = {
        "account_alias",
        "bars",
        "buying_power_available",
        "components",
        "cost_estimate",
        "current_position_exposure",
        "current_position_quantity",
        "dataset",
        "datasets",
        "decision_bar",
        "decision_quote",
        "holdings",
        "margin_available",
        "portfolio_value",
        "quote",
        "quotes",
        "saxo_greeks",
        "source_bindings",
    }
    analysis_tools = (
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
    )
    for tool_id in analysis_tools:
        fields = set(_schema_property_names(listed[tool_id].inputSchema))
        assert source_fact_fields.isdisjoint(fields), tool_id
        assert "dataset_id" in fields or "dataset_ids" in fields

    proposal_fields = set(
        _schema_property_names(listed["saxo_propose_trade_from_analysis"].inputSchema)
    )
    assert "proposal" not in proposal_fields
    assert {
        "analysis_id",
        "side",
        "quantity",
        "instrument_handle",
    } <= proposal_fields
    assert source_fact_fields.isdisjoint(proposal_fields)


@pytest.mark.anyio
async def test_analysis_and_artifact_tools_publish_discriminated_output_schemas() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    for tool_id in (
        "saxo_analyze_market",
        "saxo_analyze_instruments",
        "saxo_analyze_portfolio",
        "saxo_size_position",
        "saxo_run_scenario",
        "saxo_optimize_portfolio",
        "saxo_model_derivatives",
        "saxo_backtest_strategy",
        "saxo_propose_trade_from_analysis",
    ):
        schema = listed[tool_id].outputSchema
        assert schema is not None, tool_id
        assert {"verified", "degraded", "refused"} <= set(_schema_literal_values(schema))
        assert not _contains_open_object_schema(schema), tool_id
        for field in (
            "network_call_made",
            "local_state_changed",
            "broker_write_made",
            "next_action",
        ):
            assert field in _schema_property_names(schema), (tool_id, field)

    for tool_id in ("saxo_render_analysis", "saxo_export_analysis"):
        schema = listed[tool_id].outputSchema
        assert schema is not None, tool_id
        assert {"inline", "resource_link", "refused"} <= set(_schema_literal_values(schema))
        assert not _contains_open_object_schema(schema), tool_id
        assert "broker_write_made" in _schema_property_names(schema)


@pytest.mark.anyio
async def test_all_analytics_tools_publish_explicit_state_discriminated_output_schemas() -> None:
    server = create_mcp_server(allowed_tools=frozenset(ANALYTICS_TOOL_IDS))
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    expected_states = {
        "saxo_analytics_capabilities": {"passed", "refused"},
        "saxo_resolve_research_universe": {
            "resolved",
            "ambiguous",
            "unavailable",
            "refused",
        },
        "saxo_manage_research_universe": {"passed", "refused"},
        "saxo_sync_research_data": {"passed", "degraded", "refused"},
        "saxo_get_research_dataset": {"passed", "refused"},
        "saxo_explain_analysis": {"passed", "refused"},
        "saxo_manage_analysis_job": {
            "job_queued",
            "job_running",
            "job_completed",
            "job_failed",
            "job_cancelled",
            "job_expired",
            "job_interrupted_restart_required",
            "refused",
        },
        "saxo_list_analytics_storage": {"passed", "refused"},
        "saxo_preview_analytics_deletion": {"preview_ready", "refused"},
        "saxo_delete_analytics_data": {"deleted", "refused"},
    }
    for tool_id, states in expected_states.items():
        schema = listed[tool_id].outputSchema
        assert schema is not None, tool_id
        assert states <= set(_schema_literal_values(schema)), tool_id
        assert "pattern" not in _status_schema_keywords(schema), tool_id
        assert not _contains_open_object_schema(schema), tool_id
        for field in (
            "network_call_made",
            "local_state_changed",
            "broker_write_made",
            "approval_authority",
            "execution_authority",
            "disclaimer_response_available",
            "next_action",
        ):
            assert field in _schema_property_names(schema), (tool_id, field)


@pytest.mark.anyio
async def test_analysis_fastmcp_request_accepts_safe_handles_and_returns_canonical_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    server = create_mcp_server(allowed_tools=frozenset({"saxo_size_position"}))

    async with Client(server) as client:
        response = await client.call_tool(
            "saxo_size_position",
            {
                "request": {
                    "dataset_id": new_safe_handle(HandleKind.DATASET_ID),
                    "instrument_handle": new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                    "method": "stop_distance",
                    "maximum_loss": "10",
                    "risk_budget_confirmed": True,
                    "stop_price": "99",
                    "visibility": "fingerprint_only",
                }
            },
        )

    assert response.structured_content is not None
    assert response.structured_content["status"] == "refused"
    assert response.structured_content["analysis_id"] is None
    assert response.structured_content["broker_write_made"] is False


@pytest.mark.anyio
async def test_ambiguous_resolution_returns_exact_recovery_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    matches = tuple(
        ResolvedInstrument(
            instrument_handle=new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
            display_label=f"Synthetic listing {ordinal}",
            symbol="SYN",
            asset_type="Stock",
            exchange=f"EX{ordinal}",
            state=InstrumentState.CURRENT,
        )
        for ordinal in range(2)
    )

    async def resolve(
        _self: object,
        query: str,
        asset_types: tuple[str, ...],
        exchanges: tuple[str, ...],
    ) -> ResolutionResult:
        assert query == "Synthetic"
        assert asset_types == ()
        assert exchanges == ()
        return ResolutionResult(
            query=query,
            status=ResolutionStatus.AMBIGUOUS,
            matches=matches,
            issues=(
                ResolutionIssue(
                    code=ResolutionIssueCode.MULTIPLE_LISTINGS,
                    message="Several materially different Saxo instruments match the query.",
                ),
            ),
        )

    monkeypatch.setattr(tools_module.InstrumentResolver, "resolve_instruments", resolve)
    response = await tools_module.saxo_resolve_research_universe(
        query="Synthetic",
        asset_types=(),
        exchanges=(),
    )

    assert response.status == "ambiguous"
    assert response.next_tool == "saxo_resolve_research_universe"
    assert "asset type or exchange" in (response.next_action or "")
    assert response.broker_write_made is False


@pytest.mark.anyio
async def test_known_failure_is_value_free_and_names_the_next_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    private_marker = "private-value-must-not-escape"

    async def fail(*_args: object, **_kwargs: object) -> ResolutionResult:
        raise ResolutionError(f"{private_marker} at a private local path")

    monkeypatch.setattr(tools_module.InstrumentResolver, "resolve_instruments", fail)
    response = await tools_module.saxo_resolve_research_universe(
        query="Synthetic",
        asset_types=(),
        exchanges=(),
    )

    assert response.status == "refused"
    assert response.reason_code == "instrument_resolution_failed"
    assert response.next_tool == "saxo_analytics_capabilities"
    assert private_marker not in repr(response)
    assert "path" not in repr(response).lower()


def test_dataset_adapter_calls_existing_service_and_returns_exact_next_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    calls: list[tuple[str, int, int]] = []

    def get_page(
        selected_dataset_id: str,
        page: int,
        limit: int,
        *,
        config: object,
    ) -> DatasetPage:
        del config
        calls.append((selected_dataset_id, page, limit))
        return DatasetPage(
            dataset_id=dataset_id,
            page=1,
            limit=100,
            total_rows=0,
            rows=(),
            next_page=None,
        )

    monkeypatch.setattr(tools_module, "get_dataset", get_page)
    response = tools_module.saxo_get_research_dataset(dataset_id, page=1, limit=100)

    assert calls == [(dataset_id, 1, 100)]
    assert response.status == "passed"
    assert response.next_tool == "saxo_analyze_instruments"
    assert response.broker_write_made is False


def test_private_delivery_is_derived_from_server_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    request = tools_module.StoredPositionSizingToolRequest(
        dataset_id=new_safe_handle(HandleKind.DATASET_ID),
        instrument_handle=new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
        method="stop_distance",
        maximum_loss=Decimal(10),
        risk_budget_confirmed=True,
        stop_price=Decimal(99),
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    sim = tools_module.saxo_size_position(request)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    live = tools_module.saxo_size_position(request)

    assert sim.status == "refused"
    assert sim.reason_code == "analytics_object_not_found"
    assert live.status == "refused"
    assert live.reason_code == "inline_private_not_enabled"
    assert live.next_tool == "saxo_export_analysis"


def test_analysis_authenticates_stored_handle_and_honors_frozen_proof_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    instrument_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    authenticated: list[str] = []
    closed: list[bool] = []

    class _Store:
        def get_authenticated_dataset(self, selected: str) -> object:
            authenticated.append(selected)
            return SimpleNamespace(source_revision="capture:test")

        def close(self) -> None:
            closed.append(True)

    class _Registry:
        def profile(self, analysis_kind: str) -> object:
            assert analysis_kind == "position_sizing"
            return SimpleNamespace(
                activation_state=SimpleNamespace(value="quarantined"),
                quarantine_reason="implementation_pending",
            )

    def open_store(_config: AnalyticsConfig) -> _Store:
        return _Store()

    def proof_registry(_config: AnalyticsConfig, **_kwargs: object) -> _Registry:
        return _Registry()

    monkeypatch.setattr(
        tools_module.AnalyticsStore,
        "open",
        staticmethod(open_store),
    )
    monkeypatch.setattr(tools_module, "_proof_registry", proof_registry)

    response = tools_module.saxo_size_position(
        tools_module.StoredPositionSizingToolRequest(
            dataset_id=dataset_id,
            instrument_handle=instrument_handle,
            method="stop_distance",
            maximum_loss=Decimal(10),
            risk_budget_confirmed=True,
            stop_price=Decimal(99),
        )
    )

    assert authenticated == [dataset_id]
    assert closed == [True]
    assert isinstance(response, tools_module.RefusedAnalysisToolResponse)
    assert response.status == "refused"
    assert response.analysis_id is None
    assert response.reason_code == "implementation_pending"
    assert response.local_state_changed is False


@pytest.mark.anyio
async def test_active_proof_market_adapter_persists_replays_renders_and_explains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    _, dataset_id, source_revision, executor = await _synced_chart_fixture(config)
    registry = _active_market_registry(
        config,
        source_revision=source_revision,
    )

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)

    calculated = tools_module.saxo_analyze_market(
        tools_module.StoredMarketToolRequest(
            analysis_kind="market_comparison",
            dataset_ids=(dataset_id,),
            periods_per_year=252,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
    )

    assert executor.call_count == 1
    assert isinstance(calculated, tools_module.VerifiedAnalysisToolResponse)
    assert calculated.status == "verified"
    assert calculated.analysis_id == calculated.result.analysis_id
    assert calculated.result.metrics[0].metric_id == "price_return"
    assert calculated.result.metrics[0].value == pytest.approx(0.1)
    assert calculated.result.provenance.dataset_id == dataset_id
    replayed = replay_analysis(
        calculated.analysis_id,
        config=config,
        registry=registry,
    )
    assert replayed == calculated.result

    explained = tools_module.saxo_explain_analysis(calculated.analysis_id)
    assert explained.status == "passed"
    assert explained.result is not None
    rendered = tools_module.saxo_render_analysis(
        calculated.analysis_id,
        "relative_performance",
        output_format="html",
        width=1200,
        height=675,
    )
    assert rendered.structured_content is not None
    assert rendered.structured_content["status"] == "inline", rendered.structured_content
    assert rendered.structured_content["broker_write_made"] is False

    started = await tools_module.saxo_manage_analysis_job(
        action="start",
        request=tools_module.AnalyticsJobToolRequest(
            job_kind="report_generation",
            analysis_ids=(calculated.analysis_id,),
            parameters=(
                tools_module.AnalyticsJobParameter(
                    name="template_id",
                    value="relative_performance",
                ),
                tools_module.AnalyticsJobParameter(
                    name="output_format",
                    value="html",
                ),
                tools_module.AnalyticsJobParameter(
                    name="viewport_width",
                    value=800,
                ),
            ),
            total_work_units=1,
        ),
    )
    checked = started
    try:
        for _ in range(100):
            if checked.status not in {"job_queued", "job_running"}:
                break
            await asyncio.sleep(0)
            checked = await tools_module.saxo_manage_analysis_job(
                action="check",
                job_id=checked.result.job_id if checked.result is not None else None,
            )
        assert checked.status == "job_completed"
        assert checked.result is not None
        assert checked.result.conclusion_available is True
        assert len(checked.result.artifact_ids) == 1
    finally:
        await tools_module.shutdown_analytics_runtime()


@pytest.mark.anyio
async def test_active_proof_instrument_adapter_uses_domain_engine_and_replays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    handle, dataset_id, source_revision, executor = await _synced_chart_fixture(config)
    instrument_registry = _active_test_registry(
        config,
        analysis_kind="instrument_price_return",
        metric_ids=("price_return",),
        source_revision=source_revision,
    )

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return instrument_registry

    monkeypatch.setattr(
        tools_module,
        "_proof_registry",
        active_registry,
    )
    instrument = tools_module.saxo_analyze_instruments(
        tools_module.StoredInstrumentToolRequest(
            analysis_kind="instrument_price_return",
            dataset_ids=(dataset_id,),
            instrument_handles=(handle,),
            rolling_window=2,
            periods_per_year=252,
            requested_return="price_return",
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
    )
    assert executor.call_count == 1
    assert isinstance(instrument, tools_module.VerifiedAnalysisToolResponse)
    assert instrument.result.metrics[0].metric_id == "price_return"
    assert instrument.result.metrics[0].value == pytest.approx(0.1)
    assert (
        replay_analysis(
            instrument.analysis_id,
            config=config,
            registry=instrument_registry,
        )
        == instrument.result
    )


@pytest.mark.anyio
async def test_real_stored_context_adapters_execute_five_distinct_domain_engines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Portfolio, sizing, scenario, optimization, and derivatives use distinct typed inputs."""
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    sizing_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    scenario_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    optimization_handles = (
        new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
        new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
    )
    option_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    underlying_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    portfolio_ids = _persist_typed_context(
        config,
        context_kind="portfolio_performance_input",
        contract_ids=(
            "performance_summary_v4",
            "performance_timeseries_v4",
            "transactions_v1",
            "bookings_v1",
        ),
        at=_NOW,
        build_context=lambda dataset_id, snapshot_id, revision, _bindings: (
            StoredPortfolioExecutionContext(
                dataset=PortfolioPeriodDataset(
                    dataset_id=dataset_id,
                    snapshot_id=snapshot_id,
                    account_alias=_ACCOUNT_ALIAS,
                    start_at=_NOW - timedelta(days=1),
                    end_at=_NOW,
                    reporting_currency="USD",
                    opening_value=Decimal(1000),
                    closing_value=Decimal(1050),
                    ledger_entries=(
                        LedgerEntry(
                            account_alias=_ACCOUNT_ALIAS,
                            event_key_sha256="1" * 64,
                            revision=1,
                            occurred_at=_NOW,
                            kind=LedgerKind.TRADING_PNL,
                            amount=Decimal(50),
                            currency="USD",
                        ),
                    ),
                    source_bindings=tuple(
                        _source_binding(contract_id, revision)
                        for contract_id in (
                            "performance_summary_v4",
                            "performance_timeseries_v4",
                            "transactions_v1",
                            "bookings_v1",
                        )
                    ),
                    quality_state=QualityState.COMPLETE,
                    missing_fields=(),
                    warnings=(),
                    benchmark=None,
                    saxo_totals=SaxoPerformanceTotals(
                        closing_value=Decimal(1050),
                        total_profit_loss=Decimal(50),
                        currency="USD",
                    ),
                    named_differences=(),
                ),
            )
        ),
    )
    sizing_ids = _persist_typed_context(
        config,
        context_kind="position_sizing_input",
        contract_ids=("balances_v1", "positions_v1", "costs_v1"),
        at=_NOW,
        build_context=lambda dataset_id, _snapshot_id, revision, _bindings: (
            StoredPositionSizingExecutionContext(
                request=PositionSizingRequest(
                    dataset_id=dataset_id,
                    account_alias=_ACCOUNT_ALIAS,
                    instrument_handle=sizing_handle,
                    reporting_currency="USD",
                    method="stop_distance",
                    maximum_loss=Decimal(1),
                    risk_budget_confirmed=True,
                    entry_price=Decimal(100),
                    stop_price=Decimal(95),
                    volatility_measure=None,
                    volatility_multiplier=None,
                    value_per_price_unit=Decimal(1),
                    lot_size=Decimal(1),
                    portfolio_value=Decimal(100000),
                    maximum_weight=Decimal("0.5"),
                    buying_power=Decimal(100000),
                    reserved_buffer=Decimal(0),
                    estimated_transaction_cost=Decimal(0),
                    margin_headroom=Decimal(100000),
                    margin_requirement_per_money_unit=Decimal("0.2"),
                    source_bindings=tuple(
                        _source_binding(contract_id, revision)
                        for contract_id in ("balances_v1", "positions_v1", "costs_v1")
                    ),
                    quality_state=QualityState.COMPLETE,
                    missing_fields=(),
                    warnings=(),
                ),
            )
        ),
    )
    scenario_ids = _persist_typed_context(
        config,
        context_kind="scenario_input",
        contract_ids=("balances_v1", "positions_v1", "exposure_instruments_v1"),
        at=_NOW,
        build_context=lambda dataset_id, snapshot_id, revision, _bindings: (
            StoredScenarioExecutionContext(
                analysis_kind="scenario_custom",
                request=PortfolioScenarioRequest(
                    dataset_id=dataset_id,
                    snapshot_id=snapshot_id,
                    account_alias=_ACCOUNT_ALIAS,
                    as_of=_NOW,
                    reporting_currency="USD",
                    scenario_type="equity",
                    input_mode="numeric",
                    narrative_fingerprint_sha256=None,
                    numeric_shocks_echoed_by_caller=False,
                    caller_accepted_numeric_shocks=False,
                    echoed_shock_map_sha256=None,
                    accepted_shock_map_sha256=None,
                    historical_start_at=None,
                    historical_end_at=None,
                    components=(
                        ScenarioComponent(
                            account_alias=_ACCOUNT_ALIAS,
                            instrument_handle=scenario_handle,
                            branch_id="linear",
                            current_value=Decimal(100),
                            currency="USD",
                            current_margin_requirement=Decimal(20),
                            model_analysis_id=None,
                        ),
                    ),
                    component_shocks=(
                        ScenarioShock(
                            instrument_handle=scenario_handle,
                            price_shock_ratio=Decimal(0),
                            volatility_shock_points=Decimal(0),
                            rate_shock_basis_points=Decimal(0),
                            cash_flow_shock=Decimal(0),
                            repriced_value_at_base_fx=None,
                            stressed_margin_requirement=Decimal(20),
                        ),
                    ),
                    currency_shocks=(CurrencyShock(currency="USD", shock_ratio=Decimal(0)),),
                    current_margin_headroom=Decimal(100),
                    source_bindings=tuple(
                        _source_binding(contract_id, revision)
                        for contract_id in (
                            "balances_v1",
                            "positions_v1",
                            "exposure_instruments_v1",
                        )
                    ),
                    quality_state=QualityState.COMPLETE,
                    missing_fields=(),
                    warnings=(),
                ),
            )
        ),
    )

    def build_optimization(
        dataset_id: str,
        snapshot_id: str,
        revision: str,
        _bindings: tuple[SaxoSourceBinding, ...],
    ) -> BaseModel:
        assets = tuple(
            OptimizationAsset(
                account_alias=_ACCOUNT_ALIAS,
                instrument_handle=handle,
                asset_class="equity",
                currency="USD",
                current_weight=Decimal("0.5"),
                expected_return=Decimal("0.05"),
                lower_bound=Decimal(0),
                upper_bound=Decimal(1),
                transaction_cost_rate=Decimal("0.01"),
                margin_requirement_rate=Decimal("0.1"),
                minimum_trade_weight=Decimal(0),
                excluded=False,
            )
            for handle in optimization_handles
        )
        covariance = ((Decimal("0.04"), Decimal(0)), (Decimal(0), Decimal("0.01")))
        dataset = OptimizationDataset(
            dataset_id=dataset_id,
            snapshot_id=snapshot_id,
            account_alias=_ACCOUNT_ALIAS,
            estimation_start_at=_NOW - timedelta(days=60),
            estimation_end_at=_NOW - timedelta(days=1),
            as_of=_NOW,
            reporting_currency="USD",
            assets=assets,
            covariance_matrix=covariance,
            sample_count=60,
            return_model="historical_arithmetic",
            covariance_model="sample_covariance",
            source_bindings=tuple(
                _source_binding(contract_id, revision)
                for contract_id in (
                    "chart_v3",
                    "positions_v1",
                    "exposure_instruments_v1",
                    "balances_v1",
                    "costs_v1",
                )
            ),
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            warnings=(),
        )
        return StoredOptimizationExecutionContext(
            analysis_kind="portfolio_minimum_variance",
            request=OptimizationRequest(
                dataset=dataset,
                objective="minimum_variance",
                objective_confirmed_by_caller=True,
                constraints_confirmed_by_caller=True,
                short_policy="long_only",
                asset_class_constraints=(),
                currency_constraints=(),
                maximum_turnover=Decimal(2),
                maximum_transaction_cost_ratio=Decimal(2),
                maximum_margin_ratio=Decimal(2),
                perturbations=(
                    CovariancePerturbation(
                        perturbation_id="identity_check",
                        covariance_matrix=covariance,
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

    optimization_ids = _persist_typed_context(
        config,
        context_kind="optimization_input",
        contract_ids=(
            "chart_v3",
            "positions_v1",
            "exposure_instruments_v1",
            "balances_v1",
            "costs_v1",
        ),
        at=_NOW,
        build_context=build_optimization,
    )

    def build_derivatives(
        dataset_id: str,
        _snapshot_id: str,
        revision: str,
        _bindings: tuple[SaxoSourceBinding, ...],
    ) -> BaseModel:
        bindings = tuple(
            _source_binding(contract_id, revision)
            for contract_id in ("options_chain_reference_v1", "info_price_v1")
        )
        contract = OptionContract(
            dataset_id=dataset_id,
            option_handle=option_handle,
            underlying_handle=underlying_handle,
            contract_currency="USD",
            pricing_model="black_scholes",
            reference_kind="spot",
            option_type="call",
            exercise_style="european",
            payoff_style="vanilla",
            rate_model="constant",
            reference_price=100,
            strike=100,
            time_to_expiry_years=1,
            risk_free_rate=0.05,
            dividend_yield=0,
            days_per_year=365,
        )
        model_input = OptionModelInput(contract=contract, volatility=0.2)
        return StoredDerivativesExecutionContext(
            dataset=DerivativeDataset(
                dataset_id=dataset_id,
                account_alias=_ACCOUNT_ALIAS,
                as_of=_NOW,
                instrument_handles=(option_handle, underlying_handle),
                source_bindings=bindings,
                quality_state=QualityState.COMPLETE,
                missing_fields=(),
                warnings=(),
            ),
            model_input=model_input,
            saxo_greeks=None,
        )

    derivative_ids = _persist_typed_context(
        config,
        context_kind="derivatives_input",
        contract_ids=("options_chain_reference_v1", "info_price_v1"),
        at=_NOW,
        build_context=build_derivatives,
    )
    registrations = (
        ("portfolio_performance", ("time_weighted_return",), portfolio_ids[2], ()),
        ("position_sizing", ("position_size",), sizing_ids[2], ()),
        ("scenario_custom", ("custom_shock_effect",), scenario_ids[2], ()),
        (
            "portfolio_minimum_variance",
            ("minimum_variance_objective",),
            optimization_ids[2],
            (),
        ),
        ("derivatives_model", ("theoretical_option_value",), derivative_ids[2], ()),
    )
    registry = _active_test_registry_many(config, registrations)

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)
    analysis_routes: tuple[tuple[AnalysisInputKind, str], ...] = (
        ("portfolio_performance", portfolio_ids[0]),
        ("position_sizing", sizing_ids[0]),
        ("scenario_custom", scenario_ids[0]),
        ("portfolio_minimum_variance", optimization_ids[0]),
        ("derivatives_model", derivative_ids[0]),
    )
    for analysis_kind, dataset_id in analysis_routes:
        issued = await tools_module.saxo_sync_research_data(
            tools_module.SyncResearchRequest(
                items=(
                    tools_module.AnalysisInputSyncSpec(
                        analysis_kind=analysis_kind,
                        source_dataset_ids=(dataset_id,),
                    ),
                ),
            ),
        )
        assert issued.status == "passed"

    responses = (
        tools_module.saxo_analyze_portfolio(
            tools_module.StoredPortfolioToolRequest(
                analysis_kind="portfolio_performance",
                dataset_ids=(portfolio_ids[0],),
                visibility=VisibilityMode.PRIVATE_USER_RESULT,
            ),
        ),
        tools_module.saxo_size_position(
            tools_module.StoredPositionSizingToolRequest(
                dataset_id=sizing_ids[0],
                instrument_handle=sizing_handle,
                method="stop_distance",
                maximum_loss=Decimal(100),
                risk_budget_confirmed=True,
                stop_price=Decimal(95),
                visibility=VisibilityMode.PRIVATE_USER_RESULT,
            ),
        ),
        tools_module.saxo_run_scenario(
            tools_module.StoredScenarioToolRequest(
                analysis_kind="scenario_custom",
                dataset_id=scenario_ids[0],
                shocks=(
                    tools_module.ExplicitScenarioShock(
                        instrument_handle=scenario_handle,
                        price_shock_ratio=Decimal("-0.1"),
                    ),
                ),
                numeric_shocks_echoed_by_caller=True,
                caller_accepted_numeric_shocks=True,
                visibility=VisibilityMode.PRIVATE_USER_RESULT,
            ),
        ),
        tools_module.saxo_optimize_portfolio(
            tools_module.StoredOptimizationToolRequest(
                analysis_kind="portfolio_minimum_variance",
                dataset_id=optimization_ids[0],
                objective="minimum_variance",
                objective_confirmed_by_caller=True,
                constraints_confirmed_by_caller=True,
                short_policy="long_only",
                maximum_turnover=Decimal(2),
                maximum_transaction_cost_ratio=Decimal(2),
                maximum_margin_ratio=Decimal(2),
                visibility=VisibilityMode.PRIVATE_USER_RESULT,
            ),
        ),
        tools_module.saxo_model_derivatives(
            tools_module.StoredDerivativesToolRequest(
                analysis_kind="derivatives_model",
                dataset_id=derivative_ids[0],
                instrument_handles=(option_handle, underlying_handle),
                volatility_assumption=Decimal("0.2"),
                rate_assumption=Decimal("0.05"),
                visibility=VisibilityMode.PRIVATE_USER_RESULT,
            ),
        ),
    )

    assert all(
        isinstance(response, tools_module.VerifiedAnalysisToolResponse) for response in responses
    ), tuple((response.status, getattr(response, "reason_code", None)) for response in responses)
    assert tuple(response.analysis_kind for response in responses) == tuple(
        registration[0] for registration in registrations
    )
    assert all(
        replay_analysis(response.analysis_id, config=config, registry=registry) == response.result
        for response in responses
        if isinstance(response, tools_module.VerifiedAnalysisToolResponse)
    )
    assert all(response.broker_write_made is False for response in responses)


@pytest.mark.anyio
async def test_caller_constructed_ghost_fields_cannot_verify_a_backtest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    handle, chart_dataset_id, source_revision, _ = await _synced_chart_fixture(config)
    strategy_payload = _bounded_strategy().model_dump(mode="json")
    strategy_payload["evaluation_split"] = {
        "kind": "holdout",
        "train_end_at": datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        "holdout_start_at": datetime(2026, 8, 3, 9, 1, tzinfo=UTC),
    }
    strategy = tools_module.StrategyDefinition.model_validate(strategy_payload)
    registry = _active_test_registry(
        config,
        analysis_kind="bounded_backtest",
        metric_ids=("total_return",),
        source_revision=source_revision,
    )

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)

    issued = await tools_module.saxo_sync_research_data(
        tools_module.SyncResearchRequest(
            items=(
                tools_module.AnalysisInputSyncSpec(
                    analysis_kind="bounded_backtest",
                    source_dataset_ids=(chart_dataset_id,),
                ),
            ),
        ),
    )
    assert issued.status == "passed"
    assert issued.result is not None
    dataset_id = issued.result.datasets[0].dataset_id
    assert dataset_id != chart_dataset_id
    assert issued.result.datasets[0].data_kind == "analysis_input"

    store = AnalyticsStore.open(config)
    try:
        material = store.get_authenticated_dataset_material(dataset_id)
    finally:
        store.close()
    assert {page.contract_name for page in material.pages} == {
        "chart_v3",
        "reference_instruments_v1",
    }
    response = tools_module.saxo_backtest_strategy(
        tools_module.StoredBacktestToolRequest(
            dataset_id=dataset_id,
            instrument_handle=handle,
            strategy=strategy,
            starting_equity=1000,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
    )

    assert isinstance(response, tools_module.RefusedAnalysisToolResponse)
    assert response.reason_code == "backtest_sim_proof_unavailable"
    assert response.broker_write_made is False


@pytest.mark.anyio
async def test_pretrade_adapter_replays_real_analysis_and_requires_server_owned_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    handle, dataset_id, source_revision, _ = await _synced_chart_fixture(config)
    registry = _active_test_registry_many(
        config,
        (
            ("instrument_price_return", ("price_return",), source_revision, ()),
            (
                "pretrade_impact",
                ("estimated_transaction_cost", "maximum_loss"),
                "capture:pretrade_input",
                (),
            ),
        ),
    )

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)
    origin = tools_module.saxo_analyze_instruments(
        tools_module.StoredInstrumentToolRequest(
            analysis_kind="instrument_price_return",
            dataset_ids=(dataset_id,),
            instrument_handles=(handle,),
            rolling_window=2,
            periods_per_year=252,
            requested_return="price_return",
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
    )
    assert isinstance(origin, tools_module.VerifiedAnalysisToolResponse)

    proposal = tools_module.saxo_propose_trade_from_analysis(
        origin.analysis_id,
        handle,
        "buy",
        Decimal(1),
        proposal_price=Decimal(100),
        maximum_loss=Decimal(10),
        holding_period_days=0,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )

    assert isinstance(proposal, tools_module.RefusedAnalysisToolResponse)
    assert proposal.reason_code == "pretrade_context_unavailable"
    assert proposal.analysis_id is None
    assert proposal.broker_write_made is False
    assert proposal.approval_authority is False
    assert proposal.execution_authority is False
    assert proposal.next_tool != "saxo_create_order_preview"

    def build_pretrade_context(
        pretrade_dataset_id: str,
        _snapshot_id: str,
        _revision: str,
        bindings: tuple[SaxoSourceBinding, ...],
    ) -> BaseModel:
        quote_binding = next(
            binding for binding in bindings if binding.contract_id == "info_price_v1"
        )
        return StoredPretradeExecutionContext(
            origin_analysis_id=origin.analysis_id,
            dataset_id=pretrade_dataset_id,
            account_alias=_ACCOUNT_ALIAS,
            instrument_handle=handle,
            as_of=_NOW,
            reference_price=Decimal(100),
            contract_multiplier=Decimal(1),
            instrument_currency="USD",
            reporting_currency="USD",
            current_position_quantity=Decimal(0),
            current_position_exposure=Decimal(0),
            portfolio_value=Decimal(1000),
            current_currency_exposure=Decimal(0),
            buying_power_available=Decimal(1000),
            margin_available=Decimal(1000),
            buy_cash_required_per_unit=Decimal(100),
            sell_cash_required_per_unit=Decimal(0),
            margin_required_per_unit=Decimal(20),
            unit_cost_estimate=CostComponents(
                commission=Decimal(1),
                spread=Decimal(0),
                fx_conversion=Decimal(0),
                financing=Decimal(0),
                borrow=Decimal(0),
                custody=Decimal(0),
                tax=Decimal(0),
                turnover=Decimal(100),
                total_cost=Decimal(1),
            ),
            saxo_illustration=SaxoCostIllustration(
                commission=Decimal(1),
                stamp_duty=Decimal(0),
                total_cost=Decimal(1),
                currency="USD",
            ),
            named_cost_difference=None,
            decision_quote=DecisionPointQuote(
                dataset_id=pretrade_dataset_id,
                instrument_handle=handle,
                captured_at=_NOW,
                bid=Decimal(99),
                ask=Decimal(101),
                price_type="Tradable",
                delayed_by_minutes=0,
                quality_state=QualityState.COMPLETE,
                entitlement_state="available",
                source_binding=quote_binding,
                captured_by_mcp=True,
                warnings=(),
            ),
            decision_bar=None,
            fx_quotes=(),
            cost_holding_period_days=0,
            source_bindings=bindings,
            quality_state=QualityState.COMPLETE,
            missing_fields=(),
            warnings=(),
        )

    pretrade_ids = _persist_typed_context(
        config,
        context_kind="pretrade_input",
        contract_ids=("balances_v1", "positions_v1", "costs_v1", "info_price_v1"),
        at=_NOW,
        build_context=build_pretrade_context,
    )
    issued = await tools_module.saxo_sync_research_data(
        tools_module.SyncResearchRequest(
            items=(
                tools_module.AnalysisInputSyncSpec(
                    analysis_kind="pretrade_impact",
                    source_dataset_ids=(pretrade_ids[0],),
                    origin_analysis_id=origin.analysis_id,
                ),
            ),
        ),
    )
    assert issued.status == "passed"

    verified = tools_module.saxo_propose_trade_from_analysis(
        origin.analysis_id,
        handle,
        "buy",
        Decimal(1),
        proposal_price=Decimal(100),
        maximum_loss=Decimal(10),
        holding_period_days=0,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )

    assert isinstance(verified, tools_module.VerifiedAnalysisToolResponse)
    assert verified.analysis_kind == "pretrade_impact"
    assert verified.analysis_id != origin.analysis_id
    assert (
        replay_analysis(verified.analysis_id, config=config, registry=registry) == verified.result
    )
    assert verified.broker_write_made is False
    assert verified.approval_authority is False
    assert verified.execution_authority is False
    assert verified.next_tool == "saxo_explain_analysis"


@pytest.mark.anyio
async def test_pretrade_context_is_derived_from_exact_account_sources_and_aggregate_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An aggregate instrument result can bind only through matching account-scoped Saxo pages."""
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    handle, chart_dataset_id, chart_revision, _ = await _synced_chart_fixture(config)
    account_dataset_id, _ = _persist_position_sizing_source_pages(
        config,
        instrument_handle=handle,
    )
    quote_dataset_id = _persist_aggregate_quote_source_page(config, handle)
    registry = _active_test_registry_many(
        config,
        (
            ("instrument_price_return", ("price_return",), chart_revision, ()),
            (
                "pretrade_impact",
                ("estimated_transaction_cost", "maximum_loss"),
                "capture:aggregate-quote-source",
                (),
            ),
        ),
    )

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)
    origin = tools_module.saxo_analyze_instruments(
        tools_module.StoredInstrumentToolRequest(
            analysis_kind="instrument_price_return",
            dataset_ids=(chart_dataset_id,),
            instrument_handles=(handle,),
            rolling_window=2,
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        ),
    )
    assert isinstance(origin, tools_module.VerifiedAnalysisToolResponse)

    issued = await tools_module.saxo_sync_research_data(
        tools_module.SyncResearchRequest(
            items=(
                tools_module.AnalysisInputSyncSpec(
                    analysis_kind="pretrade_impact",
                    source_dataset_ids=(account_dataset_id, quote_dataset_id),
                    origin_analysis_id=origin.analysis_id,
                ),
            ),
        ),
    )
    assert issued.status == "passed", issued
    proposal = tools_module.saxo_propose_trade_from_analysis(
        origin.analysis_id,
        handle,
        "buy",
        Decimal(1),
        proposal_price=Decimal(100),
        maximum_loss=Decimal(10),
        holding_period_days=0,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )

    assert isinstance(proposal, tools_module.VerifiedAnalysisToolResponse), getattr(
        proposal,
        "reason_code",
        None,
    )
    assert proposal.analysis_kind == "pretrade_impact"
    assert proposal.broker_write_made is False
    assert proposal.approval_authority is False
    assert proposal.execution_authority is False


@pytest.mark.anyio
async def test_active_portfolio_adapter_refuses_price_bars_and_nonprivate_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    config = tools_module._analytics_config()  # noqa: SLF001
    _, dataset_id, source_revision, _ = await _synced_chart_fixture(config)
    registry = _active_test_registry(
        config,
        analysis_kind="portfolio_performance",
        metric_ids=("time_weighted_return",),
        source_revision=source_revision,
    )

    def active_registry(_config: AnalyticsConfig, **_kwargs: object) -> ProofRegistry:
        return registry

    monkeypatch.setattr(tools_module, "_proof_registry", active_registry)
    wrong_dataset = tools_module.saxo_analyze_portfolio(
        tools_module.StoredPortfolioToolRequest(
            analysis_kind="portfolio_performance",
            dataset_ids=(dataset_id,),
            visibility=VisibilityMode.PRIVATE_USER_RESULT,
        )
    )
    wrong_visibility = tools_module.saxo_analyze_portfolio(
        tools_module.StoredPortfolioToolRequest(
            analysis_kind="portfolio_performance",
            dataset_ids=(dataset_id,),
            visibility=VisibilityMode.FINGERPRINT_ONLY,
        )
    )

    assert isinstance(wrong_dataset, tools_module.RefusedAnalysisToolResponse)
    assert wrong_dataset.reason_code == "analytics_object_not_found"
    assert isinstance(wrong_visibility, tools_module.RefusedAnalysisToolResponse)
    assert wrong_visibility.reason_code == "private_result_required"


@pytest.mark.anyio
async def test_unavailable_typed_analysis_inputs_refuse_through_real_fastmcp_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unavailable server-owned contexts refuse before any analytical claim is made."""
    _state_env(monkeypatch, tmp_path)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    instrument_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    calls: dict[str, dict[str, object]] = {
        "saxo_analyze_portfolio": {
            "request": {
                "analysis_kind": "portfolio_performance",
                "dataset_ids": [dataset_id],
                "visibility": "private_user_result",
            }
        },
        "saxo_size_position": {
            "request": {
                "dataset_id": dataset_id,
                "instrument_handle": instrument_handle,
                "method": "stop_distance",
                "maximum_loss": "10",
                "risk_budget_confirmed": True,
                "stop_price": "99",
                "visibility": "private_user_result",
            }
        },
        "saxo_run_scenario": {
            "request": {
                "analysis_kind": "scenario_custom",
                "dataset_id": dataset_id,
                "shocks": [
                    {
                        "instrument_handle": instrument_handle,
                        "price_shock_ratio": "-0.1",
                    }
                ],
                "numeric_shocks_echoed_by_caller": True,
                "caller_accepted_numeric_shocks": True,
                "visibility": "private_user_result",
            }
        },
        "saxo_optimize_portfolio": {
            "request": {
                "analysis_kind": "portfolio_minimum_variance",
                "dataset_id": dataset_id,
                "objective": "minimum_variance",
                "objective_confirmed_by_caller": True,
                "constraints_confirmed_by_caller": True,
                "short_policy": "long_only",
                "maximum_turnover": "1",
                "maximum_transaction_cost_ratio": "0.01",
                "maximum_margin_ratio": "1",
                "visibility": "private_user_result",
            }
        },
        "saxo_model_derivatives": {
            "request": {
                "analysis_kind": "derivatives_model",
                "dataset_id": dataset_id,
                "instrument_handles": [instrument_handle],
                "visibility": "private_user_result",
            }
        },
        "saxo_backtest_strategy": {
            "request": {
                "dataset_id": dataset_id,
                "instrument_handle": instrument_handle,
                "strategy": _bounded_strategy().model_dump(mode="json"),
                "starting_equity": 1000.0,
                "visibility": "private_user_result",
            }
        },
    }
    server = create_mcp_server(allowed_tools=frozenset(calls))
    async with Client(server) as client:
        for tool_id, arguments in calls.items():
            response = await client.call_tool(tool_id, arguments)
            assert response.structured_content is not None
            assert response.structured_content["status"] == "refused", tool_id
            assert response.structured_content["reason_code"] == "analytics_object_not_found"
            assert response.structured_content["broker_write_made"] is False


@pytest.mark.anyio
async def test_job_adapter_preserves_bounded_transitions_and_no_partial_conclusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    job_id = new_safe_handle(HandleKind.JOB_ID)
    status = JobStatus(
        job_id=job_id,
        state="running",
        status_code="job_running",
        request_fingerprint="a" * 64,
        created_at=_NOW,
        updated_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
        progress=JobProgress(completed_units=1, total_units=4, remaining_units=3),
        analysis_id=None,
        artifact_ids=(),
        conclusion_available=False,
        restart_allowed=False,
    )

    class _Manager:
        async def get_job(self, selected_job_id: str) -> JobStatus:
            assert selected_job_id == job_id
            return status

    def manager_for_config(_config: AnalyticsConfig) -> AnalyticsJobManager:
        return cast("AnalyticsJobManager", _Manager())

    manager_factory = cast(
        "Callable[[AnalyticsConfig], AnalyticsJobManager]",
        manager_for_config,
    )
    monkeypatch.setattr(tools_module, "_job_manager_for_config", manager_factory)
    response = await tools_module.saxo_manage_analysis_job(action="check", job_id=job_id)
    missing = await tools_module.saxo_manage_analysis_job(action="cancel")

    assert response.status == "job_running"
    assert response.result is not None
    assert response.result.analysis_id is None
    assert response.result.artifact_ids == ()
    assert response.next_tool == "saxo_manage_analysis_job"
    assert missing.status == "refused"
    assert missing.reason_code == "job_id_required"
    assert missing.next_tool == "saxo_manage_analysis_job"


@pytest.mark.anyio
async def test_process_job_runtime_registers_real_handler_and_closes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    manager = tools_module._job_manager_for_config(  # noqa: SLF001
        tools_module._analytics_config()  # noqa: SLF001
    )

    assert set(manager._handlers) == {  # noqa: SLF001
        "monte_carlo",
        "optimization",
        "backtest",
        "report_generation",
    }
    request = JobRequest(
        job_kind="report_generation",
        analysis_ids=(new_safe_handle(HandleKind.ANALYSIS_ID),),
        total_work_units=1,
    )
    status = await manager.start_job(request)
    assert status.state in {"queued", "running"}
    cancelled = await manager.cancel_job(status.job_id)
    checked = await manager.get_job(status.job_id)
    assert cancelled.state == "cancelled"
    assert checked.state == "cancelled"
    assert checked.conclusion_available is False

    await tools_module.shutdown_analytics_runtime()
    assert tools_module._job_runtime is None  # noqa: SLF001


@pytest.mark.anyio
async def test_fastmcp_lifespan_owns_job_runtime_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    server = create_mcp_server(allowed_tools=frozenset({"saxo_manage_analysis_job"}))

    async with Client(server) as client:
        response = await client.call_tool(
            "saxo_manage_analysis_job",
            {
                "action": "start",
                "request": {
                    "job_kind": "report_generation",
                    "dataset_ids": [],
                    "analysis_ids": [new_safe_handle(HandleKind.ANALYSIS_ID)],
                    "instrument_handles": [],
                    "parameters": [],
                    "total_work_units": 1,
                    "restart_interrupted": False,
                },
            },
        )
        assert response.structured_content is not None
        assert response.structured_content["status"] in {"job_queued", "job_running"}
        assert tools_module._job_runtime is not None  # noqa: SLF001

    assert tools_module._job_runtime is None  # noqa: SLF001


@pytest.mark.anyio
async def test_runtime_shutdown_finishes_cleanup_when_transport_is_cancelled() -> None:
    started = asyncio.Event()
    manager_stopped: list[bool] = []
    store_closed: list[bool] = []

    class _Manager:
        async def shutdown(self) -> None:
            started.set()
            await asyncio.sleep(0.05)
            manager_stopped.append(True)

    class _Store:
        def close(self) -> None:
            store_closed.append(True)

    runtime = cast(
        "tuple[str, AnalyticsStore, AnalyticsJobManager]",
        ("fixture-runtime", _Store(), _Manager()),
    )
    tools_module._job_runtime = runtime  # noqa: SLF001
    shutdown = asyncio.create_task(tools_module.shutdown_analytics_runtime())
    await started.wait()

    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert manager_stopped == [True]
    assert store_closed == [True]
    assert tools_module._job_runtime is None  # noqa: SLF001


@pytest.mark.anyio
async def test_trade_proposal_replays_then_refuses_unbound_context_without_preview_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    server = create_mcp_server(allowed_tools=frozenset({"saxo_propose_trade_from_analysis"}))
    async with Client(server) as client:
        response = await client.call_tool(
            "saxo_propose_trade_from_analysis",
            {
                "analysis_id": new_safe_handle(HandleKind.ANALYSIS_ID),
                "instrument_handle": new_safe_handle(HandleKind.INSTRUMENT_HANDLE),
                "side": "buy",
                "quantity": "1",
                "visibility": "fingerprint_only",
            },
        )

    assert response.structured_content is not None
    assert response.structured_content["status"] == "refused"
    assert response.structured_content["next_tool"] != "saxo_create_order_preview"
    assert response.structured_content["broker_write_made"] is False


def test_trade_proposal_binds_instrument_and_authenticates_analysis_dataset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _state_env(monkeypatch, tmp_path)
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    bound_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    other_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)
    authenticated: list[str] = []

    class _Store:
        def find_authenticated_snapshot_materials(self, _kind: str) -> tuple[object, ...]:
            return (
                SimpleNamespace(
                    source_revision="capture:test",
                    payload={
                        "origin_analysis_id": analysis_id,
                        "instrument_handle": bound_handle,
                    },
                ),
            )

        def get_authenticated_dataset(self, selected: str) -> object:
            authenticated.append(selected)
            return object()

        def close(self) -> None:
            return None

    def replay(
        _analysis_id: str,
        *,
        config: AnalyticsConfig,
        registry: object,
    ) -> object:
        del config, registry
        return SimpleNamespace(
            provenance=SimpleNamespace(dataset_id=dataset_id),
            request=SimpleNamespace(instrument_handles=(bound_handle,)),
        )

    def open_store(_config: AnalyticsConfig) -> _Store:
        return _Store()

    def proof_registry(_config: AnalyticsConfig, **_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(
        tools_module,
        "replay_analysis",
        replay,
    )
    monkeypatch.setattr(
        tools_module.AnalyticsStore,
        "open",
        staticmethod(open_store),
    )
    monkeypatch.setattr(tools_module, "_proof_registry", proof_registry)

    mismatch = tools_module.saxo_propose_trade_from_analysis(
        analysis_id,
        other_handle,
        "buy",
        Decimal(1),
    )
    bound = tools_module.saxo_propose_trade_from_analysis(
        analysis_id,
        bound_handle,
        "buy",
        Decimal(1),
    )

    assert authenticated == [dataset_id, dataset_id]
    assert isinstance(mismatch, tools_module.RefusedAnalysisToolResponse)
    assert isinstance(bound, tools_module.RefusedAnalysisToolResponse)
    assert mismatch.reason_code == "proposal_context_mismatch"
    assert bound.reason_code == "explicit_pretrade_inputs_required"
    assert mismatch.next_tool != "saxo_create_order_preview"
    assert bound.next_tool != "saxo_create_order_preview"


def test_bound_pretrade_context_dispatches_proposal_without_broker_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A matching server-owned context produces analysis only, never a preview or write."""
    _state_env(monkeypatch, tmp_path)
    analysis_id = new_safe_handle(HandleKind.ANALYSIS_ID)
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    instrument_handle = new_safe_handle(HandleKind.INSTRUMENT_HANDLE)

    class _Store:
        def find_authenticated_snapshot_materials(self, _kind: str) -> tuple[object, ...]:
            return (
                SimpleNamespace(
                    source_revision="capture:test",
                    payload={
                        "origin_analysis_id": analysis_id,
                        "instrument_handle": instrument_handle,
                    },
                ),
            )

        def get_authenticated_dataset(self, _dataset_id: str) -> object:
            return object()

        def close(self) -> None:
            return None

    def replay(
        _analysis_id: str,
        *,
        config: AnalyticsConfig,
        registry: object,
    ) -> object:
        del config, registry
        return SimpleNamespace(
            provenance=SimpleNamespace(dataset_id=dataset_id),
            request=SimpleNamespace(instrument_handles=(instrument_handle,)),
        )

    def open_store(_config: AnalyticsConfig) -> _Store:
        return _Store()

    def proof_registry(_config: AnalyticsConfig, **_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(
        tools_module,
        "replay_analysis",
        replay,
    )
    monkeypatch.setattr(
        tools_module.AnalyticsStore,
        "open",
        staticmethod(open_store),
    )
    monkeypatch.setattr(tools_module, "_proof_registry", proof_registry)
    routed: list[dict[str, object]] = []

    def execute(**kwargs: object) -> object:
        routed.append(kwargs)
        raise tools_module.StoredAnalysisExecutionError("fixture_domain_refusal")

    monkeypatch.setattr(tools_module, "execute_pretrade_proposal", execute)

    response = tools_module.saxo_propose_trade_from_analysis(
        analysis_id,
        instrument_handle,
        "buy",
        Decimal(1),
        proposal_price=Decimal(100),
        maximum_loss=Decimal(10),
        holding_period_days=5,
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
    )

    assert len(routed) == 1
    assert routed[0]["side"] == "buy"
    assert routed[0]["quantity"] == Decimal(1)
    assert routed[0]["proposal_price"] == Decimal(100)
    assert routed[0]["maximum_loss"] == Decimal(10)
    assert isinstance(response, tools_module.RefusedAnalysisToolResponse)
    assert response.reason_code == "fixture_domain_refusal"
    assert response.broker_write_made is False
    assert response.approval_authority is False
    assert response.execution_authority is False
    assert response.next_tool != "saxo_create_order_preview"


def _schema_property_names(schema: object) -> tuple[str, ...]:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        properties = mapping.get("properties", {})
        property_mapping = (
            cast("dict[object, object]", properties) if isinstance(properties, dict) else {}
        )
        names = tuple(str(name).casefold() for name in property_mapping)
        return names + tuple(
            nested for value in mapping.values() for nested in _schema_property_names(value)
        )
    if isinstance(schema, list):
        values = cast("list[object]", schema)
        return tuple(nested for value in values for nested in _schema_property_names(value))
    return ()


def _schema_literal_values(schema: object) -> tuple[str, ...]:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        values: list[str] = []
        const = mapping.get("const")
        if isinstance(const, str):
            values.append(const)
        enum = mapping.get("enum")
        if isinstance(enum, list):
            values.extend(item for item in cast("list[object]", enum) if isinstance(item, str))
        return tuple(values) + tuple(
            nested for value in mapping.values() for nested in _schema_literal_values(value)
        )
    if isinstance(schema, list):
        return tuple(
            nested
            for value in cast("list[object]", schema)
            for nested in _schema_literal_values(value)
        )
    return ()


def _status_schema_keywords(schema: object) -> tuple[str, ...]:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        keywords: list[str] = []
        properties = mapping.get("properties")
        if isinstance(properties, dict):
            status = cast("dict[object, object]", properties).get("status")
            if isinstance(status, dict):
                keywords.extend(str(key) for key in cast("dict[object, object]", status))
        return tuple(keywords) + tuple(
            nested for value in mapping.values() for nested in _status_schema_keywords(value)
        )
    if isinstance(schema, list):
        return tuple(
            nested
            for value in cast("list[object]", schema)
            for nested in _status_schema_keywords(value)
        )
    return ()


def _contains_open_object_schema(schema: object) -> bool:
    if isinstance(schema, dict):
        mapping = cast("dict[object, object]", schema)
        if mapping.get("additionalProperties") is True:
            return True
        return any(_contains_open_object_schema(value) for value in mapping.values())
    if isinstance(schema, list):
        return any(_contains_open_object_schema(value) for value in cast("list[object]", schema))
    return False
