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

from scalo.logger import logger

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.role_store import RoleStore
from dfe_engine.auth.roles import RoleConfig

# Default password that triggers a startup warning
_DEFAULT_PASSWORD = "changeme"

# Default group definitions: name -> (roles, description)
_DEFAULT_GROUPS: dict[str, tuple[list[str], str]] = {
    "dfe-admins": (["admin"], "Full administrative access"),
    "dfe-analysts": (["data_analyst"], "Hunt, query, source CRUD"),
    "dfe-viewers": (["data_viewer"], "Dashboard and query access"),
    "dfe-infra": (["infra_admin"], "Service and deployment management"),
}


def bootstrap_auth(
    auth_dir: Path,
    default_admin_password: str = _DEFAULT_PASSWORD,
) -> tuple[AccountStore, GroupStore, APIKeyStore, RoleStore, RoleConfig]:
    """Bootstrap auth stores with sensible defaults.

    Creates subdirectories, seeds roles/groups/admin account if missing,
    and returns the initialised stores.

    Args:
        auth_dir: Root directory for auth config files.
        default_admin_password: Password for the seeded admin account.

    Returns:
        Tuple of (AccountStore, GroupStore, APIKeyStore, RoleStore, RoleConfig).
    """
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
        logger.info("Seeded built-in roles.yaml to %s", str(roles_path))

    # Load role config
    role_store = RoleStore(roles_path)
    role_config = role_store.load_config()

    # Instantiate stores
    account_store = AccountStore(accounts_dir)
    group_store = GroupStore(groups_dir)
    api_key_store = APIKeyStore(api_keys_dir)

    # Seed default groups if groups dir is empty
    if not list(groups_dir.glob("*.yaml")):
        _seed_groups(group_store)
        logger.info("Seeded default groups")

    # Seed admin account if accounts dir is empty
    if not list(accounts_dir.glob("*.yaml")):
        _seed_admin(account_store, group_store, default_admin_password)
        if default_admin_password == _DEFAULT_PASSWORD:
            logger.warning(
                "Admin account seeded with default password '%s'"
                " — change in production (set DFE_AUTH_LOCAL_ADMIN_PASSWORD)",
                _DEFAULT_PASSWORD,
            )

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
) -> None:
    """Create default admin account and add to dfe-admins group."""
    account_store.create("admin", password, groups=["dfe-admins"])
    # Also register admin as a member of the dfe-admins group
    group_store.add_member("dfe-admins", "admin")
