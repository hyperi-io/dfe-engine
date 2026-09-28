#  Project:      dfe-engine
#  File:         auth/bootstrap.py
#  Purpose:      Bootstrap auth stores with default groups, accounts, and roles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bootstrap auth stores for local authentication.

Creates the required directory structure, seeds default groups and the local
accounts, and copies the built-in roles.yaml if missing.

Two accounts are seeded from injected config, through ONE reconcile path:

``admin``      the everyday local admin. Its password comes from the deployment's
               secret store (``DFE_AUTH_LOCAL_ADMIN_PASSWORD``) and is issued with
               a forced change: the admin must replace it at first login, in every
               posture. Until then it is reasserted on every boot. After that it is
               issued again only when the injected value is neither the owner's
               password nor the one last issued (a rotation in the secret store).
``breakglass`` the recovery admin, seeded from a hash committed in the deploy
               repo (:mod:`dfe_engine.auth.breakglass`).

Once the deployment has an admin of its own, the operator retires the bootstrap
admin (:mod:`dfe_engine.auth.admin_retirement`): the seed is skipped, the account
stays disabled, and the injected password may be deleted from the secret store.

Usage::

    from pathlib import Path
    from dfe_engine.auth.bootstrap import bootstrap_auth

    stores = bootstrap_auth(Path("/etc/dfe/auth"))
    account_store, group_store, api_key_store, role_config = stores
