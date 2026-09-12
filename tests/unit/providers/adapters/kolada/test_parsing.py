from __future__ import annotations

from datetime import date

import pytest

from nordicintel_harvest.providers.adapters.kolada.parsing import (
    GENDER_LABELS,
    KpiInfo,
    MunicipalityInfo,
    OuInfo,
    build_data_request_batches,
    build_dataset_dimensions,
    build_gender_dimension,
    build_municipality_dimension,
    build_ou_dataset_dimensions,
    build_ou_dimension,
    build_ou_municipality_extension,
    build_year_dimension,
    candidate_municipality_ids,
    extract_presence,
    max_probe_year,
    parse_ou,
    probe_year_range,
)
from nordicintel_harvest.schemas.dimensions import DatasetRole


def _kpi(**overrides: object) -> KpiInfo:
    defaults: dict[str, object] = {
        "kpi_id": "N00001",
        "title": "Test KPI",
        "description": "A test KPI",
        "is_divided_by_gender": False,
        "municipality_type": "A",
        "publication_date": None,
        "prel_publication_date": None,
        "publ_period": None,
        "has_ou_data": False,
    }
    defaults.update(overrides)
    return KpiInfo(**defaults)  # type: ignore[arg-type]


def _municipalities() -> list[MunicipalityInfo]:
    return [
        MunicipalityInfo(municipality_id="0000", title="Riket", type="L"),
        MunicipalityInfo(municipality_id="0001", title="Region Stockholm", type="L"),
        MunicipalityInfo(municipality_id="0180", title="Stockholm", type="K"),
        MunicipalityInfo(municipality_id="1280", title="MalmÃ¶", type="K"),
    ]


def test_candidate_municipality_ids_k_adds_riket_even_though_it_is_type_l() -> None:
    assert set(candidate_municipality_ids("K", _municipalities())) == {
        "0180",
        "1280",
        "0000",
    }


def test_candidate_municipality_ids_l_is_already_riket_inclusive() -> None:
    assert set(candidate_municipality_ids("L", _municipalities())) == {"0000", "0001"}


def test_candidate_municipality_ids_a_returns_everything() -> None:
    assert set(candidate_municipality_ids("A", _municipalities())) == {
        "0000",
        "0001",
        "0180",
        "1280",
    }


def test_candidate_municipality_ids_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        candidate_municipality_ids("X", _municipalities())


def test_max_probe_year_is_three_years_beyond_today() -> None:
    assert max_probe_year(today=date(2026, 8, 18)) == 2029


def test_probe_year_range_is_full_history_on_first_resolve() -> None:
    assert probe_year_range(max_year=1972) == [
        1970,
        1971,
        1972,
    ]


def test_probe_year_range_empty_before_supported_history() -> None:
    assert probe_year_range(max_year=1969) == []


def test_build_data_request_batches_respects_the_25_value_cap() -> None:
    municipality_ids = [f"{i:04d}" for i in range(30)]
    years = list(range(2000, 2010))

    batches = build_data_request_batches(municipality_ids, years)

    assert len(batches) == 2
    assert [len(m) for m, _ in batches] == [25, 5]
    assert all(len(y) == len(years) for _, y in batches)


def test_extract_presence_ignores_entries_with_only_null_values() -> None:
    entries = [
        {
            "kpi": "N00001",
            "period": 2023,
            "municipality": "0180",
            "values": [
                {
                    "gender": "T",
                    "value": None,
                    "status": "",
                    "count": 1,
                    "isdeleted": False,
                }
            ],
        },
        {
            "kpi": "N00001",
            "period": 2024,
            "municipality": "0180",
            "values": [
                {
                    "gender": "T",
                    "value": 5.2,
                    "status": "",
                    "count": 1,
                    "isdeleted": False,
                }
            ],
        },
        {
            "kpi": "N00001",
            "period": 2024,
            "municipality": "1280",
            "values": [
                {
                    "gender": "M",
                    "value": None,
                    "status": "",
                    "count": 1,
                    "isdeleted": False,
                },
                {
                    "gender": "K",
                    "value": 3.1,
                    "status": "",
                    "count": 1,
                    "isdeleted": False,
                },
            ],
        },
    ]

    years, municipalities = extract_presence(entries)

    assert years == {2024}
    assert municipalities == {"0180", "1280"}


def test_build_year_dimension_orders_categories_by_year() -> None:
    dim = build_year_dimension({2024, 2020, 2022})
    assert dim.category.index == {"2020": 0, "2022": 1, "2024": 2}
    assert dim.category.label == {"2020": "2020", "2022": "2022", "2024": "2024"}
    assert dim.extension.get("elimination") is False


def test_build_municipality_dimension_uses_known_titles_and_falls_back_to_id() -> None:
    dim = build_municipality_dimension({"0180", "9999"}, {"0180": "Stockholm"})
    assert dim.category.label == {"0180": "Stockholm", "9999": "9999"}
    assert dim.extension.get("elimination") is False


def test_build_gender_dimension_is_always_the_same_three_categories() -> None:
    dim = build_gender_dimension()
    assert dim.category.label == GENDER_LABELS
    assert set(dim.category.index) == {"K", "M", "T"}
    assert dim.extension.get("elimination") is True


def test_build_dataset_dimensions_adds_gender_only_when_the_kpi_has_it() -> None:
    result = build_dataset_dimensions(
        kpi=_kpi(is_divided_by_gender=True),
        all_years={2023, 2024},
        all_municipality_ids={"0180"},
        municipality_titles={"0180": "Stockholm"},
    )

    assert result.id == ["municipality", "year", "gender"]
    assert {
        key: dimension.extension.get("elimination") for key, dimension in result.dimension.items()
    } == {"municipality": False, "year": False, "gender": True}
    assert result.first_period == "2023"
    assert result.last_period == "2024"
    assert result.role == DatasetRole(geo=["municipality"], time=["year"])


