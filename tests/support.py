"""Fixtures and a programmable adapter shared by the engine and integration tests.

The stub adapter is scripted per Table and per language rather than per call, because
what the engine has to get right is exactly that: Swedish failing while English succeeds,
one language unchanged while another is not, one Table failing without stopping the rest.
It also records what it was asked, so a test can assert that a language was never fetched
rather than only that nothing was stored for it.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from nordicintel_core.database import create_owner_engine
from nordicintel_core.jsonstat import JsonStatDataset
from nordicintel_core.models import (
    DiscoveryEntry,
    DiscoveryResult,
    DiscoveryScope,
    ExplicitSelection,
    LanguageState,
    MetadataFetchResult,
    ProviderDefinition,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

PROVIDER_ID = "scb"

# Snapshotted at import: tests that exercise the operator commands set
# NORDICINTEL_DATABASE_URL themselves, and the guard below is about the database this
# machine's processes were pointed at before any test ran.
_CONFIGURED_DATABASE_URL = os.environ.get("NORDICINTEL_DATABASE_URL")


def database_url() -> str:
    """The database these tests are allowed to destroy.

    Every test starts by truncating the schema, so this must be a throwaway database. It
    is deliberately refused when it names the same database the processes are configured
    against: pointing the suite at a working catalogue empties it, and the failure is
    silent because an empty catalogue is a perfectly valid state.
    """
    value = os.environ.get("NORDICINTEL_TEST_DATABASE_URL")
    if not value:
        pytest.skip("NORDICINTEL_TEST_DATABASE_URL is not configured")
    if value == _CONFIGURED_DATABASE_URL:
        pytest.fail(
            "NORDICINTEL_TEST_DATABASE_URL must not be the database the worker uses: "
            "these tests truncate it"
        )
    return value


@contextmanager
def owner() -> Iterator[Session]:
    """One session on one dedicated backend, exactly as a worker holds it."""
    engine = create_owner_engine(database_url())
    try:
        with engine.connect() as connection, Session(bind=connection) as session:
            yield session
    finally:
        engine.dispose()


def truncate() -> None:
    with owner() as session, session.begin():
        session.execute(
            text(
                "TRUNCATE harvest_item, harvest_job, harvest_schedule, table_metadata, "
                "table_language_state, table_registry, provider RESTART IDENTITY CASCADE"
            )
        )


def provider(
    provider_id: str = PROVIDER_ID,
    *,
    adapter_type: str = "stub",
    secret_refs: Mapping[str, str] | None = None,
) -> ProviderDefinition:
    return ProviderDefinition(
        id=provider_id,
        label=f"Provider {provider_id}",
        adapter_type=adapter_type,
        config={"base_url": "https://example.test"},
        secret_refs=dict(secret_refs or {}),
    )


def dataset(label: str) -> dict[str, Any]:
    return {
        "version": "2.0",
        "class": "dataset",
        "label": label,
        "source": "SCB",
        "updated": "2025-03-04",
        "id": ["region"],
        "size": [2],
        "role": {"geo": ["region"]},
        "dimension": {
            "region": {
                "label": "Region",
                "category": {
                    "index": ["SE", "18"],
                    "label": {"SE": "Sverige", "18": "Örebro"},
                },
            }
        },
        "value": [],
    }


def fetch_result(
    native_table_id: str,
    language: str,
    *,
    provider_id: str = PROVIDER_ID,
    label: str = "Befolkning",
    marker: Mapping[str, Any] | None = None,
) -> MetadataFetchResult:
    """One complete, valid language representation of one Table."""
    return MetadataFetchResult.model_validate(
        {
            "provider_id": provider_id,
            "native_table_id": native_table_id,
            "metadata": {
                "language": language,
                "catalog": {
                    "label": label,
                    "description": "Örebro län",
                    "source": "SCB",
                    "updated": "2025-03-04",
                    "first_period": "2024",
                    "last_period": "2025",
                    "variable_names": ["Region"],
                    "links": [
                        {
                            "rel": "self",
                            "hreflang": language,
                            "href": f"https://example.test/tables/{native_table_id}",
                        }
                    ],
                    "discontinued": False,
                },
                "dataset": dataset(f"{label} ({language})"),
            },
            "comparison_marker": dict(marker) if marker is not None else {"updated": label},
        }
    )


Behaviour = MetadataFetchResult | BaseException | Callable[[], Any]


class StubAdapter:
    """A scripted adapter that records everything the engine asked it for."""

    def __init__(
        self,
        *,
        entries: Sequence[DiscoveryEntry],
        languages: Sequence[str] = ("sv", "en"),
        authoritative: bool = True,
        provider_id: str = PROVIDER_ID,
        refresh: Mapping[str, Sequence[str]] | None = None,
        behaviour: Mapping[tuple[str, str], Behaviour] | None = None,
        discovery_error: BaseException | None = None,
        scope_override: DiscoveryScope | None = None,
    ) -> None:
        self.entries = list(entries)
        self.languages = list(languages)
        self.authoritative = authoritative
        self.provider_id = provider_id
        # None means "the adapter has no opinion", which is the normal case: the engine's
        # own floor still forces languages that have never been harvested successfully.
        self.refresh = (
            None
            if refresh is None
            else {key: list(value) for key, value in refresh.items()}
        )
        self.behaviour = dict(behaviour or {})
        self.discovery_error = discovery_error
        self.scope_override = scope_override
        self.discovered: list[DiscoveryScope] = []
        self.fetched: list[tuple[str, str]] = []
        self.compared: list[tuple[str, tuple[str, ...]]] = []

    async def resolve_languages(self, requested: Sequence[str] | None) -> list[str]:
        if requested is None:
            return list(self.languages)
        return [language for language in self.languages if language in set(requested)]

    async def discover(self, scope: DiscoveryScope) -> DiscoveryResult:
        self.discovered.append(scope)
        if self.discovery_error is not None:
            raise self.discovery_error
        return DiscoveryResult(
            scope=self.scope_override or scope,
            entries=list(self.entries),
            authoritative=self.authoritative,
        )

    async def languages_to_refresh(
        self,
        entry: DiscoveryEntry,
        stored: Mapping[str, LanguageState],
        requested: Sequence[str],
        *,
        force: bool,
    ) -> list[str]:
        self.compared.append((entry.native_table_id, tuple(stored)))
        if force:
            return list(requested)
        if self.refresh is None:
            return list(requested)
        return list(self.refresh.get(entry.native_table_id, []))

    async def fetch_metadata(
        self, entry: DiscoveryEntry, languages: Sequence[str]
    ) -> list[MetadataFetchResult]:
        assert len(languages) == 1, "the engine must request one language per call"
        language = languages[0]
        self.fetched.append((entry.native_table_id, language))
        scripted = self.behaviour.get((entry.native_table_id, language))
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted):
            produced = scripted()
            if inspect.isawaitable(produced):
                produced = await produced
            return [produced]
        if scripted is not None:
            return [scripted]
        return [fetch_result(entry.native_table_id, language, provider_id=self.provider_id)]

    async def fetch_data(
        self, native_table_id: str, selection: ExplicitSelection
    ) -> JsonStatDataset:  # pragma: no cover - harvesting never calls this
        raise NotImplementedError


class StubFactory:
    """An ``AdapterFactory`` that hands out one prepared adapter."""

    def __init__(self, adapter: StubAdapter) -> None:
        self.adapter = adapter
        self.created: list[tuple[str, dict[str, str]]] = []

    async def create(
        self, provider: ProviderDefinition, secrets: Mapping[str, str], http: Any
    ) -> StubAdapter:
        self.created.append((provider.id, dict(secrets)))
        return self.adapter


def register(
    session: Session, provider_id: str = PROVIDER_ID, **kwargs: Any
) -> ProviderDefinition:
    """Configure one Provider through the same repository the API will use."""
    from nordicintel_core.database import ProviderRepository

    return ProviderRepository(session).upsert(provider(provider_id, **kwargs))


def start_job(
    session: Session,
    request: Any = None,
    *,
    provider_id: str = PROVIDER_ID,
) -> Any:
    """Enqueue and claim one job on this session, as a worker would."""
    from nordicintel_core.database import HarvestRepository
    from nordicintel_core.models import HarvestRequest

    queue = HarvestRepository(session)
    queued = queue.enqueue(provider_id, request or HarvestRequest())
    claimed = queue.claim()
    assert claimed is not None and claimed.id == queued.id
    return claimed


@dataclass(slots=True)
class Run:
    """One finished attempt, so a test can name the job it wants to inspect."""

    job: Any
    summary: Any


async def harvest(
    session: Session,
    adapter: StubAdapter,
    *,
    request: Any = None,
    control_factory: Callable[[Any, int], Any] | None = None,
    definition: ProviderDefinition | None = None,
    provider_id: str = PROVIDER_ID,
) -> Run:
    """Run one job the way a worker does: claim, traverse, finalize, release.

    Finalization matters even in an engine test. A job left running keeps its Provider
    busy, so the next claim in the same test would find nothing to do.
    """
    from nordicintel_core.database import HarvestRepository, MetadataRepository
    from nordicintel_core.models import JobStatus

    from nordicintel_harvest.control import JobControl
    from nordicintel_harvest.engine import HarvestEngine
    from nordicintel_harvest.errors import HarvestStopped, JobFailed

    job = start_job(session, request, provider_id=provider_id)
    queue = HarvestRepository(session)
    control = (
        control_factory(queue, job.id)
        if control_factory is not None
        else JobControl(queue, job.id, interval_seconds=60.0)
    )
    engine = HarvestEngine(
        queue=queue,
        metadata=MetadataRepository(session),
        adapter=adapter,
        job=job,
        provider=definition or provider(job.provider_id),
        control=control,
    )
    try:
        summary = await engine.run()
    except HarvestStopped:
        queue.finish_job(job.id, JobStatus.CANCELLED)
        queue.release_provider(job.provider_id)
        raise
    except JobFailed as exc:
        queue.finish_job(job.id, JobStatus.FAILED, error=exc.diagnostic)
        queue.release_provider(job.provider_id)
        raise
    queue.finish_job(job.id, JobStatus.COMPLETED)
    queue.release_provider(job.provider_id)
    return Run(job=job, summary=summary)
