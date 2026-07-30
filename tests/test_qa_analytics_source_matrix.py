from __future__ import annotations

import hashlib
import inspect
import json
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

import pytest
from fastmcp import FastMCP

import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.endpoint_registry import find_registered_operation
from saxo_bank_mcp.qa_analytics_source_matrix import (
    SourceMatrixCandidateIdentity,
    SourceMatrixFixtures,
    candidate_evidence_path,
    candidate_guard_path,
    execute_analytics_source_matrix_once,
    prove_sim_environment,
    run_analytics_source_matrix,
)
from saxo_bank_mcp.secret_scan import scan_secret_text

EXPECTED_SOURCE_COUNT = 18
OWNER_DIRECTORY_MODE = 0o700
OWNER_FILE_MODE = 0o600
CAPTURED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
PRIVATE_ACCOUNT_MARKER = "private-account-marker"
PRIVATE_CLIENT_MARKER = "private-client-marker"


@dataclass(slots=True)
class FakeMatrixState:
    calls: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    auth_available: bool = True
    session_available: bool = True
    entitlements_available: bool = True
    changed_state: bool = False
    unsafe_method: bool = False
    gateway_environment: str | None = "SIM"
    oauth_event: bool = False
    history_rows: int = 1
    source_status: str = "passed"
    state_reads: dict[str, int] = field(default_factory=dict)
    ledger_start: int = 0


def _identity(character: str = "a") -> SourceMatrixCandidateIdentity:
    return SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256=character * 64,
        harness_build_sha256=chr(ord(character) + 1) * 64,
        candidate_identity_sha256=chr(ord(character) + 2) * 64,
    )


def _fixtures() -> SourceMatrixFixtures:
    return SourceMatrixFixtures(
        account_key=PRIVATE_ACCOUNT_MARKER,
        client_key=PRIVATE_CLIENT_MARKER,
        instrument_uic=211,
        asset_type="Stock",
        option_root_id=120,
    )


def _sim_env(tmp_path: Path | None = None) -> dict[str, str]:
    env = {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }
    if tmp_path is not None:
        env["XDG_STATE_HOME"] = str(tmp_path)
    return env


def _record(
    state: FakeMatrixState,
    tool: str,
    arguments: dict[str, JsonValue],
) -> None:
    state.calls.append((tool, arguments))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()


def _request_fingerprint(
    contract_id: str,
    path_template: str,
    resolved_path: str,
    params: dict[str, str],
) -> str:
    request: dict[str, object] = dict(params)
    for template, resolved in zip(
        path_template.strip("/").split("/"),
        resolved_path.strip("/").split("/"),
        strict=True,
    ):
        if template.startswith("{") and template.endswith("}"):
            request[template[1:-1]] = resolved
    return _digest({"contract_id": contract_id, "request": request})


def _registry_rows() -> list[dict[str, JsonValue]]:
    paths = [contract.path_template for contract in source_contracts_by_id().values()]
    paths.extend(
        (
            "/port/v1/orders/me",
            "/port/v1/positions/me",
            "/port/v1/balances/me",
        ),
    )
    rows: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
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


