"""Liveness reporting and cancellation observation for one claimed job.

``HarvestRepository.heartbeat`` is one call with two jobs: it refreshes the stamp
recovery reads, and it returns whether a stop has been requested. So there is one timer
here, not two, and its interval is both how promptly a cancellation is noticed and how
long a healthy worker can look dead.

The timer runs as a task on the event loop that owns the session, never on a worker
thread. Core's repository methods are synchronous and hold their transaction for the
whole call, so a task cannot interleave with one halfway through; a call from another
thread would be a second connection and would fail the ``pg_backend_pid()`` check that
defines ownership.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from types import TracebackType
from typing import TypeVar

from nordicintel_core.database import HarvestRepository
from nordicintel_core.errors import OwnershipLost

from .errors import HarvestStopped

T = TypeVar("T")

Sleep = Callable[[float], Awaitable[None]]


class JobControl:
    """Beats for one job, and turns an observed stop into a signal the engine can see."""

    def __init__(
        self,
        queue: HarvestRepository,
        job_id: int,
        *,
        interval_seconds: float,
        process_stop: asyncio.Event | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._queue = queue
        self._job_id = job_id
        self._interval = interval_seconds
        self._process_stop = process_stop or asyncio.Event()
        self._stopping = asyncio.Event()
        self._reason = ""
        self._ownership_lost = False
        self._task: asyncio.Task[None] | None = None

    @property
    def stopping(self) -> bool:
        """Whether a stop has been observed and no further upstream work should start."""
        return self._stopping.is_set()

    @property
    def ownership_lost(self) -> bool:
        """Whether the beat failed in a way that means this session no longer owns the job.

        When this is true the attempt must write nothing more, including its own
        finalization: another process may already be recovering the job.
        """
        return self._ownership_lost

    @property
    def reason(self) -> str:
        return self._reason or "The job was asked to stop."

    async def __aenter__(self) -> JobControl:
        self._task = asyncio.create_task(self._beat(), name=f"heartbeat-{self._job_id}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop beating and wait for the timer to be gone.

        Finalization runs after this returns, so a beat can never touch a job that has
        already reached a terminal status.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def request_stop(self, reason: str = "The attempt was asked to stop.") -> None:
        """Stop the attempt now, without waiting for the next beat.

        The database is not told anything here; a caller that wants the stop recorded
        calls ``HarvestRepository.cancel`` as well. The shutdown path does both.
        """
        self._stop(reason)

    def raise_if_stopping(self) -> None:
        """Refuse to start another unit of work once a stop has been observed."""
        if self._stopping.is_set():
            raise HarvestStopped(self.reason)

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Await upstream work, abandoning it promptly if a stop arrives meanwhile.

        The work is cancelled and then awaited, so its own cleanup finishes before this
        returns. Nothing after this call may assume the request completed.

        Raises:
            HarvestStopped: A stop was observed before the work finished.
        """
        self.raise_if_stopping()
        work: asyncio.Task[T] = asyncio.ensure_future(awaitable)
        watch: asyncio.Task[bool] = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            watch.cancel()
            with suppress(asyncio.CancelledError):
                await watch
        if work.done():
            return work.result()
        work.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await work
        raise HarvestStopped(self.reason)

    async def _beat(self) -> None:
        while True:
            if await self._wait_tick():
                # The process is shutting down. Make the intent durable before acting on
                # it, so a job that dies here is recovered as cancelled, not as abandoned.
                self._request_cancellation()
                return
            try:
                if self._queue.heartbeat(self._job_id):
                    self._stop("The job was cancelled.")
                    return
            except OwnershipLost:
                self._ownership_lost = True
                self._stop("This session no longer owns the job.")
                return
            except Exception:  # a broken session cannot be beaten back
                self._ownership_lost = True
                self._stop("The owner session failed while reporting liveness.")
                return

    async def _wait_tick(self) -> bool:
        """Sleep one interval. Return True if the process was asked to shut down."""
        try:
            await asyncio.wait_for(self._process_stop.wait(), timeout=self._interval)
        except TimeoutError:
            return False
        return True

    def _request_cancellation(self) -> None:
        try:
            self._queue.cancel(self._job_id)
        except Exception:  # shutdown proceeds even if the request fails
            self._ownership_lost = True
        self._stop("The worker process is shutting down.")

    def _stop(self, reason: str) -> None:
        self._reason = reason
        self._stopping.set()
