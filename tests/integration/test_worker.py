"""Worker and scheduler behaviour against a real PostgreSQL.

Everything here is about ownership: which process is allowed to advance a job, what
happens when the process holding it disappears, and how a stop reaches work that is
already in flight. None of it can be demonstrated without real backends, because the
mechanisms are ``pg_backend_pid()`` and session-scoped advisory locks.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from nordicintel_core.database import (
    HarvestRepository,
    MetadataRepository,
    ProviderRepository,
    ScheduleRepository,
    backend_pid,
)
from nordicintel_core.models import (
    DiagnosticStage,
    DiscoveryEntry,
    HarvestRequest,
    ItemStatus,
    JobStatus,
)
from sqlalchemy import text
from support import (
    PROVIDER_ID,
    StubAdapter,
    StubFactory,
    database_url,
    owner,
    provider,
    register,
)

from nordicintel_harvest.registry import AdapterRegistry
from nordicintel_harvest.scheduler import run_scheduler
from nordicintel_harvest.settings import Settings
from nordicintel_harvest.worker import Worker

pytestmark = pytest.mark.postgres

TAB1 = DiscoveryEntry(native_table_id="TAB1")
TAB2 = DiscoveryEntry(native_table_id="TAB2")


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": database_url(),
        "heartbeat_seconds": 0.2,
        "stale_after_seconds": 3,
        "queue_poll_seconds": 0.05,
        "scheduler_poll_seconds": 0.05,
        "shutdown_budget_seconds": 5.0,
        "statement_timeout_seconds": 2.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def build_worker(adapter: StubAdapter, **overrides: object) -> tuple[Worker, StubFactory]:
    factory = StubFactory(adapter)
    registry = AdapterRegistry({"stub": factory})
    return Worker(settings(**overrides), registry, {}), factory


async def test_a_worker_harvests_a_queued_job_and_releases_its_provider() -> None:
    with owner() as session:
        register(session, secret_refs={"api_key": "SCB_API_KEY"})
        job = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    factory = StubFactory(StubAdapter(entries=[TAB1, TAB2]))
    worker = Worker(
        settings(), AdapterRegistry({"stub": factory}), {"SCB_API_KEY": "secret-value"}
    )
    try:
        assert await worker.run_once(asyncio.Event()) is True
    finally:
        worker.close()

    assert factory.created == [(PROVIDER_ID, {"api_key": "secret-value"})]
    with owner() as session:
        queue = HarvestRepository(session)
        finished = queue.get_job(job.id)
        assert finished is not None and finished.status is JobStatus.COMPLETED
        statuses = {item.status for item in queue.list_items(job.id)}
        assert statuses == {ItemStatus.UPDATED}
        stored = MetadataRepository(session).get_table_by_native(PROVIDER_ID, "TAB1")
        assert stored is not None
        assert MetadataRepository(session).search("Befolkning")[0].table_id == stored.table_id
        # The provider lock went with the closed session, so the next job can claim it.
        assert queue.enqueue(PROVIDER_ID, HarvestRequest(force=True)) is not None
        assert queue.claim() is not None


async def test_a_missing_adapter_fails_the_job_with_a_diagnostic() -> None:
    with owner() as session:
        ProviderRepository(session).upsert(provider(adapter_type="pxweb"))
        job = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    worker, _ = build_worker(StubAdapter(entries=[TAB1]))
    try:
        assert await worker.run_once(asyncio.Event()) is True
    finally:
        worker.close()

    with owner() as session:
        failed = HarvestRepository(session).get_job(job.id)
        assert failed is not None and failed.status is JobStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "configuration_invalid"
        assert "pxweb" in failed.error.message


async def test_a_missing_secret_fails_the_job_without_naming_the_value() -> None:
    with owner() as session:
        register(session, secret_refs={"api_key": "SCB_API_KEY"})
        job = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    worker, _ = build_worker(StubAdapter(entries=[TAB1]))
    try:
        await worker.run_once(asyncio.Event())
    finally:
        worker.close()

    with owner() as session:
        failed = HarvestRepository(session).get_job(job.id)
        assert failed is not None and failed.status is JobStatus.FAILED
        assert failed.error is not None
        assert "api_key -> SCB_API_KEY" in failed.error.message


async def test_two_workers_never_run_one_provider_at_the_same_time() -> None:
    with owner() as session:
        register(session)
        register(session, "ssb")
        queue = HarvestRepository(session)
        queue.enqueue(PROVIDER_ID, HarvestRequest())
        queue.enqueue(PROVIDER_ID, HarvestRequest(force=True))
        queue.enqueue("ssb", HarvestRequest())

    workers = [build_worker(StubAdapter(entries=[TAB1]))[0] for _ in range(2)]
    try:
        claimed = await asyncio.gather(*(w.run_once(asyncio.Event()) for w in workers))
    finally:
        for worker in workers:
            worker.close()

    assert claimed == [True, True]
    with owner() as session:
        jobs = HarvestRepository(session).list_jobs(limit=10)
        by_provider = [(job.provider_id, job.status) for job in jobs]
        # Both workers found work, and they cannot have taken the same Provider: the
        # second scb job is still queued because its Provider was busy.
        assert (PROVIDER_ID, JobStatus.QUEUED) in by_provider
        assert sum(1 for _, status in by_provider if status is JobStatus.COMPLETED) == 2
        assert {provider_id for provider_id, status in by_provider
                if status is JobStatus.COMPLETED} == {PROVIDER_ID, "ssb"}


async def test_a_cancelled_job_stops_promptly_and_finalizes_as_cancelled() -> None:
    with owner() as session:
        register(session)
        job = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    async def cancel_then_block() -> object:
        with owner() as canceller:
            HarvestRepository(canceller).cancel(job.id)
        await asyncio.sleep(30)
        raise AssertionError("the cancelled request should never complete")

    adapter = StubAdapter(
        entries=[TAB1, TAB2],
        languages=["sv"],
        behaviour={("TAB2", "sv"): cancel_then_block},
    )
    worker, _ = build_worker(adapter)
    try:
        await asyncio.wait_for(worker.run_once(asyncio.Event()), timeout=15)
    finally:
        worker.close()

    with owner() as session:
        queue = HarvestRepository(session)
        cancelled = queue.get_job(job.id)
        assert cancelled is not None and cancelled.status is JobStatus.CANCELLED
        assert cancelled.error is None
        items = queue.list_items(job.id)
        assert items[0].status is ItemStatus.UPDATED
        assert items[1].status is ItemStatus.FAILED
        assert items[1].error is not None
        assert items[1].error.stage is DiagnosticStage.INTERRUPTED


async def test_shutting_the_worker_down_cancels_the_job_it_is_running() -> None:
    with owner() as session:
        register(session)
        job = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    stop = asyncio.Event()

    async def block_until_shutdown() -> object:
        stop.set()
        await asyncio.sleep(30)
        raise AssertionError("the cancelled request should never complete")

    adapter = StubAdapter(
        entries=[TAB1], languages=["sv"], behaviour={("TAB1", "sv"): block_until_shutdown}
    )
    worker, _ = build_worker(adapter)
    try:
        await asyncio.wait_for(worker.run(stop), timeout=15)
    finally:
        worker.close()

    with owner() as session:
        cancelled = HarvestRepository(session).get_job(job.id)
        # The shutdown made its intent durable before acting on it, so this reads as a
        # deliberate cancellation rather than as a worker that vanished.
        assert cancelled is not None and cancelled.status is JobStatus.CANCELLED


async def test_a_job_whose_owner_disappears_is_recovered_not_resumed() -> None:
    with owner() as session:
        register(session)
        job = HarvestRepository(session).enqueue(PROVIDER_ID, HarvestRequest())

    with owner() as doomed:
        queue = HarvestRepository(doomed)
        claimed = queue.claim()
        assert claimed is not None and claimed.id == job.id
        queue.begin_item(job.id, "TAB1")
        backend = backend_pid(doomed)
    # Terminating the backend drops the advisory lock and the ownership token with it,
    # which is the only evidence recovery is allowed to act on.
    with owner() as killer, killer.begin():
        killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend})

    stop = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(3.5)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(run_scheduler(settings(), stop), tick()), timeout=20
    )

    with owner() as session:
        queue = HarvestRepository(session)
        recovered = queue.get_job(job.id)
        assert recovered is not None and recovered.status is JobStatus.FAILED
        assert recovered.error is not None and recovered.error.code == "worker_abandoned"
        # Recovery inserts no retry; the ordinary schedule is what creates the next run.
        assert [row.id for row in queue.list_jobs()] == [job.id]
        assert queue.list_items(job.id)[0].status is ItemStatus.FAILED


async def test_a_second_scheduler_refuses_to_start() -> None:
    stop = asyncio.Event()
    started = asyncio.Event()

    async def first() -> None:
        await run_scheduler(settings(scheduler_poll_seconds=0.05), stop)

    async def second() -> None:
        await asyncio.sleep(0.3)
        started.set()
        with pytest.raises(RuntimeError, match="singleton lock"):
            await run_scheduler(settings(), asyncio.Event())
        stop.set()

    await asyncio.wait_for(asyncio.gather(first(), second()), timeout=15)
    assert started.is_set()


async def test_the_scheduler_enqueues_due_work_and_skips_a_busy_provider() -> None:
    with owner() as session:
        register(session)
        ScheduleRepository(session).upsert(
            PROVIDER_ID,
            enabled=True,
            every_seconds=3600,
            next_run_at=datetime.now(UTC),
            request=HarvestRequest(),
        )

    stop = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.4)
        stop.set()

    await asyncio.gather(run_scheduler(settings(), stop), tick())

    with owner() as session:
        queue = HarvestRepository(session)
        jobs = queue.list_jobs()
        # One tick enqueued the due schedule; later ticks found the Provider already
        # queued and advanced the schedule instead of stacking a second job on it.
        assert len(jobs) == 1
        assert jobs[0].status is JobStatus.QUEUED
        schedule = ScheduleRepository(session).get(PROVIDER_ID)
        assert schedule is not None and schedule.next_run_at > datetime.now(UTC)
