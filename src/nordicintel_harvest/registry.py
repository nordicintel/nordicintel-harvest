"""Resolution of a Provider's configured adapter type to an installed factory.

A Provider row names an adapter type as a string. Turning that string into code is the
one place a database value becomes an import, so it is deliberately narrow: only packages
that installed themselves into the ``nordicintel.adapters`` entry point group are
candidates, and a deployment may narrow that further with an allowlist. The registry
never imports a module named by the database.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from importlib.metadata import EntryPoint, entry_points

from nordicintel_core.errors import ConfigurationError
from nordicintel_core.models import AdapterFactory

ENTRY_POINT_GROUP = "nordicintel.adapters"


class AdapterRegistry:
    """The adapter factories this process is willing to run."""

    def __init__(self, factories: Mapping[str, AdapterFactory]) -> None:
        self._factories = dict(factories)

    def __contains__(self, adapter_type: object) -> bool:
        return isinstance(adapter_type, str) and adapter_type.strip().lower() in self._factories

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._factories))

    def get(self, adapter_type: str) -> AdapterFactory:
        """Return the factory for a Provider's adapter type.

        Raises:
            ConfigurationError: The type is not installed, or not allowlisted here.
        """
        factory = self._factories.get(adapter_type.strip().lower())
        if factory is None:
            available = ", ".join(self) or "none"
            raise ConfigurationError(
                f"Adapter type {adapter_type!r} is not available (installed: {available})"
            )
        return factory

    @classmethod
    def from_entry_points(cls, allowed: Collection[str] | None = None) -> AdapterRegistry:
        """Load installed adapter factories, optionally restricted to an allowlist.

        Args:
            allowed: Adapter type names this process may run, or None for every installed
                adapter. A name in the allowlist that is not installed is a configuration
                error, because a Provider using it would otherwise fail one job at a time.

        Returns:
            A registry over the loaded factories.

        Raises:
            ConfigurationError: An allowlisted adapter is missing, an entry point fails to
                load, or a loaded object does not implement ``AdapterFactory``.
        """
        wanted = None if allowed is None else {name.strip().lower() for name in allowed}
        factories: dict[str, AdapterFactory] = {}
        for entry in entry_points(group=ENTRY_POINT_GROUP):
            name = entry.name.strip().lower()
            if wanted is not None and name not in wanted:
                continue
            factories[name] = _load(entry)
        if wanted is not None:
            missing = sorted(wanted - factories.keys())
            if missing:
                raise ConfigurationError(
                    f"Allowlisted adapters are not installed: {', '.join(missing)}"
                )
        return cls(factories)


def _load(entry: EntryPoint) -> AdapterFactory:
    try:
        loaded = entry.load()
    except Exception as exc:  # the failing package decides the message
        raise ConfigurationError(f"Adapter {entry.name!r} failed to load: {exc}") from exc
    # A class registered instead of an instance is the common mistake, and calling it here
    # keeps the error at startup rather than inside a claimed job.
    if isinstance(loaded, type):
        loaded = loaded()
    if not isinstance(loaded, AdapterFactory):
        raise ConfigurationError(f"Adapter {entry.name!r} does not implement AdapterFactory")
    return loaded
