# pyright: reportPrivateUsage=false
# ruff: noqa: B009, SLF001

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import RFC_4122, UUID

import duckdb
import pytest

import saxo_bank_mcp.analytics_export as export_module
import saxo_bank_mcp.analytics_render as render_module
from saxo_bank_mcp import analytics_provenance as provenance_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisCalendar,
    AnalysisParameterBinding,
    AnalysisProvenance,
    AnalysisResult,
    DataCoverage,
    DataQuality,
    FxConversionMethod,
    FxSource,
    HandleKind,
    MarketAnalysisRequest,
    MetricClass,
    MetricCurrencyBinding,
    MetricValue,
    ModelScalarUnit,
    NamedModelAssumption,
    NamedModelParameter,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    ValueUnitClass,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_proof_profiles import (
    ArtifactOwnerBinding,
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofRegistry,
    SourceContractProofBinding,
    quarantine_analysis_kind,
)
from saxo_bank_mcp.analytics_provenance import (
    AnalysisReplayRefused,
    build_analysis_id,
    replay_analysis,
)
from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.analytics_store import AnalyticsStore

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
_SOURCE_SHA = "29f480ba97c45faac440876272afd3c865afcb558c5a9ee9c089d26d1ed46d09"
_PAGE_ID = "sp_679644729f339eae7292daa7c34077dd69d855b8e858a9506dd997f7eb013a3c"
_DATASET_FINGERPRINT = "73c3374786b35a8265ae0e80611cf6a1e4b9997011621eede3301f5c661a2a2e"
_SOURCE_PAYLOAD = {
    "rows": [
        {"CloseBid": 100.0, "Time": "2026-08-01T11:58:00Z"},
        {"CloseBid": 101.0, "Time": "2026-08-01T11:59:00Z"},
    ],
}
_ANALYSIS_PARAMETERS_DOMAIN = b"saxo-bank-mcp:analysis-parameters:v1\x00"
_ANALYSIS_ID_LENGTH = 35
_OPAQUE_UUID_VERSION = 4
_DISTINCT_ID_COUNT = 6
_MATERIAL_IDENTITY_VARIANT_COUNT = 4
_OWNER_FILE_MODE = 0o600
_SHA256_LENGTH = 64


@pytest.fixture(autouse=True)
def _isolate_default_quarantine_state(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "default-state"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})


def _registry(  # noqa: PLR0913
    *,
    engine_version: str = "1",
    required_metric_ids: tuple[str, ...] = ("price_return",),
    definition_updates: dict[str, object] | None = None,
    config: AnalyticsConfig | None = None,
    analysis_kind: str = "replay_unit_test",
    artifact_template_ids: tuple[str, ...] = (),
) -> tuple[ProofRegistry, ProofProfile]:
    definitions = load_metric_definition_catalog()
    if definition_updates is not None:
        changed = definitions.by_id()["price_return"].model_copy(
            update=definition_updates,
        )
        definitions = definitions.from_definitions(
            catalog_version="replay-unit-test-definition",
            production_metric_ids=definitions.production_metric_ids,
            definitions=tuple(
                changed if definition.metric_id == changed.metric_id else definition
                for definition in definitions.definitions
            ),
        )
    definitions_by_id = definitions.by_id()
    source = SourceContractProofBinding(
        contract_id="chart_v3",
        contract_sha256=_SOURCE_SHA,
        field_paths=("CloseBid", "Time"),
    )
    profile = ProofProfile(
        proof_profile_id=f"vp_{analysis_kind}_v1",
        profile_version="1",
        activation_state=ProfileActivationState.ACTIVE,
        quarantine_reason=None,
        analysis_kind=analysis_kind,
        schema_version="1",
        metric_definitions=tuple(
            MetricDefinitionBinding(
                metric_id=metric_id,
                definition_version=definitions_by_id[metric_id].definition_version,
            )
            for metric_id in required_metric_ids
        ),
        source_contracts=(source,),
        source_revision="revision-a",
        engines=(
            EngineProofBinding(
                engine_name="saxo_analytics",
                engine_version=engine_version,
                code_commit="abcdef0",
            ),
        ),
        artifact_template_ids=artifact_template_ids,
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        valid_until=_NOW + timedelta(days=1),
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version="replay-unit-test-1",
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        production_metric_ids=required_metric_ids,
        production_analysis_kinds=(profile.analysis_kind,),
        production_artifact_template_ids=artifact_template_ids,
        artifact_owners=tuple(
            ArtifactOwnerBinding(
                template_id=template_id,
                analysis_kind=analysis_kind,
            )
            for template_id in artifact_template_ids
        ),
        source_field_coverage=(source,),
        profiles=(profile,),
    )
    registry = (
        ProofRegistry(definitions=definitions, catalog=catalog)
        if config is None
        else ProofRegistry(definitions=definitions, catalog=catalog, config=config)
    )
    return registry, profile


