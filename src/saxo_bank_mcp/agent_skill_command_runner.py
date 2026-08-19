from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
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


@dataclass(frozen=True, slots=True)
class ProcessCleanupTerminalSnapshot:
    """One birth-bound terminal view used for cleanup status, receipt, and counts."""

    targets: tuple[ProcessCleanupTargetReceipt, ...]
    coverage_status: Literal["complete", "unknown"]
    signaled_process_count: int = 0
    watcher_drain_status: WatcherDrainState = "not-applicable"

    @property
    def cleanup_status(self) -> Literal["complete", "failed", "unknown"]:
        if self.watcher_drain_status in {"discarded", "still-running", "unknown"}:
            return "unknown"
        if self.coverage_status == "unknown" or any(
            target.termination_outcome == "unknown" for target in self.targets
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
            target_observation_unknown=any(
                target.termination_outcome == "unknown" for target in self.targets
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
    target_count: int = Field(ge=0)
    remaining_process_count: int | None = Field(default=None, ge=0)
    remaining_process_group_count: int | None = Field(default=None, ge=0)
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        expected = _cleanup_unknown_reason(
            watcher_drain_status=self.watcher_drain_status,
            coverage_status=self.coverage_status,
            target_observation_unknown=self.unknown_reason == "target_observation_unknown",
        )
        if self.unknown_reason != expected:
            raise ValueError("cleanup unknown reason differs from semantic state")
        if (
            self.remaining_process_count is not None
            or self.remaining_process_group_count is not None
        ):
            raise ValueError("unknown cleanup receipt cannot prove remaining counts")
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _digest(material):
            raise ValueError("cleanup unknown receipt digest mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    pgid: int
    birth_identity: str
    state: Literal["running", "zombie", "unknown"]


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


@dataclass(slots=True)
class RootBoundProcessCleanupAdmission:
    """Admit cleanup identities only inside one live, same-birth root window."""

    process: subprocess.Popen[str]
    root_pgid: int
    root_identity: ProcessCleanupIdentity | None
    _closed: threading.Event = field(default_factory=threading.Event, repr=False)

    def close(self) -> None:
        self._closed.set()

    def capture_scope(self) -> ProcessCleanupScope:
        """Take two root-bracketed snapshots and admit only exact stable identities."""
        root_pid = self.process.pid
        root_identities = (self.root_identity,) if self.root_identity is not None else ()
        tracked_pids: set[int] = {root_pid}
        tracked_pgids: set[int] = {self.root_pgid}
        if not self._root_allows_admission():
            return self._detection_scope(root_identities, tracked_pids, tracked_pgids)

        try:
            first_pids, first_pgids = _snapshot_tree(root_pid, self.root_pgid)
        except OSError:
            self.close()
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
            )
        tracked_pids.update(first_pids)
        tracked_pgids.update(first_pgids)
        observations = tuple(
            observation
            for pid in first_pids
            if (observation := read_process_observation(pid)) is not None
            and observation.state != "unknown"
        )
        if not self._root_allows_admission():
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
            )

        try:
            second_pids, second_pgids = _snapshot_tree(root_pid, self.root_pgid)
        except OSError:
            self.close()
            return ProcessCleanupScope(
                identities=root_identities,
                tracked_pids=tuple(sorted(tracked_pids)),
                tracked_pgids=tuple(sorted(tracked_pgids)),
            )
        tracked_pids.update(second_pids)
        tracked_pgids.update(second_pgids)
        second_pid_scope = frozenset(second_pids)
        second_pgid_scope = frozenset(second_pgids)
        confirmed: dict[int, ProcessCleanupIdentity] = {
            identity.pid: identity for identity in root_identities
        }
        for observation in observations:
            reobserved = read_process_observation(observation.pid)
            if not (
                reobserved is not None
                and reobserved.state != "unknown"
                and reobserved.pid == observation.pid
                and reobserved.birth_identity == observation.birth_identity
                and reobserved.pgid == observation.pgid
                and observation.pid in second_pid_scope
                and observation.pgid in second_pgid_scope
            ):
                continue
            confirmed[observation.pid] = ProcessCleanupIdentity(
                pid=observation.pid,
                pgid=observation.pgid,
                birth_identity=observation.birth_identity,
                initial_state=observation.state,
            )
        if not self._root_allows_admission():
            confirmed = {identity.pid: identity for identity in root_identities}
        return ProcessCleanupScope(
            identities=tuple(confirmed[pid] for pid in sorted(confirmed)),
            tracked_pids=tuple(sorted(tracked_pids)),
            tracked_pgids=tuple(sorted(tracked_pgids)),
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

    def _detection_scope(
        self,
        identities: tuple[ProcessCleanupIdentity, ...],
        tracked_pids: set[int],
        tracked_pgids: set[int],
    ) -> ProcessCleanupScope:
        try:
            pids, pgids = _snapshot_tree(self.process.pid, self.root_pgid)
        except OSError:
            pids, pgids = (), ()
        tracked_pids.update(pids)
        tracked_pgids.update(pgids)
        return ProcessCleanupScope(
            identities=identities,
            tracked_pids=tuple(sorted(tracked_pids)),
            tracked_pgids=tuple(sorted(tracked_pgids)),
        )


@dataclass(frozen=True, slots=True)
class CommandCleanupIdentityEvidence:
    evidence_status: CleanupIdentityEvidenceKind
    receipt_sha256: str | None = None
    unknown_reason: CleanupUnknownReason | None = None

    def __post_init__(self) -> None:
        """Require evidence kind, digest, and safe reason to agree."""
        _require_cleanup_identity_evidence_consistency(
            self.evidence_status,
            self.receipt_sha256,
            self.unknown_reason,
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    receipt: CommandReceipt
    stdout: str
    stderr: str
    cleanup_identity_receipt_sha256: str | None = None
    cleanup_identity_evidence_status: CleanupIdentityEvidenceKind = "no-target-observed"
    cleanup_unknown_reason: CleanupUnknownReason | None = None

    def __post_init__(self) -> None:
        """Require command cleanup status and digest consistency."""
        _require_cleanup_identity_evidence_consistency(
            self.cleanup_identity_evidence_status,
            self.cleanup_identity_receipt_sha256,
            self.cleanup_unknown_reason,
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

    def __post_init__(self) -> None:
        """Require failed-command cleanup status and digest consistency."""
        _require_cleanup_identity_evidence_consistency(
            self.cleanup_identity_evidence_status,
            self.cleanup_identity_receipt_sha256,
            self.cleanup_unknown_reason,
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
    tracked_identities: dict[int, ProcessCleanupIdentity] = {}
    stop_watch = threading.Event()
    watch_lock = threading.Lock()
    admission_publication_state: Literal["open", "draining", "frozen"] = "open"
    watcher_capture_inflight = 0
    watcher_capture_failed = False
    watcher_publication_discarded = False
    watcher_drain_status: WatcherDrainState = "not-applicable"
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = 124
    process_error: OSError | None = None
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

    def _capture_identities() -> None:
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
            raise
        with watch_lock:
            watcher_capture_inflight -= 1
            # Another thread can advance the state while capture_scope runs.
            if str(admission_publication_state) == "frozen":
                watcher_publication_discarded = True
                return
            tracked_pids[:] = sorted(set(tracked_pids) | set(scope.tracked_pids))
            tracked_pgids[:] = sorted(set(tracked_pgids) | set(scope.tracked_pgids))
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
        pids, pgids = _merge_snapshots(pids, pgids, *_snapshot_tree(root_pid, pgid))
        # Always include original process group: redirected sleepers share it after parent exit.
        # pgid is assigned from os.getpgid after Popen; keep it in the tracked set even if
        # the parent has already exited and descendants remain only under that group.
        tracked_pgid = pgid
        pgids = tuple(sorted(set(pgids) | {tracked_pgid}))
        pids = tuple(sorted(set(pids) | set(process_group_members(tracked_pgid))))
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
        try:
            final_pids, final_pgids = _snapshot_tree(root_pid, pgid)
        except OSError:
            final_pids, final_pgids = (), ()
        pids, pgids = _merge_snapshots(pids, pgids, final_pids, final_pgids)
        if root_pid is not None:
            pids = tuple(sorted(set(pids) | {root_pid}))
        if pgid is not None:
            pgids = tuple(sorted(set(pgids) | {pgid}))
            pids = tuple(sorted(set(pids) | set(process_group_members(pgid))))
        # Signal only still-running processes whose birth identity still matches. The returned
        # terminal snapshot is the sole source for the receipt and remaining candidate counts.
        terminal_snapshot = replace(
            cleanup_birth_bound_processes(
                identities,
                tracked_pids=pids,
                tracked_pgids=pgids,
            ),
            watcher_drain_status=watcher_drain_status,
        )
        if process is not None:
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
        ) from process_error

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
        )

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
        )
    return result


def read_process_observation(pid: int) -> ProcessObservation | None:
    """Read one local PID's group, state, and birth identity without process content."""
    try:
        completed = subprocess.run(
            ("ps", "-o", "pgid=,state=,lstart=", "-p", str(pid)),  # noqa: S607
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
    parts = line.split(maxsplit=2)
    if completed.returncode != 0 or len(parts) != 3:  # noqa: PLR2004
        return ProcessObservation(pid=pid, pgid=pid, birth_identity="", state="unknown")
    try:
        pgid = int(parts[0])
    except ValueError:
        return ProcessObservation(pid=pid, pgid=pid, birth_identity="", state="unknown")
    state_code = parts[1][:1].upper()
    state: Literal["running", "zombie", "unknown"] = (
        "zombie" if state_code == "Z" else "running" if state_code else "unknown"
    )
    return ProcessObservation(
        pid=pid,
        pgid=pgid,
        birth_identity=parts[2],
        state=state,
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


def cleanup_birth_bound_processes(
    identities: tuple[ProcessCleanupIdentity, ...],
    *,
    tracked_pids: tuple[int, ...],
    tracked_pgids: tuple[int, ...],
) -> ProcessCleanupTerminalSnapshot:
    """Clean one captured scope without ever signalling an unverified numeric identity."""
    identity_by_pid: Mapping[int, ProcessCleanupIdentity] = MappingProxyType(
        {identity.pid: identity for identity in identities},
    )
    coverage_unknown = len(identity_by_pid) != len(identities)
    tracked_groups, group_coverage_unknown = _detect_tracked_group_members(
        tracked_pgids,
        identity_by_pid,
    )
    coverage_unknown = coverage_unknown or group_coverage_unknown

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
    coverage_unknown = (
        _terminal_rescan_tracked_groups(tracked_groups, identity_by_pid) or coverage_unknown
    )

    targets = tuple(
        observe_process_cleanup_target(identity)
        for identity in sorted(identity_by_pid.values(), key=lambda item: item.pid)
    )
    if any(target.termination_outcome == "unknown" for target in targets):
        coverage_unknown = True

    for pid in sorted(set(tracked_pids) - set(identity_by_pid)):
        observation = read_process_observation(pid)
        if observation is not None and observation.state != "zombie":
            coverage_unknown = True

    return ProcessCleanupTerminalSnapshot(
        targets=targets,
        coverage_status="unknown" if coverage_unknown else "complete",
        signaled_process_count=len(signaled_pids),
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
) -> tuple[set[int], bool]:
    """Detect uncaptured members without ever expanding the cleanup target set."""
    tracked_groups = set(tracked_pgids)
    coverage_unknown = False
    for pgid in sorted(tracked_groups):
        members, group_observed = process_group_members_with_coverage(pgid)
        if not group_observed:
            coverage_unknown = True
            continue
        coverage_unknown = (
            _compare_group_members(
                members,
                pgid=pgid,
                identities=identities,
            )
            or coverage_unknown
        )
    return tracked_groups, coverage_unknown


def _terminal_rescan_tracked_groups(
    tracked_groups: set[int],
    identities: Mapping[int, ProcessCleanupIdentity],
) -> bool:
    """Detect late members so incomplete cleanup cannot publish a false zero."""
    coverage_unknown = False
    for pgid in sorted(tracked_groups):
        members, group_observed = process_group_members_with_coverage(pgid)
        if not group_observed:
            coverage_unknown = True
            continue
        coverage_unknown = (
            _compare_group_members(
                members,
                pgid=pgid,
                identities=identities,
            )
            or coverage_unknown
        )
    return coverage_unknown


def _compare_group_members(
    members: tuple[int, ...],
    *,
    pgid: int,
    identities: Mapping[int, ProcessCleanupIdentity],
) -> bool:
    """Return unknown for any member not matching the pre-cleanup captured identities."""
    unknown = False
    for pid in sorted(set(members)):
        existing = identities.get(pid)
        observation = read_process_observation(pid)
        if observation is None:
            if existing is None:
                unknown = True
            continue
        if observation.state == "unknown":
            unknown = True
            continue
        if observation.pgid != pgid:
            unknown = True
            continue
        if existing is not None:
            if existing.pgid != pgid or existing.birth_identity != observation.birth_identity:
                unknown = True
            continue
        unknown = True
    return unknown


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
                target_count=len(supplied_identities),
            )
        terminal_snapshot = ProcessCleanupTerminalSnapshot(
            targets=targets,
            coverage_status=(
                "unknown"
                if any(target.termination_outcome == "unknown" for target in targets)
                else "complete"
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
            target_count=len(terminal_snapshot.targets),
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
    target_count: int,
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
        "target_count": target_count,
        "remaining_process_count": None,
        "remaining_process_group_count": None,
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
) -> None:
    valid = {
        "authenticated": receipt_sha256 is not None and unknown_reason is None,
        "no-target-observed": receipt_sha256 is None and unknown_reason is None,
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


def _snapshot_tree(
    root_pid: int | None,
    pgid: int | None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    pids: set[int] = set()
    pgids: set[int] = set()
    if root_pid is not None:
        pids.update(descendant_pids(root_pid))
        pids.add(root_pid)
    if pgid is not None:
        pgids.add(pgid)
        members = process_group_members(pgid)
        pids.update(members)
    table = {pid: group for pid, _ppid, group in _process_table()}
    for pid in list(pids):
        group = table.get(pid)
        if group is not None:
            pgids.add(group)
            pids.update(process_group_members(group))
    return tuple(sorted(pids)), tuple(sorted(pgids))


def _merge_snapshots(
    pids_a: tuple[int, ...],
    pgids_a: tuple[int, ...],
    pids_b: tuple[int, ...],
    pgids_b: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(sorted(set(pids_a) | set(pids_b))), tuple(sorted(set(pgids_a) | set(pgids_b)))


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
