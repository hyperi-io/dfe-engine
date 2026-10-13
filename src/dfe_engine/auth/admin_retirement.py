#  Project:      dfe-engine
#  File:         auth/admin_retirement.py
#  Purpose:      Retire the bootstrap admin: the deploy-repo fact and its precondition
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Retirement of the bootstrap admin, recorded in the deploy repo.

The deployment mints a random admin password and injects it as
``DFE_AUTH_LOCAL_ADMIN_PASSWORD``. The engine issues that credential with a forced
change at first login and reasserts it whenever the injected value changes
(``auth/bootstrap.py``). That is what makes a rebuild restore the account, and it
is also why the plaintext can never be deleted from the Secret or ``.env`` -- the
engine refuses to start without it, and anyone able to write it can issue
themselves the admin at the next boot.

Retirement ends that. The fact lives beside the break-glass hash in the deploy
repo (``governance/settings/auth.yaml``, key ``admin_retired``), so it survives a
rebuild the same way: with it set, boot skips the admin seed, the account stays
disabled, and the injected password may be deleted. The break-glass account is
the recovery path from then on.

Reversal is deliberately not an API: remove ``admin_retired`` from
``governance/settings/auth.yaml`` in the deploy repo and restart, and the engine
seeds the admin again from the store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dfe_engine.auth.accounts import Account, AccountStore, DocuStoreAccountStore
    from dfe_engine.auth.groups import DocuStoreGroupStore, Group, GroupStore
    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.gitops.repo import PublishResult

# The same governed settings doc the break-glass hash is committed to.
CLASS = "gov_settings"
NAME = "auth"
KEY = "admin_retired"

# The role that makes an account able to run the deployment without the bootstrap
# admin. Group roles resolve to it (dfe-admins), never the account directly.
ADMIN_ROLE = "admin"


def is_retired(crud: GitCrud | None) -> bool:
    """Whether the bootstrap admin has been retired. Fail-soft: unreadable reads false.

    False is the safe answer -- the deployment keeps its seeded admin -- so a
    hand-edited auth.yaml that no longer parses cannot lock an operator out.
    Without gitops there is no deploy repo to record the fact in, so no retirement.
    """
    if crud is None:
        return False
    try:
        value = crud.get(CLASS, NAME).get(KEY, False)
    except ResourceNotFoundError:
        return False
    except Exception as exc:
        logger.warning(
            "auth settings file is unreadable; the admin reads as not retired", error=str(exc)
        )
        return False
    return bool(value)


def set_retired(crud: GitCrud, actor: str) -> PublishResult:
    """Commit the retirement fact. Direct, never routed through a review PR.

    A fact sitting on an unmerged branch would let the next boot seed the admin
    again, so the write that the caller reports as done has to be on main.
    """
    message = build_message(
        CommitContext(ctype="rbac", scope=NAME, summary="retire the bootstrap admin", actor=actor)
    )
    return crud.set_key(CLASS, NAME, KEY, True, actor, message=message)


def _system_admin_group_names(groups: list[Group]) -> set[str]:
    """Names of system-scope groups that carry the admin role."""
    from dfe_engine.auth.groups import GROUP_SCOPE_SYSTEM

    return {g.name for g in groups if ADMIN_ROLE in g.roles and g.scope == GROUP_SCOPE_SYSTEM}


def account_holds_system_admin(
    account: Account,
    groups: list[Group],
    *,
    bindings: Mapping[str, str],
) -> bool:
    """True when *account* holds a system-scope group that carries the admin role."""
    from dfe_engine.auth.membership import groups_held, groups_named

    admin_names = _system_admin_group_names(groups)
    if not admin_names:
        return False
    held = groups_named(groups_held(account, groups, bindings=bindings), groups)
    return any(group.name in admin_names for group in held)


def _usable_admin_session(account: Account) -> bool:
    """Whether *account* can sign in with roles (enabled, unblocked, past issued password)."""
    return account.session_denied() is None and not account.password_change_required


def other_usable_admin_exists(
    account_store: AccountStore | DocuStoreAccountStore | None,
    group_store: GroupStore | DocuStoreGroupStore | None,
    *,
    bindings: Mapping[str, str],
    excluding: str,
) -> bool:
    """True when a usable system-admin account other than *excluding* remains.

    The precondition for deleting an admin-role account: at least one other
    enabled, unblocked account that holds the admin role through a system-scope
    group must stay. Bootstrap admin and ``breakglass`` count when they can
    sign in -- the floor is "someone can still run the deployment", not
    "someone other than the recovery pair".
    """
    if account_store is None or group_store is None:
        return False
    groups = group_store.list()
    if not _system_admin_group_names(groups):
        return False
    for account in account_store.list():
        if account.username == excluding:
            continue
        if not _usable_admin_session(account):
            continue
        if account_holds_system_admin(account, groups, bindings=bindings):
            return True
    return False


def another_admin_exists(
    account_store: AccountStore | DocuStoreAccountStore | None,
    group_store: GroupStore | DocuStoreGroupStore | None,
    admin_name: str,
    *,
    bindings: Mapping[str, str],
) -> bool:
    """True when an account other than the seeded pair can sign in holding the admin role.

    The precondition for retirement, and the hint setup-status reports -- one
    predicate, so the hint never offers what the API refuses. Neither
    the bootstrap admin nor ``breakglass`` counts: retiring onto the recovery
    credential is what this exists to avoid. Only a system-scope group counts --
    an org-scoped group's roles bind inside that org, so its members cannot run
    the deployment. The account must hold that group the way a session does
    (:func:`~dfe_engine.auth.membership.groups_held`, under *bindings*, the
    deployment's ``auth.source_provider_bindings``). It must also be enabled,
    unblocked and past any issued password, since until then its session holds
    no roles.
    """
    from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME

    if account_store is None or group_store is None:
        return False
    groups = group_store.list()
    if not _system_admin_group_names(groups):
        return False
    seeded = {admin_name, BREAKGLASS_USERNAME}
    for account in account_store.list():
        if account.username in seeded or not _usable_admin_session(account):
            continue
        if account_holds_system_admin(account, groups, bindings=bindings):
            return True
    return False
