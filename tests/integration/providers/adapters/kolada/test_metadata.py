from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from legacy_inputs import provider_catalog

from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.interface import (
    DatasetCandidate,
    DatasetHarvestState,
    HarvestContext,
)

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


class Catalogue:
    def __init__(self):
        self.calls = []
        self.fail_group_page = False

    async def get_json(self, url):
        self.calls.append(url)
        await asyncio.sleep(0)
        if url.endswith("/kpi"):
            values = [
                {
                    "id": "A",
                    "title": "Test (- 2024)",
                    "description": "Text.\\nKälla: SCB. Metod.",
                    "operating_area": "Förskola",
                    "perspective": "Resurser",
                    "auspice": "T",
                    "municipality_type": "K",
                    "has_ou_data": True,
                    "publication_date": "2030-01-01",
                }
            ]
        elif url.endswith("/kpi_groups"):
            return {
                "values": [
                    {
                        "id": "G2",
                        "title": "Öppna jämförelser - Kommun",
                        "members": [
                            {"member_id": "A"},
                            {"member_id": "A"},
                            {"member_id": "UNKNOWN"},
                        ],
                    }
                ],
                "next_url": "?page=2",
            }
        elif "kpi_groups?page=2" in url:
            if self.fail_group_page:
                raise TimeoutError("group page failed")
            values = [{"id": "G1", "title": "Agenda 2030", "members": [{"member_id": "A"}]}]
        elif url.endswith("/municipality"):
            values = [{"id": "0180", "title": "Stockholm", "type": "K"}]
        elif url.endswith("/ou"):
            values = [{"id": "OU", "title": "Enhet", "municipality": "0180"}]
        elif "from_date=" in url:
            values = []
        else:
            years = url.rsplit("/", 1)[-1].split(",")
            values = (
                [
                    {
                        "kpi": "A",
                        "period": 2024,
                        "municipality": "0180",
                        "ou": "OU",
                        "values": [{"value": 1}],
                    }
                ]
                if "2024" in years
                else []
            )
        return {"values": values, "next_url": None}


def adapter_for(_provider, requests):
    return KoladaAdapter(provider_catalog.require_scope("kolada", "sv"), "sv", requests)


@pytest.mark.parametrize("provider,geo", [("kolada", "municipality"), ("kolada_ou", "ou")])
async def test_discovery_resolution_and_stable_clock(provider, geo, caplog):
    requests = Catalogue()
    adapter = adapter_for(provider, requests)
    selected = [s async for s in adapter.discover_datasets(HarvestContext({}, NOW))]
    candidate = next(
        s.candidate
        for s in selected
        if s.candidate.dataset_code == ("A_OU" if geo == "ou" else "A")
    )
    assert candidate.updated is None
    assert selected[0].source_updated_at is None
    assert "UNKNOWN" in caplog.text
    metadata = await adapter.resolve_dataset(candidate)
    assert metadata.metadata.source == candidate.source == "SCB"
    assert metadata.metadata.paths == candidate.paths
    assert metadata.metadata.extension == candidate.extension
    assert metadata.dataset.discontinued and metadata.dataset.label == "Test (- 2024)"
    assert metadata.dataset.first_period == metadata.dataset.last_period == "2024"
    assert metadata.metadata.id == [geo, "year"]
    assert metadata.dataset.updated is None
    assert metadata.metadata.extension["kolada"]["publication_date"] == "2030-01-01"
    assert [g["id"] for g in candidate.extension["kolada"]["kpi_groups"]] == [
        "G1",
        "G2",
    ]
    assert len(metadata.metadata.paths) == 3
    # Same provider responses on another day yield the same stored document.
    _ = [s async for s in adapter.discover_datasets(HarvestContext({}, NOW + timedelta(days=2)))]
    again = await adapter.resolve_dataset(candidate)
    assert again.documents()["metadata"] == metadata.documents()["metadata"]
    assert requests.calls.count("https://api.kolada.se/v3/kpi_groups") == 1
    assert requests.calls.count("https://api.kolada.se/v3/kpi_groups?page=2") == 1
    if geo == "ou":
        assert (
            metadata.metadata.dimension[geo].extension["category"]["OU"]["municipality_id"]
            == "0180"
        )


@pytest.mark.parametrize("provider", ["kolada", "kolada_ou"])
@pytest.mark.parametrize(
    "age,expected",
    [(timedelta(days=30), False), (timedelta(days=30, microseconds=1), True)],
)
async def test_stale_fetch_with_daily_successful_harvests(provider, age, expected):
    adapter = adapter_for(provider, Catalogue())
    state = DatasetHarvestState(None, NOW - age, False)
    items = [
        s
        async for s in adapter.discover_datasets(
            HarvestContext({"A": state, "A_OU": state}, NOW, NOW - timedelta(days=1))
        )
    ]
    assert all(s.should_fetch is expected for s in items)


@pytest.mark.parametrize("provider", ["kolada", "kolada_ou"])
async def test_group_cache_concurrency_and_failure_retry(provider):
    requests = Catalogue()
    adapter = adapter_for(provider, requests)
    requests.fail_group_page = True
    with pytest.raises(TimeoutError):
        await adapter.client.get_kpi("A")
    requests.fail_group_page = False
    listing = (
        adapter.ou_client.list_ou_kpis if provider == "kolada_ou" else adapter.client.list_kpis
    )
    # Listing and ID lookup cross the same catalogue seam concurrently.
    results = await asyncio.gather(listing(), *(adapter.client.get_kpi("A") for _ in range(5)))
    kpi = results[0][0]
    assert all(result is kpi for result in results[1:])
    assert [g.group_id for g in kpi.groups] == ["G1", "G2"]
    assert requests.calls.count("https://api.kolada.se/v3/kpi_groups") == 2
    assert requests.calls.count("https://api.kolada.se/v3/kpi") == 2
    assert await adapter.client.get_kpi("UNKNOWN") is None


@pytest.mark.parametrize("provider", ["kolada", "kolada_ou"])
async def test_standalone_resolution_ignores_synthetic_candidate_timestamp(provider):
    adapter = adapter_for(provider, Catalogue())
    metadata = await adapter.resolve_dataset(
        DatasetCandidate("kolada", "A_OU" if provider == "kolada_ou" else "A", "sv", updated=NOW)
    )
    assert metadata.dataset.updated is None
    assert metadata.metadata.source == "SCB"
    assert len(metadata.metadata.paths) == 3


@pytest.mark.parametrize("provider", ["kolada", "kolada_ou"])
async def test_catalogue_joins_only_matching_members_and_deduplicates_groups(provider):
    class MultipleKpis(Catalogue):
        async def get_json(self, url):
            page = await super().get_json(url)
            if url.endswith("/kpi"):
                page["values"].append({"id": "B", "title": "Ungrouped", "has_ou_data": True})
            if "kpi_groups?page=2" in url:
                page["values"].append(page["values"][0])
            return page

    adapter = adapter_for(provider, MultipleKpis())
    listing = (
        adapter.ou_client.list_ou_kpis if provider == "kolada_ou" else adapter.client.list_kpis
    )
    kpis = await listing()
    assert [k.kpi_id for k in kpis] == ["A", "B"]
    assert [g.group_id for g in kpis[0].groups] == ["G1", "G2"]
    assert kpis[1].groups == ()
    assert await adapter.client.get_kpi("B") is kpis[1]