def _analysis_result(*, analysis_kind: str = "replay_unit_test") -> AnalysisResult:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    request = MarketAnalysisRequest(
        request_kind="market",
        analysis_kind=analysis_kind,
        dataset_id=dataset_id,
        parameters=AnalysisParameterBinding(
            start_at=_NOW - timedelta(days=2),
            end_at=_NOW,
            as_of=_NOW,
            benchmark_handle=None,
            benchmark_fingerprint_sha256=None,
            fx_method=FxConversionMethod.NOT_APPLICABLE,
            fx_source=FxSource.NOT_APPLICABLE,
            fx_timestamp=None,
            calendar=AnalysisCalendar.CALENDAR_DAYS,
            reporting_currency="DKK",
            metric_currency_bindings=(),
            model_parameters=(),
        ),
    )
    identity = provenance_module.build_analysis_identity(
        _identity_inputs(dataset_id, analysis_kind=analysis_kind),
        _engine_versions(),
        None,
    )
    source_binding = ProofSourceBinding(
        source_scope="saxo_openapi",
        source_revision="revision-a",
        source_contract_sha256=_SOURCE_SHA,
    )
    engine_binding = ProofEngineBinding(
        engine_name="saxo_analytics",
        engine_version="1",
        code_commit="abcdef0",
    )
    proof_profile_id = f"vp_{analysis_kind}_v1"
    return AnalysisResult(
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        tool_name="saxo_analyze_market",
        analysis_id=identity.analysis_id,
        analysis_kind=analysis_kind,
        request=request,
        account_scope="aggregate",
        as_of=_NOW,
        valid_until=_NOW + timedelta(hours=1),
        metrics=(
            MetricValue(
                metric_id="price_return",
                value=0.125,
                unit="ratio",
                unit_class=ValueUnitClass.RATIO,
                currency=None,
                metric_class=MetricClass.CALCULATED_VERIFIED,
                source_timestamp=_NOW - timedelta(minutes=1),
                proof_profile_id=proof_profile_id,
            ),
        ),
        warnings=(),
        data_quality=DataQuality(
            state=QualityState.COMPLETE,
            coverage=DataCoverage(
                state=QualityState.COMPLETE,
                start_at=_NOW - timedelta(days=2),
                end_at=_NOW - timedelta(minutes=1),
                row_count=2,
                expected_row_count=2,
                missing_row_count=0,
            ),
            checked_at=_NOW,
            warnings=(),
        ),
        provenance=AnalysisProvenance(
            dataset_id=dataset_id,
            source_scope="saxo_openapi",
            source_revision="revision-a",
            source_contract_sha256=_SOURCE_SHA,
            source_timestamp=_NOW - timedelta(minutes=1),
            proof_receipts=(
                ActiveProofReceipt(
                    state="active",
                    analysis_kind=analysis_kind,
                    schema_version="1",
                    source_binding=source_binding,
                    engine_binding=engine_binding,
                    proof_profile_id=proof_profile_id,
                    checks_passed=(
                        "golden",
                        "property",
                        "independent_reference",
                        "saxo_reconciliation",
                        "sim_end_to_end",
                    ),
                ),
            ),
            engine_name="saxo_analytics",
            engine_version="1",
            code_commit="abcdef0",
            analysis_input_sha256=identity.input_sha256,
            analysis_parameters_sha256=_analysis_parameters_sha256(
                dataset_id,
                analysis_kind=analysis_kind,
            ),
            analysis_engine_sha256=identity.engine_sha256,
            analysis_seed_sha256=identity.seed_sha256,
            random_seed=None,
        ),
        assumptions=(),
        is_not_advice=True,
        is_not_forecast=True,
        model_distribution_only=False,
        verifies=("fixture arithmetic",),
        does_not_verify=("future returns",),
        replayable=True,
        next_actions=(),
    )


def _identity_inputs(
    dataset_id: str,
    *,
    analysis_kind: str = "replay_unit_test",
) -> dict[str, object]:
    return {
        "dataset_fingerprint_sha256": _DATASET_FINGERPRINT,
        "dataset_id": dataset_id,
        "source_revision": "revision-a",
        "tool_name": "saxo_analyze_market",
        "analysis_parameters_sha256": _analysis_parameters_sha256(
            dataset_id,
            analysis_kind=analysis_kind,
        ),
    }


