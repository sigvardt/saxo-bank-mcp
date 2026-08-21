from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Thread as _OutputDrainThread
from types import MappingProxyType
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.subprocess_environment import preserve_parent_temp_environment

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
TERM_WAIT_SECONDS = 1.0
KILL_WAIT_SECONDS = 1.0
WATCHER_DRAIN_SECONDS = 1.0
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_OWNER_FILE_MODE = 0o600
_OWNER_DIRECTORY_MODE = 0o700
type CleanupIdentityEvidenceKind = Literal[
    "authenticated",
    "no-target-observed",
    "observation-unknown",
    "write-failed",
]
type WatcherDrainState = Literal[
    "not-applicable",
    "drained",
    "discarded",
    "still-running",
    "unknown",
]
type CleanupCoverage = Literal["complete", "unknown"]
type CleanupCoverageStage = Literal[
    "first_snapshot",
    "second_snapshot",
    "watcher_capture",
    "post_exit_snapshot",
    "final_snapshot",
    "group_table",
    "group_member",
    "target_observation",
]
type CleanupCoverageSubreason = Literal[
    "snapshot_failure",
    "capture_failure",
    "table_incomplete",
    "uncaptured_member",
    "changed_identity_or_group_member",
    "observation_unknown",
]
type CleanupObservationDetectionSource = Literal[
    "first_snapshot",
    "second_snapshot",
    "watcher_capture",
    "post_exit_snapshot",
    "final_snapshot",
    "pre_signal_group_scan",
    "post_signal_group_rescan",
    "historical_pid_check",
    "target_observation",
]
type CleanupObservationState = Literal[
    "running",
    "zombie",
    "absent",
    "unknown",
    "identity_changed",
    "group_changed",
]
type CleanupAdmissionPhase = Literal["admission_open", "admission_closed"]
type CleanupProcessCategory = Literal["process_observer"]
type CleanupObservationEvidenceState = Literal[
    "current",
    "legacy-current",
    "historical",
    "none",
]
type CleanupUnknownReason = Literal[
    "watcher_publication_discarded",
    "watcher_still_running",
    "watcher_drain_unknown",
    "coverage_unknown",
    "target_observation_unknown",
    "cleanup_state_unknown",
    "cleanup_receipt_path_missing",
    "cleanup_receipt_write_failed",
    "cleanup_evidence_inconsistent",
    "cleanup_evidence_unavailable",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ProcessCleanupTargetReceipt(_StrictModel):
    pid: int = Field(gt=0)
    pgid: int = Field(gt=0)
    birth_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_birth_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    terminal_state: Literal["running", "zombie", "absent", "identity_reused", "unknown"]
    termination_outcome: Literal[
        "no_longer_running",
        "non_executing_zombie",
        "identity_changed",
        "still_running",
        "unknown",
    ]

    @model_validator(mode="after")
    def _validate_semantic_outcome(self) -> Self:
        expected = {
            "absent": "no_longer_running",
            "zombie": "non_executing_zombie",
            "identity_reused": "identity_changed",
            "running": "still_running",
            "unknown": "unknown",
        }[self.terminal_state]
        if self.termination_outcome != expected:
            raise ValueError("cleanup terminal state and outcome differ")
        if self.terminal_state in {"absent", "unknown"}:
            if self.terminal_birth_identity_sha256 is not None:
                raise ValueError("cleanup terminal identity must be absent")
        elif self.terminal_birth_identity_sha256 is None:
            raise ValueError("cleanup terminal identity is required")
        elif self.terminal_state == "identity_reused":
            if self.terminal_birth_identity_sha256 == self.birth_identity_sha256:
                raise ValueError("cleanup reused identity must differ")
        elif self.terminal_birth_identity_sha256 != self.birth_identity_sha256:
            raise ValueError("cleanup same-birth terminal identity differs")
        return self


class ProcessCleanupOffendingObservation(_StrictModel):
    """One privacy-safe process observation that can only keep cleanup unknown."""

    pid: int = Field(gt=0)
    pgid: int = Field(gt=0)
    expected_pgid: int | None = Field(default=None, gt=0)
    ppid: int | None = Field(default=None, ge=0)
    birth_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    expected_birth_identity_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    detection_source: CleanupObservationDetectionSource
    observation_state: CleanupObservationState
    admission_phase: CleanupAdmissionPhase
    occurrence_count: int | None = Field(default=None, ge=1)
    process_category: CleanupProcessCategory | None = None
    process_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_private_identity(self) -> Self:  # noqa: C901
        if self.process_category is not None and self.process_identity_sha256 is not None:
            raise ValueError("cleanup process category and identity hash are exclusive")
        if self.observation_state not in {"absent", "unknown"}:
            if self.birth_identity_sha256 is None:
                raise ValueError("observed cleanup process requires a birth identity hash")
            if self.process_category is None and self.process_identity_sha256 is None:
                raise ValueError("observed cleanup process requires a safe process identity")
        correlation_fields = {
            "expected_pgid",
            "expected_birth_identity_sha256",
            "occurrence_count",
        }
        present_correlation_fields = correlation_fields & self.model_fields_set
        if present_correlation_fields and present_correlation_fields != correlation_fields:
            raise ValueError("cleanup observation correlation fields must be complete")
        if not present_correlation_fields:
            return self
        if self.expected_pgid is None or self.occurrence_count is None:
            raise ValueError("current cleanup observation requires an expected process group")
        if self.observation_state == "group_changed" and self.expected_pgid == self.pgid:
            raise ValueError("changed-group cleanup observation requires different groups")
        if self.observation_state == "identity_changed":
            if (
                self.expected_birth_identity_sha256 is None
                or self.birth_identity_sha256 is None
                or self.expected_birth_identity_sha256 == self.birth_identity_sha256
            ):
                raise ValueError("changed-identity cleanup observation requires different births")
            if self.expected_pgid != self.pgid:
                raise ValueError("changed-identity cleanup observation requires the same group")
        return self


@dataclass(frozen=True, slots=True)
class ProcessCleanupTerminalSnapshot:
    """One birth-bound terminal view used for cleanup status, receipt, and counts."""

    targets: tuple[ProcessCleanupTargetReceipt, ...]
    coverage_status: CleanupCoverage
    coverage_stage: CleanupCoverageStage | None = None
    coverage_subreason: CleanupCoverageSubreason | None = None
    signaled_process_count: int = 0
    watcher_drain_status: WatcherDrainState = "not-applicable"
    target_observation_unknown: bool = False
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...] = ()

    def __post_init__(self) -> None:
        """Require unknown coverage to carry one fixed diagnostic pair."""
        _require_cleanup_coverage_diagnostic(
            coverage_status=self.coverage_status,
            stage=self.coverage_stage,
            subreason=self.coverage_subreason,
        )
        if self.offending_observations and self.coverage_status != "unknown":
            raise ValueError("cleanup offending observations require unknown coverage")

    @property
    def cleanup_status(self) -> Literal["complete", "failed", "unknown"]:
        if self.watcher_drain_status in {"discarded", "still-running", "unknown"}:
            return "unknown"
        if (
            self.target_observation_unknown
            or self.coverage_status == "unknown"
            or any(target.termination_outcome == "unknown" for target in self.targets)
        ):
            return "unknown"
        if any(target.termination_outcome == "still_running" for target in self.targets):
            return "failed"
        return "complete"

    @property
    def remaining_process_count(self) -> int | None:
        if self.cleanup_status == "unknown":
            return None
        return sum(target.termination_outcome == "still_running" for target in self.targets)

    @property
    def remaining_process_group_count(self) -> int | None:
        if self.cleanup_status == "unknown":
            return None
        return len(
            {
                target.pgid
                for target in self.targets
                if target.termination_outcome == "still_running"
            },
        )

    @property
    def unknown_reason(self) -> CleanupUnknownReason | None:
        if self.cleanup_status != "unknown":
            return None
        return _cleanup_unknown_reason(
            watcher_drain_status=self.watcher_drain_status,
            coverage_status=self.coverage_status,
            target_observation_unknown=(
                self.target_observation_unknown
                or any(target.termination_outcome == "unknown" for target in self.targets)
            ),
        )


class CommandCleanupIdentityReceipt(_StrictModel):
    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["command_cleanup_identity"] = "command_cleanup_identity"
    command_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    root_pid: int = Field(gt=0)
    root_pgid: int = Field(gt=0)
    target_count: int = Field(ge=1)
    targets: tuple[ProcessCleanupTargetReceipt, ...] = Field(min_length=1)
    watcher_drain_status: Literal["not-applicable", "drained"] = "not-applicable"
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        if self.target_count != len(self.targets):
            raise ValueError("cleanup identity target count differs")
        if len({target.pid for target in self.targets}) != len(self.targets):
            raise ValueError("cleanup identity target pids must be unique")
        if any(target.termination_outcome == "unknown" for target in self.targets):
            raise ValueError("authenticated cleanup receipt cannot contain unknown targets")
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _digest(material):
            raise ValueError("cleanup identity receipt digest mismatch")
        return self

    @property
    def remaining_process_count(self) -> int:
        """Derive candidate survivors only from authenticated semantic outcomes."""
        return sum(target.termination_outcome == "still_running" for target in self.targets)

    @property
    def remaining_process_group_count(self) -> int:
        """Derive candidate groups without a second raw PID or PGID liveness query."""
        return len(
            {
                target.pgid
                for target in self.targets
                if target.termination_outcome == "still_running"
            },
        )

    @property
    def cleanup_status(self) -> Literal["complete", "failed"]:
        return "failed" if self.remaining_process_count else "complete"


