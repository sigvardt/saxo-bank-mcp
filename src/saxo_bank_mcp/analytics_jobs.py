# pyright: reportPrivateUsage=false
# ruff: noqa: SLF001

"""Bounded cooperative analytics jobs owned by one running MCP process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import RLock
from typing import Annotated, Final, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_models import (
    AnalysisId,
    AnalysisResult,
    ArtifactId,
    DatasetId,
    HandleKind,
    InstrumentHandle,
    JobId,
    JobSummary,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore, StoreError

type JobKind = Literal["monte_carlo", "optimization", "backtest", "report_generation"]
type JobState = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
]
type JobStatusCode = Literal[
    "job_queued",
    "job_running",
    "job_completed",
    "job_failed",
    "job_cancelled",
    "job_expired",
    "job_interrupted_restart_required",
]
type JobParameterValue = str | int | float | bool | None

_OWNER_DIRECTORY_MODE: Final = 0o700
_WORKSPACE_DIRECTORY: Final = "job-workspaces"
_DEFAULT_JOB_TTL: Final = timedelta(minutes=30)
_MAX_JOB_TTL: Final = timedelta(days=1)
_MAX_PARAMETERS: Final = 100
_MAX_PARAMETER_TEXT: Final = 256
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_SAFE_PARAMETER_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_PARAMETER_NAME: Final = re.compile(
    r"(?i)^(?:(?:account|client|order|position|instrument)_?(?:id|key|number)|"
    r"display_?name|.*(?:token|secret|password|path|url|sql|python|callback).*)$",
)
_UNSAFE_TEXT: Final = re.compile(
    r"(?i)(?:\b(?:https?|ftp|file|wss?)://|(?:^|\s)(?:/|~/|[a-z]:[\\/])|"
    r"\b(?:select|insert|update|delete|drop|attach|copy|pragma|python|lambda|eval|exec|"
    r"import|fetch|socket|curl)\b|\b(?:accountkey|clientkey|accountid|password|secret|"
    r"access_token|refresh_token)\b)",
)
_JOB_ID_ADAPTER: Final[TypeAdapter[JobId]] = TypeAdapter(JobId)
_ANALYSIS_ID_ADAPTER: Final[TypeAdapter[AnalysisId]] = TypeAdapter(AnalysisId)
_MANAGER_LEASE_LOCK: Final = RLock()
_MANAGER_LEASES: set[Path] = set()


class JobError(RuntimeError):
    """Base error for bounded local analytics job operations."""


class JobCapacityError(JobError):
    """Raised when four active jobs already occupy the owner session."""


class JobNotFoundError(JobError):
    """Raised when a safe job handle is not present in the owner store."""


class JobStateError(JobError):
    """Raised when persisted job state is invalid or cannot transition safely."""


class JobExpiredError(JobError):
    """Internal signal that a running job crossed its persisted expiry."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class JobParameter(_StrictModel):
    """One bounded deterministic scalar job parameter."""

    name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    value: JobParameterValue

    @model_validator(mode="after")
    def _validate_parameter(self) -> Self:
        if (
            _SAFE_PARAMETER_NAME.fullmatch(self.name) is None
            or _FORBIDDEN_PARAMETER_NAME.fullmatch(self.name) is not None
        ):
            raise ValueError("job parameter name is invalid")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("job parameter must be finite")
        if isinstance(self.value, str) and (
            not self.value
            or len(self.value) > _MAX_PARAMETER_TEXT
            or _UNSAFE_TEXT.search(self.value)
        ):
            raise ValueError("job parameter text is unsafe")
        return self


class JobRequest(_StrictModel):
    """Typed deterministic work description; it contains no executable callback or path."""

    job_kind: JobKind
    dataset_ids: tuple[DatasetId, ...] = Field(default=(), max_length=100)
    analysis_ids: tuple[AnalysisId, ...] = Field(default=(), max_length=100)
    instrument_handles: tuple[InstrumentHandle, ...] = Field(default=(), max_length=100)
    parameters: tuple[JobParameter, ...] = Field(default=(), max_length=_MAX_PARAMETERS)
    total_work_units: int = Field(ge=1, le=5_000_000)
    restart_interrupted: bool = False
    recipe_request_json: str | None = Field(default=None, min_length=2, max_length=100_000)

    @model_validator(mode="after")
    def _normalize_request(self) -> Self:
        names = tuple(parameter.name for parameter in self.parameters)
        if len(set(names)) != len(names):
            raise ValueError("job parameter names must be unique")
        object.__setattr__(self, "dataset_ids", tuple(sorted(set(self.dataset_ids))))
        object.__setattr__(self, "analysis_ids", tuple(sorted(set(self.analysis_ids))))
        object.__setattr__(
            self,
            "instrument_handles",
            tuple(sorted(set(self.instrument_handles))),
        )
        object.__setattr__(
            self,
            "parameters",
            tuple(sorted(self.parameters, key=lambda parameter: parameter.name)),
        )
        return self


