"""The worker process: claim one job, run it on one backend, finalize it, release it.

The whole design follows from one constraint. Ownership of a job is a physical database
backend — ``harvest_job.owner_backend_pid`` and a session-scoped advisory lock — so an
attempt lives inside exactly one ``owner_session`` and dies with it. Nothing here
reconnects: a lost connection ends the attempt, and the scheduler's recovery decides what
happens to the job, because only a process that can prove it holds the lock may say so.

That is also why an attempt is finalized before the session closes, and why every path out
of :meth:`Worker.run_once` goes through the same finalization: a job left running with no
owner is invisible work until its heartbeat goes stale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from types import TracebackType

import httpx
from nordicintel_core.database import (
    HarvestRepository,
    MetadataRepository,
    ProviderRepository,
    create_owner_engine,
    owner_session,
)
from nordicintel_core.errors import ConfigurationError, OwnershipLost
from nordicintel_core.http import HttpClient
from nordicintel_core.models import (
    DiagnosticStage,
    HarvestJob,
    JobStatus,
    NordicIntelAdapter,
    ProviderDefinition,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from . import diagnostics
from .control import JobControl
from .engine import HarvestEngine, HarvestSummary
from .errors import HarvestStopped, JobFailed
from .registry import AdapterRegistry
from .secrets import resolve_secrets
from .settings import Settings, load_settings

logger = logging.getLogger("nordicintel.harvest.worker")


@dataclass(slots=True)
class _Attempt:
    """Everything one claimed job needs, and the client it must close afterwards."""

    provider: ProviderDefinition
    adapter: NordicIntelAdapter
    client: httpx.AsyncClient


class Worker:
    """Claims and runs harvest jobs until it is asked to stop."""

    def __init__(
        self,
        settings: Settings,
        registry: AdapterRegistry,
        environment: Mapping[str, str],
        *,
        database: Engine | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._environment = environment
        self._owns_database = database is None
        self._database = database or create_owner_engine(
            settings.database_url,
            connect_args={"options": settings.statement_timeout_option},
        )

    async def __aenter__(self) -> Worker:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_database:
            self._database.dispose()

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and run jobs until ``stop`` is set.

        An empty queue is not an error: the worker waits one poll interval and asks again.
        A failure to reach the database is, and it propagates to the supervisor rather
        than being retried behind a session that may already have lost its locks.
        """
        while not stop.is_set():
            if await self.run_once(stop):
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.queue_poll_seconds)

    async def run_once(self, stop: asyncio.Event) -> bool:
        """Claim at most one job and run it to a terminal status.

        Returns:
            True if a job was claimed, whatever its outcome; False if the queue had
            nothing this worker could take.
        """
        with owner_session(self._database) as session:
            queue = HarvestRepository(session)
            job = queue.claim()
            if job is None:
                return False
            logger.info(
                "claimed job %s for provider %s in %s",
                job.id,
                job.provider_id,
                job.request.language,
            )
            try:
                await self._run_job(session, queue, job, stop)
            finally:
                # A lost session has already dropped its locks; saying so is not an error
                # worth failing a shutdown over.
                with suppress(Exception):
                    queue.release_provider(job.provider_id)
        return True

    async def _run_job(
        self,
        session: Session,
        queue: HarvestRepository,
        job: HarvestJob,
        stop: asyncio.Event,
    ) -> None:
        attempt: _Attempt | None = None
        control: JobControl | None = None
        summary: HarvestSummary | None = None
        terminal = JobStatus.COMPLETED
        diagnostic = None
        abandoned = False
        try:
            attempt = await self._prepare(session, job)
            control = JobControl(
                queue,
                job.id,
                interval_seconds=self._settings.heartbeat_seconds,
                process_stop=stop,
            )
            async with control:
                summary = await HarvestEngine(
                    queue=queue,
                    metadata=MetadataRepository(session),
                    adapter=attempt.adapter,
                    job=job,
                    provider=attempt.provider,
                    control=control,
                ).run()
        except HarvestStopped:
            terminal = JobStatus.CANCELLED
        except JobFailed as exc:
            terminal, diagnostic = JobStatus.FAILED, exc.diagnostic
        except OwnershipLost as exc:
            # Metadata writes refuse a cancelled job, a disabled Provider and a lost
            # backend alike, and only the last of those forbids finalizing here.
            observed = self._observe_stop(queue, job)
            if observed is None:
                abandoned = True
            elif observed:
                terminal = JobStatus.CANCELLED
            else:
                terminal, diagnostic = JobStatus.FAILED, diagnostics.diagnose(
                    exc, stage=DiagnosticStage.INTERRUPTED
                )
        except Exception as exc:  # an unexpected defect still has to finalize
            logger.exception("job %s ended unexpectedly", job.id)
            terminal, diagnostic = JobStatus.FAILED, diagnostics.diagnose(
                exc, stage=DiagnosticStage.INTERRUPTED
            )
        finally:
            if attempt is not None:
                await attempt.client.aclose()
        if abandoned or (control is not None and control.ownership_lost):
            # Another process may already be recovering this job. Writing now would be a
            # second owner reporting an outcome it cannot vouch for.
            logger.warning("job %s abandoned: the owner session was lost", job.id)
            return
        try:
            finished = queue.finish_job(job.id, terminal, error=diagnostic)
        except OwnershipLost:
            logger.warning("job %s abandoned: ownership was lost before finalization", job.id)
            return
        logger.info(
            "job %s finished as %s (%s)",
            job.id,
            finished.status.value,
            "no summary" if summary is None else _describe(summary),
        )

    def _request_interval(self, provider: ProviderDefinition) -> float:
        return _request_interval(self._settings, provider)

    def _observe_stop(self, queue: HarvestRepository, job: HarvestJob) -> bool | None:
        """Ask the database whether this session still owns the job, and whether to stop.

        Returns:
            True if the job is still owned and a stop was requested, False if it is owned
            and no stop was requested, and None if ownership could not be demonstrated —
            in which case nothing further may be written from this session.
        """
        try:
            return queue.heartbeat(job.id)
        except Exception:  # any failure here means: write nothing more
            return None

    async def _prepare(self, session: Session, job: HarvestJob) -> _Attempt:
        """Build everything the job needs, turning any setup failure into a job failure."""
        client: httpx.AsyncClient | None = None
        try:
            provider = ProviderRepository(session).get(job.provider_id)
            if provider is None:
                raise ConfigurationError(f"Provider {job.provider_id!r} no longer exists")
            factory = self._registry.get(provider.adapter_type)
            secrets = resolve_secrets(provider, self._environment)
            client = httpx.AsyncClient(timeout=self._settings.http_timeout_seconds)
            http = HttpClient(client, minimum_interval_seconds=self._request_interval(provider))
            adapter = await factory.create(provider, secrets, http)
        except Exception as exc:
            if client is not None:
                await client.aclose()
            raise JobFailed(
                diagnostics.diagnose(exc, stage=DiagnosticStage.DISCOVERY)
            ) from exc
        return _Attempt(provider=provider, adapter=adapter, client=client)


