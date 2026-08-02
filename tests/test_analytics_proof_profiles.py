from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp import analytics_proof_profiles as proof_profiles_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinition,
    MetricDefinitionBinding,
    MetricDefinitionCatalog,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_models import MetricClass
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
_EXACT_METRIC_COUNT = 206
_EXACT_ANALYSIS_KIND_COUNT = 54
_SHA256_LENGTH = 64
_OWNER_FILE_MODE = 0o600
_OWNER_DIRECTORY_MODE = 0o700
_PLACEHOLDER_FORMULA_PATTERNS = (
    r"\bmodel\(",
    r"\bsupported-model\b",
    r"\bblack-scholes or black-76\b",
    r"\bselected by (?:the )?product contract\b",
    r"\buse saxo accountvalue when\b",
    r"\bor reconciled compatible\b",
    r"\bselected by the source contract\b",
    r"\bdo not sum alternatives\b",
    r"\bdeclared distribution\b",
    r"\breprice supported\b",
    r"\bmodeled supported\b",
    r"\bmodel_value\(",
    r"\bcurrent modeled price\b",
    r"\bwhen required\b",
    r"\bother declared eligible components\b",
    r"\buse the eligible saxo margin value\b",
    r"\bcompatible\b",
    r"\bfor each aligned period or the declared aggregate\b",
    r"\bbound unbooked or settlement-reserved\b",
    r"\brequested gross or net basis\b",
    r"\beligible daily or period carrying costs\b",
    r"\bdeclared bid, ask, or midpoint rule\b",
    r"\bdeclared open, close, high, or low\b",
    r"\bordered strike or delta pairs\b",
)
_INDEXED_SUFFIX_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*_([a-z])\b")
_INDEXED_EXPRESSION_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_]*_\(([^)]*)\)",
)
_INDEXED_REDUCER_PATTERN = re.compile(r"\b(?:sum|product|max|min)_\{([^}]*)\}")
_STANDALONE_INDEX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([a-z])(?![A-Za-z0-9_])",
)


@pytest.fixture(autouse=True)
def _isolate_default_quarantine_state(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "default-state"))


def _active_registry(
    *,
    config: AnalyticsConfig | None = None,
) -> tuple[ProofRegistry, ProofProfile, dict[str, str]]:
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
    registry = (
        ProofRegistry(definitions=definitions, catalog=catalog)
        if config is None
        else ProofRegistry(definitions=definitions, catalog=catalog, config=config)
    )
    return (
        registry,
        profile,
        {contract.contract_id: source_binding.contract_sha256},
    )


def _formula_indexes(formula: str) -> frozenset[str]:
    indexes = {match.group(1) for match in _INDEXED_SUFFIX_PATTERN.finditer(formula)}
    for pattern in (_INDEXED_EXPRESSION_PATTERN, _INDEXED_REDUCER_PATTERN):
        for match in pattern.finditer(formula):
            indexes.update(_STANDALONE_INDEX_PATTERN.findall(match.group(1)))
    return frozenset(indexes)


def _formula_defines_index(formula: str, index: str) -> bool:
    return bool(
        re.search(rf"\b{index.upper()}(?:_[a-z])?\s*=\s*\{{", formula)
        or re.search(rf"\b{index}\s+in\s+[A-Z](?:_[a-z])?\b", formula)
    )


