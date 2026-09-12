"""Pure parsing/derivation helpers for the Kolada adapter - no I/O.

Kolada's own catalog only tells you which municipality *type* ("K"/"L"/"A")
a KPI is scoped to and whether it splits data by gender. It never tells you
which years or which specific municipalities within that type actually carry
a value - that has to be discovered by probing `/data` and looking for a
non-null value. These helpers turn that catalog metadata plus a probe result
into the dimension shapes `DatasetMetadata` expects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from nordicintel_harvest.schemas.datasets import DatasetPath, PathItem
from nordicintel_harvest.schemas.dimensions import DatasetDimension, DatasetRole

_DISCONTINUED_YEAR_RE = re.compile(r"\(\s*-\s*(\d{4})\s*\)")
_SOURCE_RE = re.compile(r"\bKäll(?:a|or)\b\s*[:.]?\s*", re.IGNORECASE)
_ABBREVIATION_RE = re.compile(
    r"(?:\b(?:t\.ex|bl\.a|m\.fl|m\.m|d\.v\.s|s\.k|o\.s\.v|resp|inkl|exkl|ca|fig|nr)|\b[A-ZÅÄÖ])\.$",
    re.IGNORECASE,
)

EARLIEST_POSSIBLE_YEAR = 1970
MAX_FUTURE_YEARS = 3
MAX_FILTER_VALUES = (
    25  # Kolada API: kpi/municipality/year each accept at most 25 values per request
)
RIKET_ID = "0000"
GENDER_LABELS: dict[str, str] = {"K": "Kvinnor", "M": "Män", "T": "Totalt"}
GENDER_ORDER: tuple[str, ...] = ("K", "M", "T")


@dataclass(frozen=True)
class KpiInfo:
    kpi_id: str
    title: str
    description: str | None
    is_divided_by_gender: bool
    municipality_type: str  # "K" | "L" | "A"
    publication_date: str | None
    prel_publication_date: str | None
    publ_period: str | None
    has_ou_data: bool
    operating_area: str | None = None
    perspective: str | None = None
    auspice: str | None = None
    raw_description: str | None = None
    groups: tuple[KpiGroup, ...] = ()


@dataclass(frozen=True)
class KpiGroup:
    group_id: str
    title: str
    member_ids: tuple[str, ...]


def normalize_text(value: str) -> str:
    return " ".join(value.replace(r"\n", " ").split())


def parse_kpi_group(raw: dict[str, Any]) -> KpiGroup:
    group_id, title, members = raw.get("id"), raw.get("title"), raw.get("members")
    if not isinstance(group_id, str) or not group_id.strip():
        raise ValueError("Kolada KPI group has no ID")
    if not isinstance(title, str) or not title.strip() or not isinstance(members, list):
        raise ValueError(f"Malformed Kolada KPI group {group_id!r}")
    ids: set[str] = set()
    for member in members:
        member_id = member.get("member_id") if isinstance(member, dict) else None
        if not isinstance(member_id, str) or not member_id.strip():
            raise ValueError(f"Malformed member in Kolada KPI group {group_id!r}")
        ids.add(member_id.strip())
    return KpiGroup(group_id.strip(), title.strip(), tuple(sorted(ids)))


def extract_kpi_source(description: str | None, title: str) -> tuple[str, str]:
    """Extract the last explicit source clause, preserving the original text.

    A sentence ends at punctuation followed by whitespace, except common
    abbreviations and initials. Internal periods in URLs are not boundaries.
    """
    for origin, text in (("description", description), ("title", title)):
        if not text:
            continue
        text = normalize_text(text)
        matches = list(_SOURCE_RE.finditer(text))
        if not matches:
            continue
        source = text[matches[-1].end() :].strip()
        for boundary in re.finditer(r"[.!?](?=\s|$)", source):
            prefix = source[: boundary.end()]
            if boundary.group() == "." and _ABBREVIATION_RE.search(prefix):
                continue
            source = source[: boundary.start()]
            break
        source = source.strip().rstrip(".").strip()
        if source:
            return source, origin
    return "Kolada", "fallback"


def kpi_metadata(kpi: KpiInfo, *, year: int) -> dict[str, Any]:
    """Listing metadata shared by discovery and standalone resolution."""
    source, source_origin = extract_kpi_source(kpi.description, kpi.title)
    match = _DISCONTINUED_YEAR_RE.search(kpi.title)
    discontinued_year = int(match.group(1)) if match else None
    # Encoding the complete label is deterministic and does not collapse
    # distinct labels as transliterated slugs would.
    subject_code = (
        f"kolada:operating_area:{quote(kpi.operating_area, safe='')}"
        if kpi.operating_area
        else None
    )
    paths: list[DatasetPath] = []
    if subject_code:
        paths.append(
            DatasetPath(
                path=[
                    PathItem(code="kolada:operating_area", label="Verksamhetsområde"),
                    PathItem(code=subject_code, label=kpi.operating_area),
                ]
            )
        )
    for group in kpi.groups:
        paths.append(
            DatasetPath(
                path=[
                    PathItem(code="kolada:kpi_group", label="Nyckeltalsgrupp"),
                    PathItem(code=group.group_id, label=group.title),
                ]
            )
        )
    extra = {
        name: getattr(kpi, name)
        for name in (
            "operating_area",
            "perspective",
            "auspice",
            "municipality_type",
            "has_ou_data",
            "is_divided_by_gender",
            "publ_period",
            "publication_date",
            "prel_publication_date",
            "raw_description",
        )
        if getattr(kpi, name) is not None
    }
    extra["source_origin"] = source_origin
    extra["kpi_groups"] = [{"id": g.group_id, "title": g.title} for g in kpi.groups]
    if discontinued_year is not None:
        extra["discontinued_year"] = discontinued_year
    return {
        "label": kpi.title,
        "description": kpi.description,
        "source": source,
        "subject_code": subject_code,
        "subject_label": kpi.operating_area,
        "paths": paths or None,
        "discontinued": discontinued_year is not None and discontinued_year <= year,
        "extension": {"kolada": extra},
    }


@dataclass(frozen=True)
class MunicipalityInfo:
    municipality_id: str
    title: str
    type: str  # "K" | "L"


@dataclass(frozen=True)
class OuInfo:
    ou_id: str
    title: str
    municipality_id: str


def parse_kpi(raw: dict[str, Any]) -> KpiInfo:
    raw_description = str(raw["description"]) if raw.get("description") is not None else None
    description = normalize_text(raw_description) if raw_description is not None else None
    return KpiInfo(
        kpi_id=str(raw.get("id") or "").strip(),
        title=str(raw.get("title") or "").strip(),
        description=description,
        is_divided_by_gender=bool(raw.get("is_divided_by_gender")),
        municipality_type=str(raw.get("municipality_type") or "A").strip() or "A",
        publication_date=raw.get("publication_date"),
        prel_publication_date=raw.get("prel_publication_date"),
        publ_period=raw.get("publ_period"),
        has_ou_data=bool(raw.get("has_ou_data")),
        operating_area=str(raw.get("operating_area") or "").strip() or None,
        perspective=str(raw.get("perspective") or "").strip() or None,
        auspice=str(raw.get("auspice") or "").strip() or None,
        raw_description=raw_description if raw_description != description else None,
    )


def parse_municipality(raw: dict[str, Any]) -> MunicipalityInfo | None:
    municipality_id = str(raw.get("id") or "").strip()
    if not municipality_id:
        return None
    return MunicipalityInfo(
        municipality_id=municipality_id,
        title=str(raw.get("title") or municipality_id).strip(),
        type=str(raw.get("type") or "").strip(),
    )


def parse_ou(raw: dict[str, Any]) -> OuInfo | None:
    ou_id = str(raw.get("id") or "").strip()
    municipality_id = str(raw.get("municipality") or "").strip()
    if not ou_id or not municipality_id:
        return None
    return OuInfo(
        ou_id=ou_id,
        title=str(raw.get("title") or ou_id).strip(),
        municipality_id=municipality_id,
    )


def candidate_municipality_ids(
    municipality_type: str, municipalities: list[MunicipalityInfo]
) -> list[str]:
    """The municipality ids to probe for a KPI scoped to *municipality_type*.

    Per Kolada's docs: "K" KPIs report kommuner + the national total, "L"
    KPIs report regions + the national total, "A" report every area type.
    Riket (id "0000", the national total) is itself catalogued as type "L",
    so it's included automatically for "L"/"A" but has to be added explicitly
    for "K" - confirmed against the live API: a "K"-type KPI (N15522) returns
    real data for municipality "0000".
    """
    if municipality_type not in ("K", "L", "A"):
        raise ValueError(f"Unknown Kolada municipality_type: {municipality_type!r}")

    if municipality_type == "A":
        return [m.municipality_id for m in municipalities]
    if municipality_type == "L":
        return [m.municipality_id for m in municipalities if m.type == "L"]

    ids = [m.municipality_id for m in municipalities if m.type == "K"]
    if RIKET_ID not in ids and any(m.municipality_id == RIKET_ID for m in municipalities):
        ids.append(RIKET_ID)
    return ids


def max_probe_year(today: date | None = None) -> int:
    """The latest year worth ever probing for - `this_year + 3`, a fixed
    ceiling regardless of how far ahead a KPI's own publication schedule reaches."""
    resolved_today = today or datetime.now(UTC).date()
    return resolved_today.year + MAX_FUTURE_YEARS


