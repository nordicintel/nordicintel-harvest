from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from nordicintel_harvest.core.errors import ExternalAPIError
from nordicintel_harvest.core.request_manager import RequestManager, RequestResult

MAX_NAVIGATION_FOLDERS = 10_000
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json",
    "User-Agent": "NordicIntel PXWeb v1 catalogue",
}


class _PxWebRequestError(ExternalAPIError):
    def __init__(self, message: str, *, url: str, status: int | None) -> None:
        self.status = status
        super().__init__(message, url=url)


@dataclass(frozen=True)
class PxWebCategory:
    """One category in a PXWeb table's navigation path."""

    id: str
    name: str


@dataclass(frozen=True)
class PxWebTable:
    """One unique table/path occurrence in a PXWeb catalogue."""

    id: str
    path: str
    title: str
    category_path: tuple[PxWebCategory, ...]
    api_url: str
    web_url: str | None
    published: str | None = None
    updated: str | None = None
    score: float | None = None


@dataclass
class _ExpansionForm:
    action: str
    fields: dict[str, str] = field(default_factory=dict)
    submit_name: str | None = None
    submit_value: str = ""


class _ExpansionFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_ExpansionForm] = []
        self._form: _ExpansionForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._form = _ExpansionForm(action=values.get("action") or "")
            return
        if tag != "input" or self._form is None:
            return
        name = values.get("name")
        if not name:
            return
        value = values.get("value") or ""
        if (values.get("type") or "text").casefold() == "hidden":
            self._form.fields[name] = value
        if "tableofcontent_action" in (values.get("class") or "").split():
            self._form.submit_name = name
            self._form.submit_value = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


@dataclass
class _TreeNode:
    kind: str
    text: list[str] = field(default_factory=list)
    href: str | None = None


@dataclass(frozen=True)
class _WebTable:
    table_id: str
    category_ids: tuple[str, ...]
    category_names: tuple[str, ...]
    title: str
    href: str