def _analysis_parameters_sha256(
    dataset_id: str,
    *,
    account_scope: str = "aggregate",
    analysis_kind: str = "replay_unit_test",
) -> str:
    material: dict[str, object] = {
        "account_scope": account_scope,
        "analysis_kind": analysis_kind,
        "assumptions": [],
        "as_of": _NOW.isoformat(),
        "request": {
            "analysis_kind": analysis_kind,
            "dataset_id": dataset_id,
            "parameters": {
                "as_of": "2026-08-01T12:00:00Z",
                "benchmark_fingerprint_sha256": None,
                "benchmark_handle": None,
                "calendar": "calendar_days",
                "end_at": "2026-08-01T12:00:00Z",
                "fx_method": "not_applicable",
                "fx_source": "not_applicable",
                "fx_timestamp": None,
                "metric_currency_bindings": [],
                "model_parameters": [],
                "reporting_currency": "DKK",
                "start_at": "2026-07-30T12:00:00Z",
            },
            "request_kind": "market",
        },
        "schema_version": "1",
    }
    encoded = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(_ANALYSIS_PARAMETERS_DOMAIN + encoded).hexdigest()


def _engine_versions() -> dict[str, object]:
    return {"saxo_analytics": {"version": "1", "code_commit": "abcdef0"}}


def _seed_store(config: AnalyticsConfig, result: AnalysisResult) -> None:
    store = AnalyticsStore.open(config)
    try:
        page = store.put_source_page(
            source_kind="chart",
            page_key="fixture",
            source_revision="revision-a",
            source_native_revision="native-a",
            contract_name="chart_v3",
            contract_sha256=_SOURCE_SHA,
            payload=_SOURCE_PAYLOAD,
            row_count=2,
            source_timestamp=_NOW - timedelta(minutes=1),
            account_scope=None,
            instrument_handle=None,
        )
        dataset = store.create_dataset(
            dataset_id=result.provenance.dataset_id,
            account_scope="aggregate",
            source_scope="saxo_openapi",
            source_revision="revision-a",
            source_page_ids=(page.page_id,),
            created_at=_NOW,
            coverage_start=_NOW - timedelta(days=2),
            coverage_end=_NOW - timedelta(minutes=1),
            quality_state=QualityState.COMPLETE,
        )
        assert page.page_id == _PAGE_ID
        assert dataset.fingerprint_sha256 == _DATASET_FINGERPRINT
    finally:
        store.close()
    result_json = json.dumps(
        result.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            """
            INSERT INTO analyses VALUES (
                ?, ?, 'aggregate', ?, 'verified',
                'revision-a', ?, ?, ?, ?, ?
            )
            """,
            (
                result.analysis_id,
                result.provenance.dataset_id,
                result.analysis_kind,
                result.as_of,
                _NOW,
                len(result_json.encode()),
                _sha256(result_json),
                result_json,
            ),
        )
    finally:
        connection.close()
    config.paths.store_path.chmod(0o600)


def test_analysis_id_is_deterministic_opaque_and_seed_bound() -> None:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    inputs_a = {
        "dataset_fingerprint_sha256": "1" * 64,
        "dataset_id": dataset_id,
        "source_revision": "revision-a",
        "tool_name": "saxo_analyze_market",
        "analysis_parameters_sha256": "2" * 64,
    }
    inputs_b = dict(reversed(tuple(inputs_a.items())))
    engines = {"saxo_analytics": {"version": "1", "code_commit": "abcdef0"}}

    first = build_analysis_id(inputs_a, engines, 7)
    reordered = build_analysis_id(inputs_b, engines, 7)
    different_seed = build_analysis_id(inputs_a, engines, 8)
    different_engine = build_analysis_id(
        inputs_a,
        {"saxo_analytics": {"version": "2", "code_commit": "abcdef0"}},
        7,
    )
    different_revision = build_analysis_id(
        {**inputs_a, "source_revision": "revision-b"},
        engines,
        7,
    )
    different_tool = build_analysis_id(
        {**inputs_a, "tool_name": "saxo_analyze_portfolio"},
        engines,
        7,
    )
    different_parameters = build_analysis_id(
        {**inputs_a, "analysis_parameters_sha256": "3" * 64},
        engines,
        7,
    )

    assert first == reordered
    assert len(first) == _ANALYSIS_ID_LENGTH
    assert first.startswith("an_")
    opaque_uuid = UUID(hex=first.removeprefix("an_"))
    assert opaque_uuid.version == _OPAQUE_UUID_VERSION
    assert opaque_uuid.variant == RFC_4122
    assert (
        len(
            {
                first,
                different_seed,
                different_engine,
                different_revision,
                different_tool,
                different_parameters,
            },
        )
        == _DISTINCT_ID_COUNT
    )


