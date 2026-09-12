"""Single-process queue consumer with renewable claims and bounded shutdown."""

import asyncio
import logging
import time
import uuid
from datetime import datetime

from .inputs import HarvestInput
from .providers.interface import DatasetHarvestState, HarvestContext
from .runner import Progress, execute

log = logging.getLogger(__name__)


def parsed(value):
    return datetime.fromisoformat(value) if value else None


async def run_job(
    client,
    claimed,
    stop,
    *,
    concurrency=5,
    claim_started=None,
    heartbeat_seconds=15,
    cleanup_seconds=20,
    execute_fn=execute,
):
    j = claimed["job"]
    token = claimed["claim_token"]
    root = f"/v1/harvests/{j['id']}"
    progress = Progress()
    deadline = (claim_started or time.monotonic()) + claimed["lease_seconds"] - 5

    async def work():
        states = {}
        offset = 0
        while True:
            page = await client.request(
                "GET", root + "/state", claim=token, params={"offset": offset, "limit": 1000}
            )
            for row in page["items"]:
                states[row["dataset_code"]] = DatasetHarvestState(
                    parsed(row["source_updated_at"]),
                    parsed(row["last_fetched_at"]),
                    row["has_error"],
                )
            if len(page["items"]) < 1000:
                break
            offset += 1000
        context = HarvestContext(
            states, parsed(j["started_at"]), parsed(claimed["checkpoint"]), j["force"]
        )

        async def emit(payload):
            await client.request("POST", root + "/datasets", body=payload, claim=token)

        await execute_fn(
            HarvestInput.model_validate(j["input"]),
            context,
            emit,
            progress,
            limit=j["limit"],
            concurrency=concurrency,
        )

    async def heartbeat():
        nonlocal deadline
        while True:
            started = time.monotonic()
            result = await client.request(
                "POST",
                root + "/heartbeat",
                claim=token,
                body={"discovered": progress.discovered, "in_flight": progress.in_flight},
            )
            deadline = started + result["lease_seconds"] - 5
            if result["cancel_requested"]:
                return "cancelled"
            await asyncio.sleep(heartbeat_seconds)

    async def lease_watchdog():
        while time.monotonic() < deadline:
            await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        return "Worker lease could not be renewed"

    task = asyncio.create_task(work())
    pulse = asyncio.create_task(heartbeat())
    watchdog = asyncio.create_task(lease_watchdog())
    stopped = asyncio.create_task(stop.wait())
    children = [task, pulse, watchdog, stopped]
    error = None
    try:
        done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            error = "Worker interrupted"
        elif watchdog in done:
            error = watchdog.result()
        elif pulse in done:
            pulse.result()  # cancellation request, or fatal catalog communication
        else:
            task.result()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:4000]
    finally:
        for child in children:
            child.cancel()
        try:
            async with asyncio.timeout(cleanup_seconds):
                await asyncio.gather(*children, return_exceptions=True)
                await client.request(
                    "POST",
                    root + "/complete",
                    claim=token,
                    body={
                        "listing_complete": progress.listing_complete
                        and not error
                        and task.done()
                        and not task.cancelled(),
                        "error": error,
                    },
                )
        except Exception as exc:
            log.warning(
                "Job %s finalization unavailable (%s); lease recovery applies",
                j["id"],
                type(exc).__name__,
            )
    log.info("Job %s execution finished; attempts=%s", j["id"], progress.attempts)


async def worker(client, stop, *, concurrency=5, poll_seconds=5):
    worker_id = str(uuid.uuid4())
    while not stop.is_set():
        try:
            started = time.monotonic()
            # Do not replay an ambiguous claim: an accepted response might have been
            # lost. Its lease will expire; blindly claiming again could orphan work.
            claim_task = asyncio.create_task(
                client.request(
                    "POST", "/v1/harvests/claim", body={"worker_id": worker_id}, retry=False
                )
            )
            stop_task = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    [claim_task, stop_task], return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    return  # an ambiguous accepted claim expires durably
                claimed = claim_task.result()
            finally:
                claim_task.cancel()
                stop_task.cancel()
                await asyncio.gather(claim_task, stop_task, return_exceptions=True)
            if claimed:
                await run_job(client, claimed, stop, concurrency=concurrency, claim_started=started)
                continue
        except Exception as exc:
            log.warning("Claim unavailable: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass
