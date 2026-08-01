from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import RFC_4122, UUID

import duckdb
import pytest

from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_migrations import LATEST_SCHEMA_VERSION, migrate_store
from saxo_bank_mcp.analytics_models import (
    ActiveProofReceipt,
    AnalysisProvenance,
    AnalysisResult,
    DataCoverage,
    DataQuality,
    HandleKind,
    MarketAnalysisRequest,
    MetricClass,
    MetricValue,
    ProofEngineBinding,
    ProofSourceBinding,
    QualityState,
    ValueUnitClass,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_proof_profiles import (
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

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
_SOURCE_SHA = "a" * 64
_PAGE_ID = f"sp_{'b' * 64}"
_DATASET_FINGERPRINT = "c" * 64
_ANALYSIS_ID_LENGTH = 35
_OPAQUE_UUID_VERSION = 4
_DISTINCT_ID_COUNT = 4
_OWNER_FILE_MODE = 0o600


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config({"XDG_STATE_HOME": str(tmp_path / "state")})


def _registry(*, engine_version: str = "1") -> tuple[ProofRegistry, ProofProfile]:
    definitions = load_metric_definition_catalog()
    definition = definitions.by_id()["price_return"]
    source = SourceContractProofBinding(
        contract_id="chart_v3",
        contract_sha256=_SOURCE_SHA,
        field_paths=("CloseBid", "Time"),
    )
    profile = ProofProfile(
        proof_profile_id="vp_replay_unit_test_v1",
        profile_version="1",
        activation_state=ProfileActivationState.ACTIVE,
        quarantine_reason=None,
        analysis_kind="replay_unit_test",
        schema_version="1",
        metric_definitions=(
            MetricDefinitionBinding(
                metric_id=definition.metric_id,
                definition_version=definition.definition_version,
            ),
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
        artifact_template_ids=(),
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        valid_until=_NOW + timedelta(days=1),
    )
    catalog = ProofProfileCatalog(
        schema_version="1",
        catalog_version="replay-unit-test-1",
        definition_catalog_sha256=definitions.fingerprint_sha256,
        source_catalog_sha256=source_contract_catalog_sha256(),
        production_metric_ids=(definition.metric_id,),
        production_analysis_kinds=(profile.analysis_kind,),
        production_artifact_template_ids=(),
        source_field_coverage=(source,),
        profiles=(profile,),
    )
    return ProofRegistry(definitions=definitions, catalog=catalog), profile


def _analysis_result() -> AnalysisResult:
    dataset_id = new_safe_handle(HandleKind.DATASET_ID)
    analysis_id = build_analysis_id(
        {
            "dataset_fingerprint_sha256": _DATASET_FINGERPRINT,
            "dataset_id": dataset_id,
            "source_revision": "revision-a",
        },
        {"saxo_analytics": {"version": "1", "code_commit": "abcdef0"}},
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
    proof_profile_id = "vp_replay_unit_test_v1"
    return AnalysisResult(
        visibility=VisibilityMode.PRIVATE_USER_RESULT,
        tool_name="saxo_analyze_market",
        analysis_id=analysis_id,
        analysis_kind="replay_unit_test",
        request=MarketAnalysisRequest(
            request_kind="market",
            analysis_kind="replay_unit_test",
            dataset_id=dataset_id,
        ),
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
                    analysis_kind="replay_unit_test",
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


def _seed_store(config: AnalyticsConfig, result: AnalysisResult) -> None:
    migrate_store(config.paths.store_path, LATEST_SCHEMA_VERSION)
    result_json = json.dumps(
        result.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_json = "{}"
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(
            """
            INSERT INTO source_contracts
            VALUES ('sc_fixture', 'saxo_openapi', 'chart_v3', ?, ?,)
            """,
            (_SOURCE_SHA, _NOW),
        )
        connection.execute(
            """
            INSERT INTO source_pages (
                page_id, source_kind, page_key, source_revision,
                source_native_revision, contract_id, account_scope,
                instrument_handle, instrument_scope_sha256, source_timestamp,
                ingested_at, row_count, byte_count, logical_key_sha256,
                fingerprint_sha256, payload_sha256, payload_json
            ) VALUES (?, 'chart', 'fixture', 'revision-a', 'native-a',
                'sc_fixture', NULL, NULL, NULL, ?, ?, 0, 2, ?, ?, ?, ?)
            """,
            (
                _PAGE_ID,
                _NOW - timedelta(minutes=1),
                _NOW,
                "d" * 64,
                "e" * 64,
                _sha256(payload_json),
                payload_json,
            ),
        )
        connection.execute(
            """
            INSERT INTO datasets VALUES (
                ?, 'aggregate', 'saxo_openapi', 'revision-a', ?, ?, ?,
                'complete', 2, 2, ?
            )
            """,
            (
                result.provenance.dataset_id,
                _NOW,
                _NOW - timedelta(days=2),
                _NOW - timedelta(minutes=1),
                _DATASET_FINGERPRINT,
            ),
        )
        connection.execute(
            "INSERT INTO dataset_source_pages VALUES (?, ?)",
            (result.provenance.dataset_id, _PAGE_ID),
        )
        connection.execute(
            """
            INSERT INTO analyses VALUES (
                ?, ?, 'aggregate', 'replay_unit_test', 'verified',
                'revision-a', ?, ?, ?, ?, ?
            )
            """,
            (
                result.analysis_id,
                result.provenance.dataset_id,
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
    inputs_a = {
        "dataset_fingerprint_sha256": "1" * 64,
        "source_revision": "revision-a",
        "private_balance": "123456.78 DKK",
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

    assert first == reordered
    assert len(first) == _ANALYSIS_ID_LENGTH
    assert first.startswith("an_")
    assert "123456" not in first
    opaque_uuid = UUID(hex=first.removeprefix("an_"))
    assert opaque_uuid.version == _OPAQUE_UUID_VERSION
    assert opaque_uuid.variant == RFC_4122
    assert len({first, different_seed, different_engine, different_revision}) == (
        _DISTINCT_ID_COUNT
    )


@pytest.mark.parametrize("seed", [True, -1, 2**64])
def test_analysis_id_refuses_invalid_random_seed(seed: int) -> None:
    with pytest.raises(ValueError, match="seed"):
        build_analysis_id(
            {"dataset_fingerprint_sha256": "1" * 64},
            {"saxo_analytics": {"version": "1", "code_commit": "abcdef0"}},
            seed,
        )


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
    registry, profile = _registry()
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

    changed_registry, _ = _registry(engine_version="2")
    with pytest.raises(AnalysisReplayRefused) as changed:
        replay_analysis(
            result.analysis_id,
            config=config,
            registry=changed_registry,
            at=_NOW,
        )
    assert changed.value.reason_code == "engine_binding_changed"

    quarantine_analysis_kind(profile.analysis_kind, "reconciliation mismatch")
    with pytest.raises(AnalysisReplayRefused) as quarantined:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)
    assert quarantined.value.reason_code == "reconciliation_mismatch"


def test_replay_refuses_non_owner_store_permissions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _analysis_result()
    registry, _ = _registry()
    _seed_store(config, result)
    config.paths.store_path.chmod(0o644)

    with pytest.raises(AnalysisReplayRefused) as raised:
        replay_analysis(result.analysis_id, config=config, registry=registry, at=_NOW)

    assert raised.value.reason_code == "owner_only_store_required"
