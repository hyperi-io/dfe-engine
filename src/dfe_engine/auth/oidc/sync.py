#  Project:      dfe-engine
#  File:         auth/oidc/sync.py
#  Purpose:      OIDC group sync runner for API-mode providers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC group sync runner.

Fetches all groups from a provider's API and upserts them into the local
GroupStore.  Only providers with ``groups.mode == "api"`` and
``enabled == True`` are processed.  A stored group is updated only when it is
already linked to the provider group.  A name match alone links nothing, because
a directory's display names are not unique and its users may choose them.

A provider group's id names one stored group. The group already carrying it is updated whatever its name, unless it is linked to another provider not bound to this one (``auth.source_provider_bindings``); a link with no provider is pinned to this one. Only an id no group carries creates one.

Usage::

    from dfe_engine.auth.oidc.sync import sync_provider

    result = await sync_provider("my-provider", provider_registry, group_store)
    # {"created": 3, "updated": 1, "total": 4, "groups_skipped": 0, "error": None, "skipped": None}
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.auth.groups import GroupExistsError
from dfe_engine.auth.membership import linked_providers
from dfe_engine.auth.oidc.adapters.base import DirectoryError, error_frames
from dfe_engine.auth.store_names import VALID_NAME

if TYPE_CHECKING:
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.secrets import DfeSecrets

SYNC_GROUPS_SKIPPED = "auth_oidc_sync_groups_skipped_total"
SYNC_SCHEDULED_RUNS = "auth_oidc_sync_scheduled_runs_total"

SyncOutcome = Literal["ok", "partial", "error"]
"""How one sync run ended: every group synced, some left unsynced, or the run failed."""

SyncSkipReason = Literal[
    "invalid_name", "stored_unloadable", "name_taken", "id_taken", "holder_unwritable"
]
"""Why the sync left a provider group unsynced.

- ``invalid_name``: its email, name or id makes no valid group name: empty, over 128
  characters, or not starting with a letter or digit.
- ``stored_unloadable``: a stored group that does not load already holds its name.
- ``name_taken``: a stored group holds its name and is not linked to it: the stored
  group carries no ``source_id``, or another provider group's id.
- ``id_taken``: a stored group carries its id as ``source_id`` and is linked to another provider not bound to this one.
- ``holder_unwritable``: the stored group carrying its id cannot be updated by its name, one no group can have.
"""

# Why a provider whose groups mode is not api has no directory to sync, for the operator who asked for one.
_MODE_SKIP_REASONS = {
    "manual": "Groups mode is 'manual': group membership is managed in DFE, so there is no directory to sync",
    "token_claim": "Groups mode is 'token_claim': groups come from each login's token, so there is no directory to sync; link a group to the IdP group by its source ID",
}

# The provider's last_sync_status names each reason that skipped a group.
_SKIP_STATUS: dict[SyncSkipReason, str] = {
    "stored_unloadable": "their stored copy does not load",
    "invalid_name": "their identifier makes no valid group name",
    "name_taken": "their name is held by a group not linked to them",
    "id_taken": "their id is held by a group linked to a provider not bound to this one",
    "holder_unwritable": "their id is held by a stored group the sync cannot update",
}


