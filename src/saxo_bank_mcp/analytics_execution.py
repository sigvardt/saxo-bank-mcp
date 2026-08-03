"""Proof-bound execution over authenticated owner-local analytics inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instruments import PriceSeriesDataset, ResearchRefusal
from saxo_bank_mcp.analytics_market import (
    BoundedMarketResearch,
    BoundedResearchUniverse,
    analyze_bounded_market,
)
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
    MarketAnalysisRequest,
    MetricValue,
    ModelScalarUnit,
    NamedModelParameter,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    VisibilityMode,
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
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoredDataset
from saxo_bank_mcp.analytics_sync import PriceBarDatasetRow, get_dataset

_ENGINE_NAME: Final = "saxo_analytics"
_MARKET_COMPARISON_KIND: Final = "market_comparison"
_PRICE_RETURN_METRIC: Final = "price_return"
_CHART_CONTRACT: Final = "chart_v3"
_MINIMUM_PRICE_OBSERVATIONS: Final = 2
_PROOF_CHECKS: Final = (
    "golden",
    "property",
    "independent_reference",
    "saxo_reconciliation",
    "sim_end_to_end",
)


class StoredAnalysisExecutionError(RuntimeError):
    """Value-free refusal raised before an unproved analysis can be persisted."""

    def __init__(self, reason_code: str) -> None:
        """Retain only a stable public reason code."""
        super().__init__("stored analysis execution refused")
        self.reason_code = reason_code


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
    if tuple(binding.metric_id for binding in profile.metric_definitions) != (
        _PRICE_RETURN_METRIC,
    ):
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


def _active_profile(registry: ProofRegistry, analysis_kind: str) -> ProofProfile:
    profile = registry.profile(analysis_kind)
    if profile is None:
        raise StoredAnalysisExecutionError("missing_proof_profile")
    if profile.activation_state is not ProfileActivationState.ACTIVE:
        raise StoredAnalysisExecutionError(profile.quarantine_reason or "proof_quarantined")
    return profile


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
    "StoredAnalysisExecutionError",
    "execute_market_comparison",
)
