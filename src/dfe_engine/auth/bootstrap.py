#  Project:      dfe-engine
#  File:         auth/bootstrap.py
#  Purpose:      Bootstrap auth stores with default groups, accounts, and roles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bootstrap auth stores for local authentication.

Creates the required directory structure, seeds default groups and an
admin account, and copies the built-in roles.yaml if missing.

Usage::

    from pathlib import Path
    from dfe_engine.auth.bootstrap import bootstrap_auth

    stores = bootstrap_auth(Path("/etc/dfe/auth"))
    account_store, group_store, api_key_store, role_config = stores
"""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.accounts import AccountStore, DocuStoreAccountStore
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.groups import DocuStoreGroupStore, GroupStore
from dfe_engine.auth.role_store import RoleStore
from dfe_engine.auth.roles import RoleConfig

if TYPE_CHECKING:
    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.settings import SeedAccount

# Default break-glass username
_DEFAULT_ADMIN_NAME = "admin"
# Default password that triggers a startup warning
# Default break-glass password
_DEFAULT_PASSWORD = "changeme"


def admin_account_name(override: str = "") -> str:
    """Break-glass admin username: override, else ``admin``."""
    if override and override != _DEFAULT_ADMIN_NAME:
        return override
    return _DEFAULT_ADMIN_NAME


def admin_account_password(override: str = "") -> str:
    """Break-glass admin password: override, else ``changeme``."""
    if override and override != _DEFAULT_PASSWORD:
        return override
    return _DEFAULT_PASSWORD


# Default group definitions: name -> (roles, description)
_DEFAULT_GROUPS: dict[str, tuple[list[str], str]] = {
    "dfe-admins": (["admin"], "Full administrative access"),
    "dfe-analysts": (["data_analyst"], "Hunt, query, source CRUD"),
    "dfe-viewers": (["data_viewer"], "Dashboard and query access"),
    "dfe-infra": (["infra_admin"], "Service and deployment management"),
}


def bootstrap_auth(
    auth_dir: Path,
    default_admin_password: str = "",
    default_admin_name: str = "",
    account_store: AccountStore | DocuStoreAccountStore | None = None,
    group_store: GroupStore | DocuStoreGroupStore | None = None,
    gitcrud: GitCrud | None = None,
    seed_accounts: list[SeedAccount] | None = None,
) -> tuple[
    AccountStore | DocuStoreAccountStore,
    GroupStore | DocuStoreGroupStore,
    APIKeyStore,
    RoleStore,
    RoleConfig,
]:
    """Bootstrap auth stores with sensible defaults.

    Creates subdirectories, seeds roles/groups/admin account if missing,
    and returns the initialised stores.

    Args:
        auth_dir: Root directory for auth config files.
        default_admin_password: Password for the seeded admin account. Empty
            falls through to ``changeme``.
        default_admin_name: Username for the seeded admin account. Empty falls
            through to ``admin``.
        gitcrud: When gitops is enabled, the deploy-repo engine. The live store is
            hydrated from it before the seed-if-empty check (so a persisted
            break-glass password survives a rebuild), and a freshly seeded admin is
            persisted back into it.
        seed_accounts: Named accounts reconciled on every boot (config wins), so
            shared team logins survive a teardown+rebuild unchanged (dfe-infra #106).

    Returns:
        Tuple of (AccountStore, GroupStore, APIKeyStore, RoleStore, RoleConfig).
    """
    from dfe_engine.auth import account_durability

    accounts_dir = auth_dir / "accounts"
    groups_dir = auth_dir / "groups"
    api_keys_dir = auth_dir / "api-keys"
    rbac_dir = auth_dir.parent / "rbac"

    # Ensure directories exist
    for d in (accounts_dir, groups_dir, api_keys_dir, rbac_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Seed roles.yaml from built-in resource if missing
    roles_path = rbac_dir / "roles.yaml"
    if not roles_path.exists():
        _seed_roles(roles_path)
        logger.info(f"Seeded built-in roles.yaml to {roles_path}")

    # Load role config
    role_store = RoleStore(roles_path)
    role_config = role_store.load_config()

    # Instantiate stores. A caller may inject a backend (e.g. a document store); the
    # default is the YAML file store. Accounts and groups share one backend.
    if account_store is None:
        account_store = AccountStore(accounts_dir)
    if group_store is None:
        group_store = GroupStore(groups_dir)
    api_key_store = APIKeyStore(api_keys_dir)

    # Seed default groups if the store has none (backend-agnostic)
    if not group_store.list():
        _seed_groups(group_store)
        logger.info("Seeded default groups")

    # Restore accounts from the durable deploy repo BEFORE the seed-if-empty check,
    # so a rotated break-glass password survives a rebuild rather than reverting to
    # the shipped default.
    restored = account_durability.hydrate_from_deploy_repo(gitcrud, account_store)
    if restored:
        logger.info(f"Restored {restored} account(s) from the deploy repo")

    # Seed admin account if the store has no accounts yet (backend-agnostic)
    if not account_store.list():
        password = admin_account_password(default_admin_password)
        admin_name = admin_account_name(default_admin_name)
        _seed_admin(account_store, group_store, password, admin_name)
        if password == _DEFAULT_PASSWORD:
            logger.warning(
                f"Admin account seeded with default password '{_DEFAULT_PASSWORD}'"
                " — change in production"
            )
        # Persist the freshly seeded admin so the break-glass credential is durable
        # from the first start, not only after an operator rotates it.
        seeded = account_store.get(admin_name)
        if seeded is not None:
            account_durability.publish_seed(gitcrud, seeded)

    # Reconcile named seed accounts on every boot so a teardown+rebuild restores
    # the shared team logins unchanged (dfe-infra #106). Config wins: unlike the
    # break-glass admin above (seeded only into an empty store), a seed account's
    # password and groups are reasserted every startup, in any store backend.
    if seed_accounts:
        _reconcile_seed_accounts(account_store, group_store, seed_accounts)

    return account_store, group_store, api_key_store, role_store, role_config


def _seed_roles(dest: Path) -> None:
    """Copy built-in roles.yaml to dest."""
    pkg = importlib.resources.files("dfe_engine.auth.resources")
    resource = pkg.joinpath("roles.yaml")
    with importlib.resources.as_file(resource) as src:
        shutil.copy2(src, dest)


def _seed_groups(group_store: GroupStore) -> None:
    """Create default groups."""
    for name, (roles, description) in _DEFAULT_GROUPS.items():
        group_store.create(name, roles=roles, description=description)


def _seed_admin(
    account_store: AccountStore,
    group_store: GroupStore,
    password: str,
    name: str,
) -> None:
    """Create default admin account and add to dfe-admins group."""
    account_store.create(name, password, groups=["dfe-admins"])
    # Also register admin as a member of the dfe-admins group
    group_store.add_member("dfe-admins", name)


def _reconcile_seed_accounts(
    account_store: AccountStore | DocuStoreAccountStore,
    group_store: GroupStore | DocuStoreGroupStore,
    seed_accounts: list[SeedAccount],
) -> None:
    """Create or reconcile named seed accounts so config wins on every boot.

    For each spec: create it if absent, else reset the password when the
    configured one no longer verifies and align its groups to the config. Group
    rosters are reconciled to match exactly -- added to the config's groups,
    removed from any other. The break-glass admin name is skipped so its
    git-backed durability is never clobbered by a reconcile.
    """
    admin_name = admin_account_name()
    known_groups = {g.name for g in group_store.list()}

    for spec in seed_accounts:
        if spec.username == admin_name:
            logger.warning(
                f"Seed account '{spec.username}' collides with the break-glass admin; skipped"
            )
            continue

        wanted_groups = []
        for gname in spec.groups:
            if gname in known_groups:
                wanted_groups.append(gname)
            else:
                logger.warning(
                    f"Seed account '{spec.username}' references unknown group '{gname}'; skipped"
                )

        account = account_store.get(spec.username)
        if account is None:
            account_store.create(spec.username, spec.password, groups=wanted_groups)
            logger.info(f"Seeded named account '{spec.username}'")
        else:
            if spec.password and not account_store.verify_password(spec.username, spec.password):
                account_store.reset_password(spec.username, spec.password)
                logger.info(f"Reconciled password for seed account '{spec.username}'")
            if set(account.groups) != set(wanted_groups):
                account_store.update(spec.username, groups=wanted_groups)
                logger.info(f"Reconciled groups for seed account '{spec.username}'")

        # Reconcile the group rosters to match the account's groups exactly.
        wanted = set(wanted_groups)
        for group in group_store.list():
            if group.name in wanted:
                group_store.add_member(group.name, spec.username)
            elif spec.username in group.members:
                group_store.remove_member(group.name, spec.username)
