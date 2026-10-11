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
already linked to the provider group, through this provider or one bound to it.
A name match alone links nothing, because a directory's display names are not
unique and its users may choose them.

Usage::

    from dfe_engine.auth.oidc.sync import sync_provider

    result = await sync_provider("my-provider", provider_registry, group_store)
    # {"created": 3, "updated": 1, "total": 4, "groups_skipped": 0, "error": None,
    #  "skipped": None, "status": "ok"}
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.auth.groups import GroupExistsError
from dfe_engine.auth.membership import linked_providers
from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryCredentialUnreadableError,
    DirectoryNotConfiguredError,
    DirectoryPageError,
    DirectoryPageLimitError,
    DirectoryTokenRequestError,
    NoDirectoryApiError,
    NoDirectoryListingError,
)
from dfe_engine.auth.oidc.credential_env import unreadable_credential_message
from dfe_engine.auth.oidc.models import PARTIAL_SYNC_PREFIX, FixedSyncStatus
from dfe_engine.http_errors import (
    credential_failure_message,
    describe_http_error,
    http_failure_message,
)
from dfe_engine.names import VALID_NAME

if TYPE_CHECKING:
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.secrets import DfeSecrets

SYNC_GROUPS_SKIPPED = "auth_oidc_sync_groups_skipped_total"
SYNC_SCHEDULED_RUNS = "auth_oidc_sync_scheduled_runs_total"

SyncOutcome = Literal["ok", "partial", "error", "not_configured", "user_token", "skipped"]
"""How one sync run ended, as a sync result's ``status`` field.

- ``ok``: every group synced.
- ``partial``: some groups were left unsynced.
- ``error``: the run failed, including when the secret store holding the credential
  did not answer or refused access.
- ``not_configured``: the provider has no credential to read its directory with.
- ``user_token``: the provider reads each login's groups with the user's own token
  and has no directory to sync, which is a Google provider's default.
- ``skipped``: the provider is disabled or not in api mode.
"""

SYNC_NOT_CONFIGURED: FixedSyncStatus = "not_configured"
"""The ``last_sync_status`` of a provider whose directory credential is not configured."""

SYNC_USER_TOKEN: FixedSyncStatus = "user_token"  # noqa: S105, RUF100 - a sync status name
"""The ``last_sync_status`` of a provider that reads each login's groups with the user's own token."""

NOT_CONFIGURED_MESSAGE = (
    "The directory credential is not configured, so no groups were synced; "
    "configure it on the provider and sync again"
)
"""What a provider's ``sync_error`` and a sync's ``skipped`` say when there is no credential to list with."""

NO_DIRECTORY_MESSAGE = (
    "Each login reads that user's groups with their own token, so there is no directory to sync"
)
"""What a sync's ``skipped`` says for a provider with no directory listing; its ``sync_error`` stays empty."""

_DIRECTORY_API = "The directory API"


def _token_failure_message(exc: BaseException, *, service: str) -> str:
    """What a failed directory token request says: the token endpoint's owner, never the directory API."""
    if describe_http_error(exc).get("status") == 429:
        return f"{service} is throttling token requests (HTTP 429); retry shortly"
    return http_failure_message(exc, service=service)


def _listing_failure_message(exc: BaseException) -> str:
    """What the provider record and the API answer say about a failed listing, never the IdP's own text."""
    match exc:
        case NoDirectoryApiError():
            return (
                "This provider type has no directory API to list groups from; "
                "set groups.mode to token_claim"
            )
        case DirectoryCredentialUnreadableError(refused=refused):
            return unreadable_credential_message(credential="directory credential", refused=refused)
        case DirectoryTokenRequestError(service=service):
            return _token_failure_message(exc.__cause__ or exc, service=service)
        case DirectoryCredentialError():
            return credential_failure_message(service=_DIRECTORY_API)
        case DirectoryPageLimitError(pages=pages):
            return (
                f"{_DIRECTORY_API} was still handing out pages after {pages}, so the "
                "listing was abandoned; the engine log has the reason"
            )
        case DirectoryPageError():
            return (
                f"{_DIRECTORY_API} answered a listing page that was not a list of groups; "
                "the engine log has the reason"
            )
        case _:
            return http_failure_message(exc, service=_DIRECTORY_API)


SyncSkipReason = Literal[
    "invalid_name", "stored_unloadable", "name_taken", "provider_mismatch", "no_id"
]
"""Why the sync left a provider group unsynced.

- ``invalid_name``: its email, name or id makes no valid group name: empty, over 128
  characters, or not starting with a letter or digit.
- ``stored_unloadable``: a stored group that does not load already holds its name.
- ``name_taken``: a stored group holds its name and is not linked to it: the stored
  group carries no ``source_id``, or another provider group's id.
- ``provider_mismatch``: the stored group of its name carries its id but is linked to
  another provider, one ``auth.source_provider_bindings`` does not join to this one,
  and that provider keeps it.
- ``no_id``: the provider gave it no id, and only the id links a stored group to it.
"""

