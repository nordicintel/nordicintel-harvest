from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nordicintel_harvest.providers.adapters.kolada.parsing import (
    KpiGroup,
    extract_kpi_source,
    kpi_metadata,
    parse_kpi,
    parse_kpi_group,
)


@pytest.mark.parametrize(
    "description,title,expected,origin",
    [
        ("Källa: SCB. Metoden ändrades.", "Test", "SCB", "description"),
        ("Källa. Skolverket.", "Test", "Skolverket", "description"),
        ("Källa: A. Källa: SCB.", "Test", "SCB", "description"),
        ("Källor: SCB och SKR.", "Test", "SCB och SKR", "description"),
        (
            "Källa: SCB, bl.a. RKA. Metoden ändrades.",
            "Test",
            "SCB, bl.a. RKA",
            "description",
        ),
        (
            "Källa: A. Andersson, t.ex. undersökningen.",
            "Test",
            "A. Andersson, t.ex. undersökningen",
            "description",
        ),
        ("Källa: www.scb.se. Metod.", "Test", "www.scb.se", "description"),
        (None, "El från energikällor", "Kolada", "fallback"),
        ("Ingen producent", "Test Källa: SCB.", "SCB", "title"),
        ("Källa:", "Test Källa: SCB.", "SCB", "title"),
        ("Källa: SKR", "Test Källa: SCB", "SKR", "description"),
    ],
)
def test_source_boundaries(description, title, expected, origin):
    assert extract_kpi_source(description, title) == (expected, origin)


def test_catalogue_fields_and_lossless_description_normalization():
    raw = {
        "id": "A",
        "title": "Test (- 2024)",
        "description": "  Text.\\nMetod.\u00a0\t Källa: SCB.  ",
        "operating_area": "Hälso- och sjukvård",
        "perspective": "Resurser",
        "auspice": "T",
        "has_ou_data": True,
        "is_divided_by_gender": True,
        "municipality_type": "K",
        "publ_period": "2027",
        "publication_date": "2027-01-01",
        "prel_publication_date": "2026-12-01",
    }
    metadata = kpi_metadata(parse_kpi(raw), year=2026)
    assert metadata["label"] == raw["title"]
    assert metadata["description"] == "Text. Metod. Källa: SCB."
    assert metadata["source"] == "SCB"
    extra = metadata["extension"]["kolada"]
    assert extra["raw_description"] == raw["description"]
    for key in (
        "operating_area",
        "perspective",
        "auspice",
        "municipality_type",
        "has_ou_data",
        "is_divided_by_gender",
        "publ_period",
        "publication_date",
        "prel_publication_date",
    ):
        assert extra[key] == raw[key]
    assert "updated" not in metadata
    assert metadata["subject_label"] == raw["operating_area"]
    assert metadata["subject_code"].startswith("kolada:operating_area:")
    assert metadata["paths"][0].path[-1].code == metadata["subject_code"]


@pytest.mark.parametrize(
    "title,year,retired",
    [
        ("Test (-2024)", 2024, True),
        ("Test ( - 2024 )", 2026, True),
        ("Test (-2027)", 2026, False),
        ("Test (1998-)", 2026, False),
        ("Test", 2026, False),
    ],
)
def test_retirement_marker(title, year, retired):
    metadata = kpi_metadata(parse_kpi({"id": "A", "title": title}), year=year)
    assert metadata["discontinued"] is retired
    if "2027" in title:
        assert metadata["extension"]["kolada"]["discontinued_year"] == 2027


def test_paths_preserve_enriched_group_order_and_titles():
    kpi = parse_kpi({"id": "A", "title": "Test", "operating_area": "Förskola"})
    a = KpiGroup("G1", "Grundskola - Hemkommun", ("A",))
    b = KpiGroup("G2", "Agenda 2030", ("A",))
    kpi = replace(kpi, groups=(b, a))
    first = kpi_metadata(kpi, year=2026)
    assert first == kpi_metadata(kpi, year=2030)
    assert [p.path[-1].label for p in first["paths"]] == [
        "Förskola",
        "Agenda 2030",
        "Grundskola - Hemkommun",
    ]
    assert "raw_description" not in first["extension"]["kolada"]
    other = kpi_metadata(parse_kpi({"operating_area": "Forskola"}), year=2026)
    assert other["subject_code"] != first["subject_code"]


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "G", "title": "Test", "members": "[]"},
        {"id": "G", "title": "Test", "members": [{}]},
        {"id": "", "title": "Test", "members": []},
    ],
)
def test_malformed_groups_raise(raw):
    with pytest.raises(ValueError):
        parse_kpi_group(raw)


def test_real_catalogue_source_regressions():
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures" / "source_cases.json").read_text(encoding="utf-8")
    )
    for case in fixtures:
        kpi = parse_kpi(case["kpi"])
        assert extract_kpi_source(kpi.description, kpi.title) == (
            case["source"],
            case["origin"],
        ), kpi.kpi_id
        assert kpi.description is not None
        assert "\\n" not in kpi.description
