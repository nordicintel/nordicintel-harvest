"""Kolada adapter for municipality and organizational-unit datasets."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from nordicintel_harvest.providers.adapters.kolada.client import (
    KoladaClient,
    KoladaOrganizationalUnitsClient,
)
from nordicintel_harvest.providers.adapters.kolada.parsing import (
    KpiInfo,
    kpi_metadata,
)
from nordicintel_harvest.providers.interface import (
    DatasetCandidate,
    DatasetSelection,
    HarvestContext,
    ProviderAdapter,
)
from nordicintel_harvest.schemas.datasets import (
    DatasetDocuments,
    DatasetInfo,
    DatasetMetadata,
    DatasetSubject,
)

if TYPE_CHECKING:
    from nordicintel_harvest.core.request_manager import RequestManager
    from nordicintel_harvest.inputs import HarvestInput


WEB_URL = "https://kolada.se/verktyg/fri-sokning/"
OU_SUFFIX = "_OU"


class KoladaAdapter(ProviderAdapter):
    """Harvest municipality and organizational-unit datasets under provider kolada."""

    def __init__(
        self,
        provider: HarvestInput,
        language: str,
        request_manager: RequestManager,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(provider, language, request_manager, logger)
        if self.provider_code != "kolada" or language != "sv":
            raise ValueError(f"Kolada adapter does not support provider {self.provider_code!r}")
        if not self.base_api_url:
            raise ValueError(f"Kolada provider '{self.provider_code}' is missing base_api_url")

        self.client = KoladaClient(
            request_manager=self.request_manager,
            base_api_url=self.base_api_url,
            logger=self.logger,
        )
        self.ou_client = KoladaOrganizationalUnitsClient(
            request_manager=self.request_manager,
            base_api_url=self.base_api_url,
            logger=self.logger,
            base_client=self.client,
        )

    async def discover_datasets(self, context: HarvestContext) -> AsyncIterator[DatasetSelection]:
        self._harvest_year = context.started_at.astimezone(UTC).year
        cutoff = context.started_at.astimezone(UTC).date() - timedelta(days=30)
        cutoff_at = datetime.combine(cutoff, time.min, tzinfo=UTC)
        full = (
            context.force
            or context.previous_complete_started_at is None
            or context.previous_complete_started_at < cutoff_at
        )
        stale_before = context.started_at.astimezone(UTC) - timedelta(days=30)
        kpis = await self.client.list_kpis()
        changed = (
            set()
            if full
            else await self.client.changed_kpi_ids(
                from_date=cutoff, max_year=self._harvest_year + 3
            )
        )
        changed_ou = (
            set()
            if full
            else await self.ou_client.changed_kpi_ids(
                from_date=cutoff, max_year=self._harvest_year + 3
            )
        )
        for kpi in kpis:
            for data_path, changes in (("data", changed), ("oudata", changed_ou)):
                if data_path == "oudata" and not kpi.has_ou_data:
                    continue
                candidate = self._candidate(kpi, data_path=data_path)
                state = context.datasets.get(candidate.dataset_code)
                fetch = (
                    full
                    or state is None
                    or state.last_fetched_at is None
                    or state.last_fetched_at < stale_before
                    or state.has_error
                    or kpi.kpi_id in changes
                )
                yield DatasetSelection(candidate, fetch)

    def _candidate(self, kpi: KpiInfo, *, data_path: str) -> DatasetCandidate:
        return DatasetCandidate(
            provider_code=self.provider_code,
            dataset_code=kpi.kpi_id + (OU_SUFFIX if data_path == "oudata" else ""),
            language=self.language,
            **kpi_metadata(
                kpi,
                year=getattr(self, "_harvest_year", datetime.now(UTC).year),
            ),
            time_unit="Annual",
            metadata_url=f"{self.base_api_url}/kpi/{kpi.kpi_id}",
            data_url=f"{self.base_api_url}/{data_path}/kpi/{kpi.kpi_id}",
            web_url=WEB_URL,
        )

    async def resolve_dataset(
        self,
        discovered: DatasetCandidate,
    ) -> DatasetDocuments:
        is_ou = discovered.dataset_code.endswith(OU_SUFFIX)
        kpi_id = (
            discovered.dataset_code.removesuffix(OU_SUFFIX) if is_ou else discovered.dataset_code
        )
        client = self.ou_client if is_ou else self.client
        kpi = await client.get_kpi(kpi_id)
        if kpi is None:
            raise ValueError(f"Kolada KPI {discovered.dataset_code!r} not found")

        if is_ou and not kpi.has_ou_data:
            raise ValueError(
                f"Kolada OU KPI {discovered.dataset_code!r} not found or has no OU data"
            )
        result = await client.resolve_dataset_dimensions(
            kpi=kpi,
            max_year=getattr(self, "_harvest_year", datetime.now(UTC).year) + 3,
        )

        fields = kpi_metadata(kpi, year=getattr(self, "_harvest_year", datetime.now(UTC).year))
        identity = discovered.identity
        subject = {
            k: fields["subject_" + k] for k in ("code", "label") if fields.get("subject_" + k)
        }
        return DatasetDocuments(
            dataset=DatasetInfo(
                identity=identity,
                label=fields["label"],
                description=fields["description"],
                discontinued=fields["discontinued"],
                time_unit="Annual",
                first_period=result.first_period,
                last_period=result.last_period,
                source_url=WEB_URL,
            ),
            metadata=DatasetMetadata(
                identity=identity,
                source=fields["source"],
                subject=DatasetSubject(**subject) if subject else None,
                paths=fields["paths"],
                extension=fields["extension"],
                metadata_url=f"{self.base_api_url}/kpi/{kpi.kpi_id}",
                retrieval={"type": "kolada", "config": {}},
                id=result.id,
                role=result.role,
                dimension=result.dimension,
            ),
        )
