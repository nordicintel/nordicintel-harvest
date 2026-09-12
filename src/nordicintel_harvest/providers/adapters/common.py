from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# from typing import Any
# from urllib.parse import quote

TIME_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "Annual": re.compile(r"^(1[5-9][0-9]{2}|2[0-9]{3}|3000)$"),
    "Quarterly": re.compile(r"^(1[5-9][0-9]{2}|2[0-9]{3}|3000)[QK][1-4]$"),
    "Monthly": re.compile(r"^(1[5-9][0-9]{2}|2[0-9]{3}|3000)(M0?[1-9]|M1[0-2]|0?[1-9]|1[0-2])$"),
    "Weekly": re.compile(r"^(1[5-9][0-9]{2}|2[0-9]{3}|3000)(W0?[1-9]|W[1-4][0-9]|W5[0-3])$"),
    "Other": re.compile(
        r"^(1[5-9][0-9]{2}|2[0-9]{3}|3000)(W0?[1-9]|W[1-4][0-9]|W5[0-3]|V0?[1-9]|V[1-4][0-9]|V5[0-3])$"
    ),
}


TIME_ALTERNATIVES = {
    "time",
    "tid",
    "år",
    "månad",
    "vecka",
    "kvartal",
    "period",
    "year",
    "quarter",
    "month",
    "vuosi",
    "time period",
}
GEO_ALTERNATIVES = {
    "geo",
    "region",
    "land",
    "kommun",
    "county",
    "municipality",
    "country",
    "area",
    "location",
}
METRIC_ALTERNATIVES = {
    "unit",
    "innehåll",
    "tabellinnehåll",
    "mått",
    "contents",
    "measure",
    "metric",
    "value",
    "contentscode",
    "content",
    "enhet",
}


def determine_time_unit(first_period: str | None, last_period: str | None) -> str:
    if not first_period or not last_period:
        return "Other"
    for time_format, pattern in TIME_LABEL_PATTERNS.items():
        if pattern.match(first_period) and pattern.match(last_period):
            return time_format
    return "Other"


def detect_role(code: str, label: str, is_time: bool = False) -> str | None:
    if is_time:
        return "time"

    norm_code = code.strip().lower()
    norm_label = label.strip().lower()

    if (
        norm_code == "contentscode"
        or norm_code in METRIC_ALTERNATIVES
        or norm_label in METRIC_ALTERNATIVES
    ):
        return "metric"
    if norm_code in GEO_ALTERNATIVES or norm_label in GEO_ALTERNATIVES:
        return "geo"
    if norm_code in TIME_ALTERNATIVES or norm_label in TIME_ALTERNATIVES:
        return "time"
    return None


def parse_dt(value: str | datetime | None) -> datetime | None:
    """Parse a datetime value from a string or datetime to UTC.

    Accepts:
    - None → returns None
    - datetime (naive or aware) → normalised to UTC
    - ISO 8601 string with 'Z' suffix or +HH:MM offset
    - 'YYYY-MM-DDTHH:MM:SS' string without timezone marker (19 chars)
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            if len(raw) == 19 and raw[4] == "-" and raw[7] == "-" and raw[10] == "T":
                return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    return None


def normalize_note(value: Any) -> list[str] | None:
    """Normalize provider notes into a non-empty list of strings."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else None
    if isinstance(value, list):
        notes = [str(item).strip() for item in value if str(item).strip()]
        return notes or None
    return None


def coerce_bool(value: Any) -> bool | None:
    """Coerce common bool-like values to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def structured_extras(value: dict, fields: set[str]) -> dict:
    """Keep upstream extras inside the contract's explicit extension object."""
    result = {k: v for k, v in value.items() if k in fields and v is not None}
    extension = dict(value.get("extension") or {})
    for key, item in value.items():
        if key not in fields and key != "extension" and item is not None:
            if key in extension and extension[key] != item:
                raise ValueError(f"Conflicting extension field: {key}")
            extension[key] = item
    if extension:
        result["extension"] = extension
    return result
