"""Shared helpers for the live adapter test suite."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


def _as_plain_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        converted = to_jsonable(value)
        return converted if isinstance(converted, dict) else None
    if hasattr(value, "model_dump") and callable(value.model_dump):
        dumped = value.model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else None
    return None


def assert_dimension_contract(attributes: Any, *, optional: bool = False) -> None:
    """Assert live metadata carries a coordinated JSON-stat2 dimension shape."""
    dimension_ids = getattr(attributes, "id", None)
    dimensions = getattr(attributes, "dimension", None)
    if dimension_ids is None or dimensions is None:
        assert optional, "resolved metadata must carry dimensions"
        return

    assert dimension_ids
    assert set(dimension_ids) == set(dimensions)
    for dimension_id in dimension_ids:
        dimension = dimensions[dimension_id]
        assert dimension.label
        assert dimension.category.index
        assert set(dimension.category.index) == set(dimension.category.label)
        assert isinstance(dimension.extension.get("elimination"), bool)


def assert_discovered_dataset_contract(obj: Any) -> None:
    """Assert the live discovered object matches the canonical metadata contract."""
    extension = _as_plain_dict(getattr(obj, "extension", None))
    assert isinstance(extension, dict)
    assert not hasattr(obj, "nordicintel_extension")
    assert not hasattr(obj, "provider_extension")

    paths = getattr(obj, "paths", None)
    if paths is not None:
        assert isinstance(paths, list)
        for path_value in paths:
            path_obj = _as_plain_dict(path_value)
            assert isinstance(path_obj, dict)
            assert isinstance(path_obj.get("path"), list)
            for item in path_obj["path"]:
                assert isinstance(item, dict)
                assert item.get("code")
                assert item.get("label")

    assert_dimension_contract(obj, optional=True)


def assert_resolved_dataset_contract(obj: Any) -> None:
    """Assert the live resolved object matches the canonical metadata contract."""
    assert obj.dataset.identity == obj.metadata.identity
    attributes = obj.metadata
    attr_extension = _as_plain_dict(getattr(attributes, "extension", None))
    assert isinstance(attr_extension, dict)
    assert not hasattr(attributes, "nordicintel_extension")
    assert not hasattr(attributes, "provider_extension")

    assert_dimension_contract(attributes)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / datetimes to JSON-serialisable types.

    The ``not isinstance(obj, type)`` guard ensures we only call
    ``dataclasses.asdict`` on *instances*, not on the dataclass *class*
    itself — ``dataclasses.is_dataclass`` returns True for both.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            return to_jsonable(obj.model_dump(mode="json", exclude_none=True))
        except Exception:  # pragma: no cover - defensive fallback
            pass
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def save_json(path: Any, data: Any) -> None:
    """Serialise *data* and write it to *path* (always overwritten)."""
    from pathlib import Path

    p = Path(path)
    p.write_text(
        json.dumps(to_jsonable(data), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Saved output to %s", p)


async def collect_with_timeout(
    gen: AsyncIterator[T],
    max_items: int,
    timeout: float,
    label: str = "items",
) -> tuple[list[T], bool]:
    """Drain *gen* collecting up to *max_items*, respecting a kill-switch timeout.

    Returns ``(items, timed_out)``.  When *timed_out* is True the caller should
    log a warning and skip rather than fail.
    """
    items: list[T] = []

    async def _collect() -> None:
        async for item in gen:
            items.append(item)
            logger.debug("Collected %s #%d", label, len(items))
            if len(items) >= max_items:
                break

    timed_out = False
    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        logger.warning(
            "Kill switch: collection of %s timed out after %.0f s (%d items collected so far)",
            label,
            timeout,
            len(items),
        )
    return items, timed_out
