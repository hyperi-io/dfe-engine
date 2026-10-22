#  Project:      dfe-engine
#  File:         auth/oidc/registry.py
#  Purpose:      CRUD registries for OIDC provider configurations, and their boot seed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Registries for OIDC provider configurations: YAML files, or the document store.

The YAML registry persists each provider as ``{name}.yaml`` under the providers
directory. The provider name is the filename stem -- it is NOT stored inside the YAML
body. The document-store registry holds one document per provider, keyed on its name,
for engine replicas that share one store.

Example layout::

    oidc_providers/
        google-workspace.yaml   # { type: google, enabled: true, ... }
        entra-id.yaml

:func:`seed_providers` writes a directory of such files over either registry at boot.
"""

import builtins
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from scalo.logger import logger

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.store_names import store_key
from dfe_engine.http_errors import describe_http_error
from dfe_engine.names import VALID_NAME
from dfe_engine.store.documents import DocuStore
from dfe_engine.yaml_utils import YAMLError, yaml_dump, yaml_load

PROVIDERS_SKIPPED = "auth_oidc_providers_skipped_total"

PROVIDERS_COLLECTION = "oidc_providers"
"""The document-store collection holding one document per provider."""

ProviderSkipReason = Literal["unreadable", "invalid"]
"""Why a stored or seeded provider was left out.

- ``unreadable``: the file cannot be read as YAML.
- ``invalid``: it holds no valid provider, such as a list or a type the model refuses,
  or its file is not named for a provider.
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
            "Stored or seeded OIDC providers left out because they do not load, counted each time one starts failing",
            ["reason"],
        )

    def skipped(self, reason: ProviderSkipReason) -> None:
        """Record a stored or seeded provider that has started failing to load."""
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

    def put(self, name: str, provider: OIDCProvider) -> None:
        """Write *provider* as provider *name*, replacing any stored one whole.

        Raises:
            KeyError: No provider can hold *name*.
        """
        self._write(name, provider)

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


class _StoredProvider(OIDCProvider):
    """A provider as the document store holds it: keyed on its name, stamped on every write."""

    name: str
    updated_at: str = ""


class DocuStoreOIDCProviderRegistry:
    """OIDC providers in the document store, one document each -- the interface of :class:`OIDCProviderRegistry`.

    Engine replicas share the collection. The unique index on the name decides a
    create, so of two replicas creating one name exactly one succeeds. An update reads,
    applies and swaps on the revision it read, so a sync stamp and an admin's change
    landing together both hold. Every write stamps the document, so one that leaves its
    fields as they were, such as a rotated secret kept at the same path, still moves the
    collection's write counter (:meth:`revision`), which each replica's relying party is
    rebuilt on.

    Args:
        store: The engine's document store.
        collection: The collection the providers live in.
        metrics: Where a stored provider left out of :meth:`list` is counted.
    """

    def __init__(
        self,
        store: DocuStore,
        *,
        collection: str = PROVIDERS_COLLECTION,
        metrics: ProviderMetrics | None = None,
    ) -> None:
        """Open the collection, creating its unique index on the name."""
        self._store = store
        self._collection = collection
        self._metrics = metrics if metrics is not None else ProviderMetrics()
        # Documents already reported as failing, so a listing on every scheduler tick logs each once.
        self._skipped: set[str] = set()
        self._c = store.typed(
            collection, _StoredProvider, key="name", on_invalid=self._skip_invalid
        )

    def revision(self) -> int:
        """How many writes the providers have taken, whichever replica made them."""
        return self._store.revision(self._collection)

    def create(self, name: str, provider: OIDCProvider) -> OIDCProvider:
        """Store a new provider, refusing a name already held, by another replica too.

        Raises:
            ValueError: The name is not a provider name, or a provider already holds it.
        """
        if not VALID_NAME.match(name):
            raise ValueError(f"Invalid OIDC provider name: {name!r}")
        if not self._c.insert(_stored(name, provider)):
            raise ValueError(f"OIDC provider '{name}' already exists")
        return provider

    def get(self, name: str) -> OIDCProvider | None:
        """Return the named provider, or None when none holds the name or its document does not load."""
        stored = self._c.get(name)
        return _public(stored) if stored is not None else None

    def list(self) -> builtins.list[tuple[str, OIDCProvider]]:
        """Return all providers as (name, provider) tuples sorted by name.

        A document that does not load is left out, logged once and counted on
        ``auth_oidc_providers_skipped_total``, so one bad document does not stop the
        others from serving.
        """
        stored = self._c.list()
        self._skipped.difference_update(provider.name for provider in stored)
        return [(provider.name, _public(provider)) for provider in stored]

    def update(self, name: str, **fields: object) -> OIDCProvider:
        """Set *fields* on the stored provider, keeping whatever another replica wrote meanwhile.

        Raises:
            KeyError: No provider holds the name.
            DocumentConflictError: Other writes landed between every read and write.
        """
        try:
            stored = self._c.mutate(
                name, lambda current: current.model_copy(update={**fields, "updated_at": _now()})
            )
        except KeyError:
            raise KeyError(f"OIDC provider '{name}' not found") from None
        return _public(stored)

    def put(self, name: str, provider: OIDCProvider) -> None:
        """Write *provider* as provider *name*, replacing any stored one whole. Of two at once the last wins.

        Raises:
            KeyError: No provider can hold *name*.
        """
        self._c.put(store_key(name), _stored(name, provider))

    def delete(self, name: str) -> None:
        """Delete the named provider.

        Raises:
            KeyError: No provider holds the name.
        """
        if not self._c.delete(name):
            raise KeyError(f"OIDC provider '{name}' not found")

    def _skip_invalid(self, document: dict, exc: ValidationError) -> None:
        """Warn about and count a stored document no provider can be made from, once until it loads."""
        name = str(document.get("name", ""))
        if name in self._skipped:
            return
        self._skipped.add(name)
        self._metrics.skipped("invalid")
        # The validation text quotes the document's values, so the log takes the failure's class only.
        logger.warning(
            "OIDC provider skipped: its stored document does not load",
            provider=name,
            reason="invalid",
            **describe_http_error(exc),
        )


