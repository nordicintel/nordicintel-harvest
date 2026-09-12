from __future__ import annotations

import json
from typing import Any

import pytest

from nordicintel_harvest.core.request_manager import RequestResult
from nordicintel_harvest.providers.adapters.pxweb_v1.catalog import list_pxweb_tables

API_URL = "https://example.test/api/v1/en/Database"
WEB_URL = "https://example.test/pxweb/en/Database/"


class _FakeRequestManager:
    def __init__(self, responses: list[RequestResult]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

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
                "method": method,
                "params": params,
                "headers": headers,
                "json_data": json_data,
                "form_data": form_data,
                **kwargs,
            }
        )
        return self.responses.pop(0)


def _json_result(value: Any, *, url: str = API_URL, status: int = 200) -> RequestResult:
    return RequestResult(
        status=status,
        url=url,
        headers={"Content-Type": "application/json"},
        body=json.dumps(value).encode(),
        encoding="utf-8",
    )


def _html_result(value: str, *, url: str = WEB_URL, status: int = 200) -> RequestResult:
    return RequestResult(
        status=status,
        url=url,
        headers={"Content-Type": "text/html"},
        body=value.encode(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_list_pxweb_tables_preserves_every_web_path_for_one_table() -> None:
    initial_html = """
    <form action="/pxweb/en/Database/">
      <input type="hidden" name="__VIEWSTATE" value="state">
      <input class="tableofcontent_action" name="expand" value="Show all" type="submit">
    </form>
    """
    expanded_html = """
    <ul class="AspNet-TreeView-Root">
      <li class="AspNet-TreeView-Root"><span>First subject</span>
        <ul><li class="AspNet-TreeView-Leaf">
          <a href="/pxweb/en/Database/Database__first/table.px/">Table title</a>
        </li></ul>
      </li>
      <li class="AspNet-TreeView-Root"><span>Second subject</span>
        <ul><li class="AspNet-TreeView-Leaf">
          <a href="/pxweb/en/Database/Database__second/table.px/">Table title</a>
        </li></ul>
      </li>
    </ul>
    """
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "table.px",
                        "path": "/first",
                        "title": "Table title",
                        "score": 1.0,
                        "published": "2026-04-09T16:24:00",
                    }
                ]
            ),
            _html_result(initial_html),
            _html_result(expanded_html),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [table.path for table in tables] == ["/first", "/second"]
    assert [
        [(category.id, category.name) for category in table.category_path] for table in tables
    ] == [
        [("first", "First subject")],
        [("second", "Second subject")],
    ]
    assert all(table.id == "table.px" for table in tables)
    assert all(table.title == "Table title" for table in tables)
    assert all(table.published == "2026-04-09T16:24:00" for table in tables)
    assert [table.api_url for table in tables] == [
        f"{API_URL}/first/table.px",
        f"{API_URL}/second/table.px",
    ]
    assert [table.web_url for table in tables] == [
        "https://example.test/pxweb/en/Database/Database__first/table.px/",
        "https://example.test/pxweb/en/Database/Database__second/table.px/",
    ]
    assert manager.calls[0]["params"] == {"filter": "*", "query": "*"}
    assert manager.calls[2]["method"] == "POST"
    assert manager.calls[2]["form_data"] == {
        "__VIEWSTATE": "state",
        "expand": "Show all",
    }


@pytest.mark.asyncio
async def test_list_pxweb_tables_walks_api_when_web_tree_is_unavailable() -> None:
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "table.px",
                        "path": "/first",
                        "title": "Search title",
                        "published": "2026-04-08T12:00:00",
                    }
                ]
            ),
            _html_result("unavailable", status=503),
            _json_result(
                [
                    {"id": "first", "text": "First subject", "type": "l"},
                    {"id": "second", "text": "Second subject", "type": "l"},
                ]
            ),
            _json_result(
                [
                    {
                        "id": "table.px",
                        "text": "Navigation title",
                        "type": "t",
                        "updated": "2026-04-09T16:24:00",
                    }
                ],
                url=f"{API_URL}/first",
            ),
            _json_result(
                [
                    {
                        "id": "table.px",
                        "text": "Navigation title",
                        "type": "t",
                        "updated": "2026-04-09T16:24:00",
                    }
                ],
                url=f"{API_URL}/second",
            ),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [table.path for table in tables] == ["/first", "/second"]
    assert all(table.title == "Navigation title" for table in tables)
    assert all(table.updated == "2026-04-09T16:24:00" for table in tables)
    assert all(table.published == "2026-04-09T16:24:00" for table in tables)
    assert [table.web_url for table in tables] == [
        "https://example.test/pxweb/en/Database/Database__first/table.px/",
        "https://example.test/pxweb/en/Database/Database__second/table.px/",
    ]
    assert [call["url"] for call in manager.calls[2:]] == [
        API_URL,
        f"{API_URL}/first",
        f"{API_URL}/second",
    ]


