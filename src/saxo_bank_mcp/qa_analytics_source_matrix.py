from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast

import anyio
import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_pagination import PaginationCycleError
from saxo_bank_mcp.analytics_provider import (
    SaxoAnalyticsProvider,
    SourceAccessError,
    SourceEndpointError,
    SourceEntitlementError,
    SourceHttpError,
    SourcePayloadError,
    SourceProviderError,
    SourceRateLimitError,
    SourceSchemaDriftError,
    SourceTransportError,
)
from saxo_bank_mcp.analytics_source_contracts import (
    FrozenSourceJsonValue,
    SourceContract,
    SourceEnvelopeRowLocation,
    SourcePage,
    source_contracts_by_id,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation, find_registered_operation
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.secret_scan import scan_secret_text
from saxo_bank_mcp.server import mcp

ANALYTICS_SOURCE_CANDIDATE: Final = "8bb5b27eabba55d9f9af87ca0905690dfe8319b6"
DEFAULT_EVIDENCE_PATH: Final = Path(
    ".omo/evidence/saxo-bank-mcp/analytics-source-matrix/source-matrix.json",
)
_COMMIT_PATTERN: Final = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_TIMESTAMP_PATTERN: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
)
_SOURCE_COUNT: Final = 18
_REGISTRY_PAGE_SIZE: Final = 100
_HTTP_STATUS_MIN: Final = 100
_HTTP_STATUS_MAX: Final = 599
_STATE_PATHS: Final = (
    "/port/v1/orders/me",
    "/port/v1/positions/me",
    "/port/v1/balances/me",
)
_BALANCE_CONTRACT: Final = "balances_v1"
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


@dataclass(frozen=True, slots=True)
class SourceMatrixFixtures:
    account_key: str = ""
    client_key: str = ""
    instrument_uic: int = 211
    asset_type: str = "Stock"
    option_root_id: int = 120


class EnvironmentProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "refused"]
    selected_environment: Literal["SIM", "LIVE", "UNSET", "INVALID"]
    live_reads_enabled: bool
    live_writes_enabled: bool
    network_allowed: bool
    reasons: tuple[str, ...] = ()


class SchemaFieldReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    value_type: str


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
    response_visibility: Literal["redacted_body", "fingerprint_only", "unavailable"]
    response_fingerprint_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    request_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_fields: tuple[SchemaFieldReceipt, ...] = ()
    schema_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp_value_count: int = Field(ge=0)
    timestamp_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599)


class ControlledActivityReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_requested", "not_required", "refused"]
    reason: str
    history_absent: bool
    mutation_calls: Literal[0] = 0
    disclaimer_response_calls: Literal[0] = 0


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
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    captured_at: datetime
    environment_proof: EnvironmentProof
    auth_status: str
    session_status: str
    entitlement_status: str
    source_receipts: tuple[SourceContractReceipt, ...]
    controlled_activity: ControlledActivityReceipt
    cleanup: CleanupReceipt
    ledger: LedgerReceipt
    privacy: PrivacyReceipt
    live_events: int = Field(ge=0)
    live_mutation_calls: Literal[0] = 0
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TransportObservation:
    operation_id: str
    status: str
    network_call_made: bool
    response_visibility: str
    response_fingerprint: str | None
    http_status: int | None


class _McpRegisteredReadExecutor:
    def __init__(self, client: MatrixClient) -> None:
        self.client = client
        self.observations: list[_TransportObservation] = []

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        arguments: dict[str, JsonValue] = {
            "method": "GET",
            "path": request_target,
            "params": dict(params),
            "response_mode": "redacted_body",
        }
        payload = await _call_tool(self.client, "saxo_call_registered_endpoint", arguments)
        observation = _transport_observation(payload, operation)
        self.observations.append(observation)
        _require_sim_registered_read(payload, operation)
        status = observation.status
        if status == "auth_required":
            raise SourceAccessError(operation.operation_id, "auth_required")
        if status in {"denied", "live_not_called"}:
            raise SourceAccessError(operation.operation_id, "registered_read_refused")
        if status == "network_error":
            request = httpx2.Request("GET", "https://gateway.saxobank.com/openapi")
            raise httpx2.ReadError("registered_read_failed", request=request)
        http_status = observation.http_status
        if http_status is None:
            raise SourceAccessError(operation.operation_id, "registered_read_status_unavailable")
        body = payload.get("response")
        content = body.encode() if isinstance(body, str) else b""
        request = httpx2.Request("GET", "https://gateway.saxobank.com/openapi")
        return httpx2.Response(http_status, content=content, request=request)


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


