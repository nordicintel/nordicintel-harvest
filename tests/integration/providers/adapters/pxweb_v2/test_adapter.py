from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from legacy_inputs import ProviderDefinition

from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.providers.adapters.pxweb_v2.adapter import PxWebV2Adapter
from nordicintel_harvest.providers.interface import (
    DatasetCandidate,
    HarvestContext,
    ProviderAdapter,
)


class _FakeResponse:
    def __init__(self, payload: Any):
        self._payload = payload

    async def json(self) -> Any:
        return self._payload


class _FakeRequestManager:
    def __init__(self, payloads: list[Any]):
        self._payloads = payloads
        self.calls: list[dict[str, Any]] = []

    async def make_request(self, **kwargs: Any) -> _FakeResponse | None:
        self.calls.append(kwargs)
        if not self._payloads:
            return None
        return _FakeResponse(self._payloads.pop(0))

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any] | None:
        self.calls.append({"url": url, "params": params, "method": "GET", **kwargs})
        if not self._payloads:
            return None
        payload = self._payloads.pop(0)
        if isinstance(payload, (dict, list)):
            return payload
        return None

    async def set_limit(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class _DummyAdapter(ProviderAdapter):
    async def _discover_candidates(self):
        if False:  # pragma: no cover
            yield DatasetCandidate(
                provider_code="",
                dataset_code="",
                language="",
            )

    async def resolve_dataset(self, discovered: DatasetCandidate):
        raise NotImplementedError("Discovery-only test adapter")


def _provider() -> ProviderDefinition:
    return ProviderDefinition(
        provider_code="scb",
        adapter="pxweb_v2",
        default_language="en",
        languages=["en", "sv"],
        label="Statistics Sweden",
        country_code="SE",
        base_api_url="https://api.example.com",
        base_web_url="https://www.example.com",
        rate_limit=1.0,
        cell_limit=5000,
        variable_limit=1000,
        data_formats=[],
        extension={},
    )


def test_api_adapter_rejects_unsupported_language() -> None:
    with pytest.raises(ValueError):
        _DummyAdapter(
            provider=_provider(),
            language="no",
            request_manager=RequestManager(global_max_concurrency=1),
        )


@pytest.mark.asyncio
async def test_pxweb2_discover_paginates_and_handles_missing_id_and_null_updated() -> None:
    request_manager = _FakeRequestManager(
        [
            {
                "tables": [
                    {
                        "id": "",
                        "label": "missing id",
                    },
                    {
                        "id": "TAB001",
                        "label": "Population by age",
                        "updated": None,
                        "timeUnit": "Annual",
                        "firstPeriod": "2000",
                        "lastPeriod": "2024",
                        "variableNames": ["age", "sex"],
                        "source": "SCB",
                        "description": "Population dataset",
                        "subjectCode": "BE",
                        "paths": [
                            [
                                {"code": "BE", "label": "Population"},
                                {"code": "TAB001", "label": "Population by age"},
                            ]
                        ],
                        "links": {"self": "x"},
                    },
                ],
                "page": {"pageNumber": 1, "totalPages": 2},
            },
            {
                "tables": [
                    {
                        "id": "TAB002",
                        "title": "Households",
                        "updated": "2026-03-04T00:00:00Z",
                        "timeUnit": "Quarterly",
                        "firstPeriod": "2010Q1",
                        "lastPeriod": "2024Q4",
                        "variableNames": ["region"],
                        "paths": ["Households"],
                    }
                ],
                "page": {"pageNumber": 2, "totalPages": 2},
            },
        ]
    )

    adapter = PxWebV2Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )
    datasets = [
        item.candidate
        async for item in adapter.discover_datasets(HarvestContext({}, datetime.now(UTC)))
    ]
    assert len(datasets) == 2
    assert datasets[0].dataset_code == "TAB001"
    assert datasets[0].updated is None
    assert datasets[0].paths is not None
    assert [path.model_dump(exclude_defaults=True) for path in datasets[0].paths] == [
        {
            "path": [
                {"code": "BE", "label": "Population"},
                {"code": "TAB001", "label": "Population by age"},
            ]
        }
    ]
    assert datasets[1].dataset_code == "TAB002"
    assert datasets[1].paths is not None
    assert [path.model_dump(exclude_defaults=True) for path in datasets[1].paths] == [
        {"path": [{"code": "Households", "label": "Households"}]}
    ]
    assert len(request_manager.calls) == 2
    assert request_manager.calls[0]["params"]["pageNumber"] == 1
    assert request_manager.calls[1]["params"]["pageNumber"] == 2