@pytest.mark.asyncio
async def test_list_pxweb_tables_uses_api_labels_when_web_language_differs() -> None:
    finnish_web_url = "https://example.test/pxweb/fi/Database/"
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "table.px",
                        "path": "/first",
                        "title": "Table title",
                        "published": "2026-04-09T16:24:00",
                    }
                ]
            ),
            _html_result(
                """
                <ul><li class="AspNet-TreeView-Root"><span>Suomenkielinen nimi</span>
                  <ul><li class="AspNet-TreeView-Leaf">
                    <a href="/pxweb/fi/Database/Database__first/table.px/">Taulukko</a>
                  </li></ul>
                </li></ul>
                """,
                url=finnish_web_url,
            ),
            _json_result([{"id": "first", "text": "English name", "type": "l"}]),
        ]
    )

    tables = await list_pxweb_tables(
        manager,
        api_url=API_URL,
        web_url=finnish_web_url,
    )

    assert [(category.id, category.name) for category in tables[0].category_path] == [
        ("first", "English name")
    ]


@pytest.mark.asyncio
async def test_list_pxweb_tables_recognizes_language_after_nested_pxweb_segment() -> None:
    nested_web_url = "https://example.test/PxWeb/pxweb/en/Database/"
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "table.px",
                        "path": "/first",
                        "title": "Table title",
                        "published": "2026-04-09T16:24:00",
                    }
                ]
            ),
            _html_result(
                """
                <ul><li class="AspNet-TreeView-Root"><span>English name</span>
                  <ul><li class="AspNet-TreeView-Leaf">
                    <a href="/PxWeb/pxweb/en/Database/Database__first/table.px/">Table</a>
                  </li></ul>
                </li></ul>
                """,
                url=nested_web_url,
            ),
        ]
    )

    tables = await list_pxweb_tables(
        manager,
        api_url=API_URL,
        web_url=nested_web_url,
    )

    assert tables[0].category_path[0].name == "English name"
    assert len(manager.calls) == 2


@pytest.mark.asyncio
async def test_list_pxweb_tables_reconciles_stale_flat_and_current_web_entries() -> None:
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "current.px",
                        "path": "/first",
                        "title": "Current table",
                        "published": "2026-04-09T16:24:00",
                    },
                    {
                        "id": "stale.px",
                        "path": "/old",
                        "title": "Stale search hit",
                        "published": "2025-01-01T00:00:00",
                    },
                ]
            ),
            _html_result(
                """
                <ul>
                  <li class="AspNet-TreeView-Root"><span>First</span><ul>
                    <li class="AspNet-TreeView-Leaf">
                      <a href="/pxweb/en/Database/Database__first/current.px/">Current table</a>
                    </li>
                  </ul></li>
                  <li class="AspNet-TreeView-Root"><span>Second</span><ul>
                    <li class="AspNet-TreeView-Leaf">
                      <a href="/pxweb/en/Database/Database__second/new.px/">New web table</a>
                    </li>
                  </ul></li>
                </ul>
                """
            ),
            _json_result(
                [
                    {
                        "id": "new.px",
                        "text": "New navigation table",
                        "type": "t",
                        "updated": "2026-05-10T12:30:00",
                    }
                ],
                url=f"{API_URL}/second",
            ),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [table.id for table in tables] == ["current.px", "new.px"]
    assert tables[1].title == "New navigation table"
    assert tables[1].updated == "2026-05-10T12:30:00"
    assert tables[1].published == "2026-05-10T12:30:00"
    assert manager.calls[2]["url"] == f"{API_URL}/second"
    assert all("stale.px" not in call["url"] for call in manager.calls)
    assert len(manager.calls) == 3


