from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from legacy_inputs import provider_catalog

from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.adapters.kolada.client import get_paginated_values
from nordicintel_harvest.providers.adapters.kolada.parsing import extract_presence
from nordicintel_harvest.providers.interface import DatasetHarvestState, HarvestContext

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 11, tzinfo=UTC)


class Requests:
    def __init__(self, *, bad=None):
        self.calls = []
        self.bad = bad

    async def get_json(self, url):
        self.calls.append(url)
        if url.endswith("/kpi_groups"):
            return {"values": [], "next_url": None}
        if url.endswith("/kpi"):
            return {
                "values": [
                    {
                        "id": code,
                        "title": code,
                        "municipality_type": "A",
                        "has_ou_data": code != "MUNICIPAL_ONLY",
                    }
                    for code in (
                        "CHANGED",
                        "UNCHANGED",
                        "NEW",
                        "ERROR",
                        "UNFETCHED",
                        "MUNICIPAL_ONLY",
                    )
                ],
                "next_url": None,
            }
        if url.endswith("/municipality"):
            return {
                "values": [{"id": f"{i:04d}", "title": str(i), "type": "K"} for i in range(312)],
                "next_url": None,
            }
        if self.bad is not None:
            return self.bad
        return {
            "values": [{"kpi": "CHANGED", "values": [{"value": None, "isdeleted": True}]}],
            "next_url": None,
        }


def adapter_for(code, requests):
    return KoladaAdapter(provider_catalog.require_scope(code, "sv"), "sv", requests)


def states():
    result = {
        code: DatasetHarvestState(NOW, NOW, False)
        for code in ("CHANGED", "UNCHANGED", "ERROR", "UNFETCHED", "MUNICIPAL_ONLY")
    }
    result["ERROR"] = DatasetHarvestState(None, NOW, True)
    result["UNFETCHED"] = DatasetHarvestState(None, None, False)
    return {
        **result,
        **{code + "_OU": state for code, state in result.items() if code != "MUNICIPAL_ONLY"},
    }


async def test_merged_kinds_have_independent_state_and_change_feeds():
    class OnlyMunicipalityChanges(Requests):
        async def get_json(self, url):
            if "/oudata/" in url and "from_date=" in url:
                return {"values": [], "next_url": None}
            return await super().get_json(url)

    adapter = adapter_for("kolada", OnlyMunicipalityChanges())
    selected = {
        s.candidate.dataset_code: s
        async for s in adapter.discover_datasets(
            HarvestContext(states(), NOW, NOW - timedelta(days=1))
        )
    }
    assert selected["CHANGED"].should_fetch
    assert not selected["CHANGED_OU"].should_fetch
    assert "MUNICIPAL_ONLY_OU" not in selected
    assert {s.candidate.provider_code for s in selected.values()} == {"kolada"}


@pytest.mark.parametrize("provider, endpoint", [("kolada", "data"), ("kolada", "oudata")])
async def test_incremental_selection_batches_and_keeps_skips(provider, endpoint):
    requests = Requests()
    adapter = adapter_for(provider, requests)
    context = HarvestContext(states(), NOW, NOW - timedelta(days=1))
    selected = [item async for item in adapter.discover_datasets(context)]
    assert {s.candidate.dataset_code.removesuffix("_OU") for s in selected if s.should_fetch} == {
        "CHANGED",
        "NEW",
        "ERROR",
        "UNFETCHED",
    }
    assert all(s.source_updated_at is None for s in selected)
    assert ("MUNICIPAL_ONLY" in {s.candidate.dataset_code for s in selected}) == (
        provider == "kolada"
    )
    queries = [url for url in requests.calls if "from_date=" in url and f"/{endpoint}/" in url]
    assert len(queries) == 13
    municipalities = set()
    for url in queries:
        parts = urlsplit(url).path.split("/")
        assert parts[2] == endpoint
        batch = parts[4].split(",")
        municipalities.update(batch)
        assert len(batch) <= 25
        assert list(map(int, parts[6].split(","))) == list(range(2010, 2030))
        assert parse_qs(urlsplit(url).query)["from_date"] == ["2026-08-11"]
    assert len(municipalities) == 312
    # A successful previous fetch does not suppress repeated selection inside the window.
    again = [s async for s in adapter.discover_datasets(context)]
    assert next(s for s in again if s.candidate.dataset_code == "CHANGED").should_fetch


@pytest.mark.parametrize(
    "previous, force, full",
    [
        (None, False, True),
        (CUTOFF - timedelta(microseconds=1), False, True),
        (CUTOFF, False, False),
        (NOW, True, True),
    ],
)
async def test_full_selection_and_cutoff(previous, force, full):
    requests = Requests()
    adapter = adapter_for("kolada", requests)
    items = [
        s async for s in adapter.discover_datasets(HarvestContext(states(), NOW, previous, force))
    ]
    assert all(s.should_fetch for s in items) is full
    assert any("from_date=" in url for url in requests.calls) is not full


