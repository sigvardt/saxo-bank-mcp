from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import pwd
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

import anyio
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
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
_HTTP_STATUS_OK: Final = 200
_HTTP_STATUS_REDIRECT_MIN: Final = 300
_OWNER_DIRECTORY_MODE: Final = 0o700
_OWNER_FILE_MODE: Final = 0o600
_OWNER_COMMON_WRITE_MASK: Final = 0o022
_EXACT_DIRECTORY_COMPONENT_INDEX: Final = 2
_SELF_REFERENCE_EXCLUSION_COUNT: Final = 2
_ENTRYPOINT_TARGET_PART_COUNT: Final = 2
_INSTALLER_GENERATED_METADATA: Final = frozenset(
    {"INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"},
)
_EXPECTED_INSTALLER: Final = "uv"
_RUNTIME_ARTIFACT_SUFFIXES: Final = frozenset(
    {".py", ".pyi", ".so", ".dylib", ".dll", ".pyd"},
)
_SOURCE_RUNNERS: Final = (
    "scripts/generate_analytics_source_matrix_candidate.py",
    "scripts/run_analytics_source_matrix.py",
)
_SOURCE_RECEIPT_COMMON_FIELDS: Final = frozenset(
    {
        "status",
        "tool_name",
        "call_class",
        "operation_id",
        "service_group",
        "method",
        "path",
        "environment",
        "network_call_made",
        "network_call_count",
        "attempt_count",
        "initial_attempt_count",
        "continuation_attempt_count",
        "retry_count",
        "distinct_target_count",
        "successful_page_count",
        "live_write_called",
        "order_or_subscription_created",
        "arbitrary_url_allowed",
        "live_write",
        "live_access",
        "auth_exercised",
        "trading_ready",
        "http_status",
        "response",
        "response_visibility",
        "response_fingerprint",
        "response_fingerprint_scope",
        "analytics_contract_id",
        "analytics_contract_sha256",
        "page_count",
        "row_count",
        "continuation_call_count",
        "request_fingerprint_sha256",
    },
)
_SOURCE_SUCCESS_FIELDS: Final = _SOURCE_RECEIPT_COMMON_FIELDS | {
    "page_receipts",
    "source_revision_fingerprint_sha256",
    "timestamp_value_count",
    "timestamp_fingerprint_sha256",
}
_SOURCE_FAILURE_FIELDS: Final = _SOURCE_RECEIPT_COMMON_FIELDS | {"reason"}
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
_SOURCE_PLAN_UNAVAILABLE_SHA256: Final = hashlib.sha256(
    b"source-plan-unavailable",
).hexdigest()
_REDUCIBLE_SOURCE_FIXTURES: Final = {
    "bookings_v1": "fixture_client_key_unavailable",
    "closed_positions_history_v1": "fixture_client_key_unavailable",
    "costs_v1": "fixture_account_key_unavailable",
}
_LEDGER_EVENT_FIELDS: Final = frozenset(
    {
        "timestamp",
        "phase",
        "host_role",
        "method",
        "path",
        "query_names",
        "query_present",
        "status",
        "environment",
    },
)

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


class _RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    implementation: str
    cache_tag: str
    python_version: str
    platform: str
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _DependencyDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str
    files: Mapping[str, str]
    installer_metadata: Mapping[str, str]


class _SourceMatrixCandidateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["3"]
    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_files: Mapping[str, str]
    installed_files: Mapping[str, str]
    dependency_distributions: Mapping[str, _DependencyDistribution]
    runtime_identity: _RuntimeIdentity
    installed_metadata_projection: Mapping[str, str]
    console_scripts: Mapping[str, str]
    source_wheel_projection_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_exclusions: tuple[str, ...]
    installed_exclusions: tuple[str, ...]
    source_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    installed_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
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
    attempt_count: int = Field(ge=0)
    initial_attempt_count: int = Field(ge=0)
    continuation_attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    distinct_target_count: int = Field(ge=0)
    successful_page_count: int = Field(ge=0)
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
    source_revision_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp_value_count: int = Field(ge=0)
    timestamp_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    http_status: int | None = Field(default=None, ge=100, le=599)


class _SourcePageProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_number: int = Field(ge=1)
    row_count: int = Field(ge=0)
    page_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp_value_count: int = Field(ge=0)
    timestamp_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class _AuthStatusReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed"]
    tool_name: Literal["saxo_auth_status"]
    call_class: Literal["local_status_succeeded"]
    requested_environment: Literal["SIM", "LIVE"]
    effective_read_environment: Literal["SIM", "LIVE", "LIVE_READ_DISABLED"]
    live_reads: bool
    live_writes: Literal[False]
    network_call_made: Literal[False]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    sim_credentials_present: bool
    sim_credential_source: Literal["file", "env", "missing"]
    live_credentials_present: bool
    sim_redirect_uri_present: bool
    pending_pkce_authorization_present: bool
    token_cache_present: bool
    token_cache_readable: bool
    token_cache_expired: bool | None
    token_cache_refresh_supported: bool | None
    token_cache_environment: Literal["SIM", "LIVE"] | None
    scope_used: Literal[False]
    verifies: list[str]
    does_not_verify: list[str]
    blocking_reasons: list[str]
    next_action: str


class _SessionCapabilitiesReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed"]
    tool_name: Literal["saxo_get_session_capabilities"]
    call_class: Literal["sim_read_succeeded"]
    environment: Literal["SIM"]
    endpoint_path: Literal["/root/v1/sessions/capabilities"]
    token_refreshed: bool
    token: dict[str, JsonValue]
    token_refresh_supported: bool
    scope_used: Literal[False]
    network_call_made: Literal[True]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    capabilities: dict[str, JsonValue]
    next_action: str
    verifies: list[str]
    does_not_verify: list[str]


