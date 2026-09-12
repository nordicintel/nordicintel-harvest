"""Bounded discovery/resolution; destination failures stop the execution."""

import asyncio
from dataclasses import dataclass

from aiohttp import ClientError

from .core.config import Settings
from .core.errors import ExternalAPIError
from .core.request_manager import RequestManager
from .providers.registry import get_adapter
from .schemas.datasets import DatasetDocuments


@dataclass
class Progress:
    discovered: int = 0
    in_flight: int = 0
    attempts: int = 0
    listing_complete: bool = False


async def execute(
    harvest_input,
    context,
    emit,
    progress,
    *,
    limit=None,
    concurrency=5,
    adapter_factory=get_adapter,
    manager_factory=None,
    retry_delays=(5, 10),
):
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    settings = Settings()
    manager = manager_factory() if manager_factory else RequestManager.from_settings(settings)
    adapter = None
    tasks = set()
    seen = set()

    async def resolve(selection):
        candidate = selection.candidate
        stamp = selection.source_updated_at
        payload = {
            "dataset_code": candidate.dataset_code,
            "source_updated_at": stamp.isoformat() if stamp else None,
        }
        progress.in_flight += 1
        try:
            try:
                for attempt in range(len(retry_delays) + 1):
                    try:
                        documents = await adapter.resolve_dataset(candidate)
                        break
                    except (ExternalAPIError, ClientError, TimeoutError):
                        if attempt == len(retry_delays):
                            raise
                        await asyncio.sleep(retry_delays[attempt])
                documents = DatasetDocuments.model_validate(documents.documents())
                if documents.dataset.identity != candidate.identity:
                    raise ValueError("Resolved identity differs from candidate")
                payload.update(outcome="saved", documents=documents.documents())
            except Exception as exc:
                payload.update(outcome="failed", error=f"{type(exc).__name__}: {exc}"[:4000])
            # Deliberately outside the resolution exception handler: a lost catalog
            # must stop discovery rather than become hundreds of dataset failures.
            await emit(payload)
        finally:
            progress.in_flight -= 1

    try:
        await manager.set_limit(
            harvest_input.base_api_url,
            interval=harvest_input.rate_limit,
            max_concurrency=harvest_input.max_concurrency or concurrency,
        )
        adapter = adapter_factory(
            harvest_input.adapter,
            provider=harvest_input,
            language=harvest_input.language,
            request_manager=manager,
        )
        async for selection in adapter.discover_datasets(context):
            candidate = selection.candidate
            if (candidate.provider_code, candidate.language) != (
                harvest_input.provider_code,
                harvest_input.language,
            ):
                raise ValueError("Candidate outside resolved input scope")
            if candidate.dataset_code in seen:
                raise ValueError("Duplicate dataset in discovery")
            seen.add(candidate.dataset_code)
            progress.discovered += 1
            if not selection.should_fetch:
                await emit({"dataset_code": candidate.dataset_code, "outcome": "skipped"})
                continue
            progress.attempts += 1
            tasks.add(asyncio.create_task(resolve(selection)))
            if len(tasks) >= concurrency:
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                results = await asyncio.gather(*done, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
            if limit is not None and progress.attempts >= limit:
                break
        else:
            progress.listing_complete = limit is None
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if adapter:
                await adapter.close()
        finally:
            await manager.close()
