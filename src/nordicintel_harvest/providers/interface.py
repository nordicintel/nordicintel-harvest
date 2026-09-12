from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.schemas.datasets import (
    DatasetContact,
    DatasetDimension,
    DatasetDocuments,
    DatasetIdentity,
    DatasetPath,
)
from nordicintel_harvest.schemas.dimensions import DatasetRole

if TYPE_CHECKING:
    from nordicintel_harvest.inputs import HarvestInput


@dataclass
class DatasetCandidate:
    """One provider dataset observed during catalog discovery."""

    provider_code: str
    dataset_code: str
    language: str
    updated: datetime | None = None
    label: str | None = None
    description: str | None = None
    source: str | None = None
    note: list[str] | None = None
    subject_code: str | None = None
    subject_label: str | None = None
    time_unit: str | None = None
    first_period: str | None = None
    last_period: str | None = None
    discontinued: bool | None = None
    metadata_url: str | None = None
    data_url: str | None = None
    web_url: str | None = None
    doc_url: str | None = None
    paths: list[DatasetPath] | None = None
    official_statistics: bool | None = None
    contact: list[DatasetContact] | None = None
    extension: dict[str, object] = field(default_factory=dict)
    id: list[str] | None = None
    role: DatasetRole | None = None
    dimension: dict[str, DatasetDimension] | None = None

    @property
    def identity(self) -> DatasetIdentity:
        return DatasetIdentity(
            provider_code=self.provider_code,
            dataset_code=self.dataset_code,
            language=self.language,
        )


@dataclass(frozen=True)
class DatasetHarvestState:
    source_updated_at: datetime | None
    last_fetched_at: datetime | None
    has_error: bool


@dataclass(frozen=True)
class HarvestContext:
    datasets: Mapping[str, DatasetHarvestState]
    started_at: datetime
    previous_complete_started_at: datetime | None = None
    force: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "datasets", MappingProxyType(dict(self.datasets)))


@dataclass(frozen=True)
class DatasetSelection:
    candidate: DatasetCandidate
    should_fetch: bool
    source_updated_at: datetime | None = None


def select_by_timestamp(candidate: DatasetCandidate, context: HarvestContext) -> DatasetSelection:
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    state = context.datasets.get(candidate.dataset_code)
    updated = utc(candidate.updated) if candidate.updated is not None else None
    should_fetch = (
        context.force
        or state is None
        or state.last_fetched_at is None
        or state.has_error
        or updated is None
        or state.source_updated_at is None
        or updated != utc(state.source_updated_at)
    )
    return DatasetSelection(candidate, should_fetch, updated)


class ProviderAdapter(ABC):
    """Provider-specific discovery and metadata-resolution seam."""

    def __init__(
        self,
        provider: HarvestInput,
        language: str,
        request_manager: RequestManager,
        logger: logging.Logger | None = None,
    ) -> None:
        if language != provider.language:
            raise ValueError("Adapter language differs from resolved input")
        self.provider = provider
        self.language = language
        self.provider_code = provider.provider_code
        self.base_api_url = provider.base_api_url
        self.base_web_url = provider.base_web_url
        self.logger = logger or logging.getLogger(f"{self.provider_code}_adapter")
        self.request_manager = request_manager

    async def discover_datasets(self, context: HarvestContext) -> AsyncIterator[DatasetSelection]:
        async for candidate in self._discover_candidates():
            yield select_by_timestamp(candidate, context)

    async def _discover_candidates(self) -> AsyncIterator[DatasetCandidate]:
        """Yield datasets observed in the provider catalog."""
        raise NotImplementedError("Adapter must implement candidate discovery or selection")
        yield  # pragma: no cover

    @abstractmethod
    async def resolve_dataset(
        self,
        candidate: DatasetCandidate,
    ) -> DatasetDocuments:
        """Resolve a candidate into validated DatasetDocuments."""

    async def close(self) -> None:
        """Close adapter-owned resources."""
        return
