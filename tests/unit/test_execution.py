import asyncio
from datetime import UTC, datetime

import pytest

from nordicintel_harvest.catalog import CatalogError
from nordicintel_harvest.inputs import HarvestInput
from nordicintel_harvest.providers.interface import (
    DatasetCandidate,
    DatasetSelection,
    HarvestContext,
)
from nordicintel_harvest.runner import Progress, execute
from nordicintel_harvest.schemas.datasets import DatasetDocuments
from nordicintel_harvest.worker import run_job

INPUT = HarvestInput(
    adapter="pxweb_v2",
    provider_code="test",
    language="en",
    rate_limit=0,
    config={"base_api_url": "https://example.org"},
)


class Manager:
    async def set_limit(self, *args, **kwargs):
        pass

    async def close(self):
        pass


class Adapter:
    calls = []

    async def discover_datasets(self, context):
        for i in range(15):
            yield DatasetSelection(
                DatasetCandidate(provider_code="test", dataset_code=str(i), language="en"), i >= 5
            )

    async def resolve_dataset(self, candidate):
        self.calls.append(candidate.dataset_code)
        if candidate.dataset_code == "6":
            raise ValueError("malformed source")
        identity = candidate.identity.model_dump()
        return DatasetDocuments.model_validate(
            {
                "dataset": {"identity": identity, "label": "Test"},
                "metadata": {
                    "identity": identity,
                    "id": ["x"],
                    "dimension": {
                        "x": {"label": "X", "category": {"index": {"a": 0}, "label": {"a": "A"}}}
                    },
                },
            }
        )

    async def close(self):
        pass


async def test_limit_counts_attempts_not_skips_or_successes():
    adapter = Adapter()
    adapter.calls = []
    emitted = []

    async def emit(payload):
        emitted.append(payload)

    progress = Progress()
    await execute(
        INPUT,
        HarvestContext({}, datetime.now(UTC)),
        emit,
        progress,
        limit=3,
        concurrency=2,
        adapter_factory=lambda *a, **k: adapter,
        manager_factory=Manager,
    )
    assert adapter.calls == ["5", "6", "7"]
    assert [p["outcome"] for p in emitted].count("skipped") == 5
    assert [p["outcome"] for p in emitted].count("failed") == 1
    assert progress.attempts == 3 and not progress.listing_complete


async def test_destination_failure_stops_runner():
    async def emit(payload):
        raise CatalogError(401, "invalid token")

    with pytest.raises(CatalogError):
        await execute(
            INPUT,
            HarvestContext({}, datetime.now(UTC)),
            emit,
            Progress(),
            adapter_factory=lambda *a, **k: Adapter(),
            manager_factory=Manager,
        )


class Client:
    def __init__(self, cancel=False):
        self.cancel = cancel
        self.completed = []

    async def request(self, method, path, **kwargs):
        if path.endswith("/state"):
            return {"items": []}
        if path.endswith("/heartbeat"):
            return {"cancel_requested": self.cancel, "lease_seconds": 90}
        if path.endswith("/complete"):
            self.completed.append(kwargs["body"])
            return {}


def claimed():
    return {
        "job": {
            "id": "test",
            "input": INPUT.model_dump(),
            "started_at": datetime.now(UTC).isoformat(),
            "force": False,
            "limit": 5,
        },
        "claim_token": "test",
        "lease_seconds": 90,
        "checkpoint": None,
    }


async def hanging(*args, **kwargs):
    await asyncio.sleep(100)


async def test_cancellation_stops_work_and_completes():
    client = Client(cancel=True)
    await asyncio.wait_for(run_job(client, claimed(), asyncio.Event(), execute_fn=hanging), 1)
    assert client.completed == [{"listing_complete": False, "error": None}]


async def test_worker_stop_and_repeated_signal_have_one_finalization():
    stop = asyncio.Event()
    client = Client()
    task = asyncio.create_task(run_job(client, claimed(), stop, execute_fn=hanging))
    await asyncio.sleep(0.02)
    stop.set()
    stop.set()
    await asyncio.wait_for(task, 1)
    assert len(client.completed) == 1 and client.completed[0]["error"] == "Worker interrupted"


async def test_cleanup_has_overall_deadline():
    class Slow(Client):
        async def request(self, method, path, **kwargs):
            if path.endswith("/complete"):
                await asyncio.sleep(100)
            return await super().request(method, path, **kwargs)

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(
        run_job(Slow(), claimed(), stop, execute_fn=hanging, cleanup_seconds=0.05), 0.5
    )


async def test_lost_heartbeat_stops_worker():
    class Broken(Client):
        async def request(self, method, path, **kwargs):
            if path.endswith("/heartbeat"):
                raise CatalogError(503, "unavailable")
            return await super().request(method, path, **kwargs)

    client = Broken()
    await asyncio.wait_for(run_job(client, claimed(), asyncio.Event(), execute_fn=hanging), 1)
    assert "CatalogError" in client.completed[0]["error"]
