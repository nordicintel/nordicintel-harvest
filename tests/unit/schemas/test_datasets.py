from __future__ import annotations

import pytest
from pydantic import ValidationError

from nordicintel_harvest.schemas.datasets import DatasetMetadata


def _metadata(**overrides: object) -> DatasetMetadata:
    payload: dict[str, object] = {
        "identity": {"provider_code": "scb", "dataset_code": "A", "language": "sv"},
        "id": ["region", "time"],
        "role": {"geo": ["region"], "time": ["time"]},
        "dimension": {
            "region": {
                "label": "Region",
                "category": {
                    "index": {"00": 0},
                    "label": {"00": "Sweden"},
                },
                "extension": {"elimination": True},
            },
            "time": {
                "label": "Time",
                "category": {
                    "index": {"2024": 0},
                    "label": {"2024": "2024"},
                },
                "extension": {"elimination": False},
            },
        },
    }
    payload.update(overrides)
    return DatasetMetadata.model_validate(payload)


def test_storage_document_uses_json_stat2_shape_and_omits_modeled_nulls() -> None:
    metadata = _metadata(
        source=None,
        paths=[
            {
                "path": [
                    {
                        "code": "AM",
                        "label": "Arbetsmarknad",
                        "extension": {"sortCode": "010"},
                    }
                ]
            }
        ],
        contact=[{"name": "SCB", "mail": None}],
    )

    document = metadata.model_dump(mode="json", exclude_none=True)

    assert "dimension_set" not in document
    assert "required" not in str(document)
    assert document["paths"] == [
        {
            "path": [
                {
                    "code": "AM",
                    "label": "Arbetsmarknad",
                    "extension": {"sortCode": "010"},
                }
            ],
            "extension": {},
        }
    ]
    assert document["contact"] == [{"name": "SCB", "extension": {}}]
    assert "source" not in document
    assert "note" not in document["dimension"]["region"]
    assert "unit" not in document["dimension"]["region"]["category"]


def test_storage_document_preserves_explicit_null_in_opaque_extension() -> None:
    document = _metadata(extension={"provider": {"explicit_null": None}}).model_dump(
        mode="json", exclude_none=True
    )
    assert document["extension"]["provider"]["explicit_null"] is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": []}, "at least one"),
        ({"id": ["region", "region"]}, "must not contain duplicates"),
        ({"id": ["region"]}, "must exactly match"),
        ({"role": {"geo": ["ghost"]}}, "unknown dimension"),
        (
            {"role": {"geo": ["region"], "time": ["region"]}},
            "belongs to both",
        ),
        ({"role": {"geo": ["time", "region"]}}, "must follow id order"),
    ],
)
def test_metadata_rejects_invalid_dimension_correspondence(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _metadata(**overrides)


def test_metadata_round_trips_through_canonical_document() -> None:
    document = _metadata().model_dump(mode="json", exclude_none=True)
    assert (
        DatasetMetadata.model_validate(document).model_dump(mode="json", exclude_none=True)
        == document
    )