class _EntitlementSummaryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    exchange_count: int = Field(ge=0)
    max_rows: int = Field(ge=0)
    response_count: int = Field(ge=0)
    has_next_page: bool
    possibly_truncated: bool


class _EntitlementsReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed"]
    tool_name: Literal["saxo_get_entitlements"]
    call_class: Literal["sim_read_succeeded"]
    environment: Literal["SIM"]
    endpoint_path: Literal["/port/v1/users/me/entitlements"]
    entitlement_field_set: Literal["Default"]
    token_refreshed: bool
    network_call_made: Literal[True]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    entitlement_summary: _EntitlementSummaryReceipt
    exchange_ids: list[str]
    entitlement_bucket_counts: dict[str, int]
    verifies: list[str]
    does_not_verify: list[str]


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


class SourcePlanExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: str
    reason: str


class _SourceExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    included_contract_ids: tuple[str, ...]
    exclusions: tuple[SourcePlanExclusion, ...]
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AnalyticsSourceMatrixReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MatrixStatus
    reason: str
    environment: Literal["SIM"]
    source_contract_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    harness_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    captured_at: datetime
    source_plan_sha256: str = Field(
        default=_SOURCE_PLAN_UNAVAILABLE_SHA256,
        pattern=r"^[a-f0-9]{64}$",
    )
    source_plan_exclusions: tuple[SourcePlanExclusion, ...] = ()
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


