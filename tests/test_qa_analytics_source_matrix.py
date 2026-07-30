from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

import pytest
from fastmcp import FastMCP
from pydantic import TypeAdapter

import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import source_contracts_by_id
from saxo_bank_mcp.endpoint_registry import find_registered_operation
from saxo_bank_mcp.qa_analytics_source_matrix import (
    ANALYTICS_SOURCE_CANDIDATE,
    SourceMatrixFixtures,
    candidate_guard_path,
    execute_analytics_source_matrix_once,
    prove_sim_environment,
    run_analytics_source_matrix,
)
from saxo_bank_mcp.secret_scan import scan_secret_text

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
EXPECTED_CONTRACT_IDS = {
    "balances_v1",
    "bookings_v1",
    "chart_v3",
    "closed_positions_history_v1",
    "corporate_action_events_v2",
    "corporate_action_holdings_v2",
    "costs_v1",
    "exposure_instruments_v1",
    "info_price_v1",
    "info_prices_list_v1",
    "options_chain_reference_v1",
    "orders_v1",
    "performance_summary_v4",
    "performance_timeseries_v4",
    "positions_v1",
    "reference_instrument_details_v1",
    "reference_instruments_v1",
    "transactions_v1",
}
EXPECTED_SOURCE_COUNT = 18
EXPECTED_PAGE_COUNT = 2
SHA256_LENGTH = 64
OWNER_DIRECTORY_MODE = 0o700
OWNER_FILE_MODE = 0o600
PRIVATE_ACCOUNT_MARKER = "private-account-marker"
PRIVATE_CLIENT_MARKER = "private-client-marker"
PRIVATE_DISPLAY_MARKER = "private-display-marker"
PRIVATE_MONEY_MARKER = 987654321.125
CAPTURED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class FakeMatrixState:
    calls: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    changed_state: bool = False
    unsafe_ledger: bool = False
    pagination_cycle: bool = False
    auth_available: bool = True
    state_reads: dict[str, int] = field(default_factory=dict)


def _fixtures() -> SourceMatrixFixtures:
    return SourceMatrixFixtures(
        account_key=PRIVATE_ACCOUNT_MARKER,
        client_key=PRIVATE_CLIENT_MARKER,
        instrument_uic=211,
        asset_type="Stock",
        option_root_id=120,
    )


def _safe_sha(character: str) -> str:
    return character * 64


def _registry_rows() -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    paths = [contract.path_template for contract in source_contracts_by_id().values()] + [
        "/port/v1/orders/me",
        "/port/v1/positions/me",
        "/port/v1/balances/me",
    ]
    for path in paths:
        operation = find_registered_operation("GET", path)
        assert operation is not None
        if operation.operation_id in seen:
            continue
        seen.add(operation.operation_id)
        rows.append(
            {
                "operation_id": operation.operation_id,
                "service_group": operation.service_group,
                "method": "GET",
                "path_template": operation.path_template,
                "read_write_class": "read",
                "mcp_support_policy": "read_only_definition_registered",
                "refusal_reason": "",
            },
        )
    return rows


def _record(
    state: FakeMatrixState,
    tool: str,
    arguments: dict[str, JsonValue],
) -> None:
    state.calls.append((tool, arguments))