async def run_analytics_source_matrix(  # noqa: C901, PLR0913, PLR0915
    server: FastMCP,
    *,
    env: Mapping[str, str],
    fixtures: SourceMatrixFixtures,
    candidate_commit: str,
    captured_at: datetime | None = None,
    allow_controlled_activity: bool = False,
) -> AnalyticsSourceMatrixReceipt:
    _require_candidate(candidate_commit)
    capture_time = captured_at or datetime.now(tz=UTC)
    if capture_time.tzinfo is None or capture_time.utcoffset() is None:
        raise ValueError("captured_at must include a UTC offset")
    proof = prove_sim_environment(env)
    if not proof.network_allowed:
        return _refused_without_calls(
            candidate_commit,
            capture_time,
            proof,
            proof.reasons[0] if proof.reasons else "environment_not_sim",
        )
    contracts = source_contracts_by_id()
    if len(contracts) != _SOURCE_COUNT:
        return _refused_without_calls(
            candidate_commit,
            capture_time,
            proof,
            "source_contract_count_mismatch",
        )
    errors: list[str] = []
    async with Client(server) as client:
        auth = await _call_tool(client, "saxo_auth_status", {})
        auth_status = _auth_receipt_status(auth)
        registry = await _registered_operation_receipts(client, contracts)
        await _call_tool(client, "saxo_get_safe_request_ledger", {"clear": True})
        session = await _call_tool(client, "saxo_get_session_capabilities", {})
        session_status = _safe_status(session)
        if session_status not in {"passed", "completed"}:
            source_receipts = tuple(
                _unavailable_source_receipt(
                    contract,
                    registered=registry.get(contract.operation_id) is True,
                    reason="sim_session_unavailable",
                )
                for contract in contracts.values()
            )
            ledger_payload = await _call_tool(client, "saxo_get_safe_request_ledger", {})
            ledger = _ledger_receipt(ledger_payload)
            receipt = AnalyticsSourceMatrixReceipt(
                status="refused",
                reason="sim_session_unavailable",
                environment="SIM",
                candidate_commit=candidate_commit,
                captured_at=capture_time,
                environment_proof=proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status="unverified",
                source_receipts=source_receipts,
                controlled_activity=_controlled_activity(
                    source_receipts,
                    allow_controlled_activity=allow_controlled_activity,
                ),
                cleanup=_empty_cleanup(),
                ledger=ledger,
                privacy=PrivacyReceipt(findings=0, scan_errors=0),
                live_events=ledger.live_events,
                errors=(),
            )
            return _with_privacy_scan(receipt, fixtures)

        entitlements = await _call_tool(client, "saxo_get_entitlements", {})
        entitlement_status = _safe_status(entitlements)
        before = await _state_fingerprint(client, registry)
        requests = _source_requests(fixtures, capture_time.date())
        executor = _McpRegisteredReadExecutor(client)
        provider = SaxoAnalyticsProvider(request_executor=executor, contracts=contracts)
        source_receipts_list: list[SourceContractReceipt] = []
        for contract in contracts.values():
            registered = registry.get(contract.operation_id) is True
            if not registered:
                errors.append(f"registered_operation_mismatch:{contract.contract_id}")
                source_receipts_list.append(
                    _unavailable_source_receipt(
                        contract,
                        registered=False,
                        reason="registered_operation_mismatch",
                    ),
                )
                continue
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
            if contract.contract_id == _BALANCE_CONTRACT:
                source_receipts_list.append(
                    await _run_fingerprint_only_source(
                        client,
                        contract,
                        request,
                    ),
                )
                continue
            source_receipts_list.append(
                await _run_provider_source(provider, executor, contract, request),
            )
        source_receipts = tuple(source_receipts_list)
        controlled_activity = _controlled_activity(
            source_receipts,
            allow_controlled_activity=allow_controlled_activity,
        )
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
            candidate_commit=candidate_commit,
            captured_at=capture_time,
            environment_proof=proof,
            auth_status=auth_status,
            session_status=session_status,
            entitlement_status=entitlement_status,
            source_receipts=source_receipts,
            controlled_activity=controlled_activity,
            cleanup=cleanup,
            ledger=ledger,
            privacy=PrivacyReceipt(findings=0, scan_errors=0),
            live_events=ledger.live_events,
            errors=tuple(errors),
        )
    return _with_privacy_scan(receipt, fixtures)


