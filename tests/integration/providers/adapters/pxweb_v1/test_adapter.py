from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from legacy_inputs import ProviderDefinition

from nordicintel_harvest.core.request_manager import RequestResult
from nordicintel_harvest.providers.adapters.pxweb_v1.adapter import PxWebV1Adapter
from nordicintel_harvest.providers.interface import DatasetCandidate, HarvestContext
from nordicintel_harvest.schemas.datasets import DatasetPath


class _FakeRequestManager:
    def __init__(self, payloads: dict[str, Any]) -> None:

        self.payloads = payloads

        self.calls: list[dict[str, Any]] = []

        self.limit_calls: list[dict[str, Any]] = []

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any] | None:

        self.calls.append({"url": url, "params": params, "method": "GET", **kwargs})

        lookup_key = url

        if params:
            params_key = "&".join(f"{key}={params[key]}" for key in sorted(params))

            lookup_key = f"{url}?{params_key}"

        payload = self.payloads.get(lookup_key, self.payloads.get(url))

        if isinstance(payload, (dict, list)):
            return payload

        return None

    async def request(self, url: str, *, method: str, json_data: dict) -> RequestResult:

        self.calls.append({"url": url, "method": method, "json_data": json_data})

        if hasattr(self, "sample_error"):
            raise self.sample_error

        sample = getattr(
            self,
            "sample",
            {
                "class": "dataset",
                "version": "2.0",
                "id": ["region", "time", "ContentsCode"],
                "size": [2, 2, 1],
                "dimension": {
                    "region": {"category": {"index": {"SE1": 0, "SE2": 1}}},
                    "time": {"category": {"index": {"2024": 0, "2025": 1}}},
                    "ContentsCode": {"category": {"index": {"OBS": 0}}},
                },
                "value": [0, 0, 0, 0],
            },
        )

        return RequestResult(
            status=getattr(self, "status", 200),
            url=url,
            headers={},
            body=json.dumps(sample).encode(),
        )

    async def set_limit(self, url: str, *, interval: float, max_concurrency: int) -> None:

        self.limit_calls.append(
            {
                "url": url,
                "interval": interval,
                "max_concurrency": max_concurrency,
            }
        )

    async def close(self) -> None:

        return None


