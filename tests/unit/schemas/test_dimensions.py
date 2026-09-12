from __future__ import annotations

import pytest
from pydantic import ValidationError

from nordicintel_harvest.schemas.dimensions import (
    DatasetDimension,
    DatasetRole,
    DimensionCategory,
)


def _category(*ids: str) -> dict[str, object]:
    return {
        "index": {value: ordinal for ordinal, value in enumerate(ids)},
        "label": {value: value.upper() for value in ids},
    }


def test_dimension_leaves_elimination_unknown() -> None:
    dimension = DatasetDimension(label="Region", category=_category("a", "b"))
    assert dimension.extension.get("elimination") is None


def test_dimension_preserves_extension_and_link() -> None:
    dimension = DatasetDimension(
        label="Region",
        category=_category("a"),
        extension={"elimination": True, "show": "code_value"},
        link={"describedby": [{"extension": {"a": "urn:example:a"}}]},
    )
    assert dimension.extension == {
        "elimination": True,
        "show": "code_value",
    }
    assert dimension.link == {"describedby": [{"extension": {"a": "urn:example:a"}}]}


def test_dimension_extension_allows_arbitrary_nested_json() -> None:
    value = DatasetDimension(
        label="A",
        category=_category("a"),
        extension={"extension": {"nested": True}, "unknown": None},
    )
    assert value.model_dump(exclude_none=True)["extension"] == {
        "extension": {"nested": True},
        "unknown": None,
    }


def test_category_preserves_provider_ordinals() -> None:
    category = DimensionCategory(
        index={"all": 0, "b": 1, "a": 2},
        label={"all": "All", "b": "B", "a": "A"},
    )
    assert list(category.index.items()) == [("all", 0), ("b", 1), ("a", 2)]


def test_category_rejects_non_dense_ordinals() -> None:
    with pytest.raises(ValidationError, match="dense zero-based sequence"):
        DimensionCategory(index={"a": 1}, label={"a": "A"})


def test_category_rejects_mismatched_labels() -> None:
    with pytest.raises(ValidationError, match="must exactly match"):
        DimensionCategory(index={"a": 0}, label={"b": "B"})


def test_role_rejects_duplicate_members() -> None:
    with pytest.raises(ValidationError, match="must not contain duplicate"):
        DatasetRole(time=["time", "time"])