def test_analysis_identity_binds_risk_free_rate_and_result_assumptions() -> None:
    result = _analysis_result()
    risk_parameter = NamedModelParameter(
        name="risk_free_rate",
        value=0.03,
        unit=ModelScalarUnit.RATIO,
    )
    risk_changed = risk_parameter.model_copy(update={"value": 0.04})
    with_risk = result.model_copy(
        update={
            "request": result.request.model_copy(
                update={
                    "parameters": result.request.parameters.model_copy(
                        update={"model_parameters": (risk_parameter,)},
                    ),
                },
            ),
        },
    )
    changed_risk = with_risk.model_copy(
        update={
            "request": with_risk.request.model_copy(
                update={
                    "parameters": with_risk.request.parameters.model_copy(
                        update={"model_parameters": (risk_changed,)},
                    ),
                },
            ),
        },
    )
    with_assumption = result.model_copy(
        update={
            "assumptions": (
                NamedModelAssumption(
                    name="inflation_rate",
                    value=0.02,
                    unit=ModelScalarUnit.RATIO,
                ),
            ),
        },
    )

    fingerprints = tuple(
        provenance_module.analysis_parameters_sha256(candidate)
        for candidate in (result, with_risk, changed_risk, with_assumption)
    )
    identities = tuple(
        build_analysis_id(
            _identity_inputs(result.provenance.dataset_id)
            | {"analysis_parameters_sha256": fingerprint},
            _engine_versions(),
            None,
        )
        for fingerprint in fingerprints
    )

    assert len(set(fingerprints)) == _MATERIAL_IDENTITY_VARIANT_COUNT
    assert len(set(identities)) == _MATERIAL_IDENTITY_VARIANT_COUNT


@pytest.mark.parametrize("seed", [True, -1, 2**64])
def test_analysis_id_refuses_invalid_random_seed(seed: int) -> None:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    with pytest.raises(ValueError, match="seed"):
        build_analysis_id(
            {
                "dataset_fingerprint_sha256": "1" * 64,
                "dataset_id": dataset_id,
                "source_revision": "revision-a",
                "tool_name": "saxo_analyze_market",
                "analysis_parameters_sha256": "2" * 64,
            },
            _engine_versions(),
            seed,
        )


def test_analysis_id_refuses_empty_or_private_identity_material() -> None:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    with pytest.raises(ValueError, match="identity inputs"):
        build_analysis_id({}, {}, None)
    with pytest.raises(ValueError, match="engine"):
        build_analysis_id(_identity_inputs(dataset_id), {}, None)
    with pytest.raises(ValueError, match="identity input"):
        build_analysis_id(
            _identity_inputs(dataset_id) | {"private_balance": "123456.78 DKK"},
            _engine_versions(),
            None,
        )


def test_analysis_result_persists_only_value_free_identity_fingerprints() -> None:
    result = _analysis_result()

    assert len(result.provenance.analysis_input_sha256) == _SHA256_LENGTH
    assert len(result.provenance.analysis_parameters_sha256) == _SHA256_LENGTH
    assert len(result.provenance.analysis_engine_sha256) == _SHA256_LENGTH
    assert len(result.provenance.analysis_seed_sha256) == _SHA256_LENGTH
    serialized = result.model_dump_json()
    assert _DATASET_FINGERPRINT not in serialized
    assert "123456" not in serialized