class _FakeCatalogRequestManager(_FakeRequestManager):
    def __init__(self, responses: list[RequestResult]) -> None:

        super().__init__({})

        self.responses = responses

    async def request(
        self,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | list[Any] | None = None,
        form_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RequestResult | None:

        self.calls.append(
            {
                "url": url,
                "params": params,
                "method": method,
                "headers": headers,
                "json_data": json_data,
                "form_data": form_data,
                **kwargs,
            }
        )

        return self.responses.pop(0)


def _result(value: Any, *, url: str, content_type: str) -> RequestResult:

    body = json.dumps(value).encode() if content_type == "application/json" else value.encode()

    return RequestResult(
        status=200,
        url=url,
        headers={"Content-Type": content_type},
        body=body,
        encoding="utf-8",
    )


def _provider() -> ProviderDefinition:

    return ProviderDefinition(
        provider_code="statfin_pxweb",
        adapter="pxweb_v1",
        default_language="en",
        languages=["en", "sv"],
        label="StatFin PXWEB",
        country_code="FI",
        base_api_url="https://pxdata.example/api/v1",
        base_web_url="https://web.example/browser",
        rate_limit=1.0,
        cell_limit=100000,
        variable_limit=1000,
        data_formats=["json-stat2"],
        extension={"dbid": {"StatFin": "StatFin"}},
    )


@pytest.mark.asyncio
async def test_pxweb_discover_builds_canonical_dataset_from_search_listing() -> None:

    root_url = "https://pxdata.example/api/v1/en/StatFin"

    web_url = "https://web.example/browser/en/StatFin/"

    request_manager = _FakeCatalogRequestManager(
        [
            _result(
                [
                    {
                        "id": "table 1.px",
                        "title": "Population table",
                        "path": "demo / pop /",
                        "updated": "2026-03-04T09:10:00",
                    },
                ],
                url=root_url,
                content_type="application/json",
            ),
            _result(
                """

                <form action="/browser/en/StatFin/">

                  <input type="hidden" name="__VIEWSTATE" value="state">

                  <input class="tableofcontent_action" name="expand" value="all" type="submit">

                </form>

                """,
                url=web_url,
                content_type="text/html",
            ),
            _result(
                """

                <ul>

                  <li class="AspNet-TreeView-Root"><span>Demographics</span><ul>

                    <li class="AspNet-TreeView-Parent"><span>Population</span><ul>

                      <li class="AspNet-TreeView-Leaf"><a href="/browser/en/StatFin/StatFin__demo__pop/table%201.px/">Population table</a></li>

                    </ul></li>

                  </ul></li>

                  <li class="AspNet-TreeView-Root"><span>Other</span><ul>

                    <li class="AspNet-TreeView-Parent"><span>Population</span><ul>

                      <li class="AspNet-TreeView-Leaf"><a href="/browser/en/StatFin/StatFin__other__pop/table%201.px/">Population table</a></li>

                    </ul></li>

                  </ul></li>

                </ul>

                """,
                url=web_url,
                content_type="text/html",
            ),
        ]
    )

    adapter = PxWebV1Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )

    datasets = [
        item.candidate
        async for item in adapter.discover_datasets(HarvestContext({}, datetime.now(UTC)))
    ]

    assert len(datasets) == 1

    assert datasets[0].dataset_code == "table 1.px"

    assert datasets[0].updated == datetime(2026, 3, 4, 9, 10, tzinfo=UTC)

    assert datasets[0].subject_code == "pop"

    assert datasets[0].subject_label == "Population"

    assert datasets[0].metadata_url == (
        "https://pxdata.example/api/v1/en/StatFin/demo/pop/table%201.px"
    )

    assert datasets[0].web_url == (
        "https://web.example/browser/en/StatFin/StatFin__demo__pop/table%201.px/"
    )

    assert datasets[0].paths is not None

    assert [path.model_dump(exclude_defaults=True) for path in datasets[0].paths] == [
        {
            "path": [
                {"code": "demo", "label": "Demographics"},
                {"code": "pop", "label": "Population"},
            ]
        },
        {
            "path": [
                {"code": "other", "label": "Other"},
                {"code": "pop", "label": "Population"},
            ]
        },
    ]

    assert request_manager.limit_calls == []


@pytest.mark.asyncio
async def test_pxweb_discover_does_not_double_encode_configured_database_id() -> None:

    provider = _provider().model_copy(
        update={"config": {**_provider().config, "database_ids": ["Nordic%20Statistics"]}}
    )

    api_url = "https://pxdata.example/api/v1/en/Nordic%20Statistics"

    web_url = "https://web.example/browser/en/Nordic%20Statistics/"

    manager = _FakeCatalogRequestManager(
        [
            _result(
                [
                    {
                        "id": "table.px",
                        "path": "/first",
                        "title": "Table",
                        "updated": "2026-03-04T09:10:00",
                    }
                ],
                url=api_url,
                content_type="application/json",
            ),
            _result(
                """

                <ul><li class="AspNet-TreeView-Root"><span>First</span>

                  <ul><li class="AspNet-TreeView-Leaf">

                    <a href="/browser/en/Nordic%20Statistics/Nordic%20Statistics__first/table.px/">Table</a>

                  </li></ul>

                </li></ul>

                """,
                url=web_url,
                content_type="text/html",
            ),
        ]
    )

    adapter = PxWebV1Adapter(
        provider=provider,
        language="en",
        request_manager=manager,  # type: ignore[arg-type]
    )

    datasets = [
        item.candidate
        async for item in adapter.discover_datasets(HarvestContext({}, datetime.now(UTC)))
    ]

    assert len(datasets) == 1

    assert manager.calls[0]["url"] == api_url

    assert manager.calls[1]["url"] == web_url


