"""Sample-response enrichment, private to the PXWeb v1 adapter."""

from __future__ import annotations

from math import prod

from nordicintel_harvest.core.errors import ExternalAPIError
from nordicintel_harvest.core.request_manager import RequestManager
from nordicintel_harvest.providers.adapters.common import coerce_bool, structured_extras
from nordicintel_harvest.schemas.datasets import DatasetDocuments


async def enrich_metadata(
    resolved: DatasetDocuments, manager: RequestManager, cell_limit: int
) -> DatasetDocuments:
    metadata = resolved.metadata
    selection = {
        code: sorted(
            metadata.dimension[code].category.index,
            key=metadata.dimension[code].category.index.__getitem__,
        )[:2]
        for code in metadata.id
    }
    cells = prod(map(len, selection.values()))
    for code in reversed(metadata.id):
        if cells <= cell_limit:
            break
        cells //= len(selection[code])
        selection[code] = selection[code][:1]
    body = {
        "query": [
            {"code": code, "selection": {"filter": "item", "values": values}}
            for code, values in selection.items()
        ],
        "response": {"format": "json-stat2"},
    }
    assert metadata.metadata_url is not None
    response = await manager.request(metadata.metadata_url, method="POST", json_data=body)
    if response is None or not response.ok:
        status = response.status if response is not None else "no response"
        message = f"PXWeb JSON-stat2 enrichment failed: {status}"
        if response is None or response.status == 429 or response.status >= 500:
            raise ExternalAPIError(message, url=metadata.metadata_url)
        raise ValueError(message)
    sample = response.json()
    if (
        not isinstance(sample, dict)
        or sample.get("class") != "dataset"
        or sample.get("version") != "2.0"
    ):
        raise ValueError("PXWeb enrichment requires a JSON-stat2 dataset")
    ids = sample.get("id")
    dimensions = sample.get("dimension")
    sizes = sample.get("size")
    if (
        isinstance(ids, list)
        and all(isinstance(code, str) for code in ids)
        and len(ids) == len(set(ids))
        and isinstance(sizes, list)
        and len(sizes) == len(ids)
        and isinstance(dimensions, dict)
    ):
        contents = dimensions.get("ContentsCode")
        category = contents.get("category") if isinstance(contents, dict) else None
        placeholder = isinstance(category, dict) and category.get("index") in (
            {"EliminatedValue": 0},
            ["EliminatedValue"],
        )
        if placeholder and "ContentsCode" not in metadata.dimension:
            # PXWeb may synthesize a singleton metric, sometimes only in dimension.
            # Keep the selectable GET dimensions as the canonical structure.
            if "ContentsCode" in ids:
                position = ids.index("ContentsCode")
                if sizes[position] != 1:
                    raise ValueError("PXWeb synthetic ContentsCode must have size 1")
                ids = ids[:position] + ids[position + 1 :]
                sizes = sizes[:position] + sizes[position + 1 :]
            dimensions = {key: value for key, value in dimensions.items() if key != "ContentsCode"}
        elif placeholder and len(metadata.dimension["ContentsCode"].category.index) == 1:
            # Some exporters replace the sole real contents code with this marker.
            # Remap sampled extras, never the GET category identity or labels.
            real_code = next(iter(metadata.dimension["ContentsCode"].category.index))
            category = dict(category)
            category["index"] = {real_code: 0}
            for field in ("unit", "note", "coordinates"):
                values = category.get(field)
                if isinstance(values, dict):
                    if not set(values) <= {"EliminatedValue"}:
                        raise ValueError(f"PXWeb enrichment invalid category {field}")
                    category[field] = {real_code: values["EliminatedValue"]} if values else {}
            dimensions = {
                **dimensions,
                "ContentsCode": {**contents, "category": category},
            }
    if (
        not isinstance(ids, list)
        or any(not isinstance(code, str) for code in ids)
        or len(ids) != len(metadata.id)
        or set(ids) != set(metadata.id)
        or not isinstance(dimensions, dict)
        or set(dimensions) != set(metadata.id)
    ):
        raise ValueError("PXWeb enrichment dimension IDs do not match GET metadata")
    if sizes != [len(selection[code]) for code in ids]:
        raise ValueError("PXWeb enrichment size does not match selection")

    pair = resolved.documents()
    document = pair["metadata"]
    for code in ids:
        dim = dimensions[code]
        if not isinstance(dim, dict) or not isinstance(dim.get("category"), dict):
            raise ValueError(f"PXWeb enrichment has invalid dimension {code!r}")
        category = dim["category"]
        index = category.get("index")
        if isinstance(index, list):
            index = {key: ordinal for ordinal, key in enumerate(index)}
        if (
            not isinstance(index, dict)
            or set(index) != set(selection[code])
            or sorted(index.values()) != list(range(len(index)))
        ):
            raise ValueError(f"PXWeb enrichment category IDs do not match selection for {code!r}")
        target = document["dimension"][code]
        for field in ("note", "link"):
            if field in dim:
                target[field] = dim[field]
        extras = dim.get("extension", {})
        if not isinstance(extras, dict):
            raise ValueError("PXWeb dimension extension must be an object")
        target["extension"] = {
            **extras,
            **target["extension"],
        }
        for field in ("unit", "note", "coordinates"):
            if field in category:
                values = category[field]
                if not isinstance(values, dict) or not set(values) <= set(index):
                    raise ValueError(f"PXWeb enrichment invalid category {field}")
                target["category"][field] = (
                    {
                        k: structured_extras(v, {"label", "symbol", "position", "decimals"})
                        for k, v in values.items()
                    }
                    if field == "unit"
                    else values
                )

    extension = sample.get("extension", {})
    if not isinstance(extension, dict):
        raise ValueError("PXWeb dataset extension must be an object")
    px = extension.get("px", {})
    if not isinstance(px, dict):
        raise ValueError("PXWeb extension.px must be an object")
    fixed_px = {key: value for key, value in px.items() if key not in {"heading", "stub"}}
    if fixed_px:
        document["extension"]["px"] = fixed_px
    for target, source in (
        ("description", "description"),
        ("subject_code", "subject-code"),
        ("subject_label", "subject-area"),
    ):
        value = px.get(source)
        if isinstance(value, str) and value.strip():
            if target == "description":
                pair["dataset"][target] = value
            else:
                document.setdefault("subject", {})[target.removeprefix("subject_")] = value
    official = coerce_bool(px.get("official-statistics"))
    if official is not None:
        document["official_statistics"] = official
    if "source" in sample:
        document["source"] = sample["source"]
    if "note" in sample:
        document["note"] = sample["note"]
    return DatasetDocuments.model_validate(pair)
