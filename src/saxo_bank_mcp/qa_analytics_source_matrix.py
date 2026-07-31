from __future__ import annotations

import _thread
import argparse
import dis
import hashlib
import importlib.machinery
import importlib.util
import itertools
import json
import math
import os
import platform
import pwd
import re
import stat
import sys
import sysconfig
import zipimport
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path
from types import (
    BuiltinFunctionType,
    CellType,
    CodeType,
    FunctionType,
    MappingProxyType,
    MethodType,
    ModuleType,
)
from typing import Final, Literal, Self, cast
from uuid import uuid4

import anyio
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

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
_HTTP_FAILURE_STATUS_MIN: Final = 400
_HTTP_STATUS_FORBIDDEN: Final = 403
_HTTP_STATUS_RATE_LIMITED: Final = 429
_MAX_CAPABILITY_TEXT_LENGTH: Final = 80
_OWNER_DIRECTORY_MODE: Final = 0o700
_OWNER_FILE_MODE: Final = 0o600
_OWNER_ONLY_MASK: Final = 0o077
_OWNER_COMMON_WRITE_MASK: Final = 0o022
_EXACT_DIRECTORY_COMPONENT_INDEX: Final = 2
_SELF_REFERENCE_EXCLUSION_COUNT: Final = 2
_ENTRYPOINT_TARGET_PART_COUNT: Final = 2
_PORTABLE_ENTRYPOINT_MIN_LINES: Final = 4
_IMPORT_STATE_MAX_DEPTH: Final = 5
_IMPORT_CLOSURE_CELL_COUNT: Final = 2
_IMPORT_LOADER_DETAIL_COUNT: Final = 2
_CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS: Final = 4300
_EXECUTION_PROJECTION_STABILIZATION_LIMIT: Final = 3
_REGEX_CACHE_KEY_PART_COUNT: Final = 3
_GLOBAL_LOAD_OPNAMES: Final = frozenset(
    {"LOAD_FROM_DICT_OR_GLOBALS", "LOAD_GLOBAL", "LOAD_NAME"},
)
_OPAQUE_MUTATOR_NAMES: Final = frozenset(
    {
        "__iadd__",
        "__iand__",
        "__ifloordiv__",
        "__ilshift__",
        "__imatmul__",
        "__imod__",
        "__imul__",
        "__ior__",
        "__ipow__",
        "__irshift__",
        "__isub__",
        "__itruediv__",
        "__ixor__",
        "__next__",
        "__setitem__",
        "acquire",
        "add",
        "append",
        "clear",
        "close",
        "discard",
        "extend",
        "insert",
        "pop",
        "popitem",
        "release",
        "remove",
        "reset",
        "send",
        "set",
        "setdefault",
        "throw",
        "update",
        "write",
    },
)
_INSTALLER_GENERATED_METADATA: Final = frozenset(
    {"INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"},
)
_EXPECTED_INSTALLER: Final = "uv"
_OFFICIAL_LAUNCHER_ENV: Final = "SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER"
_PYTHON_SOURCE_SUFFIXES: Final = frozenset(importlib.machinery.SOURCE_SUFFIXES)
_PYTHON_BYTECODE_SUFFIXES: Final = frozenset(
    {*importlib.machinery.BYTECODE_SUFFIXES, ".pyo"},
)
_PYTHON_EXTENSION_SUFFIXES: Final = frozenset(importlib.machinery.EXTENSION_SUFFIXES)
_IMPORTABLE_ARTIFACT_SUFFIXES: Final = (
    _PYTHON_SOURCE_SUFFIXES | _PYTHON_BYTECODE_SUFFIXES | _PYTHON_EXTENSION_SUFFIXES
)
_RUNTIME_ARTIFACT_SUFFIXES: Final = _IMPORTABLE_ARTIFACT_SUFFIXES | frozenset(
    {".pyi", ".dylib", ".dll", ".pyd"},
)
_RUNTIME_TREE_EXCLUDED_PARTS: Final = frozenset(
    {"dist-packages", "site-packages"},
)
_STARTUP_CONFIGURATION_NAMES: Final = frozenset(
    {"pyvenv.cfg", "sitecustomize.py", "usercustomize.py"},
)
_STARTUP_CONFIGURATION_SUFFIXES: Final = (".pth", "._pth", ".egg-link")
_PYTHON_BUILD_CONFIG_KEYS: Final = (
    "ABIFLAGS",
    "CONFIG_ARGS",
    "EXT_SUFFIX",
    "LDLIBRARY",
    "LIBRARY",
    "MULTIARCH",
    "Py_ENABLE_SHARED",
    "Py_GIL_DISABLED",
    "SOABI",
    "VERSION",
    "WITH_PYMALLOC",
)
_SOURCE_RUNNERS: Final = (
    "scripts/generate_analytics_source_matrix_candidate.py",
    "scripts/prepare_analytics_source_matrix_runtime.py",
    "scripts/run_analytics_source_matrix.py",
    "scripts/saxo-bank-analytics-source-matrix",
    "scripts/saxo-bank-analytics-source-matrix-generate",
)
_OFFICIAL_LAUNCHER_NAMES: Final = (
    "saxo-bank-analytics-source-matrix",
    "saxo-bank-analytics-source-matrix-generate",
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
_SAFE_EXCHANGE_ID_PATTERN: Final = re.compile(r"^[A-Z0-9._:-]{1,32}$")
_JSON_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
_SOURCE_PLAN_UNAVAILABLE_SHA256: Final = hashlib.sha256(
    b"source-plan-unavailable",
).hexdigest()
_REDUCIBLE_SOURCE_FIXTURES: Final = {
    "bookings_v1": "fixture_client_key_unavailable",
    "closed_positions_history_v1": "fixture_client_key_unavailable",
    "costs_v1": "fixture_account_key_unavailable",
}
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


class _ExecutionClosureChangedError(RuntimeError):
    """Internal value-free signal that the claimed execution closure changed."""


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
    executable_projection_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    python_build_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    build_config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stdlib_merkle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stdlib_file_count: int = Field(ge=1)
    platstdlib_merkle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    platstdlib_file_count: int = Field(ge=1)
    shared_runtime_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    shared_runtime_file_count: int = Field(ge=0)
    startup_configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    startup_configuration_file_count: int = Field(ge=0)
    interpreter_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    importable_suffixes: tuple[str, ...]


class _DependencyDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str
    files: Mapping[str, str]
    installer_metadata: Mapping[str, str]


class _SourceMatrixCandidateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["4"]
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
    execution_closure_checkpoints: tuple[str, ...] = ()


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
    validate_source_execution: Callable[[], bool] | None = None,
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
    execution_checkpoints: list[str] = []

    def closure_valid(label: str) -> bool:
        execution_checkpoints.append(label)
        if validate_source_execution is None:
            return True
        try:
            return validate_source_execution() is True
        except Exception:  # noqa: BLE001 - every callback failure closes execution
            return False

    def require_closure(label: str) -> None:
        if not closure_valid(label):
            raise _ExecutionClosureChangedError

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
        if not closure_valid("readiness_to_source"):
            return _readiness_refusal(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status=entitlement_status,
                reason="candidate_execution_closure_changed",
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
        try:
            require_closure("after_claim")
            before = await _state_fingerprint(
                client,
                registry,
                checkpoint=require_closure,
                phase="before_state",
            )
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
                    await _run_provider_source(
                        client,
                        contract,
                        request,
                        checkpoint=require_closure,
                    ),
                )
            source_receipts = tuple(source_receipts_list)
            after = await _state_fingerprint(
                client,
                registry,
                checkpoint=require_closure,
                phase="after_state",
            )
            cleanup = CleanupReceipt(
                before_fingerprint=before,
                after_fingerprint=after,
                state_equal=before is not None and before == after,
                complete=before is not None and before == after,
            )
            require_closure("cleanup_readback_complete")
            if not cleanup.state_equal:
                errors.append("state_fingerprint_mismatch")
            ledger_payload = await _call_tool_with_checkpoint(
                client,
                "saxo_get_safe_request_ledger",
                {},
                checkpoint=require_closure,
                checkpoint_name="ledger_readback",
            )
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
                execution_closure_checkpoints=tuple(execution_checkpoints),
            )
        except _ExecutionClosureChangedError:
            return _claimed_execution_closure_failure(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status=entitlement_status,
                checkpoints=execution_checkpoints,
            )
        try:
            require_closure("before_mcp_client_close")
        except _ExecutionClosureChangedError:
            return _claimed_execution_closure_failure(
                candidate_identity,
                capture_time,
                proof,
                auth_status=auth_status,
                session_status=session_status,
                entitlement_status=entitlement_status,
                checkpoints=execution_checkpoints,
            )
    try:
        require_closure("after_mcp_client_close")
        require_closure("before_receipt_serialization")
        try:
            scanned_receipt = _with_privacy_scan(receipt, fixtures)
        finally:
            require_closure("after_privacy_scan")
    except _ExecutionClosureChangedError:
        return _claimed_execution_closure_failure(
            candidate_identity,
            capture_time,
            proof,
            auth_status=auth_status,
            session_status=session_status,
            entitlement_status=entitlement_status,
            checkpoints=execution_checkpoints,
        )
    return scanned_receipt


def execute_analytics_source_matrix_once(
    *,
    fixtures: SourceMatrixFixtures | None = None,
) -> int:
    """Run only the installed candidate in its fixed owner-only state location."""
    try:
        _normalize_official_import_machinery()
        _validate_official_interpreter_state()
    except (OSError, ValueError):
        return 1
    return _execute_analytics_source_matrix_once(
        fixtures=fixtures,
        server=mcp,
        env=os.environ,
        captured_at=None,
        state_root=None,
        candidate_identity=None,
    )