def execute_analytics_source_matrix_once(  # noqa: PLR0913
    *,
    out: Path,
    server: FastMCP = mcp,
    env: Mapping[str, str] | None = None,
    fixtures: SourceMatrixFixtures,
    candidate_commit: str = ANALYTICS_SOURCE_CANDIDATE,
    current_commit: str | None = None,
    captured_at: datetime | None = None,
    allow_controlled_activity: bool = False,
) -> int:
    _prepare_owner_only_directory(out.parent)
    try:
        _require_candidate(candidate_commit)
    except ValueError:
        _write_failure(out, "candidate_commit_invalid")
        return 1
    resolved_commit = _current_commit() if current_commit is None else current_commit
    if resolved_commit != candidate_commit:
        _write_failure(out, "candidate_commit_mismatch")
        return 1
    selected_env = dict(os.environ) if env is None else env
    proof = prove_sim_environment(selected_env)
    if not proof.network_allowed:
        reason = proof.reasons[0] if proof.reasons else "environment_not_sim"
        _write_failure(out, reason)
        return 1
    guard = candidate_guard_path(out, candidate_commit)
    if not _claim_candidate_guard(guard, candidate_commit):
        return 1

    async def run() -> AnalyticsSourceMatrixReceipt:
        return await run_analytics_source_matrix(
            server,
            env=selected_env,
            fixtures=fixtures,
            candidate_commit=candidate_commit,
            captured_at=captured_at,
            allow_controlled_activity=allow_controlled_activity,
        )

    try:
        receipt = anyio.run(run)
    except Exception:  # noqa: BLE001 - one-shot boundary must freeze a value-free failure
        _write_failure(out, "matrix_execution_failed")
        return 1
    payload = cast("dict[str, JsonValue]", receipt.model_dump(mode="json"))
    ok = write_scanned_json(out, payload)
    if out.exists():
        out.chmod(0o600)
    return 0 if ok and receipt.status in {"passed", "reduced", "refused"} else 1


def candidate_guard_path(out: Path, candidate_commit: str) -> Path:
    _require_candidate(candidate_commit)
    return out.with_name(f".{out.stem}.{candidate_commit[:12]}.claimed")


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