def probe_year_range(*, max_year: int) -> list[int]:
    """All supported years for rebuilding current dimensions."""
    return list(range(EARLIEST_POSSIBLE_YEAR, max_year + 1))


def batch_values(values: list[Any], limit: int = MAX_FILTER_VALUES) -> list[list[Any]]:
    return [values[i : i + limit] for i in range(0, len(values), limit)]


def build_data_request_batches(
    municipality_ids: list[str], years: list[int]
) -> list[tuple[list[str], list[int]]]:
    """Cartesian product of municipality/year batches, each within the
    25-values-per-filter API limit."""
    muni_batches = batch_values(municipality_ids)
    year_batches = batch_values(years)
    return [(m, y) for m in muni_batches for y in year_batches]


def extract_presence(
    entries: list[dict[str, Any]], *, id_key: str = "municipality"
) -> tuple[set[int], set[str]]:
    """From a list of Kolada `/data` (or `/oudata`) response entries, return
    the (years, ids) that have at least one non-null, non-missing value
    anywhere in that entry's gender breakdown. `id_key` selects which field
    carries the entity id - "municipality" for `/data`, "ou" for `/oudata`.
    """
    years_with_data: set[int] = set()
    ids_with_data: set[str] = set()
    for entry in entries:
        period = entry.get("period")
        entity_id = entry.get(id_key)
        if period is None or entity_id is None:
            continue
        value_entries = entry.get("values")
        if not isinstance(value_entries, list):
            continue
        has_value = any(
            isinstance(value_entry, dict)
            and value_entry.get("value") is not None
            and value_entry.get("isdeleted") is not True
            for value_entry in value_entries
        )
        if has_value:
            years_with_data.add(int(period))
            ids_with_data.add(str(entity_id))
    return years_with_data, ids_with_data


