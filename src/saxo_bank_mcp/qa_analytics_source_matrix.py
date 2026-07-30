from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

import anyio
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    SourceContract,
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation, find_registered_operation
from saxo_bank_mcp.secret_scan import scan_secret_text
from saxo_bank_mcp.server import mcp

_CANDIDATE_RESOURCE_DIR: Final = "_analytics_source_matrix"
_CANDIDATE_RESOURCE_NAME: Final = "source_matrix_candidate.json"
_CANDIDATE_SOURCE_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data" / "analytics" / _CANDIDATE_RESOURCE_NAME
)
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_SOURCE_COUNT: Final = 18
_REGISTRY_PAGE_SIZE: Final = 100
_HTTP_STATUS_MIN: Final = 100
_HTTP_STATUS_MAX: Final = 599
_STATE_PATHS: Final = (
    "/port/v1/orders/me",
    "/port/v1/positions/me",
    "/port/v1/balances/me",
)
_HISTORY_CONTRACTS: Final = frozenset(
    {"transactions_v1", "bookings_v1", "closed_positions_history_v1"},
)
_SAFE_SOURCE_REASON_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_:.-]{0,159}$")
_JSON_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])

type MatrixClient = Client[FastMCPTransport]
type SourceStatus = Literal["observed", "reduced", "refused"]
type MatrixStatus = Literal["passed", "reduced", "refused", "failed"]
type PaginationState = Literal[
    "completed",
    "not_applicable",
    "cycle_refused",
    "reduced",
    "refused",
]
type HistoryState = Literal["present", "absent", "unverified"]


@dataclass(frozen=True, slots=True)
class SourceMatrixFixtures:
    account_key: str = ""
    client_key: str = ""
    instrument_uic: int = 211
    asset_type: str = "Stock"
    option_root_id: int = 120


