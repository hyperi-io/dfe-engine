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
``enabled == True`` are processed.

Usage::

    from dfe_engine.auth.oidc.sync import sync_provider

    result = await sync_provider("my-provider", provider_registry, group_store)
    # {"created": 3, "updated": 1, "total": 4, "error": None, "skipped": None}
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hyperi_pylib.logger import logger

if TYPE_CHECKING:
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry


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
) -> dict:
    """Run group sync for a single OIDC provider.

    Fetches all groups from the provider's API and upserts them into
    *group_store*.  Existing groups have their description and source
    metadata updated; their roles are preserved.  New groups are created
    with empty roles.

    Args:
        provider_name: Name of the provider in *provider_registry*.
        provider_registry: Registry to look up and update provider config.
        group_store: Store to upsert group records into.
        adapter: Optional adapter override, primarily for testing.  When
            ``None`` the adapter is resolved via ``get_adapter(provider)``.

    Returns:
        A dict with keys:

        - ``created`` (int): number of new groups created.
        - ``updated`` (int): number of existing groups updated.
        - ``total`` (int): total groups processed.
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
            "error": f"Provider '{provider_name}' not found",
            "skipped": None,
        }

    # Skip disabled providers
    if not provider.enabled:
        logger.debug("OIDC sync skipped — provider disabled", provider=provider_name)
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "error": None,
            "skipped": "disabled",
        }

    # Skip non-API mode providers
    if provider.groups.mode != "api":
        mode = provider.groups.mode
        logger.debug(
            "OIDC sync skipped — mode is not api",
            provider=provider_name,
            mode=mode,
        )
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "error": None,
            "skipped": f"mode is '{mode}'",
        }

    # Resolve adapter (lazy import to avoid circular dependency at module level)
    if adapter is None:
        from dfe_engine.auth.oidc.adapters import get_adapter

        adapter = get_adapter(provider)

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
            "error": error_msg,
            "skipped": None,
        }

    created = 0
    updated = 0

    for group_info in remote_groups:
        # Determine a stable filename-safe group name.
        # Prefer email (most stable for Google/Entra), fall back to name, then id.
        raw_key = group_info.email or group_info.name or group_info.id
        group_name = _safe_name(raw_key)

        existing = group_store.get(group_name)
        if existing is None:
            group_store.create(
                name=group_name,
                roles=[],
                description=group_info.description,
            )
            group_store.update(
                group_name,
                source_provider=provider_name,
                source_id=group_info.id,
            )
            created += 1
        else:
            group_store.update(
                group_name,
                description=group_info.description,
                source_provider=provider_name,
                source_id=group_info.id,
            )
            updated += 1

    total = created + updated

    # Update provider sync metadata
    provider_registry.update(
        provider_name,
        last_sync_at=datetime.now(UTC).isoformat(),
        last_sync_status="ok",
        sync_error="",
    )

    logger.info(
        "OIDC group sync complete",
        provider=provider_name,
        created=created,
        updated=updated,
        total=total,
    )

    return {
        "created": created,
        "updated": updated,
        "total": total,
        "error": None,
        "skipped": None,
    }