async def _run_provider_source(  # noqa: C901, PLR0915
    provider: SaxoAnalyticsProvider,
    executor: _McpRegisteredReadExecutor,
    contract: SourceContract,
    request: Mapping[str, object],
) -> SourceContractReceipt:
    start = len(executor.observations)
    pages: list[SourcePage] = []
    reason = ""
    source_status: SourceStatus = "observed"
    entitlement_state: Literal["observed", "denied", "unverified", "not_required"] = "observed"
    pagination_state: PaginationState = (
        "completed" if contract.pagination is not None else "not_applicable"
    )
    http_status: int | None = None
    try:
        async for page in provider.fetch(contract.contract_id, request):
            pages.append(page)  # noqa: PERF401 - retain partial pages on failure
    except SourceEntitlementError as error:
        source_status = "reduced"
        reason = "source_entitlement_unavailable"
        entitlement_state = "denied"
        pagination_state = "reduced" if contract.pagination is not None else "not_applicable"
        http_status = error.http_status
    except PaginationCycleError:
        source_status = "reduced"
        reason = "pagination_cycle_detected"
        pagination_state = "cycle_refused"
        entitlement_state = "unverified"
    except SourceEndpointError as error:
        source_status = "reduced"
        reason = _safe_source_reason(error.code)
        pagination_state = (
            "cycle_refused" if error.code == "source_pagination_cursor_invalid" else "reduced"
        )
        entitlement_state = "unverified"
    except SourceSchemaDriftError:
        source_status = "reduced"
        reason = "source_schema_drift"
        pagination_state = "reduced" if contract.pagination is not None else "not_applicable"
        entitlement_state = "unverified"
    except SourceAccessError:
        source_status = "refused"
        reason = "source_access_unavailable"
        pagination_state = "refused" if contract.pagination is not None else "not_applicable"
        entitlement_state = "unverified"
    except SourceRateLimitError as error:
        source_status = "reduced"
        reason = "source_rate_limited"
        pagination_state = "reduced" if contract.pagination is not None else "not_applicable"
        entitlement_state = "unverified"
        http_status = 429
        del error
    except SourceHttpError as error:
        source_status = "reduced"
        reason = "source_http_error"
        pagination_state = "reduced" if contract.pagination is not None else "not_applicable"
        entitlement_state = "unverified"
        http_status = error.http_status
    except (SourcePayloadError, SourceTransportError, SourceProviderError) as error:
        source_status = "reduced"
        reason = _safe_source_reason(error.code)
        pagination_state = "reduced" if contract.pagination is not None else "not_applicable"
        entitlement_state = "unverified"
    except (RuntimeError, ValueError):
        source_status = "refused"
        reason = "registered_read_protocol_invalid"
        pagination_state = "refused" if contract.pagination is not None else "not_applicable"
        entitlement_state = "unverified"
    observations = executor.observations[start:]
    if http_status is None and observations:
        http_status = observations[-1].http_status
    return _source_receipt(
        contract=contract,
        request=request,
        pages=pages,
        observations=observations,
        source_status=source_status,
        reason=reason,
        pagination_state=pagination_state,
        entitlement_state=entitlement_state,
        registered=True,
        http_status=http_status,
    )


async def _run_fingerprint_only_source(
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
            "response_mode": "fingerprint_only",
        },
    )
    operation = find_registered_operation("GET", path)
    valid = operation is not None
    if valid:
        _require_sim_registered_read(payload, operation)
    status = _safe_status(payload)
    response_fingerprint = _safe_fingerprint(payload.get("response_fingerprint"))
    passed = status == "passed" and response_fingerprint is not None
    return SourceContractReceipt(
        contract_id=contract.contract_id,
        operation_id=contract.operation_id,
        path_template=contract.path_template,
        source_status="reduced" if passed else "refused",
        outcome_reason=(
            "fingerprint_only_schema_unavailable"
            if passed
            else "fingerprint_only_source_unavailable"
        ),
        registered_operation_matched=valid,
        mcp_call_observed=True,
        page_count=1 if passed else 0,
        row_count=0,
        continuation_call_count=0,
        network_call_count=int(payload.get("network_call_made") is True),
        pagination_state="not_applicable",
        entitlement_state="observed" if passed else "unverified",
        response_visibility="fingerprint_only",
        response_fingerprint_sha256=response_fingerprint,
        request_fingerprint_sha256=_request_fingerprint(contract, request),
        schema_fingerprint_sha256=_digest([]),
        timestamp_value_count=0,
        timestamp_fingerprint_sha256=_digest([]),
        http_status=_safe_http_status(payload.get("http_status")),
    )


