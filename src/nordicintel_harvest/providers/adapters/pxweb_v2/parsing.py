from __future__ import annotations

from typing import Any

from nordicintel_harvest.providers.adapters.common import (
    coerce_bool,
    normalize_note,
    parse_dt,
    structured_extras,
)
from nordicintel_harvest.schemas.contracts import encoded_url
from nordicintel_harvest.schemas.datasets import DatasetContact, DatasetPath
from nordicintel_harvest.schemas.dimensions import DatasetDimension, DatasetRole

_VALID_TIME_UNITS = {"Annual", "Quarterly", "Monthly", "Weekly", "Other"}
_DIMENSION_KEYS = {"label", "category", "note"}
_CATEGORY_KEYS = {"index", "label", "child", "coordinates", "unit", "note"}


def _normalize_path_item(item: Any, fallback_id: str | None = None) -> dict[str, Any] | None:
    if isinstance(item, dict):
        item_id = next(
            (
                str(item[key]).strip()
                for key in ("id", "code", "key", "value", "name", "label", "title")
                if item.get(key) is not None and str(item[key]).strip()
            ),
            fallback_id,
        )
        if not item_id:
            return None
        label = next(
            (
                str(item[key]).strip()
                for key in ("label", "title", "name", "text")
                if item.get(key) is not None and str(item[key]).strip()
            ),
            item_id,
        )
        extras = {
            key: value
            for key, value in item.items()
            if key not in {"id", "code", "key", "value", "name", "label", "title", "text"}
        }
        return {"code": item_id, "label": label, "extension": extras}

    if item is None:
        return None

    item_id = str(item).strip()
    if not item_id:
        return None
    return {"code": item_id, "label": item_id}


def _normalize_path_sequence(raw_items: list[Any]) -> dict[str, Any] | None:
    path = [
        normalized
        for index, item in enumerate(raw_items)
        if (normalized := _normalize_path_item(item, fallback_id=str(index))) is not None
    ]
    return {"path": path} if path else None


def normalize_paths(raw_paths: Any) -> list[DatasetPath] | None:
    if raw_paths is None:
        return None

    if isinstance(raw_paths, dict) and isinstance(raw_paths.get("paths"), list):
        normalized_paths = []
        for raw_path in raw_paths["paths"]:
            if isinstance(raw_path, dict) and isinstance(raw_path.get("path"), list):
                normalized = _normalize_path_sequence(raw_path["path"])
            elif isinstance(raw_path, list):
                normalized = _normalize_path_sequence(raw_path)
            else:
                normalized = None
            if normalized is not None:
                normalized_paths.append(normalized)
        return [DatasetPath.model_validate(path) for path in normalized_paths] or None

    if isinstance(raw_paths, dict) and isinstance(raw_paths.get("path"), list):
        normalized = _normalize_path_sequence(raw_paths["path"])
        return [DatasetPath.model_validate(normalized)] if normalized is not None else None

    if isinstance(raw_paths, list):
        if raw_paths and all(
            isinstance(item, dict) and isinstance(item.get("path"), list) for item in raw_paths
        ):
            normalized_paths = [
                normalized
                for item in raw_paths
                if (normalized := _normalize_path_sequence(item["path"])) is not None
            ]
            return [DatasetPath.model_validate(path) for path in normalized_paths] or None

        if raw_paths and all(isinstance(item, list) for item in raw_paths):
            normalized_paths = [
                normalized
                for item in raw_paths
                if (normalized := _normalize_path_sequence(item)) is not None
            ]
            return [DatasetPath.model_validate(path) for path in normalized_paths] or None

        normalized = _normalize_path_sequence(raw_paths)
        return [DatasetPath.model_validate(normalized)] if normalized is not None else None

    if isinstance(raw_paths, dict):
        if {"id", "label"} & set(raw_paths):
            normalized = _normalize_path_sequence([raw_paths])
            return [DatasetPath.model_validate(normalized)] if normalized is not None else None

        raw_items = []
        for key, value in raw_paths.items():
            if isinstance(value, dict):
                raw_items.append({"id": value.get("id", key), **value})
            else:
                raw_items.append({"id": key, "label": value})
        normalized = _normalize_path_sequence(raw_items)
        return [DatasetPath.model_validate(normalized)] if normalized is not None else None

    normalized = _normalize_path_sequence([raw_paths])
    return [DatasetPath.model_validate(normalized)] if normalized is not None else None


def normalize_contact(raw_contact: Any) -> list[DatasetContact] | None:
    if raw_contact is None:
        return None
    if isinstance(raw_contact, dict):
        raw_contact = raw_contact.get("contacts", [raw_contact])
    if isinstance(raw_contact, list):
        contacts = [
            DatasetContact.model_validate(
                structured_extras(item, {"name", "organization", "mail", "phone", "raw"})
            )
            for item in raw_contact
            if isinstance(item, dict)
        ]
        contacts.extend(
            DatasetContact(raw=value)
            for item in raw_contact
            if not isinstance(item, dict) and (value := str(item).strip())
        )
        return contacts or None

    value = str(raw_contact).strip()
    return [DatasetContact(raw=value)] if value else None


