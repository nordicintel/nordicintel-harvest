"""Resolution of a Provider's declared secret references against the deployment.

``ProviderDefinition.secret_refs`` maps the name an adapter asks for to the name of the
variable that holds it. The database stores only the reference, so a credential reaches
an adapter through this module or not at all.

Every error here names the reference and never the value, because a diagnostic built from
a failure is written to the job row and read by anyone who can see the queue.
"""

from __future__ import annotations

from collections.abc import Mapping

from nordicintel_core.errors import ConfigurationError
from nordicintel_core.models import ProviderDefinition


def resolve_secrets(
    provider: ProviderDefinition, environment: Mapping[str, str]
) -> dict[str, str]:
    """Look up every secret a Provider declares.

    Args:
        provider: The configured Provider, whose ``secret_refs`` name the variables.
        environment: The variables this process was given, normally ``os.environ``.

    Returns:
        The adapter-facing name mapped to its resolved value.

    Raises:
        ConfigurationError: A declared reference is absent or empty. Naming the missing
            reference is safe; reporting a partial resolution is not, so nothing is
            returned unless every reference resolved.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name, reference in provider.secret_refs.items():
        value = environment.get(reference)
        if value is None or not value.strip():
            missing.append(f"{name} -> {reference}")
            continue
        resolved[name] = value
    if missing:
        raise ConfigurationError(
            f"Provider {provider.id!r} is missing secrets: {', '.join(sorted(missing))}"
        )
    return resolved
