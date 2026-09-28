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

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.store_names import VALID_NAME, store_key
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class OIDCProviderRegistry:
    """YAML-backed store for OIDC provider configurations.

    Each provider is a ``{name}.yaml`` file in ``providers_dir``.
    The provider name is derived from the filename stem and is never stored
    inside the YAML body.
    """

    def __init__(self, providers_dir: Path) -> None:
        self._dir = Path(providers_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

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
        """Return all providers as (name, provider) tuples sorted by name."""
        result: list[tuple[str, OIDCProvider]] = []
        for path in sorted(self._dir.glob("*.yaml")):
            provider = self._read(path.stem)
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
