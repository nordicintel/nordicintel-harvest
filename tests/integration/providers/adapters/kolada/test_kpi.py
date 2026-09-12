from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from legacy_inputs import ProviderDefinition

from nordicintel_harvest.providers.adapters.kolada.adapter import KoladaAdapter
from nordicintel_harvest.providers.interface import DatasetCandidate, HarvestContext


class _FakeRequestManager:
    """Queue-based fake matching the convention used by the other adapter
    tests (test_dst_adapter.py, test_pxweb2_adapter.py) - payloads must be
    queued in the exact order the code under test will request them."""

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
        label="Kolada",
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
                "title": "Invånare totalt",
                "description": "Antal invånare",
                "is_divided_by_gender": gender,
                "municipality_type": "A",
                "publication_date": "2026-02-01",
                "prel_publication_date": None,
                "has_ou_data": False,
            }
        ],
        "next_url": None,
    }


def _municipality_page() -> dict[str, Any]:
    return {
        "values": [{"id": "0180", "title": "Stockholm", "type": "K"}],
        "next_url": None,
    }


def _data_page(period: int = 2024) -> dict[str, Any]:
    return {
        "values": [
            {
                "kpi": "N00001",
                "period": period,
                "municipality": "0180",
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


async def test_discover_datasets_yields_one_dataset_per_kpi() -> None:
    request_manager = _FakeRequestManager([_kpi_page()])
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

    assert len(discovered) == 1
    assert discovered[0].dataset_code == "N00001"
    assert discovered[0].label == "Invånare totalt"
    assert discovered[0].updated is None


async def test_resolve_dataset_builds_municipality_and_year_dimensions() -> None:
    request_manager = _FakeRequestManager([_kpi_page(), _municipality_page(), _data_page()])
    adapter = KoladaAdapter(
        provider=_provider(),
        language="sv",
        request_manager=request_manager,
        logger=None,
    )
    discovered = DatasetCandidate(
        provider_code="kolada",
        dataset_code="N00001",
        language="sv",
        label="Invånare totalt",
        updated=datetime(2026, 2, 1, tzinfo=UTC),
    )

    attrs = await adapter.resolve_dataset(discovered)

    assert attrs.metadata.id == ["municipality", "year"]
    assert attrs.dataset.first_period == "2024"
    assert attrs.dataset.last_period == "2024"
    assert attrs.metadata.dimension["municipality"].category.label == {"0180": "Stockholm"}


async def test_resolve_dataset_adds_gender_dimension_when_divided_by_gender() -> None:
    request_manager = _FakeRequestManager(
        [_kpi_page(gender=True), _municipality_page(), _data_page()]
    )
    adapter = KoladaAdapter(
        provider=_provider(),
        language="sv",
        request_manager=request_manager,
        logger=None,
    )
    discovered = DatasetCandidate(
        provider_code="kolada",
        dataset_code="N00001",
        language="sv",
        label="Invånare totalt",
        updated=datetime(2026, 2, 1, tzinfo=UTC),
    )

    attrs = await adapter.resolve_dataset(discovered)

    assert "gender" in attrs.metadata.dimension
    assert attrs.metadata.dimension["gender"].category.label == {
        "K": "Kvinnor",
        "M": "Män",
        "T": "Totalt",
    }
    assert attrs.metadata.dimension["gender"].extension.get("elimination") is True
    document = attrs.documents()["metadata"]
    assert "dimension_set" not in document
    assert "required" not in str(document)
    assert document["dimension"]["gender"]["extension"] == {"elimination": True}
