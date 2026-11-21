#  Project:      dfe-engine
#  File:         auth/jit.py
#  Purpose:      JIT provisioner -- creates shadow accounts on first OIDC login
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""JIT provisioner -- creates shadow accounts on first OIDC login.

A login is admitted only when the account it binds to would hold a role, so an
IdP user in no linked group gets no account at all rather than one that every
route refuses.
"""

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from scalo.logger import logger

from dfe_engine.auth.accounts import (
    Account,
    AccountExistsError,
    AccountStore,
    LinkedIdentity,
    discard_created,
)
from dfe_engine.auth.audit import (
    audit_account_change,
    audit_identity_link_skipped,
    audit_identity_linked,
    audit_jit_account_created,
    audit_jit_groups_updated,
    audit_jit_hdx_invited,
    audit_jit_login_refused,
    audit_jit_team_assigned,
)
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.identity_links import (
    LinkTicket,
    email_holders,
    link_candidates,
    linked_identity,
    linked_person,
    with_identity,
)
from dfe_engine.auth.membership import groups_held_by, linked_groups, linked_providers
from dfe_engine.auth.models import AuthenticationError, Scope, ScopedGrant, platform_grants
from dfe_engine.auth.protected_accounts import resolve_floor
from dfe_engine.http_errors import describe_http_error
from dfe_engine.names import VALID_NAME
from dfe_engine.orgs.role_groups import org_role_groups_held

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

# Platform teams, broadest first. Only a role granted at SYSTEM scope earns one.
_ROLE_TO_TEAM = {
    "admin": "dfe-admin",
    "infra_admin": "dfe-admin",
    "data_analyst": "dfe-analysts",
}

# What the refused caller is told. Constant, because the handler returns str(exc)
# to the client and the collision detail would say which local names are taken.
_REFUSED_MESSAGE = "OIDC login refused"

# Says what fixes the refusal, never which DFE groups exist: the caller is unauthenticated.
_NO_ROLE_MESSAGE = (
    "OIDC login refused: no DFE group with a role is linked to your identity provider "
    "groups. Ask a DFE administrator to link one."
)

# Says what fixes it without naming the account: the caller is unauthenticated.
_EMAIL_HELD_MESSAGE = (
    "OIDC login refused: another DFE account already uses this email. Ask a DFE "
    "administrator to trust your email domain on this identity provider."
)

_EMAIL_UNTRUSTED_MESSAGE = (
    "OIDC login refused: this identity provider did not verify an email in a domain it "
    "is trusted for. Ask a DFE administrator to trust your email domain on it."
)

# A subject here takes an API key's groups (api/deps.py), so no IdP may assert one.
API_KEY_SUBJECT_PREFIX = "apikey:"


class JitIdentityCollisionError(AuthenticationError):
    """An IdP assertion resolved onto an account that identity does not own.

    Attributes:
        user_id: The IdP-asserted subject.
        source_provider: The provider that asserted it.
        reason: ``protected_account``, ``local_account``, ``provider_mismatch``,
            ``subject_mismatch``, ``other_org``, ``api_key_subject`` or ``link_stale``.
    """

    def __init__(self, user_id: str, source_provider: str, reason: str) -> None:
        """Record the refused subject, the provider that asserted it and why."""
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
        """Record the refused subject and why its account is unavailable."""
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
        """Record the refused subject."""
        super().__init__(_REFUSED_MESSAGE)
        self.user_id = user_id
        self.reason = "unusable_subject"


class JitNoRoleError(AuthenticationError):
    """The IdP identity would hold no DFE role, so the login neither creates nor admits an account.

    Attributes:
        user_id: The IdP-asserted subject.
        source_provider: The provider that asserted it.
        reason: ``no_role_granted``.
    """

    def __init__(self, user_id: str, source_provider: str) -> None:
        """Record the refused subject and the provider that asserted it."""
        super().__init__(_NO_ROLE_MESSAGE)
        self.user_id = user_id
        self.source_provider = source_provider
        self.reason = "no_role_granted"


class JitEmailHeldError(AuthenticationError):
    """A first sign-in that cannot link while another account holds its email, so it gets no account of its own either. ``reason`` is ``email_held``."""

    def __init__(self, user_id: str, source_provider: str) -> None:
        """Record the refused subject and the provider that asserted it."""
        super().__init__(_EMAIL_HELD_MESSAGE)
        self.user_id = user_id
        self.source_provider = source_provider
        self.reason = "email_held"


class JitEmailUntrustedError(AuthenticationError):
    """A callback sign-in whose email the IdP did not verify, or whose domain the provider is not trusted for. ``reason`` is ``email_untrusted``."""

    def __init__(self, user_id: str, source_provider: str) -> None:
        """Record the refused subject and the provider that asserted it."""
        super().__init__(_EMAIL_UNTRUSTED_MESSAGE)
        self.user_id = user_id
        self.source_provider = source_provider
        self.reason = "email_untrusted"


class JitLinkRequiredError(AuthenticationError):
    """The sign-in's trusted email matches one local account, which it links to only after that account's password.

    Nothing is written. ``account`` names the matched account and ``reason`` is ``link_required``.
    """

    def __init__(self, *, account: str, source_provider: str, user_id: str) -> None:
        """Record the matched account, the provider and the IdP subject."""
        super().__init__(
            "Sign in with this account's password to link your identity provider to it"
        )
        self.account = account
        self.reason = "link_required"
        self.source_provider = source_provider
        self.user_id = user_id


class JitProvisioner:
    """Creates and reconciles the account behind each IdP login, refusing those it may not admit."""

    def __init__(
        self,
        account_store: AccountStore,
        group_store: GroupStore,
        hyperdx_client=None,
        admin_name: str = "",
        source_provider_bindings: dict[str, str] | None = None,
        home_org: Callable[[], str | None] | None = None,
        provider_registry: OIDCProviderRegistry | None = None,
    ) -> None:
        """Bind the stores, the names no IdP may assert and the deployment's provider bindings.

        Args:
            account_store: Where accounts are stored.
            group_store: Where groups, and so every role, are stored.
            hyperdx_client: The client a first login's HyperDX invite goes through, if any.
            admin_name: The configured local admin name, protected like the default one.
            source_provider_bindings: ``auth.source_provider_bindings``.
            home_org: Your org's name as this replica knows it, None while none is known.
            provider_registry: The OIDC providers, whose org a sign-in through one lands in.
        """
        self._accounts = account_store
        self._groups = group_store
        self._hdx = hyperdx_client
        self._invite_tasks: set[asyncio.Task] = set()
        # One definition of what is protected, shared with the stores that enforce it.
        self._protected = resolve_floor(admin_name).usernames
        self._bindings = dict(source_provider_bindings or {})
        self._home_org = home_org
        self._providers = provider_registry

    @staticmethod
    def sanitise_username(user_id: str) -> str:
        """Convert email/OIDC subject to safe filename stem.

        Case and punctuation fold, so several subjects can share one stem. The
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
        groups: Sequence[Group] | None = None,
        keep_stored_groups: bool = False,
        email_verified: bool | None = None,
    ) -> Account:
        """Create or update shadow account. Non-fatal on HyperDX failures.

        ``email`` is the IdP-asserted address (OIDC ``email`` claim / ``X-Oidc-Email``)
        and ``name`` the IdP-asserted display name. Each is written on first create
        and reconciled on later logins when present. ``groups`` is every stored group
        when the caller has listed them for this request. Otherwise the login lists
        them once, since each listing reads every group file.

        ``keep_stored_groups`` is set when the IdP's directory gave no answer and the
        token carried no groups, so ``oidc_groups`` is unknown rather than empty. An
        existing account is then judged on its stored groups, which are not
        rewritten, and no session of it is ended. A first login has none to keep.

        ``email_verified`` says whether the IdP vouched for ``email`` and is trusted
        for its domain, None where the login cannot tell (the proxy headers). A first
        login with a trusted email links to the one account holding it
        (:func:`~dfe_engine.auth.identity_links.link_candidates`): at once when an IdP
        owns that account, after its password when it is local. A login through a
        sign-in linked earlier is recorded on the account it links to, which is what
        this returns.

        Raises:
            JitIdentityCollisionError: The asserted subject is in the API-key
                namespace, or resolved onto a recovery credential or onto an
                account this provider or this subject does not own. Nothing is
                written.
            JitSubjectUnusableError: The subject sanitises to no account name.
                Nothing is written.
            JitNoRoleError: No group the account would hold carries a role. A
                first login leaves no account. An existing account records the
                groups its IdP now asserts and has every session ended, as a
                logout ends them, so no token issued for it reads or refreshes a
                session. With ``keep_stored_groups`` neither happens. Nothing else
                is written.
            JitLinkRequiredError: A trusted email matches one local account. Nothing
                is written.
            JitEmailUntrustedError: A callback login whose email is unverified or
                outside the provider's trusted domains. Nothing is written.
            JitEmailHeldError: A first callback login that does not link while another
                account holds its email (not one its owner set). Nothing is written.
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
        # A callback sign-in passes False when its email is unverified or outside the provider's trusted domains.
        if email_verified is False:
            audit_jit_login_refused(user_id, source_provider, "email_untrusted")
            raise JitEmailUntrustedError(user_id, source_provider)
        now = datetime.now(UTC).isoformat()
        wanted_email = email.strip()
        wanted_name = name.strip()

        key, existing = self._session_account(safe_name, user_id)
        if (existing is not None) and (existing.is_link_record):
            self._require_same_identity(existing, user_id, source_provider)
            person = self._linked_person(record=existing)
            if person is not None:
                self._require_reached(
                    account=person, source_provider=source_provider, user_id=user_id
                )
                return self._reconcile_linked(
                    email=wanted_email,
                    groups=groups,
                    keep_stored_groups=keep_stored_groups,
                    now=now,
                    oidc_groups=oidc_groups,
                    person=person,
                    record=existing,
                    source_provider=source_provider,
                    user_id=user_id,
                )
            # Its account is gone or no longer lists it, so the sign-in starts again as a first login.
            logger.warning("JIT removed a link record nothing lists", key=key, user_id=user_id)
            self._accounts.delete(key)
            existing = None
        if existing is not None:
            self._require_same_identity(existing, user_id, source_provider)
            self._require_available(existing, user_id, source_provider)
            if keep_stored_groups:
                oidc_groups = list(existing.groups)
            updates: dict[str, object] = {}
            if set(existing.groups) != set(oidc_groups):
                added = [g for g in oidc_groups if g not in existing.groups]
                removed = [g for g in existing.groups if g not in oidc_groups]
                updates["groups"] = oidc_groups
                audit_jit_groups_updated(user_id, added, removed)
            listed = self._groups.list() if groups is None else groups
            holds_a_role = self._holds_a_role(
                key,
                oidc_groups,
                existing.source_provider,
                listed,
                identities=existing.identities,
                org=existing.org,
            )
            if not holds_a_role:
                # Groups the directory never answered for say nothing new, so no session ends on them.
                if not keep_stored_groups:
                    if updates:
                        self._accounts.update(key, groups=oidc_groups)
                    # A token issued before the refusal must neither read nor refresh a session.
                    self._accounts.end_sessions(key)
                    audit_account_change(
                        user_id, key, "sessions_ended", {"reason": "no_role_granted"}
                    )
                self._refuse_without_role(user_id, source_provider)
            updates.update(last_login_at=now, oidc_id=source_provider, subject=user_id)
            if wanted_email and existing.email != wanted_email:
                updates["email"] = wanted_email
                # A new address nobody vouched for must not keep the old one's proof.
                updates["email_verified"] = bool(email_verified)
            elif wanted_email and email_verified is not None:
                updates["email_verified"] = email_verified
            if wanted_name and existing.name != wanted_name:
                updates["name"] = wanted_name
            return self._accounts.update(key, allow_protected=False, **updates)

        listed = self._groups.list() if groups is None else groups
        if email_verified and wanted_email:
            linked = self._link_on_first_login(
                email=wanted_email,
                listed=listed,
                now=now,
                oidc_groups=oidc_groups,
                safe_name=safe_name,
                source_provider=source_provider,
                user_id=user_id,
            )
            if linked is not None:
                return linked
        if email_verified is not None:
            self._refuse_held_email(
                email=wanted_email, source_provider=source_provider, user_id=user_id
            )

        org = self._provider_org(provider=source_provider)
        # The race branch below reconciles an account under this name, so this covers it too. An org's provider lands it as that org's viewer.
        if (not (org)) and (
            not (self._holds_a_role(safe_name, oidc_groups, source_provider, listed))
        ):
            self._refuse_without_role(user_id, source_provider)

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
            if raced.is_link_record:
                person = self._linked_person(record=raced)
                if person is None:
                    raise
                return self._reconcile_linked(
                    email=wanted_email,
                    groups=listed,
                    keep_stored_groups=keep_stored_groups,
                    now=now,
                    oidc_groups=oidc_groups,
                    person=person,
                    record=raced,
                    source_provider=source_provider,
                    user_id=user_id,
                )
            self._require_available(raced, user_id, source_provider)
            race_updates: dict[str, object] = {
                "last_login_at": now,
                "oidc_id": source_provider,
                "subject": user_id,
            }
            if not keep_stored_groups:
                race_updates["groups"] = oidc_groups
            if wanted_email:
                race_updates["email"] = wanted_email
                race_updates["email_verified"] = bool(email_verified)
            if wanted_name:
                race_updates["name"] = wanted_name
            return self._accounts.update(safe_name, allow_protected=False, **race_updates)

        try:
            stamped = self._accounts.update(
                safe_name,
                external=True,
                source_provider=source_provider,
                oidc_id=source_provider,
                org=org,
                subject=user_id,
                last_login_at=now,
                email_verified=bool(email_verified and wanted_email),
            )
            if org:
                self._groups.add_member(f"{org}-viewers", safe_name)
        except Exception:
            # Unstamped, the account reads as a local one and refuses this identity at every login.
            discard_created(self._accounts, created)
            raise

        _, org_ids = self._resolve_grants(oidc_groups, source_provider, listed)

        audit_jit_account_created(user_id, source_provider, oidc_groups, org_ids)

        # HyperDX team assignment
        team = self.resolve_hyperdx_team(oidc_groups, source_provider, groups=listed)
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

        return stamped

    def complete_link(
        self, *, groups: Sequence[Group] | None = None, ticket: LinkTicket
    ) -> Account:
        """Link the ticket's sign-in to its local account, whose password the caller has checked, and return that account.

        The account must still be the one account the ticket's email links to, and still local. A sign-in that already links to it is recorded as a login, so a repeated redemption gets the same session. Otherwise ``JitIdentityCollisionError`` (``link_stale``), and an unavailable account or one still holding no role refuses as :meth:`ensure_account` does. Nothing is written on a refusal.
        """
        source_provider = ticket.provider
        user_id = ticket.subject
        safe_name = self.sanitise_username(user_id)
        self._refuse_protected(safe_name, user_id, source_provider)
        if not VALID_NAME.match(safe_name):
            raise JitSubjectUnusableError(user_id)
        now = datetime.now(UTC).isoformat()
        listed = self._groups.list() if groups is None else groups

        _key, existing = self._session_account(safe_name, user_id)
        if existing is not None:
            self._require_same_identity(existing, user_id, source_provider)
            person = self._linked_person(record=existing) if existing.is_link_record else None
            if (person is None) or (person.username != ticket.account):
                self._refuse_stale_link(source_provider=source_provider, user_id=user_id)
            self._require_reached(account=person, source_provider=source_provider, user_id=user_id)
            return self._reconcile_linked(
                email=ticket.email,
                groups=listed,
                keep_stored_groups=ticket.groups_unresolved,
                now=now,
                oidc_groups=ticket.groups,
                person=person,
                record=existing,
                source_provider=source_provider,
                user_id=user_id,
            )

        candidates = self._link_candidates(email=ticket.email, provider=source_provider)
        names = [candidate.username for candidate in candidates]
        if (names != [ticket.account]) or (candidates[0].source_provider):
            self._refuse_stale_link(source_provider=source_provider, user_id=user_id)
        entry = LinkedIdentity(
            email=ticket.email,
            groups=ticket.groups,
            key=safe_name,
            last_login_at=now,
            linked_at=now,
            provider=source_provider,
            subject=user_id,
        )
        return self._link(entry=entry, listed=listed, method="password", person=candidates[0])

    def _link(
        self, *, entry: LinkedIdentity, listed: Sequence[Group], method: str, person: Account
    ) -> Account:
        """Link *entry*'s sign-in to *person*: the link record at its name, then the entry on *person*.

        Refused, with nothing written, when *person* is unavailable or would still hold no role. A record written before the entry fails is removed, and an unlisted record binds no session meanwhile.
        """
        self._require_linkable(entry=entry, listed=listed, person=person)
        try:
            created = self._accounts.create(password="", username=entry.key)
        except AccountExistsError:
            # Another replica linked the same sign-in first: its session is the same account's.
            raced = self._accounts.get(entry.key)
            if (raced is None) or (raced.linked_to != person.username):
                raise
            self._require_same_identity(raced, entry.subject, entry.provider)
            return self._accounts.get(person.username) or person
        try:
            self._accounts.update(
                entry.key,
                external=True,
                last_login_at=entry.last_login_at,
                linked_to=person.username,
                oidc_id=entry.provider,
                # Unlinked, the record becomes an account of its own and stays in the person's org.
                org=person.org,
                source_provider=entry.provider,
                subject=entry.subject,
            )
            current = self._accounts.get(person.username) or person
            # oidc_id stays the person's own: deleting a provider soft-deletes the accounts it names.
            linked = self._accounts.update(
                person.username,
                identities=with_identity(entry=entry, identities=current.identities),
                last_login_at=entry.last_login_at,
            )
        except Exception:
            discard_created(self._accounts, created)
            raise
        audit_identity_linked(
            account=person.username,
            method=method,
            source_provider=entry.provider,
            user_id=entry.subject,
        )
        return linked

    def _link_on_first_login(
        self,
        *,
        email: str,
        listed: Sequence[Group],
        now: str,
        oidc_groups: list[str],
        safe_name: str,
        source_provider: str,
        user_id: str,
    ) -> Account | None:
        """Link a first login to the one account its trusted email matches, or None to give it its own.

        An account an IdP owns is linked at once. A local one raises ``JitLinkRequiredError`` once it is known to be available and to hold a role with this sign-in.
        """
        candidates = self._link_candidates(email=email, provider=source_provider)
        if not (candidates):
            return None
        if len(candidates) > 1:
            audit_identity_link_skipped(
                reason="ambiguous_email", source_provider=source_provider, user_id=user_id
            )
            return None
        person = candidates[0]
        entry = LinkedIdentity(
            email=email,
            groups=oidc_groups,
            key=safe_name,
            last_login_at=now,
            linked_at=now,
            provider=source_provider,
            subject=user_id,
        )
        if person.source_provider:
            return self._link(entry=entry, listed=listed, method="verified_email", person=person)
        self._require_linkable(entry=entry, listed=listed, person=person)
        raise JitLinkRequiredError(
            account=person.username, source_provider=source_provider, user_id=user_id
        )

    def _link_candidates(self, *, email: str, provider: str) -> list[Account]:
        """The accounts a sign-in through *provider* may link to by *email*, each one it reaches (:meth:`_reaches`)."""
        candidates = link_candidates(
            accounts=self._accounts.scan(), email=email, protected=self._protected
        )
        return [
            candidate
            for candidate in candidates
            if self._reaches(account=candidate, provider=provider)
        ]

    def _require_reached(self, *, account: Account, source_provider: str, user_id: str) -> None:
        """Refuse ``other_org`` when a sign-in through *source_provider* may not land on *account* (:meth:`_reaches`)."""
        if self._reaches(account=account, provider=source_provider):
            return
        audit_jit_login_refused(user_id, source_provider, "other_org")
        raise JitIdentityCollisionError(user_id, source_provider, "other_org")

    def _reaches(self, *, account: Account, provider: str) -> bool:
        """Whether a sign-in through *provider* may land on *account*: an org's provider on that org's accounts, a platform provider on root and your-org accounts."""
        org = self._provider_org(provider=provider)
        if org:
            return account.org == org
        home = (self._home_org() if self._home_org is not None else None) or ""
        return account.org in ("", home)

    def _provider_org(self, *, provider: str) -> str:
        """The org whose provider *provider* is, empty for a platform provider or a name no provider holds."""
        found = self._providers.get(provider) if self._providers is not None else None
        return found.org if found is not None else ""

    def _linked_person(self, *, record: Account) -> Account | None:
        """The account the link record *record* binds, never a recovery credential."""
        person = linked_person(record=record, store=self._accounts)
        if (person is None) or (person.username in self._protected):
            return None
        return person

    def _reconcile_linked(
        self,
        *,
        email: str,
        groups: Sequence[Group] | None,
        keep_stored_groups: bool,
        now: str,
        oidc_groups: list[str],
        person: Account,
        record: Account,
        source_provider: str,
        user_id: str,
    ) -> Account:
        """Record a linked sign-in's login on the identity entry of *person*, never on *person*'s own groups.

        Refused as :meth:`ensure_account` refuses an existing account: unavailable, or left holding no role, which also ends every session of *person* unless the directory gave no answer.
        """
        self._require_available(person, user_id, source_provider)
        entry = linked_identity(account=person, key=record.username)
        if entry is None:
            raise KeyError(record.username)
        asserted = list(entry.groups) if keep_stored_groups else list(oidc_groups)
        current = entry.model_copy(
            update={"email": email or entry.email, "groups": asserted, "last_login_at": now}
        )
        identities = with_identity(entry=current, identities=person.identities)
        listed = self._groups.list() if groups is None else groups
        holds_a_role = self._holds_a_role(
            person.username,
            person.groups,
            person.source_provider,
            listed,
            identities=identities,
            org=person.org,
        )
        if not (holds_a_role):
            if not (keep_stored_groups):
                self._accounts.update(person.username, identities=identities)
                # A token issued before the refusal must neither read nor refresh a session.
                self._accounts.end_sessions(person.username)
                audit_account_change(
                    user_id, person.username, "sessions_ended", {"reason": "no_role_granted"}
                )
            self._refuse_without_role(user_id, source_provider)
        if set(entry.groups) != set(asserted):
            added = [g for g in asserted if g not in entry.groups]
            removed = [g for g in entry.groups if g not in asserted]
            audit_jit_groups_updated(user_id, added, removed)
        # Re-read so an unlink or another sign-in's write made during this login is kept, not overwritten.
        fresh = self._accounts.get(person.username)
        if (fresh is None) or (linked_identity(account=fresh, key=record.username) is None):
            self._refuse_stale_link(source_provider=source_provider, user_id=user_id)
        identities = with_identity(entry=current, identities=fresh.identities)
        return self._accounts.update(person.username, identities=identities, last_login_at=now)

    @staticmethod
    def _refuse_stale_link(*, source_provider: str, user_id: str) -> NoReturn:
        """Refuse a password link whose account or sign-in name changed after its ticket was issued."""
        audit_jit_login_refused(user_id, source_provider, "link_stale")
        raise JitIdentityCollisionError(user_id, source_provider, "link_stale")

    def _require_linkable(
        self, *, entry: LinkedIdentity, listed: Sequence[Group], person: Account
    ) -> None:
        """Refuse linking *entry* to *person* while *person* is disabled or blocked, or would hold no role with it."""
        self._require_available(person, entry.subject, entry.provider)
        identities = with_identity(entry=entry, identities=person.identities)
        holds_a_role = self._holds_a_role(
            person.username,
            person.groups,
            person.source_provider,
            listed,
            identities=identities,
            org=person.org,
        )
        if not (holds_a_role):
            self._refuse_without_role(entry.subject, entry.provider)

    def _refuse_held_email(self, *, email: str, source_provider: str, user_id: str) -> None:
        """Refuse a first sign-in whose email another account holds, unless that account's owner set the email themselves.

        A self-set email never blocks, so nobody can lock another person out of their identity provider by claiming their address.
        """
        holders = [
            holder
            for holder in email_holders(
                accounts=self._accounts.scan(), email=email, protected=self._protected
            )
            if holder.source_provider or holder.email_verified is not False
        ]
        if not (holders):
            return
        audit_jit_login_refused(user_id, source_provider, "email_held")
        raise JitEmailHeldError(user_id, source_provider)

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
                another subject's, or outside the provider's reach (:meth:`_reaches`).
        """
        if not self._may_adopt(existing, source_provider):
            reason = "provider_mismatch" if existing.source_provider else "local_account"
        elif existing.subject and existing.subject != user_id:
            reason = "subject_mismatch"
        elif not (self._reaches(account=existing, provider=source_provider)):
            reason = "other_org"
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
            # IdP, so no binding can name it. A caller asserting no provider name
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

    def _holds_a_role(
        self,
        username: str,
        oidc_groups: list[str],
        stamp: str,
        groups: Sequence[Group],
        *,
        identities: Sequence[LinkedIdentity] = (),
        org: str = "",
    ) -> bool:
        """Whether the account *username*, stamped *stamp*, holds a role after this login.

        An account has no role of its own: its session's roles come from the groups
        ``membership.groups_held`` names, through the providers its own stamp
        answers for. A SCIM account adopted through a binding is stamped ``scim``,
        not the provider signing in, so the check takes the stamp the account
        carries. A linked group carrying no role, as the group sync creates them,
        grants nothing, so it admits nothing either. Nor does a group an account of
        *org* may not hold (:func:`org_role_groups_held`). *groups* is every stored
        group, and *identities* the sign-ins linked to the account.
        """
        held = groups_held_by(
            username, oidc_groups, stamp, groups, bindings=self._bindings, identities=identities
        )
        home = (self._home_org() if self._home_org is not None else None) or ""
        held = org_role_groups_held(account_org=org, groups=held, home_org=home)
        return any(group.roles for group in held)

    @staticmethod
    def _refuse_without_role(user_id: str, source_provider: str) -> NoReturn:
        """Refuse a login that would hold no role, and audit it.

        Raises:
            JitNoRoleError: Always.
        """
        audit_jit_login_refused(user_id, source_provider, "no_role_granted")
        raise JitNoRoleError(user_id, source_provider)

    def _linked(
        self, oidc_groups: list[str], source_provider: str, groups: Sequence[Group]
    ) -> list[Group]:
        """The stored groups the token's identifiers are linked to for *source_provider*.

        The lookup the session's roles resolve through (``membership.groups_held``), so
        org_ids and the HyperDX team cannot see a different group set from the roles.
        *groups* is every stored group.
        """
        providers = linked_providers([source_provider], self._bindings)
        return linked_groups(oidc_groups, groups, providers)

    def _resolve_grants(
        self, oidc_groups: list[str], source_provider: str, groups: Sequence[Group]
    ) -> tuple[list[ScopedGrant], list[str]]:
        """The scoped grants and org ids the token's groups carry.

        Roles bind where ``api/deps.py`` binds them: an org-scoped group's at that
        org's scope, a system group's system-wide. A group's orgs are its
        ``org_ids``, else the org that owns it. *groups* is every stored group.
        """
        grants: list[ScopedGrant] = []
        org_ids: list[str] = []
        for group in self._linked(oidc_groups, source_provider, groups):
            scope = Scope(type="org", id=group.scope_org) if group.scope_org else Scope()
            grants.extend(
                ScopedGrant(role=role, scope=scope, org_ids=group.org_markers)
                for role in group.roles
            )
            if group.org_ids:
                org_ids.extend(group.org_ids)
            elif group.scope_org:
                org_ids.append(group.scope_org)
        return grants, org_ids

    def resolve_hyperdx_team(
        self,
        oidc_groups: list[str],
        source_provider: str,
        *,
        groups: Sequence[Group] | None = None,
    ) -> str:
        """Determine the HyperDX team: a platform team, else the caller's org team.

        A platform team goes only to a grant ``platform_grants`` keeps, broadest first,
        which is the filter the CH group bindings and the HyperDX connection read share.
        An org-scoped group's roles cover its org alone, so its member gets that org's
        team whatever roles the group holds. Only groups linked to *source_provider*
        count (:func:`~dfe_engine.auth.membership.linked_groups`). ``groups`` is every
        stored group when the caller has listed them. Otherwise they are listed here.
        """
        listed = self._groups.list() if groups is None else groups
        grants, org_ids = self._resolve_grants(oidc_groups, source_provider, listed)
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
        """Invite the user to the fork's team. A refusal or failure is logged, never raised."""
        hdx = self._hdx
        # ensure_account schedules the invite only once a client is bound.
        if hdx is None:
            return
        try:
            success = await hdx.invite_member(user_id)
        except Exception as exc:
            # The login already succeeded, so the user must not lose it over HyperDX.
            logger.warning(
                "JIT HyperDX invite failed",
                user_id=user_id,
                team_name=team_name,
                **describe_http_error(exc),
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
