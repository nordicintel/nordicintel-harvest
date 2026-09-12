"""Validated canonical JSON-stat2 dimension metadata."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DimensionRole = Literal["time", "geo", "metric"]

_ROLE_NAMES: tuple[DimensionRole, ...] = ("time", "geo", "metric")


def _require_non_empty_keys(mapping: dict[str, Any], field_name: str) -> None:
    if any(not key.strip() for key in mapping):
        raise ValueError(f"{field_name} keys must be non-empty strings")


class CategoryUnit(BaseModel):
    """Supported unit metadata for JSON-stat category entries."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    symbol: str | None = None
    position: Literal["start", "end"] | None = None
    decimals: int | None = Field(default=None, ge=0)
    extension: dict[str, Any] = Field(default_factory=dict)


class DimensionCategory(BaseModel):
    """Canonical subset of JSON-stat category metadata."""

    model_config = ConfigDict(extra="forbid")

    index: dict[str, int]
    label: dict[str, str]
    child: dict[str, list[str]] | None = None
    coordinates: dict[str, tuple[float, float]] | None = None
    unit: dict[str, CategoryUnit] | None = None
    note: dict[str, list[str]] | None = None
    extension: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_category(self) -> DimensionCategory:
        if not self.index:
            raise ValueError("dimensions category.index must not be empty")
        _require_non_empty_keys(self.index, "dimensions category.index")
        _require_non_empty_keys(self.label, "dimensions category.label")

        if set(self.index) != set(self.label):
            raise ValueError(
                "dimensions category.label keys must exactly match category.index keys"
            )

        values = list(self.index.values())
        if len(values) != len(set(values)):
            raise ValueError("dimensions category.index ordinal values must be unique")
        if sorted(values) != list(range(len(values))):
            raise ValueError(
                "dimensions category.index ordinal values must form a dense zero-based sequence"
            )

        for key, value in self.label.items():
            if not value.strip():
                raise ValueError(f"dimensions category.label[{key!r}] must be a non-empty string")

        valid_keys = set(self.index)

        if self.child is not None:
            for parent, children in self.child.items():
                if parent not in valid_keys:
                    raise ValueError(
                        f"dimensions category.child parent {parent!r} must exist in category.index"
                    )
                if len(children) != len(set(children)):
                    raise ValueError(
                        f"dimensions category.child[{parent!r}] contains duplicate child IDs"
                    )
                for child in children:
                    if child not in valid_keys:
                        raise ValueError(
                            f"dimensions category.child value {child!r} must exist in category.index"
                        )
                    if child == parent:
                        raise ValueError(
                            f"dimensions category.child[{parent!r}] cannot reference itself"
                        )

        if self.coordinates is not None:
            for key in self.coordinates:
                if key not in valid_keys:
                    raise ValueError(
                        f"dimensions category.coordinates key {key!r} must exist in category.index"
                    )

        if self.unit is not None:
            for key in self.unit:
                if key not in valid_keys:
                    raise ValueError(
                        f"dimensions category.unit key {key!r} must exist in category.index"
                    )

        if self.note is not None:
            for key in self.note:
                if key not in valid_keys:
                    raise ValueError(
                        f"dimensions category.note key {key!r} must exist in category.index"
                    )

        return self


class DatasetDimension(BaseModel):
    """Canonical subset of a JSON-stat dataset dimension entry."""

    model_config = ConfigDict(extra="forbid")

    label: str
    category: DimensionCategory
    note: list[str] | None = None
    extension: dict[str, Any] = Field(default_factory=dict)
    link: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_dimension(self) -> DatasetDimension:
        if not self.label.strip():
            raise ValueError("dimensions label must be a non-empty string")
        return self


class DatasetRole(BaseModel):
    """Canonical dataset-level JSON-stat role metadata."""

    model_config = ConfigDict(extra="forbid")

    time: list[str] | None = None
    geo: list[str] | None = None
    metric: list[str] | None = None

    @model_validator(mode="after")
    def validate_role(self) -> DatasetRole:
        for field_name in _ROLE_NAMES:
            values = getattr(self, field_name)
            if values is not None and len(values) != len(set(values)):
                raise ValueError(f"role.{field_name} must not contain duplicate dimension IDs")
        return self