@pytest.mark.asyncio
async def test_pxweb_resolve_maps_dimensions_and_preserves_dimension_extensions() -> None:

    metadata_url = "https://pxdata.example/api/v1/en/StatFin/demo/pop/table%201.px"

    request_manager = _FakeRequestManager(
        {
            metadata_url: {
                "title": "Population by region and year",
                "variables": [
                    {
                        "code": "region",
                        "text": "Region",
                        "elimination": True,
                        "values": ["SE1", "SE2"],
                        "valueTexts": ["North", "South"],
                        "map": "NUTS-2024",
                    },
                    {
                        "code": "time",
                        "text": "Year",
                        "time": True,
                        "values": ["2024", "2025"],
                        "valueTexts": ["2024", "2025"],
                    },
                    {
                        "code": "ContentsCode",
                        "text": "Contents",
                        "values": ["OBS"],
                        "valueTexts": ["Population"],
                    },
                ],
            }
        }
    )

    adapter = PxWebV1Adapter(
        provider=_provider(),
        language="en",
        request_manager=request_manager,  # type: ignore[arg-type]
    )

    discovered = DatasetCandidate(
        provider_code="statfin_pxweb",
        dataset_code="table 1.px",
        language="en",
        label="Fallback title",
        updated=datetime(2026, 3, 4, 9, 10, tzinfo=UTC),
        subject_code="pop",
        subject_label="Population",
        metadata_url=metadata_url,
        data_url=metadata_url,
        web_url="https://web.example/browser/en/StatFin/StatFin__demo__pop/table%201.px/",
        paths=[
            DatasetPath.model_validate(
                {
                    "path": [
                        {"code": "demo", "label": "demo"},
                        {"code": "pop", "label": "Population"},
                    ]
                }
            )
        ],
        extension={"discovery_mode": "search_filter"},
    )

    resolved = await adapter.resolve_dataset(discovered)

    assert resolved.dataset.label == "Population by region and year"

    assert resolved.dataset.time_unit == "Annual"

    assert resolved.dataset.first_period == "2024"

    assert resolved.dataset.last_period == "2025"

    assert resolved.metadata.subject.code == "pop"

    assert resolved.metadata.subject.label == "Population"

    assert resolved.metadata.id == ["region", "time", "ContentsCode"]

    assert resolved.metadata.role is not None

    assert resolved.metadata.role.model_dump(exclude_none=True) == {
        "geo": ["region"],
        "time": ["time"],
        "metric": ["ContentsCode"],
    }

    assert {
        key: value.model_dump(exclude_none=True, exclude_defaults=True)
        for key, value in resolved.metadata.dimension.items()
    } == {
        "region": {
            "label": "Region",
            "category": {
                "index": {"SE1": 0, "SE2": 1},
                "label": {"SE1": "North", "SE2": "South"},
            },
            "extension": {
                "elimination": True,
                "map": "NUTS-2024",
                "position": 0,
            },
        },
        "time": {
            "label": "Year",
            "category": {
                "index": {"2024": 0, "2025": 1},
                "label": {"2024": "2024", "2025": "2025"},
            },
            "extension": {"elimination": False, "position": 1},
        },
        "ContentsCode": {
            "label": "Contents",
            "category": {
                "index": {"OBS": 0},
                "label": {"OBS": "Population"},
            },
            "extension": {"elimination": False, "position": 2},
        },
    }

    assert resolved.metadata.extension == {
        "discovery_mode": "search_filter",
    }

    assert all("extension" not in dim.extension for dim in resolved.metadata.dimension.values())

    document = resolved.documents()["metadata"]

    assert "dimension_set" not in document

    assert "required" not in str(document)

    assert isinstance(document["paths"], list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variables, message",
    [
        (
            [
                {"code": "region", "values": ["A"], "valueTexts": ["A"]},
                {"code": "region", "values": ["B"], "valueTexts": ["B"]},
            ],
            "duplicate variable code",
        ),
        (
            [{"code": "region", "values": ["A", "A"], "valueTexts": ["A", "A"]}],
            "duplicate category id",
        ),
    ],
)
async def test_pxweb_resolve_rejects_malformed_dimensions(
    variables: list[dict[str, Any]], message: str
) -> None:

    metadata_url = "https://pxdata.example/api/v1/en/StatFin/table.px"

    adapter = PxWebV1Adapter(
        provider=_provider(),
        language="en",
        request_manager=_FakeRequestManager(
            {metadata_url: {"title": "Bad", "variables": variables}}
        ),  # type: ignore[arg-type]
    )

    candidate = DatasetCandidate(
        provider_code="statfin_pxweb",
        dataset_code="table.px",
        language="en",
        metadata_url=metadata_url,
    )

    with pytest.raises(ValueError, match=message):
        await adapter.resolve_dataset(candidate)


