"""A small operator command line for the period before the API exists.

Everything here goes through the same core repositories and the same admission rules the
API will use, so a Provider registered with this command is registered exactly once and in
one place. It is deliberately not a second control plane: there is no state of its own, no
schema, and no operation that a core repository does not already offer.

Short administrative transactions use the pooled API engine. Only a worker or the
scheduler needs the unpooled owner engine, because only they hold locks across calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from nordicintel_core.database import (
    HarvestRepository,
    MetadataRepository,
    ProviderRepository,
    ScheduleRepository,
    create_api_engine,
    session_scope,
)
from nordicintel_core.errors import AdmissionError, ConfigurationError, NordicIntelError
from nordicintel_core.models import (
    HarvestRequest,
    ItemStatus,
    JobStatus,
    ProviderDefinition,
)

from .registry import AdapterRegistry
from .settings import load_settings


def _languages(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _request(arguments: argparse.Namespace) -> HarvestRequest:
    return HarvestRequest(
        table_id=getattr(arguments, "table_id", None),
        force=getattr(arguments, "force", False),
        languages=_languages(getattr(arguments, "languages", None)),
    )


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _dump(models: Sequence[Any]) -> list[dict[str, Any]]:
    return [model.model_dump(mode="json") for model in models]


def _provider_upsert(session: Any, arguments: argparse.Namespace) -> None:
    with open(arguments.file, encoding="utf-8") as handle:
        definition = ProviderDefinition.model_validate(json.load(handle))
    _emit(ProviderRepository(session).upsert(definition).model_dump(mode="json"))


def _schedule_set(session: Any, arguments: argparse.Namespace) -> None:
    # An interval schedule has no natural first run, and core requires the caller to say
    # which one it means rather than inventing one behind an update.
    if arguments.at is not None:
        next_run_at = datetime.fromisoformat(arguments.at)
        if next_run_at.tzinfo is None:
            raise ConfigurationError("--at must include a timezone offset")
    elif arguments.start_now:
        next_run_at = datetime.now(UTC)
    else:
        existing = ScheduleRepository(session).get(arguments.provider_id)
        next_run_at = (
            existing.next_run_at
            if existing is not None
            else datetime.now(UTC) + timedelta(seconds=arguments.every_seconds)
        )
    schedule = ScheduleRepository(session).upsert(
        arguments.provider_id,
        enabled=not arguments.disabled,
        every_seconds=arguments.every_seconds,
        next_run_at=next_run_at,
        request=_request(arguments),
    )
    _emit(schedule.model_dump(mode="json"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nordicintel-bootstrap",
        description="Configure providers and schedules, and drive the harvest queue.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    provider = commands.add_parser("provider", help="Provider configuration").add_subparsers(
        dest="action", required=True
    )
    upsert = provider.add_parser("upsert", help="Create or replace a provider from a JSON file")
    upsert.add_argument("file")
    provider.add_parser("list", help="List configured providers")
    for action in ("enable", "disable"):
        enabling = provider.add_parser(action, help=f"{action.capitalize()} a provider")
        enabling.add_argument("provider_id")
        enabling.add_argument(
            "--cascade",
            action="store_true",
            help="Also cancel queued jobs and ask a running job to stop (disable only)",
        )

    schedule = commands.add_parser("schedule", help="Interval scheduling").add_subparsers(
        dest="action", required=True
    )
    setter = schedule.add_parser("set", help="Create or replace a provider's schedule")
    setter.add_argument("provider_id")
    setter.add_argument("--every-seconds", type=int, required=True)
    setter.add_argument("--languages", help="Comma-separated codes, or omit for adapter defaults")
    setter.add_argument("--force", action="store_true")
    setter.add_argument("--disabled", action="store_true")
    timing = setter.add_mutually_exclusive_group()
    timing.add_argument("--start-now", action="store_true", help="Make the schedule due now")
    timing.add_argument("--at", help="First run as an ISO timestamp with an offset")
    schedule.add_parser("list", help="List schedules by next run")

    harvest = commands.add_parser("harvest", help="Manual harvest requests").add_subparsers(
        dest="action", required=True
    )
    enqueue = harvest.add_parser("enqueue", help="Queue a manual harvest")
    enqueue.add_argument("provider_id")
    enqueue.add_argument("--table-id", help="Canonical table identifier for a single-table run")
    enqueue.add_argument("--languages")
    enqueue.add_argument("--force", action="store_true")
    enqueue.add_argument("--key", help="Idempotency key reused across retries of one request")

    jobs = commands.add_parser("jobs", help="Queue inspection").add_subparsers(
        dest="action", required=True
    )
    listing = jobs.add_parser("list", help="List jobs, newest first")
    listing.add_argument("--provider-id")
    listing.add_argument("--status", choices=[status.value for status in JobStatus])
    listing.add_argument("--limit", type=int, default=20)
    show = jobs.add_parser("show", help="Show one job")
    show.add_argument("job_id", type=int)
    items = jobs.add_parser("items", help="List a job's items")
    items.add_argument("job_id", type=int)
    items.add_argument("--status", choices=[status.value for status in ItemStatus])
    items.add_argument("--limit", type=int, default=50)
    cancel = jobs.add_parser("cancel", help="Cancel a queued job or ask a running one to stop")
    cancel.add_argument("job_id", type=int)
    jobs.add_parser("counts", help="Summarize queued and running work by provider")

    tables = commands.add_parser("tables", help="Catalogue inspection").add_subparsers(
        dest="action", required=True
    )
    search = tables.add_parser("search", help="Full-text search the harvested catalogue")
    search.add_argument("query")
    search.add_argument("--language")
    search.add_argument("--limit", type=int, default=20)
    show_table = tables.add_parser("show", help="Show one table's identity and controls")
    show_table.add_argument("table_id")
    show_table.add_argument("--language", help="Also print that language's catalog fields")

    commands.add_parser("adapters", help="List the adapter types this process can run")
    return parser


def _run(arguments: argparse.Namespace, database_url: str) -> None:
    if arguments.command == "adapters":
        settings = load_settings(os.environ)
        _emit(sorted(AdapterRegistry.from_entry_points(settings.adapters)))
        return
    engine = create_api_engine(database_url)
    try:
        with session_scope(engine) as session:
            _dispatch(session, arguments)
    finally:
        engine.dispose()


def _dispatch(session: Any, arguments: argparse.Namespace) -> None:  # a flat menu
    command, action = arguments.command, arguments.action
    if command == "provider":
        providers = ProviderRepository(session)
        if action == "upsert":
            _provider_upsert(session, arguments)
        elif action == "list":
            _emit(_dump(providers.list(limit=200)))
        else:
            enabled = action == "enable"
            providers.set_enabled(arguments.provider_id, enabled)
            result: dict[str, Any] = {"provider_id": arguments.provider_id, "enabled": enabled}
            if not enabled and arguments.cascade:
                cascaded = HarvestRepository(session).cancel_provider_jobs(arguments.provider_id)
                result["cancelled"] = _dump(cascaded)
            _emit(result)
    elif command == "schedule":
        if action == "set":
            _schedule_set(session, arguments)
        else:
            _emit(_dump(ScheduleRepository(session).list_schedules(limit=200)))
    elif command == "harvest":
        job = HarvestRepository(session).enqueue(
            arguments.provider_id, _request(arguments), request_key=arguments.key
        )
        _emit(job.model_dump(mode="json"))
    elif command == "jobs":
        queue = HarvestRepository(session)
        if action == "list":
            status = None if arguments.status is None else JobStatus(arguments.status)
            _emit(
                _dump(
                    queue.list_jobs(
                        provider_id=arguments.provider_id, status=status, limit=arguments.limit
                    )
                )
            )
        elif action == "show":
            found = queue.get_job(arguments.job_id)
            if found is None:
                raise AdmissionError(404, f"Job {arguments.job_id} does not exist")
            _emit(found.model_dump(mode="json"))
        elif action == "items":
            status_filter = None if arguments.status is None else ItemStatus(arguments.status)
            _emit(
                _dump(
                    queue.list_items(
                        arguments.job_id, status=status_filter, limit=arguments.limit
                    )
                )
            )
        elif action == "cancel":
            _emit(queue.cancel(arguments.job_id).model_dump(mode="json"))
        else:
            _emit(_dump(queue.queue_counts()))
    elif command == "tables":
        metadata = MetadataRepository(session)
        if action == "search":
            _emit(
                _dump(
                    metadata.search(
                        arguments.query, language=arguments.language, limit=arguments.limit
                    )
                )
            )
        else:
            table = metadata.get_table(arguments.table_id)
            if table is None:
                raise AdmissionError(404, f"Table {arguments.table_id!r} does not exist")
            payload: dict[str, Any] = {"table": table.model_dump(mode="json")}
            payload["languages"] = {
                language: state.model_dump(mode="json")
                for language, state in metadata.load_language_state(arguments.table_id).items()
            }
            if arguments.language is not None:
                language_metadata = metadata.get_language(arguments.table_id, arguments.language)
                payload["catalog"] = (
                    None
                    if language_metadata is None
                    else language_metadata.catalog.model_dump(mode="json")
                )
            _emit(payload)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point for operator commands."""
    arguments = _build_parser().parse_args(argv)
    database_url = os.environ.get("NORDICINTEL_DATABASE_URL", "")
    if not database_url.strip():
        print("NORDICINTEL_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        _run(arguments, database_url)
    except NordicIntelError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
