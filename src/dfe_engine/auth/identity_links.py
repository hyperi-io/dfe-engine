#  Project:      dfe-engine
#  File:         auth/identity_links.py
#  Purpose:      Which account an IdP sign-in links to by email, and the ticket a password link redeems
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which account an IdP sign-in links to by its verified email, and the ticket a password link redeems."""

from collections.abc import Collection, Iterable
from datetime import timedelta

from pydantic import BaseModel, Field

from dfe_engine.auth.accounts import Account, AccountStore, LinkedIdentity
from dfe_engine.auth.jwt_authority import JwtAuthority

LINK_TICKET_AUDIENCE = "dfe-engine-account-link"
"""The ``aud`` a link ticket carries, which no session token does, so the bearer path refuses it."""

LINK_TICKET_LIFETIME = timedelta(minutes=10)


class LinkTicket(BaseModel):
    """The IdP sign-in a local account's password links, as the callback saw it."""

    account: str
    """The account the sign-in's email matched."""
    email: str
    groups: list[str] = Field(default_factory=list)
    groups_unresolved: bool = False
    name: str = ""
    provider: str
    subject: str
    """The IdP ``sub`` claim."""


def asserted_groups(*, account: Account, subject: str) -> list[str]:
    """The identifiers the IdP last asserted for *subject*: its linked entry's on *account*, else *account*'s own list."""
    entry = next((identity for identity in account.identities if identity.subject == subject), None)
    return list(entry.groups) if entry is not None else list(account.groups)


def email_holders(
    *, accounts: Iterable[Account], email: str, protected: Collection[str]
) -> list[Account]:
    """The accounts holding *email*, ignoring case, vouched for or not. A link record, a soft-deleted account and a recovery credential never do."""
    wanted = email.strip().casefold()
    if not (wanted):
        return []
    return [
        account
        for account in accounts
        if account.email.strip().casefold() == wanted
        and not (account.is_link_record)
        and not (account.is_soft_deleted)
        and account.username not in protected
    ]


def link_candidates(
    *, accounts: Iterable[Account], email: str, protected: Collection[str]
) -> list[Account]:
    """The accounts an IdP sign-in vouched for *email* may link to.

    An account qualifies when it holds *email* (:func:`email_holders`) vouched for: an admin
    set it or a trusted IdP verified it, or it is a local account's email from before the
    flag existed. An email the owner set themselves never qualifies. A disabled or blocked
    account still qualifies, so the login it matches is refused rather than given an
    account of its own.
    """
    return [
        account
        for account in email_holders(accounts=accounts, email=email, protected=protected)
        if (account.email_verified is True)
        or ((not (account.source_provider)) and (account.email_verified is None))
    ]


def linked_identity(*, account: Account, key: str) -> LinkedIdentity | None:
    """The sign-in *account* lists under *key*, or None."""
    return next((identity for identity in account.identities if identity.key == key), None)


def linked_person(*, record: Account, store: AccountStore) -> Account | None:
    """The account the link record *record* points at, while it is no link record itself and still lists the record's sign-in."""
    person = store.get(record.linked_to)
    if (person is None) or (person.is_link_record):
        return None
    entry = linked_identity(account=person, key=record.username)
    return person if entry is not None and entry.subject == record.subject else None


def linked_through(*, account: Account, subject: str) -> bool:
    """Whether *account* lists a linked sign-in for the IdP subject *subject*."""
    return any(identity.subject == subject for identity in account.identities)


def mint_link_ticket(*, authority: JwtAuthority, ticket: LinkTicket) -> str:
    """Sign *ticket* for the link route. It carries no ``sub``, so it never names a session."""
    claims = {"aud": LINK_TICKET_AUDIENCE, **ticket.model_dump()}
    return authority.sign(claims=claims, expires_delta=LINK_TICKET_LIFETIME)


def read_link_ticket(*, authority: JwtAuthority, token: str) -> LinkTicket:
    """The ticket *token* carries, once its signature, issuer, expiry and audience check out, else ``InvalidTokenError``."""
    claims = authority.verify_for(audience=LINK_TICKET_AUDIENCE, token=token)
    return LinkTicket.model_validate(claims)


def trusted_email(*, email: str, email_verified: bool, trusted_domains: Collection[str]) -> bool:
    """Whether the IdP vouched for *email* and the provider is trusted for its domain."""
    local, at, domain = email.strip().rpartition("@")
    return email_verified and bool(local and at) and domain.casefold() in trusted_domains


def unlink_provider(*, org: str = "", provider: str, store: AccountStore) -> list[str]:
    """Unlink every sign-in through *provider*: each entry leaves its account, whose sessions end, and each link record is deleted. Returns the accounts changed.

    With *org*, only that org's accounts and records are touched, as an org's provider reaches no other.
    """
    changed = []
    for account in store.list():
        if (org) and (account.org != org):
            continue
        if account.is_link_record:
            if account.source_provider == provider:
                store.delete(account.username)
            continue
        kept = [identity for identity in account.identities if identity.provider != provider]
        if len(kept) == len(account.identities):
            continue
        store.update(account.username, identities=kept)
        store.end_sessions(account.username)
        changed.append(account.username)
    return changed


def with_identity(
    *, entry: LinkedIdentity, identities: Iterable[LinkedIdentity]
) -> list[LinkedIdentity]:
    """*identities* with *entry* in place of the one under its key, or added after them."""
    listed = list(identities)
    keys = [identity.key for identity in listed]
    if entry.key not in keys:
        return [*listed, entry]
    listed[keys.index(entry.key)] = entry
    return listed


def without_identity(*, identities: Iterable[LinkedIdentity], key: str) -> list[LinkedIdentity]:
    """*identities* without the one under *key*."""
    return [identity for identity in identities if identity.key != key]
