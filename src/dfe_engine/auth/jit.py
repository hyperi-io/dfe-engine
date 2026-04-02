#  Project:      dfe-engine
#  File:         auth/jit.py
#  Purpose:      JIT provisioner — creates shadow accounts on first OIDC login
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""JIT provisioner — creates shadow accounts on first OIDC login."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime

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
        org_registry=None,
    ) -> None:
        self._accounts = account_store
        self._groups = group_store
        self._hdx = hyperdx_client
        self._orgs = org_registry

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

            # Fire-and-forget invite to HyperDX team on first login.
            # Only attempted when we have a client and the user_id looks like
            # an email address (OIDC subjects that are UUIDs won't work as invites).
            if self._hdx is not None and "@" in user_id:
                team_api_key = self._resolve_team_api_key(team)
                if team_api_key:
                    try:
                        loop = asyncio.get_event_loop()
                        # Store reference to prevent garbage collection of the task.
                        _task = loop.create_task(  # noqa: RUF006
                            self._invite_to_hdx(user_id, team, team_api_key)
                        )
                    except RuntimeError:
                        # No running event loop (e.g. tests) — skip silently
                        pass

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

    def _resolve_team_api_key(self, team_name: str) -> str:
        """Look up the HyperDX team API key for a customer org team.

        Only org-scoped teams (``customer-{org}`` prefix) have a stored API key.
        Broad role teams (``dfe-admin``, ``dfe-analysts``) use the admin key
        and are not individually invited.

        Returns:
            API key string, or empty string if unavailable.
        """
        if self._orgs is None or not team_name.startswith("customer-"):
            return ""

        org_name = team_name[len("customer-") :]
        org = self._orgs.get(org_name)
        if org is None or not org.hyperdx_team_api_key_env:
            return ""

        return os.environ.get(org.hyperdx_team_api_key_env, "")

    async def _invite_to_hdx(self, user_id: str, team_name: str, team_api_key: str) -> None:
        """Fire-and-forget coroutine to invite a user to a HyperDX team."""
        try:
            success = await self._hdx.invite_member(team_api_key, user_id)
            if success:
                audit_jit_hdx_invited(user_id, team_name)
        except Exception:
            pass  # Non-fatal — already logged inside invite_member
