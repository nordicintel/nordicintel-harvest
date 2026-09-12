from nordicintel_harvest.providers.adapters.pxweb_v1.parsing import PxWebTableEntry


def test_table_entry_becomes_candidate_without_network() -> None:
    entry = PxWebTableEntry(
        id="population.px",
        title="Population",
        path="subject/demography",
        db_id="StatFin",
        language="en",
        provider_code="statfin",
        base_api_url="https://example.test/api/v1",
        base_web_url="https://example.test",
        updated="2026-09-08T10:00:00Z",
    )

    candidate = entry.to_discovered_dataset({"subject": "Subject", "demography": "Demography"})

    assert candidate is not None
    assert candidate.dataset_code == "population.px"
    assert candidate.updated.isoformat() == "2026-09-08T10:00:00+00:00"
    assert candidate.subject_code == "demography"