def _enrichment_case(cell_limit=100000):

    url = "https://pxdata.example/api/v1/sv/StatFin/table.px"

    manager = _FakeRequestManager(
        {
            url: {
                "title": "Koko taulukko",
                "variables": [
                    {
                        "code": "area",
                        "text": "Alue",
                        "elimination": True,
                        "values": ["a", "b", "c"],
                        "valueTexts": ["A", "B", "C"],
                    },
                    {
                        "code": "time",
                        "text": "Vuosi",
                        "time": True,
                        "values": ["2020", "2021", "2022"],
                    },
                    {"code": "metric", "values": ["n"]},
                ],
            }
        }
    )

    manager.sample = {
        "class": "dataset",
        "version": "2.0",
        "id": ["area", "time", "metric"],
        "size": [2, 2, 1],
        "dimension": {
            "area": {
                "category": {"index": ["a", "b"], "coordinates": {"a": [1, 2]}},
                "note": ["Aluehuomautus"],
                "link": {"describedby": []},
                "extension": {"elimination": False, "map": "kartta"},
            },
            "time": {"category": {"index": ["2020", "2021"]}},
            "metric": {
                "category": {
                    "index": ["n"],
                    "unit": {"n": {"label": "henkilöä", "decimals": 0}},
                    "note": {"n": ["Mittari"]},
                }
            },
        },
        "label": "Sample title",
        "source": "Tilastokeskus",
        "note": ["Huomautus"],
        "value": [1, 2, 3, 4],
        "status": {"0": "x"},
        "extension": {
            "px": {
                "description": "Kuvaus",
                "official-statistics": True,
                "subject-code": "01",
                "subject-area": "Väestö",
                "decimals": 0,
                "heading": ["time"],
                "stub": ["area"],
                "contents": "Henkilöt",
            }
        },
    }

    adapter = PxWebV1Adapter(
        _provider().model_copy(
            update={
                "language": "sv",
                "config": {
                    **_provider().config,
                    "extension": {
                        **_provider().config.get("extension", {}),
                        "cell_limit": cell_limit,
                    },
                },
            }
        ),
        "sv",
        manager,
    )

    candidate = DatasetCandidate(
        provider_code="statfin_pxweb",
        dataset_code="table",
        language="sv",
        metadata_url=url,
    )

    return adapter, manager, candidate