def build_municipality_dimension(
    municipality_ids: set[str], titles: dict[str, str]
) -> DatasetDimension:
    sorted_ids = sorted(municipality_ids)
    return DatasetDimension(
        label="Kommun",
        extension={"elimination": False},
        category={
            "index": {mid: i for i, mid in enumerate(sorted_ids)},
            "label": {mid: titles.get(mid, mid) for mid in sorted_ids},
        },
    )


def build_ou_dimension(
    ou_ids: set[str],
    titles: dict[str, str],
    extension: dict[str, Any] | None = None,
) -> DatasetDimension:
    sorted_ids = sorted(ou_ids)
    return DatasetDimension(
        label="Enhet",
        extension={"elimination": False, "category": extension or {}},
        category={
            "index": {oid: i for i, oid in enumerate(sorted_ids)},
            "label": {oid: titles.get(oid, oid) for oid in sorted_ids},
        },
    )


def build_year_dimension(years: set[int]) -> DatasetDimension:
    sorted_years = sorted(years)
    return DatasetDimension(
        label="År",
        extension={"elimination": False},
        category={
            "index": {str(y): i for i, y in enumerate(sorted_years)},
            "label": {str(y): str(y) for y in sorted_years},
        },
    )


def build_gender_dimension() -> DatasetDimension:
    """Kolada's gender breakdown is always exactly these three codes -
    unlike years/municipalities, there's nothing to probe for."""
    return DatasetDimension(
        label="Kön",
        extension={"elimination": True},
        category={
            "index": {code: i for i, code in enumerate(GENDER_ORDER)},
            "label": {code: GENDER_LABELS[code] for code in GENDER_ORDER},
        },
    )


