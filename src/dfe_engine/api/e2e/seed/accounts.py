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
from dfe_engine.auth import account_durability

_ADMIN_GROUP = "dfe-admins"
_DFE_ANALYST_GROUP = "dfe-analysts"
_DFE_INFRA_GROUP = "dfe-infra"
_DFE_VIEWERS_GROUP = "dfe-viewers"
_WELL_KNOWN_E2E_PASSWORD = "changeme"
_SEED_ACTOR = "e2e-seed"


def _break_glass_admin_name() -> str:
    return "admin"


def _break_glass_admin_password() -> str:
    """Break-glass password (same fallbacks as auth bootstrap)."""
    return _WELL_KNOWN_E2E_PASSWORD


class Accounts(Seed):
    """Create or reset local accounts and their group memberships."""

    def seed_initial_user(
        self,
        name: str = "initial_user",
        *,
        password: str | None = None,
    ) -> bool:
        """Seed local initial user account.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_admin(
            name,
            password=_WELL_KNOWN_E2E_PASSWORD if password is None else password,
        )

    def reset_break_glass_admin(self) -> bool:
        """Reset the break-glass admin account."""
        name = _break_glass_admin_name()
        password = _break_glass_admin_password()

        return self._ensure_admin(
            name,
            password=password,
        )

    def seed_dfe_admin_user(self, name: str = "dfe_admin", *, password: str | None = None) -> bool:
        """Seed local DFE admin account.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_admin(
            name, password=_WELL_KNOWN_E2E_PASSWORD if password is None else password
        )

    def seed_dfe_analyst_user(
        self, name: str = "dfe_analyst", *, password: str | None = None
    ) -> bool:
        """Seed local DFE analyst account.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_dfe_analyst(
            name, password=_WELL_KNOWN_E2E_PASSWORD if password is None else password
        )

    def seed_dfe_infra_user(self, name: str = "dfe_infra", *, password: str | None = None) -> bool:
        """Seed local DFE infra account.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_dfe_infra(
            name, password=_WELL_KNOWN_E2E_PASSWORD if password is None else password
        )

    def seed_dfe_viewers_user(
        self, name: str = "dfe_viewers", *, password: str | None = None
    ) -> bool:
        """Seed local DFE viewers account.

        Omitting *password* uses the well-known e2e password ``changeme``.
        """
        return self._ensure_dfe_viewers(
            name, password=_WELL_KNOWN_E2E_PASSWORD if password is None else password
        )

    def delete_all(self) -> None:
        """Clear all accounts from the YAML account registry."""
        for account in self._account_store.list():
            self._account_store.delete(account.username)

    def _ensure_admin(self, name: str = "admin", *, password: str) -> bool:
        """Create or reset local break-glass admin account.

        Returns True when the account was created, False when it was reset.
        """
        self._ensure_group(_ADMIN_GROUP)
        return self._ensure(name, password, groups=[_ADMIN_GROUP])

    def _ensure_dfe_analyst(self, name: str = "dfe_analyst", *, password: str) -> bool:
        """Create or reset local DFE analyst account.

        Returns True when the account was created, False when it was reset.
        """
        self._ensure_group(_DFE_ANALYST_GROUP)
        return self._ensure(name, password, groups=[_DFE_ANALYST_GROUP])

    def _ensure_dfe_infra(self, name: str = "dfe_infra", *, password: str) -> bool:
        """Create or reset local DFE infra account.

        Returns True when the account was created, False when it was reset.
        """
        self._ensure_group(_DFE_INFRA_GROUP)
        return self._ensure(name, password, groups=[_DFE_INFRA_GROUP])

    def _ensure_dfe_viewers(self, name: str = "dfe_viewers", *, password: str) -> bool:
        """Create or reset local DFE viewers account.

        Returns True when the account was created, False when it was reset.
        """
        self._ensure_group(_DFE_VIEWERS_GROUP)
        return self._ensure(name, password, groups=[_DFE_VIEWERS_GROUP])

    def _ensure(self, name: str, password: str, *, groups: list[str]) -> bool:
        """Create or reset local account and attach it to *groups*.

        Groups must already exist -- ``bootstrap_auth`` seeds them on every
        startup, so guard with ``_ensure_group``. Returns True when created.
        """
        created = self._upsert_local_account(name, password, groups=groups)
        for group_name in groups:
            self._ensure_membership(group_name, name)
        self._mirror_to_deploy_repo(name)
        return created

    def _mirror_to_deploy_repo(self, name: str) -> None:
        """Persist the break-glass account into the deploy repo, as a real write does.

        The setup wizard will not call itself finished until the durable copy of
        the break-glass admin MATCHES the live one, and it compares the stored
        password hash. Writing only the live store therefore leaves every seeded
        e2e run parked on the rotate-the-break-glass step forever.

        The hash is read back from the store rather than re-derived, because
        bcrypt salts afresh every time: deriving it twice would produce two hashes
        of the same password that never compare equal.

        Only the break-glass admin is git-backed, and only when gitops is on --
        the same two conditions ``api/v1/accounts.py`` applies, through the same
        routing, so a seeded deployment and an operator's password reset leave the
        repo in the same shape.
        """
        if self._gitcrud is None or not account_durability.is_break_glass(name):
            return
        account = self._account_store.get(name)
        if account is None:
            return
        settings = self._require_settings()
        account_durability.publish_account(
            self._gitcrud,
            self._forge,
            environment=settings.env,
            mode=settings.gitops.mode,
            username=name,
            doc=account.model_dump(exclude={"username"}),
            summary="seed account",
            actor=_SEED_ACTOR,
        )

    def _ensure_group(self, name: str) -> None:
        """Error on a group the startup bootstrap did not create.

        Takes no roles/description: ``auth/bootstrap.py::_DEFAULT_GROUPS`` is the
        only source of truth for what each group grants.
        """
        if self._group_store.get(name) is None:
            raise ValueError(f"Group {name} does not exist")

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
