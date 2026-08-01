from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    MetricDefinitionCatalog,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    CoverageError,
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofRegistry,
    ProofState,
    SourceContractProofBinding,
    generate_coverage_matrix,
    load_proof_profile_catalog,
    quarantine_analysis_kind,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MINIMUM_METRIC_COUNT = 120
_SHA256_LENGTH = 64


def _active_registry() -> tuple[ProofRegistry, ProofProfile, dict[str, str]]:
    definitions = load_metric_definition_catalog()
    definition = definitions.by_id()["price_return"]
    contract = source_contracts_by_id()["chart_v3"]
    source_binding = SourceContractProofBinding(
        contract_id=contract.contract_id,
        contract_sha256=source_contract_fingerprint(contract),
        field_paths=("CloseBid", "Time"),
    )
    profile = ProofProfile(
        proof_profile_id="vp_unit_test_analysis_v1",
        profile_version="1",
        activation_state=ProfileActivationState.ACTIVE,
        quarantine_reason=None,
        analysis_kind="unit_test_analysis",
        schema_version="1",
        metric_definitions=(
            MetricDefinitionBinding(
                metric_id=definition.metric_id,
                definition_version=definition.definition_version,
            ),
        ),
        source_contracts=(source_binding,),
        source_revision="revision-a",
        engines=(
            EngineProofBinding(
                engine_name="saxo_analytics",
                engine_version="1",
                code_commit="abcdef0",
            ),
        ),
        artifact_template_ids=(),
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        valid_until=_NOW + timedelta(days=1),
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version="unit-test-1",
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        production_metric_ids=(definition.metric_id,),
        production_analysis_kinds=(profile.analysis_kind,),
        production_artifact_template_ids=(),
        source_field_coverage=(source_binding,),
        profiles=(profile,),
    )
    return (
        ProofRegistry(definitions=definitions, catalog=catalog),
        profile,
        {contract.contract_id: source_binding.contract_sha256},
    )


def test_metric_definitions_are_versioned_and_machine_readable() -> None:
    catalog = load_metric_definition_catalog()

    assert catalog.schema_version == "1"
    assert set(catalog.production_metric_ids) == set(catalog.by_id())
    assert len(catalog.production_metric_ids) >= _MINIMUM_METRIC_COUNT
    assert len(catalog.fingerprint_sha256) == _SHA256_LENGTH
    for definition in catalog.definitions:
        assert definition.definition_version
        assert definition.meaning
        assert definition.formula
        assert definition.input_units
        assert definition.output_unit
        assert definition.sign_convention
        assert definition.timing_convention
        assert definition.cash_flow_treatment
        assert definition.fx_treatment.source
        assert definition.fx_treatment.direction
        assert definition.fx_treatment.timestamp
        assert definition.missing_data_policy
        assert definition.tolerance.mode
        assert definition.independent_reference
        assert definition.broker_reconciliation
        assert definition.analysis_kinds


def test_checked_in_profiles_cover_every_declared_surface_and_current_source() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    matrix = generate_coverage_matrix(definitions=definitions, catalog=catalog)

    assert matrix.complete is True
    assert matrix.missing_metric_ids == ()
    assert matrix.missing_analysis_kinds == ()
    assert matrix.missing_artifact_template_ids == ()
    assert matrix.missing_source_fields == ()
    assert catalog.definition_catalog_sha256 == definitions.fingerprint_sha256
    assert catalog.source_catalog_sha256 == source_contract_catalog_sha256()
    assert all(
        profile.activation_state is ProfileActivationState.QUARANTINED
        and profile.quarantine_reason == "implementation_pending"
        for profile in catalog.profiles
    )
    source_kinds = {
        kind
        for contract in source_contracts_by_id().values()
        for kind in contract.dependent_analysis_kinds
    }
    assert source_kinds <= set(catalog.production_analysis_kinds)