def _execute_analytics_source_matrix_once(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
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
    source_execution_validator: Callable[[], bool] | None = None
    try:
        if candidate_identity is None:
            identity = source_matrix_candidate_identity()
            initial_root_projection = _execution_root_projection_sha256()

            def verify_official_candidate() -> bool:
                try:
                    if _execution_root_projection_sha256() != initial_root_projection:
                        return False
                    current_identity = source_matrix_candidate_identity()
                    return (
                        current_identity == identity
                        and _execution_root_projection_sha256() == initial_root_projection
                    )
                except (OSError, ValidationError, ValueError):
                    return False

            source_execution_validator = verify_official_candidate
        else:
            identity = candidate_identity
        selected_state_root = (
            _source_matrix_state_root() if state_root is None else state_root.absolute()
        )
    except (OSError, ValidationError, ValueError):
        return 1
    selected_fixtures = fixtures or SourceMatrixFixtures(
        account_key=selected_env.get("SAXO_MCP_QA_ACCOUNT_KEY", ""),
        client_key=selected_env.get("SAXO_MCP_QA_CLIENT_KEY", ""),
    )
    claimed = False
    claimed_guard: _ClaimedGuardDirectory | None = None

    def claim() -> bool:
        nonlocal claimed, claimed_guard
        reopen = (
            (
                lambda: _open_guard_parent(
                    Path(pwd.getpwuid(os.getuid()).pw_dir),
                    identity.candidate_identity_sha256,
                )
            )
            if state_root is None
            else (
                lambda: _open_state_guard_parent(
                    selected_state_root,
                    identity.candidate_identity_sha256,
                )
            )
        )
        claimed_guard = _claim_candidate_guard_directory(reopen, identity)
        claimed = claimed_guard is not None
        return claimed

    async def run() -> AnalyticsSourceMatrixReceipt:
        return await run_analytics_source_matrix(
            server,
            env=selected_env,
            fixtures=selected_fixtures,
            candidate_identity=identity,
            captured_at=captured_at,
            claim_source_execution=claim,
            validate_source_execution=source_execution_validator,
        )

    def execution_closure_valid() -> bool:
        if source_execution_validator is None:
            return True
        try:
            return source_execution_validator() is True
        except Exception:  # noqa: BLE001 - callback failure closes the execution
            return False

    def require_execution_closure() -> None:
        if not execution_closure_valid():
            raise _ExecutionClosureChangedError

    def publish_failure(reason: str) -> None:
        if claimed_guard is None:
            return
        selected_reason = reason
        if reason != "candidate_execution_closure_changed" and not execution_closure_valid():
            selected_reason = "candidate_execution_closure_changed"
        before_link = (
            execution_closure_valid
            if selected_reason != "candidate_execution_closure_changed"
            else None
        )
        try:
            published_failure = _publish_claimed_failure(
                claimed_guard,
                selected_reason,
                before_link=before_link,
            )
        except _ExecutionClosureChangedError:
            try:
                published_failure = _publish_claimed_failure(
                    claimed_guard,
                    "candidate_execution_closure_changed",
                )
            except Exception:  # noqa: BLE001 - secure refusal publishes nothing
                return
            selected_reason = "candidate_execution_closure_changed"
        except Exception:  # noqa: BLE001 - secure refusal publishes nothing
            return
        if (
            published_failure is not None
            and selected_reason != "candidate_execution_closure_changed"
            and not execution_closure_valid()
        ):
            try:
                _unlink_published_evidence(claimed_guard, published_failure)
                _publish_claimed_failure(
                    claimed_guard,
                    "candidate_execution_closure_changed",
                )
            except Exception:  # noqa: BLE001 - secure refusal publishes nothing
                return

    try:
        try:
            receipt = anyio.run(run)
        except Exception:  # noqa: BLE001 - freeze only after the irreversible claim
            if claimed:
                publish_failure("matrix_execution_failed")
            return 1
        if not receipt.source_execution_claimed or claimed_guard is None:
            return 1
        if receipt.status == "failed" and receipt.reason == "candidate_execution_closure_changed":
            publish_failure("candidate_execution_closure_changed")
            return 1
        if not execution_closure_valid():
            publish_failure("candidate_execution_closure_changed")
            return 1
        receipt = receipt.model_copy(
            update={
                "execution_closure_checkpoints": (
                    *receipt.execution_closure_checkpoints,
                    "before_evidence_serialization",
                    "after_evidence_privacy_scan",
                    "before_immutable_evidence_publication",
                ),
            },
        )
        require_execution_closure()
        payload = cast("dict[str, JsonValue]", receipt.model_dump(mode="json"))
        text = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
        findings, scan_errors = scan_secret_text("source-matrix.json", text)
        require_execution_closure()
        if findings or scan_errors:
            publish_failure("evidence_secret_scan_failed")
            return 1
        published = _publish_claimed_text(
            claimed_guard,
            text,
            before_link=execution_closure_valid,
        )
        if published is None:
            return 1
        if not execution_closure_valid():
            _unlink_published_evidence(claimed_guard, published)
            publish_failure("candidate_execution_closure_changed")
            return 1
        return 0 if receipt.status in {"passed", "reduced"} else 1  # noqa: TRY300
    except _ExecutionClosureChangedError:
        publish_failure("candidate_execution_closure_changed")
        return 1
    except Exception:  # noqa: BLE001 - revalidate before publishing generic failure
        if claimed:
            publish_failure("matrix_execution_failed")
        return 1
    finally:
        if claimed_guard is not None:
            os.close(claimed_guard.descriptor)


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


async def _call_tool_with_checkpoint(
    client: MatrixClient,
    name: str,
    arguments: dict[str, JsonValue],
    *,
    checkpoint: Callable[[str], None],
    checkpoint_name: str,
) -> dict[str, JsonValue]:
    """Validate the claimed closure on both sides of one MCP boundary."""
    checkpoint(f"before_{checkpoint_name}")
    try:
        return await _call_tool(client, name, arguments)
    finally:
        checkpoint(f"after_{checkpoint_name}")


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
    *,
    checkpoint: Callable[[str], None] | None = None,
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
        "params": params,
        "response_mode": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
    }
    payload = (
        await _call_tool(
            client,
            "saxo_call_registered_endpoint",
            arguments,
        )
        if checkpoint is None
        else await _call_tool_with_checkpoint(
            client,
            "saxo_call_registered_endpoint",
            arguments,
            checkpoint=checkpoint,
            checkpoint_name=f"source:{contract.contract_id}",
        )
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
    client: MatrixClient,
    registry: Mapping[str, bool],
    *,
    checkpoint: Callable[[str], None] | None = None,
    phase: str = "state",
) -> str | None:
    fingerprints: dict[str, str] = {}
    for path in _STATE_PATHS:
        operation = find_registered_operation("GET", path)
        if operation is None or registry.get(operation.operation_id) is not True:
            return None
        checkpoint_name = operation.operation_id
        arguments: dict[str, JsonValue] = {
            "method": "GET",
            "path": path,
            "response_mode": "fingerprint_only",
        }
        payload = (
            await _call_tool(
                client,
                "saxo_call_registered_endpoint",
                arguments,
            )
            if checkpoint is None
            else await _call_tool_with_checkpoint(
                client,
                "saxo_call_registered_endpoint",
                arguments,
                checkpoint=checkpoint,
                checkpoint_name=f"{phase}:{checkpoint_name}",
            )
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


def _claimed_execution_closure_failure(  # noqa: PLR0913
    candidate_identity: SourceMatrixCandidateIdentity,
    captured_at: datetime,
    proof: EnvironmentProof,
    *,
    auth_status: str,
    session_status: str,
    entitlement_status: str,
    checkpoints: Sequence[str],
) -> AnalyticsSourceMatrixReceipt:
    """Return a claimed, value-free failure that can only publish minimally."""
    return AnalyticsSourceMatrixReceipt(
        status="failed",
        reason="candidate_execution_closure_changed",
        environment="SIM",
        source_contract_catalog_sha256=candidate_identity.source_contract_catalog_sha256,
        harness_build_sha256=candidate_identity.harness_build_sha256,
        candidate_identity_sha256=candidate_identity.candidate_identity_sha256,
        captured_at=captured_at,
        environment_proof=proof,
        auth_status=auth_status,
        session_status=session_status,
        entitlement_status=entitlement_status,
        source_receipts=(),
        history_state="unverified",
        source_execution_claimed=True,
        cleanup=_empty_cleanup(),
        ledger=_empty_ledger(),
        privacy=PrivacyReceipt(findings=0, scan_errors=0),
        live_events=0,
        errors=("candidate_execution_closure_changed",),
        execution_closure_checkpoints=tuple(checkpoints),
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


def source_matrix_candidate_identity() -> SourceMatrixCandidateIdentity:  # noqa: C901
    """Verify the complete source or installed distribution closure."""
    if os.environ.get(_OFFICIAL_LAUNCHER_ENV) == "1":
        _validate_official_interpreter_state()
    initial_search_directories = _execution_search_directory_snapshot()
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
    repository_root = Path(__file__).resolve().parents[2] if source_mode else None
    actual_files = (
        _source_candidate_files(cast("Path", repository_root))
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
    _validate_import_execution_closure(
        manifest,
        source_repository_root=repository_root,
    )
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
    identity = SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256=catalog_sha256,
        harness_build_sha256=harness_sha256,
        candidate_identity_sha256=identity_sha256,
    )
    stabilized_execution_projection = _stabilized_execution_root_projection_sha256()
    if _runtime_identity() != manifest.runtime_identity:
        raise ValueError("source matrix runtime identity changed")
    if _dependency_distributions() != dict(manifest.dependency_distributions):
        raise ValueError("source matrix dependency closure changed")
    _validate_import_execution_closure(
        manifest,
        source_repository_root=repository_root,
    )
    if (
        _execution_search_directory_snapshot() != initial_search_directories
        or _execution_root_projection_sha256() != stabilized_execution_projection
    ):
        raise ValueError("source matrix execution closure changed")
    return identity


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
    if not manifest.source_files or any(
        not _safe_artifact_path(name) or _SHA256_PATTERN.fullmatch(sha256) is None
        for name, sha256 in manifest.source_files.items()
    ):
        raise ValueError("source matrix candidate file map is invalid")
    if not manifest.installed_files or any(
        (not _safe_artifact_path(name) and not _official_launcher_record_path(name))
        or _SHA256_PATTERN.fullmatch(sha256) is None
        for name, sha256 in manifest.installed_files.items()
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
        metadata = _validated_closure_entry_metadata(path, scope="source closure")
        if not stat.S_ISREG(metadata.st_mode):
            continue
        relative = path.relative_to(repository_root).as_posix()
        if relative == "data/analytics/source_matrix_candidate.json":
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _validated_closure_entry_metadata(
    path: Path,
    *,
    scope: str,
    allowed_symlinks: frozenset[Path] = frozenset(),
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"source matrix {scope} entry is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) and path.absolute() not in allowed_symlinks:
        raise ValueError(f"source matrix {scope} contains a symlink")
    if path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_BYTECODE_SUFFIXES:
        raise ValueError(f"source matrix {scope} contains a Python cache entry")
    return metadata


def _validate_directory_entry_tree(
    root: Path,
    *,
    scope: str,
    allowed_symlinks: frozenset[Path] = frozenset(),
) -> None:
    root_metadata = _validated_closure_entry_metadata(root, scope=scope)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"source matrix {scope} is not a directory")
    for path in sorted(root.rglob("*")):
        if _inactive_runtime_cache_allowed(path):
            continue
        _validated_closure_entry_metadata(
            path,
            scope=scope,
            allowed_symlinks=allowed_symlinks,
        )


def _expected_interpreter_aliases(root: Path) -> frozenset[Path]:
    """Allow only the conventional venv aliases for the running interpreter."""
    executable = Path(sys.executable).absolute()
    executable_root = executable.parent.resolve(strict=True)
    try:
        executable_root.relative_to(root)
    except ValueError:
        return frozenset()
    target = executable.resolve(strict=True)
    version_names = {
        "python",
        f"python{sys.version_info.major}",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
    }
    if executable.suffix:
        version_names |= {f"{name}{executable.suffix}" for name in tuple(version_names)}
    result: set[Path] = set()
    for name in version_names | {executable.name}:
        candidate = executable_root / name
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) and candidate.resolve(strict=True) == target:
            result.add(candidate.absolute())
    return frozenset(result)


def _execution_path_projection(raw_value: str) -> dict[str, JsonValue]:
    raw_path = Path.cwd() if raw_value == "" else Path(raw_value)
    path = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    absolute = path.absolute()
    result: dict[str, JsonValue] = {
        "absolute_sha256": hashlib.sha256(os.fsencode(absolute)).hexdigest(),
        "raw_sha256": hashlib.sha256(os.fsencode(raw_value)).hexdigest(),
    }
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        result["exists"] = False
        return result
    result.update(
        {
            "change_time_ns": metadata.st_ctime_ns,
            "device": metadata.st_dev,
            "exists": True,
            "group": metadata.st_gid,
            "inode": metadata.st_ino,
            "link_count": metadata.st_nlink,
            "mode": metadata.st_mode,
            "modified_time_ns": metadata.st_mtime_ns,
            "owner": metadata.st_uid,
            "resolved_sha256": hashlib.sha256(
                os.fsencode(absolute.resolve(strict=True)),
            ).hexdigest(),
            "size": metadata.st_size,
            "symlink": stat.S_ISLNK(metadata.st_mode),
        },
    )
    if stat.S_ISLNK(metadata.st_mode):
        result["link_target_sha256"] = hashlib.sha256(
            os.fsencode(absolute.readlink()),
        ).hexdigest()
    elif stat.S_ISDIR(metadata.st_mode):
        directory_entries: list[dict[str, JsonValue]] = []
        for entry in (absolute, *sorted(absolute.rglob("*"))):
            entry_metadata = entry.lstat()
            if not stat.S_ISDIR(entry_metadata.st_mode) and not stat.S_ISLNK(
                entry_metadata.st_mode,
            ):
                continue
            projected: dict[str, JsonValue] = {
                "change_time_ns": entry_metadata.st_ctime_ns,
                "device": entry_metadata.st_dev,
                "group": entry_metadata.st_gid,
                "inode": entry_metadata.st_ino,
                "link_count": entry_metadata.st_nlink,
                "mode": entry_metadata.st_mode,
                "modified_time_ns": entry_metadata.st_mtime_ns,
                "name_sha256": hashlib.sha256(
                    os.fsencode(entry.relative_to(absolute)),
                ).hexdigest(),
                "owner": entry_metadata.st_uid,
                "size": entry_metadata.st_size,
            }
            if stat.S_ISLNK(entry_metadata.st_mode):
                projected["link_target_sha256"] = hashlib.sha256(
                    os.fsencode(entry.readlink()),
                ).hexdigest()
            directory_entries.append(projected)
        result["directory_entry_count"] = len(directory_entries)
        result["directory_tree_sha256"] = _digest(directory_entries)
    return result


def _execution_search_directory_snapshot() -> tuple[dict[str, JsonValue], ...]:
    """Capture ordered execution-search directories and stable metadata."""
    return tuple(_execution_path_projection(value) for value in sys.path)


def _stable_import_state(  # noqa: C901, PLR0911, PLR0912, PLR0915
    value: object,
    *,
    depth: int = 0,
    seen: frozenset[int] = frozenset(),
) -> JsonValue:
    """Project mutable finder state without retaining private strings or paths."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"float": value.hex()}
    if isinstance(value, str | bytes):
        raw = os.fsencode(value)
        return {
            "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, os.PathLike):
        raw_path = os.fspath(
            cast("os.PathLike[str] | os.PathLike[bytes]", value),
        )
        raw = raw_path if isinstance(raw_path, bytes) else os.fsencode(raw_path)
        return {
            "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, ContextVar):
        context_variable = cast("ContextVar[object]", value)
        identity = id(context_variable)
        if identity in seen:
            return {
                "cycle_module": type(context_variable).__module__,
                "cycle_qualname": type(context_variable).__qualname__,
            }
        missing = object()
        current = context_variable.get(missing)
        stable_repr = _without_object_addresses(repr(context_variable))
        return {
            "definition_sha256": hashlib.sha256(stable_repr.encode()).hexdigest(),
            "kind": "context_variable",
            "present": current is not missing,
            "value": (
                None
                if current is missing
                else _stable_import_state(
                    current,
                    depth=depth + 1,
                    seen=seen | {identity},
                )
            ),
        }
    if isinstance(value, _thread.LockType):
        return {
            "kind": "thread_lock",
            "locked": value.locked(),
        }
    if isinstance(value, _thread.RLock):
        representation = repr(value)
        count_marker = " count="
        count_start = representation.find(count_marker)
        count_start = count_start + len(count_marker) if count_start >= 0 else -1
        count_end = count_start
        while count_end >= 0 and count_end < len(representation):
            if not representation[count_end].isdigit():
                break
            count_end += 1
        return {
            "count": (
                int(representation[count_start:count_end])
                if count_start >= 0 and count_end > count_start
                else 0
            ),
            "kind": "thread_recursive_lock",
            "locked": representation.startswith("<locked "),
        }
    if isinstance(value, itertools.count):
        return {
            "kind": "count_iterator",
            "state_sha256": hashlib.sha256(
                repr(cast("object", value)).encode(),
            ).hexdigest(),
        }
    if depth >= _IMPORT_STATE_MAX_DEPTH:
        return {
            "module": type(value).__module__,
            "qualname": type(value).__qualname__,
        }
    identity = id(value)
    if identity in seen:
        return {
            "cycle_module": type(value).__module__,
            "cycle_qualname": type(value).__qualname__,
        }
    next_seen = seen | {identity}
    if isinstance(value, Mapping) and (
        isinstance(value, dict | MappingProxyType)
        or not _execution_slots_declared(cast("object", value))
    ):
        try:
            items = tuple(cast("Mapping[object, object]", value).items())
        except Exception:
            if isinstance(value, dict | MappingProxyType):
                raise
        else:
            projected: list[tuple[JsonValue, JsonValue]] = [
                (
                    _stable_import_state(key, depth=depth + 1, seen=next_seen),
                    _stable_import_state(item, depth=depth + 1, seen=next_seen),
                )
                for key, item in items
            ]

            def projection_key(item: tuple[JsonValue, JsonValue]) -> str:
                return json.dumps(item[0], default=str, sort_keys=True)

            return sorted(
                projected,
                key=projection_key,
            )
    if isinstance(value, AbstractSet):
        projected_items = [
            _stable_import_state(item, depth=depth + 1, seen=next_seen)
            for item in tuple(cast("AbstractSet[object]", value))
        ]
        return sorted(
            projected_items,
            key=lambda item: json.dumps(item, default=str, sort_keys=True),
        )
    if isinstance(value, Sequence):
        return [
            _stable_import_state(item, depth=depth + 1, seen=next_seen)
            for item in tuple(cast("Sequence[object]", value))
        ]
    value = cast("object", value)
    if isinstance(value, ModuleType):
        return _execution_reference_projection(value)
    if isinstance(value, CodeType):
        return {
            "argcount": value.co_argcount,
            "bytecode_sha256": hashlib.sha256(value.co_code).hexdigest(),
            "cellvars": _stable_import_state(
                value.co_cellvars,
                depth=depth + 1,
                seen=next_seen,
            ),
            "constants": _stable_import_state(
                value.co_consts,
                depth=depth + 1,
                seen=next_seen,
            ),
            "exceptiontable_sha256": hashlib.sha256(value.co_exceptiontable).hexdigest(),
            "filename": _stable_import_state(
                value.co_filename,
                depth=depth + 1,
                seen=next_seen,
            ),
            "firstlineno": value.co_firstlineno,
            "flags": value.co_flags,
            "freevars": _stable_import_state(
                value.co_freevars,
                depth=depth + 1,
                seen=next_seen,
            ),
            "kind": "code",
            "kwonlyargcount": value.co_kwonlyargcount,
            "linetable_sha256": hashlib.sha256(value.co_linetable).hexdigest(),
            "name": _stable_import_state(
                value.co_name,
                depth=depth + 1,
                seen=next_seen,
            ),
            "names": _stable_import_state(
                value.co_names,
                depth=depth + 1,
                seen=next_seen,
            ),
            "nlocals": value.co_nlocals,
            "posonlyargcount": value.co_posonlyargcount,
            "qualname": _stable_import_state(
                value.co_qualname,
                depth=depth + 1,
                seen=next_seen,
            ),
            "stacksize": value.co_stacksize,
            "varnames": _stable_import_state(
                value.co_varnames,
                depth=depth + 1,
                seen=next_seen,
            ),
        }
    if isinstance(value, FunctionType):
        return {
            "annotations": _stable_import_state(
                value.__annotations__,
                depth=depth + 1,
                seen=next_seen,
            ),
            "closure": _stable_import_state(
                tuple(
                    None if _empty_closure_cell(cell) else cast("object", cell.cell_contents)
                    for cell in value.__closure__ or ()
                ),
                depth=depth + 1,
                seen=next_seen,
            ),
            "code": _stable_import_state(
                value.__code__,
                depth=depth + 1,
                seen=next_seen,
            ),
            "defaults": _stable_import_state(
                value.__defaults__,
                depth=depth + 1,
                seen=next_seen,
            ),
            "dict": _stable_import_state(
                value.__dict__,
                depth=depth + 1,
                seen=next_seen,
            ),
            "kind": "function",
            "kwdefaults": _stable_import_state(
                value.__kwdefaults__,
                depth=depth + 1,
                seen=next_seen,
            ),
            "module": value.__module__,
            "qualname": value.__qualname__,
            "referenced_globals": _function_execution_dependencies(
                value,
                depth=depth + 1,
                seen=next_seen,
            ),
        }
    if isinstance(value, MethodType):
        return {
            "function": _stable_import_state(
                value.__func__,
                depth=depth + 1,
                seen=next_seen,
            ),
            "kind": "method",
            "owner": _stable_import_state(
                value.__self__,
                depth=depth + 1,
                seen=next_seen,
            ),
        }
    if isinstance(value, BuiltinFunctionType):
        owner = getattr(value, "__self__", None)
        return {
            "kind": "builtin",
            "module": str(getattr(value, "__module__", "")),
            "name": str(getattr(value, "__name__", "")),
            "owner_module": type(owner).__module__,
            "owner_qualname": type(owner).__qualname__,
            "qualname": str(getattr(value, "__qualname__", "")),
        }
    if isinstance(value, classmethod | staticmethod):
        descriptor_function = cast(  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "object",
            value.__func__,
        )
        return {
            "descriptor": ("classmethod" if isinstance(value, classmethod) else "staticmethod"),
            "function": _stable_import_state(
                descriptor_function,
                depth=depth + 1,
                seen=next_seen,
            ),
        }
    if isinstance(value, property):
        return {
            "deleter": _stable_import_state(
                value.fdel,
                depth=depth + 1,
                seen=next_seen,
            ),
            "getter": _stable_import_state(
                value.fget,
                depth=depth + 1,
                seen=next_seen,
            ),
            "kind": "property",
            "setter": _stable_import_state(
                value.fset,
                depth=depth + 1,
                seen=next_seen,
            ),
        }
    if isinstance(value, type):
        members: list[dict[str, JsonValue]] = []
        for name, item in sorted(vars(value).items()):
            item_projection: JsonValue
            if _class_execution_member(item):
                if isinstance(item, type) and depth >= 1:
                    item_projection = _execution_reference_projection(item)
                else:
                    item_projection = _stable_import_state(
                        item,
                        depth=depth + 1,
                        seen=next_seen,
                    )
            else:
                item_projection = _privacy_safe_execution_dependency(
                    item,
                    depth=depth + 1,
                    seen=next_seen,
                )
            members.append(
                {
                    "name_sha256": hashlib.sha256(name.encode()).hexdigest(),
                    "value": item_projection,
                },
            )
        metaclass = type(value)
        metaclass_projection = _execution_reference_projection(metaclass)
        if metaclass is not type:
            metaclass_projection = _privacy_safe_projected_execution_state(
                metaclass,
                _stable_import_state(
                    metaclass,
                    depth=depth + 1,
                    seen=next_seen,
                ),
            )
        return {
            "kind": "type",
            "metaclass": metaclass_projection,
            "members": members,
            "module": value.__module__,
            "qualname": value.__qualname__,
        }
    state: dict[str, JsonValue] = {
        "kind": "object",
        "type_module": type(value).__module__,
        "type_qualname": type(value).__qualname__,
    }
    try:
        raw_dict: object = object.__getattribute__(value, "__dict__")
    except Exception:  # noqa: BLE001 - thread-local state needs normal lookup
        value_type = type(value)
        thread_local = value_type.__module__ == "_thread" and value_type.__qualname__ == "_local"
        raw_dict = vars(value) if thread_local else None
    state_captured = False
    if isinstance(raw_dict, Mapping):
        state["state"] = _stable_import_state(
            cast("Mapping[object, object]", raw_dict),
            depth=depth + 1,
            seen=next_seen,
        )
        state_captured = True
    try:
        raw_closure: object = object.__getattribute__(value, "__closure__")
    except Exception:  # noqa: BLE001 - non-callable objects have no closure
        raw_closure = None
    if isinstance(raw_closure, tuple):
        closure: list[JsonValue] = []
        for cell in cast("tuple[CellType, ...]", raw_closure):
            try:
                cell_value = cast("object", cell.cell_contents)
            except ValueError:
                cell_value = None
            closure.append(
                _stable_import_state(
                    cell_value,
                    depth=depth + 1,
                    seen=next_seen,
                ),
            )
        state["closure"] = closure
        state_captured = True
    slots = _execution_slot_state(value, depth=depth, seen=next_seen)
    if slots:
        state["slots"] = slots
    if slots or _execution_slots_declared(value):
        state_captured = True
    if not state_captured and _opaque_execution_dependency_is_mutable(value):
        raise ValueError("mutable execution dependency is unsupported")
    return state


def _empty_closure_cell(cell: CellType) -> bool:
    try:
        _ = cell.cell_contents
    except ValueError:
        return True
    return False


def _class_execution_member(value: object) -> bool:
    return isinstance(
        value,
        CodeType
        | FunctionType
        | BuiltinFunctionType
        | MethodType
        | type
        | classmethod
        | staticmethod
        | property,
    ) or callable(value)


def _execution_slot_state(
    value: object,
    *,
    depth: int,
    seen: frozenset[int],
) -> list[dict[str, JsonValue]]:
    slot_names: set[str] = set()
    for value_type in type(value).__mro__:
        raw_slots = vars(value_type).get("__slots__", ())
        names = (raw_slots,) if isinstance(raw_slots, str) else raw_slots
        if not isinstance(names, Sequence):
            continue
        for raw_name in cast("Sequence[object]", names):
            if not isinstance(raw_name, str) or raw_name in {"__dict__", "__weakref__"}:
                continue
            name = raw_name
            if name.startswith("__") and not name.endswith("__"):
                owner_name = value_type.__name__.lstrip("_")
                name = f"_{owner_name}{name}"
            slot_names.add(name)
    result: list[dict[str, JsonValue]] = []
    for name in sorted(slot_names):
        projected: dict[str, JsonValue] = {
            "name_sha256": hashlib.sha256(name.encode()).hexdigest(),
        }
        try:
            item = object.__getattribute__(value, name)
        except AttributeError:
            projected["present"] = False
        except Exception as exc:
            raise ValueError("execution dependency slot is unreadable") from exc
        else:
            projected.update(
                {
                    "present": True,
                    "value": _stable_import_state(
                        item,
                        depth=depth + 1,
                        seen=seen,
                    ),
                },
            )
        result.append(projected)
    return result


def _execution_slots_declared(value: object) -> bool:
    return any("__slots__" in vars(value_type) for value_type in type(value).__mro__)


def _opaque_execution_dependency_is_mutable(value: object) -> bool:
    return any(
        name in vars(value_type)
        for value_type in type(value).__mro__
        for name in _OPAQUE_MUTATOR_NAMES
    )


def _execution_type_sha256(value: object) -> str:
    value_type = type(value)
    return hashlib.sha256(
        f"{value_type.__module__}\0{value_type.__qualname__}".encode(),
    ).hexdigest()


def _without_object_addresses(representation: str) -> str:
    marker = " at 0x"
    search_start = 0
    while True:
        marker_start = representation.find(marker, search_start)
        if marker_start < 0:
            return representation
        address_start = marker_start + len(marker)
        address_end = address_start
        while (
            address_end < len(representation)
            and representation[address_end] in "0123456789abcdefABCDEF"
        ):
            address_end += 1
        if address_end > address_start and representation[address_end : address_end + 1] == ">":
            representation = representation[:marker_start] + representation[address_end:]
            search_start = marker_start
        else:
            search_start = address_start


def _execution_reference_projection(value: object) -> dict[str, JsonValue]:
    return {
        "kind": "execution_reference",
        "module_sha256": hashlib.sha256(
            str(getattr(value, "__module__", type(value).__module__)).encode(),
        ).hexdigest(),
        "qualname_sha256": hashlib.sha256(
            str(getattr(value, "__qualname__", type(value).__qualname__)).encode(),
        ).hexdigest(),
        "type_sha256": _execution_type_sha256(value),
    }


def _privacy_safe_projected_execution_state(
    value: object,
    state: JsonValue,
) -> dict[str, JsonValue]:
    return {
        "state_sha256": _digest(state),
        "type_sha256": _execution_type_sha256(value),
    }


def _privacy_safe_execution_dependency(
    value: object,
    *,
    depth: int,
    seen: frozenset[int],
) -> dict[str, JsonValue]:
    state: JsonValue
    if value is vars(re).get("_cache"):
        state = _validated_regex_cache_state(value)
    elif value is sys.modules:
        state = {
            **_execution_reference_projection(value),
            "kind": "loaded_module_registry_reference",
        }
    else:
        state = (
            _execution_reference_projection(cast("object", value))
            if isinstance(
                value,
                ModuleType
                | FunctionType
                | BuiltinFunctionType
                | MethodType
                | type
                | classmethod
                | staticmethod
                | property,
            )
            else _stable_import_state(value, depth=depth, seen=seen)
        )
    return _privacy_safe_projected_execution_state(cast("object", value), state)


def _validated_regex_cache_state(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError("regular expression cache is unsupported")
    compiler_module = vars(re).get("_compiler")
    compiler = (
        vars(compiler_module).get("compile") if isinstance(compiler_module, ModuleType) else None
    )
    if not callable(compiler):
        raise TypeError("regular expression compiler is unavailable")
    compile_pattern = cast("Callable[[str | bytes, int], object]", compiler)
    for key, cached in tuple(cast("dict[object, object]", value).items()):
        cache_key = cast("tuple[object, ...]", key) if isinstance(key, tuple) else ()
        if (
            len(cache_key) != _REGEX_CACHE_KEY_PART_COUNT
            or (cache_key[0] is not str and cache_key[0] is not bytes)
            or not isinstance(cache_key[1], str | bytes)
            or type(cache_key[1]) is not cache_key[0]
            or not isinstance(cache_key[2], int)
            or not isinstance(cached, re.Pattern)
        ):
            raise ValueError("regular expression cache entry is unsupported")
        expected = compile_pattern(cache_key[1], cache_key[2])
        if not isinstance(expected, re.Pattern):
            raise TypeError("regular expression compiler result is unsupported")
        cached_object = cast("object", cached)
        expected_object = cast("object", expected)
        cached_details = (
            object.__getattribute__(cached_object, "pattern"),
            object.__getattribute__(cached_object, "flags"),
            object.__getattribute__(cached_object, "groups"),
            object.__getattribute__(cached_object, "groupindex"),
        )
        expected_details = (
            object.__getattribute__(expected_object, "pattern"),
            object.__getattribute__(expected_object, "flags"),
            object.__getattribute__(expected_object, "groups"),
            object.__getattribute__(expected_object, "groupindex"),
        )
        if cached_details != expected_details:
            raise ValueError("regular expression cache entry is invalid")
    return {
        "kind": "validated_regular_expression_cache",
    }


@cache
def _code_global_names(code: CodeType) -> frozenset[str]:
    names = {
        instruction.argval
        for instruction in dis.get_instructions(code)
        if instruction.opname in _GLOBAL_LOAD_OPNAMES and isinstance(instruction.argval, str)
    }
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names.update(_code_global_names(constant))
    return frozenset(names)


def _function_execution_dependencies(
    function: FunctionType,
    *,
    depth: int,
    seen: frozenset[int],
) -> list[dict[str, JsonValue]]:
    dependencies: list[dict[str, JsonValue]] = []
    global_values = cast("Mapping[str, object]", function.__globals__)
    builtin_values = cast("Mapping[str, object]", function.__builtins__)
    for name in sorted(_code_global_names(function.__code__)):
        if name in global_values:
            scope = "global"
            value = global_values[name]
        elif name in builtin_values:
            scope = "builtins"
            value = builtin_values[name]
        else:
            dependencies.append(
                {
                    "name_sha256": hashlib.sha256(name.encode()).hexdigest(),
                    "present": False,
                },
            )
            continue
        dependencies.append(
            {
                "name_sha256": hashlib.sha256(name.encode()).hexdigest(),
                "present": True,
                "scope": scope,
                "value": _privacy_safe_execution_dependency(
                    value,
                    depth=depth + 1,
                    seen=seen,
                ),
            },
        )
    return dependencies


def _module_execution_projection(
    module: ModuleType,
    cache: dict[int, JsonValue] | None = None,
) -> list[dict[str, JsonValue]]:
    """Project executable module members without retaining names or private values."""
    members: list[dict[str, JsonValue]] = []
    module_members = cast("Mapping[str, object]", vars(module))
    for name, value in sorted(module_members.items()):
        raw_value: object = value
        if not isinstance(
            value,
            FunctionType
            | BuiltinFunctionType
            | MethodType
            | type
            | classmethod
            | staticmethod
            | property,
        ):
            continue
        value_identity = id(raw_value)
        value_projection = None if cache is None else cache.get(value_identity)
        if value_projection is None:
            value_projection = _stable_import_state(raw_value)
            if cache is not None:
                cache[value_identity] = value_projection
        members.append(
            {
                "name_sha256": hashlib.sha256(name.encode()).hexdigest(),
                "value": value_projection,
            },
        )
    return members


def _projection_import_origin_paths(module: ModuleType) -> set[Path]:
    """Read module origins without iterating a lazy namespace path."""
    origins: set[Path] = set()
    module_state = vars(module)
    raw_file = module_state.get("__file__")
    if isinstance(raw_file, str):
        origins.add(Path(raw_file))
    spec = module_state.get("__spec__")
    raw_origin = getattr(spec, "origin", None)
    if isinstance(raw_origin, str) and raw_origin not in {"built-in", "frozen"}:
        origins.add(Path(raw_origin))
    raw_search: object = getattr(spec, "submodule_search_locations", None)
    if isinstance(raw_search, list | tuple):
        search_items = cast("Sequence[object]", raw_search)
    else:
        raw_search_state: object = getattr(raw_search, "__dict__", None)
        stable_search = (
            cast("Mapping[object, object]", raw_search_state).get("_path")
            if isinstance(raw_search_state, dict)
            else None
        )
        search_items = (
            cast("Sequence[object]", stable_search)
            if isinstance(stable_search, list | tuple)
            else ()
        )
    origins.update(Path(item) for item in search_items if isinstance(item, str))
    return origins


def _loaded_module_projection() -> list[dict[str, JsonValue]]:
    """Bind every loaded module entry, including negative and originless entries."""
    result: list[dict[str, JsonValue]] = []
    executable_cache: dict[int, JsonValue] = {}
    loaded_modules = cast("Mapping[str, object]", sys.modules)
    for module_name, module in sorted(loaded_modules.items()):
        projected: dict[str, JsonValue] = {
            "name_sha256": hashlib.sha256(module_name.encode()).hexdigest(),
            "present": module is not None,
        }
        if isinstance(module, ModuleType):
            projected.update(
                {
                    "executable_members": _module_execution_projection(
                        module,
                        executable_cache,
                    ),
                    "module_type": (f"{type(module).__module__}.{type(module).__qualname__}"),
                    "origins": [
                        _loaded_origin_projection(module_name, origin)
                        for origin in sorted(
                            _projection_import_origin_paths(module),
                            key=os.fspath,
                        )
                    ],
                },
            )
        elif module is not None:
            projected["value"] = _stable_import_state(module)
        result.append(projected)
    return result


def _loaded_origin_projection(
    module_name: str,
    origin: Path,
) -> dict[str, JsonValue]:
    raw = os.fspath(origin)
    projected: dict[str, JsonValue] = {
        "module_name_sha256": hashlib.sha256(module_name.encode()).hexdigest(),
        "path_sha256": hashlib.sha256(os.fsencode(origin)).hexdigest(),
    }
    if raw.startswith("<") and raw.endswith(">"):
        projected["file_backed"] = False
        return projected
    absolute = origin if origin.is_absolute() else Path.cwd() / origin
    try:
        metadata = absolute.lstat()
    except OSError:
        projected["exists"] = False
        return projected
    projected.update(
        {
            "change_time_ns": metadata.st_ctime_ns,
            "device": metadata.st_dev,
            "exists": True,
            "file_backed": stat.S_ISREG(metadata.st_mode),
            "inode": metadata.st_ino,
            "mode": metadata.st_mode,
            "modified_time_ns": metadata.st_mtime_ns,
            "owner": metadata.st_uid,
            "resolved_sha256": hashlib.sha256(
                os.fsencode(absolute.resolve(strict=True)),
            ).hexdigest(),
            "size": metadata.st_size,
            "symlink": stat.S_ISLNK(metadata.st_mode),
        },
    )
    if stat.S_ISREG(metadata.st_mode):
        projected["content_sha256"] = hashlib.sha256(absolute.read_bytes()).hexdigest()
    return projected


def _loaded_module_origin_projection() -> list[dict[str, JsonValue]]:
    result: list[dict[str, JsonValue]] = []
    loaded_modules = cast("Mapping[str, object]", sys.modules)
    for module_name, module in sorted(loaded_modules.items()):
        if not isinstance(module, ModuleType):
            continue
        result.extend(
            _loaded_origin_projection(module_name, origin)
            for origin in sorted(_projection_import_origin_paths(module), key=os.fspath)
        )
    return result


def _interpreter_flag_projection() -> dict[str, JsonValue]:
    return {
        name: cast("JsonValue", getattr(sys.flags, name))
        for name in (
            "bytes_warning",
            "debug",
            "dev_mode",
            "dont_write_bytecode",
            "hash_randomization",
            "ignore_environment",
            "inspect",
            "interactive",
            "isolated",
            "int_max_str_digits",
            "no_site",
            "no_user_site",
            "optimize",
            "quiet",
            "safe_path",
            "utf8_mode",
            "verbose",
            "warn_default_encoding",
        )
    }


def _interpreter_xoptions() -> dict[str, object]:
    raw_xoptions: object = vars(sys).get("_xoptions", {})
    if not isinstance(raw_xoptions, Mapping):
        return {}
    return {
        key: item
        for key, item in cast("Mapping[object, object]", raw_xoptions).items()
        if isinstance(key, str)
    }


def _official_interpreter_flags() -> dict[str, JsonValue]:
    return {
        "bytes_warning": 0,
        "debug": 0,
        "dev_mode": False,
        "dont_write_bytecode": 1,
        "hash_randomization": 1,
        "ignore_environment": 1,
        "inspect": 0,
        "interactive": 0,
        "isolated": 1,
        "int_max_str_digits": _CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS,
        "no_site": 1,
        "no_user_site": 1,
        "optimize": 0,
        "quiet": 0,
        "safe_path": True,
        "utf8_mode": 0,
        "verbose": 0,
        "warn_default_encoding": 0,
    }


def _interpreter_execution_projection() -> dict[str, JsonValue]:
    """Bind all behavior-changing interpreter and import execution state."""
    importer_cache: list[dict[str, JsonValue]] = []
    for raw_path, finder in sorted(sys.path_importer_cache.items()):
        importer_cache.append(
            {
                "finder": _stable_import_state(finder),
                "path": _execution_path_projection(raw_path),
            },
        )
    return {
        "flags": _interpreter_flag_projection(),
        "importable_suffixes": sorted(
            {
                *importlib.machinery.SOURCE_SUFFIXES,
                *importlib.machinery.BYTECODE_SUFFIXES,
                *importlib.machinery.EXTENSION_SUFFIXES,
            },
        ),
        "loaded_modules": _loaded_module_projection(),
        "loaded_module_origins": _loaded_module_origin_projection(),
        "meta_path": [_stable_import_state(item) for item in sys.meta_path],
        "path_hooks": [_stable_import_state(item) for item in sys.path_hooks],
        "path_importer_cache": importer_cache,
        "runtime_controls": {
            "dont_write_bytecode": sys.dont_write_bytecode,
            "int_max_str_digits": sys.get_int_max_str_digits(),
            "pycache_prefix": _stable_import_state(sys.pycache_prefix),
        },
        "sys_path": list(_execution_search_directory_snapshot()),
        "xoptions": _stable_import_state(dict(sorted(_interpreter_xoptions().items()))),
    }


def _interpreter_policy_sha256() -> str:
    """Return the deterministic policy sealed into every candidate manifest."""
    return _digest(
        {
            "allowed_importer_cache_types": (
                "_frozen_importlib_external.FileFinder",
                "zipimport.zipimporter",
                "none",
            ),
            "allowed_meta_path": (
                "_frozen_importlib.BuiltinImporter",
                "_frozen_importlib.FrozenImporter",
                "_frozen_importlib_external.PathFinder",
            ),
            "allowed_path_hooks": (
                "importlib._bootstrap_external.FileFinder.path_hook[beartype]",
                "_frozen_importlib_external.FileFinder.path_hook",
                "zipimport.zipimporter",
            ),
            "flags": _official_interpreter_flags(),
            "importable_suffixes": sorted(_IMPORTABLE_ARTIFACT_SUFFIXES),
            "runtime_controls": {
                "dont_write_bytecode": True,
                "int_max_str_digits": _CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS,
                "pycache_prefix": "isolated_runtime/.saxo-bank-mcp-pycache",
            },
            "path_importer_cache": (
                "canonical_beartype_file_finder",
                "missing_sys_path_none",
                "sealed_zipimporter",
            ),
            "xoptions": ("pycache_prefix",),
        },
    )


def _import_callable_name(value: object) -> tuple[str, str]:
    return (
        str(getattr(value, "__module__", type(value).__module__)),
        str(getattr(value, "__qualname__", type(value).__qualname__)),
    )


def _normalize_official_import_machinery() -> None:
    """Remove the one dependency-installed compatibility adapter before execution."""
    if os.environ.get(_OFFICIAL_LAUNCHER_ENV) != "1":
        return
    key_value_module = sys.modules.get("key_value")
    deprecated_finder = getattr(key_value_module, "_DeprecatedModuleFinder", None)
    if not isinstance(deprecated_finder, type):
        return
    sys.meta_path[:] = [item for item in sys.meta_path if type(item) is not deprecated_finder]


def _official_runtime_root() -> Path:
    """Resolve the venv root without `site`, which owns prefix handling on 3.12."""
    executable = Path(sys.executable).absolute()
    runtime_root = executable.parent.parent
    configuration = runtime_root / "pyvenv.cfg"
    if executable.parent.name != "bin" or not configuration.is_file() or configuration.is_symlink():
        raise ValueError("source matrix isolated runtime root is unavailable")
    return runtime_root


def _validate_owner_only_runtime() -> None:
    prefix = _official_runtime_root()
    prefix_metadata = prefix.lstat()
    if (
        not stat.S_ISDIR(prefix_metadata.st_mode)
        or prefix_metadata.st_uid != os.getuid()
        or stat.S_IMODE(prefix_metadata.st_mode) & _OWNER_ONLY_MASK
    ):
        raise ValueError("source matrix isolated runtime is not owner-only")
    for path in prefix.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & _OWNER_ONLY_MASK:
            raise ValueError("source matrix isolated runtime is not owner-only")
    for launcher_name in _OFFICIAL_LAUNCHER_NAMES:
        launcher = prefix / "bin" / launcher_name
        metadata = launcher.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != _OWNER_DIRECTORY_MODE
        ):
            raise ValueError("source matrix isolated launcher mode is invalid")


def _file_finder_hook_details(  # noqa: PLR0911
    hook: object,
) -> tuple[object, tuple[tuple[object, tuple[str, ...]], ...]] | None:
    raw_closure_object: object = getattr(hook, "__closure__", None)
    if not isinstance(raw_closure_object, tuple):
        return None
    raw_closure = cast("tuple[object, ...]", raw_closure_object)
    if len(raw_closure) != _IMPORT_CLOSURE_CELL_COUNT:
        return None
    closure = cast("tuple[CellType, CellType]", raw_closure)
    try:
        finder_type = cast("object", closure[0].cell_contents)
        raw_details = cast("object", closure[1].cell_contents)
    except ValueError:
        return None
    if not isinstance(raw_details, tuple):
        return None
    detail_items = cast("tuple[object, ...]", raw_details)
    details: list[tuple[object, tuple[str, ...]]] = []
    for raw_detail_object in detail_items:
        if not isinstance(raw_detail_object, tuple):
            return None
        raw_detail = cast("tuple[object, ...]", raw_detail_object)
        raw_suffixes = raw_detail[1] if len(raw_detail) == _IMPORT_LOADER_DETAIL_COUNT else None
        if (
            len(raw_detail) != _IMPORT_LOADER_DETAIL_COUNT
            or not isinstance(raw_suffixes, Sequence)
            or isinstance(raw_suffixes, str)
        ):
            return None
        suffix_items = cast("Sequence[object]", raw_suffixes)
        if any(not isinstance(suffix, str) for suffix in suffix_items):
            return None
        details.append(
            (
                raw_detail[0],
                tuple(cast("str", suffix) for suffix in suffix_items),
            ),
        )
    return finder_type, tuple(details)


def _official_file_finder_details() -> tuple[
    tuple[tuple[object, tuple[str, ...]], ...],
    tuple[tuple[object, tuple[str, ...]], ...],
]:
    beartype_module = sys.modules.get("beartype.claw._importlib._clawimpload")
    beartype_loader = getattr(beartype_module, "BeartypeSourceFileLoader", None)
    if not isinstance(beartype_loader, type):
        raise TypeError("source matrix sealed source loader is unavailable")
    extension = (
        importlib.machinery.ExtensionFileLoader,
        tuple(importlib.machinery.EXTENSION_SUFFIXES),
    )
    bytecode = (
        importlib.machinery.SourcelessFileLoader,
        tuple(importlib.machinery.BYTECODE_SUFFIXES),
    )
    beartype_details = (
        extension,
        (beartype_loader, tuple(importlib.machinery.SOURCE_SUFFIXES)),
        bytecode,
    )
    standard_details = (
        extension,
        (
            importlib.machinery.SourceFileLoader,
            tuple(importlib.machinery.SOURCE_SUFFIXES),
        ),
        bytecode,
    )
    return beartype_details, standard_details


def _validate_official_import_machinery() -> None:  # noqa: C901, PLR0912
    """Reject every import hook, finder, loader, cache path, and cache-state deviation."""
    if sys.meta_path != [
        importlib.machinery.BuiltinImporter,
        importlib.machinery.FrozenImporter,
        importlib.machinery.PathFinder,
    ]:
        raise ValueError("source matrix meta path is unsupported")
    beartype_details, standard_details = _official_file_finder_details()
    expected_hooks = (
        ("zipimport", "zipimporter", None),
        (
            "importlib._bootstrap_external",
            "FileFinder.path_hook.<locals>.path_hook_for_FileFinder",
            (importlib.machinery.FileFinder, beartype_details),
        ),
        (
            "_frozen_importlib_external",
            "FileFinder.path_hook.<locals>.path_hook_for_FileFinder",
            (importlib.machinery.FileFinder, standard_details),
        ),
    )
    if len(sys.path_hooks) != len(expected_hooks):
        raise ValueError("source matrix path hooks are unsupported")
    for hook, (expected_module, expected_qualname, expected_details) in zip(
        sys.path_hooks,
        expected_hooks,
        strict=True,
    ):
        if _import_callable_name(hook) != (expected_module, expected_qualname) or (
            hook is not zipimport.zipimporter
            if expected_details is None
            else _file_finder_hook_details(hook) != expected_details
        ):
            raise ValueError("source matrix path hooks are unsupported")

    search_roots = tuple(Path(value).absolute() for value in sys.path)
    flattened_loaders = [
        (suffix, loader) for loader, suffixes in beartype_details for suffix in suffixes
    ]
    for raw_path, finder in cast(
        "Mapping[object, object]",
        sys.path_importer_cache,
    ).items():
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise TypeError("source matrix importer cache key is unsupported")
        path = Path(raw_path).absolute()
        if not any(path == root or path.is_relative_to(root) for root in search_roots):
            raise ValueError("source matrix importer cache path is unsupported")
        if finder is None:
            if raw_path not in sys.path or path.exists() or path.is_symlink():
                raise ValueError("source matrix importer cache state is unsupported")
            continue
        if type(finder) is zipimport.zipimporter:
            if raw_path not in sys.path or not path.is_file() or path.is_symlink():
                raise ValueError("source matrix zip importer cache is unsupported")
            if Path(str(getattr(finder, "archive", ""))).absolute() != path:
                raise ValueError("source matrix zip importer cache is unsupported")
            continue
        if type(finder) is not importlib.machinery.FileFinder:
            raise ValueError("source matrix importer cache finder is unsupported")
        if (
            getattr(finder, "path", None) != raw_path
            or getattr(finder, "_loaders", None) != flattened_loaders
            or not path.is_dir()
            or path.is_symlink()
        ):
            raise ValueError("source matrix file finder cache is unsupported")
        metadata = path.stat()
        entries = {item.name for item in path.iterdir()}
        relaxed_entries: set[str] = (
            {name.casefold() for name in entries}
            if sys.platform.startswith(("win", "cygwin", "darwin"))
            else set()
        )
        if (
            getattr(finder, "_path_mtime", None) != metadata.st_mtime
            or getattr(finder, "_path_cache", None) != entries
            or getattr(finder, "_relaxed_path_cache", None) != relaxed_entries
        ):
            raise ValueError("source matrix file finder cache state is unsupported")


def _validate_official_interpreter_state() -> None:  # noqa: C901
    """Require the exact isolated CPython/import policy established by the launcher."""
    if os.environ.get(_OFFICIAL_LAUNCHER_ENV) != "1":
        raise ValueError("source matrix official isolated launcher is required")
    observed_flags = _interpreter_flag_projection()
    if observed_flags != _official_interpreter_flags():
        raise ValueError("source matrix interpreter flags are unsafe")
    xoptions = _interpreter_xoptions()
    if set(xoptions) != {"pycache_prefix"}:
        raise ValueError("source matrix interpreter xoptions are unsafe")
    raw_cache_prefix = xoptions.get("pycache_prefix")
    if not isinstance(raw_cache_prefix, str):
        raise TypeError("source matrix interpreter cache prefix is unavailable")
    cache_prefix = Path(raw_cache_prefix)
    expected_cache_prefix = _official_runtime_root() / ".saxo-bank-mcp-pycache"
    if sys.dont_write_bytecode is not True:
        raise ValueError("source matrix interpreter bytecode policy is unsafe")
    if sys.pycache_prefix != raw_cache_prefix:
        raise ValueError("source matrix interpreter cache prefix state is unsafe")
    if (
        sys.int_info.default_max_str_digits != _CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS
        or sys.get_int_max_str_digits() != _CPYTHON_312_DEFAULT_INT_MAX_STR_DIGITS
    ):
        raise ValueError("source matrix interpreter integer conversion limit is unsafe")
    if (
        not cache_prefix.is_absolute()
        or cache_prefix != expected_cache_prefix
        or cache_prefix.is_symlink()
        or not cache_prefix.is_dir()
    ):
        raise ValueError("source matrix interpreter cache prefix is unsafe")
    cache_metadata = cache_prefix.lstat()
    if (
        cache_metadata.st_uid != os.getuid()
        or stat.S_IMODE(cache_metadata.st_mode) != _OWNER_DIRECTORY_MODE
        or any(cache_prefix.iterdir())
    ):
        raise ValueError("source matrix interpreter cache prefix is unsafe")

    if any(not value or not Path(value).is_absolute() for value in sys.path):
        raise ValueError("source matrix import search path is unsupported")
    _validate_official_import_machinery()
    _validate_owner_only_runtime()


def _inactive_runtime_cache_allowed(path: Path) -> bool:  # noqa: C901, PLR0911, PLR0912
    launcher_mode = os.environ.get(_OFFICIAL_LAUNCHER_ENV)
    if launcher_mode not in {"1", "dev"}:
        if sys.flags.dont_write_bytecode != 1:
            return False
        cache_entry = (
            path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_BYTECODE_SUFFIXES
        )
        if not cache_entry:
            return False
        base_executable = Path(
            str(getattr(sys, "_base_executable", sys.executable)),
        ).resolve(strict=True)
        original_base_root = base_executable.parent.parent
        try:
            path.absolute().relative_to(Path(sys.base_prefix).absolute())
        except ValueError:
            pass
        else:
            if Path(sys.base_prefix).resolve(strict=True) == original_base_root:
                return True
        for raw_root in sys.path:
            root = Path.cwd() if raw_root == "" else Path(raw_root)
            if root.name not in {"site-packages", "dist-packages"}:
                continue
            try:
                path.absolute().relative_to(root.absolute())
            except ValueError:
                continue
            return True
        return False
    if sys.flags.isolated != 1 or sys.flags.dont_write_bytecode != 1 or sys.flags.no_site != 1:
        return False
    raw_cache_prefix = _interpreter_xoptions().get("pycache_prefix")
    if not isinstance(raw_cache_prefix, str):
        return False
    if launcher_mode == "dev":
        return path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_BYTECODE_SUFFIXES
    try:
        _official_runtime_root()
    except ValueError:
        return False
    try:
        path.absolute().relative_to(Path(sys.base_prefix).absolute())
    except ValueError:
        return False
    return path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_BYTECODE_SUFFIXES


def _execution_root_projection_sha256() -> str:
    """Seal import roots and import machinery without publishing private paths."""
    return _digest(
        {
            "cwd": _execution_path_projection(os.fspath(Path.cwd())),
            "executable_path_sha256": hashlib.sha256(os.fsencode(sys.executable)).hexdigest(),
            "base_executable_path_sha256": hashlib.sha256(
                os.fsencode(str(getattr(sys, "_base_executable", sys.executable))),
            ).hexdigest(),
            "prefix_sha256s": [
                hashlib.sha256(os.fsencode(str(value))).hexdigest()
                for value in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)
            ],
            "interpreter": _interpreter_execution_projection(),
        },
    )


def _stabilized_execution_root_projection_sha256() -> str:
    previous = _execution_root_projection_sha256()
    for _ in range(_EXECUTION_PROJECTION_STABILIZATION_LIMIT):
        current = _execution_root_projection_sha256()
        if current == previous:
            return current
        previous = current
    raise ValueError("source matrix execution closure does not stabilize")


def _runtime_file_projection(path: Path) -> str:
    """Hash one required runtime file without retaining its private path."""
    if path.is_symlink():
        raise ValueError("source matrix runtime file is a symlink")
    if not path.is_file():
        raise ValueError("source matrix runtime file is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_linked_file_projection(path: Path) -> tuple[str, str]:
    """Attest the regular-file target while normalizing equivalent launcher aliases."""
    current = path.absolute()
    seen: set[Path] = set()
    while current.is_symlink():
        if current in seen:
            raise ValueError("source matrix runtime file link cycle")
        seen.add(current)
        target = current.readlink()
        current = target if target.is_absolute() else current.parent / target
        current = current.absolute()
    resolved = current.resolve(strict=True)
    file_sha256 = _runtime_file_projection(resolved)
    return (
        file_sha256,
        _digest(
            {
                "mode": stat.S_IMODE(resolved.stat().st_mode),
                "resolved_file_sha256": file_sha256,
            },
        ),
    )


def _runtime_tree_entries(root: Path) -> tuple[dict[str, str], set[Path]]:
    root_metadata = _validated_closure_entry_metadata(root, scope="runtime tree")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("source matrix runtime tree is unavailable")
    entries: dict[str, str] = {}
    paths: set[Path] = set()
    for path in sorted(root.rglob("*")):
        if _inactive_runtime_cache_allowed(path):
            continue
        metadata = _validated_closure_entry_metadata(path, scope="runtime tree")
        relative_path = path.relative_to(root)
        if any(part in _RUNTIME_TREE_EXCLUDED_PARTS for part in relative_path.parts):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        relative = relative_path.as_posix()
        entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        paths.add(path.resolve(strict=True))
    return entries, paths


def _runtime_tree_projection(root: Path) -> tuple[str, int]:
    """Return a deterministic Merkle projection for runtime source and extensions."""
    entries, _paths = _runtime_tree_entries(root)
    if not entries:
        raise ValueError("source matrix runtime tree projection is empty")
    return _digest(entries), len(entries)


def _import_search_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw_value in sys.path:
        raw = Path.cwd() if raw_value == "" else Path(raw_value)
        path = raw if raw.is_absolute() else Path.cwd() / raw
        if not path.exists():
            continue
        metadata = _validated_closure_entry_metadata(path, scope="import search root")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("source matrix file-based import root is unsupported")
        resolved = path.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return tuple(roots)


def _runtime_location_projection(path: Path) -> str:
    resolved = path.resolve(strict=True)
    try:
        runtime_root = _official_runtime_root().resolve(strict=True)
        relative = resolved.relative_to(runtime_root)
    except ValueError:
        return hashlib.sha256(os.fsencode(resolved)).hexdigest()
    return _digest(
        {
            "relative_path": relative.as_posix(),
            "scope": "isolated_runtime",
        },
    )


def _startup_configuration_projection() -> tuple[str, int]:  # noqa: C901, PLR0912, PLR0915
    entries: list[dict[str, str]] = []
    observed: set[Path] = set()
    roots = _import_search_roots()
    for root in roots:
        allowed_symlinks = _expected_interpreter_aliases(root)
        _validate_directory_entry_tree(
            root,
            scope="startup search directory",
            allowed_symlinks=allowed_symlinks,
        )
        for child in sorted(root.iterdir()):
            if _inactive_runtime_cache_allowed(child):
                continue
            child_metadata = _validated_closure_entry_metadata(
                child,
                scope="startup search directory",
                allowed_symlinks=allowed_symlinks,
            )
            startup_package_name = child.name in {"sitecustomize", "usercustomize"}
            startup_module_file = any(
                child.name == f"{module_name}{suffix}"
                for module_name in ("sitecustomize", "usercustomize")
                for suffix in _IMPORTABLE_ARTIFACT_SUFFIXES
            )
            startup_file = (
                child.name in _STARTUP_CONFIGURATION_NAMES
                or child.name.endswith(_STARTUP_CONFIGURATION_SUFFIXES)
                or startup_module_file
            )
            if not startup_package_name and not startup_file:
                continue
            startup_package = startup_package_name and stat.S_ISDIR(child_metadata.st_mode)
            if not startup_package and not startup_file:
                continue
            resolved = child.resolve(strict=True)
            if resolved in observed:
                continue
            observed.add(resolved)
            root_sha256 = _runtime_location_projection(root)
            if stat.S_ISDIR(child_metadata.st_mode):
                tree_sha256, file_count = _runtime_tree_projection(child)
                entries.append(
                    {
                        "file_count": str(file_count),
                        "name_sha256": hashlib.sha256(child.name.encode()).hexdigest(),
                        "root_sha256": root_sha256,
                        "sha256": tree_sha256,
                        "type": "package",
                    },
                )
            elif stat.S_ISREG(child_metadata.st_mode):
                entries.append(
                    {
                        "file_count": "1",
                        "name_sha256": hashlib.sha256(child.name.encode()).hexdigest(),
                        "root_sha256": root_sha256,
                        "sha256": _runtime_file_projection(child),
                        "type": "file",
                    },
                )
            else:
                raise ValueError("source matrix startup configuration type is invalid")
    executable_configuration_roots: set[Path] = set()
    for executable in (
        Path(sys.executable),
        Path(str(getattr(sys, "_base_executable", sys.executable))),
    ):
        root = executable.absolute().parent
        if root.is_symlink():
            raise ValueError("source matrix executable configuration root is a symlink")
        if root.is_dir():
            executable_configuration_roots.add(root.resolve(strict=True))
    for root in sorted(executable_configuration_roots):
        for child in sorted(root.iterdir()):
            if not child.name.endswith("._pth"):
                continue
            child_metadata = _validated_closure_entry_metadata(
                child,
                scope="executable configuration directory",
            )
            if not stat.S_ISREG(child_metadata.st_mode):
                raise ValueError("source matrix path configuration type is invalid")
            resolved = child.resolve(strict=True)
            if resolved in observed:
                continue
            observed.add(resolved)
            entries.append(
                {
                    "file_count": "1",
                    "name_sha256": hashlib.sha256(child.name.encode()).hexdigest(),
                    "root_sha256": _runtime_location_projection(root),
                    "sha256": _runtime_file_projection(child),
                    "type": "file",
                },
            )
    for candidate in (
        Path(sys.prefix) / "pyvenv.cfg",
        Path(sys.executable).absolute().parent.parent / "pyvenv.cfg",
    ):
        if candidate.is_symlink():
            raise ValueError("source matrix path configuration is a symlink")
        if not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        if resolved in observed:
            continue
        observed.add(resolved)
        entries.append(
            {
                "file_count": "1",
                "name_sha256": hashlib.sha256(candidate.name.encode()).hexdigest(),
                "root_sha256": _runtime_location_projection(candidate.parent),
                "sha256": _runtime_file_projection(candidate),
                "type": "file",
            },
        )
    ordered = sorted(entries, key=lambda item: json.dumps(item, sort_keys=True))
    return _digest(ordered), sum(int(item["file_count"]) for item in ordered)


def _python_shared_runtime_projection() -> tuple[str, int]:
    raw_enabled = sysconfig.get_config_var("Py_ENABLE_SHARED")
    enabled = raw_enabled in {1, "1"}
    candidates: set[Path] = set()
    library_name = sysconfig.get_config_var("LDLIBRARY")
    if isinstance(library_name, str) and library_name:
        for directory_name in ("LIBDIR", "BINDIR"):
            raw_directory = sysconfig.get_config_var(directory_name)
            if not isinstance(raw_directory, str) or not raw_directory:
                continue
            candidate = Path(raw_directory) / library_name
            if candidate.exists():
                candidates.add(candidate.absolute())
    if enabled and not candidates:
        raise ValueError("source matrix shared Python runtime is unavailable")
    projections = sorted(_runtime_linked_file_projection(path)[1] for path in candidates)
    return _digest(projections), len(projections)


def _runtime_identity() -> _RuntimeIdentity:
    executable = Path(sys.executable)
    if not executable.is_file():
        raise ValueError("source matrix interpreter is unavailable")
    cache_tag = sys.implementation.cache_tag
    if not cache_tag:
        raise ValueError("source matrix interpreter cache tag is unavailable")
    executable_sha256, executable_projection_sha256 = _runtime_linked_file_projection(executable)
    base_executable = Path(getattr(sys, "_base_executable", sys.executable))
    base_executable_sha256 = _runtime_linked_file_projection(base_executable)[0]
    raw_stdlib = sysconfig.get_path("stdlib")
    raw_platstdlib = sysconfig.get_config_var("DESTSHARED")
    if not raw_stdlib or not isinstance(raw_platstdlib, str) or not raw_platstdlib:
        raise ValueError("source matrix standard library roots are unavailable")
    stdlib_root = Path(raw_stdlib)
    platstdlib_root = Path(raw_platstdlib)
    stdlib_merkle_sha256, stdlib_file_count = _runtime_tree_projection(stdlib_root)
    platstdlib_merkle_sha256, platstdlib_file_count = _runtime_tree_projection(
        platstdlib_root,
    )
    shared_runtime_sha256, shared_runtime_file_count = _python_shared_runtime_projection()
    startup_sha256, startup_file_count = _startup_configuration_projection()
    build_config = {
        key: hashlib.sha256(str(sysconfig.get_config_var(key)).encode()).hexdigest()
        for key in _PYTHON_BUILD_CONFIG_KEYS
    }
    return _RuntimeIdentity(
        implementation=sys.implementation.name,
        cache_tag=cache_tag,
        python_version=platform.python_version(),
        platform=platform.platform(),
        executable_sha256=executable_sha256,
        executable_projection_sha256=executable_projection_sha256,
        base_executable_sha256=base_executable_sha256,
        python_build_sha256=_digest(platform.python_build()),
        build_config_sha256=_digest(build_config),
        stdlib_merkle_sha256=stdlib_merkle_sha256,
        stdlib_file_count=stdlib_file_count,
        platstdlib_merkle_sha256=platstdlib_merkle_sha256,
        platstdlib_file_count=platstdlib_file_count,
        shared_runtime_sha256=shared_runtime_sha256,
        shared_runtime_file_count=shared_runtime_file_count,
        startup_configuration_sha256=startup_sha256,
        startup_configuration_file_count=startup_file_count,
        interpreter_policy_sha256=_interpreter_policy_sha256(),
        importable_suffixes=tuple(sorted(_IMPORTABLE_ARTIFACT_SUFFIXES)),
    )


def _dependency_distributions(  # noqa: C901, PLR0912, PLR0915
) -> dict[str, _DependencyDistribution]:
    """Seal the recursively selected runtime dependency distributions."""
    try:
        root = distribution("saxo-bank-mcp")
    except PackageNotFoundError as error:
        raise ValueError("installed source matrix distribution is unavailable") from error
    _validate_directory_entry_tree(
        Path(str(root.locate_file(""))),
        scope="dependency installation directory",
    )
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
            metadata = _validated_closure_entry_metadata(
                path,
                scope=f"dependency {name}",
            )
            if ".dist-info/" in relative and Path(relative).name in _INSTALLER_GENERATED_METADATA:
                if Path(relative).name == "INSTALLER":
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError(f"source matrix dependency {name} installer is invalid")
                    normalized = path.read_text(encoding="utf-8").strip().casefold()
                    installer_metadata["INSTALLER"] = hashlib.sha256(
                        normalized.encode(),
                    ).hexdigest()
                continue
            if not _safe_artifact_path(relative) or not stat.S_ISREG(metadata.st_mode):
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
    if installer is None:
        raise ValueError("installed source matrix installer projection is unavailable")
    metadata = _validated_closure_entry_metadata(
        installer,
        scope="installed package metadata",
    )
    if not stat.S_ISREG(metadata.st_mode):
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


def _add_import_projection(
    *,
    relative: str,
    located: Path,
    allowed_files: set[Path],
    import_roots: dict[str, set[Path]],
) -> None:
    relative_path = Path(relative)
    if (
        ".." in relative_path.parts
        or any(part.endswith(".dist-info") for part in relative_path.parts)
        or not relative_path.parts
    ):
        return
    metadata = _validated_closure_entry_metadata(located, scope="recorded import file")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("source matrix recorded import file is unavailable")
    resolved = located.resolve(strict=True)
    allowed_files.add(resolved)
    first = relative_path.parts[0]
    module_name = first if len(relative_path.parts) > 1 else first.split(".", 1)[0]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_name) is None:
        return
    import_root = (
        resolved
        if len(relative_path.parts) == 1
        else resolved.parents[len(relative_path.parts) - 2]
    )
    import_roots.setdefault(module_name, set()).add(import_root)


def _scan_recorded_import_root(
    root: Path,
    *,
    allowed_files: set[Path],
    allowed_unsealed_files: set[Path],
) -> None:
    root_metadata = _validated_closure_entry_metadata(root, scope="recorded import root")
    if not stat.S_ISREG(root_metadata.st_mode) and not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("source matrix recorded import root type is invalid")
    candidates = (root,) if stat.S_ISREG(root_metadata.st_mode) else root.rglob("*")
    for path in candidates:
        metadata = (
            root_metadata
            if path == root
            else _validated_closure_entry_metadata(path, scope="recorded import root")
        )
        if not stat.S_ISREG(metadata.st_mode):
            continue
        resolved = path.resolve(strict=True)
        if resolved not in allowed_files and resolved not in allowed_unsealed_files:
            raise ValueError("unrecorded importable artifact is present")


def _module_candidates(root: Path, module_name: str) -> set[Path]:
    directory_candidate = root / module_name
    candidates = {
        root / f"{module_name}.py",
        root / f"{module_name}.pyi",
    }
    if directory_candidate.is_dir() or directory_candidate.is_symlink():
        candidates.add(directory_candidate)
    candidates.update(
        item
        for item in root.glob(f"{module_name}.*")
        if item.suffix.casefold() in _IMPORTABLE_ARTIFACT_SUFFIXES
    )
    return {item for item in candidates if item.exists() or item.is_symlink()}


def _nonimportable_namespace_projection(root: Path) -> bool:
    """Allow inert editable-install data roots while rejecting executable shadows."""
    root_metadata = _validated_closure_entry_metadata(root, scope="namespace projection")
    if not stat.S_ISDIR(root_metadata.st_mode):
        return False
    for path in root.rglob("*"):
        metadata = _validated_closure_entry_metadata(path, scope="namespace projection")
        if stat.S_ISREG(metadata.st_mode) and path.suffix.casefold() in (
            _IMPORTABLE_ARTIFACT_SUFFIXES
        ):
            return False
    return True


def _source_package_projection(
    root: Path,
    expected: Mapping[str, str],
) -> tuple[bool, set[Path]]:
    root_metadata = _validated_closure_entry_metadata(root, scope="source projection")
    if not stat.S_ISDIR(root_metadata.st_mode):
        return False, set()
    observed: dict[str, str] = {}
    paths: set[Path] = set()
    for path in sorted(root.rglob("*")):
        metadata = _validated_closure_entry_metadata(path, scope="source projection")
        if not stat.S_ISREG(metadata.st_mode):
            continue
        relative = f"src/saxo_bank_mcp/{path.relative_to(root).as_posix()}"
        observed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        paths.add(path.resolve(strict=True))
    return observed == dict(expected), paths


def _import_origin_paths(module: object) -> set[Path]:
    origins: set[Path] = set()
    raw_file = getattr(module, "__file__", None)
    if isinstance(raw_file, str):
        origins.add(Path(raw_file))
    spec = getattr(module, "__spec__", None)
    raw_origin = getattr(spec, "origin", None)
    if isinstance(raw_origin, str) and raw_origin not in {"built-in", "frozen"}:
        origins.add(Path(raw_origin))
    raw_search = getattr(spec, "submodule_search_locations", None)
    if raw_search is not None:
        origins.update(Path(item) for item in raw_search)
    return origins


def _validate_import_execution_closure(  # noqa: C901, PLR0912, PLR0915
    manifest: _SourceMatrixCandidateManifest,
    *,
    source_repository_root: Path | None,
) -> None:
    allowed_files: set[Path] = set()
    allowed_unsealed_files: set[Path] = set()
    import_roots: dict[str, set[Path]] = {}
    roots_to_scan: set[Path] = set()
    source_expected = {
        name: sha256
        for name, sha256 in manifest.source_files.items()
        if name.startswith("src/saxo_bank_mcp/")
    }
    if source_repository_root is not None:
        source_package = source_repository_root / "src" / "saxo_bank_mcp"
        matched, source_paths = _source_package_projection(source_package, source_expected)
        if not matched:
            raise ValueError("source matrix source import projection mismatch")
        allowed_files.update(source_paths)
        import_roots.setdefault("saxo_bank_mcp", set()).add(
            source_package.resolve(strict=True),
        )
        roots_to_scan.add(source_package.resolve(strict=True))
    else:
        installed_distribution = distribution("saxo-bank-mcp")
        for item in installed_distribution.files or ():
            relative = str(item).replace(os.sep, "/")
            located = Path(str(installed_distribution.locate_file(item)))
            if relative in manifest.installed_files:
                _add_import_projection(
                    relative=relative,
                    located=located,
                    allowed_files=allowed_files,
                    import_roots=import_roots,
                )
            elif relative in manifest.installed_exclusions and ".dist-info/" not in relative:
                allowed_unsealed_files.add(located.resolve(strict=True))
    for name, expected_distribution in manifest.dependency_distributions.items():
        dependency = distribution(name)
        for relative in expected_distribution.files:
            _add_import_projection(
                relative=relative,
                located=Path(str(dependency.locate_file(relative))),
                allowed_files=allowed_files,
                import_roots=import_roots,
            )
    for roots in import_roots.values():
        roots_to_scan.update(roots)

    raw_stdlib = sysconfig.get_path("stdlib")
    raw_platstdlib = sysconfig.get_config_var("DESTSHARED")
    if not raw_stdlib or not isinstance(raw_platstdlib, str) or not raw_platstdlib:
        raise ValueError("source matrix standard library roots are unavailable")
    stdlib_root = Path(raw_stdlib).resolve(strict=True)
    platstdlib_root = Path(raw_platstdlib).resolve(strict=True)
    _stdlib_entries, stdlib_files = _runtime_tree_entries(stdlib_root)
    _platstdlib_entries, platstdlib_files = _runtime_tree_entries(platstdlib_root)
    runtime_files = stdlib_files | platstdlib_files
    runtime_roots = {stdlib_root, platstdlib_root}
    non_file_stdlib_modules: set[str] = set()
    for module_name in sys.stdlib_module_names:
        roots = import_roots.setdefault(module_name, set())
        try:
            stdlib_spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            stdlib_spec = None
        if stdlib_spec is not None and stdlib_spec.origin in {"built-in", "frozen"}:
            non_file_stdlib_modules.add(module_name)
        for runtime_root in runtime_roots:
            for candidate in _module_candidates(runtime_root, module_name):
                roots.add(candidate.resolve(strict=True))

    search_roots = _import_search_roots()
    for search_root in search_roots:
        for module_name, allowed_roots in import_roots.items():
            if module_name in non_file_stdlib_modules:
                continue
            for candidate in _module_candidates(search_root, module_name):
                if candidate.is_symlink():
                    raise ValueError("source matrix import projection is a symlink")
                resolved = candidate.resolve(strict=True)
                if resolved in allowed_roots:
                    continue
                if module_name == "saxo_bank_mcp":
                    matched, source_paths = _source_package_projection(candidate, source_expected)
                    if matched:
                        allowed_roots.add(resolved)
                        allowed_files.update(source_paths)
                        roots_to_scan.add(resolved)
                        continue
                    if _nonimportable_namespace_projection(candidate):
                        continue
                raise ValueError(f"import root {module_name} is shadowed")

    for root in roots_to_scan:
        _scan_recorded_import_root(
            root,
            allowed_files=allowed_files,
            allowed_unsealed_files=allowed_unsealed_files,
        )
    for module_name, roots in import_roots.items():
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError) as error:
            raise ValueError(f"recorded import root {module_name} is unavailable") from error
        if spec is None and module_name not in sys.stdlib_module_names:
            raise ValueError(f"recorded import root {module_name} is unavailable")
        if spec is None:
            continue
        origins: set[Path] = set()
        if isinstance(spec.origin, str) and spec.origin not in {"built-in", "frozen"}:
            origins.add(Path(spec.origin))
        if spec.submodule_search_locations is not None:
            origins.update(
                Path(item) for item in cast("Sequence[str]", spec.submodule_search_locations)
            )
        if any(
            origin.is_symlink()
            or not any(
                origin.resolve(strict=True) == root
                or origin.resolve(strict=True).is_relative_to(root)
                for root in roots
            )
            for origin in origins
        ):
            raise ValueError(f"import root {module_name} is shadowed")
    for module_name, module in tuple(sys.modules.items()):
        top_level = module_name.partition(".")[0]
        for origin in _import_origin_paths(module):
            if origin.is_symlink():
                raise ValueError(f"imported module {module_name} uses a symlink")
            resolved = origin.resolve(strict=True)
            if top_level in sys.stdlib_module_names:
                if resolved.is_file() and resolved not in runtime_files:
                    raise ValueError(f"imported standard-library module {module_name} is unsealed")
                if not any(
                    resolved == root or resolved.is_relative_to(root) for root in runtime_roots
                ):
                    raise ValueError(f"imported standard-library module {module_name} is shadowed")
                continue
            if top_level not in import_roots:
                if (
                    resolved.is_file()
                    and resolved not in allowed_files
                    and resolved not in runtime_files
                ):
                    raise ValueError(f"imported module {module_name} is unsealed")
                if resolved.is_dir() and not any(
                    resolved == root or resolved.is_relative_to(root)
                    for root in (
                        *runtime_roots,
                        *(item for roots in import_roots.values() for item in roots),
                    )
                ):
                    raise ValueError(f"imported module {module_name} is unsealed")
                continue
            if resolved.is_file() and resolved not in allowed_files:
                raise ValueError(f"imported module {module_name} is unrecorded")
            if not any(
                resolved == root or resolved.is_relative_to(root)
                for root in import_roots[top_level]
            ):
                raise ValueError(f"imported module {module_name} is shadowed")


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
    _validate_directory_entry_tree(
        Path(str(installed_distribution.locate_file(""))),
        scope="installed package directory",
    )
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
        normalized_name = (
            f"../../../{name}"
            if name in {f"bin/{launcher_name}" for launcher_name in _OFFICIAL_LAUNCHER_NAMES}
            else name
        )
        path = Path(str(installed_distribution.locate_file(package_path)))
        metadata = _validated_closure_entry_metadata(
            path,
            scope="installed package",
        )
        if _installer_entrypoint_projection(name, console_scripts):
            if not _installer_entrypoint_projection_valid(
                path,
                console_scripts[path.name],
            ):
                raise ValueError("installed console entry point projection is invalid")
            continue
        if not _safe_artifact_path(name) and not _official_launcher_record_path(
            normalized_name,
        ):
            raise ValueError("installed source matrix path is invalid")
        if ".dist-info/" in name and Path(name).name in _INSTALLER_GENERATED_METADATA:
            continue
        if name in excluded:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("installed source matrix exclusion is unavailable")
            observed_exclusions.add(name)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("installed source matrix file is unavailable")
        result[normalized_name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed_exclusions != excluded:
        raise ValueError("installed source matrix exclusions are incomplete")
    return dict(sorted(result.items()))


def _installer_entrypoint_projection(
    value: str,
    console_scripts: Mapping[str, str],
) -> bool:
    path = Path(value)
    return path.name in console_scripts and "bin" in path.parts and value not in console_scripts


def _installer_entrypoint_projection_valid(  # noqa: PLR0911
    path: Path,
    target: str,
    *,
    interpreter: Path | None = None,
) -> bool:
    try:
        metadata = _validated_closure_entry_metadata(
            path,
            scope="installed console entry point",
        )
    except ValueError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o111 == 0:
        return False
    target_parts = target.split(":")
    if (
        len(target_parts) != _ENTRYPOINT_TARGET_PART_COUNT
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", target_parts[0]) is None
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_parts[1]) is None
    ):
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
    if not lines:
        return False
    first_line = lines[0]
    body_lines = lines[1:]
    if (
        first_line == "#!/bin/sh"
        and len(lines) >= _PORTABLE_ENTRYPOINT_MIN_LINES
        and lines[1].startswith("'''exec' '")
        and lines[1].endswith('\' "$0" "$@"')
        and lines[2] == "' '''"
    ):
        raw_interpreter = (
            lines[1]
            .removeprefix("'''exec' '")
            .removesuffix(
                '\' "$0" "$@"',
            )
        )
        shebang_interpreter = Path(raw_interpreter)
        body_lines = lines[3:]
    elif first_line.startswith("#!/"):
        shebang_interpreter = Path(first_line.removeprefix("#!"))
    else:
        return False
    return (
        shebang_interpreter.resolve(strict=True) == expected_interpreter.resolve(strict=True)
        and "\n".join(body_lines) + "\n" == expected_body
    )


def _safe_artifact_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _official_launcher_record_path(value: str) -> bool:
    return value in {f"../../../bin/{launcher_name}" for launcher_name in _OFFICIAL_LAUNCHER_NAMES}


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


def _open_state_guard_parent(
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
    *,
    before_link: Callable[[], bool] | None = None,
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
        if before_link is not None and before_link() is not True:
            raise _ExecutionClosureChangedError
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


def _publish_claimed_failure(
    claim: _ClaimedGuardDirectory,
    reason: str,
    *,
    before_link: Callable[[], bool] | None = None,
) -> _PublishedEvidence | None:
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
    return _publish_claimed_text(claim, text, before_link=before_link)


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
    try:
        _normalize_official_import_machinery()
        _validate_official_interpreter_state()
        identity = source_matrix_candidate_identity()
    except (OSError, ValidationError, ValueError):
        return 1
    if arguments.identity:
        sys.stdout.write(identity.candidate_identity_sha256 + "\n")
        return 0
    if arguments.preflight:
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
    return execute_analytics_source_matrix_once(
        fixtures=SourceMatrixFixtures(
            account_key=os.environ.get("SAXO_MCP_QA_ACCOUNT_KEY", ""),
            client_key=os.environ.get("SAXO_MCP_QA_CLIENT_KEY", ""),
            instrument_uic=arguments.instrument_uic,
            asset_type=arguments.asset_type,
            option_root_id=arguments.option_root_id,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