@dataclass(frozen=True)
class ResolvedDimensions:
    """The probe-derived dimension metadata for one Kolada dataset."""

    first_period: str
    last_period: str
    id: list[str]
    role: DatasetRole
    dimension: dict[str, DatasetDimension]


def build_dataset_dimensions(
    *,
    kpi: KpiInfo,
    all_years: set[int],
    all_municipality_ids: set[str],
    municipality_titles: dict[str, str],
) -> ResolvedDimensions:
    """Assemble the Dimension Set for one Kolada dataset, given the confirmed
    years and municipalities. Raises ValueError if either set is empty - a
    dataset with no confirmed data anywhere can't be persisted."""
    if not all_years or not all_municipality_ids:
        raise ValueError(
            f"Kolada KPI {kpi.kpi_id!r} has no non-null datapoints for any "
            "probed municipality/year combination"
        )

    dimension_ids = ["municipality", "year"]
    dimensions = {
        "municipality": build_municipality_dimension(all_municipality_ids, municipality_titles),
        "year": build_year_dimension(all_years),
    }
    if kpi.is_divided_by_gender:
        dimension_ids.append("gender")
        dimensions["gender"] = build_gender_dimension()

    sorted_years = sorted(all_years)
    return ResolvedDimensions(
        first_period=str(sorted_years[0]),
        last_period=str(sorted_years[-1]),
        id=dimension_ids,
        role=DatasetRole(geo=["municipality"], time=["year"]),
        dimension=dimensions,
    )


def build_ou_municipality_extension(
    ou_ids: set[str],
    ou_municipality_ids: dict[str, str],
    municipality_titles: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Per-category extension for the "ou" dimension: each OU's owning
    municipality (id + label). An OU is scoped to exactly one municipality,
    but that relationship is a static catalog property, not a probeable
    dimension in its own right - see the Kolada OU adapter plan's
    "Dimensions" decision.
    """
    extension: dict[str, dict[str, Any]] = {}
    for ou_id in sorted(ou_ids):
        municipality_id = ou_municipality_ids.get(ou_id, "")
        extension[ou_id] = {
            "municipality_id": municipality_id,
            "municipality_label": municipality_titles.get(municipality_id, municipality_id),
        }
    return extension


def build_ou_dataset_dimensions(
    *,
    kpi: KpiInfo,
    all_years: set[int],
    all_ou_ids: set[str],
    ou_titles: dict[str, str],
    ou_municipality_ids: dict[str, str],
    municipality_titles: dict[str, str],
) -> ResolvedDimensions:
    """Assemble the Dimension Set for one Kolada OU dataset, given the confirmed
    years and OUs. Raises ValueError if either set is empty - a dataset with no
    confirmed data anywhere can't be persisted.
    """
    if not all_years or not all_ou_ids:
        raise ValueError(
            f"Kolada KPI {kpi.kpi_id!r} has no non-null OU datapoints for any "
            "probed unit/year combination"
        )

    dimension_ids = ["ou", "year"]
    dimensions = {
        "ou": build_ou_dimension(
            all_ou_ids,
            ou_titles,
            extension=build_ou_municipality_extension(
                all_ou_ids, ou_municipality_ids, municipality_titles
            ),
        ),
        "year": build_year_dimension(all_years),
    }
    if kpi.is_divided_by_gender:
        dimension_ids.append("gender")
        dimensions["gender"] = build_gender_dimension()

    sorted_years = sorted(all_years)
    return ResolvedDimensions(
        first_period=str(sorted_years[0]),
        last_period=str(sorted_years[-1]),
        id=dimension_ids,
        role=DatasetRole(geo=["ou"], time=["year"]),
        dimension=dimensions,
    )