def normalize_role(raw_role: Any) -> DatasetRole | None:
    """Validate and retain JSON-stat2 role buckets in provider order."""
    if not isinstance(raw_role, dict):
        return None

    roles: dict[str, list[str]] = {}
    assigned: dict[str, str] = {}
    for key in ("time", "geo", "metric"):
        values = raw_role.get(key)
        if not isinstance(values, list):
            continue
        normalized: list[str] = []
        for value in values:
            dimension_id = str(value).strip()
            if not dimension_id:
                continue
            existing = assigned.get(dimension_id)
            if existing is not None and existing != key:
                raise ValueError(
                    f"pxweb2 payload role assigns dimension {dimension_id!r} to "
                    f"both {existing!r} and {key!r}"
                )
            assigned[dimension_id] = key
            normalized.append(dimension_id)
        if normalized:
            roles[key] = normalized
    return DatasetRole.model_validate(roles) if roles else None


def _build_category(dimension_id: str, dim_data: dict[str, Any]) -> dict[str, Any]:
    raw_category = dim_data.get("category")
    category = raw_category if isinstance(raw_category, dict) else {}

    raw_index = category.get("index")
    if isinstance(raw_index, dict):
        index_map = {str(key): int(value) for key, value in raw_index.items()}
    elif isinstance(raw_index, list):
        index_map = {str(value): ordinal for ordinal, value in enumerate(raw_index)}
    else:
        raw_labels = category.get("label")
        index_map = (
            {str(key): ordinal for ordinal, key in enumerate(raw_labels.keys())}
            if isinstance(raw_labels, dict)
            else {}
        )

    if not index_map:
        raise ValueError(f"pxweb2 dimension {dimension_id!r} has no usable category index")

    raw_labels = category.get("label")
    if isinstance(raw_labels, dict):
        label_map = {
            category_id: str(raw_labels.get(category_id, category_id)) for category_id in index_map
        }
    else:
        label_map = {category_id: category_id for category_id in index_map}

    normalized_category: dict[str, Any] = {"index": index_map, "label": label_map}
    for key in ("child", "coordinates", "unit", "note"):
        value = category.get(key)
        if isinstance(value, dict) and value:
            normalized_category[key] = (
                {
                    k: structured_extras(v, {"label", "symbol", "position", "decimals"})
                    for k, v in value.items()
                }
                if key == "unit"
                else value
            )
    return normalized_category


def _build_dimension_extension(dim_data: dict[str, Any]) -> dict[str, Any]:
    """Return one flat dimension extension with an elimination boolean."""
    raw_category = dim_data.get("category")
    category = raw_category if isinstance(raw_category, dict) else {}
    raw_extension = dim_data.get("extension")
    extras = dict(raw_extension) if isinstance(raw_extension, dict) else {}
    if "extension" in extras:
        raise ValueError("pxweb2 dimension extension contains nested extension")

    raw_elimination = extras.get("elimination", dim_data.get("elimination"))
    elimination = coerce_bool(raw_elimination)
    extras["elimination"] = elimination if elimination is not None else False

    for key, value in dim_data.items():
        if key not in _DIMENSION_KEYS | {"extension", "link", "elimination"}:
            extras[key] = value
    category_extras = {key: value for key, value in category.items() if key not in _CATEGORY_KEYS}
    if category_extras:
        extras["category"] = category_extras
    return extras


def build_dimensions(
    raw_dimensions: Any,
    raw_dimension_ids: Any,
    raw_role: Any = None,
) -> tuple[list[str], DatasetRole | None, dict[str, DatasetDimension]]:
    """Build canonical JSON-stat2 dimension fields for one metadata payload.

    ``payload["id"]`` is the authoritative axis order. Every id it declares must
    resolve to exactly one usable dimension.
    """
    if not isinstance(raw_dimensions, dict):
        raise ValueError("pxweb2 metadata payload has no dimension object")

    if not isinstance(raw_dimension_ids, list):
        raise ValueError("pxweb2 metadata payload has no ordered id list")
    dimension_ids = [str(item).strip() for item in raw_dimension_ids]
    if not dimension_ids or any(not item for item in dimension_ids):
        raise ValueError("pxweb2 metadata payload declares no dimension ids")
    if len(dimension_ids) != len(set(dimension_ids)):
        raise ValueError("pxweb2 metadata payload contains duplicate dimension ids")
    if set(raw_dimensions) != set(dimension_ids):
        raise ValueError("pxweb2 id must exactly match dimension keys")

    roles = normalize_role(raw_role)
    role_ids = {
        value
        for role_name in ("time", "geo", "metric")
        for value in (getattr(roles, role_name) or [] if roles is not None else [])
    }
    unknown_role_ids = sorted(role_ids - set(dimension_ids))
    if unknown_role_ids:
        raise ValueError(f"pxweb2 payload role names unknown dimensions: {unknown_role_ids}")

    dimensions: dict[str, DatasetDimension] = {}
    for dimension_id in dimension_ids:
        dim_data = raw_dimensions.get(dimension_id)
        if not isinstance(dim_data, dict):
            raise ValueError(
                f"pxweb2 dimension {dimension_id!r} is declared in id but missing "
                "from the dimension object"
            )

        dimensions[dimension_id] = DatasetDimension(
            label=str(dim_data.get("label") or dimension_id),
            category=_build_category(dimension_id, dim_data),
            note=normalize_note(dim_data.get("note")),
            extension=_build_dimension_extension(dim_data),
            link=dim_data.get("link") if isinstance(dim_data.get("link"), dict) else None,
        )
    return dimension_ids, roles, dimensions


