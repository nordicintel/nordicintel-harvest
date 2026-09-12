from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from legacy_inputs import ProviderDefinition

from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.interface import DatasetCandidate, HarvestContext


class _FakeRequestManager:
    """Queue-based fake matching the convention used by test_kolada_adapter.py -
    payloads must be queued in the exact order the code under test will
    request them."""

    def __init__(self, payloads: list[Any]) -> None:
        self._payloads = payloads
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url: str, **kwargs: Any) -> dict[str, Any] | list[Any] | None:
        self.calls.append({"url": url, **kwargs})
        if url.endswith("/kpi_groups"):
            return {"values": [], "next_url": None}
        if not self._payloads:
            return {"values": [], "next_url": None}
        payload = self._payloads.pop(0)
        return payload if isinstance(payload, (dict, list)) else None

    async def set_limit(self, *args: Any, **kwargs: Any) -> None:
        return None


def _provider() -> ProviderDefinition:
    return ProviderDefinition(
        provider_code="kolada",
        adapter="kolada",
        default_language="sv",
        languages=["sv"],
        label="Kolada (enhetsdata)",
        country_code="SE",
        base_api_url="https://api.kolada.se/v3",
        base_web_url="https://kolada.se",
        rate_limit=0.35,
        cell_limit=5000,
        variable_limit=25,
        data_formats=["json"],
        extension={},
    )


def _kpi_page(kpi_id: str = "N00001", *, gender: bool = False) -> dict[str, Any]:
    return {
        "values": [
            {
                "id": kpi_id,
                "title": "Lärare med behörighet",
                "description": "Andel lärare med lärarlegitimation",
                "is_divided_by_gender": gender,
                "municipality_type": "A",
                "publication_date": "2026-02-01",
                "prel_publication_date": None,
                "has_ou_data": True,
            }
        ],
        "next_url": None,
    }


def _no_ou_kpi_page(kpi_id: str = "N99999") -> dict[str, Any]:
    return {
        "values": [
            {
                "id": kpi_id,
                "title": "Not an OU KPI",
                "description": None,
                "is_divided_by_gender": False,
                "municipality_type": "A",
                "publication_date": None,
                "prel_publication_date": None,
                "has_ou_data": False,
            }
        ],
        "next_url": None,
    }


def _ou_page() -> dict[str, Any]:
    return {
        "values": [{"id": "V15E01", "title": "Skola A", "municipality": "0180"}],
        "next_url": None,
    }


def _municipality_page() -> dict[str, Any]:
    return {
        "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
        "next_url": None,
    }


def _oudata_page(period: int = 2024) -> dict[str, Any]:
    return {
        "values": [
            {
                "kpi": "N00001",
                "period": period,
                "ou": "V15E01",
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
    }


async def test_discover_datasets_filters_out_kpis_without_ou_data() -> None:
    request_manager = _FakeRequestManager(
        [
            {
                "values": [
                    _kpi_page()["values"][0],
                    _no_ou_kpi_page()["values"][0],
                ],
                "next_url": None,
            }
        ]
    )
    adapter = KoladaAdapter(
        provider=_provider(),
        language="sv",
        request_manager=request_manager,
        logger=None,
    )

    discovered = [
        item.candidate
        async for item in adapter.discover_datasets(HarvestContext({}, datetime.now(UTC)))
    ]

    assert [d.dataset_code for d in discovered] == ["N00001", "N00001_OU", "N99999"]
    assert discovered[1].data_url == "https://api.kolada.se/v3/oudata/kpi/N00001"


async def test_resolve_dataset_builds_ou_and_year_dimensions() -> None:
    request_manager = _FakeRequestManager(
        [_kpi_page(), _ou_page(), _municipality_page(), _oudata_page()]
    )
    adapter = KoladaAdapter(
        provider=_provider(),
        language="sv",
        request_manager=request_manager,
        logger=None,
    )
    discovered = DatasetCandidate(
        provider_code="kolada",
        dataset_code="N00001_OU",
        language="sv",
        label="Lärare med behörighet",
        updated=datetime(2026, 2, 1, tzinfo=UTC),
    )

    attrs = await adapter.resolve_dataset(discovered)

    assert attrs.metadata.id == ["ou", "year"]
    assert attrs.dataset.first_period == "2024"
    assert attrs.dataset.last_period == "2024"
    assert attrs.metadata.dimension["ou"].category.label == {"V15E01": "Skola A"}
    assert attrs.metadata.dimension["ou"].extension == {
        "elimination": False,
        "category": {
            "V15E01": {
                "municipality_id": "0180",
                "municipality_label": "Stockholm",
            }
        },
    }


async def test_resolve_dataset_adds_gender_dimension_when_divided_by_gender() -> None:
    request_manager = _FakeRequestManager(
        [_kpi_page(gender=True), _ou_page(), _municipality_page(), _oudata_page()]
    )
    adapter = KoladaAdapter(
        provider=_provider(),
        language="sv",
        request_manager=request_manager,
        logger=None,
    )
    discovered = DatasetCandidate(
        provider_code="kolada",
        dataset_code="N00001_OU",
        language="sv",
        label="Lärare med behörighet",
        updated=datetime(2026, 2, 1, tzinfo=UTC),
    )

    attrs = await adapter.resolve_dataset(discovered)

    assert "gender" in attrs.metadata.dimension
    assert attrs.metadata.dimension["gender"].extension.get("elimination") is True


async def test_resolve_dataset_rejects_a_kpi_without_ou_data() -> None:
    request_manager = _FakeRequestManager([_no_ou_kpi_page(kpi_id="N00001")])
    adapter = KoladaAdapter(
        provider=_provider(),
        language="sv",
        request_manager=request_manager,
        logger=None,
    )
    discovered = DatasetCandidate(
        provider_code="kolada",
        dataset_code="N00001_OU",
        language="sv",
        label="Not an OU KPI",
        updated=None,
    )

    with pytest.raises(ValueError):
        await adapter.resolve_dataset(discovered)
