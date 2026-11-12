#  Project:      dfe-engine
#  File:         api/e2e/seed/accounts.py
#  Purpose:      Account seeder primitives for e2e-server and sibling seeders
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local-account seeding. Public methods are the scripts. Privates are reusable."""

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.auth import account_durability
from dfe_engine.auth.bootstrap import admin_account_name

_ADMIN_GROUP = "dfe-admins"
_DFE_ANALYST_GROUP = "dfe-analysts"
_DFE_INFRA_GROUP = "dfe-infra"
_DFE_VIEWERS_GROUP = "dfe-viewers"
_WELL_KNOWN_E2E_PASSWORD = "changeme"  # noqa: S105, RUF100 - e2e seed
_SEED_ACTOR = "e2e-seed"
_SEARCHABLE_COUNT = 50
_OIDC_LINKED_ADMIN = "dfe_admin"


def searchable_account_names() -> list[str]:
    """Usernames one searchable-account script writes."""
    return [f"searchable-account-{num}" for num in range(_SEARCHABLE_COUNT)]


def searchable_blocked_account_names() -> list[str]:
    """Usernames the blocked searchable-account script writes."""
    return [f"searchable-blocked-{num}" for num in range(_SEARCHABLE_COUNT)]


def searchable_api_key_names() -> list[str]:
    """Names one searchable API-key script writes."""
    return [f"searchable-api-key-{num}" for num in range(_SEARCHABLE_COUNT)]


def searchable_group_names() -> list[str]:
    """Names one searchable-group script writes."""
    return [f"searchable-group-{num}" for num in range(_SEARCHABLE_COUNT)]


def searchable_role_names() -> list[str]:
    """Names one searchable-role script writes."""
    return [f"searchable-role-{num}" for num in range(_SEARCHABLE_COUNT)]


def searchable_oidc_provider_names() -> list[str]:
    """Names one searchable OIDC-provider script writes."""
    return [f"searchable-oidc-{num}" for num in range(_SEARCHABLE_COUNT)]


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
        """Reset the bootstrap admin to a fresh deployment's: issued, and due a forced change."""
        name = admin_account_name(
            self._settings.auth.local.admin_name if self._settings is not None else ""
        )
        password = _break_glass_admin_password()

        return self._ensure_admin(name, password=password, change_required=True)

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

    def seed_searchable_accounts(self) -> bool:
        """Seed fifty local accounts a list search can filter.

        Returns True when any account was written, False when they were all already there.
        These stay in the account store. They are not the local admin, so nothing is
        published to the deploy repo.
        """
        return self._seed_searchable(searchable_account_names(), blocked=False)

    def seed_searchable_blocked_accounts(self) -> bool:
        """Seed fifty blocked local accounts a list search can filter.

        Returns True when any account was written or newly blocked, False when they
        were all already blocked. Same store-only rule as the unblocked script.
        """
        return self._seed_searchable(searchable_blocked_account_names(), blocked=True)

    def seed_searchable_api_keys(self) -> bool:
        """Seed fifty API keys a list search can filter.

        Returns True when any key was written, False when they were all already there.
        Keys stay in the API-key store. The full token is discarded, and nothing is
        published to the deploy repo. They do not expire.
        """
        store = self._require_api_key_store()
        self._ensure_group(_DFE_VIEWERS_GROUP)
        changed = False
        for num, name in enumerate(searchable_api_key_names()):
            if store.get(name) is not None:
                continue
            store.create(name, groups=[_DFE_VIEWERS_GROUP], description=f"Searchable API Key {num}")
            changed = True
        return changed

    def delete_searchable_api_keys(self) -> None:
        """Revoke the keys this seeder writes. A no-op when no key store is configured."""
        store = self._api_key_store
        if store is None:
            return
        for name in searchable_api_key_names():
            key = store.get(name)
            if key is None:
                continue
            store.revoke(key.short_token)

    def seed_searchable_groups(self) -> bool:
        """Seed fifty groups a list search can filter.

        Returns True when any group was written, False when they were all already there.
        Each group grants nothing. They stay in the group store and are not published
        to the deploy repo.
        """
        changed = False
        for num, name in enumerate(searchable_group_names()):
            if self._group_store.get(name) is not None:
                continue
            self._group_store.create(name, [], description=f"Searchable Group {num}")
            changed = True
        return changed

    def delete_searchable_groups(self) -> None:
        """Remove the groups this seeder writes. Bootstrap groups stay."""
        for name in searchable_group_names():
            group = self._group_store.get(name)
            if group is None:
                continue
            for member in list(group.members):
                self._group_store.remove_member(name, member)
            self._group_store.delete(name)

    def seed_searchable_roles(self) -> bool:
        """Seed fifty custom roles a list search can filter.

        Returns True when any role was written, False when they were all already there.
        Each role grants nothing. Built-in roles in the same file stay as they are.
        """
        store = self._require_role_store()
        changed = False
        for num, name in enumerate(searchable_role_names()):
            if store.get(name) is not None:
                continue
            store.create(name, description=f"Searchable Role {num}", permissions=[])
            changed = True
        return changed

    def delete_searchable_roles(self) -> None:
        """Remove the roles this seeder writes. A no-op when no role store is configured.

        Built-in roles stay. A reset that cleared the roles file would take them too.
        """
        store = self._role_store
        if store is None:
            return
        for name in searchable_role_names():
            if store.get(name) is None:
                continue
            store.delete(name)

    def seed_searchable_oidc_providers(self) -> bool:
        """Seed fifty disabled OIDC providers a list search can filter.

        Returns True when any provider was written, False when they were all already there.
        They stay in the provider registry. Disabled, with no issuer and no secret, so
        login and group sync leave them alone. Nothing is published to the deploy repo.
        """
        from dfe_engine.auth.oidc.models import OIDCProvider

        registry = self._require_oidc_registry()
        changed = False
        for num, name in enumerate(searchable_oidc_provider_names()):
            if registry.get(name) is not None:
                continue
            registry.create(
                name,
                OIDCProvider(enabled=False, display_name=f"Searchable Provider {num}"),
            )
            changed = True
        return changed

    def seed_searchable_oidc_users(self) -> bool:
        """Attach the searchable accounts and ``dfe_admin`` to the first searchable OIDC provider.

        Returns True when any account was attached, False when they already were.
        The provider is ``searchable-oidc-0`` and must already be in the registry,
        and the accounts come from the searchable and blocked searchable account
        seeds. ``dfe_admin`` is linked too, because the provider-deletion e2e spec
        needs a signed-in admin that goes with it. No other account is touched. A
        local password on these stays usable: the attachment is the provider stamp
        the account list filters on.
        """
        registry = self._require_oidc_registry()
        provider = searchable_oidc_provider_names()[0]
        if registry.get(provider) is None:
            raise RuntimeError(
                f"this e2e seed attaches accounts to {provider}, which is not in the "
                "OIDC provider registry; run seed_searchable_oidc_providers first"
            )
        changed = False
        seeded = [
            *searchable_account_names(),
            *searchable_blocked_account_names(),
            _OIDC_LINKED_ADMIN,
        ]
        for name in seeded:
            account = self._account_store.get(name)
            if account is None or account.is_soft_deleted:
                continue
            if account.oidc_id == provider and account.source_provider == provider:
                continue
            self._account_store.update(
                account.username,
                oidc_id=provider,
                source_provider=provider,
            )
            changed = True
        return changed

    def delete_searchable_oidc_providers(self) -> None:
        """Remove the providers this seeder writes. A no-op when no registry is configured."""
        registry = self._oidc_registry
        if registry is None:
            return
        for name in searchable_oidc_provider_names():
            if registry.get(name) is None:
                continue
            registry.delete(name)

    def delete_all(self) -> None:
        """Clear all accounts from the YAML account registry.

        Reaches past the protected-name floor: this is the e2e-server store wipe,
        mounted only where ``e2e_routes_enabled``, and ``reset_all`` puts the local
        admin and ``breakglass`` back after it.
        """
        for account in self._account_store.list():
            self._account_store.delete(account.username, allow_protected=True)

    def restore_breakglass(self) -> bool:
        """Put ``breakglass`` back the way a first boot seeds it.

        A fresh gitops deployment has the account, and :meth:`delete_all` removed
        it. The hash is minted from ``DFE_ENGINE_AUTH_BREAKGLASS_PASSWORD`` when the deploy
        repo carries none, so every reset leaves the password the engine was
        configured with. With no deploy repo, or neither a hash nor a configured
        password, there is nothing to seed from.

        Returns:
            True when the account was put back.
        """
        from dfe_engine.auth import breakglass

        local = self._settings.auth.local if self._settings is not None else None
        digest = breakglass.seed(
            self._account_store,
            self._group_store,
            self._gitcrud,
            local.breakglass_password if local is not None else "",
            recovery_email=local.recovery_email if local is not None else "",
        )
        return bool(digest)

    def _ensure_admin(
        self, name: str = "admin", *, password: str, change_required: bool = False
    ) -> bool:
        """Create or reset a local account in the admin group.

        Returns True when the account was created, False when it was reset.
        """
        self._ensure_group(_ADMIN_GROUP)
        return self._ensure(name, password, groups=[_ADMIN_GROUP], change_required=change_required)

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

    def _ensure(
        self, name: str, password: str, *, groups: list[str], change_required: bool = False
    ) -> bool:
        """Create or reset local account and attach it to *groups*.

        Groups must already exist -- ``bootstrap_auth`` seeds them on every
        startup, so guard with ``_ensure_group``. Returns True when created.
        ``change_required`` issues the password, so the account must replace it
        at its next login. Otherwise it is the account's own.
        """
        created = self._upsert_local_account(
            name, password, groups=groups, change_required=change_required
        )
        for group_name in groups:
            self._ensure_membership(group_name, name)
        self._mirror_to_deploy_repo(name)
        return created

    def _mirror_to_deploy_repo(self, name: str) -> None:
        """Persist the break-glass account into the deploy repo, as a real write does.

        ``setup-status`` reports the local admin as durable only when the
        copy in the deploy repo MATCHES the live one, and it compares the stored
        password hash. Writing only the live store therefore leaves every seeded
        e2e run reporting an undurable break-glass account.

        The hash is read back from the store rather than re-derived, because
        bcrypt salts afresh every time: deriving it twice would produce two hashes
        of the same password that never compare equal.

        Only the local admin's doc is git-backed, and only when gitops is on --
        the same two conditions ``api/v1/accounts.py`` applies, through the same
        routing, so a seeded deployment and an operator's password reset leave the
        repo in the same shape.
        """
        if self._gitcrud is None:
            return
        settings = self._require_settings()
        if not account_durability.is_local_admin(name, settings.auth.local.admin_name):
            return
        account = self._account_store.get(name)
        if account is None:
            return
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

    def _upsert_local_account(
        self, name: str, password: str, *, groups: list[str], change_required: bool = False
    ) -> bool:
        """Create the account or reset password / merge groups / enable it."""
        from dfe_engine.auth.bootstrap import admin_account_name, seeded_account_email
        from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME

        existing = self._account_store.get(name)
        recovery = self._settings.auth.local.recovery_email if self._settings is not None else ""
        email = (
            seeded_account_email(name, recovery)
            if name in {admin_account_name(), BREAKGLASS_USERNAME}
            else ""
        )
        if existing is None:
            self._account_store.create(
                name, password, groups=list(groups), email=email, change_required=change_required
            )
            return True
        self._account_store.reset_password(name, password, change_required=change_required)
        merged = list(existing.groups)
        for group in groups:
            if group not in merged:
                merged.append(group)
        updates: dict[str, object] = {"groups": merged, "enabled": True}
        if email and (not existing.email or (recovery.strip() and existing.email != email)):
            updates["email"] = email
        self._account_store.update(name, allow_protected=False, **updates)
        return False

    def _ensure_membership(self, group_name: str, username: str) -> None:
        """Idempotent group.members += username."""
        self._group_store.add_member(group_name, username)

    def _seed_searchable(self, names: list[str], *, blocked: bool) -> bool:
        """Write *names* into the viewers group. Returns whether any account changed."""
        self._ensure_group(_DFE_VIEWERS_GROUP)
        changed = False
        for num, name in enumerate(names):
            if self._upsert_searchable(name, num, blocked=blocked):
                changed = True
        return changed

    def _upsert_searchable(self, name: str, num: int, *, blocked: bool) -> bool:
        """Create one searchable account, or block an existing one. Returns whether it changed."""
        label = "Searchable Blocked" if blocked else "Searchable Account"
        existing = self._account_store.get(name)
        if existing is None:
            self._account_store.create(
                name,
                _WELL_KNOWN_E2E_PASSWORD,
                groups=[_DFE_VIEWERS_GROUP],
                email=f"{name}@e2e.local",
                name=f"{label} {num}",
            )
            self._ensure_membership(_DFE_VIEWERS_GROUP, name)
            if blocked:
                self._account_store.update(name, blocked=True)
            return True
        if blocked and not existing.blocked:
            self._account_store.update(name, blocked=True)
            self._ensure_membership(_DFE_VIEWERS_GROUP, name)
            return True
        self._ensure_membership(_DFE_VIEWERS_GROUP, name)
        return False