class _CategoryTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_WebTable] = []
        self._nodes: list[_TreeNode | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "li":
            classes = set((values.get("class") or "").split())
            kind = next(
                (
                    candidate
                    for candidate in ("Root", "Parent", "Leaf")
                    if f"AspNet-TreeView-{candidate}" in classes
                ),
                None,
            )
            self._nodes.append(_TreeNode(kind=kind) if kind else None)
            return
        if tag == "a" and self._nodes and self._nodes[-1] is not None:
            self._nodes[-1].href = values.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag != "li" or not self._nodes:
            return
        node = self._nodes.pop()
        if node is None or node.kind != "Leaf" or not node.href:
            return
        ancestors = [
            ancestor
            for ancestor in self._nodes
            if ancestor is not None and ancestor.kind in {"Root", "Parent"}
        ]
        names = tuple(_clean_text("".join(ancestor.text)) for ancestor in ancestors)
        table_id, category_ids = _ids_from_href(node.href, len(names))
        self.tables.append(
            _WebTable(
                table_id=table_id,
                category_ids=category_ids,
                category_names=names,
                title=_clean_text("".join(node.text)),
                href=node.href,
            )
        )

    def handle_data(self, data: str) -> None:
        if self._nodes and self._nodes[-1] is not None:
            self._nodes[-1].text.append(data)


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _strip_id_prefix(name: str, category_id: str) -> str:
    pattern = rf"^\s*{re.escape(category_id)}\s*[.:\-–—]\s*"
    return re.sub(pattern, "", name, count=1, flags=re.IGNORECASE).strip()


def _parse_path(path: str) -> tuple[str, ...]:
    return tuple(unquote(part).strip() for part in path.split("/") if part.strip())


def _path_string(path: tuple[str, ...]) -> str:
    return f"/{'/'.join(path)}" if path else "/"


def _ids_from_href(href: str, category_count: int) -> tuple[str, tuple[str, ...]]:
    segments = [unquote(part) for part in urlsplit(href).path.split("/") if part]
    if not segments:
        raise ValueError(f"Invalid PXWeb table link: {href!r}")
    tokens = [token for segment in segments[:-1] for token in segment.split("__") if token]
    if category_count > len(tokens):
        raise ValueError(f"Could not find category IDs in table link: {href!r}")
    categories = tuple(tokens[-category_count:]) if category_count else ()
    return segments[-1], categories


def _root_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _normalize_id(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", unquote(value))
    return "".join(
        character.casefold()
        for character in decomposed
        if not unicodedata.combining(character) and character.isalnum()
    )


def _api_language(url: str) -> str | None:
    parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    for index in range(len(parts) - 2):
        if parts[index].casefold() == "api" and parts[index + 1].casefold() == "v1":
            return parts[index + 2].casefold()
    return None


def _web_language(url: str) -> str | None:
    parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    for index in range(len(parts) - 2, -1, -1):
        if parts[index].casefold() == "pxweb":
            return parts[index + 1].casefold()
    return None


def _table_api_url(api_url: str, path: tuple[str, ...], table_id: str) -> str:
    suffix = "/".join(quote(part, safe="") for part in (*path, table_id))
    return f"{_root_url(api_url)}/{suffix}"


def _navigation_url(api_url: str, path: tuple[str, ...]) -> str:
    if not path:
        return _root_url(api_url)
    suffix = "/".join(quote(part, safe="") for part in path)
    return f"{_root_url(api_url)}/{suffix}"


def _table_web_url(web_url: str | None, path: tuple[str, ...], table_id: str) -> str | None:
    if web_url is None:
        return None
    root = _root_url(web_url)
    database_id = unquote(urlsplit(root).path.rstrip("/").split("/")[-1])
    route = "__".join(quote(part, safe="") for part in (database_id, *path))
    return f"{root}/{route}/{quote(table_id, safe='')}/"


async def _request(
    manager: RequestManager,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, str] | None = None,
    form_data: dict[str, Any] | None = None,
    referer: str | None = None,
) -> RequestResult:
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    response = await manager.request(
        url,
        method=method,
        params=params,
        headers=headers,
        form_data=form_data,
    )
    if response is None:
        raise _PxWebRequestError(f"PXWeb request failed: {url}", url=url, status=None)
    if not response.ok:
        raise _PxWebRequestError(
            f"PXWeb returned HTTP {response.status}: {url}",
            url=url,
            status=response.status,
        )
    return response


def _json_objects(response: RequestResult) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"PXWeb returned invalid JSON: {response.url}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise TypeError(f"PXWeb response is not a list of objects: {response.url}")
    return payload


def _parse_web_tables(html: str) -> list[_WebTable]:
    parser = _CategoryTreeParser()
    parser.feed(html)
    parser.close()
    return parser.tables


def _is_within_web_root(url: str, root_url: str) -> bool:
    candidate = urlsplit(url)
    root = urlsplit(root_url)
    root_path = unquote(root.path).rstrip("/") + "/"
    candidate_path = unquote(candidate.path)
    return (
        candidate.scheme.casefold() == root.scheme.casefold()
        and candidate.netloc.casefold() == root.netloc.casefold()
        and candidate_path.casefold().startswith(root_path.casefold())
    )


def _resolved_web_tables(tables: list[_WebTable], root_url: str) -> list[_WebTable]:
    output: list[_WebTable] = []
    for table in tables:
        href = urljoin(root_url, table.href)
        if not _is_within_web_root(href, root_url):
            continue
        output.append(
            _WebTable(
                table_id=table.table_id,
                category_ids=table.category_ids,
                category_names=table.category_names,
                title=table.title,
                href=href,
            )
        )
    return output


async def _fetch_web_tables(manager: RequestManager, web_url: str) -> list[_WebTable]:
    initial = await _request(manager, web_url)
    initial_tables = _parse_web_tables(initial.text)

    parser = _ExpansionFormParser()
    parser.feed(initial.text)
    parser.close()
    form = next((candidate for candidate in parser.forms if candidate.submit_name), None)
    if form is None or form.submit_name is None:
        return _resolved_web_tables(initial_tables, initial.url)

    fields: dict[str, Any] = dict(form.fields)
    fields[form.submit_name] = form.submit_value
    target = urljoin(initial.url, form.action) if form.action else initial.url
    expanded = await _request(
        manager,
        target,
        method="POST",
        form_data=fields,
        referer=initial.url,
    )
    return _resolved_web_tables(_parse_web_tables(expanded.text), expanded.url)


def _metadata_for_web_table(
    table: _WebTable,
    metadata_by_id: dict[str, list[dict[str, Any]]],
    path_metadata: dict[tuple[tuple[str, ...], str], dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = metadata_by_id.get(table.table_id.casefold(), [])
    exact = [
        candidate
        for candidate in candidates
        if _parse_path(str(candidate.get("path") or "")) == table.category_ids
    ]
    if exact:
        return exact[0]
    if candidates:
        return candidates[0]
    return path_metadata.get((table.category_ids, table.table_id.casefold()))


async def _fetch_web_only_metadata(
    request_manager: RequestManager,
    api_url: str,
    web_tables: list[_WebTable],
    search_items: list[dict[str, Any]],
    logger: logging.Logger,
) -> dict[tuple[tuple[str, ...], str], dict[str, Any]]:
    search_ids = {str(item.get("id") or "").casefold() for item in search_items if item.get("id")}
    tables_by_path: dict[tuple[str, ...], list[_WebTable]] = {}
    for table in web_tables:
        if table.table_id.casefold() not in search_ids:
            tables_by_path.setdefault(table.category_ids, []).append(table)

    metadata: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for path, tables in sorted(tables_by_path.items()):
        try:
            response = await _request(
                request_manager,
                _navigation_url(api_url, path),
            )
        except _PxWebRequestError as exc:
            if exc.status not in {400, 404}:
                raise
            logger.warning(
                "Ignoring unreachable PXWeb web-only branch path=%s status=%d",
                _path_string(path),
                exc.status,
            )
            continue
        navigation_by_id = {
            str(entry["id"]).casefold(): entry
            for entry in _json_objects(response)
            if entry.get("type") == "t" and isinstance(entry.get("id"), str)
        }
        for table in tables:
            entry = navigation_by_id.get(table.table_id.casefold())
            if entry is None:
                continue
            raw_name = entry.get("text")
            title = (
                _clean_text(raw_name)
                if isinstance(raw_name, str)
                else table.title or table.table_id
            )
            raw_updated = entry.get("updated")
            updated = str(raw_updated).strip() if raw_updated is not None else None
            metadata[(path, table.table_id.casefold())] = {
                "id": table.table_id,
                "path": _path_string(path),
                "title": title,
                "published": updated,
                "updated": updated,
                "score": 1.0,
            }
    return metadata


def _match_folder(entries: list[dict[str, Any]], category_id: str) -> dict[str, Any] | None:
    folders = [
        entry for entry in entries if entry.get("type") == "l" and isinstance(entry.get("id"), str)
    ]
    for matches in (
        [entry for entry in folders if entry["id"] == category_id],
        [entry for entry in folders if entry["id"].casefold() == category_id.casefold()],
        [entry for entry in folders if _normalize_id(entry["id"]) == _normalize_id(category_id)],
    ):
        if len(matches) == 1:
            return matches[0]
    return None


async def _resolve_api_category_paths(
    request_manager: RequestManager,
    api_url: str,
    paths: set[tuple[str, ...]],
) -> dict[tuple[str, ...], tuple[PxWebCategory, ...]]:
    cache: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    resolved: dict[tuple[str, ...], tuple[PxWebCategory, ...]] = {}
    for path in sorted(paths):
        actual_parent: tuple[str, ...] = ()
        categories: list[PxWebCategory] = []
        for category_id in path:
            if actual_parent not in cache:
                response = await _request(
                    request_manager,
                    _navigation_url(api_url, actual_parent),
                )
                cache[actual_parent] = _json_objects(response)
            entry = _match_folder(cache[actual_parent], category_id)
            if entry is None:
                break
            actual_id = str(entry["id"])
            raw_name = entry.get("text")
            name = _clean_text(raw_name) if isinstance(raw_name, str) else category_id
            categories.append(
                PxWebCategory(
                    id=category_id,
                    name=_strip_id_prefix(name, category_id) or category_id,
                )
            )
            actual_parent += (actual_id,)
        if len(categories) == len(path):
            resolved[path] = tuple(categories)
    return resolved


def _tables_from_web(
    api_url: str,
    web_tables: list[_WebTable],
    search_items: list[dict[str, Any]],
    path_metadata: dict[tuple[tuple[str, ...], str], dict[str, Any]],
    category_overrides: dict[tuple[str, ...], tuple[PxWebCategory, ...]] | None = None,
) -> list[PxWebTable]:
    category_overrides = category_overrides or {}
    metadata_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in search_items:
        table_id = item.get("id")
        path = item.get("path")
        if not isinstance(table_id, str) or not isinstance(path, str):
            raise TypeError("PXWeb search item has no string 'id' and 'path'")
        metadata_by_id.setdefault(table_id.casefold(), []).append(item)

    if not web_tables:
        raise ValueError("PXWeb web tree is empty")

    output: list[PxWebTable] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for table in web_tables:
        key = (table.category_ids, table.table_id.casefold())
        if key in seen:
            continue
        seen.add(key)
        metadata = _metadata_for_web_table(table, metadata_by_id, path_metadata)
        if metadata is None:
            continue
        names = table.category_names[-len(table.category_ids) :] if table.category_ids else ()
        if len(names) != len(table.category_ids):
            raise ValueError(f"Incomplete category labels for {table.table_id!r}")
        categories = category_overrides.get(table.category_ids) or tuple(
            PxWebCategory(
                id=category_id,
                name=_strip_id_prefix(name, category_id) or category_id,
            )
            for category_id, name in zip(table.category_ids, names, strict=True)
        )
        score = metadata.get("score")
        output.append(
            PxWebTable(
                id=table.table_id,
                path=_path_string(table.category_ids),
                title=str(metadata.get("title") or table.title or table.table_id).strip(),
                category_path=categories,
                api_url=_table_api_url(api_url, table.category_ids, table.table_id),
                web_url=table.href,
                published=(
                    str(metadata["published"]).strip()
                    if metadata.get("published") is not None
                    else None
                ),
                updated=(
                    str(metadata["updated"]).strip()
                    if metadata.get("updated") is not None
                    else None
                ),
                score=float(score) if isinstance(score, (int, float)) else None,
            )
        )
    if not output:
        raise ValueError("PXWeb web tree contained no reachable tables")
    return sorted(output, key=lambda item: (item.id.casefold(), item.path.casefold()))


async def _crawl_navigation(
    request_manager: RequestManager,
    *,
    api_url: str,
    web_url: str | None,
    logger: logging.Logger,
) -> list[PxWebTable]:
    pending: list[tuple[tuple[str, ...], tuple[PxWebCategory, ...]]] = [((), ())]
    seen_folders: set[tuple[str, ...]] = {()}
    seen_tables: set[tuple[tuple[str, ...], str]] = set()
    output: list[PxWebTable] = []

    while pending:
        path, categories = pending.pop(0)
        try:
            response = await _request(
                request_manager,
                _navigation_url(api_url, path),
            )
        except _PxWebRequestError as exc:
            if not path or exc.status not in {400, 404}:
                raise
            logger.warning(
                "Skipping unreachable PXWeb API navigation branch path=%s status=%d",
                _path_string(path),
                exc.status,
            )
            continue
        entries = _json_objects(response)
        for entry in entries:
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id.strip():
                continue
            raw_name = entry.get("text")
            name = _clean_text(raw_name) if isinstance(raw_name, str) else entry_id
            if entry.get("type") == "l":
                child_path = (*path, entry_id)
                if child_path in seen_folders:
                    continue
                if len(seen_folders) >= MAX_NAVIGATION_FOLDERS:
                    raise RuntimeError(
                        f"PXWeb navigation exceeded {MAX_NAVIGATION_FOLDERS} folders"
                    )
                seen_folders.add(child_path)
                pending.append(
                    (
                        child_path,
                        (
                            *categories,
                            PxWebCategory(
                                id=entry_id,
                                name=_strip_id_prefix(name, entry_id) or entry_id,
                            ),
                        ),
                    )
                )
                continue
            if entry.get("type") != "t":
                continue

            key = (path, entry_id.casefold())
            if key in seen_tables:
                continue
            seen_tables.add(key)
            raw_updated = entry.get("updated")
            updated = str(raw_updated).strip() if raw_updated is not None else None
            output.append(
                PxWebTable(
                    id=entry_id,
                    path=_path_string(path),
                    title=name or entry_id,
                    category_path=categories,
                    api_url=_table_api_url(api_url, path, entry_id),
                    web_url=_table_web_url(web_url, path, entry_id),
                    published=updated,
                    updated=updated,
                    score=1.0,
                )
            )

    if not output:
        raise ValueError("PXWeb navigation did not contain any tables")
    return sorted(output, key=lambda item: (item.id.casefold(), item.path.casefold()))


async def list_pxweb_tables(
    request_manager: RequestManager,
    *,
    api_url: str,
    web_url: str | None,
    logger: logging.Logger | None = None,
) -> list[PxWebTable]:
    """List complete PXWeb table/path occurrences with metadata and URLs."""

    resolved_logger = logger or logging.getLogger("pxweb_v1.catalog")
    try:
        search = await _request(
            request_manager,
            api_url,
            params={"filter": "*", "query": "*"},
        )
        search_items = _json_objects(search)
    except Exception:
        resolved_logger.warning(
            "PXWeb flat catalogue unavailable; walking navigation API",
            exc_info=True,
        )
        return await _crawl_navigation(
            request_manager,
            api_url=api_url,
            web_url=web_url,
            logger=resolved_logger,
        )

    if web_url is not None:
        try:
            web_tables = await _fetch_web_tables(request_manager, web_url)
            path_metadata = await _fetch_web_only_metadata(
                request_manager,
                api_url,
                web_tables,
                search_items,
                resolved_logger,
            )
            flat_ids = {
                str(item.get("id") or "").casefold() for item in search_items if item.get("id")
            }
            web_ids = {table.table_id.casefold() for table in web_tables}
            enriched_ids = {table_id for _path, table_id in path_metadata}
            flat_only_ids = flat_ids - web_ids
            web_only_ids = web_ids - flat_ids
            unresolved_web_ids = web_only_ids - enriched_ids
            if flat_only_ids or web_only_ids:
                resolved_logger.info(
                    "PXWeb catalogue reconciliation flat_only=%d web_only=%d enriched=%d unresolved=%d",
                    len(flat_only_ids),
                    len(web_only_ids),
                    len(web_only_ids & enriched_ids),
                    len(unresolved_web_ids),
                )
            if unresolved_web_ids:
                resolved_logger.warning(
                    "Ignoring %d web-only PXWeb table IDs absent from their API navigation folders",
                    len(unresolved_web_ids),
                )
            category_overrides: dict[tuple[str, ...], tuple[PxWebCategory, ...]] = {}
            api_language = _api_language(api_url)
            web_language = _web_language(web_url)
            if (
                api_language is not None
                and web_language is not None
                and api_language != web_language
            ):
                category_overrides = await _resolve_api_category_paths(
                    request_manager,
                    api_url,
                    {table.category_ids for table in web_tables},
                )
            return _tables_from_web(
                api_url,
                web_tables,
                search_items,
                path_metadata,
                category_overrides,
            )
        except Exception:
            resolved_logger.warning(
                "PXWeb web catalogue unavailable or incomplete; walking navigation API",
                exc_info=True,
            )

    return await _crawl_navigation(
        request_manager,
        api_url=api_url,
        web_url=web_url,
        logger=resolved_logger,
    )


__all__ = ["PxWebCategory", "PxWebTable", "list_pxweb_tables"]