def _matrix_server(state: FakeMatrixState) -> FastMCP:  # noqa: C901
    server = FastMCP("analytics-source-matrix-test")
    registry_rows = _registry_rows()

    async def saxo_auth_status() -> dict[str, JsonValue]:
        _record(state, "saxo_auth_status", {})
        return {
            "status": "passed",
            "tool_name": "saxo_auth_status",
            "call_class": "local_status_succeeded",
            "requested_environment": "SIM",
            "effective_read_environment": "SIM",
            "live_reads": False,
            "live_writes": False,
            "sim_credentials_present": True,
            "sim_credential_source": "env",
            "live_credentials_present": False,
            "sim_redirect_uri_present": False,
            "pending_pkce_authorization_present": False,
            "token_cache_present": state.auth_available,
            "token_cache_readable": state.auth_available,
            "token_cache_expired": not state.auth_available,
            "token_cache_refresh_supported": False,
            "token_cache_environment": "SIM",
            "scope_used": False,
            "verifies": [],
            "does_not_verify": [],
            "blocking_reasons": [] if state.auth_available else ["token_cache_expired"],
            "next_action": "test readiness action",
            "network_call_made": False,
            "live_write_called": False,
            "order_or_subscription_created": False,
        }

    async def saxo_get_session_capabilities() -> dict[str, JsonValue]:
        _record(state, "saxo_get_session_capabilities", {})
        return {
            "status": "passed" if state.session_available else "auth_required",
            "tool_name": "saxo_get_session_capabilities",
            "environment": "SIM",
            "call_class": "sim_read_succeeded",
            "endpoint_path": "/root/v1/sessions/capabilities",
            "token_refreshed": False,
            "token": {
                "has_access_token": True,
                "has_refresh_token": False,
                "has_code_verifier": False,
                "environment": "SIM",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "is_expired": False,
            },
            "token_refresh_supported": False,
            "scope_used": False,
            "network_call_made": state.session_available,
            "live_write_called": False,
            "order_or_subscription_created": False,
            "capabilities": {
                "AuthenticationLevel": "Strong",
                "DataLevel": "Full",
                "TradeLevel": "None",
            },
            "next_action": "use current capability fields only",
            "verifies": [
                "cached SIM bearer token can read current session capability fields",
            ],
            "does_not_verify": [
                "order placement safety",
                "instrument/account suitability",
                "real-money approval",
                "live endpoint access",
            ],
        }

    async def saxo_get_entitlements() -> dict[str, JsonValue]:
        _record(state, "saxo_get_entitlements", {})
        return {
            "status": "passed" if state.entitlements_available else "auth_required",
            "tool_name": "saxo_get_entitlements",
            "environment": "SIM",
            "call_class": "sim_read_succeeded",
            "endpoint_path": "/port/v1/users/me/entitlements",
            "entitlement_field_set": "Default",
            "token_refreshed": False,
            "network_call_made": state.entitlements_available,
            "live_write_called": False,
            "order_or_subscription_created": False,
            "entitlement_summary": {
                "exchange_count": 0,
                "max_rows": 1000,
                "response_count": 0,
                "has_next_page": False,
                "possibly_truncated": False,
            },
            "exchange_ids": [],
            "entitlement_bucket_counts": {
                "DelayedFullBook": 0,
                "DelayedGreeks": 0,
                "Greeks": 0,
                "RealTimeFullBook": 0,
                "RealTimeTopOfBook": 0,
            },
            "verifies": [
                "cached SIM bearer token can read current market-data entitlement summary",
            ],
            "does_not_verify": [
                "price availability for a specific instrument",
                "quote recency or real-time price delivery for any instrument",
                "order placement safety",
                "instrument/account suitability",
                "real-money approval",
                "live endpoint access",
            ],
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
        response_mode: str = "fingerprint_only",
        analytics_contract_id: str | None = None,
    ) -> dict[str, JsonValue]:
        arguments: dict[str, JsonValue] = {
            "method": method,
            "path": path,
            "params": cast("dict[str, JsonValue]", params or {}),
            "response_mode": response_mode,
            "analytics_contract_id": analytics_contract_id,
        }
        _record(state, "saxo_call_registered_endpoint", arguments)
        operation = find_registered_operation("GET", path)
        assert operation is not None
        if path in {
            "/port/v1/orders/me",
            "/port/v1/positions/me",
            "/port/v1/balances/me",
        }:
            read_number = state.state_reads.get(path, 0)
            state.state_reads[path] = read_number + 1
            changed = state.changed_state and read_number > 0
            scope = (
                "account_money_state_fields"
                if path == "/port/v1/balances/me"
                else "raw_response_body"
            )
            return {
                "status": "passed",
                "operation_id": operation.operation_id,
                "method": "GET",
                "path": operation.path_template,
                "environment": "SIM",
                "network_call_made": True,
                "response_visibility": "fingerprint_only",
                "response": None,
                "response_fingerprint": ("b" if changed else "a") * 64,
                "response_fingerprint_scope": scope,
                "http_status": 200,
            }
        assert analytics_contract_id is not None
        assert response_mode == "analytics_contract_receipt"
        contract = source_contracts_by_id()[analytics_contract_id]
        rows = (
            state.history_rows
            if analytics_contract_id
            in {"transactions_v1", "bookings_v1", "closed_positions_history_v1"}
            else 1
        )
        status = state.source_status
        page_count = 2 if analytics_contract_id == "chart_v3" else 1
        page_receipts: list[dict[str, JsonValue]] = [
            {
                "page_number": page_number,
                "row_count": rows if page_number == 1 else 0,
                "page_fingerprint_sha256": str(page_number) * 64,
                "source_revision_sha256": "e" * 64,
                "schema_fingerprint_sha256": "f" * 64,
                "timestamp_value_count": rows if page_number == 1 else 0,
                "timestamp_fingerprint_sha256": "9" * 64,
            }
            for page_number in range(1, page_count + 1)
        ]
        passed = status == "passed"
        return {
            "status": status,
            "tool_name": "saxo_call_registered_endpoint",
            "call_class": ("sim_read_succeeded" if passed else "sim_read_http_error"),
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "method": "GET",
            "path": operation.path_template,
            "environment": "SIM",
            "network_call_made": True,
            "network_call_count": page_count if passed else 1,
            "attempt_count": page_count if passed else 1,
            "initial_attempt_count": 1,
            "continuation_attempt_count": page_count - 1 if passed else 0,
            "retry_count": 0,
            "distinct_target_count": page_count if passed else 1,
            "successful_page_count": page_count if passed else 0,
            "live_write_called": False,
            "order_or_subscription_created": False,
            "arbitrary_url_allowed": False,
            "live_write": False,
            "live_access": False,
            "auth_exercised": True,
            "trading_ready": False,
            "response_visibility": "analytics_contract_receipt",
            "response": None,
            "response_fingerprint": _digest(page_receipts) if passed else None,
            "response_fingerprint_scope": "analytics_contract_receipt",
            "analytics_contract_id": analytics_contract_id,
            "analytics_contract_sha256": source_contract_fingerprint(contract),
            "page_count": page_count if passed else 0,
            "row_count": rows if passed else 0,
            "continuation_call_count": page_count - 1 if passed else 0,
            "page_receipts": page_receipts if passed else [],
            "source_revision_fingerprint_sha256": (
                _digest(["e" * 64] * page_count) if passed else _digest([])
            ),
            "timestamp_value_count": rows if passed else 0,
            "timestamp_fingerprint_sha256": (
                _digest(["9" * 64] * page_count) if passed else _digest([])
            ),
            "request_fingerprint_sha256": _request_fingerprint(
                analytics_contract_id,
                contract.path_template,
                path,
                params or {},
            ),
            "http_status": 200,
        }

    async def saxo_get_safe_request_ledger(
        *,
        clear: bool = False,
    ) -> dict[str, JsonValue]:
        _record(state, "saxo_get_safe_request_ledger", {"clear": clear})
        if clear:
            state.ledger_start = len(state.calls)
            return {
                "status": "cleared",
                "tool_name": "saxo_get_safe_request_ledger",
                "scope": "current_mcp_session",
                "safe_fields_only": True,
                "ledger_complete": True,
                "events_evicted": 0,
                "negative_proof_available": True,
                "request_count": 0,
                "non_get_request_count": 0,
                "unsafe_gateway_request_detected": False,
                "order_placement_endpoint_called": False,
                "events": [],
            }
        source_calls = [
            arguments
            for tool, arguments in state.calls[state.ledger_start :]
            if tool == "saxo_call_registered_endpoint"
        ]
        events: list[dict[str, JsonValue]] = [
            {
                "timestamp": "2026-07-30T12:00:00.000+00:00",
                "phase": "attempted",
                "host_role": "gateway",
                **(
                    {}
                    if state.gateway_environment is None
                    else {"environment": state.gateway_environment}
                ),
                "method": "GET",
                "path": "/sim/openapi/{redacted}",
                "query_names": [],
                "query_present": False,
                "status": None,
            }
            for _arguments in source_calls
        ]
        if state.oauth_event:
            events.append(
                {
                    "timestamp": "2026-07-30T12:00:00.000+00:00",
                    "phase": "attempted",
                    "host_role": "oauth",
                    "environment": "LIVE",
                    "method": "POST",
                    "path": "/token",
                    "query_names": [],
                    "query_present": False,
                    "status": None,
                },
            )
        if state.unsafe_method:
            events.append(
                {
                    "timestamp": "2026-07-30T12:00:01.000+00:00",
                    "phase": "attempted",
                    "host_role": "gateway",
                    "environment": "SIM",
                    "method": "POST",
                    "path": "/sim/openapi/trade/v2/orders",
                    "query_names": [],
                    "query_present": False,
                    "status": None,
                },
            )
        return {
            "status": "passed",
            "tool_name": "saxo_get_safe_request_ledger",
            "scope": "current_mcp_session",
            "safe_fields_only": True,
            "ledger_complete": True,
            "events_evicted": 0,
            "negative_proof_available": True,
            "request_count": len(events),
            "non_get_request_count": sum(event["method"] != "GET" for event in events),
            "unsafe_gateway_request_detected": state.unsafe_method,
            "order_placement_endpoint_called": state.unsafe_method,
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


def test_environment_proof_requires_exact_sim_and_disabled_live_gates() -> None:
    assert prove_sim_environment(_sim_env()).status == "passed"
    for unsafe in (
        {"SAXO_MCP_ENVIRONMENT": "LIVE"},
        {"SAXO_MCP_ENVIRONMENT": "SIM", "SAXO_MCP_ENABLE_LIVE_READS": "1"},
        {"SAXO_MCP_ENVIRONMENT": "SIM", "SAXO_MCP_ENABLE_LIVE_WRITES": "1"},
        {},
    ):
        assert prove_sim_environment(unsafe).status == "refused"


@pytest.mark.anyio
async def test_matrix_claims_after_readiness_and_uses_safe_receipts_for_all_sources() -> None:
    state = FakeMatrixState()

    def claim() -> bool:
        _record(state, "__claim__", {})
        return True

    def validate() -> bool:
        _record(state, "__validate__", {})
        return True

    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
        claim_source_execution=claim,
        validate_source_execution=validate,
    )

    assert receipt.status == "passed"
    assert receipt.source_execution_claimed is True
    assert len(receipt.source_receipts) == EXPECTED_SOURCE_COUNT
    assert {item.response_visibility for item in receipt.source_receipts} == {
        "analytics_contract_receipt",
    }
    claim_index = next(
        index for index, (tool, _arguments) in enumerate(state.calls) if tool == "__claim__"
    )
    validation_index = next(
        index for index, (tool, _arguments) in enumerate(state.calls) if tool == "__validate__"
    )
    first_source_index = next(
        index
        for index, (tool, _arguments) in enumerate(state.calls)
        if tool == "saxo_call_registered_endpoint"
    )
    assert validation_index < claim_index < first_source_index
    assert all(
        arguments["response_mode"] != "redacted_body"
        for tool, arguments in state.calls
        if tool == "saxo_call_registered_endpoint"
    )

    changed_state = FakeMatrixState()
    changed_claims = 0

    def changed_claim() -> bool:
        nonlocal changed_claims
        changed_claims += 1
        return True

    changed_receipt = await run_analytics_source_matrix(
        _matrix_server(changed_state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
        claim_source_execution=changed_claim,
        validate_source_execution=lambda: False,
    )
    assert changed_receipt.status == "refused"
    assert changed_receipt.reason == "candidate_execution_closure_changed"
    assert changed_receipt.source_execution_claimed is False
    assert changed_claims == 0
    assert not any(
        tool == "saxo_call_registered_endpoint" for tool, _arguments in changed_state.calls
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (FakeMatrixState(auth_available=False), "sim_auth_unavailable"),
        (FakeMatrixState(session_available=False), "sim_session_unavailable"),
        (FakeMatrixState(entitlements_available=False), "sim_entitlements_unavailable"),
    ],
)
async def test_readiness_refusal_never_claims_or_calls_sources(
    state: FakeMatrixState,
    reason: str,
) -> None:
    claims = 0

    def claim() -> bool:
        nonlocal claims
        claims += 1
        return True

    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
        claim_source_execution=claim,
    )

    assert receipt.status == "refused"
    assert receipt.reason == reason
    assert receipt.source_execution_claimed is False
    assert receipt.history_state == "unverified"
    assert receipt.ledger.ledger_complete is False
    assert claims == 0
    assert not any(tool == "saxo_call_registered_endpoint" for tool, _arguments in state.calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("rows", "expected"),
    [(1, "present"), (0, "absent")],
)
async def test_history_state_is_tri_state(rows: int, expected: str) -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState(history_rows=rows)),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
    )

    assert receipt.history_state == expected
    assert "controlled_activity" not in receipt.model_dump()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        (FakeMatrixState(changed_state=True), "state_fingerprint_mismatch"),
        (FakeMatrixState(unsafe_method=True), "unsafe_request_ledger"),
        (FakeMatrixState(gateway_environment="LIVE"), "unsafe_request_ledger"),
        (FakeMatrixState(gateway_environment=None), "unsafe_request_ledger"),
    ],
)
async def test_matrix_fails_closed_on_state_or_ledger_proof(
    state: FakeMatrixState,
    expected_error: str,
) -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(state),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
    )

    assert receipt.status == "failed"
    assert expected_error in receipt.errors