def _matrix_server(state: FakeMatrixState) -> FastMCP:  # noqa: C901
    server = FastMCP("analytics-source-matrix-test")
    registry_rows = _registry_rows()

    async def saxo_auth_status() -> dict[str, JsonValue]:
        _record(state, "saxo_auth_status", {})
        return {
            "requested_environment": "SIM",
            "effective_read_environment": "SIM",
            "live_reads": False,
            "live_writes": False,
            "token_cache_present": state.auth_available,
            "token_cache_readable": state.auth_available,
            "token_cache_expired": not state.auth_available,
            "token_cache_refresh_supported": True,
            "token_cache_environment": "SIM",
            "blocking_reasons": [] if state.auth_available else ["token_cache_expired"],
        }

    async def saxo_get_session_capabilities() -> dict[str, JsonValue]:
        _record(state, "saxo_get_session_capabilities", {})
        return {
            "status": "passed" if state.auth_available else "auth_required",
            "environment": "SIM",
            "network_call_made": state.auth_available,
        }

    async def saxo_get_entitlements() -> dict[str, JsonValue]:
        _record(state, "saxo_get_entitlements", {})
        return {
            "status": "passed" if state.auth_available else "auth_required",
            "environment": "SIM",
            "network_call_made": state.auth_available,
            "entitlement_count": 2 if state.auth_available else 0,
        }

    async def saxo_list_registered_endpoints(
        service_group: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "service_group": service_group,
            "limit": limit,
            "offset": offset,
        }
        _record(state, "saxo_list_registered_endpoints", arguments)
        selected = [
            row
            for row in registry_rows
            if service_group is None or row["service_group"] == service_group
        ]
        next_offset = offset + limit if offset + limit < len(selected) else None
        return {
            "status": "metadata_only_not_ready_for_trading",
            "network_call_made": False,
            "operations": selected[offset : offset + limit],
            "next_offset": next_offset,
            "returned_count": len(selected[offset : offset + limit]),
        }

    async def saxo_call_registered_endpoint(
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        response_mode: str = "redacted_body",
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "method": method,
            "path": path,
            "params": cast("dict[str, JsonValue]", params or {}),
            "response_mode": response_mode,
        }
        _record(state, "saxo_call_registered_endpoint", arguments)
        if path in {
            "/port/v1/orders/me",
            "/port/v1/positions/me",
            "/port/v1/balances/me",
        }:
            read_number = state.state_reads.get(path, 0)
            state.state_reads[path] = read_number + 1
            changed = state.changed_state and read_number > 0
            return {
                "status": "passed",
                "operation_id": find_registered_operation("GET", path).operation_id,  # type: ignore[union-attr]
                "method": "GET",
                "path": path,
                "environment": "SIM",
                "network_call_made": True,
                "response_visibility": (
                    "fingerprint_only" if path.endswith("/balances/me") else "redacted_body"
                ),
                "response": None,
                "response_fingerprint": _safe_sha("b" if changed else "a"),
                "response_fingerprint_scope": "fake_state",
                "http_status": 200,
            }
        operation = find_registered_operation("GET", path)
        assert operation is not None
        if path == "/chart/v3/charts":
            cursor = "" if params is None else params.get("$skiptoken", "")
            next_link: str | None = None
            if not cursor or state.pagination_cycle:
                next_link = "/chart/v3/charts?AssetType=Stock&Count=2&Uic=211&$skiptoken=page-2"
            payload: dict[str, JsonValue] = {
                "Data": [
                    {
                        "Time": "2026-07-29T10:00:00Z" if not cursor else "2026-07-29T11:00:00Z",
                        "CloseBid": 1.0 if not cursor else 1.1,
                    },
                ],
                "DataVersion": 1,
                "__next": next_link,
            }
            return {
                "status": "passed",
                "operation_id": operation.operation_id,
                "method": "GET",
                "path": operation.path_template,
                "environment": "SIM",
                "network_call_made": True,
                "response_visibility": "redacted_body",
                "response": json.dumps(payload),
                "response_fingerprint": _safe_sha("c"),
                "response_fingerprint_scope": "raw_response_body",
                "http_status": 200,
            }
        if path == "/port/v1/balances":
            assert response_mode == "fingerprint_only"
            return {
                "status": "passed",
                "operation_id": operation.operation_id,
                "method": "GET",
                "path": operation.path_template,
                "environment": "SIM",
                "network_call_made": True,
                "response_visibility": "fingerprint_only",
                "response": None,
                "response_fingerprint": _safe_sha("d"),
                "response_fingerprint_scope": "account_money_state_fields",
                "http_status": 200,
            }
        return {
            "status": "http_error",
            "operation_id": operation.operation_id,
            "method": "GET",
            "path": operation.path_template,
            "environment": "SIM",
            "network_call_made": True,
            "response_visibility": "redacted_body",
            "response_fingerprint": _safe_sha("e"),
            "response_fingerprint_scope": "raw_response_body",
            "http_status": 403,
            "response": json.dumps(
                {
                    "ErrorCode": "NoAccess",
                    "Message": PRIVATE_DISPLAY_MARKER,
                    "AccountKey": PRIVATE_ACCOUNT_MARKER,
                    "ClientKey": PRIVATE_CLIENT_MARKER,
                    "Balance": PRIVATE_MONEY_MARKER,
                },
            ),
        }

    async def saxo_get_safe_request_ledger(
        *,
        clear: bool = False,
    ) -> dict[str, JsonValue]:
        _record(state, "saxo_get_safe_request_ledger", {"clear": clear})
        if clear:
            return {
                "status": "cleared",
                "ledger_complete": True,
                "events_evicted": 0,
                "negative_proof_available": True,
                "request_count": 0,
                "non_get_request_count": 0,
                "events": [],
            }
        source_calls = [
            arguments
            for tool, arguments in state.calls
            if tool in {"saxo_call_registered_endpoint", "saxo_get_session_capabilities"}
        ]
        events: list[dict[str, JsonValue]] = [
            {
                "timestamp": "2026-07-30T12:00:00.000+00:00",
                "phase": "attempted",
                "host_role": "gateway",
                "method": "GET",
                "path": "/openapi/{redacted}",
                "query_names": [],
                "query_present": False,
                "status": None,
            }
            for _arguments in source_calls
        ]
        if state.unsafe_ledger:
            events.append(
                {
                    "timestamp": "2026-07-30T12:00:01.000+00:00",
                    "phase": "attempted",
                    "host_role": "gateway",
                    "method": "POST",
                    "path": "/openapi/trade/v2/orders",
                    "query_names": [],
                    "query_present": False,
                    "status": None,
                },
            )
        return {
            "status": "passed",
            "ledger_complete": True,
            "events_evicted": 0,
            "negative_proof_available": True,
            "request_count": len(events),
            "non_get_request_count": int(state.unsafe_ledger),
            "unsafe_gateway_request_detected": state.unsafe_ledger,
            "order_placement_endpoint_called": state.unsafe_ledger,
            "events": events,
        }

    for tool in (
        saxo_auth_status,
        saxo_get_session_capabilities,
        saxo_get_entitlements,
        saxo_list_registered_endpoints,
        saxo_call_registered_endpoint,
        saxo_get_safe_request_ledger,
    ):
        server.tool()(tool)
    return server


