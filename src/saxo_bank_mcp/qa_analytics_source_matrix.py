from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Self, cast
from uuid import uuid4

import anyio
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from saxo_bank_mcp import analytics_source_runtime as _source_runtime
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    SourceContract,
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_source_process import (
    CHILD_TOOL_IDS_SHA256,
    SOURCE_MATRIX_CHILD_TOOLS,
    ChildConfigurationError,
    MatrixCallPolicy,
    MatrixSession,
    OneShotProcessSession,
    ProcessMatrixSession,
    ProcessSessionError,
    ProcessSessionFacts,
    RegisteredCallProfile,
    build_child_launch_config,
)
from saxo_bank_mcp.analytics_source_runtime import (
    CandidateRuntimeError,
    CandidateRuntimeSeal,
    ExternalRunLayout,
    SourceMatrixCandidateIdentity,
    close_candidate_runtime_seal,
    open_candidate_runtime_seal,
    prepare_child_run_paths,
    revalidate_candidate_runtime,
    source_matrix_candidate_identity,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation, find_registered_operation
from saxo_bank_mcp.secret_scan import scan_secret_text

_source_candidate_files = _source_runtime._source_candidate_files  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_validate_directory_entry_tree = _source_runtime._validate_directory_entry_tree  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_runtime_file_projection = _source_runtime._runtime_file_projection  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_runtime_tree_projection = _source_runtime._runtime_tree_projection  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_runtime_identity = _source_runtime._runtime_identity  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_dependency_distributions = _source_runtime._dependency_distributions  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_installed_candidate_files = _source_runtime._installed_candidate_files  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
_remove_empty_directories = _source_runtime._remove_empty_directories  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_SOURCE_COUNT: Final = 18
_CHILD_TOOL_COUNT: Final = 6
_SOURCE_CONTRACT_ORDER: Final[tuple[str, ...]] = (
    "chart_v3",
    "reference_instruments_v1",
    "reference_instrument_details_v1",
    "options_chain_reference_v1",
    "info_price_v1",
    "info_prices_list_v1",
    "performance_summary_v4",
    "performance_timeseries_v4",
    "balances_v1",
    "positions_v1",
    "orders_v1",
    "transactions_v1",
    "bookings_v1",
    "closed_positions_history_v1",
    "exposure_instruments_v1",
    "costs_v1",
    "corporate_action_events_v2",
    "corporate_action_holdings_v2",
)
_REGISTRY_PAGE_SIZE: Final = 100
_HTTP_STATUS_MIN: Final = 100
_HTTP_STATUS_MAX: Final = 599
_HTTP_STATUS_OK: Final = 200
_HTTP_FAILURE_STATUS_MIN: Final = 400
_HTTP_STATUS_FORBIDDEN: Final = 403
_HTTP_STATUS_RATE_LIMITED: Final = 429
_MAX_CAPABILITY_TEXT_LENGTH: Final = 80
_OWNER_DIRECTORY_MODE: Final = 0o700
_OWNER_FILE_MODE: Final = 0o600
_OWNER_COMMON_WRITE_MASK: Final = 0o022
_EXACT_DIRECTORY_COMPONENT_INDEX: Final = 2
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
_SAFE_EXCHANGE_ID_PATTERN: Final = re.compile(r"^[A-Z0-9._:-]{1,32}$")
_JSON_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
_REDUCIBLE_SOURCE_FIXTURES: Final = {
    "bookings_v1": "fixture_client_key_unavailable",
    "closed_positions_history_v1": "fixture_client_key_unavailable",
    "costs_v1": "fixture_account_key_unavailable",
}
_PRIVATE_CHILD_ENV_KEYS: Final = frozenset(
    {
        "SAXO_MCP_SIM_APP_KEY",
        "SAXO_MCP_SIM_CLIENT_ID",
        "SAXO_MCP_SIM_CREDENTIAL_FILE",
        "SAXO_MCP_SIM_REDIRECT_URI",
        "SAXO_MCP_SIM_AUTH_URL",
        "SAXO_MCP_SIM_TOKEN_URL",
        "SAXO_MCP_TOKEN_CACHE_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    },
)
_POST_NETWORK_ACCESS_REASON: Final = "source_access_unavailable_after_network"
_POST_NETWORK_REFUSAL_REASONS: Final = frozenset(
    {
        "invalid_source_data_envelope",
        "invalid_source_data_row",
        "invalid_source_payload",
        "pagination_cycle_detected",
        "pagination_page_limit_exceeded",
        "source_pagination_cursor_invalid",
        "source_pagination_drift",
        "source_pagination_path_changed",
        "source_pagination_query_changed",
        "source_revision_changed",
        "source_schema_drift",
        "source_stable_key_invalid",
        "source_transport_ambiguous",
    },
)
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

type SourceStatus = Literal["observed", "reduced", "refused"]
type MatrixStatus = Literal["passed", "reduced", "refused", "failed"]
type PreclaimReason = Literal[
    "environment_not_sim",
    "live_reads_enabled",
    "live_writes_enabled",
    "source_contract_count_mismatch",
    "source_plan_invalid",
    "candidate_static_runtime_invalid",
    "child_configuration_refused",
    "child_process_unavailable",
    "child_tool_allowlist_mismatch",
    "sim_auth_unavailable",
    "registered_operation_mismatch",
    "sim_session_unavailable",
    "sim_entitlements_unavailable",
    "request_ledger_unavailable",
    "candidate_already_claimed",
]
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


@dataclass(frozen=True, slots=True)
class _ClaimedGuardDirectory:
    descriptor: int
    device: int
    inode: int
    guard_device: int
    guard_inode: int
    guard_payload: bytes
    reopen: Callable[[], tuple[int, os.stat_result]]


@dataclass(frozen=True, slots=True)
class _PublishedEvidence:
    device: int
    inode: int


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


class _RedactedTokenStatusReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    has_access_token: Literal[True]
    has_refresh_token: bool
    has_code_verifier: bool
    environment: Literal["SIM"]
    expires_at: str
    is_expired: Literal[False]

    @field_validator("expires_at")
    @classmethod
    def validate_future_expiry(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("redacted token expiry must be an ISO timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("redacted token expiry must include a UTC offset")
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("redacted token expiry must be normalized to UTC")
        if parsed <= datetime.now(tz=UTC):
            raise ValueError("redacted token marked fresh must expire in the future")
        return value

    @model_validator(mode="after")
    def validate_refresh_pair(self) -> Self:
        if self.has_refresh_token != self.has_code_verifier:
            raise ValueError("redacted refresh material flags must agree")
        return self


class _SessionCapabilityFieldsReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    AuthenticationLevel: str | int | None
    DataLevel: str | int | None
    TradeLevel: str | int | None

    @field_validator("AuthenticationLevel", "DataLevel", "TradeLevel")
    @classmethod
    def validate_capability_scalar(cls, value: str | int | None) -> str | int | None:
        if isinstance(value, bool):
            raise TypeError("session capability booleans are invalid")
        if isinstance(value, str) and (
            not value.strip()
            or len(value) > _MAX_CAPABILITY_TEXT_LENGTH
            or re.fullmatch(r"[A-Za-z0-9 _./:+-]+", value) is None
        ):
            raise ValueError("session capability text is invalid")
        return value

    @model_validator(mode="after")
    def validate_read_capability(self) -> Self:
        denied = {"", "0", "denied", "false", "noaccess", "none", "null"}
        for value in (self.AuthenticationLevel, self.DataLevel):
            if value is None or (isinstance(value, int) and value <= 0):
                raise ValueError("session authentication and data capability must be present")
            if isinstance(value, str) and value.replace(" ", "").casefold() in denied:
                raise ValueError("session authentication and data capability must permit reads")
        return self


class _SessionCapabilitiesReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["passed"]
    tool_name: Literal["saxo_get_session_capabilities"]
    call_class: Literal["sim_read_succeeded"]
    environment: Literal["SIM"]
    endpoint_path: Literal["/root/v1/sessions/capabilities"]
    token_refreshed: bool
    token: _RedactedTokenStatusReceipt
    token_refresh_supported: bool
    scope_used: Literal[False]
    network_call_made: Literal[True]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    capabilities: _SessionCapabilityFieldsReceipt
    next_action: str
    verifies: list[str]
    does_not_verify: list[str]

    @model_validator(mode="after")
    def validate_session_receipt(self) -> Self:
        refresh_material = self.token.has_refresh_token and self.token.has_code_verifier
        if self.token_refresh_supported != refresh_material:
            raise ValueError("session refresh support contradicts redacted token status")
        if self.token_refreshed and not refresh_material:
            raise ValueError("a refreshed token must retain refresh material")
        if self.verifies != [
            "cached SIM bearer token can read current session capability fields",
        ]:
            raise ValueError("session verification claims are invalid")
        if self.does_not_verify != [
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live endpoint access",
        ]:
            raise ValueError("session limitation claims are invalid")
        if not self.next_action.strip():
            raise ValueError("session next action must be present")
        return self


class _EntitlementSummaryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    exchange_count: int = Field(ge=0)
    max_rows: int | None = Field(default=None, ge=0)
    response_count: int | None = Field(default=None, ge=0)
    has_next_page: bool
    possibly_truncated: bool


class _EntitlementBucketCountsReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    DelayedFullBook: int = Field(ge=0)
    DelayedGreeks: int = Field(ge=0)
    Greeks: int = Field(ge=0)
    RealTimeFullBook: int = Field(ge=0)
    RealTimeTopOfBook: int = Field(ge=0)


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
    entitlement_bucket_counts: _EntitlementBucketCountsReceipt
    verifies: list[str]
    does_not_verify: list[str]

    @model_validator(mode="after")
    def validate_entitlement_receipt(self) -> Self:
        summary = self.entitlement_summary
        if (
            len(self.exchange_ids) != summary.exchange_count
            or len(set(self.exchange_ids)) != len(self.exchange_ids)
            or any(_SAFE_EXCHANGE_ID_PATTERN.fullmatch(item) is None for item in self.exchange_ids)
        ):
            raise ValueError("entitlement exchange summary is inconsistent")
        if summary.response_count is not None and summary.response_count < summary.exchange_count:
            raise ValueError("entitlement response total is inconsistent")
        expected_truncated = (
            summary.has_next_page
            or (
                summary.response_count is not None
                and summary.exchange_count < summary.response_count
            )
            or (summary.max_rows is not None and summary.exchange_count >= summary.max_rows)
        )
        if summary.possibly_truncated is not expected_truncated:
            raise ValueError("entitlement truncation state is inconsistent")
        if self.verifies != [
            "cached SIM bearer token can read current market-data entitlement summary",
        ]:
            raise ValueError("entitlement verification claims are invalid")
        if self.does_not_verify != [
            "price availability for a specific instrument",
            "quote recency or real-time price delivery for any instrument",
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live endpoint access",
        ]:
            raise ValueError("entitlement limitation claims are invalid")
        return self


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


class ProcessBoundaryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transport: Literal["stdio"] = "stdio"
    child_process_distinct: Literal[True] = True
    process_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stdio_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    child_spawn_count: Literal[1] = 1
    mcp_session_count: Literal[1] = 1
    mcp_initialize_count: Literal[1] = 1
    tool_list_count: Literal[1] = 1
    tool_count: Literal[6] = 6
    tool_ids_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reconnect_count: Literal[0] = 0
    restart_count: Literal[0] = 0
    child_exit_code: Literal[0] = 0
    stdout_protocol_only: Literal[True] = True
    stderr_published: Literal[False] = False


type PostclaimFailureReason = Literal[
    "candidate_static_runtime_changed",
    "child_process_failed",
    "matrix_execution_failed",
    "evidence_secret_scan_failed",
]


class SourcePlanExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: str
    reason: str


class _SourceExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    included_contract_ids: tuple[str, ...]
    exclusions: tuple[SourcePlanExclusion, ...]
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PreclaimRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["refused"] = "refused"
    reason: PreclaimReason
    source_execution_claimed: Literal[False] = False


class ClaimedMatrixDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: MatrixStatus
    reason: str
    environment: Literal["SIM"] = "SIM"
    source_contract_catalog_sha256: str
    harness_build_sha256: str
    candidate_identity_sha256: str
    captured_at: datetime
    source_plan_sha256: str
    source_plan_exclusions: tuple[SourcePlanExclusion, ...]
    environment_proof: EnvironmentProof
    auth_status: str
    session_status: str
    entitlement_status: str
    source_receipts: tuple[SourceContractReceipt, ...]
    history_state: HistoryState
    source_execution_claimed: Literal[True] = True
    cleanup: CleanupReceipt
    ledger: LedgerReceipt
    live_events: int = Field(ge=0)
    live_mutation_calls: Literal[0] = 0
    errors: tuple[str, ...]


class AnalyticsSourceMatrixReceipt(ClaimedMatrixDraft):
    process_boundary: ProcessBoundaryReceipt
    privacy: PrivacyReceipt


type MatrixProtocolOutcome = PreclaimRefusal | ClaimedMatrixDraft


@dataclass(frozen=True, slots=True)
class MatrixExecutionError(RuntimeError):
    reason: Literal["structured_result_invalid", "matrix_receipt_invalid"]

    def __str__(self) -> str:  # noqa: D105
        return self.reason


@dataclass(frozen=True, slots=True)
class PreparedMatrix:
    candidate_identity: SourceMatrixCandidateIdentity
    captured_at: datetime
    environment_proof: EnvironmentProof
    contracts: tuple[SourceContract, ...]
    requests: Mapping[str, Mapping[str, object] | None] = field(repr=False)
    source_plan: _SourceExecutionPlan
    call_policy: MatrixCallPolicy = field(repr=False)


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


def prepare_analytics_source_matrix(  # noqa: PLR0911
    *,
    env: Mapping[str, str],
    fixtures: SourceMatrixFixtures,
    candidate_identity: SourceMatrixCandidateIdentity,
    captured_at: datetime | None = None,
) -> PreclaimRefusal | PreparedMatrix:
    capture_time = captured_at or datetime.now(tz=UTC)
    proof = prove_sim_environment(env)
    if not proof.network_allowed:
        reason = proof.reasons[0] if proof.reasons else "environment_not_sim"
        return PreclaimRefusal(reason=cast("PreclaimReason", reason))
    if capture_time.tzinfo is None or capture_time.utcoffset() is None:
        return PreclaimRefusal(reason="source_plan_invalid")
    try:
        contracts_by_id = source_contracts_by_id()
        catalog_sha256 = source_contract_catalog_sha256()
    except (OSError, ValidationError, ValueError):
        return PreclaimRefusal(reason="candidate_static_runtime_invalid")
    if len(contracts_by_id) != _SOURCE_COUNT:
        return PreclaimRefusal(reason="source_contract_count_mismatch")
    if tuple(contracts_by_id) != _SOURCE_CONTRACT_ORDER:
        return PreclaimRefusal(reason="source_plan_invalid")
    if candidate_identity.source_contract_catalog_sha256 != catalog_sha256:
        return PreclaimRefusal(reason="candidate_static_runtime_invalid")
    contracts = tuple(contracts_by_id[contract_id] for contract_id in _SOURCE_CONTRACT_ORDER)
    requests = _source_requests(fixtures, capture_time.date())
    try:
        source_plan = _source_execution_plan(
            contracts_by_id,
            requests,
            candidate_identity,
        )
        profiles = _registered_call_profiles(contracts, requests)
        call_policy = MatrixCallPolicy.from_local_registry(profiles)
    except (ChildConfigurationError, OSError, TypeError, ValueError):
        return PreclaimRefusal(reason="source_plan_invalid")
    return PreparedMatrix(
        candidate_identity=candidate_identity,
        captured_at=capture_time,
        environment_proof=proof,
        contracts=contracts,
        requests=requests,
        source_plan=source_plan,
        call_policy=call_policy,
    )


async def run_analytics_source_matrix(  # noqa: C901, PLR0911, PLR0912, PLR0915
    session: MatrixSession,
    *,
    prepared: PreparedMatrix,
    claim_source_execution: Callable[[], bool],
) -> MatrixProtocolOutcome:
    tool_names = await session.list_tools_once()
    if (
        len(tool_names) != len(SOURCE_MATRIX_CHILD_TOOLS)
        or len(set(tool_names)) != len(tool_names)
        or any(":" in name for name in tool_names)
        or set(tool_names) != set(SOURCE_MATRIX_CHILD_TOOLS)
        or _digest(tuple(sorted(tool_names))) != CHILD_TOOL_IDS_SHA256
    ):
        return PreclaimRefusal(reason="child_tool_allowlist_mismatch")

    policy = prepared.call_policy
    auth = await _call_tool(session, policy, "saxo_auth_status", {})
    auth_status = _auth_receipt_status(auth)
    if auth_status != "ready":
        return PreclaimRefusal(reason="sim_auth_unavailable")
    try:
        registry = await _registered_operation_receipts(
            session,
            policy,
            prepared.contracts,
        )
    except ChildConfigurationError:
        return PreclaimRefusal(reason="registered_operation_mismatch")
    expected_operation_ids = {contract.operation_id for contract in prepared.contracts}
    state_operations = [find_registered_operation("GET", path) for path in _STATE_PATHS]
    if any(operation is None for operation in state_operations):
        return PreclaimRefusal(reason="registered_operation_mismatch")
    expected_operation_ids.update(
        operation.operation_id for operation in state_operations if operation is not None
    )
    if set(registry) != expected_operation_ids or not all(registry.values()):
        return PreclaimRefusal(reason="registered_operation_mismatch")

    capabilities = await _call_tool(
        session,
        policy,
        "saxo_get_session_capabilities",
        {},
    )
    session_status = _network_read_receipt_status(
        capabilities,
        "saxo_get_session_capabilities",
    )
    if session_status != "passed":
        return PreclaimRefusal(reason="sim_session_unavailable")
    entitlements = await _call_tool(session, policy, "saxo_get_entitlements", {})
    entitlement_status = _network_read_receipt_status(
        entitlements,
        "saxo_get_entitlements",
    )
    if entitlement_status != "passed":
        return PreclaimRefusal(reason="sim_entitlements_unavailable")
    cleared = await _call_tool(
        session,
        policy,
        "saxo_get_safe_request_ledger",
        {"clear": True},
    )
    if not _ledger_clear_valid(cleared):
        return PreclaimRefusal(reason="request_ledger_unavailable")
    if not claim_source_execution():
        return PreclaimRefusal(reason="candidate_already_claimed")

    errors: list[str] = []
    before = await _state_fingerprint(session, policy, registry)
    if before is None:
        errors.append("state_fingerprint_unverified")
    exclusion_reasons = {
        exclusion.contract_id: exclusion.reason
        for exclusion in prepared.source_plan.exclusions
    }
    source_receipts_list: list[SourceContractReceipt] = []
    for contract in prepared.contracts:
        request = prepared.requests[contract.contract_id]
        if request is None:
            source_receipts_list.append(
                _unavailable_source_receipt(
                    contract,
                    registered=True,
                    reason=exclusion_reasons[contract.contract_id],
                ),
            )
        else:
            source_receipts_list.append(
                await _run_provider_source(session, policy, contract, request),
            )
    source_receipts = tuple(source_receipts_list)
    after = await _state_fingerprint(session, policy, registry)
    cleanup = CleanupReceipt(
        before_fingerprint=before,
        after_fingerprint=after,
        state_equal=before is not None and before == after,
        complete=before is not None and before == after,
    )
    if not cleanup.state_equal:
        errors.append("state_fingerprint_mismatch")
    ledger_payload = await _call_tool(
        session,
        policy,
        "saxo_get_safe_request_ledger",
        {},
    )
    ledger = _ledger_receipt(ledger_payload)
    if not ledger.sim_only:
        errors.append("unsafe_request_ledger")
    if ledger.live_events:
        errors.append("live_events_detected")
    status = _matrix_status(source_receipts, errors)
    reason = errors[0] if errors else _matrix_reason(source_receipts, status)
    identity = prepared.candidate_identity
    try:
        return ClaimedMatrixDraft(
            status=status,
            reason=reason,
            source_contract_catalog_sha256=identity.source_contract_catalog_sha256,
            harness_build_sha256=identity.harness_build_sha256,
            candidate_identity_sha256=identity.candidate_identity_sha256,
            captured_at=prepared.captured_at,
            source_plan_sha256=prepared.source_plan.plan_sha256,
            source_plan_exclusions=prepared.source_plan.exclusions,
            environment_proof=prepared.environment_proof,
            auth_status=auth_status,
            session_status=session_status,
            entitlement_status=entitlement_status,
            source_receipts=source_receipts,
            history_state=_history_state(source_receipts),
            cleanup=cleanup,
            ledger=ledger,
            live_events=ledger.live_events,
            errors=tuple(errors),
        )
    except ValidationError:
        raise MatrixExecutionError("matrix_receipt_invalid") from None


def _registered_call_profiles(
    contracts: tuple[SourceContract, ...],
    requests: Mapping[str, Mapping[str, object] | None],
) -> tuple[RegisteredCallProfile, ...]:
    profiles: list[RegisteredCallProfile] = []
    for path in _STATE_PATHS:
        if find_registered_operation("GET", path) is None:
            raise ValueError("state operation unavailable")
        profiles.append(
            RegisteredCallProfile(
                path=path,
                params={},
                response_mode="fingerprint_only",
                analytics_contract_id=None,
            ),
        )
    for contract in contracts:
        request = requests[contract.contract_id]
        if request is None:
            continue
        path = _resolved_contract_path(contract, request)
        operation = find_registered_operation("GET", path)
        if operation is None or operation.operation_id != contract.operation_id:
            raise ValueError("source operation unavailable")
        profiles.append(
            RegisteredCallProfile(
                path=path,
                params={
                    key: _render_query_value(value)
                    for key, value in request.items()
                    if key in contract.query_parameters
                },
                response_mode="analytics_contract_receipt",
                analytics_contract_id=contract.contract_id,
            ),
        )
    return tuple(profiles)


def execute_analytics_source_matrix_once(
    *,
    fixtures: SourceMatrixFixtures | None = None,
) -> int:
    """Run one installed, process-bound source matrix and publish immutable evidence."""
    try:
        return anyio.run(
            _execute_official_source_matrix,
            fixtures or SourceMatrixFixtures(),
        )
    except Exception:  # noqa: BLE001 - the official CLI emits no private traceback
        return 1


def process_boundary_receipt(
    facts: ProcessSessionFacts,
    candidate_identity: SourceMatrixCandidateIdentity,
) -> ProcessBoundaryReceipt:
    """Reduce successful private process facts to two irreversible identities."""
    exact_counts = all(
        type(value) is int and value == expected
        for value, expected in (
            (facts.child_spawn_count, 1),
            (facts.mcp_session_count, 1),
            (facts.mcp_initialize_count, 1),
            (facts.tool_list_count, 1),
            (facts.reconnect_count, 0),
            (facts.restart_count, 0),
        )
    )
    listed_names = facts.listed_tool_names
    if type(listed_names) is not tuple or any(type(name) is not str for name in listed_names):
        raise ValueError("process boundary facts are invalid")
    tool_ids_sha256 = _digest(tuple(sorted(listed_names)))
    pipe_values = (
        *facts.stdin_identity.canonical_private_material().values(),
        *facts.stdout_identity.canonical_private_material().values(),
    )
    if (
        type(facts.coordinator_pid) is not int
        or type(facts.child_pid) is not int
        or facts.coordinator_pid <= 0
        or facts.child_pid <= 0
        or facts.child_pid == facts.coordinator_pid
        or type(facts.executable_identity_sha256) is not str
        or _SHA256_PATTERN.fullmatch(facts.executable_identity_sha256) is None
        or facts.stdin_identity.endpoint_kind != "fifo"
        or facts.stdout_identity.endpoint_kind != "fifo"
        or any(type(value) is not int or value < 0 for value in pipe_values)
        or facts.stdin_identity == facts.stdout_identity
        or type(facts.stderr_byte_count) is not int
        or facts.stderr_byte_count < 0
        or not exact_counts
        or len(listed_names) != _CHILD_TOOL_COUNT
        or len(set(listed_names)) != _CHILD_TOOL_COUNT
        or set(listed_names) != set(SOURCE_MATRIX_CHILD_TOOLS)
        or tool_ids_sha256 != CHILD_TOOL_IDS_SHA256
        or type(facts.child_exit_code) is not int
        or facts.child_exit_code != 0
        or facts.stdout_protocol_only is not True
    ):
        raise ValueError("process boundary facts are invalid")
    return ProcessBoundaryReceipt(
        process_identity_sha256=_digest(
            {
                "candidate_identity_sha256": (
                    candidate_identity.candidate_identity_sha256
                ),
                "child_executable_identity_sha256": (
                    facts.executable_identity_sha256
                ),
                "child_pid": facts.child_pid,
                "coordinator_pid": facts.coordinator_pid,
            },
        ),
        stdio_identity_sha256=_digest(
            {
                "stdin": facts.stdin_identity.canonical_private_material(),
                "stdout": facts.stdout_identity.canonical_private_material(),
            },
        ),
        tool_ids_sha256=tool_ids_sha256,
    )


async def _execute_official_source_matrix(
    fixtures: SourceMatrixFixtures,
) -> int:
    seal: CandidateRuntimeSeal | None = None
    layout: ExternalRunLayout | None = None
    result = 1
    cleanup_ok = True
    try:
        seal, layout = open_candidate_runtime_seal()
        identity = source_matrix_candidate_identity()
        if identity != seal.identity:
            return 1
        env = dict(os.environ)
        captured_at = datetime.now(tz=UTC)
        prepared = prepare_analytics_source_matrix(
            env=env,
            fixtures=fixtures,
            candidate_identity=identity,
            captured_at=captured_at,
        )
        if isinstance(prepared, PreclaimRefusal):
            return 1
        child_paths = prepare_child_run_paths(seal, layout)
        config = build_child_launch_config(child_paths, env)
        session = OneShotProcessSession(config)
        result = await _execute_source_matrix_with_events(
            fixtures=fixtures,
            env=env,
            seal=seal,
            layout=layout,
            session=session,
            state_root=_source_matrix_state_root(),
            captured_at=captured_at,
            postexit_revalidate=lambda: revalidate_candidate_runtime(seal, layout),
        )
    except (CandidateRuntimeError, ChildConfigurationError, OSError, ValueError):
        result = 1
    finally:
        if seal is not None:
            close_candidate_runtime_seal(seal)
        if layout is not None:
            try:
                _remove_empty_directories(
                    (layout.child_cache, layout.child_work, layout.child_tmp),
                )
            except OSError:
                cleanup_ok = False
    return result if cleanup_ok else 1


async def _execute_source_matrix_with_events(  # noqa: C901, PLR0912, PLR0913, PLR0915
    *,
    fixtures: SourceMatrixFixtures,
    env: Mapping[str, str],
    seal: CandidateRuntimeSeal,
    layout: ExternalRunLayout,
    session: ProcessMatrixSession,
    state_root: Path,
    captured_at: datetime,
    postexit_revalidate: Callable[[], None],
) -> int:
    """Coordinate one finite child lifecycle around an already-open runtime seal."""
    prepared = prepare_analytics_source_matrix(
        env=env,
        fixtures=fixtures,
        candidate_identity=seal.identity,
        captured_at=captured_at,
    )
    if isinstance(prepared, PreclaimRefusal):
        return 1

    claimed: _ClaimedGuardDirectory | None = None
    draft: ClaimedMatrixDraft | None = None
    preclaim: PreclaimRefusal | None = None
    process_failure = False
    matrix_failure = False
    static_failure = False
    facts: ProcessSessionFacts | None = None
    spawn_attempted = False
    spawn_succeeded = False
    lifecycle_complete = False
    claim_attempted = False

    def claim_source_execution() -> bool:
        nonlocal claim_attempted, claimed
        if claim_attempted:
            return False
        claim_attempted = True
        claimed = _claim_candidate_guard_directory(
            lambda: _open_state_guard_parent(
                state_root,
                seal.identity.candidate_identity_sha256,
            ),
            seal.identity,
        )
        return claimed is not None

    try:
        try:
            spawn_attempted = True
            await session.spawn()
            spawn_succeeded = True
            await session.initialize()
            try:
                outcome = await run_analytics_source_matrix(
                    session,
                    prepared=prepared,
                    claim_source_execution=claim_source_execution,
                )
            except ProcessSessionError:
                process_failure = True
            except Exception:  # noqa: BLE001 - converted to one finite public reason
                matrix_failure = claimed is not None
                process_failure = claimed is None
            else:
                if isinstance(outcome, PreclaimRefusal):
                    preclaim = outcome
                else:
                    draft = outcome

            if not process_failure and not matrix_failure:
                lifecycle_complete = True
                try:
                    facts = await session.close()
                except ProcessSessionError:
                    process_failure = True
        except ProcessSessionError:
            process_failure = True
        except Exception:  # noqa: BLE001 - never publish exception text
            process_failure = True
        finally:
            if spawn_succeeded and not lifecycle_complete:
                try:
                    await session.abort()
                except ProcessSessionError:
                    process_failure = True
                except Exception:  # noqa: BLE001 - never publish exception text
                    process_failure = True
            if spawn_attempted:
                try:
                    postexit_revalidate()
                except Exception:  # noqa: BLE001 - exact finite static reason
                    static_failure = True

        if claimed is None:
            # The selected finite preclaim reason is intentionally not printed or stored.
            del preclaim
            return 1

        reason: PostclaimFailureReason | None = None
        if static_failure:
            reason = "candidate_static_runtime_changed"
        elif process_failure:
            reason = "child_process_failed"
        elif matrix_failure or draft is None:
            reason = "matrix_execution_failed"

        text: str | None = None
        if reason is None:
            if facts is None:
                reason = "child_process_failed"
            else:
                try:
                    process_receipt = process_boundary_receipt(facts, seal.identity)
                    receipt = AnalyticsSourceMatrixReceipt(
                        **draft.model_dump(),
                        process_boundary=process_receipt,
                        privacy=PrivacyReceipt(findings=0, scan_errors=0),
                    )
                    text = json.dumps(
                        receipt.model_dump(mode="json"),
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                except (TypeError, ValidationError, ValueError):
                    reason = "matrix_execution_failed"

        if reason is None and text is not None:
            if _published_receipt_retains_private_values(
                text,
                facts=cast("ProcessSessionFacts", facts),
                fixtures=fixtures,
                env=env,
                seal=seal,
                layout=layout,
            ):
                reason = "evidence_secret_scan_failed"
            else:
                try:
                    findings, scan_errors = scan_secret_text("source-matrix.json", text)
                except Exception:  # noqa: BLE001 - scanner failures are finite
                    reason = "evidence_secret_scan_failed"
                else:
                    if findings or scan_errors:
                        reason = "evidence_secret_scan_failed"

        if reason is not None:
            text = _postclaim_failure_text(reason)
        if text is None:
            return 1
        try:
            published = _publish_claimed_text(claimed, text)
        except (OSError, ValueError):
            return 1
        return 0 if published is not None and reason is None else 1
    finally:
        if claimed is not None:
            os.close(claimed.descriptor)


def _postclaim_failure_text(reason: PostclaimFailureReason) -> str:
    return json.dumps(
        {"status": "failed", "reason": reason},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _published_receipt_retains_private_values(  # noqa: PLR0913
    text: str,
    *,
    facts: ProcessSessionFacts,
    fixtures: SourceMatrixFixtures,
    env: Mapping[str, str],
    seal: CandidateRuntimeSeal,
    layout: ExternalRunLayout,
) -> bool:
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return True
    field_names = _recursive_field_names(document)
    if field_names & {"child_pid", "coordinator_pid", "descriptor", "inode", "stderr"}:
        return True
    raw_integer_values = {
        facts.child_pid,
        facts.coordinator_pid,
        *facts.stdin_identity.canonical_private_material().values(),
        *facts.stdout_identity.canonical_private_material().values(),
    }
    if raw_integer_values & _recursive_integer_values(document):
        return True
    private_strings = {
        fixtures.account_key,
        fixtures.client_key,
        os.fspath(seal.runtime_root),
        os.fspath(seal.executable),
        os.fspath(seal.site_packages),
        *(os.fspath(path) for path in (
            layout.root,
            layout.coordinator_cache,
            layout.coordinator_work,
            layout.coordinator_tmp,
            layout.child_cache,
            layout.child_work,
            layout.child_tmp,
        )),
    }
    private_strings.update(
        value
        for key, value in env.items()
        if value and key in _PRIVATE_CHILD_ENV_KEYS
    )
    return any(value and value in text for value in private_strings)


def _recursive_field_names(value: object) -> frozenset[str]:
    if isinstance(value, dict):
        result = set(cast("dict[object, object]", value))
        for child in cast("dict[object, object]", value).values():
            result.update(_recursive_field_names(child))
        return frozenset(item for item in result if isinstance(item, str))
    if isinstance(value, list):
        return frozenset(
            name
            for child in cast("list[object]", value)
            for name in _recursive_field_names(child)
        )
    return frozenset()


def _recursive_integer_values(value: object) -> frozenset[int]:
    if type(value) is int:
        return frozenset((value,))
    if isinstance(value, dict):
        return frozenset(
            item
            for child in cast("dict[object, object]", value).values()
            for item in _recursive_integer_values(child)
        )
    if isinstance(value, list):
        return frozenset(
            item
            for child in cast("list[object]", value)
            for item in _recursive_integer_values(child)
        )
    return frozenset()


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
    session: MatrixSession,
    policy: MatrixCallPolicy,
    name: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    policy.validate(name, arguments)
    payload = await session.call_tool(name, arguments)
    policy.observe(name, arguments, payload)
    try:
        return _JSON_OBJECT.validate_python(payload)
    except ValidationError:
        raise MatrixExecutionError("structured_result_invalid") from None


async def _registered_operation_receipts(  # noqa: C901
    session: MatrixSession,
    policy: MatrixCallPolicy,
    contracts: Sequence[SourceContract],
) -> dict[str, bool]:
    expected: dict[str, EndpointOperation] = {}
    for contract in contracts:
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
                session,
                policy,
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
    session: MatrixSession,
    policy: MatrixCallPolicy,
    contract: SourceContract,
    request: Mapping[str, object],
) -> SourceContractReceipt:
    path = _resolved_contract_path(contract, request)
    params = {
        key: _render_query_value(value)
        for key, value in request.items()
        if key in contract.query_parameters
    }
    arguments: dict[str, JsonValue] = {
        "method": "GET",
        "path": path,
        "response_mode": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
    }
    if params:
        arguments["params"] = params
    payload = await _call_tool(
        session,
        policy,
        "saxo_call_registered_endpoint",
        arguments,
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


def _source_failure_proof_valid(payload: Mapping[str, JsonValue]) -> bool:  # noqa: PLR0911
    declared_counts = _strict_counts(
        payload.get("attempt_count"),
        payload.get("network_call_count"),
        payload.get("initial_attempt_count"),
        payload.get("continuation_attempt_count"),
        payload.get("retry_count"),
        payload.get("distinct_target_count"),
        payload.get("continuation_call_count"),
        payload.get("page_count"),
        payload.get("row_count"),
        payload.get("successful_page_count"),
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
        page_count,
        row_count,
        successful_page_count,
    ) = declared_counts
    common_valid = (
        attempt_count == initial_attempt_count + continuation_attempt_count
        and attempt_count >= distinct_target_count
        and retry_count == attempt_count - distinct_target_count
        and continuation_count == distinct_target_count - 1
        and initial_attempt_count >= 1
        and continuation_attempt_count >= continuation_count
        and page_count == 0
        and row_count == 0
        and successful_page_count == 0
        and payload.get("response_fingerprint") is None
    )
    if not common_valid:
        return False
    status = payload.get("status")
    reason = payload.get("reason")
    http_status = payload.get("http_status")
    if status == "http_error":
        if (
            payload.get("call_class") != "sim_read_http_error"
            or payload.get("network_call_made") is not True
            or network_count != attempt_count
            or network_count < 1
            or attempt_count < 1
            or distinct_target_count < 1
            or not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or not _HTTP_FAILURE_STATUS_MIN <= http_status <= _HTTP_STATUS_MAX
        ):
            return False
        if reason == "source_rate_limited":
            return http_status == _HTTP_STATUS_RATE_LIMITED
        if reason == "source_entitlement_unavailable":
            return http_status == _HTTP_STATUS_FORBIDDEN
        return reason == "source_http_error" and http_status not in {
            401,
            _HTTP_STATUS_FORBIDDEN,
            _HTTP_STATUS_RATE_LIMITED,
        }
    if (
        status != "refused"
        or payload.get("call_class") != "sim_read_refused"
        or http_status is not None
    ):
        return False
    if reason == "source_access_unavailable":
        return (
            payload.get("network_call_made") is False
            and network_count == 0
            and attempt_count == 1
            and initial_attempt_count == 1
            and continuation_attempt_count == 0
            and retry_count == 0
            and distinct_target_count == 1
            and continuation_count == 0
        )
    if reason == _POST_NETWORK_ACCESS_REASON:
        return (
            payload.get("network_call_made") is True
            and 1 <= network_count <= attempt_count
            and attempt_count - network_count <= 1
            and distinct_target_count >= 1
        )
    return (
        reason in _POST_NETWORK_REFUSAL_REASONS
        and payload.get("network_call_made") is True
        and network_count == attempt_count
        and network_count >= 1
        and distinct_target_count >= 1
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
    session: MatrixSession,
    policy: MatrixCallPolicy,
    registry: Mapping[str, bool],
) -> str | None:
    fingerprints: dict[str, str] = {}
    for path in _STATE_PATHS:
        operation = find_registered_operation("GET", path)
        if operation is None or registry.get(operation.operation_id) is not True:
            return None
        arguments: dict[str, JsonValue] = {
            "method": "GET",
            "path": path,
            "response_mode": "fingerprint_only",
        }
        payload = await _call_tool(
            session,
            policy,
            "saxo_call_registered_endpoint",
            arguments,
        )
        try:
            _require_sim_registered_read(payload, operation)
        except RuntimeError:
            return None
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
        serialized = json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        findings, scan_errors = scan_secret_text("readiness-receipt.json", serialized)
    except (TypeError, ValueError, ValidationError):
        return "unverified"
    return "passed" if not findings and not scan_errors else "unverified"


def _safe_source_reason(reason: str) -> str:
    if not reason:
        return ""
    return reason if _SAFE_SOURCE_REASON_PATTERN.fullmatch(reason) else "source_unavailable"


def _safe_fingerprint(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) else None


def _safe_http_status(value: JsonValue | None) -> int | None:
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and _HTTP_STATUS_MIN <= value <= _HTTP_STATUS_MAX
        else None
    )


def _safe_count(value: JsonValue | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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


def _require_sha256(value: str) -> None:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("candidate identity must be lowercase SHA-256")


def _source_matrix_state_root(  # pyright: ignore[reportUnusedFunction]
    *,
    owner_home: Path | None = None,
) -> Path:
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


def _open_state_guard_parent(  # pyright: ignore[reportUnusedFunction]
    state_root: Path,
    candidate_identity_sha256: str,
) -> tuple[int, os.stat_result]:
    _require_sha256(candidate_identity_sha256)
    descriptor = _open_owner_directory(state_root)
    try:
        for name in ("qa", "analytics-source-matrix", candidate_identity_sha256):
            child = _open_or_create_directory_at(
                descriptor,
                name,
                exact_mode=True,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor, _validated_directory(descriptor, exact_mode=True)
    except Exception:
        os.close(descriptor)
        raise


def _guard_payload(identity: SourceMatrixCandidateIdentity) -> bytes:
    return (
        json.dumps(
            {
                **identity.model_dump(mode="json"),
                "claimed": True,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise ValueError("source matrix file write was incomplete")
        offset += written


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 8192):
        chunks.append(chunk)
    return b"".join(chunks)


def _validated_guard_file(
    parent: int,
    expected_payload: bytes,
) -> os.stat_result:
    descriptor = os.open(
        "claimed.json",
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != _OWNER_FILE_MODE
            or _read_all(descriptor) != expected_payload
        ):
            raise ValueError("source matrix guard content, owner, or mode is invalid")
        return metadata
    finally:
        os.close(descriptor)


def _validate_claimed_guard(claim: _ClaimedGuardDirectory) -> None:
    held_metadata = _validated_directory(claim.descriptor, exact_mode=True)
    if held_metadata.st_dev != claim.device or held_metadata.st_ino != claim.inode:
        raise ValueError("source matrix held guard parent changed")
    held_guard = _validated_guard_file(claim.descriptor, claim.guard_payload)
    if held_guard.st_dev != claim.guard_device or held_guard.st_ino != claim.guard_inode:
        raise ValueError("source matrix held guard changed")
    current_parent, current_metadata = claim.reopen()
    try:
        if current_metadata.st_dev != claim.device or current_metadata.st_ino != claim.inode:
            raise ValueError("source matrix canonical guard parent changed")
        current_guard = _validated_guard_file(current_parent, claim.guard_payload)
        if current_guard.st_dev != claim.guard_device or current_guard.st_ino != claim.guard_inode:
            raise ValueError("source matrix canonical guard changed")
    finally:
        os.close(current_parent)


def _claim_candidate_guard_directory(
    reopen: Callable[[], tuple[int, os.stat_result]],
    identity: SourceMatrixCandidateIdentity,
) -> _ClaimedGuardDirectory | None:
    parent, parent_metadata = reopen()
    expected_payload = _guard_payload(identity)
    created = False
    retained = False
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
            _validated_guard_file(parent, expected_payload)
            return None
        try:
            os.fchmod(descriptor, _OWNER_FILE_MODE)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != _OWNER_FILE_MODE
            ):
                raise ValueError("source matrix guard owner or mode is invalid")
            _write_all(descriptor, expected_payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
        guard_metadata = _validated_guard_file(parent, expected_payload)
        claim = _ClaimedGuardDirectory(
            descriptor=parent,
            device=parent_metadata.st_dev,
            inode=parent_metadata.st_ino,
            guard_device=guard_metadata.st_dev,
            guard_inode=guard_metadata.st_ino,
            guard_payload=expected_payload,
            reopen=reopen,
        )
        _validate_claimed_guard(claim)
        retained = True
        return claim  # noqa: TRY300
    except Exception:
        if created:
            try:
                os.unlink("claimed.json", dir_fd=parent)
                os.fsync(parent)
            except OSError:
                pass
        raise
    finally:
        if not retained:
            os.close(parent)


def _claim_candidate_guard_at_owner(  # pyright: ignore[reportUnusedFunction]
    owner_home: Path,
    identity: SourceMatrixCandidateIdentity,
) -> bool:
    """Atomically claim the canonical owner guard through held directory FDs."""
    claim = _claim_candidate_guard_directory(
        lambda: _open_guard_parent(
            owner_home,
            identity.candidate_identity_sha256,
        ),
        identity,
    )
    if claim is None:
        return False
    os.close(claim.descriptor)
    return True


def _unlink_published_evidence(
    claim: _ClaimedGuardDirectory,
    expected: _PublishedEvidence,
) -> None:
    try:
        descriptor = os.open(
            "source-matrix.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=claim.descriptor,
        )
    except FileNotFoundError:
        return
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if metadata.st_dev != expected.device or metadata.st_ino != expected.inode:
        return
    os.unlink("source-matrix.json", dir_fd=claim.descriptor)
    os.fsync(claim.descriptor)


def _publish_claimed_text(
    claim: _ClaimedGuardDirectory,
    text: str,
) -> _PublishedEvidence | None:
    _validate_claimed_guard(claim)
    temporary_name = f".source-matrix.json.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        _OWNER_FILE_MODE,
        dir_fd=claim.descriptor,
    )
    temporary_metadata = os.fstat(descriptor)
    final_created = False
    complete = False
    published: _PublishedEvidence | None = None
    try:
        os.fchmod(descriptor, _OWNER_FILE_MODE)
        temporary_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != os.getuid()
            or stat.S_IMODE(temporary_metadata.st_mode) != _OWNER_FILE_MODE
        ):
            raise ValueError("source matrix evidence owner or mode is invalid")
        _write_all(descriptor, text.encode())
        os.fsync(descriptor)
        _validate_claimed_guard(claim)
        try:
            os.link(
                temporary_name,
                "source-matrix.json",
                src_dir_fd=claim.descriptor,
                dst_dir_fd=claim.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return None
        final_created = True
        final_descriptor = os.open(
            "source-matrix.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=claim.descriptor,
        )
        try:
            final_metadata = os.fstat(final_descriptor)
            if (
                not stat.S_ISREG(final_metadata.st_mode)
                or final_metadata.st_uid != os.getuid()
                or stat.S_IMODE(final_metadata.st_mode) != _OWNER_FILE_MODE
                or final_metadata.st_dev != temporary_metadata.st_dev
                or final_metadata.st_ino != temporary_metadata.st_ino
            ):
                raise ValueError("source matrix evidence identity is invalid")
            os.fsync(final_descriptor)
        finally:
            os.close(final_descriptor)
        os.fsync(claim.descriptor)
        _validate_claimed_guard(claim)
        published = _PublishedEvidence(
            device=temporary_metadata.st_dev,
            inode=temporary_metadata.st_ino,
        )
        complete = True
        return published
    finally:
        os.close(descriptor)
        if final_created and not complete and published is None:
            _unlink_published_evidence(
                claim,
                _PublishedEvidence(
                    device=temporary_metadata.st_dev,
                    inode=temporary_metadata.st_ino,
                ),
            )
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=claim.descriptor)
        with suppress(OSError):
            os.fsync(claim.descriptor)


def _publish_claimed_failure(  # pyright: ignore[reportUnusedFunction]
    claim: _ClaimedGuardDirectory,
    reason: PostclaimFailureReason,
) -> _PublishedEvidence | None:
    return _publish_claimed_text(claim, _postclaim_failure_text(reason))


def _prepare_owner_only_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("source matrix directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("source matrix directory is invalid")
    path.chmod(0o700)


def _claim_candidate_guard(  # pyright: ignore[reportUnusedFunction]
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


def _verify_official_local_mode(
    fixtures: SourceMatrixFixtures,
    *,
    configure_child: bool,
) -> SourceMatrixCandidateIdentity | None:
    seal: CandidateRuntimeSeal | None = None
    layout: ExternalRunLayout | None = None
    verified: SourceMatrixCandidateIdentity | None = None
    try:
        seal, layout = open_candidate_runtime_seal()
        identity = source_matrix_candidate_identity()
        configuration_ready = not configure_child
        if identity == seal.identity and configure_child:
            env = dict(os.environ)
            prepared = prepare_analytics_source_matrix(
                env=env,
                fixtures=fixtures,
                candidate_identity=identity,
                captured_at=datetime.now(tz=UTC),
            )
            if isinstance(prepared, PreparedMatrix):
                child_paths = prepare_child_run_paths(seal, layout)
                build_child_launch_config(child_paths, env)
                configuration_ready = True
        if identity == seal.identity and configuration_ready:
            revalidate_candidate_runtime(seal, layout)
            verified = identity
    except (
        CandidateRuntimeError,
        ChildConfigurationError,
        OSError,
        ValidationError,
        ValueError,
    ):
        verified = None
    finally:
        if seal is not None:
            close_candidate_runtime_seal(seal)
        if layout is not None:
            try:
                _remove_empty_directories(
                    (layout.child_cache, layout.child_work, layout.child_tmp),
                )
            except OSError:
                verified = None
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the installed one-shot Saxo SIM analytics source matrix.",
    )
    parser.add_argument("--instrument-uic", type=int, default=211)
    parser.add_argument("--asset-type", default="Stock")
    parser.add_argument("--option-root-id", type=int, default=120)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--identity",
        action="store_true",
        help="verify and print the immutable installed candidate identity",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="verify the local isolated execution closure without Saxo access",
    )
    arguments = parser.parse_args(argv)
    fixtures = SourceMatrixFixtures(
        account_key=os.environ.get("SAXO_MCP_QA_ACCOUNT_KEY", ""),
        client_key=os.environ.get("SAXO_MCP_QA_CLIENT_KEY", ""),
        instrument_uic=arguments.instrument_uic,
        asset_type=arguments.asset_type,
        option_root_id=arguments.option_root_id,
    )
    if arguments.identity:
        identity = _verify_official_local_mode(fixtures, configure_child=False)
        if identity is None:
            return 1
        sys.stdout.write(identity.candidate_identity_sha256 + "\n")
        return 0
    if arguments.preflight:
        identity = _verify_official_local_mode(fixtures, configure_child=True)
        if identity is None:
            return 1
        sys.stdout.write(
            json.dumps(
                {
                    "candidate_identity_sha256": identity.candidate_identity_sha256,
                    "closure": "sealed",
                    "dont_write_bytecode": sys.flags.dont_write_bytecode == 1,
                    "ignore_environment": sys.flags.ignore_environment == 1,
                    "isolated": sys.flags.isolated == 1,
                    "no_site": sys.flags.no_site == 1,
                },
                sort_keys=True,
            )
            + "\n",
        )
        return 0
    return execute_analytics_source_matrix_once(fixtures=fixtures)


if __name__ == "__main__":
    raise SystemExit(main())