def _source_receipt(  # noqa: PLR0913
    *,
    contract: SourceContract,
    request: Mapping[str, object],
    pages: Sequence[SourcePage],
    observations: Sequence[_TransportObservation],
    source_status: SourceStatus,
    reason: str,
    pagination_state: PaginationState,
    entitlement_state: Literal["observed", "denied", "unverified", "not_required"],
    registered: bool,
    http_status: int | None,
) -> SourceContractReceipt:
    schema_fields = _schema_receipts(contract, pages)
    timestamp_values = _timestamp_values(pages)
    response_fingerprints = [
        item.response_fingerprint for item in observations if item.response_fingerprint is not None
    ]
    response_fingerprint = (
        response_fingerprints[0]
        if len(response_fingerprints) == 1
        else _digest(response_fingerprints)
        if response_fingerprints
        else None
    )
    visibility = _response_visibility(observations)
    return SourceContractReceipt(
        contract_id=contract.contract_id,
        operation_id=contract.operation_id,
        path_template=contract.path_template,
        source_status=source_status,
        outcome_reason=_safe_source_reason(reason),
        registered_operation_matched=registered,
        mcp_call_observed=bool(observations),
        page_count=len(pages),
        row_count=sum(page.row_count for page in pages),
        continuation_call_count=max(0, len(observations) - 1),
        network_call_count=sum(item.network_call_made for item in observations),
        pagination_state=pagination_state,
        entitlement_state=entitlement_state,
        response_visibility=visibility,
        response_fingerprint_sha256=response_fingerprint,
        request_fingerprint_sha256=_request_fingerprint(contract, request),
        schema_fields=schema_fields,
        schema_fingerprint_sha256=_digest(
            [item.model_dump(mode="json") for item in schema_fields],
        ),
        timestamp_value_count=len(timestamp_values),
        timestamp_fingerprint_sha256=_digest(timestamp_values),
        http_status=http_status,
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
    non_get = sum(item.get("method") != "GET" for item in attempted)
    unsafe_host = sum(item.get("host_role") != "gateway" for item in attempted)
    ledger_complete = payload.get("ledger_complete") is True
    negative = payload.get("negative_proof_available") is True
    evicted = payload.get("events_evicted")
    events_evicted = evicted if isinstance(evicted, int) and evicted >= 0 else 0
    declared_non_get = payload.get("non_get_request_count")
    non_get_count = (
        declared_non_get if isinstance(declared_non_get, int) and declared_non_get >= 0 else non_get
    )
    unsafe_declared = (
        payload.get("unsafe_gateway_request_detected") is True
        or payload.get("order_placement_endpoint_called") is True
    )
    live_events = unsafe_host
    sim_only = (
        ledger_complete
        and negative
        and events_evicted == 0
        and non_get_count == 0
        and non_get == 0
        and unsafe_host == 0
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
        non_get_request_count=max(non_get_count, non_get),
        methods=methods,
        host_roles=host_roles,
        sim_only=sim_only,
        live_events=live_events,
    )


def _controlled_activity(
    receipts: Sequence[SourceContractReceipt],
    *,
    allow_controlled_activity: bool,
) -> ControlledActivityReceipt:
    histories = [item for item in receipts if item.contract_id in _HISTORY_CONTRACTS]
    history_absent = bool(histories) and all(item.row_count == 0 for item in histories)
    if not history_absent:
        return ControlledActivityReceipt(
            status="not_required",
            reason="history_observed",
            history_absent=False,
        )
    if not allow_controlled_activity:
        return ControlledActivityReceipt(
            status="not_requested",
            reason="controlled_activity_disabled",
            history_absent=True,
        )
    return ControlledActivityReceipt(
        status="refused",
        reason="exact_unchanged_state_proof_not_guaranteed",
        history_absent=True,
    )


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


def _schema_receipts(
    contract: SourceContract,
    pages: Sequence[SourcePage],
) -> tuple[SchemaFieldReceipt, ...]:
    fields: set[tuple[str, str]] = set()
    for source_field in contract.response_envelope.structural_fields:
        fields.add((source_field.name, source_field.value_type.value))
    row_path = (
        "Data[]"
        if contract.response_envelope.row_location is SourceEnvelopeRowLocation.DATA
        else ""
    )
    for page in pages:
        for row in page.rows:
            _collect_schema(row, row_path, fields)
    return tuple(
        SchemaFieldReceipt(path=path, value_type=value_type)
        for path, value_type in sorted(fields)
        if path
    )


def _collect_schema(
    value: FrozenSourceJsonValue,
    path: str,
    fields: set[tuple[str, str]],
) -> None:
    if isinstance(value, Mapping):
        if path:
            fields.add((path, "object"))
        for key in sorted(value):
            child_path = f"{path}.{key}" if path else key
            _collect_schema(value[key], child_path, fields)
        return
    if isinstance(value, tuple):
        if path:
            fields.add((path, "array"))
            item_path = f"{path}[]"
            if not value:
                fields.add((item_path, "empty"))
            for child in value:
                _collect_schema(child, item_path, fields)
        return
    fields.add((path, _value_type(value)))


def _timestamp_values(pages: Sequence[SourcePage]) -> list[str]:
    values: list[str] = []
    for page in pages:
        for row in page.rows:
            _collect_timestamps(row, values)
    return sorted(values)


def _collect_timestamps(value: FrozenSourceJsonValue, values: list[str]) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _collect_timestamps(child, values)
        return
    if isinstance(value, tuple):
        for child in value:
            _collect_timestamps(child, values)
        return
    if isinstance(value, str) and _TIMESTAMP_PATTERN.fullmatch(value):
        values.append(value)


def _value_type(value: FrozenSourceJsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str) and _TIMESTAMP_PATTERN.fullmatch(value):
        return "timestamp"
    return "string"


def _transport_observation(
    payload: Mapping[str, JsonValue],
    operation: EndpointOperation,
) -> _TransportObservation:
    return _TransportObservation(
        operation_id=operation.operation_id,
        status=_safe_status(payload),
        network_call_made=payload.get("network_call_made") is True,
        response_visibility=_safe_visibility(payload.get("response_visibility")),
        response_fingerprint=_safe_fingerprint(payload.get("response_fingerprint")),
        http_status=_safe_http_status(payload.get("http_status")),
    )


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


def _response_visibility(
    observations: Sequence[_TransportObservation],
) -> Literal["redacted_body", "fingerprint_only", "unavailable"]:
    values = {item.response_visibility for item in observations}
    if "redacted_body" in values:
        return "redacted_body"
    if "fingerprint_only" in values:
        return "fingerprint_only"
    return "unavailable"


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
    candidate_commit: str,
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
        candidate_commit=candidate_commit,
        captured_at=captured_at,
        environment_proof=proof,
        auth_status="not_called",
        session_status="not_called",
        entitlement_status="not_called",
        source_receipts=source_receipts,
        controlled_activity=ControlledActivityReceipt(
            status="not_requested",
            reason="network_not_allowed",
            history_absent=True,
        ),
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
        ledger_complete=True,
        events_evicted=0,
        negative_proof_available=True,
        request_count=0,
        non_get_request_count=0,
        methods=(),
        host_roles=(),
        sim_only=True,
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


def _safe_visibility(value: JsonValue | None) -> str:
    return (
        value
        if isinstance(value, str) and value in {"redacted_body", "fingerprint_only"}
        else "unavailable"
    )


def _safe_fingerprint(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) else None


def _safe_http_status(value: JsonValue | None) -> int | None:
    return (
        value if isinstance(value, int) and _HTTP_STATUS_MIN <= value <= _HTTP_STATUS_MAX else None
    )


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


def _require_candidate(candidate_commit: str) -> None:
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise ValueError("candidate commit must be a full lowercase Git SHA")


def _current_commit() -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    result = subprocess.run(
        (git, "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _prepare_owner_only_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _claim_candidate_guard(path: Path, candidate_commit: str) -> bool:
    _prepare_owner_only_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "candidate_commit": candidate_commit,
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


def _write_failure(path: Path, reason: str) -> None:
    _prepare_owner_only_directory(path.parent)
    write_scanned_json(path, {"status": "failed", "reason": _safe_source_reason(reason)})
    path.chmod(0o600)
