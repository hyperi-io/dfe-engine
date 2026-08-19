#  Project:      dfe-engine
#  File:         api/e2e/seed/accounts.py
#  Purpose:      Account seeder primitives for e2e-server and sibling seeders
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local-account seeding. Public methods are the scripts; privates are reusable."""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed

_ADMIN_GROUP = "dfe-admins"
_DEFAULT_ADMIN_NAME = "admin"
_DEFAULT_PASSWORD = "changeme"
_DEFAULT_INITIAL_USER_NAME = "initial_user"


class Accounts(Seed):
    """Create or reset local accounts and their group memberships."""

    def seed_admin(
        self,
        name: str = _DEFAULT_ADMIN_NAME,
        *,
        password: str | None = None,
    ) -> bool:
        """Seed a local admin account in ``dfe-admins``.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_admin(
            name,
            password=_DEFAULT_PASSWORD if password is None else password,
        )

    def seed_initial_user(
        self,
        name: str = _DEFAULT_INITIAL_USER_NAME,
        *,
        password: str | None = None,
    ) -> bool:
        """Seed a local admin account in ``dfe-admins``.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_admin(
            name,
            password=_DEFAULT_PASSWORD if password is None else password,
        )

    def _ensure_admin(self, name: str = "admin", *, password: str) -> bool:
        """Create or reset a local admin in ``dfe-admins``.

        Returns True when the account was created, False when it was reset.
        """
        self._ensure_group(
            _ADMIN_GROUP,
            roles=["admin"],
            description="Full administrative access",
        )
        return self._ensure(name, password, groups=[_ADMIN_GROUP])

    def _ensure(self, name: str, password: str, *, groups: list[str]) -> bool:
        """Create or reset a local account and attach it to *groups*.

        Groups must already exist (use ``_ensure_group`` from a sibling seeder
        that knows the role mapping). Returns True when created.
        """
        created = self._upsert_local_account(name, password, groups=groups)
        for group_name in groups:
            self._ensure_membership(group_name, name)
        return created

    def _ensure_group(self, name: str, *, roles: list[str], description: str = "") -> None:
        """Create *name* if missing. Does not change an existing group's roles."""
        if self._group_store.get(name) is None:
            self._group_store.create(name, roles=roles, description=description)

    def _upsert_local_account(self, name: str, password: str, *, groups: list[str]) -> bool:
        """Create the account or reset password / merge groups / enable it."""
        existing = self._account_store.get(name)
        if existing is None:
            self._account_store.create(name, password, groups=list(groups))
            return True
        self._account_store.reset_password(name, password)
        merged = list(existing.groups)
        for group in groups:
            if group not in merged:
                merged.append(group)
        self._account_store.update(name, groups=merged, enabled=True)
        return False

    def _ensure_membership(self, group_name: str, username: str) -> None:
        """Idempotent group.members += username."""
        self._group_store.add_member(group_name, username)