async def run_analytics_source_matrix(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
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
    requests = _source_requests(fixtures, capture_time.date())
    try:
        source_plan = _source_execution_plan(
            contracts,
            requests,
            candidate_identity,
        )
    except ValueError:
        return _refused_without_calls(
            candidate_identity,
            capture_time,
            proof,
            "source_plan_invalid",
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
        session_status = _network_read_receipt_status(
            session,
            "saxo_get_session_capabilities",
        )
        if session_status != "passed":
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
        entitlement_status = _network_read_receipt_status(
            entitlements,
            "saxo_get_entitlements",
        )
        if entitlement_status != "passed":
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
        if not _ledger_clear_valid(cleared):
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
        source_receipts_list: list[SourceContractReceipt] = []
        exclusion_reasons = {
            exclusion.contract_id: exclusion.reason for exclusion in source_plan.exclusions
        }
        for contract in contracts.values():
            request = requests.get(contract.contract_id)
            if request is None:
                source_receipts_list.append(
                    _unavailable_source_receipt(
                        contract,
                        registered=True,
                        reason=exclusion_reasons[contract.contract_id],
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
            source_plan_sha256=source_plan.plan_sha256,
            source_plan_exclusions=source_plan.exclusions,
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
            _source_matrix_state_root() if state_root is None else state_root.absolute()
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
        claimed = (
            _claim_candidate_guard_at_owner(
                Path(pwd.getpwuid(os.getuid()).pw_dir),
                identity,
            )
            if state_root is None
            else _claim_candidate_guard(guard, identity)
        )
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
    root = state_root.absolute()
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
    status = _safe_status(payload)
    page_proofs = _source_page_proofs(payload.get("page_receipts"))
    expected_request_fingerprint = _request_fingerprint(contract, request)
    expected_fields: frozenset[str] = (
        _SOURCE_SUCCESS_FIELDS
        if status == "passed"
        else _SOURCE_FAILURE_FIELDS
        if status in {"http_error", "refused"}
        else frozenset[str]()
    )
    schema_valid = frozenset(payload) == expected_fields
    common_valid = (
        schema_valid
        and operation is not None
        and payload.get("tool_name") == "saxo_call_registered_endpoint"
        and payload.get("operation_id") == contract.operation_id
        and payload.get("service_group") == operation.service_group
        and payload.get("method") == "GET"
        and payload.get("path") == contract.path_template
        and payload.get("environment") == "SIM"
        and payload.get("live_write_called") is False
        and payload.get("order_or_subscription_created") is False
        and payload.get("arbitrary_url_allowed") is False
        and payload.get("live_write") is False
        and payload.get("live_access") is False
        and payload.get("auth_exercised") is (operation.auth_requirement != "none")
        and payload.get("trading_ready") is False
        and payload.get("analytics_contract_id") == contract.contract_id
        and payload.get("analytics_contract_sha256") == source_contract_fingerprint(contract)
        and payload.get("request_fingerprint_sha256") == expected_request_fingerprint
        and payload.get("response") is None
        and payload.get("response_visibility") == "analytics_contract_receipt"
        and payload.get("response_fingerprint_scope") == "analytics_contract_receipt"
    )
    proof_valid = (
        common_valid
        and status == "passed"
        and payload.get("call_class") == "sim_read_succeeded"
        and _source_success_proof_valid(
            payload,
            page_proofs,
            expected_request_fingerprint,
        )
    )
    failure_valid = (
        common_valid
        and status in {"http_error", "refused"}
        and payload.get("call_class")
        == ("sim_read_http_error" if status == "http_error" else "sim_read_refused")
        and _source_failure_proof_valid(payload)
    )
    raw_reason = payload.get("reason")
    reason = (
        ""
        if proof_valid
        else (
            _safe_source_reason(raw_reason)
            if failure_valid and isinstance(raw_reason, str)
            else "analytics_contract_receipt_invalid"
        )
    )
    source_status: SourceStatus = (
        "observed" if proof_valid else "reduced" if failure_valid else "refused"
    )
    page_count = _safe_count(payload.get("page_count")) if proof_valid else 0
    row_count = _safe_count(payload.get("row_count")) if proof_valid else 0
    continuation_count = _safe_count(payload.get("continuation_call_count")) if proof_valid else 0
    network_count = _safe_count(payload.get("network_call_count"))
    schema_hashes = (
        [page.schema_fingerprint_sha256 for page in page_proofs]
        if proof_valid and page_proofs is not None
        else []
    )
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
        attempt_count=_safe_count(payload.get("attempt_count")),
        initial_attempt_count=_safe_count(payload.get("initial_attempt_count")),
        continuation_attempt_count=_safe_count(
            payload.get("continuation_attempt_count"),
        ),
        retry_count=_safe_count(payload.get("retry_count")),
        distinct_target_count=_safe_count(payload.get("distinct_target_count")),
        successful_page_count=(
            _safe_count(payload.get("successful_page_count")) if proof_valid else 0
        ),
        pagination_state=(
            "completed"
            if proof_valid and contract.pagination is not None
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
            if proof_valid
            else "unverified"
        ),
        response_visibility=("analytics_contract_receipt" if common_valid else "unavailable"),
        response_fingerprint_sha256=_safe_fingerprint(
            payload.get("response_fingerprint"),
        ),
        request_fingerprint_sha256=expected_request_fingerprint,
        schema_fingerprint_sha256=_digest(schema_hashes),
        source_revision_fingerprint_sha256=(
            _safe_fingerprint(payload.get("source_revision_fingerprint_sha256")) or _digest([])
        ),
        timestamp_value_count=(
            _safe_count(payload.get("timestamp_value_count")) if proof_valid else 0
        ),
        timestamp_fingerprint_sha256=(
            _safe_fingerprint(payload.get("timestamp_fingerprint_sha256")) or _digest([])
        ),
        http_status=_safe_http_status(payload.get("http_status")),
    )


def _source_page_proofs(
    value: JsonValue | None,
) -> tuple[_SourcePageProof, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    try:
        return tuple(_SourcePageProof.model_validate(item, strict=True) for item in value)
    except ValidationError:
        return None


def _source_success_proof_valid(
    payload: Mapping[str, JsonValue],
    pages: tuple[_SourcePageProof, ...] | None,
    expected_request_fingerprint: str,
) -> bool:
    if pages is None or not pages:
        return False
    page_material = [page.model_dump(mode="json") for page in pages]
    declared_counts = _strict_counts(
        payload.get("page_count"),
        payload.get("row_count"),
        payload.get("network_call_count"),
        payload.get("attempt_count"),
        payload.get("initial_attempt_count"),
        payload.get("continuation_attempt_count"),
        payload.get("retry_count"),
        payload.get("distinct_target_count"),
        payload.get("successful_page_count"),
        payload.get("continuation_call_count"),
        payload.get("timestamp_value_count"),
    )
    if declared_counts is None:
        return False
    (
        page_count,
        row_count,
        network_count,
        attempt_count,
        initial_attempt_count,
        continuation_attempt_count,
        retry_count,
        distinct_target_count,
        successful_page_count,
        continuation_count,
        timestamp_count,
    ) = declared_counts
    return (
        [page.page_number for page in pages] == list(range(1, len(pages) + 1))
        and page_count == len(pages)
        and successful_page_count == len(pages)
        and row_count == sum(page.row_count for page in pages)
        and distinct_target_count == len(pages)
        and continuation_count == max(0, distinct_target_count - 1)
        and attempt_count == initial_attempt_count + continuation_attempt_count
        and network_count == attempt_count
        and initial_attempt_count >= 1
        and continuation_attempt_count >= continuation_count
        and retry_count == attempt_count - distinct_target_count
        and payload.get("network_call_made") is True
        and payload.get("request_fingerprint_sha256") == expected_request_fingerprint
        and payload.get("response_fingerprint") == _digest(page_material)
        and payload.get("source_revision_fingerprint_sha256")
        == _digest([page.source_revision_sha256 for page in pages])
        and timestamp_count == sum(page.timestamp_value_count for page in pages)
        and payload.get("timestamp_fingerprint_sha256")
        == _digest([page.timestamp_fingerprint_sha256 for page in pages])
        and payload.get("http_status") == _HTTP_STATUS_OK
    )


def _source_failure_proof_valid(payload: Mapping[str, JsonValue]) -> bool:
    declared_counts = _strict_counts(
        payload.get("attempt_count"),
        payload.get("network_call_count"),
        payload.get("initial_attempt_count"),
        payload.get("continuation_attempt_count"),
        payload.get("retry_count"),
        payload.get("distinct_target_count"),
        payload.get("continuation_call_count"),
    )
    if declared_counts is None:
        return False
    (
        attempt_count,
        network_count,
        initial_attempt_count,
        continuation_attempt_count,
        retry_count,
        distinct_target_count,
        continuation_count,
    ) = declared_counts
    return (
        attempt_count == initial_attempt_count + continuation_attempt_count
        and network_count <= attempt_count
        and retry_count == max(0, attempt_count - distinct_target_count)
        and continuation_count == max(0, distinct_target_count - 1)
        and initial_attempt_count >= (1 if distinct_target_count else 0)
        and continuation_attempt_count >= continuation_count
        and payload.get("network_call_made") is (network_count > 0)
        and _safe_count(payload.get("page_count")) == 0
        and _safe_count(payload.get("row_count")) == 0
        and _safe_count(payload.get("successful_page_count")) == 0
        and (
            (
                payload.get("status") == "http_error"
                and isinstance(payload.get("http_status"), int)
                and not isinstance(payload.get("http_status"), bool)
                and not _HTTP_STATUS_OK
                <= cast("int", payload.get("http_status"))
                < _HTTP_STATUS_REDIRECT_MIN
            )
            or (payload.get("status") == "refused" and payload.get("http_status") is None)
        )
        and payload.get("response_fingerprint") is None
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
        attempt_count=0,
        initial_attempt_count=0,
        continuation_attempt_count=0,
        retry_count=0,
        distinct_target_count=0,
        successful_page_count=0,
        pagination_state="refused" if contract.pagination is not None else "not_applicable",
        entitlement_state="unverified",
        response_visibility="unavailable",
        request_fingerprint_sha256=_digest(
            {"contract_id": contract.contract_id, "request": "unavailable"},
        ),
        schema_fingerprint_sha256=_digest([]),
        source_revision_fingerprint_sha256=_digest([]),
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
    all_events: list[Mapping[str, JsonValue]] = []
    event_list_valid = isinstance(events, Sequence) and not isinstance(events, str)
    if isinstance(events, Sequence) and not isinstance(events, str):
        all_events = [item for item in events if isinstance(item, Mapping)]
        event_list_valid = len(all_events) == len(events) and _ledger_events_valid(
            all_events,
        )
    attempted = [item for item in all_events if item.get("phase") == "attempted"]
    methods = tuple(
        sorted({str(item.get("method", "unavailable")) for item in attempted}),
    )
    host_roles = tuple(
        sorted({str(item.get("host_role", "unavailable")) for item in attempted}),
    )
    gateway_events = [item for item in attempted if item.get("host_role") == "gateway"]
    gateway_non_get = sum(item.get("method") != "GET" for item in gateway_events)
    all_non_get = sum(item.get("method") != "GET" for item in attempted)
    gateway_environments = tuple(
        sorted({str(item.get("environment", "UNKNOWN")) for item in gateway_events}),
    )
    ledger_complete = payload.get("ledger_complete") is True
    negative = payload.get("negative_proof_available") is True
    evicted = payload.get("events_evicted")
    events_evicted = evicted if isinstance(evicted, int) and evicted >= 0 else 0
    declared_request_count = _strict_count(payload.get("request_count"))
    declared_non_get = _strict_count(payload.get("non_get_request_count"))
    receipt_metadata_valid = (
        payload.get("status") == "passed"
        and payload.get("tool_name") == "saxo_get_safe_request_ledger"
        and payload.get("scope") == "current_mcp_session"
        and payload.get("safe_fields_only") is True
        and payload.get("unsafe_gateway_request_detected") is False
        and payload.get("order_placement_endpoint_called") is False
        and declared_request_count == len(attempted)
        and declared_non_get == all_non_get
    )
    live_events = sum(item.get("environment") == "LIVE" for item in gateway_events)
    unknown_gateway = sum(item.get("environment") not in {"SIM", "LIVE"} for item in gateway_events)
    sim_only = (
        ledger_complete
        and negative
        and events_evicted == 0
        and event_list_valid
        and receipt_metadata_valid
        and gateway_non_get == 0
        and live_events == 0
        and unknown_gateway == 0
    )
    return LedgerReceipt(
        ledger_complete=ledger_complete,
        events_evicted=events_evicted,
        negative_proof_available=negative,
        request_count=declared_request_count or 0,
        non_get_request_count=declared_non_get or 0,
        methods=methods,
        host_roles=host_roles,
        gateway_environments=gateway_environments,
        sim_only=sim_only,
        live_events=live_events,
    )


def _ledger_events_valid(events: Sequence[Mapping[str, JsonValue]]) -> bool:
    unmatched: list[tuple[object, ...]] = []
    for event in events:
        timestamp = event.get("timestamp")
        status = event.get("status")
        query_names = event.get("query_names")
        if (
            frozenset(event) != _LEDGER_EVENT_FIELDS
            or not isinstance(timestamp, str)
            or not _valid_iso_timestamp(timestamp)
            or event.get("phase") not in {"attempted", "completed"}
            or event.get("host_role") not in {"gateway", "oauth"}
            or event.get("environment") not in {"SIM", "LIVE"}
            or not isinstance(event.get("method"), str)
            or not bool(str(event.get("method")))
            or not isinstance(event.get("path"), str)
            or not str(event.get("path")).startswith("/")
            or not isinstance(query_names, Sequence)
            or isinstance(query_names, str)
            or any(not isinstance(name, str) for name in query_names)
            or not isinstance(event.get("query_present"), bool)
        ):
            return False
        binding = (
            event.get("host_role"),
            event.get("environment"),
            event.get("method"),
            event.get("path"),
            tuple(query_names),
            event.get("query_present"),
        )
        if event.get("phase") == "attempted":
            if status is not None:
                return False
            unmatched.append(binding)
        else:
            if (
                not isinstance(status, int)
                or isinstance(status, bool)
                or not _HTTP_STATUS_MIN <= status <= _HTTP_STATUS_MAX
                or binding not in unmatched
            ):
                return False
            unmatched.remove(binding)
    return True


def _valid_iso_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _ledger_clear_valid(payload: Mapping[str, JsonValue]) -> bool:
    return (
        payload.get("status") == "cleared"
        and payload.get("tool_name") == "saxo_get_safe_request_ledger"
        and payload.get("scope") == "current_mcp_session"
        and payload.get("safe_fields_only") is True
        and payload.get("ledger_complete") is True
        and payload.get("events_evicted") == 0
        and payload.get("negative_proof_available") is True
        and payload.get("request_count") == 0
        and payload.get("non_get_request_count") == 0
        and payload.get("unsafe_gateway_request_detected") is False
        and payload.get("order_placement_endpoint_called") is False
        and payload.get("events") == []
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


def _source_execution_plan(
    contracts: Mapping[str, SourceContract],
    requests: Mapping[str, Mapping[str, object] | None],
    candidate_identity: SourceMatrixCandidateIdentity,
) -> _SourceExecutionPlan:
    if set(requests) != set(contracts):
        raise ValueError("source execution plan does not cover the catalog")
    included = tuple(
        sorted(contract_id for contract_id, request in requests.items() if request is not None)
    )
    exclusions: list[SourcePlanExclusion] = []
    for contract_id, request in sorted(requests.items()):
        if request is not None:
            continue
        reason = _REDUCIBLE_SOURCE_FIXTURES.get(contract_id)
        if reason is None:
            raise ValueError("source execution plan has an undeclared exclusion")
        exclusions.append(
            SourcePlanExclusion(
                contract_id=contract_id,
                reason=reason,
            ),
        )
    material = {
        "candidate_identity_sha256": candidate_identity.candidate_identity_sha256,
        "exclusions": [exclusion.model_dump(mode="json") for exclusion in exclusions],
        "included_contract_ids": included,
        "source_contract_catalog_sha256": (candidate_identity.source_contract_catalog_sha256),
    }
    return _SourceExecutionPlan(
        included_contract_ids=included,
        exclusions=tuple(exclusions),
        plan_sha256=_digest(material),
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
    normalized = {
        key: (str(value) if key in contract.path_parameters else _render_query_value(value))
        for key, value in request.items()
        if key in set(contract.path_parameters) | set(contract.query_parameters)
    }
    return _digest(
        {
            "contract_id": contract.contract_id,
            "request": normalized,
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
    try:
        receipt = _AuthStatusReceipt.model_validate(payload, strict=True)
    except ValidationError:
        return "unavailable"
    if (
        receipt.requested_environment == "SIM"
        and receipt.effective_read_environment == "SIM"
        and receipt.live_reads is False
        and receipt.token_cache_present is True
        and receipt.token_cache_readable is True
        and receipt.token_cache_expired is False
        and receipt.token_cache_environment == "SIM"  # noqa: S105
        and receipt.blocking_reasons == []
    ):
        return "ready"
    if (
        receipt.token_cache_present is False
        or receipt.token_cache_readable is False
        or receipt.token_cache_expired is True
        or bool(receipt.blocking_reasons)
    ):
        return "auth_required"
    return "unavailable"


def _network_read_receipt_status(
    payload: Mapping[str, JsonValue],
    tool_name: str,
) -> str:
    if (
        payload.get("network_call_made") is not True
        or payload.get("live_write_called") is not False
        or payload.get("order_or_subscription_created") is not False
    ):
        return "unverified"
    model: type[BaseModel] | None = {
        "saxo_get_session_capabilities": _SessionCapabilitiesReceipt,
        "saxo_get_entitlements": _EntitlementsReceipt,
    }.get(tool_name)
    if model is None:
        return "unverified"
    try:
        model.model_validate(payload, strict=True)
    except ValidationError:
        return "unverified"
    else:
        return "passed"


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


def _strict_count(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _strict_counts(*values: JsonValue | None) -> tuple[int, ...] | None:
    counts = tuple(_strict_count(value) for value in values)
    if any(value is None for value in counts):
        return None
    return cast("tuple[int, ...]", counts)


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
    """Verify the complete source or installed distribution closure."""
    source_mode = _CANDIDATE_SOURCE_PATH.is_file()
    if source_mode:
        text = _CANDIDATE_SOURCE_PATH.read_text(encoding="utf-8")
    else:
        resource = files("saxo_bank_mcp").joinpath(
            _CANDIDATE_RESOURCE_DIR,
            _CANDIDATE_RESOURCE_NAME,
        )
        if not resource.is_file():
            raise ValueError("source matrix candidate manifest is unavailable")
        text = resource.read_text(encoding="utf-8")
    manifest = _SourceMatrixCandidateManifest.model_validate_json(text, strict=True)
    _validate_candidate_manifest_paths(manifest)
    actual_files = (
        _source_candidate_files(Path(__file__).resolve().parents[2])
        if source_mode
        else _installed_candidate_files(manifest.installed_exclusions)
    )
    expected_files = dict(manifest.source_files) if source_mode else dict(manifest.installed_files)
    if actual_files != expected_files:
        raise ValueError("source matrix candidate artifact closure mismatch")
    if _runtime_identity() != manifest.runtime_identity:
        raise ValueError("source matrix runtime identity mismatch")
    if _dependency_distributions() != dict(manifest.dependency_distributions):
        raise ValueError("source matrix dependency closure mismatch")
    if not source_mode:
        _validate_installed_execution_closure(manifest)
    catalog_sha256 = source_contract_catalog_sha256()
    source_build_sha256 = _digest(dict(manifest.source_files))
    installed_build_sha256 = _digest(dict(manifest.installed_files))
    harness_sha256 = _digest(
        {
            "console_scripts": dict(manifest.console_scripts),
            "dependency_distributions": {
                name: item.model_dump(mode="json")
                for name, item in manifest.dependency_distributions.items()
            },
            "installed_build_sha256": installed_build_sha256,
            "installed_exclusions": manifest.installed_exclusions,
            "installed_metadata_projection": dict(manifest.installed_metadata_projection),
            "runtime_identity": manifest.runtime_identity.model_dump(mode="json"),
            "schema_version": manifest.schema_version,
            "source_build_sha256": source_build_sha256,
            "source_exclusions": manifest.source_exclusions,
            "source_wheel_projection_sha256": manifest.source_wheel_projection_sha256,
        },
    )
    identity_sha256 = _digest(
        {
            "harness_build_sha256": harness_sha256,
            "source_contract_catalog_sha256": catalog_sha256,
        },
    )
    if (
        catalog_sha256 != manifest.source_contract_catalog_sha256
        or source_build_sha256 != manifest.source_build_sha256
        or installed_build_sha256 != manifest.installed_build_sha256
        or harness_sha256 != manifest.harness_build_sha256
        or identity_sha256 != manifest.candidate_identity_sha256
    ):
        raise ValueError("installed source matrix candidate identity mismatch")
    return SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256=catalog_sha256,
        harness_build_sha256=harness_sha256,
        candidate_identity_sha256=identity_sha256,
    )


def _validate_candidate_manifest_paths(
    manifest: _SourceMatrixCandidateManifest,
) -> None:
    expected_source_exclusions = ("data/analytics/source_matrix_candidate.json",)
    candidate_resource = "saxo_bank_mcp/_analytics_source_matrix/source_matrix_candidate.json"
    if (
        manifest.source_exclusions != expected_source_exclusions
        or len(manifest.installed_exclusions) != _SELF_REFERENCE_EXCLUSION_COUNT
        or candidate_resource not in manifest.installed_exclusions
        or not any(name.endswith(".dist-info/RECORD") for name in manifest.installed_exclusions)
    ):
        raise ValueError("source matrix candidate exclusions are invalid")
    for mapping in (manifest.source_files, manifest.installed_files):
        if not mapping or any(
            not _safe_artifact_path(name) or _SHA256_PATTERN.fullmatch(sha256) is None
            for name, sha256 in mapping.items()
        ):
            raise ValueError("source matrix candidate file map is invalid")
    if set(manifest.source_files) & set(manifest.source_exclusions):
        raise ValueError("source matrix source closure includes an exclusion")
    if set(manifest.installed_files) & set(manifest.installed_exclusions):
        raise ValueError("source matrix installed closure includes an exclusion")
    if (
        not manifest.dependency_distributions
        or any(
            canonicalize_name(name) != name or not item.version or not item.files
            for name, item in manifest.dependency_distributions.items()
        )
        or set(manifest.installed_metadata_projection) != {"INSTALLER"}
        or manifest.installed_metadata_projection["INSTALLER"]
        != hashlib.sha256(_EXPECTED_INSTALLER.encode()).hexdigest()
        or not manifest.console_scripts
    ):
        raise ValueError("source matrix installed execution projection is invalid")


def _source_candidate_files(repository_root: Path) -> dict[str, str]:
    source_package = repository_root / "src" / "saxo_bank_mcp"
    selected: list[Path] = [
        repository_root / "pyproject.toml",
        repository_root / "uv.lock",
        repository_root / "data" / "analytics" / "source_contracts.json",
        repository_root / "data" / "saxo" / "openapi_inventory.json",
        *(repository_root / runner for runner in _SOURCE_RUNNERS),
        *(repository_root / "data" / "analytics" / "migrations").rglob("*"),
        *source_package.rglob("*"),
    ]
    result: dict[str, str] = {}
    for path in sorted(set(selected)):
        if (
            not path.is_file()
            or path.is_symlink()
            or "__pycache__" in path.parts
            or path.suffix == ".pyc"
        ):
            continue
        relative = path.relative_to(repository_root).as_posix()
        if relative == "data/analytics/source_matrix_candidate.json":
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _runtime_identity() -> _RuntimeIdentity:
    executable = Path(sys.executable)
    if not executable.is_file():
        raise ValueError("source matrix interpreter is unavailable")
    cache_tag = sys.implementation.cache_tag
    if not cache_tag:
        raise ValueError("source matrix interpreter cache tag is unavailable")
    return _RuntimeIdentity(
        implementation=sys.implementation.name,
        cache_tag=cache_tag,
        python_version=platform.python_version(),
        platform=platform.platform(),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )


def _dependency_distributions(  # noqa: C901, PLR0912, PLR0915
) -> dict[str, _DependencyDistribution]:
    """Seal the recursively selected runtime dependency distributions."""
    try:
        root = distribution("saxo-bank-mcp")
    except PackageNotFoundError as error:
        raise ValueError("installed source matrix distribution is unavailable") from error
    selected: dict[str, set[str]] = {}
    pending: list[tuple[Distribution, set[str]]] = [(root, set())]
    processed: dict[str, set[str]] = {}
    while pending:
        current, extras = pending.pop()
        raw_name = current.metadata["Name"]
        current_name = canonicalize_name(raw_name)
        selected.setdefault(current_name, set()).update(extras)
        previous = processed.get(current_name)
        if previous is not None and extras <= previous:
            continue
        already = processed.setdefault(current_name, set())
        already.update(extras)
        for raw_requirement in current.requires or ():
            requirement = Requirement(raw_requirement)
            marker = requirement.marker
            if marker is None:
                marker_matches = True
            else:
                marker_matches = marker.evaluate({"extra": ""}) or any(
                    marker.evaluate({"extra": extra}) for extra in selected[current_name]
                )
            if not marker_matches:
                continue
            dependency_name = canonicalize_name(requirement.name)
            try:
                dependency = distribution(dependency_name)
            except PackageNotFoundError as error:
                raise ValueError(
                    f"source matrix dependency {dependency_name} is unavailable",
                ) from error
            new_extras = set(requirement.extras)
            known_extras = selected.setdefault(dependency_name, set())
            if dependency_name not in processed or not new_extras <= known_extras:
                known_extras.update(new_extras)
                pending.append((dependency, set(known_extras)))
    selected.pop("saxo-bank-mcp", None)
    result: dict[str, _DependencyDistribution] = {}
    for name in sorted(selected):
        dependency = distribution(name)
        dependency_files = dependency.files
        if dependency_files is None:
            raise ValueError(f"source matrix dependency {name} has no RECORD")
        file_map: dict[str, str] = {}
        installer_metadata: dict[str, str] = {}
        for package_path in dependency_files:
            relative = str(package_path).replace(os.sep, "/")
            path = Path(str(dependency.locate_file(package_path)))
            if ".." in Path(relative).parts:
                continue
            if ".dist-info/" in relative and Path(relative).name in _INSTALLER_GENERATED_METADATA:
                if Path(relative).name == "INSTALLER":
                    if not path.is_file() or path.is_symlink():
                        raise ValueError(f"source matrix dependency {name} installer is invalid")
                    normalized = path.read_text(encoding="utf-8").strip().casefold()
                    installer_metadata["INSTALLER"] = hashlib.sha256(
                        normalized.encode(),
                    ).hexdigest()
                continue
            if not _safe_artifact_path(relative) or not path.is_file() or path.is_symlink():
                raise ValueError(f"source matrix dependency {name} file is invalid")
            file_map[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not file_map or installer_metadata.get("INSTALLER") is None:
            raise ValueError(f"source matrix dependency {name} projection is incomplete")
        result[name] = _DependencyDistribution(
            version=dependency.version,
            files=dict(sorted(file_map.items())),
            installer_metadata=installer_metadata,
        )
    return result


def _root_installed_metadata_projection() -> dict[str, str]:
    installed_distribution = distribution("saxo-bank-mcp")
    distribution_files = installed_distribution.files or ()
    installer = next(
        (
            Path(str(installed_distribution.locate_file(item)))
            for item in distribution_files
            if str(item).replace(os.sep, "/").endswith(".dist-info/INSTALLER")
        ),
        None,
    )
    if installer is None or not installer.is_file() or installer.is_symlink():
        raise ValueError("installed source matrix installer projection is unavailable")
    normalized = installer.read_text(encoding="utf-8").strip().casefold()
    return {"INSTALLER": hashlib.sha256(normalized.encode()).hexdigest()}


def _validate_installed_execution_closure(
    manifest: _SourceMatrixCandidateManifest,
) -> None:
    if _root_installed_metadata_projection() != dict(
        manifest.installed_metadata_projection,
    ):
        raise ValueError("installed source matrix metadata projection mismatch")
    installed_distribution = distribution("saxo-bank-mcp")
    actual_console_scripts = {
        entry_point.name: entry_point.value
        for entry_point in installed_distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    if actual_console_scripts != dict(manifest.console_scripts):
        raise ValueError("installed source matrix entry point projection mismatch")
    distribution_files = installed_distribution.files or ()
    wrappers = {
        Path(str(installed_distribution.locate_file(item))).name: Path(
            str(installed_distribution.locate_file(item)),
        )
        for item in distribution_files
        if _installer_entrypoint_projection(str(item), actual_console_scripts)
    }
    if set(wrappers) != set(actual_console_scripts) or any(
        not _installer_entrypoint_projection_valid(
            wrappers[name],
            target,
            interpreter=Path(sys.executable),
        )
        for name, target in actual_console_scripts.items()
    ):
        raise ValueError("installed console entry point projection is invalid")
    _reject_unrecorded_or_shadowed_imports(
        installed_distribution,
        manifest.dependency_distributions,
    )


def _reject_unrecorded_or_shadowed_imports(  # noqa: C901, PLR0912
    installed_distribution: Distribution,
    dependencies: Mapping[str, _DependencyDistribution],
) -> None:
    distributions: dict[str, Distribution] = {
        "saxo-bank-mcp": installed_distribution,
        **{name: distribution(name) for name in dependencies},
    }
    recorded_paths: set[Path] = set()
    import_roots: dict[str, set[Path]] = {}
    for dependency in distributions.values():
        for item in dependency.files or ():
            relative = str(item).replace(os.sep, "/")
            if ".." in Path(relative).parts or ".dist-info/" in relative:
                continue
            located = Path(str(dependency.locate_file(item)))
            recorded_paths.add(located.resolve(strict=False))
            first = Path(relative).parts[0]
            module_name = first.removesuffix(".py")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_name) is None:
                continue
            import_roots.setdefault(module_name, set()).add(
                (
                    located
                    if len(Path(relative).parts) == 1
                    else located.parents[len(Path(relative).parts) - 2]
                ).resolve(strict=False),
            )
    scanned_roots: set[Path] = set()
    for roots in import_roots.values():
        for root in roots:
            if root in scanned_roots:
                continue
            scanned_roots.add(root)
            candidates = (root,) if root.is_file() else root.rglob("*")
            for path in candidates:
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.is_symlink() or path.resolve(strict=False) not in recorded_paths:
                    raise ValueError("unrecorded importable artifact is present")
    for module_name, roots in import_roots.items():
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            raise ValueError(f"recorded import root {module_name} is unavailable")
        origins: set[Path] = set()
        if isinstance(spec.origin, str) and spec.origin not in {"built-in", "frozen"}:
            origins.add(Path(spec.origin).resolve(strict=False))
        if spec.submodule_search_locations is not None:
            origins.update(
                Path(item).resolve(strict=False) for item in spec.submodule_search_locations
            )
        if origins and any(
            not any(origin == root or origin.is_relative_to(root) for root in roots)
            for origin in origins
        ):
            raise ValueError(f"import root {module_name} is shadowed")


def _installed_candidate_files(  # noqa: C901
    exclusions: Sequence[str],
) -> dict[str, str]:
    try:
        installed_distribution = distribution("saxo-bank-mcp")
    except PackageNotFoundError as error:
        raise ValueError("installed source matrix distribution is unavailable") from error
    distribution_files = installed_distribution.files
    if distribution_files is None:
        raise ValueError("installed source matrix RECORD is unavailable")
    excluded = set(exclusions)
    console_scripts = {
        entry_point.name: entry_point.value
        for entry_point in installed_distribution.entry_points
        if entry_point.group == "console_scripts"
    }
    observed_exclusions: set[str] = set()
    result: dict[str, str] = {}
    for package_path in distribution_files:
        name = str(package_path).replace(os.sep, "/")
        path = Path(str(installed_distribution.locate_file(package_path)))
        if _installer_entrypoint_projection(name, console_scripts):
            if not _installer_entrypoint_projection_valid(
                path,
                console_scripts[path.name],
            ):
                raise ValueError("installed console entry point projection is invalid")
            continue
        if not _safe_artifact_path(name):
            raise ValueError("installed source matrix path is invalid")
        if ".dist-info/" in name and Path(name).name in _INSTALLER_GENERATED_METADATA:
            continue
        if name in excluded:
            if not path.is_file():
                raise ValueError("installed source matrix exclusion is unavailable")
            observed_exclusions.add(name)
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError("installed source matrix file is unavailable")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed_exclusions != excluded:
        raise ValueError("installed source matrix exclusions are incomplete")
    return dict(sorted(result.items()))


def _installer_entrypoint_projection(
    value: str,
    console_scripts: Mapping[str, str],
) -> bool:
    path = Path(value)
    return path.name in console_scripts and "bin" in path.parts and value not in console_scripts


def _installer_entrypoint_projection_valid(
    path: Path,
    target: str,
    *,
    interpreter: Path | None = None,
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    target_parts = target.split(":")
    if (
        len(target_parts) != _ENTRYPOINT_TARGET_PART_COUNT
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", target_parts[0]) is None
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_parts[1]) is None
    ):
        return False
    try:
        first_line, body = path.read_text(encoding="utf-8").split("\n", 1)
    except (OSError, UnicodeError, ValueError):
        return False
    module, callable_name = target_parts
    expected_body = (
        "# -*- coding: utf-8 -*-\n"
        "import sys\n"
        f"from {module} import {callable_name}\n"
        'if __name__ == "__main__":\n'
        '    if sys.argv[0].endswith("-script.pyw"):\n'
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        '    elif sys.argv[0].endswith(".exe"):\n'
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        f"    sys.exit({callable_name}())\n"
    )
    expected_interpreter = Path(sys.executable) if interpreter is None else interpreter
    shebang_interpreter = Path(first_line.removeprefix("#!"))
    return (
        first_line.startswith("#!/")
        and shebang_interpreter.resolve(strict=True) == expected_interpreter.resolve(strict=True)
        and body == expected_body
    )


def _safe_artifact_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _require_sha256(value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("candidate identity must be lowercase SHA-256")


def _source_matrix_state_root(*, owner_home: Path | None = None) -> Path:
    selected_home = Path(pwd.getpwuid(os.getuid()).pw_dir) if owner_home is None else owner_home
    if not selected_home.is_absolute():
        raise ValueError("source matrix owner home must be absolute")
    return selected_home / ".local" / "state" / "saxo-bank-mcp"


def _validated_directory(
    descriptor: int,
    *,
    exact_mode: bool,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("source matrix directory type is invalid")
    if metadata.st_uid != os.getuid():
        raise ValueError("source matrix directory owner is invalid")
    mode = stat.S_IMODE(metadata.st_mode)
    if (exact_mode and mode != _OWNER_DIRECTORY_MODE) or (
        not exact_mode and mode & _OWNER_COMMON_WRITE_MASK
    ):
        raise ValueError("source matrix directory mode is invalid")
    return metadata


def _open_owner_directory(owner_home: Path) -> int:
    if not owner_home.is_absolute():
        raise ValueError("source matrix owner home must be absolute")
    with suppress(FileExistsError):
        owner_home.mkdir(mode=_OWNER_DIRECTORY_MODE)
    try:
        descriptor = os.open(
            owner_home,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ValueError("source matrix owner directory is invalid") from error
    try:
        _validated_directory(descriptor, exact_mode=False)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_or_create_directory_at(
    parent: int,
    name: str,
    *,
    exact_mode: bool,
) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, mode=_OWNER_DIRECTORY_MODE, dir_fd=parent)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
    except OSError as error:
        raise ValueError("source matrix directory component is invalid") from error
    try:
        _validated_directory(descriptor, exact_mode=exact_mode)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_guard_parent(
    owner_home: Path,
    candidate_identity_sha256: str,
) -> tuple[int, os.stat_result]:
    _require_sha256(candidate_identity_sha256)
    descriptor = _open_owner_directory(owner_home)
    try:
        for index, name in enumerate(
            (
                ".local",
                "state",
                "saxo-bank-mcp",
                "qa",
                "analytics-source-matrix",
                candidate_identity_sha256,
            ),
        ):
            child = _open_or_create_directory_at(
                descriptor,
                name,
                exact_mode=index >= _EXACT_DIRECTORY_COMPONENT_INDEX,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor, _validated_directory(descriptor, exact_mode=True)
    except Exception:
        os.close(descriptor)
        raise


def _claim_candidate_guard_at_owner(
    owner_home: Path,
    identity: SourceMatrixCandidateIdentity,
) -> bool:
    """Atomically claim the canonical owner guard through held directory FDs."""
    parent, parent_metadata = _open_guard_parent(
        owner_home,
        identity.candidate_identity_sha256,
    )
    created = False
    try:
        try:
            descriptor = os.open(
                "claimed.json",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                _OWNER_FILE_MODE,
                dir_fd=parent,
            )
            created = True
        except FileExistsError:
            existing = os.open(
                "claimed.json",
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                metadata = os.fstat(existing)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != _OWNER_FILE_MODE
                ):
                    raise ValueError("source matrix guard owner or mode is invalid")
            finally:
                os.close(existing)
            return False
        try:
            os.fchmod(descriptor, _OWNER_FILE_MODE)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != _OWNER_FILE_MODE
            ):
                raise ValueError("source matrix guard owner or mode is invalid")
            payload = (
                json.dumps(
                    {
                        **identity.model_dump(mode="json"),
                        "claimed": True,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise ValueError("source matrix guard write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
        current_parent, current_metadata = _open_guard_parent(
            owner_home,
            identity.candidate_identity_sha256,
        )
        try:
            if (
                current_metadata.st_dev != parent_metadata.st_dev
                or current_metadata.st_ino != parent_metadata.st_ino
            ):
                raise ValueError("source matrix guard parent changed during claim")
        finally:
            os.close(current_parent)
        return True  # noqa: TRY300
    except Exception:
        if created:
            try:
                os.unlink("claimed.json", dir_fd=parent)
                os.fsync(parent)
            except OSError:
                pass
        raise
    finally:
        os.close(parent)


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
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
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
