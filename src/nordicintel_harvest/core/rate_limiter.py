from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RateLimiter:
    """Per-host limiter with interval spacing and concurrency cap."""

    def __init__(
        self,
        interval: float,
        max_concurrency: int,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval < 0:
            raise ValueError("Interval must be non-negative.")
        if max_concurrency < 1:
            raise ValueError("Max concurrency must be at least 1.")

        self.logger = logger or logging.getLogger("RateLimiter")
        self.interval = float(interval)
        self.max_concurrency = int(max_concurrency)
        self.semaphore = asyncio.Semaphore(self.max_concurrency)
        self.last_request_time = time.monotonic() - self.interval
        self.lock = asyncio.Lock()

    async def wait_for_interval(self, *, interval_override: float | None = None) -> None:
        effective_interval = self.interval if interval_override is None else interval_override
        if effective_interval < 0:
            raise ValueError("Effective interval must be non-negative.")

        async with self.lock:
            now = asyncio.get_running_loop().time()
            elapsed = now - self.last_request_time
            if elapsed < effective_interval:
                await asyncio.sleep(effective_interval - elapsed)
            self.last_request_time = asyncio.get_running_loop().time()

    @asynccontextmanager
    async def throttle(self, *, interval_override: float | None = None) -> AsyncIterator[None]:
        await self.semaphore.acquire()
        try:
            await self.wait_for_interval(interval_override=interval_override)
            yield
        finally:
            self.semaphore.release()
