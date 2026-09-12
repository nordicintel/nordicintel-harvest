import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nordicintel_harvest.providers.adapters.pxweb_v2.parsing import parse_pxweb2_discovery_table
from nordicintel_harvest.schemas.contracts import encoded_url
from nordicintel_harvest.schemas.datasets import DatasetDocuments, DatasetIdentity, DatasetInfo

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "contracts"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_real_examples_keep_identity_and_order(path):
    original = json.loads(path.read_text(encoding="utf-8"))
    pair = DatasetDocuments.model_validate(original)
    assert isinstance(pair.dataset.identity, DatasetIdentity)
    assert pair.dataset.identity == pair.metadata.identity
    assert pair.metadata.id == original["metadata"]["id"]
    for code in pair.metadata.id:
        assert (
            pair.metadata.dimension[code].category.index
            == original["metadata"]["dimension"][code]["category"]["index"]
        )
    assert DatasetDocuments.model_validate(pair.documents()) == pair


@pytest.mark.parametrize("change", ["identity", "language", "retrieval", "index", "unknown", "url"])
def test_contract_errors_rejected(change):
    raw = json.loads((FIXTURES / "pxweb_v1.json").read_text(encoding="utf-8"))
    if change == "identity":
        raw["metadata"]["identity"]["dataset_code"] = "different"
    elif change == "language":
        raw["dataset"]["identity"]["language"] = "fi"
    elif change == "retrieval":
        raw["metadata"]["retrieval"]["config"] = {}
    elif change == "index":
        category = next(iter(raw["metadata"]["dimension"].values()))["category"]
        category["index"][next(iter(category["index"]))] = 99999
    elif change == "unknown":
        raw["dataset"]["database_id"] = "misplaced"
    else:
        raw["metadata"]["metadata_url"] = "https://example.org/a b"
    with pytest.raises(ValidationError):
        DatasetDocuments.model_validate(raw)


def test_date_only_and_unknown_status_are_preserved():
    info = DatasetInfo(
        identity={"provider_code": "scb", "dataset_code": "A", "language": "sv"},
        label="A",
        updated="2026-09-12",
    )
    result = info.model_dump(mode="json", exclude_none=True)
    assert result["updated"] == "2026-09-12"
    assert "discontinued" not in result


def test_extension_nulls_are_preserved_at_every_level():
    raw = json.loads((FIXTURES / "pxweb_v1.json").read_text(encoding="utf-8"))
    raw["metadata"]["extension"]["unknown"] = None
    dimension = next(iter(raw["metadata"]["dimension"].values()))
    dimension["extension"]["unknown"] = None
    result = DatasetDocuments.model_validate(raw).documents()
    assert result["metadata"]["extension"]["unknown"] is None
    assert next(iter(result["metadata"]["dimension"].values()))["extension"]["unknown"] is None


def test_pxweb_urls_include_language_and_do_not_duplicate_ssb_route():
    parsed = parse_pxweb2_discovery_table(
        {"id": "13760", "label": "Test"},
        base_api_url="https://data.ssb.no/api/pxwebapi/v2-beta",
        base_web_url="https://www.ssb.no/en/statbank/table/",
        language="en",
    )
    assert parsed["web_url"] == "https://www.ssb.no/en/statbank/table/13760/"
    assert parsed["metadata_url"].endswith("/13760/metadata?lang=en")
    assert (
        encoded_url("https://example.org/Å land/%20?x=a b")
        == "https://example.org/%C3%85%20land/%20?x=a%20b"
    )