# The provider's last_sync_status names each reason that skipped a group.
_SKIP_STATUS: dict[SyncSkipReason, str] = {
    "stored_unloadable": "their stored copy does not load",
    "invalid_name": "their identifier makes no valid group name",
    "name_taken": "their name is held by a group not linked to them",
    "provider_mismatch": "their stored copy is linked to another provider",
    "no_id": "they carry no id to link them by",
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
    bindings: Mapping[str, str] | None = None,
) -> dict:
    """Run group sync for a single OIDC provider.

    Fetches all groups from the provider's API and upserts them into
    *group_store*.  A stored group linked to the provider group (its
    ``source_id`` is the group's id and its ``source_provider`` this provider,
    or one *bindings* joins to it) has its description updated, and keeps its
    roles and its stamp.  A stored group of the same name with no such link is
    left untouched and counted as ``name_taken``: linking one is an admin's act.
    One carrying the id under any other provider is left untouched too, counted
    as ``provider_mismatch``, so a sync never moves a group between providers.
    New groups are created with empty roles, linked to the provider group.

    Args:
        provider_name: Name of the provider in *provider_registry*.
        provider_registry: Registry to look up and update provider config.
        group_store: Store to upsert group records into.
        adapter: Optional adapter override, primarily for testing.  When
            ``None`` the adapter is resolved via ``get_adapter(provider)``.
        secrets: The DfeSecrets seam the directory credential resolves through
            when the adapter is built here.
        metrics: Where each group left unsynced is counted. ``None`` counts nothing.
        bindings: ``auth.source_provider_bindings``. A group stamped with a name
            bound to this provider, either way round, is this provider's.

    Returns:
        A dict with keys:

        - ``created`` (int): number of new groups created.
        - ``updated`` (int): number of existing groups updated.
        - ``total`` (int): total groups processed.
        - ``groups_skipped`` (int): groups left unsynced, one :data:`SyncSkipReason`
          each. Non-zero makes the provider's ``last_sync_status`` partial.
        - ``error`` (str | None): error message if the sync failed.
        - ``skipped`` (str | None): reason string if the provider was skipped, including
          :data:`NOT_CONFIGURED_MESSAGE` when it has no directory credential and
          :data:`NO_DIRECTORY_MESSAGE` when it has no directory to list.
        - ``status`` (:data:`SyncOutcome`): how the run ended. Callers key on this,
          never on the text of ``error`` or ``skipped``.
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
            "status": "error",
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
            "status": "skipped",
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
            "status": "skipped",
        }

    # Resolve adapter (lazy import to avoid circular dependency at module level)
    if adapter is None:
        from dfe_engine.auth.oidc.adapters import get_adapter

        adapter = get_adapter(provider, secrets=secrets)

    try:
        remote_groups = await adapter.list_all_groups()
    except NoDirectoryListingError:
        # The stamp keeps the scheduler from listing again until the next interval.
        logger.debug("OIDC group sync skipped -- no directory to list", provider=provider_name)
        provider_registry.update(
            provider_name,
            last_sync_at=datetime.now(UTC).isoformat(),
            last_sync_status=SYNC_USER_TOKEN,
            sync_error="",
        )
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "groups_skipped": 0,
            "error": None,
            "skipped": NO_DIRECTORY_MESSAGE,
            "status": "user_token",
        }
    except DirectoryNotConfiguredError:
        # The stamp keeps the scheduler from listing again until the next interval.
        logger.info("OIDC group sync skipped -- no directory credential", provider=provider_name)
        provider_registry.update(
            provider_name,
            last_sync_at=datetime.now(UTC).isoformat(),
            last_sync_status=SYNC_NOT_CONFIGURED,
            sync_error=NOT_CONFIGURED_MESSAGE,
        )
        return {
            "created": 0,
            "updated": 0,
            "total": 0,
            "groups_skipped": 0,
            "error": None,
            "skipped": NOT_CONFIGURED_MESSAGE,
            "status": "not_configured",
        }
    except Exception as exc:
        # The provider record and the API answer carry this, so the IdP's own text stays out.
        error_msg = _listing_failure_message(exc)
        logger.warning(
            "OIDC group sync failed",
            operation="list_all_groups",
            provider=provider_name,
            **describe_http_error(exc),
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
            "status": "error",
        }

    metrics = metrics if metrics is not None else SyncMetrics()
    own_stamps = linked_providers([provider_name], bindings or {})
    created = 0
    updated = 0
    skips: dict[SyncSkipReason, int] = dict.fromkeys(_SKIP_STATUS, 0)

    for group_info in remote_groups:
        # Only the id links a stored group to a provider group, so a group with none is skipped.
        if not group_info.id:
            metrics.skipped("no_id")
            skips["no_id"] += 1
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
        if existing.source_id != group_info.id:
            logger.warning(
                "OIDC group sync skipped a group whose name is held by a group not linked to it",
                provider=provider_name,
                group=group_name,
                group_id=group_info.id,
            )
            metrics.skipped("name_taken")
            skips["name_taken"] += 1
            continue
        # Two providers can assign one id, so a matching id under an unbound provider's link is not ours.
        if existing.source_provider not in own_stamps:
            logger.warning(
                "OIDC group sync skipped a group linked to another provider",
                provider=provider_name,
                linked_provider=existing.source_provider,
                reason="provider_mismatch",
            )
            metrics.skipped("provider_mismatch")
            skips["provider_mismatch"] += 1
            continue
        group_store.update(group_name, description=group_info.description)
        updated += 1

    if skips["no_id"]:
        # One line per run: without an id there is nothing stable to name each group by.
        logger.warning(
            "OIDC group sync skipped groups with no id",
            provider=provider_name,
            count=skips["no_id"],
        )

    total = created + updated
    skipped = sum(skips.values())
    outcome: SyncOutcome = "partial" if skipped else "ok"
    status = "ok"
    if skipped:
        why = "; ".join(text for reason, text in _SKIP_STATUS.items() if skips[reason])
        status = f"{PARTIAL_SYNC_PREFIX}{skipped} of {len(remote_groups)} groups skipped, {why}"

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
        "status": outcome,
    }