async def test_year_coverage_grows_and_cutoff_uses_utc():
    requests = Requests()
    adapter = adapter_for("kolada", requests)
    started = datetime(2033, 1, 1, 1, tzinfo=timezone(timedelta(hours=2)))
    items = [
        s
        async for s in adapter.discover_datasets(
            HarvestContext(states(), started, started - timedelta(days=1))
        )
    ]
    assert items
    queries = [u for u in requests.calls if "from_date=" in u]
    assert len(queries) == 52  # 26 years split into two batches, 13 municipality batches.
    years = set()
    for url in queries:
        years.update(map(int, urlsplit(url).path.split("/")[-1].split(",")))
        assert parse_qs(urlsplit(url).query)["from_date"] == ["2032-12-01"]
    assert years == set(range(2010, 2036))


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"values": None},
        {"values": [None]},
        {"values": [{}]},
        {"values": [{"kpi": "A"}], "next_url": 3},
    ],
)
async def test_incomplete_change_feed_fails_discovery(bad):
    adapter = adapter_for("kolada", Requests(bad=bad))
    with pytest.raises(ValueError):
        _ = [s async for s in adapter.discover_datasets(HarvestContext(states(), NOW, NOW))]


async def test_pagination_relative_links_and_cycle():
    class Pages:
        def __init__(self):
            self.calls = []

        async def get_json(self, url):
            self.calls.append(url)
            return {
                "values": [{"kpi": "A"}],
                "next_url": "?page=2" if len(self.calls) == 1 else None,
            }

    pages = Pages()
    assert await get_paginated_values(pages, "https://api.kolada.se/v3/data?page=1") == [
        {"kpi": "A"},
        {"kpi": "A"},
    ]
    assert pages.calls[-1] == "https://api.kolada.se/v3/data?page=2"

    class Cycle:
        async def get_json(self, url):
            return {"values": [], "next_url": url}

    with pytest.raises(ValueError, match="cycle"):
        await get_paginated_values(Cycle(), "https://api.kolada.se/v3/data")


def test_deleted_values_do_not_contribute_current_dimensions():
    entries = [
        {
            "period": 2020,
            "municipality": "OLD",
            "values": [{"value": 1, "isdeleted": True}],
        },
        {
            "period": 2024,
            "municipality": "NEW",
            "values": [{"value": 2, "isdeleted": False}],
        },
    ]
    assert extract_presence(entries) == ({2024}, {"NEW"})


async def test_request_error_propagates():
    class Broken(Requests):
        async def get_json(self, url):
            if "from_date" in url:
                raise TimeoutError("upstream")
            return await super().get_json(url)

    adapter = adapter_for("kolada", Broken())
    with pytest.raises(TimeoutError):
        _ = [s async for s in adapter.discover_datasets(HarvestContext(states(), NOW, NOW))]


@pytest.mark.parametrize("provider, entity", [("kolada", "municipality"), ("kolada", "ou")])
async def test_harvest_rebuild_removes_old_dimensions_and_preserves_metadata_on_empty(
    provider, entity
):
    from nordicintel_harvest.inputs import HarvestInput
    from nordicintel_harvest.runner import Progress, execute
    from nordicintel_harvest.schemas.datasets import DatasetDocuments

    class CurrentData(Requests):
        period = 2020
        deleted = False

        async def start(self):
            pass

        async def close(self):
            pass

        async def set_limit(self, *args, **kwargs):
            pass

        async def get_json(self, url):
            self.calls.append(url)
            if url.endswith("/kpi_groups"):
                return {"values": [], "next_url": None}
            if url.endswith("/kpi"):
                return {
                    "values": [
                        {
                            "id": "A",
                            "title": "A",
                            "municipality_type": "A",
                            "has_ou_data": True,
                        }
                    ],
                    "next_url": None,
                }
            if url.endswith("/municipality"):
                return {
                    "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
                    "next_url": None,
                }
            if url.endswith("/ou"):
                return {
                    "values": [{"id": "OU", "title": "School", "municipality": "0180"}],
                    "next_url": None,
                }
            if "from_date=" in url:
                return {
                    "values": [{"kpi": "A", "values": [{"isdeleted": self.deleted}]}],
                    "next_url": None,
                }
            years = set(map(int, url.rsplit("/", 1)[-1].split(",")))
            return {
                "values": [
                    {
                        "kpi": "A",
                        "period": self.period,
                        "municipality": "0180",
                        "ou": "OU",
                        "values": [{"value": 1, "isdeleted": self.deleted}],
                    }
                ]
                if self.period in years
                else [],
                "next_url": None,
            }

    requests = CurrentData()
    stored, errors = {}, {}

    async def emit(payload):
        if payload["outcome"] == "saved":
            stored[payload["dataset_code"]] = DatasetDocuments.model_validate(payload["documents"])
        elif payload["outcome"] == "failed":
            errors[payload["dataset_code"]] = payload["error"]

    async def run():
        await execute(
            HarvestInput(
                adapter="kolada", provider_code=provider, language="sv", rate_limit=0, config={}
            ),
            HarvestContext({}, NOW, force=True),
            emit,
            Progress(),
            manager_factory=lambda: requests,
        )

    await run()
    assert not errors
    requests.period = 2024
    await run()
    assert not errors
    code = "A_OU" if entity == "ou" else "A"
    before = stored[code]
    assert set(before.metadata.dimension["year"].category.index) == {"2024"}
    requests.deleted = True
    await run()
    assert set(errors) == {"A", "A_OU"}
    assert stored[code] is before
