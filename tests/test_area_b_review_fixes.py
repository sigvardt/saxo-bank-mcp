from __future__ import annotations

import inspect
import json
import stat
import tomllib
import typing
from pathlib import Path

import duckdb

import saxo_bank_mcp.analytics_source_contracts as contracts_module
import saxo_bank_mcp.analytics_store as store_module
import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
import saxo_bank_mcp.read_tools as read_tools_module
from saxo_bank_mcp.analytics_config import load_analytics_config
from saxo_bank_mcp.analytics_source_contracts import (
    compare_source_schema,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore

_OWNER_FILE_MODE = 0o600
_SHA256_LENGTH = 64


def test_installed_runner_binds_catalog_and_harness_identity() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert "saxo-bank-analytics-source-matrix" not in scripts
    shared = project["tool"]["hatch"]["build"]["targets"]["wheel"]["shared-scripts"]
    assert shared == {
        "scripts/saxo-bank-analytics-source-matrix": ("saxo-bank-analytics-source-matrix"),
        "scripts/saxo-bank-analytics-source-matrix-generate": (
            "saxo-bank-analytics-source-matrix-generate"
        ),
    }

    identity_loader = getattr(matrix_module, "source_matrix_candidate_identity", None)
    assert identity_loader is not None
    manifest = json.loads(
        Path("data/analytics/source_matrix_candidate.json").read_text(
            encoding="utf-8",
        ),
    )
    assert len(manifest["source_contract_catalog_sha256"]) == _SHA256_LENGTH
    assert len(manifest["harness_build_sha256"]) == _SHA256_LENGTH
    assert len(manifest["candidate_identity_sha256"]) == _SHA256_LENGTH
    assert manifest["source_contract_catalog_sha256"] != manifest["harness_build_sha256"]


def test_candidate_guard_uses_full_identity_and_fixed_state_location(
    tmp_path: Path,
) -> None:
    identity = "a" * 64
    first = matrix_module.candidate_guard_path(tmp_path, identity)
    evidence = matrix_module.candidate_evidence_path(tmp_path, identity)

    assert (
        "out"
        not in inspect.signature(
            matrix_module.execute_analytics_source_matrix_once,
        ).parameters
    )
    assert first.parent == evidence.parent
    assert identity in str(first)
    assert first.is_relative_to(tmp_path.resolve())


def test_readiness_refusal_is_not_a_success_or_claimed_evidence() -> None:
    source = inspect.getsource(matrix_module.execute_analytics_source_matrix_once)

    assert 'receipt.status in {"passed", "reduced", "refused"}' not in source
    assert "current_commit" not in source
    assert "candidate_commit" not in source


def test_ledger_proof_uses_explicit_environment_and_does_not_count_oauth_as_live() -> None:
    receipt = matrix_module._ledger_receipt(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        {
            "status": "passed",
            "tool_name": "saxo_get_safe_request_ledger",
            "scope": "current_mcp_session",
            "safe_fields_only": True,
            "ledger_complete": True,
            "negative_proof_available": True,
            "events_evicted": 0,
            "request_count": 2,
            "non_get_request_count": 0,
            "unsafe_gateway_request_detected": False,
            "order_placement_endpoint_called": False,
            "events": [
                {
                    "timestamp": "2026-07-30T12:00:00+00:00",
                    "phase": "attempted",
                    "host_role": "gateway",
                    "environment": "SIM",
                    "method": "GET",
                    "path": "/openapi/port/v1/orders",
                    "query_names": [],
                    "query_present": False,
                    "status": None,
                },
                {
                    "timestamp": "2026-07-30T12:00:01+00:00",
                    "phase": "attempted",
                    "host_role": "oauth",
                    "environment": "LIVE",
                    "method": "GET",
                    "path": "/token",
                    "query_names": [],
                    "query_present": False,
                    "status": None,
                },
            ],
        },
    )

    assert receipt.sim_only is True
    assert receipt.live_events == 0

    unverified = matrix_module._ledger_receipt(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        {
            "status": "passed",
            "tool_name": "saxo_get_safe_request_ledger",
            "scope": "current_mcp_session",
            "safe_fields_only": True,
            "ledger_complete": True,
            "negative_proof_available": True,
            "events_evicted": 0,
            "request_count": 1,
            "non_get_request_count": 0,
            "unsafe_gateway_request_detected": False,
            "order_placement_endpoint_called": False,
            "events": [
                {
                    "timestamp": "2026-07-30T12:00:00+00:00",
                    "phase": "attempted",
                    "host_role": "gateway",
                    "method": "GET",
                    "path": "/openapi/port/v1/orders",
                    "query_names": [],
                    "query_present": False,
                    "status": None,
                },
            ],
        },
    )
    assert unverified.sim_only is False


def test_registered_read_exposes_safe_server_side_analytics_receipt_mode() -> None:
    parameters = inspect.signature(
        read_tools_module.saxo_call_registered_endpoint,
    ).parameters

    assert "analytics_contract_id" in parameters
    response_mode = typing.get_type_hints(
        read_tools_module.saxo_call_registered_endpoint,
    )["response_mode"]
    literal = getattr(response_mode, "__value__", response_mode)
    assert "analytics_contract_receipt" in typing.get_args(literal)


def test_present_optional_type_drift_is_breaking_and_quarantined() -> None:
    contract = source_contracts_by_id()["chart_v3"]
    comparison = compare_source_schema(
        contract,
        {
            "Data": [
                {
                    "Time": "2026-07-29T08:00:00Z",
                    "CloseBid": 101.0,
                    "OpenBid": {"wrong": "type"},
                },
            ],
            "DataVersion": 1,
        },
    )

    assert comparison.compatible is False
    assert comparison.optional_type_mismatches == ("OpenBid",)
    assert comparison.quarantined_analysis_kinds == contract.dependent_analysis_kinds


def test_provider_page_has_typed_capture_metadata_and_store_adapter() -> None:
    fields = contracts_module.SourcePage.model_fields

    assert {
        "capture_revision",
        "contract_sha256",
        "source_kind",
        "source_timestamp",
        "account_scope",
        "instrument_scope_sha256",
    } <= fields.keys()
    assert hasattr(store_module.AnalyticsStore, "ingest_source_capture")


def test_historical_v1_store_migrates_before_first_source_page(
    tmp_path: Path,
) -> None:
    config = load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )
    migration_v1 = Path("data/analytics/migrations/0001_initial.sql").read_text(
        encoding="utf-8",
    )
    config.paths.store_path.unlink()
    connection = duckdb.connect(str(config.paths.store_path))
    try:
        connection.execute(migration_v1)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info('source_pages')").fetchall()
        }
        assert "logical_key_sha256" not in columns
        assert "fingerprint_sha256" not in columns
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'initial', ?, current_timestamp)",
            ("0" * 64,),
        )
        connection.execute(
            "INSERT INTO analytics_schema VALUES (TRUE, 1)",
        )
    finally:
        connection.close()
    config.paths.store_path.chmod(_OWNER_FILE_MODE)

    store = AnalyticsStore.open(config)
    try:
        stored = store.put_source_page(
            source_kind="price_bars",
            page_key="v1-upgrade-page",
            source_revision="capture:v1-upgrade",
            contract_name="chart_v3",
            contract_sha256="a" * 64,
            payload={"rows": []},
            row_count=0,
            source_timestamp=matrix_module.datetime(
                2026,
                7,
                30,
                tzinfo=matrix_module.UTC,
            ),
            account_scope="aggregate",
            instrument_handle=None,
        )
    finally:
        store.close()

    assert stored.page_id.startswith("sp_")
    assert stat.S_IMODE(config.paths.store_path.stat().st_mode) == _OWNER_FILE_MODE