class JobConclusion(_StrictModel):
    """Safe handles made visible only after a job completes."""

    analysis_id: AnalysisId | None
    artifact_ids: tuple[ArtifactId, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def _require_conclusion_handle(self) -> Self:
        artifact_ids = tuple(sorted(set(self.artifact_ids)))
        object.__setattr__(self, "artifact_ids", artifact_ids)
        if self.analysis_id is None and not artifact_ids:
            raise ValueError("a completed job requires a safe conclusion handle")
        return self


class JobProgress(_StrictModel):
    """Value-free work counts with no partial analytical conclusion."""

    completed_units: int = Field(ge=0)
    total_units: int = Field(ge=1, le=5_000_000)
    remaining_units: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if self.completed_units > self.total_units:
            raise ValueError("job progress exceeds total work")
        if self.remaining_units != self.total_units - self.completed_units:
            raise ValueError("job remaining work is inconsistent")
        return self


class JobStatus(_StrictModel):
    """Owner-safe job state; partial work never carries conclusion handles."""

    job_id: JobId
    state: JobState
    status_code: JobStatusCode
    request_fingerprint: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
    ]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    progress: JobProgress
    analysis_id: AnalysisId | None
    artifact_ids: tuple[ArtifactId, ...]
    conclusion_available: bool
    restart_allowed: bool

    def to_job_summary(self) -> JobSummary:
        """Project safe state through the existing frozen owner-facing job contract."""
        return JobSummary(
            schema_version="1",
            visibility=VisibilityMode.FINGERPRINT_ONLY,
            job_id=self.job_id,
            state=self.state,
            request_fingerprint=self.request_fingerprint,
            created_at=self.created_at,
            updated_at=self.updated_at,
            analysis_id=self.analysis_id,
            artifact_ids=self.artifact_ids,
            message=self.status_code,
        )

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        for value in (self.created_at, self.updated_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("job timestamps must use UTC")
        if self.updated_at < self.created_at or self.expires_at < self.created_at:
            raise ValueError("job timestamps are inconsistent")
        completed = self.state == "completed"
        has_conclusion = self.analysis_id is not None or bool(self.artifact_ids)
        if completed != has_conclusion or self.conclusion_available != completed:
            raise ValueError("job conclusion availability does not match state")
        if not completed and (self.analysis_id is not None or self.artifact_ids):
            raise ValueError("partial job state cannot expose a conclusion")
        if completed and self.progress.completed_units != self.progress.total_units:
            raise ValueError("completed job progress is incomplete")
        allowed_codes: dict[JobState, frozenset[JobStatusCode]] = {
            "queued": frozenset({"job_queued"}),
            "running": frozenset({"job_running"}),
            "completed": frozenset({"job_completed"}),
            "failed": frozenset({"job_failed", "job_interrupted_restart_required"}),
            "cancelled": frozenset({"job_cancelled", "job_expired"}),
        }
        if self.status_code not in allowed_codes[self.state]:
            raise ValueError("job status code does not match state")
        restart_allowed = self.status_code == "job_interrupted_restart_required"
        if self.restart_allowed != restart_allowed:
            raise ValueError("job restart availability does not match state")
        return self


class _PersistedJobState(_StrictModel):
    completed_units: int = Field(ge=0)
    total_units: int = Field(ge=1, le=5_000_000)
    expires_at: datetime
    status_code: JobStatusCode
    artifact_ids: tuple[ArtifactId, ...]

    @model_validator(mode="after")
    def _validate_persisted_state(self) -> Self:
        if self.completed_units > self.total_units:
            raise ValueError("persisted job progress exceeds total work")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("persisted job expiry must use UTC")
        return self


class _JobRow(_StrictModel):
    job_id: JobId
    state: JobState
    request_fingerprint: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$"),
    ]
    created_at: datetime
    updated_at: datetime
    analysis_id: AnalysisId | None
    request_json: str
    persisted: _PersistedJobState


type ProgressReporter = Callable[[int], Awaitable[None]]


