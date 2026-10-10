#  Project:      dfe-engine
#  File:         auth/oidc/scheduler.py
#  Purpose:      Background group sync for api-mode OIDC providers on their sync interval
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Background group sync for api-mode OIDC providers, each on its own ``sync_interval``."""

import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.auth.oidc.sync import (
    NOT_CONFIGURED_MESSAGE,
    SyncMetrics,
    SyncOutcome,
    sync_provider,
)

if TYPE_CHECKING:
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
    from dfe_engine.secrets import DfeSecrets

# The provider API refuses a shorter interval, and a provider file written out of band is held to it too.
MIN_SYNC_INTERVAL_SECONDS = 60


def sync_is_due(*, now: datetime, provider: OIDCProvider) -> bool:
    """Whether *provider* is an enabled api-mode provider whose sync interval has passed since its last sync.

    A provider never synced, or with a last-sync stamp that does not parse, is due.
    """
    if not (provider.enabled) or (provider.groups.mode != "api"):
        return False
    if not (provider.last_sync_at):
        return True
    try:
        last_sync = datetime.fromisoformat(provider.last_sync_at)
    except ValueError:
        return True
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=UTC)
    interval = max(provider.groups.sync_interval, MIN_SYNC_INTERVAL_SECONDS)
    return now >= last_sync + timedelta(seconds=interval)


def sync_outcome(*, result: dict[str, Any]) -> SyncOutcome:
    """How a ``sync_provider`` run ended: it failed, had no credential, left some groups unsynced, or synced every group."""
    if result.get("error"):
        return "error"
    if result.get("skipped") == NOT_CONFIGURED_MESSAGE:
        return "not_configured"
    if result.get("groups_skipped"):
        return "partial"
    return "ok"


class OidcSyncScheduler:
    """Syncs every api-mode provider whose interval has passed, checking once a tick.

    Every engine replica runs its own loop. Two replicas syncing one provider write the same groups, so a double run mostly costs duplicate directory API calls. A run that fails is held back for one sync interval, because a failure after the directory listing leaves ``last_sync_at`` unstamped and the provider would otherwise be listed again every tick.
    """

    # SHORTCUT: one loop per replica, move to a ClickHouse lease shaped like schema/lock.py when HPA deployments share a group store.

    def __init__(
        self,
        *,
        group_store: GroupStore,
        metrics: SyncMetrics | None,
        on_groups_created: Callable[[], None] | None,
        registry: OIDCProviderRegistry,
        secrets: DfeSecrets | None,
        tick_seconds: float,
    ) -> None:
        self._group_store = group_store
        self._last_outcomes: dict[str, SyncOutcome] = {}
        self._metrics = metrics if metrics is not None else SyncMetrics()
        self._on_groups_created = on_groups_created
        self._registry = registry
        self._retry_at: dict[str, datetime] = {}
        self._secrets = secrets
        self._tick_failing = False
        self._tick_seconds = tick_seconds

    def _held_back(self, *, name: str, now: datetime) -> bool:
        """Whether *name* failed recently enough that its retry waits for a full sync interval."""
        retry_at = self._retry_at.get(name)
        return retry_at is not None and now < retry_at

    def _log_transition(self, *, detail: str, name: str, outcome: SyncOutcome) -> None:
        """Log a provider's outcome only when it differs from its last one, so a steady state stays quiet."""
        previous = self._last_outcomes.get(name)
        self._last_outcomes[name] = outcome
        if outcome == previous:
            logger.debug("OIDC scheduled group sync", outcome=outcome, provider=name)
        elif outcome == "error":
            logger.warning("OIDC scheduled group sync failing", error=detail, provider=name)
        else:
            logger.info("OIDC scheduled group sync outcome changed", outcome=outcome, provider=name)

    async def _sync(self, *, name: str) -> tuple[SyncOutcome, str]:
        """Run one provider's sync and report how it ended, with the error text when it failed."""
        try:
            result = await sync_provider(
                group_store=self._group_store,
                metrics=self._metrics,
                provider_name=name,
                provider_registry=self._registry,
                secrets=self._secrets,
            )
        except Exception as exc:
            # One provider's failure must not stop the others; it is reported as an error outcome.
            return "error", f"{type(exc).__name__}: {exc}"
        if result.get("created") and self._on_groups_created is not None:
            self._on_groups_created()
        return sync_outcome(result=result), str(result.get("error") or "")

    async def run_forever(self) -> None:
        """Check for due providers every tick until cancelled, starting a tick plus jitter after start-up."""
        # The jitter keeps replicas that start together from checking in step; it is not a secret.
        jitter = random.uniform(a=0, b=self._tick_seconds)  # noqa: S311
        await asyncio.sleep(self._tick_seconds + jitter)
        while True:
            try:
                await self.run_once(now=datetime.now(UTC))
            except Exception:
                if not (self._tick_failing):
                    logger.exception("OIDC scheduled group sync tick failed; retrying every tick")
                self._tick_failing = True
            else:
                if self._tick_failing:
                    logger.info("OIDC scheduled group sync tick recovered")
                self._tick_failing = False
            await asyncio.sleep(self._tick_seconds)

    async def run_once(self, *, now: datetime) -> dict[str, SyncOutcome]:
        """Sync every provider due at *now*, one at a time, and return how each run ended."""
        outcomes = {}
        for name, _listed in self._registry.list():
            # Re-read just before running, so a provider another replica has just synced is not run twice.
            provider = self._registry.get(name)
            if (
                (provider is None)
                or not (sync_is_due(now=now, provider=provider))
                or (self._held_back(name=name, now=now))
            ):
                continue
            outcome, detail = await self._sync(name=name)
            if outcome == "error":
                interval = max(provider.groups.sync_interval, MIN_SYNC_INTERVAL_SECONDS)
                self._retry_at[name] = now + timedelta(seconds=interval)
            else:
                self._retry_at.pop(name, None)
            outcomes[name] = outcome
            self._metrics.scheduled_run(outcome=outcome, provider=name)
            self._log_transition(detail=detail, name=name, outcome=outcome)
        return outcomes
