"""Exact process-local proof profiles for installed SIM matrix executors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    MetricDefinitionCatalog,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    SourceContractProofBinding,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)

_EXECUTOR_METRIC_IDS: Final[dict[str, tuple[str, ...]]] = {
    "bounded_backtest": ("total_return",),
    "derivatives_model": ("theoretical_option_value",),
    "instrument_price_return": ("price_return",),
    "margin_fire_drill": ("combined_scenario_effect",),
    "market_comparison": ("price_return",),
    "portfolio_minimum_variance": ("minimum_variance_objective",),
    "portfolio_performance": ("time_weighted_return",),
    "portfolio_risk_parity": ("risk_parity_contribution",),
    "portfolio_scenario": ("custom_shock_effect",),
    "position_sizing": ("position_size",),
    "pretrade_impact": ("estimated_transaction_cost", "maximum_loss"),
    "scenario_combined": ("combined_scenario_effect",),
    "scenario_currency": ("currency_shock_effect",),
    "scenario_custom": ("custom_shock_effect",),
    "scenario_rate": ("rate_shock_effect",),
    "scenario_volatility": ("volatility_shock_effect",),
}


def _metric_bindings(
    definitions: MetricDefinitionCatalog,
    metric_ids: tuple[str, ...],
) -> tuple[tuple[MetricDefinitionBinding, ...], tuple[SourceContractProofBinding, ...]]:
    definitions_by_id = definitions.by_id()
    fields: dict[str, set[str]] = {}
    metric_bindings: list[MetricDefinitionBinding] = []
    for metric_id in metric_ids:
        metric = definitions_by_id[metric_id]
        metric_bindings.append(
            MetricDefinitionBinding(
                metric_id=metric.metric_id,
                definition_version=metric.definition_version,
            ),
        )
        for binding in metric.input_bindings:
            if binding.source_contract_id is not None:
                fields.setdefault(binding.source_contract_id, set()).update(
                    binding.field_paths,
                )
    contracts = source_contracts_by_id()
    source_bindings = tuple(
        SourceContractProofBinding(
            contract_id=contract_id,
            contract_sha256=source_contract_fingerprint(contracts[contract_id]),
            field_paths=tuple(sorted(field_paths)),
        )
        for contract_id, field_paths in sorted(fields.items())
    )
    return tuple(metric_bindings), source_bindings


def _runtime_profile(
    profile: ProofProfile,
    *,
    definitions: MetricDefinitionCatalog,
) -> ProofProfile:
    metric_ids = _EXECUTOR_METRIC_IDS.get(profile.analysis_kind)
    if metric_ids is None:
        return profile
    metric_bindings, source_bindings = _metric_bindings(definitions, metric_ids)
    return profile.model_copy(
        update={
            "metric_definitions": metric_bindings,
            "source_contracts": source_bindings,
        },
    )


def process_active_catalog(
    catalog: ProofProfileCatalog,
    *,
    definitions: MetricDefinitionCatalog,
    candidate_commit: str,
    allowed_kinds: frozenset[str],
    source_revisions: dict[str, str],
) -> ProofProfileCatalog:
    """Activate only exact metrics that each installed runtime executor emits."""
    profiles = list(catalog.profiles)
    if "bounded_backtest" in allowed_kinds and not any(
        profile.analysis_kind == "bounded_backtest" for profile in profiles
    ):
        metric_bindings, source_bindings = _metric_bindings(
            definitions,
            _EXECUTOR_METRIC_IDS["bounded_backtest"],
        )
        profiles.append(
            ProofProfile(
                proof_profile_id="vp_bounded_backtest_runtime_v1",
                profile_version="1",
                activation_state=ProfileActivationState.QUARANTINED,
                quarantine_reason="authenticated_ghost_required",
                analysis_kind="bounded_backtest",
                schema_version="1",
                metric_definitions=metric_bindings,
                source_contracts=source_bindings,
                source_revision=None,
                engines=(),
                artifact_template_ids=(),
                definition_catalog_sha256=definitions.fingerprint_sha256,
                source_catalog_sha256=source_contract_catalog_sha256(),
                valid_until=None,
            ),
        )
    active_profiles: list[ProofProfile] = []
    for original_profile in profiles:
        revision = source_revisions.get(original_profile.analysis_kind)
        if original_profile.analysis_kind not in allowed_kinds or revision is None:
            active_profiles.append(original_profile)
            continue
        profile = _runtime_profile(original_profile, definitions=definitions)
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
                },
            ),
        )
    return catalog.model_copy(
        update={
            "production_analysis_kinds": tuple(
                dict.fromkeys(
                    (*catalog.production_analysis_kinds, *(p.analysis_kind for p in profiles)),
                ),
            ),
            "production_metric_ids": tuple(
                dict.fromkeys(
                    (
                        *catalog.production_metric_ids,
                        *(
                            binding.metric_id
                            for profile in profiles
                            for binding in profile.metric_definitions
                        ),
                    ),
                ),
            ),
            "profiles": tuple(active_profiles),
        },
    )