class SourceMatrixCandidateIdentity(BaseModel):
    """Verified installed catalog and harness identity for one official candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _SourceMatrixCandidateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    harness_files: Mapping[str, str]
    harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class EnvironmentProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "refused"]
    selected_environment: Literal["SIM", "LIVE", "UNSET", "INVALID"]
    live_reads_enabled: bool
    live_writes_enabled: bool
    network_allowed: bool
    reasons: tuple[str, ...] = ()


class SourceContractReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str
    operation_id: str
    method: Literal["GET"] = "GET"
    path_template: str
    source_status: SourceStatus
    outcome_reason: str
    registered_operation_matched: bool
    mcp_call_observed: bool
    page_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    continuation_call_count: int = Field(ge=0)
    network_call_count: int = Field(ge=0)
    pagination_state: PaginationState
    entitlement_state: Literal["observed", "denied", "unverified", "not_required"]
    response_visibility: Literal[
        "analytics_contract_receipt",
        "fingerprint_only",
        "unavailable",
    ]
    response_fingerprint_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    request_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp_value_count: int = Field(ge=0)
    timestamp_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599)


class CleanupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    after_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    state_equal: bool
    resources_created: Literal[0] = 0
    resources_remaining: Literal[0] = 0
    complete: bool


class LedgerReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger_complete: bool
    events_evicted: int = Field(ge=0)
    negative_proof_available: bool
    request_count: int = Field(ge=0)
    non_get_request_count: int = Field(ge=0)
    methods: tuple[str, ...]
    host_roles: tuple[str, ...]
    gateway_environments: tuple[str, ...]
    sim_only: bool
    live_events: int = Field(ge=0)


class PrivacyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: int = Field(ge=0)
    scan_errors: int = Field(ge=0)
    raw_private_values_retained: Literal[False] = False


class AnalyticsSourceMatrixReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MatrixStatus
    reason: str
    environment: Literal["SIM"]
    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    captured_at: datetime
    environment_proof: EnvironmentProof
    auth_status: str
    session_status: str
    entitlement_status: str
    source_receipts: tuple[SourceContractReceipt, ...]
    history_state: HistoryState
    source_execution_claimed: bool
    cleanup: CleanupReceipt
    ledger: LedgerReceipt
    privacy: PrivacyReceipt
    live_events: int = Field(ge=0)
    live_mutation_calls: Literal[0] = 0
    errors: tuple[str, ...]


def prove_sim_environment(env: Mapping[str, str]) -> EnvironmentProof:
    raw_environment = env.get("SAXO_MCP_ENVIRONMENT", "").strip().upper()
    selected: Literal["SIM", "LIVE", "UNSET", "INVALID"]
    if raw_environment == "SIM":
        selected = "SIM"
    elif raw_environment == "LIVE":
        selected = "LIVE"
    elif not raw_environment:
        selected = "UNSET"
    else:
        selected = "INVALID"
    live_reads_enabled = _enabled(env.get("SAXO_MCP_ENABLE_LIVE_READS"))
    live_writes_enabled = _enabled(env.get("SAXO_MCP_ENABLE_LIVE_WRITES"))
    reasons: list[str] = []
    if selected != "SIM":
        reasons.append("environment_not_sim")
    if live_reads_enabled:
        reasons.append("live_reads_enabled")
    if live_writes_enabled:
        reasons.append("live_writes_enabled")
    allowed = not reasons
    return EnvironmentProof(
        status="passed" if allowed else "refused",
        selected_environment=selected,
        live_reads_enabled=live_reads_enabled,
        live_writes_enabled=live_writes_enabled,
        network_allowed=allowed,
        reasons=tuple(reasons),
    )


async def run_analytics_source_matrix(  # noqa: C901, PLR0911, PLR0912, PLR0913
    server: FastMCP,
    *,
    env: Mapping[str, str],
    fixtures: SourceMatrixFixtures,
    candidate_identity: SourceMatrixCandidateIdentity,
    captured_at: datetime | None = None,
    claim_source_execution: Callable[[], bool] | None = None,
) -> AnalyticsSourceMatrixReceipt:
    capture_time = captured_at or datetime.now(tz=UTC)
    if capture_time.tzinfo is None or capture_time.utcoffset() is None:
        raise ValueError("captured_at must include a UTC offset")
    proof = prove_sim_environment(env)
    if not proof.network_allowed:
        return _refused_without_calls(
            candidate_identity,
            capture_time,
            proof,
            proof.reasons[0] if proof.reasons else "environment_not_sim",
        )
    contracts = source_contracts_by_id()
    if len(contracts) != _SOURCE_COUNT:
        return _refused_without_calls(
            candidate_identity,
            capture_time,
            proof,
            "source_contract_count_mismatch",
        )
    errors: list[str] = []
    async with Client(server) as client:
        auth = await _call_tool(client, "saxo_auth_status", {})
        auth_status = _auth_receipt_status(auth)
        if auth_status != "ready":
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status="not_called",
                entitlement_status="not_called",
                reason="sim_auth_unavailable",
            )
        registry = await _registered_operation_receipts(client, contracts)
        if not all(registry.get(contract.operation_id) is True for contract in contracts.values()):
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status="not_called",
                entitlement_status="not_called",
                reason="registered_operation_mismatch",
            )
        session = await _call_tool(client, "saxo_get_session_capabilities", {})
        session_status = _safe_status(session)
        if session_status not in {"passed", "completed"}:
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status="unverified",
                reason="sim_session_unavailable",
            )
        entitlements = await _call_tool(client, "saxo_get_entitlements", {})
        entitlement_status = _safe_status(entitlements)
        if entitlement_status not in {"passed", "completed"}:
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status=entitlement_status,
                reason="sim_entitlements_unavailable",
            )
        cleared = await _call_tool(
            client,
            "saxo_get_safe_request_ledger",
            {"clear": True},
        )
        if (
            cleared.get("status") != "cleared"
            or cleared.get("ledger_complete") is not True
            or cleared.get("negative_proof_available") is not True
        ):
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status=entitlement_status,
                reason="request_ledger_unavailable",
            )
        if claim_source_execution is not None and not claim_source_execution():
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status=entitlement_status,
                reason="candidate_already_claimed",
            )
        before = await _state_fingerprint(client, registry)
        if before is None:
            errors.append("state_fingerprint_unverified")
        requests = _source_requests(fixtures, capture_time.date())
        source_receipts_list: list[SourceContractReceipt] = []
        for contract in contracts.values():
            request = requests.get(contract.contract_id)
            if request is None:
                source_receipts_list.append(
                    _unavailable_source_receipt(
                        contract,
                        registered=True,
                        reason="source_fixture_unavailable",
                    ),
                )
                continue
            source_receipts_list.append(
                await _run_provider_source(client, contract, request),
            )
        source_receipts = tuple(source_receipts_list)
        after = await _state_fingerprint(client, registry)
        cleanup = CleanupReceipt(
            before_fingerprint=before,
            after_fingerprint=after,
            state_equal=before is not None and before == after,
            complete=before is not None and before == after,
        )
        if not cleanup.state_equal:
            errors.append("state_fingerprint_mismatch")
        ledger_payload = await _call_tool(client, "saxo_get_safe_request_ledger", {})
        ledger = _ledger_receipt(ledger_payload)
        if not ledger.sim_only:
            errors.append("unsafe_request_ledger")
        if ledger.live_events:
            errors.append("live_events_detected")
        status = _matrix_status(source_receipts, errors)
        reason = errors[0] if errors else _matrix_reason(source_receipts, status)
        receipt = AnalyticsSourceMatrixReceipt(
            status=status,
            reason=reason,
            environment="SIM",
            source_contract_catalog_sha256=(candidate_identity.source_contract_catalog_sha256),
            harness_build_sha256=candidate_identity.harness_build_sha256,
            candidate_identity_sha256=candidate_identity.candidate_identity_sha256,
            captured_at=capture_time,
            environment_proof=proof,
            auth_status=auth_status,
            session_status=session_status,
            entitlement_status=entitlement_status,
            source_receipts=source_receipts,
            history_state=_history_state(source_receipts),
            source_execution_claimed=True,
            cleanup=cleanup,
            ledger=ledger,
            privacy=PrivacyReceipt(findings=0, scan_errors=0),
            live_events=ledger.live_events,
            errors=tuple(errors),
        )
    return _with_privacy_scan(receipt, fixtures)


def execute_analytics_source_matrix_once(
    *,
    fixtures: SourceMatrixFixtures | None = None,
) -> int:
    """Run only the installed candidate in its fixed owner-only state location."""
    return _execute_analytics_source_matrix_once(
        fixtures=fixtures,
        server=mcp,
        env=os.environ,
        captured_at=None,
        state_root=None,
        candidate_identity=None,
    )


def _execute_analytics_source_matrix_once(  # noqa: PLR0913
    *,
    fixtures: SourceMatrixFixtures | None,
    server: FastMCP,
    env: Mapping[str, str],
    captured_at: datetime | None,
    state_root: Path | None,
    candidate_identity: SourceMatrixCandidateIdentity | None,
) -> int:
    """Execute through the internal seam used for deterministic protocol tests."""
    selected_env = dict(env)
    proof = prove_sim_environment(selected_env)
    if not proof.network_allowed:
        return 1
    try:
        identity = (
            source_matrix_candidate_identity() if candidate_identity is None else candidate_identity
        )
        selected_state_root = (
            _source_matrix_state_root(selected_env)
            if state_root is None
            else state_root.resolve(strict=False)
        )
    except (OSError, ValidationError, ValueError):
        return 1
    selected_fixtures = fixtures or SourceMatrixFixtures(
        account_key=selected_env.get("SAXO_MCP_QA_ACCOUNT_KEY", ""),
        client_key=selected_env.get("SAXO_MCP_QA_CLIENT_KEY", ""),
    )
    guard = candidate_guard_path(
        selected_state_root,
        identity.candidate_identity_sha256,
    )
    evidence = candidate_evidence_path(
        selected_state_root,
        identity.candidate_identity_sha256,
    )
    claimed = False

    def claim() -> bool:
        nonlocal claimed
        claimed = _claim_candidate_guard(guard, identity)
        return claimed

    async def run() -> AnalyticsSourceMatrixReceipt:
        return await run_analytics_source_matrix(
            server,
            env=selected_env,
            fixtures=selected_fixtures,
            candidate_identity=identity,
            captured_at=captured_at,
            claim_source_execution=claim,
        )

    try:
        receipt = anyio.run(run)
    except Exception:  # noqa: BLE001 - freeze only after the irreversible claim
        if claimed:
            _write_immutable_failure(evidence, "matrix_execution_failed")
        return 1
    if not receipt.source_execution_claimed:
        return 1
    payload = cast("dict[str, JsonValue]", receipt.model_dump(mode="json"))
    published = _write_immutable_evidence(evidence, payload)
    return 0 if published and receipt.status in {"passed", "reduced"} else 1


def candidate_guard_path(state_root: Path, candidate_identity_sha256: str) -> Path:
    _require_sha256(candidate_identity_sha256)
    root = state_root.resolve(strict=False)
    return root / "qa" / "analytics-source-matrix" / candidate_identity_sha256 / "claimed.json"


def candidate_evidence_path(
    state_root: Path,
    candidate_identity_sha256: str,
) -> Path:
    return candidate_guard_path(
        state_root,
        candidate_identity_sha256,
    ).with_name("source-matrix.json")


async def _call_tool(
    client: MatrixClient,
    name: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    result = await client.call_tool(name, arguments, raise_on_error=False)
    try:
        return _JSON_OBJECT.validate_python(result.structured_content)
    except ValidationError:
        return {
            "status": "failed",
            "tool_name": name,
            "reason": "structured_result_invalid",
        }


async def _registered_operation_receipts(  # noqa: C901
    client: MatrixClient,
    contracts: Mapping[str, SourceContract],
) -> dict[str, bool]:
    expected: dict[str, EndpointOperation] = {}
    for contract in contracts.values():
        operation = find_registered_operation("GET", contract.path_template)
        if operation is not None:
            expected[operation.operation_id] = operation
    for path in _STATE_PATHS:
        operation = find_registered_operation("GET", path)
        if operation is not None:
            expected[operation.operation_id] = operation
    observed: dict[str, dict[str, JsonValue]] = {}
    service_groups = sorted({operation.service_group for operation in expected.values()})
    for service_group in service_groups:
        offset = 0
        while True:
            payload = await _call_tool(
                client,
                "saxo_list_registered_endpoints",
                {
                    "service_group": service_group,
                    "limit": _REGISTRY_PAGE_SIZE,
                    "offset": offset,
                },
            )
            operations = payload.get("operations")
            if isinstance(operations, Sequence) and not isinstance(operations, str):
                for item in operations:
                    if not isinstance(item, Mapping):
                        continue
                    operation_id = item.get("operation_id")
                    if isinstance(operation_id, str):
                        observed[operation_id] = dict(item)
            next_offset = payload.get("next_offset")
            if not isinstance(next_offset, int) or next_offset <= offset:
                break
            offset = next_offset
    return {
        operation_id: _registry_row_matches(operation, observed.get(operation_id))
        for operation_id, operation in expected.items()
    }


def _registry_row_matches(
    expected: EndpointOperation,
    observed: Mapping[str, JsonValue] | None,
) -> bool:
    if observed is None:
        return False
    return (
        observed.get("operation_id") == expected.operation_id
        and observed.get("method") == "GET"
        and observed.get("path_template") == expected.path_template
        and observed.get("read_write_class") == "read"
        and observed.get("mcp_support_policy") == "read_only_definition_registered"
    )


async def _run_provider_source(
    client: MatrixClient,
    contract: SourceContract,
    request: Mapping[str, object],
) -> SourceContractReceipt:
    path = _resolved_contract_path(contract, request)
    params = {
        key: _render_query_value(value)
        for key, value in request.items()
        if key in contract.query_parameters
    }
    payload = await _call_tool(
        client,
        "saxo_call_registered_endpoint",
        {
            "method": "GET",
            "path": path,
            "params": params,
            "response_mode": "analytics_contract_receipt",
            "analytics_contract_id": contract.contract_id,
        },
    )
    operation = find_registered_operation("GET", path)
    protocol_valid = (
        operation is not None
        and payload.get("analytics_contract_id") == contract.contract_id
        and payload.get("analytics_contract_sha256") == source_contract_fingerprint(contract)
        and payload.get("response") is None
        and payload.get("response_visibility") == "analytics_contract_receipt"
        and payload.get("response_fingerprint_scope") == "analytics_contract_receipt"
    )
    if operation is not None:
        _require_sim_registered_read(payload, operation)
    status = _safe_status(payload)
    passed = status == "passed" and protocol_valid
    raw_reason = payload.get("reason")
    reason = (
        ""
        if passed
        else (
            _safe_source_reason(raw_reason)
            if isinstance(raw_reason, str)
            else "analytics_contract_receipt_invalid"
        )
    )
    source_status: SourceStatus = (
        "observed" if passed else "reduced" if status in {"http_error", "refused"} else "refused"
    )
    page_count = _safe_count(payload.get("page_count")) if passed else 0
    row_count = _safe_count(payload.get("row_count")) if passed else 0
    continuation_count = _safe_count(payload.get("continuation_call_count")) if passed else 0
    network_count = _safe_count(payload.get("network_call_count"))
    page_receipts = payload.get("page_receipts")
    schema_hashes: list[str] = []
    if isinstance(page_receipts, Sequence) and not isinstance(page_receipts, str):
        for item in page_receipts:
            if isinstance(item, Mapping):
                schema_hash = _safe_fingerprint(item.get("schema_fingerprint_sha256"))
                if schema_hash is not None:
                    schema_hashes.append(schema_hash)
    return SourceContractReceipt(
        contract_id=contract.contract_id,
        operation_id=contract.operation_id,
        path_template=contract.path_template,
        source_status=source_status,
        outcome_reason=reason,
        registered_operation_matched=operation is not None,
        mcp_call_observed=True,
        page_count=page_count,
        row_count=row_count,
        continuation_call_count=continuation_count,
        network_call_count=network_count,
        pagination_state=(
            "completed"
            if passed and contract.pagination is not None
            else "not_applicable"
            if contract.pagination is None
            else "reduced"
            if source_status == "reduced"
            else "refused"
        ),
        entitlement_state=(
            "denied"
            if reason == "source_entitlement_unavailable"
            else "observed"
            if passed
            else "unverified"
        ),
        response_visibility=("analytics_contract_receipt" if protocol_valid else "unavailable"),
        response_fingerprint_sha256=_safe_fingerprint(
            payload.get("response_fingerprint"),
        ),
        request_fingerprint_sha256=_request_fingerprint(contract, request),
        schema_fingerprint_sha256=_digest(schema_hashes),
        timestamp_value_count=(_safe_count(payload.get("timestamp_value_count")) if passed else 0),
        timestamp_fingerprint_sha256=(
            _safe_fingerprint(payload.get("timestamp_fingerprint_sha256")) or _digest([])
        ),
        http_status=_safe_http_status(payload.get("http_status")),
    )


def _unavailable_source_receipt(
    contract: SourceContract,
    *,
    registered: bool,
    reason: str,
) -> SourceContractReceipt:
    return SourceContractReceipt(
        contract_id=contract.contract_id,
        operation_id=contract.operation_id,
        path_template=contract.path_template,
        source_status="refused",
        outcome_reason=_safe_source_reason(reason),
        registered_operation_matched=registered,
        mcp_call_observed=False,
        page_count=0,
        row_count=0,
        continuation_call_count=0,
        network_call_count=0,
        pagination_state="refused" if contract.pagination is not None else "not_applicable",
        entitlement_state="unverified",
        response_visibility="unavailable",
        request_fingerprint_sha256=_digest(
            {"contract_id": contract.contract_id, "request": "unavailable"},
        ),
        schema_fingerprint_sha256=_digest([]),
        timestamp_value_count=0,
        timestamp_fingerprint_sha256=_digest([]),
    )


async def _state_fingerprint(
    client: MatrixClient,
    registry: Mapping[str, bool],
) -> str | None:
    fingerprints: dict[str, str] = {}
    for path in _STATE_PATHS:
        operation = find_registered_operation("GET", path)
        if operation is None or registry.get(operation.operation_id) is not True:
            return None
        payload = await _call_tool(
            client,
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": path,
                "response_mode": "fingerprint_only",
            },
        )
        _require_sim_registered_read(payload, operation)
        if _safe_status(payload) != "passed":
            return None
        expected_scope = (
            "account_money_state_fields" if path == "/port/v1/balances/me" else "raw_response_body"
        )
        if (
            payload.get("response_visibility") != "fingerprint_only"
            or payload.get("response_fingerprint_scope") != expected_scope
        ):
            return None
        fingerprint = _safe_fingerprint(payload.get("response_fingerprint"))
        if fingerprint is None:
            return None
        fingerprints[operation.operation_id] = fingerprint
    return _digest(fingerprints)


def _ledger_receipt(payload: Mapping[str, JsonValue]) -> LedgerReceipt:
    events = payload.get("events")
    attempted: list[Mapping[str, JsonValue]] = []
    if isinstance(events, Sequence) and not isinstance(events, str):
        attempted = [
            item
            for item in events
            if isinstance(item, Mapping) and item.get("phase") == "attempted"
        ]
    methods = tuple(
        sorted({str(item.get("method", "unavailable")) for item in attempted}),
    )
    host_roles = tuple(
        sorted({str(item.get("host_role", "unavailable")) for item in attempted}),
    )
    gateway_events = [item for item in attempted if item.get("host_role") == "gateway"]
    gateway_non_get = sum(item.get("method") != "GET" for item in gateway_events)
    invalid_host = sum(item.get("host_role") not in {"gateway", "oauth"} for item in attempted)
    gateway_environments = tuple(
        sorted({str(item.get("environment", "UNKNOWN")) for item in gateway_events}),
    )
    ledger_complete = payload.get("ledger_complete") is True
    negative = payload.get("negative_proof_available") is True
    evicted = payload.get("events_evicted")
    events_evicted = evicted if isinstance(evicted, int) and evicted >= 0 else 0
    unsafe_declared = (
        payload.get("unsafe_gateway_request_detected") is True
        or payload.get("order_placement_endpoint_called") is True
    )
    live_events = sum(item.get("environment") == "LIVE" for item in gateway_events)
    unknown_gateway = sum(item.get("environment") not in {"SIM", "LIVE"} for item in gateway_events)
    sim_only = (
        ledger_complete
        and negative
        and events_evicted == 0
        and gateway_non_get == 0
        and invalid_host == 0
        and live_events == 0
        and unknown_gateway == 0
        and not unsafe_declared
    )
    request_count = payload.get("request_count")
    return LedgerReceipt(
        ledger_complete=ledger_complete,
        events_evicted=events_evicted,
        negative_proof_available=negative,
        request_count=(
            request_count
            if isinstance(request_count, int) and request_count >= 0
            else len(attempted)
        ),
        non_get_request_count=gateway_non_get,
        methods=methods,
        host_roles=host_roles,
        gateway_environments=gateway_environments,
        sim_only=sim_only,
        live_events=live_events,
    )


def _history_state(
    receipts: Sequence[SourceContractReceipt],
) -> HistoryState:
    histories = [item for item in receipts if item.contract_id in _HISTORY_CONTRACTS]
    if len(histories) != len(_HISTORY_CONTRACTS) or any(
        item.source_status != "observed" for item in histories
    ):
        return "unverified"
    return "present" if any(item.row_count > 0 for item in histories) else "absent"


def _source_requests(
    fixtures: SourceMatrixFixtures,
    to_date: date,
) -> dict[str, dict[str, object] | None]:
    from_date = to_date - timedelta(days=365)
    start = from_date.isoformat()
    end = to_date.isoformat()
    account = fixtures.account_key.strip()
    client = fixtures.client_key.strip()
    account_scope: dict[str, object] = {"AccountKey": account} if account else {}
    client_scope: dict[str, object] = {"ClientKey": client} if client else {}
    common_scope = {**account_scope, **client_scope}
    return {
        "chart_v3": {
            "AssetType": fixtures.asset_type,
            "Count": 2,
            "Uic": fixtures.instrument_uic,
        },
        "reference_instruments_v1": {
            "AssetTypes": fixtures.asset_type,
            "Uics": fixtures.instrument_uic,
            "$top": 10,
            **account_scope,
        },
        "reference_instrument_details_v1": {
            "AssetTypes": fixtures.asset_type,
            "Uics": fixtures.instrument_uic,
            "$top": 10,
            **account_scope,
        },
        "options_chain_reference_v1": {
            "OptionRootId": fixtures.option_root_id,
            **client_scope,
        },
        "info_price_v1": {
            "AssetType": fixtures.asset_type,
            "Uic": fixtures.instrument_uic,
            "Amount": 1,
            **account_scope,
        },
        "info_prices_list_v1": {
            "AssetType": fixtures.asset_type,
            "Uics": fixtures.instrument_uic,
            "Amount": 1,
            **account_scope,
        },
        "performance_summary_v4": {
            "StandardPeriod": "Year",
            **common_scope,
        },
        "performance_timeseries_v4": {
            "StandardPeriod": "Year",
            **common_scope,
        },
        "balances_v1": {**common_scope},
        "positions_v1": {"$top": 100, **common_scope},
        "orders_v1": {"$top": 100, **common_scope},
        "transactions_v1": {
            "$top": 100,
            "FromDate": start,
            "ToDate": end,
            **client_scope,
            **({"AccountKeys": account} if account else {}),
        },
        "bookings_v1": (
            {
                "ClientKey": client,
                "FromDate": start,
                "ToDate": end,
                "$top": 100,
                **account_scope,
            }
            if client
            else None
        ),
        "closed_positions_history_v1": (
            {
                "ClientKey": client,
                "FromDate": start,
                "ToDate": end,
                "$top": 100,
                **account_scope,
            }
            if client
            else None
        ),
        "exposure_instruments_v1": {
            "Uic": fixtures.instrument_uic,
            "AssetType": fixtures.asset_type,
            **common_scope,
        },
        "costs_v1": (
            {
                "AccountKey": account,
                "Uic": fixtures.instrument_uic,
                "AssetType": fixtures.asset_type,
                "Amount": 1,
                "HoldingPeriodInDays": 1,
                "Price": 50,
                "TradeContext": "Open",
            }
            if account
            else None
        ),
        "corporate_action_events_v2": {"$top": 100, **common_scope},
        "corporate_action_holdings_v2": {"$top": 100, **common_scope},
    }


def _require_sim_registered_read(
    payload: Mapping[str, JsonValue],
    operation: EndpointOperation,
) -> None:
    if (
        payload.get("operation_id") != operation.operation_id
        or payload.get("method") != "GET"
        or payload.get("path") != operation.path_template
        or payload.get("environment") != "SIM"
        or payload.get("live_write_called") is True
        or payload.get("order_or_subscription_created") is True
    ):
        raise RuntimeError("registered SIM read receipt mismatch")


def _request_fingerprint(
    contract: SourceContract,
    request: Mapping[str, object],
) -> str:
    return _digest(
        {
            "contract_id": contract.contract_id,
            "operation_id": contract.operation_id,
            "request": dict(request),
        },
    )


def _resolved_contract_path(
    contract: SourceContract,
    request: Mapping[str, object],
) -> str:
    path = contract.path_template
    for parameter in contract.path_parameters:
        value = request.get(parameter)
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise TypeError("required source path parameter unavailable")
        path = path.replace(f"{{{parameter}}}", str(value))
    return path


def _render_query_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, Sequence):
        items = tuple(cast("Sequence[object]", value))
        if all(
            isinstance(item, str | int | float) and not isinstance(item, bool) for item in items
        ):
            return ",".join(str(item) for item in items)
    raise TypeError("source query value unavailable")


def _matrix_status(
    receipts: Sequence[SourceContractReceipt],
    errors: Sequence[str],
) -> MatrixStatus:
    if errors:
        return "failed"
    statuses = {item.source_status for item in receipts}
    if statuses == {"observed"}:
        return "passed"
    if statuses == {"refused"}:
        return "refused"
    return "reduced"


def _matrix_reason(
    receipts: Sequence[SourceContractReceipt],
    status: MatrixStatus,
) -> str:
    if status == "passed":
        return ""
    reasons = sorted({item.outcome_reason for item in receipts if item.outcome_reason})
    return reasons[0] if reasons else "source_coverage_reduced"


def _with_privacy_scan(
    receipt: AnalyticsSourceMatrixReceipt,
    fixtures: SourceMatrixFixtures,
) -> AnalyticsSourceMatrixReceipt:
    text = receipt.model_dump_json()
    findings, scan_errors = scan_secret_text("source-matrix.json", text)
    private_values = tuple(
        value
        for value in (fixtures.account_key.strip(), fixtures.client_key.strip())
        if value and value in text
    )
    finding_count = len(findings) + len(private_values)
    errors = list(receipt.errors)
    status = receipt.status
    reason = receipt.reason
    if finding_count or scan_errors:
        errors.append("privacy_scan_failed")
        status = "failed"
        reason = "privacy_scan_failed"
    return receipt.model_copy(
        update={
            "status": status,
            "reason": reason,
            "privacy": PrivacyReceipt(
                findings=finding_count,
                scan_errors=len(scan_errors),
            ),
            "errors": tuple(errors),
        },
    )


def _refused_without_calls(
    candidate_identity: SourceMatrixCandidateIdentity,
    captured_at: datetime,
    proof: EnvironmentProof,
    reason: str,
) -> AnalyticsSourceMatrixReceipt:
    contracts = source_contracts_by_id()
    source_receipts = tuple(
        _unavailable_source_receipt(
            contract,
            registered=False,
            reason=reason,
        )
        for contract in contracts.values()
    )
    return AnalyticsSourceMatrixReceipt(
        status="refused",
        reason=_safe_source_reason(reason),
        environment="SIM",
        source_contract_catalog_sha256=(candidate_identity.source_contract_catalog_sha256),
        harness_build_sha256=candidate_identity.harness_build_sha256,
        candidate_identity_sha256=candidate_identity.candidate_identity_sha256,
        captured_at=captured_at,
        environment_proof=proof,
        auth_status="not_called",
        session_status="not_called",
        entitlement_status="not_called",
        source_receipts=source_receipts,
        history_state="unverified",
        source_execution_claimed=False,
        cleanup=_empty_cleanup(),
        ledger=_empty_ledger(),
        privacy=PrivacyReceipt(findings=0, scan_errors=0),
        live_events=0,
        errors=(),
    )


def _readiness_refusal(  # noqa: PLR0913
    candidate_identity: SourceMatrixCandidateIdentity,
    captured_at: datetime,
    proof: EnvironmentProof,
    *,
    auth_status: str,
    session_status: str,
    entitlement_status: str,
    reason: str,
) -> AnalyticsSourceMatrixReceipt:
    source_receipts = tuple(
        _unavailable_source_receipt(
            contract,
            registered=False,
            reason=reason,
        )
        for contract in source_contracts_by_id().values()
    )
    return AnalyticsSourceMatrixReceipt(
        status="refused",
        reason=_safe_source_reason(reason),
        environment="SIM",
        source_contract_catalog_sha256=(candidate_identity.source_contract_catalog_sha256),
        harness_build_sha256=candidate_identity.harness_build_sha256,
        candidate_identity_sha256=candidate_identity.candidate_identity_sha256,
        captured_at=captured_at,
        environment_proof=proof,
        auth_status=auth_status,
        session_status=session_status,
        entitlement_status=entitlement_status,
        source_receipts=source_receipts,
        history_state="unverified",
        source_execution_claimed=False,
        cleanup=_empty_cleanup(),
        ledger=_empty_ledger(),
        privacy=PrivacyReceipt(findings=0, scan_errors=0),
        live_events=0,
        errors=(),
    )


def _empty_cleanup() -> CleanupReceipt:
    return CleanupReceipt(
        before_fingerprint=None,
        after_fingerprint=None,
        state_equal=False,
        complete=False,
    )


def _empty_ledger() -> LedgerReceipt:
    return LedgerReceipt(
        ledger_complete=False,
        events_evicted=0,
        negative_proof_available=False,
        request_count=0,
        non_get_request_count=0,
        methods=(),
        host_roles=(),
        gateway_environments=(),
        sim_only=False,
        live_events=0,
    )


def _safe_status(payload: Mapping[str, JsonValue]) -> str:
    status = payload.get("status")
    if isinstance(status, str) and _SAFE_SOURCE_REASON_PATTERN.fullmatch(status):
        return status
    return "failed"


def _auth_receipt_status(payload: Mapping[str, JsonValue]) -> str:
    if (
        payload.get("requested_environment") == "SIM"
        and payload.get("effective_read_environment") == "SIM"
        and payload.get("live_reads") is False
        and payload.get("live_writes") is False
        and payload.get("token_cache_present") is True
        and payload.get("token_cache_readable") is True
        and payload.get("token_cache_expired") is False
        and payload.get("token_cache_environment") == "SIM"
    ):
        return "ready"
    blocking_reasons = payload.get("blocking_reasons")
    if (
        payload.get("token_cache_present") is False
        or payload.get("token_cache_readable") is False
        or payload.get("token_cache_expired") is True
        or (
            isinstance(blocking_reasons, Sequence)
            and not isinstance(blocking_reasons, str)
            and bool(blocking_reasons)
        )
    ):
        return "auth_required"
    return "unavailable"


def _safe_source_reason(reason: str) -> str:
    if not reason:
        return ""
    return reason if _SAFE_SOURCE_REASON_PATTERN.fullmatch(reason) else "source_unavailable"


def _safe_fingerprint(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) else None


def _safe_http_status(value: JsonValue | None) -> int | None:
    return (
        value if isinstance(value, int) and _HTTP_STATUS_MIN <= value <= _HTTP_STATUS_MAX else None
    )


def _safe_count(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, allow_nan=False, default=str, separators=(",", ":"), sort_keys=True
        ).encode(),
    ).hexdigest()


def _enabled(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().casefold() not in {"", "0", "false", "off", "no"}


def source_matrix_candidate_identity() -> SourceMatrixCandidateIdentity:
    """Verify the installed immutable source catalog and harness manifest."""
    resource = files("saxo_bank_mcp").joinpath(
        _CANDIDATE_RESOURCE_DIR,
        _CANDIDATE_RESOURCE_NAME,
    )
    text = (
        resource.read_text(encoding="utf-8")
        if resource.is_file()
        else _CANDIDATE_SOURCE_PATH.read_text(encoding="utf-8")
    )
    manifest = _SourceMatrixCandidateManifest.model_validate_json(text, strict=True)
    actual_files: dict[str, str] = {}
    for name, expected_sha256 in sorted(manifest.harness_files.items()):
        if (
            re.fullmatch(r"[a-z][a-z0-9_]{0,127}\.py", name) is None
            or _SHA256_PATTERN.fullmatch(expected_sha256) is None
        ):
            raise ValueError("source matrix candidate manifest is invalid")
        source = files("saxo_bank_mcp").joinpath(name)
        if not source.is_file():
            raise ValueError("source matrix harness file is not installed")
        actual_files[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    catalog_sha256 = source_contract_catalog_sha256()
    harness_sha256 = _digest(actual_files)
    identity_sha256 = _digest(
        {
            "harness_build_sha256": harness_sha256,
            "source_contract_catalog_sha256": catalog_sha256,
        },
    )
    if (
        actual_files != dict(manifest.harness_files)
        or catalog_sha256 != manifest.source_contract_catalog_sha256
        or harness_sha256 != manifest.harness_build_sha256
        or identity_sha256 != manifest.candidate_identity_sha256
    ):
        raise ValueError("installed source matrix candidate identity mismatch")
    return SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256=catalog_sha256,
        harness_build_sha256=harness_sha256,
        candidate_identity_sha256=identity_sha256,
    )


def _require_sha256(value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("candidate identity must be lowercase SHA-256")


def _source_matrix_state_root(env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_STATE_HOME", "").strip()
    if not configured:
        return (Path.home() / ".local" / "state" / "saxo-bank-mcp").resolve(
            strict=False,
        )
    state_home = Path(configured).expanduser()
    if not state_home.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    return (state_home / "saxo-bank-mcp").resolve(strict=False)


def _prepare_owner_only_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("source matrix directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("source matrix directory is invalid")
    path.chmod(0o700)


def _claim_candidate_guard(
    path: Path,
    identity: SourceMatrixCandidateIdentity,
) -> bool:
    _prepare_owner_only_directory(path.parent)
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    **identity.model_dump(mode="json"),
                    "claimed": True,
                },
                sort_keys=True,
            )
            + "\n",
        )
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return True


def _write_immutable_evidence(
    path: Path,
    payload: Mapping[str, JsonValue],
) -> bool:
    text = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    findings, scan_errors = scan_secret_text(path.name, text)
    if findings or scan_errors:
        return _write_immutable_failure(path, "evidence_secret_scan_failed")
    return _write_immutable_text(path, text)


def _write_immutable_failure(path: Path, reason: str) -> bool:
    text = (
        json.dumps(
            {
                "reason": _safe_source_reason(reason),
                "status": "failed",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return _write_immutable_text(path, text)


def _write_immutable_text(path: Path, text: str) -> bool:
    _prepare_owner_only_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the installed one-shot Saxo SIM analytics source matrix.",
    )
    parser.add_argument("--instrument-uic", type=int, default=211)
    parser.add_argument("--asset-type", default="Stock")
    parser.add_argument("--option-root-id", type=int, default=120)
    arguments = parser.parse_args(argv)
    return execute_analytics_source_matrix_once(
        fixtures=SourceMatrixFixtures(
            account_key=os.environ.get("SAXO_MCP_QA_ACCOUNT_KEY", ""),
            client_key=os.environ.get("SAXO_MCP_QA_CLIENT_KEY", ""),
            instrument_uic=arguments.instrument_uic,
            asset_type=arguments.asset_type,
            option_root_id=arguments.option_root_id,
        ),
    )
