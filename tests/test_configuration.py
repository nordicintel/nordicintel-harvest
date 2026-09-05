"""Settings, secret resolution, the adapter registry, and diagnostic construction."""

from __future__ import annotations

import pytest
from nordicintel_core.errors import (
    ConfigurationError,
    UpstreamResponseError,
    UpstreamTransportError,
)
from nordicintel_core.models import DiagnosticStage
from support import StubAdapter, StubFactory, provider

from nordicintel_harvest import diagnostics
from nordicintel_harvest.registry import AdapterRegistry
from nordicintel_harvest.secrets import resolve_secrets
from nordicintel_harvest.settings import Settings, load_settings

BASE_ENV = {"NORDICINTEL_DATABASE_URL": "postgresql://localhost/nordicintel"}


def test_settings_come_only_from_the_supplied_environment() -> None:
    settings = load_settings(
        {
            **BASE_ENV,
            "NORDICINTEL_HEARTBEAT_SECONDS": "10",
            "NORDICINTEL_STALE_AFTER_SECONDS": "60",
            "NORDICINTEL_ADAPTERS": "PxWeb, stub ,",
        }
    )
    assert settings.heartbeat_seconds == 10.0
    assert settings.stale_after_seconds == 60
    assert settings.adapters == frozenset({"pxweb", "stub"})
    assert settings.statement_timeout_option == "-c statement_timeout=30000"
    assert load_settings(BASE_ENV).adapters is None


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "DATABASE_URL"),
        ({**BASE_ENV, "NORDICINTEL_HEARTBEAT_SECONDS": "soon"}, "must be a number"),
        ({**BASE_ENV, "NORDICINTEL_QUEUE_POLL_SECONDS": "0"}, "must be positive"),
        # Three beats must fit inside the stale window, or a working worker looks dead.
        ({**BASE_ENV, "NORDICINTEL_HEARTBEAT_SECONDS": "90"}, "three heartbeat intervals"),
        (
            {**BASE_ENV, "NORDICINTEL_STATEMENT_TIMEOUT_SECONDS": "600"},
            "below stale_after_seconds",
        ),
    ],
)
def test_unusable_configuration_fails_at_startup(environment: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_settings(environment)


def test_secrets_resolve_by_reference_and_report_only_names() -> None:
    definition = provider(secret_refs={"api_key": "SCB_API_KEY", "token": "SCB_TOKEN"})
    resolved = resolve_secrets(
        definition, {"SCB_API_KEY": "secret-value", "SCB_TOKEN": "another"}
    )
    assert resolved == {"api_key": "secret-value", "token": "another"}
    with pytest.raises(ConfigurationError) as failure:
        resolve_secrets(definition, {"SCB_API_KEY": "secret-value", "SCB_TOKEN": "  "})
    message = str(failure.value)
    assert "token -> SCB_TOKEN" in message
    # Nothing partially resolved leaks, and neither does a value that did resolve.
    assert "secret-value" not in message


def test_registry_resolves_only_what_it_was_given() -> None:
    factory = StubFactory(StubAdapter(entries=[]))
    registry = AdapterRegistry({"stub": factory})
    assert registry.get("STUB") is factory
    assert "stub" in registry and "pxweb" not in registry
    with pytest.raises(ConfigurationError, match="not available"):
        registry.get("pxweb")


def test_allowlisting_an_uninstalled_adapter_fails_at_startup() -> None:
    with pytest.raises(ConfigurationError, match="not installed"):
        AdapterRegistry.from_entry_points({"definitely-not-installed"})


def test_diagnostics_keep_upstream_codes_and_hide_unknown_messages() -> None:
    response = diagnostics.diagnose(
        UpstreamResponseError("Upstream said no", code="upstream_response", status_code=503),
        stage=DiagnosticStage.FETCH_METADATA,
    )
    assert response.code == "upstream_response"
    assert response.stage is DiagnosticStage.FETCH_METADATA
    leaky = diagnostics.diagnose(
        RuntimeError("https://api.example.test/?apikey=sekret"),
        stage=DiagnosticStage.FETCH_METADATA,
    )
    assert leaky.code == "unexpected_error"
    assert "sekret" not in leaky.message


def test_oversized_details_are_trimmed_rather_than_lost() -> None:
    failures = [
        diagnostics.language_failure(
            f"l{index}",
            DiagnosticStage.FETCH_METADATA,
            UpstreamTransportError("x" * 900, code="upstream_transport"),
        )
        for index in range(400)
    ]
    aggregated = diagnostics.item_failure(failures)
    assert aggregated.code == "upstream_transport"
    assert len(aggregated.details["languages"]) < len(failures)
    assert "language(s) failed" in aggregated.message


def test_a_diagnostic_that_cannot_be_trimmed_still_names_its_code() -> None:
    built = diagnostics.build("huge", "y" * 40_000)
    assert built.code == "huge"
    assert built.details == {}


def test_settings_reject_an_empty_adapter_allowlist() -> None:
    with pytest.raises(ConfigurationError, match="at least one adapter"):
        Settings(database_url="postgresql://localhost/x", adapters=frozenset())
