"""Offline validation against the shared contract snapshots."""

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


def encoded_url(value: str) -> str:
    """Encode URI components without double-encoding existing percent escapes."""
    parts = urlsplit(value)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
            quote(parts.query, safe="%=&/?@:!$'()*+,;-._~"),
            quote(parts.fragment, safe="%/?@:!$&'()*+,;=-._~"),
        )
    )


@lru_cache
def _validators():
    schemas = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (Path(__file__).parent / "contracts").rglob("*.schema.json")
    ]
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    return {
        schema["$id"].rsplit("/", 1)[-1]: Draft202012Validator(
            schema,
            registry=registry,
            format_checker=FormatChecker(formats=["uri", "date", "date-time"]),
        )
        for schema in schemas
    }


def validate_contract(name, document):
    errors = sorted(
        _validators()[name + ".schema.json"].iter_errors(document),
        key=lambda error: str(error.absolute_path),
    )
    if errors:
        error = errors[0]
        raise ValueError(f"{name}/{'/'.join(map(str, error.absolute_path))}: {error.message}")