class CommandCleanupUnknownReceipt(_StrictModel):
    """Path-free diagnostic receipt for one fail-closed cleanup result."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["command_cleanup_unknown"] = "command_cleanup_unknown"
    command_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    root_pid: int = Field(gt=0)
    root_pgid: int = Field(gt=0)
    unknown_reason: CleanupUnknownReason
    watcher_drain_status: WatcherDrainState
    coverage_status: Literal["complete", "unknown"]
    coverage_stage: CleanupCoverageStage | None = None
    coverage_subreason: CleanupCoverageSubreason | None = None
    target_count: int = Field(ge=0)
    remaining_process_count: int | None = Field(default=None, ge=0)
    remaining_process_group_count: int | None = Field(default=None, ge=0)
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...] | None = None
    observation_evidence_state: CleanupObservationEvidenceState = "none"
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:  # noqa: C901, PLR0912, PLR0915
        expected = _cleanup_unknown_reason(
            watcher_drain_status=self.watcher_drain_status,
            coverage_status=self.coverage_status,
            target_observation_unknown=self.unknown_reason == "target_observation_unknown",
        )
        if self.unknown_reason != expected:
            raise ValueError("cleanup unknown reason differs from semantic state")
        _require_cleanup_coverage_diagnostic(
            coverage_status=self.coverage_status,
            stage=self.coverage_stage,
            subreason=self.coverage_subreason,
            allow_legacy_missing=not bool(
                {"coverage_stage", "coverage_subreason"} & self.model_fields_set
            ),
        )
        if (
            self.remaining_process_count is not None
            or self.remaining_process_group_count is not None
        ):
            raise ValueError("unknown cleanup receipt cannot prove remaining counts")
        observation_field_present = "offending_observations" in self.model_fields_set
        if observation_field_present and self.offending_observations is None:
            raise ValueError("current cleanup receipt requires an observation array")
        if self.offending_observations and self.coverage_status != "unknown":
            raise ValueError("cleanup observations require unknown coverage")
        correlation_fields = {
            "expected_pgid",
            "expected_birth_identity_sha256",
            "occurrence_count",
        }
        observation_state_field_present = "observation_evidence_state" in self.model_fields_set
        observation_shapes = {
            "current"
            if correlation_fields <= observation.model_fields_set
            else "historical"
            if not (correlation_fields & observation.model_fields_set)
            else "partial"
            for observation in self.offending_observations or ()
        }
        if "partial" in observation_shapes or len(observation_shapes) > 1:
            raise ValueError("cleanup observations must use one complete evidence schema")
        inferred_evidence_state: CleanupObservationEvidenceState = (
            "legacy-current"
            if observation_shapes == {"current"}
            else "historical"
            if observation_shapes == {"historical"}
            else "none"
        )
        if observation_state_field_present:
            if self.observation_evidence_state == "current":
                if observation_shapes != {"current"}:
                    raise ValueError("current cleanup evidence requires current observations")
            elif self.observation_evidence_state == "none":
                if observation_shapes:
                    raise ValueError("empty cleanup evidence cannot contain observations")
            else:
                raise ValueError("legacy cleanup evidence state cannot be newly serialized")
            inferred_evidence_state = self.observation_evidence_state
        current_observations = inferred_evidence_state in {"current", "legacy-current"}
        if (
            observation_field_present
            and self.coverage_stage == "group_member"
            and not self.offending_observations
        ):
            raise ValueError("group-member cleanup evidence requires observations")
        if current_observations and self.coverage_stage == "group_member":
            states: set[CleanupObservationState] = {
                item.observation_state for item in self.offending_observations or ()
            }
            if not _observation_states_match_group_member_diagnostic(
                self.coverage_subreason,
                states,
            ):
                raise ValueError("group-member cleanup evidence differs from its diagnostic")
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        accepted_materials = [material]
        if not observation_state_field_present:
            prior_observation_material = dict(material)
            prior_observation_material.pop("observation_evidence_state")
            prior_observations: list[dict[str, JsonValue]] = []
            for observation in self.offending_observations or ():
                observation_material = observation.model_dump(mode="json")
                if not (correlation_fields & observation.model_fields_set):
                    for field in correlation_fields:
                        observation_material.pop(field)
                prior_observations.append(observation_material)
            if observation_field_present:
                prior_observation_material["offending_observations"] = prior_observations
            accepted_materials.append(prior_observation_material)
        if not observation_field_present and self.offending_observations is None:
            legacy_observations = dict(material)
            legacy_observations.pop("observation_evidence_state")
            legacy_observations.pop("offending_observations")
            accepted_materials.append(legacy_observations)
        if self.coverage_stage is None and self.coverage_subreason is None:
            for candidate in tuple(accepted_materials):
                legacy_material = dict(candidate)
                legacy_material.pop("coverage_stage")
                legacy_material.pop("coverage_subreason")
                accepted_materials.append(legacy_material)
        accepted = {_digest(candidate) for candidate in accepted_materials}
        if self.receipt_sha256 not in accepted:
            raise ValueError("cleanup unknown receipt digest mismatch")
        if not observation_state_field_present:
            return self.model_copy(
                update={"observation_evidence_state": inferred_evidence_state},
            )
        return self


class EvalProcessCleanupSnapshotEvidence(_StrictModel):
    coverage_stage: CleanupCoverageStage
    coverage_subreason: CleanupCoverageSubreason
    offending_observation_count: int = Field(ge=0)
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> Self:
        _require_cleanup_coverage_diagnostic(
            coverage_status="unknown",
            stage=self.coverage_stage,
            subreason=self.coverage_subreason,
        )
        if self.offending_observation_count != sum(
            item.occurrence_count or 0 for item in self.offending_observations
        ):
            raise ValueError("nested cleanup snapshot observation count differs")
        if self.coverage_stage == "group_member" and not self.offending_observations:
            raise ValueError("nested group-member snapshot requires observations")
        if self.coverage_stage == "group_member" and not (
            _observation_states_match_group_member_diagnostic(
                self.coverage_subreason,
                {item.observation_state for item in self.offending_observations},
            )
        ):
            raise ValueError("nested cleanup snapshot differs from its diagnostic")
        return self


class EvalProcessCleanupUnknownReceipt(_StrictModel):
    """Owner-only aggregate of nested eval cleanup observations."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["eval_process_cleanup_unknown"] = "eval_process_cleanup_unknown"
    snapshot_count: int = Field(ge=1)
    unknown_snapshot_count: int = Field(ge=1)
    cleanup_snapshots: tuple[EvalProcessCleanupSnapshotEvidence, ...] = Field(min_length=1)
    offending_observation_count: int = Field(ge=1)
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...] = Field(
        min_length=1,
    )
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        if self.unknown_snapshot_count > self.snapshot_count:
            raise ValueError("nested cleanup unknown snapshots exceed total snapshots")
        if len(self.cleanup_snapshots) > self.unknown_snapshot_count:
            raise ValueError("nested cleanup evidence snapshots exceed unknown snapshots")
        if self.offending_observation_count != sum(
            item.occurrence_count or 0 for item in self.offending_observations
        ):
            raise ValueError("nested cleanup observation count differs")
        if any(
            not {
                "expected_pgid",
                "expected_birth_identity_sha256",
                "occurrence_count",
            }
            <= item.model_fields_set
            for item in self.offending_observations
        ):
            raise ValueError("nested cleanup observations require current correlation fields")
        merged = merge_process_cleanup_observations(
            [
                observation
                for snapshot in self.cleanup_snapshots
                for observation in snapshot.offending_observations
            ],
        )
        if merged != self.offending_observations:
            raise ValueError("nested cleanup aggregate observations differ")
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _digest(material):
            raise ValueError("nested cleanup receipt digest mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    pgid: int
    birth_identity: str
    state: Literal["running", "zombie", "unknown"]
    ppid: int | None = None
    process_category: CleanupProcessCategory | None = None
    process_identity_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCleanupIdentity:
    pid: int
    pgid: int
    birth_identity: str
    initial_state: Literal["running", "zombie", "unknown"]


@dataclass(frozen=True, slots=True)
class ProcessCleanupScope:
    """Birth-bound identities plus numeric discovery hints captured for one child tree."""

    identities: tuple[ProcessCleanupIdentity, ...]
    tracked_pids: tuple[int, ...]
    tracked_pgids: tuple[int, ...]
    coverage_status: CleanupCoverage
    tracked_pid_groups: tuple[tuple[int, int], ...] = ()
    observed_identities: tuple[ProcessCleanupIdentity, ...] = ()
    coverage_stage: CleanupCoverageStage | None = None
    coverage_subreason: CleanupCoverageSubreason | None = None
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...] = ()

    def __post_init__(self) -> None:
        """Reject coverage values outside the authenticated receipt vocabulary."""
        if self.coverage_status not in {"complete", "unknown"}:
            raise ValueError("cleanup scope coverage status is invalid")
        _require_cleanup_coverage_diagnostic(
            coverage_status=self.coverage_status,
            stage=self.coverage_stage,
            subreason=self.coverage_subreason,
        )
        if self.offending_observations and self.coverage_status != "unknown":
            raise ValueError("cleanup offending observations require unknown coverage")
        tracked_pids = frozenset(self.tracked_pids)
        tracked_pgids = frozenset(self.tracked_pgids)
        if any(
            pid not in tracked_pids or pgid not in tracked_pgids
            for pid, pgid in self.tracked_pid_groups
        ):
            raise ValueError("tracked cleanup PID/group binding is outside tracked scope")
        if any(
            identity.pid not in tracked_pids or identity.pgid not in tracked_pgids
            for identity in self.observed_identities
        ):
            raise ValueError("observed cleanup identity is outside tracked scope")