def _with_added_metric_source_field(
    definitions: MetricDefinitionCatalog,
    *,
    metric_id: str,
    contract_id: str,
    field_path: str,
) -> MetricDefinitionCatalog:
    definition = definitions.by_id()[metric_id]
    changed_bindings = tuple(
        binding.model_copy(
            update={"field_paths": (*binding.field_paths, field_path)},
        )
        if binding.source_contract_id == contract_id
        else binding
        for binding in definition.input_bindings
    )
    changed_definition = definition.model_copy(
        update={"input_bindings": changed_bindings},
    )
    return MetricDefinitionCatalog.from_definitions(
        catalog_version=definitions.catalog_version,
        production_metric_ids=definitions.production_metric_ids,
        definitions=tuple(
            changed_definition if item.metric_id == metric_id else item
            for item in definitions.definitions
        ),
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
        assert definition.input_bindings
        assert all(
            binding.source_contract_id is not None or binding.derived_input is not None
            for binding in definition.input_bindings
        )

    named = catalog.by_id()
    assert named["money_weighted_return"].formula == (
        "I={the complete ordered external cash-flow ledger entries}; solve for r>-1: "
        "0=-V_start+sum_{i in I}((withdrawal_i-deposit_i)/(1+r)^t_i)+"
        "V_end/(1+r)^t_end; deposits are negative investor cash flows, "
        "withdrawals and terminal value are positive."
    )
    assert named["xirr"].formula == (
        "I={the complete ordered external cash-flow ledger entries}; solve for r>-1: "
        "0=-V_start+sum_{i in I}((withdrawal_i-deposit_i)/(1+r)^"
        "((date_i-date_0).days/365))+V_end/(1+r)^"
        "((date_end-date_0).days/365); deposits are negative and withdrawals plus "
        "terminal value are positive."
    )
    assert "weighted_sum" not in named["liquidity_score"].formula
    assert "declared" not in named["market_depth"].formula
    assert "solver decision variable" not in named["target_weight"].formula
    assert "declared compounding" not in named["convexity"].formula
    assert named["maximum_drawdown"].formula == (
        "T={the complete strictly UTC-ascending eligible valuation timestamps after applying "
        "the exact profile-bound missing-data policy}; maximum_drawdown=min_{t in T}(V_t/"
        "max_{s in T,s<=t}(V_s)-1); refuse empty T or any nonpositive running peak; result "
        "is in [-1,0]."
    )


def test_metric_formulas_reject_placeholder_models_and_bind_exact_branches() -> None:
    named = load_metric_definition_catalog().by_id()
    offenders = {
        definition.metric_id: pattern
        for definition in named.values()
        for pattern in _PLACEHOLDER_FORMULA_PATTERNS
        if re.search(pattern, definition.formula, flags=re.IGNORECASE)
    }

    assert offenders == {}
    assert named["sensitivity_lower"].formula == (
        "E(lower_parameter_set, random_seed), where E is the exact equation bound by "
        "engine_name, engine_version, code_commit, and branch_id; lower_parameter_set is a "
        "complete ordered vector persisted in the request; refuse a missing parameter, changed "
        "branch_id, or changed seed."
    )
    assert named["account_value"].formula == (
        "account_value = the unique balances_v1.TotalValue row whose account scope exactly "
        "equals the request account_scope at cutoff; refuse when zero or multiple exact-scope "
        "rows exist."
    )
    option_formula = named["theoretical_option_value"].formula
    assert "pricing_model=black_scholes" in option_formula
    assert "pricing_model=black_76" in option_formula
    assert "call=S*exp(-q*T)*N(d1)-K*exp(-r*T)*N(d2)" in option_formula
    assert "call=exp(-r*T)*(F*N(d1)-K*N(d2))" in option_formula


def test_metric_formulas_define_every_index_set_and_exact_cash_shock_equations() -> None:
    named = load_metric_definition_catalog().by_id()
    undefined_indexes = {
        definition.metric_id: tuple(
            sorted(
                index
                for index in _formula_indexes(definition.formula)
                if not _formula_defines_index(definition.formula, index)
            ),
        )
        for definition in named.values()
        if any(
            not _formula_defines_index(definition.formula, index)
            for index in _formula_indexes(definition.formula)
        )
    }

    assert undefined_indexes == {}
    assert named["cash_balance"].formula == (
        "C={the unique balances_v1.CashBalance value for each Currency row whose account "
        "scope exactly equals request account_scope at cutoff}; cash_balance=sum_{c in C} "
        "CashBalance_c*fx_c_to_reporting_at_request_fx_timestamp; exclude TotalValue, "
        "CashAvailableForTrading, MarginAvailableForTrading, FundsAvailableForSettlement, "
        "FundsReservedForSettlement, and every other balance field; refuse a missing or "
        "duplicate account/currency row."
    )
    assert named["custom_shock_effect"].formula == (
        "I={the ordered components in the exact persisted portfolio snapshot}; require the "
        "stored shock_map keys to equal I; branch_id=linear sets shocked_value_i="
        "base_value_i*(1+price_shock_i), branch_id=option evaluates theoretical_option_value "
        "with the complete shocked parameter vector, and branch_id=fixed_income sets "
        "shocked_value_i=sum_{j in J_i}(CF_ij*shocked_discount_factor_ij) where J_i={the "
        "ordered cash flows for component i}; component_effect_i=(shocked_value_i-"
        "base_value_i)*fx_i_to_reporting_at_request_fx_timestamp+cash_flow_shock_i; "
        "custom_shock_effect=sum_{i in I}(component_effect_i); refuse every other branch_id, "
        "missing component, or extra shock_map key."
    )


def test_metric_definition_rejects_undefined_formula_indexes() -> None:
    definition = load_metric_definition_catalog().by_id()["maximum_drawdown"]

    with pytest.raises(ValidationError, match=r"undefined formula indices.*s.*t"):
        MetricDefinition.model_validate(
            definition.model_dump(mode="python") | {"formula": "min_t(V_t / max_(s<=t)(V_s) - 1)"},
        )


def test_metric_definition_rejects_undefined_compound_formula_index() -> None:
    definition = load_metric_definition_catalog().by_id()["maximum_drawdown"]

    with pytest.raises(ValidationError, match=r"undefined formula indices.*i"):
        MetricDefinition.model_validate(
            definition.model_dump(mode="python")
            | {"formula": "J={the complete ordered cash flows}; sum_{j in J}(CF_ij)."},
        )


@pytest.mark.parametrize(
    "indexed_value",
    ["cash_flow_ij", "shocked_discount_factor_ij"],
)
def test_metric_definition_rejects_undefined_lowercase_compound_formula_index(
    indexed_value: str,
) -> None:
    definition = load_metric_definition_catalog().by_id()["maximum_drawdown"]

    with pytest.raises(ValidationError, match=r"undefined formula indices.*i"):
        MetricDefinition.model_validate(
            definition.model_dump(mode="python")
            | {
                "formula": (
                    f"J={{the complete ordered cash flows}}; sum_{{j in J}}({indexed_value})."
                ),
            },
        )


def test_metric_definition_accepts_ordinary_identifier_suffix() -> None:
    definition = load_metric_definition_catalog().by_id()["maximum_drawdown"]
    formula = "J={the complete ordered values}; sum_{j in J}(value_j)+subject_up; branch_id=linear."

    validated = MetricDefinition.model_validate(
        definition.model_dump(mode="python") | {"formula": formula},
    )

    assert validated.formula == formula


def test_metric_definition_accepts_current_catalog_compound_formula() -> None:
    definition = load_metric_definition_catalog().by_id()["custom_shock_effect"]

    assert MetricDefinition.model_validate(definition.model_dump(mode="python")) == definition


def test_price_and_execution_metric_classes_match_their_calculation_origin() -> None:
    named = load_metric_definition_catalog().by_id()
    expected = {
        "arrival_price": MetricClass.BROKER_REPORTED,
        "midpoint_price": MetricClass.CALCULATED_VERIFIED,
        "vwap": MetricClass.CALCULATED_VERIFIED,
        "bar_approximation_price": MetricClass.APPROXIMATION,
        "slippage": MetricClass.CALCULATED_VERIFIED,
        "spread": MetricClass.CALCULATED_VERIFIED,
        "quote_delay": MetricClass.CALCULATED_VERIFIED,
        "volume": MetricClass.BROKER_REPORTED,
        "volume_weighted_price": MetricClass.CALCULATED_VERIFIED,
    }

    assert {metric_id: named[metric_id].default_metric_class for metric_id in expected} == expected


def test_checked_in_profiles_cover_every_declared_surface_and_current_source() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    matrix = proof_profiles_module.generate_declared_coverage_matrix(
        definitions=definitions,
        catalog=catalog,
    )

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

    definitions_by_id = definitions.by_id()
    source_count = len(source_contracts_by_id())
    for profile in catalog.profiles:
        profile_sources = {
            binding.contract_id: frozenset(binding.field_paths)
            for binding in profile.source_contracts
        }
        expected_sources: dict[str, set[str]] = {}
        for metric_binding in profile.metric_definitions:
            for input_binding in definitions_by_id[metric_binding.metric_id].input_bindings:
                if input_binding.source_contract_id is None:
                    continue
                expected_sources.setdefault(input_binding.source_contract_id, set()).update(
                    input_binding.field_paths,
                )
        assert profile_sources == {
            contract_id: frozenset(field_paths)
            for contract_id, field_paths in expected_sources.items()
        }
        assert len(profile_sources) < source_count

    with pytest.raises(CoverageError) as inactive:
        generate_coverage_matrix(definitions=definitions, catalog=catalog)
    assert set(inactive.value.matrix.inactive_analysis_kinds) == set(
        catalog.production_analysis_kinds,
    )


def test_active_profile_requires_exact_nonempty_source_contract_fields() -> None:
    _, profile, _ = _active_registry()

    with pytest.raises(ValidationError):
        ProofProfile.model_validate(
            profile.model_dump(mode="python") | {"source_contracts": ()},
        )


def test_active_profile_must_cover_each_metric_source_field() -> None:
    registry, profile, source_contracts = _active_registry()
    incomplete_profile = profile.model_copy(
        update={
            "source_contracts": (
                profile.source_contracts[0].model_copy(update={"field_paths": ("Time",)}),
            ),
        },
    )
    incomplete_registry = ProofRegistry(
        definitions=registry.definitions,
        catalog=registry.catalog.model_copy(update={"profiles": (incomplete_profile,)}),
    )

    status = incomplete_registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )

    assert status.state is ProofState.STALE
    assert status.reason_code == "metric_source_binding_changed"


