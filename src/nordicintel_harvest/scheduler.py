"""The scheduler process: recover abandoned jobs, then enqueue whatever is due.

There is exactly one of these. Its singleton advisory lock, and the acquire/recheck/unlock
recovery performs on each candidate, are session-scoped and span transactions, so the
whole process runs on one unpooled owner session and a duplicate process fails at startup
instead of quietly doubling the queue.

Recovery deliberately does not run on a worker's own session. Provider advisory locks are
reentrant within a session, so a worker asking whether its own job is abandoned would
always be told the lock is free — it holds it. Only a separate session's failure to take
that lock is evidence that nobody owns the job.

Two behaviours here are core's, and are kept rather than worked around. A due schedule for
a busy Provider is advanced without enqueuing, so a slow Provider does not accumulate one
job per missed tick; the run it missed is skipped, not queued. And recovery inserts no
retry: an abandoned job is closed as failed, and the ordinary schedule is what creates the
next attempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from nordicintel_core.database import (
    HarvestRepository,
    ScheduleRepository,
    create_owner_engine,
    owner_session,
)
from nordicintel_core.errors import ConfigurationError

from .settings import Settings, load_settings
from .worker import install_stop_handlers

logger = logging.getLogger("nordicintel.harvest.scheduler")


async def run_scheduler(settings: Settings, stop: asyncio.Event) -> None:
    """Recover and enqueue on one physical session until ``stop`` is set.

    Args:
        settings: Validated configuration; ``scheduler_poll_seconds`` is the tick.
        stop: Event requesting graceful shutdown.

    Raises:
        RuntimeError: Another scheduler already holds the singleton lock.
    """
    engine = create_owner_engine(
        settings.database_url,
        connect_args={"options": settings.statement_timeout_option},
    )
    try:
        with owner_session(engine) as session:
            schedules = ScheduleRepository(session)
            jobs = HarvestRepository(session)
            if not schedules.try_singleton_lock():
                raise RuntimeError("Another scheduler owns the singleton lock")
            logger.info("scheduler acquired the singleton lock")
            try:
                while not stop.is_set():
                    recovered = jobs.recover_stale(settings.stale_after_seconds)
                    if recovered:
                        logger.warning("recovered abandoned jobs: %s", recovered)
                    enqueued = schedules.enqueue_due()
                    for job in enqueued:
                        logger.info("enqueued job %s for provider %s", job.id, job.provider_id)
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop.wait(), timeout=settings.scheduler_poll_seconds
                        )
            finally:
                # On an exceptional exit the unpooled connection closes and PostgreSQL
                # drops the lock anyway; releasing it explicitly keeps the ordinary
                # shutdown observable.
                with suppress(Exception):
                    schedules.release_singleton_lock()
    finally:
        engine.dispose()


def main() -> int:
    """Console entry point for the scheduler."""
    logging.basicConfig(
        level=os.environ.get("NORDICINTEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = load_settings(os.environ)
    except ConfigurationError as exc:
        logger.error("scheduler configuration is invalid: %s", exc)
        return 2

    async def _run() -> None:
        stop = asyncio.Event()
        install_stop_handlers(stop)
        logger.info(
            "starting scheduler: poll %.1fs, stale %ds",
            settings.scheduler_poll_seconds,
            settings.stale_after_seconds,
        )
        await run_scheduler(settings, stop)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        return 130
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
