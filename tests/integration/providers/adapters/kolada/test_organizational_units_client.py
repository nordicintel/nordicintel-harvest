from __future__ import annotations

import logging
from typing import Any

from nordicintel_harvest.providers.adapters.kolada.client import KoladaOrganizationalUnitsClient
from nordicintel_harvest.providers.adapters.kolada.parsing import (
    EARLIEST_POSSIBLE_YEAR,
    KpiInfo,
    max_probe_year,
    probe_year_range,
)


class _FakeRequestManager:
    """URL-keyed fake, matching test_kolada_client.py's convention - Kolada
    calls hit multiple distinct URLs per method, so lookups need to be by
    URL, not call order."""

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def add(self, url: str, payload: dict[str, Any]) -> None:
        self.responses[url] = payload

    async def get_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(url)
        return self.responses.get(url)


def _client(request_manager: _FakeRequestManager) -> KoladaOrganizationalUnitsClient:
    return KoladaOrganizationalUnitsClient(
        request_manager=request_manager,  # type: ignore[arg-type]
        base_api_url="https://api.kolada.se/v3",
        logger=logging.getLogger("test-kolada-ou-client"),
    )


def _ou_kpi(**overrides: object) -> KpiInfo:
    defaults: dict[str, object] = {
        "kpi_id": "N00001",
        "title": "Test OU KPI",
        "description": None,
        "is_divided_by_gender": False,
        "municipality_type": "A",
        "publication_date": None,
        "prel_publication_date": None,
        "publ_period": None,
        "has_ou_data": True,
    }
    defaults.update(overrides)
    return KpiInfo(**defaults)  # type: ignore[arg-type]


async def test_list_ous_follows_pagination_and_caches() -> None:
    request_manager = _FakeRequestManager()
    page1_url = "https://api.kolada.se/v3/ou"
    page2_url = "https://api.kolada.se/v3/ou?page=2"
    request_manager.add(
        page1_url,
        {
            "values": [{"id": "V15E01", "title": "Skola A", "municipality": "0180"}],
            "next_url": page2_url,
        },
    )
    request_manager.add(
        page2_url,
        {
            "values": [{"id": "V15E02", "title": "Skola B", "municipality": "1280"}],
            "next_url": None,
        },
    )
    client = _client(request_manager)

    ous = await client.list_ous()

    assert [o.ou_id for o in ous] == ["V15E01", "V15E02"]
    assert request_manager.calls == [page1_url, page2_url]

    cached = await client.list_ous()
    assert cached is ous
    assert request_manager.calls == [page1_url, page2_url]  # no re-fetch


async def test_list_ou_kpis_filters_to_has_ou_data() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/kpi",
        {
            "values": [
                {
                    "id": "N00001",
                    "title": "Has OU",
                    "description": None,
                    "municipality_type": "A",
                    "is_divided_by_gender": False,
                    "has_ou_data": True,
                    "publication_date": None,
                    "prel_publication_date": None,
                },
                {
                    "id": "N99999",
                    "title": "No OU",
                    "description": None,
                    "municipality_type": "A",
                    "is_divided_by_gender": False,
                    "has_ou_data": False,
                    "publication_date": None,
                    "prel_publication_date": None,
                },
            ],
            "next_url": None,
        },
    )
    request_manager.add("https://api.kolada.se/v3/kpi_groups", {"values": [], "next_url": None})
    client = _client(request_manager)

    kpis = await client.list_ou_kpis()

    assert [k.kpi_id for k in kpis] == ["N00001"]


async def test_fetch_year_and_ou_presence_queries_one_url_per_year() -> None:
    request_manager = _FakeRequestManager()
    years = [2023, 2024]
    url_2023 = "https://api.kolada.se/v3/oudata/kpi/N00001/year/2023"
    url_2024 = "https://api.kolada.se/v3/oudata/kpi/N00001/year/2024"
    request_manager.add(url_2023, {"values": [], "next_url": None})
    request_manager.add(
        url_2024,
        {
            "values": [
                {
                    "kpi": "N00001",
                    "period": 2024,
                    "ou": "V15E01",
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
            ],
            "next_url": None,
        },
    )
    client = _client(request_manager)

    years_found, ous_found = await client.fetch_year_and_ou_presence(kpi_id="N00001", years=years)

    assert years_found == {2024}
    assert ous_found == {"V15E01"}
    assert set(request_manager.calls) == {url_2023, url_2024}


async def test_resolve_dataset_dimensions_probes_full_history_on_first_resolve() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/ou",
        {
            "values": [{"id": "V15E01", "title": "Skola A", "municipality": "0180"}],
            "next_url": None,
        },
    )
    request_manager.add(
        "https://api.kolada.se/v3/municipality",
        {
            "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
            "next_url": None,
        },
    )
    expected_years = probe_year_range(max_year=max_probe_year())
    for year in expected_years:
        url = f"https://api.kolada.se/v3/oudata/kpi/N00001/year/{year}"
        has_earliest_year = year == EARLIEST_POSSIBLE_YEAR
        request_manager.add(
            url,
            {
                "values": [
                    {
                        "kpi": "N00001",
                        "period": EARLIEST_POSSIBLE_YEAR,
                        "ou": "V15E01",
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

    result = await client.resolve_dataset_dimensions(kpi=_ou_kpi())

    assert result.first_period == str(EARLIEST_POSSIBLE_YEAR)
    assert result.last_period == str(EARLIEST_POSSIBLE_YEAR)
    assert result.dimension["ou"].extension["category"]["V15E01"] == {
        "municipality_id": "0180",
        "municipality_label": "Stockholm",
    }
    assert set(request_manager.calls) >= {
        "https://api.kolada.se/v3/ou",
        "https://api.kolada.se/v3/municipality",
    }


async def test_resolve_dataset_dimensions_rebuilds_current_history() -> None:
    request_manager = _FakeRequestManager()
    request_manager.add(
        "https://api.kolada.se/v3/ou",
        {
            "values": [{"id": "V15E01", "title": "Skola A", "municipality": "0180"}],
            "next_url": None,
        },
    )
    request_manager.add(
        "https://api.kolada.se/v3/municipality",
        {
            "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
            "next_url": None,
        },
    )
    expected_years = probe_year_range(max_year=max_probe_year())
    last_probed_year = expected_years[-1]
    for year in expected_years:
        url = f"https://api.kolada.se/v3/oudata/kpi/N00001/year/{year}"
        request_manager.add(
            url,
            {
                "values": [
                    {
                        "kpi": "N00001",
                        "period": last_probed_year,
                        "ou": "V15E01",
                        "values": [
                            {
                                "gender": "T",
                                "value": 9.9,
                                "status": "",
                                "count": 1,
                                "isdeleted": False,
                            }
                        ],
                    }
                ]
                if year == last_probed_year
                else [],
                "next_url": None,
            },
        )
    client = _client(request_manager)

    result = await client.resolve_dataset_dimensions(
        kpi=_ou_kpi(),
    )

    assert result.first_period == str(last_probed_year)
    assert result.last_period == str(last_probed_year)
    assert any("/year/1970" in call for call in request_manager.calls)
    assert "V99E99" not in result.dimension["ou"].category.index
