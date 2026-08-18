"""Privacy-safe partial provenance for failed Codex-native analytics proof children."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.auth_status import EffectiveReadEnvironment, EnvironmentName
from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt

type CodexNativeProofPhase = Literal[
    "before_preflight",
    "sim_preflight",
    "agent_evaluation",
    "offline_proof",
    "sim_matrix",
    "bundle_validation",
    "cleanup",
    "complete",
]
type SimPreflightStatus = Literal["not_started", "passed", "blocked", "unknown"]
type CleanupStatus = Literal["not_started", "in_progress", "complete", "failed", "unknown"]
type FailureEvidenceStatus = Literal[
    "authenticated",
    "bootstrap_authenticated",
    "missing",
    "malformed",
    "tampered",
    "crashed",
    "cleanup_failed",
]
type BootstrapEvidenceStatus = Literal[
    "authenticated",
    "missing",
    "malformed",
    "tampered",
    "unsafe",
]
type BootstrapState = Literal[
    "entered",
    "producer_imported",
    "producer_started",
    "failed",
    "complete",
]
type BootstrapPhase = Literal[
    "entry",
    "producer_import",
    "producer_handoff",
    "producer_execution",
]
type OuterRuntimeCleanupStatus = Literal["complete", "failed", "unknown"]

_PHASES: Final[tuple[CodexNativeProofPhase, ...]] = (
    "before_preflight",
    "sim_preflight",
    "agent_evaluation",
    "offline_proof",
    "sim_matrix",
    "bundle_validation",
    "cleanup",
    "complete",
)
_SAFE_REASON_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_REASON_PREFIXES: Final = ("installed_", "native_", "proof_")
_SAFE_EVAL_CASE_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_SAFE_LOGICAL_TOOL_ID_PATTERN: Final = re.compile(r"^saxo_[a-z0-9_]{1,95}$")
_BOOTSTRAP_PHASES: Final[tuple[BootstrapPhase, ...]] = (
    "entry",
    "producer_import",
    "producer_handoff",
    "producer_execution",
)
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_COMMIT_PATTERN = r"^[a-f0-9]{40}$"
_OWNER_FILE_MODE: Final = 0o600
_OWNER_DIRECTORY_MODE: Final = 0o700
_MAX_BOOTSTRAP_BYTES: Final = 32_768


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class CodexNativeSimPreflightReceipt(_StrictModel):
    """Redacted SIM session gate completed before native proof work."""

    status: Literal["passed", "blocked"]
    requested_environment: EnvironmentName
    effective_read_environment: EffectiveReadEnvironment
    live_reads: bool
    live_writes: Literal[False]
    capabilities_status: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    http_status: int | None = Field(default=None, ge=100, le=599)
    network_call_made: bool | None
    session_capabilities_proven: bool
    redacted_publication: Literal[True] = True

    @model_validator(mode="after")
    def _validate_preflight(self) -> Self:
        if self.http_status is not None and self.network_call_made is not True:
            raise ValueError("native preflight HTTP provenance is inconsistent")
        if self.status == "passed":
            if (
                self.requested_environment != "SIM"
                or self.effective_read_environment != "SIM"
                or self.live_reads
                or self.live_writes
                or self.capabilities_status != "passed"
                or self.reason is not None
                or self.network_call_made is not True
                or not self.session_capabilities_proven
            ):
                raise ValueError("native preflight pass requires current SIM capabilities")
        elif self.session_capabilities_proven or self.capabilities_status == "passed":
            raise ValueError("blocked native preflight cannot prove session capabilities")
        return self


class CodexNativeAgentEvaluationCaseSummary(_StrictModel):
    """Allowlisted per-case facts from one validated failed eval report."""

    case_id: str = Field(pattern=_SAFE_EVAL_CASE_ID_PATTERN.pattern)
    status: Literal["passed", "failed", "skipped", "planned"]
    error: str = Field(max_length=128)
    assertion_status: Literal["passed", "failed", "not_required"]
    grant_status: Literal["passed", "failed", "not_required"]
    required_logical_tool_ids: tuple[str, ...] = Field(max_length=64)
    required_logical_tool_count: int = Field(ge=0, le=64)
    invoked_logical_tool_ids: tuple[str, ...] = Field(max_length=128)
    invoked_logical_tool_count: int = Field(ge=0, le=128)
    model_tool_event_count: int | None = Field(ge=0)
    model_command_event_count: int | None = Field(ge=0)
    model_mcp_event_count: int | None = Field(ge=0)
    model_saxo_event_count: int | None = Field(ge=0)
    plugin_list_exit_code: int | None = None
    plugin_list_stdout_schema_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    mcp_probe_stage: (
        Literal[
            "runtime_binding",
            "contract_validation",
            "command_start",
            "command_exit",
            "payload_parse",
            "tool_visibility",
            "complete",
        ]
        | None
    ) = None
    mcp_probe_exit_code: int | None = None
    mcp_probe_stdout_schema_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def _validate_allowlisted_case(self) -> Self:
        logical_ids = (*self.required_logical_tool_ids, *self.invoked_logical_tool_ids)
        if any(_SAFE_LOGICAL_TOOL_ID_PATTERN.fullmatch(tool_id) is None for tool_id in logical_ids):
            raise ValueError("agent evaluation summary logical tool id is unsafe")
        if len(set(self.required_logical_tool_ids)) != len(self.required_logical_tool_ids):
            raise ValueError("agent evaluation summary required tools must be unique")
        if self.required_logical_tool_count != len(self.required_logical_tool_ids):
            raise ValueError("agent evaluation summary required tool count differs")
        if self.invoked_logical_tool_count != len(self.invoked_logical_tool_ids):
            raise ValueError("agent evaluation summary invoked tool count differs")
        if self.error and _SAFE_REASON_PATTERN.fullmatch(self.error) is None:
            raise ValueError("agent evaluation summary error is unsafe")
        if self.status == "failed" and not self.error:
            raise ValueError("failed agent evaluation case requires a safe error")
        if self.status == "passed" and self.error:
            raise ValueError("passed agent evaluation case cannot carry an error")
        if (self.plugin_list_exit_code is None) != (self.plugin_list_stdout_schema_sha256 is None):
            raise ValueError("plugin list command evidence must be complete")
        _validate_mcp_probe_command_evidence(
            stage=self.mcp_probe_stage,
            exit_code=self.mcp_probe_exit_code,
            stdout_schema_sha256=self.mcp_probe_stdout_schema_sha256,
        )
        return self


def _validate_mcp_probe_command_evidence(
    *,
    stage: str | None,
    exit_code: int | None,
    stdout_schema_sha256: str | None,
) -> None:
    paired = (exit_code is None) == (stdout_schema_sha256 is None)
    completed_command = stage in {
        "command_exit",
        "payload_parse",
        "tool_visibility",
        "complete",
    }
    if not paired or (completed_command != (exit_code is not None)):
        raise ValueError("mcp probe command evidence must match its stage")
    if stage is None and exit_code is not None:
        raise ValueError("mcp probe command evidence requires a stage")
    if stage == "command_exit" and exit_code == 0:
        raise ValueError("failed mcp probe command must have a nonzero exit")
    if stage in {"payload_parse", "tool_visibility", "complete"} and exit_code != 0:
        raise ValueError("completed mcp probe command must have exit zero")


class CodexNativeAgentEvaluationFailureSummary(_StrictModel):
    """Authenticated redacted summary retained before failed eval cleanup."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_agent_evaluation_failure_summary"] = (
        "codex_native_agent_evaluation_failure_summary"
    )
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_status: Literal["failed"] = "failed"
    case_count: int = Field(ge=1, le=64)
    failed_case_count: int = Field(ge=0, le=64)
    cases: tuple[CodexNativeAgentEvaluationCaseSummary, ...] = Field(
        min_length=1,
        max_length=64,
    )
    summary_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_summary(self) -> Self:
        if self.case_count != len(self.cases):
            raise ValueError("agent evaluation summary case count differs")
        if self.failed_case_count != sum(case.status == "failed" for case in self.cases):
            raise ValueError("agent evaluation summary failed case count differs")
        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("agent evaluation summary case ids must be unique")
        material = self.model_dump(mode="json", exclude={"summary_sha256"})
        if self.summary_sha256 != _digest(material):
            raise ValueError("agent evaluation summary digest mismatch")
        return self


