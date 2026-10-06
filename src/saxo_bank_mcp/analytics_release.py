"""Installed recipe activation bound to a checked-in verification receipt and exact code."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from typing import cast

from saxo_bank_mcp.analytics_chart_semantics import STORED_TABLE_TEMPLATES
from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofRegistry,
    SourceContractProofBinding,
    load_proof_profile_catalog,
)
from saxo_bank_mcp.analytics_runtime import FAMILIES, PROOF_CHECKS, RECIPE_KINDS
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)


def runtime_code_sha256() -> str:
    """Hash installed implementation bytes rather than an unrelated Git checkout revision."""
    package = files("saxo_bank_mcp")
    names = sorted(
        item.name
        for item in package.iterdir()
        if item.name.endswith(".py")
        and (
            item.name.startswith("analytics_")
            or item.name in {"mcp_analytics_tools.py", "server.py", "server_tool_registration.py"}
        )
    )
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + package.joinpath(name).read_bytes() + b"\0")
    for dependency in ("duckdb", "fastmcp", "matplotlib", "numpy", "plotly", "pydantic", "scipy"):
        digest.update(dependency.encode() + b"\0" + version(dependency).encode() + b"\0")
    return digest.hexdigest()


def _release_document() -> object:
    path = Path(__file__).resolve().parents[2] / "data/analytics/production_release.json"
    try:
        value = (
            path.read_text()
            if path.is_file()
            else files("saxo_bank_mcp")
            .joinpath("_analytics_production/production_release.json")
            .read_text()
        )
        return json.loads(value)
    except (OSError, ValueError):
        return None


def build_recipe_registry(
    config: AnalyticsConfig,
    *,
    verified: bool,
    code_sha256: str,
) -> ProofRegistry:
    """Build narrow registrations. Only the release loader supplies production verification."""
    definitions = load_metric_definition_catalog()
    legacy = load_proof_profile_catalog(definitions=definitions)
    by_id = definitions.by_id()
    source_contracts = source_contracts_by_id()
    profiles: list[ProofProfile] = []
    for family in FAMILIES:
        for kind in sorted(family.SUPPORTED_KINDS):
            metric_ids = family.METRIC_IDS[kind]
            bindings = tuple(
                MetricDefinitionBinding(
                    metric_id=identifier, definition_version=by_id[identifier].definition_version
                )
                for identifier in metric_ids
            )
            sources = tuple(
                SourceContractProofBinding(
                    contract_id=identifier,
                    contract_sha256=source_contract_fingerprint(source_contracts[identifier]),
                    field_paths=tuple(field.name for field in source_contracts[identifier].fields),
                )
                for identifier in family.SOURCE_CONTRACTS[kind]
            )
            additional = sources
            profiles.append(
                ProofProfile(
                    proof_profile_id=f"vp_production_{kind}_v1",
                    profile_version="1",
                    activation_state=ProfileActivationState.ACTIVE
                    if verified
                    else ProfileActivationState.QUARANTINED,
                    quarantine_reason=None if verified else "release_unverified",
                    analysis_kind=kind,
                    schema_version="1",
                    metric_definitions=bindings,
                    required_metric_ids=family.REQUIRED_METRICS[kind],
                    source_contracts=sources,
                    additional_source_contracts=additional,
                    model_metric_ids=metric_ids
                    if family.__name__.endswith("runtime_models") or kind == "fixed_income"
                    else (),
                    approximation_metric_ids=("do_nothing_counterfactual",)
                    if kind == "portfolio_time_machine"
                    else (),
                    source_revision=None,
                    source_revision_scope="contract",
                    engines=(
                        EngineProofBinding(
                            engine_name="saxo_analytics",
                            engine_version="1-production",
                            code_commit=code_sha256,
                        ),
                    ),
                    artifact_template_ids=STORED_TABLE_TEMPLATES,
                    definition_catalog_sha256=definitions.fingerprint_sha256,
                    source_catalog_sha256=source_contract_catalog_sha256(),
                    valid_until=datetime(2027, 10, 4, tzinfo=UTC) if verified else None,
                )
            )
    # Preserve any catalogued route which the runtime does not yet own as quarantined.
    profiles.extend(
        profile for profile in legacy.profiles if profile.analysis_kind not in RECIPE_KINDS
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version="1-production",
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        production_metric_ids=legacy.production_metric_ids,
        production_analysis_kinds=tuple(sorted(profile.analysis_kind for profile in profiles)),
        production_artifact_template_ids=(
            *legacy.production_artifact_template_ids,
            *STORED_TABLE_TEMPLATES,
        ),
        artifact_owners=legacy.artifact_owners,
        source_field_coverage=legacy.source_field_coverage,
        profiles=tuple(sorted(profiles, key=lambda profile: profile.analysis_kind)),
    )
    return ProofRegistry(definitions=definitions, catalog=catalog, config=config)


def load_production_registry(config: AnalyticsConfig) -> ProofRegistry:
    """Activate only the verified installed release; changed code remains quarantined."""
    code_hash = runtime_code_sha256()
    document = _release_document()
    verified = False
    if isinstance(document, dict):
        receipt = cast("dict[str, object]", document)
        verified = (
            receipt.get("code_sha256") == code_hash
            and receipt.get("definition_catalog_sha256")
            == load_metric_definition_catalog().fingerprint_sha256
            and receipt.get("source_catalog_sha256") == source_contract_catalog_sha256()
            and receipt.get("recipe_kinds") == list(RECIPE_KINDS)
            and receipt.get("checks_passed") == list(PROOF_CHECKS)
            and receipt.get("suite_passed") is True
        )
    return build_recipe_registry(config, verified=verified, code_sha256=code_hash)