@pytest.mark.asyncio
async def test_pxweb2_discover_returns_full_listing_without_query_filter() -> None:
    request_manager = _FakeRequestManager(
        [
            {
                "tables": [
                    {
                        "id": "OLD1",
                        "label": "Old",
                        "updated": "2026-03-01T00:00:00Z",
                    },
                    {
                        "id": "NEW1",
                        "label": "New",
                        "updated": "2026-03-04T00:00:00Z",
                    },
                ],
                "page": {"pageNumber": 1, "totalPages": 1},
            }
        ]
    )
    adapter = PxWebV2Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )
    datasets = [
        item.candidate
        async for item in adapter.discover_datasets(HarvestContext({}, datetime.now(UTC)))
    ]
    assert [d.dataset_code for d in datasets] == ["OLD1", "NEW1"]
    assert "updatedAfter" not in request_manager.calls[0]["params"]


@pytest.mark.asyncio
async def test_pxweb2_discover_raises_on_invalid_payload_shape() -> None:
    request_manager = _FakeRequestManager([{"page": {"pageNumber": 1, "totalPages": 1}}])
    adapter = PxWebV2Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="Invalid pxweb2 tables response shape"):
        _ = [
            item.candidate
            async for item in adapter.discover_datasets(HarvestContext({}, datetime.now(UTC)))
        ]


def _metadata_payload() -> dict[str, Any]:
    return {
        "id": ["Region", "Tid", "ContentsCode"],
        "role": {"geo": ["Region"], "time": ["Tid"], "metric": ["ContentsCode"]},
        "dimension": {
            "Region": {
                "label": "region",
                "category": {
                    "index": {"00": 0, "01": 1},
                    "label": {"00": "Riket", "01": "Stockholm"},
                },
                "elimination": True,
            },
            "Tid": {
                "label": "år",
                "category": {"index": {"2024": 0}, "label": {"2024": "2024"}},
            },
            "ContentsCode": {
                "label": "tabellinnehåll",
                "category": {
                    "index": {"BE0101N1": 0},
                    "label": {"BE0101N1": "Folkmängd"},
                    "unit": {"BE0101N1": {"base": "antal", "decimals": 0}},
                },
            },
        },
        "extension": {"px": {"subject-area": "Population", "official-statistics": True}},
    }


@pytest.mark.asyncio
async def test_pxweb2_resolve_maps_dimensions_roles_and_extensions() -> None:
    request_manager = _FakeRequestManager([_metadata_payload()])
    adapter = PxWebV2Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )
    discovered = DatasetCandidate(
        provider_code="scb",
        dataset_code="TAB0001",
        language="en",
        label="Population",
    )

    resolved = await adapter.resolve_dataset(discovered)

    # payload["id"] order, not dimension-object key order
    assert resolved.metadata.id == ["Region", "Tid", "ContentsCode"]
    assert resolved.metadata.role is not None
    assert resolved.metadata.role.model_dump(exclude_none=True) == {
        "geo": ["Region"],
        "time": ["Tid"],
        "metric": ["ContentsCode"],
    }
    assert resolved.metadata.dimension["Region"].category.label == {
        "00": "Riket",
        "01": "Stockholm",
    }
    assert resolved.metadata.dimension["Region"].extension.get("elimination") is True
    assert resolved.metadata.dimension["Tid"].extension.get("elimination") is False
    assert resolved.metadata.dimension["ContentsCode"].extension.get("elimination") is False
    assert resolved.metadata.subject.label == "Population"
    assert resolved.metadata.official_statistics is True


@pytest.mark.asyncio
async def test_pxweb2_resolve_fails_loudly_on_an_id_without_a_dimension() -> None:
    payload = _metadata_payload()
    payload["id"] = ["Region", "Tid", "ContentsCode", "Ghost"]
    request_manager = _FakeRequestManager([payload])
    adapter = PxWebV2Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )
    discovered = DatasetCandidate(provider_code="scb", dataset_code="TAB0001", language="en")

    with pytest.raises(ValueError, match="exactly match dimension keys"):
        await adapter.resolve_dataset(discovered)


@pytest.mark.asyncio
async def test_pxweb2_resolve_fails_loudly_on_a_role_naming_an_unknown_dimension() -> None:
    payload = _metadata_payload()
    payload["role"]["geo"] = ["Region", "Ghost"]
    request_manager = _FakeRequestManager([payload])
    adapter = PxWebV2Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )
    discovered = DatasetCandidate(provider_code="scb", dataset_code="TAB0001", language="en")

    with pytest.raises(ValueError, match="role names unknown dimensions"):
        await adapter.resolve_dataset(discovered)
