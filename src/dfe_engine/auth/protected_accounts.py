#  Project:      dfe-engine
#  File:         auth/protected_accounts.py
#  Purpose:      The protected-name floor: the writes that would remove the way back in
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The floor under the local admin and ``breakglass``: they keep their way back in.

Two accounts are how an operator gets into a deployment whose federation is
broken or hostile: the local admin seeded from config, and ``breakglass``, whose
hash lives in the deploy repo. Four writes take that away -- disabling the
account, deleting it, renaming it, and dropping it from the group that carries
the admin role -- and any of them leaves the deployment reachable only by whoever
broke it.

The floor is enforced in the STORES (:mod:`dfe_engine.auth.accounts`,
:mod:`dfe_engine.auth.groups`), not in each route, so a caller nobody has written
yet inherits it: SCIM, the native account and group routers, JIT provisioning and
the CLI all reach the same two classes. :class:`ProtectedAccountError` is an
:class:`~dfe_engine.auth.models.AuthorizationError`, so an unhandled one is
already a 403 rather than a 500.

The legitimate ways to switch either account off go through the deploy repo, not
the store: the bootstrap admin is retired with
``POST /api/v1/auth/setup/retire-admin`` (:mod:`dfe_engine.auth.admin_retirement`),
and ``breakglass`` is turned off with the ``breakglass.enabled`` governance flag
(:mod:`dfe_engine.auth.breakglass`). The reconcile paths that implement those pass
``allow_protected=True``.

The set keys on NAMES, which is a string coupling rather than a fact about the
account: ``Account`` carries no flag marking a recovery credential, so a rename of
the admin moves the floor only because ``settings.auth.local.admin_name`` moves
with it. Refs #505.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from dfe_engine.auth.models import AuthorizationError


class ProtectedAccountError(AuthorizationError):
    """A write would have left a recovery credential unable to log in."""


@dataclass(frozen=True, slots=True)
class ProtectedFloor:
    """The names a write may not lock out, and the group they hold the role through.

    Attributes:
        usernames: The local admin and ``breakglass``.
        group: The group whose roles make those accounts able to run the
            deployment; removing a protected member from it is refused.
    """

    usernames: frozenset[str]
    group: str

    def is_protected(self, username: str) -> bool:
        """Whether *username* is a recovery credential."""
        return username in self.usernames

    def check_account_update(
        self,
        username: str,
        fields: Mapping[str, object],
        current_groups: Collection[str],
    ) -> None:
        """Refuse a field update that disables a protected account or de-roles it.

        Args:
            username: The account being updated.
            fields: The update as the caller passed it.
            current_groups: The account's groups before the update.

        Raises:
            ProtectedAccountError: The update would disable the account or drop
                it from :attr:`group`.
        """
        if username not in self.usernames:
            return
        if "enabled" in fields and not fields["enabled"]:
            raise ProtectedAccountError(_disable_message(username))
        if fields.get("blocked"):
            raise ProtectedAccountError(_disable_message(username))
        if "groups" not in fields or self.group not in current_groups:
            return
        if self.group not in _as_names(fields["groups"]):
            raise ProtectedAccountError(_derole_message(username, self.group))

    def check_account_state(
        self,
        username: str,
        *,
        enabled: bool,
        blocked: bool = False,
        groups: Collection[str],
    ) -> None:
        """Refuse a whole-record write that lands a protected account locked out.

        The check for :meth:`~dfe_engine.auth.accounts.AccountStore.put`, which
        replaces the stored document rather than merging fields into it.

        Args:
            username: The account being written.
            enabled: The ``enabled`` value the write would store.
            blocked: The ``blocked`` value the write would store.
            groups: The group list the write would store.

        Raises:
            ProtectedAccountError: The record would be disabled or outside
                :attr:`group`.
        """
        if username not in self.usernames:
            return
        if not enabled or blocked:
            raise ProtectedAccountError(_disable_message(username))
        if self.group not in groups:
            raise ProtectedAccountError(_derole_message(username, self.group))

    def check_account_delete(self, username: str) -> None:
        """Refuse deleting a protected account.

        A rename is a create plus a delete, so this is also what stops one.

        Raises:
            ProtectedAccountError: *username* is a recovery credential.
        """
        if username in self.usernames:
            raise ProtectedAccountError(
                f"'{username}' is a recovery credential and cannot be deleted or renamed; "
                "retire the bootstrap admin through the setup API, or turn break-glass "
                "off with the breakglass.enabled governance flag"
            )

    def check_member_removal(self, group_name: str, usernames: Iterable[str]) -> None:
        """Refuse dropping a protected account from the admin-role group.

        Takes the whole removal set so a caller removing several members refuses
        the operation before applying any of it.

        Raises:
            ProtectedAccountError: *group_name* is :attr:`group` and one of
                *usernames* is a recovery credential.
        """
        if group_name != self.group:
            return
        for username in usernames:
            if username in self.usernames:
                raise ProtectedAccountError(_derole_message(username, group_name))

    def check_members_replaced(
        self,
        group_name: str,
        current: Collection[str],
        wanted: object,
    ) -> None:
        """Refuse a member-list replacement that drops a protected account.

        Args:
            group_name: The group being replaced.
            current: Its member list before the write.
            wanted: The member list the write would store, as the caller passed
                it -- anything that is not a list of names reads as empty, so a
                malformed payload refuses rather than slipping through.

        Raises:
            ProtectedAccountError: The replacement drops a recovery credential
                from :attr:`group`.
        """
        keeping = _as_names(wanted)
        self.check_member_removal(group_name, [m for m in current if m not in keeping])


def resolve_floor(admin_name: str = "") -> ProtectedFloor:
    """Build the floor for a deployment whose admin is named *admin_name*.

    Args:
        admin_name: ``settings.auth.local.admin_name``. Empty falls through to
            the default the bootstrap seeds.

    Returns:
        The floor to enforce, resolved once per store.
    """
    # Deferred: bootstrap and breakglass both import the account store, which
    # imports this module.
    from dfe_engine.auth.bootstrap import admin_account_name
    from dfe_engine.auth.breakglass import GROUP, USERNAME

    return ProtectedFloor(
        usernames=frozenset({admin_account_name(admin_name), USERNAME}),
        group=GROUP,
    )


def _as_names(value: object) -> list[str]:
    """A group- or member-list payload as names; anything else reads as empty.

    ``**fields`` is untyped at the store boundary, and a string is iterable, so a
    payload that is not a list of names must read as empty -- that refuses the
    write rather than letting a substring match satisfy the floor.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        return []
    return [str(item) for item in value]


def _disable_message(username: str) -> str:
    """Refusal text for a write that would disable a recovery credential."""
    return (
        f"'{username}' is a recovery credential and cannot be disabled here; "
        "retire the bootstrap admin through the setup API, or turn break-glass "
        "off with the breakglass.enabled governance flag"
    )


def _derole_message(username: str, group_name: str) -> str:
    """Refusal text for a write that would strip a recovery credential of its role."""
    return (
        f"'{username}' is a recovery credential and cannot be removed from "
        f"'{group_name}'; it would authenticate with no role"
    )