class CodexNativeBootstrapEnvelope(_StrictModel):
    """Durable stdlib startup evidence written before producer import."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_proof_bootstrap"] = "codex_native_proof_bootstrap"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    installed_cache_sha256: str = Field(pattern=_SHA256_PATTERN)
    bootstrap_module_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_module_sha256: str = Field(pattern=_SHA256_PATTERN)
    catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_binding_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    install_report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    bootstrap_state: BootstrapState
    completed_bootstrap_phases: tuple[BootstrapPhase, ...]
    current_bootstrap_phase: BootstrapPhase | Literal["complete"]
    child_exit_code: int | None
    sim_preflight_status: SimPreflightStatus
    network_call_made: bool | None
    model_event_count: int | None = Field(ge=0)
    mcp_event_count: int | None = Field(ge=0)
    saxo_event_count: int | None = Field(ge=0)
    execution_performed: bool | None
    broker_write_made: bool | None
    live_mutation_calls: int | None = Field(ge=0)
    purchase_occurred: bool | None
    disclaimer_response_made: bool | None
    child_cleanup_status: CleanupStatus
    child_remaining_process_count: int | None = Field(ge=0)
    child_remaining_process_group_count: int | None = Field(ge=0)
    reason: str = Field(pattern=r"^proof_bootstrap_[a-z0-9_]{1,104}$")
    redacted_publication: Literal[True] = True
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_bootstrap_state(self) -> Self:  # noqa: C901
        material = self.model_dump(mode="json", exclude={"envelope_sha256"})
        legacy_material = dict(material)
        if self.runtime_binding_sha256 is None and self.install_report_sha256 is None:
            legacy_material.pop("runtime_binding_sha256")
            legacy_material.pop("install_report_sha256")
        if self.envelope_sha256 not in {_digest(material), _digest(legacy_material)}:
            raise ValueError("bootstrap envelope digest mismatch")
        indexes = tuple(_BOOTSTRAP_PHASES.index(phase) for phase in self.completed_bootstrap_phases)
        if indexes != tuple(range(len(indexes))):
            raise ValueError("bootstrap phases must be an ordered prefix")
        expected_state: dict[BootstrapState, tuple[tuple[BootstrapPhase, ...], str]] = {
            "entered": (("entry",), "producer_import"),
            "producer_imported": (("entry", "producer_import"), "producer_handoff"),
            "producer_started": (
                ("entry", "producer_import", "producer_handoff"),
                "producer_execution",
            ),
            "complete": (_BOOTSTRAP_PHASES, "complete"),
            "failed": (self.completed_bootstrap_phases, self.current_bootstrap_phase),
        }
        phases, current = expected_state[self.bootstrap_state]
        if self.completed_bootstrap_phases != phases or self.current_bootstrap_phase != current:
            raise ValueError("bootstrap state and phases are inconsistent")
        if self.bootstrap_state in {"entered", "producer_imported", "producer_started"}:
            if self.child_exit_code is not None:
                raise ValueError("unfinished bootstrap cannot claim a child exit")
        elif self.bootstrap_state == "complete":
            if self.child_exit_code != 0 or self.reason != "proof_bootstrap_complete":
                raise ValueError("completed bootstrap requires a zero exit")
        elif self.child_exit_code is None or self.child_exit_code == 0:
            raise ValueError("failed bootstrap requires a nonzero exit")
        producer_started = "producer_handoff" in self.completed_bootstrap_phases
        inactive_values = (
            self.network_call_made,
            self.execution_performed,
            self.broker_write_made,
            self.purchase_occurred,
            self.disclaimer_response_made,
        )
        inactive_counts = (
            self.model_event_count,
            self.mcp_event_count,
            self.saxo_event_count,
            self.live_mutation_calls,
            self.child_remaining_process_count,
            self.child_remaining_process_group_count,
        )
        if not producer_started:
            if (
                self.sim_preflight_status != "not_started"
                or any(value is not False for value in inactive_values)
                or any(value != 0 for value in inactive_counts)
                or self.child_cleanup_status != "complete"
            ):
                raise ValueError("pre-handoff bootstrap must prove exact inactivity")
        elif (
            self.sim_preflight_status != "unknown"
            or any(value is not None for value in inactive_values)
            or any(value is not None for value in inactive_counts)
            or self.child_cleanup_status != "unknown"
        ):
            raise ValueError("post-handoff bootstrap outcomes must remain unknown")
        return self


@dataclass(frozen=True, slots=True)
class CodexNativeBootstrapVerification:
    """Parent-owned bootstrap file verification without retaining its path."""

    status: BootstrapEvidenceStatus
    envelope: CodexNativeBootstrapEnvelope | None

    def __post_init__(self) -> None:
        """Require one exact status-to-envelope relationship."""
        if (self.status == "authenticated") != (self.envelope is not None):
            raise ValueError("bootstrap verification state is inconsistent")


class CodexNativeChildFailureEnvelope(_StrictModel):
    """One redacted failure receipt emitted by the installed proof child."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_child_failure"] = "codex_native_child_failure"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    installed_cache_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_module_sha256: str = Field(pattern=_SHA256_PATTERN)
    catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_phases: tuple[CodexNativeProofPhase, ...]
    current_phase: CodexNativeProofPhase
    child_exit_code: int
    sim_preflight_status: SimPreflightStatus
    sim_preflight: CodexNativeSimPreflightReceipt | None
    network_call_made: bool | None
    model_event_count: int | None = Field(ge=0)
    mcp_event_count: int | None = Field(ge=0)
    saxo_event_count: int | None = Field(ge=0)
    agent_evaluation_failure_summary: CodexNativeAgentEvaluationFailureSummary | None = None
    execution_performed: bool | None
    broker_write_made: bool | None
    live_mutation_calls: int | None = Field(ge=0)
    purchase_occurred: bool | None
    disclaimer_response_made: bool | None
    child_cleanup_status: CleanupStatus
    child_remaining_process_count: int | None = Field(ge=0)
    child_remaining_process_group_count: int | None = Field(ge=0)
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    redacted_publication: Literal[True] = True
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_truth_state(self) -> Self:  # noqa: C901
        if self.child_exit_code == 0:
            raise ValueError("failure envelope requires nonzero child exit")
        indexes = tuple(_PHASES.index(phase) for phase in self.completed_phases)
        if indexes != tuple(sorted(set(indexes))):
            raise ValueError("completed proof phases must be unique and ordered")
        if indexes and indexes[-1] > _PHASES.index(self.current_phase):
            raise ValueError("completed proof phase cannot follow current phase")
        if self.sim_preflight_status == "not_started":
            if self.sim_preflight is not None or self.network_call_made is not False:
                raise ValueError("unstarted preflight cannot carry network evidence")
        elif self.sim_preflight_status in {"passed", "blocked"}:
            if self.sim_preflight is None or self.sim_preflight.status != self.sim_preflight_status:
                raise ValueError("preflight status requires its exact typed receipt")
            if self.network_call_made != self.sim_preflight.network_call_made:
                raise ValueError("preflight network provenance mismatch")
        elif self.sim_preflight is not None:
            raise ValueError("unknown preflight cannot carry a typed receipt")
        if (
            self.agent_evaluation_failure_summary is not None
            and self.current_phase != "agent_evaluation"
        ):
            raise ValueError("agent evaluation summary is out of phase")
        if self.execution_performed is False and (
            self.model_event_count != 0
            or self.mcp_event_count != 0
            or self.saxo_event_count != 0
            or self.broker_write_made is not False
            or self.live_mutation_calls != 0
            or self.purchase_occurred is not False
            or self.disclaimer_response_made is not False
        ):
            raise ValueError("unexecuted child cannot carry proof activity")
        return self