"""

import importlib.resources
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.accounts import Account, AccountStore, DocuStoreAccountStore, matches_digest
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.groups import DocuStoreGroupStore, GroupMetrics, GroupStore
from dfe_engine.auth.role_store import RoleStore
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.git_identity import DEFAULT_FALLBACK_DOMAIN
from dfe_engine.settings import is_dev_posture

if TYPE_CHECKING:
    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.settings import SeedAccount

# Default local admin username
_DEFAULT_ADMIN_NAME = "admin"
# The shipped placeholder password. Refused outside a dev posture.
_DEFAULT_PASSWORD = "changeme"
# Floor for any password the operator sets through the API.
MIN_ADMIN_PASSWORD_LENGTH = 12
# Group the local admin belongs to.
_ADMIN_GROUP = "dfe-admins"


class DefaultCredentialsError(RuntimeError):
    """The admin password is unset or the shipped default in a production posture."""


def admin_account_name(override: str = "") -> str:
    """Local admin username: override, else ``admin``."""
    if override and override != _DEFAULT_ADMIN_NAME:
        return override
    return _DEFAULT_ADMIN_NAME


def seeded_account_email(username: str, recovery_email: str = "") -> str:
    """Contact email for a bootstrap-seeded local account.

    ``DFE_RECOVERY_EMAIL`` wins when set; otherwise ``{username}@dfe.local``.
    """
    if recovery_email.strip():
        return recovery_email.strip()
    return f"{username}@{DEFAULT_FALLBACK_DOMAIN}"


def _is_synthetic_fallback_email(email: str, username: str) -> bool:
    """True when *email* is the placeholder ``{username}@dfe.local``."""
    return email == f"{username}@{DEFAULT_FALLBACK_DOMAIN}"


def admin_account_password(override: str = "") -> str:
    """Local admin password: override, else ``changeme``."""
    if override and override != _DEFAULT_PASSWORD:
        return override
    return _DEFAULT_PASSWORD


def default_credentials_in_use(admin_password: str) -> bool:
    """True when the deployment is running on the unset/shipped admin password.

    THE contract for the ``changeme`` check, and the one every repo in the suite
    copies: strip surrounding whitespace, then compare against empty and
    ``changeme``. The strip is what the copies disagree on -- a password arriving
    through a kubernetes Secret, a heredoc or an ``.env`` line keeps its trailing
    newline, so an exact comparison reads ``"changeme\\n"`` as a minted password
    while the login it guards still accepts ``changeme``.
    """
    candidate = admin_password.strip()
    return not candidate or candidate == _DEFAULT_PASSWORD


def admin_on_default_password(
    account_store: AccountStore | DocuStoreAccountStore | None,
    admin_name: str,
    admin_password: str,
) -> bool:
    """Whether the local admin still logs in on the shipped default password, now.

    THE ``default_credentials`` verdict, read fresh wherever it is reported. The
    configured password is what the boot reconcile issues, so it decides until the
    account shows otherwise: an admin that cannot hold a session (retired), or one
    whose owner has replaced its issued password, is off the default whatever
    config still says -- the reconcile does not issue that config value again. Both
    are fields the password-change path writes, so this is a store read, no bcrypt.

    Args:
        account_store: The live account store; None while bootstrap is in flight.
        admin_name: The local admin's username.
        admin_password: The configured admin password.

    Returns:
        True while the default opens the admin account.
    """
    if not default_credentials_in_use(admin_password):
        return False
    account = account_store.get(admin_name) if account_store is not None else None
    if account is None:
        return True
    if account.session_denied() is not None:
        return False
    # Never issued a password, so the next boot issues config's.
    if not account.seeded_password_hash:
        return True
    return account.password_change_required


def require_admin_password(admin_password: str, environment: str, *, retired: bool = False) -> None:
    """Refuse to start on the default admin password outside a dev posture.

    The posture predicate is the one gitops auto-merge gates on
    (:func:`dfe_engine.settings.is_dev_posture`), so a deployment cannot be dev
    enough to auto-merge yet production enough to be refused here, or the reverse.

    ``retired`` lifts the refusal: a retired admin is never seeded, so an absent
    password is the intended end state and the operator has deleted it.

    A dev posture on the default starts with a warning; whether the admin is still
    on it is :func:`admin_on_default_password`, read where it is reported.

    Raises:
        DefaultCredentialsError: production posture with no minted password.
    """
    if retired or not default_credentials_in_use(admin_password):
        return
    if is_dev_posture(environment):
        logger.warning(
            f"Local admin is running on the default password '{_DEFAULT_PASSWORD}' "
            f"(DFE_ENV={environment}); change it before this deployment carries data"
        )
        return
    raise DefaultCredentialsError(
        f"DFE_AUTH_LOCAL_ADMIN_PASSWORD is unset or '{_DEFAULT_PASSWORD}' but "
        f"DFE_ENV is '{environment}': set DFE_AUTH_LOCAL_ADMIN_PASSWORD to the "
        "password your deployment minted (dfe-docker: make init; kubernetes: the "
        "engine Secret), or set DFE_ENV to dev/development/local/test/ci"
    )


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
    group_metrics: GroupMetrics | None = None,
    gitcrud: GitCrud | None = None,
    seed_accounts: list[SeedAccount] | None = None,
    breakglass_password: str = "",
    recovery_email: str = "",
) -> tuple[
    AccountStore | DocuStoreAccountStore,
    GroupStore | DocuStoreGroupStore,
    APIKeyStore,
    RoleStore,
    RoleConfig,
]:
    """Bootstrap auth stores with sensible defaults.

    Creates subdirectories, seeds roles and groups, reconciles the local accounts
    from config, and returns the initialised stores.

    Args:
        auth_dir: Root directory for auth config files.
        default_admin_password: Password for the local admin. Empty falls through
            to ``changeme``.
        default_admin_name: Username for the local admin. Empty falls through
            to ``admin``.
        group_metrics: Where the default group store counts the group files it
            skips. Unused when ``group_store`` is given.
        gitcrud: When gitops is enabled, the deploy-repo engine. The live store is
            hydrated from it before the reconcile, an admin the reconcile created or
            re-hashed is persisted back into it, and the break-glass hash and the
            admin-retirement fact are read from its governance settings.
        seed_accounts: Named accounts reconciled on every boot (config wins), so
            shared team logins survive a teardown+rebuild unchanged (dfe-infra #106).
        breakglass_password: First-boot break-glass password. Minted into the
            deploy repo as a hash when none is committed, ignored thereafter.
        recovery_email: Contact email for the admin and break-glass accounts
            (``DFE_RECOVERY_EMAIL``). Empty falls back to ``{username}@dfe.local``.

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
        account_store = AccountStore(accounts_dir, admin_name=default_admin_name)
    if group_store is None:
        group_store = GroupStore(groups_dir, admin_name=default_admin_name, metrics=group_metrics)
    api_key_store = APIKeyStore(api_keys_dir)

    # Seed default groups if the store has none (backend-agnostic)
    if not group_store.list():
        _seed_groups(group_store)
        logger.info("Seeded default groups")

    # Restore accounts from the durable deploy repo BEFORE the reconcile, so an
    # account the engine persisted survives a rebuild of the live store.
    restored = account_durability.hydrate_from_deploy_repo(gitcrud, account_store)
    if restored:
        logger.info(f"Restored {restored} account(s) from the deploy repo")

    from dfe_engine.auth import admin_retirement
    from dfe_engine.settings import SeedAccount

    # One reconcile path for every config-owned account: the admin goes through the
    # same mechanism as the named seeds, so a rebuild restores the minted credential.
    admin_name = admin_account_name(default_admin_name)
    retired = admin_retirement.is_retired(gitcrud)
    specs = [s for s in (seed_accounts or []) if s.username != admin_name]
    if not retired:
        specs.insert(
            0,
            SeedAccount(
                username=admin_name,
                password=admin_account_password(default_admin_password),
                groups=[_ADMIN_GROUP],
                email=seeded_account_email(admin_name, recovery_email),
            ),
        )
    seeded = _reconcile_seed_accounts(account_store, group_store, specs, issued={admin_name})

    if retired:
        _disable_retired_admin(account_store, admin_name)
    # Publish every boot that writes the admin's credential: an unpublished reset
    # leaves the superseded hash in the deploy repo for hydration to put back, and
    # the next boot resets it again.
    elif admin_name in seeded:
        admin = account_store.get(admin_name)
        if admin is not None:
            account_durability.publish_direct(gitcrud, admin, summary="seed account")

    # The recovery admin: its hash lives in the deploy repo, not in config.
    from dfe_engine.auth import breakglass

    breakglass.seed(
        account_store,
        group_store,
        gitcrud,
        breakglass_password,
        recovery_email=recovery_email,
    )

    return account_store, group_store, api_key_store, role_store, role_config


def _disable_retired_admin(
    account_store: AccountStore | DocuStoreAccountStore,
    admin_name: str,
) -> None:
    """Keep a retired admin disabled, whatever the deploy repo's copy of it says.

    The retirement fact is the authority: an account doc restored from a commit
    made before the retirement is still enabled, and this is what stops that
    rebuild handing the credential back.
    """
    account = account_store.get(admin_name)
    if account is not None and account.enabled:
        # The retirement fact in the deploy repo is what authorises disabling a
        # protected name; the store refuses it from anywhere else.
        account_store.update(admin_name, enabled=False, allow_protected=True)
        logger.info(f"Local admin '{admin_name}' is retired; disabled the account")


def _seed_roles(dest: Path) -> None:
    """Copy built-in roles.yaml to dest."""
    pkg = importlib.resources.files("dfe_engine.auth.resources")
    resource = pkg.joinpath("roles.yaml")
    with importlib.resources.as_file(resource) as src:
        shutil.copy2(src, dest)


def _seed_groups(group_store: GroupStore | DocuStoreGroupStore) -> None:
    """Create default groups."""
    for name, (roles, description) in _DEFAULT_GROUPS.items():
        group_store.create(name, roles=roles, description=description)


def _config_password_wins(
    account_store: AccountStore | DocuStoreAccountStore,
    account: Account,
    password: str,
    *,
    issued: bool,
) -> bool:
    """Whether the reconcile writes the configured *password* over the stored one.

    A named seed, and an issued password its owner has not yet replaced, follow
    config whenever the stored hash no longer verifies. An admin from before the
    flag was never issued its password, so config issues it. Once the owner has
    replaced an issued password, config wins only when it differs from both the
    owner's password and the one last issued -- a rotation in the secret store --
    so a restart keeps the owner's choice, including when the owner's password is
    written back into config.
    """
    if not issued or account.password_change_required:
        return not account_store.verify_password(account.username, password)
    if not account.seeded_password_hash:
        return True
    if account_store.verify_password(account.username, password):
        return False
    return not matches_digest(password, account.seeded_password_hash)


def _reconcile_seed_accounts(
    account_store: AccountStore | DocuStoreAccountStore,
    group_store: GroupStore | DocuStoreGroupStore,
    seed_accounts: list[SeedAccount],
    *,
    issued: set[str] | None = None,
) -> list[str]:
    """Create or reconcile config-owned accounts so config wins on every boot.

    For each spec: create it if absent, else reset the password when config wins
    (:func:`_config_password_wins`) and align its groups to the config. Group
    rosters are reconciled to match exactly -- added to the config's groups,
    removed from any other.

    Args:
        account_store: The live account store.
        group_store: The live group store.
        seed_accounts: The config-owned accounts.
        issued: Usernames whose configured password is issued with a forced
            change -- the owner must replace it at first login.

    Returns:
        The usernames whose stored credential this pass wrote -- created or
        reset. The caller mirrors those into the deploy repo, so the durable copy
        tracks the hash the live store is actually serving.
    """
    issued = issued or set()
    known_groups = {g.name for g in group_store.list()}
    seeded: list[str] = []

    for spec in seed_accounts:
        wanted_groups = []
        for gname in spec.groups:
            if gname in known_groups:
                wanted_groups.append(gname)
            else:
                logger.warning(
                    f"Seed account '{spec.username}' references unknown group '{gname}'; skipped"
                )

        change_required = spec.username in issued
        account = account_store.get(spec.username)
        if account is None:
            account_store.create(
                spec.username,
                spec.password,
                groups=wanted_groups,
                email=spec.email,
                change_required=change_required,
            )
            seeded.append(spec.username)
            logger.info(f"Seeded named account '{spec.username}'")
        else:
            if spec.password and _config_password_wins(
                account_store, account, spec.password, issued=change_required
            ):
                account_store.reset_password(
                    spec.username, spec.password, change_required=change_required
                )
                seeded.append(spec.username)
                logger.info(f"Reconciled password for seed account '{spec.username}'")
            updates: dict[str, object] = {}
            if set(account.groups) != set(wanted_groups):
                updates["groups"] = wanted_groups
            if spec.email and (
                not account.email
                or (
                    account.email != spec.email
                    and not _is_synthetic_fallback_email(spec.email, spec.username)
                )
            ):
                updates["email"] = spec.email
            if updates:
                account_store.update(spec.username, **updates)
                logger.info(f"Reconciled fields for seed account '{spec.username}'")

        # Reconcile the group rosters to match the account's groups exactly.
        wanted = set(wanted_groups)
        for group in group_store.list():
            if group.name in wanted:
                group_store.add_member(group.name, spec.username)
            elif spec.username in group.members:
                group_store.remove_member(group.name, spec.username)

    return seeded
