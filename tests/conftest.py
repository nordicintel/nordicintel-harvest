from __future__ import annotations

import pytest
from support import truncate


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest) -> None:
    """Start every database test from an empty schema, on a session of its own."""
    if request.node.get_closest_marker("postgres") is None:
        return
    truncate()