type ProviderRegistry = OIDCProviderRegistry | DocuStoreOIDCProviderRegistry
"""Either provider registry: YAML files, or the document store."""


def seed_providers(
    registry: ProviderRegistry, seed_dir: Path, *, metrics: ProviderMetrics | None = None
) -> builtins.list[str]:
    """Write each provider file in *seed_dir* over the stored provider of its name.

    Config wins: a seeded provider replaces the stored one whole on every boot, API
    edits and the sync record included, as copying the files into the auth directory
    did. A stored provider no file names is left alone. File ``<name>.yaml`` holds
    provider ``<name>``, and one that does not load is logged, counted and skipped. Two
    replicas booting at once write the same providers, and each ends stored once.

    Args:
        registry: Where the providers are written.
        seed_dir: The directory of provider files, usually a mounted ConfigMap.
        metrics: Where a file left out is counted.

    Returns:
        The names written, in name order.
    """
    if not seed_dir.is_dir():
        logger.info("No OIDC provider seed directory", seed_dir=str(seed_dir))
        return []
    metrics = metrics if metrics is not None else ProviderMetrics()
    seeded: builtins.list[str] = []
    for path in sorted(seed_dir.glob("*.yaml")):
        provider = _seed_file(path, metrics)
        if provider is None:
            continue
        registry.put(path.stem, provider)
        seeded.append(path.stem)
    logger.info("OIDC providers seeded", count=len(seeded), providers=seeded)
    return seeded


def _seed_file(path: Path, metrics: ProviderMetrics) -> OIDCProvider | None:
    """The provider seed file *path* holds, or None when it cannot be seeded, logged and counted."""
    if not VALID_NAME.match(path.stem):
        metrics.skipped("invalid")
        logger.warning("OIDC provider seed file skipped: not named for a provider", file=path.name)
        return None
    try:
        data = yaml_load(path)
    except (OSError, UnicodeDecodeError, YAMLError, RecursionError) as exc:
        _skip_seed_file(path, "unreadable", exc, metrics)
        return None
    try:
        return OIDCProvider.model_validate(data if data is not None else {})
    except ValidationError as exc:
        _skip_seed_file(path, "invalid", exc, metrics)
        return None


def _skip_seed_file(
    path: Path, reason: ProviderSkipReason, exc: Exception, metrics: ProviderMetrics
) -> None:
    metrics.skipped(reason)
    # The validation text quotes the file's values, so the log takes the failure's class only.
    logger.warning(
        "OIDC provider seed file skipped: it does not load",
        file=path.name,
        reason=reason,
        **describe_http_error(exc),
    )


def _stored(name: str, provider: OIDCProvider) -> _StoredProvider:
    return _StoredProvider(name=name, updated_at=_now(), **provider.model_dump())


def _public(stored: _StoredProvider) -> OIDCProvider:
    """*stored* without its name and write stamp."""
    return OIDCProvider.model_validate(stored.model_dump(exclude={"name", "updated_at"}))


def _now() -> str:
    return datetime.now(UTC).isoformat()
