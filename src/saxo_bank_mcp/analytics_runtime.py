"""Production recipe dispatch, canonical results and authenticated replay bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel

from saxo_bank_mcp import (
    analytics_runtime_market,
    analytics_runtime_models,
    analytics_runtime_portfolio,
)
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisCalendar,
    AnalysisParameterBinding,
    AnalysisProvenance,
    AnalysisResult,
    AnalysisStatus,
    AnalysisWarning,
    DataCoverage,
    DataQuality,
    FxConversionMethod,
    FxSource,
    InputDatasetDependency,
    MetricCurrencyBinding,
    MetricValue,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    RecipeAnalysisRequest,
    ValueUnitClass,
    VisibilityMode,
)
from saxo_bank_mcp.analytics_proof_profiles import ProofRegistry, ProofState
from saxo_bank_mcp.analytics_provenance import (
    build_analysis_identity,
    build_analysis_parameters_sha256,
)
from saxo_bank_mcp.analytics_runtime_inputs import (
    AnalyticsExecutionError,
    RecipeRequest,
    ResearchInputs,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    StoredDataset,
)

FAMILIES = (analytics_runtime_market, analytics_runtime_portfolio, analytics_runtime_models)
RECIPE_KINDS = tuple(sorted(kind for family in FAMILIES for kind in family.SUPPORTED_KINDS))
PROOF_CHECKS = (
    "golden",
    "property",
    "independent_reference",
    "saxo_reconciliation",
    "installed_end_to_end",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _bound_dataset(inputs: ResearchInputs) -> StoredDataset:
    """Pin every input capture without requiring independently captured data to share a revision."""
    if len(inputs.materials) == 1 and not inputs.has_supporting_pages:
        return inputs.materials[0].dataset
    pages = tuple(sorted(inputs.pages, key=lambda page: page.page_id))
    primary = max(pages, key=lambda page: (page.source_timestamp, page.page_id))
    digest = _digest(
        {
            "pages": sorted(page.page_id for page in inputs.pages),
            "inputs": sorted(
                (material.dataset.dataset_id, material.dataset.fingerprint_sha256)
                for material in inputs.materials
            ),
        }
    )
    raw = bytearray(bytes.fromhex(digest[:32]))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    quality = (
        QualityState.COMPLETE
        if all(
            material.dataset.quality_state is QualityState.COMPLETE for material in inputs.materials
        )
        else QualityState.PARTIAL
    )
    return inputs.store.create_dataset(
        dataset_id=f"ds_{UUID(bytes=bytes(raw)).hex}",
        account_scope=inputs.account_scope,
        source_scope="saxo_openapi",
        source_revision=primary.source_revision,
        source_page_ids=tuple(
            page.page_id for page in pages if page.source_revision == primary.source_revision
        ),
        lineage_source_page_ids=tuple(
            page.page_id for page in pages if page.source_revision != primary.source_revision
        ),
        aggregate_lineage_contract_ids=(
            "chart_v3",
            "info_price_v1",
            "options_chain_reference_v1",
            "reference_instruments_v1",
            "reference_instrument_details_v1",
        ),
        created_at=inputs.as_of,
        coverage_start=min(material.coverage_start for material in inputs.materials),
        coverage_end=max(material.coverage_end for material in inputs.materials),
        quality_state=quality,
    )


def execute_analysis(  # noqa: C901, PLR0912, PLR0913, PLR0915 - canonical proof assembly is sequential
    *,
    tool_name: str,
    request: BaseModel,
    config: AnalyticsConfig,
    store: AnalyticsStore,
    registry: ProofRegistry,
    cancellation_check: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    persist: bool = True,
) -> AnalysisResult:
    """Calculate from authenticated owner inputs using a release-verified recipe only."""
    recipe = RecipeRequest.from_model(request)
    family = next((item for item in FAMILIES if recipe.analysis_kind in item.SUPPORTED_KINDS), None)
    if family is None:
        raise AnalyticsExecutionError("recipe_unavailable")
    if len(set(recipe.dataset_ids)) != len(recipe.dataset_ids):
        raise AnalyticsExecutionError("duplicate_input_dataset")
    profile = registry.profile(recipe.analysis_kind)
    if profile is None or profile.activation_state.value != "active":
        raise AnalyticsExecutionError(
            (profile.quarantine_reason or "proof_quarantined")
            if profile
            else "missing_proof_profile"
        )
    materials = tuple(
        store.get_authenticated_dataset_material(identifier) for identifier in recipe.dataset_ids
    )
    if not materials or any(
        material.dataset.quality_state is QualityState.INVALID for material in materials
    ):
        raise AnalyticsExecutionError("analysis_input_invalid")
    inputs = ResearchInputs(
        config,
        store,
        materials,
        cancellation_check=cancellation_check,
        progress=progress,
        proof_registry=registry,
    )
    inputs.check_cancellation()
    payload = family.execute_recipe(recipe, inputs)
    inputs.check_cancellation()
    dataset = _bound_dataset(inputs)
    material = store.get_authenticated_dataset_material(dataset.dataset_id)
    as_of = max(inputs.as_of, *(page.source_timestamp for page in inputs.pages))
    definitions = registry.definitions.by_id()
    metric_ids = tuple(metric.metric_id for metric in payload.metrics)
    contracts = {page.contract_name: page.contract_sha256 for page in inputs.pages}
    required_contracts = registry.required_source_contract_ids(recipe.analysis_kind)
    if not required_contracts <= contracts.keys():
        raise AnalyticsExecutionError(
            "proof_source_contract_missing", tuple(sorted(required_contracts - contracts.keys()))
        )
    engine = profile.engines[0]
    checked = registry.status(
        recipe.analysis_kind,
        "1",
        {key: contracts[key] for key in required_contracts},
        source_revision=dataset.source_revision,
        engine_versions={engine.engine_name: (engine.engine_version, engine.code_commit)},
        metric_ids=metric_ids,
    )
    if checked.state is not ProofState.ACTIVE:
        raise AnalyticsExecutionError(checked.reason_code)
    currency = inputs.currency()
    monetary = [
        metric
        for metric in payload.metrics
        if definitions[metric.metric_id].unit_class is ValueUnitClass.MONETARY
    ]
    if currency == "XXX" and monetary:
        currencies = {metric.currency for metric in monetary if metric.currency is not None}
        if len(currencies) == 1:
            currency = cast("str", next(iter(currencies)))
    if any(
        (metric.currency is None and definitions[metric.metric_id].output_unit == "price_currency")
        or (metric.currency or currency) == "XXX"
        for metric in monetary
    ):
        raise AnalyticsExecutionError("analysis_currency_unavailable")
    mismatched = tuple(
        metric.metric_id
        for metric in monetary
        if definitions[metric.metric_id].output_unit == "reporting_currency"
        and metric.currency is not None
        and metric.currency != currency
    )
    if mismatched:
        raise AnalyticsExecutionError("analysis_currency_mismatch", mismatched)
    options_values = recipe.arguments.get("options")
    option_map: Mapping[str, object] = (
        cast("Mapping[str, object]", options_values) if isinstance(options_values, Mapping) else {}
    )
    benchmark = option_map.get("benchmark_handle")
    benchmark = benchmark if isinstance(benchmark, str) else None
    benchmark_fingerprint = None
    if benchmark is not None:
        benchmark_materials = [
            item
            for item in inputs.materials
            if any(row.instrument_handle == benchmark for row in inputs.dataset_rows(item))
        ]
        if len(benchmark_materials) != 1:
            raise AnalyticsExecutionError("benchmark_input_missing_or_ambiguous")
        benchmark_fingerprint = benchmark_materials[0].dataset.fingerprint_sha256
    parameters = AnalysisParameterBinding(
        start_at=material.coverage_start,
        end_at=material.coverage_end,
        as_of=as_of,
        benchmark_handle=benchmark,
        benchmark_fingerprint_sha256=benchmark_fingerprint,
        fx_method=FxConversionMethod.NOT_APPLICABLE,
        fx_source=FxSource.NOT_APPLICABLE,
        fx_timestamp=None,
        calendar=AnalysisCalendar.CALENDAR_DAYS,
        reporting_currency=currency,
        metric_currency_bindings=tuple(
            sorted(
                (
                    MetricCurrencyBinding(
                        metric_id=metric.metric_id,
                        currency=metric.currency or currency,
                    )
                    for metric in monetary
                    if definitions[metric.metric_id].output_unit == "price_currency"
                ),
                key=lambda binding: binding.metric_id,
            )
        ),
        model_parameters=(),
        analysis_dependencies=inputs.analysis_dependencies,
        input_dataset_dependencies=tuple(
            InputDatasetDependency(
                dataset_id=item.dataset.dataset_id,
                fingerprint_sha256=item.dataset.fingerprint_sha256,
            )
            for item in inputs.materials
        ),
    )
    canonical_request = RecipeAnalysisRequest(
        request_kind="recipe",
        analysis_kind=recipe.analysis_kind,
        dataset_id=dataset.dataset_id,
        input_dataset_ids=recipe.dataset_ids,
        recipe_arguments_json=json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ),
        parameters=parameters,
    )
    assumptions = tuple(sorted(payload.assumptions, key=lambda assumption: assumption.name))
    parameter_hash = build_analysis_parameters_sha256(
        account_scope=inputs.account_scope,
        analysis_kind=recipe.analysis_kind,
        assumptions=assumptions,
        as_of=as_of,
        request=canonical_request,
    )
    seed = recipe.arguments.get("random_seed")
    seed = seed if type(seed) is int else None
    simulation = option_map.get("simulation")
    if isinstance(simulation, Mapping):
        simulation_values = cast("Mapping[str, object]", simulation)
        if type(simulation_values.get("random_seed")) is int:
            seed = cast("int", simulation_values["random_seed"])
    identity = build_analysis_identity(
        {
            "analysis_parameters_sha256": parameter_hash,
            "dataset_fingerprint_sha256": dataset.fingerprint_sha256,
            "dataset_id": dataset.dataset_id,
            "source_revision": dataset.source_revision,
            "tool_name": tool_name,
        },
        {engine.engine_name: {"version": engine.engine_version, "code_commit": engine.code_commit}},
        seed,
    )
    shas = tuple(sorted(set(contracts.values())))
    source_binding = ProofSourceBinding(
        source_scope="saxo_openapi",
        source_revision=dataset.source_revision,
        source_contract_sha256=shas[0]
        if len(shas) == 1
        else _digest({"source_contract_sha256s": list(shas)}),
        source_contract_sha256s=() if len(shas) == 1 else shas,
    )
    quality = dataset.quality_state
    warnings = set(payload.warnings)
    if quality is not QualityState.COMPLETE:
        warnings.add("source_coverage_incomplete")
    degraded = bool(payload.unavailable_fields or warnings)
    warning_models = tuple(
        AnalysisWarning(
            code=code,
            message="This result preserves the named source or model limitation.",
            quality_state=quality,
        )
        for code in sorted(warnings)
    )
    engine_binding = ProofEngineBinding(
        engine_name=engine.engine_name,
        engine_version=engine.engine_version,
        code_commit=engine.code_commit,
    )
    metrics = tuple(
        MetricValue(
            metric_id=metric.metric_id,
            value=metric.value,
            unit=definitions[metric.metric_id].output_unit,
            unit_class=definitions[metric.metric_id].unit_class,
            currency=(metric.currency or currency)
            if definitions[metric.metric_id].unit_class is ValueUnitClass.MONETARY
            else None,
            metric_class=metric.metric_class or definitions[metric.metric_id].default_metric_class,
            source_timestamp=as_of,
            proof_profile_id=profile.proof_profile_id,
        )
        for metric in payload.metrics
    )
    result = AnalysisResult(
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        status=AnalysisStatus.DEGRADED if degraded else AnalysisStatus.VERIFIED,
        tool_name=tool_name,
        analysis_id=identity.analysis_id,
        analysis_kind=recipe.analysis_kind,
        request=canonical_request,
        account_scope=inputs.account_scope,
        as_of=as_of,
        valid_until=profile.valid_until or datetime.now(UTC),
        metrics=metrics,
        tables=payload.tables,
        unavailable_fields=payload.unavailable_fields,
        warnings=warning_models,
        data_quality=DataQuality(
            state=quality,
            coverage=DataCoverage(
                state=quality,
                start_at=material.coverage_start,
                end_at=material.coverage_end,
                row_count=dataset.row_count,
                expected_row_count=dataset.row_count if quality is QualityState.COMPLETE else None,
                missing_row_count=0,
            ),
            checked_at=as_of,
            warnings=warning_models,
        ),
        provenance=AnalysisProvenance(
            dataset_id=dataset.dataset_id,
            source_scope="saxo_openapi",
            source_revision=dataset.source_revision,
            source_contract_sha256=source_binding.source_contract_sha256,
            source_contract_sha256s=source_binding.source_contract_sha256s,
            source_timestamp=as_of,
            proof_receipts=(
                ActiveProofReceipt(
                    state="active",
                    analysis_kind=recipe.analysis_kind,
                    schema_version="1",
                    source_binding=source_binding,
                    engine_binding=engine_binding,
                    proof_profile_id=profile.proof_profile_id,
                    checks_passed=PROOF_CHECKS,
                ),
            ),
            engine_name=engine.engine_name,
            engine_version=engine.engine_version,
            code_commit=engine.code_commit,
            analysis_input_sha256=identity.input_sha256,
            analysis_parameters_sha256=parameter_hash,
            analysis_engine_sha256=identity.engine_sha256,
            analysis_seed_sha256=identity.seed_sha256,
            random_seed=seed,
            analysis_dependencies=parameters.analysis_dependencies,
            input_dataset_dependencies=parameters.input_dataset_dependencies,
        ),
        assumptions=assumptions,
        is_not_advice=True,
        is_not_forecast=payload.is_not_forecast,
        model_distribution_only=payload.model_distribution_only,
        verifies=payload.verifies,
        does_not_verify=payload.does_not_verify,
        replayable=True,
        next_actions=("Explain or export this exact saved result.",),
    )
    inputs.check_cancellation()
    if persist:
        store.put_analysis(result)
    return result