@dataclass(slots=True)
class RootBoundProcessCleanupAdmission:
    """Admit cleanup identities only inside one live, same-birth root window."""

    process: subprocess.Popen[str]
    root_pgid: int
    root_identity: ProcessCleanupIdentity | None
    _closed: threading.Event = field(default_factory=threading.Event, repr=False)

    def close(self) -> None:
        self._closed.set()

    def root_pid_for_numeric_discovery(self) -> int | None:
        """Return the numeric root only while the original process is still birth-bound."""
        return self.process.pid if self._root_allows_admission() else None

    def capture_scope(self) -> ProcessCleanupScope:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Take two root-bracketed snapshots and admit only exact stable identities."""
        root_pid = self.process.pid
        root_identities = (self.root_identity,) if self.root_identity is not None else ()
        observed_identities: dict[tuple[int, int, str], ProcessCleanupIdentity] = {
            (identity.pid, identity.pgid, identity.birth_identity): identity
            for identity in root_identities
        }
        tracked_pids: set[int] = {root_pid}
        tracked_pgids: set[int] = {self.root_pgid}
        tracked_pid_groups: set[tuple[int, int]] = {(root_pid, self.root_pgid)}
        if not self._root_allows_admission():
            return self._detection_scope(
                root_identities,
                tuple(observed_identities.values()),
                tracked_pids,
                tracked_pgids,
                tracked_pid_groups,
                coverage_status="complete",
            )

        try:
            (
                first_pids,
                first_pgids,
                first_coverage,
                first_pid_groups,
                first_discovered_identities,
            ) = _tree_snapshot_parts(
                _snapshot_tree_checked(
                    root_pid,
                    self.root_pgid,
                ),
            )
        except (OSError, RuntimeError, ValueError):
            self.close()
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
                coverage_status="unknown",
                tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
                observed_identities=tuple(observed_identities.values()),
                coverage_stage="first_snapshot",
                coverage_subreason="snapshot_failure",
            )
        tracked_pids.update(first_pids)
        tracked_pgids.update(first_pgids)
        if first_coverage == "unknown":
            self.close()
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
                coverage_status="unknown",
                tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
                observed_identities=tuple(observed_identities.values()),
                coverage_stage="first_snapshot",
                coverage_subreason="snapshot_failure",
            )
        observations: list[ProcessObservation] = []
        offending_observations: list[ProcessCleanupOffendingObservation] = []
        observation_diagnostic: (
            tuple[
                CleanupCoverageStage,
                CleanupCoverageSubreason,
            ]
            | None
        ) = None
        for pid in first_pids:
            observation = read_process_observation(pid)
            if observation is None:
                continue
            if observation.state == "unknown":
                observation_diagnostic = ("group_member", "observation_unknown")
                offending_observations.append(
                    _offending_process_observation(
                        pid=pid,
                        expected_pgid=observation.pgid,
                        observation=observation,
                        detection_source="first_snapshot",
                        admission_phase="admission_open",
                        observation_state="unknown",
                        expected_birth_identity=observation.birth_identity,
                    ),
                )
                continue
            observations.append(observation)
            observed_identity = ProcessCleanupIdentity(
                pid=observation.pid,
                pgid=observation.pgid,
                birth_identity=observation.birth_identity,
                initial_state=observation.state,
            )
            if observed_identity.pgid in tracked_pgids:
                observed_identities[
                    (
                        observed_identity.pid,
                        observed_identity.pgid,
                        observed_identity.birth_identity,
                    )
                ] = observed_identity
        if not self._root_allows_admission():
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
                coverage_status=("unknown" if observation_diagnostic is not None else "complete"),
                tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
                observed_identities=tuple(observed_identities.values()),
                coverage_stage=(
                    observation_diagnostic[0] if observation_diagnostic is not None else None
                ),
                coverage_subreason=(
                    observation_diagnostic[1] if observation_diagnostic is not None else None
                ),
                offending_observations=_dedupe_offending_observations(
                    offending_observations,
                ),
            )
        tracked_pid_groups.update(first_pid_groups)
        for discovered in first_discovered_identities:
            observed_identities[(discovered.pid, discovered.pgid, discovered.birth_identity)] = (
                discovered
            )

        try:
            (
                second_pids,
                second_pgids,
                second_coverage,
                second_pid_groups,
                second_discovered_identities,
            ) = _tree_snapshot_parts(
                _snapshot_tree_checked(
                    root_pid,
                    self.root_pgid,
                ),
            )
        except (OSError, RuntimeError, ValueError):
            self.close()
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
                coverage_status="unknown",
                tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
                observed_identities=tuple(observed_identities.values()),
                coverage_stage="second_snapshot",
                coverage_subreason="snapshot_failure",
            )
        tracked_pids.update(second_pids)
        tracked_pgids.update(second_pgids)
        if second_coverage == "unknown":
            self.close()
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
                coverage_status="unknown",
                tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
                observed_identities=tuple(observed_identities.values()),
                coverage_stage="second_snapshot",
                coverage_subreason="snapshot_failure",
            )
        second_pid_scope = frozenset(second_pids)
        second_pgid_scope = frozenset(second_pgids)
        confirmed: dict[int, ProcessCleanupIdentity] = {
            identity.pid: identity for identity in root_identities
        }
        first_observed_pids = {observation.pid for observation in observations}
        for pid in sorted(second_pid_scope - first_observed_pids):
            observation = read_process_observation(pid)
            if observation is None or observation.state == "unknown":
                continue
            observed_identity = ProcessCleanupIdentity(
                pid=observation.pid,
                pgid=observation.pgid,
                birth_identity=observation.birth_identity,
                initial_state=observation.state,
            )
            if observed_identity.pgid in tracked_pgids:
                observed_identities[
                    (
                        observed_identity.pid,
                        observed_identity.pgid,
                        observed_identity.birth_identity,
                    )
                ] = observed_identity
        for observation in observations:
            reobserved = read_process_observation(observation.pid)
            if observation.pid not in second_pid_scope:
                continue
            if reobserved is None:
                continue
            if reobserved.state == "unknown":
                observation_diagnostic = observation_diagnostic or (
                    "group_member",
                    "observation_unknown",
                )
                offending_observations.append(
                    _offending_process_observation(
                        pid=observation.pid,
                        expected_pgid=observation.pgid,
                        observation=reobserved,
                        detection_source="second_snapshot",
                        admission_phase="admission_open",
                        observation_state="unknown",
                        expected_birth_identity=observation.birth_identity,
                    ),
                )
                continue
            reobserved_identity = ProcessCleanupIdentity(
                pid=reobserved.pid,
                pgid=reobserved.pgid,
                birth_identity=reobserved.birth_identity,
                initial_state=reobserved.state,
            )
            if reobserved_identity.pgid in tracked_pgids:
                observed_identities[
                    (
                        reobserved_identity.pid,
                        reobserved_identity.pgid,
                        reobserved_identity.birth_identity,
                    )
                ] = reobserved_identity
            if not (
                reobserved.pid == observation.pid
                and reobserved.birth_identity == observation.birth_identity
                and reobserved.pgid == observation.pgid
                and observation.pgid in second_pgid_scope
            ):
                observation_diagnostic = observation_diagnostic or (
                    "group_member",
                    "changed_identity_or_group_member",
                )
                offending_observations.append(
                    _offending_process_observation(
                        pid=observation.pid,
                        expected_pgid=observation.pgid,
                        observation=reobserved,
                        detection_source="second_snapshot",
                        admission_phase="admission_open",
                        observation_state=(
                            "group_changed"
                            if reobserved.pgid != observation.pgid
                            else "identity_changed"
                        ),
                        expected_birth_identity=observation.birth_identity,
                    ),
                )
                continue
            confirmed[observation.pid] = ProcessCleanupIdentity(
                pid=observation.pid,
                pgid=observation.pgid,
                birth_identity=observation.birth_identity,
                initial_state=observation.state,
            )
        if not self._root_allows_admission():
            confirmed = {identity.pid: identity for identity in root_identities}
        else:
            tracked_pid_groups.update(second_pid_groups)
            for discovered in second_discovered_identities:
                observed_identities[
                    (discovered.pid, discovered.pgid, discovered.birth_identity)
                ] = discovered
        return ProcessCleanupScope(
            identities=tuple(confirmed[pid] for pid in sorted(confirmed)),
            tracked_pids=tuple(sorted(tracked_pids)),
            tracked_pgids=tuple(sorted(tracked_pgids)),
            coverage_status="unknown" if observation_diagnostic is not None else "complete",
            tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
            observed_identities=tuple(
                observed_identities[key] for key in sorted(observed_identities)
            ),
            coverage_stage=(
                observation_diagnostic[0] if observation_diagnostic is not None else None
            ),
            coverage_subreason=(
                observation_diagnostic[1] if observation_diagnostic is not None else None
            ),
            offending_observations=_dedupe_offending_observations(
                offending_observations,
            ),
        )

    def _root_allows_admission(self) -> bool:
        if self._closed.is_set() or self.root_identity is None:
            return False
        try:
            return_code = self.process.poll()
        except OSError:
            self.close()
            return False
        if return_code is not None:
            self.close()
            return False
        observation = read_process_observation(self.process.pid)
        if not (
            observation is not None
            and observation.state == "running"
            and observation.pid == self.root_identity.pid
            and observation.pgid == self.root_identity.pgid
            and observation.birth_identity == self.root_identity.birth_identity
        ):
            self.close()
            return False
        return not self._closed.is_set()

    def _detection_scope(  # noqa: PLR0913
        self,
        identities: tuple[ProcessCleanupIdentity, ...],
        observed_identities: tuple[ProcessCleanupIdentity, ...],
        tracked_pids: set[int],
        tracked_pgids: set[int],
        tracked_pid_groups: set[tuple[int, int]],
        *,
        coverage_status: CleanupCoverage,
    ) -> ProcessCleanupScope:
        try:
            pids, pgids, detected_coverage, _pid_groups, _discovered = _tree_snapshot_parts(
                _snapshot_tree_checked(
                    None,
                    self.root_pgid,
                ),
            )
        except (OSError, RuntimeError, ValueError):
            pids, pgids, detected_coverage, _pid_groups, _discovered = (
                (),
                (),
                "unknown",
                (),
                (),
            )
        tracked_pids.update(pids)
        tracked_pgids.update(pgids)
        return ProcessCleanupScope(
            identities=identities,
            tracked_pids=tuple(sorted(tracked_pids)),
            tracked_pgids=tuple(sorted(tracked_pgids)),
            coverage_status=(
                "unknown"
                if coverage_status == "unknown" or detected_coverage == "unknown"
                else "complete"
            ),
            # Closed-root scans are detection-only. They may extend the numeric scope but cannot
            # create trusted discovery-time PID/group bindings for later reuse classification.
            tracked_pid_groups=tuple(sorted(tracked_pid_groups)),
            observed_identities=observed_identities,
            coverage_stage=(
                "first_snapshot"
                if coverage_status == "unknown" or detected_coverage == "unknown"
                else None
            ),
            coverage_subreason=(
                "snapshot_failure"
                if coverage_status == "unknown" or detected_coverage == "unknown"
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CommandCleanupIdentityEvidence:
    evidence_status: CleanupIdentityEvidenceKind
    receipt_sha256: str | None = None
    unknown_reason: CleanupUnknownReason | None = None
    cleanup_coverage_stage: CleanupCoverageStage | None = None
    cleanup_coverage_subreason: CleanupCoverageSubreason | None = None

    def __post_init__(self) -> None:
        """Require evidence kind, digest, and safe reason to agree."""
        _require_cleanup_identity_evidence_consistency(
            self.evidence_status,
            self.receipt_sha256,
            self.unknown_reason,
            self.cleanup_coverage_stage,
            self.cleanup_coverage_subreason,
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    receipt: CommandReceipt
    stdout: str
    stderr: str
    cleanup_identity_receipt_sha256: str | None = None
    cleanup_identity_evidence_status: CleanupIdentityEvidenceKind = "no-target-observed"
    cleanup_unknown_reason: CleanupUnknownReason | None = None
    cleanup_coverage_stage: CleanupCoverageStage | None = None
    cleanup_coverage_subreason: CleanupCoverageSubreason | None = None

    def __post_init__(self) -> None:
        """Require command cleanup status and digest consistency."""
        _require_cleanup_identity_evidence_consistency(
            self.cleanup_identity_evidence_status,
            self.cleanup_identity_receipt_sha256,
            self.cleanup_unknown_reason,
            self.cleanup_coverage_stage,
            self.cleanup_coverage_subreason,
        )

    def json_stdout(self) -> dict[str, JsonValue]:
        try:
            return JSON_OBJECT_ADAPTER.validate_json(self.stdout)
        except ValidationError:
            return {}

    def json_value(self) -> JsonValue | None:
        try:
            return cast("JsonValue", json.loads(self.stdout))
        except json.JSONDecodeError:
            return None


@dataclass(frozen=True, slots=True)
class CommandFailureError(Exception):
    receipt: CommandReceipt
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    remaining_process_count: int | None = None
    remaining_process_group_count: int | None = None
    cleanup_identity_receipt_sha256: str | None = None
    cleanup_identity_evidence_status: CleanupIdentityEvidenceKind = "no-target-observed"
    cleanup_unknown_reason: CleanupUnknownReason | None = None
    cleanup_coverage_stage: CleanupCoverageStage | None = None
    cleanup_coverage_subreason: CleanupCoverageSubreason | None = None

    def __post_init__(self) -> None:
        """Require failed-command cleanup status and digest consistency."""
        _require_cleanup_identity_evidence_consistency(
            self.cleanup_identity_evidence_status,
            self.cleanup_identity_receipt_sha256,
            self.cleanup_unknown_reason,
            self.cleanup_coverage_stage,
            self.cleanup_coverage_subreason,
        )


def run_command(  # noqa: C901, PLR0912, PLR0913, PLR0915
    name: str,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 180,
    cleanup_identity_receipt_path: Path | None = None,
) -> CommandResult:
    """Run a command; always clean the process group on success, fail, interrupt, timeout."""
    if env is None:
        msg = "isolated env is required"
        raise ValueError(msg)
    executable = shutil_which(argv[0], path=env.get("PATH"))
    command: tuple[str, ...] = argv if executable is None else (executable, *argv[1:])
    process: subprocess.Popen[str] | None = None
    pgid: int | None = None
    root_pid: int | None = None
    admission: RootBoundProcessCleanupAdmission | None = None
    tracked_pids: list[int] = []
    tracked_pgids: list[int] = []
    tracked_pid_groups: set[tuple[int, int]] = set()
    tracked_identities: dict[int, ProcessCleanupIdentity] = {}
    tracked_observed_identities: dict[tuple[int, int, str], ProcessCleanupIdentity] = {}
    stop_watch = threading.Event()
    watch_lock = threading.Lock()
    admission_publication_state: Literal["open", "draining", "frozen"] = "open"
    watcher_capture_inflight = 0
    watcher_capture_failed = False
    watcher_publication_discarded = False
    watcher_drain_status: WatcherDrainState = "not-applicable"
    tracked_coverage_status: CleanupCoverage = "complete"
    tracked_coverage_stage: CleanupCoverageStage | None = None
    tracked_coverage_subreason: CleanupCoverageSubreason | None = None
    tracked_offending_observations: list[ProcessCleanupOffendingObservation] = []
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = 124
    process_error: OSError | None = None
    output_drain: _OutputDrainThread | None = None
    drained_output: list[tuple[str, str]] = []
    output_drain_errors: list[OSError | subprocess.SubprocessError | ValueError] = []
    pids: tuple[int, ...] = ()
    pgids: tuple[int, ...] = ()
    terminal_snapshot = ProcessCleanupTerminalSnapshot(targets=(), coverage_status="complete")

    def _poll_root_process() -> int | None:
        if process is None:
            if admission is not None:
                admission.close()
            return None
        try:
            return_code = process.poll()
        except OSError:
            if admission is not None:
                admission.close()
            raise
        if return_code is not None and admission is not None:
            admission.close()
        return return_code

    def _capture_identities() -> None:  # noqa: C901
        nonlocal tracked_coverage_status
        nonlocal tracked_coverage_stage, tracked_coverage_subreason
        nonlocal watcher_capture_failed, watcher_capture_inflight, watcher_publication_discarded
        if admission is None:
            return
        with watch_lock:
            if admission_publication_state != "open":
                return
            watcher_capture_inflight += 1
        try:
            scope = admission.capture_scope()
        except BaseException:
            with watch_lock:
                watcher_capture_inflight -= 1
                watcher_capture_failed = True
                tracked_coverage_status = "unknown"
                if tracked_coverage_stage is None:
                    tracked_coverage_stage = "watcher_capture"
                    tracked_coverage_subreason = "capture_failure"
            raise
        with watch_lock:
            watcher_capture_inflight -= 1
            if scope.coverage_status == "unknown":
                tracked_coverage_status = "unknown"
                if tracked_coverage_stage is None:
                    tracked_coverage_stage = scope.coverage_stage
                    tracked_coverage_subreason = scope.coverage_subreason
            # Another thread can advance the state while capture_scope runs.
            if str(admission_publication_state) == "frozen":
                watcher_publication_discarded = True
                return
            tracked_offending_observations.extend(scope.offending_observations)
            tracked_pids[:] = sorted(set(tracked_pids) | set(scope.tracked_pids))
            tracked_pgids[:] = sorted(set(tracked_pgids) | set(scope.tracked_pgids))
            tracked_pid_groups.update(scope.tracked_pid_groups)
            for observed in scope.observed_identities:
                tracked_observed_identities[
                    (observed.pid, observed.pgid, observed.birth_identity)
                ] = observed
            for candidate in scope.identities:
                identity = tracked_identities.get(candidate.pid)
                if identity is None:
                    tracked_identities[candidate.pid] = candidate
                elif (
                    identity.birth_identity == candidate.birth_identity
                    and identity.pgid != candidate.pgid
                ):
                    tracked_identities[candidate.pid] = ProcessCleanupIdentity(
                        pid=identity.pid,
                        pgid=candidate.pgid,
                        birth_identity=identity.birth_identity,
                        initial_state=identity.initial_state,
                    )

    def _write_cleanup_receipt() -> CommandCleanupIdentityEvidence:
        if root_pid is None or pgid is None:
            return CommandCleanupIdentityEvidence(evidence_status="no-target-observed")
        if cleanup_identity_receipt_path is None:
            if terminal_snapshot.cleanup_status == "unknown":
                return CommandCleanupIdentityEvidence(
                    evidence_status="write-failed",
                    unknown_reason="cleanup_receipt_path_missing",
                )
            return CommandCleanupIdentityEvidence(evidence_status="no-target-observed")
        return write_command_cleanup_identity_receipt(
            cleanup_identity_receipt_path,
            name=name,
            argv=argv,
            cwd=cwd,
            root_pid=root_pid,
            root_pgid=pgid,
            terminal_snapshot=terminal_snapshot,
        )

    def _watch() -> None:
        while not stop_watch.is_set():
            if admission is None:
                time.sleep(0.001)
                continue
            try:
                _capture_identities()
            except (OSError, RuntimeError, ValueError):
                return
            time.sleep(0.001)

    def _drain_process_output() -> None:
        if process is None:
            return
        try:
            drained_output.append(process.communicate())
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            output_drain_errors.append(error)

    watcher: threading.Thread | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=preserve_parent_temp_environment(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        root_pid = process.pid
        pgid = os.getpgid(process.pid)
        admission = open_root_bound_process_cleanup_admission(process, pgid)
        # Immediate snapshot so fast-exit parents still leave tracked members.
        _capture_identities()
        output_drain = _OutputDrainThread(
            target=_drain_process_output,
            name=f"cmd-output-{name}",
            daemon=True,
        )
        output_drain.start()
        watcher = threading.Thread(target=_watch, name=f"cmd-watch-{name}", daemon=True)
        watcher.start()
        deadline = time.monotonic() + timeout_seconds
        while _poll_root_process() is None:
            # Continuous capture while parent is alive (escaped groups / new sessions).
            _capture_identities()
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.005)
        with watch_lock:
            pids = tuple(tracked_pids)
            pgids = tuple(tracked_pgids)
        try:
            (
                observed_pids,
                observed_pgids,
                observed_coverage,
                _observed_pid_groups,
                _observed_discovered_identities,
            ) = _tree_snapshot_parts(
                _snapshot_tree_checked(
                    admission.root_pid_for_numeric_discovery(),
                    pgid,
                ),
            )
        except (OSError, RuntimeError, ValueError):
            (
                observed_pids,
                observed_pgids,
                observed_coverage,
                _observed_pid_groups,
                _observed_discovered_identities,
            ) = (
                (),
                (),
                "unknown",
                (),
                (),
            )
        with watch_lock:
            if observed_coverage == "unknown":
                tracked_coverage_status = "unknown"
                if tracked_coverage_stage is None:
                    tracked_coverage_stage = "post_exit_snapshot"
                    tracked_coverage_subreason = "snapshot_failure"
        pids, pgids = _merge_snapshots(pids, pgids, observed_pids, observed_pgids)
        # Always include original process group: redirected sleepers share it after parent exit.
        # pgid is assigned from os.getpgid after Popen; keep it in the tracked set even if
        # the parent has already exited and descendants remain only under that group.
        tracked_pgid = pgid
        pgids = tuple(sorted(set(pgids) | {tracked_pgid}))
        _capture_identities()
    except OSError as exc:
        process_error = exc
    finally:
        # Stop new admission under the publication lock, then give any already registered pass
        # one bounded drain window. A pass either publishes while draining or is discarded once
        # the immutable target snapshot is frozen below.
        with watch_lock:
            admission_publication_state = "draining"
            if admission is not None:
                admission.close()
        stop_watch.set()
        if watcher is not None:
            try:
                watcher.join(timeout=WATCHER_DRAIN_SECONDS)
                watcher_drain_status = "still-running" if watcher.is_alive() else "drained"
            except (OSError, RuntimeError):
                watcher_drain_status = "unknown"
        with watch_lock:
            admission_publication_state = "frozen"
            if (
                watcher_capture_failed or watcher_capture_inflight
            ) and watcher_drain_status == "drained":
                watcher_drain_status = "unknown"
            elif watcher_publication_discarded and watcher_drain_status == "drained":
                watcher_drain_status = "discarded"
            pids, pgids = _merge_snapshots(
                pids,
                pgids,
                tuple(tracked_pids),
                tuple(tracked_pgids),
            )
            identities = tuple(tracked_identities[pid] for pid in sorted(tracked_identities))
            observed_identities = tuple(
                tracked_observed_identities[key] for key in sorted(tracked_observed_identities)
            )
            pid_groups = tuple(sorted(tracked_pid_groups))
            cleanup_coverage_status = tracked_coverage_status
            cleanup_coverage_stage = tracked_coverage_stage
            cleanup_coverage_subreason = tracked_coverage_subreason
            cleanup_offending_observations = _dedupe_offending_observations(
                tracked_offending_observations,
            )
        try:
            (
                final_pids,
                final_pgids,
                final_coverage,
                _final_pid_groups,
                _final_discovered_identities,
            ) = _tree_snapshot_parts(
                _snapshot_tree_checked(
                    (admission.root_pid_for_numeric_discovery() if admission is not None else None),
                    pgid,
                ),
            )
        except (OSError, RuntimeError, ValueError):
            (
                final_pids,
                final_pgids,
                final_coverage,
                _final_pid_groups,
                _final_discovered_identities,
            ) = ((), (), "unknown", (), ())
        if final_coverage == "unknown":
            cleanup_coverage_status = "unknown"
            if cleanup_coverage_stage is None:
                cleanup_coverage_stage = "final_snapshot"
                cleanup_coverage_subreason = "snapshot_failure"
        pids, pgids = _merge_snapshots(pids, pgids, final_pids, final_pgids)
        if root_pid is not None:
            pids = tuple(sorted(set(pids) | {root_pid}))
        if pgid is not None:
            pgids = tuple(sorted(set(pgids) | {pgid}))
        # Signal only still-running processes whose birth identity still matches. The returned
        # terminal snapshot is the sole source for the receipt and remaining candidate counts.
        try:
            if observed_identities:
                cleanup_snapshot = cleanup_birth_bound_processes(
                    identities,
                    tracked_pids=pids,
                    tracked_pgids=pgids,
                    tracked_pid_groups=pid_groups,
                    observed_identities=observed_identities,
                    coverage_status=cleanup_coverage_status,
                    coverage_stage=cleanup_coverage_stage,
                    coverage_subreason=cleanup_coverage_subreason,
                )
            else:
                cleanup_snapshot = cleanup_birth_bound_processes(
                    identities,
                    tracked_pids=pids,
                    tracked_pgids=pgids,
                    tracked_pid_groups=pid_groups,
                    coverage_status=cleanup_coverage_status,
                    coverage_stage=cleanup_coverage_stage,
                    coverage_subreason=cleanup_coverage_subreason,
                )
            terminal_snapshot = replace(
                cleanup_snapshot,
                watcher_drain_status=watcher_drain_status,
                offending_observations=_dedupe_offending_observations(
                    [
                        *cleanup_offending_observations,
                        *cleanup_snapshot.offending_observations,
                    ],
                ),
            )
        except (OSError, ValidationError, ValueError):
            terminal_snapshot = ProcessCleanupTerminalSnapshot(
                targets=(),
                coverage_status="unknown",
                coverage_stage="target_observation",
                coverage_subreason="observation_unknown",
                watcher_drain_status=watcher_drain_status,
                target_observation_unknown=True,
                offending_observations=cleanup_offending_observations,
            )
        if output_drain is not None:
            output_drain.join(timeout=KILL_WAIT_SECONDS + TERM_WAIT_SECONDS)
            output_drain_failed = output_drain.is_alive() or bool(output_drain_errors)
            if output_drain.is_alive():
                if process is not None:
                    for stream in (process.stdout, process.stderr):
                        if stream is None:
                            continue
                        with suppress(OSError, ValueError):
                            stream.close()
                output_drain.join()
                process_error = process_error or OSError("command_output_drain_timeout")
            elif output_drain_errors:
                process_error = process_error or OSError(
                    type(output_drain_errors[0]).__name__,
                )
            elif drained_output:
                stdout, stderr = drained_output[0]
            if output_drain_failed:
                terminal_snapshot = replace(
                    terminal_snapshot,
                    coverage_status="unknown",
                    coverage_stage=(terminal_snapshot.coverage_stage or "target_observation"),
                    coverage_subreason=(
                        terminal_snapshot.coverage_subreason or "observation_unknown"
                    ),
                    target_observation_unknown=True,
                )
        elif process is not None:
            try:
                stdout, stderr = process.communicate(
                    timeout=KILL_WAIT_SECONDS + TERM_WAIT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                stdout = stdout or ""
                stderr = stderr or "communicate_timeout"
            except OSError as exc:
                process_error = process_error or exc

    cleanup_identity_evidence = _write_cleanup_receipt()
    remaining_process_count = terminal_snapshot.remaining_process_count
    remaining_process_group_count = terminal_snapshot.remaining_process_group_count

    if timed_out:
        exit_code = 124
        receipt = _receipt(
            name,
            argv,
            cwd,
            root_pid,
            pgid,
            exit_code,
            stdout,
            stderr,
            timed_out=True,
            cleanup_attempted=True,
        )
        raise CommandFailureError(
            receipt=receipt,
            stdout=stdout,
            stderr=stderr,
            remaining_process_count=remaining_process_count,
            remaining_process_group_count=remaining_process_group_count,
            cleanup_identity_receipt_sha256=cleanup_identity_evidence.receipt_sha256,
            cleanup_identity_evidence_status=cleanup_identity_evidence.evidence_status,
            cleanup_unknown_reason=cleanup_identity_evidence.unknown_reason,
            cleanup_coverage_stage=cleanup_identity_evidence.cleanup_coverage_stage,
            cleanup_coverage_subreason=cleanup_identity_evidence.cleanup_coverage_subreason,
        )

    if process_error is not None:
        safe_error = type(process_error).__name__
        receipt = _receipt(
            name,
            argv,
            cwd,
            root_pid,
            pgid,
            124,
            "",
            safe_error,
            timed_out=False,
            cleanup_attempted=True,
        )
        raise CommandFailureError(
            receipt=receipt,
            stdout="",
            stderr=safe_error,
            remaining_process_count=remaining_process_count,
            remaining_process_group_count=remaining_process_group_count,
            cleanup_identity_receipt_sha256=cleanup_identity_evidence.receipt_sha256,
            cleanup_identity_evidence_status=cleanup_identity_evidence.evidence_status,
            cleanup_unknown_reason=cleanup_identity_evidence.unknown_reason,
            cleanup_coverage_stage=cleanup_identity_evidence.cleanup_coverage_stage,
            cleanup_coverage_subreason=cleanup_identity_evidence.cleanup_coverage_subreason,
        ) from process_error

    if process is not None:
        exit_code = int(process.returncode if process.returncode is not None else 124)

    receipt = _receipt(
        name,
        argv,
        cwd,
        root_pid,
        pgid,
        exit_code,
        stdout,
        stderr,
        timed_out=False,
        cleanup_attempted=True,
    )
    result = CommandResult(
        receipt=receipt,
        stdout=stdout,
        stderr=stderr,
        cleanup_identity_receipt_sha256=cleanup_identity_evidence.receipt_sha256,
        cleanup_identity_evidence_status=cleanup_identity_evidence.evidence_status,
        cleanup_unknown_reason=cleanup_identity_evidence.unknown_reason,
        cleanup_coverage_stage=cleanup_identity_evidence.cleanup_coverage_stage,
        cleanup_coverage_subreason=cleanup_identity_evidence.cleanup_coverage_subreason,
    )
    cleanup_closed = terminal_snapshot.cleanup_status == "complete" and (
        cleanup_identity_receipt_path is None
        or cleanup_identity_evidence.evidence_status in {"authenticated", "no-target-observed"}
    )
    if exit_code == 0 and not cleanup_closed:
        safe_cleanup_error = (
            "process_cleanup_failed"
            if terminal_snapshot.cleanup_status == "failed"
            else "process_cleanup_unknown"
        )
        raise CommandFailureError(
            receipt=result.receipt,
            stdout=result.stdout,
            stderr=safe_cleanup_error,
            remaining_process_count=remaining_process_count,
            remaining_process_group_count=remaining_process_group_count,
            cleanup_identity_receipt_sha256=cleanup_identity_evidence.receipt_sha256,
            cleanup_identity_evidence_status=cleanup_identity_evidence.evidence_status,
            cleanup_unknown_reason=cleanup_identity_evidence.unknown_reason,
            cleanup_coverage_stage=cleanup_identity_evidence.cleanup_coverage_stage,
            cleanup_coverage_subreason=cleanup_identity_evidence.cleanup_coverage_subreason,
        )
    if exit_code != 0:
        raise CommandFailureError(
            receipt=result.receipt,
            stdout=result.stdout,
            stderr=result.stderr,
            remaining_process_count=remaining_process_count,
            remaining_process_group_count=remaining_process_group_count,
            cleanup_identity_receipt_sha256=cleanup_identity_evidence.receipt_sha256,
            cleanup_identity_evidence_status=cleanup_identity_evidence.evidence_status,
            cleanup_unknown_reason=cleanup_identity_evidence.unknown_reason,
            cleanup_coverage_stage=cleanup_identity_evidence.cleanup_coverage_stage,
            cleanup_coverage_subreason=cleanup_identity_evidence.cleanup_coverage_subreason,
        )
    return result


def read_process_observation(pid: int) -> ProcessObservation | None:
    """Read one local PID while retaining command identity only as a category or digest."""
    try:
        completed = subprocess.run(
            (  # noqa: S607
                "ps",
                "-o",
                "ppid=,pgid=,state=,lstart=,comm=",
                "-p",
                str(pid),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ProcessObservation(pid=pid, pgid=pid, birth_identity="", state="unknown")
    line = next((item.strip() for item in completed.stdout.splitlines() if item.strip()), "")
    if completed.returncode != 0 and not line:
        return None
    parts = line.split(maxsplit=8)
    if completed.returncode != 0 or len(parts) != 9:  # noqa: PLR2004
        return ProcessObservation(pid=pid, pgid=pid, birth_identity="", state="unknown")
    try:
        ppid = int(parts[0])
        pgid = int(parts[1])
    except ValueError:
        return ProcessObservation(pid=pid, pgid=pid, birth_identity="", state="unknown")
    state_code = parts[2][:1].upper()
    state: Literal["running", "zombie", "unknown"] = (
        "zombie" if state_code == "Z" else "running" if state_code else "unknown"
    )
    command_identity = parts[8]
    process_category: CleanupProcessCategory | None = (
        "process_observer" if Path(command_identity).name == "ps" else None
    )
    return ProcessObservation(
        pid=pid,
        pgid=pgid,
        ppid=ppid,
        birth_identity=" ".join(parts[3:8]),
        state=state,
        process_category=process_category,
        process_identity_sha256=(
            None
            if process_category is not None
            else hashlib.sha256(command_identity.encode()).hexdigest()
        ),
    )


def capture_process_cleanup_identity(pid: int) -> ProcessCleanupIdentity | None:
    observation = read_process_observation(pid)
    if observation is None or observation.state == "unknown":
        return None
    return ProcessCleanupIdentity(
        pid=observation.pid,
        pgid=observation.pgid,
        birth_identity=observation.birth_identity,
        initial_state=observation.state,
    )


def open_root_bound_process_cleanup_admission(
    process: subprocess.Popen[str],
    root_pgid: int,
) -> RootBoundProcessCleanupAdmission:
    """Bind all later identity admission to one exact root process handle and birth."""
    root_identity: ProcessCleanupIdentity | None = None
    try:
        root_active = process.poll() is None
    except OSError:
        root_active = False
    if root_active:
        candidate = capture_process_cleanup_identity(process.pid)
        if (
            candidate is not None
            and candidate.pid == process.pid
            and candidate.pgid == root_pgid
            and candidate.initial_state == "running"
        ):
            try:
                process.poll()
            except OSError:
                candidate = None
            root_identity = candidate
    return RootBoundProcessCleanupAdmission(
        process=process,
        root_pgid=root_pgid,
        root_identity=root_identity,
    )


def capture_process_cleanup_scope(
    process: subprocess.Popen[str],
    root_pgid: int,
) -> ProcessCleanupScope:
    """Capture one nested child scope through the shared root-bound admission gate."""
    return open_root_bound_process_cleanup_admission(process, root_pgid).capture_scope()


def observe_process_cleanup_target(
    identity: ProcessCleanupIdentity,
) -> ProcessCleanupTargetReceipt:
    observation = read_process_observation(identity.pid)
    initial_sha256 = hashlib.sha256(identity.birth_identity.encode()).hexdigest()
    if observation is None:
        terminal_sha256 = None
        terminal_state = "absent"
        outcome = "no_longer_running"
    elif observation.state == "unknown":
        terminal_sha256 = None
        terminal_state = "unknown"
        outcome = "unknown"
    else:
        terminal_sha256 = hashlib.sha256(observation.birth_identity.encode()).hexdigest()
        if observation.birth_identity != identity.birth_identity:
            terminal_state = "identity_reused"
            outcome = "identity_changed"
        elif observation.state == "zombie":
            terminal_state = "zombie"
            outcome = "non_executing_zombie"
        elif observation.state == "running":
            terminal_state = "running"
            outcome = "still_running"
        else:
            terminal_state = "unknown"
            outcome = "unknown"
    return ProcessCleanupTargetReceipt(
        pid=identity.pid,
        pgid=identity.pgid,
        birth_identity_sha256=initial_sha256,
        terminal_birth_identity_sha256=terminal_sha256,
        terminal_state=terminal_state,
        termination_outcome=outcome,
    )


def cleanup_birth_bound_processes(  # noqa: C901, PLR0912, PLR0913, PLR0915
    identities: tuple[ProcessCleanupIdentity, ...],
    *,
    tracked_pids: tuple[int, ...],
    tracked_pgids: tuple[int, ...],
    tracked_pid_groups: tuple[tuple[int, int], ...] = (),
    observed_identities: tuple[ProcessCleanupIdentity, ...] = (),
    coverage_status: CleanupCoverage = "complete",
    coverage_stage: CleanupCoverageStage | None = None,
    coverage_subreason: CleanupCoverageSubreason | None = None,
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...] = (),
) -> ProcessCleanupTerminalSnapshot:
    """Clean one captured scope without ever signalling an unverified numeric identity."""
    _require_cleanup_coverage_diagnostic(
        coverage_status=coverage_status,
        stage=coverage_stage,
        subreason=coverage_subreason,
    )
    identity_by_pid: Mapping[int, ProcessCleanupIdentity] = MappingProxyType(
        {identity.pid: identity for identity in identities},
    )
    observed_by_pid: dict[int, list[ProcessCleanupIdentity]] = {}
    for observed in observed_identities:
        observed_by_pid.setdefault(observed.pid, []).append(observed)
    expected_groups_by_pid: dict[int, set[int]] = {}
    for pid, pgid in tracked_pid_groups:
        expected_groups_by_pid.setdefault(pid, set()).add(pgid)
    coverage_unknown = coverage_status == "unknown" or len(identity_by_pid) != len(identities)
    diagnostic = (
        (coverage_stage, coverage_subreason)
        if coverage_stage is not None and coverage_subreason is not None
        else None
    )
    if len(identity_by_pid) != len(identities) and diagnostic is None:
        diagnostic = ("group_member", "changed_identity_or_group_member")
    private_observations = list(offending_observations)
    tracked_groups, group_diagnostic, group_observations = _detect_tracked_group_members(
        tracked_pgids,
        identity_by_pid,
    )
    private_observations.extend(group_observations)
    coverage_unknown = coverage_unknown or group_diagnostic is not None
    diagnostic = diagnostic or group_diagnostic

    term_targets = tuple(
        identity
        for identity in reversed(tuple(identity_by_pid.values()))
        if _same_birth_running(identity)
    )
    signaled_pids: set[int] = set()
    for identity in term_targets:
        if _signal_same_birth(
            identity,
            signal.SIGTERM,
            group_leader=identity_by_pid.get(identity.pgid),
        ):
            signaled_pids.add(identity.pid)
    if signaled_pids:
        time.sleep(TERM_WAIT_SECONDS)

    kill_targets = tuple(
        identity
        for identity in reversed(tuple(identity_by_pid.values()))
        if _same_birth_running(identity)
    )
    for identity in kill_targets:
        if _signal_same_birth(
            identity,
            signal.SIGKILL,
            group_leader=identity_by_pid.get(identity.pgid),
        ):
            signaled_pids.add(identity.pid)
    if any(identity.pid in signaled_pids for identity in kill_targets):
        time.sleep(KILL_WAIT_SECONDS)

    # A child can join a tracked group after target selection. Detection can only make
    # coverage unknown; it can never expand the immutable pre-cleanup signal target set.
    rescan_diagnostic, rescan_observations = _terminal_rescan_tracked_groups(
        tracked_groups,
        identity_by_pid,
    )
    private_observations.extend(rescan_observations)
    coverage_unknown = rescan_diagnostic is not None or coverage_unknown
    diagnostic = diagnostic or rescan_diagnostic

    targets = tuple(
        observe_process_cleanup_target(identity)
        for identity in sorted(identity_by_pid.values(), key=lambda item: item.pid)
    )
    if any(target.termination_outcome == "unknown" for target in targets):
        coverage_unknown = True
        diagnostic = diagnostic or ("target_observation", "observation_unknown")

    for pid in sorted(set(tracked_pids) - set(identity_by_pid)):
        observation = read_process_observation(pid)
        historical = tuple(observed_by_pid.get(pid, ()))
        expected_groups = frozenset(expected_groups_by_pid.get(pid, ()))
        matching_births = tuple(
            identity
            for identity in historical
            if observation is not None and observation.birth_identity == identity.birth_identity
        )
        expected_identity = next(
            (
                identity
                for identity in matching_births
                if observation is not None and identity.pgid == observation.pgid
            ),
            matching_births[0] if matching_births else historical[0] if historical else None,
        )
        if observation is not None and observation.state == "unknown":
            coverage_unknown = True
            diagnostic = diagnostic or ("group_member", "observation_unknown")
            private_observations.append(
                _offending_process_observation(
                    pid=pid,
                    expected_pgid=(
                        expected_identity.pgid
                        if expected_identity is not None
                        else observation.pgid
                    ),
                    observation=observation,
                    detection_source="historical_pid_check",
                    admission_phase="admission_closed",
                    observation_state="unknown",
                    expected_birth_identity=(
                        expected_identity.birth_identity if expected_identity is not None else None
                    ),
                ),
            )
        elif observation is not None and historical and not matching_births:
            # A reliable current birth that differs from every observed historical birth proves
            # numeric PID reuse. Detection-only history never expands the signal target set.
            continue
        elif observation is not None and observation.state != "zombie":
            coverage_unknown = True
            moved_group = bool(
                expected_identity is not None and observation.pgid != expected_identity.pgid
            )
            diagnostic = diagnostic or (
                "group_member",
                ("changed_identity_or_group_member" if moved_group else "uncaptured_member"),
            )
            private_observations.append(
                _offending_process_observation(
                    pid=pid,
                    expected_pgid=(
                        expected_identity.pgid
                        if expected_identity is not None
                        else min(expected_groups)
                        if expected_groups
                        else observation.pgid
                    ),
                    observation=observation,
                    detection_source="historical_pid_check",
                    admission_phase="admission_closed",
                    observation_state="group_changed" if moved_group else None,
                    expected_birth_identity=(
                        expected_identity.birth_identity if expected_identity is not None else None
                    ),
                ),
            )

    return ProcessCleanupTerminalSnapshot(
        targets=targets,
        coverage_status="unknown" if coverage_unknown else "complete",
        coverage_stage=diagnostic[0] if coverage_unknown and diagnostic is not None else None,
        coverage_subreason=(diagnostic[1] if coverage_unknown and diagnostic is not None else None),
        signaled_process_count=len(signaled_pids),
        offending_observations=_dedupe_offending_observations(private_observations),
    )


def _same_birth_running(identity: ProcessCleanupIdentity) -> bool:
    observation = read_process_observation(identity.pid)
    return bool(
        observation is not None
        and observation.state == "running"
        and observation.birth_identity == identity.birth_identity
    )


def _signal_same_birth(
    identity: ProcessCleanupIdentity,
    sig: signal.Signals,
    *,
    group_leader: ProcessCleanupIdentity | None,
) -> bool:
    """Recheck target and group-owner birth identity immediately before os.kill."""
    observation = read_process_observation(identity.pid)
    if not (
        observation is not None
        and observation.state == "running"
        and observation.birth_identity == identity.birth_identity
    ):
        return False
    if identity.pid != identity.pgid:
        if group_leader is None or group_leader.pid != identity.pgid:
            return False
        leader_observation = read_process_observation(group_leader.pid)
        if leader_observation is not None and not (
            leader_observation.state != "unknown"
            and leader_observation.pgid == identity.pgid
            and leader_observation.birth_identity == group_leader.birth_identity
        ):
            return False
    _signal_pid(identity.pid, sig)
    return True


def _detect_tracked_group_members(
    tracked_pgids: tuple[int, ...],
    identities: Mapping[int, ProcessCleanupIdentity],
) -> tuple[
    set[int],
    tuple[CleanupCoverageStage, CleanupCoverageSubreason] | None,
    tuple[ProcessCleanupOffendingObservation, ...],
]:
    """Detect uncaptured members without ever expanding the cleanup target set."""
    tracked_groups = set(tracked_pgids)
    diagnostic: tuple[CleanupCoverageStage, CleanupCoverageSubreason] | None = None
    observations: list[ProcessCleanupOffendingObservation] = []
    for pgid in sorted(tracked_groups):
        members, group_observed = process_group_members_with_coverage(pgid)
        if not group_observed:
            diagnostic = diagnostic or ("group_table", "table_incomplete")
            continue
        member_diagnostic, member_observations = _compare_group_members(
            members,
            pgid=pgid,
            identities=identities,
            detection_source="pre_signal_group_scan",
        )
        diagnostic = diagnostic or member_diagnostic
        observations.extend(member_observations)
    return tracked_groups, diagnostic, _dedupe_offending_observations(observations)


def _terminal_rescan_tracked_groups(
    tracked_groups: set[int],
    identities: Mapping[int, ProcessCleanupIdentity],
) -> tuple[
    tuple[CleanupCoverageStage, CleanupCoverageSubreason] | None,
    tuple[ProcessCleanupOffendingObservation, ...],
]:
    """Detect late members so incomplete cleanup cannot publish a false zero."""
    diagnostic: tuple[CleanupCoverageStage, CleanupCoverageSubreason] | None = None
    observations: list[ProcessCleanupOffendingObservation] = []
    for pgid in sorted(tracked_groups):
        members, group_observed = process_group_members_with_coverage(pgid)
        if not group_observed:
            diagnostic = diagnostic or ("group_table", "table_incomplete")
            continue
        member_diagnostic, member_observations = _compare_group_members(
            members,
            pgid=pgid,
            identities=identities,
            detection_source="post_signal_group_rescan",
        )
        diagnostic = diagnostic or member_diagnostic
        observations.extend(member_observations)
    return diagnostic, _dedupe_offending_observations(observations)


def _compare_group_members(
    members: tuple[int, ...],
    *,
    pgid: int,
    identities: Mapping[int, ProcessCleanupIdentity],
    detection_source: Literal["pre_signal_group_scan", "post_signal_group_rescan"],
) -> tuple[
    tuple[CleanupCoverageStage, CleanupCoverageSubreason] | None,
    tuple[ProcessCleanupOffendingObservation, ...],
]:
    """Return unknown for any member not matching the pre-cleanup captured identities."""
    diagnostic: tuple[CleanupCoverageStage, CleanupCoverageSubreason] | None = None
    offending: list[ProcessCleanupOffendingObservation] = []
    for pid in sorted(set(members)):
        existing = identities.get(pid)
        observation = read_process_observation(pid)
        if observation is None:
            if existing is None:
                diagnostic = diagnostic or ("group_member", "uncaptured_member")
                offending.append(
                    _offending_process_observation(
                        pid=pid,
                        expected_pgid=pgid,
                        observation=None,
                        detection_source=detection_source,
                        admission_phase="admission_closed",
                        observation_state="absent",
                    ),
                )
            continue
        if observation.state == "unknown":
            diagnostic = diagnostic or ("group_member", "observation_unknown")
            offending.append(
                _offending_process_observation(
                    pid=pid,
                    expected_pgid=pgid,
                    observation=observation,
                    detection_source=detection_source,
                    admission_phase="admission_closed",
                    observation_state="unknown",
                    expected_birth_identity=(
                        existing.birth_identity if existing is not None else None
                    ),
                ),
            )
            continue
        if observation.pgid != pgid:
            diagnostic = diagnostic or (
                "group_member",
                "changed_identity_or_group_member",
            )
            offending.append(
                _offending_process_observation(
                    pid=pid,
                    expected_pgid=pgid,
                    observation=observation,
                    detection_source=detection_source,
                    admission_phase="admission_closed",
                    observation_state="group_changed",
                    expected_birth_identity=(
                        existing.birth_identity if existing is not None else None
                    ),
                ),
            )
            continue
        if existing is not None:
            if existing.pgid != pgid or existing.birth_identity != observation.birth_identity:
                diagnostic = diagnostic or (
                    "group_member",
                    "changed_identity_or_group_member",
                )
                offending.append(
                    _offending_process_observation(
                        pid=pid,
                        expected_pgid=pgid,
                        observation=observation,
                        detection_source=detection_source,
                        admission_phase="admission_closed",
                        observation_state=(
                            "group_changed" if existing.pgid != pgid else "identity_changed"
                        ),
                        expected_birth_identity=existing.birth_identity,
                    ),
                )
            continue
        diagnostic = diagnostic or ("group_member", "uncaptured_member")
        offending.append(
            _offending_process_observation(
                pid=pid,
                expected_pgid=pgid,
                observation=observation,
                detection_source=detection_source,
                admission_phase="admission_closed",
            ),
        )
    return diagnostic, _dedupe_offending_observations(offending)


def _offending_process_observation(  # noqa: PLR0913
    *,
    pid: int,
    expected_pgid: int,
    observation: ProcessObservation | None,
    detection_source: CleanupObservationDetectionSource,
    admission_phase: CleanupAdmissionPhase,
    observation_state: CleanupObservationState | None = None,
    expected_birth_identity: str | None = None,
) -> ProcessCleanupOffendingObservation:
    expected_birth_sha256 = (
        hashlib.sha256(expected_birth_identity.encode()).hexdigest()
        if expected_birth_identity
        else None
    )
    if observation is None:
        return ProcessCleanupOffendingObservation(
            pid=pid,
            pgid=expected_pgid,
            expected_pgid=expected_pgid,
            ppid=None,
            birth_identity_sha256=None,
            expected_birth_identity_sha256=expected_birth_sha256,
            detection_source=detection_source,
            observation_state=observation_state or "absent",
            admission_phase=admission_phase,
            occurrence_count=1,
            process_category=None,
            process_identity_sha256=None,
        )
    birth_sha256 = (
        hashlib.sha256(observation.birth_identity.encode()).hexdigest()
        if observation.birth_identity
        else None
    )
    process_identity_sha256 = observation.process_identity_sha256
    if (
        observation.process_category is None
        and process_identity_sha256 is None
        and observation.birth_identity
    ):
        process_identity_sha256 = _digest(
            {"opaque_process_identity": observation.birth_identity},
        )
    return ProcessCleanupOffendingObservation(
        pid=pid,
        pgid=observation.pgid,
        expected_pgid=expected_pgid,
        ppid=observation.ppid,
        birth_identity_sha256=birth_sha256,
        expected_birth_identity_sha256=expected_birth_sha256,
        detection_source=detection_source,
        observation_state=observation_state or observation.state,
        admission_phase=admission_phase,
        occurrence_count=1,
        process_category=observation.process_category,
        process_identity_sha256=(
            None if observation.process_category is not None else process_identity_sha256
        ),
    )


def _dedupe_offending_observations(
    observations: list[ProcessCleanupOffendingObservation]
    | tuple[ProcessCleanupOffendingObservation, ...],
) -> tuple[ProcessCleanupOffendingObservation, ...]:
    return merge_process_cleanup_observations(observations)


def merge_process_cleanup_observations(
    observations: list[ProcessCleanupOffendingObservation]
    | tuple[ProcessCleanupOffendingObservation, ...],
) -> tuple[ProcessCleanupOffendingObservation, ...]:
    """Merge identical private observations while preserving occurrence counts."""
    unique: dict[str, ProcessCleanupOffendingObservation] = {}
    for observation in observations:
        material = observation.model_dump(mode="json", exclude={"occurrence_count"})
        key = _digest(material)
        existing = unique.get(key)
        observation_count = observation.occurrence_count
        existing_count = existing.occurrence_count if existing is not None else None
        if observation_count is None or (existing is not None and existing_count is None):
            unique[f"{key}:{len(unique)}"] = observation
            continue
        if existing is None:
            unique[key] = observation
            continue
        if existing_count is None:
            raise ValueError("cleanup observation count became unknown")
        unique[key] = existing.model_copy(
            update={
                "occurrence_count": existing_count + observation_count,
            },
        )
    return tuple(unique.values())


def write_command_cleanup_identity_receipt(  # noqa: PLR0913
    path: Path,
    *,
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    root_pid: int,
    root_pgid: int,
    identities: tuple[ProcessCleanupIdentity, ...] | None = None,
    terminal_snapshot: ProcessCleanupTerminalSnapshot | None = None,
) -> CommandCleanupIdentityEvidence:
    if terminal_snapshot is None:
        supplied_identities = identities or ()
        try:
            targets = tuple(
                observe_process_cleanup_target(identity) for identity in supplied_identities
            )
        except (OSError, ValueError):
            return _write_command_cleanup_unknown_receipt(
                path,
                name=name,
                argv=argv,
                cwd=cwd,
                root_pid=root_pid,
                root_pgid=root_pgid,
                unknown_reason="target_observation_unknown",
                watcher_drain_status="not-applicable",
                coverage_status="unknown",
                coverage_stage="target_observation",
                coverage_subreason="observation_unknown",
                target_count=len(supplied_identities),
            )
        terminal_snapshot = ProcessCleanupTerminalSnapshot(
            targets=targets,
            coverage_status=(
                "unknown"
                if any(target.termination_outcome == "unknown" for target in targets)
                else "complete"
            ),
            coverage_stage=(
                "target_observation"
                if any(target.termination_outcome == "unknown" for target in targets)
                else None
            ),
            coverage_subreason=(
                "observation_unknown"
                if any(target.termination_outcome == "unknown" for target in targets)
                else None
            ),
        )
    if terminal_snapshot.cleanup_status == "unknown":
        unknown_reason = terminal_snapshot.unknown_reason or "cleanup_state_unknown"
        return _write_command_cleanup_unknown_receipt(
            path,
            name=name,
            argv=argv,
            cwd=cwd,
            root_pid=root_pid,
            root_pgid=root_pgid,
            unknown_reason=unknown_reason,
            watcher_drain_status=terminal_snapshot.watcher_drain_status,
            coverage_status=terminal_snapshot.coverage_status,
            coverage_stage=terminal_snapshot.coverage_stage,
            coverage_subreason=terminal_snapshot.coverage_subreason,
            target_count=len(terminal_snapshot.targets),
            offending_observations=terminal_snapshot.offending_observations,
        )
    targets = terminal_snapshot.targets
    if not targets:
        return CommandCleanupIdentityEvidence(evidence_status="no-target-observed")
    material = {
        "schema_version": "1",
        "receipt_kind": "command_cleanup_identity",
        "command_identity_sha256": _digest(
            {"name": name, "argv": argv, "cwd": str(cwd.resolve())},
        ),
        "root_pid": root_pid,
        "root_pgid": root_pgid,
        "target_count": len(targets),
        "targets": tuple(target.model_dump(mode="python") for target in targets),
        "watcher_drain_status": terminal_snapshot.watcher_drain_status,
    }
    receipt = CommandCleanupIdentityReceipt.model_validate(
        {**material, "receipt_sha256": _digest(material)},
        strict=True,
    )
    if not _atomic_owner_only_write(path, receipt.model_dump_json() + "\n"):
        return CommandCleanupIdentityEvidence(
            evidence_status="write-failed",
            unknown_reason="cleanup_receipt_write_failed",
        )
    return CommandCleanupIdentityEvidence(
        evidence_status="authenticated",
        receipt_sha256=receipt.receipt_sha256,
    )


def _write_command_cleanup_unknown_receipt(  # noqa: PLR0913
    path: Path,
    *,
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    root_pid: int,
    root_pgid: int,
    unknown_reason: CleanupUnknownReason,
    watcher_drain_status: WatcherDrainState,
    coverage_status: Literal["complete", "unknown"],
    coverage_stage: CleanupCoverageStage | None,
    coverage_subreason: CleanupCoverageSubreason | None,
    target_count: int,
    offending_observations: tuple[ProcessCleanupOffendingObservation, ...] = (),
) -> CommandCleanupIdentityEvidence:
    material = {
        "schema_version": "1",
        "receipt_kind": "command_cleanup_unknown",
        "command_identity_sha256": _digest(
            {"name": name, "argv": argv, "cwd": str(cwd.resolve())},
        ),
        "root_pid": root_pid,
        "root_pgid": root_pgid,
        "unknown_reason": unknown_reason,
        "watcher_drain_status": watcher_drain_status,
        "coverage_status": coverage_status,
        "coverage_stage": coverage_stage,
        "coverage_subreason": coverage_subreason,
        "target_count": target_count,
        "remaining_process_count": None,
        "remaining_process_group_count": None,
        "offending_observations": tuple(
            observation.model_dump(mode="python") for observation in offending_observations
        ),
        "observation_evidence_state": "current" if offending_observations else "none",
    }
    receipt = CommandCleanupUnknownReceipt.model_validate(
        {**material, "receipt_sha256": _digest(material)},
        strict=True,
    )
    if not _atomic_owner_only_write(path, receipt.model_dump_json() + "\n"):
        return CommandCleanupIdentityEvidence(
            evidence_status="write-failed",
            unknown_reason="cleanup_receipt_write_failed",
        )
    return CommandCleanupIdentityEvidence(
        evidence_status="observation-unknown",
        receipt_sha256=receipt.receipt_sha256,
        unknown_reason=receipt.unknown_reason,
        cleanup_coverage_stage=receipt.coverage_stage,
        cleanup_coverage_subreason=receipt.coverage_subreason,
    )


def verify_command_cleanup_identity_receipt(
    path: Path,
    *,
    expected_receipt_sha256: str,
) -> CommandCleanupIdentityReceipt | None:
    try:
        metadata = os.lstat(path)
        receipt = CommandCleanupIdentityReceipt.model_validate_json(
            path.read_bytes(),
            strict=True,
        )
    except (OSError, ValidationError):
        return None
    if not (
        path.is_absolute()
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
        and receipt.receipt_sha256 == expected_receipt_sha256
    ):
        return None
    return receipt


def verify_command_cleanup_unknown_receipt(
    path: Path,
    *,
    expected_receipt_sha256: str,
) -> CommandCleanupUnknownReceipt | None:
    """Verify one owner-only diagnostic receipt without exposing command details."""
    try:
        metadata = os.lstat(path)
        receipt = CommandCleanupUnknownReceipt.model_validate_json(
            path.read_bytes(),
            strict=True,
        )
    except (OSError, ValidationError):
        return None
    if not (
        path.is_absolute()
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
        and receipt.receipt_sha256 == expected_receipt_sha256
    ):
        return None
    return receipt


def write_eval_process_cleanup_receipt(
    path: Path,
    *,
    snapshots: tuple[ProcessCleanupTerminalSnapshot, ...],
) -> CommandCleanupIdentityEvidence:
    """Persist nested cleanup observations without command text or raw process identity."""
    unknown_snapshots = tuple(
        snapshot for snapshot in snapshots if snapshot.cleanup_status == "unknown"
    )
    observations = _dedupe_offending_observations(
        [
            observation
            for snapshot in unknown_snapshots
            for observation in snapshot.offending_observations
        ],
    )
    evidence_snapshots = tuple(
        snapshot
        for snapshot in unknown_snapshots
        if snapshot.coverage_stage is not None
        and snapshot.coverage_subreason is not None
        and snapshot.offending_observations
    )
    if not observations or not evidence_snapshots:
        return CommandCleanupIdentityEvidence(
            evidence_status="write-failed",
            unknown_reason="cleanup_evidence_unavailable",
        )
    material = {
        "schema_version": "1",
        "receipt_kind": "eval_process_cleanup_unknown",
        "snapshot_count": len(snapshots),
        "unknown_snapshot_count": len(unknown_snapshots),
        "cleanup_snapshots": tuple(
            {
                "coverage_stage": snapshot.coverage_stage,
                "coverage_subreason": snapshot.coverage_subreason,
                "offending_observation_count": sum(
                    observation.occurrence_count or 0
                    for observation in snapshot.offending_observations
                ),
                "offending_observations": tuple(
                    observation.model_dump(mode="python")
                    for observation in snapshot.offending_observations
                ),
            }
            for snapshot in evidence_snapshots
        ),
        "offending_observation_count": sum(
            observation.occurrence_count or 0 for observation in observations
        ),
        "offending_observations": tuple(
            observation.model_dump(mode="python") for observation in observations
        ),
    }
    receipt = EvalProcessCleanupUnknownReceipt.model_validate(
        {**material, "receipt_sha256": _digest(material)},
        strict=True,
    )
    if not _atomic_owner_only_write(path, receipt.model_dump_json() + "\n"):
        return CommandCleanupIdentityEvidence(
            evidence_status="write-failed",
            unknown_reason="cleanup_receipt_write_failed",
        )
    first_diagnostic = receipt.cleanup_snapshots[0]
    return CommandCleanupIdentityEvidence(
        evidence_status="observation-unknown",
        receipt_sha256=receipt.receipt_sha256,
        unknown_reason="coverage_unknown",
        cleanup_coverage_stage=first_diagnostic.coverage_stage,
        cleanup_coverage_subreason=first_diagnostic.coverage_subreason,
    )


def verify_eval_process_cleanup_receipt(
    path: Path,
    *,
    expected_receipt_sha256: str,
) -> EvalProcessCleanupUnknownReceipt | None:
    """Verify one owner-only nested cleanup diagnostic receipt."""
    try:
        metadata = os.lstat(path)
        receipt = EvalProcessCleanupUnknownReceipt.model_validate_json(
            path.read_bytes(),
            strict=True,
        )
    except (OSError, ValidationError):
        return None
    if not (
        path.is_absolute()
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
        and receipt.receipt_sha256 == expected_receipt_sha256
    ):
        return None
    return receipt


def retain_eval_process_cleanup_receipt(
    source: Path,
    destination: Path,
    *,
    expected_receipt_sha256: str,
) -> EvalProcessCleanupUnknownReceipt | None:
    """Verify and atomically retain one nested receipt outside its temporary run root."""
    receipt = verify_eval_process_cleanup_receipt(
        source,
        expected_receipt_sha256=expected_receipt_sha256,
    )
    if receipt is None:
        return None
    if not _atomic_owner_only_create(destination, receipt.model_dump_json() + "\n"):
        return None
    return verify_eval_process_cleanup_receipt(
        destination,
        expected_receipt_sha256=expected_receipt_sha256,
    )


def _atomic_owner_only_create(path: Path, text: str) -> bool:
    """Atomically publish one immutable owner-only file without replacing a target."""
    if not path.is_absolute():
        return False
    parent = path.parent
    descriptor: int | None = None
    temporary: Path | None = None
    directory_descriptor: int | None = None
    try:
        parent_metadata = os.lstat(parent)
        if not (
            stat.S_ISDIR(parent_metadata.st_mode)
            and not stat.S_ISLNK(parent_metadata.st_mode)
            and parent_metadata.st_uid == os.getuid()
            and stat.S_IMODE(parent_metadata.st_mode) == _OWNER_DIRECTORY_MODE
        ):
            return False
        descriptor, raw_temporary = tempfile.mkstemp(
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
        os.fchmod(descriptor, _OWNER_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(directory_descriptor)
        temporary.unlink()
        temporary = None
        os.fsync(directory_descriptor)
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
    return True


def _atomic_owner_only_write(path: Path, text: str) -> bool:
    if not path.is_absolute():
        return False
    parent = path.parent
    temporary: Path | None = None
    try:
        parent_metadata = os.lstat(parent)
        if not (
            stat.S_ISDIR(parent_metadata.st_mode)
            and not stat.S_ISLNK(parent_metadata.st_mode)
            and parent_metadata.st_uid == os.getuid()
            and stat.S_IMODE(parent_metadata.st_mode) == _OWNER_DIRECTORY_MODE
        ):
            return False
        if path.exists() or path.is_symlink():
            existing = os.lstat(path)
            if not (
                stat.S_ISREG(existing.st_mode)
                and not stat.S_ISLNK(existing.st_mode)
                and existing.st_uid == os.getuid()
                and existing.st_nlink == 1
                and stat.S_IMODE(existing.st_mode) == _OWNER_FILE_MODE
            ):
                return False
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            _OWNER_FILE_MODE,
        )
        try:
            os.fchmod(descriptor, _OWNER_FILE_MODE)
            os.write(descriptor, text.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(path)
        path.chmod(_OWNER_FILE_MODE)
    except OSError:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return False
    else:
        return True


def _require_cleanup_identity_evidence_consistency(
    status: CleanupIdentityEvidenceKind,
    receipt_sha256: str | None,
    unknown_reason: CleanupUnknownReason | None,
    coverage_stage: CleanupCoverageStage | None,
    coverage_subreason: CleanupCoverageSubreason | None,
) -> None:
    coverage_diagnostic_present = coverage_stage is not None or coverage_subreason is not None
    if coverage_diagnostic_present:
        _require_cleanup_coverage_diagnostic(
            coverage_status="unknown",
            stage=coverage_stage,
            subreason=coverage_subreason,
        )
    if status == "observation-unknown":
        if unknown_reason == "coverage_unknown" and (
            not coverage_diagnostic_present or coverage_stage == "target_observation"
        ):
            raise ValueError("coverage-unknown evidence requires its coverage diagnostic")
        if unknown_reason == "target_observation_unknown" and (
            coverage_stage != "target_observation" or coverage_subreason != "observation_unknown"
        ):
            raise ValueError("target-observation evidence requires its diagnostic")
    valid = {
        "authenticated": (
            receipt_sha256 is not None
            and unknown_reason is None
            and not coverage_diagnostic_present
        ),
        "no-target-observed": (
            receipt_sha256 is None and unknown_reason is None and not coverage_diagnostic_present
        ),
        "observation-unknown": (
            receipt_sha256 is not None
            and unknown_reason
            in {
                "watcher_publication_discarded",
                "watcher_still_running",
                "watcher_drain_unknown",
                "coverage_unknown",
                "target_observation_unknown",
                "cleanup_state_unknown",
            }
        ),
        "write-failed": (
            receipt_sha256 is None
            and not coverage_diagnostic_present
            and unknown_reason
            in {
                "cleanup_receipt_path_missing",
                "cleanup_receipt_write_failed",
                "cleanup_evidence_inconsistent",
                "cleanup_evidence_unavailable",
            }
        ),
    }[status]
    if not valid:
        raise ValueError("cleanup identity evidence status and digest differ")


def _require_cleanup_coverage_diagnostic(
    *,
    coverage_status: CleanupCoverage,
    stage: CleanupCoverageStage | None,
    subreason: CleanupCoverageSubreason | None,
    allow_legacy_missing: bool = False,
) -> None:
    if (stage is None) != (subreason is None):
        raise ValueError("cleanup coverage stage and subreason must be paired")
    if stage is None:
        if coverage_status == "unknown" and not allow_legacy_missing:
            raise ValueError("unknown cleanup coverage requires a diagnostic")
        return
    if coverage_status != "unknown":
        raise ValueError("complete cleanup coverage cannot carry a diagnostic")
    allowed: dict[CleanupCoverageStage, frozenset[CleanupCoverageSubreason]] = {
        "first_snapshot": frozenset({"snapshot_failure"}),
        "second_snapshot": frozenset({"snapshot_failure"}),
        "watcher_capture": frozenset({"capture_failure"}),
        "post_exit_snapshot": frozenset({"snapshot_failure"}),
        "final_snapshot": frozenset({"snapshot_failure"}),
        "group_table": frozenset({"table_incomplete"}),
        "group_member": frozenset(
            {
                "uncaptured_member",
                "changed_identity_or_group_member",
                "observation_unknown",
            },
        ),
        "target_observation": frozenset({"observation_unknown"}),
    }
    if subreason not in allowed[stage]:
        raise ValueError("cleanup coverage stage and subreason differ")


def _observation_states_match_group_member_diagnostic(
    subreason: CleanupCoverageSubreason | None,
    states: set[CleanupObservationState],
) -> bool:
    expected_states: set[CleanupObservationState] | None = (
        {"running", "zombie", "absent"}
        if subreason == "uncaptured_member"
        else {"identity_changed", "group_changed"}
        if subreason == "changed_identity_or_group_member"
        else {"unknown"}
        if subreason == "observation_unknown"
        else None
    )
    return expected_states is not None and bool(states & expected_states)


def _cleanup_unknown_reason(
    *,
    watcher_drain_status: WatcherDrainState,
    coverage_status: Literal["complete", "unknown"],
    target_observation_unknown: bool,
) -> CleanupUnknownReason:
    if watcher_drain_status == "discarded":
        return "watcher_publication_discarded"
    if watcher_drain_status == "still-running":
        return "watcher_still_running"
    if watcher_drain_status == "unknown":
        return "watcher_drain_unknown"
    if target_observation_unknown:
        return "target_observation_unknown"
    if coverage_status == "unknown":
        return "coverage_unknown"
    return "cleanup_state_unknown"


def _digest(value: object) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(rendered).hexdigest()


def process_still_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remaining_live_pids(pids: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(pid for pid in sorted(set(pids)) if process_still_running(pid))


def remaining_live_pgids(pgids: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(
        pgid
        for pgid in sorted(set(pgids))
        if any(process_still_running(pid) for pid in process_group_members(pgid))
    )


def process_group_members(pgid: int) -> tuple[int, ...]:
    members: list[int] = []
    for pid, _ppid, group in _process_table():
        if group == pgid:
            members.append(pid)
    if not members and process_still_running(pgid):
        members.append(pgid)
    return tuple(sorted(set(members)))


def process_group_members_with_coverage(pgid: int) -> tuple[tuple[int, ...], bool]:
    """Return group members plus whether the process-table observation was complete."""
    table, observed = _process_table_checked()
    if not observed:
        return (), False
    members = tuple(sorted({pid for pid, _ppid, group in table if group == pgid}))
    if members:
        return members, True
    leader = read_process_observation(pgid)
    if leader is not None and leader.state == "unknown":
        return (), False
    if leader is not None and leader.pgid == pgid:
        return (pgid,), True
    return (), True


def descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Return root and all descendants via ppid walk, including escaped process groups."""
    table = _process_table()
    children: dict[int, list[int]] = {}
    for pid, ppid, _group in table:
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        stack.extend(children.get(current, ()))
    return tuple(sorted(pid for pid in found if process_still_running(pid)))


