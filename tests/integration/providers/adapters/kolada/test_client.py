from __future__ import annotations

import logging
from typing import Any

from nordicintel_harvest.providers.adapters.kolada.client import KoladaClient
from nordicintel_harvest.providers.adapters.kolada.parsing import (
    EARLIEST_POSSIBLE_YEAR,
    KpiInfo,
    MunicipalityInfo,
    build_data_request_batches,
    max_probe_year,
    probe_year_range,
)


class _FakeRequestManager:
    """URL-keyed fake - Kolada calls hit multiple distinct URLs per method
    (unlike the queue-based fakes used for other adapters), so lookups need
    to be by URL, not call order."""

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def add(self, url: str, payload: dict[str, Any]) -> None:
        self.responses[url] = payload

    async def get_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(url)
        return self.responses.get(url)


def _client(request_manager: _FakeRequestManager) -> KoladaClient:
    return KoladaClient(
        request_manager=request_manager,  # type: ignore[arg-type]
        base_api_url="https://api.kolada.se/v3",
        logger=logging.getLogger("test-kolada-client"),
    )


def _kpi_payload(**overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "N00001",
        "title": "First",
        "description": None,
        "municipality_type": "A",
        "is_divided_by_gender": False,
        "has_ou_data": False,
        "publication_date": None,
        "prel_publication_date": None,
    }
    payload.update(overrides)
    return payload


async def test_list_kpis_follows_pagination_via_next_url() -> None:
    request_manager = _FakeRequestManager()
    page1_url = "https://api.kolada.se/v3/kpi"
    page2_url = "https://api.kolada.se/v3/kpi?page=2"
    request_manager.add(page1_url, {"values": [_kpi_payload(id="N00001")], "next_url": page2_url})
    request_manager.add(
        page2_url,
        {"values": [_kpi_payload(id="N00002", title="Second")], "next_url": None},
    )
    groups_url = "https://api.kolada.se/v3/kpi_groups"
    request_manager.add(groups_url, {"values": [], "next_url": None})
    client = _client(request_manager)

    kpis = await client.list_kpis()

    assert [k.kpi_id for k in kpis] == ["N00001", "N00002"]
    assert request_manager.calls == [page1_url, page2_url, groups_url]

    cached = await client.list_kpis()
    assert cached is kpis
    assert request_manager.calls == [page1_url, page2_url, groups_url]  # no re-fetch


async def test_get_kpi_looks_up_by_id_from_the_cached_list() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/kpi",
        {"values": [_kpi_payload(id="N00001")], "next_url": None},
    )
    request_manager.add("https://api.kolada.se/v3/kpi_groups", {"values": [], "next_url": None})
    client = _client(request_manager)

    kpi = await client.get_kpi("N00001")
    assert kpi is not None
    assert kpi.title == "First"
    assert await client.get_kpi("MISSING") is None


async def test_list_municipalities_parses_type_field() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/municipality",
        {
            "values": [
                {"id": "0000", "title": "Riket", "type": "L"},
                {"id": "0180", "title": "Stockholm", "type": "K"},
            ],
            "next_url": None,
        },
    )
    client = _client(request_manager)

    municipalities = await client.list_municipalities()

    assert municipalities == [
        MunicipalityInfo(municipality_id="0000", title="Riket", type="L"),
        MunicipalityInfo(municipality_id="0180", title="Stockholm", type="K"),
    ]