def test_active_profile_rejects_an_overbroad_metric_source_field() -> None:
    registry, profile, source_contracts = _active_registry()
    overbroad_profile = profile.model_copy(
        update={
            "source_contracts": (
                profile.source_contracts[0].model_copy(
                    update={"field_paths": ("CloseBid", "Time", "UnexpectedField")},
                ),
            ),
        },
    )
    overbroad_registry = ProofRegistry(
        definitions=registry.definitions,
        catalog=registry.catalog.model_copy(update={"profiles": (overbroad_profile,)}),
    )

    status = overbroad_registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )

    assert status.state is ProofState.STALE
    assert status.reason_code == "metric_source_binding_changed"


def test_active_profile_refuses_a_consistently_bound_nonexistent_source_field() -> None:
    registry, profile, source_contracts = _active_registry()
    changed_definitions = _with_added_metric_source_field(
        registry.definitions,
        metric_id="price_return",
        contract_id="chart_v3",
        field_path="UnexpectedField",
    )
    changed_source = profile.source_contracts[0].model_copy(
        update={"field_paths": (*profile.source_contracts[0].field_paths, "UnexpectedField")},
    )
    changed_profile = profile.model_copy(
        update={
            "definition_catalog_sha256": changed_definitions.fingerprint_sha256,
            "source_contracts": (changed_source,),
        },
    )
    changed_registry = ProofRegistry(
        definitions=changed_definitions,
        catalog=registry.catalog.model_copy(
            update={
                "definition_catalog_sha256": changed_definitions.fingerprint_sha256,
                "source_field_coverage": (changed_source,),
                "profiles": (changed_profile,),
            },
        ),
    )

    status = changed_registry.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )

    assert status.state is ProofState.REFUSED
    assert status.reason_code == "metric_source_field_missing"