def terminate_process_group(pgid: int, *, escalate: bool) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        for pid in process_group_members(pgid):
            _signal_pid(pid, signal.SIGTERM)
    if _wait_pgid_exit(pgid, TERM_WAIT_SECONDS):
        return
    if not escalate:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        for pid in process_group_members(pgid):
            _signal_pid(pid, signal.SIGKILL)
    _wait_pgid_exit(pgid, KILL_WAIT_SECONDS)


def terminate_pid_tree(root_pid: int, *, escalate: bool) -> None:
    pids = descendant_pids(root_pid)
    for pid in reversed(pids):
        _signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + TERM_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not remaining_live_pids(pids):
            return
        time.sleep(0.05)
    if not escalate:
        return
    for pid in reversed(descendant_pids(root_pid)):
        _signal_pid(pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + KILL_WAIT_SECONDS
    while time.monotonic() < kill_deadline:
        if not remaining_live_pids(descendant_pids(root_pid)):
            return
        time.sleep(0.05)


def cleanup_recorded_groups(pgids: tuple[int, ...]) -> tuple[int, ...]:
    for pgid in sorted(set(pgids)):
        if remaining_live_pgids((pgid,)):
            terminate_process_group(pgid, escalate=True)
    return remaining_live_pgids(pgids)


def load_json_object(path: Path) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return {}


def shutil_which(command: str, *, path: str | None) -> str | None:
    import shutil  # noqa: PLC0415

    return shutil.which(command, path=path)


def _tree_snapshot_parts(
    snapshot: tuple[
        tuple[int, ...],
        tuple[int, ...],
        CleanupCoverage,
    ]
    | tuple[
        tuple[int, ...],
        tuple[int, ...],
        CleanupCoverage,
        tuple[tuple[int, int], ...],
    ]
    | tuple[
        tuple[int, ...],
        tuple[int, ...],
        CleanupCoverage,
        tuple[tuple[int, int], ...],
        tuple[ProcessCleanupIdentity, ...],
    ],
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    CleanupCoverage,
    tuple[tuple[int, int], ...],
    tuple[ProcessCleanupIdentity, ...],
]:
    """Normalize legacy test snapshots to the birth-bound production shape."""
    if len(snapshot) == 3:  # noqa: PLR2004
        pids, pgids, coverage = snapshot
        return pids, pgids, coverage, (), ()
    if len(snapshot) == 4:  # noqa: PLR2004
        pids, pgids, coverage, pid_groups = snapshot
        return pids, pgids, coverage, pid_groups, ()
    return snapshot


def _snapshot_tree_checked(
    root_pid: int | None,
    pgid: int | None,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    CleanupCoverage,
    tuple[tuple[int, int], ...],
    tuple[ProcessCleanupIdentity, ...],
]:
    """Capture one tree/group view with same-row process birth identities."""
    pids: set[int] = set()
    pgids: set[int] = set()
    table, observed = _process_identity_table_checked()
    children: dict[int, list[int]] = {}
    group_by_pid: dict[int, int] = {}
    identity_by_pid: dict[int, ProcessCleanupIdentity] = {}
    for ppid, identity in table:
        pid = identity.pid
        group = identity.pgid
        children.setdefault(ppid, []).append(pid)
        group_by_pid[pid] = group
        identity_by_pid[pid] = identity
    if root_pid is not None:
        stack = [root_pid]
        while stack:
            current = stack.pop()
            if current in pids:
                continue
            pids.add(current)
            stack.extend(children.get(current, ()))
    if pgid is not None:
        pgids.add(pgid)
        if root_pid is not None:
            group_by_pid.setdefault(root_pid, pgid)
        pids.update(pid for pid, group in group_by_pid.items() if group == pgid)
    for pid in tuple(pids):
        group = group_by_pid.get(pid)
        if group is not None:
            pgids.add(group)
    pids.update(pid for pid, group in group_by_pid.items() if group in pgids)
    return (
        tuple(sorted(pids)),
        tuple(sorted(pgids)),
        "complete" if observed else "unknown",
        tuple(sorted((pid, group_by_pid[pid]) for pid in pids if pid in group_by_pid)),
        tuple(identity_by_pid[pid] for pid in sorted(pids) if pid in identity_by_pid),
    )


def _merge_snapshots(
    pids_a: tuple[int, ...],
    pgids_a: tuple[int, ...],
    pids_b: tuple[int, ...],
    pgids_b: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(sorted(set(pids_a) | set(pids_b))), tuple(sorted(set(pgids_a) | set(pgids_b)))


def _process_identity_table_checked() -> tuple[
    tuple[tuple[int, ProcessCleanupIdentity], ...],
    bool,
]:
    """Read PID, parent, group, state, and birth in one process-table row."""
    try:
        output = subprocess.run(
            ("ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="),  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return (), False
    if output.returncode != 0:
        return (), False
    rows: list[tuple[int, ProcessCleanupIdentity]] = []
    malformed = False
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) != 9:  # noqa: PLR2004
            malformed = malformed or bool(parts)
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
        except ValueError:
            malformed = True
            continue
        state_code = parts[3][:1].upper()
        state: Literal["running", "zombie", "unknown"] = (
            "zombie" if state_code == "Z" else "running" if state_code else "unknown"
        )
        rows.append(
            (
                ppid,
                ProcessCleanupIdentity(
                    pid=pid,
                    pgid=pgid,
                    birth_identity=" ".join(parts[4:9]),
                    initial_state=state,
                ),
            ),
        )
    return tuple(rows), not malformed


def _process_table() -> tuple[tuple[int, int, int], ...]:
    return _process_table_checked()[0]


def _process_table_checked() -> tuple[tuple[tuple[int, int, int], ...], bool]:
    try:
        output = subprocess.run(
            ("ps", "-axo", "pid=,ppid=,pgid="),  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return (), False
    if output.returncode != 0:
        return (), False
    rows: list[tuple[int, int, int]] = []
    malformed = False
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:  # noqa: PLR2004
            malformed = malformed or bool(parts)
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            malformed = True
            continue
    return tuple(rows), not malformed


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _wait_pgid_exit(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not remaining_live_pgids((pgid,)):
            return True
        time.sleep(0.05)
    return not remaining_live_pgids((pgid,))


def _receipt(  # noqa: PLR0913
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    pid: int | None,
    pgid: int | None,
    exit_code: int,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool,
    cleanup_attempted: bool,
) -> CommandReceipt:
    return CommandReceipt(
        name=name,
        argv=argv,
        cwd=str(cwd),
        pid=pid,
        pgid=pgid,
        exit_code=exit_code,
        stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
        timed_out=timed_out,
        cleanup_attempted=cleanup_attempted,
    )