class SyncMetrics:
    """The OIDC group sync's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._skipped = manager.counter(
            SYNC_GROUPS_SKIPPED,
            "Provider groups an OIDC group sync left unsynced",
            ["reason"],
        )
        self._scheduled_runs = manager.counter(
            SYNC_SCHEDULED_RUNS,
            "OIDC group syncs the background scheduler ran, by provider and outcome",
            ["outcome", "provider"],
        )

    def scheduled_run(self, *, outcome: SyncOutcome, provider: str) -> None:
        """Record one sync the background scheduler ran."""
        if self._manager is None:
            return
        self._scheduled_runs.labels(outcome=outcome, provider=provider).inc()

    def skipped(self, reason: SyncSkipReason) -> None:
        """Record a provider group one sync run left unsynced."""
        if self._manager is None:
            return
        self._skipped.labels(reason=reason).inc()


def _record_sync_error(
    *, error: str, provider_name: str, provider_registry: OIDCProviderRegistry
) -> dict:
    """Stamp *provider_name* with a failed sync and *error*, and return the sync result that reports it."""
    provider_registry.update(
        last_sync_at=datetime.now(UTC).isoformat(),
        last_sync_status="error",
        name=provider_name,
        sync_error=error,
    )
    return {
        "created": 0,
        "updated": 0,
        "total": 0,
        "groups_skipped": 0,
        "error": error,
        "skipped": None,
    }


def _safe_name(value: str) -> str:
    """Convert a group email, name or ID to a safe filename stem.

    Lowercases the value and replaces any characters that are not
    alphanumeric, hyphens, underscores, or dots with a hyphen.
    Collapses consecutive hyphens and strips leading/trailing hyphens.
    """
    slug = value.lower()
    slug = re.sub(r"[^a-z0-9._-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


async def sync_provider(
    provider_name: str,
    provider_registry: OIDCProviderRegistry,
    group_store: GroupStore,
    adapter: OIDCGroupAdapter | None = None,
    secrets: DfeSecrets | None = None,
    metrics: SyncMetrics | None = None,
    *,
    bindings: Mapping[str, str] | None = None,
) -> dict:
    """Run group sync for a single OIDC provider.

    Fetches all groups from the provider's API and upserts them into *group_store*. A stored group linked to the provider group (its ``source_id`` is the group's id, whatever the group's name) has its description updated and keeps its roles and link; a link with no provider is pinned to this one. One whose ``source_provider`` names another provider not bound to this one is left untouched and counted as ``id_taken``. With no group carrying the id, a stored group of the same name is left untouched and counted as ``name_taken``: linking one is an admin's act. New groups are created with empty roles, linked to the provider group.

    Args:
        provider_name: Name of the provider in *provider_registry*.
        provider_registry: Registry to look up and update provider config.
        group_store: Store to upsert group records into.
        adapter: Optional adapter override, primarily for testing.  When
            ``None`` the adapter is resolved via ``get_adapter(provider)``.
        secrets: The DfeSecrets seam the directory credential resolves through
            when the adapter is built here.
        metrics: Where each group left unsynced is counted. ``None`` counts nothing.
        bindings: ``auth.source_provider_bindings``; a group stamped by a source bound to this provider is linked to it too.

    Returns:
        A dict with keys:

        - ``created`` (int): number of new groups created.
        - ``updated`` (int): number of existing groups updated.
        - ``total`` (int): total groups processed.
        - ``groups_skipped`` (int): groups left unsynced, one :data:`SyncSkipReason`
          each. Non-zero makes the provider's ``last_sync_status`` partial.
        - ``error`` (str | None): error message if the sync failed.
        - ``skipped`` (str | None): why the sync did not run, for the operator who asked for it: the provider is disabled or its groups mode is not ``api``. ``None`` when it ran.
    """
    # Resolve provider config
    provider = provider_registry.get(provider_name)
    if provider is None:
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "groups_skipped": 0,
            "error": f"Provider '{provider_name}' not found",
            "skipped": None,
        }

    # Skip disabled providers
    if not provider.enabled:
        logger.debug("OIDC sync skipped -- provider disabled", provider=provider_name)
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "groups_skipped": 0,
            "error": None,
            "skipped": "The provider is disabled",
        }

    # Skip non-API mode providers
    if provider.groups.mode != "api":
        mode = provider.groups.mode
        logger.debug(
            "OIDC sync skipped -- mode is not api",
            provider=provider_name,
            mode=mode,
        )
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "groups_skipped": 0,
            "error": None,
            "skipped": _MODE_SKIP_REASONS[mode],
        }

    # Resolve adapter (lazy import to avoid circular dependency at module level)
    if adapter is None:
        from dfe_engine.auth.oidc.adapters import get_adapter

        adapter = get_adapter(provider, secrets=secrets)

    try:
        remote_groups = await adapter.list_all_groups()
    except DirectoryError as exc:
        logger.warning("OIDC group sync failed", error=str(exc), provider=provider_name)
        return _record_sync_error(
            error=str(exc), provider_name=provider_name, provider_registry=provider_registry
        )
    except Exception as exc:
        # An unexpected error's text could quote a directory credential, so only its type and frames are logged and its type answered.
        logger.error(
            "OIDC group sync failed unexpectedly",
            error_type=type(exc).__name__,
            frames=error_frames(exc=exc),
            provider=provider_name,
        )
        return _record_sync_error(
            error=f"{provider.type} directory: {type(exc).__name__}",
            provider_name=provider_name,
            provider_registry=provider_registry,
        )

    metrics = metrics if metrics is not None else SyncMetrics()
    created = 0
    updated = 0
    skips: dict[SyncSkipReason, int] = dict.fromkeys(_SKIP_STATUS, 0)
    linked = group_store.by_source_id()
    own_providers = linked_providers(bindings=bindings or {}, names=[provider_name])

    for group_info in remote_groups:
        holder = linked.get(group_info.id)
        if holder is not None:
            if holder.source_provider and holder.source_provider not in own_providers:
                logger.warning(
                    "OIDC group sync skipped a group whose id is held by a group linked to a provider not bound to this one",
                    provider=provider_name,
                    group=holder.name,
                    group_id=group_info.id,
                )
                metrics.skipped("id_taken")
                skips["id_taken"] += 1
                continue
            # A link with no provider answers any provider, so the sync pins it to this one.
            source_provider = holder.source_provider or provider_name
            try:
                group_store.update(
                    description=group_info.description,
                    name=holder.name,
                    source_provider=source_provider,
                )
            except KeyError:
                # Only a name no group can have (or a group deleted since the listing) refuses its update.
                logger.warning(
                    "OIDC group sync skipped a group whose id is held by a stored group it cannot update",
                    provider=provider_name,
                    group=holder.name,
                    group_id=group_info.id,
                )
                metrics.skipped("holder_unwritable")
                skips["holder_unwritable"] += 1
                continue
            updated += 1
            continue

        # Determine a stable filename-safe group name.
        # Prefer email (most stable for Google/Entra), fall back to name, then id.
        raw_key = group_info.email or group_info.name or group_info.id
        group_name = _safe_name(raw_key)
        if not VALID_NAME.match(group_name):
            logger.warning(
                "OIDC group sync skipped a group whose identifier makes no valid group name",
                provider=provider_name,
                group_id=group_info.id,
            )
            metrics.skipped("invalid_name")
            skips["invalid_name"] += 1
            continue

        existing = group_store.get(group_name)
        if existing is None:
            try:
                group = group_store.create(
                    description=group_info.description,
                    name=group_name,
                    roles=[],
                    source_id=group_info.id,
                    source_provider=provider_name,
                )
            except GroupExistsError:
                # A stored group that does not load holds the name; the store counts the file.
                logger.warning(
                    "OIDC group sync skipped a group whose stored copy does not load",
                    provider=provider_name,
                    group=group_name,
                )
                metrics.skipped("stored_unloadable")
                skips["stored_unloadable"] += 1
                continue
            if group.source_id:
                linked[group.source_id] = group
            created += 1
            continue

        # No group carries the id and a name links nothing, since a directory user can choose one.
        logger.warning(
            "OIDC group sync skipped a group whose name is held by a group not linked to it",
            provider=provider_name,
            group=group_name,
            group_id=group_info.id,
        )
        metrics.skipped("name_taken")
        skips["name_taken"] += 1

    total = created + updated
    skipped = sum(skips.values())
    status = "ok"
    if skipped:
        why = "; ".join(text for reason, text in _SKIP_STATUS.items() if skips[reason])
        status = f"partial: {skipped} of {len(remote_groups)} groups skipped, {why}"

    # Update provider sync metadata
    provider_registry.update(
        provider_name,
        last_sync_at=datetime.now(UTC).isoformat(),
        last_sync_status=status,
        sync_error="",
    )

    logger.info(
        "OIDC group sync complete",
        provider=provider_name,
        created=created,
        updated=updated,
        total=total,
        groups_skipped=skipped,
    )

    return {
        "created": created,
        "updated": updated,
        "total": total,
        "groups_skipped": skipped,
        "error": None,
        "skipped": None,
    }
