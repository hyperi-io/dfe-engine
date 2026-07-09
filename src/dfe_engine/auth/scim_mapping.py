#  Project:      dfe-engine
#  File:         auth/scim_mapping.py
#  Purpose:      Pure mappers between engine Account/Group and SCIM 2.0 wire models
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pure, unit-testable mappers between the engine's local identity model
(:class:`dfe_engine.auth.accounts.Account`,
:class:`dfe_engine.auth.groups.Group`) and the SCIM 2.0 wire schema provided
by ``scim2-models``.

This layer is ADDITIVE: SCIM is a second face over the SAME gitops-backed
account/group stores. The mappers never touch a store - they translate values
only, so they can be tested in isolation.

Identity conventions:
    - SCIM ``User.id``       == engine ``Account.username`` (the store key)
    - SCIM ``User.userName`` == engine ``Account.username``
    - SCIM ``User.active``   == engine ``Account.enabled``
    - SCIM ``User.externalId`` == engine ``Account.external_id`` (IdP object id)
    - SCIM ``Group.id``          == engine ``Group.name`` (the store key)
    - SCIM ``Group.displayName`` == engine ``Group.name``
    - SCIM ``Group.externalId``  == engine ``Group.source_id`` (IdP group id)

``source_provider`` is stamped ``"scim"`` on anything provisioned through the
SCIM face so downstream tooling can tell IdP-owned records apart.
"""

from __future__ import annotations

import secrets
from datetime import datetime

from scim2_models import Group as ScimGroup
from scim2_models import GroupMember, GroupMembership, Meta
from scim2_models import User as ScimUser

from dfe_engine.auth.accounts import Account
from dfe_engine.auth.groups import Group

# Marker written to ``source_provider`` for anything the SCIM face provisions.
SCIM_SOURCE_PROVIDER = "scim"


def _parse_dt(value: str) -> datetime | None:
    """Best-effort parse of an ISO 8601 timestamp; None when absent/invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ── Account <-> SCIM User ────────────────────────────────────


def account_to_scim_user(
    account: Account,
    *,
    groups: list[str] | None = None,
    location: str | None = None,
) -> ScimUser:
    """Map an engine :class:`Account` to a SCIM :class:`User`.

    Args:
        account: The engine account to represent.
        groups: Group names the account belongs to (read-only membership view).
            Falls back to ``account.groups`` when not supplied.
        location: Absolute URL of this resource, for ``meta.location``.

    Returns:
        A populated SCIM ``User`` (password never emitted).
    """
    member_of = account.groups if groups is None else groups
    user = ScimUser(
        id=account.username,
        user_name=account.username,
        active=account.enabled,
        external_id=account.external_id or None,
        display_name=account.username,
        groups=[GroupMembership(value=g, display=g) for g in member_of] or None,
    )
    user.meta = Meta(
        resource_type="User",
        created=_parse_dt(account.created_at),
        last_modified=_parse_dt(account.updated_at),
        location=location,
    )
    return user


def scim_user_to_account_fields(user: ScimUser) -> dict[str, object]:
    """Extract the writable engine-account fields from a SCIM :class:`User`.

    The returned dict is shaped for :meth:`AccountStore.update` (and the
    ``username``/``password`` keys for :meth:`AccountStore.create`). SCIM
    ``groups`` is a read-only attribute on User - membership is managed via the
    Groups endpoint - so it is intentionally NOT returned here.

    Args:
        user: Inbound SCIM user (creation/replacement request).

    Returns:
        Dict with keys ``username``, ``enabled``, ``external_id``,
        ``source_provider`` and, when present on the wire, ``password``.
    """
    fields: dict[str, object] = {
        "username": user.user_name,
        # SCIM active defaults to enabled when the IdP omits it.
        "enabled": True if user.active is None else bool(user.active),
        "external_id": user.external_id or "",
        "source_provider": SCIM_SOURCE_PROVIDER,
    }
    if user.password:
        fields["password"] = user.password
    return fields


def generate_provisioning_password() -> str:
    """Return a strong random password for an SSO-provisioned account.

    IdP-provisioned users authenticate via the IdP, not a local password, but
    the store requires a bcrypt-hashable secret. This mints one they never use.
    """
    return secrets.token_urlsafe(32)


# ── Group <-> SCIM Group ─────────────────────────────────────


def group_to_scim_group(group: Group, *, location: str | None = None) -> ScimGroup:
    """Map an engine :class:`Group` to a SCIM :class:`Group`.

    Args:
        group: The engine group to represent.
        location: Absolute URL of this resource, for ``meta.location``.

    Returns:
        A populated SCIM ``Group`` with member references.
    """
    scim_group = ScimGroup(
        id=group.name,
        display_name=group.name,
        external_id=group.source_id or None,
        members=[GroupMember(value=m, display=m) for m in group.members] or None,
    )
    scim_group.meta = Meta(resource_type="Group", location=location)
    return scim_group


def scim_group_to_group_fields(group: ScimGroup) -> dict[str, object]:
    """Extract the writable engine-group fields from a SCIM :class:`Group`.

    Args:
        group: Inbound SCIM group (creation/replacement request).

    Returns:
        Dict with keys ``name``, ``members``, ``source_id`` and
        ``source_provider``. ``roles`` is not set by SCIM (IdPs do not manage
        DFE role bindings) - the caller supplies an empty default.
    """
    members = [m.value for m in (group.members or []) if m.value]
    return {
        "name": group.display_name,
        "members": members,
        "source_id": group.external_id or "",
        "source_provider": SCIM_SOURCE_PROVIDER,
    }