async def test_enrichment_preserves_complete_get_metadata():

    adapter, manager, candidate = _enrichment_case()

    documents = (await adapter.resolve_dataset(candidate)).documents()
    result = documents["metadata"]

    assert manager.calls[-1] == {
        "url": candidate.metadata_url,
        "method": "POST",
        "json_data": {
            "query": [
                {"code": "area", "selection": {"filter": "item", "values": ["a", "b"]}},
                {
                    "code": "time",
                    "selection": {"filter": "item", "values": ["2020", "2021"]},
                },
                {"code": "metric", "selection": {"filter": "item", "values": ["n"]}},
            ],
            "response": {"format": "json-stat2"},
        },
    }

    assert documents["dataset"]["label"] == "Koko taulukko"

    assert documents["dataset"]["last_period"] == "2022"

    assert result["source"] == "Tilastokeskus"

    assert documents["dataset"]["description"] == "Kuvaus"

    assert result["official_statistics"] is True

    assert result["subject"]["label"] == "Väestö"

    assert result["note"] == ["Huomautus"]

    area = result["dimension"]["area"]

    assert area["category"]["index"] == {"a": 0, "b": 1, "c": 2}

    assert area["category"]["coordinates"] == {"a": [1.0, 2.0]}

    assert area["extension"]["elimination"] is True

    assert area["extension"]["map"] == "kartta"

    assert result["dimension"]["metric"]["category"]["unit"]["n"]["decimals"] == 0

    assert result["extension"]["px"]["decimals"] == 0

    assert not {"heading", "stub"} & result["extension"]["px"].keys()

    assert not {"value", "status", "size"} & result.keys()


async def test_enrichment_reduces_selection_in_reverse_order():

    adapter, manager, candidate = _enrichment_case(cell_limit=2)

    manager.sample["size"] = [2, 1, 1]

    manager.sample["dimension"]["time"]["category"]["index"] = ["2020"]

    await adapter.resolve_dataset(candidate)

    assert manager.calls[-1]["json_data"]["query"][1]["selection"]["values"] == ["2020"]


@pytest.mark.parametrize("change", ["dimension", "category", "size", "class", "extension", "unit"])
async def test_enrichment_rejects_malformed_response(change):

    adapter, manager, candidate = _enrichment_case()

    if change == "dimension":
        manager.sample["id"][0] = "unknown"

    elif change == "category":
        manager.sample["dimension"]["area"]["category"]["index"] = ["a", "unknown"]

    elif change == "unit":
        manager.sample["dimension"]["metric"]["category"]["unit"] = {"unknown": {"label": "x"}}

    else:
        manager.sample[change] = None

    with pytest.raises(ValueError):
        await adapter.resolve_dataset(candidate)


async def test_enrichment_optional_px_and_invalid_normalized_values():

    adapter, manager, candidate = _enrichment_case()

    manager.sample["extension"] = {"px": {"description": 4, "official-statistics": "unknown"}}

    candidate.description = "Fallback"

    result = await adapter.resolve_dataset(candidate)

    assert result.dataset.description == "Fallback"

    assert result.metadata.official_statistics is None

    del manager.sample["extension"]

    await adapter.resolve_dataset(candidate)


@pytest.mark.parametrize("status", [400, 403, 429, 500])
async def test_enrichment_http_failure(status):

    from nordicintel_harvest.core.errors import ExternalAPIError

    adapter, manager, candidate = _enrichment_case()

    manager.status = status

    with pytest.raises(ExternalAPIError if status >= 429 else ValueError):
        await adapter.resolve_dataset(candidate)


async def test_enrichment_propagates_cancellation():

    adapter, manager, candidate = _enrichment_case()

    manager.sample_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await adapter.resolve_dataset(candidate)


async def test_enrichment_matches_reordered_axes_by_id():
    adapter, manager, candidate = _enrichment_case()
    manager.sample["id"] = ["metric", "area", "time"]
    manager.sample["size"] = [1, 2, 2]
    result = await adapter.resolve_dataset(candidate)
    assert result.metadata.id == ["area", "time", "metric"]


@pytest.mark.parametrize("listed", [True, False])
async def test_enrichment_ignores_synthetic_contents_placeholder(listed):
    adapter, manager, candidate = _enrichment_case()
    manager.sample["dimension"]["ContentsCode"] = {"category": {"index": {"EliminatedValue": 0}}}
    if listed:
        manager.sample["id"].insert(0, "ContentsCode")
        manager.sample["size"].insert(0, 1)
    result = await adapter.resolve_dataset(candidate)
    assert result.metadata.id == ["area", "time", "metric"]
    assert set(result.metadata.dimension) == set(result.metadata.id)
    assert result.metadata.dimension["area"].category.index == {"a": 0, "b": 1, "c": 2}
    assert result.metadata.dimension["metric"].category.unit["n"].label == "henkilöä"


