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
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.audit import (
    audit_jit_account_created,
    audit_jit_groups_updated,
    audit_jit_hdx_invited,
    audit_jit_team_assigned,
)
from dfe_engine.auth.groups import GroupStore

# Steady-state OIDC traffic calls ensure_account on EVERY request. last_login_at
# is a coarse "recently active" marker (not an audit trail), so refresh it at
# most this often instead of rewriting the account YAML on every request.
_LOGIN_REFRESH_SECONDS = 3600

# Role carried by the auto-provisioned per-domain org group (Task C). org_analyst
# is the org-scoped role from the scoped-RBAC redesign: tenant-scoped CH reads
# (row-filtered dfe_tenant_reader) + org-scoped HyperDX. Defined in roles.yaml.
_ORG_DOMAIN_ROLE = "org_analyst"


class JitProvisioner:
    def __init__(
        self,
        account_store: AccountStore,
        group_store: GroupStore,
        hyperdx_client=None,
        org_registry=None,
        role_config=None,
        *,
        per_group: bool = False,
        ga_team_name: str = "dfe",
    ) -> None:
        self._accounts = account_store
        self._groups = group_store
        self._hdx = hyperdx_client
        self._orgs = org_registry
        # RoleConfig: source of each principal's config-driven HyperDX access
        # (RoleConfig.effective_hyperdx). Task C gates provisioning on access != none.
        self._role_config = role_config
        # DFE_HYPERDX_PER_GROUP posture (Task B/D): False (GA) = one shared team
        # `ga_team_name`; True (post-GA) = per-org `customer-<org>` team.
        self._per_group = per_group
        self._ga_team_name = ga_team_name

    @staticmethod
    def sanitise_username(user_id: str) -> str:
        """Reduce an email/OIDC subject to a readable, filesystem-safe slug.

        This is the DISPLAY slug only - it is NOT injective (see account_key):
        jane@corp.com, jane.corp.com and Jane_corp~com all reduce to the same
        'jane-corp-com'.
        """
        return re.sub(r"[^a-z0-9-]", "-", user_id.lower()).strip("-")

    @staticmethod
    def account_key(user_id: str) -> str:
        """Stable, collision-resistant shadow-account key (the YAML filename stem).

        The readable slug alone is NOT injective: jane@corp.com, jane.corp.com
        and Jane_corp~com all collapse to 'jane-corp-com', which would make
        three distinct OIDC subjects share ONE shadow account (and one enabled
        flag / group set). We keep the slug for readability but append a short
        stable hash of the ORIGINAL subject, so the SAME subject always maps to
        the SAME account while DIFFERENT subjects map to DIFFERENT accounts.
        deps.py resolves the account back with this same function for the
        enabled check, so the round-trip holds.
        """
        slug = JitProvisioner.sanitise_username(user_id)
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
        return f"{slug}-{digest}" if slug else digest

    @staticmethod
    def _email_domain(user_id: str) -> str:
        """Lowercase email domain of an OIDC subject that IS an email, else ''.

        OIDC subjects are commonly the user's email (the HyperDX invite path
        already keys off ``@`` in the subject). An opaque/UUID subject has no
        domain, so we return '' and the caller skips domain-group association
        gracefully (no email -> no org binding).
        """
        if "@" not in user_id:
            return ""
        return user_id.rsplit("@", 1)[1].strip().lower()

    @staticmethod
    def _domain_group_name(domain: str) -> str:
        """Group name for an email domain: ``acme.com`` -> ``org_acme_com``.

        Non-alphanumerics collapse to underscores so the name is a safe YAML
        filename stem (the store keys groups by filename).
        """
        slug = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_")
        return f"org_{slug}"

    @staticmethod
    def _login_is_stale(last_login_at: str, now_iso: str) -> bool:
        """True when last_login_at is missing/unparseable or past the refresh window."""
        if not last_login_at:
            return True
        try:
            prev = datetime.fromisoformat(last_login_at)
            now = datetime.fromisoformat(now_iso)
        except ValueError:
            return True
        return (now - prev) >= timedelta(seconds=_LOGIN_REFRESH_SECONDS)

    def ensure_account(
        self,
        user_id: str,
        oidc_groups: list[str],
        source_provider: str,
    ) -> Account:
        """Create or update shadow account. Non-fatal on HyperDX failures."""
        safe_name = self.account_key(user_id)
        now = datetime.now(UTC).isoformat()

        existing = self._accounts.get(safe_name)
        if existing is not None:
            # Subsequent login. Only rewrite the account YAML when something
            # material changed - group membership, or last_login_at is stale
            # past the refresh window - so back-to-back requests for an
            # unchanged account do NOT each pay a read-modify-write (and
            # last_login_at tracks ~login time, not last-request time).
            if set(existing.groups) != set(oidc_groups):
                added = [g for g in oidc_groups if g not in existing.groups]
                removed = [g for g in existing.groups if g not in oidc_groups]
                self._accounts.update(safe_name, groups=oidc_groups, last_login_at=now)
                audit_jit_groups_updated(user_id, added, removed)
                return self._accounts.get(safe_name)
            if self._login_is_stale(existing.last_login_at, now):
                self._accounts.update(safe_name, last_login_at=now)
                return self._accounts.get(safe_name)
            # Unchanged and recently active — no write.
            return existing

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

        # First-login org association (Task C): tie this shadow account to the org
        # that owns its email domain, via an `org_<domain>` group. Idempotent, and
        # only on first login - so repeat logins never rewrite it (the account's
        # early-return no-op path below is preserved). Different users on the same
        # domain each hit this on THEIR first login: the group is created once,
        # then each new account is added as a member.
        self._ensure_domain_group(user_id, safe_name)

        # Resolve org_ids from groups
        org_ids = []
        for gname in oidc_groups:
            group = self._groups.get(gname)
            if group and group.org_ids:
                org_ids.extend(group.org_ids)

        audit_jit_account_created(user_id, source_provider, oidc_groups, org_ids)

        # HyperDX team assignment. resolve_hyperdx_team returns "" for a principal
        # whose effective HyperDX access is `none` (Task C: no team, no invite, and
        # dfe-ui shows no link), so a falsy team here IS the scope gate.
        team = self.resolve_hyperdx_team(oidc_groups)
        if team:
            audit_jit_team_assigned(user_id, team, "hyperdx-access")

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
        """Resolve the HyperDX team for a set of OIDC groups (Task C + D).

        Consolidated onto DFE_HYPERDX_PER_GROUP + the config-driven HyperDX access
        (RoleConfig.effective_hyperdx), replacing the hardcoded role->team dict:

        - a principal whose cumulative HyperDX access is ``none`` -> "" (Task C:
          no team, no provisioning),
        - GA posture (per_group False) -> the ONE shared team ``ga_team_name``,
        - per-group posture (per_group True) -> the principal's org team
          ``customer-<first org id>``, or "" when no org resolves.
        """
        if self._hyperdx_access(oidc_groups) == "none":
            return ""
        if not self._per_group:
            return self._ga_team_name
        org_ids = self._org_ids_for_groups(oidc_groups)
        return f"customer-{org_ids[0]}" if org_ids else ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_domain_group(self, user_id: str, member: str) -> None:
        """Ensure the shadow account's email-domain org group exists + is joined.

        The org-association half of the external-OIDC -> org -> tenant-isolation
        chain (Task C). Given a subject that is an email:

        - derive the domain (``jane@acme.com`` -> ``acme.com``); a non-email
          subject yields no domain and we skip (no org binding),
        - ensure a group ``org_<domain>`` exists. If a managed org CLAIMS the
          domain (``OrgRegistry.find_by_domain``), the group carries THAT org's
          org_ids -> the member's resolved org_ids include them -> the
          TenantScopedClient + CH row policy scope their reads. Unclaimed domain
          -> the group exists with NO org_ids (no tenant access),
        - add the shadow account (its account_key, ``member``) to the group.

        Idempotent: the group is created once (first login for the domain) and
        add_member only writes when the member is new, so repeat logins and
        additional same-domain users never duplicate or rewrite.
        """
        domain = self._email_domain(user_id)
        if not domain:
            return  # No email domain on the subject -> no org association.

        group_name = self._domain_group_name(domain)
        if self._groups.get(group_name) is None:
            org = self._orgs.find_by_domain(domain) if self._orgs is not None else None
            if org is not None:
                # Claimed domain: bind org_analyst at the OWNING ORG's scope, never
                # system. org_analyst is org-restricted by design (its reads must
                # not resolve system-wide) - a system-scoped grant would cover all
                # scopes and over-grant. The org's org_ids drive the CH tenant
                # setting so the member's reads are row-filtered to that org.
                scope = f"org:{org.name}"
                roles = [_ORG_DOMAIN_ROLE]
                org_ids = list(org.org_ids)
            else:
                # Unclaimed domain: the account exists but gets NO grants until an
                # org claims the domain (or an admin assigns a role). Assigning
                # org_analyst here would grant its reads to an unscoped account.
                scope = "system"
                roles = []
                org_ids = []
            try:
                self._groups.create(
                    group_name,
                    roles=roles,
                    description=f"Auto-provisioned org group for email domain {domain}",
                    members=[member],
                    org_ids=org_ids,
                    scope=scope,
                )
                return
            except ValueError:
                # Race: another login created it first - fall through to the
                # idempotent add_member so this account still joins.
                pass
        self._groups.add_member(group_name, member)

    def _roles_for_groups(self, oidc_groups: list[str]) -> set[str]:
        """Union of role names across the principal's known groups."""
        roles: set[str] = set()
        for gname in oidc_groups:
            group = self._groups.get(gname)
            if group:
                roles.update(group.roles)
        return roles

    def _org_ids_for_groups(self, oidc_groups: list[str]) -> list[str]:
        """Org ids across the principal's known groups (first-seen order)."""
        org_ids: list[str] = []
        for gname in oidc_groups:
            group = self._groups.get(gname)
            if group:
                org_ids.extend(group.org_ids)
        return org_ids

    def _hyperdx_access(self, oidc_groups: list[str]) -> str:
        """Cumulative HyperDX access level for the principal (config-driven).

        Uses RoleConfig.effective_hyperdx across the principal's group roles - never
        a hardcoded role list. When no role_config is wired (minimal construction,
        e.g. some tests) default to ``full`` so provisioning is not silently
        suppressed; the real app always wires role_config.
        """
        if self._role_config is None:
            return "full"
        roles = sorted(self._roles_for_groups(oidc_groups))
        return self._role_config.effective_hyperdx(roles).access

    def _resolve_team_api_key(self, team_name: str) -> str:
        """Look up the HyperDX team API key for the invite.

        Both team models store the key in an env var recorded on an org's
        ``hyperdx_team_api_key_env`` (written by OrgLifecycleManager). Per-group:
        ``customer-<org>`` maps to that org. GA: the shared team is not org-specific,
        so any org's stored env points at the one shared team's key.

        Returns:
            API key string, or empty string if unavailable.
        """
        if self._orgs is None:
            return ""

        env_var = ""
        if team_name.startswith("customer-"):
            org = self._orgs.get(team_name[len("customer-") :])
            env_var = org.hyperdx_team_api_key_env if org is not None else ""
        else:
            # GA shared team: reuse whichever org has already recorded the env var.
            for org in self._orgs.list():
                if org.hyperdx_team_api_key_env:
                    env_var = org.hyperdx_team_api_key_env
                    break

        return os.environ.get(env_var, "") if env_var else ""

    async def _invite_to_hdx(self, user_id: str, team_name: str, team_api_key: str) -> None:
        """Fire-and-forget coroutine to invite a user to a HyperDX team."""
        try:
            success = await self._hdx.invite_member(team_api_key, user_id)
            if success:
                audit_jit_hdx_invited(user_id, team_name)
        except Exception:
            pass  # Non-fatal — already logged inside invite_member