def test_replay_reads_owner_store_and_returns_byte_equal_result(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    _seed_store(config, result)

    first = replay_analysis(
        result.analysis_id,
        config=config,
        registry=registry,
        at=_NOW,
    )
    second = replay_analysis(
        result.analysis_id,
        config=config,
        registry=registry,
        at=_NOW,
    )

    assert first == result
    assert second == result
    assert first.model_dump_json() == second.model_dump_json()
    assert stat.S_IMODE(config.paths.store_path.stat().st_mode) == _OWNER_FILE_MODE


def test_store_verified_binding_renders_exports_and_reports_without_caller_stamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_registry_type = getattr(render_module, "ArtifactBindingRegistry")
    stored_render_request_type = getattr(render_module, "StoredRenderRequest")
    stored_table_request_type = getattr(export_module, "StoredTableExportRequest")
    stored_report_request_type = getattr(export_module, "StoredReportExportRequest")
    config = _config(tmp_path)
    result = _analysis_result(analysis_kind="market_comparison")
    registry, _ = _registry(
        analysis_kind="market_comparison",
        artifact_template_ids=("relative_performance",),
    )
    _seed_store(config, result)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setattr(render_module, "_utc_now", lambda: _NOW)
    bindings = binding_registry_type(
        config=config,
        proof_registry=registry,
    )
    issued = bindings.issue(result.analysis_id)
    store = AnalyticsStore.open(config)
    try:
        chart = render_module.render_analysis(
            stored_render_request_type(
                binding_id=issued.binding_id,
                template_id="relative_performance",
                output_format="png",
                width=1200,
                height=675,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
        table = export_module.export_analysis(
            stored_table_request_type(
                binding_id=issued.binding_id,
                output_format="json",
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
        report = export_module.export_analysis(
            stored_report_request_type(
                binding_id=issued.binding_id,
                template_id="relative_performance",
                output_format="pdf",
                viewport_width=1280,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
    finally:
        store.close()

    assert isinstance(chart, render_module.InlineArtifact)
    assert chart.content.startswith(b"\x89PNG")
    assert isinstance(table, render_module.InlineArtifact)
    table_document = json.loads(table.content)
    assert table_document["rows"] == [
        {
            "metric_currency": None,
            "metric_id": "price_return",
            "metric_unit": "ratio",
            "metric_value": 0.125,
        },
    ]
    assert isinstance(report, render_module.InlineArtifact)
    assert report.content.startswith(b"%PDF")
    assert all("unverified caller" not in line for line in chart.visible_stamps)


def test_verified_delivery_preserves_exact_25_mib_inline_boundary_and_link_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_registry_type = getattr(render_module, "ArtifactBindingRegistry")
    deliver_bound = getattr(render_module, "_deliver_bound_payload")
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    _seed_store(config, result)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setattr(render_module, "_utc_now", lambda: _NOW)
    bindings = binding_registry_type(
        config=config,
        proof_registry=registry,
    )
    issued = bindings.issue(result.analysis_id)
    inline_stamps = bindings._stamps_for(
        issued.binding_id,
        visibility=VisibilityMode.INLINE_PRIVATE,
    )
    link_stamps = bindings._stamps_for(
        issued.binding_id,
        visibility=VisibilityMode.LOCAL_RESOURCE_LINK,
    )
    limit = config.limits.artifact_bytes
    at_limit = render_module.build_artifact_payload(
        media_type="application/octet-stream",
        extension="bin",
        content=b"x" * limit,
        semantics_sha256="1" * 64,
        stamps=inline_stamps,
    )
    over_limit = render_module.build_artifact_payload(
        media_type="application/octet-stream",
        extension="bin",
        content=b"y" * (limit + 1),
        semantics_sha256="2" * 64,
        stamps=link_stamps,
    )
    store = AnalyticsStore.open(config)
    try:
        inline = deliver_bound(
            at_limit,
            binding_id=issued.binding_id,
            config=config,
            store=store,
            bindings=bindings,
        )
        linked = deliver_bound(
            over_limit,
            binding_id=issued.binding_id,
            config=config,
            store=store,
            bindings=bindings,
        )
    finally:
        store.close()

    assert isinstance(inline, render_module.InlineArtifact)
    assert inline.byte_count == limit
    assert isinstance(linked, render_module.ArtifactResourceLink)
    assert linked.byte_count == limit + 1
    assert linked.reason_code == "artifact_return_limit"


def test_server_issued_artifact_binding_refuses_after_source_revision_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_registry_type = getattr(render_module, "ArtifactBindingRegistry")
    stored_render_request_type = getattr(render_module, "StoredRenderRequest")
    config = _config(tmp_path)
    result = _analysis_result(analysis_kind="market_comparison")
    registry, _ = _registry(
        analysis_kind="market_comparison",
        artifact_template_ids=("relative_performance",),
    )
    _seed_store(config, result)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setattr(render_module, "_utc_now", lambda: _NOW)
    bindings = binding_registry_type(
        config=config,
        proof_registry=registry,
    )
    issued = bindings.issue(result.analysis_id)
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            "UPDATE datasets SET source_revision = 'revision-b' WHERE dataset_id = ?",
            (result.provenance.dataset_id,),
        )
    finally:
        connection.close()

    store = AnalyticsStore.open(config)
    try:
        delivery = render_module.render_analysis(
            stored_render_request_type(
                binding_id=issued.binding_id,
                template_id="relative_performance",
                output_format="png",
                width=1200,
                height=675,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
    finally:
        store.close()

    assert isinstance(delivery, render_module.ArtifactRefusal)
    assert delivery.reason_code == "artifact_analysis_binding_invalid"


def test_artifact_runtime_environment_and_host_trust_are_not_caller_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_registry_type = getattr(render_module, "ArtifactBindingRegistry")
    stored_render_request_type = getattr(render_module, "StoredRenderRequest")
    config = _config(tmp_path)
    result = _analysis_result(analysis_kind="market_comparison")
    registry, _ = _registry(
        analysis_kind="market_comparison",
        artifact_template_ids=("relative_performance",),
    )
    _seed_store(config, result)
    monkeypatch.setattr(render_module, "_utc_now", lambda: _NOW)

    with pytest.raises(TypeError):
        binding_registry_type(
            config=config,
            proof_registry=registry,
            environment="SIM",
        )
    with pytest.raises(TypeError):
        binding_registry_type(
            config=config,
            proof_registry=registry,
            trusted_local_host=True,
        )

    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    bindings = binding_registry_type(config=config, proof_registry=registry)
    issued = bindings.issue(result.analysis_id)
    store = AnalyticsStore.open(config)
    try:
        delivery = render_module.render_analysis(
            stored_render_request_type(
                binding_id=issued.binding_id,
                template_id="relative_performance",
                output_format="png",
                width=1200,
                height=675,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
    finally:
        store.close()

    assert isinstance(delivery, render_module.ArtifactResourceLink)
    assert delivery.owner_only is True
    assert delivery.reason_code == "inline_private_not_enabled"
    assert any(line == "Environment: LIVE" for line in delivery.visible_stamps)


def test_explicit_link_visible_artifact_keeps_link_requested_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_registry_type = getattr(render_module, "ArtifactBindingRegistry")
    stored_render_request_type = getattr(render_module, "StoredRenderRequest")
    config = _config(tmp_path)
    result = _analysis_result(analysis_kind="market_comparison").model_copy(
        update={"visibility": VisibilityMode.LOCAL_RESOURCE_LINK},
    )
    registry, _ = _registry(
        analysis_kind="market_comparison",
        artifact_template_ids=("relative_performance",),
    )
    _seed_store(config, result)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setattr(render_module, "_utc_now", lambda: _NOW)
    bindings = binding_registry_type(config=config, proof_registry=registry)
    issued = bindings.issue(result.analysis_id)
    store = AnalyticsStore.open(config)
    try:
        delivery = render_module.render_analysis(
            stored_render_request_type(
                binding_id=issued.binding_id,
                template_id="relative_performance",
                output_format="png",
                width=1200,
                height=675,
            ),
            config=config,
            store=store,
            bindings=bindings,
        )
    finally:
        store.close()

    assert isinstance(delivery, render_module.ArtifactResourceLink)
    assert delivery.reason_code == "local_resource_link_requested"


@pytest.mark.parametrize(
    ("delivery_mode", "mutation"),
    [
        ("inline", "status"),
        ("inline", "fingerprint"),
        ("link", "dataset_revision"),
        ("link", "analysis_revision"),
    ],
)
def test_artifact_registration_atomically_rechecks_the_issued_analysis_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery_mode: str,
    mutation: str,
) -> None:
    binding_registry_type = getattr(render_module, "ArtifactBindingRegistry")
    config = _config(tmp_path)
    result = _analysis_result()
    use_link = delivery_mode == "link"
    if use_link:
        result = result.model_copy(update={"visibility": VisibilityMode.LOCAL_RESOURCE_LINK})
    registry, _ = _registry()
    _seed_store(config, result)
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setattr(render_module, "_utc_now", lambda: _NOW)
    bindings = binding_registry_type(config=config, proof_registry=registry)
    issued = bindings.issue(result.analysis_id)
    expected_visibility = (
        VisibilityMode.LOCAL_RESOURCE_LINK if use_link else VisibilityMode.INLINE_PRIVATE
    )
    payload = render_module.build_artifact_payload(
        media_type="text/plain",
        extension="txt",
        content=b"synthetic race-bound artifact",
        semantics_sha256="8" * 64,
        stamps=bindings._stamps_for(
            issued.binding_id,
            visibility=expected_visibility,
        ),
    )
    store = AnalyticsStore.open(config)
    original_writer = store._write_connection
    raced = False

    @contextmanager
    def invalidate_before_registration() -> Generator[duckdb.DuckDBPyConnection]:
        nonlocal raced
        if not raced:
            raced = True
            with original_writer() as connection:
                if mutation == "status":
                    connection.execute(
                        "UPDATE analyses SET status = 'invalidated' WHERE analysis_id = ?",
                        (result.analysis_id,),
                    )
                elif mutation == "fingerprint":
                    connection.execute(
                        "UPDATE analyses SET fingerprint_sha256 = ? WHERE analysis_id = ?",
                        ("0" * 64, result.analysis_id),
                    )
                elif mutation == "dataset_revision":
                    connection.execute(
                        "UPDATE datasets SET source_revision = 'revision-raced' "
                        "WHERE dataset_id = ?",
                        (result.provenance.dataset_id,),
                    )
                else:
                    connection.execute(
                        "UPDATE analyses SET source_revision = 'revision-raced' "
                        "WHERE analysis_id = ?",
                        (result.analysis_id,),
                    )
                store._bump_revision(connection)
        with original_writer() as connection:
            yield connection

    monkeypatch.setattr(store, "_write_connection", invalidate_before_registration)
    try:
        delivery = render_module._deliver_bound_payload(
            payload,
            binding_id=issued.binding_id,
            config=config,
            store=store,
            bindings=bindings,
        )
        with store._read_connection() as connection:
            artifact_count = connection.execute("SELECT count(*) FROM artifacts").fetchone()
    finally:
        store.close()

    assert raced is True
    assert isinstance(delivery, render_module.ArtifactRefusal)
    assert delivery.reason_code == "artifact_analysis_binding_invalid"
    assert artifact_count == (0,)


@pytest.mark.parametrize("mutation", ["payload", "lineage"])
def test_replay_refuses_changed_bound_source_material(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    _seed_store(config, result)
    replacement_page_id: str | None = None
    if mutation == "lineage":
        store = AnalyticsStore.open(config)
        try:
            replacement = store.put_source_page(
                source_kind="chart",
                page_key="replacement",
                source_revision="revision-a",
                source_native_revision="native-a",
                contract_name="chart_v3",
                contract_sha256=_SOURCE_SHA,
                payload={"rows": [_SOURCE_PAYLOAD["rows"][0]]},
                row_count=1,
                source_timestamp=_NOW - timedelta(minutes=1),
                account_scope=None,
                instrument_handle=None,
            )
            replacement_page_id = replacement.page_id
        finally:
            store.close()
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        if mutation == "payload":
            connection.execute(
                "UPDATE source_pages SET payload_json = '{\"rows\":[]}' WHERE page_id = ?",
                (_PAGE_ID,),
            )
        else:
            assert replacement_page_id is not None
            connection.execute(
                "UPDATE dataset_source_pages SET page_id = ? WHERE dataset_id = ?",
                (replacement_page_id, result.provenance.dataset_id),
            )
    finally:
        connection.close()

    with pytest.raises(AnalysisReplayRefused) as raised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)

    assert raised.value.reason_code == "dataset_integrity_changed"


def test_replay_refuses_invalidated_or_revised_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    _seed_store(config, result)

    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            "UPDATE analyses SET status = 'invalidated' WHERE analysis_id = ?",
            (result.analysis_id,),
        )
    finally:
        connection.close()
    with pytest.raises(AnalysisReplayRefused) as invalidated:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)
    assert invalidated.value.reason_code == "analysis_invalidated"

    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            "UPDATE analyses SET status = 'verified' WHERE analysis_id = ?",
            (result.analysis_id,),
        )
        connection.execute(
            "UPDATE datasets SET source_revision = 'revision-b' WHERE dataset_id = ?",
            (result.provenance.dataset_id,),
        )
    finally:
        connection.close()
    with pytest.raises(AnalysisReplayRefused) as revised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)
    assert revised.value.reason_code == "source_revision_changed"


def test_replay_refuses_missing_or_changed_proof_and_quarantine(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, profile = _registry(config=config)
    _seed_store(config, result)

    missing_catalog = registry.catalog.model_copy(update={"profiles": ()})
    missing_registry = ProofRegistry(
        definitions=registry.definitions,
        catalog=missing_catalog,
    )
    with pytest.raises(AnalysisReplayRefused) as missing:
        replay_analysis(
            result.analysis_id,
            config=config,
            registry=missing_registry,
            at=_NOW,
        )
    assert missing.value.reason_code == "missing_proof_profile"

    changed_registry, _ = _registry(engine_version="2", config=config)
    with pytest.raises(AnalysisReplayRefused) as changed:
        replay_analysis(
            result.analysis_id,
            config=config,
            registry=changed_registry,
            at=_NOW,
        )
    assert changed.value.reason_code == "engine_binding_changed"

    quarantine_analysis_kind(
        profile.analysis_kind,
        "reconciliation mismatch",
        source_revision="revision-a",
        config=config,
    )
    with pytest.raises(AnalysisReplayRefused) as quarantined:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)
    assert quarantined.value.reason_code == "reconciliation_mismatch"


def test_replay_refuses_unknown_unbound_or_missing_required_metrics(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    original = result.metrics[0]
    unknown_result = result.model_copy(
        update={
            "metrics": (original.model_copy(update={"metric_id": "unknown_metric"}),),
        },
    )
    _seed_store(config, unknown_result)
    with pytest.raises(AnalysisReplayRefused) as unknown:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)
    assert unknown.value.reason_code == "metric_definition_missing"

    second_config = _config(tmp_path / "unbound")
    extra_result = result.model_copy(
        update={
            "metrics": (
                original,
                original.model_copy(update={"metric_id": "daily_return"}),
            ),
        },
    )
    _seed_store(second_config, extra_result)
    with pytest.raises(AnalysisReplayRefused) as unbound:
        replay_analysis(
            result.analysis_id,
            config=second_config,
            registry=registry,
            at=_NOW,
        )
    assert unbound.value.reason_code == "metric_not_bound"

    third_config = _config(tmp_path / "missing")
    required_registry, _ = _registry(
        required_metric_ids=("price_return", "daily_return"),
    )
    _seed_store(third_config, result)
    with pytest.raises(AnalysisReplayRefused) as missing:
        replay_analysis(
            result.analysis_id,
            config=third_config,
            registry=required_registry,
            at=_NOW,
        )
    assert missing.value.reason_code == "required_metric_missing"


def test_replay_refuses_metric_unit_or_identity_fingerprint_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    unit_registry, _ = _registry(definition_updates={"output_unit": "decimal_ratio"})
    _seed_store(config, result)
    with pytest.raises(AnalysisReplayRefused) as unit:
        replay_analysis(result.analysis_id, config=config, registry=unit_registry, at=_NOW)
    assert unit.value.reason_code == "metric_unit_mismatch"

    identity_config = _config(tmp_path / "identity")
    registry, _ = _registry()
    _seed_store(identity_config, result)
    connection = duckdb.connect(str(identity_config.paths.store_path))
    try:
        connection.execute(
            "UPDATE datasets SET fingerprint_sha256 = ? WHERE dataset_id = ?",
            ("f" * 64, result.provenance.dataset_id),
        )
    finally:
        connection.close()
    with pytest.raises(AnalysisReplayRefused) as identity:
        replay_analysis(
            result.analysis_id,
            config=identity_config,
            registry=registry,
            at=_NOW,
        )
    assert identity.value.reason_code == "analysis_identity_changed"


def test_replay_refuses_tool_or_safe_analysis_parameter_change(tmp_path: Path) -> None:
    result = _analysis_result()
    registry, _ = _registry()
    tool_config = _config(tmp_path / "tool")
    _seed_store(
        tool_config,
        result.model_copy(update={"tool_name": "saxo_analyze_portfolio"}),
    )
    with pytest.raises(AnalysisReplayRefused) as tool_changed:
        replay_analysis(result.analysis_id, config=tool_config, registry=registry, at=_NOW)
    assert tool_changed.value.reason_code == "analysis_identity_changed"

    parameter_config = _config(tmp_path / "parameters")
    _seed_store(
        parameter_config,
        result.model_copy(update={"account_scope": "selected SIM account"}),
    )
    with pytest.raises(AnalysisReplayRefused) as parameters_changed:
        replay_analysis(
            result.analysis_id,
            config=parameter_config,
            registry=registry,
            at=_NOW,
        )
    assert parameters_changed.value.reason_code == "analysis_identity_changed"


@pytest.mark.parametrize(
    ("registry_kwargs", "reason_code"),
    [
        (
            {"default_metric_class": MetricClass.MODEL_OUTPUT},
            "metric_class_mismatch",
        ),
        (
            {"unit_class": ValueUnitClass.PERCENTAGE},
            "metric_unit_class_mismatch",
        ),
    ],
)
def test_replay_refuses_metric_definition_semantic_change(
    tmp_path: Path,
    registry_kwargs: dict[str, object],
    reason_code: str,
) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry(definition_updates=registry_kwargs)
    _seed_store(config, result)

    with pytest.raises(AnalysisReplayRefused) as raised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)

    assert raised.value.reason_code == reason_code


def test_replay_refuses_metric_definition_currency_semantics(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    metric = result.metrics[0].model_copy(
        update={
            "unit": "usd",
            "unit_class": ValueUnitClass.MONETARY,
            "currency": "DKK",
        },
    )
    changed_result = result.model_copy(update={"metrics": (metric,)})
    registry, _ = _registry(
        definition_updates={
            "output_unit": "usd",
            "unit_class": ValueUnitClass.MONETARY,
        },
    )
    _seed_store(config, changed_result)

    with pytest.raises(AnalysisReplayRefused) as raised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)

    assert raised.value.reason_code == "metric_currency_mismatch"


@pytest.mark.parametrize("output_unit", ["reporting_currency", "price_currency"])
def test_replay_binds_monetary_metric_to_exact_requested_currency(
    tmp_path: Path,
    output_unit: str,
) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    currency_bindings = (
        (MetricCurrencyBinding(metric_id="price_return", currency="USD"),)
        if output_unit == "price_currency"
        else ()
    )
    request = result.request.model_copy(
        update={
            "parameters": result.request.parameters.model_copy(
                update={
                    "reporting_currency": "USD",
                    "metric_currency_bindings": currency_bindings,
                },
            ),
        },
    )
    metric = result.metrics[0].model_copy(
        update={
            "unit": output_unit,
            "unit_class": ValueUnitClass.MONETARY,
            "currency": "DKK",
        },
    )
    changed_result = result.model_copy(
        update={"request": request, "metrics": (metric,)},
    )
    registry, _ = _registry(
        definition_updates={
            "output_unit": output_unit,
            "unit_class": ValueUnitClass.MONETARY,
        },
    )
    _seed_store(config, changed_result)

    with pytest.raises(AnalysisReplayRefused) as raised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)

    assert raised.value.reason_code == "metric_currency_mismatch"


def test_replay_refuses_non_owner_store_permissions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    _seed_store(config, result)
    config.paths.store_path.chmod(0o644)

    with pytest.raises(AnalysisReplayRefused) as raised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)

    assert raised.value.reason_code == "owner_only_store_required"
