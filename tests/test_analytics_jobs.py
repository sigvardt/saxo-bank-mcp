# pyright: reportPrivateUsage=false
# ruff: noqa: PLR2004, SLF001

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import get_ident
from typing import cast

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


class _SlowCancellingHandler(_ControlledHandler):
    def __init__(self) -> None:
        super().__init__()
        self.cancelling = asyncio.Event()
        self.allow_stop = asyncio.Event()

    async def __call__(
        self,
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        try:
            return await super().__call__(request, context)
        except asyncio.CancelledError:
            self.cancelling.set()
            await self.allow_stop.wait()
            raise


class _CancellationSuppressingHandler(_ControlledHandler):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = asyncio.Event()

    async def __call__(
        self,
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        try:
            return await super().__call__(request, context)
        except asyncio.CancelledError:
            self.cancelled.set()
            return JobConclusion(
                analysis_id=new_safe_handle(HandleKind.ANALYSIS_ID),
                artifact_ids=(),
            )


class _RepeatedCancellationSuppressingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellation_count = 0

    async def __call__(
        self,
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        del request
        await context.report_progress(1)
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellation_count += 1
                self.cancelled.set()
        return JobConclusion(
            analysis_id=new_safe_handle(HandleKind.ANALYSIS_ID),
            artifact_ids=(),
        )


class _ExplodingHandlerMapping(Mapping[object, object]):
    def __getitem__(self, key: object) -> object:
        raise KeyError(key)

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("synthetic handler mapping conversion failure")

    def __len__(self) -> int:
        return 1


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


async def _wait_for_persisted_state(
    manager: AnalyticsJobManager,
    job_id: str,
    expected: str,
) -> jobs_module._JobRow:
    for _ in range(200):
        row = manager._require_row(job_id)
        if row.state == expected:
            return row
        await asyncio.sleep(0)
    raise AssertionError(f"job row did not reach {expected}")


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
async def test_cancelling_job_remains_deletion_protected_until_task_stops(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _SlowCancellingHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    cancellation: asyncio.Task[jobs_module.JobStatus] | None = None
    try:
        await manager.start_job(_request())
        await handler.started.wait()
        cancellation = asyncio.create_task(manager.cancel_job(next(iter(manager._tasks))))
        await handler.cancelling.wait()

        with pytest.raises(StoreValidationError, match="active jobs"):
            preview_deletion(
                StorageScope(data_types=(StorageDataType.JOBS,)),
                store=store,
            )

        handler.allow_stop.set()
        cancelled = await cancellation
        assert cancelled.state == "cancelled"
    finally:
        handler.allow_stop.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_cancelling_task_remains_counted_until_it_stops(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    cancelling_handler = _SlowCancellingHandler()
    active_handlers = [_ControlledHandler() for _ in range(3)]
    selected = iter((cancelling_handler, *active_handlers))

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
    cancellation: asyncio.Task[jobs_module.JobStatus] | None = None
    try:
        first = await manager.start_job(_request(1))
        await cancelling_handler.started.wait()
        cancellation = asyncio.create_task(manager.cancel_job(first.job_id))
        await cancelling_handler.cancelling.wait()

        await asyncio.gather(*(manager.start_job(_request(index)) for index in range(2, 5)))
        with pytest.raises(JobCapacityError, match="four"):
            await manager.start_job(_request(5))

        cancelling_handler.allow_stop.set()
        await cancellation
    finally:
        cancelling_handler.allow_stop.set()
        for handler in active_handlers:
            handler.release.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
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
async def test_workspace_cleanup_refusal_fails_without_publishing_a_conclusion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    external = config.paths.analytics_root / "synthetic-external-completion-file"
    content = b"synthetic external completion content"
    external.write_bytes(content)
    external.chmod(0o400)
    original_mode = stat.S_IMODE(external.stat().st_mode)
    workspace: Path | None = None

    async def complete(
        request: JobRequest,
        context: JobExecutionContext,
    ) -> JobConclusion:
        nonlocal workspace
        workspace = context.workspace
        os.link(external, context.workspace / "linked.bin")
        await context.report_progress(request.total_work_units)
        return JobConclusion(
            analysis_id=new_safe_handle(HandleKind.ANALYSIS_ID),
            artifact_ids=(),
        )

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
        failed = await _wait_for_persisted_state(manager, status.job_id, "failed")

        assert failed.persisted.status_code == "job_failed"
        assert failed.analysis_id is None
        assert workspace is not None
        assert workspace.exists()  # noqa: ASYNC240
        assert external.read_bytes() == content
        assert stat.S_IMODE(external.stat().st_mode) == original_mode
        assert external.stat().st_nlink == 2
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
async def test_second_manager_cannot_interrupt_live_rows_and_releases_lease_on_shutdown(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _ControlledHandler()
    first = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    second: AnalyticsJobManager | None = None
    replacement: AnalyticsJobManager | None = None
    try:
        started = await first.start_job(_request())
        await handler.started.wait()

        with pytest.raises(JobStateError, match=r"manager.*lease"):
            second = AnalyticsJobManager(store=store, config=config, handlers={})
        assert (await first.get_job(started.job_id)).state == "running"

        await first.shutdown()
        replacement = AnalyticsJobManager(store=store, config=config, handlers={})
        recovered = await replacement.get_job(started.job_id)
        assert recovered.state == "failed"
        assert recovered.status_code == "job_interrupted_restart_required"
    finally:
        handler.release.set()
        if second is not None:
            await second.shutdown()
        await first.shutdown()
        if replacement is not None:
            await replacement.shutdown()
        store.close()


@pytest.mark.anyio
async def test_manager_refuses_config_not_exactly_bound_to_the_store(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    alias = config.paths.analytics_root / "synthetic-store-alias.duckdb"
    alias.hardlink_to(config.paths.store_path)
    mismatched = config.model_copy(
        update={"paths": config.paths.model_copy(update={"store_path": alias})},
    )
    first = AnalyticsJobManager(store=store, config=config, handlers={})
    second: AnalyticsJobManager | None = None
    try:
        with pytest.raises(JobStateError, match=r"config.*store"):
            second = AnalyticsJobManager(store=store, config=mismatched, handlers={})
    finally:
        if second is not None:
            await second.shutdown()
        await first.shutdown()
        store.close()


@pytest.mark.anyio
async def test_handler_mapping_conversion_failure_does_not_leak_manager_lease(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    lease_key = config.paths.store_path.resolve(strict=True)
    manager: AnalyticsJobManager | None = None
    try:
        with pytest.raises(RuntimeError, match="mapping conversion"):
            AnalyticsJobManager(
                store=store,
                config=config,
                handlers=cast("jobs_module.HandlerMap", _ExplodingHandlerMapping()),
            )

        assert lease_key not in jobs_module._MANAGER_LEASES
        manager = AnalyticsJobManager(store=store, config=config, handlers={})
    finally:
        if manager is not None:
            await manager.shutdown()
        jobs_module._MANAGER_LEASES.discard(lease_key)
        store.close()


@pytest.mark.anyio
async def test_cancelled_shutdown_keeps_lease_until_suppressing_handler_stops(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _RepeatedCancellationSuppressingHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    shutdown_transport: asyncio.Task[None] | None = None
    replacement: AnalyticsJobManager | None = None
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()
        shutdown_transport = asyncio.create_task(manager.shutdown())
        await handler.cancelled.wait()

        shutdown_transport.cancel()
        done, _pending = await asyncio.wait({shutdown_transport}, timeout=0.1)
        caller_cancelled_promptly = shutdown_transport in done
        if caller_cancelled_promptly:
            with pytest.raises(asyncio.CancelledError):
                await shutdown_transport

        with pytest.raises(JobStateError, match=r"manager.*lease"):
            replacement = AnalyticsJobManager(store=store, config=config, handlers={})

        handler.release.set()
        await manager.shutdown()
        assert caller_cancelled_promptly is True
        replacement = AnalyticsJobManager(store=store, config=config, handlers={})
        row = replacement._require_row(started.job_id)
        assert row.state == "failed"
        assert row.persisted.status_code == "job_interrupted_restart_required"
    finally:
        handler.release.set()
        if shutdown_transport is not None and not shutdown_transport.done():
            await asyncio.gather(shutdown_transport, return_exceptions=True)
        await manager.shutdown()
        if replacement is not None:
            await replacement.shutdown()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize("stop_kind", ["cancel", "expire"])
async def test_transport_cancellation_cannot_strand_a_stop_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_kind: str,
) -> None:
    now = _NOW
    monkeypatch.setattr(jobs_module, "_utc_now", lambda: now)
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _RepeatedCancellationSuppressingHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
        job_ttl=timedelta(seconds=30),
    )
    transport: asyncio.Task[jobs_module.JobStatus] | None = None
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()
        if stop_kind == "expire":
            now += timedelta(seconds=31)
            transport = asyncio.create_task(manager.get_job(started.job_id))
            expected_status = "job_expired"
        else:
            transport = asyncio.create_task(manager.cancel_job(started.job_id))
            expected_status = "job_cancelled"
        await handler.cancelled.wait()

        transport.cancel()
        done, _pending = await asyncio.wait({transport}, timeout=0.1)
        caller_cancelled_promptly = transport in done
        if caller_cancelled_promptly:
            with pytest.raises(asyncio.CancelledError):
                await transport
        handler.release.set()
        if not caller_cancelled_promptly:
            await asyncio.gather(transport, return_exceptions=True)

        row = await _wait_for_persisted_state(manager, started.job_id, "cancelled")
        assert caller_cancelled_promptly is True
        assert row.persisted.status_code == expected_status
        assert row.analysis_id is None
    finally:
        handler.release.set()
        if transport is not None and not transport.done():
            await asyncio.gather(transport, return_exceptions=True)
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_cancel_stop_intent_blocks_a_suppressed_cancellation_conclusion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _CancellationSuppressingHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()

        stopped = await manager.cancel_job(started.job_id)

        assert handler.cancelled.is_set()
        assert stopped.state == "cancelled"
        assert stopped.status_code == "job_cancelled"
        assert stopped.conclusion_available is False
        assert stopped.analysis_id is None
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_shutdown_stop_intent_blocks_a_suppressed_cancellation_conclusion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    handler = _CancellationSuppressingHandler()
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": handler},
    )
    try:
        started = await manager.start_job(_request())
        await handler.started.wait()

        await manager.shutdown()
        row = manager._require_row(started.job_id)

        assert handler.cancelled.is_set()
        assert row.state == "failed"
        assert row.persisted.status_code == "job_interrupted_restart_required"
        assert row.analysis_id is None
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.anyio
async def test_immediate_prestart_cancellation_cleans_task_and_workspace(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    manager = AnalyticsJobManager(
        store=store,
        config=config,
        handlers={"monte_carlo": _ControlledHandler()},
    )
    try:
        started = await manager.start_job(_request())
        task = manager._tasks[started.job_id]
        workspace = manager._workspace_root / started.job_id
        task.cancel()

        cancelled = await manager.cancel_job(started.job_id)

        assert cancelled.state == "cancelled"
        assert started.job_id not in manager._tasks
        assert not workspace.exists()
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


def test_startup_removes_orphan_workspace_with_owner_read_only_children(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    workspace_root = config.paths.analytics_root / "job-workspaces"
    workspace_root.mkdir(mode=0o700)
    orphan = workspace_root / new_safe_handle(HandleKind.JOB_ID)
    nested = orphan / "nested"
    nested.mkdir(mode=0o700, parents=True)
    scratch = nested / "scratch.bin"
    scratch.write_bytes(b"synthetic-interrupted-workspace")
    scratch.chmod(0o400)
    nested.chmod(0o400)
    orphan.chmod(0o500)

    store = AnalyticsStore.open(config)
    manager: AnalyticsJobManager | None = None
    try:
        manager = AnalyticsJobManager(store=store, config=config, handlers={})
        assert not orphan.exists()
    finally:
        if orphan.exists():
            orphan.chmod(0o700)
            if nested.exists():
                nested.chmod(0o700)
            if scratch.exists():
                scratch.chmod(0o600)
        if manager is not None:
            asyncio.run(manager.shutdown())
        store.close()


def test_workspace_cleanup_refuses_hardlink_without_changing_external_inode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    workspace_root = config.paths.analytics_root / "job-workspaces"
    workspace_root.mkdir(mode=0o700)
    orphan = workspace_root / new_safe_handle(HandleKind.JOB_ID)
    orphan.mkdir(mode=0o700)
    external = config.paths.analytics_root / "synthetic-external-workspace-file"
    content = b"synthetic hardlink boundary content"
    external.write_bytes(content)
    external.chmod(0o400)
    original_mode = stat.S_IMODE(external.stat().st_mode)
    os.link(external, orphan / "linked.bin")

    store = AnalyticsStore.open(config)
    manager: AnalyticsJobManager | None = None
    try:
        with pytest.raises(JobStateError, match="owner-contained"):
            manager = AnalyticsJobManager(store=store, config=config, handlers={})
        assert external.read_bytes() == content
        assert stat.S_IMODE(external.stat().st_mode) == original_mode
        assert external.stat().st_nlink == 2
    finally:
        if manager is not None:
            asyncio.run(manager.shutdown())
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
        asyncio.run(manager.shutdown())
        store.close()