def _sim_env() -> dict[str, str]:
    return {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }


def test_environment_proof_requires_exact_sim_and_disabled_live_gates() -> None:
    passed = prove_sim_environment(_sim_env())
    assert passed.status == "passed"
    assert passed.selected_environment == "SIM"
    assert passed.live_reads_enabled is False
    assert passed.live_writes_enabled is False
    assert passed.network_allowed is True

    for unsafe in (
        {"SAXO_MCP_ENVIRONMENT": "LIVE"},
        {
            "SAXO_MCP_ENVIRONMENT": "SIM",
            "SAXO_MCP_ENABLE_LIVE_READS": "1",
        },
        {
            "SAXO_MCP_ENVIRONMENT": "SIM",
            "SAXO_MCP_ENABLE_LIVE_WRITES": "I_UNDERSTAND_REAL_MONEY_RISK",
        },
        {},
    ):
        refused = prove_sim_environment(unsafe)
        assert refused.status == "refused"
        assert refused.network_allowed is False


@pytest.mark.anyio
async def test_matrix_looks_up_and_receipts_all_18_contracts_through_fastmcp() -> None:
    state = FakeMatrixState()
    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert receipt.status == "reduced"
    assert receipt.environment == "SIM"
    assert receipt.auth_status == "ready"
    assert receipt.candidate_commit == ANALYTICS_SOURCE_CANDIDATE
    assert len(receipt.source_receipts) == EXPECTED_SOURCE_COUNT
    assert {item.contract_id for item in receipt.source_receipts} == EXPECTED_CONTRACT_IDS
    assert all(item.registered_operation_matched for item in receipt.source_receipts)
    assert all(item.mcp_call_observed for item in receipt.source_receipts)
    source_tools = [
        tool
        for tool, arguments in state.calls
        if tool == "saxo_call_registered_endpoint"
        and arguments["path"]
        not in {
            "/port/v1/orders/me",
            "/port/v1/positions/me",
            "/port/v1/balances/me",
        }
    ]
    assert len(source_tools) >= EXPECTED_SOURCE_COUNT
    first_source_index = next(
        index
        for index, (tool, _arguments) in enumerate(state.calls)
        if tool == "saxo_call_registered_endpoint"
    )
    registry_indices = [
        index
        for index, (tool, _arguments) in enumerate(state.calls)
        if tool == "saxo_list_registered_endpoints"
    ]
    assert registry_indices
    assert max(registry_indices) < first_source_index
    assert not any(tool.startswith("http") for tool, _arguments in state.calls)


@pytest.mark.anyio
async def test_matrix_records_structural_pagination_and_hashed_timestamps() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState()),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    chart = next(item for item in receipt.source_receipts if item.contract_id == "chart_v3")
    assert chart.source_status == "observed"
    assert chart.page_count == EXPECTED_PAGE_COUNT
    assert chart.row_count == EXPECTED_PAGE_COUNT
    assert chart.continuation_call_count == 1
    assert chart.pagination_state == "completed"
    assert chart.timestamp_value_count == EXPECTED_PAGE_COUNT
    assert len(chart.timestamp_fingerprint_sha256) == SHA256_LENGTH
    assert {field.path for field in chart.schema_fields} >= {
        "Data",
        "Data[]",
        "Data[].CloseBid",
        "Data[].Time",
        "DataVersion",
    }
    serialized = receipt.model_dump_json()
    assert "page-2" not in serialized
    assert "2026-07-29T10:00:00Z" not in serialized