def test_build_dataset_dimensions_omits_gender_when_the_kpi_does_not_have_it() -> None:
    result = build_dataset_dimensions(
        kpi=_kpi(is_divided_by_gender=False),
        all_years={2024},
        all_municipality_ids={"0180"},
        municipality_titles={"0180": "Stockholm"},
    )
    assert result.id == ["municipality", "year"]
    assert "gender" not in result.dimension


def test_build_dataset_dimensions_rejects_a_kpi_with_no_confirmed_data() -> None:
    with pytest.raises(ValueError):
        build_dataset_dimensions(
            kpi=_kpi(),
            all_years=set(),
            all_municipality_ids=set(),
            municipality_titles={},
        )


def _ou_kpi(**overrides: object) -> KpiInfo:
    defaults: dict[str, object] = {
        "kpi_id": "N00001",
        "title": "Test OU KPI",
        "description": "A test OU KPI",
        "is_divided_by_gender": False,
        "municipality_type": "A",
        "publication_date": None,
        "prel_publication_date": None,
        "publ_period": None,
        "has_ou_data": True,
    }
    defaults.update(overrides)
    return KpiInfo(**defaults)  # type: ignore[arg-type]


def test_parse_ou_extracts_id_title_and_municipality() -> None:
    ou = parse_ou({"id": "V15E01", "title": "Storskolan", "municipality": "0180"})
    assert ou == OuInfo(ou_id="V15E01", title="Storskolan", municipality_id="0180")


def test_parse_ou_returns_none_when_id_or_municipality_is_missing() -> None:
    assert parse_ou({"title": "No id", "municipality": "0180"}) is None
    assert parse_ou({"id": "V15E01", "title": "No muni"}) is None


def test_extract_presence_defaults_to_municipality_key() -> None:
    entries = [
        {
            "period": 2024,
            "municipality": "0180",
            "values": [{"gender": "T", "value": 1.0}],
        }
    ]
    years, ids = extract_presence(entries)
    assert years == {2024}
    assert ids == {"0180"}


def test_extract_presence_with_ou_id_key_reads_the_ou_field() -> None:
    entries = [
        {
            "period": 2024,
            "ou": "V15E01",
            "values": [{"gender": "T", "value": 1.0}],
        },
        {
            "period": 2024,
            "ou": "V15E02",
            "values": [{"gender": "T", "value": None}],
        },
    ]
    years, ids = extract_presence(entries, id_key="ou")
    assert years == {2024}
    assert ids == {"V15E01"}


def test_build_ou_dimension_uses_known_titles_and_falls_back_to_id() -> None:
    dim = build_ou_dimension({"V15E01", "V99E99"}, {"V15E01": "Storskolan"})
    assert dim.label == "Enhet"
    assert dim.extension.get("elimination") is False
    assert dim.category.label == {
        "V15E01": "Storskolan",
        "V99E99": "V99E99",
    }


def test_build_ou_municipality_extension_maps_each_ou_to_its_municipality() -> None:
    extension = build_ou_municipality_extension(
        {"V15E01", "V99E99"},
        ou_municipality_ids={"V15E01": "0180"},
        municipality_titles={"0180": "Stockholm"},
    )
    assert extension == {
        "V15E01": {"municipality_id": "0180", "municipality_label": "Stockholm"},
        "V99E99": {"municipality_id": "", "municipality_label": ""},
    }


def test_build_ou_dataset_dimensions_adds_gender_only_when_the_kpi_has_it() -> None:
    result = build_ou_dataset_dimensions(
        kpi=_ou_kpi(is_divided_by_gender=True),
        all_years={2023, 2024},
        all_ou_ids={"V15E01"},
        ou_titles={"V15E01": "Storskolan"},
        ou_municipality_ids={"V15E01": "0180"},
        municipality_titles={"0180": "Stockholm"},
    )

    assert result.id == ["ou", "year", "gender"]
    assert {
        key: dimension.extension.get("elimination") for key, dimension in result.dimension.items()
    } == {"ou": False, "year": False, "gender": True}
    assert result.first_period == "2023"
    assert result.last_period == "2024"
    assert result.role == DatasetRole(geo=["ou"], time=["year"])
    assert result.dimension["ou"].extension == {
        "elimination": False,
        "category": {
            "V15E01": {
                "municipality_id": "0180",
                "municipality_label": "Stockholm",
            }
        },
    }


def test_build_ou_dataset_dimensions_omits_gender_when_the_kpi_does_not_have_it() -> None:
    result = build_ou_dataset_dimensions(
        kpi=_ou_kpi(is_divided_by_gender=False),
        all_years={2024},
        all_ou_ids={"V15E01"},
        ou_titles={"V15E01": "Storskolan"},
        ou_municipality_ids={"V15E01": "0180"},
        municipality_titles={"0180": "Stockholm"},
    )
    assert result.id == ["ou", "year"]
    assert "gender" not in result.dimension


def test_build_ou_dataset_dimensions_rejects_a_kpi_with_no_confirmed_data() -> None:
    with pytest.raises(ValueError):
        build_ou_dataset_dimensions(
            kpi=_ou_kpi(),
            all_years=set(),
            all_ou_ids=set(),
            ou_titles={},
            ou_municipality_ids={},
            municipality_titles={},
        )