REQUEST_INTERVAL_KEY = "request_interval_seconds"


def _request_interval(settings: Settings, provider: ProviderDefinition) -> float:
    """How far apart this Provider's requests must be.

    Upstream quotas are the Provider's property, not the worker's: one publisher allows
    three calls a second and the next allows one every two. The worker builds the HTTP
    client, so the Provider row is where that number has to be read from, and the process
    default only applies to Providers that do not state one.
    """
    if REQUEST_INTERVAL_KEY not in provider.config:
        return settings.minimum_request_interval_seconds
    value = provider.config[REQUEST_INTERVAL_KEY]
    # Core decodes JSONB with exact decimals, so a fractional interval arrives as a
    # Decimal even though it was written as a JSON number.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or value < 0:
        raise ConfigurationError(
            f"provider.config.{REQUEST_INTERVAL_KEY} must be a non-negative number"
        )
    return float(value)


def _describe(summary: HarvestSummary) -> str:
    return (
        f"{summary.language}: {summary.updated} updated, {summary.skipped} skipped, "
        f"{summary.failed} failed"
    )


def install_stop_handlers(stop: asyncio.Event) -> None:
    """Ask the running loop to set ``stop`` on the signals a supervisor sends.

    Signal handling is not available on every platform this may run on during
    development, so an unsupported signal is skipped rather than failing startup.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(received, stop.set)


def log_startup(role: str, settings: Settings, registry: AdapterRegistry) -> None:
    """Record what is actually running, so a report can be tied to a build."""
    packages = []
    for name in ("nordicintel-harvest", "nordicintel-core"):
        try:
            packages.append(f"{name}=={version(name)}")
        except PackageNotFoundError:  # pragma: no cover - source checkouts
            packages.append(f"{name}==unknown")
    logger.info(
        "starting %s: %s, python %s, adapters [%s], heartbeat %.1fs, stale %ds",
        role,
        ", ".join(packages),
        sys.version.split()[0],
        ", ".join(registry) or "none",
        settings.heartbeat_seconds,
        settings.stale_after_seconds,
    )


async def run_worker(
    settings: Settings, registry: AdapterRegistry, environment: Mapping[str, str]
) -> None:
    """Run one worker process until it is signalled, then let its job finish.

    The claimed job stops cooperatively, which takes as long as the upstream request it is
    waiting on. ``shutdown_budget_seconds`` bounds that: past it the attempt is cancelled
    outright, and the job is left for recovery rather than held while a supervisor waits.
    """
    stop = asyncio.Event()
    install_stop_handlers(stop)
    log_startup("worker", settings, registry)
    async with Worker(settings, registry, environment) as worker:
        running = asyncio.ensure_future(worker.run(stop))
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({running, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not running.done():
                logger.info("shutdown requested; finishing the running job")
                try:
                    await asyncio.wait_for(running, timeout=settings.shutdown_budget_seconds)
                except TimeoutError:
                    logger.warning("shutdown budget expired; the job is left for recovery")
            else:
                await running
        finally:
            stopped.cancel()
            with suppress(asyncio.CancelledError):
                await stopped


def main() -> int:
    """Console entry point for the worker."""
    logging.basicConfig(
        level=os.environ.get("NORDICINTEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = load_settings(os.environ)
        registry = AdapterRegistry.from_entry_points(settings.adapters)
    except ConfigurationError as exc:
        logger.error("worker configuration is invalid: %s", exc)
        return 2
    try:
        asyncio.run(run_worker(settings, registry, os.environ))
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