@pytest.mark.anyio
async def test_matrix_records_entitlement_reduction_and_fingerprint_only_balance() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState()),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    denied = [item for item in receipt.source_receipts if item.entitlement_state == "denied"]
    assert denied
    assert all(item.source_status == "reduced" for item in denied)
    assert all(item.outcome_reason == "source_entitlement_unavailable" for item in denied)
    balance = next(item for item in receipt.source_receipts if item.contract_id == "balances_v1")
    assert balance.source_status == "reduced"
    assert balance.response_visibility == "fingerprint_only"
    assert balance.outcome_reason == "fingerprint_only_schema_unavailable"
    assert balance.response_fingerprint_sha256 == _safe_sha("d")


@pytest.mark.anyio
async def test_empty_history_cannot_trigger_unprovable_controlled_activity() -> None:
    state = FakeMatrixState()
    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
        allow_controlled_activity=True,
    )

    assert receipt.controlled_activity.status == "refused"
    assert receipt.controlled_activity.reason == "exact_unchanged_state_proof_not_guaranteed"
    assert receipt.controlled_activity.mutation_calls == 0
    assert receipt.cleanup.complete is True
    assert receipt.cleanup.resources_created == 0
    assert not any(
        tool
        in {
            "saxo_place_order",
            "saxo_place_sim_order",
            "saxo_create_order_preview",
            "saxo_register_disclaimer_response",
        }
        for tool, _arguments in state.calls
    )


@pytest.mark.anyio
async def test_matrix_proves_cleanup_state_equality_and_safe_sim_ledger() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState()),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert receipt.cleanup.before_fingerprint == receipt.cleanup.after_fingerprint
    assert receipt.cleanup.state_equal is True
    assert receipt.cleanup.complete is True
    assert receipt.ledger.ledger_complete is True
    assert receipt.ledger.events_evicted == 0
    assert receipt.ledger.negative_proof_available is True
    assert receipt.ledger.methods == ("GET",)
    assert receipt.ledger.host_roles == ("gateway",)
    assert receipt.ledger.non_get_request_count == 0
    assert receipt.ledger.sim_only is True
    assert receipt.ledger.live_events == 0
    assert receipt.live_events == 0
    assert receipt.live_mutation_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (FakeMatrixState(changed_state=True), "state_fingerprint_mismatch"),
        (FakeMatrixState(unsafe_ledger=True), "unsafe_request_ledger"),
    ],
)
async def test_matrix_fails_closed_on_state_change_or_unsafe_ledger(
    state: FakeMatrixState,
    expected_reason: str,
) -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert receipt.status == "failed"
    assert expected_reason in receipt.errors


@pytest.mark.anyio
async def test_pagination_cycle_is_reduced_without_copying_cursor() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState(pagination_cycle=True)),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    chart = next(item for item in receipt.source_receipts if item.contract_id == "chart_v3")
    assert chart.source_status == "reduced"
    assert chart.pagination_state == "cycle_refused"
    assert chart.outcome_reason == "pagination_cycle_detected"
    assert chart.network_call_count == EXPECTED_PAGE_COUNT
    assert "page-2" not in receipt.model_dump_json()


@pytest.mark.anyio
async def test_missing_auth_emits_18_refusals_without_source_network_calls() -> None:
    state = FakeMatrixState(auth_available=False)
    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert receipt.status == "refused"
    assert receipt.auth_status == "auth_required"
    assert len(receipt.source_receipts) == EXPECTED_SOURCE_COUNT
    assert {item.source_status for item in receipt.source_receipts} == {"refused"}
    assert {item.outcome_reason for item in receipt.source_receipts} == {
        "sim_session_unavailable",
    }
    assert not any(tool == "saxo_call_registered_endpoint" for tool, _arguments in state.calls)


@pytest.mark.anyio
async def test_evidence_contains_no_raw_private_values_and_passes_secret_scan() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState()),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    serialized = receipt.model_dump_json()
    assert PRIVATE_ACCOUNT_MARKER not in serialized
    assert PRIVATE_CLIENT_MARKER not in serialized
    assert PRIVATE_DISPLAY_MARKER not in serialized
    assert str(PRIVATE_MONEY_MARKER) not in serialized
    findings, errors = scan_secret_text("source-matrix.json", serialized)
    assert findings == []
    assert errors == []
    assert receipt.privacy.findings == 0
    assert receipt.privacy.scan_errors == 0