class JobExecutionContext:
    """Trusted in-process execution context; paths never enter a public result."""

    __slots__ = ("_committer", "_reporter", "_workspace", "total_work_units")

    def __init__(
        self,
        *,
        workspace: Path,
        total_work_units: int,
        reporter: ProgressReporter,
        committer: Callable[[AnalysisResult], Awaitable[JobConclusion]] | None = None,
    ) -> None:
        """Bind one private workspace and its manager-owned progress reporter."""
        self._workspace = workspace
        self.total_work_units = total_work_units
        self._reporter = reporter
        self._committer = committer

    @property
    def workspace(self) -> Path:
        """Return the private workspace only to the registered in-process handler."""
        return self._workspace

    async def report_progress(self, completed_units: int) -> None:
        """Persist safe work counts without accepting a partial conclusion."""
        await self._reporter(completed_units)

    async def commit_analysis(self, result: AnalysisResult) -> JobConclusion:
        """Publish a complete result and its job receipt at one manager-owned checkpoint."""
        if self._committer is None:
            raise JobStateError("analytics job has no result commit checkpoint")
        return await self._committer(result)


class JobHandler(Protocol):
    """Registered in-process handler contract; handlers are never accepted in a job request."""

    async def __call__(
        self,
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        """Execute one deterministic request in the current event loop."""
        ...


type HandlerMap = Mapping[JobKind, JobHandler]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AnalyticsJobManager:
    """At-most-four cooperative tasks tied to the lifetime of one MCP process."""

    def __init__(
        self,
        *,
        store: AnalyticsStore,
        config: AnalyticsConfig,
        handlers: HandlerMap,
        job_ttl: timedelta = _DEFAULT_JOB_TTL,
    ) -> None:
        """Bind one store, fixed limits, and an allowlist of in-process handlers."""
        validated = AnalyticsConfig.model_validate(config)
        if job_ttl <= timedelta(0) or job_ttl > _MAX_JOB_TTL:
            raise ValueError("analytics job TTL is invalid")
        store._require_open()
        if validated != store._config:
            raise JobStateError("analytics job config does not match its store")
        validated_handlers = dict(handlers)
        lease_key = _acquire_manager_lease(store)
        self._lease_key = lease_key
        self._lease_held = True
        self._store = store
        self._config = store._config
        self._handlers = validated_handlers
        self._job_ttl = job_ttl
        try:
            self._workspace_root = _prepare_workspace_root(validated)
            self._tasks: dict[str, asyncio.Task[None]] = {}
            self._finalizers: dict[str, asyncio.Task[None]] = {}
            self._stop_intents: dict[str, JobStatusCode] = {}
            self._cleanup_failures: set[str] = set()
            self._lock = asyncio.Lock()
            self._closed = False
            self._shutdown_task: asyncio.Task[None] | None = None
            self._recover_interrupted_rows()
        except BaseException:
            self._release_lease()
            raise

    async def start_job(self, request: JobRequest) -> JobStatus:
        """Start, deduplicate, or explicitly restart one deterministic local job."""
        self._require_open()
        validated = JobRequest.model_validate(request)
        handler = self._handlers.get(validated.job_kind)
        if handler is None:
            raise JobStateError("analytics job kind has no registered in-process handler")
        fingerprint, request_json = _request_identity(validated)
        persisted_request = JobRequest.model_validate_json(request_json)
        launch = False
        async with self._lock:
            self._require_open()
            row = self._find_by_fingerprint(fingerprint)
            previous_row = row
            workspace: Path | None = None
            if row is not None:
                if row.request_json != request_json:
                    raise JobStateError("analytics job fingerprint binding is invalid")
                if (
                    validated.restart_interrupted
                    and row.persisted.status_code == "job_interrupted_restart_required"
                ):
                    workspace = self._prepare_workspace(row.job_id)
                    try:
                        row = self._restart_row(row)
                    except BaseException:
                        _cleanup_workspace(self._workspace_root, row.job_id)
                        raise
                    launch = True
            else:
                job_id = new_safe_handle(HandleKind.JOB_ID)
                workspace = self._prepare_workspace(job_id)
                try:
                    row = self._insert_row(
                        job_id,
                        fingerprint,
                        request_json,
                        validated.total_work_units,
                    )
                except BaseException:
                    _cleanup_workspace(self._workspace_root, job_id)
                    raise
                launch = True

            if launch:
                if workspace is None:
                    raise JobStateError("analytics job workspace was not prepared")
                try:
                    task = asyncio.create_task(
                        self._run_job(row.job_id, persisted_request, workspace, handler),
                        name=f"analytics-{row.job_id}",
                    )
                except BaseException:
                    self._rollback_failed_launch(row, previous_row=previous_row)
                    _cleanup_workspace(self._workspace_root, row.job_id)
                    raise
                self._tasks[row.job_id] = task
                task.add_done_callback(
                    partial(self._schedule_task_finalizer, row.job_id),
                )
            return _status_from_row(row)

    async def get_job(self, job_id: str) -> JobStatus:
        """Return safe state and expire active work without exposing partial conclusions."""
        self._require_open()
        validated_job_id = _JOB_ID_ADAPTER.validate_python(job_id)
        task: asyncio.Task[None] | None = None
        async with self._lock:
            row = self._require_row(validated_job_id)
            if row.state in {"queued", "running"} and _utc_now() >= row.persisted.expires_at:
                task = self._tasks.get(validated_job_id)
                if task is not None:
                    self._stop_intents.setdefault(validated_job_id, "job_expired")
                    task.cancel()
                else:
                    row = self._set_terminal(
                        row,
                        state="cancelled",
                        status_code="job_expired",
                    )
        if task is not None:
            waiter = asyncio.gather(task, return_exceptions=True)
            await asyncio.shield(waiter)
            await asyncio.shield(self._ensure_task_finalizer(validated_job_id, task))
            row = self._require_row(validated_job_id)
        return _status_from_row(row)

    async def cancel_job(self, job_id: str) -> JobStatus:
        """Cancel one active local job; repeated cancellation is idempotent."""
        self._require_open()
        validated_job_id = _JOB_ID_ADAPTER.validate_python(job_id)
        task: asyncio.Task[None] | None = None
        async with self._lock:
            row = self._require_row(validated_job_id)
            if row.state in {"queued", "running"}:
                task = self._tasks.get(validated_job_id)
                if task is not None:
                    self._stop_intents.setdefault(validated_job_id, "job_cancelled")
                    task.cancel()
                else:
                    row = self._set_terminal(
                        row,
                        state="cancelled",
                        status_code="job_cancelled",
                    )
        if task is not None:
            waiter = asyncio.gather(task, return_exceptions=True)
            await asyncio.shield(waiter)
            await asyncio.shield(self._ensure_task_finalizer(validated_job_id, task))
            row = self._require_row(validated_job_id)
        return _status_from_row(row)

    async def shutdown(self) -> None:
        """Stop process-owned work as interrupted; never auto-resume it later."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_impl(),
                name="analytics-job-manager-shutdown",
            )
        await asyncio.shield(self._shutdown_task)

    async def _shutdown_impl(self) -> None:
        """Finish shutdown independently of cancellation of its transport caller."""
        tasks: tuple[asyncio.Task[None], ...] = ()
        job_ids: tuple[str, ...] = ()
        try:
            async with self._lock:
                self._closed = True
                tasks = tuple(self._tasks.values())
                job_ids = tuple(self._tasks)
                for job_id, task in tuple(self._tasks.items()):
                    self._stop_intents.setdefault(
                        job_id,
                        "job_interrupted_restart_required",
                    )
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                finalizers = tuple(
                    self._ensure_task_finalizer(job_id, task)
                    for job_id, task in zip(job_ids, tasks, strict=True)
                )
                await asyncio.gather(*finalizers)
        finally:
            if all(task.done() for task in tasks):
                self._release_lease()

    async def _run_job(
        self,
        job_id: str,
        request: JobRequest,
        workspace: Path,
        handler: JobHandler,
    ) -> None:
        try:
            async with self._lock:
                row = self._require_row(job_id)
                if row.state != "queued":
                    return
                self._write_row(
                    row,
                    state="running",
                    persisted=row.persisted.model_copy(update={"status_code": "job_running"}),
                )
            context = JobExecutionContext(
                workspace=workspace,
                total_work_units=request.total_work_units,
                reporter=lambda completed: self._record_progress(job_id, completed),
                committer=lambda result: self._commit_analysis(job_id, result),
            )
            conclusion = await handler(request, context)
            validated_conclusion = JobConclusion.model_validate(conclusion)
            _cleanup_workspace(self._workspace_root, job_id)
            async with self._lock:
                row = self._require_row(job_id)
                if row.state not in {"queued", "running"}:
                    return
                if job_id in self._stop_intents:
                    return
                if _utc_now() >= row.persisted.expires_at:
                    self._set_terminal(row, state="cancelled", status_code="job_expired")
                    return
                persisted = row.persisted.model_copy(
                    update={
                        "completed_units": row.persisted.total_units,
                        "status_code": "job_completed",
                        "artifact_ids": validated_conclusion.artifact_ids,
                    },
                )
                self._write_row(
                    row,
                    state="completed",
                    persisted=persisted,
                    analysis_id=validated_conclusion.analysis_id,
                )
        except asyncio.CancelledError:
            raise
        except JobExpiredError:
            async with self._lock:
                row = self._row(job_id)
                if (
                    job_id not in self._stop_intents
                    and row is not None
                    and row.state in {"queued", "running"}
                ):
                    self._set_terminal(row, state="cancelled", status_code="job_expired")
        except Exception:  # noqa: BLE001
            async with self._lock:
                row = self._row(job_id)
                if (
                    job_id not in self._stop_intents
                    and row is not None
                    and row.state in {"queued", "running"}
                ):
                    self._set_terminal(row, state="failed", status_code="job_failed")

    async def _record_progress(self, job_id: str, completed_units: int) -> None:
        if type(completed_units) is not int:
            raise JobStateError("job progress must use integer work units")
        async with self._lock:
            row = self._require_row(job_id)
            if row.state != "running":
                raise JobStateError("job progress requires a running job")
            if job_id in self._stop_intents:
                raise JobStateError("job progress is unavailable after stop intent")
            if _utc_now() >= row.persisted.expires_at:
                raise JobExpiredError("analytics job expired")
            if not row.persisted.completed_units <= completed_units <= row.persisted.total_units:
                raise JobStateError("job progress must be monotone and bounded")
            persisted = row.persisted.model_copy(update={"completed_units": completed_units})
            self._write_row(row, state="running", persisted=persisted)

    async def _commit_analysis(self, job_id: str, result: AnalysisResult) -> JobConclusion:
        """Serialize cancellation against one transaction containing the result and receipt."""
        async with self._lock:
            self._require_open()
            row = self._require_row(job_id)
            if row.state != "running" or job_id in self._stop_intents:
                raise JobStateError("analytics job result commit was stopped")
            if _utc_now() >= row.persisted.expires_at:
                raise JobExpiredError("analytics job expired before result commit")
            validated = AnalysisResult.model_validate(result)
            conclusion = JobConclusion(analysis_id=validated.analysis_id, artifact_ids=())
            persisted = row.persisted.model_copy(
                update={
                    "completed_units": row.persisted.total_units,
                    "status_code": "job_completed",
                    "artifact_ids": (),
                }
            )
            _cleanup_workspace(self._workspace_root, job_id)
            # There is no await between the stop-intent check and transaction completion.
            # Both rows become visible together; a late cancel reads the completed receipt.
            with self._store.transaction():
                self._store.put_analysis(validated)
                self._write_row(
                    row,
                    state="completed",
                    persisted=persisted,
                    analysis_id=validated.analysis_id,
                )
            return conclusion

    def _insert_row(
        self,
        job_id: str,
        fingerprint: str,
        request_json: str,
        total_units: int,
    ) -> _JobRow:
        now = _utc_now()
        persisted = _PersistedJobState(
            completed_units=0,
            total_units=total_units,
            expires_at=now + self._job_ttl,
            status_code="job_queued",
            artifact_ids=(),
        )
        with self._store._write_connection() as connection:
            active_row = connection.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')",
            ).fetchone()
            if active_row is None or type(active_row[0]) is not int:
                raise StoreError("analytics job capacity state is invalid")
            if active_row[0] >= self._config.limits.concurrent_jobs:
                raise JobCapacityError("at most four analytics jobs may run concurrently")
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, state, request_fingerprint, created_at, updated_at,
                    analysis_id, message, request_json
                ) VALUES (?, 'queued', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    fingerprint,
                    now,
                    now,
                    persisted.model_dump_json(),
                    request_json,
                ),
            )
            self._store._bump_revision(connection)
        return self._require_row(job_id)

    def _rollback_failed_launch(
        self,
        row: _JobRow,
        *,
        previous_row: _JobRow | None,
    ) -> None:
        with self._store._write_connection() as connection:
            if previous_row is None:
                connection.execute(
                    "DELETE FROM jobs WHERE job_id = ? AND state = 'queued'",
                    (row.job_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'failed', updated_at = ?, analysis_id = NULL, message = ?
                    WHERE job_id = ? AND state = 'queued'
                    """,
                    (
                        _utc_now(),
                        previous_row.persisted.model_dump_json(),
                        row.job_id,
                    ),
                )
            self._store._bump_revision(connection)

    def _restart_row(self, row: _JobRow) -> _JobRow:
        now = _utc_now()
        persisted = _PersistedJobState(
            completed_units=0,
            total_units=row.persisted.total_units,
            expires_at=now + self._job_ttl,
            status_code="job_queued",
            artifact_ids=(),
        )
        with self._store._write_connection() as connection:
            active_row = connection.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')",
            ).fetchone()
            if active_row is None or type(active_row[0]) is not int:
                raise StoreError("analytics job capacity state is invalid")
            if active_row[0] >= self._config.limits.concurrent_jobs:
                raise JobCapacityError("at most four analytics jobs may run concurrently")
            connection.execute(
                """
                UPDATE jobs
                SET state = 'queued', updated_at = ?, analysis_id = NULL, message = ?
                WHERE job_id = ? AND state = 'failed'
                """,
                (now, persisted.model_dump_json(), row.job_id),
            )
            self._store._bump_revision(connection)
        return self._require_row(row.job_id)

    def _set_terminal(
        self,
        row: _JobRow,
        *,
        state: Literal["failed", "cancelled"],
        status_code: JobStatusCode,
    ) -> _JobRow:
        persisted = row.persisted.model_copy(
            update={"status_code": status_code, "artifact_ids": ()},
        )
        return self._write_row(
            row,
            state=state,
            persisted=persisted,
            analysis_id=None,
        )

    def _write_row(
        self,
        row: _JobRow,
        *,
        state: JobState,
        persisted: _PersistedJobState,
        analysis_id: str | None | object = ...,
    ) -> _JobRow:
        updated_at = _utc_now()
        next_analysis_id = row.analysis_id if analysis_id is ... else analysis_id
        with self._store._write_connection() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, updated_at = ?, analysis_id = ?, message = ?
                WHERE job_id = ?
                """,
                (
                    state,
                    updated_at,
                    next_analysis_id,
                    persisted.model_dump_json(),
                    row.job_id,
                ),
            )
            self._store._bump_revision(connection)
        return self._require_row(row.job_id)

    def _find_by_fingerprint(self, fingerprint: str) -> _JobRow | None:
        if _SHA256_PATTERN.fullmatch(fingerprint) is None:
            raise JobStateError("analytics job fingerprint is invalid")
        with self._store._read_connection() as connection:
            raw = connection.execute(
                """
                SELECT job_id, state, request_fingerprint, epoch_us(created_at),
                    epoch_us(updated_at), analysis_id, message, request_json
                FROM jobs
                WHERE request_fingerprint = ?
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return None if raw is None else _parse_row(raw)

    def _row(self, job_id: str) -> _JobRow | None:
        with self._store._read_connection() as connection:
            raw = connection.execute(
                """
                SELECT job_id, state, request_fingerprint, epoch_us(created_at),
                    epoch_us(updated_at), analysis_id, message, request_json
                FROM jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return None if raw is None else _parse_row(raw)

    def _require_row(self, job_id: str) -> _JobRow:
        row = self._row(job_id)
        if row is None:
            raise JobNotFoundError("analytics job handle was not found")
        return row

    def _recover_interrupted_rows(self) -> None:
        now = _utc_now()
        recovered: list[str] = []
        with self._store._write_connection() as connection:
            rows = cast(
                "list[tuple[object, ...]]",
                connection.execute(
                    """
                    SELECT job_id, state, message FROM jobs
                    ORDER BY job_id
                    """,
                ).fetchall(),
            )
            for raw_job_id, raw_state, raw_message in rows:
                job_id = _JOB_ID_ADAPTER.validate_python(raw_job_id)
                if raw_state not in {"queued", "running"}:
                    continue
                persisted = _PersistedJobState.model_validate_json(str(raw_message))
                interrupted = persisted.model_copy(
                    update={
                        "status_code": "job_interrupted_restart_required",
                        "artifact_ids": (),
                    },
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'failed', updated_at = ?, analysis_id = NULL, message = ?
                    WHERE job_id = ?
                    """,
                    (now, interrupted.model_dump_json(), job_id),
                )
                recovered.append(job_id)
            if recovered:
                self._store._bump_revision(connection)
        for workspace in tuple(self._workspace_root.iterdir()):
            try:
                job_id = _JOB_ID_ADAPTER.validate_python(workspace.name)
            except ValueError as error:
                raise JobStateError("analytics workspace root contains an unknown entry") from error
            _cleanup_workspace(self._workspace_root, job_id)

    def _prepare_workspace(self, job_id: str) -> Path:
        _cleanup_workspace(self._workspace_root, job_id)
        workspace = self._workspace_root / job_id
        workspace.mkdir(mode=_OWNER_DIRECTORY_MODE)
        workspace.chmod(_OWNER_DIRECTORY_MODE)
        if stat.S_IMODE(workspace.stat().st_mode) != _OWNER_DIRECTORY_MODE:
            raise JobStateError("analytics job workspace is not owner-only")
        return workspace

    def _schedule_task_finalizer(
        self,
        job_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._ensure_task_finalizer(job_id, task)

    def _ensure_task_finalizer(
        self,
        job_id: str,
        task: asyncio.Task[None],
    ) -> asyncio.Task[None]:
        existing = self._finalizers.get(job_id)
        if existing is not None:
            return existing
        finalizer = asyncio.create_task(
            self._finalize_job_task(job_id, task),
            name=f"analytics-finalize-{job_id}",
        )
        self._finalizers[job_id] = finalizer
        finalizer.add_done_callback(partial(self._drop_task_finalizer, job_id))
        return finalizer

    async def _finalize_job_task(
        self,
        job_id: str,
        task: asyncio.Task[None],
    ) -> None:
        cleanup_failed = False
        try:
            _cleanup_workspace(self._workspace_root, job_id)
        except (JobError, OSError):
            self._cleanup_failures.add(job_id)
            cleanup_failed = True
        try:
            async with self._lock:
                row = self._row(job_id)
                intent = self._stop_intents.get(job_id)
                if (
                    row is not None
                    and cleanup_failed
                    and row.state
                    in {
                        "queued",
                        "running",
                        "completed",
                    }
                ):
                    self._set_terminal(
                        row,
                        state="failed",
                        status_code="job_failed",
                    )
                elif row is not None and row.state in {"queued", "running"}:
                    if intent == "job_cancelled":
                        self._set_terminal(
                            row,
                            state="cancelled",
                            status_code="job_cancelled",
                        )
                    elif intent == "job_expired":
                        self._set_terminal(
                            row,
                            state="cancelled",
                            status_code="job_expired",
                        )
                    elif intent == "job_interrupted_restart_required" or task.cancelled():
                        self._set_terminal(
                            row,
                            state="failed",
                            status_code="job_interrupted_restart_required",
                        )
                    else:
                        self._set_terminal(
                            row,
                            state="failed",
                            status_code="job_failed",
                        )
                self._stop_intents.pop(job_id, None)
        finally:
            if self._tasks.get(job_id) is task:
                self._tasks.pop(job_id, None)

    def _drop_task_finalizer(
        self,
        job_id: str,
        finalizer: asyncio.Task[None],
    ) -> None:
        if self._finalizers.get(job_id) is finalizer:
            self._finalizers.pop(job_id, None)
        if not finalizer.cancelled():
            finalizer.exception()

    def _release_lease(self) -> None:
        if not self._lease_held:
            return
        _release_manager_lease(self._lease_key)
        self._lease_held = False

    def _require_open(self) -> None:
        if self._closed:
            raise JobStateError("analytics job manager is closed")


def _request_identity(request: JobRequest) -> tuple[str, str]:
    payload = request.model_dump(mode="json", exclude={"restart_interrupted"})
    payload["restart_interrupted"] = False
    request_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(request_json.encode()).hexdigest(), request_json


def _parse_row(raw: tuple[object, ...]) -> _JobRow:
    try:
        message = _required_string(raw[6])
        analysis_id = None if raw[5] is None else _ANALYSIS_ID_ADAPTER.validate_python(raw[5])
        return _JobRow(
            job_id=_JOB_ID_ADAPTER.validate_python(raw[0]),
            state=cast("JobState", raw[1]),
            request_fingerprint=str(raw[2]),
            created_at=_database_datetime(raw[3]),
            updated_at=_database_datetime(raw[4]),
            analysis_id=analysis_id,
            request_json=str(raw[7]),
            persisted=_PersistedJobState.model_validate_json(message),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise JobStateError("persisted analytics job state is invalid") from error


def _required_string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("persisted job field must be text")
    return value


def _database_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif type(value) is int:
        result = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    else:
        raise JobStateError("persisted analytics job timestamp is invalid")
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _status_from_row(row: _JobRow) -> JobStatus:
    completed = row.state == "completed"
    return JobStatus(
        job_id=row.job_id,
        state=row.state,
        status_code=row.persisted.status_code,
        request_fingerprint=row.request_fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.persisted.expires_at,
        progress=JobProgress(
            completed_units=row.persisted.completed_units,
            total_units=row.persisted.total_units,
            remaining_units=row.persisted.total_units - row.persisted.completed_units,
        ),
        analysis_id=row.analysis_id if completed else None,
        artifact_ids=row.persisted.artifact_ids if completed else (),
        conclusion_available=completed,
        restart_allowed=row.persisted.status_code == "job_interrupted_restart_required",
    )


def _prepare_workspace_root(config: AnalyticsConfig) -> Path:
    root = config.paths.analytics_root / _WORKSPACE_DIRECTORY
    if root.is_symlink():
        raise JobStateError("analytics job workspace root cannot be a symlink")
    root.mkdir(mode=_OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    root.chmod(_OWNER_DIRECTORY_MODE)
    resolved = root.resolve(strict=True)
    if not resolved.is_relative_to(config.paths.analytics_root.resolve(strict=True)):
        raise JobStateError("analytics job workspace escapes the owner store")
    if stat.S_IMODE(resolved.stat().st_mode) != _OWNER_DIRECTORY_MODE:
        raise JobStateError("analytics job workspace root is not owner-only")
    return resolved


def _cleanup_workspace(root: Path, job_id: str) -> None:
    validated_job_id = _JOB_ID_ADAPTER.validate_python(job_id)
    workspace = root / validated_job_id
    if workspace.is_symlink():
        workspace.unlink()
        return
    if workspace.exists():
        resolved = workspace.resolve(strict=True)
        if resolved.parent != root.resolve(strict=True):
            raise JobStateError("analytics job cleanup escaped its owner-only root")
        _recover_workspace_permissions(resolved)
        shutil.rmtree(resolved)


def _recover_workspace_permissions(workspace: Path) -> None:
    _require_owner_workspace_node(workspace, directory=True)
    workspace.chmod(_OWNER_DIRECTORY_MODE)
    for current, directory_names, file_names in os.walk(
        workspace,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        _require_contained_workspace_path(workspace, current_path)
        _require_owner_workspace_node(current_path, directory=True)
        current_path.chmod(_OWNER_DIRECTORY_MODE)
        for name in tuple(directory_names):
            child = current_path / name
            _require_contained_workspace_path(workspace, child)
            if child.is_symlink():
                child.unlink()
                directory_names.remove(name)
                continue
            _require_owner_workspace_node(child, directory=True)
            child.chmod(_OWNER_DIRECTORY_MODE)
        for name in file_names:
            child = current_path / name
            _require_contained_workspace_path(workspace, child)
            if child.is_symlink():
                child.unlink()
                continue
            _require_owner_workspace_node(child, directory=False)


def _require_contained_workspace_path(workspace: Path, candidate: Path) -> None:
    if candidate != workspace and not candidate.is_relative_to(workspace):
        raise JobStateError("analytics job cleanup escaped its workspace")


def _require_owner_workspace_node(path: Path, *, directory: bool) -> None:
    try:
        node = path.lstat()
    except OSError as error:
        raise JobStateError("analytics job workspace node is unavailable") from error
    expected_kind = stat.S_ISDIR(node.st_mode) if directory else stat.S_ISREG(node.st_mode)
    if not expected_kind or node.st_uid != os.getuid() or (not directory and node.st_nlink != 1):
        raise JobStateError("analytics job workspace node is not owner-contained")


def _acquire_manager_lease(store: AnalyticsStore) -> Path:
    key = store._config.paths.store_path.resolve(strict=True)
    with _MANAGER_LEASE_LOCK:
        if key in _MANAGER_LEASES:
            raise JobStateError("analytics job manager lease is already held")
        _MANAGER_LEASES.add(key)
    return key


def _release_manager_lease(key: Path) -> None:
    with _MANAGER_LEASE_LOCK:
        _MANAGER_LEASES.discard(key)


async def start_job(
    request: JobRequest,
    *,
    manager: AnalyticsJobManager,
) -> JobStatus:
    """Start a job through the configured MCP-owned manager."""
    return await manager.start_job(request)


async def get_job(job_id: str, *, manager: AnalyticsJobManager) -> JobStatus:
    """Read one safe job status through the configured manager."""
    return await manager.get_job(job_id)


async def cancel_job(job_id: str, *, manager: AnalyticsJobManager) -> JobStatus:
    """Cancel one safe job handle through the configured manager."""
    return await manager.cancel_job(job_id)


__all__ = [
    "AnalyticsJobManager",
    "JobCapacityError",
    "JobConclusion",
    "JobError",
    "JobExecutionContext",
    "JobExpiredError",
    "JobHandler",
    "JobNotFoundError",
    "JobParameter",
    "JobRequest",
    "JobStateError",
    "JobStatus",
    "cancel_job",
    "get_job",
    "start_job",
]
