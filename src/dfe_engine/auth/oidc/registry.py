#  Project:      dfe-engine
#  File:         auth/oidc/registry.py
#  Purpose:      YAML-backed CRUD registry for OIDC provider configurations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""YAML-backed registry for OIDC provider configurations.

Each provider is persisted as ``{name}.yaml`` under the providers directory.
The provider name is the filename stem -- it is NOT stored inside the YAML body.

Example layout::

    oidc_providers/
        google-workspace.yaml   # { type: google, enabled: true, ... }
        entra-id.yaml
"""

import builtins
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from scalo.logger import logger

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.store_names import store_key
from dfe_engine.http_errors import describe_http_error
from dfe_engine.names import VALID_NAME
from dfe_engine.yaml_utils import YAMLError, yaml_dump, yaml_load

PROVIDERS_SKIPPED = "auth_oidc_providers_skipped_total"

ProviderSkipReason = Literal["unreadable", "invalid"]
"""Why a stored provider file was left out of the listing.

- ``unreadable``: the file cannot be read as YAML.
- ``invalid``: the file holds no valid provider, such as a list or a type the model refuses.
"""


class ProviderMetrics:
    """The provider registry's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        """Register the skip counter on *manager*, when one is wired."""
        self._manager = manager
        if manager is None:
            return
        self._skipped = manager.counter(
            PROVIDERS_SKIPPED,
            "Stored OIDC provider files left out of the listing, counted each time one starts failing",
            ["reason"],
        )

    def skipped(self, reason: ProviderSkipReason) -> None:
        """Record a provider file that has started failing to load."""
        if self._manager is None:
            return
        self._skipped.labels(reason=reason).inc()


class OIDCProviderRegistry:
    """YAML-backed store for OIDC provider configurations.

    Each provider is a ``{name}.yaml`` file in ``providers_dir``.
    The provider name is derived from the filename stem and is never stored
    inside the YAML body.

    Args:
        providers_dir: Directory holding the provider files.
        metrics: Where a provider file left out of :meth:`list` is counted. ``None`` counts nothing.
    """

    def __init__(self, providers_dir: Path, *, metrics: ProviderMetrics | None = None) -> None:
        """Open *providers_dir*, creating it when it is missing."""
        self._dir = Path(providers_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._metrics = metrics if metrics is not None else ProviderMetrics()
        # Files already reported as failing, so a listing on every scheduler tick logs each once.
        self._skipped: set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        """The file provider *name* lives in.

        Raises:
            KeyError: No provider can hold *name*, so it is looked up nowhere.
        """
        return self._dir / f"{store_key(name)}.yaml"

    def _read(self, name: str) -> OIDCProvider | None:
        try:
            path = self._path(name)
        except KeyError:
            return None
        if not path.exists():
            return None
        data = yaml_load(path)
        if data is None:
            data = {}
        # Name is the filename stem -- not stored in the YAML body
        return OIDCProvider.model_validate(data)

    def _listed(self, path: Path) -> OIDCProvider | None:
        """The provider in *path*, or None when it does not load, reported once until it loads clean."""
        try:
            provider = self._read(path.stem)
        except (OSError, UnicodeDecodeError, YAMLError, RecursionError) as exc:
            self._skip(path, "unreadable", exc)
            return None
        except ValidationError as exc:
            self._skip(path, "invalid", exc)
            return None
        self._skipped.discard(path.name)
        return provider

    def _skip(self, path: Path, reason: ProviderSkipReason, exc: Exception) -> None:
        """Warn about and count a provider file that does not load, unless it is already reported."""
        if path.name in self._skipped:
            return
        self._skipped.add(path.name)
        self._metrics.skipped(reason)
        # The validation text quotes the file's values, so the log takes the failure's class only.
        logger.warning(
            "OIDC provider file skipped: it does not load",
            file=path.name,
            reason=reason,
            **describe_http_error(exc),
        )

    def _write(self, name: str, provider: OIDCProvider) -> None:
        # Persist the full provider model to YAML; no name field to exclude
        data = provider.model_dump()
        yaml_dump(data, self._path(name))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, name: str, provider: OIDCProvider) -> OIDCProvider:
        """Create a new provider entry and persist it to YAML.

        Args:
            name: Unique provider name (used as the filename stem).
            provider: Fully-constructed OIDCProvider instance.

        Returns:
            The stored OIDCProvider.

        Raises:
            ValueError: If the name is not a valid provider name, or a provider
                with this name already exists.
        """
        if not VALID_NAME.match(name):
            raise ValueError(f"Invalid OIDC provider name: {name!r}")
        if self._path(name).exists():
            raise ValueError(f"OIDC provider '{name}' already exists")
        self._write(name, provider)
        return provider

    def get(self, name: str) -> OIDCProvider | None:
        """Return the named provider, or None if it does not exist."""
        return self._read(name)

    def list(self) -> builtins.list[tuple[str, OIDCProvider]]:
        """Return all providers as (name, provider) tuples sorted by name.

        A file that does not load is left out, logged and counted on
        ``auth_oidc_providers_skipped_total``, so one bad file does not stop the others
        from serving. :meth:`get` still raises for it.
        """
        result: list[tuple[str, OIDCProvider]] = []
        for path in sorted(self._dir.glob("*.yaml")):
            provider = self._listed(path)
            if provider is not None:
                result.append((path.stem, provider))
        return result

    def update(self, name: str, **fields: object) -> OIDCProvider:
        """Update one or more fields on an existing provider and persist.

        Accepted keyword arguments correspond to fields on OIDCProvider.

        Args:
            name: Provider name to update.
            **fields: Fields to update.

        Returns:
            The updated OIDCProvider.

        Raises:
            KeyError: If the provider does not exist.
        """
        provider = self._read(name)
        if provider is None:
            raise KeyError(f"OIDC provider '{name}' not found")
        updated = provider.model_copy(update=fields)
        self._write(name, updated)
        return updated

    def delete(self, name: str) -> None:
        """Delete the named provider.

        Args:
            name: Provider name to delete.

        Raises:
            KeyError: If the provider does not exist.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"OIDC provider '{name}' not found")
        path.unlink()
