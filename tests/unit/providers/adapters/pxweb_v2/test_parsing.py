from __future__ import annotations

import pytest

from nordicintel_harvest.providers.adapters.common import coerce_bool, normalize_note
from nordicintel_harvest.providers.adapters.pxweb_v2.parsing import (
    build_dimensions,
    normalize_contact,
    normalize_paths,
    normalize_role,
    parse_pxweb2_discovery_table,
    parse_pxweb2_metadata_payload,
)


def test_normalize_note_handles_supported_inputs() -> None:
    assert normalize_note(None) is None
    assert normalize_note("  note ") == ["note"]
    assert normalize_note([" a ", "", "b"]) == ["a", "b"]
    assert normalize_note(3) is None


def test_coerce_bool_handles_supported_inputs() -> None:
    assert coerce_bool(True) is True
    assert coerce_bool(0) is False
    assert coerce_bool("yes") is True
    assert coerce_bool("No") is False
    assert coerce_bool("unknown") is None


def test_normalize_paths_returns_flat_multiple_paths() -> None:
    paths = normalize_paths(
        [
            [{"id": "A", "label": "A", "sortCode": "2"}],
            [{"id": "B", "label": "B", "sortCode": "1"}],
        ]
    )
    assert paths is not None
    assert [path.model_dump(exclude_defaults=True) for path in paths] == [
        {"path": [{"code": "A", "label": "A", "extension": {"sortCode": "2"}}]},
        {"path": [{"code": "B", "label": "B", "extension": {"sortCode": "1"}}]},
    ]


def test_normalize_contact_returns_an_array() -> None:
    contacts = normalize_contact([{"name": "SCB", "mail": "info@scb.se"}, "free-form contact"])
    assert contacts is not None
    assert [
        contact.model_dump(exclude_none=True, exclude_defaults=True) for contact in contacts
    ] == [
        {"mail": "info@scb.se", "name": "SCB"},
        {"raw": "free-form contact"},
    ]


def test_parse_pxweb2_discovery_table_maps_fields() -> None:
    parsed = parse_pxweb2_discovery_table(
        {
            "id": "TAB1",
            "title": "Table 1",
            "updated": "2026-03-04T12:00:00Z",
            "timeUnit": "Monthly",
            "firstPeriod": "2020M01",
            "lastPeriod": "2026M02",
            "description": "Dataset",
            "source": "SCB",
            "note": ["n1"],
            "subjectCode": "BE",
            "paths": [[{"id": "BE", "label": "Population", "sortCode": "010"}]],
            "links": {"self": "x"},
            "variableNames": ["age", "sex"],
        },
        base_api_url="https://api.example.com",
        base_web_url="https://www.example.com",
        language="en",
    )

    assert parsed is not None
    assert parsed["dataset_code"] == "TAB1"
    assert parsed["time_unit"] == "Monthly"
    assert [path.model_dump(exclude_defaults=True) for path in parsed["paths"]] == [
        {"path": [{"code": "BE", "label": "Population", "extension": {"sortCode": "010"}}]}
    ]
    assert parsed["metadata_url"].endswith("/tables/TAB1/metadata?lang=en")
    assert parsed["data_url"].endswith("/tables/TAB1/data?lang=en")
    assert parsed["extension"]["variable_names"] == ["age", "sex"]


def _dimension(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "label": "Region",
        "category": {
            "index": {"02": 0, "01": 1},
            "label": {"02": "B", "01": "A"},
        },
        "extension": {"elimination": False, "show": "value"},
    }
    payload.update(overrides)
    return payload


def test_build_dimensions_preserves_ids_categories_roles_and_extensions() -> None:
    ids, role, dimensions = build_dimensions(
        {
            "time": _dimension(label="Time"),
            "region": _dimension(
                extension={
                    "elimination": True,
                    "show": "code_value",
                    "codelists": [],
                },
                link={"describedby": [{"extension": {"01": "urn:region:01"}}]},
            ),
        },
        ["region", "time"],
        {"geo": ["region"], "time": ["time"]},
    )

    assert ids == ["region", "time"]
    assert role is not None
    assert role.model_dump(exclude_none=True) == {
        "time": ["time"],
        "geo": ["region"],
    }
    assert list(dimensions) == ["region", "time"]
    assert list(dimensions["region"].category.index.items()) == [
        ("02", 0),
        ("01", 1),
    ]
    assert dimensions["region"].extension == {
        "elimination": True,
        "show": "code_value",
        "codelists": [],
    }
    assert dimensions["region"].link == {"describedby": [{"extension": {"01": "urn:region:01"}}]}


def test_build_dimensions_defaults_missing_elimination_to_false() -> None:
    ids, _role, dimensions = build_dimensions(
        {
            "region": {
                "label": "Region",
                "category": {"index": ["01"], "label": {"01": "A"}},
            }
        },
        ["region"],
    )
    assert ids == ["region"]
    assert dimensions["region"].extension.get("elimination") is False


@pytest.mark.parametrize(
    ("dimensions", "ids", "role", "message"),
    [
        (None, ["a"], None, "no dimension object"),
        ({"a": _dimension()}, None, None, "no ordered id list"),
        ({"a": _dimension()}, [], None, "declares no dimension ids"),
        ({"a": _dimension()}, ["a", "a"], None, "duplicate dimension ids"),
        ({"a": _dimension()}, ["a", "ghost"], None, "exactly match"),
        ({"a": _dimension()}, ["a"], {"time": ["ghost"]}, "unknown dimensions"),
    ],
)
def test_build_dimensions_fails_loudly(
    dimensions: object, ids: object, role: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_dimensions(dimensions, ids, role)


def test_build_dimensions_rejects_direct_nested_extension() -> None:
    with pytest.raises(ValueError, match="contains nested extension"):
        build_dimensions(
            {
                "a": _dimension(
                    extension={
                        "elimination": False,
                        "extension": {"show": "value"},
                    }
                )
            },
            ["a"],
        )


def test_normalize_role_rejects_a_dimension_in_two_roles() -> None:
    with pytest.raises(ValueError, match="both"):
        normalize_role({"geo": ["region"], "time": ["region"]})


def test_metadata_parser_promotes_contact_without_duplication() -> None:
    parsed = parse_pxweb2_metadata_payload(
        {
            "id": ["ContentsCode"],
            "role": {"metric": ["ContentsCode"]},
            "dimension": {
                "ContentsCode": {
                    "label": "tabellinnehåll",
                    "category": {
                        "index": {"AM0301AE": 0},
                        "label": {"AM0301AE": "Index"},
                        "unit": {
                            "AM0301AE": {
                                "base": "index",
                                "label": None,
                                "decimals": 1,
                            }
                        },
                    },
                    "extension": {"elimination": False, "show": "value"},
                }
            },
            "extension": {
                "contact": [{"name": "SCB", "mail": "info@scb.se"}],
                "px": {"subject-area": "Arbetsmarknad"},
            },
        },
        default_note=None,
        default_subject_label=None,
        default_official_statistics=None,
        default_contact=None,
        default_extension={"links": []},
    )

    assert parsed["id"] == ["ContentsCode"]
    assert parsed["dimension"]["ContentsCode"].extension.get("elimination") is False
    assert parsed["contact"][0].name == "SCB"
    assert "contact" not in parsed["extension"]
