# pyright: reportPrivateUsage=false
# ruff: noqa: PLR2004, SLF001

from __future__ import annotations

import asyncio
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import get_ident

import pytest
from pydantic import ValidationError

import saxo_bank_mcp.analytics_jobs as jobs_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_jobs import (
    AnalyticsJobManager,
    JobCapacityError,
    JobConclusion,
    JobExecutionContext,
    JobParameter,
    JobRequest,
    JobStateError,
)
from saxo_bank_mcp.analytics_models import (
    HandleKind,
    JobSummary,
    VisibilityMode,
    new_safe_handle,
)
from saxo_bank_mcp.analytics_storage_tools import preview_deletion
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    StorageDataType,
    StorageScope,
    StoreValidationError,
)

_NOW = datetime(2026, 8, 2, 10, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


def _request(
    ordinal: int = 1,
    *,
    restart_interrupted: bool = False,
    total_work_units: int = 4,
) -> JobRequest:
    return JobRequest(
        job_kind="monte_carlo",
        dataset_ids=(new_safe_handle(HandleKind.DATASET_ID),),
        instrument_handles=(new_safe_handle(HandleKind.INSTRUMENT_HANDLE),),
        parameters=(
            JobParameter(name="ordinal", value=ordinal),
            JobParameter(name="seed", value=700 + ordinal),
        ),
        total_work_units=total_work_units,
        restart_interrupted=restart_interrupted,
    )


class _ControlledHandler:
    def __init__(self, *, completed_units: int = 1) -> None:
        self.completed_units = completed_units
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.workspace: Path | None = None
        self.thread_id: int | None = None

    async def __call__(
        self,
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        if request.total_work_units != context.total_work_units:
            raise AssertionError("handler work units changed")
        self.calls += 1
        self.workspace = context.workspace
        self.thread_id = get_ident()
        (context.workspace / "scratch.bin").write_bytes(b"synthetic-temporary-state")
        await context.report_progress(self.completed_units)
        self.started.set()
        await self.release.wait()
        return JobConclusion(
            analysis_id=new_safe_handle(HandleKind.ANALYSIS_ID),
            artifact_ids=(),
        )


async def _wait_for_state(
    manager: AnalyticsJobManager,
    job_id: str,
    expected: str,
) -> jobs_module.JobStatus:
    for _ in range(200):
        status = await manager.get_job(job_id)
        if status.state == expected:
            return status
        await asyncio.sleep(0)
    raise AssertionError(f"job did not reach {expected}")


@pytest.mark.anyio
async def test_four_jobs_run_in_process_and_fifth_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handlers = [_ControlledHandler() for _ in range(4)]
    selected = iter(handlers)

    async def dispatch(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        return await next(selected)(request, context)

    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": dispatch},
    )
    try:
        statuses = [await manager.start_job(_request(index)) for index in range(4)]
        await asyncio.gather(*(handler.started.wait() for handler in handlers))

        with pytest.raises(JobCapacityError, match="four"):
            await manager.start_job(_request(5))

        assert len({status.job_id for status in statuses}) == 4
        assert all(handler.thread_id == get_ident() for handler in handlers)
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_active_jobs_cannot_be_deleted_or_free_false_capacity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handlers = [_ControlledHandler() for _ in range(5)]
    selected = iter(handlers)

    async def dispatch(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        return await next(selected)(request, context)

    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": dispatch},
    )
    try:
        await asyncio.gather(*(manager.start_job(_request(index)) for index in range(1, 5)))
        await asyncio.gather(*(handler.started.wait() for handler in handlers[:4]))

        with pytest.raises(StoreValidationError, match="active jobs"):
            preview_deletion(
                StorageScope(data_types=(StorageDataType.JOBS,)),
                store=store,
            )

        with pytest.raises(JobCapacityError, match="four"):
            await manager.start_job(_request(5))
        assert len(manager._tasks) == 4
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_progress_never_exposes_a_partial_conclusion(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _ControlledHandler(completed_units=2)
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()

        running = await manager.get_job(started.job_id)
        assert running.state == "running"
        assert running.progress.completed_units == 2
        assert running.progress.total_units == 4
        assert running.progress.remaining_units == 2
        assert running.analysis_id is None
        assert running.artifact_ids == ()
        assert running.conclusion_available is False
        summary = running.to_job_summary()
        assert isinstance(summary, JobSummary)
        assert summary.state == "running"
        assert summary.message == "job_running"
        assert summary.visibility is VisibilityMode.FINGERPRINT_ONLY

        handler.release.set()
        completed = await _wait_for_state(manager, started.job_id, "completed")
        assert completed.progress.completed_units == 4
        assert completed.analysis_id is not None
        assert completed.conclusion_available is True
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_duplicate_start_is_idempotent_by_normalized_request_fingerprint(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _ControlledHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    first_request = _request()
    equivalent = JobRequest(
        job_kind=first_request.job_kind,
        dataset_ids=tuple(reversed(first_request.dataset_ids)),
        instrument_handles=tuple(reversed(first_request.instrument_handles)),
        parameters=tuple(reversed(first_request.parameters)),
        total_work_units=first_request.total_work_units,
    )
    try:
        first = await manager.start_job(first_request)
        duplicate = await manager.start_job(equivalent)
        await handler.started.wait()

        assert duplicate.job_id == first.job_id
        assert duplicate.request_fingerprint == first.request_fingerprint
        assert handler.calls == 1
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_interrupted_job_requires_explicit_restart_and_reuses_persisted_input(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    first_handler = _ControlledHandler()
    first_manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": first_handler},
    )
    request = _request()
    started = await first_manager.start_job(request)
    await first_handler.started.wait()
    await first_manager.shutdown()

    second_handler = _ControlledHandler()
    second_manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": second_handler},
    )
    try:
        recovered = await second_manager.get_job(started.job_id)
        duplicate = await second_manager.start_job(request)
        assert recovered.state == "failed"
        assert recovered.status_code == "job_interrupted_restart_required"
        assert recovered.restart_allowed is True
        assert duplicate.state == "failed"
        assert second_handler.calls == 0

        restarted = await second_manager.start_job(
            request.model_copy(update={"restart_interrupted": True}),
        )
        await second_handler.started.wait()
        assert restarted.job_id == started.job_id
        assert restarted.request_fingerprint == started.request_fingerprint
        assert second_handler.calls == 1

        second_handler.release.set()
        completed = await _wait_for_state(second_manager, started.job_id, "completed")
        assert completed.conclusion_available is True
    finally:
        await second_manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_explicit_restart_cannot_exceed_four_active_jobs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    interrupted_handler = _ControlledHandler()
    first_manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": interrupted_handler},
    )
    interrupted_request = _request(1)
    await first_manager.start_job(interrupted_request)
    await interrupted_handler.started.wait()
    await first_manager.shutdown()

    active_handlers = [_ControlledHandler() for _ in range(4)]
    selected = iter(active_handlers)

    async def dispatch(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        return await next(selected)(request, context)

    second_manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": dispatch},
    )
    try:
        for ordinal in range(2, 6):
            await second_manager.start_job(_request(ordinal))
        await asyncio.gather(*(handler.started.wait() for handler in active_handlers))

        with pytest.raises(JobCapacityError, match="four"):
            await second_manager.start_job(
                interrupted_request.model_copy(update={"restart_interrupted": True}),
            )
    finally:
        await second_manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_start_rechecks_shutdown_state_inside_the_lifecycle_lock(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": _ControlledHandler()},
    )
    await manager._lock.acquire()
    start = asyncio.create_task(manager.start_job(_request()))
    await asyncio.sleep(0)
    manager._closed = True
    manager._lock.release()
    try:
        with pytest.raises(JobStateError, match="closed"):
            await start
        assert (
            store.list_storage(
                StorageScope(data_types=(StorageDataType.JOBS,)),
            )
            == ()
        )
        assert manager._tasks == {}
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_failed_workspace_launch_leaves_no_queued_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": _ControlledHandler()},
    )

    def fail_workspace(_job_id: str) -> Path:
        raise JobStateError("synthetic workspace refusal")

    monkeypatch.setattr(manager, "_prepare_workspace", fail_workspace)
    try:
        with pytest.raises(JobStateError, match="workspace refusal"):
            await manager.start_job(_request())
        assert (
            store.list_storage(
                StorageScope(data_types=(StorageDataType.JOBS,)),
            )
            == ()
        )
        assert manager._tasks == {}
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_workspace_cleanup_failure_cannot_retain_a_finished_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)

    async def complete(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        await context.report_progress(request.total_work_units)
        return JobConclusion(
            analysis_id=new_safe_handle(HandleKind.ANALYSIS_ID),
            artifact_ids=(),
        )

    cleanup_calls = 0
    original_cleanup = jobs_module._cleanup_workspace

    def fail_final_cleanup(root: Path, job_id: str) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls > 1:
            raise JobStateError("synthetic cleanup refusal")
        original_cleanup(root, job_id)

    monkeypatch.setattr(jobs_module, "_cleanup_workspace", fail_final_cleanup)
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": complete},
    )
    try:
        status = await manager.start_job(_request(total_work_units=1))
        task = manager._tasks.get(status.job_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert status.job_id not in manager._tasks
        assert (await manager.get_job(status.job_id)).state == "completed"
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_expired_job_is_cancelled_without_a_conclusion_and_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _NOW
    monkeypatch.setattr(jobs_module, "_utc_now", lambda: now)
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _ControlledHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
        job_ttl=timedelta(seconds=30),
    )
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()
        assert handler.workspace is not None
        assert handler.workspace.exists()
        now += timedelta(seconds=31)

        expired = await manager.get_job(started.job_id)

        assert expired.state == "cancelled"
        assert expired.status_code == "job_expired"
        assert expired.analysis_id is None
        assert expired.artifact_ids == ()
        assert expired.conclusion_available is False
        assert handler.workspace is not None
        assert not handler.workspace.exists()
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_explicit_cancellation_is_idempotent_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _ControlledHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()
        assert handler.workspace is not None
        assert handler.workspace.exists()

        cancelled = await manager.cancel_job(started.job_id)
        repeated = await manager.cancel_job(started.job_id)

        assert cancelled.state == repeated.state == "cancelled"
        assert cancelled.conclusion_available is False
        assert handler.workspace is not None
        assert not handler.workspace.exists()
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_restart_cleans_an_orphaned_terminal_job_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _ControlledHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    started = await manager.start_job(_request())
    await handler.started.wait()
    handler.release.set()
    completed = await _wait_for_state(manager, started.job_id, "completed")
    await manager.shutdown()

    orphan = config.paths.analytics_root / "job-workspaces" / completed.job_id
    orphan.mkdir(mode=0o700)
    (orphan / "stale.bin").write_bytes(b"synthetic-stale-temporary-state")
    recovered_manager = AnalyticsJobManager(store=store, config=config, handlers={})
    try:
        assert not orphan.exists()
        assert (await recovered_manager.get_job(completed.job_id)).state == "completed"
    finally:
        await recovered_manager.shutdown()
        store.close()


def test_job_request_rejects_paths_network_code_and_duplicate_parameters() -> None:
    unsafe_values = (
        "https://example.invalid/input",
        "/" + "tmp/private-input",
        "SELECT value FROM stored_rows",
        "lambda value: value",
    )
    for unsafe in unsafe_values:
        with pytest.raises(ValidationError):
            JobRequest(
                job_kind="optimization",
                parameters=(JobParameter(name="input", value=unsafe),),
                total_work_units=1,
            )

    with pytest.raises(ValidationError, match="unique"):
        JobRequest(
            job_kind="optimization",
            parameters=(
                JobParameter(name="seed", value=1),
                JobParameter(name="seed", value=2),
            ),
            total_work_units=1,
        )

    sensitive_name = "account" + "_key"
    with pytest.raises(ValidationError, match="name"):
        JobParameter(name=sensitive_name, value="synthetic-opaque-input")


def test_job_workspace_root_is_owner_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    manager = AnalyticsJobManager(store=store, config=config, handlers={})
    try:
        assert stat.S_IMODE(manager._workspace_root.stat().st_mode) == 0o700
        assert manager._workspace_root.is_relative_to(config.paths.analytics_root)
    finally:
        store.close()