class CodexNativeVerifiedChildFailure(_StrictModel):
    """Parent-authenticated failure receipt suitable for redacted publication."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_verified_child_failure"] = (
        "codex_native_verified_child_failure"
    )
    status: Literal["refused"] = "refused"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    installed_cache_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_module_sha256: str = Field(pattern=_SHA256_PATTERN)
    catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    bootstrap_evidence_status: BootstrapEvidenceStatus
    bootstrap_authenticated: bool
    bootstrap_envelope: CodexNativeBootstrapEnvelope | None
    failure_evidence_status: FailureEvidenceStatus
    producer_authenticated: bool
    completed_phases: tuple[CodexNativeProofPhase, ...] | None
    current_phase: CodexNativeProofPhase | None
    child_exit_code: int | None
    sim_preflight_status: SimPreflightStatus
    sim_preflight: CodexNativeSimPreflightReceipt | None
    network_call_made: bool | None
    model_event_count: int | None = Field(ge=0)
    mcp_event_count: int | None = Field(ge=0)
    saxo_event_count: int | None = Field(ge=0)
    agent_evaluation_failure_summary: CodexNativeAgentEvaluationFailureSummary | None = None
    execution_performed: bool | None
    broker_write_made: bool | None
    live_mutation_calls: int | None = Field(ge=0)
    purchase_occurred: bool | None
    disclaimer_response_made: bool | None
    child_cleanup_status: CleanupStatus
    child_remaining_process_count: int | None = Field(ge=0)
    child_remaining_process_group_count: int | None = Field(ge=0)
    command_timed_out: bool
    command_cleanup_attempted: bool
    command_stdout_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_stderr_sha256: str = Field(pattern=_SHA256_PATTERN)
    outer_runtime_cleanup_status: OuterRuntimeCleanupStatus
    outer_remaining_process_count: int | None = Field(ge=0)
    outer_remaining_process_group_count: int | None = Field(ge=0)
    child_envelope_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    redacted_publication: Literal[True] = True
    failure_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_evidence_state(self) -> Self:
        trusted = self.failure_evidence_status in {"authenticated", "cleanup_failed"}
        if self.bootstrap_authenticated != (self.bootstrap_evidence_status == "authenticated"):
            raise ValueError("bootstrap authentication state is inconsistent")
        if self.bootstrap_authenticated != (self.bootstrap_envelope is not None):
            raise ValueError("bootstrap envelope state is inconsistent")
        if self.bootstrap_envelope is not None and (
            self.bootstrap_envelope.candidate_commit != self.candidate_commit
            or self.bootstrap_envelope.installed_cache_sha256 != self.installed_cache_sha256
            or self.bootstrap_envelope.producer_module_sha256 != self.producer_module_sha256
            or self.bootstrap_envelope.catalog_sha256 != self.catalog_sha256
            or self.bootstrap_envelope.contract_sha256 != self.contract_sha256
        ):
            raise ValueError("bootstrap and verified child bindings differ")
        if trusted and not self.bootstrap_authenticated:
            raise ValueError("producer evidence requires an authenticated bootstrap")
        if self.producer_authenticated != (
            self.failure_evidence_status in {"authenticated", "cleanup_failed"}
        ):
            raise ValueError("producer authentication state is inconsistent")
        if not trusted and any(
            value is not None
            for value in (
                self.completed_phases,
                self.current_phase,
                self.sim_preflight,
                self.network_call_made,
                self.model_event_count,
                self.mcp_event_count,
                self.saxo_event_count,
                self.agent_evaluation_failure_summary,
                self.execution_performed,
                self.broker_write_made,
                self.live_mutation_calls,
                self.purchase_occurred,
                self.disclaimer_response_made,
                self.child_remaining_process_count,
                self.child_remaining_process_group_count,
            )
        ):
            raise ValueError("untrusted failure evidence must publish unknown outcomes")
        if not trusted and self.sim_preflight_status != "unknown":
            raise ValueError("untrusted failure evidence must publish unknown preflight")
        return self


@dataclass(slots=True)
class CodexNativeProofProgress:
    """Mutable child-local tracker that only promotes validated observations."""

    candidate_commit: str
    installed_cache_sha256: str
    producer_module_sha256: str
    catalog_sha256: str
    contract_sha256: str
    completed_phases: list[CodexNativeProofPhase] = field(default_factory=list, init=False)
    current_phase: CodexNativeProofPhase = field(default="before_preflight", init=False)
    sim_preflight_status: SimPreflightStatus = field(default="not_started", init=False)
    sim_preflight: CodexNativeSimPreflightReceipt | None = field(default=None, init=False)
    network_call_made: bool | None = field(default=False, init=False)
    model_event_count: int | None = field(default=0, init=False)
    mcp_event_count: int | None = field(default=0, init=False)
    saxo_event_count: int | None = field(default=0, init=False)
    agent_evaluation_failure_summary: CodexNativeAgentEvaluationFailureSummary | None = field(
        default=None,
        init=False,
    )
    execution_performed: bool | None = field(default=False, init=False)
    broker_write_made: bool | None = field(default=False, init=False)
    live_mutation_calls: int | None = field(default=0, init=False)
    purchase_occurred: bool | None = field(default=False, init=False)
    disclaimer_response_made: bool | None = field(default=False, init=False)
    child_cleanup_status: CleanupStatus = field(default="not_started", init=False)
    child_remaining_process_count: int | None = field(default=None, init=False)
    child_remaining_process_group_count: int | None = field(default=None, init=False)
    _pre_matrix_mcp_event_count: int | None = field(default=None, init=False, repr=False)
    _pre_matrix_saxo_event_count: int | None = field(default=None, init=False, repr=False)

    def begin_phase(self, phase: CodexNativeProofPhase) -> None:
        if phase in {"before_preflight", "complete"}:
            raise ValueError("proof phase cannot be started explicitly")
        expected_index = len(self.completed_phases) + 1
        if _PHASES.index(phase) != expected_index:
            raise ValueError("proof phase sequence is invalid")
        if self.current_phase != "before_preflight" and (
            not self.completed_phases or self.completed_phases[-1] != self.current_phase
        ):
            raise ValueError("current proof phase is incomplete")
        self.current_phase = phase
        self.execution_performed = True
        if phase == "sim_preflight":
            self.sim_preflight_status = "unknown"
            self.network_call_made = None
            self.mcp_event_count = None
            self.saxo_event_count = None
        elif phase == "agent_evaluation":
            self.model_event_count = None
            self.mcp_event_count = None
            self.saxo_event_count = None
            self.agent_evaluation_failure_summary = None
            self._make_outcomes_unknown()
        elif phase == "sim_matrix":
            self._pre_matrix_mcp_event_count = self.mcp_event_count
            self._pre_matrix_saxo_event_count = self.saxo_event_count
            self.mcp_event_count = None
            self.saxo_event_count = None
            self._make_outcomes_unknown()
        elif phase == "cleanup":
            self.child_cleanup_status = "in_progress"

    def complete_phase(self, phase: CodexNativeProofPhase) -> None:
        if self.current_phase != phase or phase in self.completed_phases:
            raise ValueError("proof phase completion is invalid")
        self.completed_phases.append(phase)

    def record_preflight(self, receipt: CodexNativeSimPreflightReceipt) -> None:
        if self.current_phase != "sim_preflight":
            raise ValueError("SIM preflight receipt is out of phase")
        self.sim_preflight = receipt
        self.sim_preflight_status = receipt.status
        self.network_call_made = receipt.network_call_made
        self.model_event_count = 0
        self.mcp_event_count = 0 if receipt.capabilities_status == "not_called" else 1
        self.saxo_event_count = None if receipt.network_call_made else 0

    def record_agent_activity(
        self,
        *,
        model_event_count: int | None,
        mcp_event_count: int | None,
        saxo_event_count: int | None,
    ) -> None:
        if self.current_phase != "agent_evaluation":
            raise ValueError("agent activity is out of phase")
        self.model_event_count = _nonnegative_or_none(model_event_count)
        self.mcp_event_count = _add_known(1, _nonnegative_or_none(mcp_event_count))
        self.saxo_event_count = _add_known(None, _nonnegative_or_none(saxo_event_count))

    def record_agent_evaluation_failure(
        self,
        summary: CodexNativeAgentEvaluationFailureSummary,
    ) -> None:
        if self.current_phase != "agent_evaluation":
            raise ValueError("agent evaluation failure summary is out of phase")
        if self.agent_evaluation_failure_summary is not None:
            raise ValueError("agent evaluation failure summary is already recorded")
        self.agent_evaluation_failure_summary = summary

    def record_matrix(self, matrix: SimToolMatrixReceipt) -> None:
        if self.current_phase != "sim_matrix":
            raise ValueError("SIM matrix receipt is out of phase")
        matrix_mcp_events = len(matrix.tool_receipts) + len(matrix.analysis_execution_receipts)
        self.mcp_event_count = _add_known(
            self._pre_matrix_mcp_event_count,
            matrix_mcp_events,
        )
        matrix_saxo_events = sum(receipt.network_call_made for receipt in matrix.tool_receipts)
        matrix_saxo_events += sum(
            receipt.network_call_made for receipt in matrix.analysis_execution_receipts
        )
        self.saxo_event_count = _add_known(
            self._pre_matrix_saxo_event_count,
            matrix_saxo_events,
        )
        self.network_call_made = bool(self.network_call_made) or matrix_saxo_events > 0
        lifecycle = matrix.controlled_sim_lifecycle
        self.broker_write_made = bool(
            lifecycle and any(case.sim_mutation_call_count > 0 for case in lifecycle.cases)
        ) or any(receipt.broker_write_made for receipt in matrix.analysis_execution_receipts)
        self.live_mutation_calls = matrix.live_mutation_calls
        self.purchase_occurred = matrix.purchase_occurred
        self.disclaimer_response_made = matrix.disclaimer_response_made

    def record_cleanup(
        self,
        *,
        status: Literal["complete", "failed", "unknown"],
        remaining_process_count: int | None,
        remaining_process_group_count: int | None,
    ) -> None:
        if self.current_phase != "cleanup":
            raise ValueError("cleanup receipt is out of phase")
        self.child_cleanup_status = status
        self.child_remaining_process_count = _nonnegative_or_none(remaining_process_count)
        self.child_remaining_process_group_count = _nonnegative_or_none(
            remaining_process_group_count,
        )

    def _make_outcomes_unknown(self) -> None:
        self.broker_write_made = None
        self.live_mutation_calls = None
        self.purchase_occurred = None
        self.disclaimer_response_made = None


def verify_bootstrap_envelope_file(  # noqa: C901, PLR0911, PLR0912, PLR0913
    path: Path,
    *,
    expected_parent: Path,
    candidate_commit: str,
    installed_cache_sha256: str,
    bootstrap_module_sha256: str,
    producer_module_sha256: str,
    catalog_sha256: str,
    contract_sha256: str,
    child_exit_code: int | None,
    runtime_binding_sha256: str | None = None,
    install_report_sha256: str | None = None,
) -> CodexNativeBootstrapVerification:
    """Verify one owner-only startup file and discard its filesystem location."""
    try:
        parent_metadata = os.lstat(path.parent)
        path_metadata = os.lstat(path)
        parent_matches = path.parent.resolve(strict=True) == expected_parent.resolve(strict=True)
    except FileNotFoundError:
        return CodexNativeBootstrapVerification(status="missing", envelope=None)
    except OSError:
        return CodexNativeBootstrapVerification(status="unsafe", envelope=None)
    if not (
        path.is_absolute()
        and parent_matches
        and stat.S_ISDIR(parent_metadata.st_mode)
        and not stat.S_ISLNK(parent_metadata.st_mode)
        and parent_metadata.st_uid == os.getuid()
        and stat.S_IMODE(parent_metadata.st_mode) == _OWNER_DIRECTORY_MODE
        and stat.S_ISREG(path_metadata.st_mode)
        and not stat.S_ISLNK(path_metadata.st_mode)
        and path_metadata.st_uid == os.getuid()
        and path_metadata.st_nlink == 1
        and stat.S_IMODE(path_metadata.st_mode) == _OWNER_FILE_MODE
        and path_metadata.st_size <= _MAX_BOOTSTRAP_BYTES
    ):
        return CodexNativeBootstrapVerification(status="unsafe", envelope=None)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened_metadata = os.fstat(descriptor)
        if (
            opened_metadata.st_dev != path_metadata.st_dev
            or opened_metadata.st_ino != path_metadata.st_ino
        ):
            return CodexNativeBootstrapVerification(status="unsafe", envelope=None)
        raw = os.read(descriptor, _MAX_BOOTSTRAP_BYTES + 1)
    except OSError:
        return CodexNativeBootstrapVerification(status="unsafe", envelope=None)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > _MAX_BOOTSTRAP_BYTES:
        return CodexNativeBootstrapVerification(status="unsafe", envelope=None)
    try:
        text = raw.decode("utf-8")
        decoded_object: object = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return CodexNativeBootstrapVerification(status="malformed", envelope=None)
    if not isinstance(decoded_object, dict):
        return CodexNativeBootstrapVerification(status="malformed", envelope=None)
    decoded = cast("dict[str, object]", decoded_object)
    expected = {
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "installed_cache_sha256": installed_cache_sha256,
        "bootstrap_module_sha256": bootstrap_module_sha256,
        "producer_module_sha256": producer_module_sha256,
        "catalog_sha256": catalog_sha256,
        "contract_sha256": contract_sha256,
    }
    if runtime_binding_sha256 is not None:
        expected["runtime_binding_sha256"] = runtime_binding_sha256
    if install_report_sha256 is not None:
        expected["install_report_sha256"] = install_report_sha256
    if any(decoded.get(key) != value for key, value in expected.items()):
        return CodexNativeBootstrapVerification(status="tampered", envelope=None)
    claimed_digest = decoded.get("envelope_sha256")
    material = {key: value for key, value in decoded.items() if key != "envelope_sha256"}
    if not isinstance(claimed_digest, str) or claimed_digest != _digest(material):
        return CodexNativeBootstrapVerification(status="tampered", envelope=None)
    try:
        envelope = CodexNativeBootstrapEnvelope.model_validate_json(text, strict=True)
    except ValidationError:
        return CodexNativeBootstrapVerification(status="malformed", envelope=None)
    if envelope.child_exit_code is not None and envelope.child_exit_code != child_exit_code:
        return CodexNativeBootstrapVerification(status="tampered", envelope=None)
    return CodexNativeBootstrapVerification(status="authenticated", envelope=envelope)


def build_child_failure_envelope(
    progress: CodexNativeProofProgress,
    *,
    child_exit_code: int,
    reason: str,
) -> CodexNativeChildFailureEnvelope:
    """Bind one safe failure envelope to the child-local progress state."""
    material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_child_failure",
        "harness_policy": "codex_native_v1",
        "candidate_commit": progress.candidate_commit,
        "installed_cache_sha256": progress.installed_cache_sha256,
        "producer_module_sha256": progress.producer_module_sha256,
        "catalog_sha256": progress.catalog_sha256,
        "contract_sha256": progress.contract_sha256,
        "completed_phases": tuple(progress.completed_phases),
        "current_phase": progress.current_phase,
        "child_exit_code": child_exit_code,
        "sim_preflight_status": progress.sim_preflight_status,
        "sim_preflight": (
            progress.sim_preflight.model_dump(mode="json")
            if progress.sim_preflight is not None
            else None
        ),
        "network_call_made": progress.network_call_made,
        "model_event_count": progress.model_event_count,
        "mcp_event_count": progress.mcp_event_count,
        "saxo_event_count": progress.saxo_event_count,
        "agent_evaluation_failure_summary": (
            progress.agent_evaluation_failure_summary.model_dump(mode="python")
            if progress.agent_evaluation_failure_summary is not None
            else None
        ),
        "execution_performed": progress.execution_performed,
        "broker_write_made": progress.broker_write_made,
        "live_mutation_calls": progress.live_mutation_calls,
        "purchase_occurred": progress.purchase_occurred,
        "disclaimer_response_made": progress.disclaimer_response_made,
        "child_cleanup_status": progress.child_cleanup_status,
        "child_remaining_process_count": progress.child_remaining_process_count,
        "child_remaining_process_group_count": progress.child_remaining_process_group_count,
        "reason": safe_failure_reason(reason),
        "redacted_publication": True,
    }
    return CodexNativeChildFailureEnvelope.model_validate(
        {**material, "envelope_sha256": _digest(material)},
    )


def verify_child_failure_envelope(  # noqa: C901, PLR0912, PLR0913
    *,
    raw_stdout: str,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
    catalog_sha256: str,
    contract_sha256: str,
    child_exit_code: int | None,
    command_timed_out: bool,
    command_cleanup_attempted: bool,
    command_stdout_sha256: str,
    command_stderr_sha256: str,
    remaining_process_count: int | None,
    remaining_process_group_count: int | None,
    runtime_cleanup_status: OuterRuntimeCleanupStatus,
    bootstrap_verification: CodexNativeBootstrapVerification,
) -> CodexNativeVerifiedChildFailure:
    """Authenticate a failed child's one-line envelope or publish unknown facts."""
    expected = {
        "candidate_commit": candidate_commit,
        "installed_cache_sha256": installed_cache_sha256,
        "producer_module_sha256": producer_module_sha256,
        "catalog_sha256": catalog_sha256,
        "contract_sha256": contract_sha256,
    }
    actual_stdout_sha256 = hashlib.sha256(raw_stdout.encode()).hexdigest()
    envelope: CodexNativeChildFailureEnvelope | None = None
    status: FailureEvidenceStatus
    bootstrap_authenticated = bootstrap_verification.status == "authenticated"
    if not bootstrap_authenticated:
        status = cast(
            "FailureEvidenceStatus",
            (
                "tampered"
                if bootstrap_verification.status == "tampered"
                else "malformed"
                if bootstrap_verification.status in {"malformed", "unsafe"}
                else "missing"
            ),
        )
    elif command_timed_out or (child_exit_code is not None and child_exit_code < 0):
        status = "crashed"
    elif not raw_stdout:
        status = "missing"
    else:
        try:
            raw = json.loads(raw_stdout)
        except (TypeError, json.JSONDecodeError):
            raw = None
        if not isinstance(raw, dict):
            status = "malformed"
        elif all(key in raw for key in expected) and any(
            cast("dict[str, object]", raw).get(key) != value for key, value in expected.items()
        ):
            status = "tampered"
        else:
            try:
                envelope = CodexNativeChildFailureEnvelope.model_validate_json(
                    raw_stdout,
                    strict=True,
                )
            except ValidationError:
                status = "malformed"
            else:
                material = envelope.model_dump(mode="json", exclude={"envelope_sha256"})
                accepted_digests = {_digest(material)}
                if envelope.agent_evaluation_failure_summary is None:
                    legacy_material = dict(material)
                    legacy_material.pop("agent_evaluation_failure_summary")
                    accepted_digests.add(_digest(legacy_material))
                if (
                    envelope.envelope_sha256 not in accepted_digests
                    or envelope.child_exit_code != child_exit_code
                    or command_stdout_sha256 != actual_stdout_sha256
                ):
                    status = "tampered"
                    envelope = None
                else:
                    status = "authenticated"
    cleanup_complete = (
        runtime_cleanup_status == "complete"
        and command_cleanup_attempted
        and remaining_process_count == 0
        and remaining_process_group_count == 0
    )
    if envelope is not None and not cleanup_complete:
        status = "cleanup_failed"
    if status == "cleanup_failed" and envelope is not None:
        return _cleanup_failed_verified_failure(
            expected=expected,
            envelope=envelope,
            child_exit_code=child_exit_code,
            command_timed_out=command_timed_out,
            command_cleanup_attempted=command_cleanup_attempted,
            command_stdout_sha256=command_stdout_sha256,
            command_stderr_sha256=command_stderr_sha256,
            runtime_cleanup_status=runtime_cleanup_status,
            remaining_process_count=remaining_process_count,
            remaining_process_group_count=remaining_process_group_count,
            bootstrap_verification=bootstrap_verification,
        )
    if status != "authenticated":
        return _unknown_verified_failure(
            expected=expected,
            status=status,
            producer_authenticated=envelope is not None,
            child_exit_code=child_exit_code,
            command_timed_out=command_timed_out,
            command_cleanup_attempted=command_cleanup_attempted,
            command_stdout_sha256=command_stdout_sha256,
            command_stderr_sha256=command_stderr_sha256,
            runtime_cleanup_status=runtime_cleanup_status,
            remaining_process_count=remaining_process_count,
            remaining_process_group_count=remaining_process_group_count,
            child_envelope_sha256=envelope.envelope_sha256 if envelope is not None else None,
            bootstrap_verification=bootstrap_verification,
        )
    if envelope is None:
        raise AssertionError("authenticated child envelope is missing")
    material = _verified_material(
        expected=expected,
        failure_evidence_status="authenticated",
        producer_authenticated=True,
        completed_phases=envelope.completed_phases,
        current_phase=envelope.current_phase,
        child_exit_code=child_exit_code,
        sim_preflight_status=envelope.sim_preflight_status,
        sim_preflight=envelope.sim_preflight,
        network_call_made=envelope.network_call_made,
        model_event_count=envelope.model_event_count,
        mcp_event_count=envelope.mcp_event_count,
        saxo_event_count=envelope.saxo_event_count,
        agent_evaluation_failure_summary=envelope.agent_evaluation_failure_summary,
        execution_performed=envelope.execution_performed,
        broker_write_made=envelope.broker_write_made,
        live_mutation_calls=envelope.live_mutation_calls,
        purchase_occurred=envelope.purchase_occurred,
        disclaimer_response_made=envelope.disclaimer_response_made,
        child_cleanup_status=envelope.child_cleanup_status,
        child_remaining_process_count=envelope.child_remaining_process_count,
        child_remaining_process_group_count=envelope.child_remaining_process_group_count,
        command_timed_out=command_timed_out,
        command_cleanup_attempted=command_cleanup_attempted,
        command_stdout_sha256=command_stdout_sha256,
        command_stderr_sha256=command_stderr_sha256,
        runtime_cleanup_status=runtime_cleanup_status,
        remaining_process_count=remaining_process_count,
        remaining_process_group_count=remaining_process_group_count,
        child_envelope_sha256=envelope.envelope_sha256,
        reason=envelope.reason,
        bootstrap_verification=bootstrap_verification,
    )
    return CodexNativeVerifiedChildFailure.model_validate(
        {**material, "failure_receipt_sha256": _digest(material)},
    )