@pytest.mark.anyio
async def test_live_oauth_auth_event_is_not_a_live_gateway_event() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState(oauth_event=True)),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
    )

    assert receipt.status == "passed"
    assert receipt.ledger.sim_only is True
    assert receipt.ledger.live_events == 0
    assert receipt.ledger.gateway_environments == ("SIM",)


@pytest.mark.anyio
async def test_matrix_receipt_contains_no_private_fixture_values() -> None:
    receipt = await run_analytics_source_matrix(
        _matrix_server(FakeMatrixState()),
        env=_sim_env(),
        fixtures=_fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
    )

    serialized = receipt.model_dump_json()
    assert PRIVATE_ACCOUNT_MARKER not in serialized
    assert PRIVATE_CLIENT_MARKER not in serialized
    findings, errors = scan_secret_text("source-matrix.json", serialized)
    assert findings == []
    assert errors == []


def test_one_shot_guard_and_evidence_are_global_immutable_and_owner_only(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state-root"
    identity = _identity()
    state = FakeMatrixState()
    first = matrix_module._execute_analytics_source_matrix_once(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        fixtures=_fixtures(),
        server=_matrix_server(state),
        env=_sim_env(tmp_path),
        captured_at=CAPTURED_AT,
        state_root=state_root,
        candidate_identity=identity,
    )
    evidence = candidate_evidence_path(
        state_root,
        identity.candidate_identity_sha256,
    )
    guard = candidate_guard_path(state_root, identity.candidate_identity_sha256)
    first_payload = evidence.read_bytes()
    call_count = len(state.calls)
    second = matrix_module._execute_analytics_source_matrix_once(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        fixtures=_fixtures(),
        server=_matrix_server(state),
        env=_sim_env(tmp_path),
        captured_at=CAPTURED_AT,
        state_root=state_root,
        candidate_identity=identity,
    )

    assert first == 0
    assert second == 1
    assert len(state.calls) > call_count
    assert evidence.read_bytes() == first_payload
    assert stat.S_IMODE(evidence.parent.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert stat.S_IMODE(evidence.stat().st_mode) == OWNER_FILE_MODE
    assert stat.S_IMODE(guard.stat().st_mode) == OWNER_FILE_MODE
    payload = json.loads(first_payload)
    assert payload["source_contract_catalog_sha256"] == (identity.source_contract_catalog_sha256)
    assert payload["harness_build_sha256"] == identity.harness_build_sha256
    assert payload["candidate_identity_sha256"] == identity.candidate_identity_sha256


def test_readiness_failure_writes_no_guard_or_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / "state-root"
    identity = _identity()
    result = matrix_module._execute_analytics_source_matrix_once(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        fixtures=_fixtures(),
        server=_matrix_server(FakeMatrixState(auth_available=False)),
        env=_sim_env(tmp_path),
        captured_at=CAPTURED_AT,
        state_root=state_root,
        candidate_identity=identity,
    )

    assert result == 1
    assert not candidate_guard_path(
        state_root,
        identity.candidate_identity_sha256,
    ).exists()
    assert not candidate_evidence_path(
        state_root,
        identity.candidate_identity_sha256,
    ).exists()


def test_post_claim_failure_is_frozen_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state-root"
    identity = _identity()

    async def fail_after_claim(
        *_args: object,
        **kwargs: object,
    ) -> Never:
        claim = kwargs["claim_source_execution"]
        assert callable(claim)
        assert claim()
        raise RuntimeError("private upstream detail")

    monkeypatch.setattr(matrix_module, "run_analytics_source_matrix", fail_after_claim)
    result = matrix_module._execute_analytics_source_matrix_once(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        fixtures=_fixtures(),
        server=_matrix_server(FakeMatrixState()),
        env=_sim_env(tmp_path),
        captured_at=CAPTURED_AT,
        state_root=state_root,
        candidate_identity=identity,
    )
    evidence = candidate_evidence_path(
        state_root,
        identity.candidate_identity_sha256,
    )

    assert result == 1
    assert json.loads(evidence.read_text(encoding="utf-8")) == {
        "reason": "matrix_execution_failed",
        "status": "failed",
    }
    assert "private upstream detail" not in evidence.read_text(encoding="utf-8")


def test_official_api_exposes_no_test_injection_or_output_routing() -> None:
    parameters = inspect.signature(execute_analytics_source_matrix_once).parameters

    assert tuple(parameters) == ("fixtures",)
