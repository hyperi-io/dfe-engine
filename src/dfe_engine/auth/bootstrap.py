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
import secrets  # stdlib CSPRNG for the first-boot admin password - NOT dfe_engine.secrets
import shutil
from pathlib import Path

from scalo.logger import logger

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.role_store import RoleStore
from dfe_engine.auth.roles import RoleConfig

# Convenience admin password, seeded ONLY in a dev posture when DFE_ADMIN_PASSWORD
# is unset. A non-dev posture NEVER seeds this well-known default - it would be a
# shipped default credential (CWE-1392/CWE-798); it generates a random one instead.
_DEFAULT_PASSWORD = "changeme"

# Entropy for a generated first-boot admin password. token_urlsafe emits ~1.3 chars
# per byte, so 24 bytes -> a 32-char, 192-bit password shown once in the logs.
_GENERATED_PW_BYTES = 24

# Default group definitions: name -> (roles, description)
_DEFAULT_GROUPS: dict[str, tuple[list[str], str]] = {
    "dfe-admins": (["admin"], "Full administrative access"),
    "dfe-analysts": (["data_analyst"], "Hunt, query, source CRUD"),
    "dfe-viewers": (["data_viewer"], "Dashboard and query access"),
    "dfe-infra": (["infra"], "Service and deployment management"),
}


def bootstrap_auth(
    auth_dir: Path,
    default_admin_password: str | None = None,
    *,
    dev_posture: bool = True,
) -> tuple[AccountStore, GroupStore, APIKeyStore, RoleStore, RoleConfig]:
    """Bootstrap auth stores with sensible defaults.

    Creates subdirectories, seeds roles/groups/admin account if missing,
    and returns the initialised stores.

    Args:
        auth_dir: Root directory for auth config files.
        default_admin_password: Password for the seeded admin account, or None when
            the operator did not supply one (DFE_ADMIN_PASSWORD unset). None + dev
            posture falls back to the convenience default; None + non-dev posture
            generates a random one-time password (never a shipped default).
        dev_posture: True for a dev/test/local posture. Only consulted when
            default_admin_password is None - it selects the unset-password fallback.

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

    # Seed admin account if accounts dir is empty. Password choice + logging are
    # handled together so the generated-password line is emitted exactly once, only
    # when a fresh admin is actually seeded.
    if not list(accounts_dir.glob("*.yaml")):
        password, generated = _resolve_admin_password(default_admin_password, dev_posture)
        _seed_admin(account_store, group_store, password)
        _log_admin_seed(password, generated=generated)

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


def _resolve_admin_password(supplied: str | None, dev_posture: bool) -> tuple[str, bool]:
    """Pick the first-boot admin password; return (password, was_generated).

    - supplied (DFE_ADMIN_PASSWORD set): honour it verbatim - the operator's call.
    - unset + dev posture: the convenience default, so local dev stays frictionless.
    - unset + non-dev posture: a random password. Shipping a usable well-known
      default outside dev is CWE-1392 (use of default credentials), so generate one
      and surface it once (see _log_admin_seed) rather than ship 'changeme'.
    """
    if supplied is not None:
        return supplied, False
    if dev_posture:
        return _DEFAULT_PASSWORD, False
    return secrets.token_urlsafe(_GENERATED_PW_BYTES), True


def _log_admin_seed(password: str, *, generated: bool) -> None:
    """Announce the first-boot admin seed - loudly when it was generated.

    A generated password is shown ONCE here (the Jenkins/ArgoCD/Vault bootstrap
    pattern): it is recoverable only from this first-boot log line. The Account
    model carries no must-change marker to force a reset, so the warning tells the
    operator to rotate it immediately. The password is functional output, not prose.
    """
    if generated:
        logger.warning(
            "No DFE_ADMIN_PASSWORD in a non-dev posture: generated a random one-time "
            "admin password. Log in as 'admin' and change it IMMEDIATELY - shown only "
            "once here: %s",
            password,
        )
    elif password == _DEFAULT_PASSWORD:
        logger.warning(
            "Admin account seeded with the well-known default password '%s' - set "
            "DFE_ADMIN_PASSWORD before any real deployment",
            _DEFAULT_PASSWORD,
        )
