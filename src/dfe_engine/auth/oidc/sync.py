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

Usage::

    from dfe_engine.auth.oidc.sync import sync_provider

    result = await sync_provider("my-provider", provider_registry, group_store)
    # {"created": 3, "updated": 1, "total": 4, "groups_skipped": 0, "error": None, "skipped": None}
"""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.auth.groups import GroupExistsError
from dfe_engine.auth.store_names import VALID_NAME

if TYPE_CHECKING:
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.secrets import DfeSecrets

SYNC_GROUPS_SKIPPED = "auth_oidc_sync_groups_skipped_total"

SyncSkipReason = Literal["invalid_name", "stored_unloadable", "name_taken"]
"""Why the sync left a provider group unsynced.

- ``invalid_name``: its email, name or id makes no valid group name: empty, over 128
  characters, or not starting with a letter or digit.
- ``stored_unloadable``: a stored group that does not load already holds its name.
- ``name_taken``: a stored group holds its name and is not linked to it: the stored
  group carries no ``source_id``, or another provider group's id.
"""

# The provider's last_sync_status names each reason that skipped a group.
_SKIP_STATUS: dict[SyncSkipReason, str] = {
    "stored_unloadable": "their stored copy does not load",
    "invalid_name": "their identifier makes no valid group name",
    "name_taken": "their name is held by a group not linked to them",
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

    def skipped(self, reason: SyncSkipReason) -> None:
        """Record a provider group one sync run left unsynced."""
        if self._manager is None:
            return
        self._skipped.labels(reason=reason).inc()


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
) -> dict:
    """Run group sync for a single OIDC provider.

    Fetches all groups from the provider's API and upserts them into
    *group_store*.  A stored group linked to the provider group (its
    ``source_id`` is the group's id) has its description and source
    metadata updated, and keeps its roles.  A stored group of the same name
    with no such link is left untouched and counted as ``name_taken``:
    linking one is an admin's act.  New groups are created with empty roles,
    linked to the provider group.

    Args:
        provider_name: Name of the provider in *provider_registry*.
        provider_registry: Registry to look up and update provider config.
        group_store: Store to upsert group records into.
        adapter: Optional adapter override, primarily for testing.  When
            ``None`` the adapter is resolved via ``get_adapter(provider)``.
        secrets: The DfeSecrets seam the directory credential resolves through
            when the adapter is built here.
        metrics: Where each group left unsynced is counted. ``None`` counts nothing.

    Returns:
        A dict with keys:

        - ``created`` (int): number of new groups created.
        - ``updated`` (int): number of existing groups updated.
        - ``total`` (int): total groups processed.
        - ``groups_skipped`` (int): groups left unsynced, one :data:`SyncSkipReason`
          each. Non-zero makes the provider's ``last_sync_status`` partial.
        - ``error`` (str | None): error message if the sync failed.
        - ``skipped`` (str | None): reason string if the provider was skipped.
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
            "skipped": "disabled",
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
            "skipped": f"mode is '{mode}'",
        }

    # Resolve adapter (lazy import to avoid circular dependency at module level)
    if adapter is None:
        from dfe_engine.auth.oidc.adapters import get_adapter

        adapter = get_adapter(provider, secrets=secrets)

    try:
        remote_groups = await adapter.list_all_groups()
    except Exception as exc:
        error_msg = str(exc)
        logger.warning(
            "OIDC group sync failed",
            provider=provider_name,
            error=error_msg,
        )
        provider_registry.update(
            provider_name,
            last_sync_at=datetime.now(UTC).isoformat(),
            last_sync_status="error",
            sync_error=error_msg,
        )
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "groups_skipped": 0,
            "error": error_msg,
            "skipped": None,
        }

    metrics = metrics if metrics is not None else SyncMetrics()
    created = 0
    updated = 0
    skips: dict[SyncSkipReason, int] = dict.fromkeys(_SKIP_STATUS, 0)

    for group_info in remote_groups:
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
                group_store.create(
                    name=group_name,
                    roles=[],
                    description=group_info.description,
                    source_provider=provider_name,
                    source_id=group_info.id,
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
            created += 1
            continue

        # A name match is no link: only the IdP group's own id links a group, and a user cannot choose that.
        if not group_info.id or existing.source_id != group_info.id:
            logger.warning(
                "OIDC group sync skipped a group whose name is held by a group not linked to it",
                provider=provider_name,
                group=group_name,
                group_id=group_info.id,
            )
            metrics.skipped("name_taken")
            skips["name_taken"] += 1
            continue
        group_store.update(
            group_name,
            description=group_info.description,
            source_provider=provider_name,
            source_id=group_info.id,
        )
        updated += 1

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