@pytest.mark.asyncio
async def test_list_pxweb_tables_ignores_unreachable_web_only_branches() -> None:
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "current.px",
                        "path": "/current",
                        "title": "Current table",
                        "published": "2026-04-09T16:24:00",
                    }
                ]
            ),
            _html_result(
                """
                <ul>
                  <li class="AspNet-TreeView-Root"><span>Current</span><ul>
                    <li class="AspNet-TreeView-Leaf">
                      <a href="/pxweb/en/Database/Database__current/current.px/">Current table</a>
                    </li>
                  </ul></li>
                  <li class="AspNet-TreeView-Root"><span>Other database</span><ul>
                    <li class="AspNet-TreeView-Leaf">
                      <a href="/pxweb/en/Database/Database__other/foreign.px/">Foreign table</a>
                    </li>
                  </ul></li>
                </ul>
                """
            ),
            _json_result([], url=f"{API_URL}/other", status=400),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [table.id for table in tables] == ["current.px"]
    assert [call["url"] for call in manager.calls] == [
        API_URL,
        WEB_URL,
        f"{API_URL}/other",
    ]


@pytest.mark.asyncio
async def test_list_pxweb_tables_supports_extensionless_table_ids() -> None:
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "B72",
                        "path": "/B/B7",
                        "title": "Extensionless table",
                        "published": "2026-04-09T16:24:00",
                    }
                ]
            ),
            _html_result(
                """
                <ul>
                  <li class="AspNet-TreeView-Root"><span>Group B</span><ul>
                    <li class="AspNet-TreeView-Parent"><span>Group B7</span><ul>
                      <li class="AspNet-TreeView-Leaf">
                        <a href="/pxweb/en/Database/START__B__B7/B72/">Extensionless table</a>
                      </li>
                    </ul></li>
                  </ul></li>
                </ul>
                """
            ),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [(table.id, table.path) for table in tables] == [("B72", "/B/B7")]
    assert len(manager.calls) == 2


@pytest.mark.asyncio
async def test_list_pxweb_tables_ignores_links_outside_requested_web_database() -> None:
    manager = _FakeRequestManager(
        [
            _json_result(
                [
                    {
                        "id": "current.px",
                        "path": "/current",
                        "title": "Current table",
                        "published": "2026-04-09T16:24:00",
                    }
                ]
            ),
            _html_result(
                """
                <ul>
                  <li class="AspNet-TreeView-Root"><span>Current</span><ul>
                    <li class="AspNet-TreeView-Leaf">
                      <a href="/pxweb/en/Database/Database__current/current.px/">Current table</a>
                    </li>
                  </ul></li>
                  <li class="AspNet-TreeView-Root"><span>Other database</span><ul>
                    <li class="AspNet-TreeView-Leaf">
                      <a href="/pxweb/en/Other/Other__subject/">Other subject</a>
                    </li>
                  </ul></li>
                </ul>
                """
            ),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [table.id for table in tables] == ["current.px"]
    assert len(manager.calls) == 2


@pytest.mark.asyncio
async def test_list_pxweb_tables_api_walk_skips_unreachable_child_folders() -> None:
    manager = _FakeRequestManager(
        [
            _json_result([], status=503),
            _json_result(
                [
                    {"id": "bad", "text": "Broken branch", "type": "l"},
                    {"id": "good", "text": "Good branch", "type": "l"},
                ]
            ),
            _json_result([], url=f"{API_URL}/bad", status=400),
            _json_result(
                [
                    {
                        "id": "table.px",
                        "text": "Reachable table",
                        "type": "t",
                        "updated": "2026-04-09T16:24:00",
                    }
                ],
                url=f"{API_URL}/good",
            ),
        ]
    )

    tables = await list_pxweb_tables(manager, api_url=API_URL, web_url=WEB_URL)

    assert [(table.id, table.path) for table in tables] == [("table.px", "/good")]