def test_clean_wheel_loads_metric_and_proof_catalogs_outside_repository(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    uv_path = shutil.which("uv")
    assert uv_path is not None
    build = subprocess.run(
        (
            uv_path,
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ),
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        installed_files = set(archive.namelist())
    assert "saxo_bank_mcp/_analytics_metric_definitions/metric_definitions.json" in installed_files
    assert "saxo_bank_mcp/_analytics_proof_profiles/proof_profiles.json" in installed_files

    script = """
from saxo_bank_mcp.analytics_metric_definitions import load_metric_definition_catalog
from saxo_bank_mcp.analytics_proof_profiles import load_proof_profile_catalog

definitions = load_metric_definition_catalog()
proofs = load_proof_profile_catalog(definitions=definitions)
print(f"{len(definitions.definitions)}:{len(proofs.profiles)}")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(wheel)
    loaded = subprocess.run(
        (sys.executable, "-c", script),
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == "206:54"


def test_coverage_fails_when_a_production_metric_has_no_definition() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    missing = definitions.production_metric_ids[0]
    incomplete = definitions.model_copy(
        update={
            "definitions": tuple(
                definition
                for definition in definitions.definitions
                if definition.metric_id != missing
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        generate_coverage_matrix(definitions=incomplete, catalog=catalog)

    assert raised.value.matrix.missing_metric_ids == (missing,)


def test_coverage_fails_when_a_production_kind_has_no_profile() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    missing = catalog.production_analysis_kinds[0]
    incomplete = catalog.model_copy(
        update={
            "profiles": tuple(
                profile for profile in catalog.profiles if profile.analysis_kind != missing
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        generate_coverage_matrix(definitions=definitions, catalog=incomplete)

    assert raised.value.matrix.missing_analysis_kinds == (missing,)


def test_coverage_fails_when_an_artifact_or_source_field_loses_proof() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    artifact_id = catalog.production_artifact_template_ids[0]
    artifact_incomplete = catalog.model_copy(
        update={
            "profiles": tuple(
                profile.model_copy(
                    update={
                        "artifact_template_ids": tuple(
                            item for item in profile.artifact_template_ids if item != artifact_id
                        ),
                    },
                )
                for profile in catalog.profiles
            ),
        },
    )

    with pytest.raises(CoverageError) as artifact_error:
        generate_coverage_matrix(definitions=definitions, catalog=artifact_incomplete)

    assert artifact_error.value.matrix.missing_artifact_template_ids == (artifact_id,)

    source = catalog.source_field_coverage[0]
    missing_path = source.field_paths[0]
    source_incomplete = catalog.model_copy(
        update={
            "source_field_coverage": (
                source.model_copy(
                    update={
                        "field_paths": tuple(
                            path for path in source.field_paths if path != missing_path
                        ),
                    },
                ),
                *catalog.source_field_coverage[1:],
            ),
        },
    )

    with pytest.raises(CoverageError) as source_error:
        generate_coverage_matrix(definitions=definitions, catalog=source_incomplete)

    assert source_error.value.matrix.missing_source_fields == (
        f"{source.contract_id}.{missing_path}",
    )


def test_definition_change_without_profile_rebinding_is_stale() -> None:
    registry, profile, source_contracts = _active_registry()
    changed = registry.definitions.by_id()["price_return"].model_copy(
        update={"formula": "changed formula without a new proof binding"},
    )
    changed_definitions = MetricDefinitionCatalog.from_definitions(
        catalog_version="unit-test-changed",
        production_metric_ids=registry.definitions.production_metric_ids,
        definitions=(
            changed,
            *tuple(
                definition
                for definition in registry.definitions.definitions
                if definition.metric_id != changed.metric_id
            ),
        ),
    )
    changed_registry = ProofRegistry(
        definitions=changed_definitions,
        catalog=registry.catalog,
    )

    status = changed_registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )

    assert status.state is ProofState.STALE
    assert status.reason_code == "definition_catalog_changed"


def test_source_revision_engine_change_and_expiry_make_proof_stale() -> None:
    registry, profile, source_contracts = _active_registry()

    active = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )
    revised = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-b",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )
    engine_changed = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("2", "abcdef0")},
        at=_NOW,
    )
    commit_changed = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef1")},
        at=_NOW,
    )
    assert profile.valid_until is not None
    expired = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=profile.valid_until + timedelta(microseconds=1),
    )

    assert active.state is ProofState.ACTIVE
    assert revised.reason_code == "source_revision_changed"
    assert engine_changed.reason_code == "engine_binding_changed"
    assert commit_changed.reason_code == "engine_binding_changed"
    assert expired.reason_code == "proof_expired"
    assert all(
        item.state is ProofState.STALE
        for item in (revised, engine_changed, commit_changed, expired)
    )


def test_missing_proof_refuses_and_runtime_quarantine_fails_closed() -> None:
    registry, profile, source_contracts = _active_registry()
    missing = registry.status("missing_unit_test_analysis", "1", {})

    quarantine_analysis_kind(profile.analysis_kind, "schema drift")
    quarantined = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )

    assert missing.state is ProofState.REFUSED
    assert missing.reason_code == "missing_proof_profile"
    assert quarantined.state is ProofState.QUARANTINED
    assert quarantined.reason_code == "schema_drift"


def test_runtime_quarantine_does_not_echo_arbitrary_private_reason() -> None:
    registry, profile, source_contracts = _active_registry()

    quarantine_analysis_kind(profile.analysis_kind, "private account 123456")
    quarantined = registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )

    assert quarantined.state is ProofState.QUARANTINED
    assert quarantined.reason_code == "runtime_quarantine"
    assert "123456" not in quarantined.reason_code
