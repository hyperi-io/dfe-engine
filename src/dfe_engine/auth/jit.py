#  Project:      dfe-engine
#  File:         auth/jit.py
#  Purpose:      JIT provisioner — creates shadow accounts on first OIDC login
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""JIT provisioner — creates shadow accounts on first OIDC login."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from scalo.logger import logger

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.audit import (
    audit_jit_account_created,
    audit_jit_groups_updated,
    audit_jit_hdx_invited,
    audit_jit_team_assigned,
)
from dfe_engine.auth.groups import GroupStore

# Broadest-wins precedence (highest first)
_BROAD_ROLES = {"admin", "infra_admin", "data_analyst"}
_ROLE_TO_TEAM = {
    "admin": "dfe-admin",
    "infra_admin": "dfe-admin",
    "data_analyst": "dfe-analysts",
}


class JitProvisioner:
    def __init__(
        self,
        account_store: AccountStore,
        group_store: GroupStore,
        hyperdx_client=None,
    ) -> None:
        self._accounts = account_store
        self._groups = group_store
        self._hdx = hyperdx_client
        self._invite_tasks: set[asyncio.Task] = set()

    @staticmethod
    def sanitise_username(user_id: str) -> str:
        """Convert email/OIDC subject to safe filename stem."""
        return re.sub(r"[^a-z0-9-]", "-", user_id.lower()).strip("-")

    def ensure_account(
        self,
        user_id: str,
        oidc_groups: list[str],
        source_provider: str,
    ) -> Account:
        """Create or update shadow account. Non-fatal on HyperDX failures."""
        safe_name = self.sanitise_username(user_id)
        now = datetime.now(UTC).isoformat()

        existing = self._accounts.get(safe_name)
        if existing is not None:
            # Subsequent login — update groups if changed + last_login_at
            if set(existing.groups) != set(oidc_groups):
                added = [g for g in oidc_groups if g not in existing.groups]
                removed = [g for g in existing.groups if g not in oidc_groups]
                self._accounts.update(safe_name, groups=oidc_groups, last_login_at=now)
                audit_jit_groups_updated(user_id, added, removed)
            else:
                self._accounts.update(safe_name, last_login_at=now)
            return self._accounts.get(safe_name)

        # First login — create shadow account
        try:
            self._accounts.create(safe_name, "", groups=oidc_groups)
            self._accounts.update(
                safe_name,
                external=True,
                source_provider=source_provider,
                last_login_at=now,
            )
        except ValueError:
            # Race condition: another request created it
            self._accounts.update(safe_name, groups=oidc_groups, last_login_at=now)
            return self._accounts.get(safe_name)

        # Resolve org_ids from groups
        org_ids = []
        for gname in oidc_groups:
            group = self._groups.get(gname)
            if group and group.org_ids:
                org_ids.extend(group.org_ids)

        audit_jit_account_created(user_id, source_provider, oidc_groups, org_ids)

        # HyperDX team assignment
        team = self.resolve_hyperdx_team(oidc_groups)
        if team:
            audit_jit_team_assigned(user_id, team, "broadest-wins")

            # Invites ride the machine JWT onto the fork's session-scoped
            # /team/invitation (the shared default team); org-scoped
            # email-shaped users only.
            if self._hdx is not None and "@" in user_id and team.startswith("customer-"):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # Nothing would ever await the coroutine off a running loop, and
                    # the invite is the user's only route to a HyperDX team.
                    logger.warning(
                        "JIT HyperDX invite skipped - no running event loop",
                        user_id=user_id,
                        team_name=team,
                    )
                else:
                    # The set holds a strong reference so the loop cannot drop the
                    # task mid-flight.
                    task = loop.create_task(self._invite_to_hdx(user_id, team))
                    self._invite_tasks.add(task)
                    task.add_done_callback(self._invite_tasks.discard)

        return self._accounts.get(safe_name)

    def resolve_hyperdx_team(self, oidc_groups: list[str]) -> str:
        """Determine HyperDX team. Broadest role wins."""
        all_roles: set[str] = set()
        all_org_ids: list[str] = []
        for gname in oidc_groups:
            group = self._groups.get(gname)
            if group:
                all_roles.update(group.roles)
                all_org_ids.extend(group.org_ids)

        # Check broad roles first (precedence order)
        for role in ("admin", "infra_admin", "data_analyst"):
            if role in all_roles:
                return _ROLE_TO_TEAM[role]

        # No broad role — org-scoped team
        if all_org_ids:
            return f"customer-{all_org_ids[0]}"

        return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _invite_to_hdx(self, user_id: str, team_name: str) -> None:
        """Invite the user to the fork's team; a refusal or failure is logged, never raised."""
        try:
            success = await self._hdx.invite_member(user_id)
        except Exception as exc:
            # The login already succeeded, so the user must not lose it over HyperDX.
            logger.warning(
                "JIT HyperDX invite failed",
                user_id=user_id,
                team_name=team_name,
                error=str(exc),
            )
            return
        if success:
            audit_jit_hdx_invited(user_id, team_name)
            return
        logger.warning(
            "JIT HyperDX invite refused - the user has no HyperDX team",
            user_id=user_id,
            team_name=team_name,
        )
