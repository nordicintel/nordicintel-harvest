"""HTTP-facing client for the Kolada v3 API.

Owns pagination, the 25-value-per-filter batching, and orchestrating the
year/municipality presence probe. Dimension-shape and derivation logic lives
in `parsing.py`; `KoladaAdapter` selects the appropriate client for its provider.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import urlencode, urljoin

from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.providers.adapters.kolada.parsing import (
    KpiGroup,
    KpiInfo,
    MunicipalityInfo,
    OuInfo,
    ResolvedDimensions,
    build_data_request_batches,
    build_dataset_dimensions,
    build_ou_dataset_dimensions,
    candidate_municipality_ids,
    extract_presence,
    max_probe_year,
    parse_kpi,
    parse_kpi_group,
    parse_municipality,
    parse_ou,
    probe_year_range,
)


async def get_paginated_values(request_manager: RequestManager, url: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    next_url: str | None = url
    while next_url:
        if next_url in seen:
            raise ValueError("Kolada pagination cycle")
        seen.add(next_url)
        payload = await request_manager.get_json(next_url)
        if not isinstance(payload, dict):
            raise ValueError("Malformed Kolada page")
        page_values = payload.get("values")
        if not isinstance(page_values, list) or any(
            not isinstance(item, dict) for item in page_values
        ):
            raise ValueError("Malformed Kolada page values")
        values.extend(page_values)
        link = payload.get("next_url")
        if link is not None and (not isinstance(link, str) or not link.strip()):
            raise ValueError("Malformed Kolada pagination link")
        next_url = urljoin(next_url, link) if link else None
    return values


class KoladaClient:
    """Fetches and caches Kolada catalog data, and resolves one KPI's
    dimensions. One instance is owned by a `KoladaAdapter` for the duration
    of one harvest. Catalog caches are lock-guarded because concurrent dataset
    resolutions can race to populate them."""

    def __init__(
        self,
        *,
        request_manager: RequestManager,
        base_api_url: str,
        logger: logging.Logger,
    ) -> None:
        self.request_manager = request_manager
        self.base_api_url = base_api_url.rstrip("/")
        self.logger = logger
        self._kpis: list[KpiInfo] | None = None
        self._kpis_by_id: dict[str, KpiInfo] | None = None
        self._municipalities: list[MunicipalityInfo] | None = None
        self._kpis_lock = asyncio.Lock()
        self._municipalities_lock = asyncio.Lock()

    async def _get_paginated(self, url: str) -> list[dict[str, Any]]:
        return await get_paginated_values(self.request_manager, url)

    async def list_kpis(self) -> list[KpiInfo]:
        """Return KPIs with sorted, unique group memberships; cache only a complete catalogue."""
        if self._kpis is not None:
            return self._kpis
        async with self._kpis_lock:
            if self._kpis is not None:
                return self._kpis
            raw = await self._get_paginated(f"{self.base_api_url}/kpi")
            kpis = [parse_kpi(item) for item in raw if str(item.get("id") or "").strip()]
            indexed: dict[str, dict[str, KpiGroup]] = {k.kpi_id: {} for k in kpis}
            raw = await self._get_paginated(f"{self.base_api_url}/kpi_groups")
            groups: dict[str, KpiGroup] = {}
            for item in raw:
                group = parse_kpi_group(item)
                previous = groups.get(group.group_id)
                if previous is not None and previous != group:
                    raise ValueError(f"Conflicting Kolada KPI group {group.group_id!r}")
                groups[group.group_id] = group
            for group in groups.values():
                for member_id in group.member_ids:
                    if member_id not in indexed:
                        self.logger.warning(
                            "Unknown Kolada KPI group member group=%s kpi=%s",
                            group.group_id,
                            member_id,
                        )
                        continue
                    indexed[member_id][group.group_id] = group
            enriched = [
                replace(
                    kpi,
                    groups=tuple(
                        sorted(
                            indexed[kpi.kpi_id].values(),
                            key=lambda g: (g.title, g.group_id),
                        )
                    ),
                )
                for kpi in kpis
            ]
            self._kpis_by_id = {kpi.kpi_id: kpi for kpi in enriched}
            self._kpis = enriched
            return enriched

    async def changed_kpi_ids(
        self, *, from_date: date, max_year: int, data_path: str = "data"
    ) -> set[str]:
        municipalities = await self.list_municipalities()
        ids = [item.municipality_id for item in municipalities]
        if not ids:
            raise ValueError("Kolada municipality catalog is empty")
        changed: set[str] = set()
        for municipality_batch, year_batch in build_data_request_batches(
            ids, list(range(2010, max_year + 1))
        ):
            url = (
                f"{self.base_api_url}/{data_path}/municipality/{','.join(municipality_batch)}"
                f"/year/{','.join(map(str, year_batch))}?{urlencode({'from_date': from_date.isoformat()})}"
            )
            for entry in await self._get_paginated(url):
                kpi = entry.get("kpi")
                if not isinstance(kpi, str) or not kpi.strip():
                    raise ValueError("Kolada change entry has no KPI identifier")
                changed.add(kpi)
        return changed

    async def get_kpi(self, kpi_id: str) -> KpiInfo | None:
        if self._kpis_by_id is None:
            await self.list_kpis()
        assert self._kpis_by_id is not None
        return self._kpis_by_id.get(kpi_id)

    async def list_municipalities(self) -> list[MunicipalityInfo]:
        if self._municipalities is not None:
            return self._municipalities
        async with self._municipalities_lock:
            if self._municipalities is not None:
                return self._municipalities
            raw = await self._get_paginated(f"{self.base_api_url}/municipality")
            municipalities = [
                parsed for item in raw if (parsed := parse_municipality(item)) is not None
            ]
            self._municipalities = municipalities
            return municipalities

    def _data_url(self, kpi_id: str, municipality_ids: list[str], years: list[int]) -> str:
        muni_segment = ",".join(municipality_ids)
        year_segment = ",".join(str(y) for y in years)
        return (
            f"{self.base_api_url}/data/kpi/{kpi_id}/municipality/{muni_segment}/year/{year_segment}"
        )

    async def fetch_year_and_municipality_presence(
        self, *, kpi_id: str, municipality_ids: list[str], years: list[int]
    ) -> tuple[set[int], set[str]]:
        if not municipality_ids or not years:
            return set(), set()

        years_found: set[int] = set()
        municipalities_found: set[str] = set()
        for muni_batch, year_batch in build_data_request_batches(municipality_ids, years):
            url = self._data_url(kpi_id, muni_batch, year_batch)
            entries = await self._get_paginated(url)
            batch_years, batch_munis = extract_presence(entries)
            years_found |= batch_years
            municipalities_found |= batch_munis
        return years_found, municipalities_found

    async def resolve_dataset_dimensions(
        self,
        *,
        kpi: KpiInfo,
        max_year: int | None = None,
    ) -> ResolvedDimensions:
        """Rebuild dimensions from all supported years of current observations."""
        municipalities = await self.list_municipalities()
        candidates = candidate_municipality_ids(kpi.municipality_type, municipalities)

        probe_years = probe_year_range(
            max_year=max_year if max_year is not None else max_probe_year()
        )

        new_years: set[int] = set()
        new_municipality_ids: set[str] = set()
        if probe_years:
            (
                new_years,
                new_municipality_ids,
            ) = await self.fetch_year_and_municipality_presence(
                kpi_id=kpi.kpi_id, municipality_ids=candidates, years=probe_years
            )

        all_years = new_years
        all_municipality_ids = new_municipality_ids

        titles = {m.municipality_id: m.title for m in municipalities}
        return build_dataset_dimensions(
            kpi=kpi,
            all_years=all_years,
            all_municipality_ids=all_municipality_ids,
            municipality_titles=titles,
        )


class KoladaOrganizationalUnitsClient:
    """Fetches and caches Kolada OU catalog data, and resolves one KPI's OU
    dimensions. One instance is owned by a `KoladaAdapter` for the duration
    of one organizational-unit harvest. `list_ous` is lock-guarded because
    concurrent dataset resolutions can race to populate it."""

    def __init__(
        self,
        *,
        request_manager: RequestManager,
        base_api_url: str,
        logger: logging.Logger,
        base_client: KoladaClient | None = None,
    ) -> None:
        self.request_manager = request_manager
        self.base_api_url = base_api_url.rstrip("/")
        self.logger = logger
        self._base = base_client or KoladaClient(
            request_manager=request_manager,
            base_api_url=base_api_url,
            logger=logger,
        )
        self._ous: list[OuInfo] | None = None
        self._ous_lock = asyncio.Lock()

    async def changed_kpi_ids(self, *, from_date: date, max_year: int) -> set[str]:
        return await self._base.changed_kpi_ids(
            from_date=from_date, max_year=max_year, data_path="oudata"
        )

    async def list_ou_kpis(self) -> list[KpiInfo]:
        kpis = await self._base.list_kpis()
        return [kpi for kpi in kpis if kpi.has_ou_data]

    async def get_kpi(self, kpi_id: str) -> KpiInfo | None:
        return await self._base.get_kpi(kpi_id)

    async def list_ous(self) -> list[OuInfo]:
        if self._ous is not None:
            return self._ous
        async with self._ous_lock:
            if self._ous is not None:
                return self._ous
            raw = await get_paginated_values(self.request_manager, f"{self.base_api_url}/ou")
            ous = [parsed for item in raw if (parsed := parse_ou(item)) is not None]
            self._ous = ous
            return ous

    async def fetch_year_and_ou_presence(
        self, *, kpi_id: str, years: list[int]
    ) -> tuple[set[int], set[str]]:
        if not years:
            return set(), set()

        years_found: set[int] = set()
        ous_found: set[str] = set()
        for year in years:
            url = f"{self.base_api_url}/oudata/kpi/{kpi_id}/year/{year}"
            entries = await get_paginated_values(self.request_manager, url)
            year_years, year_ous = extract_presence(entries, id_key="ou")
            years_found |= year_years
            ous_found |= year_ous
        return years_found, ous_found

    async def resolve_dataset_dimensions(
        self,
        *,
        kpi: KpiInfo,
        max_year: int | None = None,
    ) -> ResolvedDimensions:
        """Rebuild OU dimensions from all supported years of current observations."""
        ous = await self.list_ous()
        ou_titles = {ou.ou_id: ou.title for ou in ous}
        ou_municipality_ids = {ou.ou_id: ou.municipality_id for ou in ous}
        municipalities = await self._base.list_municipalities()
        municipality_titles = {m.municipality_id: m.title for m in municipalities}

        probe_years = probe_year_range(
            max_year=max_year if max_year is not None else max_probe_year()
        )

        new_years: set[int] = set()
        new_ou_ids: set[str] = set()
        if probe_years:
            new_years, new_ou_ids = await self.fetch_year_and_ou_presence(
                kpi_id=kpi.kpi_id, years=probe_years
            )

        all_years = new_years
        all_ou_ids = new_ou_ids

        return build_ou_dataset_dimensions(
            kpi=kpi,
            all_years=all_years,
            all_ou_ids=all_ou_ids,
            ou_titles=ou_titles,
            ou_municipality_ids=ou_municipality_ids,
            municipality_titles=municipality_titles,
        )