def safe_failure_reason(reason: str) -> str:
    return (
        reason
        if _SAFE_REASON_PATTERN.fullmatch(reason) is not None
        and reason.startswith(_SAFE_REASON_PREFIXES)
        else "proof_child_failed"
    )


def _unknown_verified_failure(  # noqa: PLR0913
    *,
    expected: dict[str, str],
    status: FailureEvidenceStatus,
    producer_authenticated: bool,
    child_exit_code: int | None,
    command_timed_out: bool,
    command_cleanup_attempted: bool,
    command_stdout_sha256: str,
    command_stderr_sha256: str,
    runtime_cleanup_status: OuterRuntimeCleanupStatus,
    remaining_process_count: int | None,
    remaining_process_group_count: int | None,
    child_envelope_sha256: str | None,
    bootstrap_verification: CodexNativeBootstrapVerification,
) -> CodexNativeVerifiedChildFailure:
    reason_by_status: dict[FailureEvidenceStatus, str] = {
        "authenticated": "proof_child_failed",
        "missing": "proof_child_failure_envelope_missing",
        "malformed": "proof_child_failure_envelope_malformed",
        "tampered": "proof_child_failure_envelope_tampered",
        "crashed": "proof_child_process_crashed",
        "cleanup_failed": "proof_child_cleanup_failed",
        "bootstrap_authenticated": "proof_child_failure_envelope_missing",
    }
    material = _verified_material(
        expected=expected,
        failure_evidence_status=status,
        producer_authenticated=producer_authenticated,
        completed_phases=None,
        current_phase=None,
        child_exit_code=child_exit_code,
        sim_preflight_status="unknown",
        sim_preflight=None,
        network_call_made=None,
        model_event_count=None,
        mcp_event_count=None,
        saxo_event_count=None,
        agent_evaluation_failure_summary=None,
        execution_performed=None,
        broker_write_made=None,
        live_mutation_calls=None,
        purchase_occurred=None,
        disclaimer_response_made=None,
        child_cleanup_status="unknown",
        child_remaining_process_count=None,
        child_remaining_process_group_count=None,
        command_timed_out=command_timed_out,
        command_cleanup_attempted=command_cleanup_attempted,
        command_stdout_sha256=command_stdout_sha256,
        command_stderr_sha256=command_stderr_sha256,
        runtime_cleanup_status=runtime_cleanup_status,
        remaining_process_count=remaining_process_count,
        remaining_process_group_count=remaining_process_group_count,
        child_envelope_sha256=child_envelope_sha256,
        reason=reason_by_status[status],
        bootstrap_verification=bootstrap_verification,
    )
    return CodexNativeVerifiedChildFailure.model_validate(
        {**material, "failure_receipt_sha256": _digest(material)},
    )


