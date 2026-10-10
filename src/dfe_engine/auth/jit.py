#  Project:      dfe-engine
#  File:         auth/jit.py
#  Purpose:      JIT provisioner -- creates shadow accounts on first OIDC login
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""JIT provisioner -- creates shadow accounts on first OIDC login."""

import asyncio
import re
from datetime import UTC, datetime

from scalo.logger import logger

from dfe_engine.auth.accounts import Account, AccountStore, discard_created
from dfe_engine.auth.audit import (
    audit_jit_account_created,
    audit_jit_groups_updated,
    audit_jit_hdx_invited,
    audit_jit_login_refused,
    audit_jit_team_assigned,
)
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.membership import linked_groups, linked_providers
from dfe_engine.auth.models import AuthenticationError, Scope, ScopedGrant, platform_grants
from dfe_engine.auth.oidc.idp_errors import describe_idp_error
from dfe_engine.auth.protected_accounts import resolve_floor
from dfe_engine.auth.store_names import VALID_NAME

# Platform teams, broadest first; only a role granted at SYSTEM scope earns one.
_ROLE_TO_TEAM = {
    "admin": "dfe-admin",
    "infra_admin": "dfe-admin",
    "data_analyst": "dfe-analysts",
}

# What the refused caller is told. Constant, because the handler returns str(exc)
# to the client and the collision detail would say which local names are taken.
_REFUSED_MESSAGE = "OIDC login refused"

# A subject here takes an API key's groups (api/deps.py), so no IdP may assert one.
API_KEY_SUBJECT_PREFIX = "apikey:"


class JitIdentityCollisionError(AuthenticationError):
    """An IdP assertion resolved onto an account that identity does not own.

    Attributes:
        user_id: The IdP-asserted subject.
        source_provider: The provider that asserted it.
        reason: ``protected_account``, ``local_account``, ``provider_mismatch``,
            ``subject_mismatch`` or ``api_key_subject``.
    """

    def __init__(self, user_id: str, source_provider: str, reason: str) -> None:
        super().__init__(_REFUSED_MESSAGE)
        self.user_id = user_id
        self.source_provider = source_provider
        self.reason = reason


class JitAccountUnavailableError(AuthenticationError):
    """The IdP identity's own account is disabled or blocked.

    Attributes:
        user_id: The IdP-asserted subject.
        reason: ``account_disabled`` or ``account_blocked``.
    """

    def __init__(self, user_id: str, reason: str) -> None:
        super().__init__("Account blocked" if reason == "account_blocked" else "Account disabled")
        self.user_id = user_id
        self.reason = reason


class JitSubjectUnusableError(AuthenticationError):
    """The IdP subject sanitises to no account name: empty, or longer than one may be.

    Attributes:
        user_id: The IdP-asserted subject.
        reason: ``unusable_subject``.
    """

    def __init__(self, user_id: str) -> None:
        super().__init__(_REFUSED_MESSAGE)
        self.user_id = user_id
        self.reason = "unusable_subject"


class JitProvisioner:
    def __init__(
        self,
        account_store: AccountStore,
        group_store: GroupStore,
        hyperdx_client=None,
        admin_name: str = "",
        source_provider_bindings: dict[str, str] | None = None,
    ) -> None:
        self._accounts = account_store
        self._groups = group_store
        self._hdx = hyperdx_client
        self._invite_tasks: set[asyncio.Task] = set()
        # One definition of what is protected, shared with the stores that enforce it.
        self._protected = resolve_floor(admin_name).usernames
        self._bindings = dict(source_provider_bindings or {})

    @staticmethod
    def sanitise_username(user_id: str) -> str:
        """Convert email/OIDC subject to safe filename stem.

        Case and punctuation fold, so several subjects can share one stem; the
        account's recorded ``subject`` is what tells them apart.
        """
        return re.sub(r"[^a-z0-9-]", "-", user_id.lower()).strip("-")

    def ensure_account(
        self,
        user_id: str,
        oidc_groups: list[str],
        source_provider: str,
        email: str = "",
        name: str = "",
    ) -> Account:
        """Create or update shadow account. Non-fatal on HyperDX failures.

        ``email`` is the IdP-asserted address (OIDC ``email`` claim / ``X-Oidc-Email``)
        and ``name`` the IdP-asserted display name. Each is written on first create
        and reconciled on later logins when present.

        Raises:
            JitIdentityCollisionError: The asserted subject is in the API-key
                namespace, or resolved onto a recovery credential or onto an
                account this provider or this subject does not own. Nothing is
                written.
            JitSubjectUnusableError: The subject sanitises to no account name.
                Nothing is written.
            Exception: Any other error is the store failing to record the login,
                never a refusal of the identity. A first login leaves no account.
        """
        if user_id.startswith(API_KEY_SUBJECT_PREFIX):
            audit_jit_login_refused(user_id, source_provider, "api_key_subject")
            raise JitIdentityCollisionError(user_id, source_provider, "api_key_subject")
        safe_name = self.sanitise_username(user_id)
        self._refuse_protected(safe_name, user_id, source_provider)
        if not VALID_NAME.match(safe_name):
            raise JitSubjectUnusableError(user_id)
        now = datetime.now(UTC).isoformat()
        wanted_email = email.strip()
        wanted_name = name.strip()

        key, existing = self._session_account(safe_name, user_id)
        if existing is not None:
            self._require_same_identity(existing, user_id, source_provider)
            self._require_available(existing, user_id, source_provider)
            # Subsequent login -- update groups if changed + last_login_at
            updates: dict[str, object] = {
                "last_login_at": now,
                "oidc_id": source_provider,
                "subject": user_id,
            }
            if set(existing.groups) != set(oidc_groups):
                added = [g for g in oidc_groups if g not in existing.groups]
                removed = [g for g in existing.groups if g not in oidc_groups]
                updates["groups"] = oidc_groups
                audit_jit_groups_updated(user_id, added, removed)
            if wanted_email and existing.email != wanted_email:
                updates["email"] = wanted_email
            if wanted_name and existing.name != wanted_name:
                updates["name"] = wanted_name
            self._accounts.update(key, **updates)
            return self._accounts.get(key)

        # First login -- create shadow account
        try:
            created = self._accounts.create(
                email=wanted_email,
                groups=oidc_groups,
                name=wanted_name,
                password="",
                username=safe_name,
            )
        except ValueError:
            # Race condition: another request created it. The identity guard runs
            # again because the account it created is the one about to be written.
            raced = self._accounts.get(safe_name)
            if raced is None:
                # Nothing holds the name, so this is a store fault, not a race.
                raise
            self._require_same_identity(raced, user_id, source_provider)
            self._require_available(raced, user_id, source_provider)
            race_updates: dict[str, object] = {
                "groups": oidc_groups,
                "last_login_at": now,
                "oidc_id": source_provider,
                "subject": user_id,
            }
            if wanted_email:
                race_updates["email"] = wanted_email
            if wanted_name:
                race_updates["name"] = wanted_name
            self._accounts.update(safe_name, **race_updates)
            return self._accounts.get(safe_name)

        try:
            self._accounts.update(
                safe_name,
                external=True,
                source_provider=source_provider,
                oidc_id=source_provider,
                subject=user_id,
                last_login_at=now,
            )
        except Exception:
            # Unstamped, the account reads as a local one and refuses this identity at every login.
            discard_created(self._accounts, created)
            raise

        _, org_ids = self._resolve_grants(oidc_groups, source_provider)

        audit_jit_account_created(user_id, source_provider, oidc_groups, org_ids)

        # HyperDX team assignment
        team = self.resolve_hyperdx_team(oidc_groups, source_provider)
        if team:
            audit_jit_team_assigned(user_id, team, "broadest-wins")

            # POST /team/invitation invites into the calling identity's own team, so
            # this lands the user in the engine's default HyperDX team, not in
            # ``team``. Only an email-shaped user resolved to an org team is invited.
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

    def _refuse_protected(self, safe_name: str, user_id: str, source_provider: str) -> None:
        """Refuse any assertion resolving onto a recovery credential.

        The floor under :meth:`_require_same_identity`, and unconditional: these
        two accounts are how an operator gets in when federation is broken or
        hostile, so no IdP may read, create or rewrite one whatever it claims.
        The raw subject is checked as well as the sanitised one, because the
        session binds to an account stored under the raw subject first.

        Raises:
            JitIdentityCollisionError: ``safe_name`` or ``user_id`` is the admin or
                break-glass name.
        """
        if safe_name not in self._protected and user_id not in self._protected:
            return
        audit_jit_login_refused(user_id, source_provider, "protected_account")
        raise JitIdentityCollisionError(user_id, source_provider, "protected_account")

    def _session_account(self, safe_name: str, user_id: str) -> tuple[str, Account | None]:
        """The account a session for *user_id* binds to, and the name it is stored under.

        The session looks the raw subject up before the sanitised one
        (``api.deps.account_for_session_subject``), so an account stored under the
        raw subject is the one this login reconciles. A stem-named shadow beside it
        would be a second account for one identity.
        """
        if user_id != safe_name:
            named = self._accounts.get(user_id)
            if named is not None:
                return user_id, named
        return safe_name, self._accounts.get(safe_name)

    def _require_same_identity(self, existing: Account, user_id: str, source_provider: str) -> None:
        """Refuse when the stored account is not this IdP identity's own.

        The account key is a sanitised subject with no provider in it, so without
        this a provider asserting somebody else's subject would rewrite their
        groups, email and display name. Two subjects from one provider can share a
        stem, so the account's recorded subject must be this one too.

        Raises:
            JitIdentityCollisionError: The account is local, another provider's,
                or another subject's.
        """
        if not self._may_adopt(existing, source_provider):
            reason = "provider_mismatch" if existing.source_provider else "local_account"
        elif existing.subject and existing.subject != user_id:
            reason = "subject_mismatch"
        else:
            return
        audit_jit_login_refused(user_id, source_provider, reason)
        raise JitIdentityCollisionError(user_id, source_provider, reason)

    def _may_adopt(self, existing: Account, source_provider: str) -> bool:
        """Whether *source_provider* owns the identity behind *existing*.

        Two ways to own it: the provider stamped the account itself on an earlier
        login, or the deployment bound the account's stamp to this provider
        (``auth.source_provider_bindings``) because a SCIM connector and an OIDC
        provider are the same IdP.
        """
        if not source_provider or not existing.source_provider:
            # An account carrying no stamp is a local credential that belongs to no
            # IdP, so no binding can name it; a caller asserting no provider name
            # owns nothing. Both are refused before any comparison runs.
            return False
        if existing.external and existing.source_provider == source_provider:
            return True
        return self._bindings.get(existing.source_provider) == source_provider

    @staticmethod
    def _require_available(existing: Account, user_id: str, source_provider: str) -> None:
        """Refuse when the operator has disabled or blocked this identity."""
        denied = existing.session_denied()
        if denied is None:
            return
        reason = "account_blocked" if existing.blocked else "account_disabled"
        audit_jit_login_refused(user_id, source_provider, reason)
        raise JitAccountUnavailableError(user_id, reason)

    def _linked(self, oidc_groups: list[str], source_provider: str) -> list[Group]:
        """The stored groups the token's identifiers are linked to for *source_provider*.

        The lookup the session's roles resolve through (``membership.groups_held``), so
        org_ids and the HyperDX team cannot see a different group set from the roles.
        """
        providers = linked_providers([source_provider], self._bindings)
        return linked_groups(oidc_groups, self._groups.list(), providers)

    def _resolve_grants(
        self, oidc_groups: list[str], source_provider: str
    ) -> tuple[list[ScopedGrant], list[str]]:
        """The scoped grants and org ids the token's groups carry.

        Roles bind where ``api/deps.py`` binds them: an org-scoped group's at that
        org's scope, a system group's system-wide. A group's orgs are its
        ``org_ids``, else the org that owns it.
        """
        grants: list[ScopedGrant] = []
        org_ids: list[str] = []
        for group in self._linked(oidc_groups, source_provider):
            scope = Scope(type="org", id=group.scope_org) if group.scope_org else Scope()
            grants.extend(ScopedGrant(role=role, scope=scope) for role in group.roles)
            if group.org_ids:
                org_ids.extend(group.org_ids)
            elif group.scope_org:
                org_ids.append(group.scope_org)
        return grants, org_ids

    def resolve_hyperdx_team(self, oidc_groups: list[str], source_provider: str) -> str:
        """Determine the HyperDX team: a platform team, else the caller's org team.

        A platform team goes only to a grant ``platform_grants`` keeps, broadest first,
        which is the filter the CH group bindings and the HyperDX connection read share.
        An org-scoped group's roles cover its org alone, so its member gets that org's
        team whatever roles the group holds. Only groups linked to *source_provider*
        count (:func:`~dfe_engine.auth.membership.linked_groups`).
        """
        grants, org_ids = self._resolve_grants(oidc_groups, source_provider)
        platform_roles = {grant.role for grant in platform_grants(grants)}
        for role, team in _ROLE_TO_TEAM.items():
            if role in platform_roles:
                return team
        if org_ids:
            return f"customer-{org_ids[0]}"
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
                **describe_idp_error(exc),
            )
            return
        if success:
            audit_jit_hdx_invited(user_id, team_name)
            return
        logger.warning(
            "JIT HyperDX invite not sent - HyperDX refused or failed the request",
            user_id=user_id,
            team_name=team_name,
        )