def test_declared_coverage_rejects_an_overbroad_profile_source_field() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    owner = next(profile for profile in catalog.profiles if profile.source_contracts)
    source = owner.source_contracts[0]
    overbroad_owner = owner.model_copy(
        update={
            "source_contracts": (
                source.model_copy(
                    update={"field_paths": (*source.field_paths, "UnexpectedField")},
                ),
                *owner.source_contracts[1:],
            ),
        },
    )
    overbroad_catalog = catalog.model_copy(
        update={
            "profiles": tuple(
                overbroad_owner if profile is owner else profile for profile in catalog.profiles
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=overbroad_catalog,
        )

    assert raised.value.matrix.overbroad_source_fields == (
        f"{owner.analysis_kind}:{source.contract_id}.UnexpectedField",
    )


def test_declared_coverage_rejects_a_consistently_bound_nonexistent_source_field() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    changed_definitions = _with_added_metric_source_field(
        definitions,
        metric_id="price_return",
        contract_id="chart_v3",
        field_path="UnexpectedField",
    )
    owner_kinds = frozenset(definitions.by_id()["price_return"].analysis_kinds)
    changed_profiles = tuple(
        profile.model_copy(
            update={
                "definition_catalog_sha256": changed_definitions.fingerprint_sha256,
                "source_contracts": tuple(
                    source.model_copy(
                        update={"field_paths": (*source.field_paths, "UnexpectedField")},
                    )
                    if profile.analysis_kind in owner_kinds and source.contract_id == "chart_v3"
                    else source
                    for source in profile.source_contracts
                ),
            },
        )
        for profile in catalog.profiles
    )
    changed_catalog = catalog.model_copy(
        update={
            "definition_catalog_sha256": changed_definitions.fingerprint_sha256,
            "source_field_coverage": tuple(
                source.model_copy(
                    update={"field_paths": (*source.field_paths, "UnexpectedField")},
                )
                if source.contract_id == "chart_v3"
                else source
                for source in catalog.source_field_coverage
            ),
            "profiles": changed_profiles,
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=changed_definitions,
            catalog=changed_catalog,
        )

    assert {f"{kind}:chart_v3.UnexpectedField" for kind in owner_kinds}.issubset(
        raised.value.matrix.missing_source_fields
    )


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
    assert (
        "saxo_bank_mcp/_analytics_vision_requirements/vision_coverage_requirements.json"
        in installed_files
    )

    script = """
from saxo_bank_mcp.analytics_metric_definitions import load_metric_definition_catalog
from saxo_bank_mcp.analytics_proof_profiles import load_proof_profile_catalog
from saxo_bank_mcp.analytics_vision_requirements import load_vision_coverage_requirements

definitions = load_metric_definition_catalog()
proofs = load_proof_profile_catalog(definitions=definitions)
requirements = load_vision_coverage_requirements()
print(f"{len(definitions.definitions)}:{len(proofs.profiles)}:{len(requirements.required_metric_ids)}")
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
    assert loaded.stdout.strip() == "206:54:206"


def test_vision_requirements_are_an_independent_resource() -> None:
    spec = importlib.util.find_spec("saxo_bank_mcp.analytics_vision_requirements")
    assert spec is not None
    module = importlib.import_module("saxo_bank_mcp.analytics_vision_requirements")
    requirements = module.load_vision_coverage_requirements()

    assert len(requirements.required_metric_ids) == _EXACT_METRIC_COUNT
    assert len(requirements.required_analysis_kinds) == _EXACT_ANALYSIS_KIND_COUNT
    assert requirements.required_artifact_owners
    assert requirements.required_source_contracts


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
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=incomplete,
            catalog=catalog,
        )

    assert raised.value.matrix.missing_metric_ids == (missing,)


def test_coverage_fails_when_metric_is_deleted_consistently_from_catalogs() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    missing = "price_return"
    pruned_definitions = definitions.model_copy(
        update={
            "production_metric_ids": tuple(
                item for item in definitions.production_metric_ids if item != missing
            ),
            "definitions": tuple(
                definition
                for definition in definitions.definitions
                if definition.metric_id != missing
            ),
        },
    )
    pruned_catalog = catalog.model_copy(
        update={
            "production_metric_ids": tuple(
                item for item in catalog.production_metric_ids if item != missing
            ),
            "profiles": tuple(
                profile.model_copy(
                    update={
                        "metric_definitions": tuple(
                            binding
                            for binding in profile.metric_definitions
                            if binding.metric_id != missing
                        ),
                    },
                )
                for profile in catalog.profiles
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=pruned_definitions,
            catalog=pruned_catalog,
        )

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
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=incomplete,
        )

    assert raised.value.matrix.missing_analysis_kinds == (missing,)


def test_coverage_fails_when_kind_is_deleted_consistently_from_catalogs() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    missing = "goal_model"
    pruned_definitions = definitions.model_copy(
        update={
            "definitions": tuple(
                definition.model_copy(
                    update={
                        "analysis_kinds": tuple(
                            kind for kind in definition.analysis_kinds if kind != missing
                        ),
                    },
                )
                for definition in definitions.definitions
            ),
        },
    )
    pruned_catalog = catalog.model_copy(
        update={
            "production_analysis_kinds": tuple(
                kind for kind in catalog.production_analysis_kinds if kind != missing
            ),
            "profiles": tuple(
                profile for profile in catalog.profiles if profile.analysis_kind != missing
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=pruned_definitions,
            catalog=pruned_catalog,
        )

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
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=artifact_incomplete,
        )

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
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=source_incomplete,
        )

    assert source_error.value.matrix.missing_source_fields == (
        f"{source.contract_id}.{missing_path}",
    )


def test_artifact_template_cannot_be_reassigned_to_another_analysis_kind() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    owner = next(profile for profile in catalog.profiles if profile.artifact_template_ids)
    artifact_id = owner.artifact_template_ids[0]
    recipient = next(
        profile for profile in catalog.profiles if profile.analysis_kind != owner.analysis_kind
    )
    reassigned = catalog.model_copy(
        update={
            "profiles": tuple(
                profile.model_copy(
                    update={
                        "artifact_template_ids": (
                            tuple(
                                item
                                for item in profile.artifact_template_ids
                                if item != artifact_id
                            )
                            if profile.analysis_kind == owner.analysis_kind
                            else (*profile.artifact_template_ids, artifact_id)
                            if profile.analysis_kind == recipient.analysis_kind
                            else profile.artifact_template_ids
                        ),
                    },
                )
                for profile in catalog.profiles
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=reassigned,
        )

    assert raised.value.matrix.misassigned_artifact_template_ids == (artifact_id,)


def test_coverage_fails_when_artifact_is_deleted_consistently_from_catalogs() -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    missing = "price_volume_indicator"
    pruned = catalog.model_copy(
        update={
            "production_artifact_template_ids": tuple(
                item for item in catalog.production_artifact_template_ids if item != missing
            ),
            "artifact_owners": tuple(
                binding for binding in catalog.artifact_owners if binding.template_id != missing
            ),
            "profiles": tuple(
                profile.model_copy(
                    update={
                        "artifact_template_ids": tuple(
                            item for item in profile.artifact_template_ids if item != missing
                        ),
                    },
                )
                for profile in catalog.profiles
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=pruned,
        )

    assert raised.value.matrix.missing_artifact_template_ids == (missing,)


def test_coverage_fails_when_source_contract_is_deleted_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions = load_metric_definition_catalog()
    catalog = load_proof_profile_catalog(definitions=definitions)
    missing = "reference_instruments_v1"
    contracts = dict(source_contracts_by_id())
    missing_field = "Identifier"
    monkeypatch.setattr(
        proof_profiles_module,
        "source_contracts_by_id",
        lambda: {key: value for key, value in contracts.items() if key != missing},
    )
    pruned = catalog.model_copy(
        update={
            "source_field_coverage": tuple(
                binding
                for binding in catalog.source_field_coverage
                if binding.contract_id != missing
            ),
            "profiles": tuple(
                profile.model_copy(
                    update={
                        "source_contracts": tuple(
                            binding
                            for binding in profile.source_contracts
                            if binding.contract_id != missing
                        ),
                    },
                )
                for profile in catalog.profiles
            ),
        },
    )

    with pytest.raises(CoverageError) as raised:
        proof_profiles_module.generate_declared_coverage_matrix(
            definitions=definitions,
            catalog=pruned,
        )

    assert f"{missing}.{missing_field}" in raised.value.matrix.missing_source_fields


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
        at=profile.valid_until,
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


def test_missing_proof_refuses_and_runtime_quarantine_fails_closed(
    tmp_path: Path,
) -> None:
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})
    registry, profile, source_contracts = _active_registry(config=config)
    missing = registry.status("missing_unit_test_analysis", "1", {})

    quarantine_analysis_kind(
        profile.analysis_kind,
        "schema drift",
        source_revision="revision-a",
        config=config,
    )
    new_registry = ProofRegistry(
        definitions=registry.definitions,
        catalog=registry.catalog,
        config=config,
    )
    quarantined = new_registry.status(
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
    quarantine_path = proof_profiles_module.quarantine_store_path(config)
    assert stat.S_IMODE(quarantine_path.stat().st_mode) == _OWNER_FILE_MODE
    assert profile.analysis_kind in quarantine_path.read_text(encoding="utf-8")
    assert "revision-a" in quarantine_path.read_text(encoding="utf-8")


def test_runtime_quarantine_does_not_echo_arbitrary_private_reason(
    tmp_path: Path,
) -> None:
    config = load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})
    registry, profile, source_contracts = _active_registry(config=config)

    quarantine_analysis_kind(
        profile.analysis_kind,
        "private account 123456",
        source_revision="revision-a",
        config=config,
    )
    new_registry = ProofRegistry(
        definitions=registry.definitions,
        catalog=registry.catalog,
        config=config,
    )
    quarantined = new_registry.status(
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
    assert "123456" not in proof_profiles_module.quarantine_store_path(config).read_text(
        encoding="utf-8",
    )


def test_registry_without_config_uses_persistent_owner_only_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "implicit-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    registry, profile, source_contracts = _active_registry()

    quarantine_analysis_kind(
        profile.analysis_kind,
        "schema drift",
        source_revision="revision-a",
        config=None,
    )
    reloaded = ProofRegistry(definitions=registry.definitions, catalog=registry.catalog)
    status = reloaded.status(
        profile.analysis_kind,
        "1",
        source_contracts,
        source_revision="revision-a",
        engine_versions={"saxo_analytics": ("1", "abcdef0")},
        at=_NOW,
    )
    quarantine_path = state_home / "saxo-bank-mcp" / "analytics" / "proof-quarantines.json"

    assert status.state is ProofState.QUARANTINED
    assert status.reason_code == "schema_drift"
    assert stat.S_IMODE(quarantine_path.stat().st_mode) == _OWNER_FILE_MODE
    assert stat.S_IMODE(quarantine_path.parent.stat().st_mode) == _OWNER_DIRECTORY_MODE