def _cleanup_failed_verified_failure(  # noqa: PLR0913
    *,
    expected: dict[str, str],
    envelope: CodexNativeChildFailureEnvelope,
    child_exit_code: int | None,
    command_timed_out: bool,
    command_cleanup_attempted: bool,
    command_stdout_sha256: str,
    command_stderr_sha256: str,
    runtime_cleanup_status: OuterRuntimeCleanupStatus,
    remaining_process_count: int | None,
    remaining_process_group_count: int | None,
    bootstrap_verification: CodexNativeBootstrapVerification,
) -> CodexNativeVerifiedChildFailure:
    """Keep phase and positive facts while making unfinished outcomes unknown."""
    material = _verified_material(
        expected=expected,
        failure_evidence_status="cleanup_failed",
        producer_authenticated=True,
        completed_phases=envelope.completed_phases,
        current_phase=envelope.current_phase,
        child_exit_code=child_exit_code,
        sim_preflight_status=envelope.sim_preflight_status,
        sim_preflight=envelope.sim_preflight,
        network_call_made=True if envelope.network_call_made is True else None,
        model_event_count=None,
        mcp_event_count=None,
        saxo_event_count=None,
        agent_evaluation_failure_summary=envelope.agent_evaluation_failure_summary,
        execution_performed=True if envelope.execution_performed is True else None,
        broker_write_made=True if envelope.broker_write_made is True else None,
        live_mutation_calls=(
            envelope.live_mutation_calls
            if envelope.live_mutation_calls is not None and envelope.live_mutation_calls > 0
            else None
        ),
        purchase_occurred=True if envelope.purchase_occurred is True else None,
        disclaimer_response_made=(True if envelope.disclaimer_response_made is True else None),
        child_cleanup_status=envelope.child_cleanup_status,
        child_remaining_process_count=envelope.child_remaining_process_count,
        child_remaining_process_group_count=envelope.child_remaining_process_group_count,
        command_timed_out=command_timed_out,
        command_cleanup_attempted=command_cleanup_attempted,
        command_stdout_sha256=command_stdout_sha256,
        command_stderr_sha256=command_stderr_sha256,
        runtime_cleanup_status=runtime_cleanup_status,
        remaining_process_count=remaining_process_count,
        remaining_process_group_count=remaining_process_group_count,
        child_envelope_sha256=envelope.envelope_sha256,
        reason="proof_child_cleanup_failed",
        bootstrap_verification=bootstrap_verification,
    )
    return CodexNativeVerifiedChildFailure.model_validate(
        {**material, "failure_receipt_sha256": _digest(material)},
    )


