"""CLI controls use the same durable API as every other caller."""

import argparse
import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from .catalog import CatalogClient, CatalogError
from .schemas.contracts import validate_contract
from .worker import worker


async def dispatch(args):
    url = os.environ.get("CATALOG_API_URL", "").strip()
    token = os.environ.get("CATALOG_WRITE_TOKEN", "").strip()
    if not url or not token:
        raise ValueError("CATALOG_API_URL and CATALOG_WRITE_TOKEN are required")
    client = CatalogClient(
        url, token, float(os.environ.get("CATALOG_REQUEST_TIMEOUT_SECONDS", "10"))
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def interrupt(*_):
        loop.call_soon_threadsafe(stop.set)

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if args.command == "worker":
            await worker(
                client,
                stop,
                concurrency=int(os.environ.get("HARVEST_MAX_CONCURRENCY", "5")),
                poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "5")),
            )
            return
        if args.command == "request":
            result = await client.request(
                "POST",
                "/v1/harvests",
                body={
                    "provider_code": args.provider,
                    "language": args.language,
                    "force": args.force,
                    "limit": args.limit,
                },
                retry=False,
            )
        elif args.command == "request-all":
            result = await client.request(
                "POST",
                "/v1/harvests/batch",
                body={"all_configured": True, "force": args.force, "limit": args.limit},
                retry=False,
            )
        elif args.command in ("status", "cancel"):
            suffix = "/cancel" if args.command == "cancel" else ""
            result = await client.request(
                "POST" if suffix else "GET", f"/v1/harvests/{args.job_id}" + suffix
            )
        else:
            records = json.loads(Path(args.file).read_text(encoding="utf-8"))
            for record in records:
                validate_contract("provider", record)
            result = {"imported": [], "unchanged": [], "differences": []}
            for record in records:
                path = "/v1/providers/" + record["code"]
                try:
                    existing = await client.request("GET", path)
                except CatalogError as exc:
                    if exc.status != 404:
                        raise
                    await client.request("PUT", path, body=record)
                    result["imported"].append(record["code"])
                else:
                    result["unchanged" if existing == record else "differences"].append(
                        record["code"]
                    )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await client.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    load_dotenv(override=False)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description="NordicIntel durable metadata harvesting")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("worker")
    for name in ("request", "request-all"):
        request = commands.add_parser(name)
        if name == "request":
            request.add_argument("--provider", required=True)
            request.add_argument("--language", choices=("sv", "en"), required=True)
        request.add_argument("--force", action="store_true")
        request.add_argument("--limit", type=int)
    for name in ("status", "cancel"):
        commands.add_parser(name).add_argument("job_id")
    commands.add_parser("import-providers").add_argument("file")
    try:
        asyncio.run(dispatch(parser.parse_args()))
    except (ValueError, CatalogError) as exc:
        parser.exit(1, f"{exc}\n")