def test_candidate_guard_is_single_use_and_evidence_is_owner_only(
    tmp_path: Path,
) -> None:
    out = tmp_path / ".omo/evidence/saxo-bank-mcp/analytics-source-matrix/matrix.json"
    state = FakeMatrixState()
    first = execute_analytics_source_matrix_once(
        out=out,
        server=_matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        current_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )
    first_payload = out.read_text(encoding="utf-8")
    call_count = len(state.calls)
    second = execute_analytics_source_matrix_once(
        out=out,
        server=_matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        current_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert first == 0
    assert second == 1
    assert len(state.calls) == call_count
    assert stat.S_IMODE(out.parent.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert stat.S_IMODE(out.stat().st_mode) == OWNER_FILE_MODE
    guard = candidate_guard_path(out, ANALYTICS_SOURCE_CANDIDATE)
    assert guard.exists()
    assert stat.S_IMODE(guard.stat().st_mode) == OWNER_FILE_MODE
    assert out.read_text(encoding="utf-8") == first_payload


def test_candidate_mismatch_and_unsafe_environment_do_not_consume_run_guard(
    tmp_path: Path,
) -> None:
    for suffix, env, current_commit, reason in (
        ("mismatch", _sim_env(), "f" * 40, "candidate_commit_mismatch"),
        (
            "live",
            {"SAXO_MCP_ENVIRONMENT": "LIVE"},
            ANALYTICS_SOURCE_CANDIDATE,
            "environment_not_sim",
        ),
    ):
        out = tmp_path / suffix / "matrix.json"
        state = FakeMatrixState()
        result = execute_analytics_source_matrix_once(
            out=out,
            server=_matrix_server(state),
            env=env,
            fixtures=_fixtures(),
            candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
            current_commit=current_commit,
            captured_at=CAPTURED_AT,
        )
        payload = JSON_OBJECT.validate_json(out.read_text(encoding="utf-8"))
        assert result == 1
        assert payload["reason"] == reason
        assert state.calls == []
        assert not candidate_guard_path(out, ANALYTICS_SOURCE_CANDIDATE).exists()


def test_missing_private_fixtures_reduce_only_scoped_sources_and_claim_guard(
    tmp_path: Path,
) -> None:
    out = tmp_path / "missing-fixtures" / "matrix.json"
    state = FakeMatrixState()

    result = execute_analytics_source_matrix_once(
        out=out,
        server=_matrix_server(state),
        env=_sim_env(),
        fixtures=SourceMatrixFixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        current_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert result == 0
    payload = JSON_OBJECT.validate_json(out.read_text(encoding="utf-8"))
    assert payload["status"] == "reduced"
    receipts = payload["source_receipts"]
    assert isinstance(receipts, list)
    unavailable: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        contract_id = receipt.get("contract_id")
        if receipt.get("outcome_reason") == "source_fixture_unavailable" and isinstance(
            contract_id, str
        ):
            unavailable.add(contract_id)
    assert unavailable == {
        "bookings_v1",
        "closed_positions_history_v1",
        "costs_v1",
    }
    source_calls = [
        arguments
        for tool, arguments in state.calls
        if tool == "saxo_call_registered_endpoint"
        and arguments["path"]
        not in {
            "/port/v1/orders/me",
            "/port/v1/positions/me",
            "/port/v1/balances/me",
        }
    ]
    assert len({str(call["path"]) for call in source_calls}) == (
        EXPECTED_SOURCE_COUNT - len(unavailable)
    )
    assert len(source_calls) >= EXPECTED_SOURCE_COUNT - len(unavailable)
    assert candidate_guard_path(out, ANALYTICS_SOURCE_CANDIDATE).exists()


def test_unexpected_matrix_failure_is_frozen_after_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "failed-run" / "matrix.json"

    async def fail_run(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("private upstream detail")

    monkeypatch.setattr(matrix_module, "run_analytics_source_matrix", fail_run)
    result = execute_analytics_source_matrix_once(
        out=out,
        server=_matrix_server(FakeMatrixState()),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_commit=ANALYTICS_SOURCE_CANDIDATE,
        current_commit=ANALYTICS_SOURCE_CANDIDATE,
        captured_at=CAPTURED_AT,
    )

    assert result == 1
    assert JSON_OBJECT.validate_json(out.read_text(encoding="utf-8")) == {
        "status": "failed",
        "reason": "matrix_execution_failed",
    }
    assert candidate_guard_path(out, ANALYTICS_SOURCE_CANDIDATE).exists()
    assert "private upstream detail" not in out.read_text(encoding="utf-8")


def test_private_evidence_destination_is_gitignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert ".omo/" in gitignore