async def test_fetch_year_and_municipality_presence_batches_and_aggregates() -> None:
    request_manager = _FakeRequestManager()
    municipality_ids = [f"{i:04d}" for i in range(26)]  # forces a 25 + 1 batch split
    years = [2023]

    batch1_ids = ",".join(municipality_ids[:25])
    batch2_id = municipality_ids[25]
    url1 = f"https://api.kolada.se/v3/data/kpi/N00001/municipality/{batch1_ids}/year/2023"
    url2 = f"https://api.kolada.se/v3/data/kpi/N00001/municipality/{batch2_id}/year/2023"

    request_manager.add(
        url1,
        {
            "values": [
                {
                    "kpi": "N00001",
                    "period": 2023,
                    "municipality": "0000",
                    "values": [
                        {
                            "gender": "T",
                            "value": 1.0,
                            "status": "",
                            "count": 1,
                            "isdeleted": False,
                        }
                    ],
                }
            ],
            "next_url": None,
        },
    )
    request_manager.add(
        url2,
        {
            "values": [
                {
                    "kpi": "N00001",
                    "period": 2023,
                    "municipality": batch2_id,
                    "values": [
                        {
                            "gender": "T",
                            "value": 2.0,
                            "status": "",
                            "count": 1,
                            "isdeleted": False,
                        }
                    ],
                }
            ],
            "next_url": None,
        },
    )
    client = _client(request_manager)

    (
        years_found,
        municipalities_found,
    ) = await client.fetch_year_and_municipality_presence(
        kpi_id="N00001", municipality_ids=municipality_ids, years=years
    )

    assert years_found == {2023}
    assert municipalities_found == {"0000", batch2_id}
    assert set(request_manager.calls) == {url1, url2}


def _single_muni_kpi(**overrides: object) -> KpiInfo:
    defaults: dict[str, object] = {
        "kpi_id": "N00001",
        "title": "Test",
        "description": None,
        "is_divided_by_gender": False,
        "municipality_type": "K",
        "publication_date": None,
        "prel_publication_date": None,
        "publ_period": None,
        "has_ou_data": False,
    }
    defaults.update(overrides)
    return KpiInfo(**defaults)  # type: ignore[arg-type]


async def test_resolve_dataset_dimensions_probes_full_history_on_first_resolve() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/municipality",
        {
            "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
            "next_url": None,
        },
    )
    expected_years = probe_year_range(max_year=max_probe_year())
    # The full ~1970-> range spans more than 25 years, so the client issues
    # one batched request per <=25-year chunk - every chunk needs a mock,
    # not just one combined URL, or unmocked batches silently return no data.
    batches = build_data_request_batches(["0180"], expected_years)
    for muni_batch, year_batch in batches:
        muni_segment = ",".join(muni_batch)
        year_segment = ",".join(str(y) for y in year_batch)
        url = (
            f"https://api.kolada.se/v3/data/kpi/N00001"
            f"/municipality/{muni_segment}/year/{year_segment}"
        )
        has_earliest_year = EARLIEST_POSSIBLE_YEAR in year_batch
        request_manager.add(
            url,
            {
                "values": [
                    {
                        "kpi": "N00001",
                        "period": EARLIEST_POSSIBLE_YEAR,
                        "municipality": "0180",
                        "values": [
                            {
                                "gender": "T",
                                "value": 4.2,
                                "status": "",
                                "count": 1,
                                "isdeleted": False,
                            }
                        ],
                    }
                ]
                if has_earliest_year
                else [],
                "next_url": None,
            },
        )
    client = _client(request_manager)

    result = await client.resolve_dataset_dimensions(kpi=_single_muni_kpi())

    assert result.first_period == str(EARLIEST_POSSIBLE_YEAR)
    assert result.last_period == str(EARLIEST_POSSIBLE_YEAR)
    assert any(str(EARLIEST_POSSIBLE_YEAR) in call for call in request_manager.calls)


async def test_resolve_dataset_dimensions_rebuilds_current_history() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/municipality",
        {
            "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
            "next_url": None,
        },
    )
    expected_years = probe_year_range(max_year=max_probe_year())
    last_probed_year = expected_years[-1]
    for muni_batch, year_batch in build_data_request_batches(["0180"], expected_years):
        probe_url = f"https://api.kolada.se/v3/data/kpi/N00001/municipality/{','.join(muni_batch)}/year/{','.join(map(str, year_batch))}"
        request_manager.add(
            probe_url,
            {
                "values": [
                    {
                        "kpi": "N00001",
                        "period": last_probed_year,
                        "municipality": "0180",
                        "values": [{"value": 9.9, "isdeleted": False}],
                    }
                ]
                if last_probed_year in year_batch
                else [],
                "next_url": None,
            },
        )
    client = _client(request_manager)

    result = await client.resolve_dataset_dimensions(
        kpi=_single_muni_kpi(),
    )

    assert result.first_period == str(last_probed_year)
    assert result.last_period == str(last_probed_year)
    assert probe_url in request_manager.calls
    assert any("1970" in call for call in request_manager.calls)