def _verified_material(  # noqa: PLR0913
    *,
    expected: dict[str, str],
    failure_evidence_status: FailureEvidenceStatus,
    producer_authenticated: bool,
    completed_phases: tuple[CodexNativeProofPhase, ...] | None,
    current_phase: CodexNativeProofPhase | None,
    child_exit_code: int | None,
    sim_preflight_status: SimPreflightStatus,
    sim_preflight: CodexNativeSimPreflightReceipt | None,
    network_call_made: bool | None,
    model_event_count: int | None,
    mcp_event_count: int | None,
    saxo_event_count: int | None,
    agent_evaluation_failure_summary: CodexNativeAgentEvaluationFailureSummary | None,
    execution_performed: bool | None,
    broker_write_made: bool | None,
    live_mutation_calls: int | None,
    purchase_occurred: bool | None,
    disclaimer_response_made: bool | None,
    child_cleanup_status: CleanupStatus,
    child_remaining_process_count: int | None,
    child_remaining_process_group_count: int | None,
    command_timed_out: bool,
    command_cleanup_attempted: bool,
    command_stdout_sha256: str,
    command_stderr_sha256: str,
    runtime_cleanup_status: OuterRuntimeCleanupStatus,
    remaining_process_count: int | None,
    remaining_process_group_count: int | None,
    child_envelope_sha256: str | None,
    reason: str,
    bootstrap_verification: CodexNativeBootstrapVerification,
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "receipt_kind": "codex_native_verified_child_failure",
        "status": "refused",
        "harness_policy": "codex_native_v1",
        **expected,
        "bootstrap_evidence_status": bootstrap_verification.status,
        "bootstrap_authenticated": bootstrap_verification.status == "authenticated",
        "bootstrap_envelope": (
            bootstrap_verification.envelope.model_dump(mode="python")
            if bootstrap_verification.envelope is not None
            else None
        ),
        "failure_evidence_status": failure_evidence_status,
        "producer_authenticated": producer_authenticated,
        "completed_phases": completed_phases,
        "current_phase": current_phase,
        "child_exit_code": child_exit_code,
        "sim_preflight_status": sim_preflight_status,
        "sim_preflight": sim_preflight.model_dump(mode="json") if sim_preflight else None,
        "network_call_made": network_call_made,
        "model_event_count": model_event_count,
        "mcp_event_count": mcp_event_count,
        "saxo_event_count": saxo_event_count,
        "agent_evaluation_failure_summary": (
            agent_evaluation_failure_summary.model_dump(mode="python")
            if agent_evaluation_failure_summary is not None
            else None
        ),
        "execution_performed": execution_performed,
        "broker_write_made": broker_write_made,
        "live_mutation_calls": live_mutation_calls,
        "purchase_occurred": purchase_occurred,
        "disclaimer_response_made": disclaimer_response_made,
        "child_cleanup_status": child_cleanup_status,
        "child_remaining_process_count": child_remaining_process_count,
        "child_remaining_process_group_count": child_remaining_process_group_count,
        "command_timed_out": command_timed_out,
        "command_cleanup_attempted": command_cleanup_attempted,
        "command_stdout_sha256": command_stdout_sha256,
        "command_stderr_sha256": command_stderr_sha256,
        "outer_runtime_cleanup_status": runtime_cleanup_status,
        "outer_remaining_process_count": remaining_process_count,
        "outer_remaining_process_group_count": remaining_process_group_count,
        "child_envelope_sha256": child_envelope_sha256,
        "reason": reason,
        "redacted_publication": True,
    }


def _digest(value: object) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(rendered).hexdigest()


def _nonnegative_or_none(value: int | None) -> int | None:
    if value is not None and value < 0:
        raise ValueError("event count cannot be negative")
    return value


def _add_known(left: int | None, right: int | None) -> int | None:
    return left + right if left is not None and right is not None else None
