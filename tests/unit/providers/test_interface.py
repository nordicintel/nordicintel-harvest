from datetime import UTC, datetime, timedelta, timezone

import pytest

from nordicintel_harvest.providers.interface import (
    DatasetCandidate,
    DatasetHarvestState,
    HarvestContext,
    select_by_timestamp,
)

NOW = datetime(2026, 9, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "state, updated, force, expected",
    [
        (None, NOW, False, True),
        (DatasetHarvestState(NOW, NOW, False), NOW, False, False),
        (DatasetHarvestState(NOW, NOW, False), NOW + timedelta(days=1), False, True),
        (DatasetHarvestState(NOW, NOW, False), NOW - timedelta(days=1), False, True),
        (DatasetHarvestState(None, NOW, False), NOW, False, True),
        (DatasetHarvestState(NOW, None, False), NOW, False, True),
        (DatasetHarvestState(NOW, NOW, True), NOW, False, True),
        (DatasetHarvestState(NOW, NOW, False), None, False, True),
        (DatasetHarvestState(NOW, NOW, False), NOW, True, True),
        (
            DatasetHarvestState(NOW, NOW, False),
            NOW.astimezone(timezone(timedelta(hours=2))),
            False,
            False,
        ),
    ],
)
def test_timestamp_selection(state, updated, force, expected):
    candidate = DatasetCandidate("scb", "A", "sv", updated=updated)
    context = HarvestContext({"A": state} if state else {}, NOW, force=force)
    selection = select_by_timestamp(candidate, context)
    assert selection.should_fetch is expected
    assert selection.candidate is candidate


def test_context_copies_and_freezes_state():
    states = {}
    context = HarvestContext(states, NOW)
    states["A"] = DatasetHarvestState(NOW, NOW, False)
    assert not context.datasets
    with pytest.raises(TypeError):
        context.datasets["A"] = states["A"]