def parse_pxweb2_discovery_table(
    table: dict[str, Any],
    *,
    base_api_url: str,
    base_web_url: str | None,
    language: str,
) -> dict[str, Any] | None:
    dataset_code = str(table.get("id") or "").strip()
    if not dataset_code:
        return None

    raw_time_unit = table.get("timeUnit")
    time_unit = (
        raw_time_unit
        if isinstance(raw_time_unit, str) and raw_time_unit in _VALID_TIME_UNITS
        else "Other"
    )

    raw_variable_names = table.get("variableNames")
    variable_names = (
        [str(v) for v in raw_variable_names if v is not None]
        if isinstance(raw_variable_names, list)
        else []
    )

    description = table.get("description")
    if isinstance(description, str) and not description.strip():
        description = None
    if description is not None and not isinstance(description, str):
        description = str(description)

    cleaned_api_url = base_api_url.rstrip("/")
    metadata_url = encoded_url(f"{cleaned_api_url}/tables/{dataset_code}/metadata?lang={language}")
    data_url = f"{cleaned_api_url}/tables/{dataset_code}/data?lang={language}"
    web_url = (
        (
            f"{base_web_url.rstrip('/')}/{dataset_code}/"
            if base_web_url.rstrip("/").endswith("/statbank/table")
            else f"{base_web_url.rstrip('/')}/{language}/table/{dataset_code}"
        )
        if base_web_url
        else None
    )

    return {
        "dataset_code": dataset_code,
        "label": str(table.get("label") or table.get("title") or dataset_code),
        "updated": parse_dt(table.get("updated")),
        "time_unit": time_unit,
        "first_period": str(table.get("firstPeriod") or ""),
        "last_period": str(table.get("lastPeriod") or ""),
        "description": description,
        "source": str(table.get("source")) if table.get("source") is not None else None,
        "note": normalize_note(table.get("note")),
        "subject_code": (
            str(table.get("subjectCode")) if table.get("subjectCode") is not None else None
        ),
        "paths": normalize_paths(table.get("paths")),
        "discontinued": coerce_bool(table.get("discontinued")),
        "metadata_url": metadata_url,
        "data_url": data_url,
        "web_url": web_url,
        "extension": {
            "links": table.get("links"),
            "variable_names": variable_names,
        },
    }


def parse_pxweb2_metadata_payload(
    payload: dict[str, Any],
    *,
    default_note: list[str] | None,
    default_subject_label: str | None,
    default_official_statistics: bool | None,
    default_contact: list[DatasetContact] | None,
    default_extension: dict[str, Any],
) -> dict[str, Any]:
    dimension_ids, role, dimensions = build_dimensions(
        payload.get("dimension"),
        payload.get("id"),
        payload.get("role"),
    )

    extension = dict(default_extension)
    note = normalize_note(payload.get("note")) or default_note
    subject_label = default_subject_label
    official_statistics = default_official_statistics
    contact = default_contact

    raw_extension = payload.get("extension")
    if isinstance(raw_extension, dict):
        extension.update({key: value for key, value in raw_extension.items() if key != "contact"})
        px_metadata = raw_extension.get("px")
        if isinstance(px_metadata, dict):
            subject_label = subject_label or (
                str(px_metadata.get("subject-area"))
                if px_metadata.get("subject-area") is not None
                else None
            )
            official_statistics = (
                coerce_bool(px_metadata.get("official-statistics"))
                if px_metadata.get("official-statistics") is not None
                else official_statistics
            )

        normalized_contact = normalize_contact(raw_extension.get("contact"))
        if normalized_contact is not None:
            contact = normalized_contact

    return {
        "id": dimension_ids,
        "role": role,
        "dimension": dimensions,
        "note": note,
        "subject_label": subject_label,
        "official_statistics": official_statistics,
        "contact": contact,
        "extension": extension,
    }