@pytest.mark.parametrize("multiple_contents", [False, True])
async def test_enrichment_maps_only_singleton_contents_placeholder(multiple_contents):
    adapter, manager, candidate = _enrichment_case(cell_limit=1)
    variable = manager.payloads[candidate.metadata_url]["variables"][-1]
    variable["code"] = "ContentsCode"
    if multiple_contents:
        variable["values"] = ["n", "other"]
    manager.sample["id"] = ["area", "time", "ContentsCode"]
    manager.sample["size"] = [1, 1, 1]
    manager.sample["dimension"].pop("metric")
    manager.sample["dimension"]["area"]["category"]["index"] = ["a"]
    manager.sample["dimension"]["time"]["category"]["index"] = ["2020"]
    manager.sample["dimension"]["ContentsCode"] = {
        "category": {
            "index": {"EliminatedValue": 0},
            "unit": {"EliminatedValue": {"label": "people"}},
            "note": {"EliminatedValue": ["A metric note"]},
        }
    }
    if multiple_contents:
        with pytest.raises(ValueError, match="category IDs"):
            await adapter.resolve_dataset(candidate)
        return
    result = await adapter.resolve_dataset(candidate)
    category = result.metadata.dimension["ContentsCode"].category
    assert category.index == {"n": 0}
    assert category.label == {"n": "n"}
    assert category.unit["n"].label == "people"
    assert category.note == {"n": ["A metric note"]}
    assert manager.calls[-1]["json_data"]["query"][-1]["selection"]["values"] == ["n"]


@pytest.mark.parametrize("change", ["extra_category", "wrong_size", "other_mismatch"])
async def test_enrichment_placeholder_does_not_hide_mismatches(change):
    adapter, manager, candidate = _enrichment_case()
    manager.sample["dimension"]["ContentsCode"] = {"category": {"index": {"EliminatedValue": 0}}}
    manager.sample["id"].insert(0, "ContentsCode")
    manager.sample["size"].insert(0, 1)
    if change == "extra_category":
        manager.sample["dimension"]["ContentsCode"]["category"]["index"]["other"] = 1
    elif change == "wrong_size":
        manager.sample["size"][0] = 2
    else:
        manager.sample["dimension"]["time"]["category"]["index"] = ["wrong", "2021"]
    with pytest.raises(ValueError):
        await adapter.resolve_dataset(candidate)


@pytest.mark.parametrize("kind", ["missing", "invalid_json", "extra_dimension"])
async def test_enrichment_rejects_missing_or_invalid_response(kind):
    from nordicintel_harvest.core.errors import ExternalAPIError

    adapter, manager, candidate = _enrichment_case()
    if kind == "extra_dimension":
        manager.sample["dimension"]["unexpected"] = {"category": {"index": ["x"]}}
    else:

        async def request(*_args, **_kwargs):
            if kind == "missing":
                return None
            return RequestResult(
                status=200, url=candidate.metadata_url, headers={}, body=b"not json"
            )

        manager.request = request
    with pytest.raises(ExternalAPIError if kind == "missing" else ValueError):
        await adapter.resolve_dataset(candidate)


@pytest.mark.parametrize("values", [None, []])
async def test_enrichment_does_not_sample_incomplete_get_categories(values):
    adapter, manager, candidate = _enrichment_case()
    manager.payloads[candidate.metadata_url]["variables"][0]["values"] = values
    with pytest.raises(ValueError, match="has no values"):
        await adapter.resolve_dataset(candidate)
    assert all(call["method"] == "GET" for call in manager.calls)
